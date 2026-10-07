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


if __name__ == "__main__":
    unittest.main()
