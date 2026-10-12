"""模型调用的输出容错：限制输出长度、约定重试次数，并把异常翻译成可行动的提示。

三个 Agent（岗位复核、公司风险、聊天回复）都走提示词 JSON，结论只有几十字，
限制 token 上限可以兜住个别模型的长篇大论，输出不合规时多给一次重试机会，
少丢一批岗位、少让用户手动补救。
"""

from __future__ import annotations

import enum
import re
from typing import Any

from openai import APIStatusError
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models.openai import ModelHTTPError

# 结论输出本身只有几十字，但思考型模型（DeepSeek R1、部分 OpenRouter 匿名模型）
# 的思考 token 计入 max_tokens：2000 会被纯思考耗尽、正文一个字都吐不出来。
# 上限只是天花板，费用按实际输出计，非思考模型不受影响。
MAX_OUTPUT_TOKENS = 8000

# 思考耗尽时自动加倍的顶格值（评审失败时逐级翻倍到这里）
MAX_OUTPUT_TOKENS_CEILING = 16000

# pydantic-ai 默认只重试 1 次，这里给到 2 次，只影响输出格式不合规的情况
OUTPUT_RETRIES = 2

# 这些词多半是网络抖动或超时，不是模型本身的问题，等一会儿重试就行
_NETWORK_HINTS = ("timeout", "timed out", "connection", "connect", "unreachable", "temporarily")


class ErrorKind(str, enum.Enum):
    """模型调用异常的种类；调用方按种类决定重试还是放弃。

    判定顺序经过设计：HTTP 状态码优先于关键词，auth 先于限流——
    429 也可能是 Key 无权限，先看身份再看额度，避免给错处置建议。
    """

    # Key 无效 / 无权限：重试无用，去改配置
    AUTH = "auth"
    # 余额或用量超限：重试无用
    QUOTA = "quota"
    # 限流：等一会儿重试有意义
    RATE_LIMIT = "rate_limit"
    # 输出被截断：提高 token 上限重试
    OUTPUT_TRUNCATED = "output_truncated"
    # 思考型模型把输出上限全耗在思考上，正文一个字没吐：换模型
    THINKING_OVERFLOW = "thinking_overflow"
    # 输出格式不合规：换模型或重试
    BAD_OUTPUT = "bad_output"
    # 网络超时 / 中断：稍后重试
    NETWORK = "network"
    # 其余未知错误：稍后重试
    OTHER = "other"

    @property
    def retryable(self) -> bool:
        """重试有没有意义。"""
        return self not in (self.AUTH, self.QUOTA, self.BAD_OUTPUT, self.THINKING_OVERFLOW)


def classify(exc: Exception) -> ErrorKind:
    """把模型调用异常归类；``explain`` 的文案和调用方的重试策略都以它为准。"""
    status = getattr(exc, "status_code", None)
    if isinstance(exc, (ModelHTTPError, APIStatusError)) and isinstance(status, int):
        if status in (401, 403):
            return ErrorKind.AUTH
        if status == 429:
            return ErrorKind.RATE_LIMIT
        if status >= 500:
            return ErrorKind.NETWORK
        return ErrorKind.OTHER
    if isinstance(exc, UsageLimitExceeded):
        return ErrorKind.QUOTA
    if isinstance(exc, UnexpectedModelBehavior):
        # 思考型模型把输出上限全花在思考上、正文一个字没吐——单独归类给针对性提示
        if "before any response was generated" in str(exc) or "token limit" in str(exc).lower():
            return ErrorKind.THINKING_OVERFLOW
        return ErrorKind.BAD_OUTPUT
    if any(hint in str(exc).lower() for hint in _NETWORK_HINTS):
        return ErrorKind.NETWORK
    return ErrorKind.OTHER


def explain(exc: Exception) -> str:
    """把模型调用异常翻译成一句可行动的中文提示。

    按"重试有没有意义"分类：Key、权限、额度这类重试无用，需要人去改配置；
    限流、5xx、网络抖动多半是暂时的，重试有意义。
    """
    kind = classify(exc)
    if kind is ErrorKind.AUTH:
        status = getattr(exc, "status_code", "")
        return f"API Key 无效或无权限（HTTP {status}，重试无用：请到设置里换 Key）"
    if kind is ErrorKind.QUOTA:
        return "超出用量上限（重试无用：请提高上限或等额度恢复）"
    if kind is ErrorKind.RATE_LIMIT:
        status = getattr(exc, "status_code", "")
        return f"接口限流或余额不足（HTTP {status}：等一会再试，持续出现请检查账户余额）"
    if kind is ErrorKind.BAD_OUTPUT:
        return "模型输出格式不合规（重试无用：请换用支持结构化输出的模型）"
    if kind is ErrorKind.THINKING_OVERFLOW:
        return (
            "这个模型的思考过程耗尽了输出上限，正文一个字都没生成"
            "（重试无用：请在模型列表里换一个常规模型，"
            "如 deepseek-v4-flash、glm-5.3-flash、qwen3.5-flash 等）"
        )
    if kind is ErrorKind.NETWORK:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            return f"模型服务暂时异常（HTTP {status}：稍后重试即可）"
        return "连接超时或网络中断（稍后重试即可）"
    if kind is ErrorKind.OTHER:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            return f"请求被接口拒绝（HTTP {status}，重试无用：请检查模型名与参数）"
        return f"调用失败（稍后重试即可）：{exc}"
    return f"调用失败（稍后重试即可）：{exc}"


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """从模型回复里抠出 JSON：剥掉 markdown 围栏，截取第一个配平的大括号块。

    不少模型会把 JSON 包进 ```json 围栏或前后带说明文字，
    严格的 ``json.loads(整段文本)`` 会白白失败；这里按括号配平提取。
    解析不了抛 ``ValueError``（含 ``JSONDecodeError``）。
    """
    import json as _json

    if not (text := (text or "").strip()):
        raise ValueError("模型回复为空")
    if "```" in text and (blocks := _FENCE_RE.findall(text)):
        text = max(blocks, key=len).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("回复里没有找到 JSON 对象")
    depth, in_str, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_str:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return _json.loads(text[start : index + 1])
    raise ValueError("JSON 未闭合")
