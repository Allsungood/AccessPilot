"""单元测试: 节点池的健壮性 —— 三个真实事故的回归锁。

       1. 一个畸形 REALITY 节点让**整份配置**校验失败, 红杏完全起不来。
       2. 一次源站限流的刷新把七千个候选剪成十个。
       3. 自动选择挑中"国内出口"节点, 于是 GitHub 必然打不开。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401  隔离数据目录

from accesspilot import config, freenodes  # noqa: E402


def _vless(short_id: str) -> dict:
    return {
        "name": f"n-{short_id or 'empty'}",
        "type": "vless",
        "server": "example.com",
        "port": 443,
        "uuid": "00000000-0000-0000-0000-000000000000",
        "reality-opts": {"public-key": "k", "short-id": short_id},
    }


class TestRealityShortId(unittest.TestCase):
    """真实事故: `proxy 7718: invalid REALITY short id` -> 内核拒绝整份配置。

    池子从 10 个扩到 15360 个之后才踩到 —— 但"坏节点能不能拖垮整个客户端"
    与池子大小无关, 迟早会踩。
    """

    def test_valid_short_ids_are_kept(self) -> None:
        for sid in ("", "ab12", "0123456789abcdef", "AABBCCDD"):
            kept = config.sanitize_proxies([_vless(sid)])
            self.assertEqual(len(kept), 1, f"合法 short-id {sid!r} 不该被丢弃")

    def test_invalid_short_ids_are_dropped(self) -> None:
        # 奇数长度(实测就是这个形态) / 非十六进制 / 超过 16 个字符
        for sid in ("abc", "zz", "0" * 18, "xy12"):
            kept = config.sanitize_proxies([_vless(sid)])
            self.assertEqual(kept, [], f"非法 short-id {sid!r} 必须丢弃该节点")

    def test_one_bad_node_does_not_poison_the_others(self) -> None:
        nodes = [_vless("ab12"), _vless("abc"), _vless("cd34")]
        kept = config.sanitize_proxies(nodes)
        self.assertEqual(len(kept), 2, "只该丢坏的那一个")
        self.assertNotIn("n-abc", [k["name"] for k in kept])

    def test_nodes_without_reality_are_untouched(self) -> None:
        ss = {"name": "s", "type": "ss", "server": "a.com", "port": 443,
              "cipher": "aes-128-gcm", "password": "p"}
        self.assertEqual(len(config.sanitize_proxies([ss])), 1)


class TestPoolReservoir(unittest.TestCase):
    """真实事故: 源站限流的那一轮只测出十来个活节点, 于是 15360 -> 10。

    池子是**候选储备**, 不是"已验证白名单"; 一次坏刷新不该把它掏空。
    """

    def _sub(self, n: int):
        from accesspilot.subscription import Subscription

        return Subscription(name="free", proxies=[
            {"name": f"n{i}", "type": "ss", "server": f"1.1.1.{i}", "port": 443,
             "cipher": "aes-128-gcm", "password": "p"}
            for i in range(n)
        ])

    def test_reservoir_prevents_collapse(self) -> None:
        saved: list = []
        with mock.patch.object(freenodes.sub_mod, "load_profile",
                               return_value=self._sub(1000)), \
             mock.patch.object(freenodes.sub_mod, "save_profile",
                               side_effect=lambda s: saved.append(s)):
            before, after = freenodes.prune_profile(
                "free", {"n0": 10, "n1": 20}, reservoir=300
            )
        self.assertEqual(before, 1000)
        self.assertEqual(after, 300, "只测通 2 个也不能把池子剪到 2 个")
        self.assertEqual(len(saved[0].proxies), 300)

    def test_without_reservoir_behaviour_is_unchanged(self) -> None:
        saved: list = []
        with mock.patch.object(freenodes.sub_mod, "load_profile",
                               return_value=self._sub(1000)), \
             mock.patch.object(freenodes.sub_mod, "save_profile",
                               side_effect=lambda s: saved.append(s)):
            _before, after = freenodes.prune_profile("free", {"n0": 10})
        self.assertEqual(after, 1, "不传 reservoir 时保持原有语义")

    def test_reservoir_never_invents_nodes(self) -> None:
        """储备只能从**已有**节点里补, 不能凭空造。"""
        saved: list = []
        with mock.patch.object(freenodes.sub_mod, "load_profile",
                               return_value=self._sub(5)), \
             mock.patch.object(freenodes.sub_mod, "save_profile",
                               side_effect=lambda s: saved.append(s)):
            _b, after = freenodes.prune_profile("free", {"n0": 1}, reservoir=500)
        self.assertEqual(after, 5, "最多就是把原来的 5 个都留着")

    def test_still_refuses_to_empty_the_profile(self) -> None:
        from accesspilot.util import Fail

        saved: list = []
        with mock.patch.object(freenodes.sub_mod, "load_profile",
                               return_value=self._sub(10)), \
             mock.patch.object(freenodes.sub_mod, "save_profile",
                               side_effect=lambda s: saved.append(s)):
            with self.assertRaises(Fail):
                freenodes.prune_profile("free", {"完全不存在的节点": 5})
        self.assertEqual(saved, [])


class TestDomesticNodesExcluded(unittest.TestCase):
    """真实事故: 自动选择挑中 `🇨🇳_CN_中国 #2`, 于是 github.com TLS 握手 20 秒超时。

    根因是 url-test 的探测目标是 gstatic.com/generate_204 —— **国内节点也能过**,
    于是它凭延迟最低赢了, 成了一个连不上被墙站点的"出口"。
    """

    def setUp(self) -> None:
        import re

        self.rx = re.compile(config.DOMESTIC_EXCLUDE)

    def test_pure_domestic_nodes_are_excluded(self) -> None:
        for name in ("🇨🇳_CN_中国", "🇨🇳_CN_中国 #2", "美国剩余流量",
                     "官网续费", "Traffic 到期"):
            self.assertTrue(self.rx.search(name), f"{name!r} 必须被排除")

    def test_overseas_and_transit_chains_are_kept(self) -> None:
        # 中转链名字里有"中国"但出口在德国 —— 误杀它会白白损失好节点
        for name in ("🇭🇰_HK_中国香港->🇩🇪_DE_德国", "香港|@ripaojiedian",
                     "🇸🇬SG_28|702KB/s|R002-260618 01", "github.com/freefq - 俄罗斯  1"):
            self.assertFalse(self.rx.search(name), f"{name!r} 不该被排除")

    def test_url_test_group_carries_the_filter(self) -> None:
        """回归: 渲染出来的每一个 url-test 组都必须带 exclude-filter。"""
        import inspect

        src = inspect.getsource(config)
        self.assertIn('"exclude-filter": DOMESTIC_EXCLUDE', src)
        # 两处 url-test 定义都要带(exclude-filter 出现次数 >= 2)
        self.assertGreaterEqual(src.count('"exclude-filter": DOMESTIC_EXCLUDE'), 2)


class TestExitPinnedToDomesticNode(unittest.TestCase):
    """最后一个、也是最隐蔽的坑。

    `exclude-filter` 只作用于 url-test 组, **管不到 state.json 里显式保存的选择**。
    实测现场: `♻️ 自动选择` 已经正确落在美国节点, 而 `🚀 节点选择` 却被钉在
    `🇨🇳_CN_中国` 上 —— 排除规则被整个绕过, GitHub 必然打不开, 而所有检查都
    显示正常。
    """

    def setUp(self) -> None:
        from accesspilot.state import load_state, save_state

        self.st = load_state()
        self.st.selected = {}
        save_state(self.st)

    def _pin(self, name: str) -> None:
        from accesspilot import rules
        from accesspilot.state import save_state

        self.st.selected = {rules.G_SELECT: name}
        save_state(self.st)

    def test_detects_domestic_pin(self) -> None:
        from accesspilot import guard

        for bad in ("🇨🇳_CN_中国", "🇨🇳_CN_中国 #2", "美国直连中国"):
            self._pin(bad)
            self.assertIsNotNone(guard.domestic_pinned(self.st), f"{bad!r} 应被识别")

    def test_ignores_overseas_pin(self) -> None:
        from accesspilot import guard

        for good in ("🇸🇬SG_28|702KB/s|R002-260618 01", "香港|@ripaojiedian",
                     "🇭🇰_HK_中国香港->🇩🇪_DE_德国"):
            self._pin(good)
            self.assertIsNone(guard.domestic_pinned(self.st), f"{good!r} 不该被识别")

    def test_repair_unpins_and_restores_auto(self) -> None:
        from accesspilot import api, guard, rules
        from accesspilot.state import load_state

        self._pin("🇨🇳_CN_中国 #2")
        with mock.patch.object(guard.process, "is_running", return_value=True), \
             mock.patch.object(guard, "port_serving", return_value=True), \
             mock.patch.object(guard.sysproxy, "effective",
                               return_value=(True, "127.0.0.1:7890")), \
             mock.patch.object(api, "select") as sel:
            res = guard.repair(self.st, deep=False, quiet=True)
        sel.assert_called_once_with(self.st, rules.G_SELECT, rules.G_AUTO)
        self.assertTrue(any("国内节点" in r for r in res["repairs"]), res["repairs"])
        self.assertIsNone(guard.domestic_pinned(load_state()))


class TestProbeCoversTheRealTargets(unittest.TestCase):
    """回归: 自愈层的探测目标必须覆盖**产品真正要服务的站点**。

    原来只测 gstatic.com/generate_204 —— 而国内节点也能通过它。于是自愈层一直
    报"健康", 用户却打不开 GitHub。健康判据和目标不一致, 就只是自我安慰。
    """

    def test_targets_include_github_and_huggingface(self) -> None:
        from accesspilot import guard

        keys = {k for k, _ in guard.TRAFFIC_TARGETS}
        self.assertIn("github", keys)
        self.assertIn("huggingface", keys)

    def test_probe_reports_per_site(self) -> None:
        """必须逐站报告, 才能区分"链路断了"(重启内核)和"出口选错了"(换节点)。"""
        from accesspilot import guard

        with mock.patch.object(guard, "http_request") as req:
            def fake(url, **kw):
                # 只有 github 失败 —— 这正是"出口选错"的形态
                if "github.com" in url:
                    raise OSError("blocked")
                return 200, {}, b""

            req.side_effect = fake
            sites = guard.probe_targets(guard.load_state())
        self.assertFalse(sites["github"])
        self.assertTrue(sites["huggingface"])

    def test_repin_prefers_already_selected_nodes(self) -> None:
        """候选顺序: 先把别的组**正在用的**节点排前面。

        只按 profile 顺序取头部节点的话, 拿到的全是没验证过的死节点
        (实测前 8 个 github.com/freefq 全部 503/504), 于是"换出口"永远失败。
        """
        from accesspilot import api, guard, rules

        seen: list[str] = []

        def fake_delay(st, name, *, url, timeout_ms):
            seen.append(name)
            return 42 if name == "KNOWN_GOOD" else -1

        with mock.patch.object(api, "proxy",
                               return_value={"now": "KNOWN_GOOD"}), \
             mock.patch.object(api, "delay", side_effect=fake_delay), \
             mock.patch.object(api, "select") as sel, \
             mock.patch("accesspilot.subscription.load_profile") as lp:
            lp.return_value = type("S", (), {"proxies": [
                {"name": f"dead{i}"} for i in range(1, 20)]})()
            picked = guard.repin_for_sites(guard.load_state(), want="github")
        self.assertEqual(picked, "KNOWN_GOOD")
        self.assertEqual(seen[0], "KNOWN_GOOD", "已知能用的节点必须第一个被试")
        self.assertEqual(sel.call_count, 2, "通用组和 AI 组都要钉上")


if __name__ == "__main__":
    unittest.main()
