"""以本地 Web 服务方式启动 JobCourier，并在就绪后打开浏览器。

用法（项目根目录）::

    uv run python -m job
"""

from __future__ import annotations

from job.api.serve import run

if __name__ == "__main__":
    run(open_browser=True)
