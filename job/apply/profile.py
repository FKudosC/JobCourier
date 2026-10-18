"""网申资料：把结构化资料与简历正文合成一份可检索的键值集。

官网表单问的字段（学校、专业、到岗时间……）多半不在简历正文里成对出现，
所以以配置中心填的结构化资料为准；缺的再从简历正文里按常见写法兜底捞一把。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from job.apply import knowledge, safety
from job.models.search import SearchConfigRow
from job.models.setting import ApplyProfileSettings


@dataclass(frozen=True)
class ProfileEntry:
    """一条可用资料：字段键、界面标签、取值、来源与安全分类。"""

    key: str
    label: str
    value: str
    source: str = "资料"
    kind: str = safety.FACT
    # 这条值是怎么来的（推算依据之类）；没有就是空串。对照表上要能看见「这是推的」。
    note: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "source": self.source,
            "kind": self.kind,
            "note": self.note,
        }


# 简历正文里常见写法的兜底：手机号、邮箱、链接。手机号允许写成
# 「138-0013-8000」这种带分隔符的样子，捞出来统一去掉分隔符。
_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)[\s\-]?(\d{4})[\s\-]?(\d{4})(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL = re.compile(r"https?://\S+")
# 「姓名：张三」这类显式写法；也认第一行的 2–4 个汉字
_NAMED = re.compile(r"(?:姓\s*名|名\s*字)\s*[:：]?\s*([\u4e00-\u9fa5]{2,4})")
_CJK_NAME = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")


def resume_facts(resume: str) -> dict[str, str]:
    """从简历正文里捞出手机号、邮箱、姓名、链接；捞不到的不出现在结果里。"""
    text = resume or ""
    facts: dict[str, str] = {}
    if phone := _PHONE.search(text):
        facts["phone"] = "".join(phone.groups())
    if email := _EMAIL.search(text):
        facts["email"] = email.group(0)
    if name := _NAMED.search(text):
        facts["name"] = name.group(1)
    else:
        for line in (ln.strip() for ln in text.splitlines()):
            if not line:
                continue
            # 第一行往往就是姓名，但「个人简历」这类标题要排除
            if _CJK_NAME.match(line) and "简历" not in line:
                facts["name"] = line
            break
    urls = _URL.findall(text)
    if urls:
        facts["portfolio"] = urls[0].rstrip("）),，。")
        for url in urls:
            if "github" in url.lower():
                facts["github"] = url.rstrip("）),，。")
                break
    return facts


class ApplyProfile:
    """本次网申可用的全部资料：结构化字段优先，简历兜底。"""

    def __init__(self, entries: list[ProfileEntry], resume: str = "") -> None:
        self.entries = entries
        self.resume = resume
        self._by_key = {entry.key: entry for entry in entries}
        # 同义键也指到同一条：库里叫 height、模型回过来 stature，都要能查到。
        self._by_alias = {
            alias: entry
            for entry in entries
            for alias in knowledge.KEY_ALIASES.get(entry.key, ())
            if alias not in self._by_key
        }

    @classmethod
    def load(cls) -> "ApplyProfile":
        """读取配置中心的网申资料 + 求职配置里的简历正文。"""
        settings = ApplyProfileSettings.load()
        resume = str(SearchConfigRow.load().get("resume_text") or "")
        return cls.from_sources(settings, resume, learned=learned_values())

    @classmethod
    def from_sources(
        cls,
        settings: ApplyProfileSettings,
        resume: str = "",
        *,
        learned: Mapping[str, str] | None = None,
    ) -> "ApplyProfile":
        """组装资料；结构化字段为空时用简历里捞到的事实补上。

        四层兜底，越靠后越「轻信」：结构化字段 → 简历正文 → 按确定规则推算 →
        在本机别的网站上手工填过、被记下来的那条（``learned`` 由调用方从库里取；
        传 ``None`` 表示不查库，测试与纯函数路径用得上）。
        """
        facts = resume_facts(resume)
        entries: list[ProfileEntry] = []
        for key, label in ApplyProfileSettings.LABELS.items():
            value = str(getattr(settings, key, "") or "").strip()
            source = "资料"
            if not value and (fallback := facts.get(key)):
                value, source = fallback, "简历"
            if value:
                entries.append(
                    ProfileEntry(
                        key=key,
                        label=label,
                        value=value,
                        source=source,
                        kind=safety.kind(key, source),
                    )
                )
        entries.extend(_derived(entries))
        entries.extend(_remembered(entries, learned))
        return cls(entries, resume=resume)

    def values(self) -> dict[str, str]:
        """字段键 → 取值。"""
        return {entry.key: entry.value for entry in self.entries}

    def get(self, key: str) -> str:
        entry = self._by_key.get(key)
        if entry is None:
            entry = self._by_alias.get(key)
        return entry.value if entry else ""

    def entry(self, key: str) -> ProfileEntry | None:
        return self._by_key.get(key) or self._by_alias.get(key)

    def labels(self) -> dict[str, str]:
        """字段键 → 界面标签。"""
        return {entry.key: entry.label for entry in self.entries}

    def key_of_label(self, text: str) -> str:
        """按界面标签反查字段键（归一化后比对）；认不出返回空串。

        用户自己补的字段没有关键词规则可命中，只能靠标签对上号；「姓名 / 姓名*」
        这类写法差异交给归一化处理。
        """
        target = normalize_label(text)
        if not target:
            return ""
        for entry in self.entries:
            if normalize_label(entry.label) == target:
                return entry.key
        return ""

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)


# 标签比对前先去掉空白与标点：官网写「姓名（必填）」、资料里写「姓名」，得算同一个。
_LABEL_NOISE = re.compile(r"[\s*＊·:：,，。.、()（）\[\]【】<>《》\"'’“”/\\|!！?？\-—_]+")


def normalize_label(text: str) -> str:
    """标签归一化：去空白与标点、转小写，只用来比对，不拿来显示。"""
    return _LABEL_NOISE.sub("", str(text or "").strip()).lower()


def _derived(entries: list[ProfileEntry]) -> list[ProfileEntry]:
    """按确定规则推算出来的字段（本科 → 学士；080904K → 计算机类 / 工学）。"""
    filled = {entry.key: entry.value for entry in entries}
    out: list[ProfileEntry] = []
    for key, value, basis in knowledge.derive(filled):
        if filled.get(key):
            continue
        filled[key] = value
        out.append(
            ProfileEntry(
                key=key,
                label=ApplyProfileSettings.LABELS.get(key, key),
                value=value,
                source="推算",
                kind=safety.DERIVED,
                note=basis,
            )
        )
    return out


def _remembered(
    entries: list[ProfileEntry], learned: Mapping[str, str] | None
) -> list[ProfileEntry]:
    """在别的网站上手工填过、被记下来的那条：库里没有这个键时才补上。

    只补空档——配置中心里写着的、简历里捞出来的，都比它可信。唯一例外是**推算**
    出来的那条：那是机器按已有资料猜的，用户后来在官网页面上把它改对了、被记下来，
    就该拿记下来的这条换掉它，否则推错了永远改不回来。换的时候就地替换，别在
    ``entries`` 里留成两条。
    """
    if not learned:
        return []
    out: list[ProfileEntry] = []
    for key, value in learned.items():
        text = str(value or "").strip()
        if not text:
            continue
        hit = next((pos for pos, entry in enumerate(entries) if entry.key == key), -1)
        if hit >= 0:
            old = entries[hit]
            if old.source != "推算":
                continue
            entries[hit] = ProfileEntry(
                key=key,
                label=old.label,
                value=text,
                source="记忆",
                kind=old.kind,
                note="在别的网站手工填过，本机记下来了",
            )
            continue
        out.append(
            ProfileEntry(
                key=key,
                label=ApplyProfileSettings.LABELS.get(key, key),
                value=text,
                source="记忆",
                kind=safety.kind(key),
                note="在别的网站手工填过，本机记下来了",
            )
        )
    return out


def learned_values() -> dict[str, str]:
    """本机记下来的「人工补过的新字段」；库还没建好时返回空字典。

    键先归到规范写法：官网那栏叫「有无子女」还是 ``has_children``，都得落到同一个
    字段上，否则同一个东西在库里存成两条、界面上也就跟着重复。
    """
    from job.models.application import ApplyLearnedRow

    try:
        rows = ApplyLearnedRow.values()
    except Exception:  # noqa: BLE001 记忆是加分项，读不到不该拖垮资料组装
        return {}
    return {knowledge.canonical_key(key) or key: value for key, value in rows.items()}
