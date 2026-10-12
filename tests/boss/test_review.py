"""Tests for AI job scoring and intent review."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic_ai.exceptions import AgentRunError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider

from job.boss.jobs import Job
from job.boss.review import JobReviewer, RawReview
from job.models.setting import LlmSettings

LLM = LlmSettings(api_key="k", model="deepseek-v4-pro")
JOB = Job(title="AI Agent 开发工程师", company="某科技", salary="15-25K", description="搭建 LLM 智能体")
RESUME = "Python LangGraph"


def _dims(**over: int) -> list[dict]:
    """五维满分作答，个别维度可覆盖。"""
    full = {"duty": 40, "evidence": 25, "hard": 15, "stack": 10, "conditions": 10}
    return [
        {"key": key, "score": over.get(key, full), "evidence": f"{key} 依据"}
        for key, full in full.items()
    ]


def _reviewer_answering(
    payload: dict,
    prompts: list[str] | None = None,
    *,
    resume: str = "",
    scam_block: bool = False,
    requirement: str = "只投 Agent",
):
    """复核器的模型换成本地函数：记录提示词，按给定素材回复 JSON 文本。"""

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert not info.output_tools, "不能走工具调用，DeepSeek 思考模式会拒绝"
        if prompts is not None:
            prompts.append(str(messages[-1].parts[-1].content))
        return ModelResponse(parts=[TextPart(json.dumps(payload, ensure_ascii=False))])

    reviewer = JobReviewer(
        requirement=requirement, resume=resume, scam_block=scam_block, llm=LLM
    )
    return reviewer, reviewer.agent.override(model=FunctionModel(answer))


def _payload(**over) -> dict:
    """一份「完全匹配」的评分素材，按需覆盖。"""
    data = {"dimensions": _dims(), "hard_gaps": [], "reason": "Agent 开发"}
    data.update(over)
    return data


def test_from_config_requires_resume_or_scam():
    """AI 岗位筛选已由关键词研判 + 评分替代：只有简历匹配或骗子识别才建复核器。"""
    assert JobReviewer.from_config({"resume_text": "Python"}, LLM) is None
    assert JobReviewer.from_config({"scam_block": True}, LLM) is not None
    config = {"resume_match": True, "resume_text": " Python / LangGraph "}
    reviewer = JobReviewer.from_config(config, LLM)
    assert reviewer is not None and reviewer.requirement == ""


def test_from_config_resume_match():
    config = {"resume_match": True, "resume_text": " Python / LangGraph "}
    reviewer = JobReviewer.from_config(config, LLM)
    assert reviewer is not None
    assert reviewer.resume == "Python / LangGraph" and reviewer.requirement == ""
    assert reviewer.checks == ["简历技术匹配"]
    assert "评分规则" in reviewer.instructions
    assert "岗位意图" not in reviewer.instructions
    assert (
        JobReviewer.from_config({"resume_match": True, "resume_text": ""}, LLM) is None
    )
    assert JobReviewer.from_config({"resume_text": "Python"}, LLM) is None


def test_from_config_salary_range():
    config = {"resume_match": True, "resume_text": "Python", "salary_min": 15, "salary_max": 30}
    reviewer = JobReviewer.from_config(config, LLM)
    assert reviewer is not None
    assert reviewer.salary_range is not None
    assert (reviewer.salary_range.low, reviewer.salary_range.high) == (15, 30)
    assert JobReviewer.from_config({"resume_match": True, "resume_text": "Python"}, LLM).salary_range is None


def test_both_checks_in_one_prompt():
    config = {
        "resume_match": True,
        "resume_text": "Python LangGraph",
        "salary_min": 18,
        "salary_max": 30,
    }
    reviewer = JobReviewer.from_config(config, LLM)
    assert reviewer is not None and reviewer.checks == ["简历技术匹配"]
    prompt = reviewer._prompt(JOB)
    assert "Python LangGraph" in prompt
    assert "搭建 LLM 智能体" in prompt
    # 薪资结论由程序算好放进提示词
    assert "交集为 18K-25K" in prompt
    assert "五个维度" in prompt


def test_agent_uses_configured_model():
    reviewer = JobReviewer(requirement="只投 Agent", llm=LLM)
    assert reviewer.agent.model.model_name == "deepseek-v4-pro"


def test_match_passes_and_prompt_has_requirement():
    prompts: list[str] = []
    reviewer, override = _reviewer_answering(_payload(), prompts)
    with override:
        verdict = asyncio.run(reviewer.check(JOB))
    assert verdict.match is True and verdict.reason == "Agent 开发"
    assert verdict.score is None and verdict.hard_gaps == []
    assert "只投 Agent" in prompts[0] and "搭建 LLM 智能体" in prompts[0]


def test_score_sums_dimensions_in_program():
    """总分是程序求和的：模型不用也不可能直接给总分。"""
    payload = _payload(dimensions=_dims(duty=30))
    reviewer, override = _reviewer_answering(payload, resume=RESUME)
    with override:
        verdict = asyncio.run(reviewer.check(JOB))
    assert verdict.score == 90
    assert verdict.match is True
    assert [d.key for d in verdict.dimensions] == ["duty", "evidence", "hard", "stack", "conditions"]


def test_out_of_range_score_is_clamped():
    """越界分数压回满分区间，缺的维度补零。"""
    dims = _dims()
    dims[0]["score"] = 99
    payload = {"dimensions": dims[:2], "hard_gaps": [], "reason": "部分"}
    reviewer, override = _reviewer_answering(payload, resume=RESUME)
    with override:
        verdict = asyncio.run(reviewer.check(JOB))
    duty = verdict.dimensions[0]
    assert duty.score == 40 and duty.max == 40
    assert verdict.score == sum(d.max for d in verdict.dimensions[:2]) + 0


def test_high_score_without_evidence_is_capped():
    """讲不出证据的高分被压到满分六成。"""
    dims = _dims()
    dims[0]["evidence"] = ""
    dims[0]["score"] = 40
    payload = {"dimensions": dims, "hard_gaps": [], "reason": ""}
    reviewer, override = _reviewer_answering(payload, resume=RESUME)
    with override:
        verdict = asyncio.run(reviewer.check(JOB))
    assert verdict.dimensions[0].score == int(40 * JobReviewer.NO_EVIDENCE_RATIO)


def test_hard_gaps_cap_total_score():
    """缺 2 项硬性要求时总分封顶 74。"""
    dims = _dims()
    payload = {"dimensions": dims, "hard_gaps": ["Kubernetes 运维", "英语流利"], "reason": "缺口"}
    reviewer, override = _reviewer_answering(payload, resume=RESUME)
    with override:
        verdict = asyncio.run(reviewer.check(JOB))
    assert verdict.score == 74
    assert verdict.match is True  # 没过线的是 3 项以上才算核心缺失


def test_three_hard_gaps_fail():
    """缺 ≥3 项硬性要求直接判不合适，原因带上缺口。"""
    gaps = ["Docker", "K8s", "英语"]
    payload = {"dimensions": _dims(), "hard_gaps": gaps, "reason": "缺口多"}
    reviewer, override = _reviewer_answering(payload, resume=RESUME)
    with override:
        verdict = asyncio.run(reviewer.check(JOB))
    assert verdict.match is False
    assert "Docker" in verdict.reason and "K8s" in verdict.reason
    assert verdict.score == 64


def test_hard_gaps_ignored_without_resume():
    """没开简历匹配时 hard_gaps 不参与判定，分数为 None。"""
    payload = {"dimensions": _dims(), "hard_gaps": ["Docker", "K8s", "英语"], "reason": "x"}
    reviewer, override = _reviewer_answering(payload, requirement="只投 Agent")
    with override:
        verdict = asyncio.run(reviewer.check(JOB))
    assert verdict.score is None
    assert verdict.hard_gaps == []
    assert verdict.match is True


def test_mismatch_rejects_with_reason():
    payload = _payload(intent_ok=False, reason="销售岗")
    reviewer, override = _reviewer_answering(payload)
    with override:
        assert asyncio.run(reviewer.check(JOB)).match is False


def test_scam_verdict_blocks_the_job():
    payload = _payload(scam_ok=False, reason="刷单兼职")
    reviewer, override = _reviewer_answering(payload, scam_block=True, requirement="")
    with override:
        verdict = asyncio.run(reviewer.check(JOB))
    assert verdict.match is False
    assert verdict.reason == "刷单兼职"


def test_scam_rule_only_in_prompt_when_enabled():
    off = JobReviewer(requirement="只投 Agent", llm=LLM)
    assert "骗子岗位识别" not in off.instructions
    on = JobReviewer(scam_block=True, llm=LLM)
    assert "骗子岗位识别" in on.instructions
    assert on.checks == ["屏蔽骗子岗位"]


def test_min_score_from_config_only_when_enabled():
    config = {"resume_match": True, "resume_text": "Python", "min_score": 70}
    assert JobReviewer.from_config(config, LLM).min_score is None
    reviewer = JobReviewer.from_config({**config, "score_filter": True}, LLM)
    assert reviewer.min_score == 70
    assert reviewer.checks == ["简历技术匹配", "匹配度 ≥70"]


def test_low_score_rejected_by_min_score():
    dims = _dims(duty=15)
    payload = {"dimensions": dims, "hard_gaps": [], "reason": "部分吻合"}
    reviewer, override = _reviewer_answering(payload, resume=RESUME)
    with override:
        reviewer.min_score = 80
        verdict = asyncio.run(reviewer.check(JOB))
        assert verdict.match is False
        assert verdict.reason == "匹配度 75 分，低于 80 分"
        reviewer.min_score = 50
        assert asyncio.run(reviewer.check(JOB)).match is True


def test_scam_check_joins_other_checks():
    """和其他复核项一起开时都生效，一次调用即可。"""
    config = {
        "resume_match": True,
        "resume_text": "Python",
        "scam_block": True,
    }
    reviewer = JobReviewer.from_config(config, LLM)
    assert reviewer is not None
    assert reviewer.checks == ["屏蔽骗子岗位", "简历技术匹配"]
    assert "骗子岗位识别" in reviewer.instructions
    assert "评分规则" in reviewer.instructions


def test_scam_block_alone_creates_reviewer():
    """只开骗子识别、没开岗位筛选时也得有复核器，否则这个开关等于没用。"""
    reviewer = JobReviewer.from_config({"scam_block": True}, LLM)
    assert reviewer is not None
    assert reviewer.scam_block is True
    assert reviewer.checks == ["屏蔽骗子岗位"]


def test_detail_json_roundtrip():
    """detail 字段存库后能还原出维度明细。"""
    payload = _payload(dimensions=_dims(duty=30))
    reviewer, override = _reviewer_answering(payload, resume=RESUME)
    with override:
        verdict = asyncio.run(reviewer.check(JOB))
    data = json.loads(verdict.detail)
    assert data["hard_gaps"] == []
    assert len(data["dimensions"]) == 5
    assert data["dimensions"][0] == {
        "key": "duty",
        "label": "核心职责匹配",
        "score": 30,
        "max": 40,
        "evidence": "duty 依据",
    }


def test_retry_on_rate_limit(monkeypatch):
    """限流错误等几秒重试一次，重试成功不丢岗位。"""
    calls = {"n": 0}

    def answer(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ModelHTTPError(status_code=429, model_name="deepseek-v4-pro")
        return ModelResponse(parts=[TextPart(json.dumps(_payload(), ensure_ascii=False))])

    monkeypatch.setattr(JobReviewer, "RETRY_DELAY", 0)
    reviewer = JobReviewer(requirement="只投 Agent", resume=RESUME, llm=LLM)
    with reviewer.agent.override(model=FunctionModel(answer)):
        verdict = asyncio.run(reviewer.check(JOB))
    assert calls["n"] == 2 and verdict.match is True


def test_no_retry_on_auth_error():
    """Key 无效不重试，直接抛错让人去改配置。"""

    def fail(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=401, model_name="deepseek-v4-flash")

    reviewer = JobReviewer(requirement="只投 Agent", llm=LLM)
    with (
        reviewer.agent.override(model=FunctionModel(fail)),
        pytest.raises(AgentRunError),
    ):
        asyncio.run(reviewer.check(JOB))


def test_rules_actually_reach_the_model():
    """回归：pydantic-ai 会静默丢弃字符串形式的 instructions，必须走 system_prompt。

    丢了也不报错，只是模型收不到判定规则，AI 复核形同虚设。
    """
    sent: list[str] = []

    def answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        sent.extend(str(getattr(p, "content", "")) for m in messages for p in m.parts)
        return ModelResponse(parts=[TextPart(json.dumps(_payload(), ensure_ascii=False))])

    reviewer = JobReviewer(scam_block=True, llm=LLM)
    with reviewer.agent.override(model=FunctionModel(answer)):
        asyncio.run(reviewer.check(JOB))
    joined = "".join(sent)
    assert "骗子岗位识别" in joined
    assert JOB.title in joined


def test_raw_review_tolerates_missing_optional_fields():
    """模型少给字段时按默认值处理，不炸。"""
    raw = RawReview.model_validate({"dimensions": []})
    assert raw.scam_ok and raw.intent_ok and raw.hard_gaps == []


def test_extract_json_handles_fences_and_prose():
    """围栏、前后说明文字、嵌套括号都要能抠出 JSON。"""
    from job.utils.llm import extract_json

    payload = {"a": 1, "b": {"c": "包含 } 花括号 \" 引号"}}
    text = f"分析如下：\n```json\n{json.dumps(payload, ensure_ascii=False)}\n```\n以上就是结论"
    assert extract_json(text) == payload
    assert extract_json("结论：" + json.dumps(payload)) == payload


def test_fenced_output_still_scores():
    """模型把 JSON 包进 markdown 围栏时也能评分——解析层剥掉围栏。"""
    fenced = f"```json\n{json.dumps(_payload(), ensure_ascii=False)}\n```"

    def answer(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(fenced)])

    reviewer = JobReviewer(requirement="只投 Agent", resume=RESUME, llm=LLM)
    with reviewer.agent.override(model=FunctionModel(answer)):
        verdict = asyncio.run(reviewer.check(JOB))
    assert verdict.match is True and verdict.score == 100


def test_prose_then_json_retries():
    """第一次回散文、第二次给合法 JSON：纠错重试后正常评分，不丢岗位。"""
    calls = {"n": 0}

    def answer(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(parts=[TextPart("这个岗位和您的背景非常匹配，建议投递。")])
        return ModelResponse(parts=[TextPart(json.dumps(_payload(), ensure_ascii=False))])

    reviewer = JobReviewer(requirement="只投 Agent", resume=RESUME, llm=LLM)
    with reviewer.agent.override(model=FunctionModel(answer)):
        verdict = asyncio.run(reviewer.check(JOB))
    assert calls["n"] == 2 and verdict.match is True


def test_dict_dimensions_are_coerced():
    """dimensions 给成 {维度名: 分数} 的字典（中文键）也能解析。

    字典形式带不出 evidence，按「无证据高分」规则各维度压到满分六成——
    与证据规则的口径一致，不给没依据的模型虚高分。
    """
    payload = {
        "scam_ok": True,
        "intent_ok": True,
        "dimensions": {"核心职责匹配": 30, "可迁移证据": 20, "硬性要求": 12, "工具与技术栈": 8, "实际条件": 9},
        "reason": "吻合",
    }

    def answer(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(json.dumps(payload, ensure_ascii=False))])

    reviewer = JobReviewer(requirement="只投 Agent", resume=RESUME, llm=LLM)
    with reviewer.agent.override(model=FunctionModel(answer)):
        verdict = asyncio.run(reviewer.check(JOB))
    # 无证据钳制：每维度压到「满分 × 0.6」
    expected = sum(int(full * JobReviewer.NO_EVIDENCE_RATIO) for _, full in JobReviewer.DIMENSIONS.values())
    assert verdict.score == expected
    assert verdict.dimensions[0].key == "duty"


def test_always_invalid_output_raises_after_retries(monkeypatch):
    """三次都解析不了才报「输出不合规」，且错误可被上层按 BAD_OUTPUT 处理。"""
    calls = {"n": 0}

    def answer(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        return ModelResponse(parts=[TextPart("我觉得还行，您看着办。")])

    monkeypatch.setattr(JobReviewer, "PARSE_RETRIES", 2)
    reviewer = JobReviewer(requirement="只投 Agent", resume=RESUME, llm=LLM)
    with reviewer.agent.override(model=FunctionModel(answer)), pytest.raises(AgentRunError):
        asyncio.run(reviewer.check(JOB))
    assert calls["n"] == 2


def test_api_key_is_trimmed():
    assert LlmSettings(api_key="  sk-1 \n").api_key == "sk-1"


def test_provider_defaults_to_deepseek():
    assert isinstance(LLM.provider, DeepSeekProvider)
    assert str(LLM.provider.client.base_url).startswith(LlmSettings.DEFAULT_BASE_URL)


def test_provider_uses_custom_base_url():
    llm = LlmSettings(
        base_url=" http://localhost:11434/v1 ", api_key="k", model="qwen3"
    )
    assert isinstance(llm.provider, OpenAIProvider)
    assert str(llm.provider.client.base_url) == "http://localhost:11434/v1/"


def test_ready_needs_key_and_model():
    assert not LlmSettings(api_key="k").ready
    assert not LlmSettings(model="deepseek-v4-pro").ready
    assert LLM.ready
