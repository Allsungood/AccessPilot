"""可用性诊断: 目标平台连通性、出口 IP 归属、ChatGPT 区域检测."""
from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable

from .state import AppState
from .util import http_request

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    #: 这组"现代浏览器"头是 Cloudflare 的硬性门槛, 不能省。
    #: 实测 chatgpt.com: 只带 UA/Accept/Accept-Language 一律 403,
    #: 补上 Sec-Fetch-* + sec-ch-ua 立刻 200(且返回未压缩 HTML)。
    #: 少了它们会把"其实能用"的节点误判成"IP 被拒绝"。
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

#: OpenAI 官方不支持的国家/地区(参考 OpenAI Supported Countries 文档)
OPENAI_BLOCKED_REGIONS = {
    "CN": "中国大陆",
    "HK": "中国香港",
    "MO": "中国澳门",
    "RU": "俄罗斯",
    "IR": "伊朗",
    "KP": "朝鲜",
    "CU": "古巴",
    "SY": "叙利亚",
    "BY": "白俄罗斯",
    "VE": "委内瑞拉",
}


@dataclass
class TargetResult:
    key: str
    name: str
    url: str
    ok: bool
    latency_ms: int
    status: int
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "url": self.url,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "status": self.status,
            "detail": self.detail,
        }


def proxy_url(st: AppState) -> str:
    return f"http://127.0.0.1:{st.mixed_port}"


# --------------------------------------------------------------------------- #
# 单站点探测
# --------------------------------------------------------------------------- #


def _probe(
    key: str,
    name: str,
    url: str,
    proxy: str | None,
    *,
    timeout: float = 12.0,
    accept: tuple[int, ...] = (200, 204, 301, 302, 304, 401, 403),
    validator: Callable[[int, bytes], tuple[bool, str]] | None = None,
    extra_urls: tuple[str, ...] = (),
) -> TargetResult:
    start = time.time()
    try:
        status, _, body = http_request(
            url, headers=BROWSER_HEADERS, timeout=timeout, proxy=proxy
        )
    except Exception as e:
        return TargetResult(
            key, name, url, False, int((time.time() - start) * 1000), 0, f"连接失败: {e}"
        )
    ms = int((time.time() - start) * 1000)
    ok = status in accept
    detail = f"HTTP {status}"
    if validator is not None:
        ok, detail = validator(status, body)

    # 主页 200 不代表页面真能加载: 大型前端站点的关键资源拉不动时,
    # 浏览器里就是"一直转圈"。对每个附加资源做一次真实请求。
    if ok and extra_urls:
        for extra in extra_urls:
            try:
                s2, _, _ = http_request(
                    extra, headers=BROWSER_HEADERS, timeout=timeout, proxy=proxy
                )
            except Exception as e:
                ok, detail = False, f"资源 {extra.split('/')[2]} 加载失败: {e}"
                break
            if s2 not in accept:
                ok, detail = False, f"资源 {extra.split('/')[2]} HTTP {s2}"
                break
    return TargetResult(key, name, url, ok, ms, status, detail)


#: 真正的 Cloudflare 拦截/挑战页特征。
#: 千万不要用 "blocked" / "cloudflare" 这种泛词做判断 ——
#: chatgpt.com 的真实页面里就含有 `"offlineBlocked":["boolean",false]`
#: 和 `Cloudflare-Workers-Version-Overrides`, 泛词匹配必然误报。
_CF_CHALLENGE_MARKERS = (
    "just a moment",
    "challenge-platform",
    "cf-chl-",
    "attention required! | cloudflare",
    "you have been blocked",
    "error 1020",
    "enable javascript and cookies to continue",
)


def _chatgpt_validator(status: int, body: bytes) -> tuple[bool, str]:
    text = body.decode("utf-8", errors="replace")
    low = text.lower()
    if "unsupported_country" in low or "not available in your country" in low:
        return False, "IP 所在地区不受 OpenAI 支持"
    if any(m in low for m in _CF_CHALLENGE_MARKERS):
        return False, "被 Cloudflare 挑战页拦截(IP 信誉差, 建议换节点)"
    if status == 403:
        return False, "HTTP 403, 该出口 IP 被拒绝"
    if status in (200, 302) or status < 400:
        if "data-build" in low or 'id="root"' in low:
            return True, f"HTTP {status}, 真实页面 {len(body) // 1024}KB"
        return True, f"HTTP {status}"
    return False, f"HTTP {status}"


def _discord_validator(status: int, body: bytes) -> tuple[bool, str]:
    if status == 200:
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(data, dict) and data.get("url"):
                return True, f"网关可用 ({data.get('url', '')[:40]})"
        except json.JSONDecodeError:
            pass
        return True, "HTTP 200"
    if status == 429:
        return True, "HTTP 429(限流, 但链路可达)"
    return False, f"HTTP {status}"


TARGETS: list[dict[str, Any]] = [
    {
        "key": "chatgpt",
        "name": "ChatGPT 网页版",
        "url": "https://chatgpt.com/",
        "validator": _chatgpt_validator,
    },
    {
        "key": "openai_api",
        "name": "OpenAI API",
        "url": "https://api.openai.com/v1/models",
        "accept": (200, 401),  # 401 = 可达但未带密钥
    },
    {
        "key": "discord",
        "name": "Discord",
        "url": "https://discord.com/api/v9/gateway",
        "validator": _discord_validator,
    },
    {
        "key": "x",
        "name": "X (Twitter)",
        "url": "https://x.com/",
        # 主页 200 不代表能用: 资源域拉不动时浏览器里就是永远转圈
        "extra_urls": ("https://abs.twimg.com/favicons/twitter.3.ico",),
    },
    {
        "key": "google",
        "name": "Google",
        "url": "https://www.google.com/generate_204",
        "accept": (204, 200),
    },
    {
        "key": "youtube",
        "name": "YouTube",
        "url": "https://www.youtube.com/",
    },
    {
        "key": "github",
        "name": "GitHub",
        "url": "https://github.com/",
    },
    {
        "key": "telegram",
        "name": "Telegram",
        "url": "https://telegram.org/",
    },
    {
        "key": "wikipedia",
        "name": "Wikipedia",
        "url": "https://en.wikipedia.org/wiki/Main_Page",
    },
    {
        "key": "netflix",
        "name": "Netflix",
        "url": "https://www.netflix.com/",
    },
]

SITE_BY_KEY = {t["key"]: t for t in TARGETS}


def probe_sites(
    proxy: str | None,
    *,
    keys: list[str] | None = None,
    timeout: float = 12.0,
) -> list[TargetResult]:
    chosen = [SITE_BY_KEY[k] for k in keys if k in SITE_BY_KEY] if keys else TARGETS
    results = []
    for t in chosen:
        results.append(
            _probe(
                t["key"],
                t["name"],
                t["url"],
                proxy,
                timeout=timeout,
                accept=tuple(t.get("accept", (200, 204, 301, 302, 304, 401, 403))),
                validator=t.get("validator"),
                extra_urls=tuple(t.get("extra_urls", ())),
            )
        )
    return results


# --------------------------------------------------------------------------- #
# 出口信息
# --------------------------------------------------------------------------- #


def exit_info(proxy: str | None, *, timeout: float = 10.0) -> dict[str, Any]:
    """查询出口 IP 归属地(用于判断节点是否适合 ChatGPT)."""
    fields = "status,message,country,countryCode,regionName,city,isp,org,as,proxy,hosting,query"
    url = f"http://ip-api.com/json/?fields={fields}&lang=zh-CN"
    try:
        status, _, body = http_request(url, timeout=timeout, proxy=proxy)
        if status != 200:
            return {"error": f"HTTP {status}"}
        data = json.loads(body.decode("utf-8", errors="replace"))
        if data.get("status") != "success":
            return {"error": data.get("message", "查询失败")}
        code = str(data.get("countryCode") or "").upper()
        data["openai_blocked"] = code in OPENAI_BLOCKED_REGIONS
        data["region_note"] = OPENAI_BLOCKED_REGIONS.get(code, "")
        return data
    except Exception as e:
        return {"error": str(e)}


def chatgpt_trace(proxy: str | None, *, timeout: float = 10.0) -> dict[str, Any]:
    """读取 Cloudflare trace, 拿到访问 ChatGPT 时真实的出口 IP 与地区."""
    try:
        status, _, body = http_request(
            "https://chatgpt.com/cdn-cgi/trace",
            headers=BROWSER_HEADERS,
            timeout=timeout,
            proxy=proxy,
        )
        text = body.decode("utf-8", errors="replace")
        info: dict[str, Any] = {}
        for line in text.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
        if not info:
            return {"error": f"HTTP {status}, 无 trace 数据"}
        loc = info.get("loc", "")
        info["openai_blocked"] = loc in OPENAI_BLOCKED_REGIONS
        info["region_note"] = OPENAI_BLOCKED_REGIONS.get(loc, "")
        return info
    except Exception as e:
        return {"error": str(e)}


# --------------------------------------------------------------------------- #
# DNS 防污染自检
# --------------------------------------------------------------------------- #

#: 国内常见的 DNS 污染返回段
_POISON_HINTS = ("0.0.0.0", "127.0.0.1", "1.2.3.4", "243.185.187.")


def system_dns_probe(domain: str = "www.google.com") -> dict[str, Any]:
    """用系统解析器解析域名, 判断是否被污染."""
    try:
        infos = socket.getaddrinfo(domain, 443, proto=socket.IPPROTO_TCP)
    except Exception as e:
        return {"domain": domain, "ok": False, "ips": [], "detail": f"解析失败: {e}"}
    ips = sorted({i[4][0] for i in infos})
    poisoned = any(ip.startswith(_POISON_HINTS) for ip in ips)
    return {
        "domain": domain,
        "ok": not poisoned,
        "ips": ips,
        "detail": "疑似存在 DNS 污染" if poisoned else "解析正常",
    }


def dns_via_local(domain: str, port: int = 1053) -> list[str]:
    """直接向内核 DNS(1053 端口)发起一次查询, 用于确认内核 DNS 在工作."""
    query = _build_dns_query(domain)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(4)
            s.sendto(query, ("127.0.0.1", port))
            data, _ = s.recvfrom(4096)
    except Exception:
        return []
    return _parse_dns_a(data)


def _build_dns_query(domain: str) -> bytes:
    import random
    import struct

    tid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    q = b"".join(bytes([len(p)]) + p.encode() for p in domain.split(".")) + b"\x00"
    return header + q + struct.pack(">HH", 1, 1)


def _parse_dns_a(data: bytes) -> list[str]:
    import struct

    if len(data) < 12:
        return []
    _, flags, qd, an, _, _ = struct.unpack(">HHHHHH", data[:12])
    idx = 12
    for _ in range(qd):
        while idx < len(data) and data[idx] != 0:
            if data[idx] & 0xC0:
                idx += 1
                break
            idx += data[idx] + 1
        idx += 5
    out: list[str] = []
    for _ in range(an):
        if idx >= len(data):
            break
        if data[idx] & 0xC0:
            idx += 2
        else:
            while idx < len(data) and data[idx] != 0:
                idx += data[idx] + 1
            idx += 1
        if idx + 10 > len(data):
            break
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[idx : idx + 10])
        idx += 10
        if rtype == 1 and rdlen == 4:
            out.append(".".join(str(b) for b in data[idx : idx + 4]))
        idx += rdlen
    return out
