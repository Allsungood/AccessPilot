"""单元测试: 全局命令注册.

背景: 用户按文档执行 `accesspilot free auto` 却得到
"不是内部或外部命令" —— 因为文档里一直写 `accesspilot`, 但从来没把它
真正注册成全局命令。这类"文档与实现不一致"的问题要用测试防住。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot import cli  # noqa: E402
from accesspilot.util import Fail  # noqa: E402


class TestInstallCmd(unittest.TestCase):
    def test_writes_shim_into_given_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc = cli.cmd_install_cmd(argparse.Namespace(dir=tmp))
            self.assertEqual(rc, 0)
            shim = Path(tmp) / ("accesspilot.cmd" if sys.platform == "win32" else "accesspilot")
            self.assertTrue(shim.exists(), "包装器必须被创建")

    def test_shim_is_runnable_and_forwards_args(self) -> None:
        """包装器必须真的能跑起来并把参数传下去, 否则等于没装."""
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            cli.cmd_install_cmd(argparse.Namespace(dir=tmp))
            shim = Path(tmp) / ("accesspilot.cmd" if sys.platform == "win32" else "accesspilot")
            env = dict(os.environ)
            if sys.platform == "win32":
                result = subprocess.run(
                    [str(shim), "--version"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", env=env, timeout=90,
                )
            else:
                result = subprocess.run(
                    [str(shim), "--version"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", env=env, timeout=90,
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("AccessPilot", result.stdout)

    def test_shim_works_from_other_directory(self) -> None:
        """从任意工作目录都要能用(不能依赖 cwd)."""
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            cli.cmd_install_cmd(argparse.Namespace(dir=tmp))
            shim = Path(tmp) / ("accesspilot.cmd" if sys.platform == "win32" else "accesspilot")
            cwd = tempfile.gettempdir()
            result = subprocess.run(
                [str(shim), "status"], cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("AccessPilot", result.stdout)

    def test_reports_failure_for_unwritable_dir(self) -> None:
        with self.assertRaises(Fail):
            cli.cmd_install_cmd(argparse.Namespace(dir="Z:\\definitely\\not\\writable\\dir"))

    def test_command_is_registered(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["install-cmd"])
        self.assertTrue(callable(args.func))


class TestDocumentedCommandsExist(unittest.TestCase):
    """README 里写给用户的命令, 必须真的存在于 CLI 里."""

    def test_readme_commands_are_valid(self) -> None:
        import re

        readme = Path(__file__).resolve().parent.parent / "README.md"
        text = readme.read_text(encoding="utf-8")
        parser = cli.build_parser()
        # 抓取形如 `accesspilot xxx yyy` 的示例(仅取反引号包裹的)
        found = set(re.findall(r"`accesspilot ([a-z-]+)", text))
        choices = set(parser._subparsers._group_actions[0].choices)  # type: ignore[attr-defined]
        for cmd in sorted(found):
            if cmd.startswith("-"):
                continue
            self.assertIn(
                cmd, choices,
                f"README 里写了 `accesspilot {cmd}`, 但 CLI 中不存在这个命令",
            )


if __name__ == "__main__":
    unittest.main()
