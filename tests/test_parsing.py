"""单元测试: 最小 YAML 解析/生成 与 订阅解析."""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401  把数据目录隔离到临时目录(见 tests/__init__.py)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TestMiniYaml(unittest.TestCase):
    def test_scalars(self) -> None:
        from accesspilot import miniyaml as Y

        obj = Y.load(
            "a: 1\nb: 1.5\nc: true\nd: null\ne: 'x: y'\nf: \"q # z\"\ng: [1, 2, 3]\n"
            "h: {x: 1, y: two}\ni: bare text\n"
        )
        self.assertEqual(obj["a"], 1)
        self.assertEqual(obj["b"], 1.5)
        self.assertIs(obj["c"], True)
        self.assertIsNone(obj["d"])
        self.assertEqual(obj["e"], "x: y")
        self.assertEqual(obj["f"], "q # z")
        self.assertEqual(obj["g"], [1, 2, 3])
        self.assertEqual(obj["h"], {"x": 1, "y": "two"})
        self.assertEqual(obj["i"], "bare text")

    def test_nested_and_sequences(self) -> None:
        from accesspilot import miniyaml as Y

        text = """
dns:
  enable: true
  nameserver:
    - 1.1.1.1
    - 8.8.8.8
proxy-groups:
  - name: G1
    type: select
    proxies:
      - A
      - DIRECT
  - name: G2
    type: url-test
    url: "http://x/generate_204"
rules:
  - DOMAIN-SUFFIX,a.com,G1
"""
        obj = Y.load(text)
        self.assertTrue(obj["dns"]["enable"])
        self.assertEqual(obj["dns"]["nameserver"], ["1.1.1.1", "8.8.8.8"])
        self.assertEqual(len(obj["proxy-groups"]), 2)
        self.assertEqual(obj["proxy-groups"][0]["proxies"], ["A", "DIRECT"])
        self.assertEqual(obj["proxy-groups"][1]["url"], "http://x/generate_204")
        self.assertEqual(obj["rules"], ["DOMAIN-SUFFIX,a.com,G1"])

    def test_roundtrip(self) -> None:
        from accesspilot import miniyaml as Y

        obj = {
            "port": 7890,
            "flag": False,
            "empty": None,
            "names": ["a", "b"],
            "nested": {"k": "v w", "list": [{"n": 1}, {"n": 2}]},
        }
        self.assertEqual(Y.load(Y.dump(obj)), obj)

    def test_comments_and_quotes(self) -> None:
        from accesspilot import miniyaml as Y

        obj = Y.load(
            "# 头部注释\n"
            "name: \"带 # 号的值\"  # 尾部注释\n"
            "path: '/a/b#c'\n"
            "h: '+.google.com'\n"
        )
        self.assertEqual(obj["name"], "带 # 号的值")
        self.assertEqual(obj["path"], "/a/b#c")
        self.assertEqual(obj["h"], "+.google.com")

    def test_real_rule_fixture(self) -> None:
        from accesspilot import miniyaml as Y

        f = FIXTURES / "OpenAI.yaml"
        if not f.exists():
            self.skipTest("fixture 缺失")
        obj = Y.load(f.read_text(encoding="utf-8"))
        self.assertIn("payload", obj)
        self.assertTrue(all(isinstance(x, str) for x in obj["payload"]))
        self.assertTrue(any("openai.com" in x for x in obj["payload"]))

    def test_generated_config_reparses(self) -> None:
        from accesspilot import miniyaml as Y

        text = Y.dump(
            {
                "proxies": [{"name": "节点 1", "type": "ss", "port": 1, "udp": True}],
                "rules": ["MATCH,DIRECT"],
            }
        )
        obj = Y.load(text)
        self.assertEqual(obj["proxies"][0]["name"], "节点 1")
        self.assertIs(obj["proxies"][0]["udp"], True)


class TestSubscription(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("ACCESSPILOT_HOME")
        os.environ["ACCESSPILOT_HOME"] = self.tmp.name
        import importlib

        from accesspilot import paths

        importlib.reload(paths)

    def tearDown(self) -> None:
        # 必须恢复而不是 pop: 否则后续用例会退回用户真实目录
        if self._old_home is not None:
            os.environ["ACCESSPILOT_HOME"] = self._old_home
        else:
            os.environ.pop("ACCESSPILOT_HOME", None)
        self.tmp.cleanup()

    def test_parse_base64_subscription(self) -> None:
        from accesspilot import subscription

        links = "\n".join(
            [
                "ss://" + base64.urlsafe_b64encode(b"aes-128-gcm:pw").decode() + "@a:1#A",
                "trojan://pw@b:443?sni=b#B",
            ]
        )
        encoded = base64.b64encode(links.encode()).decode()
        proxies, fmt = subscription.parse_content(encoded, "t")
        self.assertEqual(fmt, "links")
        self.assertEqual(len(proxies), 2)

    def test_parse_clash_subscription(self) -> None:
        from accesspilot import subscription

        text = """
port: 7890
proxies:
  - name: "HK 01"
    type: ss
    server: 1.1.1.1
    port: 443
    cipher: aes-128-gcm
    password: x
  - name: "US 01"
    type: trojan
    server: 2.2.2.2
    port: 443
    password: y
"""
        proxies, fmt = subscription.parse_content(text, "t")
        self.assertEqual(fmt, "clash")
        self.assertEqual([p["name"] for p in proxies], ["HK 01", "US 01"])

    def test_dedupe_and_uniquify(self) -> None:
        from accesspilot import subscription

        dup = [
            {"name": "A", "type": "ss", "server": "1.1.1.1", "port": 1, "password": "p"},
            {"name": "A", "type": "ss", "server": "1.1.1.1", "port": 1, "password": "p"},
            {"name": "A", "type": "ss", "server": "2.2.2.2", "port": 1, "password": "p"},
        ]
        out = subscription.uniquify_names(subscription.dedupe(dup))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["name"], "A")
        self.assertEqual(out[1]["name"], "A #2")

    def test_invalid_content(self) -> None:
        from accesspilot import subscription
        from accesspilot.util import Fail

        with self.assertRaises(Fail):
            subscription.parse_content("这不是订阅内容", "t")
        with self.assertRaises(Fail):
            subscription.parse_content("", "t")

    def test_profile_roundtrip(self) -> None:
        from accesspilot import subscription
        from accesspilot.util import Fail

        sub = subscription.Subscription(
            name="测试档",
            url="https://example.com/sub",
            proxies=[{"name": "n1", "type": "ss", "server": "1.1.1.1", "port": 1}],
            total=100 * 1024**3,
            download=20 * 1024**3,
        )
        slug = subscription.save_profile(sub)
        loaded = subscription.load_profile(slug)
        self.assertEqual(loaded.name, "测试档")
        self.assertEqual(len(loaded.proxies), 1)
        self.assertIn("已用", loaded.traffic_text())
        self.assertIn("长期有效", loaded.expire_text())
        names = [s.name for s in subscription.list_profiles()]
        self.assertIn("测试档", names)
        subscription.delete_profile(slug)
        with self.assertRaises(Fail):
            subscription.load_profile(slug)


if __name__ == "__main__":
    unittest.main()
