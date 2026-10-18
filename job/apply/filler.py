"""表单填充：把匹配好的资料真正写进页面里的控件。

只做「按控件类型填值」这一件事，不做判断——填什么由 :mod:`job.apply.matcher` 决定。
刻意不碰提交按钮：企业官网的表单提交后撤不回来，交给人点。

官网的控件远不止 ``<input>`` 和 ``<select>`` 两种：antd / element-ui 的「假下拉」是
input 加浮层，级联要一级一级点，日期要在日历上挑格子，拖拽上传连 input 都藏起来。
这些统一按「先试原生写法 → 再试拟真交互 → 最后回读校验」处理；校验不过就老实报
「请手动填写」，绝不留一个看着填上了、其实没生效的框。
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from patchright.async_api import Error as PlaywrightError

from job.apply import safety
from job.apply.matcher import align_option, split_admin
from job.apply.scanner import FormField, normalize
from job.models.search import SearchConfigRow
from job.utils import log

if TYPE_CHECKING:
    from patchright.async_api import Frame, Locator, Page

    from job.boss.session import BossSession


class _FillError(Exception):
    """填不进去的原因：找不到控件、没有可上传的简历文件……"""


# 假下拉（antd / element-ui / select2 这类）展开后的浮层选项
_OPTION_SELECTORS = (
    '[role="option"]',
    ".ant-select-item-option",
    ".ant-cascader-menu-item",
    ".el-select-dropdown__item",
    ".el-cascader-node",
    # 北森 phoenix（zhiye.com / 很多国企官网在用）：简单列表、多选选择器、地区级联
    "li.phoenix-selectList__listItem",
    ".phoenix-selectList__singleLabel",
    ".list-item-container",
    ".area-item-name",
    ".area-text-label",
    ".phoenix-select__tagItem",
    'li[class*="option"]',
)

# 点开一个「假下拉」之后，浮层可能是这几种东西之一，填法完全不同：
#   日期面板 / 简单列表 / 多选选择器（勾选后点确定）/ 地区级联（一层层往下钻）
# 顺序要紧：地区级联和多选选择器都包在通用的 ``common-unmodeled-layer`` 里，
# 必须排在通用兜底前面，否则会被当成简单列表点错。
_OVERLAY_KINDS = (
    ("date", ".phoenix-calendar"),
    ("picker", ".constant-main-selector-container"),
    ("area", ".area-selector-container"),
    ("list", ".phoenix-selectList"),
    # 兜底：认不出具体组件时，只要有一个真展开的浮层容器就按「简单列表」点
    ("list", ".common-unmodeled-layer"),
)
# 浮层里那个可以敲日期的输入框（比在日历里点格子稳）
_OVERLAY_DATE_INPUT = ".phoenix-calendar-input"
# 选择器的「确定」按钮
_OVERLAY_CONFIRM = ".phoenix-button__content"
# 浮层高度低于这个值就是没展开的空壳（页面里常驻一堆 0 高的占位层）
_OVERLAY_MIN_HEIGHT = 60
# 地区级联浮层：每个格子真正能勾中的是那个小勾选框，文字只用来往下一级钻
_AREA_ITEM = ".area-item-container"
_AREA_ICON = ".icon-container"
_AREA_LABEL = ".area-text-label"
# 「确定」按钮上可能出现的文字（有的组件用「确认 / 完成」）
_CONFIRM_TEXTS = ("确定", "确认", "完成", "ok")

# 整组单选 / 多选：组件库不给每个选项配 input，只能按文字点
_CHOICE_SELECTORS = (
    # 北森 phoenix 的单选组：真正的文字节点，点它最稳
    ".phoenix-radio__radio-text",
    'label',
    '[role="radio"]',
    '[role="checkbox"]',
    '[class*="radio"]',
    '[class*="checkbox"]',
)

# 读原生 select 的选项（``gj-options`` 是标记，便于测试替身识别这一段）
_OPTIONS_JS = """
/* gj-options */
(el) => [...(el.options || [])].map((o) => [String(o.value), (o.textContent || '').trim()])
"""

# 读控件现在「显示」出来的值：组件库把选中结果挂在控件外面的展示节点上，
# 光读 input.value 会读到空串，回读校验就永远过不了。
_SHOWN_JS = """
/* gj-shown */
(el) => {
  // 多选 / 地区级联把选中的值做成一个个小标签，先找它们（最准）
  let scope = el;
  for (let i = 0; i < 4 && scope; i += 1) {
    if (scope.querySelectorAll) {
      const tags = [...scope.querySelectorAll('.phoenix-select__tagItem, .phoenix-select__tag')]
        .map((t) => (t.textContent || '').trim())
        .filter(Boolean);
      if (tags.length) return tags.join(' ').slice(0, 120);
    }
    scope = scope.parentElement;
  }
  let node = el;
  let deep = '';
  for (let i = 0; i < 6 && node; i += 1) {
    const text = (node.innerText || '').trim();
    if (text) {
      if (i < 4) return text.slice(0, 120);
      if (!deep) deep = text;
    }
    node = node.parentElement;
  }
  return deep.slice(0, 200);
}
"""

# 读原生下拉**当前选中**的那一项文字：``input.value`` 给的是 value 属性，不是人看到
# 的那句话（资料写「本科」、官网写「本科（学士）」时，拿 value 比会永远对不上）。
_SELECTED_JS = """
/* gj-selected */
(el) => {
  const sel = el.matches && el.matches('select') ? el : (el.querySelector ? el.querySelector('select') : null);
  if (!sel) return '';
  return [...(sel.selectedOptions || [])].map((o) => (o.textContent || '').trim()).join(' ').slice(0, 120);
}
"""

# 读页面自己弹出来的校验报错 / 提示：官网不通过时会在那一栏下面写一行红字，程序填完
# 不管它，用户看到的还是一张「都填好了」的对照表。这里把那些话连同它属于哪一栏读回来。
_ERRORS_JS = """
/* gj-errors */
() => {
  const out = [];
  const seen = new Set();
  const nodes = document.querySelectorAll(
    '[role=alert], [aria-invalid=true], .ant-form-item-explain, .el-form-item__error, '
    + '[class*=error], [class*=invalid], [class*=danger]'
  );
  for (const node of nodes) {
    if (out.length >= 12) break;
    const text = (node.innerText || node.textContent || '').trim();
    if (!text || text.length > 120 || seen.has(text)) continue;
    seen.add(text);
    // 认领到哪一栏：从报错节点往上找最近的一块「里面带 data-gj-idx 控件」的区域
    let scope = node;
    let idx = '';
    for (let i = 0; i < 6 && scope; i += 1) {
      const control = scope.querySelectorAll ? scope.querySelector('[data-gj-idx]') : null;
      if (control) { idx = control.getAttribute('data-gj-idx') || ''; break; }
      scope = scope.parentElement;
    }
    out.push({ idx, text });
  }
  return out;
}
"""

# 量一个控件的高度：页面里常驻一堆 0 高的浮层空壳，必须靠高度认出真展开的那个
_BOX_JS = """
/* gj-box */
(el) => el.getBoundingClientRect().height
"""

# 把浮层往下滚一屏：选项多的时候（省市区、行业、专业目录）组件库只渲染看得见的
# 那几行，要选的项在下面时 DOM 里压根没这个节点，光等是等不出来的，必须滚下去。
_SCROLL_JS = """
/* gj-scroll */
() => {
  const sel = '.ant-select-dropdown, .el-select-dropdown, .el-cascader-menu, '
    + '.phoenix-selectList, .area-selector-container, .constant-main-selector-container, '
    + '.common-unmodeled-layer, [role="listbox"]';
  for (const node of document.querySelectorAll(sel)) {
    const rect = node.getBoundingClientRect();
    if (rect.height < 60) continue;
    const before = node.scrollTop;
    node.scrollTop = before + Math.max(Math.round(rect.height * 0.8), 60);
    if (node.scrollTop - before > 0) return 1;
  }
  return 0;
}
"""

# 认一下这栏是不是「点一下才弹浮层」的自研下拉：类名里带 select/dropdown 这类词
# （往上多看几层，比扫描那一刻看得更深），或者挂了 combobox / aria-haspopup。
# 这种控件直接 fill 只会写进一个假值——页面看着填上了，其实什么都没选中。
_PROBE_JS = """
/* gj-probe */
(el) => {
  let hint = false;
  let node = el;
  for (let i = 0; i < 6 && node && node.nodeType === 1; i += 1) {
    const cls = String(node.className || '').toLowerCase();
    if (/(^|[-_])(select|dropdown|combobox|cascader|picker)([-_]|$)/.test(cls)) {
      hint = true;
      break;
    }
    node = node.parentElement;
  }
  const role = (el.getAttribute('role') || '').toLowerCase();
  const pop = (el.getAttribute('aria-haspopup') || '').toLowerCase();
  const combo = role === 'combobox' || pop === 'listbox' || pop === 'menu' || pop === 'true';
  return { hint: hint, combo: combo };
}
"""

# 浮层展开、选项渲染出来的等待节奏：等几轮、每轮隔多久（级联的下一级要等上一级选完）
_OPTION_RETRY = 6
_OPTION_INTERVAL = 0.25
# 级联下拉给宽一点：下一级的选项要走接口拉回来，慢的官网得等两三秒
_CASCADE_RETRY = 20
# 点完选项到回读之间的缓冲：组件库改 input.value 是异步的
_AFTER_PICK = 0.15
# 等系统文件选择框弹出来的上限（毫秒）
_CHOOSER_TIMEOUT = 8000
# 单个控件的操作上限（毫秒）：官网表单动辄十几栏，Playwright 默认的 30 秒等待
# 一叠加就像程序卡死了。这里给每个动作都压一个上限，填不动就当场认输、往下走。
_ACTION_TIMEOUT = 3000
# 点开浮层（假下拉 / 日期面板）的等待上限（毫秒）。组件库的浮层宿主节点常常过不了
# Playwright 那套「可见、稳定、能接收事件」的可操作性检查——实测北森 phoenix 的
# 虚拟列表每次都是 3 秒直接超时，于是「认得出是哪栏、值也有，就是填不进去」。
# 点开这一步改成强制点击，但仍要等节点挂上去、滚动到位，所以给一个宽一些的上限。
_OPEN_TIMEOUT = 8000
# 点开之后等浮层真渲染出来：等几轮、每轮隔多久（组件库是异步渲染，点完立刻找常扑空）
_OPEN_RETRY = 8

# 回读一栏要 1~3 次往返（定位 + 读值 + 组件库再读展示文字）。一百多栏串着读，
# 光是等往返就要好几秒。回读只读不写、各栏互不依赖，并发跑；上限压在个位数，
# 免得把官网的连接池或渲染线程挤爆。
_READBACK_CONCURRENCY = 8
# 上传控件给宽一点：文件要先落盘再回读
_UPLOAD_TIMEOUT = 8000
# 上传后等官网回写的节奏：webuploader 这类控件是「选了文件才由 JS 异步传」，
# 传成功后把服务器返回的路径写回旁边那个只读栏，失败则弹 alert。
# 回写是服务端异步的，实测慢站点要五六秒才落笔，所以窗口给宽一点。
_UPLOAD_RETRY = 24
_UPLOAD_INTERVAL = 0.25
# 单栏的总上限（秒）：控件再挑剔也不许把整张表拖住，超了就跳过、接着填下一栏。
# 比单个动作的上限宽得多——级联三级加上等选项刷新，正常也就十几秒。
_FIELD_TIMEOUT = 30
# 官网自己弹的上传报错 → 给人看的一句话
_UPLOAD_ALERTS = (
    ("F_EXCEED_SIZE", "文件超过官网大小限制"),
    ("文件大小超出限制", "文件超过官网大小限制"),
    ("文件总大小超出限制", "文件超过官网大小限制"),
    ("禁止上传该类型文件", "官网不接受这种文件类型"),
    ("上传失败", "官网上传失败"),
)

# 读上传控件旁边那个「已传文件路径」的只读栏：成功后才写进服务器路径。
# 找不到这样的栏返回 null，说明这个官网没法回读，只能当它传上去了。
_UPLOAD_PATH_JS = """
/* gj-upload-path */
(el) => {
  let node = el;
  // 往上找的层数要够：webuploader 的路径栏常挂在 input 往上第 3~4 层的容器上，
  // 少找一层就会读不到服务器回写的路径，把「其实传成功了」误判成失败。
  for (let i = 0; i < 6 && node; i++) {
    const box = node.querySelector ? node.querySelector('.upload-path') : null;
    if (box) return box.value || '';
    node = node.parentElement;
  }
  return null;
}
"""

# 回读控件自己挂上的文件数：``set_input_files`` 之后 input.files 有值才算真喂进去了。
# 很多官网没有那个「已传文件路径」只读栏，这时它是唯一能拿到的实证。
_UPLOAD_FILES_JS = """
/* gj-files */
(el) => {
  if (!el) return 0;
  // 扫到的可能是一层外壳 div，真正的 input[type=file] 藏在里面
  const input = el.matches && el.matches('input[type=file]')
    ? el
    : (el.querySelector ? el.querySelector('input[type=file]') : null);
  return input && input.files ? input.files.length : 0;
}
"""

# 上传控件里真正接文件的 input：扫到的若是外壳（div / 按钮），按它往下找一层。
_NESTED_FILE = 'input[type="file"]'

# 拖拽上传区：往区域上派一个带文件的 drop 事件，让控件自己的 JS 把文件收下。
# 这条路只对「真的有 drop 处理逻辑」的区域有用；没有的话控件会一声不吭地忽略掉，
# 所以调用方必须靠回读校验来判定到底成没成。
_DROP_JS = """
/* gj-drop */
(el, payload) => {
  if (!el || typeof DataTransfer !== 'function') return false;
  const bin = atob(payload.b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  const file = new File([bytes], payload.name, {type: payload.type || 'application/octet-stream'});
  const box = new DataTransfer();
  box.items.add(file);
  const zone = (el.closest && el.closest('[class*=upload], .uploader, [class*=drop]')) || el;
  for (const kind of ['dragenter', 'dragover', 'drop']) {
    zone.dispatchEvent(new DragEvent(kind, {bubbles: true, cancelable: true, dataTransfer: box}));
  }
  return true;
}
"""

# drop 事件得把文件内容塞进页面脚本里，太大的文件不走这条路（交给别的兜底）
_DROP_MAX_BYTES = 8 * 1024 * 1024

# 读控件声明的接收类型（``accept``）：喂文件之前先自查一遍类型对不对得上，
# 免得把 PNG 塞进只收 Excel 的框里，让官网弹一堆看不懂的报错。
_UPLOAD_ACCEPT_JS = """
/* gj-accept */
(el) => {
  if (!el) return '';
  const input = el.matches && el.matches('input[type=file]')
    ? el
    : (el.querySelector ? el.querySelector('input[type=file]') : null);
  return input ? (input.getAttribute('accept') || '') : '';
}
"""

# 级联值里的层级分隔符：浙江省/杭州市/西湖区、江苏 > 南京
_CASCADE_SEP = re.compile(r"\s*(?:[/／>»]|->|—|–|\||、|,|，)\s*")
# 扫描时给控件打的序号，形如 ``[data-gj-idx="3"]``：读页面报错时靠它认领到哪一栏
_SLOT_RE = re.compile(r'data-gj-idx="(\d+)"')
# 日期写法：2024-06-30 / 2024/6/30 / 2024年6月30日 / 2024-06 / 2024
_DATE_PARTS = re.compile(r"(\d{4})\D{0,3}(\d{1,2})?\D{0,3}(\d{1,2})?")
_DIGITS = re.compile(r"\D")
# 数字栏收得下的写法：纯数字，后面最多跟个单位（175cm / 65 kg）
_NUMBER_LEAD = re.compile(r"^(-?\d+(?:\.\d+)?)\s*[^\d]*$")


def _cascade_parts(value: str) -> list[str]:
    """把「浙江省/杭州市/西湖区」拆成三级；只有一级时返回单元素列表。"""
    parts = [part.strip() for part in _CASCADE_SEP.split(value or "") if part.strip()]
    return parts or [value]


def _accept_ok(accept: str, path: Path) -> bool:
    """控件声明的 ``accept`` 收不收这份文件；accept 为空一律放行。

    ``accept`` 写成 ``.pdf,.docx`` / ``image/*`` / ``application/pdf`` 都算数，
    认不出来的一律放行——宁可交给官网自己校验，也别拿我们猜的规则拦下真文件。
    """
    tokens = [token.strip().lower() for token in (accept or "").split(",") if token.strip()]
    if not tokens:
        return True
    suffix = path.suffix.lower()
    mime = mimetypes.guess_type(path.name)[0] or ""
    for token in tokens:
        if token.startswith(".") and suffix == token:
            return True
        if token.endswith("/*") and mime.startswith(token[:-1]):
            return True
        if token == mime:
            return True
    return False


def _size_text(size: int) -> str:
    """把字节数写成「1.0 MB」这种一眼能看懂的样子。"""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def _file_text(path: Path) -> str:
    """「林一舟.png」（1.0 MB）」——报错里带上文件名和体积，用户才知道该换哪一份。"""
    try:
        size = path.stat().st_size
    except OSError:
        return f"「{path.name}」"
    return f"「{path.name}」（{_size_text(size)}）"


def _date_iso(value: str, kind: str = "date") -> str:
    """把「2024年6月」「2024-06」「2024/6/30」这类写法转成控件认的 ISO 值。

    认不出来就原样返回——让控件自己试，总好过我们瞎编一个日期。只给到年月的
    值按当月 1 号补齐，官网的日期控件基本都要求完整日期。
    """
    match = _DATE_PARTS.search(value or "")
    if not match:
        return value
    year, month, day = match.group(1), match.group(2), match.group(3)
    if not month:
        return year
    if kind == "month":
        return f"{year}-{int(month):02d}"
    return f"{year}-{int(month):02d}-{int(day or 1):02d}"


def _digits(text: str) -> str:
    """只留数字：「2001年05月01日」与「2001-05-01」是同一个日子。"""
    return _DIGITS.sub("", text or "")


_LOOSE_KEEP = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]")


def _loose(text: str) -> str:
    """只留数字 / 字母 / 汉字：「8001～10000」与「8001-10000」在这里是同一个串。

    官网回写选中值时爱换分隔符（区间那个「～」常写成「-」），回读校验得容得下这点差异。
    """
    return _LOOSE_KEEP.sub("", text or "").lower()


def _same_value(shown: str, want: str) -> bool:
    """回读到的文字和准备填的值是不是同一个（官网爱换写法：「本科」写成「本科（学士）」）。"""
    left, right = _loose(shown), _loose(want)
    if not left or not right:
        return False
    return left == right or left in right or right in left


def _numeric(value: str) -> str:
    """把「175cm」「65 kg」削成数字；削不出来就返回空串。

    官网的 ``input[type=number]`` 只认数字，塞「175cm」进去会直接报错。这里只削掉
    数字后面的单位；「8K-12K」这种带范围、带日期的写法一律不猜，交回给人填。
    """
    text = (value or "").replace(",", "").strip()
    match = _NUMBER_LEAD.match(text)
    return match.group(1) if match else ""


def _brief(exc: Exception) -> str:
    """Playwright 的报错常拖着几十行调用日志，这里只留第一行。"""
    lines = str(exc).strip().splitlines()
    return (lines[0] if lines else "")[:200]


async def _box_height(locator: "Locator") -> float:
    """量控件的高度（像素）；量不到当 0。用来区分「真展开的浮层」和「0 高的占位壳」。"""
    try:
        raw = await locator.evaluate(_BOX_JS)
    except PlaywrightError:
        return 0.0
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


async def _click_node(locator: "Locator", timeout: int = _ACTION_TIMEOUT) -> None:
    """用 DOM 的 ``click()`` 触发一次点击，绕开 Playwright 的可操作性检查。

    组件库把选项塞进虚拟列表 / 浮层时，「可见、稳定、能接收事件」三连检查经常
    永远不满足（实测北森 phoenix 的 ``phoenix-selectList__virtualList`` 直接超时），
    DOM 点击不挑这些，组件库的委托监听照样收得到。真失败了再退回普通点击。
    """
    try:
        await locator.evaluate("(el) => el.click()")
    except PlaywrightError:
        await locator.click(timeout=timeout)


async def _open_widget(locator: "Locator") -> None:
    """点开一个浮层控件（假下拉 / 日期面板 / 自研选择器）。

    这一步以前是普通 ``click``，于是整批栏位都栽在同一个地方：日志里连着十几行
    ``Locator.click: Timeout 3000ms exceeded``——值认出来了、控件也定位到了，
    就是点不开。这里按三档递进，每档都跳过那套过不了的可操作性检查：

    1. ``force=True``：仍是完整的鼠标事件序列（antd / element 监听的是
       ``mousedown``，纯 DOM 的 ``click()`` 唤不醒它们），但不等「稳定 / 能接收事件」；
    2. DOM 的 ``click()``：元素被浮层盖着、或压根量不到盒子时走这条；
    3. 普通点击兜底：前两档都没成，才退回 Playwright 自己那套等待。
    """
    try:
        await locator.click(force=True, timeout=_OPEN_TIMEOUT)
        return
    except PlaywrightError:
        pass
    try:
        await locator.evaluate("(el) => el.click()")
        return
    except PlaywrightError:
        await locator.click(timeout=_OPEN_TIMEOUT)


async def _type_text(locator: "Locator", value: str) -> None:
    """往文本框里写值；写不进去时退回「聚焦 + 逐字敲」。

    ``fill`` 要过「可见、可编辑、能接收事件」的检查，组件库把真正的 input 压在
    浮层下面、或要求先聚焦才解锁时，它会一直等到超时（日志里的
    ``Locator.fill: Timeout 3000ms exceeded``）。退回真实按键序列，组件库认。
    值有没有真落进去由回读校验把关，这里不假装成功。
    """
    try:
        await locator.fill(value, timeout=_ACTION_TIMEOUT)
        return
    except PlaywrightError:
        pass
    await locator.focus(timeout=_OPEN_TIMEOUT)
    await locator.press_sequentially(value, timeout=_OPEN_TIMEOUT)


async def _option_text(locator: "Locator") -> str:
    """读一个选项上显示的文字；读不到就返回空串。

    回读校验要拿「真正点中的那句官网文字」比对，而不是资料里的原值——
    资料写「8-10K」、官网写「8001～10000」，拿原值比永远对不上。
    """
    try:
        return (await locator.inner_text(timeout=_ACTION_TIMEOUT)).strip()
    except PlaywrightError:
        return ""


async def _drop_file(locator: "Locator", path: Path) -> bool:
    """往拖拽上传区派一个带文件的 drop 事件；事件派下去了返回 True。

    只负责「把事件送到」，不保证控件真的收了——收没收由调用方回读校验。
    文件太大或读不出来时直接返回 False，让调用方换别的兜底。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if not raw or len(raw) > _DROP_MAX_BYTES:
        return False
    payload = {
        "name": path.name,
        "type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "b64": base64.b64encode(raw).decode("ascii"),
    }
    try:
        return bool(await locator.evaluate(_DROP_JS, payload))
    except PlaywrightError:
        return False


@dataclass
class FillOutcome:
    """一次填表的结果统计。"""

    filled: int = 0
    skipped: int = 0
    failed: int = 0
    # 上传简历后被官网覆盖、又被补回来的栏数
    restored: int = 0
    # 填完之后官网自己报「这栏不对」的栏数（读页面红字得到的，不算填失败）
    flagged: int = 0
    fields: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """没有填不进去的字段就算成功（跳过的不算失败）。"""
        return self.failed == 0

    def absorb(self, other: "FillOutcome") -> "FillOutcome":
        """把另一批的结果并进来，返回自己。

        逐栏填（``only``）时每一栏各回一份结果，整张表填完要合起来才算得出总数：
        「填进去几项」是面板上「已填 N 项」的依据，少算了数字就对不上。
        """
        self.filled += other.filled
        self.skipped += other.skipped
        self.failed += other.failed
        self.restored += other.restored
        self.flagged += other.flagged
        if other.fields:
            self.fields = other.fields
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "filled": self.filled,
            "skipped": self.skipped,
            "failed": self.failed,
            "restored": self.restored,
            "flagged": self.flagged,
            "fields": self.fields,
        }


class FormFiller:
    """按控件类型把值写进页面：文本框直接填，下拉选一项，单选/多选打勾，文件走上传。"""

    def __init__(self, session: "BossSession") -> None:
        self.session = session
        # 要不要自动上传简历 / 寸照。关掉时文件栏一律留给人：浏览器里自己选文件
        # 比程序猜路径稳当，也不至于把简历传到别的栏里去。
        self.upload_files = True

    async def fill(
        self,
        page: "Page",
        fields: list[FormField],
        *,
        only: FormField | None = None,
        done: list[FormField] | None = None,
    ) -> FillOutcome:
        """填完所有「有值可填」的栏——匹配到的资料，或用户在面板上手动填的；不提交。

        ``only`` 只填指定的那一栏（其余栏仍留在 ``fields`` 里，级联这类要看同组栏位
        的填法才不会散架）——网申助手逐栏推理、逐栏填入时走这条路。这时回读只做这
        一栏，也不去读官网的校验红字：那两件事留给整批填完时做。``done`` 是调用方
        手里「已经填进去的栏」，逐栏模式下由它一路带着，上传简历后照样能把被官网
        覆盖掉的栏补回来。
        """
        outcome = FillOutcome()
        # 已经写进去的栏：上传简历后要按它们复核一遍
        written: list[FormField] = done if done is not None else []
        by_index = {item.index: item for item in fields}
        for item in fields:
            if only is not None and item is not only:
                continue
            if item.hidden:
                # 级联的下一级：由第一级带着一起填，这里不单独处理，也不算跳过
                continue
            if not item.key and not item.filled:
                # 既没匹配到资料、也没人手动填：没东西可写，跳过
                outcome.skipped += 1
                continue
            if safety.is_irreversible(item):
                # 提交按钮这类不可逆操作永远留给人点，程序碰都不碰
                outcome.skipped += 1
                item.ok = False
                item.note = "提交按钮请手动点击"
                continue
            if item.is_file and not self.upload_files:
                # 关掉了自动上传：文件这一栏交回给人，别硬塞一个可能不对的文件
                outcome.skipped += 1
                item.ok = False
                item.note = "已关闭自动上传，请在浏览器里自己选文件"
                continue
            if not item.filled:
                # 匹配上了却没有值：多半是下拉选项对不上，留给用户手动选
                outcome.failed += 1
                item.ok = False
                item.note = item.note or "没有可填的值，请手动填写"
                continue
            if await self._already(page, item):
                # 页面里已经是这个值了（上一轮填进去的，或官网自己带出来的）：
                # 不重复写。多轮补漏靠它收敛——某一轮一项都没新填，说明剩下的
                # 栏不是「没扫到」，而是「填不动」，再扫也是白扫。
                item.ok = True
                item.note = item.note or "页面里已经是这个值"
                continue
            try:
                group = [by_index[i] for i in item.group if i in by_index]
                # 给单栏一个总上限：控件卡住（浮层不收起、选项永远不刷新）时，
                # 宁可这一栏认输往下走，也不能让整张表停在这一格上。
                await asyncio.wait_for(self._apply(page, item, group), timeout=_FIELD_TIMEOUT)
            except asyncio.TimeoutError:
                outcome.failed += 1
                item.ok = False
                item.note = "这一栏填得太久，先跳过，请手动填写"
                log(f"填表超时（{item.label or item.name or item.selector}）")
                continue
            except _FillError as exc:
                outcome.failed += 1
                item.ok = False
                item.note = str(exc) or "填写失败，请手动填写"
                continue
            except PlaywrightError as exc:
                # 控件太挑剔（被浮层挡住、只读、类型对不上……）：日志里留全貌，界面上只说人话
                outcome.failed += 1
                item.ok = False
                item.note = "这一栏没能自动填上，请手动填写"
                log(f"填表失败（{item.label or item.name or item.selector}）：{_brief(exc)}")
                continue
            if item.is_file:
                # 官网常拿上传的简历去覆盖已经填好的栏，补回来
                outcome.restored += await self._restore(page, written)
            else:
                written.append(item)
            outcome.filled += 1
            item.ok = True
            item.note = ""
        if only is not None:
            await self.readback(page, [only])
            outcome.fields = [only.to_dict()]
            return outcome
        return outcome.absorb(await self.settle(page, fields))

    async def settle(self, page: "Page", fields: list[FormField]) -> FillOutcome:
        """整张表填完后的收尾：回读页面真实值 + 读官网自己弹的校验报错。

        逐栏填（``only``）时每一栏只管自己那一格，不做这两件事；整批填完再走这一遍，
        对照表右侧「实际填进去的」与官网的红字提示才齐。不联网、不截图，纯本地读 DOM。
        """
        outcome = FillOutcome()
        # 回读：把每一栏**页面里现在真实显示的值**记下来，对照表右侧据此并排显示
        # 「准备填的」和「实际填进去的」。
        await self.readback(page, fields)
        # 官网的校验报错：填进去不等于填对了，它说哪一栏不对就挂哪一栏，对照表如实
        # 显示，下一轮返修也只盯这几栏。
        outcome.flagged = await self._complaints(page, fields)
        outcome.fields = [item.to_dict() for item in fields]
        return outcome

    async def readback(self, page: "Page", fields: list[FormField]) -> None:
        """逐栏回读页面当前的实际显示值，写进 ``shown`` / ``echo``。

        这一步只读不写：读不到就留空（前端按「没回读」处理，不标红），读到了
        就与准备填的值比一比——比出不一样说明这一栏**看着填上了其实没生效**，
        对照表里标出来，让人一眼看到该手动补哪一格。

        网申助手也拿它当「用户刚在浏览器里手填了什么」的入口：用户在官网页面上
        补的那几栏，值只存在于页面里，只有回读能把它捞出来。

        各栏并发读（``_READBACK_CONCURRENCY``）：读的是各自的控件，互不干扰，
        结果按栏写回，先后顺序不影响结果，但整体耗时从「栏数 × 往返」压成
        「栏数 ÷ 并发 × 往返」。
        """
        todo = [item for item in fields if not item.hidden and not safety.is_irreversible(item)]
        if not todo:
            return
        slots = asyncio.Semaphore(_READBACK_CONCURRENCY)

        async def one(item: FormField) -> None:
            async with slots:
                try:
                    locator = await self._locator(page, item)
                except (PlaywrightError, _FillError):
                    return
                try:
                    shown = await self._read_one(locator, item)
                except PlaywrightError:
                    return
                item.shown = (shown or "").strip()[:120]
                item.echo = self._echo(item)

        await asyncio.gather(*(one(item) for item in todo))

    async def _read_one(self, locator: "Locator", item: FormField) -> str:
        """读一栏当前显示的值；组件库控件要连它外层的展示节点一起看。"""
        if item.is_file:
            # 上传栏回读的是官网自己写回的路径 / 文件名，读不到就看 input.files
            path = (await self._upload_path(locator)) or ""
            if path:
                return path
            return "已选文件" if await self._has_file(locator) else ""
        value = await self._value(locator)
        if value:
            return value
        if item.tag == "select" or item.type in ("radio", "checkbox"):
            return await self._shown_text(locator)
        if item.is_fake_select or item.is_date_widget:
            return await self._shown_text(locator)
        return ""

    @staticmethod
    def _echo(item: FormField) -> bool:
        """回读到的值和准备填的值对得上吗；没得比一律算对。

        只对「值就写在 input 里」的普通文本 / 数字栏做严格比对——这几类回读
        拿到的就是原值，对不上说明真没写进去。下拉 / 单选 / 日期这些组件库控件
        回写时会换写法（「本科」写成「本科（学士）」），各自的填法在填的那一刻
        已经严格校验过一次，这里不再重复判，免得把填对了的标成红的。
        """
        if item.is_file or item.tag == "select" or item.type in ("radio", "checkbox"):
            return True
        if item.is_fake_select or item.is_date_widget:
            return True
        shown = _loose(item.shown)
        want = _loose(item.filled)
        if not shown or not want:
            return True
        if item.type == "number":
            return _digits(item.shown) == _digits(item.filled)
        return shown == want

    async def _restore(self, page: "Page", done: list[FormField]) -> int:
        """上传简历后把被官网覆盖掉的栏补回来。

        有些官网会拿上传的简历解析结果重写姓名、手机号这些栏，把刚填好的内容
        冲掉。上传之后逐栏回读，发现和原来写进去的不一样就再写一次——这样
        「简历里写的」和「表单上填的」不会各说各话。
        """
        restored = 0
        for item in done:
            # 下拉、单选/多选有自己的回读方式，这里只管纯文本栏
            if item.tag == "select" or item.type in ("radio", "checkbox"):
                continue
            value = _numeric(item.filled) if item.type == "number" else item.filled
            if not value:
                continue
            try:
                locator = await self._locator(page, item)
                shown = await self._value(locator)
            except (PlaywrightError, _FillError):
                continue
            if normalize(shown) == normalize(value):
                continue
            try:
                await locator.fill(value, timeout=_ACTION_TIMEOUT)
            except PlaywrightError:
                continue
            restored += 1
        return restored

    async def _apply(self, page: "Page", item: FormField, group: list[FormField]) -> None:
        """按类型分派；定位不到控件时抛 ``_FillError``。"""
        locator = await self._locator(page, item)
        if item.is_file:
            await self._upload(page, locator, item)
            return
        if item.tag == "select":
            if group:
                await self._cascade_selects(page, item, group)
                return
            await self._select(locator, item)
            return
        if item.type in ("radio", "checkbox"):
            await self._choose(page, locator, item)
            return
        if item.is_fake_select:
            await self._overlay_select(page, item, locator)
            return
        if item.is_date_widget:
            await self._date(page, item, locator)
            return
        if item.type == "number":
            await self._number(locator, item)
            return
        if await self._dropdown_like(locator):
            # 扫描时没认出来的自研下拉（点一下才弹浮层）：直接 fill 只会写进一个
            # 假值，页面看着填上了其实没选中，改走「点开浮层 → 挑选项 → 回读」。
            await self._overlay_select(page, item, locator)
            return
        await _type_text(locator, item.filled)

    # --- 各类控件 ---

    async def _select(self, locator: "Locator", item: FormField) -> None:
        """原生下拉：选项文字常和资料对不上（「本科」对「本科（学士）」），按最贴近的挑。

        读不到选项（少数控件是异步塞进去的）就退回让浏览器自己按 value 匹配；
        级联下拉的下一级要等上一级选完才刷新，所以没挑到时再等几轮。
        """
        options = await self._options(locator)
        if not options:
            await self._select_blind(locator, item)
            return
        label = ""
        for attempt in range(_OPTION_RETRY):
            label = align_option(item.filled, [text for _value, text in options])
            if label or attempt + 1 >= _OPTION_RETRY:
                break
            await asyncio.sleep(_OPTION_INTERVAL)
            options = await self._options(locator)
        if not label:
            raise _FillError(f"下拉里没有「{item.filled}」这一项，请手动选择")
        value = next(value for value, text in options if text == label)
        await locator.select_option(value=value, timeout=_ACTION_TIMEOUT)

    async def _cascade_selects(
        self, page: "Page", head: FormField, group: list[FormField]
    ) -> None:
        """省 / 市 / 区三级联动下拉：按同一串地址逐级往下选。

        下一级的选项要等上一级选完才由接口刷新出来，所以每一级都得重读选项等它出来，
        不能拿扫描那一刻的旧选项去匹配。资料里没这么多级时，多出来的级留给人自己选。
        """
        levels = [head, *group]
        parts = split_admin(head.filled)
        if len(parts) > len(levels):
            # 官网只给了这么几级下拉：再往下的区 / 县填不进去，别假装填成功了
            log(
                f"级联下拉只有 {len(levels)} 级，资料里的「{''.join(parts[len(levels):])}」填不进去"
            )
        for index, (level, part) in enumerate(zip(levels, parts)):
            value = await self._wait_cascade_option(page, level, part)
            if value is None:
                # 是第几级没等到，直接决定用户看到的那句话：第一级没得选和
                # 「省选好了、市没出来」是两回事，别都报成「没有这一项」。
                if index == 0:
                    raise _FillError(f"下拉里没有「{part}」这一项，请手动选择")
                raise _FillError(f"上一级选好了，但下一级没等到「{part}」，请手动选择")
            locator = await self._locator(page, level)
            await locator.select_option(value=value, timeout=_ACTION_TIMEOUT)
            await asyncio.sleep(_AFTER_PICK)
            # 逐级回读：整页 postback 回来之后，控件有可能悄悄退回占位项，
            # 这时若不当场认输，用户只会看到「省填上了、市没填」这种半截结果。
            # 读不到值（控件没有回读能力）一律放行，只有「读到了、但不是刚选的那个」才认输。
            shown = await self._value(locator)
            if shown and shown != value:
                raise _FillError(
                    f"第 {index + 1} 级「{part}」选了没生效（官网回读成「{shown}」），请手动选择"
                )

    async def _wait_cascade_option(
        self, page: "Page", level: FormField, part: str
    ) -> str | None:
        """等某一级下拉里出现要选的那一项，返回它的 value；一直等不到返回 None。

        下一级的选项要等上一级选完才刷新出来（有的是前端接口，有的是整页 postback
        回来），所以每一轮都重新定位控件、重读选项——DOM 被 postback 换掉之后，
        扫描时打的 ``data-gj-idx`` 会失效，得靠 ``_locator`` 里的 CSS 兜底路径重找。
        """
        for attempt in range(_CASCADE_RETRY):
            try:
                locator = await self._locator(page, level)
                options = await self._options(locator)
            except (_FillError, PlaywrightError):
                options = []
            label = align_option(part, [text for _value, text in options])
            if label:
                return next(value for value, text in options if text == label)
            if attempt + 1 < _CASCADE_RETRY:
                await asyncio.sleep(_OPTION_INTERVAL)
        return None

    @staticmethod
    async def _select_blind(locator: "Locator", item: FormField) -> None:
        """读不到选项时让浏览器自己按文字匹配一次；对不上就当场认输。

        这里必须压上限：选项对不上时 Playwright 默认会重试 30 秒，一张表十几栏
        叠起来，用户只会看到「点了没反应」。
        """
        try:
            await locator.select_option(item.filled, timeout=_ACTION_TIMEOUT)
        except PlaywrightError as exc:
            raise _FillError(f"下拉里没有「{item.filled}」这一项，请手动选择") from exc

    async def _number(self, locator: "Locator", item: FormField) -> None:
        """数字栏：只写数字部分，官网的 number 控件塞「175cm」进去会直接报错。"""
        value = _numeric(item.filled)
        if not value:
            raise _FillError(f"「{item.filled}」不是数字，请手动填写")
        await _type_text(locator, value)

    async def _choose(self, page: "Page", locator: "Locator", item: FormField) -> None:
        """勾选类控件：整组单选 / 多选没有独立 input，只能按文字点。

        组件库（phoenix / antd / element）常把「性别」这种整组选项做成一个容器加
        一堆文字，扫出来的是一条 ``tag='div'`` 的字段，原生 ``check()`` 落不到地方。
        这种按选项文字点；普通 checkbox 还是走原生 ``check()``。
        """
        options = [text for text in item.options if text.strip()]
        if not options:
            await locator.check(timeout=_ACTION_TIMEOUT)
            return
        target = align_option(item.filled, options) or item.filled
        chosen = await self._find_choice(self._frame(page, item), target)
        if chosen is None:
            raise _FillError(f"选项里没有「{item.filled}」，请手动选择")
        # 整组单选 / 多选的选项节点同样是组件库的 div，普通点击过不了可操作性检查
        await _click_node(chosen)
        await asyncio.sleep(_AFTER_PICK)

    async def _overlay_select(self, page: "Page", item: FormField, locator: "Locator") -> None:
        """假下拉 / 级联：点开浮层，按浮层类型挑，最后回读校验。

        组件库的浮层花样多（日期面板 / 简单列表 / 多选选择器 / 地区级联），
        统一走「点开 → 认类型 → 按类型挑 → 确定 → 回读」；认不出类型就按
        「按文字点选项」这条最通用的路走。
        """
        frame = self._frame(page, item)
        await _open_widget(locator)
        if not await self._overlay_ready(frame):
            # 浮层没弹出来：组件库偶尔第一下收不到（节点刚被重渲染、还在动画里）。
            # 再点一次，别直接判成「浮层里没有这一项」。
            await _open_widget(locator)
            await self._overlay_ready(frame)
        kind = await self._overlay_kind(frame)
        chosen: list[str] = []
        try:
            if kind == "date":
                await self._overlay_date(page, item)
            elif kind == "area":
                await self._pick_area(page, item)
            elif kind == "picker":
                chosen = await self._pick_items(page, item)
            else:
                chosen = await self._click_option(page, item)
            await self._confirm(frame, kind)
        except _FillError:
            await self._dismiss(page, locator)
            raise
        await asyncio.sleep(_AFTER_PICK)
        if not await self._picked(locator, item, kind, chosen):
            await self._dismiss(page, locator)
            raise _FillError("下拉点了但没选上，请手动选择")
        if await self._overlay_open(frame):
            await self._dismiss(page, locator)

    async def _click_option(self, page: "Page", item: FormField) -> list[str]:
        """简单列表 / 级联：按「/」拆开，一级一级点文字；返回真正点中的选项文字。"""
        chosen: list[str] = []
        for part in _cascade_parts(item.filled):
            option = await self._wait_option(page, item, part)
            if option is None:
                raise _FillError(f"下拉浮层里没有「{part}」，请手动选择")
            # 先读文字再点：DOM 点击会让浮层立刻收起，点完再读就什么都读不到了
            label = await _option_text(option)
            await _click_node(option)
            chosen.append(label or part)
            await asyncio.sleep(_AFTER_PICK)
        return chosen

    async def _pick_items(self, page: "Page", item: FormField) -> list[str]:
        """多选选择器（「期望从事行业」这类）：勾上对得上的几项，再点确定。"""
        chosen: list[str] = []
        for part in _cascade_parts(item.filled):
            found = await self._wait_option(page, item, part)
            if found is None:
                continue
            label = await _option_text(found)
            await _click_node(found)
            chosen.append(label or part)
            await asyncio.sleep(_AFTER_PICK)
        if not chosen:
            raise _FillError(f"选择器里没有「{item.filled}」，请手动选择")
        return chosen

    async def _pick_area(self, page: "Page", item: FormField) -> None:
        """地区级联（「期望工作城市」这类）：一层层往下钻，钻到底再点确定。

        北森 phoenix 的地区浮层里，真正能勾中的是每个格子那个 14x14 的小勾选框
        （``.icon-container``），点文字不生效、只会往下一级钻；而且勾选框被浮层
        盖着，Playwright 的可操作性检查等不到，必须 ``force=True`` 直接点。
        """
        parts = _cascade_parts(item.filled)
        if not parts:
            raise _FillError(f"地区里没有「{item.filled}」，请手动选择")
        frame = self._frame(page, item)
        picked = False
        for index, part in enumerate(parts):
            node = await self._wait_area_item(frame, part)
            if node is None:
                break
            await self._check_area(node, part)
            picked = True
            await asyncio.sleep(_AFTER_PICK)
            if index + 1 < len(parts) and await self._find_area_item(frame, parts[index + 1]) is None:
                # 下一级还没出现：点一下这一级的名字，钻进它的子级列表
                await self._drill_area(node)
                await asyncio.sleep(_AFTER_PICK)
        if not picked:
            raise _FillError(f"地区里没有「{item.filled}」，请手动选择")

    async def _overlay_date(self, page: "Page", item: FormField) -> None:
        """日期浮层：面板里那个能敲字的输入框最稳，敲 ISO 回车即可。"""
        iso = _date_iso(item.filled)
        box = self._frame(page, item).locator(_OVERLAY_DATE_INPUT)
        try:
            if await box.count():
                await box.first.fill(iso, timeout=_ACTION_TIMEOUT)
                await box.first.press("Enter", timeout=_ACTION_TIMEOUT)
                return
        except PlaywrightError:
            pass
        cell = await self._wait_date_cell(page, item, iso)
        if cell is None:
            raise _FillError(f"日期面板里没找到「{iso}」，请手动选择")
        await _click_node(cell)

    async def _date(self, page: "Page", item: FormField, locator: "Locator") -> None:
        """日期控件：原生 date/month 直接按 ISO 写；组件库的要展开面板挑格子。"""
        if item.widget != "date":
            kind = "month" if item.type == "month" else "date"
            await locator.fill(_date_iso(item.filled, kind), timeout=_ACTION_TIMEOUT)
            return
        iso = _date_iso(item.filled)
        await _open_widget(locator)
        cell = await self._wait_date_cell(page, item, iso)
        if cell is None:
            await self._dismiss(page, locator)
            raise _FillError(f"日期面板里没找到「{iso}」，请手动选择")
        await _click_node(cell)
        await asyncio.sleep(_AFTER_PICK)
        if _digits(iso) not in _digits(await self._value(locator)):
            raise _FillError("日期点了但没选上，请手动选择")

    async def _upload(self, page: "Page", locator: "Locator", item: FormField) -> None:
        """文件上传：常规控件直接喂文件；拖拽区那种退一步走系统的文件选择框。

        很多官网的上传控件（webuploader / element 等）是「选了文件才由 JS 异步传」，
        喂完文件不代表真传上去了。喂之前先按控件声明的 ``accept`` 自查一遍类型，
        喂完之后再回读两处证据——控件自己挂上的文件数、以及旁边那个「已传文件路径」
        只读栏；两处都拿不到回执就如实认输，绝不像以前那样「读不到回执就当你成功了」。
        """
        path = self._pick_file(item.key)
        if path is None:
            what = "寸照" if item.key == "photo" else "简历"
            raise _FillError(f"本机没有可上传的{what}文件，请先在「简历配置」上传")
        self._check_upload(path, await self._accept(locator))
        before = await self._upload_path(locator)
        dialogs: list[Any] = []

        def _collect(dialog: Any) -> None:
            dialogs.append(dialog)
            asyncio.ensure_future(dialog.dismiss())

        page.on("dialog", _collect)
        try:
            await self._feed_file(page, locator, item, path)
            for _ in range(_UPLOAD_RETRY):
                if dialogs:
                    break
                after = await self._upload_path(locator)
                if after is not None:
                    if after != before:
                        # 服务器路径已经写回来了：真传上去了
                        return
                elif await self._has_file(locator):
                    # 官网没有可回读的只读栏，但文件确实挂到了控件上
                    return
                await asyncio.sleep(_UPLOAD_INTERVAL)
        finally:
            page.remove_listener("dialog", _collect)
        if dialogs:
            message = str(getattr(dialogs[-1], "message", "") or "")
            raise _FillError(self._upload_hint(message, path))
        if not await self._has_file(locator):
            raise _FillError("文件没能喂进这个上传控件，请手动上传")
        raise _FillError("文件喂进去了，但官网没回执，请手动确认上传结果")

    @staticmethod
    def _check_upload(path: Path, accept: str) -> None:
        """喂文件之前先自查：空文件、或类型不在控件声明的 ``accept`` 里，当场说清楚。

        这类问题官网自己也会拦，但它弹的是「F_EXCEED_SIZE」这种天书，用户只会看到
        「点了没反应」。在这里先判一遍，报一句人话，比让官网拦下来强。
        """
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise _FillError(f"读不到这份文件（{path.name}），请手动上传") from exc
        if size <= 0:
            raise _FillError(f"「{path.name}」是空文件，请换一份再传")
        if not _accept_ok(accept, path):
            kind = path.suffix.lower() or "未知类型"
            raise _FillError(f"官网只收「{accept}」这类文件，本机这份是 {kind}，请手动上传")

    async def _feed_file(
        self, page: "Page", locator: "Locator", item: FormField, path: Path
    ) -> None:
        """把文件交给控件：常规 input 直接喂；不行就往里找隐藏的 file 控件；再不行走系统文件选择框。"""
        target: Any = [str(path)] if item.multiple else str(path)
        try:
            await locator.set_input_files(target, timeout=_UPLOAD_TIMEOUT)
            return
        except PlaywrightError as exc:
            reason = str(exc).splitlines()[0] if str(exc) else "控件不支持直接上传"
        # 兜底一：扫到的是一层外壳（div / 按钮），真正的 input[type=file] 藏在里面
        nested = locator.locator(_NESTED_FILE)
        try:
            if await nested.count():
                await nested.first.set_input_files(target, timeout=_UPLOAD_TIMEOUT)
                return
        except PlaywrightError:
            pass
        # 兜底二：拖拽上传区——派一个带文件的 drop 事件进去，控件自己的 JS 收下
        if await _drop_file(locator, path) and await self._has_file(locator):
            return
        # 拖拽上传区：点它一下会弹出系统的文件选择框，直接把文件喂给那个选择框
        try:
            async with page.expect_file_chooser(timeout=_CHOOSER_TIMEOUT) as chooser:
                await locator.click(timeout=_UPLOAD_TIMEOUT)
            await (await chooser.value).set_files(str(path))
        except PlaywrightError as exc:
            raise _FillError(f"这个上传控件没法自动填（{reason}），请手动上传") from exc

    @staticmethod
    async def _upload_path(locator: "Locator") -> str | None:
        """读上传控件旁边那个「已传文件路径」的只读栏；没有这样的栏返回 None。"""
        try:
            value = await locator.evaluate(_UPLOAD_PATH_JS)
        except PlaywrightError:
            return None
        return value if isinstance(value, str) else None

    @staticmethod
    async def _has_file(locator: "Locator") -> bool:
        """回读控件自己挂上的文件数：大于 0 才算文件真喂进去了。"""
        try:
            count = await locator.evaluate(_UPLOAD_FILES_JS)
        except PlaywrightError:
            return False
        try:
            return int(count or 0) > 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    async def _accept(locator: "Locator") -> str:
        """读控件声明的接收类型（``accept``）；读不到返回空串（一律放行）。"""
        try:
            value = await locator.evaluate(_UPLOAD_ACCEPT_JS)
        except PlaywrightError:
            return ""
        return value if isinstance(value, str) else ""

    @staticmethod
    def _upload_hint(message: str, path: Path | None = None) -> str:
        """把官网弹的上传报错翻成一句人话，并带上文件名和体积。

        官网报「F_EXCEED_SIZE」这类天书时，用户根本不知道自己交上去的是哪一份、
        有多大。把这两样写进提示里，才知道下一步该换什么文件。
        """
        text = (message or "").strip()
        hint = next((phrase for needle, phrase in _UPLOAD_ALERTS if needle in text), "")
        if not hint:
            hint = f"官网上传失败（{text[:60]}）" if text else "官网上传没成功"
        where = f"官网不让传{_file_text(path)}：" if path is not None else "官网不让传："
        return f"{where}{hint}，请手动上传"

    # --- 定位与读取 ---

    async def _locator(self, page: "Page", item: FormField) -> "Locator":
        """先按扫描时打的 ``data-gj-idx`` 找，找不到退回扫描时记下的 CSS 路径。"""
        frame = self._frame(page, item)
        for selector in (item.selector, item.fallback):
            if not selector:
                continue
            locator = frame.locator(selector)
            if await locator.count():
                return locator.first
        raise _FillError("页面上找不到这个控件，页面可能已跳转或刷新")

    @staticmethod
    def _frame(page: "Page", item: FormField) -> "Frame":
        """取字段所在的 frame；frame 已消失时退回主文档。"""
        frames = page.frames
        if 0 <= item.frame < len(frames):
            return frames[item.frame]
        return page.main_frame

    @staticmethod
    async def _options(locator: "Locator") -> list[tuple[str, str]]:
        """读原生 select 的选项：``(value, 显示文字)``；读不到返回空表。"""
        try:
            raw = await locator.evaluate(_OPTIONS_JS)
        except PlaywrightError:
            return []
        out: list[tuple[str, str]] = []
        for row in raw or []:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                out.append((str(row[0]), str(row[1])))
        return out

    @staticmethod
    async def _value(locator: "Locator") -> str:
        """回读控件当前的显示值；读不到返回空串（空串一律按「没选上」处理）。"""
        try:
            return await locator.input_value(timeout=_ACTION_TIMEOUT)
        except PlaywrightError:
            return ""

    async def _already(self, page: "Page", item: FormField) -> bool:
        """这一栏在页面里是不是已经是目标值了。

        只对「值能如实读回来」的控件做这个判断——普通控件的 ``input.value``、原生
        下拉当前选中的那项文字、上传栏已经挂上的文件。单选 / 多选 / 假下拉 / 日期
        的显示值挂在组件库自己的节点上，读回来容易骗人，交给各自的填法去回读校验。

        这是「只填变化处」的闸门：第二次点「填入浏览器」时，页面里已经是目标值的
        栏一律跳过，不重复写、也不重复上传。
        """
        if item.type in ("radio", "checkbox") or item.is_fake_select or item.is_date_widget:
            return False
        try:
            locator = await self._locator(page, item)
        except _FillError:
            return False
        if item.is_file:
            # 上传栏：页面里已经挂着文件就别再传一遍（重复上传会覆盖官网自己解析出的内容）
            return await self._has_file(locator)
        if item.tag == "select":
            shown = await self._selected_text(locator)
            return bool(shown) and _same_value(shown, item.filled)
        shown = await self._value(locator)
        return bool(shown) and normalize(shown) == normalize(item.filled)

    @staticmethod
    async def _selected_text(locator: "Locator") -> str:
        """读原生下拉当前选中项的**文字**（不是 value 属性）。"""
        try:
            return str(await locator.evaluate(_SELECTED_JS) or "")
        except PlaywrightError:
            return ""

    async def _complaints(self, page: "Page", fields: list[FormField]) -> int:
        """读页面自己弹的校验报错 / 提示，挂到对应的栏上；返回命中的栏数。

        官网校验不过时会在那一栏下面写一行红字（「请填写正确的手机号」），程序填完
        不管它，用户看到的还是一张「都填好了」的对照表。这里把那些话读回来写进
        ``note`` 并标 ``ok=False``——不联网、不截图，纯本地读 DOM。
        """
        by_slot: dict[tuple[int, str], FormField] = {}
        loose: dict[str, FormField] = {}
        for item in fields:
            match = _SLOT_RE.search(item.selector)
            if not match:
                continue
            by_slot[(item.frame, match.group(1))] = item
            loose.setdefault(match.group(1), item)
        total = 0
        for frame_index, frame in enumerate(page.frames):
            try:
                raw = await frame.evaluate(_ERRORS_JS)
            except (PlaywrightError, TypeError):
                continue
            for row in raw or []:
                if not isinstance(row, dict):
                    continue
                slot = str(row.get("idx") or "")
                item = by_slot.get((frame_index, slot)) or loose.get(slot)
                text = str(row.get("text") or "").strip()
                if item is None or not text or item.note.startswith("官网提示"):
                    continue
                item.ok = False
                item.note = f"官网提示：{text}"[:120]
                total += 1
        return total

    @staticmethod
    async def _shown_text(locator: "Locator") -> str:
        """读控件外层展示出来的文字（组件库把选中值挂在控件外面，input 是空的）。"""
        try:
            return str(await locator.evaluate(_SHOWN_JS) or "")
        except PlaywrightError:
            return ""

    async def _picked(
        self,
        locator: "Locator",
        item: FormField,
        kind: str = "",
        chosen: list[str] | None = None,
    ) -> bool:
        """回读这一栏现在显示的值，确认真的选上了。

        组件库的 input.value 常是空的（值放在外面的展示节点里），所以先读 input，
        读不到再读这一栏的可见文字；两处都读不到就当它没有回读能力，放行。
        """
        shown = await self._value(locator) or await self._shown_text(locator)
        if not shown:
            return True
        if kind == "date" or item.is_date_widget:
            digits = _digits(_date_iso(item.filled))
            return bool(digits[:6]) and digits[:6] in _digits(shown)
        parts = _cascade_parts(item.filled)
        if kind == "area":
            # 地区级联会把人填的「上海」回写成官网自己的「上海市」，
            # 逐级比对：任一级对上了就算落值成功
            return any(normalize(part) in normalize(shown) for part in parts)
        if chosen:
            # 真正点中的是官网自己的写法（资料「8-10K」→ 官网「8001～10000」），
            # 回读就得拿这句去比，拿资料原值比会永远判成「没选上」；官网回写时
            # 常把区间那个「～」换成「-」，所以比的是「只留字」的串
            shown_key = _loose(shown)
            return any(_loose(text) and _loose(text) in shown_key for text in chosen)
        return normalize(parts[-1]) in normalize(shown) if parts else False

    @staticmethod
    async def _confirm(frame: "Frame", kind: str) -> None:
        """多选选择器 / 地区级联要点「确定」才落值；简单列表点了就落。

        浮层里可能并排放着「取消」和「确定」，必须按文字挑，不能取第一个。
        """
        if kind not in ("picker", "area"):
            return
        button = await FormFiller._find_confirm(frame)
        if button is None:
            return
        try:
            await _click_node(button)
        except PlaywrightError:
            pass
        await asyncio.sleep(_AFTER_PICK)

    @staticmethod
    async def _find_confirm(frame: "Frame") -> "Locator | None":
        """挑出浮层里的「确定」按钮；实在认不出文字就退回第一个。"""
        try:
            buttons = await frame.locator(_OVERLAY_CONFIRM).all()
        except PlaywrightError:
            return None
        fallback: "Locator | None" = None
        for button in buttons:
            try:
                if not await button.is_visible():
                    continue
                text = normalize(await button.inner_text(timeout=_ACTION_TIMEOUT))
            except PlaywrightError:
                continue
            if text in _CONFIRM_TEXTS:
                return button
            if fallback is None:
                fallback = button
        return fallback

    @staticmethod
    async def _dismiss(page: "Page", locator: "Locator") -> None:
        """收起没关掉的浮层，免得它挡住后面那些栏的点击。

        组件库的浮层对 Escape 免疫（实测北森 phoenix 按完 Esc 浮层还在），
        所以补一记点空白处——点左上角，那地方不会是任何选项。
        """
        try:
            await locator.press("Escape", timeout=_ACTION_TIMEOUT)
        except PlaywrightError:
            pass
        try:
            await page.keyboard.press("Escape")
        except PlaywrightError:
            pass
        try:
            await page.mouse.click(3, 3)
        except PlaywrightError:
            pass

    async def _wait_option(self, page: "Page", item: FormField, text: str) -> "Locator | None":
        """在浮层里等出文字最贴近的选项；浮层是异步渲染的，等几轮再放弃。

        选项多的时候浮层里只渲染看得见的那几行，要选的项在下面就得先滚下去，
        所以每一轮找不到就把浮层往下滚一屏再找。
        """
        frame = self._frame(page, item)
        for attempt in range(_OPTION_RETRY):
            found = await self._find_option(frame, text)
            if found is not None:
                return found
            if attempt + 1 >= _OPTION_RETRY:
                return None
            await self._scroll_overlay(frame)
            await asyncio.sleep(_OPTION_INTERVAL)
        return None

    @staticmethod
    async def _scroll_overlay(frame: "Frame") -> bool:
        """把展开的浮层往下滚一屏；滚得动（下面还有内容）返回 True。"""
        try:
            return bool(await frame.evaluate(_SCROLL_JS))
        except PlaywrightError:
            return False

    @staticmethod
    async def _dropdown_like(locator: "Locator") -> bool:
        """这栏是不是「点一下才弹浮层」的自研下拉？"""
        try:
            probe = await locator.evaluate(_PROBE_JS)
        except PlaywrightError:
            return False
        if not isinstance(probe, dict):
            return False
        return bool(probe.get("hint") or probe.get("combo"))

    @staticmethod
    async def _find_option(frame: "Frame", text: str) -> "Locator | None":
        """在一堆可见选项里挑最贴近的那个。

        候选的挑法交给 :func:`job.apply.matcher.align_option`（完全相等 / 互相包含 /
        同义说法 / 金额区间），所以资料里的「8-10K」能落到官网的「8001～10000」、
        「随时到岗」能落到「一周内」——光按字面比会全落空。
        """
        if not normalize(text):
            return None
        found: list[tuple["Locator", str]] = []
        for selector in _OPTION_SELECTORS:
            try:
                options = await frame.locator(selector).all()
            except PlaywrightError:
                continue
            for option in options:
                try:
                    if not await option.is_visible():
                        continue
                    label = (await option.inner_text(timeout=_ACTION_TIMEOUT)).strip()
                except PlaywrightError:
                    continue
                if not label:
                    continue
                found.append((option, label))
        if not found:
            return None
        label = align_option(text, [text for _option, text in found])
        if not label:
            return None
        for option, text in found:
            if text == label:
                return option
        return None

    @staticmethod
    async def _find_choice(frame: "Frame", text: str) -> "Locator | None":
        """在整组单选 / 多选里挑出文字对得上的那个选项（组件库不给 input，只能按文字点）。"""
        target = normalize(text)
        if not target:
            return None
        for selector in _CHOICE_SELECTORS:
            try:
                options = await frame.locator(selector).all()
            except PlaywrightError:
                continue
            for option in options:
                try:
                    if not await option.is_visible():
                        continue
                    label = normalize(await option.inner_text(timeout=_ACTION_TIMEOUT))
                except PlaywrightError:
                    continue
                if label and (label == target or target in label or label in target):
                    return option
        return None

    async def _wait_area_item(self, frame: "Frame", text: str) -> "Locator | None":
        """在地区浮层里等出文字对得上的那个格子；浮层是异步渲染的，等几轮再放弃。

        省份多的时候同样要往下滚才渲染得出来，每一轮找不到就先滚一屏。
        """
        for attempt in range(_OPTION_RETRY):
            found = await self._find_area_item(frame, text)
            if found is not None:
                return found
            if attempt + 1 >= _OPTION_RETRY:
                return None
            await self._scroll_overlay(frame)
            await asyncio.sleep(_OPTION_INTERVAL)
        return None

    @staticmethod
    async def _find_area_item(frame: "Frame", text: str) -> "Locator | None":
        """在一堆地区格子里挑文字最贴近的那个；候选挑法同 :meth:`_find_option`。"""
        if not normalize(text):
            return None
        try:
            cells = await frame.locator(_AREA_ITEM).all()
        except PlaywrightError:
            return None
        found: list[tuple["Locator", str]] = []
        for cell in cells:
            try:
                if not await cell.is_visible():
                    continue
                label = (await cell.inner_text(timeout=_ACTION_TIMEOUT)).strip()
            except PlaywrightError:
                continue
            if not label:
                continue
            found.append((cell, label))
        if not found:
            return None
        label = align_option(text, [text for _cell, text in found])
        if not label:
            return None
        for cell, text in found:
            if text == label:
                return cell
        return None

    @staticmethod
    async def _check_area(cell: "Locator", part: str) -> None:
        """勾中一个地区格子：点它那个小勾选框（点不到就退回点格子本身）。"""
        icon = cell.locator(_AREA_ICON)
        try:
            if await icon.count():
                await icon.first.click(force=True, timeout=_ACTION_TIMEOUT)
                return
        except PlaywrightError:
            pass
        try:
            await cell.click(force=True, timeout=_ACTION_TIMEOUT)
        except PlaywrightError as exc:
            raise _FillError(f"地区里没能选中「{part}」，请手动选择") from exc

    @staticmethod
    async def _drill_area(cell: "Locator") -> None:
        """钻进下一级：点格子上的文字（不勾选，只是切换出它的市 / 区列表）。"""
        label = cell.locator(_AREA_LABEL)
        try:
            if await label.count():
                await label.first.click(force=True, timeout=_ACTION_TIMEOUT)
                return
        except PlaywrightError:
            pass
        try:
            await cell.click(force=True, timeout=_ACTION_TIMEOUT)
        except PlaywrightError:
            pass

    @staticmethod
    async def _overlay_kind(frame: "Frame") -> str:
        """认一下浮层里装的是什么：日期面板 / 简单列表 / 多选选择器 / 地区级联。

        页面里常驻一堆 0 高的同名占位层，所以必须量高度，别把空壳当成浮层。
        认不出来就返回空串，走「按文字点选项」那条最通用的路。
        """
        for kind, selector in _OVERLAY_KINDS:
            try:
                nodes = await frame.locator(selector).all()
            except PlaywrightError:
                continue
            for node in nodes:
                if await _box_height(node) >= _OVERLAY_MIN_HEIGHT:
                    return kind
        return ""

    @staticmethod
    async def _overlay_open(frame: "Frame") -> bool:
        """浮层还开着吗？开着就收起来，免得挡住后面那些栏。"""
        if await FormFiller._overlay_kind(frame):
            return True
        for selector in _OPTION_SELECTORS:
            try:
                options = await frame.locator(selector).all()
            except PlaywrightError:
                continue
            for option in options:
                try:
                    if await option.is_visible():
                        return True
                except PlaywrightError:
                    continue
        return False

    @staticmethod
    async def _overlay_ready(frame: "Frame") -> bool:
        """等浮层真渲染出来（组件库是异步渲染的，点完立刻找常常扑空）。"""
        for attempt in range(_OPEN_RETRY):
            if await FormFiller._overlay_open(frame):
                return True
            if attempt + 1 < _OPEN_RETRY:
                await asyncio.sleep(_OPTION_INTERVAL)
        return False

    async def _wait_date_cell(self, page: "Page", item: FormField, iso: str) -> "Locator | None":
        """在日期面板里等出那一格；面板同样要等渲染。"""
        frame = self._frame(page, item)
        for attempt in range(_OPTION_RETRY):
            found = await self._find_date_cell(frame, iso)
            if found is not None or attempt + 1 >= _OPTION_RETRY:
                return found
            await asyncio.sleep(_OPTION_INTERVAL)
        return None

    @staticmethod
    async def _find_date_cell(frame: "Frame", iso: str) -> "Locator | None":
        """优先找带完整日期的格子（antd 的 title、自研的 data-date），
        再退一步找「只有日号」的那种格子（element-ui 的日期表）。"""
        for selector in (f'[title="{iso}"]', f'[data-date="{iso}"]'):
            locator = frame.locator(selector)
            try:
                if await locator.count() and await locator.first.is_visible():
                    return locator.first
            except PlaywrightError:
                continue
        day = iso[8:10] if len(iso) >= 10 else ""
        if not day:
            return None
        for selector in (".el-date-table td.available", ".ant-picker-cell-inner"):
            try:
                cells = await frame.locator(selector).all()
            except PlaywrightError:
                continue
            for cell in cells:
                try:
                    if not await cell.is_visible():
                        continue
                    if (await cell.inner_text()).strip().lstrip("0") == day.lstrip("0"):
                        return cell
                except PlaywrightError:
                    continue
        return None

    @staticmethod
    def _pick_file(key: str) -> Path | None:
        """这一栏要传的本机文件：认领成 resume 用简历原件，认领成 photo 用寸照。"""
        config = SearchConfigRow.load()
        field = "photo_path" if key == "photo" else "resume_path"
        raw = str(config.get(field) or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_file() else None
