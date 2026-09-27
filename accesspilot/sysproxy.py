"""系统代理与 TUN 模式集成(以 Windows 为主, 兼顾 macOS/Linux)."""
from __future__ import annotations

import ctypes
import os
import sys

from . import paths
from .state import AppState
from .util import json_dump, json_load, run_hidden

# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #

_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_INTERNET_OPTION_REFRESH = 37
_INTERNET_OPTION_SETTINGS_CHANGED = 39


def _refresh_wininet() -> None:
    """通知系统代理设置已变更, 否则部分程序要重启才生效."""
    try:
        wininet = ctypes.windll.Wininet  # type: ignore[attr-defined]
        wininet.InternetSetOptionW(0, _INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        wininet.InternetSetOptionW(0, _INTERNET_OPTION_REFRESH, 0, 0)
    except Exception:
        pass


def _win_read() -> dict[str, object]:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS) as key:
        out: dict[str, object] = {}
        for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"):
            try:
                out[name] = winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                out[name] = None
        return out


def _win_write(**values: object) -> None:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0, winreg.KEY_SET_VALUE
    ) as key:
        for name, value in values.items():
            if value is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            elif isinstance(value, int):
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))


_BACKUP = "system_proxy_backup.json"


def backup_system_proxy() -> None:
    if sys.platform != "win32":
        return
    path = paths.cache_dir() / _BACKUP
    if path.exists():
        return
    json_dump(path, _win_read())


def restore_system_proxy() -> None:
    if sys.platform != "win32":
        return
    path = paths.cache_dir() / _BACKUP
    data = json_load(path, None)
    if not isinstance(data, dict):
        return
    _win_write(
        ProxyEnable=int(data.get("ProxyEnable") or 0),
        ProxyServer=data.get("ProxyServer"),
        ProxyOverride=data.get("ProxyOverride"),
        AutoConfigURL=data.get("AutoConfigURL"),
    )
    _refresh_wininet()


def _set_user_env(name: str, value: str | None) -> None:
    """设置用户级环境变量(供终端/CLI 工具使用), 并广播变更."""
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
        ) as key:
            if value is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        try:
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x1A
            SMTO_ABORTIFHUNG = 0x0002
            result = ctypes.c_long()
            ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
                HWND_BROADCAST,
                WM_SETTINGCHANGE,
                0,
                ctypes.c_wchar_p("Environment"),
                SMTO_ABORTIFHUNG,
                5000,
                ctypes.byref(result),
            )
        except Exception:
            pass
    else:
        os.environ[name] = value or ""


ENV_KEYS = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"]
ENV_KEYS_LOWER = [k.lower() for k in ENV_KEYS]


def set_env_proxy(host_port: str, no_proxy: str = "localhost,127.0.0.1,::1") -> None:
    url = f"http://{host_port}"
    for key in ENV_KEYS + ENV_KEYS_LOWER:
        if key.upper() == "NO_PROXY":
            _set_user_env(key, no_proxy)
        else:
            _set_user_env(key, url)


def clear_env_proxy() -> None:
    for key in ENV_KEYS + ENV_KEYS_LOWER:
        _set_user_env(key, None)


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #


def enable(st: AppState, *, with_env: bool = True) -> str:
    host_port = f"127.0.0.1:{st.mixed_port}"
    if sys.platform == "win32":
        backup_system_proxy()
        _win_write(
            ProxyEnable=1,
            ProxyServer=host_port,
            ProxyOverride=st.bypass,
            AutoConfigURL=None,
        )
        _refresh_wininet()
        detail = f"Windows 系统代理 -> {host_port}"
    elif sys.platform == "darwin":
        for svc in _mac_services():
            run_hidden(["networksetup", "-setwebproxy", svc, "127.0.0.1", str(st.mixed_port)])
            run_hidden(
                ["networksetup", "-setsecurewebproxy", svc, "127.0.0.1", str(st.mixed_port)]
            )
        detail = f"macOS 网络服务代理 -> {host_port}"
    else:
        run_hidden(
            [
                "gsettings",
                "set",
                "org.gnome.system.proxy",
                "mode",
                "manual",
            ]
        )
        run_hidden(
            [
                "gsettings",
                "set",
                "org.gnome.system.proxy.http",
                "host",
                "127.0.0.1",
            ]
        )
        run_hidden(
            [
                "gsettings",
                "set",
                "org.gnome.system.proxy.http",
                "port",
                str(st.mixed_port),
            ]
        )
        detail = f"GNOME 代理 -> {host_port}"
    if with_env:
        set_env_proxy(host_port)
        detail += " | 已写入终端环境变量"
    st.system_proxy_on = True
    return detail


def disable(st: AppState, *, with_env: bool = True) -> str:
    if sys.platform == "win32":
        _win_write(ProxyEnable=0)
        _refresh_wininet()
        restore_system_proxy()
        detail = "已关闭 Windows 系统代理"
    elif sys.platform == "darwin":
        for svc in _mac_services():
            run_hidden(["networksetup", "-setwebproxystate", svc, "off"])
            run_hidden(["networksetup", "-setsecurewebproxystate", svc, "off"])
        detail = "已关闭 macOS 系统代理"
    else:
        run_hidden(["gsettings", "set", "org.gnome.system.proxy", "mode", "none"])
        detail = "已关闭 GNOME 代理"
    if with_env:
        clear_env_proxy()
        detail += " | 已清除终端环境变量"
    st.system_proxy_on = False
    return detail


def status() -> tuple[bool, str]:
    if sys.platform != "win32":
        return (False, "非 Windows 平台, 请查看系统网络设置")
    try:
        data = _win_read()
    except Exception as e:
        return (False, f"读取系统代理设置失败: {e}")
    enabled = bool(data.get("ProxyEnable"))
    server = data.get("ProxyServer") or ""
    return enabled, str(server)


def _mac_services() -> list[str]:
    code, out = run_hidden(["networksetup", "-listallnetworkservices"])
    services = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if line and not line.startswith("*"):
            services.append(line)
    return services


# --------------------------------------------------------------------------- #
# TUN 模式
# --------------------------------------------------------------------------- #


def tun_available() -> tuple[bool, str]:
    """检查 TUN 模式前置条件."""
    from .util import is_admin

    if sys.platform == "win32":
        if not paths.wintun_dll().exists():
            return False, "缺少 wintun.dll, 请运行: accesspilot core install"
        if not is_admin():
            return False, "TUN 模式需要管理员权限, 请用管理员身份运行终端"
        return True, "TUN 可用(需要管理员权限)"
    if not is_admin():
        return False, "TUN 模式需要 root 权限"
    return True, "TUN 可用"
