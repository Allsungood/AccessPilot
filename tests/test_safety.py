"""单元测试: 断网防护(self-heal)与看门狗.

事故背景: 内核崩溃/被杀后, 如果系统代理仍指向 127.0.0.1:7890, 这台机器上
**所有**网站都打不开(连本该直连的国内站点也一样), 因为浏览器把流量全丢给了
一个已经不存在的端口。这类"工具把用户网络搞挂"的故障必须被测试锁死。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401  隔离数据目录

from accesspilot import process  # noqa: E402
from accesspilot.state import load_state, save_state  # noqa: E402


class TestHealIfBroken(unittest.TestCase):
    def setUp(self) -> None:
        st = load_state()
        st.system_proxy_on = False
        save_state(st)

    def _state_with_proxy_on(self) -> None:
        st = load_state()
        st.system_proxy_on = True
        save_state(st)

    def test_heals_when_core_dead_and_proxy_on(self) -> None:
        """内核已死 + 系统代理开着 -> 必须还原, 否则用户整台机器断网."""
        self._state_with_proxy_on()
        disabled: list[bool] = []
        with mock.patch.object(process, "is_running", return_value=False), mock.patch.object(
            process.sysproxy, "status", return_value=(True, "127.0.0.1:7890")
        ), mock.patch.object(
            process.sysproxy, "disable", side_effect=lambda st: disabled.append(True)
            or "已关闭 Windows 系统代理"
        ):
            healed = process.heal_if_broken(quiet=True)
        self.assertTrue(healed, "应当执行还原")
        self.assertEqual(disabled, [True], "必须真的关闭系统代理")
        self.assertFalse(load_state().system_proxy_on, "状态标记要同步复位")

    def test_no_action_when_core_running_and_proxy_effective(self) -> None:
        """内核活着、代理设置也确实是我们写的 -> 什么都不用做."""
        self._state_with_proxy_on()
        with mock.patch.object(process, "is_running", return_value=True), mock.patch.object(
            process.sysproxy, "effective", return_value=(True, "127.0.0.1:7890")
        ), mock.patch.object(process.sysproxy, "disable") as disable, mock.patch.object(
            process.sysproxy, "enable"
        ) as enable:
            healed = process.heal_if_broken(quiet=True)
        self.assertFalse(healed)
        disable.assert_not_called()
        enable.assert_not_called()
        self.assertTrue(load_state().system_proxy_on)

    def test_repairs_when_core_alive_but_proxy_flipped_off(self) -> None:
        """内核活着、但 ProxyEnable 被外部程序改成了 0 -> 必须贴回去.

        这是"红杏又断了"的真身, 也是旧代码漏掉的方向: 旧实现在
        `if is_running(): return False` 处直接返回, 于是永远看不见这种掉线。
        实测: 手动置 1 之后 19 秒就被改回 0。
        """
        self._state_with_proxy_on()
        with mock.patch.object(process, "is_running", return_value=True), mock.patch.object(
            process.sysproxy, "effective",
            side_effect=[(False, "ProxyEnable 被改成了 0(外部程序干的)"),
                         (True, "127.0.0.1:7890")],
        ), mock.patch.object(
            process.sysproxy, "enable", return_value="Windows 系统代理 -> 127.0.0.1:7890"
        ) as enable, mock.patch.object(process.sysproxy, "disable") as disable:
            healed = process.heal_if_broken(quiet=True)
        self.assertTrue(healed, "应当执行修复")
        enable.assert_called_once()
        disable.assert_not_called()
        self.assertTrue(load_state().system_proxy_on)

    def test_no_action_when_state_flag_off(self) -> None:
        with mock.patch.object(process, "is_running", return_value=False), mock.patch.object(
            process.sysproxy, "disable"
        ) as disable:
            healed = process.heal_if_broken(quiet=True)
        self.assertFalse(healed)
        disable.assert_not_called()

    def test_clears_stale_flag_when_proxy_already_off(self) -> None:
        """系统代理其实已经关了, 只是状态标记还是 true -> 只复位标记即可."""
        self._state_with_proxy_on()
        with mock.patch.object(process, "is_running", return_value=False), mock.patch.object(
            process.sysproxy, "status", return_value=(False, "")
        ), mock.patch.object(process.sysproxy, "disable") as disable:
            healed = process.heal_if_broken(quiet=True)
        self.assertFalse(healed)
        disable.assert_not_called()
        self.assertFalse(load_state().system_proxy_on)

    def test_idempotent(self) -> None:
        self._state_with_proxy_on()
        with mock.patch.object(process, "is_running", return_value=False), mock.patch.object(
            process.sysproxy, "status", return_value=(True, "127.0.0.1:7890")
        ), mock.patch.object(process.sysproxy, "disable", return_value="ok"):
            self.assertTrue(process.heal_if_broken(quiet=True))
            self.assertFalse(process.heal_if_broken(quiet=True), "第二次不应重复处理")


class TestWatchdogLifecycle(unittest.TestCase):
    def test_stop_watchdog_is_safe_without_pidfile(self) -> None:
        from accesspilot import paths

        paths.watchdog_pid_file().unlink(missing_ok=True)
        process.stop_watchdog()  # 不应抛异常
        self.assertFalse(paths.watchdog_pid_file().exists())

    def test_stop_watchdog_ignores_garbage_pidfile(self) -> None:
        from accesspilot import paths

        paths.ensure_dirs()
        paths.watchdog_pid_file().write_text("{ 这不是 JSON", encoding="utf-8")
        process.stop_watchdog()
        self.assertFalse(paths.watchdog_pid_file().exists())

    def test_watchdog_command_is_registered_but_hidden(self) -> None:
        from accesspilot.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["_watchdog", "--pid", "1234"])
        self.assertEqual(args.pid, 1234)
        self.assertTrue(callable(args.func))


class TestFreeDoesNotHijackProxy(unittest.TestCase):
    """回归: free auto 曾经擅自打开系统代理, 导致用户整台机器断网."""

    def test_free_auto_passes_system_proxy_false(self) -> None:
        import inspect

        from accesspilot import cli

        src = inspect.getsource(cli.cmd_free)
        self.assertIn(
            "system_proxy=False", src,
            "free auto 必须显式传 system_proxy=False, 不得改动用户的系统代理",
        )
        self.assertNotIn("system_proxy=None", src)


if __name__ == "__main__":
    unittest.main()
