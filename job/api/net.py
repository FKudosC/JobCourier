"""本地服务的网络参数：端口 / 监听地址只在这里定义一处真源。

后端（``serve.py``、``app.py``）从这里取；前端 dev server 走 ``JOB_COURIER_PORT``
环境变量对齐（见 ``frontend/vite.config.ts``）。默认只监听回环地址，避免把本地
求职数据暴露到局域网；确实需要局域网调试时再显式设置 ``JOB_COURIER_HOST``。
"""

from __future__ import annotations

import os


def _env_port(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if 1 <= value <= 65535 else default


# 后端服务端口（可用 JOB_COURIER_PORT 覆盖）
PORT = _env_port("JOB_COURIER_PORT", 8000)

# 默认只监听本机回环；JOB_COURIER_HOST=0.0.0.0 时才放开到局域网
HOST = os.environ.get("JOB_COURIER_HOST") or "127.0.0.1"

# 浏览器 / 探测脚本统一走回环地址
URL = f"http://127.0.0.1:{PORT}"

# 前端 ``vite dev`` 端口（仅开发期使用）
DEV_PORT = 5173
