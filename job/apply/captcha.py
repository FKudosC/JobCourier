"""验证码识别：把页面上的图形验证码截下来，读出字符再填进输入框。

四步：找到验证码图片和它旁边的输入框 → 截图 → 识图 → 填框。识图先走本地 OCR
（离线、免费、不联网），本地读不出来再交给多模态模型兜底——两条路并联，本地能
读准就不打扰模型。

一条底线：**只读图、只填框，绝不点提交**。识别不出、模型不支持视觉、页面找不到
验证码，都如实回报「请手动填写」，绝不硬塞一个猜出来的值——填错的验证码比留空更糟。
大模型识图用的是「大模型服务」里配置的接口地址与 API Key，模型可另选一个支持视觉的。
"""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from patchright.async_api import Error as PlaywrightError

from job.models.setting import CaptchaSettings, LlmSettings
from job.utils import ocr

if TYPE_CHECKING:
    from patchright.async_api import Frame, Locator, Page

# 识图提示词：只要字符本身，别的都不要；看不清就直说
_PROMPT = (
    "这是一张网页上的图形验证码。请只输出图中的验证码字符，"
    "不要解释、不要标点、不要空格；如果实在看不清，就回复「看不清」。"
)

# 只留字母、数字和汉字：验证码常见就是这几种
_KEEP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]")
# 模型说「看不清」这类兜底话，不算识别成功
_NO_READ = ("看不清", "无法", "不确定", "unclear", "cannot", "sorry")
# 本地 OCR 的置信度门槛：低于这个数宁可交给模型，也不赌
_LOCAL_MIN_SCORE = 0.6
# 等图片真正加载出来的上限（秒）：src 就位不等于位图解码完成，早截是白板
_LOAD_WAIT = 3.0

# 识图输出的 token 上限：视觉模型可能是「推理型」，会先把思考过程写满预算，正文就被
# 截成空串——给足余量，别让答案饿死在思考上。
_MAX_TOKENS = 512
# 从「思考过程」结尾捞答案时的候选形状（验证码就是几个字母数字）
_TAIL_RE = re.compile(r"[0-9A-Za-z]{3,8}")

# 找验证码的页面脚本（``gj-captcha`` 是标记，便于测试识别这一段）：
# 图片候选看 src/id/class/alt/name，输入框候选看 name/id/placeholder/aria-label，
# 各自再兜一层「旁边文字里有『验证码』」；最后按位置把最近的一图一框配成一对。
_FIND_JS = """
/* gj-captcha */
() => {
  const HINTS = ['验证码', 'captcha', 'checkcode', 'verifycode', 'vcode',
    'validcode', 'authcode', 'securitycode', 'randcode', 'imagecode'];
  const low = (s) => (s || '').toLowerCase();
  // 归一化：去掉下划线/连字符/点/空格等，让 verify_code、verify-code 都能对上 verifycode
  const norm = (s) => low(s).replace(/[^0-9a-z\\u4e00-\\u9fff]/g, '');
  const hit = (blob) => { const b = norm(blob); return HINTS.some((h) => b.includes(norm(h))); };
  const textOf = (el) => (el ? (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim() : '');
  const ownText = (el) => {
    let node = el.parentElement;
    for (let i = 0; i < 6 && node; i += 1) {
      const t = textOf(node);
      if (t && t.length <= 40) return t;
      node = node.parentElement;
    }
    return '';
  };
  const cssPath = (el) => {
    const parts = [];
    let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < 5; depth += 1) {
      if (node.id) {
        parts.unshift('#' + CSS.escape(node.id));
        break;
      }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = [...parent.children].filter((c) => c.tagName === node.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };
  const visible = (el) => {
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8;
  };
  const blobOf = (el) => low([el.getAttribute('src'), el.id, el.className,
    el.getAttribute('alt'), el.getAttribute('name'), el.getAttribute('title')]
    .filter(Boolean).join(' '));
  const imgs = [...document.querySelectorAll('img, canvas')].filter(visible)
    .map((el) => ({ el, score: hit(blobOf(el)) ? 3 : (hit(low(ownText(el))) ? 2 : 0) }))
    .filter((x) => x.score > 0);
  const inputs = [...document.querySelectorAll('input')].filter(visible)
    .map((el) => {
      const blob = low([el.name, el.id, el.placeholder,
        el.getAttribute('aria-label'), el.getAttribute('title')].filter(Boolean).join(' '));
      return { el, score: hit(blob) ? 3 : (hit(low(ownText(el))) ? 2 : 0) };
    })
    .filter((x) => x.score > 0);
  if (!imgs.length && !inputs.length) return null;
  // 最像验证码的那张图：先看命中强度，再看尺寸——验证码图通常不大
  const pickImg = imgs.sort((a, b) => {
    const ra = a.el.getBoundingClientRect();
    const rb = b.el.getBoundingClientRect();
    return (b.score - a.score) || (ra.width * ra.height - rb.width * rb.height);
  })[0];
  let img = pickImg ? pickImg.el : null;
  let input = null;
  if (inputs.length) {
    const sorted = inputs.sort((a, b) => b.score - a.score);
    if (img) {
      // 取离图片最近的那个输入框（验证码图和输入框总是挨着）
      const ir = img.getBoundingClientRect();
      const cx = ir.left + ir.width / 2;
      const cy = ir.top + ir.height / 2;
      sorted.sort((a, b) => {
        const ra = a.el.getBoundingClientRect();
        const rb = b.el.getBoundingClientRect();
        const da = Math.hypot(ra.left + ra.width / 2 - cx, ra.top + ra.height / 2 - cy);
        const db = Math.hypot(rb.left + rb.width / 2 - cx, rb.top + rb.height / 2 - cy);
        return (a.score - b.score) || (da - db);
      });
    }
    input = sorted[0].el;
  }
  // 兜底：找到了验证码输入框却找不到图片时，在同一容器里找最近的可见图
  if (!img && input) {
    let node = input.parentElement;
    for (let i = 0; i < 4 && node && !img; i += 1) {
      const cands = [...node.querySelectorAll('img, canvas')].filter(visible);
      if (cands.length) {
        img = cands.filter((c) => hit(blobOf(c)))[0] || cands[0];
        break;
      }
      node = node.parentElement;
    }
  }
  return {
    image: img ? cssPath(img) : '',
    input: input ? cssPath(input) : '',
  };
}
"""


@dataclass
class CaptchaResult:
    """一次验证码识别的结果。"""

    ok: bool = False
    code: str = ""
    filled: bool = False
    # 页面上确实找到了验证码控件（图 + 框）；用来区分「这页没有验证码」和「有但没读出来」
    detected: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "filled": self.filled,
            "detected": self.detected,
            "message": self.message,
        }


class CaptchaSolver:
    """按「找图 → 截图 → 识图 → 填框」处理图形验证码。"""

    def __init__(
        self,
        *,
        llm: LlmSettings | None = None,
        settings: CaptchaSettings | None = None,
        timeout: float = 60,
    ) -> None:
        self.llm = llm or LlmSettings.load()
        self.settings = settings or CaptchaSettings.load()
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        """开着且大模型服务可用，才谈得上自动识别。"""
        return self.settings.enabled and self.llm.ready

    @property
    def model(self) -> str:
        """识图模型：优先用单独选的，没选就用大模型服务里的主模型。"""
        return (self.settings.model or self.llm.model).strip()

    async def solve(self, page: "Page") -> CaptchaResult:
        """识别并填写当前页面上的图形验证码；任何一步不成，都退回「请手动填写」。"""
        if not self.settings.enabled:
            return CaptchaResult(message="未开启验证码自动识别")
        if not self.llm.ready:
            return CaptchaResult(message="请先在「系统设置」里开通大模型服务")
        if not self.model:
            return CaptchaResult(message="请先选择识图模型")
        found = await self._find(page)
        if found is None:
            return CaptchaResult(message="页面上没找到图形验证码，请手动处理")
        frame_index, target = found
        png = await self._shot(page, frame_index, str(target.get("image") or ""))
        if not png:
            return CaptchaResult(detected=True, message="验证码图片没截下来，请手动填写")
        code = await self._read_local(png)
        if not code:
            try:
                code = await self._recognize(png)
            except Exception as exc:  # noqa: BLE001 识图失败不该拖垮整次扫描
                return CaptchaResult(detected=True, message=f"识图失败：{exc}")
        if not code:
            return CaptchaResult(detected=True, message="没读出验证码，请手动填写")
        filled = await self._fill(page, frame_index, str(target.get("input") or ""), code)
        if not filled:
            return CaptchaResult(
                ok=True,
                code=code,
                detected=True,
                message=f"已识别验证码「{code}」，但没找到输入框，请手动填写",
            )
        return CaptchaResult(
            ok=True,
            code=code,
            filled=True,
            detected=True,
            message=f"已自动识别并填写验证码：{code}",
        )

    # --- 内部 ---

    async def _find(self, page: "Page") -> tuple[int, dict[str, Any]] | None:
        """逐 frame 找验证码图与输入框；返回帧序号与选择器。"""
        for index, frame in enumerate(page.frames):
            try:
                raw = await frame.evaluate(_FIND_JS)
            except PlaywrightError:
                continue
            if isinstance(raw, dict) and (raw.get("image") or raw.get("input")):
                return index, raw
        return None

    async def _shot(self, page: "Page", frame_index: int, selector: str) -> bytes | None:
        """把验证码图片截成 PNG 字节；找不到或截图失败返回 ``None``。"""
        if not selector:
            return None
        locator = self._locator(page, frame_index, selector)
        if locator is None:
            return None
        await self._wait_loaded(locator)
        try:
            return await locator.screenshot()
        except PlaywrightError:
            return None

    @staticmethod
    async def _wait_loaded(locator: "Locator") -> None:
        """等图片解码完再截图：``src`` 已就位但位图没到位时，截出来是一片空白。

        刚打开页面就来扫验证码，最容易踩这个坑；等不到也不强求，照旧截一张。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _LOAD_WAIT
        while True:
            try:
                ready = await locator.evaluate(
                    "el => el.tagName !== 'IMG' || (el.complete && el.naturalWidth > 0)"
                )
            except PlaywrightError:
                return
            if ready or loop.time() >= deadline:
                return
            await asyncio.sleep(0.1)

    async def _read_local(self, png: bytes) -> str:
        """先用本地 OCR 试读：读得准就直接用，读不准返回空串交给多模态兜底。

        本地引擎是可选依赖，没装、模型跑不起来、图太花，一律当作「读不出来」，
        绝不往外抛异常拖垮扫描。
        """
        try:
            rows = await asyncio.to_thread(ocr.lines, png)
        except Exception:  # noqa: BLE001 本地兜底失败不是错误，交给模型
            return ""
        if len(rows) != 1:
            return ""
        text, score, _top, _left = rows[0]
        if score < _LOCAL_MIN_SCORE:
            return ""
        code = _KEEP.sub("", text)
        return code if 3 <= len(code) <= 8 else ""

    async def _recognize(self, png: bytes) -> str:
        """把图片交给多模态模型，读回验证码字符。"""
        client = self.llm.provider.client
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "max_tokens": _MAX_TOKENS,
            "temperature": 0,
            "timeout": self.timeout,
        }
        try:
            # 关掉「推理」：读几个字符不值得先想一大段，开着会慢到十几秒。
            response = await client.chat.completions.create(
                **payload, reasoning_effort="none"
            )
        except Exception as exc:  # noqa: BLE001 个别兼容接口不认这个参数，退回老写法
            if "reasoning" not in str(exc).lower():
                raise
            response = await client.chat.completions.create(**payload)
        choice = response.choices[0]
        raw = (choice.message.content or "").strip()
        if not raw:
            # 推理型模型偶尔只把答案写进「思考过程」，正文留空；截断时思考本身也不完整，
            # 所以只在正常收尾（stop）时才去结尾捞一次。
            if choice.finish_reason != "length":
                raw = self._tail_answer(getattr(choice.message, "reasoning_content", "") or "")
        if not raw or any(word in raw.lower() for word in _NO_READ):
            return ""
        return _KEEP.sub("", raw)[:8]

    @staticmethod
    def _tail_answer(reason: str) -> str:
        """从思考过程结尾捞最后一个像验证码的候选；捞不到返回空串。"""
        if not reason:
            return ""
        found = _TAIL_RE.findall(reason[-200:])
        return found[-1] if found else ""

    async def _fill(self, page: "Page", frame_index: int, selector: str, code: str) -> bool:
        """把识别出来的验证码填进输入框；填不进去返回 ``False``。"""
        if not selector:
            return False
        locator = self._locator(page, frame_index, selector)
        if locator is None:
            return False
        try:
            await locator.fill(code)
        except PlaywrightError:
            return False
        return True

    @staticmethod
    def _locator(page: "Page", frame_index: int, selector: str) -> "Locator | None":
        """按帧序号与选择器取定位器；帧已消失时退回主文档。"""
        frames = page.frames
        frame: "Frame" = frames[frame_index] if 0 <= frame_index < len(frames) else page.main_frame
        try:
            locator = frame.locator(selector)
        except PlaywrightError:
            return None
        return locator.first
