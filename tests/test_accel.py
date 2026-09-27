"""单元测试: 免节点直连加速(IP 优选).

测试原则: 全部用例都要能在**离线**环境下跑完 —— 用本地 TCP 监听器与
手工构造的 DNS 报文代替真实网络, 避免测试依赖外网可达性。
"""
from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401  把数据目录隔离到临时目录(见 tests/__init__.py)

from accesspilot import accel, config, rules  # noqa: E402
from accesspilot.state import AppState  # noqa: E402
from accesspilot.subscription import Subscription  # noqa: E402


def dns_response(name: str, ips: list[str], rcode: int = 0) -> bytes:
    """手工构造一个 DNS 响应报文(用于测试解析器, 不依赖网络)."""
    flags = 0x8180 | (rcode & 0xF)
    header = struct.pack(">HHHHHH", 0x1234, flags, 1, len(ips), 0, 0)
    question = (
        b"".join(bytes([len(p)]) + p.encode() for p in name.split("."))
        + b"\x00"
        + struct.pack(">HH", 1, 1)
    )
    answers = b""
    for ip in ips:
        answers += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton(ip)
    return header + question + answers


class _Listener:
    """本地 TCP 监听器, 模拟一个"可用 IP:端口"."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.close()

    def close(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "_Listener":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class TestDnsParsing(unittest.TestCase):
    def test_wireformat(self) -> None:
        raw = dns_response("raw.githubusercontent.com", ["185.199.109.133"])
        self.assertEqual(accel.parse_dns_a(raw), ["185.199.109.133"])
        self.assertEqual(accel.parse_doh_payload(raw), ["185.199.109.133"])

    def test_multiple_records(self) -> None:
        raw = dns_response("github.com", ["20.205.243.166", "140.82.112.3"])
        self.assertEqual(
            accel.parse_doh_payload(raw), ["20.205.243.166", "140.82.112.3"]
        )

    def test_rcode_error_yields_empty(self) -> None:
        raw = dns_response("x.com", [], rcode=3)  # NXDOMAIN
        self.assertEqual(accel.parse_dns_a(raw), [])

    def test_malformed_returns_none(self) -> None:
        for bad in (b"", b"\x00", b"not-a-dns-message"):
            self.assertIsNone(accel.parse_dns_a(bad), f"应拒绝: {bad!r}")

    def test_json_format(self) -> None:
        payload = json.dumps(
            {
                "Status": 0,
                "Answer": [
                    {"name": "a.com", "type": 5, "data": "b.com"},
                    {"name": "a.com", "type": 1, "data": "1.2.3.4"},
                ],
            }
        ).encode()
        self.assertEqual(accel.parse_doh_payload(payload), ["1.2.3.4"])

    def test_json_error_status(self) -> None:
        payload = json.dumps({"Status": 3, "Answer": []}).encode()
        self.assertEqual(accel.parse_doh_payload(payload), [])

    def test_json_garbage(self) -> None:
        self.assertEqual(accel.parse_doh_payload(b"{not json"), [])


class TestAccelPolicy(unittest.TestCase):
    def test_is_accelerated(self) -> None:
        self.assertTrue(accel.is_accelerated("raw.githubusercontent.com"))
        self.assertTrue(accel.is_accelerated("github.com"))
        self.assertTrue(accel.is_accelerated("sub.github.io"))
        self.assertTrue(accel.is_accelerated("ghcr.io"))
        self.assertFalse(accel.is_accelerated("chatgpt.com"))
        self.assertFalse(accel.is_accelerated("discord.com"))
        self.assertFalse(accel.is_accelerated("x.com"))

    def test_suffix_must_be_label_aligned(self) -> None:
        # 防止 "notgithub.com" 被误判为 github.com
        self.assertFalse(accel.is_accelerated("notgithub.com"))
        self.assertFalse(accel.is_accelerated("evil-github.com"))

    def test_pools_cover_github_family(self) -> None:
        for domain in (
            "raw.githubusercontent.com",
            "github.com",
            "github.githubassets.com",
            "codeload.github.com",
            "ghcr.io",
        ):
            self.assertTrue(accel._pool_for(domain), f"{domain} 缺少内置 IP 池")

    def test_pool_ip_format(self) -> None:
        for domain, ips in accel.IP_POOLS.items():
            for ip in ips:
                parts = ip.split(".")
                self.assertEqual(len(parts), 4, f"{domain} 的 {ip} 不是 IPv4")
                self.assertTrue(all(0 <= int(p) <= 255 for p in parts))

    def test_declared_failures_are_not_accelerated(self) -> None:
        """实验结论必须体现在代码里: 这些域名换 IP 救不了, 不应加入加速列表."""
        for domain in ("chatgpt.com", "discord.com", "x.com", "huggingface.co"):
            self.assertFalse(
                accel.is_accelerated(domain),
                f"{domain} 实测无法靠换 IP 加速, 不应出现在 ACCEL_SUFFIXES",
            )


class TestProbing(unittest.TestCase):
    def test_tcp_latency_alive_and_dead(self) -> None:
        with _Listener() as lis:
            self.assertIsNotNone(accel.tcp_latency("127.0.0.1", lis.port, timeout=2))
        # 关闭后的端口应探测失败
        self.assertIsNone(accel.tcp_latency("127.0.0.1", lis.port, timeout=0.5))

    def test_rank_ips_orders_by_latency(self) -> None:
        with _Listener() as good:
            results = accel.rank_ips(
                "example.com",
                ["192.0.2.1", "127.0.0.1"],  # 192.0.2.0/24 是保留测试段, 必然不可达
                port=good.port,
                timeout=0.8,
                verify_tls=False,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].ip, "127.0.0.1")
        self.assertTrue(results[0].usable)

    def test_rank_ips_empty_input(self) -> None:
        self.assertEqual(accel.rank_ips("example.com", []), [])

    def test_quick_candidates_falls_back_to_system_dns(self) -> None:
        with _Listener() as lis:
            ips = accel.quick_candidates("localhost", port=lis.port, timeout=1.0)
        self.assertIn("127.0.0.1", ips)

    def test_dial_best_direct_fallback(self) -> None:
        """未加速域名必须走正常解析, 而不是被优选逻辑影响."""
        with _Listener() as lis:
            sock, ip = accel.dial_best("localhost", lis.port, timeout=3)
            self.assertEqual(ip, "127.0.0.1")
            sock.close()


class TestCache(unittest.TestCase):
    def test_mark_bad_removes_entry(self) -> None:
        cache = accel.BestIPCache()
        cache._data["a.com"] = accel.CacheEntry(
            [accel.ProbeResult("1.1.1.1", 10, 20), accel.ProbeResult("2.2.2.2", 11, 21)],
            time.time() + 100,
        )
        cache.mark_bad("a.com", "1.1.1.1")
        left = cache.peek("a.com")
        self.assertEqual([r.ip for r in left or []], ["2.2.2.2"])

    def test_mark_bad_all_clears_expiry(self) -> None:
        cache = accel.BestIPCache()
        cache._data["a.com"] = accel.CacheEntry(
            [accel.ProbeResult("1.1.1.1", 10, 20)], time.time() + 100
        )
        cache.mark_bad("a.com", "1.1.1.1")
        self.assertIsNone(cache.peek("a.com"))

    def test_expiry(self) -> None:
        cache = accel.BestIPCache()
        cache._data["a.com"] = accel.CacheEntry([accel.ProbeResult("1.1.1.1")], time.time() - 1)
        self.assertIsNone(cache.peek("a.com"))

    def test_resolve_async_dedupes(self) -> None:
        """同一域名并发请求时, 只能触发一次完整优选."""
        cache = accel.BestIPCache()
        calls: list[str] = []
        gate = threading.Event()

        def slow_resolve(domain: str, **kw: object) -> list[object]:
            calls.append(domain)
            gate.wait(2)  # 卡住线程, 保证第二次调用时它仍在 pending
            return []

        cache.resolve = slow_resolve  # type: ignore[assignment]
        cache.resolve_async("a.com")
        time.sleep(0.1)
        cache.resolve_async("a.com")
        time.sleep(0.1)
        self.assertEqual(calls, ["a.com"], "同一域名不应并发重复优选")
        gate.set()
        time.sleep(0.3)
        # 完成后应能再次触发(不会被永久卡住)
        gate.set()
        cache.resolve_async("a.com")
        time.sleep(0.2)
        self.assertEqual(len(calls), 2)


class TestConfigIntegration(unittest.TestCase):
    def _state(self, accel_on: bool) -> AppState:
        st = AppState()
        st.ensure_secret()
        st.accel_enable = accel_on
        return st

    def test_no_node_no_accel_raises(self) -> None:
        from accesspilot.util import Fail

        with self.assertRaises(Fail):
            config.build_config(Subscription(name="empty", proxies=[]), self._state(False))

    def test_no_node_mode_with_accel(self) -> None:
        cfg = config.build_config(Subscription(name="empty", proxies=[]), self._state(True))
        names = [g["name"] for g in cfg["proxy-groups"]]
        self.assertIn(rules.G_ACCEL, names)
        # 免节点模式下默认必须直连, 不能假装有节点
        final = next(g for g in cfg["proxy-groups"] if g["name"] == rules.G_FINAL)
        self.assertEqual(final["proxies"][-1], "DIRECT")
        proxy_names = [p["name"] for p in cfg["proxies"]]
        self.assertEqual(proxy_names, [rules.ACCEL_PROXY_NAME])
        accel_proxy = cfg["proxies"][0]
        self.assertEqual(accel_proxy["type"], "socks5")
        self.assertEqual(accel_proxy["server"], "127.0.0.1")
        self.assertEqual(accel_proxy["port"], self._state(True).accel_port)

    def test_accel_rules_are_first(self) -> None:
        cfg = config.build_config(Subscription(name="empty", proxies=[]), self._state(True))
        first = cfg["rules"][0]
        self.assertIn(rules.G_ACCEL, first)
        self.assertTrue(first.startswith("DOMAIN-SUFFIX,github.com,"))
        # 直连加速必须优先于内联的通用代理规则(否则 GitHub 会被送去节点)
        gfw_idx = next(i for i, r in enumerate(cfg["rules"]) if r.startswith("RULE-SET,proxy"))
        accel_idx = next(
            i for i, r in enumerate(cfg["rules"]) if "githubusercontent.com" in r
        )
        self.assertLess(accel_idx, gfw_idx)

    def test_accel_off_keeps_base_groups_only(self) -> None:
        sub = Subscription(name="s", proxies=[{"name": "n1", "type": "ss", "server": "1.1.1.1",
                                               "port": 1, "cipher": "aes-128-gcm", "password": "x"}])
        cfg = config.build_config(sub, self._state(False))
        names = [g["name"] for g in cfg["proxy-groups"]]
        self.assertNotIn(rules.G_ACCEL, names)
        self.assertNotIn(rules.ACCEL_PROXY_NAME, [p["name"] for p in cfg["proxies"]])

    def test_accel_on_keeps_real_nodes(self) -> None:
        sub = Subscription(name="s", proxies=[{"name": "n1", "type": "ss", "server": "1.1.1.1",
                                               "port": 1, "cipher": "aes-128-gcm", "password": "x"}])
        cfg = config.build_config(sub, self._state(True))
        sel = next(g for g in cfg["proxy-groups"] if g["name"] == rules.G_SELECT)
        self.assertIn("n1", sel["proxies"])
        self.assertIn(rules.ACCEL_PROXY_NAME, sel["proxies"])

    def test_external_ui_only_when_installed(self) -> None:
        import tempfile
        from accesspilot import paths as paths_mod

        with tempfile.TemporaryDirectory() as tmp:
            import os

            old = os.environ.get("ACCESSPILOT_HOME")
            os.environ["ACCESSPILOT_HOME"] = tmp
            try:
                st = self._state(True)
                cfg = config.build_config(Subscription(name="e", proxies=[]), st)
                self.assertNotIn("external-ui", cfg, "面板未安装时不应声明 external-ui")
            finally:
                if old:
                    os.environ["ACCESSPILOT_HOME"] = old
                else:
                    os.environ.pop("ACCESSPILOT_HOME", None)
                paths_mod.ensure_dirs()


if __name__ == "__main__":
    unittest.main()
