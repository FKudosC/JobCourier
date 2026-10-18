"""定制简历：以基础简历为事实母版，按岗位由 AI 重排与改写，程序侧拦截幻觉。

「母版定制」的边界由硬校验保证：生成稿里出现母版没有的数字、日期、邮箱，
或删掉了母版的机构条目，都会被拦下并带着问题清单重试一次——
提示词约束"不要编造"不可靠，逐 token 核对才可靠。
"""

from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from job.models.setting import LlmSettings
from job.paths import USER_DATA_DIR
from job.utils import telemetry
from job.utils.llm import MAX_OUTPUT_TOKENS, OUTPUT_RETRIES

# 母版里出现的这些过程性短语若在生成稿中原样照抄，说明 AI 把说明文字当成了简历内容
ARTIFACT_PHRASES: tuple[str, ...] = (
    "基于原始简历",
    "根据原始简历",
    "针对该岗位",
    "为该岗位定制",
    "定制简历",
    "岗位匹配亮点",
    "匹配亮点",
    "以下是为您",
    "以下是定制",
    "根据您的要求",
    "岗位JD",
    "参考JD",
    "定制说明",
)
# 明显说了一半的行尾：逗号类标点或常用连接词收尾
TAIL_CONNECTORS: tuple[str, ...] = ("，", "、", "-", "—", "从", "把", "与", "和", "的", "了", "在", "对", "向")
# 母版没有、模型自己发明的占位符
PLACEHOLDERS: tuple[str, ...] = ("待填写", "待补充", "待更新", "[年份]", "TBD", "XXX公司", "xx公司")
# 母版中的机构条目关键词：命中这些词的短行按机构名对待，定制稿必须保留
INSTITUTION_WORDS: tuple[str, ...] = (
    "有限公司",
    "有限责任公司",
    "股份公司",
    "集团",
    "研究院",
    "研究所",
    "事务所",
    "大学",
    "学院",
    "学校",
)

# 数字与日期事实：邮箱 / 链接 / 手机号 / 年月 / 其余数字
# 手机号排在年月前面，否则 138-0013-8000 里的 8137-67 会被当成「年月」。
_FACT_TOKEN = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+"  # 邮箱
    r"|https?://\S+"  # 链接
    r"|(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d(?:[-\s]?\d){8}(?!\d)"  # 手机号（可带分隔）
    r"|\d{4}\s*[年.·/-]\s*\d{1,2}\s*月?"  # 年月
    r"|\d+(?:[.,]\d+)*"  # 其余数字（含小数、千分位）
)
# 简历里常见的 OCR 误识：字母 I/l 被写成数字 1（如 I500 → 1500）。
# 只在「前面不是字母数字、后面跟着至少两位数字」时替换，避免动到 IP、ISO 这类词。
_OCR_DIGIT = re.compile(r"(?<![A-Za-z0-9])[Il](?=\d{2,})")
_YEAR_MONTH = re.compile(r"(\d{4})\s*[年.·/-]\s*(\d{1,2})\s*月?")
# 全角数字与常见分隔符归一化，避免「１０」或「1,000」绕过核对
_FULLWIDTH = str.maketrans("０１２３４５６７８９．，", "0123456789.,")


def _norm_fact(raw: str) -> str:
    """把同一事实的不同写法归一成同一个 token。

    日期统一成 ``YYYY-MM``、手机号去掉分隔符、千分位逗号去掉，邮箱与链接转小写。
    否则母版写 ``2024.06``、生成稿写 ``2024年6月`` 就会被当成两个事实。
    """
    token = raw.strip()
    if "@" in token or token.lower().startswith("http"):
        return token.lower().rstrip(".,;)]，。；）")
    if match := _YEAR_MONTH.fullmatch(token):
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return f"{year}-{month:02d}"
    digits = re.sub(r"\D", "", token)
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("1"):
        return digits
    return token.replace(",", "")


def _fact_tokens(text: str) -> set[str]:
    """提取母版的事实 token 集合，供生成稿逐个核对。"""
    normalized = (text or "").translate(_FULLWIDTH)
    normalized = _OCR_DIGIT.sub("1", normalized)
    return {_norm_fact(match.group(0)) for match in _FACT_TOKEN.finditer(normalized)}


class ResumeTailor(BaseModel):
    """用 Pydantic AI 按岗位定制简历；接口地址、API Key 与模型来自配置中心「AI 服务」。"""

    SYSTEM: ClassVar[str] = (
        "你在帮求职者针对特定岗位定制简历。基础简历是事实全集，"
        "你只能重排顺序、调整详略、改写措辞，让与岗位最相关的经历排在前面、写得更具体。\n"
        "硬性规则：\n"
        "1. 禁止编造：不得出现基础简历里没有的项目、公司、职位、学历、技能、数字或日期；\n"
        "2. 禁止删除：基础简历里的每段工作、项目、教育经历都要保留，可以压缩但不能整段消失；\n"
        "3. 禁止把岗位职责写成自己做过的经历；\n"
        "4. 每个数字保持原有归属（公司 / 项目 / 时间），不得跨经历拼接因果；\n"
        "5. 不要输出「岗位匹配亮点」「定制说明」等解释性内容，也不要提及这是定制简历；\n"
        "6. 保留联系方式、邮箱等基本信息；\n"
        "7. 只输出定制后的简历正文本身，不要任何前后缀说明。"
    )
    RETRY: ClassVar[str] = (
        "你上一版定制简历存在以下问题，请修正后重新输出完整的简历正文：\n{issues}\n"
        "仍然只允许在基础简历的事实范围内重排与改写，只输出正文。"
    )
    # 定制结果明显过短的底线：低于母版一半字数视为整段丢失
    MIN_LENGTH_RATIO: ClassVar[float] = 0.5

    resume: str = Field(description="基础简历文本（事实母版）")
    title: str = ""
    company: str = ""
    salary: str = ""
    description: str = ""
    requirement: str = ""
    llm: LlmSettings = Field(default_factory=LlmSettings)
    timeout: float = 120

    @cached_property
    def agent(self) -> Agent[None, str]:
        """定制 Agent：纯文本输出，内容正确性交给 ``validate`` 的程序硬校验。"""
        model = OpenAIChatModel(self.llm.model, provider=self.llm.provider)
        agent = Agent(
            model,
            output_type=str,
            system_prompt=self.SYSTEM,
            retries={"output": OUTPUT_RETRIES},
            model_settings=OpenAIChatModelSettings(
                thinking=False, temperature=0.4, timeout=self.timeout, max_tokens=MAX_OUTPUT_TOKENS * 2
            ),
        )
        return telemetry.attach(agent)

    async def generate(self) -> "TailorResult":
        """生成定制简历：不合规先带着问题清单重试一次，仍有问题则原样带回。"""
        prompt = self._prompt()
        draft = (await self.agent.run(prompt)).output
        issues = validate(self.resume, draft)
        if issues:
            retry = (await self.agent.run(self.RETRY.format(issues="；".join(issues)))).output
            retry_issues = validate(self.resume, retry)
            if not retry_issues or len(retry_issues) < len(issues):
                return TailorResult(content=retry, issues=retry_issues)
            return TailorResult(content=draft, issues=issues)
        return TailorResult(content=draft, issues=[])

    def _prompt(self) -> str:
        parts = [
            f"目标岗位：{self.title} · {self.company} · {self.salary or '薪资未知'}",
            f"岗位详情：\n{self.description}" if self.description else "",
            f"求职方向：{self.requirement}" if self.requirement else "",
            f"基础简历（事实母版，不得超出它的事实范围）：\n{self.resume}",
        ]
        return "\n\n".join(p for p in parts if p)


class TailorResult(BaseModel):
    """定制结果：``issues`` 非空时内容仍可用，但展示时要提示未通过的校验。"""

    content: str = Field(default="", description="定制后的简历正文")
    issues: list[str] = Field(default_factory=list, description="未通过的硬校验问题清单")

    @property
    def ok(self) -> bool:
        return not self.issues


def validate(master: str, draft: str) -> list[str]:
    """对定制稿做完整性硬校验，返回问题清单；空列表表示通过。

    校验的是「母版定制」的底线：没有编造事实、没有丢机构条目、没有把
    说明文字当正文、没有发明占位符、篇幅没有骤减。
    """
    draft = (draft or "").strip()
    master = master or ""
    issues: list[str] = []
    if not draft:
        return ["定制结果为空"]

    if len(draft) < len(master) * ResumeTailor.MIN_LENGTH_RATIO:
        issues.append("定制结果明显偏短，可能整段丢失了经历")

    facts = _fact_tokens(master)
    fabricated = sorted(_fact_tokens(draft) - facts)
    if fabricated:
        head = "、".join(fabricated[:5])
        more = f" 等 {len(fabricated)} 处" if len(fabricated) > 5 else ""
        issues.append(f"出现了母版里没有的数字或事实：{head}{more}")

    if hit := [p for p in ARTIFACT_PHRASES if p in draft]:
        issues.append(f"包含解释性文字：{'、'.join(hit[:3])}")

    if hit := [p for p in PLACEHOLDERS if p in draft]:
        issues.append(f"包含未填写的占位符：{'、'.join(hit[:3])}")

    if missing := _missing_institutions(master, draft):
        issues.append(f"丢失了母版中的机构条目：{'、'.join(missing[:3])}")

    if last := _last_line(draft):
        if len(last) > 12 and any(last.endswith(word) for word in TAIL_CONNECTORS):
            issues.append("结尾疑似被截断，请检查是否完整")
    return issues


# 机构行里的分隔符：机构名到分隔符为止
_SEPARATORS = " |·——/（(，,、:："


def _core_name(line: str) -> str:
    """从一行里取机构名：定位机构关键词，向前回溯到分隔符。"""
    for word in INSTITUTION_WORDS:
        idx = line.find(word)
        if idx < 0:
            continue
        start = idx
        while start > 0 and line[start - 1] not in _SEPARATORS and not line[start - 1].isdigit():
            start -= 1
        name = line[start : idx + len(word)]
        if len(name) >= 4:
            return name
    return ""


def _missing_institutions(master: str, draft: str) -> list[str]:
    """母版里像机构名的条目（公司 / 学校），定制稿里必须还能找到。"""
    normalized = draft.translate(_FULLWIDTH)
    missing: list[str] = []
    for line in master.splitlines():
        name = line.strip().translate(_FULLWIDTH)
        if not 4 <= len(name) <= 60:
            continue
        if not any(word in name for word in INSTITUTION_WORDS):
            continue
        core = _core_name(name)
        if len(core) >= 4 and core not in normalized:
            missing.append(core)
    return list(dict.fromkeys(missing))


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


# 自动生成的定制简历落盘目录
CUSTOM_DIR = USER_DATA_DIR / "resumes" / "custom"


def _custom_path(platform: str, job_uid: str) -> Path:
    """某岗位的自动定制简历文件路径（uid 里的特殊字符替换掉）。"""
    safe = re.sub(r"[^\w.-]", "-", f"{platform}-{job_uid}")
    return CUSTOM_DIR / f"{safe}.txt"


def save_custom_resume(platform: str, job_uid: str, content: str) -> Path | None:
    """把（自动或手动）生成的定制简历存到本地；内容为空不存。"""
    if not (content or "").strip():
        return None
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    path = _custom_path(platform, job_uid)
    path.write_text(content, encoding="utf-8")
    return path


def load_saved_resume(platform: str, job_uid: str) -> str | None:
    """读取已生成的定制简历；没有返回 None。"""
    path = _custom_path(platform, job_uid)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
