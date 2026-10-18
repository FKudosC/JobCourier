"""测试全局兜底：跑测试时把用户数据目录指到临时目录，绝不碰真实的 ``user_data/``。

``job.paths.USER_DATA_DIR`` 以及基于它的那些常量（简历目录、寸照目录、数据库、
Chrome profile、后端日志）都是在模块导入时就算好的，所以必须在 ``job.*`` 被导入
之前把环境变量设好。conftest.py 比测试模块先加载，正好赶得上。

不这么做的话，像上传简历 / 寸照这类用例会把 ``resume.pdf``、``me.jpg`` 直接写进
用户真实的 ``user_data/``——跑一次测试就覆盖掉用户自己的文件。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_SANDBOX = Path(tempfile.mkdtemp(prefix="job-courier-tests-"))
os.environ["JOB_COURIER_DATA_DIR"] = str(_SANDBOX)


@pytest.fixture(scope="session", autouse=True)
def _isolated_user_data():
    """确认整套测试跑在沙箱里，跑完把沙箱删掉。"""
    from job.paths import USER_DATA_DIR

    assert USER_DATA_DIR == _SANDBOX, "测试必须跑在临时数据目录里，别碰真实的 user_data/"
    yield _SANDBOX
    shutil.rmtree(_SANDBOX, ignore_errors=True)
