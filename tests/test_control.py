"""contract 层单元测试: accesspilot/control.py.

GUI 只认这一层, 所以这一层错了整个界面就是错的。测试全部离线(打桩),
重点锁住三类真实踩过的坑:

  1. **snapshot() 绝不能拉全量 /proxies**。免费节点池有六千个节点时那份
     响应有几 MB, 实测把一次快照从 <20ms 顶到 947ms —— 界面每 1~2 秒
     轮询一次, 这样写界面必卡死。这条用"断言 api.proxies 根本没被调用"
     来锁, 比测时间更稳(不受机器负载影响)。

  2. **"当前节点"不能是组名**。🚀 节点选择 默认指向 ♻️ 自动选择, 而
     ♻️ 自动选择 才指向真节点。直接把 `now` 显示出来, 用户会看到
     "当前节点: ♻️ 自动选择" —— 毫无意义。

  3. **可预期的失败不抛异常**。界面代码不该 try/except 满天飞;
     内核没起来、节点池空、API 超时, 都应该是带 error 字段的返回值。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot import control, rules  # noqa: E402


def _state(**kw):
    st = mock.MagicMock()
    st.active_profile = "free"
    st.mixed_port = 7890
    st.tun_enable = False
    st.system_proxy_on = True
    st.last_start = 0.0
    st.selected = {}
    for k, v in kw.items():
        setattr(st, k, v)
    return st


class SnapshotContract(unittest.TestCase):
    """snapshot() 是界面轮询的热路径, 契约最严。"""

    def setUp(self) -> None:
        self.st = _state()
        for target, value in (
            ("accesspilot.control.load_state", lambda: self.st),
            ("accesspilot.control.process.is_running", lambda: True),
            ("accesspilot.control.sysproxy.status", lambda: (True, "127.0.0.1:7890")),
            ("accesspilot.control.api.mode", lambda _st: "rule"),
            ("accesspilot.control.api.version", lambda _st: "1.19.31"),
            ("accesspilot.control._task_exists", lambda _n: True),
            ("accesspilot.control._load_ai_cache", lambda: control.AiStatus()),
        ):
            p = mock.patch(target, side_effect=value)
            self.addCleanup(p.stop)
            p.start()

    def test_never_fetches_all_proxies(self) -> None:
        """锁住 947ms 那个坑: 快照只准单组查询。"""
        with mock.patch("accesspilot.control.api.proxies") as all_proxies, \
             mock.patch("accesspilot.control.api.proxy",
                        return_value={"now": "🇰🇷 韩国 KT", "all": ["a", "b"]}):
            control.snapshot()
        all_proxies.assert_not_called()

    def test_resolves_group_chain_to_real_node(self) -> None:
        """🚀 节点选择 -> ♻️ 自动选择 -> 真节点, 界面要显示真节点。"""
        chain = {
            rules.G_SELECT: {"now": rules.G_AUTO, "all": ["x"] * 5},
            rules.G_AUTO: {"now": "🇰🇷 韩国 KT", "all": ["x"] * 5},
        }
        with mock.patch("accesspilot.control.api.proxies") as all_proxies, \
             mock.patch("accesspilot.control.api.proxy",
                        side_effect=lambda _st, n: chain.get(n, {})):
            all_proxies.return_value = {}
            s = control.snapshot()
        self.assertEqual(s.node, "🇰🇷 韩国 KT")
        self.assertNotIn("自动选择", s.node)

    def test_resolve_does_not_loop_forever_on_cycle(self) -> None:
        """策略组互相指向(环)时必须停下, 不能把界面挂死。"""
        chain = {rules.G_SELECT: {"now": rules.G_AI},
                 rules.G_AI: {"now": rules.G_SELECT}}
        with mock.patch("accesspilot.control.api.proxy",
                        side_effect=lambda _st, n: chain.get(n, {})):
            node, cand = control._resolve_current(self.st, rules.G_SELECT)
        self.assertEqual(node, "")

    def test_memoizes_repeated_group_lookups(self) -> None:
        """🤖 AI 服务 与 🚀 节点选择 常指向同一条链, 不该走两遍。"""
        calls: list[str] = []

        def fake(_st, name):
            calls.append(name)
            return {rules.G_SELECT: {"now": rules.G_AUTO},
                    rules.G_AUTO: {"now": "节点X"},
                    rules.G_AI: {"now": rules.G_SELECT}}.get(name, {})

        with mock.patch("accesspilot.control.api.proxy", side_effect=fake):
            s = control.snapshot()
        self.assertEqual(s.node, "节点X")
        self.assertEqual(s.ai_node, "节点X")
        # 三个组名各查一次即可; 没有记忆化的话 G_AUTO 会被查两遍
        self.assertEqual(calls.count(rules.G_AUTO), 1, f"实际调用序列: {calls}")

    def test_connected_requires_traffic_actually_taken_over(self) -> None:
        """内核在跑但既没开系统代理也没开 TUN = 没连上, 不能骗用户。"""
        with mock.patch("accesspilot.control.sysproxy.status",
                        return_value=(False, "")), \
             mock.patch("accesspilot.control.api.proxy", return_value={}):
            s = control.snapshot()
        self.assertTrue(s.running)
        self.assertFalse(s.connected)

    def test_core_down_does_not_raise(self) -> None:
        with mock.patch("accesspilot.control.process.is_running", return_value=False), \
             mock.patch("accesspilot.control.sysproxy.status", return_value=(False, "")):
            s = control.snapshot()
        self.assertFalse(s.running)
        self.assertEqual(s.error, "")

    def test_api_failure_does_not_raise(self) -> None:
        with mock.patch("accesspilot.control.api.mode", side_effect=OSError("boom")), \
             mock.patch("accesspilot.control.api.proxy", side_effect=OSError("boom")):
            s = control.snapshot()
        self.assertEqual(s.mode, "rule")
        self.assertEqual(s.node, "")


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        control.invalidate_cache()

    def test_ttl_reuses_value(self) -> None:
        calls = []
        fn = lambda: (calls.append(1), 42)[1]  # noqa: E731
        self.assertEqual(control._cached("k", 60.0, fn), 42)
        self.assertEqual(control._cached("k", 60.0, fn), 42)
        self.assertEqual(len(calls), 1, "TTL 内不该重复求值")

    def test_expired_entry_refetches(self) -> None:
        calls = []
        fn = lambda: (calls.append(1), 7)[1]  # noqa: E731
        control._cached("k", 0.0, fn)
        control._cached("k", 0.0, fn)
        self.assertEqual(len(calls), 2)

    def test_failure_falls_back_to_last_value(self) -> None:
        """取不到新值时要退回旧值, 不能让界面突然变空。"""
        control._cached("k", 0.0, lambda: "old")

        def boom():
            raise OSError("down")

        self.assertEqual(control._cached("k", 0.0, boom), "old")


class NodeListTests(unittest.TestCase):
    def test_filters_groups_and_sorts(self) -> None:
        fake = {
            rules.G_SELECT: {"type": "Selector", "now": "慢节点"},
            "GLOBAL": {"type": "Selector"},
            "REJECT": {"type": "Reject"},
            "慢节点": {"type": "Shadowsocks", "history": [{"delay": 900}]},
            "快节点": {"type": "Shadowsocks", "history": [{"delay": 20}]},
            "死节点": {"type": "Shadowsocks", "history": [{"delay": 0}]},
            "无记录": {"type": "Shadowsocks"},
        }
        st = _state()
        with mock.patch("accesspilot.control.load_state", return_value=st), \
             mock.patch("accesspilot.control.api.proxies", return_value=fake):
            nodes = control.list_nodes()
        names = [n.name for n in nodes]
        self.assertEqual(set(names), {"慢节点", "快节点", "死节点", "无记录"})
        self.assertNotIn(rules.G_SELECT, names)
        self.assertEqual(names[0], "慢节点", "当前节点要排最前")
        self.assertEqual(names[1], "快节点", "其余按延迟")
        self.assertEqual(nodes[1].latency_ms, 20)
        self.assertFalse([n for n in nodes if n.name == "无记录"][0].alive)

    def test_alive_only(self) -> None:
        fake = {"A": {"type": "Shadowsocks", "history": [{"delay": 0}]},
                "B": {"type": "Shadowsocks", "history": [{"delay": 30}]}}
        st = _state()
        with mock.patch("accesspilot.control.load_state", return_value=st), \
             mock.patch("accesspilot.control.api.proxies", return_value=fake):
            nodes = control.list_nodes(alive_only=True)
        self.assertEqual([n.name for n in nodes], ["B"])

    def test_limit(self) -> None:
        fake = {f"n{i}": {"type": "Shadowsocks", "history": [{"delay": i + 1}]}
                for i in range(50)}
        st = _state()
        with mock.patch("accesspilot.control.load_state", return_value=st), \
             mock.patch("accesspilot.control.api.proxies", return_value=fake):
            self.assertEqual(len(control.list_nodes(limit=10)), 10)

    def test_api_down_returns_empty_not_raise(self) -> None:
        st = _state()
        with mock.patch("accesspilot.control.load_state", return_value=st), \
             mock.patch("accesspilot.control.api.proxies", side_effect=OSError("x")):
            self.assertEqual(control.list_nodes(), [])


class FailSoftTests(unittest.TestCase):
    """契约: 可预期的失败返回带 error 的结果, 不抛异常。"""

    def setUp(self) -> None:
        self.st = _state()
        p = mock.patch("accesspilot.control.load_state", return_value=self.st)
        self.addCleanup(p.stop)
        p.start()
        # _fail() 内部会再调一次 snapshot(), 这里让它保持简单
        p2 = mock.patch("accesspilot.control.process.is_running", return_value=False)
        self.addCleanup(p2.stop)
        p2.start()
        p3 = mock.patch("accesspilot.control.sysproxy.status", return_value=(False, ""))
        self.addCleanup(p3.stop)
        p3.start()

    def test_turn_on_failure_returns_status(self) -> None:
        with mock.patch("accesspilot.control.process.start",
                        side_effect=RuntimeError("内核没装")):
            s = control.turn_on()
        self.assertIn("内核没装", s.error)

    def test_turn_off_failure_returns_status(self) -> None:
        with mock.patch("accesspilot.control.process.stop",
                        side_effect=RuntimeError("停不掉")):
            s = control.turn_off()
        self.assertIn("停不掉", s.error)

    def test_set_mode_rejects_unknown(self) -> None:
        s = control.set_mode("turbo")
        self.assertIn("未知模式", s.error)

    def test_set_mode_without_core_is_explained(self) -> None:
        s = control.set_mode("global")
        self.assertIn("内核未运行", s.error)

    def test_select_node_without_core(self) -> None:
        s = control.select_node("某节点")
        self.assertIn("内核未运行", s.error)

    def test_verify_ai_without_core(self) -> None:
        ai = control.verify_ai()
        self.assertFalse(ai.ok)
        self.assertIn("内核未运行", ai.detail)

    def test_health_state_absent_module_is_safe(self) -> None:
        """health.py 还没落地时界面也不能崩。"""
        state = control.health_state()
        self.assertIsInstance(state, dict)
        self.assertIn("enabled", state)

    def test_run_bg_routes_exception_to_callback(self) -> None:
        """后台线程里抛异常不能静默吞掉, 也不能炸掉进程。"""
        import threading

        got: list[BaseException] = []
        done = threading.Event()

        def on_error(e: BaseException) -> None:
            got.append(e)
            done.set()

        control.run_bg(lambda: 1 / 0, on_error=on_error)
        self.assertTrue(done.wait(5), "on_error 没被调用")
        self.assertIsInstance(got[0], ZeroDivisionError)

    def test_run_bg_returns_result(self) -> None:
        import threading

        box: list[int] = []
        done = threading.Event()
        control.run_bg(lambda a, b: a + b, 2, 3,
                       on_done=lambda r: (box.append(r), done.set()))
        self.assertTrue(done.wait(5))
        self.assertEqual(box, [5])


class BrandTests(unittest.TestCase):
    def test_brand_constants(self) -> None:
        self.assertEqual(control.BRAND_NAME, "红杏")
        self.assertTrue(control.BRAND_VERSION)

    def test_modes_cover_kernel_modes(self) -> None:
        from accesspilot import api

        self.assertEqual(set(control.MODES), set(api.MODES))
        self.assertEqual(set(control.MODES), {"rule", "global", "direct"})


    def test_excludes_kernel_builtin_entries(self) -> None:
        """内核内建的特殊出口不是节点, 不能出现在列表里。

        真实事故: PASS-RULE 的 type 是 "PassRule"(不是 "Pass"), 过滤名单漏了它,
        于是界面上多出一个看得见、点不动的死条目 —— 双击它内核回
        "Selector update error: proxy not exist" (HTTP 400)。
        """
        fake = {
            "PASS-RULE": {"type": "PassRule"},
            "REJECT-DROP": {"type": "RejectDrop"},
            "PASS": {"type": "Pass"},
            "REJECT": {"type": "Reject"},
            "DIRECT": {"type": "Direct"},
            "COMPATIBLE": {"type": "Compatible"},
            "GLOBAL": {"type": "Selector"},
            "真节点": {"type": "Vless", "history": [{"delay": 42}]},
        }
        st = _state()
        with mock.patch("accesspilot.control.load_state", return_value=st), \
             mock.patch("accesspilot.control.api.proxies", return_value=fake):
            nodes = control.list_nodes()
        self.assertEqual([n.name for n in nodes], ["真节点"],
                         "内建条目漏进来了, 用户会看到点不动的死条目")

    def test_is_real_node_rejects_builtins(self) -> None:
        for name in ("PASS-RULE", "REJECT-DROP", "GLOBAL", "DIRECT",
                     rules.G_SELECT, rules.G_AUTO):
            self.assertFalse(control._is_real_node(name), f"{name} 不该被当成节点")
        self.assertTrue(control._is_real_node("🇰🇷 韩国 | KOR #2"))


class TurnOnUsesLiveProxyState(unittest.TestCase):
    """回归: turn_on 必须看**实时**的注册表状态, 不能信 state.json 里的缓存。

    真实事故(2026-10-01): 本机上 FastGithub / 蓝灯都会去改系统代理。它们把
    ProxyServer 改成自己的端口、退出后 ProxyEnable 留在 0 —— 而我们的
    state.json 还记着"上次是我开的"(system_proxy_on=True)。
    旧写法 `if system_proxy and not st.system_proxy_on` 于是在用户点「打开」
    时**直接跳过**, 大圆钮点了没反应, 而且不报任何错。
    """

    def setUp(self) -> None:
        self.st = _state(system_proxy_on=True)
        for target, value in (
            ("accesspilot.control.load_state", lambda: self.st),
            ("accesspilot.control.process.is_running", lambda: True),
            ("accesspilot.control.save_state", lambda _st: None),
            ("accesspilot.control.api.mode", lambda _st: "rule"),
            ("accesspilot.control.api.proxy", lambda _st, _n: {}),
            ("accesspilot.control.api.version", lambda _st: "x"),
            ("accesspilot.control._task_exists", lambda _n: False),
            ("accesspilot.control._load_ai_cache", lambda: control.AiStatus()),
        ):
            p = mock.patch(target, side_effect=value)
            self.addCleanup(p.stop)
            p.start()

    def test_reenables_when_registry_off_but_state_says_on(self) -> None:
        with mock.patch("accesspilot.control.sysproxy.status",
                        return_value=(False, "127.0.0.1:38457")), \
             mock.patch("accesspilot.control.sysproxy.enable") as enable:
            control.turn_on()
        enable.assert_called_once_with(self.st)
        self.assertTrue(self.st.system_proxy_on)

    def test_does_nothing_when_already_on(self) -> None:
        with mock.patch("accesspilot.control.sysproxy.status",
                        return_value=(True, "127.0.0.1:7890")), \
             mock.patch("accesspilot.control.sysproxy.enable") as enable:
            control.turn_on()
        enable.assert_not_called()

    def test_clears_proxy_when_asked_not_to_use_it(self) -> None:
        with mock.patch("accesspilot.control.sysproxy.status",
                        return_value=(True, "127.0.0.1:7890")), \
             mock.patch("accesspilot.control.sysproxy.disable") as disable:
            control.turn_on(system_proxy=False)
        disable.assert_called_once()
        self.assertFalse(self.st.system_proxy_on)


if __name__ == "__main__":
    unittest.main()
