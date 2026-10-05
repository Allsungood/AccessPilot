"""系统代理与 TUN 模式集成(以 Windows 为主, 兼顾 macOS/Linux)."""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time

from . import paths
from .state import AppState
from .util import json_dump, json_load, run_hidden, warn

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
    """记录**我们动手之前**的系统代理状态。

    只拍一次快照(否则第二次 enable 会把"我们自己开的"当成用户的原始状态存进去,
    关闭时就还不回去了)。但如果当前注册表里已经是我们自己的配置 —— 例如上一轮
    异常退出没来得及恢复 —— 那就不能拿它当快照, 否则等于把"关闭"变成"保持开启"。
    """
    if sys.platform != "win32":
        return
    path = paths.cache_dir() / _BACKUP
    if path.exists():
        return
    data = _win_read()
    server = str(data.get("ProxyServer") or "")
    if data.get("ProxyEnable") and server.startswith("127.0.0.1:") and _looks_like_ours(server):
        return
    json_dump(path, data)


def _looks_like_ours(server: str) -> bool:
    """这个 server 是不是我们自己的本地端口(而不是别人的代理)."""
    try:
        port = int(server.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return False
    from .state import DEFAULT_MIXED_PORT

    st = None
    try:
        from .state import load_state

        st = load_state()
    except Exception:
        pass
    ours = {getattr(st, "mixed_port", None), getattr(st, "accel_port", None), DEFAULT_MIXED_PORT}
    return port in {p for p in ours if isinstance(p, int)}


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
    # 恢复完就把快照删掉。留着它是有害的: 下次 enable 因为"文件已存在"
    # 不会再拍新快照, 于是这份陈旧状态会一直传下去 —— 用户在我们关闭代理之后
    # 自己开的代理, 会被下一次 disable 用这份旧快照覆盖掉。
    path.unlink(missing_ok=True)


def _do_broadcast() -> None:
    try:
        # PDWORD_PTR 是**指针宽度**(x64 上 8 字节)。原来传的是 c_long()
        # —— 只有 4 字节, 而系统会往这个地址写满 8 字节, 实测每次调用都会
        # 越界写掉紧随其后的 4 个字节。日常可能看不出问题, 但它是内存破坏,
        # 必须改成 c_size_t 才是对的宽度。
        hwnd_broadcast = 0xFFFF
        wm_settingchange = 0x1A
        smto_abortifhung = 0x0002
        result = ctypes.c_size_t()
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.SendMessageTimeoutW.restype = ctypes.c_size_t
        user32.SendMessageTimeoutW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        user32.SendMessageTimeoutW(
            ctypes.c_void_p(hwnd_broadcast),
            wm_settingchange,
            None,
            ctypes.c_wchar_p("Environment"),
            smto_abortifhung,
            1000,
            ctypes.byref(result),
        )
    except Exception:  # noqa: BLE001
        pass


def _broadcast_env_change(*, wait: float = 0.0) -> None:
    """广播 WM_SETTINGCHANGE, 让已经开着的程序重新读环境变量。

    三个必须遵守的约束, 全部来自实测:

    1. **一批只广播一次, 绝不能每个变量广播一次。** 原来是 8 个变量各广播一次。
    2. **默认放到后台线程里, 绝不能让调用方等它。** 实测这台机器上有 334 个
       顶层窗口, 一次广播要 **25.3 秒** —— 超时是按**每个窗口**算的, 不是总共,
       所以传 5000 也拦不住。放在调用路径上就意味着: 用户点一下「关闭」,
       界面卡 25 秒。这正是"红杏卡死了"的来源之一。
    3. 只有**真的改动过**变量时才调用它(见 clear_env_proxy / purge_stale_env)。

    wait>0 时最多等这么久 —— 只有 `accesspilot proxy env` 这种"用户马上要开
    终端用"的场景才需要, 其余一律 fire-and-forget。
    """
    if sys.platform != "win32":
        return
    if wait > 0:
        # 常见情况是"窗口不多, 秒回"; 真慢也不超过 wait
        t = threading.Thread(target=_do_broadcast, name="ap-env-broadcast", daemon=True)
        t.start()
        t.join(timeout=wait)
        return
    threading.Thread(target=_do_broadcast, name="ap-env-broadcast", daemon=True).start()


def _set_user_env(name: str, value: str | None) -> None:
    """设置/删除一个用户级环境变量。**不广播** —— 成批改完由调用方广播一次。"""
    if sys.platform != "win32":
        os.environ[name] = value or ""
        return
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


ENV_KEYS = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"]
ENV_KEYS_LOWER = [k.lower() for k in ENV_KEYS]


def set_env_proxy(host_port: str, no_proxy: str = "localhost,127.0.0.1,::1") -> None:
    url = f"http://{host_port}"
    for key in ENV_KEYS + ENV_KEYS_LOWER:
        _set_user_env(key, no_proxy if key.upper() == "NO_PROXY" else url)
    # 这是用户**显式**要求"给我的终端配上"的场景, 等一下广播是值得的 ——
    # 否则他新开的终端读到的还是旧环境, 会以为命令没生效。
    _broadcast_env_change(wait=3.0)


def clear_env_proxy() -> None:
    """清除我们写过的终端代理变量。

    只有**真的删掉了东西**才广播。广播本身很贵(这台机器上一次 25 秒),
    而"本来就没有这些变量"是最常见的情况。
    """
    removed: list[str] = []
    for key in ENV_KEYS + ENV_KEYS_LOWER:
        if _read_user_env(key) is not None:
            _set_user_env(key, None)
            removed.append(key)
    if removed:
        _broadcast_env_change()


def _read_user_env(name: str) -> str | None:
    """读用户级环境变量(不展开, 原样)."""
    if sys.platform != "win32":
        return os.environ.get(name)
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except (FileNotFoundError, OSError):
        return None


def _our_proxy_urls(st: AppState | None = None) -> set[str]:
    """所有"指向本机内核端口"的代理 URL 写法."""
    ports: set[int] = set()
    if st is not None:
        ports.add(int(st.mixed_port))
        ports.add(int(st.accel_port))
    from .state import DEFAULT_MIXED_PORT

    ports.add(DEFAULT_MIXED_PORT)
    urls: set[str] = set()
    for p in ports:
        for host in ("127.0.0.1", "localhost"):
            urls.add(f"http://{host}:{p}")
            urls.add(f"socks5://{host}:{p}")
            urls.add(f"socks5h://{host}:{p}")
    return urls


def purge_stale_env(st: AppState | None = None) -> list[str]:
    """清掉**指向我们自己端口**的残留终端代理变量, 返回被清掉的变量名。

    为什么必须做这件事: `set_env_proxy` 写的是 HKCU\\Environment —— 那是
    **用户级、持久、对之后启动的每一个进程都生效**的。一旦写进去而没清干净,
    它会劫持所有后续程序的网络请求(包括我们自己的更新检查, 以及用户终端里的
    任何工具), 而用户完全看不出是谁干的。这是本项目里杀伤面最大的一个副作用。

    安全边界: **只删值等于我们自己端口的那几个变量**。用户自己设的公司代理、
    其它工具设的值, 一律不碰 —— 值不匹配就原样留着。
    """
    if sys.platform != "win32":
        return []
    ours = _our_proxy_urls(st)
    removed: list[str] = []
    for key in ENV_KEYS + ENV_KEYS_LOWER:
        if key.upper() == "NO_PROXY":
            continue
        cur = _read_user_env(key)
        if cur is not None and cur.strip().rstrip("/") in {u.rstrip("/") for u in ours}:
            _set_user_env(key, None)
            removed.append(key)
    if removed:
        _broadcast_env_change()
    return removed


# --------------------------------------------------------------------------- #
# 生效校验: "写进去了" != "真的生效"
# --------------------------------------------------------------------------- #


def effective(st: AppState) -> tuple[bool, str]:
    """系统代理此刻**真实**是不是我们要的那一个。

    这是本项目最重要的一次判断。原来只写不读: 写注册表 -> 直接认定成功 ->
    把 st.system_proxy_on 置 True。而实测有程序会在 1~29 秒内把 ProxyEnable
    改回 0(360 主动防御、蓝灯、FastGithub、浏览器「重置代理设置」都干过这事)。
    于是界面显示"已连接", 实际全部直连 —— 用户看到的就是"红杏又断了"。
    """
    if sys.platform != "win32":
        return True, "非 Windows, 跳过"
    try:
        data = _win_read()
    except Exception as e:  # noqa: BLE001
        return False, f"读注册表失败: {e}"
    pac = data.get("AutoConfigURL")
    if pac:
        return False, f"有 PAC 脚本在接管代理设置: {pac}"
    if not data.get("ProxyEnable"):
        return False, "ProxyEnable 被改成了 0(外部程序干的)"
    server = str(data.get("ProxyServer") or "")
    want = f"127.0.0.1:{st.mixed_port}"
    if server != want:
        return False, f"代理指向了别处: {server or '(空)'}, 期望 {want}"
    return True, want


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #


def enable(st: AppState, *, with_env: bool = False, attempts: int = 3) -> str:
    """开启系统代理, 并且**校验它真的生效了**。

    with_env 默认是 False —— 这一点是刻意改的。写 HKCU\\Environment 是用户级、
    持久、影响之后所有进程的副作用, 不该是"开个代理"的默认后果。需要给终端里的
    命令行工具用的用户, 显式执行 `accesspilot proxy env` 即可。
    """
    host_port = f"127.0.0.1:{st.mixed_port}"
    if sys.platform == "win32":
        backup_system_proxy()
        # 写 -> 读回校验 -> 不对就重试。外部程序可能在毫秒级把值改掉,
        # 一次写不成不代表失败, 但在报告成功之前必须确认它真的生效了。
        ok_now, why = False, ""
        for i in range(max(1, attempts)):
            _win_write(
                ProxyEnable=1,
                ProxyServer=host_port,
                ProxyOverride=st.bypass,
                AutoConfigURL=None,
            )
            _refresh_wininet()
            time.sleep(0.12 * (i + 1))
            ok_now, why = effective(st)
            if ok_now:
                break
        detail = f"Windows 系统代理 -> {host_port}"
        if not ok_now:
            # 不谎报成功。上层(guard/界面)要靠这句话决定是否继续重试。
            detail += f" ⚠ 写入未能生效: {why}"
            warn(f"系统代理写入未能生效: {why}")
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
    else:
        # 不写环境变量时, 顺手清掉历史版本留下的、指向我们自己端口的残留值。
        purged = purge_stale_env(st)
        if purged:
            detail += f" | 已清理残留环境变量 {', '.join(purged)}"
    st.system_proxy_on = True
    return detail


def pause(st: AppState) -> bool:
    """临时摘掉系统代理, 但**保留**原始快照。

    用于内核重启的空窗期。restart() 原来用 keep_system_proxy=True 让系统代理
    一直开着, 而此时内核已经没了, 新内核还要跑 `mihomo -t` 配置校验(六千节点的
    配置要几十秒)—— 这段时间系统代理指向的是一个**没人监听的端口**, 浏览器
    全部超时。这正是用户说的"红杏又断了"。

    摘掉它, 重启完成后 start() 会自己重新打开。

    刻意不调 disable(): disable() 会 restore_system_proxy() 把用户的原始设置
    还回去**并删掉快照**, 那样 start() 就得重新拍一次快照, 快照语义会被搅乱
    (可能把"我们自己开的"当成用户的原始状态存下来)。
    """
    if sys.platform != "win32":
        return False
    try:
        _win_write(ProxyEnable=0)
        _refresh_wininet()
        return True
    except Exception:  # noqa: BLE001
        return False


def disable(st: AppState, *, with_env: bool = True) -> str:
    if sys.platform == "win32":
        _win_write(ProxyEnable=0)
        _refresh_wininet()
        restore_system_proxy()
        detail = "已关闭 Windows 系统代理"
        if not with_env:
            purged = purge_stale_env(st)
            if purged:
                detail += f" | 已清理残留环境变量 {', '.join(purged)}"
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
