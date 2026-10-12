"""投递决策引擎：采集器与投递流程共用同一条筛选与记账链。

流程拆成三段，让 AI 评审与页面浏览重叠（评审是大模型调用，最慢的一步）：

1. ``admit``：查重 → 黑名单 → 关键词 → 期望薪资（纯本地，点开详情前就能判定）；
2. ``start_review``：并发发起 AI 五维评审（信号量限流），调用方继续浏览不等待；
3. ``finish``：等评审结论，按节奏串行投递并记账——投递动作保持拟人节奏不并发。

开启 ``downgrade_on_fail`` 时，投递失败会降级为「待手动投递」并入库，
岗位页可回填记账；默认关闭时失败只记日志。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.exceptions import AgentRunError

from job.boss.filters import KeywordFilter, PaceProfile
from job.boss.review import JobReviewer
from job.boss.salary import SalaryRange
from job.boss.salary import parse as parse_salary
from job.models.blacklist import CompanyBlacklistRow
from job.models.job import JobRow
from job.utils.llm import explain

if TYPE_CHECKING:
    from job.boss.jobs import Job
    from job.boss.review import Verdict

# 日志回调：(级别 info / ok 投递 / dup 重复 / skip 跳过 / warn / manual 待手动, 内容)
LogSink = Callable[[str, str], Awaitable[None]]

# process 的结果状态
APPLIED = "applied"      # 投递成功
CHATTED = "chatted"      # 平台显示此前已沟通过/已申请过
SKIP = "skip"            # 查重、筛选或 AI 判为不合适，已入库
FAILED = "failed"        # 投递动作失败（BOSS 行为：只记日志）
MANUAL = "manual"        # 投递失败已降级为「待手动投递」并入库
LIMIT = "limit"          # 达到该平台当日投递上限

# 同时进行的 AI 评审数：评审是每个岗位最慢的一步，与浏览重叠后按小批并发
REVIEW_CONCURRENCY = 3


async def stop_await(stop: asyncio.Event, coro: Awaitable, timeout: float | None = None):
    """等一个 awaitable 完成，但 ``stop`` 置位（或超时）时取消它并抛 ``CancelledError``。

    页面等待、模型调用这类长 await 默认对停止请求无感，是「点停止后还要等很久」
    的根源；把关键等待都套上它，停止才能即刻生效。取消 Playwright 的等待是安全的。
    """
    task: asyncio.Task = asyncio.ensure_future(coro)
    stop_task = asyncio.ensure_future(stop.wait())
    wait_set: set[asyncio.Task] = {task, stop_task}
    done, pending = await asyncio.wait(wait_set, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    stop_task.cancel()
    if task in done:
        return task.result()
    task.cancel()
    raise asyncio.CancelledError


@dataclass
class Outcome:
    """一个岗位走完整条决策链的结果。"""

    status: str
    job: "Job | None" = None
    note: str = ""


class DeliveryEngine:
    """一次投递会话的共享状态与决策链，由采集器驱动。"""

    def __init__(
        self,
        *,
        platform: str,
        daily_limit: int,
        keywords: KeywordFilter | None = None,
        salary_range: SalaryRange | None = None,
        reviewer: JobReviewer | None = None,
        pace: PaceProfile | None = None,
        day_factor: float = 1.0,
        on_log: LogSink | None = None,
        stop: asyncio.Event,
        downgrade_on_fail: bool = False,
        review_concurrency: int = REVIEW_CONCURRENCY,
        company_prefetcher=None,
        auto_resume_hook: Callable[["Job", "Verdict", LogSink], Awaitable[None]] | None = None,
    ) -> None:
        self.platform = platform
        self.daily_limit = daily_limit
        self._keywords = keywords or KeywordFilter()
        self._salary_range = salary_range
        self._reviewer = reviewer
        self._pace = pace or PaceProfile()
        self._day_factor = day_factor
        self._on_log = on_log
        self._stop = stop
        # 投递失败是否降级为「待手动投递」入库：BOSS 之外的平台开启
        self.downgrade_on_fail = downgrade_on_fail
        # AI 评审并发上限：与浏览重叠，但不把模型服务打爆
        self._review_sem = asyncio.Semaphore(max(1, review_concurrency))
        # 公司背景预查（发现岗位时后台自动查企业）；None 表示不启用
        self._prefetcher = company_prefetcher
        # 评分达标后自动定制简历的回调：None 表示不启用
        self._auto_resume_hook = auto_resume_hook
        self.applied_today = JobRow.count_today(platform)
        self.saved = 0
        self.limit_hit = False
        self._blacklist_ids: dict[str, str] = {}
        self._blacklist_names: dict[str, str] = {}

    # --- 基础设施 ---

    async def log(self, text: str, level: str = "info") -> None:
        """把一条过程日志交给 ``on_log``；没传回调就丢弃。"""
        if self._on_log is not None:
            await self._on_log(level, text)

    async def pause(self, span: tuple[float, float]) -> None:
        """随机停顿（乘以今日节奏系数）；期间请求停止会立即返回。"""
        seconds = random.uniform(*span) * self._day_factor
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), seconds)

    @property
    def limit_reached(self) -> bool:
        return self.applied_today >= self.daily_limit

    def hit_limit(self) -> None:
        self.limit_hit = True
        self._stop.set()

    # --- 预筛：黑名单与期望薪资（点开详情前就能判定，不耗 AI） ---

    def load_blacklist(self) -> None:
        """整表载入黑名单：公司 ID 索引、公司名索引，以及全部词条（匹配岗位名用）。"""
        self._blacklist_ids = {}
        self._blacklist_names = {}
        self._blacklist_keywords: list[str] = []
        for key, reason in CompanyBlacklistRow.names().items():
            if key.startswith("name:"):
                self._blacklist_names[key[5:]] = reason
                self._blacklist_keywords.append(key[5:].lower())
            else:
                self._blacklist_ids[key] = reason
        # BOSS 公司键行也带 name 字段，一并加入岗位名匹配
        for name in CompanyBlacklistRow.entry_names():
            lowered = name.lower()
            if lowered and lowered not in self._blacklist_keywords:
                self._blacklist_keywords.append(lowered)

    def title_keyword(self, title: str) -> str:
        """岗位名称命中黑名单词条时返回该词条，没命中返回空串。"""
        lowered = (title or "").lower()
        for kw in getattr(self, "_blacklist_keywords", []):
            if kw and kw in lowered:
                return kw
        return ""

    def blacklist_reason(self, brand_id: str, company: str) -> str:
        """黑名单命中返回原因，没命中返回空串。

        公司 ID 索引只对 BOSS 生效；手动按公司名加入的记录天然全量匹配。
        """
        if self.platform == "boss" and brand_id and brand_id in self._blacklist_ids:
            return self._blacklist_ids[brand_id]
        return self._blacklist_names.get(company.strip(), "")

    def salary_reason(self, salary: str) -> str:
        """设了期望薪资且岗位薪资解析得出、又没有交集时返回原因，否则空串。"""
        if self._salary_range is None:
            return ""
        actual = parse_salary(salary)
        if actual is not None and actual.intersect(self._salary_range) is None:
            return f"薪资 {actual.label()} 与期望 {self._salary_range.label()} 不符"
        return ""

    def record(
        self,
        job: "Job",
        *,
        suitable: bool = True,
        reason: str = "",
        score: int | None = None,
        detail: str | None = None,
        applied: bool = False,
    ) -> None:
        """判断结果入库（统一走 JobRow.record，含平台与评分明细）。"""
        JobRow.record(
            job,
            suitable=suitable,
            reason=reason,
            score=score,
            detail=detail,
            applied=applied,
        )

    # --- 决策链三段 ---

    async def admit(self, job: "Job") -> "Job | None":
        """投递前的本地筛选：查重 → 黑名单 → 关键词 → 期望薪资。

        不合适（或看过）的岗位入库并打日志后返回 None；通过原样返回岗位，
        并顺手把公司交给后台预查（查企业不阻塞投递）。
        """
        name = f"{job.title} · {job.company}"
        if seen := JobRow.find_duplicate(job):
            note = f"：{seen.reason}" if seen.reason else ""
            await self.log(f"{name}（看过，{seen.result}{note}）", "dup")
            return None
        for reason in (
            self.blacklist_reason(job.brand_id, job.company),
            self.title_keyword(job.title),
            self._keywords.reject_reason(job.title, job.company),
            self.salary_reason(job.salary),
        ):
            if reason:
                self.record(job, suitable=False, reason=reason)
                await self.log(f"{name}（{reason}）", "skip")
                return None
        if self._prefetcher is not None:
            self._prefetcher.schedule(
                platform=self.platform, brand_id=job.brand_id, name=job.company, link=job.link
            )
        return job

    def start_review(self, job: "Job") -> "asyncio.Task[Verdict | None]":
        """并发发起 AI 评审：立即返回 Task，调用方继续浏览；并发数由信号量限流。"""
        return asyncio.create_task(self._review_once(job))

    async def _review_once(self, job: "Job") -> "Verdict | None":
        if self._reviewer is None or self._stop.is_set():
            return None
        async with self._review_sem:
            if self._stop.is_set():
                return None
            try:
                # 停止请求即刻打断在途的模型调用，不再干等它跑完
                return await stop_await(self._stop, self._reviewer.check(job))
            except AgentRunError as exc:
                await self.log(
                    f"{job.title} · {job.company}（AI 复核失败，{explain(exc)}）", "warn"
                )
                return None
            except asyncio.CancelledError:
                return None

    def needs_description(self, job: "Job") -> bool:
        """详情缺失且开启了 AI 复核：该岗位无法评分，交给 ``skip_missing_description`` 处理。"""
        return self._reviewer is not None and not job.description.strip()

    async def skip_missing_description(self, job: "Job") -> Outcome:
        """详情拿不到（如需登录后才可见）：只入库留人工判断，不评分不投递。"""
        name = f"{job.title} · {job.company}"
        self.record(job, reason="详情需登录后可见，未评分")
        await self.log(f"{name}（详情需登录，已采集未评分）", "warn")
        return Outcome(SKIP)

    async def finish(
        self,
        job: "Job",
        review_task: "asyncio.Task[Verdict | None] | None",
        *,
        apply: Callable[["Job"], Awaitable[str]] | None = None,
        chatted_mark: str = "",
        read_span: tuple[float, float] = (3, 8),
        rest: bool = True,
    ) -> Outcome:
        """等评审结论并按节奏投递：投递动作串行、拟人停顿，评审早已并行完成。"""
        name = f"{job.title} · {job.company}"
        if self._stop.is_set():
            return Outcome(FAILED, note="已请求停止")
        verdict = await review_task if review_task is not None else None
        reason, score, detail = "符合筛选条件", None, None
        if verdict is not None:
            reason, score = verdict.reason, verdict.score
            detail = verdict.detail
            if not verdict.match:
                self.record(job, suitable=False, reason=reason, score=score, detail=detail)
                await self.log(f"{name}（不合适：{reason}）", "skip")
                await self.pause(read_span)
                return Outcome(SKIP)
        elif self._reviewer is not None:
            # 评审失败（如模型思考超限）：岗位入库留痕，收尾的自动评分会补上
            self.record(job, reason="AI 复核失败，未评分")
            return Outcome(FAILED)

        # 评分达标的高分岗位：后台自动生成定制简历，不阻塞投递
        if verdict is not None and self._auto_resume_hook is not None and not self._stop.is_set():
            asyncio.create_task(self._auto_resume_hook(job, verdict, self.log))

        await self.pause(read_span)
        if self._stop.is_set():
            return Outcome(FAILED, note="已请求停止")
        if apply is None:
            return Outcome(FAILED, note="没有投递实现")
        status = await apply(job)
        if status == chatted_mark and chatted_mark:
            self.record(job, reason=reason, score=score, detail=detail)
            await self.log(f"{name}（{status}）", "skip")
            return Outcome(CHATTED)
        if status:
            if self.downgrade_on_fail:
                self.record(
                    job,
                    reason=f"{JobRow.MANUAL_PENDING_PREFIX}：{status}",
                    score=score,
                    detail=detail,
                )
                await self.log(f"{name}（{status}，已标记待手动投递）", "manual")
                return Outcome(MANUAL, note=status)
            level = "warn" if self.limit_hit else "skip"
            await self.log(f"{name}（{status}）", level)
            return Outcome(FAILED, note=status)

        self.record(job, reason=reason, score=score, detail=detail, applied=True)
        self.saved += 1
        self.applied_today += 1
        salary = job.salary or "薪资未知"
        match = "" if score is None else f" · 匹配 {score} 分"
        await self.log(f"{job.title} · {job.company} · {salary}{match}", "ok")
        if self.limit_reached:
            self.hit_limit()
            return Outcome(LIMIT, job=job)
        if rest and self._pace.rest_every and self.saved % self._pace.rest_every == 0:
            await self.log(f"已投递 {self.saved} 条，休息一会")
            await self.pause(self._pace.rest)
        return Outcome(APPLIED, job=job)

    async def process(
        self,
        job: "Job",
        *,
        fetch_detail: Callable[["Job"], Awaitable["Job"]] | None = None,
        apply: Callable[["Job"], Awaitable[str]] | None = None,
        chatted_mark: str = "",
        read_span: tuple[float, float] = (3, 8),
        rest: bool = True,
        require_description: bool = False,
    ) -> Outcome:
        """单岗位走完整条决策链（评审与投递串行）；批量场景请用 admit/start_review/finish。"""
        if (await self.admit(job)) is None:
            return Outcome(SKIP)
        if fetch_detail is not None:
            job = await fetch_detail(job)
        if require_description and self.needs_description(job):
            return await self.skip_missing_description(job)
        task = self.start_review(job)
        try:
            return await self.finish(
                job, task, apply=apply, chatted_mark=chatted_mark, read_span=read_span, rest=rest
            )
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
