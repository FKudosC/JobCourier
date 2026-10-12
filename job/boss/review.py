"""AI 岗位复核：关键词过滤通过后，让大模型识别骗子岗位、判断意图并按维度给匹配分。

评分采用「模型给分项、程序做决定」：大模型只输出各维度的分数与原文证据、
缺失的硬性要求，总分由程序求和并按规则封顶，是否投递也由程序判定——
防止模型算错数、虚高分或一票否决。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from functools import cached_property
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import AgentRunError, UnexpectedModelBehavior
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from job.boss.salary import SalaryRange, fit_summary
from job.models.setting import LlmSettings
from job.utils import telemetry
from job.utils.llm import (
    MAX_OUTPUT_TOKENS,
    MAX_OUTPUT_TOKENS_CEILING,
    ErrorKind,
    classify,
    extract_json,
)

if TYPE_CHECKING:
    from job.boss.jobs import Job

# 中文维度名 → 维度键（个别模型不按 key 输出时兜底）
_DIM_ALIASES: dict[str, str] = {
    "核心职责匹配": "duty",
    "核心职责": "duty",
    "职责": "duty",
    "可迁移证据": "evidence",
    "可迁移": "evidence",
    "证据": "evidence",
    "硬性要求": "hard",
    "硬性": "hard",
    "工具与技术栈": "stack",
    "工具": "stack",
    "技术栈": "stack",
    "实际条件": "conditions",
    "条件": "conditions",
}


def parse_review(text: str) -> RawReview:
    """把模型回复解析成评分素材：宽容提取 JSON、维度名与结构变体都兜住。"""
    data = extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("回复的 JSON 不是对象")
    try:
        return RawReview.model_validate(data)
    except ValidationError:
        coerced = _coerce_dimensions(data)
        if coerced is None:
            raise
        return RawReview.model_validate(coerced)


def _coerce_dimensions(data: dict) -> dict | None:
    """处理常见的结构变体：dimensions 给成 {维度: 分数} 的字典、或条目里 key 缺失。

    认不出的维度名丢弃，认出但缺 evidence 的补空串；实在不行返回 None 让上层报错。
    """
    dims = data.get("dimensions")
    if isinstance(dims, dict):
        items = []
        for name, score in dims.items():
            key = _DIM_ALIASES.get(str(name).strip(), str(name).strip())
            items.append({"key": key, "score": score, "evidence": ""})
        return {**data, "dimensions": items}
    if isinstance(dims, list):
        items = []
        for item in dims:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or item.get("dimension") or "").strip()
            key = _DIM_ALIASES.get(key, key)
            if "score" not in item and "分" not in item:
                continue
            items.append(
                {
                    "key": key,
                    "score": item.get("score", item.get("分")),
                    "evidence": str(item.get("evidence") or item.get("依据") or ""),
                }
            )
        if not items:
            return None
        return {**data, "dimensions": items}
    return None


class DimScore(BaseModel):
    """一个评分维度的分数与证据（模型输出）。"""

    key: str = Field(description="维度标识：duty / evidence / hard / stack / conditions")
    score: int = Field(description="该维度得分，不超过它的满分")
    evidence: str = Field(
        default="",
        description="60 字内的原文依据：摘录 JD 或简历里的短句；讲不出依据就写「未体现」",
    )


class DimVerdict(BaseModel):
    """一个维度的展示信息：程序按 DIMENSIONS 补全名称与满分。"""

    key: str
    label: str
    score: int
    max: int
    evidence: str


# 文档字符串与字段说明会作为输出要求发给模型
class RawReview(BaseModel):
    """模型输出的评分素材：只给事实与分项，不做投递决定。"""

    scam_ok: bool = Field(
        default=True,
        description="未开启骗子识别时恒为 true；否则判断岗位是否正规，疑似诈骗/传销为 false",
    )
    intent_ok: bool = Field(
        default=True,
        description="未开启岗位意图判断时恒为 true；否则岗位命中求职方向里明确排除的方向为 false",
    )
    dimensions: list[DimScore] = Field(
        description="五个维度都要给：duty / evidence / hard / stack / conditions"
    )
    hard_gaps: list[str] = Field(
        default_factory=list,
        description=(
            "岗位详情里明确必备、但简历里未体现或明确不满足的要求，每条 20 字内；"
            "「优先」「加分」不算必备，待确认的意愿（如到岗时间）也不算"
        ),
    )
    reason: str = Field(default="", description="20 字以内的中文结论，说明为什么合适或不合适")


class Verdict(BaseModel):
    """岗位复核结论（程序判定后的最终结果）。"""

    match: bool = Field(description="岗位是否通过全部复核项")
    reason: str = Field(
        description="20 字以内的中文理由：通过时说明为什么合适，不通过时说明哪一项不满足"
    )
    score: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="简历技术匹配度 0–100（程序求和并封顶后），没给简历时为 null",
    )
    dimensions: list[DimVerdict] = Field(
        default_factory=list, description="五个维度的得分明细，给详情页展示"
    )
    hard_gaps: list[str] = Field(default_factory=list, description="岗位必备但简历未体现的要求")

    @property
    def detail(self) -> str:
        """存库的评分明细 JSON。"""
        import json

        return json.dumps(
            {"dimensions": [d.model_dump() for d in self.dimensions], "hard_gaps": self.hard_gaps},
            ensure_ascii=False,
        )


class JobReviewer(BaseModel):
    """用 Pydantic AI 调大模型复核岗位；接口地址、API Key 与模型来自配置中心「AI 服务」。

    三项复核可单独或同时开启，同时开启时一次调用判断：
    ``scam_block`` 骗子岗位识别，``requirement`` 岗位意图（目标岗位要求），
    ``resume`` 简历技术匹配（五维评分）。
    """

    BASE: ClassVar[str] = (
        "你是求职助手的岗位评估员，负责给出评估素材，最终是否投递由程序判定。"
        "评估时先读简历，再读岗位详情，逐条核对，不要臆测简历里没有的经历。"
        "无论被问了什么，只输出一个 JSON 对象，不要 markdown 代码块标记，"
        "不要任何解释文字，结构如下：\n"
        '{"scam_ok": true, "intent_ok": true, '
        '"dimensions": ['
        '{"key": "duty", "score": 30, "evidence": "依据原文短句"}, '
        '{"key": "evidence", "score": 20, "evidence": "…"}, '
        '{"key": "hard", "score": 12, "evidence": "…"}, '
        '{"key": "stack", "score": 8, "evidence": "…"}, '
        '{"key": "conditions", "score": 9, "evidence": "…"}], '
        '"hard_gaps": [], "reason": "20字以内结论"}'
        "（dimensions 固定为 duty / evidence / hard / stack / conditions 五项，"
        "score 不超过各维度满分 40 / 25 / 15 / 10 / 10）"
    )
    SCAM: ClassVar[str] = (
        "骗子岗位识别（输出 scam_ok）：判断这条招聘信息是否是诈骗、传销，或只收简历不招人的虚假岗位。"
        "命中以下任一特征即 scam_ok=false："
        "要求刷单、打字、点赞、关注返现；"
        "入职前收取培训费、押金、服装费、体检费、中介费或保证金；"
        "以「储备干部」「管理培训生」为名实为拉人头、发展下线；"
        "通篇只有「高薪」「月入过万」「零经验可」「包吃住」这类空泛承诺，没有具体职责；"
        "要求先加微信、下载指定 App、扫码填表或提交身份证才能面试；"
        "职责与岗位名称明显不符（如招运营实为销售）；"
        "招聘主体与公司名对不上。"
        "判断要保守：正规企业的高薪岗、应届生培养岗、管理培训生都不算骗子，"
        "只有特征明确时才判 false，模棱两可就判 true。"
    )
    INTENT: ClassVar[str] = (
        "岗位意图判断（输出 intent_ok）：对照目标岗位要求，只看岗位名称和岗位职责，"
        "命中明确排除的方向才判 false；要求里没排除的方向一律 true。"
    )
    SCORING: ClassVar[str] = (
        "评分规则（输出 dimensions 与 hard_gaps）：\n"
        "1. 每个维度先在 evidence 里摘录 JD 或简历原文短句作为依据，再给分；"
        "给不出依据的高分会被程序压低。\n"
        "2. 区分四种情况：已证明满足 / 有相邻证据 / 未体现 / 明确不满足——"
        "「未体现」不等于「不满足」，只给部分分，不给零分也不给满分。\n"
        "3. 同一个缺口只在最相关的维度扣一次分；要求是「A 或 B」时满足任一即可；"
        "「优先」「加分」项不是必备，不满足不扣分。\n"
        "4. 薪资只影响 conditions 维度，薪资结论已单独给出，不要自己换算。\n"
        "5. hard_gaps 只列岗位详情里明确必备、简历未体现或明确不满足的项；"
        "没有就给空列表，不要为了凑数硬填。"
    )
    # 五个评分维度：标识 → (名称, 满分)
    DIMENSIONS: ClassVar[dict[str, tuple[str, int]]] = {
        "duty": ("核心职责匹配", 40),
        "evidence": ("可迁移证据", 25),
        "hard": ("硬性要求", 15),
        "stack": ("工具与技术栈", 10),
        "conditions": ("实际条件", 10),
    }
    # 硬性要求缺 1/2/≥3 项时的总分封顶：缺得越多，软能力满分也盖不住核心缺口
    GAP_CAPS: ClassVar[tuple[tuple[int, int], ...]] = ((3, 64), (2, 74), (1, 84))
    # 证据缺失时单维度得分压到满分的这个比例
    NO_EVIDENCE_RATIO: ClassVar[float] = 0.6
    # 限流 / 网络错误后的重试间隔（秒）
    RETRY_DELAY: ClassVar[float] = 3.0
    # 输出解析失败后的纠错重试次数
    PARSE_RETRIES: ClassVar[int] = 3

    requirement: str = ""
    resume: str = ""
    # 期望薪资区间（月薪 K）；None 表示没有设定
    salary_range: SalaryRange | None = None
    scam_block: bool = False
    # 最低匹配度；None 表示不按匹配度过滤
    min_score: int | None = None
    llm: LlmSettings = Field(default_factory=LlmSettings)
    timeout: float = 30
    # 输出 token 上限；思考型模型烧完上限会自动加倍（见 review）
    max_tokens: int = MAX_OUTPUT_TOKENS

    @classmethod
    def from_config(
        cls, config: Mapping[str, Any], llm: LlmSettings
    ) -> JobReviewer | None:
        """求职配置开启了至少一项复核（且内容非空）时返回复核器，否则返回 None。

        岗位方向匹配已由 AI 关键词研判 + 五维评分承担，这里不再读求职方向。
        """
        resume = str(config.get("resume_text") or "").strip()
        resume = resume if config.get("resume_match") else ""
        scam = bool(config.get("scam_block"))
        if not resume and not scam:
            return None
        min_score = config.get("min_score") if config.get("score_filter") else None
        low, high = float(config.get("salary_min") or 0), float(config.get("salary_max") or 0)
        salary = SalaryRange.monthly(low, high) if low > 0 or high > 0 else None
        return cls(
            resume=resume,
            salary_range=salary,
            scam_block=scam,
            min_score=min_score,
            llm=llm,
        )

    @property
    def checks(self) -> list[str]:
        """已开启的复核项名称。"""
        names = (
            ("屏蔽骗子岗位", self.scam_block),
            ("AI 岗位筛选", self.requirement),
            ("简历技术匹配", self.resume),
            (f"匹配度 ≥{self.min_score}", self.resume and self.min_score is not None),
        )
        return [name for name, on in names if on]

    @property
    def instructions(self) -> str:
        """按已开启的复核项拼系统提示词。"""
        rules = (
            (self.SCAM, self.scam_block),
            (self.INTENT, self.requirement),
            (self.SCORING, bool(self.resume)),
        )
        return "\n".join([self.BASE, *(rule for rule, on in rules if on)])

    @cached_property
    def agent(self) -> Agent[None, str]:
        """复核 Agent：纯文本输出，JSON 的提取与校验由 ``parse_review`` 宽容处理。

        不用 ``PromptedOutput`` 强约束结构——不少模型（尤其兼容接口）会包 markdown
        围栏或漏字段，结构化输出模式直接失败；纯文本 + 程序解析 + 纠错重试稳得多。
        """
        model = OpenAIChatModel(self.llm.model, provider=self.llm.provider)
        agent = Agent(
            model,
            output_type=str,
            system_prompt=self.instructions,
            model_settings=OpenAIChatModelSettings(
                thinking=False, temperature=0, timeout=self.timeout, max_tokens=self.max_tokens
            ),
        )
        return telemetry.attach(agent)

    async def check(self, job: Job) -> Verdict:
        """复核岗位：是否合适、原因与匹配度（含最低匹配度判断）。

        未开启简历技术匹配时匹配度为 None；接口出错时抛 ``AgentRunError``。
        """
        raw = await self.review(job)
        hard_gaps = raw.hard_gaps[:5] if self.resume else []
        dimensions = self._normalize(raw.dimensions, hard_gaps)
        score = sum(d.score for d in dimensions) if self.resume else None
        reason = raw.reason or "符合要求"

        if self.scam_block and not raw.scam_ok:
            return self._verdict(False, reason, score, dimensions, hard_gaps)
        if self.requirement and not raw.intent_ok:
            return self._verdict(False, reason, score, dimensions, hard_gaps)
        if score is not None:
            if self.min_score is not None and score < self.min_score:
                return self._verdict(
                    False, f"匹配度 {score} 分，低于 {self.min_score} 分", score, dimensions, hard_gaps
                )
            if len(hard_gaps) >= self.GAP_CAPS[0][0]:
                missing = "、".join(hard_gaps[:2])
                return self._verdict(False, f"缺少{missing}", score, dimensions, hard_gaps)
        return self._verdict(True, reason, score, dimensions, hard_gaps)

    def _verdict(
        self,
        match: bool,
        reason: str,
        score: int | None,
        dimensions: list[DimVerdict],
        hard_gaps: list[str],
    ) -> Verdict:
        return Verdict(
            match=match, reason=reason, score=score, dimensions=dimensions, hard_gaps=hard_gaps
        )

    async def review(self, job: Job) -> RawReview:
        """调用模型给出评分素材；解析不了带着具体问题纠错重试，仍不行抛 ``AgentRunError``。

        限流与网络错误是暂时的，等几秒重试一次；
        思考型模型把输出上限烧完时，自动加倍 max_tokens（至顶格值）重试；
        输出不合规（围栏、缺字段、维度名不对）按上次的具体错误追问，最多 ``PARSE_RETRIES`` 次。
        """
        prompt = self._prompt(job)
        failures = 0
        while True:
            try:
                text = (await self.agent.run(prompt)).output
            except AgentRunError as exc:
                kind = classify(exc.__cause__ or exc)
                if (
                    kind is ErrorKind.THINKING_OVERFLOW
                    and self.max_tokens < MAX_OUTPUT_TOKENS_CEILING
                ):
                    # 思考把上限烧光：加倍后重建 Agent 再试
                    self.max_tokens = min(MAX_OUTPUT_TOKENS_CEILING, self.max_tokens * 2)
                    self.__dict__.pop("agent", None)
                    await asyncio.sleep(self.RETRY_DELAY)
                    continue
                if kind in (ErrorKind.NETWORK, ErrorKind.RATE_LIMIT):
                    await asyncio.sleep(self.RETRY_DELAY)
                    text = (await self.agent.run(prompt)).output
                else:
                    raise
            try:
                return parse_review(text)
            except (ValidationError, ValueError) as exc:
                failures += 1
                if failures >= self.PARSE_RETRIES:
                    raise UnexpectedModelBehavior(
                        f"重试 {self.PARSE_RETRIES} 次输出仍不合规：{exc}"
                    ) from exc
                prompt = (
                    f"{self._prompt(job)}\n\n"
                    f"你上一次的回复无法解析（{str(exc)[:200]}）。"
                    "请严格按要求只输出一个 JSON 对象：不要 markdown 代码块标记，"
                    "不要解释文字，dimensions 必须包含 duty / evidence / hard / stack / "
                    "conditions 五项，每项都有 key、score、evidence。"
                )

    def _normalize(self, dims: list[DimScore], hard_gaps: list[str]) -> list[DimVerdict]:
        """把模型的分项整理成五维明细：越界压回、缺的补零、没有证据的高分压低、按缺口封顶。"""
        given: dict[str, DimScore] = {}
        for dim in dims:
            if dim.key in self.DIMENSIONS and dim.key not in given:
                given[dim.key] = dim
        result: list[DimVerdict] = []
        for key, (label, full) in self.DIMENSIONS.items():
            dim = given.get(key)
            score = min(max(dim.score, 0), full) if dim else 0
            evidence = (dim.evidence if dim else "").strip()
            if dim and score > full * self.NO_EVIDENCE_RATIO and not evidence:
                # 讲不出依据的高分不可信，压到及格线
                score = int(full * self.NO_EVIDENCE_RATIO)
            result.append(
                DimVerdict(key=key, label=label, score=score, max=full, evidence=evidence)
            )
        for count, cap in self.GAP_CAPS:
            if len(hard_gaps) >= count:
                result = self._cap(result, cap)
                break
        return result

    @staticmethod
    def _cap(dims: list[DimVerdict], cap: int) -> list[DimVerdict]:
        """总分超过封顶时等比压回并精确补齐到 ``cap``，保持维度间的相对差距。

        先按比例向下取整，再把封顶线减去实际总分所差的分数从高分维度逐点补回。
        """
        total = sum(d.score for d in dims)
        if total <= cap or total == 0:
            return dims
        ratio = cap / total
        scaled = [
            DimVerdict(**{**d.model_dump(), "score": int(d.score * ratio)}) for d in dims
        ]
        short = cap - sum(d.score for d in scaled)
        order = sorted(range(len(scaled)), key=lambda i: scaled[i].score, reverse=True)
        for i in range(short):
            key = order[i % len(order)]
            scaled[key] = DimVerdict(**{**scaled[key].model_dump(), "score": scaled[key].score + 1})
        return scaled

    def _prompt(self, job: Job) -> str:
        parts = [
            f"目标岗位要求：\n{self.requirement}" if self.requirement else "",
            f"求职者简历：\n{self.resume}" if self.resume else "",
            f"薪资判断：{fit_summary(job.salary, self.salary_range)}" if self.resume else "",
            (
                f"岗位名称：{job.title}\n公司：{job.company}\n薪资：{job.salary}\n"
                f"岗位详情：\n{job.description}"
            ),
            (
                "输出要求：dimensions 必须包含全部五个维度（"
                + "、".join(f"{key}满分{full}" for key, (_, full) in self.DIMENSIONS.items())
                + "），每个维度先写 evidence 再写 score。"
                if self.resume
                else ""
            ),
        ]
        return "\n\n".join(p for p in parts if p)
