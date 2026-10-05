"""单元测试: 网页控制台的写操作防护 + 生成配置里的监听地址.

事故背景(webgui): 控制台**只按 URL 路径分发**请求, 令牌校验写在
`if method in ("POST", "PUT", "DELETE")` 分支里, 而路由根本不看方法 ——
于是 `GET /api/stop` 会真的停掉内核, `GET /api/proxy` 会真的关掉系统代理。
任何用户访问的网页只要放一张 `<img src="http://127.0.0.1:9099/api/stop">`
就能悄悄断掉他的网(用户看到的现象只是"网又断了", 永远查不到是哪个网页干的),
而模块文档里还写着"已做 CSRF 防护"。这里把修复锁死:
GET 一律只读; 写操作必须 POST + 会话令牌 + Host/Origin 校验。

事故背景(config): DNS 监听地址被写死成 `0.0.0.0:1053`, 即使没开局域网共享,
`netstat` 上也是 `0.0.0.0:1053 LISTENING`。同一张网(咖啡厅/宿舍/办公室)里
任何人都能拿它当免费 DNS 解析器: 做放大攻击的跳板, 或者反过来窥探、投毒这台
机器的解析结果 —— 而用户以为自己只是开了个代理。

本文件不监听任何端口、不碰真实系统代理/注册表: 校验逻辑是纯函数,
路由用假请求对象直接调用。
"""
from __future__ import annotations

import collections
import contextlib
import inspect
import io
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401  隔离数据目录

from accesspilot import config, process, webgui  # noqa: E402
from accesspilot.state import AppState, load_state, save_state  # noqa: E402
from accesspilot.subscription import Subscription  # noqa: E402

PORT = 9099
TOKEN = "unit-test-token-0123456789abcdef"
OWN_ORIGIN = f"http://127.0.0.1:{PORT}"

Reply = collections.namedtuple("Reply", "code data raw")


# --------------------------------------------------------------------------- #
# 假请求: 直接驱动 Handler._dispatch, 不监听端口
# --------------------------------------------------------------------------- #


def make_handler(
    path: str,
    method: str = "GET",
    hdrs: dict[str, str] | None = None,
    body: bytes = b"",
    *,
    port: int = PORT,
) -> webgui.Handler:
    """造一个不接网线的 Handler(只填 _dispatch 需要的那几个属性)."""
    handler = webgui.Handler.__new__(webgui.Handler)
    handler.path = path
    handler.command = method
    handler.requestline = f"{method} {path} HTTP/1.1"  # log_request 会取它拼日志
    handler.request_version = "HTTP/1.1"
    handler.headers = dict(hdrs or {})
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.close_connection = False
    handler.client_address = ("127.0.0.1", 54321)
    handler.server = types.SimpleNamespace(server_address=("127.0.0.1", port))
    return handler


def dispatch(
    path: str,
    method: str = "GET",
    hdrs: dict[str, str] | None = None,
    body: dict | None = None,
    *,
    port: int = PORT,
) -> Reply:
    """走一遍真正的分发逻辑, 返回 (状态码, 解析后的 JSON, 原始响应体)."""
    payload = b"" if body is None else json.dumps(body).encode()
    headers = dict(hdrs or {})
    if payload:
        headers["Content-Length"] = str(len(payload))
    handler = make_handler(path, method, headers, payload, port=port)
    # 令牌换成测试常量, 免得断言跟着每次随机生成的会话令牌漂移
    with mock.patch.object(webgui, "session_token", return_value=TOKEN):
        handler._dispatch(method)
    raw = handler.wfile.getvalue()
    head, _, rest = raw.partition(b"\r\n\r\n")
    code = int(head.split(b" ", 2)[1])
    try:
        data = json.loads(rest.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        data = None
    return Reply(code, data, rest)


def req_headers(
    *,
    host: str | None = f"127.0.0.1:{PORT}",
    origin: str | None = None,
    token: str | None = None,
) -> dict[str, str]:
    hdrs: dict[str, str] = {}
    if host is not None:
        hdrs["Host"] = host
    if origin is not None:
        hdrs["Origin"] = origin
    if token is not None:
        hdrs[webgui.TOKEN_HEADER] = token
    return hdrs


def _sub() -> Subscription:
    return Subscription(
        name="t",
        proxies=[
            {
                "name": "HK-01",
                "type": "ss",
                "server": "1.1.1.1",
                "port": 443,
                "cipher": "aes-128-gcm",
                "password": "x",
            }
        ],
    )


# --------------------------------------------------------------------------- #
# BUG 1: GET 不得触发任何写操作
# --------------------------------------------------------------------------- #


class TestWriteRoutesRejectGet(unittest.TestCase):
    """回归: `<img src="http://127.0.0.1:9099/api/stop">` 曾经真的能停掉代理."""

    def assertRejected(self, *args, **kwargs) -> tuple[int, str]:
        reject = webgui.validate_request(*args, **kwargs)
        self.assertIsNotNone(reject, "本应被拒绝的请求被放行了")
        return reject  # type: ignore[return-value]

    def test_every_write_route_rejects_get(self) -> None:
        for path in sorted(webgui.WRITE_API):
            with self.subTest(path=path):
                code, _ = self.assertRejected(
                    "GET", path, req_headers(), {}, port=PORT, token=TOKEN
                )
                self.assertEqual(code, 405, f"GET {path} 必须返回 405")

    def test_get_is_rejected_even_with_a_valid_token(self) -> None:
        """令牌只能证明"调用者可信", 不能把 GET 变成写操作."""
        for path in sorted(webgui.WRITE_API):
            with self.subTest(path=path):
                code, _ = self.assertRejected(
                    "GET",
                    path,
                    req_headers(token=TOKEN),
                    {"token": TOKEN},
                    port=PORT,
                    token=TOKEN,
                )
                self.assertEqual(code, 405)

    def test_read_only_routes_stay_get(self) -> None:
        for path in sorted(webgui.READ_ONLY_API):
            with self.subTest(path=path):
                self.assertIsNone(
                    webgui.validate_request(
                        "GET", path, req_headers(), {}, port=PORT, token=TOKEN
                    ),
                    f"只读接口 {path} 不该被拦",
                )

    def test_read_only_routes_reject_post(self) -> None:
        code, _ = self.assertRejected(
            "POST", "/api/status", req_headers(token=TOKEN), {}, port=PORT, token=TOKEN
        )
        self.assertEqual(code, 405)

    def test_route_tables_cover_every_api_handler(self) -> None:
        """漏登记的接口会静默退化成"未知接口", 所以用源码扫描逼它失败.

        新加接口时必须同时在 READ_ONLY_API / WRITE_API 里登记 —— 否则
        validate_request() 会对它返回"放行未知路径", 写操作又回到 GET 可触发。
        """
        src = inspect.getsource(webgui._handle_api)
        declared = set(re.findall(r'"(/api/[a-z/]+)"', src))
        registered = set(webgui.READ_ONLY_API) | set(webgui.WRITE_API)
        self.assertEqual(
            declared - registered, set(), "这些接口没登记进路由表(会绕过写操作校验)"
        )
        self.assertEqual(
            registered - declared, set(), "路由表里有 _handle_api 不认识的接口"
        )
        self.assertEqual(webgui.READ_ONLY_API & webgui.WRITE_API, frozenset())


class TestNothingMutatesWithoutAuthorization(unittest.TestCase):
    """恶意请求不仅要被拒, 而且必须**什么都没改**."""

    #: 会改本机状态的入口, 全部换成 mock: 一旦被误调用立刻现形
    SIDE_EFFECTS = (
        ("process.stop", lambda: mock.patch.object(process, "stop")),
        ("process.start", lambda: mock.patch.object(process, "start")),
        ("process.restart", lambda: mock.patch.object(process, "restart")),
        ("process.reload_config", lambda: mock.patch.object(process, "reload_config")),
        ("sysproxy.enable", lambda: mock.patch.object(webgui.sysproxy, "enable")),
        ("sysproxy.disable", lambda: mock.patch.object(webgui.sysproxy, "disable")),
        ("api.select", lambda: mock.patch.object(webgui.api, "select")),
    )

    def setUp(self) -> None:
        st = load_state()
        st.system_proxy_on = True
        st.tun_enable = True
        st.active_profile = ""
        save_state(st)

    def test_evil_requests_do_not_mutate(self) -> None:
        # 五种攻击形态 × 全部写接口
        variants = [
            ("GET 不带令牌(一张 <img> 就能造出来)", "GET", req_headers(), {}),
            ("POST 不带令牌", "POST", req_headers(), {}),
            ("POST 错令牌", "POST", req_headers(token="wrong-token"), {"token": "wrong-token"}),
            (
                "POST 外部 Origin",
                "POST",
                req_headers(origin="http://evil.example", token=TOKEN),
                {"token": TOKEN},
            ),
            (
                "POST 外部 Host(DNS-rebinding)",
                "POST",
                req_headers(host="evil.example:9099", token=TOKEN),
                {"token": TOKEN},
            ),
        ]
        with mock.patch.object(webgui.Handler, "log_message"), contextlib.ExitStack() as stack:
            mocks = {
                name: stack.enter_context(factory()) for name, factory in self.SIDE_EFFECTS
            }
            for label, method, hdrs, body in variants:
                for path in sorted(webgui.WRITE_API):
                    with self.subTest(variant=label, path=path):
                        reply = dispatch(path, method, hdrs, body)
                        self.assertIn(reply.code, (403, 405))
                        self.assertIsNone(reply.data.get("ok"))
            for name, mocked in mocks.items():
                self.assertFalse(mocked.called, f"{name} 被未授权的请求调用了")
        st = load_state()
        self.assertTrue(st.system_proxy_on, "系统代理状态被改动了")
        self.assertTrue(st.tun_enable, "TUN 状态被改动了")

    def test_get_on_proxy_route_does_not_disable_system_proxy(self) -> None:
        """最危险的一条: GET /api/proxy 以前会直接把系统代理关掉."""
        with mock.patch.object(webgui.Handler, "log_message"), mock.patch.object(
            webgui.sysproxy, "disable"
        ) as disable, mock.patch.object(webgui.sysproxy, "enable") as enable:
            reply = dispatch("/api/proxy", "GET", req_headers())
        self.assertEqual(reply.code, 405)
        disable.assert_not_called()
        enable.assert_not_called()
        self.assertTrue(load_state().system_proxy_on)


# --------------------------------------------------------------------------- #
# BUG 1: 令牌 / Origin / Host
# --------------------------------------------------------------------------- #


class TestWriteRequiresToken(unittest.TestCase):
    def assertRejected(self, *args, **kwargs) -> tuple[int, str]:
        reject = webgui.validate_request(*args, **kwargs)
        self.assertIsNotNone(reject, "本应被拒绝的请求被放行了")
        return reject  # type: ignore[return-value]

    def test_post_without_token_is_rejected(self) -> None:
        for path in sorted(webgui.WRITE_API):
            with self.subTest(path=path):
                code, _ = self.assertRejected(
                    "POST", path, req_headers(), {}, port=PORT, token=TOKEN
                )
                self.assertEqual(code, 403)

    def test_post_with_wrong_token_is_rejected(self) -> None:
        code, _ = self.assertRejected(
            "POST",
            "/api/stop",
            req_headers(token="not-the-token"),
            {"token": "not-the-token"},
            port=PORT,
            token=TOKEN,
        )
        self.assertEqual(code, 403)

    def test_old_static_header_is_not_a_token(self) -> None:
        """旧版那个固定的 `X-AccessPilot: 1` 必须失效(任何脚本都知道它)."""
        code, _ = self.assertRejected(
            "POST",
            "/api/stop",
            req_headers() | {"X-AccessPilot": "1"},
            {},
            port=PORT,
            token=TOKEN,
        )
        self.assertEqual(code, 403)

    def test_token_in_query_is_rejected(self) -> None:
        """令牌不得走 query: query 会经由 Referer / 历史记录泄漏出去."""
        with mock.patch.object(webgui.Handler, "log_message"), mock.patch.object(
            process, "stop"
        ) as stop:
            reply = dispatch(f"/api/stop?token={TOKEN}", "POST", req_headers())
        self.assertEqual(reply.code, 403)
        stop.assert_not_called()

    def test_post_with_token_header_is_accepted(self) -> None:
        self.assertIsNone(
            webgui.validate_request(
                "POST",
                "/api/stop",
                req_headers(token=TOKEN),
                {},
                port=PORT,
                token=TOKEN,
            )
        )

    def test_post_with_token_in_body_is_accepted(self) -> None:
        self.assertIsNone(
            webgui.validate_request(
                "POST", "/api/stop", req_headers(), {"token": TOKEN}, port=PORT, token=TOKEN
            )
        )


class TestOriginAndHostChecks(unittest.TestCase):
    def assertRejected(self, *args, **kwargs) -> tuple[int, str]:
        reject = webgui.validate_request(*args, **kwargs)
        self.assertIsNotNone(reject, "本应被拒绝的请求被放行了")
        return reject  # type: ignore[return-value]

    def test_foreign_origin_is_rejected(self) -> None:
        reject = self.assertRejected(
            "POST",
            "/api/stop",
            req_headers(origin="http://evil.example", token=TOKEN),
            {"token": TOKEN},
            port=PORT,
            token=TOKEN,
        )
        self.assertEqual(reject[0], 403)

    def test_null_origin_is_rejected(self) -> None:
        reject = self.assertRejected(
            "POST",
            "/api/stop",
            req_headers(origin="null", token=TOKEN),
            {"token": TOKEN},
            port=PORT,
            token=TOKEN,
        )
        self.assertEqual(reject[0], 403)

    def test_origin_on_another_port_is_rejected(self) -> None:
        reject = self.assertRejected(
            "POST",
            "/api/stop",
            req_headers(origin="http://127.0.0.1:1234", token=TOKEN),
            {"token": TOKEN},
            port=PORT,
            token=TOKEN,
        )
        self.assertEqual(reject[0], 403)

    def test_foreign_origin_is_rejected_for_reads_too(self) -> None:
        reject = self.assertRejected(
            "GET", "/api/status", req_headers(origin="http://evil.example"), {}, port=PORT
        )
        self.assertEqual(reject[0], 403)

    def test_own_origin_is_accepted(self) -> None:
        for origin in (OWN_ORIGIN, f"http://localhost:{PORT}"):
            with self.subTest(origin=origin):
                self.assertIsNone(
                    webgui.validate_request(
                        "POST",
                        "/api/stop",
                        req_headers(origin=origin, token=TOKEN),
                        {},
                        port=PORT,
                        token=TOKEN,
                    )
                )

    def test_foreign_host_is_rejected(self) -> None:
        """DNS-rebinding: evil.example 解析到 127.0.0.1, 浏览器会带着它来访问."""
        for host in ("evil.example:9099", "evil.example", "127.0.0.1.nip.io:9099"):
            with self.subTest(host=host):
                reject = self.assertRejected(
                    "POST",
                    "/api/stop",
                    req_headers(host=host, token=TOKEN),
                    {"token": TOKEN},
                    port=PORT,
                    token=TOKEN,
                )
                self.assertEqual(reject[0], 403)

    def test_missing_or_wrong_port_host_is_rejected(self) -> None:
        for host in (None, "", "127.0.0.1", "127.0.0.1:1234"):
            with self.subTest(host=host):
                reject = self.assertRejected(
                    "POST",
                    "/api/stop",
                    req_headers(host=host, token=TOKEN),
                    {"token": TOKEN},
                    port=PORT,
                    token=TOKEN,
                )
                self.assertEqual(reject[0], 403)

    def test_localhost_host_is_accepted(self) -> None:
        self.assertIsNone(
            webgui.validate_request(
                "POST",
                "/api/stop",
                req_headers(host=f"localhost:{PORT}", token=TOKEN),
                {},
                port=PORT,
                token=TOKEN,
            )
        )

    def test_foreign_host_blocks_the_dashboard_page(self) -> None:
        """首页里带着会话令牌, 被 rebinding 读到就等于令牌泄漏 —— 首页也拦."""
        reject = self.assertRejected(
            "GET", "/", req_headers(host="evil.example:9099"), {}, port=PORT
        )
        self.assertEqual(reject[0], 403)


# --------------------------------------------------------------------------- #
# BUG 1: 正常 UI 必须照常工作
# --------------------------------------------------------------------------- #


class TestLegitimateRequests(unittest.TestCase):
    def test_authorized_post_reaches_the_handler(self) -> None:
        with mock.patch.object(webgui.Handler, "log_message"), mock.patch.object(
            process, "start"
        ) as start:
            reply = dispatch(
                "/api/start", "POST", req_headers(origin=OWN_ORIGIN, token=TOKEN), {}
            )
        self.assertEqual(reply.code, 200)
        self.assertEqual(reply.data, {"ok": True})
        start.assert_called_once()

    def test_body_token_is_accepted_end_to_end(self) -> None:
        with mock.patch.object(webgui.Handler, "log_message"), mock.patch.object(
            webgui.sysproxy, "disable", return_value="已关闭 Windows 系统代理"
        ) as disable, mock.patch.object(webgui.sysproxy, "enable") as enable:
            reply = dispatch(
                "/api/proxy",
                "POST",
                req_headers(origin=OWN_ORIGIN),
                {"enable": False, "token": TOKEN},
            )
        self.assertEqual(reply.code, 200)
        self.assertTrue(reply.data["ok"])
        disable.assert_called_once()
        enable.assert_not_called()

    def test_read_only_get_works(self) -> None:
        with mock.patch.object(webgui.Handler, "log_message"), mock.patch.object(
            webgui, "_status_payload", return_value={"running": True}
        ) as payload:
            reply = dispatch("/api/status", "GET", req_headers(origin=OWN_ORIGIN))
        self.assertEqual(reply.code, 200)
        self.assertEqual(reply.data, {"running": True})
        payload.assert_called_once()

    def test_dashboard_page_is_served_with_the_session_token(self) -> None:
        with mock.patch.object(webgui.Handler, "log_message"):
            reply = dispatch("/", "GET", req_headers())
        self.assertEqual(reply.code, 200)
        self.assertIn(TOKEN.encode(), reply.raw)
        self.assertIn(webgui.TOKEN_HEADER.encode(), reply.raw)

    def test_injected_script_wraps_fetch_for_writes(self) -> None:
        """页面自己的 api() 会把写操作发成 POST, 包装脚本负责补令牌头."""
        html = webgui.inject_token(b"<html><script>1</script></html>")
        self.assertIn(b"X-AccessPilot-Token", html)
        self.assertIn(webgui.session_token().encode(), html)
        self.assertLess(html.index(b"window.fetch"), html.index(b"<script>1</script>"))

    def test_token_survives_asset_without_script_tag(self) -> None:
        html = webgui.inject_token(b"<html>no script here</html>")
        self.assertIn(webgui.session_token().encode(), html)


# --------------------------------------------------------------------------- #
# BUG 2: DNS 监听地址
# --------------------------------------------------------------------------- #


class TestDnsListenAddress(unittest.TestCase):
    def test_allow_lan_off_binds_loopback(self) -> None:
        st = AppState()
        self.assertFalse(st.allow_lan, "默认必须是关闭局域网共享")
        self.assertEqual(config.build_dns(st)["listen"], "127.0.0.1:1053")

    def test_allow_lan_off_stays_loopback_even_with_a_lan_ip(self) -> None:
        """没开局域网共享时, 就算查得到网卡地址也不许对外监听."""
        st = AppState()
        st.allow_lan = False
        with mock.patch.object(config, "lan_address", return_value="192.168.1.5"):
            listen = config.build_dns(st)["listen"]
        self.assertEqual(listen, "127.0.0.1:1053")
        self.assertFalse(listen.startswith("0.0.0.0"))

    def test_allow_lan_on_prefers_the_interface_address(self) -> None:
        st = AppState()
        st.allow_lan = True
        with mock.patch.object(config, "lan_address", return_value="192.168.1.5"):
            self.assertEqual(config.build_dns(st)["listen"], "192.168.1.5:1053")

    def test_allow_lan_on_without_detectable_address_falls_back_to_wildcard(self) -> None:
        st = AppState()
        st.allow_lan = True
        with mock.patch.object(config, "lan_address", return_value=None):
            self.assertEqual(config.dns_listen(st), "0.0.0.0:1053")

    def test_lan_address_never_returns_loopback(self) -> None:
        with mock.patch.object(config.socket, "socket") as sock_cls:
            sock = sock_cls.return_value
            sock.getsockname.return_value = ("127.0.0.1", 51234)
            self.assertIsNone(config.lan_address())
        with mock.patch.object(config.socket, "socket") as sock_cls:
            sock_cls.return_value.getsockname.return_value = ("192.168.1.7", 51234)
            self.assertEqual(config.lan_address(), "192.168.1.7")
        with mock.patch.object(config.socket, "socket") as sock_cls:
            sock_cls.return_value.connect.side_effect = OSError("no route")
            self.assertIsNone(config.lan_address())


class TestRenderedConfigListeners(unittest.TestCase):
    def test_no_wildcard_listeners_when_allow_lan_is_off(self) -> None:
        st = AppState()
        st.allow_lan = False
        st.ensure_secret()
        cfg = config.build_config(_sub(), st)
        self.assertFalse(cfg["allow-lan"])
        self.assertEqual(cfg["bind-address"], "127.0.0.1", "代理端口必须只听回环")
        self.assertEqual(cfg["dns"]["listen"], "127.0.0.1:1053")
        self.assertNotEqual(cfg["dns"]["listen"], "0.0.0.0:1053")
        self.assertTrue(cfg["external-controller"].startswith("127.0.0.1:"))
        # dns-hijack 是 TUN 的内部重定向, 与被监听的地址无关, 不得被顺手改掉
        self.assertEqual(cfg["tun"]["dns-hijack"], ["any:53", "tcp://any:53"])

    def test_tun_and_dns_hijack_path_unchanged(self) -> None:
        st = AppState()
        st.ensure_secret()
        cfg = config.build_config(_sub(), st, tun=True)
        self.assertTrue(cfg["tun"]["enable"])
        self.assertEqual(cfg["dns"]["enhanced-mode"], "fake-ip")
        self.assertTrue(cfg["dns"]["enable"])

    def test_allow_lan_on_keeps_lan_sharing_working(self) -> None:
        st = AppState()
        st.allow_lan = True
        st.ensure_secret()
        with mock.patch.object(config, "lan_address", return_value="192.168.1.5"):
            cfg = config.build_config(_sub(), st)
        self.assertTrue(cfg["allow-lan"])
        self.assertEqual(cfg["bind-address"], "*")
        self.assertEqual(cfg["dns"]["listen"], "192.168.1.5:1053")


if __name__ == "__main__":
    unittest.main()
