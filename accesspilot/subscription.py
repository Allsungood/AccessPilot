"""订阅抓取与解析: 支持 base64 分享链接订阅 与 Clash YAML 订阅两种主流格式."""
from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from . import miniyaml, paths, sharelink
from .util import SUB_UA, Fail, http_request, json_dump, json_load, ok

__all__ = ["Subscription", "parse_content", "fetch", "load_profile", "save_profile",
           "list_profiles", "delete_profile", "dedupe", "uniquify_names"]


@dataclass
class Subscription:
    """一份订阅解析结果."""

    name: str
    url: str = ""
    proxies: list[dict[str, Any]] = field(default_factory=list)
    upload: int = 0
    download: int = 0
    total: int = 0
    expire: int = 0
    updated: float = 0.0
    source_format: str = ""
    home: str = ""

    # ------------------------------------------------------------------ #
    @property
    def used(self) -> int:
        return self.upload + self.download

    def traffic_text(self) -> str:
        from .util import human_size

        if not self.total:
            return "未知流量"
        left = max(self.total - self.used, 0)
        pct = self.used * 100 / self.total
        return (
            f"已用 {human_size(self.used)} / {human_size(self.total)}"
            f" ({pct:.1f}%), 剩余 {human_size(left)}"
        )

    def expire_text(self) -> str:
        if not self.expire:
            return "长期有效"
        left = self.expire - time.time()
        if left <= 0:
            return "已过期"
        return time.strftime("%Y-%m-%d", time.localtime(self.expire)) + (
            f" (剩 {int(left // 86400)} 天)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "upload": self.upload,
            "download": self.download,
            "total": self.total,
            "expire": self.expire,
            "updated": self.updated,
            "source_format": self.source_format,
            "home": self.home,
            "proxies": self.proxies,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Subscription":
        return cls(
            name=d.get("name", "unnamed"),
            url=d.get("url", ""),
            proxies=d.get("proxies", []) or [],
            upload=d.get("upload", 0),
            download=d.get("download", 0),
            total=d.get("total", 0),
            expire=d.get("expire", 0),
            updated=d.get("updated", 0.0),
            source_format=d.get("source_format", ""),
            home=d.get("home", ""),
        )


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def _looks_like_yaml(text: str) -> bool:
    head = text[:4096]
    return bool(re.search(r"^\s*proxies\s*:", head, re.M)) or bool(
        re.search(r"^\s*proxy-groups\s*:", head, re.M)
    )


def _maybe_b64_to_text(text: str) -> tuple[str, bool]:
    stripped = re.sub(r"\s+", "", text)
    if "://" in stripped and "proxies:" not in text:
        return text, False
    if len(stripped) < 16 or not re.fullmatch(r"[A-Za-z0-9+/\-_=]+", stripped):
        return text, False
    try:
        decoded = sharelink.b64decode_any(stripped).decode("utf-8", errors="replace")
    except Fail:
        return text, False
    if "://" in decoded or "proxies:" in decoded:
        return decoded, True
    return text, False


def parse_content(text: str, name: str = "subscription") -> tuple[list[dict[str, Any]], str]:
    """解析订阅正文, 返回 (proxies, 格式标识)."""
    text = text.lstrip("\ufeff").strip()
    if not text:
        raise Fail("订阅内容为空")

    if _looks_like_yaml(text):
        obj = miniyaml.load(text)
        if not isinstance(obj, dict):
            raise Fail("Clash 订阅 YAML 结构异常")
        proxies = obj.get("proxies") or []
        if not isinstance(proxies, list):
            raise Fail("Clash 订阅 proxies 字段不是列表")
        cleaned: list[dict[str, Any]] = []
        for i, p in enumerate(proxies):
            if not isinstance(p, dict) or "type" not in p or "server" not in p:
                continue
            p = dict(p)
            p.setdefault("name", f"{name}-{i + 1}")
            cleaned.append(p)
        if not cleaned:
            raise Fail("Clash 订阅中未找到可用节点")
        return cleaned, "clash"

    decoded, _ = _maybe_b64_to_text(text)
    links = sharelink.parse_links(decoded)
    if not links:
        raise Fail("订阅内容无法识别(既不是 Clash YAML, 也解析不出分享链接)")
    return links, "links"


# --------------------------------------------------------------------------- #
# 节点整理
# --------------------------------------------------------------------------- #


def _ident(p: dict[str, Any]) -> tuple[Any, ...]:
    return (
        p.get("type"),
        str(p.get("server", "")).lower(),
        p.get("port"),
        p.get("uuid") or p.get("password") or "",
        p.get("network", ""),
        (p.get("ws-opts") or {}).get("path", "") if isinstance(p.get("ws-opts"), dict) else "",
    )


def dedupe(proxies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for p in proxies:
        key = _ident(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def uniquify_names(proxies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used: dict[str, int] = {}
    for p in proxies:
        base = str(p.get("name") or "node").strip() or "node"
        if base in used:
            used[base] += 1
            p["name"] = f"{base} #{used[base]}"
        else:
            used[base] = 1
            p["name"] = base
    return proxies


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #


def _parse_userinfo(header: str) -> dict[str, int]:
    info: dict[str, int] = {}
    for part in header.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        try:
            info[k] = int(float(v.strip()))
        except ValueError:
            continue
    return info


def fetch(url: str, name: str | None = None, *, timeout: float = 30.0) -> Subscription:
    """下载并解析一份订阅."""
    if not url.startswith(("http://", "https://")):
        raise Fail(f"订阅地址必须以 http(s) 开头: {url}")
    status, headers, body = http_request(
        url,
        headers={"User-Agent": SUB_UA, "Accept": "*/*"},
        timeout=timeout,
    )
    if status >= 400:
        raise Fail(f"订阅下载失败 HTTP {status}: {url}")
    text = body.decode("utf-8", errors="replace")

    profile_name = name
    if not profile_name:
        cd = headers.get("content-disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd or "")
        if m:
            profile_name = urllib.parse.unquote(m.group(1)).strip()
    if not profile_name:
        profile_name = urllib.parse.urlsplit(url).hostname or "subscription"

    proxies, fmt = parse_content(text, profile_name)
    proxies = uniquify_names(dedupe(proxies))

    sub = Subscription(
        name=profile_name,
        url=url,
        proxies=proxies,
        updated=time.time(),
        source_format=fmt,
    )
    ui = _parse_userinfo(headers.get("subscription-userinfo", ""))
    sub.upload = ui.get("upload", 0)
    sub.download = ui.get("download", 0)
    sub.total = ui.get("total", 0)
    sub.expire = ui.get("expire", 0)
    sub.home = headers.get("profile-web-page-url", "") or ""
    return sub


# --------------------------------------------------------------------------- #
# 本地存储
# --------------------------------------------------------------------------- #


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", name).strip("._")
    return (s or "profile")[:64]


def profile_path(name: str):
    return paths.profiles_dir() / f"{_slug(name)}.json"


def save_profile(sub: Subscription) -> str:
    paths.ensure_dirs()
    path = profile_path(sub.name)
    json_dump(path, sub.to_dict())
    return path.stem


def load_profile(name: str) -> Subscription:
    path = profile_path(name)
    if not path.exists():
        raise Fail(f"配置档不存在: {name}")
    data = json_load(path, {})
    if not data:
        raise Fail(f"配置档损坏: {name}")
    return Subscription.from_dict(data)


def list_profiles() -> list[Subscription]:
    paths.ensure_dirs()
    out: list[Subscription] = []
    for f in sorted(paths.profiles_dir().glob("*.json")):
        data = json_load(f, None)
        if data:
            out.append(Subscription.from_dict(data))
    return out


def delete_profile(name: str) -> None:
    path = profile_path(name)
    if path.exists():
        path.unlink()
        ok(f"已删除配置档 {name}")
    else:
        raise Fail(f"配置档不存在: {name}")
