"""测试包初始化: 把 AccessPilot 的数据目录强制隔离到临时目录.

由 runtests.py 自动生成/恢复 —— 详见该脚本的说明。
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

TEST_HOME = tempfile.mkdtemp(prefix="accesspilot-tests-")
os.environ["ACCESSPILOT_HOME"] = TEST_HOME

atexit.register(lambda: shutil.rmtree(TEST_HOME, ignore_errors=True))
