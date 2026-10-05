"""单元测试: 链路自愈(guard)与系统代理的"写后校验".

事故背景(本项目最严重的一次稳定性缺陷):
    内核进程活得好好的, 是 **Windows 的 ProxyEnable 被外部程序改回了 0**。
    实测(probe-proxy-revert.py): 手动置 1 之后 **19.28 秒**就被改回 0, 之后
    每 30~40 秒复发一次; 而 ProxyServer / AutoConfigURL 都没被动过。

    旧代码的三处保护判据完全相同 —— "内核进程还在吗":
        cli.cmd_ensure          内核在跑 -> return 0
        cli.cmd_watchdog        内核在跑 -> 什么都不做
        process.heal_if_broken  内核在跑 -> return False
    所以三处全都看不见这种掉线, 界面显示"已连接"而实际全部直连。
    用户的原话是"红杏又断了"。

这些测试把修复锁死。**所有测试都不许碰真实注册表** —— 一律 mock 掉。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401  隔离数据目录

from accesspilot import guard, sysproxy  # noqa: E402
from accesspilot.state import load_state, save_state  # noqa: E402


def _win(enable: int = 1, server: str = "127.0.0.1:7890", pac=None) -> dict:
    return {
        "ProxyEnable": enable,
        "ProxyServer": server,
        "ProxyOverride": "localhost;127.*;<local>",
        "AutoConfigURL": pac,
    }


class TestEffective(unittest.TestCase):
    """effective() = "注册表里此刻真的是我们要的那个设置吗". """

    def setUp(self) -> None:
        self.st = load_state()
        self.st.mixed_port = 7890
        self.st.tun_enable = False

    def test_detects_flipped_off(self) -> None:
        with mock.patch.object(sysproxy, "_win_read", return_value=_win(enable=0)):
            ok, why = sysproxy.effective(self.st)
        self.assertFalse(ok, "ProxyEnable=0 必须判定为未生效")
        self.assertIn("ProxyEnable", why)

    def test_detects_other_tool_took_over(self) -> None:
        with mock.patch.object(sysproxy, "_win_read", return_value=_win(server="127.0.0.1:10809")):
            ok, why = sysproxy.effective(self.st)
        self.assertFalse(ok, "别的程序把代理改到自己的端口, 必须判定为未生效")
        self.assertIn("10809", why)

    def test_detects_pac_hijack(self) -> None:
        with mock.patch.object(
            sysproxy, "_win_read", return_value=_win(pac="http://evil.local/proxy.pac")
        ):
            ok, why = sysproxy.effective(self.st)
        self.assertFalse(ok, "PAC 脚本会静默接管代理设置, 必须判定为未生效")
        self.assertIn("PAC", why)

    def test_ok_when_exactly_ours(self) -> None:
        with mock.patch.object(sysproxy, "_win_read", return_value=_win()):
            ok, why = sysproxy.effective(self.st)
        self.assertTrue(ok)
        self.assertEqual(why, "127.0.0.1:7890")

    def test_registry_error_is_not_a_crash(self) -> None:
        with mock.patch.object(sysproxy, "_win_read", side_effect=OSError("拒绝访问")):
            ok, why = sysproxy.effective(self.st)
        self.assertFalse(ok)
        self.assertIn("读注册表失败", why)


class TestVerificationOnWrite(unittest.TestCase):
    """enable() 必须"写后校验", 不能写完就宣称成功."""

    def setUp(self) -> None:
        self.st = load_state()
        self.st.mixed_port = 7890

    def test_retries_until_effective(self) -> None:
        """第一次写没生效 -> 必须重试, 而不是就此认输或谎报成功."""
        calls = {"n": 0}

        def fake_effective(st):
            calls["n"] += 1
            return (calls["n"] >= 2, "127.0.0.1:7890")

        with mock.patch.object(sysproxy, "_win_write") as w, mock.patch.object(
            sysproxy, "_refresh_wininet"
        ), mock.patch.object(sysproxy, "backup_system_proxy"), mock.patch.object(
            sysproxy, "purge_stale_env", return_value=[]
        ), mock.patch.object(sysproxy, "effective", side_effect=fake_effective), mock.patch.object(
            sysproxy.time, "sleep"
        ):
            detail = sysproxy.enable(self.st)
        self.assertEqual(w.call_count, 2, "应当重试到生效为止")
        self.assertNotIn("未能生效", detail)

    def test_reports_failure_instead_of_lying(self) -> None:
        """三次都写不进去时必须如实报告, 不能谎报成功。"""
        with mock.patch.object(sysproxy, "_win_write"), mock.patch.object(
            sysproxy, "_refresh_wininet"
        ), mock.patch.object(sysproxy, "backup_system_proxy"), mock.patch.object(
            sysproxy, "purge_stale_env", return_value=[]
        ), mock.patch.object(
            sysproxy, "effective", return_value=(False, "ProxyEnable 被改成了 0")
        ), mock.patch.object(sysproxy.time, "sleep"):
            detail = sysproxy.enable(self.st)
        self.assertIn("未能生效", detail)
        self.assertIn("ProxyEnable", detail)

    def test_does_not_write_env_vars_by_default(self) -> None:
        """回归: 默认**不得**往 HKCU\\Environment 写代理变量.

        那是用户级、持久、对之后每一个进程都生效的副作用 —— 会劫持用户终端里
        所有工具的网络请求, 而用户完全看不出是谁干的。不该是"开个代理"的
        默认后果。需要的人显式执行 `accesspilot proxy env`。
        """
        with mock.patch.object(sysproxy, "_win_write"), mock.patch.object(
            sysproxy, "_refresh_wininet"
        ), mock.patch.object(sysproxy, "backup_system_proxy"), mock.patch.object(
            sysproxy, "effective", return_value=(True, "127.0.0.1:7890")
        ), mock.patch.object(sysproxy, "set_env_proxy") as setenv, mock.patch.object(
            sysproxy, "purge_stale_env", return_value=[]
        ), mock.patch.object(sysproxy.time, "sleep"):
            sysproxy.enable(self.st)
        setenv.assert_not_called()

    def test_opt_in_still_writes_env_vars(self) -> None:
        """显式要求时仍然要写(保留给 `accesspilot proxy env`)。"""
        with mock.patch.object(sysproxy, "_win_write"), mock.patch.object(
            sysproxy, "_refresh_wininet"
        ), mock.patch.object(sysproxy, "backup_system_proxy"), mock.patch.object(
            sysproxy, "effective", return_value=(True, "127.0.0.1:7890")
        ), mock.patch.object(sysproxy, "set_env_proxy") as setenv, mock.patch.object(
            sysproxy.time, "sleep"
        ):
            sysproxy.enable(self.st, with_env=True)
        setenv.assert_called_once()


class TestPurgeStaleEnv(unittest.TestCase):
    """只清我们自己的残留值, 绝不动用户自己的代理设置."""

    def _run(self, table: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        removed: list[str] = []
        state = dict(table)

        def fake_read(name):
            return state.get(name)

        def fake_set(name, value):
            if value is None:
                removed.append(name)
                state.pop(name, None)
            else:
                state[name] = value

        with mock.patch.object(sysproxy, "_read_user_env", side_effect=fake_read), \
             mock.patch.object(sysproxy, "_set_user_env", side_effect=fake_set):
            got = sysproxy.purge_stale_env(load_state())
        return got, state

    def test_removes_our_own_leaked_values(self) -> None:
        removed, left = self._run({
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "ALL_PROXY": "http://localhost:7890",
            "no_proxy": "localhost,127.0.0.1,::1",
        })
        self.assertCountEqual(
            removed, ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"],
            "指向本机内核端口的残留值必须被清掉",
        )
        self.assertNotIn("no_proxy", removed, "NO_PROXY 不是代理指向, 不该删")

    def test_never_touches_a_foreign_proxy(self) -> None:
        """用户自己在用的公司代理/别的工具, 一个都不许动。"""
        foreign = {
            "HTTP_PROXY": "http://proxy.corp.example:8080",
            "HTTPS_PROXY": "http://127.0.0.1:10809",
            "ALL_PROXY": "socks5://127.0.0.1:1080",
        }
        removed, left = self._run(foreign)
        self.assertEqual(removed, [], "不属于我们的值必须原样保留")
        self.assertEqual(left, foreign)

    def test_empty_value_is_left_alone(self) -> None:
        removed, left = self._run({"HTTP_PROXY": ""})
        self.assertEqual(removed, [])
        self.assertEqual(left, {"HTTP_PROXY": ""})


class TestGuardCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.st = load_state()
        self.st.mixed_port = 7890
        self.st.tun_enable = False
        self.st.system_proxy_on = True
        save_state(self.st)

    def test_unhealthy_when_kernel_alive_but_proxy_flipped_off(self) -> None:
        """这就是真实掉线现场: 内核活着(旧代码因此全部放行), 代理却是关的."""
        with mock.patch.object(guard.process, "is_running", return_value=True), \
             mock.patch.object(sysproxy, "effective", return_value=(False, "ProxyEnable 被改成了 0")):
            res = guard.check(self.st)
        self.assertTrue(res["kernel"])
        self.assertFalse(res["effective"])
        self.assertFalse(res["healthy"], "必须判定为不健康, 否则永远不会被修")

    def test_healthy_when_kernel_alive_and_proxy_ours(self) -> None:
        with mock.patch.object(guard.process, "is_running", return_value=True), \
             mock.patch.object(sysproxy, "effective", return_value=(True, "127.0.0.1:7890")):
            res = guard.check(self.st)
        self.assertTrue(res["healthy"])

    def test_kernel_down_is_unhealthy(self) -> None:
        with mock.patch.object(guard.process, "is_running", return_value=False):
            res = guard.check(self.st)
        self.assertFalse(res["healthy"])
        self.assertIn("内核未运行", res["reason"])

    def test_tun_mode_does_not_depend_on_the_registry_switch(self) -> None:
        """TUN 在网络层接管, 本来就不该占用系统代理设置 —— 所以不看注册表."""
        self.st.tun_enable = True
        with mock.patch.object(guard.process, "is_running", return_value=True), \
             mock.patch.object(sysproxy, "effective", return_value=(False, "ProxyEnable 被改成了 0")):
            res = guard.check(self.st)
        self.assertTrue(res["effective"], "TUN 模式下注册表开关与链路无关")
        self.assertEqual(res["mode"], "tun")


class TestGuardRepair(unittest.TestCase):
    def setUp(self) -> None:
        self.st = load_state()
        self.st.mixed_port = 7890
        self.st.tun_enable = False
        self.st.system_proxy_on = True
        save_state(self.st)

    def test_reapplies_system_proxy(self) -> None:
        with mock.patch.object(guard.process, "is_running", return_value=True), \
             mock.patch.object(guard, "port_serving", return_value=True), \
             mock.patch.object(sysproxy, "effective",
                               side_effect=[(False, "ProxyEnable 被改成了 0"), (True, "ok")]), \
             mock.patch.object(sysproxy, "enable", return_value="ok") as enable:
            res = guard.repair(self.st, quiet=True)
        enable.assert_called_once()
        self.assertIn("重贴系统代理", res["repairs"])
        self.assertTrue(res["healthy"])

    def test_detaches_proxy_instead_of_pointing_at_a_dead_port(self) -> None:
        """内核在跑但端口没人监听 -> 必须**摘掉**系统代理, 绝不能指过去。

        真实故障: `config set-port` 把新端口写进 state.json 而内核仍在旧端口服务,
        guard 若无条件按 state 改系统代理, 10 秒内所有浏览器就打不开网页了 ——
        而且事后 effective() 还会显示"已生效", 没有任何东西会纠正它。
        """
        with mock.patch.object(guard.process, "is_running", return_value=True), \
             mock.patch.object(guard, "port_serving", return_value=False), \
             mock.patch.object(sysproxy, "effective",
                               return_value=(False, "ProxyEnable 被改成了 0")), \
             mock.patch.object(sysproxy, "enable") as enable, \
             mock.patch.object(sysproxy, "disable", return_value="closed") as disable:
            res = guard.repair(self.st, quiet=True)
        enable.assert_not_called()
        disable.assert_called_once()
        self.assertIn("内核端口未监听, 已摘掉系统代理", res["repairs"])
        self.assertFalse(load_state().system_proxy_on)

    def test_allow_restart_false_never_restarts_kernel(self) -> None:
        """看门狗必须用这个模式: process.start() 会 stop_watchdog(),
        那等于让看门狗在执行修复的过程中把自己杀掉。"""
        with mock.patch.object(guard.process, "is_running", return_value=False), \
             mock.patch.object(guard.process, "start") as start:
            res = guard.repair(self.st, quiet=True, allow_restart=False)
        start.assert_not_called()
        self.assertEqual(res["repairs"], [])

    def test_does_not_restart_kernel_when_user_asked_off(self) -> None:
        """用户主动点过「关闭」时, 保活/自愈都不得把它拉回来。"""
        with mock.patch.object(guard.process, "is_running", return_value=False), \
             mock.patch.object(guard.process, "start") as start, \
             mock.patch("accesspilot.intent.user_wants_off", return_value=True):
            guard.repair(self.st, quiet=True)
        start.assert_not_called()

    def test_restarts_kernel_when_it_is_gone_and_user_wants_it(self) -> None:
        with mock.patch.object(guard.process, "is_running", return_value=False), \
             mock.patch.object(guard.process, "start", return_value={}) as start, \
             mock.patch("accesspilot.intent.user_wants_off", return_value=False):
            res = guard.repair(self.st, quiet=True)
        start.assert_called_once()
        self.assertIn("重启内核", res["repairs"])

    def test_healthy_link_is_left_alone(self) -> None:
        with mock.patch.object(guard.process, "is_running", return_value=True), \
             mock.patch.object(sysproxy, "effective", return_value=(True, "127.0.0.1:7890")), \
             mock.patch.object(sysproxy, "enable") as enable:
            res = guard.repair(self.st, quiet=True)
        enable.assert_not_called()
        self.assertTrue(res["healthy"])
        self.assertEqual(res["repairs"], [])


class TestFightDetection(unittest.TestCase):
    """有外部程序反复关我们的代理时, 要能算出来并告诉用户."""

    def test_counts_recent_repairs_only(self) -> None:
        import time

        with mock.patch.object(guard, "history", return_value=[
            {"t": time.time() - 10, "kind": "sysproxy_reapply"},
            {"t": time.time() - 20, "kind": "sysproxy_reapply"},
            {"t": time.time() - 9000, "kind": "sysproxy_reapply"},  # 太旧, 不算
            {"t": time.time() - 5, "kind": "kernel_restart"},        # 别的类型, 不算
        ]):
            self.assertEqual(guard.flap_count(window=3600), 2)
            self.assertFalse(guard.fighting(window=3600))

    def test_fighting_threshold(self) -> None:
        import time

        with mock.patch.object(guard, "history", return_value=[
            {"t": time.time() - i, "kind": "sysproxy_reapply"} for i in range(5)
        ]):
            self.assertTrue(guard.fighting(window=3600))

    def test_record_is_capped_and_readable(self) -> None:
        from accesspilot import paths

        paths.ensure_dirs()
        (paths.cache_dir() / guard.HISTORY_FILE).unlink(missing_ok=True)
        for i in range(guard.HISTORY_MAX + 25):
            guard.record("sysproxy_reapply", f"第 {i} 次")
        items = guard.history(limit=guard.HISTORY_MAX)
        self.assertLessEqual(len(items), guard.HISTORY_MAX, "历史文件不能无限增长")
        self.assertEqual(items[-1]["detail"], f"第 {guard.HISTORY_MAX + 24} 次")


class TestKeepaliveIsNotBlind(unittest.TestCase):
    """回归: 保活任务(cmd_ensure)原来只问"内核还在吗", 于是永远修不好掉线。

    它是每 5 分钟跑一次的计划任务 —— 恰恰是最该发现问题的那一处。
    """

    def test_ensure_checks_health_even_when_kernel_is_running(self) -> None:
        from accesspilot import cli

        args = type("A", (), {"force": False, "deep": False, "sysproxy": False})()
        with mock.patch.object(cli.process, "is_running", return_value=True), \
             mock.patch.object(cli.guard, "repair", return_value={"repairs": ["重贴系统代理"]}) as rep:
            rc = cli.cmd_ensure(args)
        self.assertEqual(rc, 0)
        rep.assert_called_once()

    def test_ensure_does_not_touch_proxy_when_user_asked_off(self) -> None:
        from accesspilot import cli

        args = type("A", (), {"force": False, "deep": False, "sysproxy": False})()
        with mock.patch.object(cli.process, "is_running", return_value=False), \
             mock.patch.object(cli.process, "start") as start, \
             mock.patch("accesspilot.intent.user_wants_off", return_value=True):
            rc = cli.cmd_ensure(args)
        self.assertEqual(rc, 0)
        start.assert_not_called()


class TestBackupLifecycle(unittest.TestCase):
    def test_restore_deletes_backup_so_it_cannot_go_stale(self) -> None:
        """恢复后必须删掉快照。

        否则它会一直陈旧下去: 用户在我们关闭代理之后自己开的代理, 会被下一次
        disable 用这份旧快照覆盖掉。
        """
        from accesspilot import paths
        from accesspilot.util import json_dump

        paths.ensure_dirs()
        path = paths.cache_dir() / sysproxy._BACKUP
        json_dump(path, _win(enable=0, server="", pac=None))
        with mock.patch.object(sysproxy, "_win_write"), mock.patch.object(
            sysproxy, "_refresh_wininet"
        ):
            sysproxy.restore_system_proxy()
        self.assertFalse(path.exists(), "快照用过一次就该删掉")

    def test_does_not_snapshot_our_own_configuration(self) -> None:
        """上一轮异常退出把我们的配置留在了注册表里 -> 绝不能拿它当"原始状态",
        否则"关闭"会变成"保持开启"。"""
        from accesspilot import paths

        paths.ensure_dirs()
        path = paths.cache_dir() / sysproxy._BACKUP
        path.unlink(missing_ok=True)
        with mock.patch.object(sysproxy, "_win_read", return_value=_win(enable=1)):
            sysproxy.backup_system_proxy()
        self.assertFalse(path.exists(), "当前已经是我们的配置, 不该拍快照")


if __name__ == "__main__":
    unittest.main()
