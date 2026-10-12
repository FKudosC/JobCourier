"""本地服务启动：uvicorn 同时伺服 REST/WebSocket 与前端静态文件。

端口与监听地址集中在 ``job/api/net.py``（默认 127.0.0.1:8000）。启动脚本和
``python -m job`` 都指向这里。Windows 以 GUI 方式启动时进程没有 stdout/stderr，
会崩掉 uvicorn 的日志初始化，所以在这个模块导入 uvicorn 之前先把它们兜到日志
文件（带轮转，避免长跑后日志无限增长）。
"""

from __future__ import annotations

import logging
import logging.handlers
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from job.api.net import HOST, PORT, URL
from job.paths import USER_DATA_DIR

_READY_TIMEOUT = 300.0

_log_file = None
if sys.stdout is None or sys.stderr is None:
    _log_path = USER_DATA_DIR / "logs" / "backend.log"
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_file = logging.handlers.RotatingFileHandler(
        _log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _log_file.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(message)s")
    )
    _stream = _log_file.stream
    sys.stdout = sys.stdout or _stream
    sys.stderr = sys.stderr or _stream

import uvicorn  # noqa: E402  必须在 stdout 兜底之后导入

from job.api.app import app  # noqa: E402
from job.utils import get_logger  # noqa: E402

log = get_logger("serve")
if _log_file is not None:
    # 让统一的 jobcourier logger 也写进轮转文件
    logging.getLogger("jobcourier").addHandler(_log_file)


def _ping_ok() -> bool:
    """本地是否已有本项目在跑（``/ping`` 通即视为已就绪）。"""
    try:
        with urllib.request.urlopen(f"{URL}/ping", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def _port_in_use() -> bool:
    """端口是否已被别的进程占用（TCP 连接探测，避免 bind 的 TIME_WAIT 干扰）。"""
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", PORT)) == 0


def _open_browser_when_ready() -> None:
    """等 ``/ping`` 通了再开浏览器，免得先看到一个连不上的空白页。"""
    deadline = time.monotonic() + _READY_TIMEOUT
    while time.monotonic() < deadline:
        if _ping_ok():
            webbrowser.open(URL)
            return
        time.sleep(1)


def run(*, open_browser: bool = False) -> None:
    """启动服务；``open_browser`` 为真时在就绪后打开默认浏览器。"""
    if _ping_ok():
        log.info("检测到本地服务已在运行（%s），不重复启动", URL)
        if open_browser:
            webbrowser.open(URL)
        return
    if _port_in_use():
        log.error(
            "端口 %s 已被其他程序占用。请关闭占用进程，"
            "或设置环境变量 JOB_COURIER_PORT 换一个端口后重试。",
            PORT,
        )
        raise SystemExit(1)
    if open_browser:
        threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    log.info("本地服务启动：%s（监听 %s:%s）", URL, HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
