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


class TestPruneNeverEmptiesProfile(unittest.TestCase):
    """回归: 每小时的自动刷新不能把用户的节点列表清空。

    prune_profile 原来只校验了"测速结果不为空", 没校验"结果里真的有节点属于
    这个配置档"。当测速结果里的名字一个都不在配置档里时, 它会写出 sub.proxies=[],
    用户的节点就被清空了 —— 而且是计划任务干的, 用户完全无从知道。
    """

    def _sub(self):
        from accesspilot.subscription import Subscription

        return Subscription(name="free", proxies=[{
            "name": "节点A", "type": "ss", "server": "1.1.1.1", "port": 443,
            "cipher": "aes-128-gcm", "password": "x",
        }])

    def test_refuses_to_write_empty_profile(self) -> None:
        from accesspilot import freenodes
        from accesspilot.util import Fail

        saved: list = []
        with mock.patch.object(freenodes.sub_mod, "load_profile", return_value=self._sub()), \
             mock.patch.object(freenodes.sub_mod, "save_profile",
                               side_effect=lambda s: saved.append(s)):
            with self.assertRaises(Fail):
                freenodes.prune_profile("free", {"一个都不存在的节点": 10})
        self.assertEqual(saved, [], "绝不能写出空配置档")

    def test_normal_prune_still_works(self) -> None:
        from accesspilot import freenodes

        saved: list = []
        with mock.patch.object(freenodes.sub_mod, "load_profile", return_value=self._sub()), \
             mock.patch.object(freenodes.sub_mod, "save_profile",
                               side_effect=lambda s: saved.append(s)):
            before, after = freenodes.prune_profile("free", {"节点A": 20})
        self.assertEqual((before, after), (1, 1))
        self.assertEqual(len(saved), 1, "正常情况下应当写入")


class TestAccelDoesNotKillUnrelatedProcesses(unittest.TestCase):
    """回归: 加速器启动时不能按"进程名像 python 就杀"。

    旧实现在端口被占时会 taskkill 掉任何名字以 python/accesspilot 开头的进程 ——
    用户自己跑着的 Python 程序、另一个 accesspilot 命令都可能被无声杀掉,
    而那段代码的注释写的却是"给出明确报错"。
    """

    def test_start_accel_refuses_foreign_listener(self) -> None:
        from accesspilot import process
        from accesspilot.util import Fail

        with mock.patch.object(process, "accel_running", return_value=False), \
             mock.patch.object(process, "_listeners", return_value=[999999]), \
             mock.patch.object(process, "_process_name", return_value="python.exe"), \
             mock.patch.object(process, "_read_accel_pid", return_value=None), \
             mock.patch.object(process, "_kill_pid") as kill:
            with self.assertRaises(Fail):
                process.start_accel(load_state())
        kill.assert_not_called()

    def test_stop_accel_wont_kill_a_pid_that_is_not_listening(self) -> None:
        from accesspilot import process

        with mock.patch.object(process, "_read_accel_pid",
                               return_value={"pid": 999999, "port": 7895}), \
             mock.patch.object(process, "_pid_alive", return_value=True), \
             mock.patch.object(process, "_listeners", return_value=[]), \
             mock.patch.object(process, "_kill_pid") as kill:
            process.stop_accel()
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
