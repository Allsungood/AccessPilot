"""回归测试: 按吞吐选节点 + 记忆里的 AI 节点不能盲钉.

## 两个真实故障(2026-10-03, 用户的原话是"网速慢！")

**一、工具按「延迟」选节点, 而用户感受到的是「吞吐」。** 在免费节点上这两件事
几乎不相关, 实测同一批里:

    EPODONIOS #1329   291 KB/s        🇩🇪 德国 | DEU    22.8 KB/s
    🇫🇷_法国_102       197 KB/s        socks5-47.242...   2.3 KB/s

相差上百倍而延迟差不多。所以"自动选择最快的节点"挑的是**响应最快**的,
不是**下得动**的 —— 用户看到的是"连接是通的但很慢"。

**二、钉住的 ChatGPT 节点死了, 却一直钉着。** `_ensure_ai_pin` 原来在"记忆没过期"
时**一次都不测**, 直接把节点原样钉回去。于是节点死后的十几分钟里, 每一轮健康检查
都在往一个死节点上钉。用户的体感是"X 和 Discord 都能开, 就 ChatGPT 打不开"。

## 为什么用源码级锁

这两处都是**时序/分支**上的疏漏, 不是纯函数能覆盖的:
* 第一条要跑真实的网络下载才测得出来(而单测不能打真实网络);
* 第二条是"某个分支里少做了一件事", 用 mock 把 `_ensure_ai_pin` 整条跑通
  成本很高, 而**漏掉的正是那个分支**。
所以锁在源码上, 并写清为什么 —— 这类"防呆只做了一半"的坑, 靠 review 是看不住的。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot import freenodes, health  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class RankBySpeedTests(unittest.TestCase):
    def test_sorts_by_throughput_desc(self) -> None:
        with mock.patch.object(freenodes, "measure_speed", side_effect=[
                {"name": "a", "kbps": 10.0},
                {"name": "b", "kbps": 300.0},
                {"name": "c", "kbps": 50.0}]):
            rows = freenodes.rank_by_speed(None, ["a", "b", "c"])
        self.assertEqual([r["name"] for r in rows], ["b", "c", "a"])

    def test_failed_node_sinks_to_the_bottom(self) -> None:
        """测不出来的节点没有 kbps, 不能因此排到最前面。"""
        with mock.patch.object(freenodes, "measure_speed", side_effect=[
                {"name": "dead", "ok": False, "detail": "URLError"},
                {"name": "ok", "kbps": 5.0}]):
            rows = freenodes.rank_by_speed(None, ["dead", "ok"])
        self.assertEqual(rows[0]["name"], "ok")

    def test_progress_is_reported(self) -> None:
        seen: list[tuple[int, int]] = []
        with mock.patch.object(freenodes, "measure_speed",
                               side_effect=[{"name": n, "kbps": 1.0}
                                            for n in "abc"]):
            freenodes.rank_by_speed(None, ["a", "b", "c"],
                                    progress=lambda i, n: seen.append((i, n)))
        self.assertEqual(seen, [(1, 3), (2, 3), (3, 3)])

    def test_empty_input(self) -> None:
        self.assertEqual(freenodes.rank_by_speed(None, []), [])


class MeasureSpeedTests(unittest.TestCase):
    def test_select_failure_is_reported_not_raised(self) -> None:
        with mock.patch("accesspilot.api.select", side_effect=OSError("no")):
            r = freenodes.measure_speed(object(), "x")
        self.assertFalse(r["ok"])
        self.assertEqual(r["kbps"], 0.0)
        self.assertIn("选择失败", r["detail"])

    def test_speed_target_is_not_cloudflare(self) -> None:
        """源码级锁: 别把测速目标换回 speed.cloudflare.com。

        它会对反复请求回 HTTP 429, 而 429 的响应体只有 1 字节 —— 测出来是
        "0 KB/s", 看起来像节点死了, 其实是**测速工具自己**被限流。这个假数据
        极难识别: 我当时读出过一个 291 KB/s, 换 cachefly 实测只有 31 KB/s,
        两个数字都像是真的。
        """
        self.assertNotIn("speed.cloudflare.com", freenodes.SPEED_TEST_URL)
        self.assertTrue(freenodes.SPEED_TEST_URL.startswith("https://"))


class QuickAiCheckTests(unittest.TestCase):
    def _st(self):
        st = mock.MagicMock()
        st.mixed_port = 7890
        return st

    def test_select_failure_is_not_usable(self) -> None:
        with mock.patch.object(health.api, "select", side_effect=OSError("x")):
            r = health._quick_ai_check(self._st(), "n", timeout=1.0)
        self.assertFalse(r["chatgpt_ok"])
        self.assertIn("选择失败", r["detail"])

    def test_request_failure_is_not_usable(self) -> None:
        """节点死了就是死在这里 —— 必须返回"不可用"而不是抛异常。

        它是在健康轮的中间被调用的, 抛出去会把整轮带崩, 那比少测一次糟得多。
        """
        with mock.patch.object(health.api, "select"), \
             mock.patch.object(health, "http_request", side_effect=OSError("dead")):
            r = health._quick_ai_check(self._st(), "n", timeout=1.0)
        self.assertFalse(r["chatgpt_ok"])
        self.assertEqual(r["detail"], "OSError")

    def test_200_is_usable_and_records_region(self) -> None:
        with mock.patch.object(health.api, "select"), \
             mock.patch.object(health, "http_request",
                               return_value=(200, {}, b"ip=1.2.3.4\nloc=sg\n")):
            r = health._quick_ai_check(self._st(), "n", timeout=1.0)
        self.assertTrue(r["chatgpt_ok"])
        self.assertEqual(r["chatgpt_loc"], "SG")

    def test_403_is_alive_but_not_usable(self) -> None:
        """403 = 出口地区不受支持。活着但**不能**算可用, 否则又钉回一个打不开的。"""
        with mock.patch.object(health.api, "select"), \
             mock.patch.object(health, "http_request", return_value=(403, {}, b"")):
            r = health._quick_ai_check(self._st(), "n", timeout=1.0)
        self.assertFalse(r["chatgpt_ok"])
        self.assertEqual(r["chatgpt_status"], 403)


class NoBlindRepinTests(unittest.TestCase):
    """源码级锁: "记忆没过期"这个分支**必须**仍然实测一次。"""

    def setUp(self) -> None:
        self.src = (ROOT / "accesspilot" / "health.py").read_text(encoding="utf-8")

    def test_unexpired_memory_is_still_probed(self) -> None:
        m = re.search(
            r"if time\.time\(\) - remembered_at < _ai_recheck:\s*\n(.*?)\n        else:",
            self.src, re.S,
        )
        self.assertIsNotNone(m, "找不到「记忆未过期」那个分支 —— 代码结构变了, 请复核")
        self.assertIn("_quick_ai_check", m.group(1),
                      "没过期的记忆节点又被盲钉回去了: 节点死了会一直钉着, "
                      "用户看到 ChatGPT 十几分钟打不开")

    def test_stale_memory_is_forgotten(self) -> None:
        """确认失败之后要**忘掉**它, 否则下一轮还会再钉回去、再白测一次。"""
        m = re.search(
            r"if time\.time\(\) - remembered_at < _ai_recheck:\s*\n(.*?)\n        else:",
            self.src, re.S,
        )
        self.assertIn("_forget_ai_if", m.group(1))


class FreeAutoUsesSpeedTests(unittest.TestCase):
    def test_free_auto_ranks_verified_nodes_by_speed(self) -> None:
        src = (ROOT / "accesspilot" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("rank_by_speed", src,
                      "free auto 没有按吞吐挑节点 —— 延迟低不等于下得动")
        self.assertIn("--speed-seconds", src)

    def test_speed_step_runs_only_on_verified_nodes(self) -> None:
        """只在**已通过平台验证**的节点里测吞吐。

        几千个节点逐个测是不现实的; 而且没验证过的节点测出多快都没意义 ——
        它可能压根打不开 X。
        """
        src = (ROOT / "accesspilot" / "cli.py").read_text(encoding="utf-8")
        m = re.search(r"if len\(good\) > 1 and args\.speed_seconds > 0:(.*?)"
                      r"ai_capable = sorted", src, re.S)
        self.assertIsNotNone(m, "找不到吞吐测试那一段")
        self.assertIn('"name"] for r in good', m.group(1))


if __name__ == "__main__":
    unittest.main()
