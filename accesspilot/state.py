"""本地状态存储: 当前配置档、端口、TUN、镜像策略、手动选择的节点等."""
from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass, field
from typing import Any

from . import paths
from .util import json_dump, json_load

DEFAULT_MIXED_PORT = 7890
DEFAULT_API_PORT = 9090


@dataclass
class AppState:
    active_profile: str = ""
    mixed_port: int = DEFAULT_MIXED_PORT
    api_port: int = DEFAULT_API_PORT
    api_secret: str = ""
    allow_lan: bool = False
    tun_enable: bool = False
    tun_stack: str = "mixed"
    mirror: str = "jsdelivr"
    dns_mode: str = "fake-ip"
    ipv6: bool = False
    #: 免节点直连加速(IP 优选, 面向 GitHub 系资源)
    accel_enable: bool = False
    accel_port: int = 7895
    #: 让 WARP 的 WireGuard 握手借道某个节点(节点名); 空 = 直连
    warp_dialer: str = ""
    #: 各策略组的手动选择结果 {group: node}
    selected: dict[str, str] = field(default_factory=dict)
    #: 系统代理是否由本工具开启
    system_proxy_on: bool = False
    bypass: str = (
        "localhost;127.*;10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.20.*;"
        "172.21.*;172.22.*;172.23.*;172.24.*;172.25.*;172.26.*;172.27.*;"
        "172.28.*;172.29.*;172.30.*;172.31.*;192.168.*;<local>"
    )
    tun_device: str = "AccessPilot"
    last_start: float = 0.0

    # ------------------------------------------------------------------ #
    def ensure_secret(self) -> str:
        if not self.api_secret:
            self.api_secret = secrets.token_hex(16)
        return self.api_secret

    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_state() -> AppState:
    paths.ensure_dirs()
    data = json_load(paths.state_file(), None)
    st = AppState()
    if isinstance(data, dict):
        for key, value in data.items():
            if hasattr(st, key):
                setattr(st, key, value)
    st.ensure_secret()
    return st


def save_state(st: AppState) -> None:
    paths.ensure_dirs()
    json_dump(paths.state_file(), st.to_dict())
