"""本地图形控制台(零依赖, 基于标准库 http.server).

安全设计(每一条都对应一个真实的攻击面, 别为了省事删掉):
  * 只监听 127.0.0.1, 不对外暴露;
  * **所有**请求(包括首页)都校验 `Host` 头, 只认 127.0.0.1:<port> /
    localhost:<port>。这挡的是 DNS-rebinding: 攻击者把自己的域名解析到
    127.0.0.1, 浏览器就会以"同源"的身份来访问本服务, 连首页里的会话令牌
    都能被读走, 所以首页也不能例外;
  * GET/HEAD 一律只读; 凡是会改动本机状态的接口只接受 POST, 且必须带上
    每次启动随机生成的会话令牌(`X-AccessPilot-Token` 头或 POST body 里的
    token 字段)。旧实现只按 URL 路径分发, GET 一样会执行写操作 —— 于是
    `<img src="http://127.0.0.1:9099/api/stop">` 这一行 HTML 就能把用户正在
    用的代理停掉(用户看到的现象是"网又断了", 根本不会想到是某个网页干的);
  * 令牌只走请求头/body, **不走 query**: URL 会经由 Referer、浏览器历史、
    代理日志泄漏给第三方站点;
  * `Origin` 头只要出现, 就必须正好是本服务自己的源。
"""
from __future__ import annotations

import json
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import __version__, api, diag, paths, process, rules, sysproxy
from .state import load_state, save_state
from .subscription import fetch, list_profiles, load_profile, save_profile
from .util import (
    Fail,
    Progress,
    download,
    err,
    extract_archive,
    info,
    ok,
    open_in_browser,
    warn,
)

ASSET_DIR = Path(__file__).parent / "assets"

METACUBEXD_URLS = [
    "https://github.com/MetaCubeX/metacubexd/archive/refs/heads/gh-pages.zip",
    "https://ghfast.top/https://github.com/MetaCubeX/metacubexd/archive/refs/heads/gh-pages.zip",
    "https://gh-proxy.com/https://github.com/MetaCubeX/metacubexd/archive/refs/heads/gh-pages.zip",
    "https://github.com/MetaCubeX/metacubexd/releases/latest/download/compressed-dist.tgz",
    "https://ghfast.top/https://github.com/MetaCubeX/metacubexd/releases/latest/download/compressed-dist.tgz",
]


# --------------------------------------------------------------------------- #
# 面板资源
# --------------------------------------------------------------------------- #


def dashboard_html() -> bytes:
    path = ASSET_DIR / "dashboard.html"
    if not path.exists():
        return b"<h1>dashboard.html missing</h1>"
    return inject_token(path.read_bytes())


def inject_token(html: bytes) -> bytes:
    """把会话令牌注入控制台首页, 并让页面里的写请求自动带上令牌头。

    为什么在服务端注入、而不是把令牌写进 assets/dashboard.html:
    令牌必须**每次启动都不一样**, 静态文件里写死的值等于没有令牌(旧版那个
    `X-AccessPilot: 1` 就是这个毛病)。页面自己的 `api()` 只负责把写操作发成
    POST, 这里用一层 fetch 包装补上 `X-AccessPilot-Token`; 包装脚本插在页面
    自己的 <script> 之前, 所以不会出现"包装还没装上就发请求"的时序问题。
    """
    token_js = json.dumps(session_token())
    boot = (
        "<script>/* 由 webgui.py 注入: 会话令牌 + 写操作自动带令牌头 */\n"
        "(() => {\n"
        f"  const TOKEN = {token_js};\n"
        "  window.__ACCESSPILOT_TOKEN__ = TOKEN;\n"
        "  const raw = window.fetch.bind(window);\n"
        "  window.fetch = (input, init) => {\n"
        "    init = init || {};\n"
        '    const m = String(init.method || "GET").toUpperCase();\n'
        '    if (m !== "GET" && m !== "HEAD") {\n'
        "      init.headers = Object.assign({}, init.headers || {},\n"
        f"        {{'{TOKEN_HEADER}': TOKEN}});\n"
        "    }\n"
        "    return raw(input, init);\n"
        "  };\n"
        "})();\n"
        "</script>\n"
    ).encode("utf-8")
    marker = b"<script>"
    if marker in html:
        return html.replace(marker, boot + marker, 1)
    return html + boot  # 没有脚本标签也把令牌带上, 至少 window.__ACCESSPILOT_TOKEN__ 可用


def install_dashboard_ui(*, force: bool = False) -> Path:
    """下载 metacubexd 到 runtime/ui, 由内核通过 /ui 路径直接提供服务."""
    target = paths.runtime_dir() / "ui"
    index = target / "index.html"
    if index.exists() and not force:
        ok(f"面板已安装: http://127.0.0.1:{load_state().api_port}/ui")
        return target
    target.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for url in METACUBEXD_URLS:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / ("ui.zip" if url.endswith(".zip") else "ui.tgz")
                with Progress(f"下载面板 {url.split('/')[2]}"):
                    download(url, archive, show_progress=False, timeout=120)
                files = extract_archive(archive, Path(tmp) / "x")
                root = _find_web_root(files)
                if root is None:
                    raise Fail("未找到 index.html")
                for item in target.iterdir():
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                for item in root.iterdir():
                    dst = target / item.name
                    if item.is_dir():
                        shutil.copytree(item, dst)
                    else:
                        shutil.copy2(item, dst)
            ok(f"面板已安装: http://127.0.0.1:{load_state().api_port}/ui")
            return target
        except Exception as e:  # noqa: PERF203
            last_err = e
            continue
    raise Fail(f"面板下载失败, 可稍后重试: {last_err}")


def _find_web_root(files: list[Path]) -> Path | None:
    for f in files:
        if f.name.lower() == "index.html":
            return f.parent
    return None


# --------------------------------------------------------------------------- #
# API 处理
# --------------------------------------------------------------------------- #


def _status_payload() -> dict[str, Any]:
    st = load_state()
    data = process.status()
    traffic = {"upload": 0, "download": 0, "active": 0}
    if data["running"]:
        try:
            conns = api.connections(st)
            traffic = {
                "upload": conns.get("uploadTotal", 0),
                "download": conns.get("downloadTotal", 0),
                "active": len(conns.get("connections") or []),
            }
        except Exception:
            pass
    profile_info: dict[str, Any] = {}
    if st.active_profile:
        try:
            sub = load_profile(st.active_profile)
            profile_info = {
                "name": sub.name,
                "nodes": len(sub.proxies),
                "traffic": sub.traffic_text(),
                "expire": sub.expire_text(),
            }
        except Fail:
            profile_info = {"name": st.active_profile, "nodes": 0}
    return {
        "version": __version__,
        "status": data,
        "state": {
            "profile": st.active_profile,
            "mixed_port": st.mixed_port,
            "api_port": st.api_port,
            "tun": st.tun_enable,
            "mirror": st.mirror,
        },
        "traffic": traffic,
        "profile_info": profile_info,
        "tun_available": sysproxy.tun_available()[0],
    }


def _groups_payload() -> dict[str, Any]:
    st = load_state()
    if not process.is_running():
        return {"running": False, "groups": []}
    data = api.proxies(st)
    known = {n: i for i, n in enumerate(rules.ALL_GROUPS + [rules.G_ACCEL])}
    out = []
    for name, g in data.items():
        if not g.get("all"):
            continue
        out.append(
            {
                "name": name,
                "type": g.get("type"),
                "now": g.get("now"),
                "all": g.get("all"),
                "is_group": name in known,
            }
        )
    out.sort(key=lambda x: (not x["is_group"], known.get(x["name"], 99)))
    return {"running": True, "groups": out}


def _handle_api(path: str, query: dict[str, list[str]], body: dict[str, Any]) -> Any:
    st = load_state()

    if path == "/api/status":
        return _status_payload()

    if path == "/api/groups":
        return _groups_payload()

    if path == "/api/select":
        group = str(body.get("group") or "")
        node = str(body.get("node") or "")
        if not group or not node:
            raise Fail("缺少 group/node 参数")
        api.select(st, group, node)
        st.selected[group] = node
        save_state(st)
        return {"ok": True, "group": group, "node": node}

    if path == "/api/delay":
        group = (query.get("group") or [""])[0]
        node = (query.get("node") or [""])[0]
        timeout = int((query.get("timeout") or ["5000"])[0])
        if node:
            try:
                return {"node": node, "delay": api.delay(st, node, timeout_ms=timeout)}
            except Fail as e:
                return {"node": node, "delay": -1, "error": str(e)}
        if group:
            return {"group": group, "delays": api.group_delay(st, group, timeout_ms=timeout)}
        raise Fail("缺少 group 或 node 参数")

    if path == "/api/proxy":
        enable = bool(body.get("enable"))
        if enable:
            detail = sysproxy.enable(st)
        else:
            detail = sysproxy.disable(st)
        save_state(st)
        return {"ok": True, "detail": detail, "enabled": st.system_proxy_on}

    if path == "/api/tun":
        enable = bool(body.get("enable"))
        if enable:
            available, reason = sysproxy.tun_available()
            if not available:
                raise Fail(f"TUN 不可用: {reason}")
        st.tun_enable = enable
        save_state(st)
        if process.is_running():
            process.restart(st=st, tun=enable, system_proxy=False if enable else None)
        return {"ok": True, "tun": st.tun_enable}

    if path == "/api/start":
        st = load_state()
        process.start(st=st, tun=st.tun_enable)
        return {"ok": True}

    if path == "/api/stop":
        process.stop()
        return {"ok": True}

    if path == "/api/restart":
        st = load_state()
        process.restart(st=st, tun=st.tun_enable)
        return {"ok": True}

    if path == "/api/subs":
        active = st.active_profile
        return {
            "profiles": [
                {
                    "name": s.name,
                    "nodes": len(s.proxies),
                    "traffic": s.traffic_text(),
                    "expire": s.expire_text(),
                    "active": s.name == active,
                }
                for s in list_profiles()
            ]
        }

    if path == "/api/sub/add":
        url = str(body.get("url") or "").strip()
        if not url:
            raise Fail("请填写订阅链接")
        sub = fetch(url, body.get("name") or None)
        slug = save_profile(sub)
        if body.get("use", True):
            st.active_profile = slug
            save_state(st)
            if process.is_running():
                process.reload_config(sub, st)
        return {"ok": True, "name": slug, "nodes": len(sub.proxies)}

    if path == "/api/sub/use":
        name = str(body.get("name") or "")
        sub = load_profile(name)
        st.active_profile = name
        save_state(st)
        if process.is_running():
            process.reload_config(sub, st)
        return {"ok": True, "name": name}

    if path == "/api/sub/update":
        name = str(body.get("name") or st.active_profile)
        sub = load_profile(name)
        if not sub.url:
            raise Fail("该配置档没有订阅地址")
        fresh = fetch(sub.url, name)
        save_profile(fresh)
        if process.is_running() and st.active_profile == name:
            process.reload_config(fresh, st)
        return {"ok": True, "nodes": len(fresh.proxies), "traffic": fresh.traffic_text()}

    if path == "/api/sub/rm":
        from .subscription import delete_profile

        name = str(body.get("name") or "")
        delete_profile(name)
        if st.active_profile == name:
            st.active_profile = ""
            save_state(st)
        return {"ok": True}

    if path == "/api/test":
        keys = query.get("site") or None
        running = process.is_running()
        proxy = diag.proxy_url(st) if running else None
        results = diag.probe_sites(proxy, keys=keys, timeout=10.0)
        return {
            "proxy": proxy,
            "results": [r.as_dict() for r in results],
        }

    if path == "/api/ip":
        running = process.is_running()
        proxy = diag.proxy_url(st) if running else None
        return {"exit": diag.exit_info(proxy), "trace": diag.chatgpt_trace(proxy)}

    if path == "/api/log":
        lines = int((query.get("lines") or ["120"])[0])
        return {"log": process.tail_log(lines)}

    if path == "/api/doctor":
        v = process.status()
        return {
            "core_version": v.get("core_version"),
            "running": v.get("running"),
            "tun_available": sysproxy.tun_available(),
        }

    raise Fail(f"未知接口: {path}")


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #


#: 写操作必须携带的令牌头。旧版本要求的是固定的 `X-AccessPilot: 1` —— 那只是
#: "必须带一个自定义头"的标志, 本地任何脚本都满足, 一旦写进某个页面就永久有效。
#: 现在换成每次进程启动随机生成的令牌。
TOKEN_HEADER = "X-AccessPilot-Token"

#: 会话令牌: 只在本次进程生命周期内有效, 重启即换。
#: 网页读不到它(同源策略挡住了跨站读取), DNS-rebinding 又被 Host 校验拦住;
#: 本机上的其它程序能读到它, 但它们本来就能直接改系统代理, 不在威胁模型里。
_SESSION_TOKEN = secrets.token_urlsafe(32)


def session_token() -> str:
    """当前进程的网页控制台会话令牌."""
    return _SESSION_TOKEN


#: 只读接口: 允许 GET/HEAD, 不改变任何本机状态。
READ_ONLY_API = frozenset(
    {
        "/api/status",
        "/api/groups",
        "/api/delay",  # 只是让内核测一次延迟, 不改本机设置
        "/api/subs",
        "/api/test",
        "/api/ip",
        "/api/log",
        "/api/doctor",
    }
)

#: 写接口: 只接受 POST, 且必须带对的令牌。
#: 这份清单就是"改本机状态"的完整名单 —— 旧实现靠 if 顺序分发, 漏掉任何一个
#: 都会重新变成 GET 可触发的 CSRF 洞。
WRITE_API = frozenset(
    {
        "/api/select",
        "/api/proxy",
        "/api/tun",
        "/api/start",
        "/api/stop",
        "/api/restart",
        "/api/sub/add",
        "/api/sub/use",
        "/api/sub/update",
        "/api/sub/rm",
    }
)


def api_route(path: str) -> str:
    """接口分类: 'read'(只读) / 'write'(会改本机状态) / 'unknown'."""
    if path in READ_ONLY_API:
        return "read"
    if path in WRITE_API:
        return "write"
    return "unknown"


def _header(headers: Any, name: str) -> str:
    """大小写不敏感地取请求头(兼容 http.client 的 Message 和测试里的 dict)."""
    getter = getattr(headers, "get", None)
    if getter is not None:
        value = getter(name)
        if value is not None:
            return str(value)
    items = headers.items() if hasattr(headers, "items") else ()
    for key, value in items:
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def host_allowed(host_header: str, port: int) -> bool:
    """Host 头是否就是本服务自己。

    必须有这条: DNS-rebinding。攻击者把 evil.example 解析到 127.0.0.1,
    浏览器就会带着 `Host: evil.example:9099` 打到本地服务上 —— 对浏览器而言
    这是"同源", Origin 检查形同虚设, 首页里的令牌也就能被读走。只认
    127.0.0.1 / localhost 才能把它挡在门外。
    """
    raw = (host_header or "").strip().lower()
    name, sep, port_text = raw.partition(":")
    if name not in ("127.0.0.1", "localhost"):
        return False
    if not sep:
        return port == 80  # 只有默认端口浏览器才会省略端口号
    return port_text.isdigit() and int(port_text) == port


def origin_allowed(origin: str, port: int) -> bool:
    """Origin 头存在时, 必须正好是本服务自己的源。

    跨站请求一定带 Origin(fetch / 表单提交都带), 所以"带了就必须对得上"
    不会误伤正常页面, 却能挡住一切从别的站点发来的请求。
    """
    raw = (origin or "").strip()
    if not raw or raw.lower() == "null":  # null = 沙箱 iframe / file:// 等不透明源
        return False
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "http":
        return False
    if (parts.hostname or "").lower() not in ("127.0.0.1", "localhost"):
        return False
    try:
        origin_port = parts.port
    except ValueError:
        return False
    return (origin_port or 80) == port


def validate_request(
    method: str,
    path: str,
    headers: Any,
    body: dict[str, Any] | None = None,
    *,
    port: int,
    token: str | None = None,
) -> tuple[int, str] | None:
    """请求校验: 放行返回 None, 否则返回 (HTTP 状态码, 错误说明).

    刻意写成不依赖 Handler 的纯函数: 这类"少一个判断就出事"的逻辑必须能被
    单元测试直接喂假请求验证, 而不是靠"起个真服务碰碰看"。
    """
    if not host_allowed(_header(headers, "Host"), port):
        return 403, "非法 Host(只允许通过 127.0.0.1 / localhost 访问本控制台)"
    origin = _header(headers, "Origin")
    if origin and not origin_allowed(origin, port):
        return 403, "非法 Origin(已拦截跨站请求)"
    route = api_route(path)
    if route == "read":
        if method not in ("GET", "HEAD"):
            return 405, f"{path} 是只读接口, 只接受 GET"
        return None
    if route == "write":
        if method != "POST":
            # 旧实现走到这里之前就已经把活干了: GET /api/stop 会真的停掉内核,
            # GET /api/proxy 会真的关掉系统代理 —— 一张 <img> 就能做到。
            return 405, f"{path} 会改变本机设置, 只接受 POST"
        expect = session_token() if token is None else token
        supplied = _header(headers, TOKEN_HEADER) or str((body or {}).get("token") or "")
        if not supplied or not secrets.compare_digest(
            supplied.encode("utf-8"), expect.encode("utf-8")
        ):
            return 403, f"缺少或错误的 {TOKEN_HEADER}"
        return None
    return None  # 未登记的路径交给 _handle_api 报"未知接口"


class Handler(BaseHTTPRequestHandler):
    server_version = f"AccessPilot/{__version__}"
    protocol_version = "HTTP/1.1"

    #: 请求体上限。控制台的请求都很小, 而 Content-Length 是客户端说了算的,
    #: 不设上限的话一个 POST 就能让本服务把内存读穿。
    MAX_BODY = 8 * 1024 * 1024

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.path.startswith("/api/"):
            return
        super().log_message(fmt, *args)

    # ---------------------------------------------------------------- #
    def _send(
        self,
        code: int,
        body: bytes,
        ctype: str = "application/json",
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200, extra: dict[str, str] | None = None) -> None:
        self._send(
            code, json.dumps(obj, ensure_ascii=False).encode(), "application/json", extra
        )

    def _port(self) -> int:
        """本服务实际监听的端口 —— Host/Origin 校验必须比对真实端口."""
        try:
            return int(self.server.server_address[1])
        except (AttributeError, IndexError, TypeError, ValueError):  # pragma: no cover
            return 0

    def _read_body(self) -> dict[str, Any] | None:
        """读 POST body; 超过上限或长度非法时返回 None(由调用方回错误)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None
        if length <= 0:
            return {}
        if length > self.MAX_BODY:
            # 不读完就必须断开连接, 否则残留字节会被当成下一个请求解析。
            self.close_connection = True
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 注意顺序: Host/Origin 放在最前面, 且对**所有**请求生效。首页 HTML 里
        # 带着会话令牌, 一旦被 DNS-rebinding 读到, 后面的令牌校验就形同虚设。
        body = self._read_body() if method == "POST" else {}
        if body is None:
            self._json({"error": "请求体过大或长度非法"}, 413)
            return
        reject = validate_request(method, path, self.headers, body, port=self._port())
        if reject is not None:
            code, message = reject
            extra = None
            if code == 405:
                extra = {"Allow": "POST" if api_route(path) == "write" else "GET, HEAD"}
            self._json({"error": message}, code, extra)
            return

        if path in ("/", "/index.html"):
            self._send(200, dashboard_html(), "text/html")
            return

        if not path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return

        try:
            result = _handle_api(path, query, body)
        except Fail as e:
            self._json({"error": str(e)}, 400)
            return
        except Exception as e:  # pragma: no cover
            self._json({"error": f"内部错误: {e}"}, 500)
            return
        self._json(result)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


def serve(port: int = 9099, *, open_browser: bool = False) -> int:
    paths.ensure_dirs()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    ok(f"控制台已启动: {url}")
    info("按 Ctrl+C 退出")
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.6), open_in_browser(url)), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
        info("控制台已停止")
    finally:
        httpd.server_close()
    return 0
