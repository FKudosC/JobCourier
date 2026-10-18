"""BOSS 聊天：守着聊天页，HR 发来新消息时按提示词和简历自动回复；消息入库并关联岗位。"""

from __future__ import annotations

import asyncio
import contextlib
import random
from datetime import datetime
from functools import cached_property
from itertools import takewhile
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from patchright.async_api import Error as PlaywrightError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import (
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from job.boss.filters import BASE_URL, KeywordFilter, ReplyPaceProfile
from job.boss.jobs import Job, LogSink, read_description
from job.models.blacklist import CompanyBlacklistRow
from job.models.chat import ChatMessage, ChatMessageRow, ChatStatusRow
from job.models.job import JobRow
from job.models.pending import ReplyPendingRow
from job.models.setting import LlmSettings
from job.utils import as_dict, log, telemetry
from job.utils.llm import MAX_OUTPUT_TOKENS, OUTPUT_RETRIES, explain

if TYPE_CHECKING:
    from patchright.async_api import Page, Response

    from job.boss.review import JobReviewer
    from job.boss.session import BossSession


class Friend(BaseModel):
    """会话列表接口 getGeekFriendList.json 里的一位 HR。"""

    model_config = ConfigDict(
        validate_by_name=True,
        validate_by_alias=True,
        str_strip_whitespace=True,
        coerce_numbers_to_str=True,
    )

    boss_id: str = Field(default="", validation_alias="encryptBossId")
    # HR 的数字 ID：最后一条消息的发送方是它，说明在等我回复
    uid: str = ""
    name: str = ""
    company: str = Field(default="", validation_alias="brandName")
    job_id: str = Field(default="", validation_alias="encryptJobId")
    last_from: str = Field(default="", validation_alias=AliasPath("lastMessageInfo", "fromId"))
    last_mid: str = Field(default="", validation_alias=AliasPath("lastMessageInfo", "msgId"))

    @field_validator("*", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Any) -> Any:
        """接口里的 null 按空串处理。"""
        return "" if value is None else value

    @property
    def waiting(self) -> bool:
        """最后一条是 HR 发的，在等我回复（在手机上看过也算）。"""
        return bool(self.uid) and self.last_from == self.uid

    @property
    def label(self) -> str:
        """日志里的称呼：「张女士 · 某科技」。"""
        return " · ".join(p for p in (self.name, self.company) if p)


def _validate_all[M: BaseModel](model: type[M], items: Any) -> list[M]:
    """列表里的每一项 → ``model``；格式不对的跳过。"""
    result = []
    for item in items if isinstance(items, list) else []:
        with contextlib.suppress(ValidationError):
            result.append(model.model_validate(item))
    return result


# HR 拒绝的常见说法；词要具体，宁漏勿误——误标会话会漏掉 HR 的后续消息
_REJECT_WORDS = (
    "不合适",
    "不考虑",
    "不太匹配",
    "匹配度不够",
    "与岗位要求不符",
    "招满",
    "已经招到",
    "招到了",
    "暂不考虑",
    "暂时不考虑",
    "不再推进",
    "放入人才库",
    "加入人才库",
    "祝求职顺利",
    "祝您求职",
)
# 命中这些词的多半是提问或 JD 讨论，不是拒绝
_REJECT_EXCLUSIONS = ("?", "？", "方便", "聊聊", "了解", "看看你")


def detect_rejection(text: str) -> bool:
    """程序侧判断 HR 这条消息是否明确拒绝：命中拒绝词、不带疑问、也不长篇大论。

    只用来做快路径——明确拒绝的会话直接标记结束，省一次 AI 调用；
    拿不准的一律返回 False，交给 AI 复核。
    """
    text = (text or "").strip()
    if not text or len(text) > 60:
        return False
    if any(mark in text for mark in _REJECT_EXCLUSIONS):
        return False
    return any(word in text for word in _REJECT_WORDS)


# HR 索要简历的说法；系统提示（已发送 / 已同意）不算
_RESUME_HINTS = ("附件简历", "发份简历", "发一份简历", "发个简历", "发下简历", "发我简历", "发个您的简历", "提供一份简历", "简历发我", "简历发一下", "投个简历")
_RESUME_EXCLUSIONS = ("已发送", "已同意", "已发出", "请求已")


def detect_resume_request(text: str) -> bool:
    """程序侧判断 HR 是否在索要简历，作为 AI 判断 ``send_resume`` 的兜底。"""
    text = (text or "").strip()
    if not text or any(mark in text for mark in _RESUME_EXCLUSIONS):
        return False
    return any(word in text for word in _RESUME_HINTS)


# HR 询问联系方式 / 交换微信的说法；命中即回复需要用户确认
_CONTACT_HINTS = (
    "微信",
    "联系方式",
    "电话号码",
    "留个电话",
    "手机号",
    "vx",
    "v信",
    "加个好友",
    "加个联系",
)


def detect_contact_request(text: str) -> bool:
    """程序侧判断 HR 是否在询问联系方式 / 交换微信。"""
    text = (text or "").strip().lower()
    if not text:
        return False
    return any(word in text for word in _CONTACT_HINTS)


# HR 的结束语：短、无疑问、命中这些说法时对话已自然结束，不再回复
_CLOSING_HINTS = (
    "谢谢",
    "拜拜",
    "再见",
    "先忙",
    "保持联系",
    "再联系",
    "有消息",
    "有合适",
    "到时候联系",
    "后续联系",
    "有需要联系",
)
# 单独的应答词：HR 只回一个词，对话已结束
_SHORT_ACKS = frozenset({"好的", "好", "嗯", "嗯嗯", "收到", "ok", "OK", "Ok", "好嘞", "行", "好嘞好嘞"})


def detect_closing(text: str) -> bool:
    """程序侧判断 HR 是否在说结束语：短、无疑问、命中告别说法或单独应答。

    只用于最后一条 HR 消息——长句和带问号的都不是结束语。
    """
    text = (text or "").strip()
    if not text or len(text) > 20 or "?" in text or "？" in text:
        return False
    if text in _SHORT_ACKS:
        return True
    return any(word in text for word in _CLOSING_HINTS)


def job_from_boss_data(payload: dict[str, Any]) -> Job:
    """打开会话时的 getBossData 接口 → 岗位（没有职位描述）。"""
    zp = as_dict(payload.get("zpData"))
    data, job = as_dict(zp.get("data")), as_dict(zp.get("job"))
    return Job.model_validate(
        {
            "job_id": data.get("encryptJobId"),
            "title": job.get("jobName"),
            "salary": job.get("salaryDesc"),
            "company": job.get("brandName") or data.get("companyName"),
            "brand_id": job.get("encryptBrandId") or data.get("encryptBrandId"),
            "location": job.get("locationName"),
            "experience": job.get("experienceName"),
            "education": job.get("degreeName"),
            "hr_name": data.get("name"),
            "hr_title": data.get("title"),
        }
    )


# 文档字符串与字段说明会作为输出要求发给模型
class ChatDecision(BaseModel):
    """这次回复的判断：会话走向与要发给 HR 的回复。"""

    outcome: Literal["continue", "hr_rejected", "declined"] = Field(
        description=(
            "hr_rejected：HR 明确表示我不合适、不考虑或岗位已招满；"
            "declined：我按岗位判断婉拒这个岗位；continue：其他情况，继续沟通"
        )
    )
    reply: str = Field(
        default="",
        description="要发给 HR 的回复正文，不加引号、署名或任何解释，不换行；hr_rejected 时留空",
    )
    send_resume: bool = Field(
        default=False,
        description="HR 在索要简历时为 true，回复发出后会自动发送附件简历",
    )
    interview: bool = Field(
        default=False,
        description=(
            "只看 HR 说了什么：HR 主动邀请面试，或 HR 在和我确认面试时间、地点、方式时为 true；"
            "HR 还没邀请、只是我方在回复里提出或询问面试（如「方便先线上面试吗」），"
            "HR 只是介绍面试流程（如「我们是线下面试」），或我方婉拒时都为 false"
        ),
    )

    @field_validator("reply")
    @classmethod
    def _one_line(cls, value: str) -> str:
        return " ".join(value.split())


class ChatReplier(BaseModel):
    """按「自动回复」提示词、简历、岗位与聊天记录生成给 HR 的回复，并判断会话是否已结束。"""

    # 附带给模型的最近消息条数
    HISTORY: ClassVar[int] = 20

    prompt: str
    resume: str
    llm: LlmSettings
    timeout: float = 60

    @cached_property
    def agent(self) -> Agent[None, ChatDecision]:
        model = OpenAIChatModel(self.llm.model, provider=self.llm.provider)
        agent = Agent(
            model,
            output_type=PromptedOutput(ChatDecision),
            system_prompt=self.prompt,
            retries={"output": OUTPUT_RETRIES},
            model_settings=OpenAIChatModelSettings(
                thinking=False, temperature=0.7, timeout=self.timeout, max_tokens=MAX_OUTPUT_TOKENS
            ),
        )
        return telemetry.attach(agent)

    async def reply(self, job: Job, history: list[ChatMessage], verdict: str) -> ChatDecision:
        """判断会话走向并生成回复；接口出错时抛 ``AgentRunError``。"""
        return (await self.agent.run(self.build_prompt(job, history, verdict))).output

    def build_prompt(self, job: Job, history: list[ChatMessage], verdict: str) -> str:
        lines = [f"{'HR' if m.from_hr else '我'}：{m.text}" for m in history if m.text]
        parts = [
            f"我的简历：\n{self.resume}",
            (
                f"沟通的岗位：{job.title} · {job.company} · {job.salary or '薪资未知'}\n"
                f"岗位详情：\n{job.description or '（暂无）'}"
            ),
            f"岗位判断：{verdict}" if verdict else "",
            "聊天记录（从早到晚）：\n" + "\n".join(lines[-self.HISTORY :]),
            "请回复 HR 的最新消息。",
        ]
        return "\n\n".join(p for p in parts if p)


class ChatResponder:
    """在单独的标签页守着 BOSS 聊天页，逐个回复 HR 发来的新消息。

    聊天页只打开一次，之后在页面里直接查询最近的会话，不再刷新页面。
    流程：查询最近会话 → 找出等我回复的会话 → 打开会话读取岗位与消息 →
    新消息入库并打印 → HR 主动沟通的新岗位按投递规则判断后入库 → 按回复节奏等待 →
    AI 判断会话走向并生成回复 → 模拟打字发送；HR 已拒绝或我方婉拒的会话会被标记。
    """

    CHAT_URL = f"{BASE_URL}/web/geek/chat"
    PLATFORM: ClassVar[str] = "boss"
    # 打开会话时的 HR 与岗位接口
    BOSS_API = "getBossData"
    # 每次查询最近更新的会话数
    RECENT = 20
    # 在页面里查询最近会话：先按更新时间取会话 ID，再批量取详情；返回 {code, result}
    FRIENDS_JS = """
    async (limit) => {
      const ids = await fetch('/wapi/zprelation/friend/geekFilterByLabel?labelId=0')
        .then(r => r.json());
      if (ids.code !== 0) return {code: ids.code, result: []};
      const top = (ids.zpData?.friendList || []).slice(0, limit).map(f => f.friendId);
      if (!top.length) return {code: 0, result: []};
      const detail = await fetch('/wapi/zprelation/friend/getGeekFriendList.json', {
        method: 'POST',
        headers: {'content-type': 'application/x-www-form-urlencoded'},
        body: 'friendIds=' + encodeURIComponent(top.join(',')),
      }).then(r => r.json());
      return {code: detail.code, result: detail.zpData?.result || []};
    }
    """
    # 会话列表项
    ITEM = ".friend-content"
    # 当前会话的消息
    MESSAGE = ".chat-message li.message-item"
    INPUT = "#chat-input"
    SEND = ".btn-send"
    # 工具栏的「发简历」按钮与点开后的确认面板
    RESUME_BUTTON = ".chat-controls .toolbar-btn"
    RESUME_PANEL = ".panel-resume"
    RESUME_SENT = "附件简历请求已发送"
    # HR 主动索要附件简历的卡片，带「拒绝 / 同意」按钮
    RESUME_REQUEST = "附件简历"
    LOGIN = ".login-dialog-wrap"
    # 两次刷新会话列表之间的等待（秒）
    POLL: ClassVar[tuple[float, float]] = (60, 120)
    # 打开聊天页遇到网络异常时的重试间隔（秒）
    RETRY = 60
    # 打字时每个字的间隔（毫秒）
    TYPING: ClassVar[tuple[float, float]] = (80, 220)
    # 读取当前会话消息：[{mid, from_hr, text}]，系统提示等既不是 HR 也不是我的跳过
    # HR 索要简历、交换微信等卡片没有 .text-content，去掉时间、头像和按钮后取剩下的文字
    READ_JS = """
    (selector) => {
      const NOISE = '.message-time, .time, .figure, .avatar, img, button, [class*="btn"],'
        + ' [class*="status"], [class*="operate"]';
      const BUTTONS = new Set(['拒绝', '同意', '接受', '查看', '确定', '取消']);
      const cardText = (li) => {
        const parts = [];
        const noisy = (el) => {
          const hit = el.closest(NOISE);
          return hit && hit !== li && li.contains(hit);
        };
        const walker = document.createTreeWalker(li, NodeFilter.SHOW_TEXT);
        for (let node; (node = walker.nextNode());) {
          const text = node.textContent.trim();
          if (text && !noisy(node.parentElement) && !BUTTONS.has(text)
              && !/^\\d{1,2}:\\d{2}$/.test(text)) parts.push(text);
        }
        return parts.join(' ');
      };
      return [...document.querySelectorAll(selector)]
        .filter(li => li.dataset.mid
          && (li.classList.contains('item-friend') || li.classList.contains('item-myself')))
        .map(li => {
          const text = li.querySelector('.text-content')?.innerText?.trim();
          const full = text || cardText(li);
          // BOSS 系统卡片（竞争力 PK 报告等）不是 HR 说话，直接丢弃
          if (/你与该职位竞争者|竞争者PK/.test(full)) return null;
          return {
            mid: li.dataset.mid,
            from_hr: li.classList.contains('item-friend'),
            text: full,
          };
        })
        .filter(Boolean);
    }
    """

    def __init__(self, session: BossSession, company_prefetcher=None) -> None:
        self.session = session
        self._stop = asyncio.Event()
        # 公司背景预查器（由工作台注入）；None 表示不启用
        self._prefetcher = company_prefetcher
        # 已处理过的 HR 最后一条消息，失败也不再重复处理
        self._handled: set[str] = set()
        self._replier: ChatReplier | None = None
        self._pace = ReplyPaceProfile()
        self._keywords = KeywordFilter()
        self._reviewer: JobReviewer | None = None
        self._on_log: LogSink | None = None
        self._replied = 0
        # 开启后 AI 生成的回复先进「待确认回复」，消息页同意后才发出
        self._confirm = False
        # 自动拉黑主动联系的 HR（可由自动回复设置关闭）
        self._block_proactive_hr = True
        # 黑名单缓存：公司 ID / 公司名 → 原因；每次 run 开始时整表载入
        self._blacklist_ids: dict[str, str] = {}
        self._blacklist_names: dict[str, str] = {}

    def _prefetch_company(self, job: Job) -> None:
        """HR 主动沟通的新岗位入列时把公司交给后台预查；没接预查器时什么都不做。"""
        if self._prefetcher is not None:
            self._prefetcher.schedule(
                platform=self.PLATFORM, brand_id=job.brand_id, name=job.company, link=job.link
            )

    def _load_blacklist(self) -> None:
        """整表载入黑名单，按公司 ID 与公司名两个索引缓存。"""
        self._blacklist_ids = {}
        self._blacklist_names = {}
        for key, reason in CompanyBlacklistRow.names().items():
            if key.startswith("name:"):
                self._blacklist_names[key[5:]] = reason
            else:
                self._blacklist_ids[key] = reason

    def _blacklist_reason(self, brand_id: str, company: str) -> str:
        """黑名单命中返回原因，没命中返回空串。"""
        if brand_id and brand_id in self._blacklist_ids:
            return self._blacklist_ids[brand_id]
        return self._blacklist_names.get(company.strip(), "")

    def request_stop(self) -> None:
        """请求停止自动回复。"""
        self._stop.set()

    async def run(
        self,
        replier: ChatReplier,
        *,
        pace: ReplyPaceProfile | None = None,
        keywords: KeywordFilter | None = None,
        reviewer: JobReviewer | None = None,
        confirm: bool = False,
        block_proactive_hr: bool = True,
        on_log: LogSink | None = None,
    ) -> str:
        """一直守着聊天页直到请求停止；返回 stopped / need_login。

        只在回复时段内回复；HR 主动沟通的新岗位按 ``keywords`` 与 ``reviewer`` 判断是否合适后入库。
        ``confirm`` 为真时不直接发送，AI 的回复进「待确认回复」队列等人同意。
        ``block_proactive_hr`` 控制是否自动拉黑主动联系的 HR。
        """
        self._stop.clear()
        # 重新开启时再检查一遍，上次停止前没回复完的会话不会被漏掉
        self._handled.clear()
        self._replier = replier
        self._pace = pace or ReplyPaceProfile()
        self._keywords = keywords or KeywordFilter()
        self._reviewer = reviewer
        self._confirm = confirm
        self._block_proactive_hr = block_proactive_hr
        self._on_log = on_log
        self._replied = 0
        self._load_blacklist()
        if purged := ChatMessageRow.purge_system_cards():
            log(f"已清理 {purged} 条系统卡片消息（竞争力 PK 报告等）", "chat")
        log("启动浏览器", "chat")
        page = await self.session.new_page()
        try:
            log("打开聊天页", "chat")
            if not await self._reopen(page):
                return "need_login"
            low, high = self.POLL
            await self._log(
                f"已打开聊天页，正在等待 HR 的新消息（每 {low / 60:g}–{high / 60:g} 分钟检查一次）"
            )
            while not self._stop.is_set():
                now = datetime.now().astimezone()
                if not self._pace.is_active(now):
                    start = self._pace.next_active(now)
                    await self._log(f"不在回复时段，{start:%m-%d %H:%M} 后继续回复")
                    await self._sleep((start - now).total_seconds())
                    continue
                await self._flush_approved(page)
                friends = await self._recent_friends(page)
                if friends is None:
                    if not await self._reopen(page):
                        return "need_login"
                    continue
                pending = [
                    f
                    for f in friends
                    if f.waiting
                    and f.last_mid not in self._handled
                    and not ReplyPendingRow.seen_for(f.last_mid)
                ]
                if pending:
                    # 最后一条是 HR 发的也可能只是系统消息（如发简历后的提示），打开后才确定
                    log(f"检查 {len(pending)} 个最后一条来自 HR 的会话", "chat")
                for friend in pending:
                    if self._stop.is_set():
                        break
                    self._handled.add(friend.last_mid)
                    await self._handle(page, friend)
                await self._sync_recent(page, friends)
                await self._sleep(random.uniform(*self.POLL))
            return "stopped"
        finally:
            with contextlib.suppress(PlaywrightError):
                await page.close()

    async def _reopen(self, page: Page) -> bool:
        """打开聊天页，网络异常时每隔一段时间重试，直到恢复或请求停止；需要登录时返回 False。"""
        while (opened := await self._open_page(page)) is None:
            await self._log(f"网络异常，打开聊天页失败，{self.RETRY} 秒后重试", "warn")
            await self._sleep(self.RETRY)
            if self._stop.is_set():
                return True
        return opened

    async def _open_page(self, page: Page) -> bool | None:
        """打开聊天页并等会话列表出现；需要登录时返回 False，网络异常返回 None。"""
        try:
            await page.goto(self.CHAT_URL, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            log(f"打开聊天页失败：{exc!r}", "chat")
            return None
        try:
            await page.wait_for_selector(self.ITEM, timeout=20_000)
        except PlaywrightTimeoutError:
            return not (await page.locator(self.LOGIN).is_visible() or "login" in page.url)
        return True

    async def _recent_friends(self, page: Page) -> list[Friend] | None:
        """在页面里查询最近更新的会话；接口出错（如登录失效）返回 None。"""
        try:
            payload = as_dict(
                await asyncio.wait_for(page.evaluate(self.FRIENDS_JS, self.RECENT), 30)
            )
        except (PlaywrightError, TimeoutError) as exc:
            log(f"查询会话失败：{exc!r}", "chat")
            return None
        if payload.get("code") != 0:
            log(f"查询会话失败：code={payload.get('code')}", "chat")
            return None
        log(f"查询到 {len(payload.get('result') or [])} 个最近会话", "chat")
        return self.parse_friends(payload.get("result"))

    @staticmethod
    def parse_friends(items: Any) -> list[Friend]:
        """会话详情列表 → Friend；缺 HR ID 或格式不对的跳过。"""
        return [f for f in _validate_all(Friend, items) if f.boss_id]

    async def _handle(self, page: Page, friend: Friend) -> None:
        """处理一位 HR 的新消息：读取入库 → 判断岗位 → 等待 → 回复。"""
        job = await self._open_chat(page, friend)
        if job is None:
            # 会话列表是虚拟滚动，找不到时重新打开聊天页再试一次
            await self._open_page(page)
            job = await self._open_chat(page, friend)
        if job is None:
            await self._log(f"{friend.label}（打开会话失败，请手动查看）", "warn")
            return
        uid = JobRow.uid_for(job) if job.job_id else ""
        messages = await self._read_messages(page)
        fresh = self._record(messages, friend, uid, job)
        if any(not m.from_hr for m in fresh):
            # 我方（手动或自动）已经发出新消息：这个会话还挂着的待确认回复自动了结
            ReplyPendingRow.resolve_open(friend.boss_id)
        incoming = [m for m in fresh if m.from_hr and m.text]
        for message in incoming:
            await self._log(f"{friend.label}：{message.text}", "recv")
        if not (pending := self.unanswered(messages)):
            log(f"{friend.label} 没有待回复的消息（最后一条是系统消息），跳过", "chat")
            return
        hr_text = pending[-1].text
        # 主动联系的 HR：岗位不在库里说明从未投递过，直接拉黑禁止回复（可开关）
        if (uid == "" or JobRow.get_dict(uid) is None) and self._block_proactive_hr:
            CompanyBlacklistRow.add(
                brand_id=job.brand_id,
                name=job.company,
                reason="主动联系的 HR，禁止回复",
                source="hr",
            )
            ChatStatusRow.mark(
                friend.boss_id, "blocked", source="auto", text=incoming[-1].text if incoming else ""
            )
            await self._log(f"{friend.label}（主动联系的 HR，已拉黑并禁止回复）", "skip")
            return
        # 明确拒绝的说法走程序快路径，省一次 AI 调用；拿不准的仍交给 AI
        if detect_rejection(hr_text):
            ChatStatusRow.follow_ai(
                friend.boss_id, outcome="hr_rejected", interview=False, text=hr_text
            )
            await self._log(f"{friend.label}（HR 明确拒绝，不再回复）", "skip")
            return
        # HR 的结束语（如「好的谢谢」「先忙了」）：对话已自然结束，不再回复
        if detect_closing(hr_text):
            await self._log(f"{friend.label}（HR 结束了对话：{hr_text}，不再回复）", "skip")
            return
        if not incoming and ChatStatusRow.ended(friend.boss_id):
            await self._log(f"{friend.label}（会话已结束，没有新消息，跳过）", "skip")
            return

        verdict, job = await self._judge(page, job, uid)
        await self._sleep(random.uniform(*self._pace.delay))
        if self._stop.is_set():
            return
        assert self._replier is not None
        try:
            decision = await self._replier.reply(job, messages, verdict)
        except AgentRunError as exc:
            await self._log(f"{friend.label}（AI 回复失败，{explain(exc)}，请手动回复）", "warn")
            return
        await self._apply(page, friend, uid, decision, hr_text, job)

    @staticmethod
    def unanswered(messages: list[ChatMessage]) -> list[ChatMessage]:
        """我最后一条消息之后 HR 发来的消息（上次入库后没来得及回复的也算）。"""
        tail = takewhile(lambda m: m.from_hr, reversed(messages))
        return [m for m in reversed(list(tail)) if m.text]

    @staticmethod
    def _record(
        messages: list[ChatMessage],
        friend: Friend,
        uid: str,
        job: "Job | None" = None,
        *,
        auto: bool = False,
    ) -> list[ChatMessage]:
        """消息入库并关联岗位，返回新入库的。

        公司与职位随手记在消息上：会话没关联到库内岗位（如 HR 主动沟通的
        新岗位）时，消息页也能显示公司名和沟通职位。
        """
        return ChatMessageRow.record_new(
            messages,
            job_uid=uid,
            boss_id=friend.boss_id,
            hr_name=friend.name,
            company=friend.company,
            job_title=job.title if job else "",
            auto=auto,
        )

    async def _apply(
        self,
        page: Page,
        friend: Friend,
        uid: str,
        decision: ChatDecision,
        hr_text: str,
        job: "Job | None" = None,
    ) -> None:
        """按 AI 的判断处理：HR 已拒绝就只更新沟通状态；否则发出回复，发出后再更新沟通状态。

        两类回复必须等用户同意才会发出：
        - HR 询问联系方式 / 交换微信 / 索要简历（无论是否开启「发送前确认」）；
        - 用户开启了「发送前确认」的所有回复。
        """
        if decision.outcome == "hr_rejected":
            ChatStatusRow.follow_ai(
                friend.boss_id, outcome=decision.outcome, interview=decision.interview, text=hr_text
            )
            await self._log(f"{friend.label}（HR 已拒绝，不再回复）", "skip")
            return
        text = decision.reply
        if not text:
            await self._log(f"{friend.label}（AI 未给出回复内容，请手动回复）", "warn")
            return
        wants_resume = decision.send_resume or detect_resume_request(hr_text)
        contact_ask = detect_contact_request(hr_text)
        # HR 索要联系方式 / 简历：必须用户同意，绝不自动回复或自动发简历
        needs_consent = contact_ask or wants_resume
        if needs_consent or self._confirm:
            ReplyPendingRow.create(
                boss_id=friend.boss_id,
                job_uid=uid,
                hr_name=friend.name,
                company=friend.company,
                text=text,
                outcome=decision.outcome,
                send_resume=wants_resume,
                last_mid=friend.last_mid,
            )
            if contact_ask or (wants_resume and not self._confirm):
                # HR 索要联系方式/简历：醒目提示等用户同意
                await self._log(
                    f"{friend.label}（HR 索要联系方式/简历，回复待确认——同意后才会发送"
                    + ("并发送简历）" if wants_resume else "）"),
                    "manual",
                )
            else:
                await self._log(f"{friend.label}（已生成待确认回复，请到「消息」页确认后发送）")
            return
        if not await self._send(page, text):
            await self._log(f"{friend.label}（发送失败，请手动回复）", "warn")
            return
        self._record(await self._read_messages(page), friend, uid, job, auto=True)
        declined = decision.outcome == "declined"
        status = ChatStatusRow.follow_ai(
            friend.boss_id, outcome=decision.outcome, interview=decision.interview, text=text
        )
        if status == "invited":
            await self._log(f"{friend.label}（识别到面试邀请，已标记有面试）")
        self._replied += 1
        await self._log(f"回复 {friend.label}{'（已婉拒）' if declined else ''}：{text}", "reply")
        await self._rest_after(self._replied)

    async def _flush_approved(self, page: Page) -> None:
        """把用户在消息页同意的待确认回复逐条发出；发送失败的退回待确认再试。"""
        rows = ReplyPendingRow.approved()
        for row in rows:
            if self._stop.is_set():
                return
            friend = Friend(boss_id=row.boss_id, name=row.hr_name, company=row.company)
            if not friend.label:
                await self._log("待确认回复缺少会话信息，请到 BOSS 直聘手动发送", "warn")
                ReplyPendingRow.mark_sent(row.id, failed=True)
                continue
            job = await self._open_chat(page, friend)
            if job is None:
                await self._open_page(page)
                job = await self._open_chat(page, friend)
            if job is None or not await self._send(page, row.text):
                await self._log(f"{friend.label}（发送待确认回复失败，稍后再试）", "warn")
                continue
            ReplyPendingRow.mark_sent(row.id)
            uid = row.job_uid or (JobRow.uid_for(job) if job.job_id else "")
            self._record(await self._read_messages(page), friend, uid, job, auto=True)
            ChatStatusRow.follow_ai(
                row.boss_id, outcome=row.outcome, interview=False, text=row.text
            )
            self._replied += 1
            await self._log(f"回复 {friend.label}（已确认）：{row.text}", "reply")
            if row.send_resume:
                if await self._accept_resume_request(page):
                    await self._log(f"{friend.label}（已同意 HR 的附件简历请求）", "reply")
                elif await self._send_resume(page):
                    await self._log(f"{friend.label}（已发送附件简历请求，对方确认后发到邮箱）", "reply")
                else:
                    await self._log(f"{friend.label}（发简历失败，请手动发送）", "warn")
            await self._rest_after(self._replied)

    async def _sync_recent(self, page: Page, friends: list[Friend]) -> None:
        """把最近会话里还没入库的读一遍入库。

        自动投递打招呼产生的会话最后一条是我发的消息，不算「等我回复」，
        正常轮询永远不会打开它们——消息页因此看不到聊天内容。这里按
        「库里没有这个 HR 的消息」主动补读，只记录不回复。
        """
        known = ChatMessageRow.known_boss_ids()
        statuses = ChatStatusRow.statuses()
        fresh = [
            f
            for f in friends
            if f.boss_id
            and f.boss_id not in known
            and f.last_mid not in self._handled
            and statuses.get(f.boss_id) != "blocked"
        ]
        if not fresh:
            return
        log(f"同步 {len(fresh)} 个未入库的会话", "chat")
        for friend in fresh:
            if self._stop.is_set():
                return
            self._handled.add(friend.last_mid)
            await self._sync_chat(page, friend)

    async def _sync_chat(self, page: Page, friend: Friend) -> None:
        """打开一个还没入库的会话，把消息读入库；打开失败静默跳过。"""
        job = await self._open_chat(page, friend)
        if job is None:
            # 会话列表是虚拟滚动，找不到时重新打开聊天页再试一次
            await self._open_page(page)
            job = await self._open_chat(page, friend)
        if job is None:
            return
        uid = JobRow.uid_for(job) if job.job_id else ""
        messages = await self._read_messages(page)
        if self._record(messages, friend, uid, job):
            await self._log(f"已同步会话：{friend.label}")

    async def _open_chat(self, page: Page, friend: Friend) -> Job | None:
        """在会话列表里点开这位 HR，返回会话对应的岗位；找不到或加载失败返回 None。"""
        item = (
            page.locator(self.ITEM)
            .filter(has_text=friend.name)
            .filter(has_text=friend.company)
            .first
        )

        def is_boss_data(response: Response) -> bool:
            return response.status == 200 and self.BOSS_API in response.url

        try:
            async with page.expect_response(is_boss_data, timeout=10_000) as resp:
                await item.click(timeout=5_000)
            payload = as_dict(await (await resp.value).json())
            await page.wait_for_selector(self.MESSAGE, timeout=10_000)
        except (PlaywrightError, ValueError):
            return None
        job = job_from_boss_data(payload)
        if not job.job_id and friend.job_id:
            job = job.model_copy(update={"job_id": friend.job_id})
        return job

    async def _read_messages(self, page: Page) -> list[ChatMessage]:
        """读取当前会话里的消息（从早到晚）。"""
        try:
            return _validate_all(ChatMessage, await page.evaluate(self.READ_JS, self.MESSAGE))
        except PlaywrightError:
            return []

    async def _judge(self, page: Page, job: Job, uid: str) -> tuple[str, Job]:
        """岗位是否合适的说明（给 AI 参考）与补全后的岗位。

        库里已有就沿用之前的判断；没有说明是 HR 主动沟通的新岗位，按投递规则判断后入库。
        """
        if uid and (row := JobRow.get_dict(uid)):
            description = row["description"]
            # 投递时详情接口偶尔没返回描述，这里补读并回写
            if not description and (description := await self._description(page, job)):
                JobRow.set_description(uid, description)
            job = job.model_copy(update={"description": description})
            return self._verdict(row["suitable"], row["reason"]), job
        if not uid:
            return "", job

        job = job.model_copy(update={"description": await self._description(page, job)})
        self._prefetch_company(job)
        name = f"{job.title} · {job.company}"
        if reason := self._blacklist_reason(job.brand_id, job.company):
            JobRow.record(job, suitable=False, reason=reason)
            await self._log(f"{name}（HR 主动沟通的新岗位，{reason}）", "skip")
            return self._verdict(False, reason), job
        if reason := self._keywords.reject_reason(job.title, job.company):
            JobRow.record(job, suitable=False, reason=reason)
            await self._log(f"{name}（HR 主动沟通的新岗位，{reason}）", "skip")
            return self._verdict(False, reason), job
        suitable, reason, score, detail = True, "HR 主动沟通", None, None
        if self._reviewer:
            try:
                result = await self._reviewer.check(job)
            except AgentRunError as exc:
                await self._log(f"{name}（AI 复核失败，先按合适处理：{exc}）", "warn")
            else:
                suitable, reason, score, detail = (
                    result.match,
                    result.reason,
                    result.score,
                    result.detail,
                )
        JobRow.record(
            job, suitable=suitable, reason=reason, score=score, detail=detail
        )
        match = "" if score is None else f" · 匹配 {score} 分"
        await self._log(
            f"{name}（HR 主动沟通的新岗位，{'合适' if suitable else '不合适'}：{reason}{match}）",
            "info" if suitable else "skip",
        )
        return self._verdict(suitable, reason), job

    @staticmethod
    def _verdict(suitable: bool, reason: str) -> str:
        head = "合适" if suitable else "不合适"
        return f"{head}（{reason}）" if reason else head

    async def _description(self, page: Page, job: Job) -> str:
        """在新标签页打开职位详情读职位描述（失败会重试）；读不到返回空串。"""
        if not job.link:
            return ""
        if not (description := await read_description(self.session, job.link)):
            await self._log(f"{job.title} 职位详情加载失败，按已有信息判断", "warn")
        return description

    async def _send(self, page: Page, text: str) -> bool:
        """模拟打字输入回复并发送；发出后返回 True。"""
        mine = page.locator(f"{self.MESSAGE}.item-myself")
        try:
            before = await mine.count()
            box = page.locator(self.INPUT)
            await box.click(timeout=5_000)
            await box.press_sequentially(text, delay=random.uniform(*self.TYPING))
            await page.locator(self.SEND).click(timeout=5_000)
            for _ in range(20):
                if await mine.count() > before:
                    return True
                await page.wait_for_timeout(500)
        except PlaywrightError:
            return False
        return False

    async def _accept_resume_request(self, page: Page) -> bool:
        """HR 发来「我想要一份您的附件简历」卡片时点「同意」；没有待处理的请求或点完按钮没消失返回 False。"""
        card = page.locator(f"{self.MESSAGE}.item-friend", has_text=self.RESUME_REQUEST).last
        agree = card.get_by_text("同意", exact=True)
        try:
            if not await agree.count() or not await agree.first.is_visible():
                return False
            await agree.first.click(timeout=5_000)
            for _ in range(20):
                if not await agree.count() or not await agree.first.is_visible():
                    return True
                await page.wait_for_timeout(500)
        except PlaywrightError:
            return False
        return False

    async def _send_resume(self, page: Page) -> bool:
        """点「发简历」并在弹出的面板里确定，聊天里出现「附件简历请求已发送」才算发出。

        按钮不可用（双方还没互动）、面板没出现或没等到提示时返回 False。
        """
        button = page.locator(self.RESUME_BUTTON, has_text="发简历").first
        panel = page.locator(self.RESUME_PANEL)
        sent = page.locator(".message-item", has_text=self.RESUME_SENT)
        try:
            if "unable" in (await button.get_attribute("class", timeout=5_000) or ""):
                return False
            before = await sent.count()
            await button.click(timeout=5_000)
            await panel.locator(".btn-v2", has_text="确定").first.click(timeout=5_000)
            for _ in range(20):
                if await sent.count() > before:
                    return True
                await page.wait_for_timeout(500)
        except PlaywrightError:
            return False
        return False

    async def _rest_after(self, count: int) -> None:
        """每回复满 ``rest_every`` 条歇一会（分钟）。"""
        every = self._pace.rest_every
        if every and count % every == 0:
            await self._log(f"已回复 {count} 条，休息一会")
            await self._sleep(random.uniform(*self._pace.rest) * 60)

    async def _log(self, text: str, level: str = "info") -> None:
        log(f"{level} {text}", "chat")
        if self._on_log is not None:
            await self._on_log(level, text)

    async def _sleep(self, seconds: float) -> None:
        """停顿；期间请求停止会立即返回。"""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), max(0.0, seconds))
