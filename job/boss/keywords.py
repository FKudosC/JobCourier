"""岗位关键词研判：根据简历由大模型生成适合招聘网站搜索框的多个关键词。

产出的是「搜索词」而不是岗位名：要照顾招聘网站的搜索习惯（短、带技术栈、
常见的岗位叫法），让每个关键词都能捞到一批合适的岗位。
"""

from __future__ import annotations

from functools import cached_property
from typing import ClassVar

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from job.models.setting import LlmSettings
from job.utils import telemetry
from job.utils.llm import MAX_OUTPUT_TOKENS, extract_json


class KeywordAgent(BaseModel):
    """用 Pydantic AI 根据简历生成岗位搜索关键词。"""

    SYSTEM: ClassVar[str] = (
        "你是求职助手，根据简历研判求职者适配的岗位方向。"
        "你只输出一个 JSON 对象，不要 markdown 代码块标记，不要解释文字："
        '{"keywords": ["关键词1", "关键词2", ...]}'
    )
    PROMPT: ClassVar[str] = (
        "根据简历研判求职者适配的岗位方向，生成 {count} 个适合在招聘网站搜索框使用的"
        "岗位搜索关键词，按与简历技能的相关程度从高到低排列：\n"
        "1. 第一梯队（前 6 个）：与简历经历最直接对口的岗位，如简历做过什么就投什么；\n"
        "2. 第二梯队（中间 6 个）：技能高度可迁移的相邻岗位，如同一技术栈的上下游岗位；\n"
        "3. 第三梯队（最后 4 个）：简历技能可以支撑的相关岗位，与技能相关但方向有所延伸；\n"
        "4. 所有关键词都必须与简历中的技能相关，不可完全无关；每个不超过 12 个字；\n"
        "5. 用招聘网站上真实存在的岗位叫法，不要生造词，不要写公司名、地点或薪资。"
    )
    # 生成的关键词数量
    COUNT: ClassVar[int] = 16
    # 解析失败的纠错重试次数
    PARSE_RETRIES: ClassVar[int] = 3

    resume: str = Field(description="简历文本")
    requirement: str = Field(default="", description="求职方向（AI 岗位筛选里的目标要求，可空）")
    llm: LlmSettings = Field(default_factory=LlmSettings)
    timeout: float = 60

    @cached_property
    def agent(self) -> Agent[None, str]:
        """关键词 Agent：纯文本输出 + 宽容解析（与岗位评分同套容错）。"""
        model = OpenAIChatModel(self.llm.model, provider=self.llm.provider)
        agent = Agent(
            model,
            output_type=str,
            system_prompt=self.SYSTEM,
            model_settings=OpenAIChatModelSettings(
                thinking=False, temperature=0.3, timeout=self.timeout, max_tokens=MAX_OUTPUT_TOKENS
            ),
        )
        return telemetry.attach(agent)

    async def generate(self) -> list[str]:
        """生成关键词列表；解析不了带错误纠错重试，仍失败抛 ``AgentRunError``。"""
        prompt = self._prompt()
        last: Exception | None = None
        for _ in range(self.PARSE_RETRIES):
            text = (await self.agent.run(prompt)).output
            try:
                return self._parse(text)
            except ValueError as exc:
                last = exc
                prompt = (
                    f"{self._prompt()}\n\n"
                    f"你上一次的回复无法解析（{str(exc)[:200]}）。"
                    "请只输出一个 JSON 对象：{\"keywords\": [\"…\", …]}，"
                    "不要 markdown 代码块标记，不要解释文字。"
                )
        raise AgentRunError(f"重试 {self.PARSE_RETRIES} 次输出仍不合规：{last}")

    def _prompt(self) -> str:
        parts = [self.PROMPT.format(count=self.COUNT)]
        if self.requirement:
            parts.append(f"求职方向要求（优先围绕它生成）：\n{self.requirement}")
        parts.append(f"简历：\n{self.resume}")
        return "\n\n".join(parts)

    def _parse(self, text: str) -> list[str]:
        data = extract_json(text)
        if not isinstance(data, dict) or not isinstance(data.get("keywords"), list):
            raise ValueError("回复里没有 keywords 列表")
        cleaned: list[str] = []
        for raw in data["keywords"]:
            word = str(raw).strip().strip("「」\"'")
            if 1 < len(word) <= 20 and word not in cleaned:
                cleaned.append(word)
        if not cleaned:
            raise ValueError("keywords 列表为空或全无效")
        return cleaned[: self.COUNT]
