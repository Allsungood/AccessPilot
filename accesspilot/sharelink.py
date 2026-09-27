"""分享链接 / 订阅链接解析器.

把各家机场与服务端工具导出的 URI 转换成 mihomo 内核的 proxies 结构。

参考的链接格式标准(均来自公开开源实现):
  * SIP002 / SIP003  : shadowsocks 社区标准
  * VMess AEAD       : v2ray 分享链接(JSON + base64)
  * VLESS / Trojan   : Xray-core 分享链接(URI + query)
  * Hysteria2 / TUIC : 各自官方文档的 URI 方案
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import urllib.parse
from typing import Any

from .util import Fail, warn

__all__ = ["parse_link", "parse_links", "b64decode_any"]


# --------------------------------------------------------------------------- #
# 基础解码工具
# --------------------------------------------------------------------------- #


def b64decode_any(data: str) -> bytes:
    """宽松的 base64 解码: 兼容 urlsafe、缺失 padding、空白符."""
    s = re.sub(r"\s+", "", data.strip())
    s = s.replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    s += "=" * pad
    try:
        return base64.b64decode(s)
    except (binascii.Error, ValueError) as e:
        raise Fail(f"base64 解码失败: {e}") from e


def _qs(parsed: urllib.parse.ParseResult) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}


def _name_of(parsed: urllib.parse.ParseResult, fallback: str) -> str:
    if parsed.fragment:
        return urllib.parse.unquote(parsed.fragment).strip() or fallback
    return fallback


def _truthy(v: str | None) -> bool:
    return str(v).lower() in ("1", "true", "yes")


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# 各协议
# --------------------------------------------------------------------------- #


def _parse_ss(uri: str) -> dict[str, Any]:
    raw = uri[len("ss://") :]
    query: dict[str, str] = {}
    name = ""
    if "#" in raw:
        raw, frag = raw.split("#", 1)
        name = urllib.parse.unquote(frag).strip()
    if "?" in raw:
        raw, q = raw.split("?", 1)
        query = {k: v0[0] for k, v0 in urllib.parse.parse_qs(q).items()}

    if "@" in raw:  # SIP002: ss://base64(method:pass)@host:port
        userinfo, hostport = raw.rsplit("@", 1)
        try:
            decoded = b64decode_any(userinfo).decode("utf-8", errors="replace")
        except Fail:
            decoded = urllib.parse.unquote(userinfo)
        if ":" not in decoded:
            raise Fail("ss 链接缺少 method:password")
        method, password = decoded.split(":", 1)
    else:  # 旧格式: ss://base64(method:pass@host:port)
        decoded = b64decode_any(raw).decode("utf-8", errors="replace")
        if "@" not in decoded:
            raise Fail("ss 链接格式无法识别")
        userinfo, hostport = decoded.rsplit("@", 1)
        if ":" not in userinfo:
            raise Fail("ss 链接缺少 method:password")
        method, password = userinfo.split(":", 1)

    if ":" not in hostport:
        raise Fail("ss 链接缺少端口")
    server, port = hostport.rsplit(":", 1)

    node: dict[str, Any] = {
        "name": name or f"ss-{server}",
        "type": "ss",
        "server": server.strip("[]"),
        "port": _int(port),
        "cipher": method,
        "password": password,
        "udp": True,
    }

    plugin = query.get("plugin")
    if plugin:
        parts = urllib.parse.unquote(plugin).split(";")
        pname = parts[0]
        popts = dict(
            p.split("=", 1) if "=" in p else (p, "true") for p in parts[1:] if p
        )
        if pname in ("obfs-local", "simple-obfs"):
            node["plugin"] = "obfs"
            node["plugin-opts"] = {
                "mode": popts.get("obfs", "http"),
                "host": popts.get("obfs-host", ""),
            }
        elif pname in ("v2ray-plugin", "xray-plugin"):
            node["plugin"] = "v2ray-plugin"
            node["plugin-opts"] = {
                "mode": "websocket",
                "host": popts.get("host", ""),
                "path": popts.get("path", "/"),
                "tls": "tls" in popts,
                "skip-cert-verify": False,
            }
        else:
            node["plugin"] = pname
            node["plugin-opts"] = popts
    return node


def _parse_ssr(uri: str) -> dict[str, Any]:
    body = uri[len("ssr://") :]
    decoded = b64decode_any(body).decode("utf-8", errors="replace")
    if "/?" in decoded:
        main, q = decoded.split("/?", 1)
    else:
        main, q = decoded, ""
    fields = main.split(":")
    if len(fields) < 6:
        raise Fail("ssr 链接字段不足")
    # 标准顺序: host:port:protocol:method:obfs:base64(password)
    password_b64 = fields[-1]
    obfs = fields[-2]
    protocol = fields[-3]
    method = fields[-4]
    port = _int(fields[-5])
    server = ":".join(fields[:-5])
    params = {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    def dec(key: str) -> str:
        val = params.get(key, "")
        if not val:
            return ""
        try:
            return b64decode_any(val).decode("utf-8", errors="replace")
        except Fail:
            return ""

    pwd = ""
    try:
        pwd = b64decode_any(password_b64).decode("utf-8", errors="replace")
    except Fail:
        pwd = password_b64
    return {
        "name": dec("remarks") or f"ssr-{server}",
        "type": "ssr",
        "server": server.strip("[]"),
        "port": port,
        "cipher": method,
        "password": pwd,
        "obfs": obfs,
        "protocol": protocol,
        "obfs-param": dec("obfsparam"),
        "protocol-param": dec("protoparam"),
        "udp": True,
    }


def _parse_vmess(uri: str) -> dict[str, Any]:
    body = uri[len("vmess://") :].strip()
    try:
        obj = json.loads(b64decode_any(body).decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise Fail(f"vmess 链接 JSON 无效: {e}") from e

    server = str(obj.get("add", "")).strip()
    port = _int(obj.get("port"))
    if not server or not port:
        raise Fail("vmess 链接缺少服务器或端口")

    network = str(obj.get("net") or "tcp").lower()
    tls = str(obj.get("tls") or "").lower()
    host = str(obj.get("host") or "")
    path = str(obj.get("path") or "")
    sni = str(obj.get("sni") or "")
    node: dict[str, Any] = {
        "name": str(obj.get("ps") or f"vmess-{server}"),
        "type": "vmess",
        "server": server.strip("[]"),
        "port": port,
        "uuid": str(obj.get("id") or ""),
        "alterId": _int(obj.get("aid"), 0),
        "cipher": str(obj.get("scy") or "auto") or "auto",
        "udp": True,
    }
    if tls in ("tls", "reality"):
        node["tls"] = True
        node["servername"] = sni or host or server
        if _truthy(str(obj.get("skip-cert-verify") or obj.get("allowInsecure") or "")):
            node["skip-cert-verify"] = True
    if network == "ws":
        node["network"] = "ws"
        ws: dict[str, Any] = {"path": path or "/"}
        if host:
            ws["headers"] = {"Host": host}
        node["ws-opts"] = ws
    elif network == "grpc":
        node["network"] = "grpc"
        node["grpc-opts"] = {"grpc-service-name": path or host}
    elif network in ("h2", "http"):
        node["network"] = "h2"
        node["h2-opts"] = {"host": [host] if host else [], "path": path or "/"}
    else:
        node["network"] = "tcp"
        if str(obj.get("type") or "").lower() == "http":
            node["network"] = "http"
            node["http-opts"] = {"path": [path or "/"], "headers": {"Host": [host]}}
    fp = str(obj.get("fp") or "")
    if fp:
        node["client-fingerprint"] = fp
    return node


def _parse_vless(uri: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(uri)
    q = _qs(parsed)
    if not parsed.hostname:
        raise Fail("vless 链接缺少服务器")
    node: dict[str, Any] = {
        "name": _name_of(parsed, f"vless-{parsed.hostname}"),
        "type": "vless",
        "server": parsed.hostname,
        "port": parsed.port or 443,
        "uuid": urllib.parse.unquote(parsed.username or ""),
        "udp": True,
    }
    security = (q.get("security") or "").lower()
    if security in ("tls", "reality", "xtls"):
        node["tls"] = True
        node["servername"] = q.get("sni") or q.get("peer") or q.get("host") or ""
    if security == "reality":
        node["reality-opts"] = {
            "public-key": q.get("pbk", ""),
            "short-id": q.get("sid", ""),
        }
    if q.get("flow"):
        node["flow"] = q["flow"]
    if q.get("fp"):
        node["client-fingerprint"] = q["fp"]
    if q.get("alpn"):
        node["alpn"] = [a for a in q["alpn"].split(",") if a]
    if _truthy(q.get("allowInsecure") or q.get("insecure")):
        node["skip-cert-verify"] = True
    network = (q.get("type") or "tcp").lower()
    if network in ("ws", "httpupgrade", "splithttp"):
        node["network"] = "ws" if network == "ws" else "ws"
        ws: dict[str, Any] = {"path": urllib.parse.unquote(q.get("path") or "/")}
        if q.get("host"):
            ws["headers"] = {"Host": q["host"]}
        node["ws-opts"] = ws
    elif network == "grpc":
        node["network"] = "grpc"
        node["grpc-opts"] = {
            "grpc-service-name": urllib.parse.unquote(q.get("serviceName") or "")
        }
    elif network in ("h2", "http"):
        node["network"] = "h2"
        node["h2-opts"] = {
            "host": [q["host"]] if q.get("host") else [],
            "path": urllib.parse.unquote(q.get("path") or "/"),
        }
    else:
        node["network"] = "tcp"
    return node


def _parse_trojan(uri: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(uri)
    q = _qs(parsed)
    if not parsed.hostname:
        raise Fail("trojan 链接缺少服务器")
    node: dict[str, Any] = {
        "name": _name_of(parsed, f"trojan-{parsed.hostname}"),
        "type": "trojan",
        "server": parsed.hostname,
        "port": parsed.port or 443,
        "password": urllib.parse.unquote(parsed.username or ""),
        "udp": True,
        "sni": q.get("sni") or q.get("peer") or "",
    }
    if _truthy(q.get("allowInsecure") or q.get("insecure")):
        node["skip-cert-verify"] = True
    if q.get("alpn"):
        node["alpn"] = [a for a in q["alpn"].split(",") if a]
    if q.get("fp"):
        node["client-fingerprint"] = q["fp"]
    network = (q.get("type") or "tcp").lower()
    if network == "ws":
        node["network"] = "ws"
        ws: dict[str, Any] = {"path": urllib.parse.unquote(q.get("path") or "/")}
        if q.get("host"):
            ws["headers"] = {"Host": q["host"]}
        node["ws-opts"] = ws
    elif network == "grpc":
        node["network"] = "grpc"
        node["grpc-opts"] = {
            "grpc-service-name": urllib.parse.unquote(q.get("serviceName") or "")
        }
    return node


def _parse_hysteria2(uri: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(uri)
    q = _qs(parsed)
    if not parsed.hostname:
        raise Fail("hysteria2 链接缺少服务器")
    auth = parsed.username or ""
    password = urllib.parse.unquote(auth)
    if parsed.password:
        password = f"{urllib.parse.unquote(auth)}:{urllib.parse.unquote(parsed.password)}"
    if not password and q.get("password"):
        password = q["password"]
    node: dict[str, Any] = {
        "name": _name_of(parsed, f"hy2-{parsed.hostname}"),
        "type": "hysteria2",
        "server": parsed.hostname,
        "port": parsed.port or 443,
        "password": password,
        "sni": q.get("sni") or q.get("peer") or "",
        "skip-cert-verify": _truthy(q.get("insecure")),
    }
    if q.get("obfs"):
        node["obfs"] = q["obfs"]
        node["obfs-password"] = q.get("obfs-password", "")
    if q.get("alpn"):
        node["alpn"] = [a for a in q["alpn"].split(",") if a]
    if q.get("mport"):
        node["ports"] = q["mport"]
    if q.get("up"):
        node["up"] = q["up"]
    if q.get("down"):
        node["down"] = q["down"]
    return node


def _parse_tuic(uri: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(uri)
    q = _qs(parsed)
    if not parsed.hostname:
        raise Fail("tuic 链接缺少服务器")
    node: dict[str, Any] = {
        "name": _name_of(parsed, f"tuic-{parsed.hostname}"),
        "type": "tuic",
        "server": parsed.hostname,
        "port": parsed.port or 443,
        "uuid": urllib.parse.unquote(parsed.username or ""),
        "password": urllib.parse.unquote(parsed.password or ""),
        "sni": q.get("sni") or "",
        "skip-cert-verify": _truthy(q.get("allow_insecure") or q.get("insecure")),
        "udp-relay-mode": q.get("udp_relay_mode", "native"),
        "congestion-controller": q.get("congestion_control", "bbr"),
    }
    if q.get("alpn"):
        node["alpn"] = [a for a in q["alpn"].split(",") if a]
    return node


def _parse_socks_http(uri: str, scheme: str) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(uri)
    if not parsed.hostname:
        raise Fail(f"{scheme} 链接缺少服务器")
    node: dict[str, Any] = {
        "name": _name_of(parsed, f"{scheme}-{parsed.hostname}"),
        "type": "socks5" if scheme.startswith("socks") else "http",
        "server": parsed.hostname,
        "port": parsed.port or (1080 if scheme.startswith("socks") else 8080),
        "udp": True,
    }
    if parsed.username:
        node["username"] = urllib.parse.unquote(parsed.username)
        node["password"] = urllib.parse.unquote(parsed.password or "")
    return node


_HANDLERS = {
    "ss": _parse_ss,
    "ssr": _parse_ssr,
    "vmess": _parse_vmess,
    "vless": _parse_vless,
    "trojan": _parse_trojan,
    "trojan-go": _parse_trojan,
    "hysteria2": _parse_hysteria2,
    "hy2": _parse_hysteria2,
    "hysteria": _parse_hysteria2,
    "tuic": _parse_tuic,
}


def parse_link(uri: str) -> dict[str, Any] | None:
    """解析单条分享链接; 无法识别时返回 None."""
    uri = uri.strip()
    if not uri or uri.startswith("#"):
        return None
    if "://" not in uri:
        return None
    scheme = uri.split("://", 1)[0].lower()
    if scheme in _HANDLERS:
        node = _HANDLERS[scheme](uri)
    elif scheme in ("socks", "socks5", "socks5h", "http", "https"):
        node = _parse_socks_http(uri, scheme)
    else:
        return None
    if node.get("type") in ("vmess", "vless", "tuic") and not node.get("uuid"):
        raise Fail(f"{scheme} 链接缺少 uuid")
    if node.get("type") in ("ss", "ssr", "trojan", "hysteria2") and not node.get(
        "password"
    ):
        raise Fail(f"{scheme} 链接缺少密码")
    if node.get("type") == "vless":
        node.pop("password", None)
    return node


def parse_links(text: str, *, strict: bool = False) -> list[dict[str, Any]]:
    """解析多行分享链接文本."""
    out: list[dict[str, Any]] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or "://" not in line:
            continue
        try:
            node = parse_link(line)
        except Fail as e:
            if strict:
                raise
            warn(f"跳过无法解析的链接: {e}")
            continue
        except ValueError as e:
            # 例如 vless://u@[2600:... 少写一个右括号, urlsplit 会抛
            # "Invalid IPv6 URL"。免费节点源里这种烂链接很常见, 跳过即可。
            if strict:
                raise
            warn(f"跳过格式异常的链接: {e}")
            continue
        if node:
            out.append(node)
    return out
