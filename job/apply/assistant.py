"""网申助手：串起「打开官网 → 扫描表单 → 填表 → 等确认 → 标记已提交」。

浏览器复用投递用的那个 :class:`~job.boss.session.BossSession`——再开一个会话会
和它抢同一份 Chrome profile。每个岗位一个标签页，用完关掉。

一条硬规矩：**不点提交按钮**。企业官网的表单提交后撤不回来，还有验证码、登录态
这些只有人能处理的门槛，所以最后一步永远由用户自己点，这里只负责记状态。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from patchright.async_api import Error as PlaywrightError

from job.apply.filler import FillOutcome, FormFiller
from job.apply.matcher import FieldMatcher
from job.apply.profile import ApplyProfile
from job.apply.scanner import FormField, FormScanner, guess_company, guess_title
from job.models.application import ApplicationRow
from job.models.job import JobRow
from job.utils import log

if TYPE_CHECKING:
    from patchright.async_api import Page

    from job.boss.session import BossSession


class ApplyError(Exception):
    """网申流程里的可读错误：路由层转成 HTTP 400，前端弹提示。"""


class ApplyAssistant:
    """一个岗位一次网申：开页、扫描、填表、提交（提交这一步只记状态）。"""

    # 打开企业官网的等待上限（毫秒）：官网五花八门，给宽一点
    TIMEOUT = 45_000

    def __init__(self, session: "BossSession") -> None:
        self.session = session
        self.scanner = FormScanner(session)
        self.filler = FormFiller(session)
        # 岗位 uid → 已打开的标签页；页面被关掉或崩溃时会重新开一个
        self._pages: dict[str, Page] = {}

    # --- 对外流程 ---

    async def open_task(self, uid: str, url: str = "") -> dict[str, Any]:
        """建/取任务并打开官网，扫一遍表单；返回任务（含字段对照表）。

        不传 ``uid`` 就是新建一条独立网申任务：用户只给一个企业官网地址，
        公司 / 岗位名从页面上自己认；传 ``uid`` 则是接着已有的那条任务继续。
        """
        uid = (uid or "").strip()
        target = (url or "").strip()
        if not uid:
            if not target:
                raise ApplyError("请先填写企业官网的网申地址（投递表单页）")
            uid = ApplicationRow.create_standalone(target).uid
        else:
            existing = ApplicationRow.get_dict(uid)
            if existing is None and JobRow.get_dict(uid) is None:
                raise ApplyError("网申任务不存在，请先新建一条网申任务")
            # 没传新地址就沿用上次填的；两个都没有说明还没告诉程序去哪儿网申
            target = target or str((existing or {}).get("url") or "").strip()
            if not target:
                raise ApplyError("请先填写这个岗位的网申地址（企业官网的投递表单页）")
            if JobRow.get_dict(uid) is not None:
                ApplicationRow.create(uid, target)
            else:
                ApplicationRow.set_url(uid, target)
        page = await self._page(uid)
        await page.goto(target, wait_until="domcontentloaded", timeout=self.TIMEOUT)
        return await self._scan(page, uid)

    async def scan(self, uid: str) -> dict[str, Any]:
        """重扫当前页面：官网表单常在填了一栏后才展开下一栏。"""
        task = self._task(uid)
        page = await self._page(uid)
        if not str(task.get("url") or "").strip():
            raise ApplyError("请先填写这个岗位的网申地址（企业官网的投递表单页）")
        return await self._scan(page, uid)

    async def fill(self, uid: str, fields: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """把对照表里的值填进页面；``fields`` 是前端改过的行（序号 + 键 + 值）。"""
        task = self._task(uid)
        page = await self._page(uid)
        items = [FormField.from_dict(row, index=pos) for pos, row in enumerate(task["fields"])]
        if not items:
            await self._scan(page, uid)
            task = self._task(uid)
            items = [FormField.from_dict(row, index=pos) for pos, row in enumerate(task["fields"])]
        self._override(items, fields or [])
        outcome = await self.filler.fill(page, items)
        matcher = self._matcher(task)
        matcher.remember(items)
        ApplicationRow.save_fields(uid, [item.to_dict() for item in items])
        if outcome.filled:
            ApplicationRow.set_status(uid, "filled", note=self._fill_note(outcome))
        saved = ApplicationRow.get_dict(uid) or {}
        return {"task": saved, "outcome": outcome.to_dict()}

    async def submit(self, uid: str) -> dict[str, Any]:
        """标记为已提交（用户在浏览器里点了提交之后用），并关掉这个标签页。"""
        self._task(uid)
        saved = ApplicationRow.set_status(uid, "submitted", note="已提交") or {}
        await self.close_page(uid)
        return saved

    async def close_page(self, uid: str) -> None:
        """关掉这个岗位的标签页；没开过就什么都不做。"""
        page = self._pages.pop(uid, None)
        if page is None:
            return
        try:
            await page.close()
        except PlaywrightError:
            pass

    async def delete(self, uid: str) -> bool:
        """删掉任务，顺手关掉它的标签页。"""
        await self.close_page(uid)
        return ApplicationRow.delete_by_uid(uid)

    # --- 内部 ---

    async def _scan(self, page: "Page", uid: str) -> dict[str, Any]:
        """扫页面 → 匹配资料 → 存对照表；遇到验证码把窗口调到屏幕前喊人。"""
        task = self._task(uid)
        gate = await self.scanner.gate(page)
        if gate:
            await self._alert(gate)
        fields = await self.scanner.scan(page)
        await self._matcher(task).match(fields)
        ApplicationRow.save_fields(uid, [f.to_dict() for f in fields], note=gate)
        return {"task": await self._detect(page, uid) or {}, "gate": gate}

    async def _detect(self, page: "Page", uid: str) -> dict[str, Any] | None:
        """从页面认出这是哪家公司、哪个岗位，回填到任务上。"""
        meta = await self.scanner.meta(page)
        return ApplicationRow.set_meta(uid, title=guess_title(meta), company=guess_company(meta))

    def _task(self, uid: str) -> dict[str, Any]:
        task = ApplicationRow.get_dict(uid)
        if task is None:
            raise ApplyError("网申任务不存在，请先新建一条网申任务")
        return task

    def _matcher(self, task: dict[str, Any]) -> FieldMatcher:
        return FieldMatcher(ApplyProfile.load(), domain=str(task.get("domain") or ""))

    async def _page(self, uid: str) -> "Page":
        """复用这个岗位已打开的标签页；已关掉就新开一个。"""
        page = self._pages.get(uid)
        if page is not None and not page.is_closed():
            return page
        page = await self.session.new_page()
        self._pages[uid] = page
        return page

    async def _alert(self, text: str) -> None:
        """遇到验证码这类拦路虎：把窗口调到屏幕前，让用户手动过一下。"""
        await self.session.set_background(False)
        log(f"网申暂停：{text}")

    @staticmethod
    def _override(items: list[FormField], rows: list[dict[str, Any]]) -> None:
        """用前端改过的行覆盖对照表；改过的行记成「手动」。"""
        by_index = {item.index: item for item in items}
        for row in rows:
            try:
                item = by_index.get(int(row.get("index", -1)))
            except (TypeError, ValueError):
                continue
            if item is None:
                continue
            changed = False
            if "key" in row and str(row.get("key") or "") != item.key:
                item.key = str(row.get("key") or "")
                changed = True
            if "filled" in row and str(row.get("filled") or "") != item.filled:
                item.filled = str(row.get("filled") or "")
                changed = True
            if changed:
                item.source = "手动"
                item.confidence = 1.0
                item.ok = True
                item.note = ""

    @staticmethod
    def _fill_note(outcome: FillOutcome) -> str:
        """填表结果的一句话说明。"""
        text = f"已填 {outcome.filled} 项"
        if outcome.skipped:
            text += f"，跳过 {outcome.skipped} 项"
        if outcome.failed:
            text += f"，{outcome.failed} 项需要手动处理"
        return text
