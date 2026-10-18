"""BOSS 自动投递：逐个点击卡片取详情，筛选复核通过后点「立即沟通」投递并入库。

带断点续采：每个城市看完即记录进度，中途停止或登录失效后重开会从没看完的城市继续。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from patchright.async_api import Error as PlaywrightError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from job.boss.filters import BASE_URL, KeywordFilter, PaceProfile, SearchUrl
from job.boss.salary import SalaryRange
from job.platforms import checkpoint
from job.platforms.engine import APPLIED, DeliveryEngine, stop_await
from job.utils import as_dict

# 日志回调：(级别 info / ok 投递 / dup 重复 / skip 跳过 / warn, 内容)
LogSink = Callable[[str, str], Awaitable[None]]

if TYPE_CHECKING:
    from patchright.async_api import Locator, Page, Response

    from job.boss.review import JobReviewer
    from job.boss.session import BossSession

# 字体加密字符（Unicode 私用区）
_ENCRYPTED = re.compile(r"[\ue000-\uf8ff]")
# 职位详情页的职位描述
DESCRIPTION = ".job-sec-text"


async def read_description(
    session: "BossSession", link: str, *, attempts: int = 3, stop: asyncio.Event | None = None
) -> str:
    """在新标签页打开职位详情页读职位描述，读不到隔几秒重试；都失败返回空串。

    传入 ``stop`` 时：每轮尝试前检查停止请求，页面打开与读取也可被停止打断。
    开页走 ``session.new_page()``：后台静默时窗口保持最小化，不会弹出来打断前台。
    """
    for attempt in range(attempts):
        if stop is not None and stop.is_set():
            return ""
        if attempt:
            await asyncio.sleep(random.uniform(2, 5))
        detail = await session.new_page()
        try:
            if stop is not None:
                text = await stop_await(stop, _read_once(detail, link), timeout=45)
            else:
                text = await _read_once(detail, link)
            if text := text.strip():
                return text
        except (PlaywrightError, RuntimeError, asyncio.CancelledError):
            # RuntimeError：用户点过「关闭浏览器」，封锁期开不了页——本轮放弃即可
            pass
        finally:
            with contextlib.suppress(PlaywrightError):
                await detail.close()
    return ""


async def _read_once(detail: Page, link: str) -> str:
    """打开详情页读职位描述正文。"""
    await detail.goto(link, wait_until="domcontentloaded")
    return await detail.locator(DESCRIPTION).first.inner_text(timeout=15_000)


def _detail_path(*keys: str) -> AliasPath:
    """详情接口 zpData 下的字段路径。"""
    return AliasPath("zpData", *keys)


class Job(BaseModel):
    """一条职位：通用岗位模型。

    BOSS 的列表接口 joblist.json 与详情接口 job/detail.json 通过字段别名直接解析。
    ``link`` 是普通字段，BOSS 解析路径按 job_id 兜底拼接。
    """

    model_config = ConfigDict(
        validate_by_name=True,
        validate_by_alias=True,
        str_strip_whitespace=True,
        coerce_numbers_to_str=True,
    )

    platform: str = Field(default="boss", description="来源平台标识，见 job.platforms")
    job_id: str = Field(default="", validation_alias="encryptJobId")
    title: str = Field(default="", validation_alias="jobName")
    salary: str = Field(default="", validation_alias="salaryDesc")
    company: str = Field(default="", validation_alias="brandName")
    brand_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "encryptBrandId", _detail_path("brandComInfo", "encryptBrandId")
        ),
    )
    location: str = ""
    experience: str = Field(default="", validation_alias="jobExperience")
    education: str = Field(default="", validation_alias="jobDegree")
    description: str = Field(
        default="",
        validation_alias=AliasChoices(
            _detail_path("jobInfo", "postDescription"),
            _detail_path("jobCard", "postDescription"),
        ),
    )
    address: str = Field(
        default="",
        validation_alias=AliasChoices(
            _detail_path("jobInfo", "address"),
            _detail_path("jobCard", "address"),
        ),
    )
    hr_name: str = Field(
        default="",
        validation_alias=AliasChoices("bossName", _detail_path("bossInfo", "name")),
    )
    hr_title: str = Field(
        default="",
        validation_alias=AliasChoices("bossTitle", _detail_path("bossInfo", "title")),
    )
    link: str = Field(default="", description="职位详情页地址")

    @model_validator(mode="before")
    @classmethod
    def _fill_boss_link(cls, data: Any) -> Any:
        """BOSS 列表数据没有 link 字段，按 job_id 拼详情页地址（别名与字段名都认）。"""
        if isinstance(data, dict) and not data.get("link"):
            job_id = data.get("encryptJobId") or data.get("job_id")
            if job_id:
                data = {**data, "link": f"{BASE_URL}/job_detail/{job_id}.html"}
        return data

    @model_validator(mode="before")
    @classmethod
    def _join_location(cls, data: Any) -> Any:
        """列表接口的城市 / 区 / 商圈拼成「广州·黄埔区·科学城」。"""
        if not isinstance(data, dict) or "location" in data:
            return data
        parts = (data.get(k) for k in ("cityName", "areaDistrict", "businessDistrict"))
        location = "·".join(str(p).strip() for p in parts if p)
        return {**data, "location": location} if location else data

    @field_validator("*", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Any) -> Any:
        """接口里的 null 按空字符串处理。"""
        return "" if value is None else value

    @field_validator("salary")
    @classmethod
    def _drop_encrypted_salary(cls, value: str) -> str:
        """字体加密的薪资无法还原，直接留空。"""
        return "" if _ENCRYPTED.search(value) else value

    def with_detail(self, payload: dict[str, Any]) -> Job:
        """用详情接口的非空字段（描述、地址、HR、公司 ID）补全当前职位。"""
        detail = Job.model_validate(payload)
        fields = ("description", "address", "hr_name", "hr_title", "brand_id")
        return self.model_copy(
            update={k: v for k in fields if (v := getattr(detail, k))}
        )


class JobScraper:
    """在 BossSession 的页面上自动投递：点击卡片 → 取详情 → 复核 → 立即沟通 → 入库。

    决策链（查重/黑名单/关键词/薪资/AI 复核/记账）在 ``DeliveryEngine``，
    本类只负责 BOSS 专属的页面交互：卡片点击、列表接口拦截、「立即沟通」投递动作。
    """

    # 平台标识与展示名（工作台调度用）
    PLATFORM: ClassVar[str] = "boss"
    PLATFORM_NAME: ClassVar[str] = "BOSS直聘"
    # 平台首页：勾选平台时先打开这个窗口供用户登录
    HOME_URL: ClassVar[str] = BASE_URL

    # 列表卡片
    CARD = "li.job-card-box, .job-card-wrapper"
    # 登录弹层
    LOGIN = ".login-dialog-wrap"
    # 右侧详情里的沟通按钮（未沟通过显示「立即沟通」，沟通过显示「继续沟通」）
    CHAT_BTN = ".job-detail-box .op-btn-chat, .job-detail-container .op-btn-chat"
    # 点「立即沟通」后弹窗里的「留在此页」
    STAY = re.compile(r"^\s*留在此页")
    # 沟通按钮的文字（CSS 选择器匹配不到时按文字兜底定位）
    CHAT_TEXT = re.compile(r"^\s*(立即|继续)沟通\s*$")
    # 弹窗容器
    DIALOG = "[class*='dialog']"
    # 当日投递次数用完的弹窗文案
    LIMIT = re.compile(r"上限|明天再来")
    # 沟通按钮是「继续沟通」时的结果
    CHATTED = "此前已沟通"
    # 每日投递上限
    DAILY_LIMIT = 150
    # 每个岗位关键词每轮最多投递数：一轮投满还没到当日额度就从第一个关键词再开始
    PER_KEYWORD_ROUND = 10
    # 列表接口 URL 特征
    LIST_API = "joblist.json"
    # 详情接口 URL 特征
    DETAIL_APIS = ("job/detail.json", "job/card.json")
    # 最多下滑加载次数
    MAX_SCROLLS = 20
    # 登录失效时的等待重试次数：等用户在可见浏览器里扫码，等不到才放弃
    LOGIN_RETRIES = 5

    def __init__(self, session: BossSession) -> None:
        self.session = session
        self._stop = asyncio.Event()
        self._listed: dict[str, Job] = {}
        self._pace = PaceProfile()
        self._keywords = KeywordFilter()
        self._reviewer: JobReviewer | None = None
        self._day_factor = 1.0
        self._on_log: LogSink | None = None
        # 期望薪资区间；None 表示没有设定
        self._salary_range: SalaryRange | None = None
        self._engine: DeliveryEngine | None = None
        # 求职配置（build_targets 时记下，关键词循环重建 URL 用）与关键词列表
        self._config: dict[str, Any] = {}
        self._queries: list[str] = []
        # 公司背景预查器（由工作台注入）；None 表示不启用
        self.company_prefetcher = None
        # 评分达标后自动定制简历的回调（由工作台注入）
        self.auto_resume_hook = None
        # 右侧详情面板当前显示的岗位 ID（投递前据此判断要不要重开卡片）
        self._pane_job_id = ""
        # 本轮当前关键词已投递数（每轮每关键词最多 PER_KEYWORD_ROUND 个）
        self._kw_applied = 0

    @property
    def engine(self) -> DeliveryEngine:
        """当前投递会话的决策引擎；``search`` 每轮重建，直接调用时按现有配置懒建。"""
        if self._engine is None:
            self._engine = DeliveryEngine(
                platform=self.PLATFORM,
                daily_limit=self.DAILY_LIMIT,
                keywords=self._keywords,
                salary_range=self._salary_range,
                reviewer=self._reviewer,
                pace=self._pace,
                day_factor=self._day_factor,
                on_log=self._on_log,
                stop=self._stop,
                company_prefetcher=self.company_prefetcher,
                auto_resume_hook=self.auto_resume_hook,
            )
            self._engine.load_blacklist()
        return self._engine

    def build_targets(self, config: Mapping[str, Any]) -> list[tuple[str, str]]:
        """求职配置 → [(城市名, 搜索 URL)]，按所选城市顺序；记住配置供关键词循环重建 URL。"""
        self._config = dict(config)
        return SearchUrl.by_city(config)

    def request_stop(self) -> None:
        """请求停止当前投递。"""
        self._stop.set()

    async def search(
        self,
        targets: list[tuple[str, str]],
        *,
        pace: PaceProfile | None = None,
        keywords: KeywordFilter | None = None,
        reviewer: JobReviewer | None = None,
        salary_range: SalaryRange | None = None,
        queries: list[str] | None = None,
        on_job: Callable[[Job], Awaitable[None]] | None = None,
        on_log: LogSink | None = None,
    ) -> tuple[list[Job], str]:
        """按关键词 × 城市轮询投递：``targets`` 为 [(城市名, 搜索 URL)]。

        ``queries`` 是岗位关键词列表（AI 研判生成或手动填写），每个关键词
        对每个城市单独跑一轮搜索。每日额度有限，按轮分配：每个关键词每轮
        最多投 ``PER_KEYWORD_ROUND`` 个，一轮投满还没到当日额度就从第一个
        关键词再开始，直到额度用完或所有关键词的岗位都看完。
        断点按「关键词@城市」记录看完的条目（本轮投满的中断点不记，下轮
        从头快速掠过已投岗位继续）。
        按节奏 ``pace`` 投递，``keywords`` 与黑名单不符合的卡片跳过不点开，
        ``salary_range`` 设了期望薪资时，与岗位薪资没有交集的卡片也跳过；
        传了 ``reviewer`` 时，点开详情后再做一次 AI 复核，不通过的不投递。
        看过的岗位连同是否合适、原因一起入库，下次直接跳过；每天最多投递 ``DAILY_LIMIT`` 次。
        过程日志交给 ``on_log``（不打印到终端）。
        返回 (职位列表, 状态)；状态为 done / stopped / need_login / limit。
        """
        self._stop.clear()
        self._pace = pace or PaceProfile()
        self._keywords = keywords or KeywordFilter()
        self._reviewer = reviewer
        self._salary_range = salary_range
        self._on_log = on_log
        self._day_factor = PaceProfile.daily_factor(str(self.session.user_data_dir))
        self._queries = [q.strip() for q in (queries or []) if q and q.strip()] or [""]
        self._engine = DeliveryEngine(
            platform=self.PLATFORM,
            daily_limit=self.DAILY_LIMIT,
            keywords=self._keywords,
            salary_range=self._salary_range,
            reviewer=self._reviewer,
            pace=self._pace,
            day_factor=self._day_factor,
            on_log=on_log,
            stop=self._stop,
            company_prefetcher=self.company_prefetcher,
            auto_resume_hook=self.auto_resume_hook,
        )
        engine = self.engine
        await engine.log(
            f"今日已投递 {engine.applied_today} / {self.DAILY_LIMIT}（{self.PLATFORM_NAME}）"
        )
        if engine.limit_reached:
            return [], "limit"
        page = await self.session.page()

        page.on("response", self._on_list_response)
        jobs: list[Job] = []
        try:
            entries = [
                f"{kw}@{city}"
                for kw in self._queries
                for city, _ in targets
            ]
            sig = checkpoint.sig_of(targets, self._queries)
            done_entries = checkpoint.load(self.PLATFORM, sig)
            if skipped := [e for e in entries if e in done_entries]:
                await engine.log(f"断点续采：{'、'.join(skipped)}上次已看完，自动跳过")
            await engine.log(
                f"每个岗位关键词每轮最多投递 {self.PER_KEYWORD_ROUND} 个岗位，"
                f"投满一轮还没用完当日额度就从第一个关键词再开始"
            )
            round_no = 0
            while not self._stop.is_set() and not engine.limit_reached:
                if not [e for e in entries if e not in done_entries]:
                    break
                round_no += 1
                progressed = False
                for kw in self._queries:
                    if self._stop.is_set() or engine.limit_reached:
                        break
                    self._kw_applied = 0
                    for city, _ in targets:
                        if self._stop.is_set() or engine.limit_reached:
                            break
                        entry = f"{kw}@{city}"
                        if entry in done_entries:
                            continue
                        label = f"{city}（{kw}）" if kw else city
                        await engine.log(
                            f"开始搜索{label}的岗位"
                            + (f"（第 {round_no} 轮）" if round_no > 1 else "")
                        )
                        state = await self._search_city(
                            page, SearchUrl.build(self._config, city, kw or None), jobs, on_job
                        )
                        if state == "quota":
                            # 本关键词本轮已投满：其余城市留给下一轮
                            progressed = True
                            break
                        if state != "done":
                            return jobs, state
                        done_entries.add(entry)
                        checkpoint.save(self.PLATFORM, sig, entries, done_entries)
                        progressed = True
                if not progressed:
                    break
            if not self._stop.is_set() and not engine.limit_reached:
                checkpoint.clear(self.PLATFORM)
            return jobs, self._end_state()
        finally:
            page.remove_listener("response", self._on_list_response)

    async def _search_city(
        self,
        page: Page,
        url: str,
        jobs: list[Job],
        on_job: Callable[[Job], Awaitable[None]] | None,
    ) -> str:
        """投递一个城市的搜索结果，投递成功的追加进 ``jobs``；返回状态。

        遇到登录弹层不立刻放弃：提示用户在可见浏览器里扫码，隔几十秒重试，
        重试几次仍没登录才返回 need_login；期间请求停止会立即返回 stopped。
        """
        for attempt in range(self.LOGIN_RETRIES):
            self._listed.clear()
            await page.goto(url, wait_until="domcontentloaded")
            try:
                # 停止请求可打断最长 20 秒的卡片等待
                await stop_await(self._stop, page.wait_for_selector(self.CARD, timeout=20_000))
            except asyncio.CancelledError:
                return "stopped"
            except PlaywrightTimeoutError:
                if not await page.locator(self.LOGIN).is_visible():
                    return "done"  # 没有岗位，不是要登录
                if attempt + 1 < self.LOGIN_RETRIES:
                    await self._log(
                        "需要登录 BOSS，请在打开的浏览器里登录，登录后自动继续…", "warn"
                    )
                    await self._pause((30, 60))
                    if self._stop.is_set():
                        return "stopped"
                    continue
                return "need_login"

            done: set[str] = set()
            idle = 0
            for _ in range(self.MAX_SCROLLS):
                seen = len(done)
                jobs.extend(await self._scrape_cards(page, done, on_job))
                idle = 0 if len(done) > seen else idle + 1
                if self._stop.is_set() or idle >= 2:
                    break
                if self._kw_applied >= self.PER_KEYWORD_ROUND:
                    return "quota"  # 本关键词本轮投满，换下一个关键词
                await self._load_more(page)
                await self._pause(self._pace.scroll)
            return self._end_state()
        return "need_login"

    async def _scrape_cards(
        self,
        page: Page,
        done: set[str],
        on_job: Callable[[Job], Awaitable[None]] | None,
    ) -> list[Job]:
        """逐个处理当前可见、未看过的卡片，两阶段流水线提速。

        阶段一（页面交互，串行）：查重/黑名单/关键词/薪资预筛 → 点卡片取详情 →
        立即发起 AI 评审（并发后台跑，不等待）→ 继续下一张卡片；本轮关键词额度
        投满就不再点开新卡片。阶段二：逐个收评审结论，按拟人节奏串行投递——
        评审延迟被浏览时间覆盖；额度已满时只收结论不入库（合适的岗位下一轮再投）。
        """
        cards = page.locator(self.CARD)
        pending: list[tuple[Job, "asyncio.Task"]] = []
        engine = self.engine
        for i in range(await cards.count()):
            if self._stop.is_set() or self._kw_applied >= self.PER_KEYWORD_ROUND:
                break
            card = cards.nth(i)
            try:
                job = await self._listed_job_for(card, i)
                if job is None or job.job_id in done:
                    continue
                done.add(job.job_id)
                if (job := await engine.admit(job)) is None:
                    continue
                job = await self._open_detail(page, card, job)
            except PlaywrightError as exc:
                # 列表重渲染导致个别卡片失效：跳过这张，绝不让一轮投递中断
                await engine.log(
                    f"第 {i + 1} 张卡片处理失败，已跳过（{str(exc).splitlines()[0]}）", "warn"
                )
                continue
            pending.append((job, engine.start_review(job)))

        batch: list[Job] = []

        async def quota_full(_job: Job) -> str:
            return "本关键词本轮额度已满，下一轮继续"

        for job, task in pending:
            full = self._kw_applied >= self.PER_KEYWORD_ROUND
            outcome = await engine.finish(
                job,
                task,
                # 额度已满：不执行投递动作（岗位不入库，下一轮重新处理）
                apply=quota_full if full else (lambda j: self._apply(page, j)),
                chatted_mark=self.CHATTED,
            )
            if outcome.status == APPLIED:
                self._kw_applied += 1
                batch.append(outcome.job or job)
                if on_job is not None:
                    await on_job(job)
        return batch

    async def _apply(self, page: Page, job: Job) -> str:
        """点「立即沟通」投递；投递成功返回空串，否则返回原因。

        投递前先确认详情面板显示的就是该岗位——两阶段流水线里投递晚于点卡片，
        面板常常停在别的岗位上，不重开会把招呼发给错误的公司。列表里定位不到
        卡片时退回单独打开职位详情页完成投递，绝不给面板上恰好显示的岗位打招呼。
        """
        target = page
        detail: Page | None = None
        if not await self._show_job(page, job):
            detail = await self.session.new_page()
            try:
                await detail.goto(job.link, wait_until="domcontentloaded")
            except PlaywrightError:
                with contextlib.suppress(PlaywrightError):
                    await detail.close()
                return "职位详情页打开失败，已跳过打招呼"
            target = detail
        try:
            button = await self._chat_button(target)
            if button is None:
                return "页面上找不到沟通按钮，已跳过打招呼"
            text = (await button.inner_text(timeout=3_000)).strip()
            if text != "立即沟通":
                return self.CHATTED if "继续" in text else f"沟通按钮为「{text}」"
            pages_before = len(page.context.pages)
            await button.click(timeout=5_000)
            return await self._settle(target, pages_before)
        except PlaywrightError as exc:
            return f"投递失败：{str(exc).splitlines()[0]}"
        finally:
            if detail is not None:
                # 给打招呼请求留点落地时间再关标签页
                with contextlib.suppress(PlaywrightError):
                    await detail.wait_for_timeout(1_500)
                with contextlib.suppress(PlaywrightError):
                    await detail.close()

    async def _chat_button(self, page: Page) -> "Locator | None":
        """找沟通按钮：CSS 选择器优先，都不可见时按「立即/继续沟通」文字兜底。"""
        for sel in (self.CHAT_BTN, ".op-btn-chat", ".btn-startchat"):
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    return loc
            except PlaywrightError:
                continue
        by_text = page.get_by_text(self.CHAT_TEXT).first
        try:
            return by_text if await by_text.count() else None
        except PlaywrightError:
            return None

    async def _settle(self, page: Page, pages_before: int) -> str:
        """点完「立即沟通」等结果，判定这次点击到底发出去了没有。

        BOSS 有时弹「留在此页」，有时弹次数上限，有时不弹任何框直接在后台
        建会话（按钮变「继续沟通」或新开聊天标签页）——只等弹窗会把成功
        误判成失败，导致岗位没入库、下一轮重复打招呼。
        """
        stay = page.get_by_text(self.STAY).first
        limit = page.locator(self.DIALOG).filter(has_text=self.LIMIT).first
        for _ in range(16):
            try:
                if await stay.is_visible():
                    await stay.click(timeout=3_000)
                    return ""
                if await limit.is_visible():
                    self.engine.hit_limit()
                    return "今日投递次数已达上限，停止投递"
                if len(page.context.pages) > pages_before:
                    return ""  # 聊天窗口已打开：打招呼已发出
                button = page.locator(self.CHAT_BTN).first
                if await button.count() and "立即" not in (
                    await button.inner_text(timeout=1_000)
                ):
                    return ""  # 按钮变成「继续沟通」：会话已建立
            except PlaywrightError:
                pass
            await page.wait_for_timeout(500)
        return "点击沟通后没有出现确认弹窗或新会话，可能未发出"

    async def _show_job(self, page: Page, job: Job) -> bool:
        """让右侧详情面板显示目标岗位：面板已是要投的岗位直接通过，否则按
        job_id 重新点开它的列表卡片。定位不到或点不开返回 False。"""
        if job.job_id and self._pane_job_id == job.job_id:
            return True
        if not job.job_id:
            return False
        card = page.locator(self._card_sel(job.job_id)).first
        if not await card.count():
            return False
        try:
            async with page.expect_response(self._is_detail, timeout=8_000):
                await card.click(timeout=5_000)
        except (PlaywrightError, ValueError):
            return False
        self._pane_job_id = job.job_id
        return True

    def _card_sel(self, job_id: str) -> str:
        """按 job_id 定位列表卡片；CARD 是逗号并列的两个选择器，属性要各自接上。"""
        return ", ".join(f'{sel}[data-jobid="{job_id}"]' for sel in self.CARD.split(", "))

    def _end_state(self) -> str:
        if self.engine.limit_hit:
            return "limit"
        return "stopped" if self._stop.is_set() else "done"

    async def _log(self, text: str, level: str = "info") -> None:
        """把一条过程日志交给 ``on_log``；没传回调就丢弃。"""
        if self._on_log is not None:
            await self._on_log(level, text)

    async def _pause(self, span: tuple[float, float]) -> None:
        """随机停顿（乘以今日节奏系数）；期间请求停止会立即返回。"""
        await self.engine.pause(span)

    async def _listed_job_for(self, card: Locator, index: int) -> Job | None:
        """找卡片对应的列表职位：先按 data-jobid，找不到就按顺序对应。"""
        try:
            job_id = await card.get_attribute("data-jobid", timeout=2_000)
        except PlaywrightError:
            # 列表翻页重渲染后卡片已被移除：2 秒内放弃这张，别卡住整轮投递
            return None
        if job_id and job_id in self._listed:
            return self._listed[job_id]
        jobs = list(self._listed.values())
        return jobs[index] if index < len(jobs) else None

    async def _open_detail(self, page: Page, card: Locator, job: Job) -> Job:
        """点击卡片，等详情接口返回并合并进 Job；接口没给职位描述时打开详情页补读。"""
        try:
            async with page.expect_response(self._is_detail, timeout=8_000) as resp:
                await card.click(timeout=5_000)
            self._pane_job_id = job.job_id
            job = job.with_detail(as_dict(await (await resp.value).json()))
        except (PlaywrightError, ValueError):
            pass
        if job.description or not job.link:
            return job
        if description := await read_description(self.session, job.link, stop=self._stop):
            return job.model_copy(update={"description": description})
        await self._log(f"{job.title} 职位详情加载失败，先按列表信息判断")
        return job

    async def _load_more(self, page: Page) -> None:
        """滚到底部触发下一页；等不到列表接口就短暂等待。"""
        try:
            async with page.expect_response(self._is_list, timeout=8_000):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except PlaywrightError:
            await page.wait_for_timeout(1_500)

    async def _on_list_response(self, response: Response) -> None:
        """监听列表接口，把每条职位解析成 Job 按 job_id 收下。"""
        if not self._is_list(response):
            return
        try:
            payload = as_dict(await response.json())
        except (PlaywrightError, ValueError):
            return
        for item in as_dict(payload.get("zpData")).get("jobList") or []:
            try:
                job = Job.model_validate(item)
            except ValidationError as exc:
                await self._log(f"列表数据解析失败：{exc}", "warn")
                continue
            if job.job_id:
                self._listed.setdefault(job.job_id, job)

    def _is_list(self, response: Response) -> bool:
        return response.status == 200 and self.LIST_API in response.url

    def _is_detail(self, response: Response) -> bool:
        if response.status != 200:
            return False
        return any(marker in response.url for marker in self.DETAIL_APIS)
