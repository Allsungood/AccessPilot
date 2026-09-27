"""本地图形控制台(零依赖, 基于标准库 http.server).

安全设计:
  * 只监听 127.0.0.1, 不对外暴露;
  * 所有写操作必须携带自定义头 `X-AccessPilot`, 且校验 Host, 防止
    恶意网页通过 CSRF 操纵本机代理设置。
"""
from __future__ import annotations

import json
import re
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
    return path.read_bytes()


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


class Handler(BaseHTTPRequestHandler):
    server_version = f"AccessPilot/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if self.path.startswith("/api/"):
            return
        super().log_message(fmt, *args)

    # ---------------------------------------------------------------- #
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    def _guard(self) -> bool:
        """写操作防护: 校验自定义头 + Host, 阻止 CSRF."""
        if self.headers.get("X-AccessPilot") != "1":
            self._json({"error": "缺少 X-AccessPilot 头"}, 403)
            return False
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._json({"error": "非法 Host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            self._json({"error": "非法 Origin"}, 403)
            return False
        return True

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
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

        if path in ("/", "/index.html"):
            self._send(200, dashboard_html(), "text/html")
            return

        if not path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return

        if method in ("POST", "PUT", "DELETE"):
            if not self._guard():
                return

        body = self._read_body() if method in ("POST", "PUT", "DELETE") else {}
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
