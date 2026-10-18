"""网申链的状态机：把「开页 → 扫描 → 规划 → 人工确认 → 填表」摆成一张图。

为什么要摆成图，而不是照旧顺序调用几个方法：

* **停得下来**。网申不能一口气跑完——AI 出完对照表要停下来等人点头，企业官网的
  提交按钮更是永远只能人点。图里有一个显式的「确认关卡」节点，停在哪儿是画出来的，
  不是藏在某个 ``if`` 里。
* **进得去任意一关**。用户点「重新扫描」「填入浏览器」时不是重跑整条链，而是从
  对应的节点进图（:class:`EntryNode` 按 ``state.stage`` 路由）。每条支路都能单独测。
* **断了能接上**。每过一关往设置表记一笔断点，进程被关掉后再打开，
  :func:`load_stage` 能把上一次没跑完的那一关接着跑。

节点本身只做调度，真正的脏活（开页、扫表、填表）仍旧在
:class:`~job.apply.assistant.ApplyAssistant` 上——这样单测可以直接把宿主换成假的，
不用起浏览器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic_graph import BaseNode, End, Graph, GraphBuilder, GraphRunContext

from job.models.setting import SettingRow

if TYPE_CHECKING:
    from job.apply.assistant import ApplyAssistant


# --- 阶段：一条网申在图上走到哪儿了 -------------------------------------------

# 还没扫过这张表
STAGE_PENDING = "pending"
# 规则扫完了；验证码识图与 AI 规划这两件慢活还没跑（跑在后台）
STAGE_SCANNED = "scanned"
# 对照表就绪，停在确认关卡等人点头——**人工中断点**
STAGE_AWAITING_CONFIRM = "awaiting_confirm"
# 用户确认了清单，可以往页面里填
STAGE_CONFIRMED = "confirmed"
# 填完了（提交那一下永远由用户自己点）
STAGE_FILLED = "filled"

STAGE_LABELS: dict[str, str] = {
    STAGE_PENDING: "待扫描",
    STAGE_SCANNED: "已扫描",
    STAGE_AWAITING_CONFIRM: "待确认",
    STAGE_CONFIRMED: "已确认",
    STAGE_FILLED: "已填表",
}


@dataclass
class ApplyState:
    """一次网申在图上跑到哪儿了；也是节点之间唯一共享的东西。"""

    uid: str
    url: str = ""
    stage: str = STAGE_PENDING
    # 进图时要不要重新导航到官网：新建任务要，原地重扫不要
    navigate: bool = True
    # 慢活（验证码 / AI 规划）挂后台跑：面板先拿到对照表，不用干等
    defer: bool = False
    # 用户在对照表上改过的行，填表时以它为准
    overrides: list[dict[str, Any]] = field(default_factory=list)
    # 用户在「AI 推理（请核对）」里删掉的行（对照表里的序号）：既不填进页面，
    # 也不回流进资料库，还在该站点记一笔拒绝，下次不再推同一条
    rejected: list[int] = field(default_factory=list)
    gate: str = ""
    captcha: dict[str, Any] = field(default_factory=dict)
    outcome: dict[str, Any] = field(default_factory=dict)
    # 走过的节点；出问题时能一眼看出卡在哪一关
    trace: list[str] = field(default_factory=list)

    def step(self, name: str) -> None:
        """记一笔走过的节点。"""
        self.trace.append(name)


# --- 断点：进程被关掉后从哪一关接着跑 -----------------------------------------

_CHECKPOINT_PREFIX = "apply_stage"


def checkpoint_key(uid: str) -> str:
    """某条网申任务在设置表里的断点键。"""
    return f"{_CHECKPOINT_PREFIX}:{uid}"


def load_stage(uid: str) -> str:
    """读出这条任务上次跑到哪一关；没记过就返回空串。"""
    key = checkpoint_key(uid)
    return SettingRow.get_many((key,)).get(key, "") or ""


def save_stage(uid: str, stage: str) -> None:
    """记一笔断点。"""
    SettingRow.put_many({checkpoint_key(uid): stage})


def clear_stage(uid: str) -> None:
    """任务删掉或跑完了，断点也一起清掉。"""
    SettingRow.put_many({checkpoint_key(uid): ""})


# --- 节点 ---------------------------------------------------------------------


class EntryNode(BaseNode[ApplyState, "ApplyAssistant", ApplyState]):
    """按 ``stage`` 决定从哪一关进图。

    这样「重扫」「填表」「接着上次跑」都是同一张图，不需要为每种入口单独写一条
    路径，也不会出现「入口和主流程各改一半」的漂移。
    """

    async def run(
        self, ctx: GraphRunContext[ApplyState, "ApplyAssistant"]
    ) -> "OpenNode | PlanNode | ConfirmNode | FillNode | End[ApplyState]":
        state = ctx.state
        state.step(f"entry:{state.stage}")
        if state.stage == STAGE_SCANNED:
            return PlanNode()
        if state.stage == STAGE_AWAITING_CONFIRM:
            return ConfirmNode()
        if state.stage == STAGE_CONFIRMED:
            return FillNode()
        if state.stage == STAGE_FILLED:
            return End(state)
        return OpenNode()


class OpenNode(BaseNode[ApplyState, "ApplyAssistant", ApplyState]):
    """打开（或切回）企业官网，等表单渲染稳定。"""

    async def run(self, ctx: GraphRunContext[ApplyState, "ApplyAssistant"]) -> "ScanNode":
        state = ctx.state
        state.step("open")
        await ctx.deps._open_page(state)
        state.stage = STAGE_SCANNED
        return ScanNode()


class ScanNode(BaseNode[ApplyState, "ApplyAssistant", ApplyState]):
    """扫表单 + 规则匹配：这一步是快活，一两秒内把对照表摆出来。"""

    async def run(
        self, ctx: GraphRunContext[ApplyState, "ApplyAssistant"]
    ) -> "PlanNode | End[ApplyState]":
        state = ctx.state
        state.step("scan")
        await ctx.deps._scan_fast(state)
        save_stage(state.uid, STAGE_SCANNED)
        if state.defer:
            # 慢活挂后台：面板先拿到对照表，验证码识图与 AI 规划补完再刷新
            ctx.deps._plan_later(state)
            return End(state)
        return PlanNode()


class PlanNode(BaseNode[ApplyState, "ApplyAssistant", ApplyState]):
    """慢活：验证码识图 + AI 兜底规划。跑完对照表就齐了。"""

    async def run(self, ctx: GraphRunContext[ApplyState, "ApplyAssistant"]) -> "ConfirmNode":
        state = ctx.state
        state.step("plan")
        await ctx.deps._plan(state)
        return ConfirmNode()


class ConfirmNode(BaseNode[ApplyState, "ApplyAssistant", ApplyState]):
    """人工确认关卡：停在这儿，等用户看过对照表再点「填入浏览器」。

    这是整条链上唯一的**中断点**。它什么都不填、什么都不点，只是把状态钉在
    「待确认」——用户点确认（走 :data:`STAGE_CONFIRMED` 进图）才会往下走。
    """

    async def run(
        self, ctx: GraphRunContext[ApplyState, "ApplyAssistant"]
    ) -> "End[ApplyState]":
        state = ctx.state
        state.step("confirm")
        state.stage = STAGE_AWAITING_CONFIRM
        save_stage(state.uid, STAGE_AWAITING_CONFIRM)
        return End(state)


class FillNode(BaseNode[ApplyState, "ApplyAssistant", ApplyState]):
    """把对照表填进页面。只填不提交——提交按钮永远留给用户点。

    ``defer`` 时整条填表链挂后台跑：逐栏写、回读、AI 接手补全加起来少说一分钟，
    节点先交回一张「正在填」的空表——前端干等两分钟才拿到响应就会报「请求超时」。
    """

    async def run(self, ctx: GraphRunContext[ApplyState, "ApplyAssistant"]) -> End[ApplyState]:
        state = ctx.state
        state.step("fill")
        await ctx.deps._fill_node(state)
        state.stage = STAGE_FILLED
        save_stage(state.uid, STAGE_FILLED)
        return End(state)


@lru_cache(maxsize=1)
def _graph() -> Graph[ApplyState, Any, EntryNode, ApplyState]:
    """建一次图，之后复用。延迟导入宿主类型，避开与 assistant 的循环依赖。"""
    from job.apply.assistant import ApplyAssistant

    builder = GraphBuilder(
        name="apply",
        state_type=ApplyState,
        deps_type=ApplyAssistant,
        input_type=EntryNode,
        output_type=ApplyState,
        auto_instrument=False,
    )
    builder.add(
        builder.edge_from(builder.start_node).to(EntryNode),
        builder.node(EntryNode),
        builder.node(OpenNode),
        builder.node(ScanNode),
        builder.node(PlanNode),
        builder.node(ConfirmNode),
        builder.node(FillNode),
    )
    return builder.build(validate_graph_structure=False)


async def run(host: "ApplyAssistant", state: ApplyState) -> ApplyState:
    """从 :class:`EntryNode` 进图，跑到停在某一关（或跑完）为止。"""
    return await _graph().run(state=state, deps=host, inputs=EntryNode())


# 阶段从早到晚的顺序：拿它挑「推断值」和「断点」里更靠后的那个。
_STAGE_ORDER = (
    STAGE_PENDING,
    STAGE_SCANNED,
    STAGE_AWAITING_CONFIRM,
    STAGE_CONFIRMED,
    STAGE_FILLED,
)


def _infer(task: dict[str, Any]) -> str:
    """只看落库的事实（状态 + 对照表）能推到哪一关。"""
    status = str(task.get("status") or "")
    if status in ("filled", "submitted"):
        return STAGE_FILLED
    if task.get("fields"):
        return STAGE_AWAITING_CONFIRM
    return STAGE_PENDING


def stage_of(task: dict[str, Any], uid: str = "") -> str:
    """这条任务走到哪一关了（给面板显示用）。

    单看「状态 + 对照表」推不出「用户已经点过确认」——确认只落在断点里，所以
    有断点时取两者中更靠后的那个。断点也只在**领先**时才作数：填完表之后
    ``reopen`` 把状态退回去，显示要跟着退，不能被旧断点拽住。
    """
    inferred = _infer(task)
    saved = load_stage(uid) if uid else ""
    if saved in _STAGE_ORDER and _STAGE_ORDER.index(saved) > _STAGE_ORDER.index(inferred):
        return saved
    return inferred


def stage_label(stage: str) -> str:
    """阶段的中文名。"""
    return STAGE_LABELS.get(stage, stage)
