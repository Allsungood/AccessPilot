"""回归测试: ChatGPT 的拦路虎是**出口地区**, 不是"连得上".

真实故障(2026-09, 用户截图里的 "Unable to load site"):
    每小时一次的 `accesspilot free auto` 会把 🤖 AI 服务 组重置成
    "🚀 节点选择" -> "♻️ 自动选择"(url-test) -> 选中**最快**的节点。
    当时最快的是香港节点: X / Discord / Google 全绿, 但 ChatGPT 返回
    HTTP 403 —— OpenAI 的支持地区列表里没有中国香港。
    网页于是显示 "Unable to load site"(截图里的 IP:2403:27c0:... 就是它)。

这次故障证伪了两个看起来很有道理的假设, 都用测试钉死:

  1. "把 AI 组的探测地址换成 chatgpt.com, url-test 就会自动跳过被封地区"
     —— **错的**。内核的健康检查把 403 当成成功: 实测香港节点对
     `https://chatgpt.com/cdn-cgi/trace` 报 "121 ms 成功",
     韩国节点报 "4120 ms 成功", 两者在 url-test 眼里没有区别。

  2. "X / Discord 能打开就说明这个节点能用" —— **错的**。ChatGPT 额外受
     "出口国家是否在 OpenAI 支持列表"限制, 必须**单独实测**并在事后钉住,
     否则下一次节点池刷新就会把它冲掉。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot import freenodes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def resp(status: int, body: bytes = b"ok") -> tuple[int, dict, bytes]:
    return status, {}, body


def trace_body(loc: str, ip: str = "1.2.3.4") -> bytes:
    return f"fl=1a2\nip={ip}\nloc={loc}\ncolo=ICN\nwarp=off\n".encode()


class TestChatGPTProbe(unittest.TestCase):
    def setUp(self) -> None:
        self.st = mock.MagicMock()
        self.st.mixed_port = 7890
        self.st.api_secret = "s"
        self.st.api_base.return_value = "http://127.0.0.1:9090"

    def _patch(self, responses: dict[str, tuple[int, dict, bytes]]) -> None:
        def fake(url: str, **kw):
            for key, r in responses.items():
                if key in url:
                    return r
            return 500, {}, b""

        p = mock.patch("accesspilot.util.http_request", side_effect=fake)
        self.addCleanup(p.stop)
        p.start()

    def _ok_x_discord(self) -> dict[str, tuple[int, dict, bytes]]:
        return {
            "https://x.com/": resp(200),
            "https://abs.twimg.com/": resp(200),
            "https://discord.com/api/v9/gateway": resp(200),
        }

    def test_trace_200_is_usable_and_loc_parsed(self) -> None:
        with mock.patch("accesspilot.api.select"):
            self._patch({**self._ok_x_discord(),
                         freenodes.CHATGPT_TRACE_URL: resp(200, trace_body("KR"))})
            r = freenodes.verify_node(self.st, "韩国节点")
        self.assertTrue(freenodes.chatgpt_usable(r))
        self.assertEqual(r["chatgpt_loc"], "KR")
        self.assertEqual(r["chatgpt_status"], 200)

    def test_trace_403_is_not_usable(self) -> None:
        """香港: X/Discord 全绿, ChatGPT 却是 403 —— 这次故障的核心."""
        with mock.patch("accesspilot.api.select"):
            self._patch({**self._ok_x_discord(),
                         freenodes.CHATGPT_TRACE_URL: resp(403, b"blocked")})
            r = freenodes.verify_node(self.st, "香港节点")
        self.assertTrue(freenodes.fully_usable(r), "X/Discord 确实是好的")
        self.assertFalse(freenodes.chatgpt_usable(r), "但 ChatGPT 用不了")
        self.assertEqual(r["chatgpt_status"], 403)

    def test_x_discord_green_does_not_imply_chatgpt(self) -> None:
        """把"X/Discord 全绿 != ChatGPT 能用"这条钉死, 防回归."""
        with mock.patch("accesspilot.api.select"):
            self._patch({**self._ok_x_discord(),
                         freenodes.CHATGPT_TRACE_URL: resp(403, b"blocked")})
            r = freenodes.verify_node(self.st, "节点")
        self.assertTrue(freenodes.fully_usable(r))
        self.assertNotEqual(freenodes.fully_usable(r), freenodes.chatgpt_usable(r))

    def test_connection_failure_marks_unusable(self) -> None:
        with mock.patch("accesspilot.api.select"):
            self._patch({**self._ok_x_discord()})  # trace 落到默认 500
            r = freenodes.verify_node(self.st, "坏节点")
        self.assertFalse(freenodes.chatgpt_usable(r))

    def test_probe_url_is_trace_not_homepage(self) -> None:
        """锁定探测地址: 必须是 /cdn-cgi/trace(轻、且地区信息在响应里)."""
        self.assertEqual(freenodes.CHATGPT_TRACE_URL,
                         "https://chatgpt.com/cdn-cgi/trace")

    def test_select_failure_has_all_keys(self) -> None:
        """选择失败时的返回字典也要带 chatgpt_* 键, 否则下游 KeyError."""
        with mock.patch("accesspilot.api.select", side_effect=Exception("boom")):
            r = freenodes.verify_node(self.st, "节点")
        for k in ("chatgpt_ok", "chatgpt_loc", "chatgpt_status"):
            self.assertIn(k, r)


class TestChatGPTReason(unittest.TestCase):
    def test_blocked_region_named_in_chinese(self) -> None:
        msg = freenodes.chatgpt_reason(
            {"chatgpt_status": 403, "chatgpt_loc": "HK"})
        self.assertIn("中国香港", msg)
        self.assertIn("403", msg)

    def test_unknown_blocked_region_still_reports(self) -> None:
        msg = freenodes.chatgpt_reason(
            {"chatgpt_status": 403, "chatgpt_loc": "ZZ"})
        self.assertIn("地区不受支持", msg)

    def test_timeout_reported(self) -> None:
        self.assertIn("连接失败", freenodes.chatgpt_reason({"chatgpt_status": 0}))

    def test_ok_reports_exit_country(self) -> None:
        self.assertIn("KR", freenodes.chatgpt_reason(
            {"chatgpt_status": 200, "chatgpt_loc": "KR"}))

    def test_hk_and_cn_are_known_blocked(self) -> None:
        for code in ("HK", "CN", "MO"):
            self.assertIn(code, freenodes.CHATGPT_BLOCKED_REGIONS)


class TestCurrentSelection(unittest.TestCase):
    """读内核里当前钉住的节点 —— "别把能用的换掉" 依赖它."""

    def test_reads_now(self) -> None:
        st = mock.MagicMock()
        with mock.patch("accesspilot.api.proxies",
                        return_value={"🤖 AI 服务": {"now": "韩国节点"}}):
            self.assertEqual(
                freenodes.current_selection(st, "🤖 AI 服务"), "韩国节点")

    def test_missing_group_returns_empty(self) -> None:
        st = mock.MagicMock()
        with mock.patch("accesspilot.api.proxies", return_value={}):
            self.assertEqual(freenodes.current_selection(st, "🤖 AI 服务"), "")

    def test_api_error_returns_empty_not_raise(self) -> None:
        """内核没起来/接口挂了不能把整个 free auto 带崩."""
        st = mock.MagicMock()
        with mock.patch("accesspilot.api.proxies", side_effect=Exception("down")):
            self.assertEqual(freenodes.current_selection(st, "🤖 AI 服务"), "")


class TestFreeAutoPinsAI(unittest.TestCase):
    """源码级回归锁.

    这次故障没有任何单元测试能自然覆盖(它发生在编排层), 所以直接锁住
    那行**已知会致故障**的写法: 把 G_AI 和 G_SOCIAL/G_MEDIA 一起无条件
    重置成 G_SELECT。
    """

    def setUp(self) -> None:
        self.src = (ROOT / "accesspilot" / "cli.py").read_text(encoding="utf-8")

    def test_ai_group_not_reset_with_social_and_media(self) -> None:
        pattern = re.compile(
            r"for\s+group\s+in\s*\(\s*rules\.G_AI\s*,\s*rules\.G_SOCIAL\s*,\s*rules\.G_MEDIA\s*\)"
        )
        self.assertIsNone(
            pattern.search(self.src),
            "🤖 AI 服务 不能和社交/流媒体一起无条件重置 —— "
            "那会让它跟随 url-test 选中香港节点, ChatGPT 直接 403",
        )

    def test_ai_group_is_pinned_to_verified_node(self) -> None:
        self.assertRegex(
            self.src,
            r"api\.select\(\s*st\s*,\s*rules\.G_AI\s*,\s*ai_node\s*\)",
            "free auto 必须把实测能上 ChatGPT 的节点钉进 🤖 AI 服务",
        )

    def test_chatgpt_capable_nodes_survive_pruning(self) -> None:
        """能上 ChatGPT 但 X/Discord 没过的节点不能被 prune 删掉,
        否则钉进 AI 组的选择会指向一个不存在的节点。"""
        self.assertIn("keep[r[\"name\"]] = 99999", self.src)

    def test_no_defeatist_claim_in_output(self) -> None:
        """旧版本会打印"免费节点上不了 ChatGPT" —— 已被实测证伪."""
        self.assertNotIn("但上不了 ChatGPT", self.src)

    def test_refresh_keeps_a_working_pinned_node(self) -> None:
        """刷新出新一批节点却没有能上 ChatGPT 的时, 不能把当前能用的换掉."""
        self.assertIn("current_selection", self.src)
        self.assertRegex(
            self.src,
            r"keep\.setdefault\(\s*pinned\s*,",
            "保留原有 AI 节点时必须把它留在 keep 里, 否则会被 prune 删掉",
        )


if __name__ == "__main__":
    unittest.main()
