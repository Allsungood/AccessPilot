"""单元测试: 配置生成与规则库."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401  必须最先导入: 它把数据目录隔离到临时目录,
#                否则测试会读到你真实注册的 WARP / 订阅, 断言随环境漂移

from accesspilot import config, miniyaml, rules  # noqa: E402
from accesspilot.state import AppState  # noqa: E402
from accesspilot.subscription import Subscription  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def sample_sub() -> Subscription:
    return Subscription(
        name="sample",
        proxies=[
            {"name": "HK-01", "type": "ss", "server": "1.1.1.1", "port": 443,
             "cipher": "aes-128-gcm", "password": "x"},
            {"name": "US-01", "type": "vless", "server": "2.2.2.2", "port": 443,
             "uuid": "u", "network": "ws", "tls": True,
             "ws-opts": {"path": "/ws", "headers": {"Host": "a.b"}}},
            {"name": "JP-01", "type": "trojan", "server": "3.3.3.3", "port": 443,
             "password": "y"},
        ],
    )


class TestRules(unittest.TestCase):
    def test_inline_covers_targets(self) -> None:
        text = "\n".join(rules.inline_rules())
        for domain in ("openai.com", "chatgpt.com", "discord.com", "x.com",
                       "twitter.com", "twimg.com", "t.me"):
            self.assertIn(f"DOMAIN-SUFFIX,{domain},", text)

    def test_target_groups_used(self) -> None:
        text = "\n".join(rules.build_rules())
        self.assertIn(rules.G_AI, text)
        self.assertIn(rules.G_SOCIAL, text)

    def test_match_is_last(self) -> None:
        r = rules.build_rules()
        self.assertTrue(r[-1].startswith("MATCH,"))

    def test_ai_before_generic(self) -> None:
        r = rules.build_rules()
        ai_idx = next(i for i, x in enumerate(r) if "openai.com" in x)
        gfw_idx = next(i for i, x in enumerate(r) if x.startswith("RULE-SET,proxy"))
        self.assertLess(ai_idx, gfw_idx, "AI 规则必须先于通用代理规则")

    def test_mirror_switch(self) -> None:
        jsd = rules.rule_providers("jsdelivr")["direct"]["url"]
        raw = rules.rule_providers("raw")["direct"]["url"]
        gh = rules.rule_providers("ghproxy")["direct"]["url"]
        self.assertIn("jsdelivr.net", jsd)
        self.assertIn("raw.githubusercontent.com", raw)
        self.assertIn("ghfast.top", gh)


class TestConfigBuild(unittest.TestCase):
    def setUp(self) -> None:
        self.st = AppState()
        self.st.ensure_secret()
        self.cfg = config.build_config(sample_sub(), self.st)

    def test_ports_and_api(self) -> None:
        self.assertEqual(self.cfg["mixed-port"], 7890)
        self.assertIn("external-controller", self.cfg)
        self.assertTrue(self.cfg["secret"])

    def test_proxy_fingerprint_injected(self) -> None:
        by_name = {p["name"]: p for p in self.cfg["proxies"]}
        self.assertEqual(by_name["US-01"]["client-fingerprint"], "chrome")
        self.assertEqual(by_name["JP-01"]["client-fingerprint"], "chrome")
        # ss 不支持指纹, 不应被写入
        self.assertNotIn("client-fingerprint", by_name["HK-01"])

    def test_no_global_fingerprint_key(self) -> None:
        # mihomo >= 1.19 已移除该字段, 保留会导致内核启动直接失败
        self.assertNotIn("global-client-fingerprint", self.cfg)

    def test_groups_exist_and_reference_nodes(self) -> None:
        names = [g["name"] for g in self.cfg["proxy-groups"]]
        for g in rules.ALL_GROUPS:
            self.assertIn(g, names)
        sel = next(g for g in self.cfg["proxy-groups"] if g["name"] == rules.G_SELECT)
        self.assertIn("HK-01", sel["proxies"])
        auto = next(g for g in self.cfg["proxy-groups"] if g["name"] == rules.G_AUTO)
        self.assertEqual(auto["type"], "url-test")
        self.assertEqual(len(auto["proxies"]), 3)

    def test_no_proxy_group_loop(self) -> None:
        """策略组之间不能形成环, 否则内核拒绝加载."""
        refs = {
            g["name"]: {p for p in g["proxies"] if p in [x["name"] for x in self.cfg["proxy-groups"]]}
            for g in self.cfg["proxy-groups"]
        }
        visiting: set[str] = set()
        done: set[str] = set()

        def walk(node: str) -> None:
            self.assertNotIn(node, visiting, f"策略组存在环: {node}")
            if node in done:
                return
            visiting.add(node)
            for nxt in refs.get(node, ()):
                walk(nxt)
            visiting.discard(node)
            done.add(node)

        for name in refs:
            walk(name)

    def test_dns_antipollution(self) -> None:
        dns = self.cfg["dns"]
        self.assertTrue(dns["enable"])
        policy = dns["nameserver-policy"]
        for key in ("+.openai.com", "+.chatgpt.com", "+.discord.com", "+.x.com"):
            self.assertIn(key, policy)
        self.assertIn("fallback-filter", dns)
        self.assertTrue(dns["fallback-filter"]["geoip"])

    def test_tun_toggle(self) -> None:
        st = AppState()
        st.ensure_secret()
        off = config.build_config(sample_sub(), st)
        self.assertFalse(off["tun"]["enable"])
        on = config.build_config(sample_sub(), st, tun=True)
        self.assertTrue(on["tun"]["enable"])
        self.assertEqual(on["tun"]["stack"], "mixed")

    def test_rules_present(self) -> None:
        text = "\n".join(self.cfg["rules"])
        self.assertIn(f"MATCH,{rules.G_FINAL}", text)
        self.assertIn("GEOIP,CN", text)
        self.assertTrue(self.cfg["rule-providers"])

    def test_serializable_and_reparsable(self) -> None:
        text = miniyaml.dump(self.cfg)
        back = miniyaml.load(text)
        self.assertEqual(len(back["proxies"]), 3)
        self.assertEqual(back["rules"], self.cfg["rules"])
        self.assertEqual(back["proxy-groups"][0]["name"], self.cfg["proxy-groups"][0]["name"])

    def test_empty_subscription_fails(self) -> None:
        from accesspilot.util import Fail

        with self.assertRaises(Fail):
            config.build_config(Subscription(name="empty", proxies=[]), self.st)


class TestRuleProviderFormat(unittest.TestCase):
    """锁定规则集格式约定.

    回归背景: Loyalsoldier/clash-rules 的文件以 .txt 结尾, 内容却是 Clash
    YAML payload。若声明为 format: text, 内核不会报错, 只会把 'payload:'
    当作规则解析并把**整份规则集静默丢弃**(曾导致国内直连/广告拦截全部失效)。
    这些用例把该约定固定下来。
    """

    def test_declared_format_is_yaml_for_payload_files(self) -> None:
        providers = rules.rule_providers("jsdelivr")
        for name in ("direct", "proxy", "reject", "gfw", "cncidr", "lancidr",
                     "private", "telegramcidr"):
            self.assertEqual(
                providers[name]["format"], "yaml",
                f"{name} 是 YAML payload 文件, 不能标成 text",
            )
        for name in ("openai", "discord", "twitter", "google"):
            self.assertEqual(providers[name]["format"], "yaml")

    def test_fixture_is_yaml_payload(self) -> None:
        f = FIXTURES / "loyalsoldier_proxy_head.txt"
        self.assertTrue(f.exists(), "缺少规则集格式样本")
        first = next(line.strip() for line in f.read_text(encoding="utf-8").splitlines()
                     if line.strip())
        self.assertEqual(first, "payload:")
        obj = miniyaml.load(f.read_text(encoding="utf-8"))
        self.assertIn("payload", obj)
        self.assertTrue(any("google.com" in x for x in obj["payload"]))

    def test_behavior_matches_provider_kind(self) -> None:
        providers = rules.rule_providers("jsdelivr")
        self.assertEqual(providers["direct"]["behavior"], "domain")
        self.assertEqual(providers["cncidr"]["behavior"], "ipcidr")
        self.assertEqual(providers["openai"]["behavior"], "classical")

    def test_cached_path_matches_format(self) -> None:
        # 缓存文件后缀必须与格式一致, 避免读到上次格式的旧缓存
        for name, p in rules.rule_providers("jsdelivr").items():
            if p["format"] == "yaml":
                self.assertTrue(p["path"].endswith(".yaml"), name)


if __name__ == "__main__":
    unittest.main()
