"""回归测试: 通用流量(🚀 节点选择)不能挂在"自动选择"上.

## 真实故障(2026-10-03, 用户直接受影响)

`free auto` 跑完把 `🚀 节点选择` 指向了 `♻️ 自动选择`(一个 url-test 组)。
url-test **只按延迟挑**, 于是它选中了 `🇺🇸_美国_114`(246 ms, 全场第三快) ——
而那个节点**根本没通过平台验证**。

现象很迷惑人:
    chatgpt.com -> 200      ← 因为 ChatGPT 走被单独钉住的 🤖 AI 服务
    x.com       -> 000      ← 连不上
    discord.com -> 000      ← 连不上

用户看到的是"代理明明开着, 但 X 和 Discord 打不开"。

## 为什么这是"同一个坑的第二次"

代码里早就有一条注释写着 `🤖 AI 服务 **绝不能**跟随自动选择: url-test 只挑最快,
完全不看出口地区`, 并且真的做了防呆 —— 但那条**只做在 AI 组上**, 因为当时只踩过
"自动选择挑中香港 -> ChatGPT 403"。

这次证明:**同样的推理对通用流量一样成立**, 只是判据从"出口地区在不在
OpenAI 支持列表"换成"X / Discord 到底能不能打开"。两条判据都不是延迟能替代的。

所以这里锁两层:
1. `fully_usable()` 的语义必须是**三项全过**(X 主页 + X 静态资源 + Discord) ——
   少一项就回到"看着通、实际打不开";
2. 源码级锁: `api.select(..., G_SELECT, G_AUTO)` 这个调用**必须**被
   `if not select_node` 挡住 —— 没有验证过的节点时才能退化到自动选择。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot import freenodes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class FullyUsableTests(unittest.TestCase):
    def test_all_three_required(self) -> None:
        ok = {"x_ok": True, "x_asset_ok": True, "discord_ok": True}
        self.assertTrue(freenodes.fully_usable(ok))

    def test_missing_discord_is_not_usable(self) -> None:
        self.assertFalse(freenodes.fully_usable(
            {"x_ok": True, "x_asset_ok": True, "discord_ok": False}))

    def test_missing_x_asset_is_not_usable(self) -> None:
        """只测 X 主页会出假阳性: HTTP 200 但页面永远加载不出来(真实踩过)."""
        self.assertFalse(freenodes.fully_usable(
            {"x_ok": True, "x_asset_ok": False, "discord_ok": True}))

    def test_empty_probe_is_not_usable(self) -> None:
        self.assertFalse(freenodes.fully_usable({}))


class SelectGroupPinningTests(unittest.TestCase):
    """源码级锁 —— 这类"防呆只做了一半"的疏漏, 靠人 review 是看不住的。"""

    def setUp(self) -> None:
        self.src = (ROOT / "accesspilot" / "cli.py").read_text(encoding="utf-8")

    def test_g_select_is_pinned_to_a_verified_node(self) -> None:
        self.assertRegex(
            self.src,
            r"api\.select\(st,\s*rules\.G_SELECT,\s*select_node\)",
            "🚀 节点选择 没有钉到经过平台验证的节点上",
        )

    def test_auto_group_only_as_a_fallback(self) -> None:
        """退化到自动选择必须被 `if not select_node` 挡住。

        不加这道门, 就会退回到"按延迟挑最快" —— 而那正是这个 bug 的成因。
        """
        m = re.search(
            r"if not select_node:\s*\n\s*for group in \(rules\.G_SELECT,\):\s*\n"
            r"\s*try:\s*\n\s*api\.select\(st, group, rules\.G_AUTO\)",
            self.src,
        )
        self.assertIsNotNone(
            m, "G_SELECT -> G_AUTO 没有被 `if not select_node` 保护")

    def test_ai_group_pinning_still_there(self) -> None:
        """顺手确认没把原有的 AI 组防呆改坏."""
        self.assertRegex(self.src, r"api\.select\(st,\s*rules\.G_AI,\s*ai_node\)")

    def test_fallback_says_why(self) -> None:
        """退化时必须告诉用户原因, 否则"X 打不开"又要从头查一遍."""
        self.assertIn("不保证能打开 X / Discord", self.src)


class SharedExitTests(unittest.TestCase):
    """两个组要尽量共用**同一个出口节点**。

    ## 真实故障(2026-10-03, 用户拿 Google 的报错来问)

        "我们的系统检测到您的计算机网络中存在异常流量"
        IP 地址：84.239.42.49 ≠ 172.245.237.196

    原因是线上配置里:
        gemini.google.com -> 🤖 AI 服务     (钉在节点 A)
        gstatic.com       -> 🚀 节点选择     (钉在节点 B)
        googleapis.com    -> 🚀 节点选择
        google.com        -> 🚀 节点选择

    一次 Gemini 页面加载同时用到两组域名, 于是**同一个会话从两个 IP 出去**,
    Google 判定成异常流量。

    佐证(同一个代理, 两个目标两个出口):
        api.ipify.org             -> 84.239.42.49
        chatgpt.com/cdn-cgi/trace -> 103.106.229.236

    这个坑**不限于 Google**: 任何跨组用域名的站点都会这样。
    """

    def setUp(self) -> None:
        self.src = (ROOT / "accesspilot" / "cli.py").read_text(encoding="utf-8")

    def test_prefers_a_node_that_satisfies_both_groups(self) -> None:
        # 缩进不写死: 这段代码嵌在几层 if 里, 用 \s* 匹配以免挪一层就失效。
        m = re.search(
            r"if speed:\s*\n\s*shared = \[r\[\"name\"\] for r in ai_capable\]"
            r"(.*?)\n\s*else:", self.src, re.S)
        self.assertIsNotNone(m, "找不到共用出口那一段 —— 代码结构变了, 请复核")
        # 关键: 选了 shared 里的节点之后, **两个组都要用它是同一个值**
        self.assertIn("ai_node = select_node", m.group(1),
                      "只改了通用组没改 AI 组, 两个出口还是不一样")

    def test_warns_when_sharing_is_impossible(self) -> None:
        """找不到两边都行的节点时必须说明后果, 不能默默分两个出口。"""
        self.assertIn("两个来源 IP", self.src)


class PickBestNodeKeepsOneExitTests(unittest.TestCase):
    """界面上的「自动选最优」也必须维持出口一致。

    这个按钮原来**只切 🚀 节点选择**, 不碰 🤖 AI 服务 —— 用户随手点一下,
    两个组就又指向不同节点了, 而 `free auto` 里做的对齐会被它破坏掉。
    真实后果就是那条 Google 报错(同一会话两个来源 IP)。

    出口一致性是个**不变量**, 只在某一个入口维持是不够的。
    """

    def setUp(self) -> None:
        self.src = (ROOT / "accesspilot" / "control.py").read_text(encoding="utf-8")

    def test_prefers_a_node_usable_for_both_groups(self) -> None:
        self.assertIn("shared = [(n, r) for n, r in probes", self.src,
                      "pick_best_node 没有优先选「两样都行」的节点")
        self.assertIn("if freenodes.chatgpt_usable(r):", self.src)

    def test_also_aligns_the_ai_group(self) -> None:
        m = re.search(r"def pick_best_node\(.*?\n(?=def )", self.src, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertIn("rules.G_AI", body,
                      "pick_best_node 只切了通用组, AI 组还指着别的节点 —— "
                      "出口会分叉, Google 会报异常流量")


if __name__ == "__main__":
    unittest.main()
