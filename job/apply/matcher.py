"""字段匹配：把「表单上的字段」对应到「我有哪些资料」。

三档优先级，越靠前越可信：

1. **历史映射** —— 同一个官网的表单上次填过，直接沿用（confidence 0.95）；
2. **关键词规则** —— 字段文字命中中英关键词表（confidence 0.9）；
3. **AI 兜底** —— 规则没命中的字段交给人模型判断，未开通 AI 服务就静默跳过。

一条硬规矩：只认「资料里真有值」的键，绝不凭空编造。猜不准的字段留空，
由用户在字段对照表里手动补——填错比留空麻烦得多。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from job.apply.profile import ApplyProfile
from job.apply.scanner import FormField, normalize
from job.models.application import ApplyMappingRow
from job.models.setting import LlmSettings
from job.utils import telemetry
from job.utils.llm import MAX_OUTPUT_TOKENS, OUTPUT_RETRIES, extract_json

# 字段键 → 关键词；命中任意一个即算对应上。英文关键词统一小写（比对前会归一化）。
RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("graduate_year", ("毕业年份", "毕业时间", "毕业年月", "graduation", "graduationyear")),
    ("school", ("毕业学校", "毕业院校", "学校", "院校", "母校", "school", "university", "college")),
    ("major", ("所学专业", "专业名称", "专业", "major", "specialty")),
    ("degree", ("最高学历", "学历", "学位", "degree", "education")),
    ("name", ("姓名", "名字", "真实姓名", "fullname", "yourname", "name")),
    ("gender", ("性别", "gender", "sex")),
    ("phone", ("手机号码", "手机号", "手机", "联系电话", "电话", "联系方式", "mobile", "phone", "tel")),
    ("email", ("电子邮箱", "邮箱", "电子邮件", "email", "mail")),
    ("wechat", ("微信号", "微信", "wechat", "weixin")),
    ("birth", ("出生年月", "出生日期", "生日", "birth", "birthday")),
    ("city", ("现居城市", "所在城市", "居住城市", "现居地", "城市", "city")),
    ("address", ("通讯地址", "联系地址", "详细地址", "家庭住址", "地址", "address")),
    ("expected_salary", ("期望薪资", "期望月薪", "期望工资", "薪资要求", "expectedsalary", "salaryexpectation")),
    ("available", ("到岗时间", "可到岗", "入职时间", "最快到岗", "available", "onboard")),
    ("github", ("github", "gitee", "码云", "代码仓库")),
    ("portfolio", ("个人主页", "个人网站", "作品集", "博客", "portfolio", "website", "homepage", "blog")),
    ("self_intro", ("自我介绍", "自我评价", "个人简介", "个人介绍", "selfintroduction", "introduction", "aboutme")),
)

# AI 兜底只处理这些控件类型：下拉与单选/多选靠选项文字判断，问模型反而更糊
_AI_TYPES = frozenset({"text", "textarea", "email", "tel", "url", "search", "number", "select-one", "select"})


@dataclass
class FieldMapping:
    """一个字段的匹配结果。"""

    key: str = ""
    value: str = ""
    source: str = ""
    confidence: float = 0.0


def _align_option(value: str, options: list[str]) -> str:
    """在表单选项里挑一个最贴近资料的：完全相等 > 互相包含 > 空串。"""
    target = normalize(value)
    if not target:
        return ""
    best = ""
    best_score = 0
    for option in options:
        text = normalize(option)
        if not text:
            continue
        if text == target:
            return option
        if target in text or text in target:
            score = min(len(text), len(target))
            if score > best_score:
                best, best_score = option, score
    return best


class FieldMatcher:
    """规则优先、AI 兜底，把资料对应到表单字段。"""

    SYSTEM: ClassVar[str] = (
        "你在帮求职者把个人资料填进企业官网的网申表单。\n"
        "我会给出「可用资料」（键 + 中文标签 + 取值）和「还没识别的表单字段」（序号 + 文字）。\n"
        "请判断每个字段该填哪一条资料，只输出一个 JSON 对象：键是字段序号的字符串，值是资料的键。\n"
        "规则：\n"
        "1. 只使用我给出的资料键，不要发明新的键；\n"
        "2. 判断不了的字段不要出现在 JSON 里，留空比填错好；\n"
        "3. 只输出 JSON，不要任何解释。\n"
        '例如：{"3": "school", "5": "major"}'
    )

    def __init__(
        self,
        profile: ApplyProfile,
        *,
        llm: LlmSettings | None = None,
        domain: str = "",
        timeout: float = 60,
    ) -> None:
        self.profile = profile
        self.llm = llm or LlmSettings.load()
        self.domain = domain
        self.timeout = timeout

    async def match(self, fields: list[FormField]) -> list[FormField]:
        """给每个字段填上 key / value / source / confidence；原地修改并返回。"""
        history = ApplyMappingRow.lookup(self.domain, [f.signature() for f in fields])
        pending: list[FormField] = []
        for field in fields:
            if self._apply_history(field, history) or self._apply_rule(field):
                continue
            if field.type in _AI_TYPES and not field.is_file:
                pending.append(field)
        if pending:
            await self._apply_ai(pending)
        return fields

    def remember(self, fields: list[FormField]) -> int:
        """把本次确认过的映射记到域名下，下次同一个官网直接沿用。"""
        items = [
            {"signature": f.signature(), "label": f.label, "value": f.filled, "source": f.source}
            for f in fields
            if f.key and f.filled
        ]
        return ApplyMappingRow.remember(self.domain, items)

    # --- 三档匹配 ---

    def _apply_history(self, field: FormField, history: dict[str, ApplyMappingRow]) -> bool:
        row = history.get(field.signature())
        if row is None or not row.value:
            return False
        self._assign(field, key=row.source or "manual", value=row.value, source="沿用", confidence=0.95)
        return True

    def _apply_rule(self, field: FormField) -> bool:
        key = self._rule_key(field)
        if not key:
            return False
        value = self.profile.get(key)
        if not value:
            return False
        if field.tag == "select" and field.options:
            option = _align_option(value, field.options)
            if not option:
                field.key = key
                field.ok = False
                field.note = "表单选项里没有匹配项，请手动选择"
                return True
            self._assign(field, key=key, value=option, source="资料", confidence=0.9)
            return True
        if field.type in ("radio", "checkbox"):
            own = normalize(field.label or field.value)
            if own and (normalize(value) in own or own in normalize(value)):
                self._assign(field, key=key, value=field.label or value, source="资料", confidence=0.85)
            return True
        self._assign(field, key=key, value=value, source="资料", confidence=0.9)
        return True

    def _rule_key(self, field: FormField) -> str:
        """按关键词命中挑一个键；同时命中多个时取关键词更长的那个。"""
        text = field.normalized
        if not text:
            return ""
        best_key = ""
        best_score = 0
        for key, keywords in RULES:
            if not self.profile.get(key):
                continue
            for keyword in keywords:
                if keyword in text and len(keyword) > best_score:
                    best_key, best_score = key, len(keyword)
        return best_key

    async def _apply_ai(self, fields: list[FormField]) -> None:
        """把规则没命中的字段交给模型判断；未配置或调用失败都静默跳过。"""
        if not self.llm.ready or not self.profile:
            return
        try:
            raw = await self._ask(fields)
        except Exception:  # noqa: BLE001 AI 只是兜底，失败不该拖垮整次扫描
            return
        by_index = {str(f.index): f for f in fields}
        for index, key in raw.items():
            field = by_index.get(str(index))
            if field is None or not isinstance(key, str):
                continue
            value = self.profile.get(key)
            if not value:
                continue
            if field.tag == "select" and field.options:
                option = _align_option(value, field.options)
                if not option:
                    continue
                self._assign(field, key=key, value=option, source="AI", confidence=0.6)
            else:
                self._assign(field, key=key, value=value, source="AI", confidence=0.6)

    async def _ask(self, fields: list[FormField]) -> dict[str, str]:
        """构造提示词并解析模型返回的 ``{字段序号: 资料键}``。"""
        model = OpenAIChatModel(self.llm.model, provider=self.llm.provider)
        agent = telemetry.attach(
            Agent(
                model,
                output_type=str,
                system_prompt=self.SYSTEM,
                retries={"output": OUTPUT_RETRIES},
                model_settings=OpenAIChatModelSettings(
                    thinking=False, temperature=0, timeout=self.timeout, max_tokens=MAX_OUTPUT_TOKENS
                ),
            )
        )
        result = (await agent.run(self._prompt(fields))).output
        data = extract_json(result)
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    def _prompt(self, fields: list[FormField]) -> str:
        entries = [e for e in self.profile.entries if e.key in {k for k, _ in RULES}]
        lines = [f"- {e.key}（{e.label}）：{e.value}" for e in entries]
        rows = [
            f"- 序号 {f.index}：{f.label or f.name or f.placeholder}（类型 {f.type}"
            + (f"，选项：{'、'.join(f.options)}" if f.options else "")
            + ")"
            for f in fields
        ]
        return "\n".join(
            [
                "可用资料：",
                *lines,
                "",
                "还没识别的表单字段：",
                *rows,
                "",
                "请输出 JSON：字段序号 → 资料键。",
            ]
        )

    @staticmethod
    def _assign(field: FormField, *, key: str, value: str, source: str, confidence: float) -> None:
        field.key = key
        field.filled = value
        field.source = source
        field.confidence = confidence
        field.ok = True
        field.note = ""
