"""表单填充：把匹配好的资料真正写进页面里的控件。

只做「按控件类型填值」这一件事，不做判断——填什么由 :mod:`job.apply.matcher` 决定。
刻意不碰提交按钮：企业官网的表单提交后撤不回来，交给人点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from patchright.async_api import Error as PlaywrightError

from job.apply.scanner import FormField
from job.models.search import SearchConfigRow

if TYPE_CHECKING:
    from patchright.async_api import Frame, Locator, Page

    from job.boss.session import BossSession


class _FillError(Exception):
    """填不进去的原因：找不到控件、没有可上传的简历文件……"""


@dataclass
class FillOutcome:
    """一次填表的结果统计。"""

    filled: int = 0
    skipped: int = 0
    failed: int = 0
    fields: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """没有填不进去的字段就算成功（跳过的不算失败）。"""
        return self.failed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "filled": self.filled,
            "skipped": self.skipped,
            "failed": self.failed,
            "fields": self.fields,
        }


class FormFiller:
    """按控件类型把值写进页面：文本框直接填，下拉选一项，单选/多选打勾，文件走上传。"""

    def __init__(self, session: "BossSession") -> None:
        self.session = session

    async def fill(self, page: "Page", fields: list[FormField]) -> FillOutcome:
        """填完所有「已匹配到值」的字段，逐字段记下结果；不提交。"""
        outcome = FillOutcome()
        for item in fields:
            if not item.key:
                outcome.skipped += 1
                continue
            if not item.filled:
                # 匹配上了却没有值：多半是下拉选项对不上，留给用户手动选
                outcome.failed += 1
                item.ok = False
                item.note = item.note or "没有可填的值，请手动填写"
                continue
            try:
                await self._apply(page, item)
            except (PlaywrightError, _FillError) as exc:
                outcome.failed += 1
                item.ok = False
                item.note = str(exc) or "填写失败，请手动填写"
                continue
            outcome.filled += 1
            item.ok = True
            item.note = ""
        outcome.fields = [item.to_dict() for item in fields]
        return outcome

    async def _apply(self, page: "Page", item: FormField) -> None:
        """按类型分派；定位不到控件时抛 ``_FillError``。"""
        locator = await self._locator(page, item)
        if item.is_file:
            path = self._resume_file()
            if path is None:
                raise _FillError("本机没有可上传的简历文件，请先在「简历配置」上传")
            await locator.set_input_files(str(path))
            return
        if item.tag == "select":
            await locator.select_option(item.filled)
            return
        if item.type in ("radio", "checkbox"):
            await locator.check()
            return
        await locator.fill(item.filled)

    async def _locator(self, page: "Page", item: FormField) -> "Locator":
        """先按扫描时打的 ``data-gj-idx`` 找，找不到退回扫描时记下的 CSS 路径。"""
        frame = self._frame(page, item)
        for selector in (item.selector, item.fallback):
            if not selector:
                continue
            locator = frame.locator(selector)
            if await locator.count():
                return locator.first
        raise _FillError("页面上找不到这个控件，页面可能已跳转或刷新")

    @staticmethod
    def _frame(page: "Page", item: FormField) -> "Frame":
        """取字段所在的 frame；frame 已消失时退回主文档。"""
        frames = page.frames
        if 0 <= item.frame < len(frames):
            return frames[item.frame]
        return page.main_frame

    @staticmethod
    def _resume_file() -> Path | None:
        """本机简历原件的路径；没上传过返回 None。"""
        raw = str(SearchConfigRow.load().get("resume_path") or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_file() else None
