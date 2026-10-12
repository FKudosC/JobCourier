"""Tests for OpenTelemetry wiring: off by default, opt-in, and no prompt content by default."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from job import models as models_pkg
from job.models import init_db, reset_engine
from job.models.setting import TelemetrySettings
from job.utils import telemetry


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(models_pkg, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(models_pkg, "DATA_DIR", tmp_path)
    reset_engine()
    init_db()
    yield
    reset_engine()


@pytest.fixture(autouse=True)
def reset_telemetry():
    """telemetry keeps module-level state; every test starts from a clean slate."""
    telemetry._installed = False
    telemetry._settings = None
    yield
    telemetry._installed = False
    telemetry._settings = None


def test_defaults_are_off():
    """Nothing is recorded until the user explicitly turns tracing on."""
    s = TelemetrySettings()
    assert s.enabled is False
    assert s.exporter == "console"
    assert s.content is False


def test_settings_round_trip(tmp_db: Path):
    TelemetrySettings(enabled=True, exporter="otlp", endpoint="http://x/v1/traces").save()
    loaded = TelemetrySettings.load()
    assert loaded.enabled is True
    assert loaded.exporter == "otlp"
    assert loaded.endpoint == "http://x/v1/traces"


def test_disabled_installs_nothing(tmp_db: Path, monkeypatch: pytest.MonkeyPatch):
    """Disabled means no TracerProvider is registered at all - zero overhead."""
    called = []
    monkeypatch.setattr(
        "opentelemetry.trace.set_tracer_provider", lambda p: called.append(p)
    )
    assert telemetry.setup() is False
    assert called == []
    assert telemetry.enabled() is False


def test_attach_is_noop_when_disabled(tmp_db: Path):
    agent = Agent(TestModel())
    assert telemetry.attach(agent) is agent
    assert not agent.instrument


def test_enabled_registers_provider_and_instruments(tmp_db: Path, monkeypatch: pytest.MonkeyPatch):
    TelemetrySettings(enabled=True, exporter="console").save()
    providers = []
    monkeypatch.setattr("opentelemetry.trace.set_tracer_provider", providers.append)

    agent = Agent(TestModel())
    assert telemetry.attach(agent) is agent
    assert agent.instrument is not False
    assert providers, "a TracerProvider should have been registered"


def test_content_stays_out_of_spans_by_default(tmp_db: Path, monkeypatch: pytest.MonkeyPatch):
    """Prompts carry the user's resume, so content capture must be opt-in too."""
    TelemetrySettings(enabled=True, exporter="console").save()
    monkeypatch.setattr("opentelemetry.trace.set_tracer_provider", lambda p: None)
    telemetry.setup()
    assert telemetry._settings.include_content is False
    assert telemetry._settings.include_binary_content is False


def test_content_can_be_opted_in(tmp_db: Path, monkeypatch: pytest.MonkeyPatch):
    TelemetrySettings(enabled=True, exporter="console", content=True).save()
    monkeypatch.setattr("opentelemetry.trace.set_tracer_provider", lambda p: None)
    telemetry.setup()
    assert telemetry._settings.include_content is True


def test_setup_is_idempotent(tmp_db: Path, monkeypatch: pytest.MonkeyPatch):
    TelemetrySettings(enabled=True, exporter="console").save()
    providers = []
    monkeypatch.setattr("opentelemetry.trace.set_tracer_provider", providers.append)
    assert telemetry.setup() is True
    assert telemetry.setup() is True
    assert len(providers) == 1
