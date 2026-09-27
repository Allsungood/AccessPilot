#!/usr/bin/env python
"""AccessPilot 测试入口（推荐用这个，不要直接用 unittest discover）。

为什么需要它: 测试隔离依赖 `tests/__init__.py` 把数据目录指向临时目录。
但 `unittest discover -s tests` 不会自动执行包的 `__init__.py`，而且这个文件
被 `Remove-Item tests\\_*.py` 之类的通配符误删过两次（`_*` 会匹配 `__init__.py`），
一旦丢失，测试就会去读写用户**真实**的 ~/.accesspilot，断言随环境漂移。

本脚本在跑测试前会强制自检并恢复该文件，从根上消除这个坑。

用法:
    python runtests.py              # 跑全部单元测试
    python runtests.py -v           # 详细输出
    python runtests.py --e2e        # 追加端到端实测(需要内核, 会临时占用端口)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"
INIT = TESTS / "__init__.py"

INIT_SOURCE = '''"""测试包初始化: 把 AccessPilot 的数据目录强制隔离到临时目录.

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
'''


def ensure_isolation() -> bool:
    """确保隔离文件存在; 返回 True 表示这次是自动恢复的."""
    if INIT.exists() and "ACCESSPILOT_HOME" in INIT.read_text(encoding="utf-8"):
        return False
    INIT.parent.mkdir(parents=True, exist_ok=True)
    INIT.write_text(INIT_SOURCE, encoding="utf-8")
    return True


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    run_e2e = "--e2e" in argv

    if ensure_isolation():
        print("[!] tests/__init__.py 缺失或损坏, 已自动恢复(测试隔离必需)")

    # 兜底: 即使隔离文件失效, 也从环境变量层面把它拦住
    os.environ.setdefault(
        "ACCESSPILOT_HOME", tempfile.mkdtemp(prefix="accesspilot-runtests-")
    )
    sys.path.insert(0, str(ROOT))
    print(f"[i] 测试数据目录: {os.environ['ACCESSPILOT_HOME']}")

    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS), pattern="test_*.py", top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2 if verbose else 1).run(suite)

    code = 0 if result.wasSuccessful() else 1
    if run_e2e:
        print("\n" + "=" * 60)
        print("[i] 端到端实测（真实内核）")
        e2e = subprocess.run(
            [sys.executable, str(TESTS / "e2e_check.py")], cwd=str(ROOT)
        )
        code = code or e2e.returncode
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
