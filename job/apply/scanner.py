"""表单扫描：把页面（含 iframe）里的表单控件读成一份结构化字段清单。

只在浏览器里做「取原始属性」这一件事——控件的类型、名字、附近文字原样带回，
怎么理解这些字段交给 Python 侧（好测，也好改）。
"""

from __future__ import annotations

import asyncio
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
  // 取文本前要摘掉的噪音节点：控件本身、脚本样式，以及组件库的「假占位」
  // （phoenix 之类会把「请选择 / 请输入 / 中国大陆」塞在隐藏 span 里，
  // 不摘掉就会被当成这一栏的标签）。
  const NOISE =
    'input, select, textarea, button, script, style, [hidden], [aria-hidden="true"], ' +
    '[class*="placeholder"], [class*="Placeholder"], [class*="unmodeled"]';
  // 容器自己的文字：先把里面的控件摘掉再取文本。select 的 innerText 就是选项文字，
  // 不摘掉会把「男 / 女」当成这一栏的标签。
  const ownLabelText = (node) => {
    const clone = node.cloneNode(true);
    clone.querySelectorAll(NOISE).forEach((n) => n.remove());
    return (clone.textContent || '').replace(/\\s+/g, ' ').trim();
  };
  // 类名可能是字符串，也可能是 SVG 的 SVGAnimatedString
  const clsOf = (node) => {
    if (!node) return '';
    const raw = node.className;
    if (typeof raw === 'string') return raw;
    if (raw && typeof raw.baseVal === 'string') return raw.baseVal;
    return '';
  };
  // 元素是否还显示着（display / visibility / opacity / 尺寸）
  const shown = (node) => {
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (Number(style.opacity) === 0) return false;
    const r = node.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  // 页头 / 导航 / 搜索栏里的输入框不是表单栏，扫进来只会多出一行
  // （招聘官网页头常挂「搜索职位」和一个「上传简历」按钮）。
  const CHROME_ROLES = ['banner', 'navigation', 'search', 'menubar', 'toolbar'];
  const CHROME_CLS =
    /(^|[-_])(nav|navbar|nav-bar|topbar|top-bar|header|toolbar|breadcrumb|searchbar|search-bar|search-box|searchbox|search-form|searchform)([-_]|$)/i;
  const inChrome = (el) => {
    let node = el;
    for (let i = 0; i < 6 && node && node.nodeType === 1; i += 1) {
      const tag = node.tagName.toLowerCase();
      if (tag === 'nav' || tag === 'header') return true;
      if (CHROME_ROLES.includes((node.getAttribute('role') || '').toLowerCase())) return true;
      if (CHROME_CLS.test(clsOf(node))) return true;
      node = node.parentElement;
    }
    return false;
  };
  // 表单行标题：<div class="form-item"><div class="form-item__title"><label>性别</label></div>
  // <div class="form-item__control">…</div></div> 这类写法里，标题在控件外面、和控件同属
  // 一个「行」容器。往上逐层找「第一个带标题的容器」，那一层的标题就是这一栏的名字——
  // 比摸兄弟节点可靠得多。组件库（phoenix / 北森）会在控件外面再套三四层没意义的壳
  // （unmodeled-layer__inner / __protect / __content），所以层数给足 10 层；一旦某一层
  // 冒出两个以上不同标题，说明已经爬到「行」上面去了，就此收手。
  const TITLE_SEL =
    'label, legend, [class*="label"], [class*="title"], [class*="caption"], [class*="name"]';
  const ROW_TAGS = ['tr', 'td', 'th', 'li', 'label', 'fieldset'];
  // 只认「行」这一级的容器，免得往上摸到「教育经历」这种区块小标题就当成了栏名。
  const ROW_HINT =
    /(^|[-_\\s])(form-item|formitem|form-row|formrow|form-group|formgroup|form-field|formfield|item|row|cell|field|control|value)([-_\\s]|$)/i;
  // 控件所在的那一行：返回 {node, text}。node 是「行」容器本身，除了给标签，
  // 还用来判断「同一行里的几个控件算一栏」（结束时间 + 至今 这种）。
  const rowOf = (el) => {
    let node = el.parentElement;
    for (let i = 0; i < 10 && node && node.nodeType === 1; i += 1) {
      const seen = [];
      for (const cand of node.querySelectorAll(TITLE_SEL)) {
        if (cand === el || cand.contains(el)) continue;
        if (cand.querySelector('input, select, textarea')) continue;
        const text = ownLabelText(cand);
        if (text && text.length <= 24 && seen.indexOf(text) < 0) seen.push(text);
      }
      if (seen.length > 1) return null;
      if (seen.length === 1) {
        const looksRow =
          ROW_TAGS.indexOf(node.tagName.toLowerCase()) >= 0 || ROW_HINT.test(clsOf(node));
        // 标题就在控件旁边（两层以内）时不必是「行」容器；离得远就必须落在行上，
        // 否则很容易把「教育经历」这类区块小标题当成栏名。
        return looksRow || i <= 2 ? { node: node, text: seen[0] } : null;
      }
      node = node.parentElement;
    }
    return null;
  };
  const rowTitle = (el) => {
    const row = rowOf(el);
    return row ? row.text : '';
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
  // 这一栏对「该填成什么样」的约束：单位提示、只让填数字的 pattern、数值范围、
  // 长度上限。填表前按这些提示把值改成这一栏要的样子（身高 175 ↔ 1.75）。
  const hintsOf = (el) => ({
    min: el.getAttribute('min') || '',
    max: el.getAttribute('max') || '',
    step: el.getAttribute('step') || '',
    pattern: el.getAttribute('pattern') || '',
    maxlength: el.getAttribute('maxlength') || '',
    inputmode: el.getAttribute('inputmode') || '',
  });
  // 标签候选一：最近的那个「只装了这一个控件」的容器文字，
  // 对应 <div><span>姓名</span><input></div> 这类没有真 label 的写法。
  const ownText = (el) => {
    let node = el.parentElement;
    for (let i = 0; i < 4 && node; i += 1) {
      const controls = node.querySelectorAll('input:not([type="hidden"]), select, textarea');
      if (controls.length > 1) break;
      const text = ownLabelText(node);
      if (text && text.length <= 30) return text;
      node = node.parentElement;
    }
    return '';
  };
  // 标签候选二：表格布局里紧邻的那一格文字。只看自己所在格左边那一格，
  // 且那一格必须没装控件——它要是装着控件，说明那是「上一栏」，
  // 再往左摸就会把上一栏的标签（电子邮箱）安到这一栏（身份证号）头上。
  // 从「所在的格」起步而不是从控件起步：生源地那种一格塞了三个下拉、
  // 文件上传那种格子里只有「浏览…」按钮的写法，靠这一步才找得回真标签。
  const cellText = (el) => {
    const cell = el.closest('td, th');
    const sib = cell ? cell.previousElementSibling : null;
    if (!sib) return '';
    if (sib.matches('input, select, textarea')) return '';
    if (sib.querySelector('input:not([type="hidden"]), select, textarea')) return '';
    const text = ownLabelText(sib);
    return text && text.length <= 20 ? text : '';
  };
  // 标签候选三：非表格布局里紧挨着控件的那段文字（表格布局走 cellText）。
  // 兄弟本身是控件（上一栏）就停手，别再往左摸。
  const siblingText = (el) => {
    let node = el;
    for (let i = 0; i < 4 && node; i += 1) {
      const sib = node.previousElementSibling;
      if (sib) {
        if (sib.matches('input, select, textarea')) break;
        if (sib.querySelector('input:not([type="hidden"]), select, textarea')) break;
        const text = ownLabelText(sib);
        if (text && text.length <= 30) return text;
      }
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
  // 假下拉 / 级联 / 日期控件：antd、element-ui 这类组件库把值藏在 input 里，
  // 选项要展开浮层才出现，直接 fill() 只会写进一个假值。这里按外层容器的类名认出来，
  // 交给填表环节走「点开 → 点选项 → 回读校验」；原生 select 与勾选类控件不掺和。
  // 按「词」匹配，避免 selector / candidate 这类包含子串的词误判。
  const WIDGET_RE = /(^|[-_])(select|dropdown|combobox)([-_]|$)/;
  const DATE_RE = /(^|[-_])(date|datetime|datepicker|picker|calendar)([-_]|$)/;
  const CASCADER_RE = /(^|[-_])cascader([-_]|$)/;
  const widgetOf = (el, tag, type) => {
    if (tag === 'select' || ['file', 'radio', 'checkbox'].includes(type)) return '';
    // 不再盯着某几个组件库的固定类名，改成「往上几层看类名里有没有这些词」——
    // phoenix / 北森、antd、element-ui 都覆盖得到，新的组件库也不用再改代码。
    let node = el;
    for (let i = 0; i < 4 && node && node.nodeType === 1; i += 1) {
      const cls = clsOf(node).toLowerCase();
      if (CASCADER_RE.test(cls)) return 'cascader';
      if (DATE_RE.test(cls)) return 'date';
      if (WIDGET_RE.test(cls)) return 'select';
      node = node.parentElement;
    }
    const role = el.getAttribute('role') || '';
    const pop = el.getAttribute('aria-haspopup') || '';
    if (role === 'combobox' || pop === 'listbox') return 'select';
    return '';
  };
  const CTRL_SEL = 'input:not([type="hidden"]), select, textarea';
  const collected = [];
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const tag = el.tagName.toLowerCase();
    const type = (tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : (el.type || 'text')).toLowerCase();
    if (type === 'hidden' || el.disabled) continue;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const invisible = style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0;
    if (invisible && type !== 'file') continue;
    if (rect.width === 0 && rect.height === 0 && type !== 'file') continue;
    if (inChrome(el)) continue;
    collected.push({ el, tag, type });
  }
  // 一行只出一栏：行里既有普通控件、又跟着「至今」这种小勾选框时，只留普通控件。
  const byRow = new Map();
  const picked = new Set();
  for (const it of collected) {
    it.row = rowOf(it.el);
    if (!it.row) continue;
    const list = byRow.get(it.row.node);
    if (list) list.push(it);
    else byRow.set(it.row.node, [it]);
  }
  for (const list of byRow.values()) {
    const main = list.filter((x) => x.type !== 'checkbox' && x.type !== 'radio');
    (main.length ? main : list).forEach((x) => picked.add(x));
  }
  let idx = 0;
  for (const it of collected) {
    const el = it.el;
    const tag = it.tag;
    const type = it.type;
    if (it.row && !picked.has(it)) continue;
    // 控件在**文档**里的位置（不是视口里的）：视觉核对要在整页截图上画框，
    // 视口坐标会随滚动位置漂，加上滚动量才是稳定的那一份。
    const box = el.getBoundingClientRect();
    const labelText = el.labels && el.labels.length ? textOf(el.labels[0]) : '';
    const own = ownText(el);
    const cell = cellText(el);
    // 文件栏必须能说清自己叫什么（行标题 / 真 label / 容器文字 / 左边那一格）。
    // 只剩「旁边一句无关的话」可认的，是页头那种「拖拽上传简历快速解析」的独立小工具，
    // 不是表单里的一栏——填进去还会把整页表单冲掉。
    if (type === 'file' && !(it.row || labelText || own || cell || el.getAttribute('aria-label'))) {
      continue;
    }
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
      readonly: !!el.readOnly,
      hints: hintsOf(el),
      labelText: labelText,
      ownText: own,
      cellText: cell,
      siblingText: siblingText(el),
      options: tag === 'select'
        ? [...el.options].map((o) => (o.textContent || '').trim()).filter(Boolean).slice(0, 40)
        : groupOptions(el),
      value: el.value || '',
      checked: !!el.checked,
      multiple: !!el.multiple,
      widget: widgetOf(el, tag, type),
      rowText: it.row ? it.row.text : '',
      path: cssPath(el),
      box: [
        Math.round(box.left + window.scrollX),
        Math.round(box.top + window.scrollY),
        Math.round(box.width),
        Math.round(box.height),
      ],
    });
    idx += 1;
  }
  // 单选 / 多选整组兜底：组件库常不给每个选项配 input，只用一个容器装文字
  // （phoenix 的「性别」就是这样），按控件扫会整栏漏掉。这里按「组」补一轮。
  const GROUP_SEL =
    '[class*="radio-group"], [class*="radiogroup"], [class*="radioGroup"], [role="radiogroup"], ' +
    '[class*="checkbox-group"], [class*="checkboxgroup"], [class*="checkboxGroup"]';
  const OPT_SEL = 'label, [class*="radio"], [class*="checkbox"]';
  for (const group of document.querySelectorAll(GROUP_SEL)) {
    if (!shown(group) || inChrome(group)) continue;
    if (group.querySelector('input:not([type="hidden"])')) continue;
    const opts = [];
    for (const opt of group.querySelectorAll(OPT_SEL)) {
      const text = ownLabelText(opt);
      if (text && text.length <= 20 && !opts.includes(text)) opts.push(text);
    }
    if (opts.length < 2) continue;
    const cls = clsOf(group).toLowerCase();
    const box = group.getBoundingClientRect();
    group.setAttribute('data-gj-idx', String(idx));
    out.push({
      idx,
      tag: 'div',
      type: cls.includes('checkbox') ? 'checkbox' : 'radio',
      name: '',
      id: group.id || '',
      placeholder: '',
      ariaLabel: '',
      required: false,
      readonly: false,
      hints: hintsOf(group),
      labelText: '',
      ownText: '',
      cellText: '',
      siblingText: '',
      rowText: rowTitle(group),
      options: opts.slice(0, 30),
      value: '',
      checked: false,
      multiple: false,
      widget: '',
      path: cssPath(group),
      box: [
        Math.round(box.left + window.scrollX),
        Math.round(box.top + window.scrollY),
        Math.round(box.width),
        Math.round(box.height),
      ],
    });
    idx += 1;
  }
  // 组件库的「纯角色」控件：有些 Select / Cascader / DatePicker / 单选组压根不渲染
  // <input>，整栏只有一个带 role 的 div（antd 的 Select、element-ui 的 el-select、
  // 各种自研组件都会这么写）。按控件标签扫会整栏漏掉——这就是「换个企业又少
  // 一大堆栏」的另一半根子。这里按可访问性角色补一遍，只补「前面没收到过」的：
  // 自己带 data-gj-idx，或肚子里装着已收的控件，都算收到过。
  // 选项、标签仍走前面那几套启发式，填表环节按 widget 走「点开 → 点选项 → 回读」。
  const ROLE_TYPES = {
    combobox: 'select',
    listbox: 'select',
    tree: 'select',
    radiogroup: 'radio',
    textbox: 'text',
    searchbox: 'text',
    spinbutton: 'text',
    switch: 'checkbox',
    slider: 'text',
  };
  const ARIA_SEL =
    Object.keys(ROLE_TYPES).map((r) => '[role="' + r + '"]').join(',') +
    ',[aria-haspopup="listbox"],[aria-haspopup="tree"],[aria-haspopup="grid"]';
  // 收过就算收过：自己带标记、肚子里装着已收的控件、或者本身就在某个已收控件的
  // 肚子里（浮层的 listbox / tree 常常嵌在 combobox 内部，别当成新的一栏）。
  const already = (el) => !!el.closest('[data-gj-idx]') || !!el.querySelector('[data-gj-idx]');
  for (const el of document.querySelectorAll(ARIA_SEL)) {
    if (already(el) || !shown(el) || inChrome(el)) continue;
    const role = (el.getAttribute('role') || '').toLowerCase();
    const popup = (el.getAttribute('aria-haspopup') || '').toLowerCase();
    const cls = clsOf(el).toLowerCase();
    let type = ROLE_TYPES[role] || (popup ? 'select' : '');
    if (!type) continue;
    let widget = type === 'select' ? 'select' : '';
    // 类名比 role 更具体：cascader / 日期控件都会写成 combobox，得按类名拨回来。
    if (CASCADER_RE.test(cls)) {
      widget = 'cascader';
      type = 'select';
    } else if (DATE_RE.test(cls) || popup === 'grid') {
      widget = 'date';
      type = 'text';
    }
    const opts = [];
    for (const opt of el.querySelectorAll('[role="option"], [role="treeitem"], option')) {
      const text = ownLabelText(opt);
      if (text && text.length <= 24 && opts.indexOf(text) < 0) opts.push(text);
    }
    const label =
      rowTitle(el) ||
      ownText(el) ||
      cellText(el) ||
      siblingText(el) ||
      el.getAttribute('aria-label') ||
      '';
    // 认不出是哪一栏、又列不出选项的纯角色 div 多半是装饰，别塞进对照表。
    if (!label && !opts.length) continue;
    const box = el.getBoundingClientRect();
    el.setAttribute('data-gj-idx', String(idx));
    out.push({
      idx,
      tag: 'div',
      type,
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      required: el.getAttribute('aria-required') === 'true',
      readonly: false,
      hints: hintsOf(el),
      labelText: '',
      ownText: label,
      cellText: '',
      siblingText: '',
      rowText: label,
      options: opts.slice(0, 30),
      value: '',
      checked: false,
      multiple: false,
      widget,
      path: cssPath(el),
      box: [
        Math.round(box.left + window.scrollX),
        Math.round(box.top + window.scrollY),
        Math.round(box.width),
        Math.round(box.height),
      ],
    });
    idx += 1;
  }
  return out;
}
"""

# 页面上的「拦路虎」提示词：出现这些就别硬填了，喊人。
# 只认「人机验证 / 滑块」这类强特征——「验证码」三个字本身就是很多官网表单的
# 一个栏名（简历页常带「验证码：」这一行），拿它当拦路虎会把整页判成要人处理。
_GATE_HINTS = ("captcha", "人机验证", "安全验证", "滑块", "拖动滑块")
# 明显的验证码 iframe
_GATE_IFRAMES = (
    "iframe[src*='captcha']",
    "iframe[src*='geetest']",
    "iframe[src*='yidun']",
    "iframe[id*='captcha']",
)

# 可访问性大纲发给模型时的字数上限：官网表单再长也够用，又不至于把提示词撑爆。
_OUTLINE_MAX = 12000

# 页面信息脚本：读标题 / 站点名 / 一级标题，用来判断这是哪家公司、哪个岗位
_META_JS = """
() => {
  const content = (sel) => {
    const el = document.querySelector(sel);
    return el ? (el.getAttribute('content') || '').trim() : '';
  };
  const h1 = document.querySelector('h1');
  return {
    title: (document.title || '').trim(),
    site: content('meta[property="og:site_name"]') || content('meta[name="application-name"]'),
    h1: h1 ? (h1.innerText || h1.textContent || '').trim().slice(0, 80) : '',
    host: location.hostname || '',
  };
}
"""

# 标题里的分隔符：官网标题常见「岗位 - 公司」「公司 | 招聘」
_TITLE_SEPS = re.compile(r"\s*[|｜\-–—_·:：]\s*")
# 这些片段不是公司名，别拿来当公司
_GENERIC_NAMES = frozenset(
    {
        "首页",
        "登录",
        "招聘",
        "人才招聘",
        "校园招聘",
        "社会招聘",
        "加入我们",
        "联系我们",
        "岗位详情",
        "职位详情",
    }
)

# 省略号也要算标点：官网的文件上传按钮写「浏览…」，不归一化掉就认不出是按钮字，
# 会把整栏的标签定成「浏览…」，真标签（附件简历）就丢了。
_PUNCT = re.compile(r"[\s*＊·・:：,，.。…/／\\|｜()（）\[\]【】<>《》\"'`~!！?？;；\-—_+]+")

# 文件上传控件旁边那行「按钮字」不是字段名：<span>浏览…<input type=file></span>
# 这种写法里，容器文字是按钮，真标签在上一格。归一化后按这几个词直接丢掉。
_BUTTON_TEXTS = frozenset(
    {"浏览", "选择文件", "未选择文件", "上传", "上传文件", "选择", "browse", "choosefile"}
)

# 文本域旁边那个字数计数器（「0/2000」）不是栏名，别让它顶掉真标签。
_COUNTER_RE = re.compile(r"^\d+\s*/\s*\d+$")


def normalize(text: str) -> str:
    """归一化字段文字：去标点空白、统一小写，便于比对。"""
    return _PUNCT.sub("", (text or "").strip().lower())


def read_page_meta(raw: Any) -> dict[str, str]:
    """把页面信息脚本的返回值规整成 ``title`` / ``site`` / ``h1`` / ``host``。"""
    data = raw if isinstance(raw, dict) else {}
    return {
        "title": str(data.get("title") or "").strip(),
        "site": str(data.get("site") or "").strip(),
        "h1": str(data.get("h1") or "").strip(),
        "host": str(data.get("host") or "").strip(),
    }


def _title_parts(title: str) -> list[str]:
    """把页面标题按分隔符拆成几段，去掉空段。"""
    return [part.strip() for part in _TITLE_SEPS.split(title or "") if part.strip()]


def _drop_shadow_uploads(fields: list[FormField]) -> list[FormField]:
    """去掉上传组件放在文件控件旁边的那个「只读路径框」。

    webuploader / EasyUI 这类上传组件是「一个只读文本框显示文件名 + 一个真正的
    ``input[type=file]``」两件套，扫出来就是同一栏的两行（「简历上传：文本 必填」
    +「简历上传：上传文件」）。只读框本身填不进去，留着只会让用户以为程序漏填了。

    判定：同一个 frame 里，标签归一化后相同，且这个非文件控件是「普通的单行文本框」
    （select / 勾选框 / 多行文本另有用途，不动），再满足下面任意一条就丢：

    * 它是只读的（上传组件最常见的写法）；
    * 它紧挨着那个文件控件；
    * 它的值是空的（真文本框多半会带个初值或占位符）。

    实测里官网的上传组件花样很多：有的把 file 控件裹在 div 里、中间还夹着隐藏域，
    位置并不相邻；有的影子框干脆不带 readonly。只按「只读 + 相邻」判会漏掉这些，
    于是同一栏还是两行。这里放宽到「同标签组里出现了 file 控件」——
    同一栏既要求填文本又要求传文件，现实中不存在。
    """
    positions: dict[int, list[int]] = {}
    for pos, item in enumerate(fields):
        positions.setdefault(item.frame, []).append(pos)
    drop: set[int] = set()
    for group in positions.values():
        files = [pos for pos in group if fields[pos].is_file and normalize(fields[pos].label)]
        if not files:
            continue
        labels = {normalize(fields[pos].label) for pos in files}
        for pos in group:
            item = fields[pos]
            if item.is_file or normalize(item.label) not in labels:
                continue
            if not _is_plain_text_box(item):
                continue
            near = any(abs(pos - other) == 1 for other in files)
            if item.readonly or near or not item.value.strip():
                drop.add(pos)
    return [field for pos, field in enumerate(fields) if pos not in drop] if drop else fields


def _is_plain_text_box(item: FormField) -> bool:
    """是不是一个「只能敲字」的普通输入框（上传组件的影子框就是这种）。"""
    if item.tag == "textarea":
        return True
    if item.tag != "input":
        return False
    return item.type in ("", "text", "search", "url", "tel", "email", "password", "number")


def guess_company(meta: dict[str, str]) -> str:
    """猜公司名：站点名优先，其次标题最后一段（官网标题常是「岗位 - 公司」）。"""
    site = str(meta.get("site") or "").strip()
    if site and site not in _GENERIC_NAMES:
        return site[:60]
    parts = _title_parts(str(meta.get("title") or ""))
    if len(parts) > 1 and parts[-1] not in _GENERIC_NAMES:
        return parts[-1][:60]
    return ""


def guess_title(meta: dict[str, str]) -> str:
    """猜岗位名：一级标题优先，其次标题第一段。"""
    h1 = str(meta.get("h1") or "").strip()
    if h1:
        return h1[:60]
    parts = _title_parts(str(meta.get("title") or ""))
    if parts and parts[0] not in _GENERIC_NAMES:
        return parts[0][:60]
    return ""


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
    multiple: bool = False
    # 只读栏：上传组件常拿它显示「已选文件路径」，本身填不进去
    readonly: bool = False
    # 组件库控件（假下拉 / 级联 / 日期）：空串就是普通控件
    widget: str = ""
    # 这一栏对取值的约束（单位提示、pattern、min/max、inputmode……）：填表前按它把值
    # 改成这一栏要的样子（身高 175 ↔ 1.75）。取不到就是空字典。
    hints: dict[str, str] = field(default_factory=dict)
    selector: str = ""
    fallback: str = ""
    # 控件在**文档**里的位置 ``[x, y, 宽, 高]``（CSS 像素，iframe 里的是帧内坐标）。
    # 只给视觉核对在整页截图上画框用；取不到就是空列表，画不了框不影响别的。
    box: list[float] = field(default_factory=list)
    # 匹配与填充结果
    key: str = ""
    filled: str = ""
    # 填完之后从页面回读到的**实际显示值**（纯本地读 DOM，不联网）。
    # ``echo`` 是这次回读的结论：读回来的和准备填的是不是同一个值。
    shown: str = ""
    echo: bool = True
    source: str = ""
    confidence: float = 0.0
    ok: bool = True
    note: str = ""
    # 这一栏的值是模型按已有资料**推**出来的（资料库里本来没有这个键），不是资料原文。
    # 面板据此把它单列一区请用户核对；点「填入浏览器」认可后才回流进资料库。
    inferred: bool = False
    # 级联下拉（省 / 市 / 区三级都叫「生源地」）：第一级带着整串地址，其余几级的
    # 序号记在 ``group`` 里，面板只显示第一级这一行；``hidden`` 的几行不单独展示。
    group: list[int] = field(default_factory=list)
    hidden: bool = False

    @classmethod
    def from_raw(cls, raw: dict[str, Any], *, index: int, frame: int = 0) -> "FormField":
        """把浏览器里带回来的原始属性整理成一个字段。"""
        tag = str(raw.get("tag") or "input")
        field_type = str(raw.get("type") or "text")
        name = str(raw.get("name") or "")
        element_id = str(raw.get("id") or "")
        placeholder = str(raw.get("placeholder") or "")
        label = cls._pick_label(raw, name=name, element_id=element_id, placeholder=placeholder)
        # 采集脚本给每个控件打的 ``data-gj-idx`` 是**按 frame 各自从 0 数**的，
        # 而 ``index`` 是全页面连号。iframe 表单（官网很常见）里两者对不上，
        # 拿全局号去查会把别的一栏定位错，所以优先用脚本带回来的那个号。
        slot = raw.get("idx")
        marker = slot if isinstance(slot, int) else index
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
            multiple=bool(raw.get("multiple")),
            readonly=bool(raw.get("readonly")),
            widget=str(raw.get("widget") or ""),
            hints=cls._hints(raw),
            selector=f'[data-gj-idx="{marker}"]',
            fallback=str(raw.get("path") or ""),
            box=cls._box(raw),
        )

    @staticmethod
    def _hints(raw: dict[str, Any]) -> dict[str, str]:
        """把采集脚本带回来的提示属性规整成 ``{min, max, pattern, inputmode...}``。

        空值一律丢掉：调用方按「有没有这一条提示」判断，留下空串只会让判断变复杂。
        """
        data = raw.get("hints")
        if not isinstance(data, dict):
            return {}
        return {
            str(key): str(value).strip()
            for key, value in data.items()
            if str(value or "").strip()
        }

    @staticmethod
    def _box(raw: dict[str, Any]) -> list[float]:
        """把采集脚本带回来的 ``box`` 规整成 ``[x, y, 宽, 高]``；不合规就是空列表。

        宽或高为 0 的框画出来只是个点，画上去反而干扰模型判断，一并当没有。
        """
        values = raw.get("box")
        if not isinstance(values, (list, tuple)) or len(values) != 4:
            return []
        try:
            box = [float(v) for v in values]
        except (TypeError, ValueError):
            return []
        if box[2] <= 0 or box[3] <= 0:
            return []
        return box

    @staticmethod
    def _pick_label(raw: dict[str, Any], *, name: str, element_id: str, placeholder: str) -> str:
        """按可靠性挑一个能代表字段的中文说法。

        真 ``label`` / ``aria-label`` 最可信；其次是「只装了这一个控件的容器」
        和「表格里左边那一格」——这两档能修好表格型官网表单把隔壁单元格的标签
        一起拼进来的毛病（「电子邮箱」被当成「身份证号」的标签）。文件上传控件
        旁边那行「浏览…」按钮字不算标签，跳过。占位符与 ``name`` 只是最后的
        兜底，宁可拿英文 name，也别把别人的标签安到这一栏上。
        """
        for candidate in (
            raw.get("labelText"),
            raw.get("rowText"),
            raw.get("ariaLabel"),
            raw.get("ownText"),
            raw.get("cellText"),
            raw.get("siblingText"),
            placeholder,
        ):
            text = str(candidate or "").strip()
            if text and normalize(text) not in _BUTTON_TEXTS:
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

    @property
    def is_fake_select(self) -> bool:
        """组件库的假下拉 / 级联：值靠点浮层选，光写 input 不生效。"""
        return self.widget in ("select", "cascader")

    @property
    def is_date_widget(self) -> bool:
        """日期控件：原生 date/month 直接写 ISO，组件库的要挑日历格。"""
        return self.widget == "date" or self.type in (
            "date",
            "month",
            "datetime-local",
            "time",
            "week",
        )

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
            multiple=bool(data.get("multiple")),
            readonly=bool(data.get("readonly")),
            widget=str(data.get("widget") or ""),
            hints=cls._hints(data),
            selector=str(data.get("selector") or ""),
            fallback=str(data.get("fallback") or ""),
            box=cls._box(data),
            key=str(data.get("key") or ""),
            filled=str(data.get("filled") or ""),
            shown=str(data.get("shown") or ""),
            echo=bool(data.get("echo", True)),
            source=str(data.get("source") or ""),
            confidence=float(data.get("confidence") or 0),
            ok=bool(data.get("ok", True)),
            note=str(data.get("note") or ""),
            inferred=bool(data.get("inferred")),
            group=[int(i) for i in (data.get("group") or [])],
            hidden=bool(data.get("hidden")),
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
            "multiple": self.multiple,
            "readonly": self.readonly,
            "widget": self.widget,
            "hints": self.hints,
            # 定位信息：下次接着填要用，前端也拿它做「跳到该字段」的锚点
            "selector": self.selector,
            "fallback": self.fallback,
            "box": self.box,
            "key": self.key,
            "filled": self.filled,
            "shown": self.shown,
            "echo": self.echo,
            "source": self.source,
            "confidence": round(self.confidence, 2),
            "ok": self.ok,
            "note": self.note,
            "inferred": self.inferred,
            "group": self.group,
            "hidden": self.hidden,
        }


class FormScanner:
    """扫描表单：读页面里的全部控件，并识别需要人处理的拦路虎。"""

    def __init__(self, session: "BossSession") -> None:
        self.session = session

    async def scan(self, page: "Page") -> list[FormField]:
        """扫描所有 frame 里的表单控件，按出现顺序返回。"""

        async def collect(frame: Any) -> list[Any]:
            try:
                return list(await frame.evaluate(_COLLECT_JS) or [])
            except PlaywrightError:
                return []  # 跨域 frame 或已销毁的 frame 直接跳过

        # 逐帧串行要等一串往返；iframe 多的站（广告、埋点）白搭几百毫秒。
        # 各帧互不依赖，并发取回后仍按 frame 顺序拼，序号与串行时完全一致。
        batches = await asyncio.gather(*(collect(frame) for frame in page.frames))
        fields: list[FormField] = []
        for frame_index, raw in enumerate(batches):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                fields.append(FormField.from_raw(item, index=len(fields), frame=frame_index))
        fields = _drop_shadow_uploads(fields)
        for item in fields:
            if _COUNTER_RE.match(item.label.strip()):
                item.label = ""
        return fields

    async def outline(self, page: "Page") -> str:
        """把页面的可访问性树摘成一段文字，给模型当「这张表单长什么样」的底稿。

        ``aria_snapshot(mode="ai")`` 是浏览器自己算出来的可访问名与层级：分段标题、
        必填标记、组件库控件的真实角色（combobox / radiogroup / spinbutton）都在
        里面，比从 DOM 里抠标签可靠。纯文字模型看不到布局，这份大纲补的正是
        「哪一栏属于哪一段」这层信息——两处都叫「开始时间」，一处在教育经历、
        一处在工作经历，光看标签分不出来，看层级就一目了然。

        取不到就返回空串：它给模型锦上添花，不该拖垮扫描，也不该让没装浏览器
        的测试环境炸掉。
        """
        chunks: list[str] = []
        total = 0
        for frame in page.frames:
            try:
                text = str(await frame.locator("body").aria_snapshot(mode="ai") or "").strip()
            except Exception:  # noqa: BLE001 拿不到这一帧的大纲就跳过它
                continue
            if not text:
                continue
            if total + len(text) > _OUTLINE_MAX:
                text = text[: max(0, _OUTLINE_MAX - total)]
            chunks.append(text)
            total += len(text)
            if total >= _OUTLINE_MAX:
                break
        return "\n".join(chunks)

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

    async def meta(self, page: "Page") -> dict[str, str]:
        """读页面标题 / 站点名 / 一级标题，用来判断这是哪家公司、哪个岗位。"""
        for frame in page.frames:
            try:
                raw = await frame.evaluate(_META_JS)
            except PlaywrightError:
                continue
            data = read_page_meta(raw)
            if data["title"] or data["site"] or data["h1"]:
                return data
        return read_page_meta(None)
