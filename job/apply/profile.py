"""网申资料：把结构化资料与简历正文合成一份可检索的键值集。

官网表单问的字段（学校、专业、到岗时间……）多半不在简历正文里成对出现，
所以以配置中心填的结构化资料为准；缺的再从简历正文里按常见写法兜底捞一把。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from job.models.search import SearchConfigRow
from job.models.setting import ApplyProfileSettings


@dataclass(frozen=True)
class ProfileEntry:
    """一条可用资料：字段键、界面标签、取值与来源。"""

    key: str
    label: str
    value: str
    source: str = "资料"

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "label": self.label, "value": self.value, "source": self.source}


# 简历正文里常见写法的兜底：手机号、邮箱、链接
_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
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
        facts["phone"] = phone.group(1)
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

    @classmethod
    def load(cls) -> "ApplyProfile":
        """读取配置中心的网申资料 + 求职配置里的简历正文。"""
        settings = ApplyProfileSettings.load()
        resume = str(SearchConfigRow.load().get("resume_text") or "")
        return cls.from_sources(settings, resume)

    @classmethod
    def from_sources(cls, settings: ApplyProfileSettings, resume: str = "") -> "ApplyProfile":
        """组装资料；结构化字段为空时用简历里捞到的事实补上。"""
        facts = resume_facts(resume)
        entries: list[ProfileEntry] = []
        for key, label in ApplyProfileSettings.LABELS.items():
            value = str(getattr(settings, key, "") or "").strip()
            source = "资料"
            if not value and (fallback := facts.get(key)):
                value, source = fallback, "简历"
            if value:
                entries.append(ProfileEntry(key=key, label=label, value=value, source=source))
        for key, raw in settings.extra.items():
            if (value := str(raw or "").strip()):
                entries.append(ProfileEntry(key=key, label=key, value=value, source="自定义"))
        return cls(entries, resume=resume)

    def values(self) -> dict[str, str]:
        """字段键 → 取值。"""
        return {entry.key: entry.value for entry in self.entries}

    def get(self, key: str) -> str:
        entry = self._by_key.get(key)
        return entry.value if entry else ""

    def entry(self, key: str) -> ProfileEntry | None:
        return self._by_key.get(key)

    def labels(self) -> dict[str, str]:
        """字段键 → 界面标签。"""
        return {entry.key: entry.label for entry in self.entries}

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)
