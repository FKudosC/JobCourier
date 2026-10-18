"""启动入口的后台开关：``--background`` / ``JOB_COURIER_BACKGROUND``。

为什么单独测：Windows 上虚拟环境的 ``pythonw.exe`` 有时只是控制台程序的转发壳，
它会拉起控制台版 ``python.exe``，进程于是又挂上（隐藏的）控制台——只凭 ``isatty``
判断「有没有终端」会失效，日志就找不到落点。所以后台运行必须有一个不依赖终端的
显式开关，且它要能压过终端判据。
"""

from __future__ import annotations

import io

import pytest

from job.api import serve


class _Tty(io.StringIO):
    """假装自己是一个真终端。"""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def clean_switch(monkeypatch):
    """恢复成「命令行没写、环境变量没设」的干净状态。"""
    monkeypatch.delenv(serve._BACKGROUND_ENV, raising=False)
    monkeypatch.setattr(serve.sys, "argv", ["main.py"])
    return monkeypatch


def test_switch_is_off_by_default(clean_switch):
    assert serve._background_requested() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_env_var_accepts_common_truthy(clean_switch, value):
    clean_switch.setenv(serve._BACKGROUND_ENV, value)
    assert serve._background_requested() is True


@pytest.mark.parametrize("value", ["", "0", "no", "off", "false", "background"])
def test_env_var_ignores_falsy(clean_switch, value):
    clean_switch.setenv(serve._BACKGROUND_ENV, value)
    assert serve._background_requested() is False


@pytest.mark.parametrize("flag", ["--background", "--bg"])
def test_flag_on_command_line(clean_switch, flag):
    clean_switch.setattr(serve.sys, "argv", ["main.py", flag])
    assert serve._background_requested() is True


def test_unrelated_args_do_not_trip_the_switch(clean_switch):
    clean_switch.setattr(serve.sys, "argv", ["main.py", "--port", "9000"])
    assert serve._background_requested() is False


def test_switch_beats_a_real_terminal(clean_switch):
    """核心回归：终端是真的，但开关说了算。"""
    clean_switch.setattr(serve.sys, "stdout", _Tty())
    clean_switch.setattr(serve.sys, "stderr", _Tty())
    assert serve._headless() is False
    clean_switch.setenv(serve._BACKGROUND_ENV, "1")
    assert serve._headless() is True


def test_run_does_not_pop_the_panel_when_background(clean_switch):
    opened: list[str] = []
    clean_switch.setattr(serve, "_ping_ok", lambda: True)
    clean_switch.setattr(serve.webbrowser, "open", opened.append)
    clean_switch.setattr(serve.sys, "argv", ["main.py", "--background"])
    serve.run(open_browser=True)
    assert opened == []


def test_run_pops_the_panel_when_foreground(clean_switch):
    opened: list[str] = []
    clean_switch.setattr(serve, "_ping_ok", lambda: True)
    clean_switch.setattr(serve.webbrowser, "open", opened.append)
    serve.run(open_browser=True)
    assert opened == [serve.URL]
