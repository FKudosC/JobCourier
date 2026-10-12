"""表单扫描：把页面（含 iframe）里的表单控件读成一份结构化字段清单。

只在浏览器里做「取原始属性」这一件事——控件的类型、名字、附近文字原样带回，
怎么理解这些字段交给 Python 侧（好测，也好改）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from patchright.async_api import Error as PlaywrightError

if TYPE_CHECKING:
    from patchright.async_api import Page

    from job.boss.session import BossSession

# 采集脚本：给每个控件打上 data-gj-idx 标记（填表时按它定位），
# 并把 label / 附近文字 / 选项 / CSS 路径等原始信息一起带回。
_COLLECT_JS = """
() => {
  const out = [];
  const textOf = (el) => (el ? (el.innerText || el.textContent || '').trim().slice(0, 80) : '');
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
  const nearText = (el) => {
    let node = el.parentElement;
    for (let i = 0; i < 3 && node; i += 1) {
      const controls = node.querySelectorAll('input, select, textarea').length;
      const text = textOf(node);
      if (text && text.length <= 60 && controls <= 2) return text;
      node = node.parentElement;
    }
    return '';
  };
  const groupOptions = (el) => {
    if (!el.name) return [];
    const siblings = [...document.querySelectorAll('input')].filter(
      (x) => x.name === el.name && x !== el,
    );
    const labels = siblings
      .map((x) => (x.labels && x.labels.length ? textOf(x.labels[0]) : '') || x.value)
      .filter(Boolean);
    return [...new Set(labels)].slice(0, 30);
  };
  let idx = 0;
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const tag = el.tagName.toLowerCase();
    const type = (tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : (el.type || 'text')).toLowerCase();
    if (type === 'hidden' || el.disabled) continue;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const invisible = style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0;
    if (invisible && type !== 'file') continue;
    if (rect.width === 0 && rect.height === 0 && type !== 'file') continue;
    el.setAttribute('data-gj-idx', String(idx));
    out.push({
      idx,
      tag,
      type,
      name: el.name || '',
      id: el.id || '',
      placeholder: el.placeholder || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      labelText: el.labels && el.labels.length ? textOf(el.labels[0]) : '',
      nearText: nearText(el),
      options: tag === 'select'
        ? [...el.options].map((o) => (o.textContent || '').trim()).filter(Boolean).slice(0, 40)
        : groupOptions(el),
      value: el.value || '',
      checked: !!el.checked,
      multiple: !!el.multiple,
      path: cssPath(el),
    });
    idx += 1;
  }
  return out;
}
"""

# 页面上的「拦路虎」提示词：出现这些就别硬填了，喊人
_GATE_HINTS = ("验证码", "captcha", "人机验证", "安全验证", "滑块", "拖动滑块")
# 明显的验证码 iframe
_GATE_IFRAMES = (
    "iframe[src*='captcha']",
    "iframe[src*='geetest']",
    "iframe[src*='yidun']",
    "iframe[id*='captcha']",
)

_PUNCT = re.compile(r"[\s*＊·・:：,，.。/／\\|｜()（）\[\]【】<>《》\"'`~!！?？;；\-—_+]+")


def normalize(text: str) -> str:
    """归一化字段文字：去标点空白、统一小写，便于比对。"""
    return _PUNCT.sub("", (text or "").strip().lower())


@dataclass
class FormField:
    """一个表单控件，以及它最终被填了什么。"""

    index: int
    frame: int = 0
    tag: str = "input"
    type: str = "text"
    name: str = ""
    element_id: str = ""
    label: str = ""
    placeholder: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)
    value: str = ""
    checked: bool = False
    selector: str = ""
    fallback: str = ""
    # 匹配与填充结果
    key: str = ""
    filled: str = ""
    source: str = ""
    confidence: float = 0.0
    ok: bool = True
    note: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any], *, index: int, frame: int = 0) -> "FormField":
        """把浏览器里带回来的原始属性整理成一个字段。"""
        tag = str(raw.get("tag") or "input")
        field_type = str(raw.get("type") or "text")
        name = str(raw.get("name") or "")
        element_id = str(raw.get("id") or "")
        placeholder = str(raw.get("placeholder") or "")
        label = cls._pick_label(raw, name=name, element_id=element_id, placeholder=placeholder)
        return cls(
            index=index,
            frame=frame,
            tag=tag,
            type=field_type,
            name=name,
            element_id=element_id,
            label=label,
            placeholder=placeholder,
            required=bool(raw.get("required")),
            options=[str(o) for o in (raw.get("options") or [])],
            value=str(raw.get("value") or ""),
            checked=bool(raw.get("checked")),
            selector=f'[data-gj-idx="{index}"]',
            fallback=str(raw.get("path") or ""),
        )

    @staticmethod
    def _pick_label(raw: dict[str, Any], *, name: str, element_id: str, placeholder: str) -> str:
        """按可靠性挑一个能代表字段的中文说法。"""
        for candidate in (
            raw.get("labelText"),
            raw.get("ariaLabel"),
            raw.get("nearText"),
            placeholder,
        ):
            text = str(candidate or "").strip()
            if text:
                return text
        return name or element_id

    @property
    def text(self) -> str:
        """用于匹配的字段文字：标签 + 名字 + 占位符 + id 拼在一起。"""
        return " ".join(x for x in (self.label, self.name, self.placeholder, self.element_id) if x)

    @property
    def normalized(self) -> str:
        return normalize(self.text)

    @property
    def is_file(self) -> bool:
        return self.type == "file"

    def signature(self) -> str:
        """跨站点复用的字段签名：同一个官网的同一栏下次直接命中。"""
        base = normalize(self.label) or normalize(self.name) or normalize(self.element_id)
        return f"{base}|{self.type}"

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, index: int = -1) -> "FormField":
        """从存下的字段对照表还原一条（关掉再打开接着填）。"""
        return cls(
            index=int(data.get("index", index)),
            frame=int(data.get("frame", 0) or 0),
            tag=str(data.get("tag") or ("select" if data.get("type") == "select" else "input")),
            type=str(data.get("type") or "text"),
            name=str(data.get("name") or ""),
            label=str(data.get("label") or ""),
            placeholder=str(data.get("placeholder") or ""),
            required=bool(data.get("required")),
            options=[str(o) for o in (data.get("options") or [])],
            value=str(data.get("value") or ""),
            selector=str(data.get("selector") or ""),
            fallback=str(data.get("fallback") or ""),
            key=str(data.get("key") or ""),
            filled=str(data.get("filled") or ""),
            source=str(data.get("source") or ""),
            confidence=float(data.get("confidence") or 0),
            ok=bool(data.get("ok", True)),
            note=str(data.get("note") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        """前端字段对照表的一行。"""
        return {
            "index": self.index,
            "frame": self.frame,
            "tag": self.tag,
            "type": self.type,
            "label": self.label,
            "name": self.name,
            "placeholder": self.placeholder,
            "required": self.required,
            "options": self.options,
            "value": self.value,
            # 定位信息：下次接着填要用，前端也拿它做「跳到该字段」的锚点
            "selector": self.selector,
            "fallback": self.fallback,
            "key": self.key,
            "filled": self.filled,
            "source": self.source,
            "confidence": round(self.confidence, 2),
            "ok": self.ok,
            "note": self.note,
        }


class FormScanner:
    """扫描表单：读页面里的全部控件，并识别需要人处理的拦路虎。"""

    def __init__(self, session: "BossSession") -> None:
        self.session = session

    async def scan(self, page: "Page") -> list[FormField]:
        """扫描所有 frame 里的表单控件，按出现顺序返回。"""
        fields: list[FormField] = []
        for frame_index, frame in enumerate(page.frames):
            try:
                raw = await frame.evaluate(_COLLECT_JS)
            except PlaywrightError:
                continue  # 跨域 frame 或已销毁的 frame 直接跳过
            for item in raw or []:
                if not isinstance(item, dict):
                    continue
                fields.append(FormField.from_raw(item, index=len(fields), frame=frame_index))
        return fields

    async def gate(self, page: "Page") -> str:
        """检测需要人工介入的门槛（验证码 / 人机验证）；没有返回空串。"""
        for frame in page.frames:
            try:
                text = await frame.evaluate(
                    "() => (document.body ? document.body.innerText : '').slice(0, 4000)"
                )
            except PlaywrightError:
                continue
            low = str(text or "").lower()
            for hint in _GATE_HINTS:
                if hint in low:
                    return f"页面出现「{hint}」，请手动处理后再继续"
            for selector in _GATE_IFRAMES:
                try:
                    if await frame.locator(selector).count():
                        return "页面出现验证码控件，请手动处理后再继续"
                except PlaywrightError:
                    continue
        return ""
