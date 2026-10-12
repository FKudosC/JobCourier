"""JobCourier 启动入口：本地 Web 服务（API + 前端静态文件，8000 端口）。

``start-jobcourier.cmd`` 与 ``uv run python -m job`` 都走这里。
"""

from __future__ import annotations

from job.api.serve import run

if __name__ == "__main__":
    run()
