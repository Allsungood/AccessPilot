"""单元测试: 平台连通性诊断(离线, 不碰真实网络).

重点覆盖真实踩过的坑:
  * Cloudflare 会对缺少 Sec-Fetch-*/sec-ch-ua 的请求直接 403 ——
    实测 chatgpt.com 只带 UA/Accept/Accept-Language 一律 403,
    补上这组"现代浏览器"头立刻 200。少了它们会把"其实能用"的
    节点误判成"IP 被拒绝", 从而把好节点整批丢掉;
  * Cloudflare 拦截判定不能用 "blocked" / "cloudflare" 这种泛词 ——
    chatgpt.com 的真实页面里就含有 `"offlineBlocked":["boolean",false]`
    和 `Cloudflare-Workers-Version-Overrides`, 泛词匹配必然误报。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot.diag import BROWSER_HEADERS, _chatgpt_validator  # noqa: E402

#: 照真实 chatgpt.com 的 200 响应构造, 里面故意埋了两个"陷阱"字符串
REAL_CHATGPT_PAGE = (
    '<!DOCTYPE html>\n<html class="xsw4dja x108lcm5" data-build="prod-7909290517810">'
    '<div id="root"></div>'
    '<script>["offlineBlocked",["boolean",false]]</script>'
    "<script>const _ = `Cloudflare-Workers-Version-Overrides`;</script>"
).encode()

#: Cloudflare 挑战页, 必须判为失败
CF_CHALLENGE_PAGE = (
    "<html><head><title>Just a moment...</title></head>"
    '<body><div id="cf-chl-widget-abc"></div>'
    "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'></script>"
    "</body></html>"
).encode()


class BrowserHeadersTest(unittest.TestCase):
    def test_has_cloudflare_gate_headers(self) -> None:
        for key in (
            "Sec-Fetch-Mode",
            "Sec-Fetch-Site",
            "Sec-Fetch-User",
            "Sec-Fetch-Dest",
            "Upgrade-Insecure-Requests",
            "sec-ch-ua",
            "sec-ch-ua-mobile",
            "sec-ch-ua-platform",
        ):
            self.assertIn(key, BROWSER_HEADERS, f"缺少 {key}, Cloudflare 会返回 403")

    def test_keeps_basic_headers(self) -> None:
        for key in ("User-Agent", "Accept", "Accept-Language"):
            self.assertIn(key, BROWSER_HEADERS)


class ChatGptValidatorTest(unittest.TestCase):
    def test_real_page_with_trap_strings_passes(self) -> None:
        """真实页面含 offlineBlocked / Cloudflare-Workers 也不能误判。"""
        ok, detail = _chatgpt_validator(200, REAL_CHATGPT_PAGE)
        self.assertTrue(ok, f"真实页面被误判为失败: {detail}")
        self.assertIn("真实页面", detail)

    def test_real_page_without_build_marker_passes(self) -> None:
        ok, detail = _chatgpt_validator(200, b"<html><body>hello</body></html>")
        self.assertTrue(ok, detail)

    def test_challenge_page_rejected(self) -> None:
        ok, detail = _chatgpt_validator(200, CF_CHALLENGE_PAGE)
        self.assertFalse(ok, "Cloudflare 挑战页必须判为失败")
        self.assertIn("Cloudflare", detail)

    def test_unsupported_country_rejected(self) -> None:
        ok, detail = _chatgpt_validator(200, b'{"error":"unsupported_country"}')
        self.assertFalse(ok)
        self.assertIn("地区", detail)

    def test_403_rejected(self) -> None:
        ok, detail = _chatgpt_validator(403, b"<html>Access denied</html>")
        self.assertFalse(ok)
        self.assertIn("403", detail)

    def test_500_rejected(self) -> None:
        ok, _ = _chatgpt_validator(500, b"boom")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
