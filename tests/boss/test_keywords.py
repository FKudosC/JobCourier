"""关键词研判：JSON 解析容错与关键词清洗。"""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from job.boss.keywords import KeywordAgent
from job.models.setting import LlmSettings

LLM = LlmSettings(api_key="k", model="deepseek-v4-pro")


def _agent_answering(text: str) -> tuple[KeywordAgent, object]:
    agent = KeywordAgent(resume="3 年 Python，做过 RAG", llm=LLM)

    def answer(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    return agent, agent.agent.override(model=FunctionModel(answer))


def test_parse_fenced_json():
    payload = json.dumps(
        {"keywords": ["AI 应用开发", "Agent 工程师", "LLM 工程师", "Python 后端"]},
        ensure_ascii=False,
    )
    agent, override = _agent_answering(f"```json\n{payload}\n```")
    with override:
        words = asyncio.run(agent.generate())
    assert words[:2] == ["AI 应用开发", "Agent 工程师"]
    assert len(words) == 4


def test_parse_cleans_and_dedupes():
    payload = {"keywords": ["AI 应用开发", " AI 应用开发 ", "", "「Agent 工程师」", "x"]}
    agent, override = _agent_answering(json.dumps(payload, ensure_ascii=False))
    with override:
        words = asyncio.run(agent.generate())
    assert words == ["AI 应用开发", "Agent 工程师"]


def test_invalid_output_retries_then_raises():
    calls = {"n": 0}

    def wrapped(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        return ModelResponse(parts=[TextPart("我觉得可以投 Python 相关岗位。")])

    agent = KeywordAgent(resume="r", llm=LLM)
    with (
        agent.agent.override(model=FunctionModel(wrapped)),
        pytest.raises(AgentRunError),
    ):
        asyncio.run(agent.generate())
    assert calls["n"] == KeywordAgent.PARSE_RETRIES


def test_prompt_carries_resume_and_requirement():
    agent = KeywordAgent(resume="简历正文", requirement="只投 Agent", llm=LLM)
    prompt = agent._prompt()
    assert "简历正文" in prompt and "只投 Agent" in prompt
    assert "6" in prompt  # 关键词数量
