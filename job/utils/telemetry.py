"""OpenTelemetry 链路追踪：把 LLM 调用打到终端或 OTLP 采集端，默认关闭。

未开启时不安装任何 TracerProvider，OpenTelemetry 走 no-op 实现，没有额外开销。
开启后 ``Agent`` 才会产出 span；span 里默认不含提示词原文（简历、岗位详情等）。
"""

from __future__ import annotations

import atexit
from typing import TYPE_CHECKING

from pydantic_ai.models.instrumented import InstrumentationSettings

from job.utils import log

if TYPE_CHECKING:
    from pydantic_ai import Agent

# 写进每条 span 的 resource 属性
SERVICE_NAME = "job-courier"

# 全局 TracerProvider 只能装一次，重复调用直接复用第一次的结果
_installed = False
_settings: InstrumentationSettings | None = None


def setup() -> bool:
    """按已保存的配置安装全局 TracerProvider，返回追踪是否真正启用。

    幂等：只有第一次调用会装配 provider，之后都返回同一个结果。改完配置需要重启应用。
    """
    global _installed, _settings
    if _installed:
        return _settings is not None
    _installed = True

    # 延迟导入：设置表依赖数据库，模块顶层导入会形成循环
    from job.models.setting import TelemetrySettings

    config = TelemetrySettings.load()
    if not config.enabled:
        return False
    try:
        _install(config)
    except ImportError as exc:
        log(f"未安装 opentelemetry SDK，链路追踪不可用：{exc}", tag="otel")
        return False
    # pydantic-ai 的 include_content 默认是 True，会把简历和岗位详情原文写进 span，
    # 这里跟着配置走，默认关闭。
    _settings = InstrumentationSettings(
        include_content=config.content,
        include_binary_content=config.content,
    )
    log(f"链路追踪已开启：{config.exporter}（原文记录：{'开' if config.content else '关'}）", tag="otel")
    return True


def enabled() -> bool:
    """链路追踪是否已启用。"""
    return _settings is not None


def attach(agent: Agent) -> Agent:
    """给 Agent 挂上链路追踪；追踪未启用时原样返回。

    首次调用时才读取配置并装配 provider，所以只要有 Agent 被构造就会自动生效，
    不依赖应用启动顺序。
    """
    if not setup() or _settings is None:
        return agent
    agent.instrument = _settings
    return agent


def _install(config) -> None:
    """装配 TracerProvider 与 span exporter，并注册退出前 flush。"""
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    if config.exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=config.endpoint))
        )
    else:
        # 打到终端，便于本地排查；批量导出交给 otlp 模式
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)

