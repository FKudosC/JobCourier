"""通用小工具：统一日志与异常记录。"""

from __future__ import annotations

import logging
import sys
from typing import Any

_ROOT_NAME = "jobcourier"
_configured = False


def get_logger(tag: str = "boss") -> logging.Logger:
    """取一个带统一格式的子 logger（时间戳 + 级别 + 模块前缀）。"""
    global _configured
    if not _configured:
        logger = logging.getLogger(_ROOT_NAME)
        handler = logging.StreamHandler(sys.stdout or sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%H:%M:%S"
            )
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _configured = True
    return logging.getLogger(f"{_ROOT_NAME}.{tag}")


def as_dict(value: Any) -> dict[str, Any]:
    """是 dict 就原样返回，否则返回空 dict（接口数据防御用）。"""
    return value if isinstance(value, dict) else {}


def log(msg: str, tag: str = "boss") -> None:
    """带模块前缀记一行 INFO 日志。"""
    get_logger(tag).info(msg)


def exception_text(exc: BaseException) -> str:
    """「类型：消息」文本，用于拼给用户看的提示（不写日志）。"""
    return f"{type(exc).__name__}：{exc}"


def log_exception(tag: str, exc: BaseException) -> str:
    """记一条异常（含堆栈），并返回「类型：消息」文本供调用方拼用户提示。"""
    get_logger(tag).error("%s: %s", type(exc).__name__, exc, exc_info=True)
    return exception_text(exc)
