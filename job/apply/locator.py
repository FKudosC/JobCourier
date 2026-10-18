"""表单定位兜底：规则和文字版 AI 都认不出的栏位，截一小张图交给多模态模型指认。

官网表单里总有几栏光看文字认不出来——表格型表单把标签放在隔壁单元格、组件库的
假下拉没有可读文本、一栏里塞了两三个问题。这时把「标签 + 控件」那一小块截下来
让能读图的模型看一眼，比让纯文字模型硬猜准得多。

三条护栏，缺一不可：

1. **默认关闭**。截图会离开本机、发到用户自己配置的模型服务，画面里可能有个人
   资料，属于敏感数据，必须由用户主动打开；
2. **限个数、限时长**。一次扫描最多处理 :attr:`ApplyLocateSettings.max_fields`
   个栏位，每个栏位有超时，避免一次网申把几十张截图都甩给模型；
3. **失败回落**。模型说判断不了、超时、页面找不到控件，一律回落成「请手动填写」，
   绝不硬塞一个猜出来的值——填错比留空糟得多。

提示词里只放「资料键 + 中文标签」，不放取值：模型只需要认这是哪一栏，值由本机
自己查，能少发一点个人资料就少发一点。
"""

from __future__ import annotations

import asyncio
import base64
import re
from typing import TYPE_CHECKING, Any

from patchright.async_api import Error as PlaywrightError

from job.apply.matcher import align_option
from job.apply.profile import ApplyProfile
from job.models.setting import ApplyLocateSettings, LlmSettings

if TYPE_CHECKING:
    from patchright.async_api import Frame, Page

# 只处理这些控件类型；单选/多选靠选项文字判、文件上传另走一条路，都不掺和
_TYPES = frozenset(
    {"text", "textarea", "email", "tel", "url", "search", "number", "select-one", "select"}
)

# 模型说这类话就是没认出来，不算定位成功
_NO_ANSWER = ("无法判断", "不确定", "无法确定", "看不清", "认不出", "unknown", "cannot", "none")

# 资料键只允许字母数字下划线，别的字符一律剥掉
_KEY = re.compile("[^0-9A-Za-z_]")

# 给栏位截图前先挑一个「装得下标签和控件」的容器（``gj-shot`` 是标记，便于测试识别）：
# 从控件往上找，第一层高度、宽度都像一行表单的祖先就是要截的范围。
_SHOT_JS = """
/* gj-shot */
(el) => {
  document.querySelectorAll('[data-gj-shot]').forEach((n) => n.removeAttribute('data-gj-shot'));
  const style = window.getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return '';
  const box = el.getBoundingClientRect();
  if (box.width < 8 || box.height < 8) return '';
  let node = el;
  for (let i = 0; i < 4 && node.parentElement; i += 1) {
    const r = node.getBoundingClientRect();
    const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
    if (r.height >= 24 && r.height <= 220 && r.width >= 60 && r.width <= 760 && text) break;
    node = node.parentElement;
  }
  node.setAttribute('data-gj-shot', '1');
  return '[data-gj-shot="1"]';
}
"""


class FieldLocator:
    """把「规则没命中、又确实看得见」的栏位截图交给多模态模型指认。"""

    def __init__(
        self,
        profile: ApplyProfile,
        *,
        llm: LlmSettings | None = None,
        settings: ApplyLocateSettings | None = None,
    ) -> None:
        self.profile = profile
        self.llm = llm or LlmSettings.load()
        self.settings = settings or ApplyLocateSettings.load()

    @property
    def enabled(self) -> bool:
        """开着、大模型可用、有资料可填，三样齐了才谈得上定位。"""
        return bool(self.settings.enabled and self.llm.ready and self.model and self.profile)

    @property
    def model(self) -> str:
        return (self.llm.model or "").strip()

    async def locate(self, page: "Page", fields: list[Any]) -> int:
        """给匹配失败的少数栏位补值；返回真正补上的条数。"""
        if not self.enabled:
            return 0
        targets = [f for f in fields if self._eligible(f)][: max(0, int(self.settings.max_fields))]
        if not targets:
            return 0
        options = self._options()
        if not options:
            return 0
        done = 0
        for field in targets:
            key = ""
            try:
                key = await asyncio.wait_for(
                    self._identify(page, field, options), timeout=max(1, int(self.settings.timeout))
                )
            except Exception:  # noqa: BLE001 兜底失败不该拖垮整次扫描
                key = ""
            if self._apply(field, key):
                done += 1
            else:
                self._give_up(field)
        return done

    # --- 内部 ---

    @staticmethod
    def _eligible(field: Any) -> bool:
        """还没填上、看得见、又不是单选/多选/上传的普通栏位。"""
        if field.key or field.filled or field.is_file:
            return False
        if field.type in ("radio", "checkbox", "file", "hidden", "submit", "button", "password"):
            return False
        return field.type in _TYPES or field.is_fake_select or field.is_date_widget

    def _options(self) -> list[tuple[str, str]]:
        """可用资料：只给「键 + 中文标签」，取值不出本机。"""
        return [(entry.key, entry.label or entry.key) for entry in self.profile.entries]

    async def _identify(self, page: "Page", field: Any, options: list[tuple[str, str]]) -> str:
        """截图 → 问模型 → 解析出资料键；任何一步不成返回空串。"""
        png = await self._shot(page, field)
        if not png:
            return ""
        raw = await self._ask(field, options, png)
        names = {name for name, _ in options}
        key = _KEY.sub("", (raw or "").strip().strip("`\"'。.，,"))
        if key in names:
            return key
        # 模型偶尔会多写一句，取第一个能对上的词
        for token in re.split(r"[\s,，:：/|]+", raw or ""):
            candidate = _KEY.sub("", token.strip("`\"'。.，,"))
            if candidate in names:
                return candidate
        return ""

    async def _shot(self, page: "Page", field: Any) -> bytes | None:
        """把「标签 + 控件」那一小块截成 PNG；找不到或截图失败返回 ``None``。"""
        frame = self._frame(page, field)
        locator = self._locator(frame, field.selector)
        if locator is None:
            return None
        try:
            shot_selector = await locator.evaluate(_SHOT_JS)
        except PlaywrightError:
            return None
        if not isinstance(shot_selector, str) or not shot_selector:
            return None
        target = self._locator(frame, shot_selector)
        if target is None:
            return None
        try:
            return await target.screenshot()
        except PlaywrightError:
            return None

    async def _ask(self, field: Any, options: list[tuple[str, str]], png: bytes) -> str:
        """把这一栏的截图交给多模态模型，读回资料键。"""
        client = self.llm.provider.client
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        response = await client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._prompt(field, options)},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            max_tokens=24,
            temperature=0,
            timeout=max(1, int(self.settings.timeout)),
        )
        raw = (response.choices[0].message.content or "").strip()
        if not raw or any(word in raw.lower() for word in _NO_ANSWER):
            return ""
        return raw

    @staticmethod
    def _prompt(field: Any, options: list[tuple[str, str]]) -> str:
        label = field.label or field.placeholder or field.name or field.element_id or "（没有文字）"
        lines = [f"- {key}：{text}" for key, text in options]
        return "\n".join(
            [
                "这是企业官网网申表单里的一个栏位截图，请判断这一栏该填哪条资料。",
                f"页面上能读到的文字是「{label}」，控件类型是 {field.type}。",
                "",
                "可用资料（键：说明）：",
                *lines,
                "",
                "要求：",
                "1. 只输出资料键本身（例如 school），不要输出内容、不要解释；",
                "2. 只能从我列出的键里选；",
                "3. 判断不了就回复「无法判断」。",
            ]
        )

    def _apply(self, field: Any, key: str) -> bool:
        """把识别出的键落成这一栏的值；下拉对不上选项就当没识别出来。"""
        if not key:
            return False
        value = self.profile.get(key)
        if not value:
            return False
        if field.tag == "select" and field.options:
            value = align_option(value, field.options)
            if not value:
                return False
        field.key = key
        field.filled = value
        field.source = "AI 定位"
        field.confidence = 0.5
        field.ok = True
        field.note = ""
        return True

    @staticmethod
    def _give_up(field: Any) -> None:
        """没认出来就如实说：留空交给用户，别塞一个猜的值。"""
        field.ok = False
        field.note = "请手动填写"

    @staticmethod
    def _frame(page: "Page", field: Any) -> "Frame":
        """按帧序号取帧；帧已消失时退回主文档。"""
        frames = page.frames
        index = int(getattr(field, "frame", 0) or 0)
        return frames[index] if 0 <= index < len(frames) else page.main_frame

    @staticmethod
    def _locator(frame: "Frame", selector: str) -> Any:
        if not selector:
            return None
        try:
            return frame.locator(selector).first
        except PlaywrightError:
            return None
