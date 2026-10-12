"""Tests for LLM output tolerance: bounded output, extra retries, actionable error messages."""

from __future__ import annotations

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models.openai import ModelHTTPError

from job.boss.chat import ChatReplier
from job.boss.company import CompanyReviewer
from job.boss.review import JobReviewer
from job.models.setting import LlmSettings
from job.utils.llm import MAX_OUTPUT_TOKENS, OUTPUT_RETRIES, explain

LLM = LlmSettings(api_key="k", model="deepseek-v4-pro")


def _agents():
    """三个会调模型的 Agent：岗位复核、公司风险、聊天回复。"""
    return {
        "reviewer": JobReviewer(requirement="只投 Agent", llm=LLM).agent,
        "company": CompanyReviewer(llm=LLM).agent,
        "chat": ChatReplier(prompt="简短回复", resume="r", llm=LLM).agent,
    }


@pytest.mark.parametrize("name", ["reviewer", "company", "chat"])
def test_output_is_bounded(name):
    """三个 Agent 都要限死输出长度，避免个别模型长篇大论烧钱。"""
    assert _agents()[name].model_settings["max_tokens"] == MAX_OUTPUT_TOKENS


@pytest.mark.parametrize("name", ["company", "chat"])
def test_output_retries_are_raised(name):
    """输出格式不合规时多给一次机会，少丢一批岗位。

    岗位复核改用纯文本输出 + 自有解析重试（见 test_review.py），不在此列。
    """
    agent = _agents()[name]
    assert agent._max_output_retries == OUTPUT_RETRIES
    assert agent._max_tool_retries == 1, "工具重试不该被顺带改掉"


def test_review_uses_own_parse_retries():
    """岗位复核走纯文本输出，解析重试在 review() 里自己做。"""
    agent = JobReviewer(requirement="只投 Agent", llm=LLM).agent
    assert agent.output_type is str or str(agent.output_type) == "str"
    assert JobReviewer.PARSE_RETRIES >= 2


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_says_retry_is_useless(status):
    """Key 或权限不对，重试一万次也没用，要提示人去改配置。"""
    message = explain(ModelHTTPError(status_code=status, model_name="m"))
    assert "重试无用" in message
    assert f"HTTP {status}" in message


def test_rate_limit_and_quota_are_split():
    """429 多半是限流，等一会能好；额度用完则要人去处理。"""
    limited = explain(ModelHTTPError(status_code=429, model_name="m"))
    assert "重试无用" not in limited
    assert "余额" in limited
    assert "重试无用" in explain(UsageLimitExceeded("quota"))


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_error_is_retryable(status):
    """模型服务挂了一下是暂时的，提示稍后重试。"""
    message = explain(ModelHTTPError(status_code=status, model_name="m"))
    assert "稍后重试即可" in message and "重试无用" not in message


@pytest.mark.parametrize("status", [400, 404])
def test_other_client_error_is_not_retryable(status):
    """模型名写错这类问题重试无用，让人去检查配置。"""
    assert "重试无用" in explain(ModelHTTPError(status_code=status, model_name="m"))


def test_bad_output_points_at_the_model():
    """输出不合规是模型的问题，该提示换模型而不是重试。"""
    message = explain(UnexpectedModelBehavior("没有返回 JSON"))
    assert "重试无用" in message and "结构化输出" in message


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("Read timed out"),
        ConnectionError("connection reset by peer"),
        RuntimeError("service temporarily unavailable"),
    ],
)
def test_network_blips_are_retryable(exc):
    """网络抖动、超时这类要按能重试处理，别让人白折腾配置。"""
    assert "稍后重试即可" in explain(exc)


def test_unknown_error_keeps_detail():
    """认不出来的异常也要把原文带回去，别丢线索。"""
    message = explain(RuntimeError("某个没见过的怪问题"))
    assert "稍后重试即可" in message and "某个没见过的怪问题" in message
