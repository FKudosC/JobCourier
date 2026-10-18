"""表单视觉核对：把扫出来的栏位在截图上编号，请多模态模型核对「多没多、少没少」。

各家官网的表单结构千差万别，光靠 DOM 启发式扫出来的栏位清单总会有出入：表格型
表单把标签放在隔壁单元格、组件库的假下拉没有可读文本、上传组件在真控件旁边放
一个只读影子框……这些都会让网申面板上的清单和浏览器里实际要填的对不上。

这里换一个思路：**不让模型从零读表单**（必漏项，还贵），而是让它做**核对**——
DOM 已经扫出一份候选清单，程序在页面上照这份清单画好红框和编号，模型只需指出
「哪几个框其实不用填」「哪里还有一栏没框到」。这样模型的活儿小得多，也不容易漏。

落地方式是「页面内叠加标记 + 视口分片截图」，不用图像库：

1. 按控件自己的 ``data-gj-idx`` 标记，在页面里插一层 ``position: fixed`` 的
   红框和编号——用视口坐标定位，页面滚到哪儿、控件在不在 iframe 里都不会错位；
2. 长表单按视口切片：滚一段、画一次、截一张，最多 :attr:`ApplyVisionSettings.max_tiles`
   张，免得把几十屏都甩给模型；
3. 截完立刻把标记清掉，页面上不留痕。

护栏（缺一不可）：

* **只补行，不动页面**。核对结果只用来增删对照表里的行，绝不允许它点页面上的
  任何东西——填表仍然只走 :mod:`job.apply.filler`；
* **打码**。登录后的招聘系统常把姓名、手机号预填在表里，凡是**已经带着值**的
  栏位一律涂黑再截图；
* **限时长、限条数、失败放行**。一次核对有总超时，模型说看不到、超时、返回的
  不是 JSON，都直接沿用 DOM 扫出来的那份结果，只记日志；
* **填完不再截图**。只有扫描阶段会调用这里，那时页面上还没有本机填进去的资料。
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Any

from patchright.async_api import Error as PlaywrightError

from job.apply import safety
from job.apply.scanner import FormField
from job.models.setting import ApplyVisionSettings, LlmSettings

if TYPE_CHECKING:
    from patchright.async_api import Page

# 一次最多画多少个框：再多编号就挤成一团，模型反而看不清
_MAX_MARKS = 80
# 模型一次最多能报几个漏掉的栏位：给个上限，免得它把整页文字都当栏位报回来
_MAX_MISSING = 6
# 回话的 token 上限
_MAX_TOKENS = 512
# 滚到下一屏之后等一拍再截图：让懒加载的内容有机会挂上来
_SCROLL_WAIT_MS = 150
# 模型说这类话就是没看清图，当没核对
_NO_ANSWER = ("无法判断", "看不清", "看不到", "没有图片", "cannot", "unable")
# 从 ``selector`` 里抠出控件标记（``[data-gj-idx="12"]``）
_MARKER_RE = re.compile(r'data-gj-idx="([^"]+)"')

_GHOST_NOTE = "视觉核对：这一栏不用填写，已收起"
_MANUAL_NOTE = "视觉核对：官网有这一栏，程序没定位到，请在浏览器里手动填写"

# 滚到指定位置；返回实际滚到哪，便于排查
_SCROLL_JS = """
/* gj-vision-scroll */
(top) => {
  window.scrollTo(0, top);
  return window.scrollY;
}
"""

# 页面尺寸：文档总高 + 视口高，用来决定切几屏
_SIZE_JS = """
/* gj-vision-size */
() => {
  const doc = document.documentElement;
  const body = document.body;
  return {
    h: Math.max(doc ? doc.scrollHeight : 0, body ? body.scrollHeight : 0),
    vh: window.innerHeight || 0,
  };
}
"""

# 清标记：截完图立刻把叠加层摘掉，页面上不留痕
_CLEAR_JS = """
/* gj-vision */
() => {
  document.querySelectorAll('[data-gj-vision]').forEach((n) => n.remove());
  return 0;
}
"""

# 画标记：给每个候选栏位画红框 + 左上角编号；已经带着值的栏位额外涂一块黑。
# 用 ``position: fixed`` + 视口坐标——页面滚到哪儿、控件在不在 iframe 里，
# 标记都正好盖在控件上（iframe 内的 fixed 是相对该 iframe 自己的视口算的）。
_MARK_JS = r"""
/* gj-vision */
(payload) => {
  document.querySelectorAll('[data-gj-vision]').forEach((n) => n.remove());
  const body = document.body;
  if (!body) return 0;
  const vw = window.innerWidth || 0;
  const vh = window.innerHeight || 0;
  let drawn = 0;
  for (const item of payload.items) {
    const el = document.querySelector('[data-gj-idx="' + item.idx + '"]');
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
    const frame = document.createElement('div');
    frame.setAttribute('data-gj-vision', '1');
    frame.style.cssText =
      'position:fixed;z-index:2147483646;pointer-events:none;box-sizing:border-box;' +
      'left:' + r.left + 'px;top:' + r.top + 'px;' +
      'width:' + r.width + 'px;height:' + r.height + 'px;' +
      'outline:2px solid #e11d48;' +
      (item.mask ? 'background:#000;' : 'background:transparent;');
    body.appendChild(frame);
    const tag = document.createElement('span');
    tag.setAttribute('data-gj-vision', '1');
    tag.textContent = String(item.n);
    tag.style.cssText =
      'position:fixed;z-index:2147483647;pointer-events:none;' +
      'left:' + r.left + 'px;top:' + Math.max(0, r.top - 13) + 'px;' +
      'background:#e11d48;color:#fff;font:bold 11px/13px sans-serif;' +
      'padding:0 3px;border-radius:2px;';
    body.appendChild(tag);
    drawn += 1;
  }
  return drawn;
}
"""

# 按模型报出的栏位名，在页面里找那个还没被扫到的控件（``gj-vision-find`` 是标记）。
# 找到就按采集脚本的规矩打上 ``data-gj-idx``，填表时照样能定位到它。
_FIND_JS = r"""
/* gj-vision-find */
(payload) => {
  const norm = (s) => (s || '').toLowerCase()
    .replace(/[\s:：*＊()（）\[\]【】<>《》"'“”]/g, '');
  const want = norm(payload.text);
  if (want.length < 2) return null;
  const SEL =
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"])' +
    ':not([type="reset"]):not([type="image"]), select, textarea';
  const textOf = (el) =>
    (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const blobOf = (el) => {
    const bits = [el.name, el.id, el.placeholder,
      el.getAttribute('aria-label'), el.getAttribute('title')];
    let node = el;
    for (let i = 0; i < 3 && node; i += 1) {
      const t = textOf(node);
      if (t && t.length <= 40) bits.push(t);
      node = node.parentElement;
    }
    return norm(bits.filter(Boolean).join(' '));
  };
  for (const el of document.querySelectorAll(SEL)) {
    if (el.hasAttribute('data-gj-idx')) continue;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    if (!blobOf(el).includes(want)) continue;
    const tag = el.tagName.toLowerCase();
    const marker = String(payload.marker);
    el.setAttribute('data-gj-idx', marker);
    return {
      idx: marker,
      tag: tag,
      type: tag === 'select'
        ? 'select'
        : (el.getAttribute('type') || (tag === 'textarea' ? 'textarea' : 'text')),
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      label: payload.text || '',
      required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
      options: tag === 'select'
        ? [...el.options].map((o) => o.textContent.trim()).filter(Boolean)
        : [],
    };
  }
  return null;
}
"""


def _marker_of(item: Any) -> str:
    """从栏位的 ``selector`` 里抠出控件标记；没有标记就画不了框。"""
    found = _MARKER_RE.search(str(getattr(item, "selector", "") or ""))
    return found.group(1) if found else ""


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


@dataclass
class VisionResult:
    """一次视觉核对的结果。"""

    # 真的问过模型、拿到了可用的答复
    checked: bool = False
    # 被标成「不用填」的栏位数
    ghosts: int = 0
    # 补进来的栏位（可能带 selector，也可能只是个提示行）
    added: list[FormField] = dc_field(default_factory=list)
    message: str = ""


class VisionChecker:
    """把扫出来的栏位画在截图上，请多模态模型核对多没多、少没少。"""

    def __init__(
        self,
        *,
        llm: LlmSettings | None = None,
        settings: ApplyVisionSettings | None = None,
    ) -> None:
        self.llm = llm or LlmSettings.load()
        self.settings = settings or ApplyVisionSettings.load()

    @property
    def enabled(self) -> bool:
        """开着、大模型可用、有模型可调，三样齐了才谈得上核对。"""
        return bool(self.settings.enabled and self.llm.ready and self.model)

    @property
    def model(self) -> str:
        """识图模型：优先用单独选的，没选就用大模型服务里的主模型。"""
        return (self.settings.model or self.llm.model or "").strip()

    async def check(self, page: Page, fields: list[Any]) -> VisionResult:
        """核对一遍：把多出来的标掉、把漏掉的补进 ``fields``；返回做了什么。"""
        if not self.enabled:
            return VisionResult(message="未开启表单视觉核对")
        numbered = self._candidates(fields)[: _MAX_MARKS]
        if not numbered:
            return VisionResult(message="没有可核对的栏位")
        try:
            shots = await self._shots(page, numbered)
        except PlaywrightError as exc:
            return VisionResult(message=f"页面截图失败：{exc}")
        if not shots:
            return VisionResult(message="没截到页面")
        try:
            raw = await asyncio.wait_for(
                self._ask(shots, numbered), timeout=max(1, int(self.settings.timeout))
            )
        except Exception as exc:  # noqa: BLE001 核对是加分项，失败不该拖垮扫描
            return VisionResult(message=f"视觉核对失败：{exc}")
        extra, missing = self._parse(raw)
        if not raw:
            return VisionResult(message="视觉核对：模型没给结果，沿用普通扫描")
        if not extra and not missing:
            return VisionResult(checked=True, message="视觉核对：没有出入")
        return await self._merge(page, fields, numbered, extra, missing)

    # --- 截图 ---

    async def _shots(self, page: Page, numbered: list[Any]) -> list[bytes]:
        """按视口分片截图，每片都先画好标记；截完一定把标记清掉。"""
        size = await self._size(page)
        shots: list[bytes] = []
        try:
            for top in self._tiles(size, numbered):
                if top:
                    await page.evaluate(_SCROLL_JS, top)
                    await page.wait_for_timeout(_SCROLL_WAIT_MS)
                await self._draw(page, numbered)
                shots.append(await page.screenshot())
                await self._clear(page)
        finally:
            await self._clear(page)
        return shots

    async def _size(self, page: Page) -> dict[str, Any]:
        """页面总高与视口高；取不到就按「一屏」处理。"""
        try:
            raw = await page.evaluate(_SIZE_JS)
        except PlaywrightError:
            return {}
        return raw if isinstance(raw, dict) else {}

    def _tiles(self, size: dict[str, Any], numbered: list[Any]) -> list[int]:
        """要滚到哪几个位置截图。

        优先只切「候选栏位所在的那一段」，免得页头一大块横幅白白占掉几屏；
        候选栏位都算不出位置（比如整张表都在 iframe 里）时才退回整页切。
        """
        view = max(200, int(size.get("vh") or 0) or 720)
        height = max(view, int(size.get("h") or 0) or view)
        boxes = [b for b in (getattr(item, "box", None) or [] for item in numbered) if len(b) == 4]
        if boxes:
            start = max(0.0, min(b[1] for b in boxes) - 40)
            end = min(float(height), max(b[1] + b[3] for b in boxes) + 40)
        else:
            start, end = 0.0, float(height)
        span = max(0.0, end - start)
        limit = max(1, int(self.settings.max_tiles))
        count = max(1, min(limit, math.ceil(span / view) if span else 1))
        bottom = max(0, height - view)
        if count == 1:
            return [max(0, min(int(start), bottom))]
        step = span / count
        return [max(0, min(int(start + i * step), bottom)) for i in range(count)]

    async def _draw(self, page: Page, numbered: list[Any]) -> None:
        """在页面上画红框与编号；按 frame 分组，各画各的。"""
        frames = page.frames
        payload: dict[int, list[dict[str, Any]]] = {}
        for number, item in enumerate(numbered, 1):
            marker = _marker_of(item)
            if not marker:
                continue
            index = int(getattr(item, "frame", 0) or 0)
            payload.setdefault(index, []).append(
                {"n": number, "idx": marker, "mask": self._mask(item)}
            )
        for index, items in payload.items():
            if not 0 <= index < len(frames):
                continue
            try:
                await frames[index].evaluate(_MARK_JS, {"items": items})
            except PlaywrightError:
                continue

    async def _clear(self, page: Page) -> None:
        """把叠加层摘干净；摘不掉也不影响别的。"""
        for frame in page.frames:
            try:
                await frame.evaluate(_CLEAR_JS)
            except PlaywrightError:
                continue

    def _mask(self, item: Any) -> bool:
        """已经带着值的栏位要涂黑：登录后的招聘系统常把姓名、手机号预填在表里。"""
        if not self.settings.mask_sensitive:
            return False
        return bool(str(getattr(item, "value", "") or "").strip())

    # --- 问模型 ---

    async def _ask(self, shots: list[bytes], numbered: list[Any]) -> str:
        """把带标记的截图和栏位清单交给多模态模型，读回差异。"""
        client = self.llm.provider.client
        content: list[dict[str, Any]] = [{"type": "text", "text": self._prompt(shots, numbered)}]
        content.extend({"type": "image_url", "image_url": {"url": _data_uri(png)}} for png in shots)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": _MAX_TOKENS,
            "temperature": 0,
            "timeout": max(1, int(self.settings.timeout)),
        }
        try:
            # 关掉「推理」：核对几个框不值得先想一大段，开着会慢到十几秒。
            response = await client.chat.completions.create(**payload, reasoning_effort="none")
        except Exception as exc:  # noqa: BLE001 个别兼容接口不认这个参数，退回老写法
            if "reasoning" not in str(exc).lower():
                raise
            response = await client.chat.completions.create(**payload)
        raw = (response.choices[0].message.content or "").strip()
        if not raw or any(word in raw.lower() for word in _NO_ANSWER):
            return ""
        return raw

    def _prompt(self, shots: list[bytes], numbered: list[Any]) -> str:
        """提示词：只放「编号 + 栏位名 + 类型」，取值一律不出本机。"""
        lines: list[str] = []
        for number, item in enumerate(numbered, 1):
            label = (item.label or item.placeholder or item.name or "（没有文字）").strip()[:30]
            kind = str(item.type or "text")
            if item.widget:
                kind = f"{kind}/{item.widget}"
            required = "，必填" if item.required else ""
            lines.append(f"- {number}：{label}（{kind}{required}）")
        return "\n".join(
            [
                f"这是企业官网网申表单的截图，共 {len(shots)} 张，从上到下是同一页的不同位置。",
                "图上每个红框是一个栏位，左上角的红色数字是它的编号；",
                "黑色色块是程序为了保护隐私涂掉的内容，它本身也是一个栏位，不用管它。",
                "",
                "请核对两件事：",
                "1. 哪些编号其实不是「需要填写或选择」的栏位？（标题、说明文字、图片、按钮、",
                "   已经展示出来的只读内容都算）",
                "2. 截图里有没有哪一栏需要填写或选择，却没有被编号标出来？",
                "",
                "程序扫出来的栏位清单（仅供参考，以截图为准）：",
                *lines,
                "",
                "只输出 JSON，不要解释、不要代码块：",
                '{"not_field": [编号...], "missing": [{"label": "页面上的文字"}]}',
                "没有多余的编号就写 []，没有漏掉的栏位也写 []。",
            ]
        )

    # --- 解析与合并 ---

    @staticmethod
    def _parse(raw: str) -> tuple[list[int], list[str]]:
        """把模型回的那段 JSON 拆成「多出来的编号」和「漏掉的栏位名」。"""
        if not raw:
            return [], []
        found = re.search(r"\{.*\}", raw, re.S)
        if not found:
            return [], []
        try:
            data = json.loads(found.group(0))
        except (TypeError, ValueError):
            return [], []
        if not isinstance(data, dict):
            return [], []
        extra: list[int] = []
        for key in ("not_field", "notField", "notfield", "extra", "ghosts"):
            value = data.get(key)
            if not isinstance(value, list):
                continue
            for item in value:
                try:
                    extra.append(int(str(item).strip()))
                except (TypeError, ValueError):
                    continue
            if extra:
                break
        missing: list[str] = []
        raw_missing = data.get("missing")
        if not isinstance(raw_missing, list):
            # 模型偶尔把整串文字塞进来，逐字拆开会造出一堆假栏位，宁可当它没说。
            return extra, missing
        for item in raw_missing:
            text = ""
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                text = str(item.get("label") or item.get("name") or item.get("text") or "")
            text = text.strip()[:40]
            if text and text not in missing:
                missing.append(text)
        return extra, missing

    async def _merge(
        self,
        page: Page,
        fields: list[Any],
        numbered: list[Any],
        extra: list[int],
        missing: list[str],
    ) -> VisionResult:
        """把差异落成对照表：多出来的收起，漏掉的补一行。"""
        result = VisionResult(checked=True)
        for number in extra:
            if not 1 <= number <= len(numbered):
                continue
            item = numbered[number - 1]
            item.hidden = True
            item.note = _GHOST_NOTE
            result.ghosts += 1
        next_index = max((int(f.index) for f in fields), default=-1) + 1
        for text in missing[: _MAX_MISSING]:
            item = await self._resolve(page, text, marker=f"v{next_index}")
            if item is None:
                item = FormField(index=next_index, label=text, note=_MANUAL_NOTE, ok=False)
            else:
                item.index = next_index
            fields.append(item)
            result.added.append(item)
            next_index += 1
        result.message = f"视觉核对：收起 {result.ghosts} 栏，补上 {len(result.added)} 栏"
        return result

    async def _resolve(self, page: Page, text: str, *, marker: str) -> FormField | None:
        """按模型说的栏位名在页面里找回那个控件；找不到返回 ``None``。"""
        for index, frame in enumerate(page.frames):
            try:
                raw = await frame.evaluate(_FIND_JS, {"text": text, "marker": marker})
            except PlaywrightError:
                continue
            if not isinstance(raw, dict):
                continue
            return FormField.from_raw(raw, index=0, frame=index)
        return None

    # --- 候选 ---

    @staticmethod
    def _candidates(fields: list[Any]) -> list[Any]:
        """能画框的栏位：没被收起、不是提交按钮、还带着控件标记。"""
        out: list[Any] = []
        for item in fields:
            if getattr(item, "hidden", False) or safety.is_irreversible(item):
                continue
            if _marker_of(item):
                out.append(item)
        return out
