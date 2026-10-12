"""薪资解析：把 BOSS 的薪资文案（如「15-25K·14薪」「1-2万」「8千-1.2万」）换算成月薪区间。

解析在程序侧完成，算术不经过大模型：预筛用它过滤薪资不符的岗位，
AI 复核把算好的交集结论放进提示词，只做定性判断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 金额：15 / 15.5 / 1.2万 / 8千
_NUMBER = r"(\d+(?:\.\d+)?)"
# 一段金额 + 可选单位（K / 千 / 万）
_TERM = rf"{_NUMBER}\s*([kK千万])?"

# 「15-25K」「15K-25K」「1-2万」「8千-1.2万」——单位写在尾部时两端共享（「1-2万」= 1万–2万）
_RANGE_RE = re.compile(rf"{_TERM}\s*[-–~至到]\s*{_TERM}")
# 「8千以上」「20K+」
_AT_LEAST_RE = re.compile(rf"{_TERM}\s*(?:以上|\+)")
# 「300-500元/天」「500元/日」
_DAILY_RE = re.compile(rf"{_NUMBER}\s*元?\s*[-–~至到]?\s*{_NUMBER}?\s*元\s*/\s*[天日]")
# 「20-40万/年」「30万年薪」「25万·年」
_YEARLY_RE = re.compile(
    rf"{_TERM}\s*[-–~至到]\s*{_TERM}\s*万\s*/?\s*年"
    rf"|{_NUMBER}\s*万\s*(?:/\s*年|年薪|一年)"
)
# 「15-25K·14薪」里的发薪月数
_BONUS_RE = re.compile(r"(\d+)\s*薪")


def _to_k(value: str, unit: str) -> float:
    """数值 + 单位 → 千元/月（K）。「万」= 10K，「千 / K」= 1K，无单位按 K。"""
    number = float(value)
    return number * 10 if unit == "万" else number


@dataclass(frozen=True)
class SalaryRange:
    """月薪区间（单位 K，即千元/月）。"""

    low: float
    high: float

    @classmethod
    def monthly(cls, low: float, high: float) -> SalaryRange:
        """以月薪 K 构造区间，高低反了自动交换。"""
        return cls(low, high) if low <= high else cls(high, low)

    def intersect(self, other: SalaryRange) -> SalaryRange | None:
        """与另一区间的交集（含端点）；不相交返回 None。"""
        low, high = max(self.low, other.low), min(self.high, other.high)
        return SalaryRange(low, high) if low <= high else None

    def label(self) -> str:
        """给提示词和日志看的文本：15K-25K。"""
        return f"{self.low:g}K-{self.high:g}K"


def parse(text: str) -> SalaryRange | None:
    """薪资文案 → 月薪区间；面议、日薪或认不出的返回 None。

    年薪按 12 个月折算成月薪，「·14薪」只说明年终多发，不影响月薪区间；
    字体加密的薪资（私用区字符）上游已置空，这里拿到的是空串。
    """
    text = (text or "").strip()
    if not text or "面议" in text:
        return None
    if _DAILY_RE.search(text):
        return None

    # 年薪先判：否则「20-40万/年」会被当成 20-40K 的月薪区间
    if "万" in text and _YEARLY_RE.search(text):
        numbers = [float(n) for n in re.findall(_NUMBER, text)[:2]]
        if len(numbers) == 1:
            numbers = [numbers[0], numbers[0]]
        low, high = sorted(numbers)
        return SalaryRange.monthly(low * 10 / 12, high * 10 / 12)

    if match := _RANGE_RE.search(text):
        # 尾部单位共享给前一段：「1-2万」两端都是万；「8千-1.2万」各用各的
        first_unit = match.group(2) or match.group(4) or ""
        low = _to_k(match.group(1), first_unit)
        high = _to_k(match.group(3), match.group(4) or first_unit)
        return SalaryRange.monthly(low, high)

    if match := _AT_LEAST_RE.search(text):
        low = _to_k(match.group(1), match.group(2) or "")
        # 「15K 以上」上限放宽到 1.5 倍，避免把高薪岗错杀
        return SalaryRange.monthly(low, low * 1.5)

    return None


def bonus_months(text: str) -> int:
    """「·14薪」里的发薪月数；没写就是 12。"""
    if match := _BONUS_RE.search(text or ""):
        months = int(match.group(1))
        return months if months >= 12 else 12
    return 12


def monthly_estimate(text: str) -> float | None:
    """薪资文案 → 估算月薪（元 / 月），岗位列表按它排序；认不出返回 None。

    始终走本模块的 ``parse()``，保证与薪资预筛用的是同一套解析（历史上曾有另一套
    简化逻辑，遇到「8千-1.2万」这类混合单位会算错）。月薪取区间中值；日薪 / 时薪
    按每月约 21.75 个工作日、每天 8 小时折算。
    """
    raw = (text or "").strip()
    numbers = [float(n) for n in re.findall(_NUMBER, raw)[:2]]
    average = sum(numbers) / len(numbers) if numbers else None

    # 日薪 / 时薪 parse() 不管（返回 None），这里单独按工作日折算
    if "时" in raw:
        return None if average is None else average * 8 * 21.75
    if "天" in raw or "日" in raw:
        return None if average is None else average * 21.75
    if (rng := parse(raw)) is not None:
        return (rng.low + rng.high) / 2 * 1000
    # parse() 认不出的单值写法（「15K」「1.5万」）在这里兜底
    if average is None:
        return None
    if "万" in raw:
        return average * 10000 / 12 if "年" in raw else average * 10000
    if "k" in raw.lower() or "千" in raw:
        return average * 1000
    return None


def fit_summary(salary: str, expected: SalaryRange | None) -> str:
    """给 AI 提示词的薪资结论：岗位区间与期望区间求交集，只给结论不给原始文案。

    期望为空、岗位薪资解析不了时返回中性说明，让模型不要臆测数字。
    """
    actual = parse(salary)
    if expected is None:
        return "用户没有设定期望薪资范围，薪资只作参考，不据此扣分。"
    if actual is None:
        return (
            f"岗位薪资「{salary or '未知'}」无法解析出月薪区间，"
            "薪资维度按信息不足处理，不要臆测。"
        )
    if (both := actual.intersect(expected)) is None:
        return (
            f"岗位月薪 {actual.label()} 与期望 {expected.label()} 没有交集，"
            "薪资不匹配。薪资不匹配只降低「实际条件」维度，不要因此全盘否定。"
        )
    return (
        f"岗位月薪 {actual.label()} 与期望 {expected.label()} 的交集为 {both.label()}，"
        "薪资范围匹配。"
    )
