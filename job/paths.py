"""用户数据目录。

运行期产生的用户信息（数据库、简历、Chrome 登录态、日志）统一放在项目根目录的
``user_data/`` 下，方便集中管理、整体备份，也不会被提交到仓库。
"""

from __future__ import annotations

import os
from pathlib import Path

# 项目根目录（job/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resolve_data_dir() -> Path:
    """默认用项目内的 ``user_data/``，可用 ``JOB_COURIER_DATA_DIR`` 覆盖。"""
    override = os.environ.get("JOB_COURIER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return PROJECT_ROOT / "user_data"


USER_DATA_DIR = _resolve_data_dir()
