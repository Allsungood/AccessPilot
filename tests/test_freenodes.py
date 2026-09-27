"""单元测试: 公开免费节点(离线, 用 mock 代替真实网络).

重点覆盖真实发生过的坑:
  * "测速快"不等于"能用" —— 平台验证必须存在且要真正执行;
  * 平台验证必须顺序执行(要切策略组, 并发会把请求打到别的节点);
  * X 的验证必须包含静态资源(主页 200 但资源域拉不动 = 浏览器里永远转圈);
  * 畸形链接(缺右括号的 IPv6 等)只能跳过, 不能炸掉整个抓取流程。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot import freenodes  # noqa: E402
from accesspilot import sharelink  # noqa: E402


def fake_response(status: int, body: bytes = b"ok") -> tuple[int, dict, bytes]:
    return status, {}, body


class TestVerifyNode(unittest.TestCase):
    def setUp(self) -> None:
        self.st = mock.MagicMock()
        self.st.mixed_port = 7890
        self.st.api_secret = "s"
        self.st.api_base.return_value = "http://127.0.0.1:9090"

    def _patch_http(self, responses: dict[str, tuple[int, dict, bytes]]) -> None:
        def fake(url: str, **kw):
            for key, resp in responses.items():
                if key in url:
                    return resp
            return 500, {}, b""

        p = mock.patch("accesspilot.util.http_request", side_effect=fake)
        self.addCleanup(p.stop)
        p.start()

    def test_all_pass(self) -> None:
        with mock.patch("accesspilot.api.select"):
            self._patch_http({
                "https://x.com/": fake_response(200),
                "https://abs.twimg.com/": fake_response(200),
                "https://discord.com/api/v9/gateway": fake_response(200),
            })
            r = freenodes.verify_node(self.st, "节点A")
        self.assertTrue(freenodes.fully_usable(r))
        self.assertTrue(r["latency_ms"] >= 0)

    def test_asset_failure_marks_unusable(self) -> None:
        """主页 200 但静态资源拉不动 —— 必须判为不可用(真实发生过的假阳性)."""
        with mock.patch("accesspilot.api.select"):
            self._patch_http({
                "https://x.com/": fake_response(200),
                "https://abs.twimg.com/": fake_response(404),
                "https://discord.com/api/v9/gateway": fake_response(200),
            })
            r = freenodes.verify_node(self.st, "节点B")
        self.assertTrue(r["x_ok"], "主页本身是通的")
        self.assertFalse(r["x_asset_ok"])
        self.assertFalse(freenodes.fully_usable(r), "资源域坏了就不算可用")

    def test_select_failure_reported(self) -> None:
        with mock.patch("accesspilot.api.select", side_effect=Exception("boom")):
            r = freenodes.verify_node(self.st, "节点C")
        self.assertFalse(freenodes.fully_usable(r))
        self.assertIn("boom", r["detail"])

    def test_fully_usable_requires_all_three(self) -> None:
        base = {"name": "n", "latency_ms": 100, "x_ok": True,
                "x_asset_ok": True, "discord_ok": False, "detail": ""}
        self.assertFalse(freenodes.fully_usable(base))
        base["discord_ok"] = True
        self.assertTrue(freenodes.fully_usable(base))

    def test_x_check_includes_asset(self) -> None:
        """锁定: X 的验证必须包含静态资源, 防假阳性回归."""
        self.assertEqual(
            freenodes.X_CHECK_URLS,
            ("https://x.com/", "https://abs.twimg.com/favicons/twitter.3.ico"),
        )


class TestMalformedLinks(unittest.TestCase):
    def test_unclosed_ipv6_bracket_is_skipped(self) -> None:
        """urlsplit 对残缺 IPv6 抛 ValueError, 必须跳过而不是崩溃."""
        # 实测: '[2600:1]:443' -> ValueError("does not appear to be an IPv4 or IPv6 address")
        #       '[2600:1:2::3:443' -> ValueError("Invalid IPv6 URL")
        nodes = sharelink.parse_links("vless://u@[2600:1]:443#坏链接")
        self.assertEqual(nodes, [])
        nodes = sharelink.parse_links("vless://u@[2600:1:2::3:443#坏链接2")
        self.assertEqual(nodes, [])

    def test_bad_link_does_not_kill_good_ones(self) -> None:
        good = "ss://" + __import__("base64").urlsafe_b64encode(b"aes-128-gcm:pw").decode() + "@1.2.3.4:8388#好"
        nodes = sharelink.parse_links("vless://u@[2600:1]:443#坏\n" + good)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["name"], "好")

    def test_regex_fallback_still_extracts(self) -> None:
        """正则兜底从任意文本里捞链接."""
        text = "这里有免费节点: trojan://pw@a.b:443#T1 和 trojan://pw@c.d:443#T2"
        nodes = freenodes._extract(text, "test")
        self.assertEqual(len(nodes), 2)


class TestVerifySequential(unittest.TestCase):
    def test_verify_many_calls_verify_node_in_order(self) -> None:
        """必须顺序执行 —— 并发会把请求打到别的节点, 结果全是假的."""
        calls: list[str] = []
        with mock.patch(
            "accesspilot.freenodes.verify_node",
            side_effect=lambda st, node, **kw: calls.append(node) or {"name": node},
        ):
            freenodes.verify_many(mock.MagicMock(), ["a", "b", "c"])
        self.assertEqual(calls, ["a", "b", "c"])


class TestSources(unittest.TestCase):
    def test_sources_have_expected_shape(self) -> None:
        for name, path in freenodes.SOURCES:
            self.assertTrue(name and "/" in name, name)
            self.assertTrue(path.startswith("gh/"), f"{name}: {path}")

    def test_source_pool_is_large_enough(self) -> None:
        """节点池太小会导致"整批失效"时无可用节点, 锁定最少 10 个源."""
        self.assertGreaterEqual(len(freenodes.SOURCES), 10)


if __name__ == "__main__":
    unittest.main()
