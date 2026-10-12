"""查企业：打开 BOSS 公司主页读工商信息，用公司全称上网搜负面信息，再让大模型评估风险。

点「查企业」才会触发，结果按公司 ID 存进 ``CompanyRow``，岗位和会话共用。
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Awaitable, Callable
from functools import cached_property
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen

from ddgs import DDGS
from lxml import html
from patchright.async_api import Error as PlaywrightError
from pydantic import BaseModel, Field
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from job.boss.filters import BASE_URL
from job.models.blacklist import CompanyBlacklistRow
from job.models.company import CompanyRow
from job.models.job import JobRow
from job.models.setting import LlmSettings
from job.utils import exception_text, log, telemetry
from job.utils.llm import MAX_OUTPUT_TOKENS, OUTPUT_RETRIES

if TYPE_CHECKING:
    from patchright.async_api import Page

    from job.boss.session import BossSession


# 360 搜索对同一 IP 的并发请求敏感：限流，避免被判定为爬虫而返回验证页
_SO_LIMIT = threading.Semaphore(2)

# 相关性过滤：搜索负面关键词时，结果里常混进与该公司无关的通用维权/行业新闻
_CITY_PREFIX = re.compile(
    r"^(?:中国|北京|上海|天津|重庆|广州|深圳|杭州|南京|苏州|武汉|成都|西安|厦门|"
    r"长沙|郑州|青岛|宁波|无锡|合肥|福州|济南|大连|沈阳|昆明|佛山|东莞|珠海|"
    r"石家庄|太原|南昌|贵阳|南宁|兰州|银川|西宁|乌鲁木齐|呼和浩特|海口|三亚|"
    r"长春|哈尔滨|拉萨)(?:市|省|自治区|特别行政区)?"
)
_COMPANY_SUFFIX = re.compile(r"(?:股份)?有限(?:责任)?公司$|(?:集团|公司|厂|店|中心|研究院)$")


def company_aliases(name: str, full_name: str = "") -> list[str]:
    """公司简称/全称 → 判断搜索结果是否相关的字号（去掉行政区前缀与公司后缀）。"""
    aliases: list[str] = []
    for raw in (name, full_name):
        text = re.sub(r"[\s（）()·、,，]", "", raw or "")
        if not text:
            continue
        if text not in aliases:
            aliases.append(text)
        core = _COMPANY_SUFFIX.sub("", _CITY_PREFIX.sub("", text))
        if len(core) >= 2 and core not in aliases:
            aliases.append(core)
    return aliases


def filter_relevant_hits(hits: list["SearchHit"], aliases: list[str]) -> list["SearchHit"]:
    """只保留标题或摘要里出现公司字号的搜索结果，挡掉同名/无关的通用文章。"""
    if not aliases:
        return hits
    return [hit for hit in hits if any(a in f"{hit.title}{hit.body}" for a in aliases)]


class SearchHit(BaseModel):
    """一条搜索结果。"""

    title: str = ""
    href: str = ""
    body: str = ""
    # 命中的是哪组关键词
    query: str = ""


class CompanyPoint(BaseModel):
    """评估依据。"""

    text: str = Field(description="一条依据，30 字以内")
    href: str = Field(default="", description="依据来自哪条搜索结果的链接；来自工商信息时留空")


# 文档字符串与字段说明会作为输出要求发给模型
class CompanyVerdict(BaseModel):
    """对这家公司的风险评估。"""

    risk: Literal["low", "medium", "high", "unknown"] = Field(
        description=(
            "low：没有与该公司相关的负面信息；medium：有少量或较早的劳动纠纷、差评；"
            "high：欠薪、大规模裁员、失信被执行、诈骗或经营异常等严重问题；"
            "unknown：搜索结果几乎都与该公司无关，无法判断"
        )
    )
    summary: str = Field(description="一句话结论，40 字以内")
    points: list[CompanyPoint] = Field(default_factory=list, description="最多 5 条依据")


class CompanyPage:
    """在已登录的浏览器里另开标签页读公司信息，读完就关，不打扰投递和回复。"""

    # 岗位详情页里的公司链接
    COMPANY_LINK = ".company-info a[href*='/gongsi/']"
    # 公司主页「工商信息」的每一项：<li><span class="t">企业名称：</span>值</li>
    BUSINESS = ".business-detail li"
    LINK_ID = re.compile(r"/gongsi/(?!job/)([\w~-]+)\.html")
    # 页面最长等待（毫秒）
    TIMEOUT = 20_000

    def __init__(self, session: BossSession) -> None:
        self._session = session

    @staticmethod
    def url(brand_id: str) -> str:
        return f"{BASE_URL}/gongsi/{brand_id}.html"

    @classmethod
    def brand_id_from(cls, href: str) -> str:
        """公司链接 → 公司 ID；不是公司主页链接返回空串。"""
        match = cls.LINK_ID.search(href)
        return match.group(1) if match else ""

    @staticmethod
    def parse_business(pairs: list[list[str]]) -> dict[str, str]:
        """[[字段名：, 值]] → {字段名: 值}，空值和「-」不要。"""
        info = {}
        for label, value in pairs:
            key, value = label.strip().rstrip("：:"), value.strip()
            if key and value and value != "-":
                info[key] = value
        return info

    async def brand_id(self, job_link: str) -> str:
        """打开岗位详情页，从公司链接里取公司 ID；找不到返回空串。"""
        if not job_link:
            return ""

        async def read(page: Page) -> str:
            link = await page.wait_for_selector(self.COMPANY_LINK, timeout=self.TIMEOUT)
            return self.brand_id_from(await link.get_attribute("href") or "") if link else ""

        return await self._read(job_link, read, "")

    async def business(self, brand_id: str) -> dict[str, str]:
        """打开公司主页读工商信息；页面上没有时返回空字典。"""

        async def read(page: Page) -> dict[str, str]:
            await page.wait_for_selector(self.BUSINESS, timeout=self.TIMEOUT)
            pairs = await page.eval_on_selector_all(
                self.BUSINESS,
                """items => items.map(li => {
                    const label = li.querySelector('.t');
                    const name = label ? label.innerText : '';
                    return [name, li.innerText.slice(name.length)];
                })""",
            )
            return self.parse_business(pairs)

        return await self._read(self.url(brand_id), read, {})

    async def _read[T](self, url: str, read: Callable[[Page], Awaitable[T]], empty: T) -> T:
        page = await self._session.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            return await read(page)
        except PlaywrightError:
            return empty
        finally:
            await page.close()


class CompanySearch:
    """以公司全称逐组上网搜负面关键词，合并去重。

    搜索源：360 搜索（``so.com``，国内可直连）优先，``ddgs`` 兜底；任一源可用即返回。
    关键词要短：堆太多词搜出来的都是通用维权文章；多组并行也会被搜索服务排队，不比逐组快。
    """

    QUERIES: ClassVar[tuple[str, ...]] = ("欠薪 裁员", "怎么样", "失信 被执行")
    SO_URL: ClassVar[str] = "https://www.so.com/s"
    # 桌面 UA：360 搜索对脚本 UA 会返回空壳页
    USER_AGENT: ClassVar[str] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    # ddgs 兜底源与区域
    backend: str = "yandex"
    region: str = "cn-zh"
    max_results: int = 8
    timeout: int = 15

    def __init__(self, text: Callable[[str], list[dict[str, Any]]] | None = None) -> None:
        # 测试时可换成假的搜索函数
        self._text = text or self._web

    # --- 真实搜索：多源依次尝试 ---

    def _web(self, query: str) -> list[dict[str, Any]]:
        """360 搜索优先，ddgs 兜底；全部失败时抛 ``RuntimeError``（带各源原因）。"""
        errors: list[str] = []
        for name, source in (("360 搜索", self._so), ("ddgs", self._ddgs)):
            try:
                results = source(query)
            except Exception as exc:  # noqa: BLE001 单源失败换下一个源
                errors.append(f"{name} {exception_text(exc)}")
                continue
            if results:
                return results
            errors.append(f"{name}没有返回结果")
        raise RuntimeError("；".join(errors) or "搜索服务不可用")

    def _so(self, query: str) -> list[dict[str, Any]]:
        """360 搜索：GET 结果页，再用 lxml 解析网页结果。"""
        request = Request(
            f"{self.SO_URL}?q={quote(query)}",
            headers={"User-Agent": self.USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        with _SO_LIMIT, urlopen(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8", "ignore")
        return self.parse_so(body, self.max_results)

    @staticmethod
    def parse_so(body: str, limit: int = 8) -> list[dict[str, str]]:
        """解析 360 结果页；跳过图片 / 相关搜索等不是网页结果的条目。"""
        results: list[dict[str, str]] = []
        for item in html.fromstring(body).cssselect("li.res-list"):
            links = item.cssselect("h3 a")
            if not links:
                continue
            href = (links[0].get("href") or "").strip()
            title = " ".join(links[0].text_content().split())
            if not href or not title:
                continue
            if "image.so.com" in href or "/i?src=imageonebox" in href or "so.com/s?q=" in href:
                continue
            if title.endswith(("_360图片", "_360搜索", "相关搜索")):
                continue
            descs = item.cssselect("p.res-desc")
            summary = " ".join(descs[0].text_content().split()) if descs else ""
            results.append({"title": title, "href": href, "body": summary})
            if len(results) >= limit:
                break
        return results

    def _ddgs(self, query: str) -> list[dict[str, Any]]:
        return DDGS(timeout=min(self.timeout, 10)).text(
            query, region=self.region, backend=self.backend, max_results=self.max_results
        )

    async def search(
        self, name: str, on_step: Callable[[str], Awaitable[None]] | None = None
    ) -> list[SearchHit]:
        """搜索失败的组跳过；全部失败时抛 ``RuntimeError``（带可读原因）。

        三组关键词并发搜索（原来逐组串行要 3 倍时间），每组仍走线程避免阻塞事件循环。
        """
        hits: dict[str, SearchHit] = {}
        failures: list[str] = []
        if on_step is not None:
            await on_step(f"正在网上搜索「{name}」…")
        results = await asyncio.gather(
            *(self._search_one(f"{name} {query}") for query in self.QUERIES),
            return_exceptions=True,
        )
        for _query, result in zip(self.QUERIES, results):
            if isinstance(result, BaseException):
                failures.append(str(result) or "搜索失败")
                continue
            for item in result:
                hit = SearchHit(
                    title=str(item.get("title") or ""),
                    href=str(item.get("href") or ""),
                    body=str(item.get("body") or ""),
                    query=_query,
                )
                if hit.href and hit.href not in hits:
                    hits[hit.href] = hit
        if len(failures) == len(self.QUERIES):
            raise RuntimeError(f"网络搜索失败：{failures[0]}")
        return list(hits.values())

    async def _search_one(self, query: str) -> list[dict[str, Any]]:
        # 异常向上抛，由 gather(return_exceptions=True) 收集后按失败组计数
        return await asyncio.to_thread(self._text, query)


class CompanyReviewer(BaseModel):
    """让大模型结合工商信息和搜索结果评估公司风险；服务来自配置中心「AI 服务」。"""

    INSTRUCTIONS: ClassVar[str] = (
        "你是求职者的背景调查助手，评估这家公司是否值得去面试。"
        "搜索结果里常混有同名或名字相近的其他公司、与该公司无关的通用维权文章，"
        "只采信明确指向该公司（全称或简称与所在地、行业一致）的内容，其余忽略。"
        "结合工商信息判断：经营状态异常（吊销、注销、停业）、成立时间很短或注册资本极低也要提示。"
        "依据要具体，能对应到搜索结果的附上它的链接。"
    )

    llm: LlmSettings = Field(default_factory=LlmSettings)
    timeout: float = 60

    @cached_property
    def agent(self) -> Agent[None, CompanyVerdict]:
        """结论走提示词 JSON 输出，理由同 ``JobReviewer.agent``。"""
        model = OpenAIChatModel(self.llm.model, provider=self.llm.provider)
        agent = Agent(
            model,
            output_type=PromptedOutput(CompanyVerdict),
            system_prompt=self.INSTRUCTIONS,
            retries={"output": OUTPUT_RETRIES},
            model_settings=OpenAIChatModelSettings(
                thinking=False, temperature=0, timeout=self.timeout, max_tokens=MAX_OUTPUT_TOKENS
            ),
        )
        return telemetry.attach(agent)

    async def review(
        self, name: str, full_name: str, info: dict[str, str], hits: list[SearchHit]
    ) -> CompanyVerdict:
        """接口出错或输出不合规时抛 ``AgentRunError``。"""
        result = await self.agent.run(self._prompt(name, full_name, info, hits))
        return result.output

    def _prompt(
        self, name: str, full_name: str, info: dict[str, str], hits: list[SearchHit]
    ) -> str:
        business = "\n".join(f"{k}：{v}" for k, v in info.items()) or "未取得工商信息"
        results = "\n\n".join(
            f"[{i}] {h.title}\n{h.href}\n{h.body}" for i, h in enumerate(hits, 1)
        ) or "没有搜索结果"
        return (
            f"公司简称：{name}\n企业全称：{full_name or '未知'}\n\n"
            f"工商信息：\n{business}\n\n搜索结果：\n{results}"
        )


class Pages(Protocol):
    async def brand_id(self, job_link: str) -> str: ...

    async def business(self, brand_id: str) -> dict[str, str]: ...


class Searcher(Protocol):
    async def search(
        self, name: str, on_step: Callable[[str], Awaitable[None]] | None = None
    ) -> list[SearchHit]: ...


class Reviewer(Protocol):
    async def review(
        self, name: str, full_name: str, info: dict[str, str], hits: list[SearchHit]
    ) -> CompanyVerdict: ...


class CompanyChecker:
    """串起整个查询：公司 ID → 工商信息 → 网上搜索 → AI 评估 → 入库。

    平台感知：只有 BOSS 有公司主页可读工商信息；其他平台按公司名搜索评估。
    AI 评估为高风险的公司自动进黑名单（手动查询与后台预查共用这条规则）。
    """

    NO_AI: ClassVar[str] = "未开通 AI 服务，只展示工商信息和搜索结果"

    def __init__(self, page: Pages | None, search: Searcher, reviewer: Reviewer | None) -> None:
        self._page = page
        self._search = search
        self._reviewer = reviewer

    async def check(self, job_uid: str, on_step: Callable[[str], Awaitable[None]]) -> str:
        """查这个岗位所属的公司，返回公司 ID；找不到公司或搜索全部失败时抛 ``RuntimeError``。"""
        job = JobRow.get_dict(job_uid)
        if job is None:
            raise RuntimeError("岗位不存在")
        name, brand_id = job["company"], job["brandId"]
        platform = str(job.get("platform") or "boss")
        if platform == "boss" and not brand_id:
            await on_step("正在打开岗位详情页，查找公司主页…")
            brand_id = await self._page.brand_id(job["link"])
            if not brand_id:
                raise RuntimeError("岗位详情页里没有找到公司主页，岗位可能已下线")
            JobRow.set_brand_id(job_uid, brand_id)

        return await self.check_company(
            platform=platform,
            brand_id=brand_id,
            name=name,
            link=job["link"] if platform == "boss" else "",
            on_step=on_step,
        )

    async def check_company(
        self,
        *,
        platform: str,
        brand_id: str = "",
        name: str = "",
        link: str = "",
        on_step: Callable[[str], Awaitable[None]] | None = None,
    ) -> str | None:
        """按公司键（ID 或名字）执行一次完整查询并入库；返回公司键，失败抛异常。

        ``page`` 给了且是 BOSS 平台才读工商信息；其他平台只用公司名网上搜索。
        黑名单键与公司键对齐：BOSS 用公司 ID，其他平台用 ``name:公司名``
        （黑名单按公司名匹配本就跨平台）。
        """
        step = on_step or (lambda _text: _noop())

        if platform == "boss" and brand_id and self._page is not None:
            await step("正在读取工商信息…")
            info = await self._page.business(brand_id)
        else:
            info = {}
        full_name = info.get("企业名称", "")

        hits = await self._search.search(full_name or name, on_step)
        aliases = company_aliases(name, full_name)
        relevant = filter_relevant_hits(hits, aliases)
        if len(relevant) != len(hits):
            await step(f"过滤掉 {len(hits) - len(relevant)} 条与公司无关的结果")
        hits = relevant

        if self._reviewer is None:
            verdict = CompanyVerdict(risk="unknown", summary=self.NO_AI)
        else:
            await step("AI 正在分析…")
            verdict = await self._reviewer.review(name, full_name, info, hits)

        company_key = brand_id or (f"name:{name}" if name else "")
        if not company_key:
            return None
        CompanyRow.save(
            company_key,
            name=name or brand_id,
            full_name=full_name,
            info=info,
            hits=[h.model_dump() for h in hits],
            risk=verdict.risk,
            summary=verdict.summary,
            points=[p.model_dump() for p in verdict.points],
        )
        # 高风险公司自动进黑名单，后续投递预筛直接跳过
        if verdict.risk == "high" and (name or brand_id):
            blacklist_key = company_key if platform == "boss" else f"name:{name}"
            CompanyBlacklistRow.add(
                brand_id=blacklist_key,
                name=name or brand_id,
                reason=str(verdict.summary or "高风险"),
                source="ai",
            )
        return company_key


async def _noop() -> None:
    return None


class CompanyPrefetcher:
    """公司背景预查：发现岗位时在后台自动查企业，不阻塞投递。

    - 并发上限 ``concurrency``（网络搜索 + 大模型调用都慢，限流防打爆）；
    - 同一家公司一次会话只查一次，且 7 天内查过的直接跳过（结果在库里）；
    - 高风险公司自动进黑名单（由 ``CompanyChecker.check_company`` 统一处理）。
    """

    FRESH_DAYS: ClassVar[int] = 7

    def __init__(self, checker_factory: Callable[[], CompanyChecker], *, concurrency: int = 2) -> None:
        # 工厂每次现建 checker：LLM 配置可能中途变化
        self._checker_factory = checker_factory
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._scheduled: set[str] = set()

    def schedule(self, *, platform: str, brand_id: str = "", name: str = "", link: str = "") -> bool:
        """排一个后台查询；公司已查过（未过期）或已排程时返回 False。"""
        key = brand_id or (f"name:{name}" if name else "")
        if not key or key in self._scheduled:
            return False
        self._scheduled.add(key)
        if key in CompanyRow.fresh([key], days=self.FRESH_DAYS):
            return False
        asyncio.create_task(self._run(platform=platform, key=key, brand_id=brand_id, name=name, link=link))
        return True

    async def _run(self, *, platform: str, key: str, brand_id: str, name: str, link: str) -> None:
        async with self._sem:
            try:
                checker = self._checker_factory()
                await checker.check_company(
                    platform=platform, brand_id=brand_id, name=name, link=link
                )
            except (RuntimeError, PlaywrightError, AgentRunError) as exc:
                # 失败的从排程集合移除：下次遇到同公司会重试（网络抖动/浏览器忙碌是暂时的）
                self._scheduled.discard(key)
                log(f"自动查企业失败（{name or brand_id}）：{exc}", "company")
                return
            except Exception as exc:  # noqa: BLE001 预查是后台任务，异常只记日志
                self._scheduled.discard(key)
                log(
                    f"自动查企业意外失败（{name or brand_id}）：{exception_text(exc)}",
                    "company",
                )
                return
        log(f"已自动完成公司背景检查：{name or brand_id}", "company")
