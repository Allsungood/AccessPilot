"""单元测试: 分享链接解析."""
from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401  统一的数据目录隔离(见 tests/__init__.py)

from accesspilot import sharelink  # noqa: E402


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


class TestSS(unittest.TestCase):
    def test_sip002(self) -> None:
        uri = f"ss://{b64('aes-256-gcm:p@ss:word')}@1.2.3.4:8388#%E9%A6%99%E6%B8%AF"
        node = sharelink.parse_link(uri)
        assert node is not None
        self.assertEqual(node["type"], "ss")
        self.assertEqual(node["cipher"], "aes-256-gcm")
        self.assertEqual(node["password"], "p@ss:word")
        self.assertEqual(node["server"], "1.2.3.4")
        self.assertEqual(node["port"], 8388)
        self.assertEqual(node["name"], "香港")

    def test_legacy_and_plugin(self) -> None:
        legacy = "ss://" + b64("chacha20-ietf-poly1305:pw@example.com:443") + "#Legacy"
        node = sharelink.parse_link(legacy)
        assert node is not None
        self.assertEqual(node["server"], "example.com")
        self.assertEqual(node["port"], 443)

        plugin = (
            "ss://" + b64("aes-128-gcm:pw") + "@h:1?plugin="
            + "obfs-local%3Bobfs%3Dhttp%3Bobfs-host%3Dbing.com#P"
        )
        node = sharelink.parse_link(plugin)
        assert node is not None
        self.assertEqual(node["plugin"], "obfs")
        self.assertEqual(node["plugin-opts"]["host"], "bing.com")


class TestVmess(unittest.TestCase):
    def test_ws_tls(self) -> None:
        payload = {
            "v": "2",
            "ps": "美国 01",
            "add": "us.example.com",
            "port": "443",
            "id": "11111111-2222-3333-4444-555555555555",
            "aid": "0",
            "scy": "auto",
            "net": "ws",
            "type": "none",
            "host": "cdn.example.com",
            "path": "/ray",
            "tls": "tls",
            "sni": "cdn.example.com",
            "fp": "chrome",
        }
        uri = "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()
        node = sharelink.parse_link(uri)
        assert node is not None
        self.assertEqual(node["type"], "vmess")
        self.assertEqual(node["alterId"], 0)
        self.assertTrue(node["tls"])
        self.assertEqual(node["network"], "ws")
        self.assertEqual(node["ws-opts"]["path"], "/ray")
        self.assertEqual(node["ws-opts"]["headers"]["Host"], "cdn.example.com")
        self.assertEqual(node["client-fingerprint"], "chrome")

    def test_grpc(self) -> None:
        payload = {
            "v": "2", "ps": "g", "add": "a.b", "port": 443,
            "id": "x", "net": "grpc", "path": "svc", "tls": "tls",
        }
        uri = "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()
        node = sharelink.parse_link(uri)
        assert node is not None
        self.assertEqual(node["grpc-opts"]["grpc-service-name"], "svc")


class TestVless(unittest.TestCase):
    def test_reality(self) -> None:
        uri = (
            "vless://11111111-2222-3333-4444-555555555555@1.1.1.1:443"
            "?encryption=none&security=reality&sni=www.microsoft.com&fp=chrome"
            "&pbk=PUBKEY&sid=ab12&type=tcp&flow=xtls-rprx-vision#%E6%97%A5%E6%9C%AC"
        )
        node = sharelink.parse_link(uri)
        assert node is not None
        self.assertEqual(node["type"], "vless")
        self.assertTrue(node["tls"])
        self.assertEqual(node["flow"], "xtls-rprx-vision")
        self.assertEqual(node["reality-opts"]["public-key"], "PUBKEY")
        self.assertEqual(node["reality-opts"]["short-id"], "ab12")
        self.assertEqual(node["name"], "日本")

    def test_ws(self) -> None:
        uri = (
            "vless://uuid-1@h.example.com:80?encryption=none&type=ws"
            "&path=%2Fws&host=cdn.example.com&security=none#WS"
        )
        node = sharelink.parse_link(uri)
        assert node is not None
        self.assertFalse(node.get("tls"))
        self.assertEqual(node["ws-opts"]["path"], "/ws")


class TestOthers(unittest.TestCase):
    def test_trojan(self) -> None:
        node = sharelink.parse_link(
            "trojan://pwd@t.example.com:443?sni=t.example.com&allowInsecure=1&type=ws&path=/x#T"
        )
        assert node is not None
        self.assertEqual(node["type"], "trojan")
        self.assertTrue(node["skip-cert-verify"])
        self.assertEqual(node["network"], "ws")

    def test_hysteria2(self) -> None:
        node = sharelink.parse_link(
            "hysteria2://letmein@h.example.com:8443?sni=h.example.com&obfs=salamander&obfs-password=op#H"
        )
        assert node is not None
        self.assertEqual(node["type"], "hysteria2")
        self.assertEqual(node["password"], "letmein")
        self.assertEqual(node["obfs"], "salamander")

    def test_tuic(self) -> None:
        node = sharelink.parse_link(
            "tuic://uuid-9:pwd@t.example.com:443?sni=t.example.com&alpn=h3&congestion_control=bbr#T"
        )
        assert node is not None
        self.assertEqual(node["uuid"], "uuid-9")
        self.assertEqual(node["password"], "pwd")
        self.assertEqual(node["alpn"], ["h3"])

    def test_ssr(self) -> None:
        main = "1.2.3.4:8388:auth_aes128_md5:aes-256-cfb:tls1.2_ticket_auth:" + b64("pw")
        query = "/?remarks=" + b64("SSR节点") + "&obfsparam=" + b64("cloudfront.net")
        uri = "ssr://" + base64.urlsafe_b64encode((main + query).encode()).decode()
        node = sharelink.parse_link(uri)
        assert node is not None
        self.assertEqual(node["type"], "ssr")
        self.assertEqual(node["name"], "SSR节点")
        self.assertEqual(node["password"], "pw")
        self.assertEqual(node["obfs"], "tls1.2_ticket_auth")
        self.assertEqual(node["obfs-param"], "cloudfront.net")

    def test_socks5(self) -> None:
        node = sharelink.parse_link("socks5://user:pw@127.0.0.1:1080#Local")
        assert node is not None
        self.assertEqual(node["type"], "socks5")
        self.assertEqual(node["username"], "user")

    def test_unknown_scheme(self) -> None:
        self.assertIsNone(sharelink.parse_link("wireguard://xxx"))
        self.assertIsNone(sharelink.parse_link("not a link"))

    def test_missing_password_raises(self) -> None:
        from accesspilot.util import Fail

        with self.assertRaises(Fail):
            sharelink.parse_link("trojan://@h.example.com:443#x")

    def test_parse_links_skips_bad(self) -> None:
        text = "ss://" + b64("aes-128-gcm:pw") + "@h:1#ok\n乱码行\n# 注释\n"
        nodes = sharelink.parse_links(text)
        self.assertEqual(len(nodes), 1)


if __name__ == "__main__":
    unittest.main()
