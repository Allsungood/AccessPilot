"""Cloudflare WARP 集成 —— 完全免费、不需要账号与信用卡的境外出口.

为什么值得做: 前面已经用实验证明, ChatGPT / Discord / X 被 SNI 阻断或地区封禁,
换 IP 无效, 必须有境外节点。而 WARP 的注册接口
(`api.cloudflareclient.com`) 不需要邮箱、不需要账号、不需要任何付款方式,
对"没有信用卡"的用户是唯一零门槛的路径。

原理: WARP 本质是 Cloudflare 提供的 WireGuard 接入。因为整条链路是加密的,
墙看不到 TLS 的 SNI, 所以它**同时**能绕过 DNS 污染、IP 封禁和 SNI 阻断。

关于 X25519: Python 标准库没有椭圆曲线运算, 而 WireGuard 必须要它。
这里用纯 Python 实现 RFC 7748 的 Montgomery 阶梯(约 40 行), 并用 RFC 官方
测试向量锁定正确性 —— 避免为了一个功能引入第三方密码学库。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import paths
from .util import Fail, http_request, info, json_dump, json_load, ok, warn

# --------------------------------------------------------------------------- #
# X25519 (RFC 7748)
# --------------------------------------------------------------------------- #

_P = 2**255 - 19
_A24 = 121665
_BASE_POINT = b"\x09" + b"\x00" * 31

#: RFC 7748 §5.2 官方测试向量
RFC7748_VECTORS = [
    (
        "a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4",
        "e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c",
        "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552",
    ),
]
#: 迭代 1000 次后的期望值(RFC 7748 §5.2 的 iterate 测试)
RFC7748_ITER_1000 = "684cf59ba83309552800ef566f2f4d3c1c3887c49360e3875f2eb94d99532c51"


def _clamp(scalar: bytes) -> int:
    b = bytearray(scalar)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return int.from_bytes(b, "little")


def x25519(scalar: bytes, u_coord: bytes) -> bytes:
    """标准 X25519 标量乘法(Montgomery 阶梯). 输入输出均为 32 字节小端."""
    if len(scalar) != 32 or len(u_coord) != 32:
        raise ValueError("X25519 输入必须是 32 字节")
    k = _clamp(scalar)
    x1 = int.from_bytes(u_coord, "little") % _P
    x2, z2, x3, z3 = 1, 0, x1, 1
    swap = 0
    for t in reversed(range(255)):
        kt = (k >> t) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = pow(da + cb, 2, _P)
        z3 = x1 * pow(da - cb, 2, _P) % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def generate_keypair() -> tuple[str, str]:
    """返回 (私钥 base64, 公钥 base64)."""
    private = os.urandom(32)
    public = x25519(private, _BASE_POINT)
    return (
        base64.b64encode(private).decode(),
        base64.b64encode(public).decode(),
    )


# --------------------------------------------------------------------------- #
# 注册与配置获取
# --------------------------------------------------------------------------- #

API_BASE = "https://api.cloudflareclient.com/v0a2158"
CLIENT_VERSION = "a-6.30-2158"
USER_AGENT = "okhttp/3.12.1"

#: 可选的接入点(不同网络对不同端口/地址的封锁情况不同)
DEFAULT_ENDPOINTS = [
    "162.159.192.1:2408",
    "162.159.195.1:2408",
    "188.114.96.1:2408",
    "188.114.97.1:2408",
    "188.114.98.1:2408",
    "188.114.99.1:2408",
    "162.159.192.1:500",
    "162.159.192.1:1701",
    "162.159.192.1:4500",
    "engage.cloudflareclient.com:2408",
]

WARP_FILE = "warp.json"


@dataclass
class WarpProfile:
    """一次 WARP 注册的完整结果."""

    device_id: str = ""
    account_id: str = ""
    license: str = ""
    private_key: str = ""
    peer_public_key: str = ""
    client_id: str = ""
    address_v4: str = ""
    address_v6: str = ""
    endpoint: str = "162.159.192.1:2408"
    created: float = 0.0
    account_type: str = "free"
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    def reserved(self) -> list[int]:
        """把 client_id 解码成 mihomo 需要的 3 字节 reserved 字段."""
        if not self.client_id:
            return [0, 0, 0]
        try:
            raw = base64.b64decode(self.client_id + "=" * (-len(self.client_id) % 4))
        except Exception:
            return [0, 0, 0]
        return [raw[0] if len(raw) > 0 else 0,
                raw[1] if len(raw) > 1 else 0,
                raw[2] if len(raw) > 2 else 0]

    def host_port(self) -> tuple[str, int]:
        host, _, port = self.endpoint.partition(":")
        return host, int(port or 2408)

    def to_mihomo_proxy(self, name: str = "☁️ WARP", *, mtu: int = 1280,
                        endpoint: str | None = None,
                        dialer_proxy: str | None = None) -> dict[str, Any]:
        """转成 mihomo 的 wireguard 出站.

        dialer_proxy: 让 WireGuard 握手先经过这个节点再出去。用于
        "WARP 的 UDP 直接被墙、但手上已有节点" 的场景 —— 借道节点把
        WARP 隧道建起来, 从而拿到一个 Cloudflare 的出口 IP。
        """
        host, port = self.host_port()
        if endpoint:
            host, _, p = endpoint.partition(":")
            port = int(p or 2408)
        node: dict[str, Any] = {
            "name": name,
            "type": "wireguard",
            "server": host,
            "port": port,
            "ip": self.address_v4.split("/")[0] + "/32" if self.address_v4 else "",
            "private-key": self.private_key,
            "public-key": self.peer_public_key,
            "allowed-ips": ["0.0.0.0/0", "::/0"],
            "reserved": self.reserved(),
            "mtu": mtu,
            "udp": True,
            "remote-dns-resolve": True,
            "dns": ["1.1.1.1", "1.0.0.1"],
        }
        if self.address_v6:
            node["ipv6"] = self.address_v6.split("/")[0] + "/128"
        if dialer_proxy:
            node["dialer-proxy"] = dialer_proxy
        return node

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WarpProfile":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _api_request(path: str, *, method: str = "GET", body: dict[str, Any] | None = None,
                 token: str = "", timeout: float = 20.0) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "User-Agent": USER_AGENT,
        "CF-Client-Version": CLIENT_VERSION,
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    status, _, raw = http_request(
        f"{API_BASE}{path}", method=method, headers=headers, data=data, timeout=timeout
    )
    text = raw.decode("utf-8", errors="replace")
    if status >= 400:
        raise Fail(f"WARP 接口返回 HTTP {status}: {text[:200]}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise Fail(f"WARP 接口返回内容无法解析: {e}") from e
    if not isinstance(parsed, dict):
        raise Fail("WARP 接口返回结构异常")
    return parsed


def register(device_name: str = "AccessPilot") -> WarpProfile:
    """注册一个 WARP 设备. 不需要邮箱/账号/付款方式."""
    private_key, public_key = generate_keypair()
    payload = {
        "key": public_key,
        "install_id": "",
        "fcm_token": "",
        "tos": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "model": "PC",
        "serial_number": "",
        "locale": "en_US",
    }
    data = _api_request("/reg", method="POST", body=payload)

    config = data.get("config") or {}
    peers = config.get("peers") or [{}]
    peer = peers[0] if peers else {}
    iface = config.get("interface") or {}
    addrs = iface.get("addresses") or {}
    endpoint_raw = (peer.get("endpoint") or {}).get("host") or ""
    endpoint = endpoint_raw or "162.159.192.1:2408"
    if endpoint.startswith("engage.cloudflareclient.com"):
        endpoint = "162.159.192.1:2408"

    profile = WarpProfile(
        device_id=str(data.get("id") or ""),
        account_id=str((data.get("account") or {}).get("id") or ""),
        license=str((data.get("account") or {}).get("license") or ""),
        private_key=private_key,
        peer_public_key=str(peer.get("public_key") or ""),
        client_id=str(config.get("client_id") or ""),
        address_v4=str(addrs.get("v4") or ""),
        address_v6=str(addrs.get("v6") or ""),
        endpoint=endpoint,
        created=time.time(),
        account_type=str((data.get("account") or {}).get("account_type") or "free"),
    )
    if not profile.peer_public_key or not profile.address_v4:
        raise Fail("WARP 注册返回的数据不完整, 无法生成 WireGuard 配置")
    save_profile(profile)
    return profile


def apply_license(profile: WarpProfile, license_key: str) -> WarpProfile:
    """把 WARP+ / Zero Trust 的 license 绑定到当前设备(可选, 免费账号可跳过)."""
    device_id = profile.device_id
    _api_request(
        f"/reg/{device_id}/account",
        method="PUT",
        body={"license": license_key.strip()},
        token=profile.license,
    )
    profile.account_type = "plus"
    profile.license = license_key.strip()
    save_profile(profile)
    return profile


def profile_path():
    return paths.home() / WARP_FILE


def save_profile(profile: WarpProfile) -> None:
    paths.ensure_dirs()
    json_dump(profile_path(), profile.to_dict())


def load_profile() -> WarpProfile | None:
    data = json_load(profile_path(), None)
    if not isinstance(data, dict):
        return None
    try:
        return WarpProfile.from_dict(data)
    except TypeError:
        return None


def delete_profile() -> bool:
    p = profile_path()
    if p.exists():
        p.unlink()
        return True
    return False


__all__ = [
    "x25519", "generate_keypair", "register", "apply_license",
    "WarpProfile", "load_profile", "save_profile", "delete_profile",
    "RFC7748_VECTORS", "RFC7748_ITER_1000", "DEFAULT_ENDPOINTS",
]
