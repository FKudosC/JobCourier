"""网申助手：串起「打开官网 → 扫描表单 → 填表 → 等确认 → 标记已提交」。

浏览器复用投递用的那个 :class:`~job.boss.session.BossSession`——再开一个会话会
和它抢同一份 Chrome profile。每个岗位一个标签页，用完关掉。

一条硬规矩：**不点提交按钮**。企业官网的表单提交后撤不回来，还有验证码、登录态
这些只有人能处理的门槛，所以最后一步永远由用户自己点，这里只负责记状态。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any, Callable

from patchright.async_api import Error as PlaywrightError

from job.apply import knowledge, pipeline, safety
from job.apply.captcha import CaptchaResult, CaptchaSolver
from job.apply.filler import FillOutcome, FormFiller
from job.apply.locator import FieldLocator
from job.apply.matcher import FieldMatcher, askable
from job.apply.profile import ApplyProfile
from job.apply.scanner import FormField, FormScanner, guess_company, guess_title
from job.apply.vision import VisionChecker, VisionResult
from job.models.application import ApplicationRow, ApplyLearnedRow, ApplyRejectRow
from job.models.job import JobRow
from job.models.setting import ApplyAutoSettings
from job.utils import log

if TYPE_CHECKING:
    from patchright.async_api import Page

    from job.boss.session import BossSession


class ApplyError(Exception):
    """网申流程里的可读错误：路由层转成 HTTP 400，前端弹提示。"""


# 数一数页面里有多少个能填的控件——只用来判断表单渲染完了没有。
# 光数 input / select / textarea 会漏掉一整类页面：组件库的 Select、Cascader、
# DatePicker、Switch 压根不渲染 <input>，整栏只有一个带 role 的 div。这种表单
# 控件数永远是 0，被当成「空壳页」一路等到 SETTLE_MAX_MS 才开工——用户看到的就是
# 「转好几秒才出结果」。这里把带角色的控件也算上（只算看得见的，别把没展开的浮层
# 算进来，否则真控件还没渲染出来就先「稳」了）。
_COUNT_JS = """
() => {
  const role =
    '[role="combobox"], [role="listbox"], [role="tree"], [role="textbox"], ' +
    '[role="searchbox"], [role="spinbutton"], [role="switch"], ' +
    '[aria-haspopup="listbox"], [aria-haspopup="tree"], [aria-haspopup="grid"]';
  let n = document.querySelectorAll('input:not([type="hidden"]), select, textarea').length;
  for (const el of document.querySelectorAll(role)) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 || r.height > 0) n += 1;
  }
  return n;
}
"""

# 只有机器猜出来的库值才允许被官网页面上的真实值纠正：用户自己在配置中心写下的
# （``资料`` / ``自定义``）、简历正文里捞出来的（``简历``），都比页面上的写法可信，
# 官网换个写法（「本科」写成「本科（学士）」）不该把好值盖掉。
_FIXABLE = frozenset({"推算"})

# 比对两个值是不是同一个：去掉空白与全角空格、转小写；数字栏另按数字比。
_SPACES = re.compile(r"[\s\u3000]+")
_DIGITS_ONLY = re.compile(r"\D")


def _alike(a: str, b: str, *, numeric: bool = False) -> bool:
    """两个值算不算同一个（只用来比对，不拿来显示）。"""
    if numeric:
        return _DIGITS_ONLY.sub("", str(a or "")) == _DIGITS_ONLY.sub("", str(b or ""))
    return _SPACES.sub("", str(a or "")).lower() == _SPACES.sub("", str(b or "")).lower()


def _bare(row: dict[str, str]) -> dict[str, str]:
    """把整理时打的 ``_src`` 标记摘掉，还原成能直接落库的条目。"""
    return {key: value for key, value in row.items() if key != "_src"}


class ApplyAssistant:
    """一个岗位一次网申：开页、扫描、填表、提交（提交这一步只记状态）。"""

    # 打开企业官网的等待上限（毫秒）：官网五花八门，给宽一点
    TIMEOUT = 45_000
    # 等表单渲染：最少等 MIN，之后每 STEP 数一次控件，连续 STABLE 次不变就算稳了，
    # 总共最多等 MAX。官网表单多是前端框架渲染出来的——goto 回来时页面只有一个
    # 页头搜索框，表单要等一两秒才由 JS 挂上去（实测某国企官网 0.25s 就 load，
    # 表单 1.4s 才出现）。这里靠「控件数连续几轮不变」等它，而不是靠 load 事件。
    SETTLE_MIN_MS = 1_000
    SETTLE_STEP_MS = 250
    SETTLE_STABLE = 2
    # 只有一两个控件的页面（页头搜索框那种空壳）放宽一点：连续 1.5 秒不变也认，
    # 免得真·单栏表单一直等到 MAX。
    SETTLE_SLOW_STABLE = 6
    # 少于这么多个控件就按「空壳」对待，不轻易收工
    SETTLE_MIN_CONTROLS = 2
    SETTLE_MAX_MS = 6_000

    # --- 逐栏推理：规则与记忆够不着的栏，一栏一栏单独问模型 ---------------------
    # 整表出计划时，模型一张表三四十栏、看着看着就顾此失彼：认得前一半就收工，
    # 后半段整片空着，换个企业又重演一遍。一栏一问，它每次只看这一栏，注意力全在
    # 这儿——这是「网申助手老填不好」的主治。并发几路是为了别让三四十栏的表拖成
    # 半小时；单栏有超时，整批有总预算，到点就停，剩下的栏照旧交回给人手填。
    REASON_WORKERS = 4
    REASON_TIMEOUT = 45.0
    REASON_BUDGET_MS = 180_000

    # --- AI 补全：第一遍填完之后，把还空着的栏再问一轮模型 ---------------------
    # 第一遍（规则 + 记忆 + 逐栏推理）跑完，页面上总还剩几栏：官网选项跟资料对不上、
    # 资料里没有这一条、压根没认出是哪栏。这一轮由模型接手：先在选项里挑一个最贴近
    # 的，再不行就整栏问一次。预算比逐栏推理短——它是收尾，不该让人干等。
    COMPLETE_WORKERS = 4
    COMPLETE_TIMEOUT = 45.0
    COMPLETE_BUDGET_MS = 90_000

    def __init__(
        self,
        session: "BossSession",
        *,
        on_update: Callable[..., None] | None = None,
    ) -> None:
        self.session = session
        self.scanner = FormScanner(session)
        self.filler = FormFiller(session)
        # 后台补全（验证码识图 / AI 兜底）做完后的回调：API 层拿它广播给前端刷新面板
        self.on_update = on_update
        # 岗位 uid → 已打开的标签页；页面被关掉或崩溃时会重新开一个
        self._pages: dict[str, Page] = {}
        # 岗位 uid → 上一次真正导航过去的地址。重开面板时不重复 goto，免得把
        # 用户刚在浏览器里填的东西连同表单进度一起冲掉（用户改了地址才重新导航）
        self._opened: dict[str, str] = {}
        # 挂着的后台补全任务：留个强引用，免得还没跑完就被回收
        self._bg: set[asyncio.Task] = set()
        # 正在后台填表的岗位 uid：同一张页面上不许开第二遍，免得两遍互相覆盖
        self._filling: set[str] = set()

    # --- 对外流程 ---

    async def open_task(
        self, uid: str, url: str = "", *, defer: bool = False
    ) -> dict[str, Any]:
        """建/取任务并打开官网，扫一遍表单；返回任务（含字段对照表）。

        不传 ``uid`` 就是新建一条独立网申任务：用户只给一个企业官网地址，
        公司 / 岗位名从页面上自己认；传 ``uid`` 则是接着已有的那条任务继续。
        ``defer=True``（API 层用）时只做快活——开页、扫控件、规则匹配——立刻
        把对照表交出去，验证码识图与 AI 兜底挂后台补，面板不用干等十几秒。
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
        # 地址齐了才调窗口：用户点这一下就是要去官网上干活，窗口得能看见
        await self._wake_browser(surface=True)
        state = pipeline.ApplyState(
            uid=uid, url=target, stage=pipeline.STAGE_PENDING, navigate=True, defer=defer
        )
        return (await pipeline.run(self, state)).outcome

    async def scan(self, uid: str, *, defer: bool = False) -> dict[str, Any]:
        """重扫当前页面：官网表单常在填了一栏后才展开下一栏。"""
        task = self._task(uid)
        if not str(task.get("url") or "").strip():
            raise ApplyError("请先填写这个岗位的网申地址（企业官网的投递表单页）")
        await self._wake_browser(surface=True)
        # 原地重扫：不重新导航（页面已经开着），从「待扫描」这一关重新进图
        state = pipeline.ApplyState(uid=uid, stage=pipeline.STAGE_PENDING, navigate=False, defer=defer)
        return (await pipeline.run(self, state)).outcome

    async def fill(
        self,
        uid: str,
        fields: list[dict[str, Any]] | None = None,
        rejected: list[int] | None = None,
        *,
        defer: bool = False,
    ) -> dict[str, Any]:
        """把对照表里的值填进页面；``fields`` 是前端改过的行（序号 + 键 + 值）。

        ``rejected`` 是用户在「AI 推理（请核对）」里删掉的行序号：这几行既不填进
        页面，也不回流进资料库。

        ``defer``（API 层用）为真时整条填表链挂后台跑，立刻交回一张「正在填」的空
        表——逐栏写、回读、AI 接手补全加起来少说一分钟，同步等下来前端会先报
        「请求超时」。跑完由 :meth:`_fill_later` 广播给面板刷新。
        """
        self._task(uid)
        # 「填入浏览器」之后用户要盯着官网核对，这一下必须把窗口摆到眼前
        await self._wake_browser(surface=True)
        # 「填入浏览器」就是用户在确认关卡上点头：从「已确认」这一关进图，直接走填表节点
        state = pipeline.ApplyState(
            uid=uid,
            stage=pipeline.STAGE_CONFIRMED,
            navigate=False,
            defer=defer,
            overrides=list(fields or []),
            rejected=[int(i) for i in (rejected or [])],
        )
        return (await pipeline.run(self, state)).outcome

    @staticmethod
    def _handoff(
        rows: list[dict[str, Any]], level: str
    ) -> dict[str, list[dict[str, Any]]]:
        """跑完之后还差什么，分三堆交回给人。

        * ``gaps``：认出了是哪一栏，但「网申资料」里没有对应内容——去资料页补上就能自动填；
        * ``unsure``：AI 兜底填上的，算填上，但请人扫一眼对不对；
        * ``manual``：压根没认出这一栏是什么，程序帮不上忙，只能在浏览器里手填。

        ``level`` 是交付口径：``loose`` 只认必填栏，``balanced``（默认）必填栏加
        AI 兜底，``strict`` 任何没填上的都算。
        """
        gaps: list[dict[str, Any]] = []
        unsure: list[dict[str, Any]] = []
        manual: list[dict[str, Any]] = []
        for row in rows:
            if row.get("hidden"):
                continue
            label = str(
                row.get("label") or row.get("name") or row.get("placeholder") or ""
            ).strip()
            key = str(row.get("key") or "").strip()
            value = str(row.get("filled") or "").strip()
            required = bool(row.get("required"))
            item = {
                "index": row.get("index"),
                "label": label,
                "key": key,
                "type": str(row.get("type") or ""),
                "required": required,
            }
            if value:
                if str(row.get("source") or "") == "AI" and level != "loose":
                    unsure.append(item)
                continue
            if not key:
                if required or level == "strict":
                    manual.append(item)
                continue
            if required or level == "strict":
                gaps.append(item)
        return {"gaps": gaps, "unsure": unsure, "manual": manual}

    def _deliver(
        self,
        uid: str,
        rows: list[dict[str, Any]],
        *,
        note: str | None = None,
    ) -> dict[str, Any] | None:
        """存对照表，顺手按当前交付口径重算一份交付清单。

        对照表一改（重扫、填完、验证码写回），上一份交付清单就过期了。统一在这里
        重算，面板上那份「还差什么」才永远和眼前的对照表对得上。
        """
        handoff = self._handoff(rows, ApplyAutoSettings.load().handoff)
        return ApplicationRow.save_fields(uid, rows, note=note, handoff=handoff)

    async def confirm(self, uid: str) -> dict[str, Any]:
        """用户在对照表上确认了清单：记一笔「已确认」，填表这一步就名正言顺。

        确认本身不动页面——它只是把这条任务在图上往前推一关，之后点「填入浏览器」
        或重启进程都能从这一关接着跑。
        """
        self._task(uid)
        pipeline.save_stage(uid, pipeline.STAGE_CONFIRMED)
        return ApplicationRow.get_dict(uid) or {}

    async def submit(self, uid: str) -> dict[str, Any]:
        """标记为已提交（用户在浏览器里点了提交之后用），并关掉这个标签页。"""
        self._task(uid)
        saved = ApplicationRow.set_status(uid, "submitted", note="已提交") or {}
        pipeline.save_stage(uid, pipeline.STAGE_FILLED)
        await self.close_page(uid)
        return saved

    async def reopen(self, uid: str) -> dict[str, Any]:
        """撤销「已提交」：误点之后退回上一步，可以接着填、接着改。

        已经扫到过表单的退回「已填表」，还没扫过的退回「待网申」；顺手把
        提交时间清掉，免得统计里还把它算成已提交。
        """
        task = self._task(uid)
        status = "filled" if task.get("fields") else "pending"
        # 断点跟着往回退一关：否则旧断点会把「已确认 / 已填表」拽住，显示与实际不符
        pipeline.save_stage(
            uid, pipeline.STAGE_AWAITING_CONFIRM if task.get("fields") else pipeline.STAGE_PENDING
        )
        return ApplicationRow.set_status(uid, status, note="已撤销提交") or {}

    async def close_page(self, uid: str) -> None:
        """关掉这个岗位的标签页；没开过就什么都不做。"""
        self._opened.pop(uid, None)
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
        removed = ApplicationRow.delete_by_uid(uid)
        if removed:
            # 任务没了，断点也别留着——否则新建同 uid 的任务会从旧断点接着跑
            pipeline.clear_stage(uid)
        return removed

    # --- 状态机的宿主接口：节点只做调度，脏活都在这儿 -------------------------
    # 这些方法名是 :mod:`job.apply.pipeline` 里各节点约定好的，改动要两边一起改。

    async def _open_page(self, state: pipeline.ApplyState) -> None:
        """打开（或切回）这个岗位的标签页；``navigate`` 为假时只复用已开的页面。

        已经在这个地址上跑过的，就不再 ``goto`` 一次：官网表单常在填完一栏后跳到
        下一步（地址跟着变），重开面板时再导航一回，会把用户刚在浏览器里填的东西
        连同进度一起冲掉——这正是「重新点填表又从头加载」的根子。只有标签页是新的
        （空白）或用户换了地址，才真正导航。
        """
        page = await self._page(state.uid)
        if not (state.navigate and state.url):
            return
        same = self._opened.get(state.uid) == state.url
        if same and str(getattr(page, "url", "") or "") not in ("", "about:blank"):
            return
        await page.goto(state.url, wait_until="domcontentloaded", timeout=self.TIMEOUT)
        self._opened[state.uid] = state.url

    async def _scan_fast(self, state: pipeline.ApplyState) -> None:
        """等表单渲染稳定；``defer`` 时顺手把快活做完，把对照表交出去。

        ``defer=False`` 只等表单稳定——完整扫描（含验证码识图与 AI 兜底）留给
        :meth:`_plan`，这样两种入口共用同一套「快活 / 慢活」的分工。
        """
        page = await self._page(state.uid)
        await self._settle(page)
        if not state.defer:
            return
        task = self._task(state.uid)
        gate = await self.scanner.gate(page)
        if gate:
            await self._alert(gate)
        fields = await self.scanner.scan(page)
        self._matcher(task).match_rules(fields)
        await self._locate(page, fields)
        # 备注先不动：等后台补完再统一写，免得写两遍还清不干净
        self._deliver(state.uid, [f.to_dict() for f in fields])
        state.gate = gate
        state.outcome = {
            "task": await self._detect(page, state.uid) or {},
            "gate": gate,
            "pending": True,
        }

    async def _plan(self, state: pipeline.ApplyState) -> None:
        """慢活：验证码识图 + AI 兜底匹配；``defer=False`` 时一次把整张表做完。"""
        page = await self._page(state.uid)
        if not state.defer:
            state.outcome = await self._scan_sync(page, state.uid)
            state.gate = str(state.outcome.get("gate") or "")
            return
        await self._finish(page, state.uid, state.gate)

    def _plan_later(self, state: pipeline.ApplyState) -> None:
        """把慢活挂到后台补：面板先拿到对照表，补完再广播刷新。"""
        uid = state.uid
        gate = state.gate

        async def job() -> None:
            page = await self._page(uid)
            await self._finish(page, uid, gate)
            # 慢活跑完对照表就齐了，断点推一关：进程被关掉也能从「待确认」接着来
            pipeline.save_stage(uid, pipeline.STAGE_AWAITING_CONFIRM)

        self._spawn(job())

    async def _fill_node(self, state: pipeline.ApplyState) -> None:
        """填表节点的宿主入口：``defer`` 时把整条链挂后台，先交回一张「正在填」的空表。"""
        if not state.defer:
            await self._fill_now(state)
            return
        if state.uid not in self._filling:
            self._fill_later(state)
        # 后台填表跑完会广播刷新；这里先给面板一张「正在填」的空表，别让它干等
        state.outcome = {
            "task": ApplicationRow.get_dict(state.uid) or {},
            "outcome": {},
            "pending": True,
        }

    def _fill_later(self, state: pipeline.ApplyState) -> None:
        """把填表挂后台：面板立刻拿到响应，填完再广播刷新。

        用一份独立的状态副本跑（``_fill_now`` 只认 ``uid`` / ``overrides`` /
        ``rejected``），免得后台任务和主链抢着改同一个 ``state``。
        """
        uid = state.uid
        sub = pipeline.ApplyState(
            uid=uid,
            stage=pipeline.STAGE_CONFIRMED,
            navigate=False,
            overrides=list(state.overrides),
            rejected=list(state.rejected),
        )
        self._filling.add(uid)

        async def job() -> None:
            try:
                await self._fill_now(sub)
            except Exception as exc:  # noqa: BLE001 后台填表失败只记日志，别把服务带走
                log(f"网申后台填表失败：{exc}")
            finally:
                self._filling.discard(uid)
            self._notify(
                ApplicationRow.get_dict(uid) or {},
                list(sub.outcome.get("inferred") or []),
                dict(sub.outcome.get("outcome") or {}),
            )

        self._spawn(job())

    async def _fill_now(self, state: pipeline.ApplyState) -> None:
        """把对照表填进页面（只填不提交），把结果写回任务与状态。"""
        uid = state.uid
        task = self._task(uid)
        page = await self._page(uid)
        items = [FormField.from_dict(row, index=pos) for pos, row in enumerate(task["fields"])]
        if not items:
            await self._scan(page, uid)
            task = self._task(uid)
            items = [
                FormField.from_dict(row, index=pos) for pos, row in enumerate(task["fields"])
            ]
        self._override(items, state.overrides)
        # 用户删掉的推算行：既不填进页面，也不回流进资料库；在该站点记一笔拒绝，
        # 免得下次重扫又把同一条推出来让人再删一遍
        dropped = self._drop_rejected(items, state.rejected)
        # 文件栏要不要代传，由「网申填表」里的开关说了算（手动填表也照它办）
        self.filler.upload_files = ApplyAutoSettings.load().upload_files
        # 挨个填：一栏一栏地写。整批一次写时，一格卡住（浮层不收起、选项永远不刷新）
        # 就是整张表停在那儿；逐栏写则各自认输、各自往下走，某一栏填不动不牵连别的栏。
        written: list[FormField] = []
        outcome = FillOutcome()
        for item in items:
            outcome.absorb(await self.filler.fill(page, items, only=item, done=written))
        # 整张表填完再收尾一遍：回读每栏的真实值 + 读官网自己弹的红字
        outcome.absorb(await self.filler.settle(page, items))
        matcher = self._matcher(task)
        # 第一遍（规则 + 记忆 + 逐栏推理）没填上的栏：AI 接手再补一轮，补出来的值
        # 当场写进页面。只补空栏——已经填进去的、用户自己改过的一律不碰。
        outcome.absorb(await self._complete(page, items, matcher))
        matcher.remember(items)
        self._learn(task, matcher.profile, items)
        # 用户认可的「AI 推算」行：点「填入浏览器」这一刻落库，带依据与来源站点
        inferred = await self._learn_inferred(task, items, matcher)
        if dropped:
            ApplyRejectRow.remember(str(task.get("domain") or ""), dropped)
        self._deliver(uid, [item.to_dict() for item in items])
        if outcome.filled:
            ApplicationRow.set_status(uid, "filled", note=self._fill_note(outcome))
        state.stage = pipeline.STAGE_FILLED
        state.outcome = {
            "task": ApplicationRow.get_dict(uid) or {},
            "outcome": outcome.to_dict(),
            "inferred": inferred,
        }

    async def _complete(
        self, page: "Page", items: list[FormField], matcher: FieldMatcher
    ) -> FillOutcome:
        """AI 接手补全：第一遍填完之后，把页面上还空着的栏再问一轮模型。

        第一遍跑完还空着的栏有三种：官网选项跟资料对不上（认下了是哪栏，值却排不进
        它的选项）、资料里没有这一条、压根没认出是哪栏。这一轮逐栏再问一次模型：

        * 认下了是哪栏、资料里有值 → 先在本地对齐选项，再请模型在选项里挑一个；
        * 其余的整栏交给模型 → 从资料 / 简历里取值，或按已有资料推算（带依据）。

        补出来的值当场写进页面（这一步才是「操作浏览器补全」）。**只补空栏**：已经
        填进去的一律不碰，用户自己改过的更不碰；提交按钮与文件栏一次都不碰。模型也
        补不出来的栏原样留着，交付清单照旧把它们列成「待手动」交回给人。

        返回这一轮真正写进页面的结果统计；没开通 AI 服务时什么都不做。
        """
        outcome = FillOutcome()
        if not matcher.llm.ready or not matcher.profile:
            return outcome
        pending = [
            item
            for item in items
            if not item.filled
            and not item.hidden
            and not item.is_file
            and not safety.is_irreversible(item)
            and askable(item)
        ]
        if not pending:
            return outcome
        outline = await self._outline(page, matcher)
        rejected = ApplyRejectRow.rejected(str(matcher.domain or ""))
        deadline = time.monotonic() + self.COMPLETE_BUDGET_MS / 1000
        queue = list(pending)
        landed: list[FormField] = []
        started = time.perf_counter()

        async def worker() -> None:
            while queue:
                if time.monotonic() >= deadline:
                    return
                item = queue.pop(0)
                try:
                    await asyncio.wait_for(
                        self._complete_one(item, matcher, outline, rejected),
                        timeout=self.COMPLETE_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    log(f"AI 补全超时（{item.label or item.name}）", tag="apply")
                    continue
                except Exception as exc:  # noqa: BLE001 一栏出岔子不该带走整批
                    log(f"AI 补全出错（{item.label or item.name}）：{exc}", tag="apply")
                    continue
                if item.filled:
                    landed.append(item)

        await asyncio.gather(
            *(worker() for _ in range(max(1, min(self.COMPLETE_WORKERS, len(queue)))))
        )
        matcher.spread(items)
        if landed:
            written: list[FormField] = []
            for item in landed:
                outcome.absorb(await self.filler.fill(page, items, only=item, done=written))
            outcome.absorb(await self.filler.settle(page, items))
        elapsed = (time.perf_counter() - started) * 1000
        log(
            f"AI 补全：待补 {len(pending)} 栏，补上 {len(landed)} 栏，耗时 {elapsed:.0f}ms",
            tag="apply",
        )
        return outcome

    @staticmethod
    async def _complete_one(
        item: FormField, matcher: FieldMatcher, outline: str, rejected: set[str]
    ) -> None:
        """补一栏：资料值对不上选项时先请模型挑一个，认不出是哪栏的整栏交给模型。"""
        key = str(item.key or "")
        profile = matcher.profile
        value = str(profile.get(key) or "") if key and profile else ""
        if value and item.options and await matcher.align_or_ask(item, key, value):
            return
        await matcher.match_one(item, outline=outline, rejected=rejected)

    # --- 内部 ---

    @staticmethod
    def _learn(task: dict[str, Any], profile: ApplyProfile, items: list[FormField]) -> None:
        """把用户手工补的新字段回流进本机资料库：换个官网再遇到就直接有值可填。

        只收「资料库里本来就没有」的那些——用户自定的键，或内置键但本机空着 /
        只从记忆里来的（用户改了内容就以新值覆盖）。敏感事实（身份证、银行卡……）
        不回库，文件栏不回库：它们的值不是一条通用资料。关掉「学习我在浏览器里手填的
        内容」就一条都不收（只填不学）。
        """
        if not ApplyAutoSettings.load().learn_web:
            return
        learned = []
        seen: set[str] = set()
        for item in items:
            if item.source != "手动" or not item.key or not item.filled or item.is_file:
                continue
            # 同义归并：用户补的「民族」「专业类别」这类叫法先归到内置规范键，免得
            # 同一个东西在库里存成两三条；归并不出来（自定字段）就按原键走。
            key = knowledge.canonical_key(item.key) or item.key
            if key in seen:
                continue
            if safety.is_sensitive(key):
                continue
            entry = profile.entry(key)
            if entry is not None and entry.source != "记忆":
                continue
            seen.add(key)
            learned.append({"key": key, "label": item.label, "value": item.filled})
        if learned:
            ApplyLearnedRow.learn(str(task.get("domain") or ""), learned)

    async def _learn_page(
        self, task: dict[str, Any], items: list[FormField]
    ) -> list[dict[str, str]]:
        """把「用户刚在浏览器里手填、资料库里却没有」的栏记进本机资料库。

        和 :meth:`_learn` 分工不同：那边收的是用户在**对照表上**改的值（已经在
        内存里了），这边收的是用户绕过面板、直接在官网页面上填的值——值只活在
        页面里，得先把页面回读一遍（``shown``）才拿得到。所以调用方要保证
        :meth:`~job.apply.filler.FormFiller.readback` 已经跑过。

        规则那一关先拦掉占位提示、按钮字这类明显的垃圾；剩下拿不准的长尾（官网
        自定的怪栏位、把声明正文当值学回来的那种）再交给模型判一遍——
        :meth:`~job.apply.matcher.FieldMatcher.tidy_learned` 未开通 AI 或调用失败
        时原样返回，整理只是加分项，不该让学习本身失败。

        关掉「学习我在浏览器里手填的内容」就一条都不学。

        返回这次新学到的条目（键 / 标签 / 值），调用方拿去给用户看学到了什么。
        """
        if not ApplyAutoSettings.load().learn_web:
            return []
        learned = self._harvest(ApplyProfile.load(), items)
        if learned:
            learned = await self._matcher(task).tidy_learned(learned)
        if learned:
            ApplyLearnedRow.learn(str(task.get("domain") or ""), learned)
            log(f"网申学习：记住 {len(learned)} 项新资料（{'、'.join(i['label'] for i in learned)}）")
        return learned

    async def learn_now(self, uid: str) -> dict[str, Any]:
        """用户点「学习手填内容」：把官网页面和本机资料库逐栏比一遍，学新的、纠错的。

        在「重新扫描 / 填入浏览器」的自动学习之外，给用户一个显式的学习动作——
        他在浏览器里把表单改完、补完，点这一下就当场比对入库：

        * **新增**：页面上有、资料库里没有（或只靠记忆）的栏 → 学进来；
        * **纠正**：库里有值、但页面上的真实值不一样，且那条本就是机器推出来的
          （``推算``）→ 用页面上的值改掉。用户自己在配置中心写下的（``资料`` /
          ``自定义``）、简历正文里捞出来的（``简历``）都比页面写法可信，不动；
        * 官网的占位提示、按钮字、乱码、敏感事实（身份证、银行卡）、文件栏、
          下拉 / 单选 / 日期这些回读会换写法的控件，一律不比不学。

        只按上次存的对照表回读，页面上新冒出来的栏永远读不到，所以先重扫一遍拿全量
        栏位。关掉「学习我在浏览器里手填的内容」时不做任何事（只填不学），返回里带
        ``disabled`` 让前端提示去系统设置里打开。

        返回 ``{"added": [...], "fixed": [...]}``，前端据此报「新学 N 条 / 纠正 M 条」。
        """
        if not ApplyAutoSettings.load().learn_web:
            return {"added": [], "fixed": [], "disabled": True}
        task = self._task(uid)
        started = time.perf_counter()
        page = await self._page(uid)
        fields = await self.scanner.scan(page)
        scanned = time.perf_counter()
        await self.filler.readback(page, fields)
        read = time.perf_counter()
        profile = ApplyProfile.load()
        fresh = self._harvest(profile, fields)
        fixes = self._fixes(profile, fields)
        matcher = self._matcher(task)
        # 新学的和纠错的合成一趟交给模型整理：分两次调要串两趟模型，慢一倍、token
        # 也翻倍。先打 ``_src`` 标记，整理完按标记拆回来——两拨的键天然不相交
        # （「记忆」对「推算」），合并不会互相顶掉。
        marked = [dict(row, _src="added") for row in fresh] + [
            dict(row, _src="fixed") for row in fixes
        ]
        if marked:
            marked = await matcher.tidy_learned(marked)
        # 留一行耗时：慢的时候能一眼看出是卡在扫页面、回读，还是在等模型
        log(
            f"网申学习耗时：扫描 {scanned - started:.2f}s + 回读 {read - scanned:.2f}s"
            f" + 整理 {time.perf_counter() - read:.2f}s（{len(fields)} 栏）"
        )
        fresh = [_bare(row) for row in marked if row.get("_src") == "added"]
        fixes = [_bare(row) for row in marked if row.get("_src") == "fixed"]
        domain = str(task.get("domain") or "")
        if fresh:
            ApplyLearnedRow.learn(domain, fresh)
        if fixes:
            # 纠正来的那几条标成手填（它比推算强，落库后能压住旧值），并留下依据
            ApplyLearnedRow.learn(
                domain,
                [
                    dict(row, source="手动", basis="官网页面值与本机记录不同")
                    for row in fixes
                ],
            )
        # 库里本来没有的算「新增」；库里有值（只是靠记忆 / 推算）的算「纠正」
        added = [row for row in fresh if profile.entry(str(row.get("key") or "")) is None]
        fixed = [row for row in fresh if profile.entry(str(row.get("key") or "")) is not None]
        fixed.extend(fixes)
        if added or fixed:
            names = "、".join(str(row.get("label") or row.get("key")) for row in added + fixed)
            log(f"网申学习：新学 {len(added)} 条、纠正 {len(fixed)} 条（{names}）")
        return {"added": added, "fixed": fixed}

    @staticmethod
    def _drop_rejected(items: list[FormField], rejected: list[int]) -> list[str]:
        """用户在「AI 推理（请核对）」里删掉的行：清空它，别填进页面。

        返回被否掉的资料键（归一化后），交给 :class:`ApplyRejectRow` 记一笔，
        同一站点下次不再推同一条。
        """
        wanted = {int(i) for i in rejected}
        if not wanted:
            return []
        keys: list[str] = []
        for item in items:
            if item.index not in wanted:
                continue
            key = knowledge.canonical_key(item.key) or item.key
            if key and not safety.is_sensitive(key):
                keys.append(key)
            item.filled = ""
            item.key = ""
            item.inferred = False
            item.source = ""
            item.note = ""
            item.confidence = 0.0
            item.ok = False
        return keys

    @staticmethod
    async def _learn_inferred(
        task: dict[str, Any],
        items: list[FormField],
        matcher: FieldMatcher | None = None,
    ) -> list[dict[str, str]]:
        """把用户认可的「AI 推算」行回流进资料库（带依据、来源站点与时间）。

        只在点「填入浏览器」这一刻落库，且只落**仍展示、非空**的行——用户删掉的
        已经在上一步清空了，自然落不进来。关掉「自动学习 AI 推算」就一条都不落。
        敏感事实（身份证、银行卡）与文件栏不收：它们不是一条通用资料。

        落库前还过一遍模型（:meth:`~job.apply.matcher.FieldMatcher.tidy_learned`）：
        判一判这条是不是真资料、有没有跟库里别的条目同义——推出来的东西最容易把官网
        的一句提示、一个按钮字当成资料存进去。未开通 AI 服务时原样落库。
        """
        if not ApplyAutoSettings.load().learn_ai:
            return []
        learned: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in items:
            if not item.inferred or item.hidden or item.is_file:
                continue
            value = str(item.filled or "").strip()
            if not value or not item.key:
                continue
            # 模型把官网的占位提示（「请选择」）当成答案写下来时，别把它当成一条推算
            # 记进库里——那不是用户认可的内容，是官网自己的一句提示。
            if safety.unfilled(item, value) or safety.garbled(value):
                continue
            # 共指归并：模型给的键先归到库里那一条，归不出来就按它自己的键走
            key = knowledge.canonical_key(item.key) or item.key
            if not key or key in seen or safety.is_sensitive(key):
                continue
            seen.add(key)
            learned.append(
                {
                    "key": key,
                    "label": item.label,
                    "value": value,
                    "source": "AI推算",
                    "basis": item.note,
                    "confidence": item.confidence,
                }
            )
        if learned and matcher is not None:
            # AI 判一遍有效性并归纳同义叫法；没开通 AI 服务时原样返回
            learned = await matcher.tidy_learned(learned)
        if learned:
            ApplyLearnedRow.learn(str(task.get("domain") or ""), learned)
            log(
                f"网申学习：认可 {len(learned)} 条 AI 推算"
                f"（{'、'.join(i['label'] for i in learned)}）"
            )
        return learned

    @staticmethod
    def _harvest(profile: ApplyProfile, items: list[FormField]) -> list[dict[str, str]]:
        """从回读到的页面值里挑出「值得记下来」的那些；纯函数，方便单测。

        只认**页面现在真实显示的值**（``shown``，纯本地读 DOM 得到）：下拉读到的
        是官网的选项文字而不是 value 属性，不会把 ``110000`` 这种内部编码当成
        「籍贯」存进资料库。

        不收的几类：文件栏（值不是一个通用资料）、敏感事实（身份证、银行卡，
        界面上本来就掩码）、验证码（一次性的）、提交按钮、**其实没填的栏**
        （下拉的「请选择」占位、按钮或声明栏的文字、把整份选项堆在一起的值），
        以及**资料库里已经有值**的键——那说明这条不是「库里有没对上」，而是官网
        写法不同，不该拿页面上的写法盖掉用户自己填的资料。
        """
        learned: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in items:
            # 推算行另走 :meth:`_learn_inferred`：那条带依据与 ``AI推算`` 来源，
            # 不能被这里当成「用户手填的」记成一条手填资料
            if item.hidden or item.is_file or item.inferred or safety.is_irreversible(item):
                continue
            value = str(item.shown or "").strip()
            # 官网没填的下拉会回读出「请选择」这种占位提示，「点击添加」这种按钮字
            # 也会被采集脚本连人带栏扫进来——它们都不是一条个人资料，学进库里就是
            # 污染（「学到的全是无效信息」的根子）。统一交给 safety 那一关判掉。
            if not value or len(value) > 120 or safety.unfilled(item, value):
                continue
            # 官网响应头漏了 charset 时，中文标签会以「鎵嬫満鍙风爜」这种乱码形态扫进来：
            # 它看着也是汉字，关键词表和占位符名单都拦不住，学进库里纯属污染。
            if safety.garbled(value):
                continue
            label = str(item.label or item.placeholder or item.name or "").strip()
            if safety.garbled(label):
                continue
            # 同义归并：官网写「民族」、资料库里叫「nation」，归到一个键上才不会
            # 存成两条；归并不出来（官网自定的怪栏位）就按标签当新键。
            key = knowledge.canonical_key(item.key or label)
            # 「类型 / 等级 / 称谓 / 开始时间」这类重复块里的从属栏位名，和「意向公司1」
            # 这种带站点编号的键，都指不明一条资料，学下来只会污染资料库。
            if (
                not key
                or key == "captcha"
                or key in seen
                or safety.is_sensitive(key)
                or safety.vague(key)
            ):
                continue
            # 库里已经有「不是靠记忆学来的」值时不动它：那是用户自己填的资料，官网
            # 换个写法不该盖掉。反过来，来源是「记忆」的说明这条本身就是上一轮学来
            # 的，得允许用这次页面上的真实值纠正——否则第一轮学错就再也改不回来。
            entry = profile.entry(key)
            if entry is not None and entry.source != "记忆":
                continue
            seen.add(key)
            learned.append({"key": key, "label": label or key, "value": value})
        return learned

    @staticmethod
    def _fixes(profile: ApplyProfile, items: list[FormField]) -> list[dict[str, str]]:
        """从回读到的页面值里挑出「库里有、但记错了」的那些；纯函数，方便单测。

        只纠**机器推出来的**那条（``推算``）：它是按已有资料猜的，本来就可能猜错，
        用户照着官网页面改对之后，该用页面上的真实值把它换掉。用户自己在配置中心
        写下的、简历正文里捞出来的，都比页面上的写法可信，一律不动——官网换个写法
        （「本科」写成「本科（学士）」）不该盖掉好值。

        只比纯文本 / 数字栏：下拉、单选、日期这些回读时会换写法，拿它纠正只会把好值
        改坏。敏感事实、文件栏、推算行、乱码、占位提示也一律不比。
        """
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in items:
            if item.hidden or item.is_file or item.inferred or safety.is_irreversible(item):
                continue
            # 组件库控件回读会换写法（「本科」↔「本科（学士）」），不拿它纠错
            if (
                item.tag == "select"
                or item.type in ("radio", "checkbox")
                or item.is_fake_select
                or item.is_date_widget
            ):
                continue
            value = str(item.shown or "").strip()
            if not value or len(value) > 120 or safety.unfilled(item, value):
                continue
            if safety.garbled(value):
                continue
            label = str(item.label or item.placeholder or item.name or "").strip()
            if safety.garbled(label):
                continue
            key = knowledge.canonical_key(item.key or label)
            if not key or key in seen or safety.is_sensitive(key) or safety.vague(key):
                continue
            entry = profile.entry(key)
            if entry is None or entry.source not in _FIXABLE:
                continue
            if _alike(entry.value, value, numeric=item.type == "number"):
                continue
            seen.add(key)
            out.append({"key": key, "label": label or key, "value": value})
        return out

    async def _control_count(self, page: "Page") -> int:
        """页面（含 iframe）里能填的控件大概有多少个，用来判断表单渲染完没有。"""

        async def count(frame: Any) -> int:
            try:
                return int(await frame.evaluate(_COUNT_JS))
            except (PlaywrightError, TypeError, ValueError):
                return 0

        # 逐帧串行要等一串往返；iframe 多的站（广告、埋点）会白搭几百毫秒。
        # 数控件彼此无关，并发数完再加总。
        totals = await asyncio.gather(*(count(frame) for frame in page.frames))
        return sum(totals)

    async def _settle(self, page: "Page") -> None:
        """等表单渲染稳定再扫描。

        官网表单大多是前端框架渲染出来的：``goto`` 回来时页面还是个空壳，立刻扫
        只能扫到页头的搜索框（这就是「换个企业又少一大堆栏」的根子）。这里先等
        ``SETTLE_MIN_MS``，之后每 ``SETTLE_STEP_MS`` 数一次控件，连续
        ``SETTLE_STABLE`` 次不变就开工，最多等 ``SETTLE_MAX_MS``。空壳页面
        （只有页头那一个搜索框）按 ``SETTLE_SLOW_STABLE`` 放宽，别误判成「填完了」。
        不用 ``networkidle``——那要等所有请求都静下来，动辄六七秒。
        """
        await page.wait_for_timeout(self.SETTLE_MIN_MS)
        last = -1
        stable = 0
        elapsed = self.SETTLE_MIN_MS
        while elapsed < self.SETTLE_MAX_MS:
            count = await self._control_count(page)
            stable = stable + 1 if count > 0 and count == last else 0
            last = count
            need = (
                self.SETTLE_STABLE
                if count >= self.SETTLE_MIN_CONTROLS
                else self.SETTLE_SLOW_STABLE
            )
            if stable >= need:
                return
            await page.wait_for_timeout(self.SETTLE_STEP_MS)
            elapsed += self.SETTLE_STEP_MS

    async def _scan(self, page: "Page", uid: str, *, defer: bool = False) -> dict[str, Any]:
        """扫页面 → 匹配资料 → 存对照表；``defer`` 时把慢活挂到后台。

        ``defer=True``（API 层用）只做快活——拦路提示、扫控件、规则与记忆匹配
        ——一两秒内把对照表交出去，面板立刻能看；验证码识图与 AI 兜底交给后台
        协程补，补完再广播刷新。``defer=False``（重扫、测试）一次做完。
        """
        await self._settle(page)
        if not defer:
            return await self._scan_sync(page, uid)
        task = self._task(uid)
        gate = await self.scanner.gate(page)
        if gate:
            await self._alert(gate)
        fields = await self.scanner.scan(page)
        self._matcher(task).match_rules(fields)
        await self._locate(page, fields)
        # 备注先不动：等后台补完再统一写，免得写两遍还清不干净
        self._deliver(uid, [f.to_dict() for f in fields])
        self._spawn(self._finish(page, uid, gate))
        return {
            "task": await self._detect(page, uid) or {},
            "gate": gate,
            "pending": True,
        }

    async def _scan_sync(self, page: "Page", uid: str) -> dict[str, Any]:
        """一次做完的完整扫描：拦路 → 验证码 → 扫描 → 匹配 → 存表。"""
        task = self._task(uid)
        gate = await self.scanner.gate(page)
        # 不管有没有拦路虎，只要开着验证码识别就找一遍——官网的验证码多半是
        # 表单里的一栏（不是整页拦截），只等 gate 触发会永远填不上。
        captcha = await self._solve_captcha(page)
        if captcha.get("filled"):
            # 验证码已经自动填上，不再算拦路虎
            gate = ""
        elif captcha.get("detected"):
            if captcha.get("message"):
                await self._alert(str(captcha["message"]))
        elif gate:
            await self._alert(gate)
        fields = await self.scanner.scan(page)
        # 用户可能在浏览器里手工补过栏：先把页面现在的真实值读回来，把资料库里
        # 没有的那些记进资料库，再建 matcher——这样刚学到的这一轮就能直接对上。
        await self.filler.readback(page, fields)
        learned = await self._learn_page(task, fields)
        # 先核对再匹配：视觉补进来的栏也一起走规则，省得下一轮才轮到它
        result = await self._vision(page, fields)
        matcher = self._matcher(task)
        if result is not None and result.added:
            matcher.match_rules(result.added)
        # 规则与记忆先出一版（快活），够不着的栏再一栏一栏交给模型（慢活）
        matcher.match_rules(fields)
        await self._reason(fields, matcher, outline=await self._outline(page, matcher))
        await self._locate(page, fields)
        self._deliver(uid, [f.to_dict() for f in fields], note=self._note(gate, captcha))
        result: dict[str, Any] = {"task": await self._detect(page, uid) or {}, "gate": gate}
        if captcha:
            result["captcha"] = captcha
        if learned:
            result["learned"] = learned
        return result

    async def _finish(self, page: "Page", uid: str, gate: str) -> None:
        """后台补全：验证码识图 + AI 兜底匹配，跑完广播给面板刷新。"""
        try:
            captcha = await self._solve_captcha(page)
            if captcha.get("filled"):
                gate = ""
            elif captcha.get("detected") and captcha.get("message"):
                await self._alert(str(captcha["message"]))
            task = self._task(uid)
            # 用户可能刚在浏览器里手工改过 / 加过栏：重扫一遍当前页面，拿**全量真实
            # 栏位**——只按上一次存的对照表回读，页面上新冒出来的栏永远读不到、也就
            # 永远学不进资料库（「点重新扫描学不到东西」的根子）。重扫之后对照表本身
            # 也和浏览器里眼前的表单一致，不多一栏也不少一栏。
            fields = await self.scanner.scan(page)
            # 回读页面真实值，资料库里没有的记下来，再建 matcher——学到的这几栏
            # 下面 match_rules 就能直接对上。
            await self.filler.readback(page, fields)
            learned = await self._learn_page(task, fields)
            matcher = self._matcher(task)
            matcher.match_rules(fields)
            # 视觉核对放在 AI 兜底之前：它补进来的栏也能被同一轮认出来
            result = await self._vision(page, fields)
            if result is not None and result.added:
                matcher.match_rules(result.added)
            await self._reason(fields, matcher, outline=await self._outline(page, matcher))
            await self._locate(page, fields)
            # 后台补全可能和用户点「填入浏览器」抢同一份对照表：以重扫结果为准，
            # 但把旧表里用户自己动过手的那几行覆盖回来，别把他的改动冲掉。
            current = self._task(uid)
            rows = self._merge_rows(list(current.get("fields") or []), fields)
            if captcha.get("code"):
                self._stash_captcha(rows, str(captcha["code"]))
            note: str | None = self._note(gate, captcha)
            if not note and str(current.get("status") or "") == "filled":
                # 用户刚填完：别把「已填 N 项」那句备注抹掉
                note = None
            self._deliver(uid, rows, note=note)
            self._notify(ApplicationRow.get_dict(uid) or {}, learned)
        except Exception as exc:  # noqa: BLE001 后台补全是加分项，失败只记日志
            log(f"网申后台补全失败：{exc}")

    @staticmethod
    def _ident(item: FormField) -> tuple[int, str]:
        """一栏在页面上的稳定身份：帧号 + 控件选择器。

        选择器是采集脚本按 DOM 顺序打的号（``[data-gj-idx="N"]``），同一个页面上
        一栏一个；加上帧号是因为不同 iframe 各自从 0 数。取不到选择器时退回字段
        签名（标签 + 类型），至少不会把「姓名」和「邮箱」认成同一栏。
        """
        return (item.frame, item.selector or item.signature())

    @classmethod
    def _merge_rows(
        cls, rows: list[dict[str, Any]], fresh: list[FormField]
    ) -> list[dict[str, Any]]:
        """把重扫到的全量栏位并回当前对照表：重扫结果为准，旧表里用户动过手的行盖回来。

        以重扫结果打底，是因为它才是「浏览器里眼前的表单」——用户在官网点「添加」
        加出来的新栏、改过写法的老栏，都以它为准，这样面板才和浏览器一致，不多一栏
        也不少一栏。旧表里 ``source == "手动"`` 的行是用户在面板上点「修改」改过的，
        重扫重建不出来，必须按身份覆盖回去，否则他的改动会被冲掉。

        两边靠 :meth:`_ident` 对上号，不靠序号——用户插一栏进来会把后面的序号整体
        顶位，只看序号会把 A 栏的结论安到 B 栏头上。
        """
        manual: dict[tuple[int, str], FormField] = {}
        for pos, row in enumerate(rows):
            base = FormField.from_dict(row, index=pos)
            if base.source == "手动" and base.key:
                manual.setdefault(cls._ident(base), base)
        out: list[dict[str, Any]] = []
        for item in fresh:
            keep = manual.get(cls._ident(item))
            if keep is not None:
                item.key = keep.key
                item.filled = keep.filled
                item.source = keep.source
                item.confidence = keep.confidence
                item.ok = keep.ok
                item.inferred = keep.inferred
                item.note = keep.note or item.note
            out.append(item.to_dict())
        return out

    @staticmethod
    def _note(gate: str, captcha: dict[str, Any]) -> str:
        """备注只留「要人处理」的事：拦路虎，或页面上有验证码但这次没自动填上。"""
        note = gate
        if not note and captcha.get("detected") and not captcha.get("filled"):
            note = str(captcha.get("message") or "")
        return note

    def _spawn(self, coro: Any) -> None:
        """挂一个后台补全任务；留强引用，免得还没跑完就被回收。"""
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _notify(
        self,
        task: dict[str, Any],
        learned: list[dict[str, str]] | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> None:
        """后台补全跑完，回调 API 层广播给前端刷新面板。

        ``learned`` 是这一趟新记进资料库的条目，一并带上——面板据此提示用户
        「你刚在浏览器里填的那几栏，助手已经记住了」。

        ``outcome`` 是后台填表这一趟的结果统计，面板拿它提示「已填几项、几项待手动」。
        """
        if self.on_update is None:
            return
        try:
            self.on_update(task, learned or [], outcome or {})
        except Exception as exc:  # noqa: BLE001 广播失败不该影响任务本身
            log(f"网申进度广播失败：{exc}")

    async def solve_captcha(self, uid: str) -> dict[str, Any]:
        """手动触发一次验证码识别（不重扫整张表）；识出的码写回对照表那一行。"""
        task = self._task(uid)
        page = await self._page(uid)
        result = await self._solve_captcha(page)
        rows = [dict(row) for row in (task.get("fields") or [])]
        code = str(result.get("code") or "")
        saved: dict[str, Any] | None = None
        if code and self._stash_captcha(rows, code):
            saved = self._deliver(uid, rows)
        elif result.get("message"):
            saved = self._deliver(uid, rows, note=str(result["message"]))
        if saved is not None:
            self._notify(saved)
        return {"captcha": result, "task": saved or ApplicationRow.get_dict(uid) or task}

    @staticmethod
    def _stash_captcha(rows: list[dict[str, Any]], code: str) -> bool:
        """把识出的验证码写进对照表里「验证码」那一行，随其他信息一起填入。"""
        for row in rows:
            text = " ".join(
                str(row.get(field) or "")
                for field in ("label", "name", "placeholder", "key")
            )
            if "验证码" in text or "captcha" in text.lower():
                row["key"] = row.get("key") or "captcha"
                row["filled"] = code
                row["source"] = "AI"
                row["confidence"] = 0.9
                row["ok"] = True
                row["note"] = ""
                return True
        return False

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

    async def _outline(self, page: "Page", matcher: FieldMatcher) -> str:
        """AI 兜底要用的可访问性大纲；没开 AI 服务就不白算这一趟。"""
        if not matcher.llm.ready or not matcher.profile:
            return ""
        return await self.scanner.outline(page)

    async def _reason(
        self, items: list[FormField], matcher: FieldMatcher, *, outline: str = ""
    ) -> int:
        """逐栏推理：把规则与记忆没认领的栏，一栏一栏交给模型判，判一栏落一栏。

        只挑「还空着、也值得问」的栏（:func:`~job.apply.matcher.askable`）：规则已经
        认领的、页面上已经填着值的、文件栏与提交按钮都不问——省下的时间留给真正
        认不出的那些。取值规矩、推算标记全在
        :meth:`~job.apply.matcher.FieldMatcher.match_one` 里，这里只管挑栏、限时、
        并发，返回这一趟认领了几栏。
        """
        if not matcher.llm.ready or not matcher.profile:
            return 0
        pending = [
            item for item in items if askable(item) and not (item.key and item.filled)
        ]
        if not pending:
            return 0
        # 「用户在这个站点删过的推算」一次取齐：同一趟里这份名单不会变，逐栏去查库
        # 既慢又没意义。
        rejected = ApplyRejectRow.rejected(str(matcher.domain or ""))
        deadline = time.monotonic() + self.REASON_BUDGET_MS / 1000
        queue = list(pending)
        landed = 0
        started = time.perf_counter()

        async def worker() -> None:
            nonlocal landed
            while queue:
                if time.monotonic() >= deadline:
                    return
                item = queue.pop(0)
                try:
                    summary = await asyncio.wait_for(
                        matcher.match_one(item, outline=outline, rejected=rejected),
                        timeout=self.REASON_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    log(f"网申逐栏推理超时（{item.label or item.name}）", tag="apply")
                    continue
                except Exception as exc:  # noqa: BLE001 一栏出岔子不该带走整批
                    log(
                        f"网申逐栏推理出错（{item.label or item.name}）：{exc}", tag="apply"
                    )
                    continue
                if summary:
                    landed += 1

        await asyncio.gather(
            *(worker() for _ in range(max(1, min(self.REASON_WORKERS, len(queue)))))
        )
        matcher.spread(items)
        elapsed = (time.perf_counter() - started) * 1000
        log(
            f"网申逐栏推理：待判 {len(pending)} 栏，认领 {landed} 栏，耗时 {elapsed:.0f}ms",
            tag="apply",
        )
        return landed

    async def _locate(self, page: "Page", fields: list[FormField]) -> None:
        """规则与文字版 AI 都没认出的少数栏位，截图交给多模态模型兜底。

        默认关闭；开着也只处理几个栏位、每个都有超时，认不出就回落「请手动填写」。
        这里同样只记日志不弹窗——窗口归用户管。
        """
        locator = FieldLocator(ApplyProfile.load())
        if not locator.enabled:
            return
        try:
            await locator.locate(page, fields)
        except Exception as exc:  # noqa: BLE001 兜底是加分项，失败不该拖垮扫描
            log(f"表单定位兜底失败：{exc}")

    async def _vision(self, page: "Page", fields: list[FormField]) -> VisionResult | None:
        """视觉核对：把扫出来的栏位编号画在截图上，请多模态模型核对多没多少没少。

        各家官网的表单结构千差万别，DOM 启发式扫出来的清单总会有出入。这里用
        「核对」而不是「从零列举」：模型只看框对不对、有没有漏框，便宜也不容易漏。

        跟 :meth:`_locate` 一样，这是加分项——超时、模型不支持视觉、返回的不是
        JSON，都直接沿用 DOM 扫出来的那份，只记日志，绝不弹窗。
        """
        checker = VisionChecker()
        if not checker.enabled:
            return None
        try:
            result = await checker.check(page, fields)
        except Exception as exc:  # noqa: BLE001 核对失败不该拖垮扫描
            log(f"表单视觉核对失败：{exc}")
            return None
        if result.message:
            log(f"表单视觉核对：{result.message}")
        return result

    async def _page(self, uid: str) -> "Page":
        """复用这个岗位已打开的标签页；已关掉就新开一个。"""
        page = self._pages.get(uid)
        if page is not None and not page.is_closed():
            return page
        page = await self.session.new_page()
        self._pages[uid] = page
        return page

    async def _wake_browser(self, *, surface: bool = False) -> None:
        """解开「用户点过关闭浏览器」的封锁；``surface=True`` 时把窗口调到屏幕前。

        用户在别处点过「关闭浏览器」，之后又回来点「填表」——这时浏览器是锁着的，
        ``open()`` 会直接抛「已被手动关闭」，页面开不出来。网申是用户主动发起的
        动作，先把锁解开让页面能正常打开。

        只有用户自己在面板上点的动作（开官网 / 重扫 / 填表）才 ``surface=True``：
        他接下来要盯着官网核对填进去的东西，窗口不摆到眼前反而要来回找。后台补全
        这类自己跑的轮次不调窗口——那正是「别打断前台」的场景。摆窗口失败只记日志：
        它是个加分项，不该把填表本身拖垮。

        走 :meth:`~job.boss.session.BossSession.surface` 而不是 ``set_background``：
        后者会把「后台静默运行」这个设置一起改掉，之后每个查询任务都把窗口顶到
        用户面前。用户只是想看一眼，不是想关掉后台运行。
        """
        self.session.resume()
        if not surface:
            return
        try:
            await self.session.surface()
        except Exception as exc:  # noqa: BLE001 调窗口失败不该拖垮填表
            log(f"调出浏览器窗口失败：{exc}")

    async def _alert(self, text: str) -> None:
        """遇到验证码这类拦路虎：只记一笔，绝不把窗口弹到用户眼前。

        窗口归用户管：他点「查看浏览器」才调出来。任务自己弹窗会打断正在
        干活的人，拦路提示由工作台的日志和网申面板上的横幅告诉用户。
        """
        log(f"网申暂停：{text}")

    async def _solve_captcha(self, page: "Page") -> dict[str, Any]:
        """让 AI 读图识别验证码并填进输入框；没开这个功能就静默跳过。"""
        solver = CaptchaSolver()
        if not solver.enabled:
            return {}
        try:
            result: CaptchaResult = await solver.solve(page)
        except Exception as exc:  # noqa: BLE001 识图是加分项，失败不该拖垮扫描
            return CaptchaResult(message=f"验证码识别失败：{exc}").to_dict()
        return result.to_dict()

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
        if outcome.flagged:
            text += f"，官网提示 {outcome.flagged} 项要核对"
        return text
