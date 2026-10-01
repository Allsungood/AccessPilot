"""回归测试: 节点名必须在**清洗之后**才消除重名.

真实事故(2026-10-01): `freenodes.fetch_all()` 里的顺序原来是

    proxies = uniquify_names(dedupe(collected))   # 先去重名
    proxies = sanitize(proxies)                   # 后清洗

而 `sanitize` 会清洗名字里的控制字符 —— 那可能把两个原本**不同**的名字洗成
同一个:

    节点 A: "🇭🇰 香港 | HKG #3"
    节点 B: "🇭🇰 香港 | HKG #3\\x9f"      (订阅源里混进的控制字符)
    uniquify 后: A 与 B 不同, 通过
    sanitize 后: B 的 \\x9f 被洗掉 -> 两者都叫 "🇭🇰 香港 | HKG #3" -> 撞名

后果不是"少一个节点", 而是 mihomo 拒绝**整份配置**:

    level=error msg="proxy 🇭🇰 香港 | HKG #3 is the duplicate name"
    configuration file ... test failed

于是每小时一次的 `free auto` 都在配置校验那一步中断, 配置档被留在"几千个未经
验证的节点"的膨胀状态 —— 实测 6227 个节点里有 10 组重名。这和 2026-09-30 那次
"一个节点的控制字符让 6027 个节点整份报废"是同一类事故: 一个坏数据点,
整份配置陪葬。

这个文件同时锁两件事:
  1. 流水线的顺序必须是 dedupe -> sanitize -> uniquify;
  2. 清洗**之后**再消除重名, 撞名必须被拆开(而不是被丢掉)。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot import config, subscription  # noqa: E402

#: 一个 C1 控制字符。订阅源里把 UTF-8 的 emoji 按 Latin-1 解码时就会产出它。
CTRL = "\x9f"


def _node(name: str, server: str = "1.2.3.4") -> dict:
    return {
        "name": name, "type": "ss", "server": server, "port": 443,
        "cipher": "aes-128-gcm", "password": "p",
    }


class OrderingMatters(unittest.TestCase):
    def test_cleaning_can_create_collisions(self) -> None:
        """先决条件: 这两个名字在清洗前是不同的 —— 否则后面的断言没有意义."""
        a, b = "🇭🇰 香港 | HKG #3", "🇭🇰 香港 | HKG #3" + CTRL
        self.assertNotEqual(a, b)
        self.assertEqual(config.sanitize_proxies([_node(b)])[0]["name"], a,
                         "清洗后应该和 A 撞名 —— 这正是事故的成因")

    def test_sanitize_then_uniquify_keeps_names_unique(self) -> None:
        nodes = [_node("🇭🇰 香港 | HKG #3", "1.1.1.1"),
                 _node("🇭🇰 香港 | HKG #3" + CTRL, "2.2.2.2")]
        out = subscription.uniquify_names(config.sanitize_proxies(nodes))
        names = [p["name"] for p in out]
        self.assertEqual(len(names), len(set(names)), f"仍然重名: {names}")

    def test_old_order_would_collide(self) -> None:
        """把老顺序反过来跑一遍, 证明它确实会撞 —— 免得有人把顺序改回去."""
        nodes = [_node("🇭🇰 香港 | HKG #3", "1.1.1.1"),
                 _node("🇭🇰 香港 | HKG #3" + CTRL, "2.2.2.2")]
        old = config.sanitize_proxies(subscription.uniquify_names(nodes))
        names = [p["name"] for p in old]
        self.assertNotEqual(len(names), len(set(names)),
                            "老顺序居然没撞? 那说明 sanitize 不再改名字了, "
                            "请重新评估这条注释是否还成立")

    def test_collision_is_renamed_not_dropped(self) -> None:
        """撞名要拆开而不是丢掉 —— 两个节点是**不同的服务器**, 都有用."""
        nodes = [_node("同名", "1.1.1.1"), _node("同名" + CTRL, "2.2.2.2")]
        out = subscription.uniquify_names(config.sanitize_proxies(nodes))
        self.assertEqual(len(out), 2, "不该因为重名丢节点")
        servers = sorted(p["server"] for p in out)
        self.assertEqual(servers, ["1.1.1.1", "2.2.2.2"])


class PipelineOrderInSource(unittest.TestCase):
    """源码级锁: 顺序写反了是最容易犯的错, 而且单测不一定拦得住."""

    def test_fetch_all_sanitizes_before_uniquify(self) -> None:
        src = (Path(__file__).resolve().parent.parent
               / "accesspilot" / "freenodes.py").read_text(encoding="utf-8")
        i_san = src.index("proxies = sanitize(sub_mod.dedupe(collected))")
        i_uniq = src.index("proxies = sub_mod.uniquify_names(proxies)")
        self.assertLess(i_san, i_uniq, "顺序必须是 sanitize 在前、uniquify 在后")

    def test_no_leftover_old_order(self) -> None:
        src = (Path(__file__).resolve().parent.parent
               / "accesspilot" / "freenodes.py").read_text(encoding="utf-8")
        self.assertNotIn("uniquify_names(sub_mod.dedupe(collected))", src)

    def test_parse_content_order(self) -> None:
        """订阅解析那条路 (`subscription.parse_content`) 也必须是先清洗后取名."""
        src = (Path(__file__).resolve().parent.parent
               / "accesspilot" / "subscription.py").read_text(encoding="utf-8")
        self.assertIn("uniquify_names(dedupe(proxies))", src,
                      "parse_content 里没有 sanitize(它拿不到 config), "
                      "所以这里保持原样 —— 但如果哪天加了清洗, 顺序必须跟着改")


if __name__ == "__main__":
    unittest.main()
