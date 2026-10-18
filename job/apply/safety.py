"""网申安全规则：哪些资料能填、哪些能出本机、填完怎么复核。

* ``FACT`` 已核验事实 —— 用户在「网申资料」里亲手填的结构化字段，拿来即用；
* ``DERIVED`` 有证据的改写 —— 从简历正文里捞出来的（手机号、邮箱、链接），
  用的时候记着来源，别当成用户确认过的事实；
* ``SENSITIVE`` 敏感事实 —— 身份证、银行卡、社保公积金……只在本机用，
  绝不进大模型的提示词（界面照常明文，方便用户核对）；
* ``OPTIONAL`` 可选未知 —— 用户没填就留空，宁可空着也不让模型硬凑；
* ``DECLARATION`` 声明 —— 自我介绍、职业规划这类用户自己的话，只照抄不生成；
* ``IRREVERSIBLE`` 不可逆操作 —— 提交按钮，永远留给人点，程序碰都不碰。

两条硬规矩落在这里：**数据最小化**（外发前先过 :func:`public_entries`）与
**重复模块按结束日期倒序**（多段实习、多段项目，最近的经历填在最前面）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable

from job.models.setting import ApplyProfileSettings

if TYPE_CHECKING:
    from job.apply.profile import ProfileEntry
    from job.apply.scanner import FormField

# 分类名。界面按它显示小标签，填充逻辑按它决定「填不填、怎么填」。
FACT = "事实"
DERIVED = "改写"
SENSITIVE = "敏感"
OPTIONAL = "选填"
DECLARATION = "声明"
IRREVERSIBLE = "不可逆"

# 敏感事实：和配置中心那份「只在本机用」的名单保持一致，改一处即可
SENSITIVE_KEYS = frozenset(ApplyProfileSettings.PRIVATE)

# 声明：用户自己的话，只照抄，不让模型改写或生成
DECLARATION_KEYS = frozenset(
    {"self_intro", "career_plan", "strengths", "leave_reason"}
)

# 选填：不是人人都有答案，用户没填就留空，别让模型硬凑一个
OPTIONAL_KEYS = frozenset(
    {
        "height",
        "weight",
        "blood_type",
        "health",
        "religion",
        "marital",
        "hobby",
        "qq",
        "dingtalk",
        "referral_code",
        "relative_in_company",
        "noncompete",
        "archive_location",
        "driver_license",
        "patent",
        "publications",
        "software_copyright",
    }
)

# 不可逆的控件类型：提交 / 重置 / 图片按钮，点了就撤不回来
IRREVERSIBLE_TYPES = frozenset({"submit", "reset", "image", "button"})

# 一段经历里的日期写法：2023.06 / 2023-06 / 2023/6/30 / 2023年6月
_DATE_TOKEN = re.compile(r"(\d{4})\s*[年./\-]\s*(\d{1,2})?(?:\s*[月./\-]\s*(\d{1,2}))?")
# 归一化用的标点：和扫描器的 normalize 口径一致（这里另写一份，是为了不把扫描器
# 那条重依赖链引进来——safety 到处都在用，得保持轻）
_PUNCT = re.compile(r"[\s*＊·・:：,，.。…/／\\|｜()（）\[\]【】<>《》\"'`~!！?？;；\-—_+]+")

# 官网栏位没填时自带的提示文字：下拉的「请选择」、输入框的「请输入」、按钮的
# 「点击添加」……回读页面时它们会被当成「用户刚填的值」捞出来，学进资料库就成了
# 一堆垃圾。下面这份名单一律判成「其实没填」。
_PLACEHOLDER_VALUES = frozenset(
    {
        "请选择",
        "请选择…",
        "请选择...",
        "请选择日期",
        "请选择时间",
        "请选择地区",
        "请选择省份",
        "请选择城市",
        "请输入",
        "请输入…",
        "请输入内容",
        "请填写",
        "请填写内容",
        "请选择（必填）",
        "点击添加",
        "点击上传",
        "点击选择",
        "请添加",
        "选择文件",
        "上传文件",
        "未选择文件",
        "暂无",
        "暂无数据",
        "--",
        "---",
        "—",
        "必填",
        "选填",
        "please select",
        "please input",
        "please enter",
        "select",
        "search",
        "choose",
        "clicktoadd",
    }
)

# 归一化后落在这些词上的值也不是一条资料：它们是按钮 / 声明栏 / 区块标题的文字，
# 被采集脚本连人带栏一起扫了进来（「确认投递」「亲属声明」「人员类型（校招）」）。
_NON_FIELD_TEXTS = frozenset(
    {
        "确认投递",
        "确认提交",
        "确认",
        "提交",
        "保存",
        "下一步",
        "上一步",
        "亲属声明",
        "人员类型校招",
        "人员类型社招",
        "点击添加",
        "添加",
        "删除",
        "编辑",
        "修改",
        "同意",
        "不同意",
        "展开",
        "收起",
        "查看更多",
    }
)

# 重复块里的从属栏位名。官网的「语言能力」「教育经历」「家庭成员」这种块，一块里
# 就并排放着「类型 / 等级 / 称谓 / 开始时间」——它们脱离了所在的那一块根本指不明
# 「哪一条个人资料」，学进资料库只会多出一个含义不明的键：别家官网随便哪一栏叫
# 「类型」都会被它填上。这类从属栏位一律不进资料库，它所在的那条主资料（语言能力、
# 工作经历……）本来就有自己的键。
_VAGUE_KEYS = frozenset(
    {
        "类型",
        "等级",
        "名称",
        "称谓",
        "关系",
        "序号",
        "备注",
        "说明",
        "描述",
        "状态",
        "操作",
        "时间",
        "日期",
        "结果",
        "内容",
        "情况",
        "数量",
        "单位",
        "详情",
        "其他",
        "开始时间",
        "结束时间",
        "获得时间",
        "起始时间",
        "起止时间",
        "开始日期",
        "结束日期",
        "获得日期",
        "是否",
        "有无",
    }
)

# 「同意调剂的意向公司1」「家庭成员2」：块里的第几个实例，是站点自己的编号。
# 这种键换个网站就对不上，学下来只是给资料库添一条死数据。
_NUMBERED_KEY = re.compile(r"\d+$")


def _plain(text: str) -> str:
    """去标点空白、统一小写；只用来和上面两份名单比对。"""
    return _PUNCT.sub("", (text or "").strip().lower())


def unfilled(field: "FormField", value: str) -> bool:
    """回读到的值是不是「其实没填」：占位提示、按钮字，或下拉把选项全堆在一起。

    官网没填的下拉，回读拿到的是占位符（「请选择」）或整份选项文字（「有 / 无」），
    都不是用户给的答案。学习那一关据此把它当没填，才不会记成一条资料。
    """
    text = str(value or "").strip()
    if not text:
        return True
    low = text.lower()
    if low in _PLACEHOLDER_VALUES:
        return True
    placeholder = str(getattr(field, "placeholder", "") or "").strip().lower()
    if placeholder and low == placeholder:
        return True
    if _plain(text) in _NON_FIELD_TEXTS:
        return True
    options = [str(o).strip() for o in (getattr(field, "options", None) or []) if str(o).strip()]
    if options and _plain(text) == _plain("".join(options)):
        return True
    return False


def garbled(text: str) -> bool:
    """这串字是不是「编码读错」的产物（UTF-8 被当成 GBK 解出来的那种乱码）。

    官网响应头没带 charset 时，浏览器会按 GBK 去猜，中文标签就变成「鎵嬫満鍙风爜」
    这样的字——它看着也是汉字，关键词表和占位符名单都拦不住，学进资料库只是污染。
    判法是把这串字按 GBK 编回去、再按 UTF-8 解一次：真·中文标签在第一步就编不出来
    （或第二步解不出来），乱码却会干净地还原成人话。太短的串容易误判，放过。
    """
    text = str(text or "").strip()
    if len(text) < 4:
        return False
    try:
        restored = text.encode("gbk").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False
    return bool(restored.strip()) and restored != text


def vague(key: str) -> bool:
    """这个键是不是「指不明一条资料」：重复块里的从属栏位名，或站点自己的编号。

    只拿**归一化之后的键**来判（调用方先过 :func:`job.apply.knowledge.canonical_key`），
    这样「证件类型」归到 ``id_type``、「证书名称」归到 ``certificates`` 之后都不会被
    误伤，真正落单的「类型 / 等级」才判掉。
    """
    text = _plain(key)
    if not text:
        return True
    if text in _VAGUE_KEYS:
        return True
    return bool(_NUMBERED_KEY.search(text))

# 「至今」这类没有结束日期的写法，按最靠前处理
_PRESENT = ("至今", "现在", "目前", "present", "now", "current")
# 多段经历之间的分隔：空行，或一行横线
_ENTRY_SEP = re.compile(r"\n\s*\n|^[ \t]*-{3,}[ \t]*$", re.MULTILINE)
# 没有日期时给个垫底值，保证排序稳定
_NO_DATE = (0, 0, 0)
_PRESENT_DATE = (9999, 12, 31)


def kind(key: str, source: str = "") -> str:
    """字段键属于哪一类；``source`` 传「简历」时，捞出来的算「有证据的改写」。"""
    if key in SENSITIVE_KEYS:
        return SENSITIVE
    if key in DECLARATION_KEYS:
        return DECLARATION
    if key in OPTIONAL_KEYS:
        return OPTIONAL
    if source == "简历":
        return DERIVED
    return FACT


def is_sensitive(key: str) -> bool:
    """这个键是不是敏感事实（身份证、银行卡……）。"""
    return key in SENSITIVE_KEYS


def is_irreversible(field: "FormField") -> bool:
    """是不是不可逆的控件（提交按钮）。提交永远留给人点，程序碰都不碰。"""
    return str(getattr(field, "type", "") or "").lower() in IRREVERSIBLE_TYPES


def public_entries(entries: Iterable["ProfileEntry"]) -> list["ProfileEntry"]:
    """外发前的最小化：敏感事实只在本机用，绝不进大模型的提示词。

    身份证、银行卡这些栏靠关键词规则在本地就能对上；万一规则没命中，也只是
    留空请用户手动填，不会把号码发给第三方模型。
    """
    return [entry for entry in entries if not is_sensitive(entry.key)]


# 长号码：身份证号（18 位）、银行卡号（16～19 位）。简历正文要整段交给模型推理，
# 外发前把这类号码抹掉——它们的栏位本来就只认资料里的原值，推不出来也不该被编
# 出来。门槛定在 15 位是为了放过手机号 / 座机：那是要填进表单的联系方式。
_LONG_NUMBERS = re.compile(r"\d[\d\s\-]{13,}\d")


def redact(text: str) -> str:
    """外发前把正文里的长号码抹成掩码；其余原样保留。"""
    return _LONG_NUMBERS.sub("[已掩码]", text or "")


def end_date_of(text: str) -> tuple[int, int, int]:
    """取一段经历里最后一个日期当结束日期；「至今」按最靠前算。"""
    lowered = (text or "").lower()
    if any(word in lowered for word in _PRESENT):
        return _PRESENT_DATE
    matches = list(_DATE_TOKEN.finditer(text or ""))
    if not matches:
        return _NO_DATE
    last = matches[-1]
    return (int(last.group(1)), int(last.group(2) or 0), int(last.group(3) or 0))


def split_entries(value: str) -> list[str]:
    """把一段多条目文本按空行（或一行横线）拆开；只有一段时返回单元素列表。"""
    parts = [part.strip() for part in _ENTRY_SEP.split(value or "") if part.strip()]
    return parts or ([value] if value else [])


def order_by_end_date(values: list[str]) -> list[str]:
    """多段经历按结束日期倒序：最近的排最前，没有日期的排最后。"""
    return sorted(values, key=end_date_of, reverse=True)
