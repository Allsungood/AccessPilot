"""单元测试: 纯 Python X25519 与 Cloudflare WARP 集成.

X25519 是 WireGuard 的密码学基础, 用错了会导致"配置看起来正常但永远连不上",
所以这里用 RFC 7748 官方测试向量 + DH 对称性做硬校验。
WARP 的注册与 profile 处理则全部离线测试(不发起真实注册)。
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401  把数据目录隔离到临时目录(见 tests/__init__.py)

from accesspilot import config, warp  # noqa: E402
from accesspilot.state import AppState  # noqa: E402
from accesspilot.subscription import Subscription  # noqa: E402


class TestX25519(unittest.TestCase):
    def test_rfc7748_vectors(self) -> None:
        for scalar_hex, u_hex, expect_hex in warp.RFC7748_VECTORS:
            got = warp.x25519(bytes.fromhex(scalar_hex), bytes.fromhex(u_hex)).hex()
            self.assertEqual(got, expect_hex)

    def test_rfc7748_iterated_1000(self) -> None:
        """RFC 7748 §5.2 的迭代测试 —— 能同时验证阶梯、clamping 与模运算."""
        k = u = bytes.fromhex("09" + "00" * 31)
        for _ in range(1000):
            k, u = warp.x25519(k, u), k
        self.assertEqual(k.hex(), warp.RFC7748_ITER_1000)

    def test_diffie_hellman_symmetry(self) -> None:
        """双方用各自私钥算出的共享密钥必须一致(自校验, 不依赖外部向量)."""
        a_priv = os.urandom(32)
        b_priv = os.urandom(32)
        base = bytes.fromhex("09" + "00" * 31)
        a_pub = warp.x25519(a_priv, base)
        b_pub = warp.x25519(b_priv, base)
        self.assertEqual(warp.x25519(a_priv, b_pub), warp.x25519(b_priv, a_pub))

    def test_small_order_point_yields_zero(self) -> None:
        """阶为 1 的点(全零)必须得到全零结果 —— 防止实现漏掉边界情况."""
        self.assertEqual(warp.x25519(os.urandom(32), bytes(32)), bytes(32))

    def test_rejects_wrong_length(self) -> None:
        with self.assertRaises(ValueError):
            warp.x25519(b"\x00" * 31, b"\x09" + b"\x00" * 31)

    def test_keypair_format(self) -> None:
        priv, pub = warp.generate_keypair()
        self.assertEqual(len(base64.b64decode(priv)), 32)
        self.assertEqual(len(base64.b64decode(pub)), 32)
        # 公钥必须能由私钥推出
        self.assertEqual(
            warp.x25519(base64.b64decode(priv), bytes.fromhex("09" + "00" * 31)),
            base64.b64decode(pub),
        )

    def test_keypair_is_random(self) -> None:
        self.assertNotEqual(warp.generate_keypair()[0], warp.generate_keypair()[0])


def sample_profile() -> warp.WarpProfile:
    return warp.WarpProfile(
        device_id="11111111-2222-3333-4444-555555555555",
        account_id="aaaa",
        license="tok",
        private_key=base64.b64encode(b"\x01" * 32).decode(),
        peer_public_key=base64.b64encode(b"\x02" * 32).decode(),
        client_id=base64.b64encode(b"\x24\x0e\x53").decode(),  # -> [36, 14, 83]
        address_v4="172.16.0.2",
        address_v6="2606:4700:110::1",
        endpoint="162.159.192.1:2408",
    )


class TestWarpProfile(unittest.TestCase):
    def test_reserved_decoding(self) -> None:
        self.assertEqual(sample_profile().reserved(), [36, 14, 83])

    def test_reserved_empty_is_zeros(self) -> None:
        p = sample_profile()
        p.client_id = ""
        self.assertEqual(p.reserved(), [0, 0, 0])

    def test_reserved_short_client_id(self) -> None:
        p = sample_profile()
        p.client_id = base64.b64encode(b"\x01").decode()
        self.assertEqual(p.reserved(), [1, 0, 0])

    def test_host_port(self) -> None:
        self.assertEqual(sample_profile().host_port(), ("162.159.192.1", 2408))
        p = sample_profile()
        p.endpoint = "engage.cloudflareclient.com"
        self.assertEqual(p.host_port(), ("engage.cloudflareclient.com", 2408))

    def test_to_mihomo_proxy(self) -> None:
        node = sample_profile().to_mihomo_proxy()
        self.assertEqual(node["type"], "wireguard")
        self.assertEqual(node["server"], "162.159.192.1")
        self.assertEqual(node["port"], 2408)
        self.assertEqual(node["ip"], "172.16.0.2/32")
        self.assertEqual(node["ipv6"], "2606:4700:110::1/128")
        self.assertEqual(node["reserved"], [36, 14, 83])
        self.assertEqual(node["mtu"], 1280)
        self.assertTrue(node["udp"])
        self.assertIn("0.0.0.0/0", node["allowed-ips"])
        self.assertNotIn("dialer-proxy", node)

    def test_endpoint_override_and_mtu(self) -> None:
        node = sample_profile().to_mihomo_proxy(endpoint="188.114.97.1:500", mtu=1420)
        self.assertEqual(node["server"], "188.114.97.1")
        self.assertEqual(node["port"], 500)
        self.assertEqual(node["mtu"], 1420)

    def test_dialer_proxy(self) -> None:
        node = sample_profile().to_mihomo_proxy(dialer_proxy="香港 01")
        self.assertEqual(node["dialer-proxy"], "香港 01")

    def test_profile_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("ACCESSPILOT_HOME")
            os.environ["ACCESSPILOT_HOME"] = tmp
            try:
                self.assertIsNone(warp.load_profile())
                prof = sample_profile()
                warp.save_profile(prof)
                back = warp.load_profile()
                assert back is not None
                self.assertEqual(back.device_id, prof.device_id)
                self.assertEqual(back.peer_public_key, prof.peer_public_key)
                self.assertTrue(warp.delete_profile())
                self.assertIsNone(warp.load_profile())
                self.assertFalse(warp.delete_profile())
            finally:
                if old:
                    os.environ["ACCESSPILOT_HOME"] = old
                else:
                    os.environ.pop("ACCESSPILOT_HOME", None)

    def test_corrupt_profile_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("ACCESSPILOT_HOME")
            os.environ["ACCESSPILOT_HOME"] = tmp
            try:
                from accesspilot import paths

                paths.ensure_dirs()
                warp.profile_path().write_text("不是 JSON", encoding="utf-8")
                self.assertIsNone(warp.load_profile())
            finally:
                if old:
                    os.environ["ACCESSPILOT_HOME"] = old
                else:
                    os.environ.pop("ACCESSPILOT_HOME", None)


class TestWarpConfigIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("ACCESSPILOT_HOME")
        os.environ["ACCESSPILOT_HOME"] = self.tmp.name
        from accesspilot import paths

        paths.ensure_dirs()

    def tearDown(self) -> None:
        if self.old:
            os.environ["ACCESSPILOT_HOME"] = self.old
        else:
            os.environ.pop("ACCESSPILOT_HOME", None)
        self.tmp.cleanup()

    def _state(self) -> AppState:
        st = AppState()
        st.ensure_secret()
        return st

    def test_warp_appears_as_node_and_group_member(self) -> None:
        warp.save_profile(sample_profile())
        cfg = config.build_config(Subscription(name="none", proxies=[]), self._state())
        names = [p["name"] for p in cfg["proxies"]]
        self.assertIn("☁️ WARP", names)
        sel = next(g for g in cfg["proxy-groups"] if g["name"] == "🚀 节点选择")
        self.assertIn("☁️ WARP", sel["proxies"])
        auto = next(g for g in cfg["proxy-groups"] if g["name"] == "♻️ 自动选择")
        self.assertIn("☁️ WARP", auto["proxies"])

    def test_warp_alone_is_not_no_node_mode(self) -> None:
        """有 WARP 时不能退化成"免节点模式"(那样默认会直连, 用户会以为坏了)."""
        warp.save_profile(sample_profile())
        cfg = config.build_config(Subscription(name="none", proxies=[]), self._state())
        select = next(g for g in cfg["proxy-groups"] if g["name"] == "🚀 节点选择")
        self.assertNotEqual(select["proxies"][0], "DIRECT")

    def test_warp_with_dialer(self) -> None:
        warp.save_profile(sample_profile())
        st = self._state()
        st.warp_dialer = "香港 01"
        sub = Subscription(
            name="s",
            proxies=[{"name": "香港 01", "type": "ss", "server": "1.1.1.1", "port": 1,
                      "cipher": "aes-128-gcm", "password": "x"}],
        )
        cfg = config.build_config(sub, st)
        node = next(p for p in cfg["proxies"] if p["name"] == "☁️ WARP")
        self.assertEqual(node["dialer-proxy"], "香港 01")

    def test_accel_proxy_excluded_from_auto_select(self) -> None:
        """IP 优选器只走直连, 不应被放进 url-test 组(那会污染测速结果)."""
        from accesspilot import rules

        warp.save_profile(sample_profile())
        st = self._state()
        st.accel_enable = True
        cfg = config.build_config(Subscription(name="none", proxies=[]), st)
        auto = next(g for g in cfg["proxy-groups"] if g["name"] == "♻️ 自动选择")
        self.assertNotIn(rules.ACCEL_PROXY_NAME, auto["proxies"])
        self.assertIn("☁️ WARP", auto["proxies"])

    def test_config_serializable_with_wireguard(self) -> None:
        from accesspilot import miniyaml

        warp.save_profile(sample_profile())
        cfg = config.build_config(Subscription(name="none", proxies=[]), self._state())
        back = miniyaml.load(miniyaml.dump(cfg))
        node = next(p for p in back["proxies"] if p["name"] == "☁️ WARP")
        self.assertEqual(node["reserved"], [36, 14, 83])
        self.assertEqual(node["allowed-ips"], ["0.0.0.0/0", "::/0"])


if __name__ == "__main__":
    unittest.main()
