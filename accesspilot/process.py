"""内核进程管理: 启动/停止/重启/状态/日志."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import api, config, paths, sysproxy
from .state import AppState, load_state, save_state
from .subscription import Subscription, load_profile
from .util import Fail, info, json_dump, json_load, ok, run_hidden, warn


def _read_pid() -> dict[str, object] | None:
    data = json_load(paths.pid_file(), None)
    if isinstance(data, dict) and data.get("pid"):
        return data
    return None


def _win_pid_alive(pid: int) -> bool | None:
    """直接问 Windows 内核"这个 pid 还活着吗".

    为什么不用 tasklist: 它要**起一个进程**, 实测一次约 80ms; 而
    `running_pid()` 在 `stop()` 的等待循环里会被反复调用, 界面刷新状态时
    每 1~2 秒也要问一次 —— 实测让一次只读快照从 <20ms 变成 165ms。
    这里是一次内核调用, 微秒级。

    返回 True/False = 确定的答案; None = 问不出来(调用方自己兜底)。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: PERF203
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        # PROCESS_QUERY_LIMITED_INFORMATION: 权限要求最低, 足够问退出码
        handle = k32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            err = ctypes.get_last_error()
            # 5 = ERROR_ACCESS_DENIED -> 进程在, 只是不让我们打开(受保护进程)
            # 87 = ERROR_INVALID_PARAMETER -> pid 不存在
            if err == 5:
                return True
            if err == 87:
                return False
            return None
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: PERF203
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        fast = _win_pid_alive(pid)
        if fast is not None:
            return fast
        # 极少数情况问不出来才退回老办法(慢, 但只在这条路径上)
        code, out = run_hidden(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], timeout=15
        )
        return code == 0 and str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _process_name(pid: int) -> str:
    if sys.platform == "win32":
        code, out = run_hidden(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], timeout=15
        )
        if code == 0 and out.strip().startswith('"'):
            return out.split('","')[0].strip('"')
        return "?"
    return "?"


def _listeners(port: int) -> list[int]:
    """返回正在 LISTEN 指定端口的进程 pid(PID 0 表示系统保留)."""
    if sys.platform == "win32":
        code, out = run_hidden(["netstat", "-ano", "-p", "TCP"], timeout=20)
        pids: list[int] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and "LISTEN" in parts[3].upper():
                if parts[1].endswith(f":{port}"):
                    try:
                        pid = int(parts[4])
                    except ValueError:
                        continue
                    if pid > 0:
                        pids.append(pid)
        return sorted(set(pids))
    pids = []
    import glob

    for path in glob.glob("/proc/[0-9]*"):
        try:
            with open(f"{path}/comm") as fh:
                pass
        except OSError:
            continue
    return pids


def _kill_pid(pid: int) -> None:
    if sys.platform == "win32":
        run_hidden(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=20)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def _wait_port_free(port: int, timeout: float = 20.0) -> bool:
    """等端口真正释放(进程死了不代表端口立即可用, 偶发"绑定失败")."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _listeners(port):
            return True
        time.sleep(0.5)
    return False


def reclaim_ports(st: AppState, *, auto: bool = True) -> None:
    """启动前预检端口: 清理残留内核, 对其它程序给出明确报错.

    没有这道检查, 残留的旧内核会继续占用 9090 端口, 新旧实例的
    external-controller 密钥又不同, 结果表现为"启动成功但接口 401",
    非常难排查。
    """
    for label, port in (("代理端口", st.mixed_port), ("控制端口", st.api_port)):
        for pid in _listeners(port):
            if pid == os.getpid():
                continue
            name = _process_name(pid)
            low = name.lower()
            if low.startswith(("mihomo", "clash")):
                if not auto:
                    raise Fail(f"{label} {port} 被残留内核占用 (pid={pid})")
                warn(f"清理残留内核进程 pid={pid} ({label} {port})")
                _kill_pid(pid)
                if not _wait_port_free(port):
                    raise Fail(f"{label} {port} 未能释放, 请稍后重试")
            elif not _pid_alive(pid):
                # 进程刚死、端口还没归还给系统(Windows 上常见), 等一下即可
                if not _wait_port_free(port):
                    raise Fail(f"{label} {port} 未能释放, 请稍后重试")
            else:
                raise Fail(
                    f"{label} {port} 已被 {name}(pid={pid}) 占用。\n"
                    f"      请关闭该程序, 或修改端口后重试:\n"
                    f"        accesspilot config set-port {port + 1}"
                )
    paths.pid_file().unlink(missing_ok=True)


def running_pid() -> int | None:
    data = _read_pid()
    if not data:
        return None
    pid = int(data["pid"])  # type: ignore[arg-type]
    if _pid_alive(pid):
        return pid
    return None


def is_running() -> bool:
    return running_pid() is not None


def status() -> dict[str, object]:
    pid = running_pid()
    st = load_state()
    version = ""
    if pid:
        try:
            version = api.version(st)
        except Exception:
            version = "API 未响应"
    enabled, server = sysproxy.status()
    return {
        "running": bool(pid),
        "pid": pid,
        "core_version": version,
        "mixed_port": st.mixed_port,
        "api_port": st.api_port,
        "profile": st.active_profile,
        "tun": st.tun_enable,
        "accel": st.accel_enable,
        "accel_running": accel_running(),
        "accel_port": st.accel_port,
        "system_proxy": enabled,
        "system_proxy_server": server,
        "config": str(paths.config_file()),
        "log": str(paths.log_file()),
    }


def _spawn(binary: Path, args: list[str]) -> int:
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    log = open(paths.log_file(), "ab", buffering=0)
    kwargs: dict[str, object] = {
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "cwd": str(paths.runtime_dir()),
        "close_fds": True,
    }
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = (
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([str(binary), *args], **kwargs)  # type: ignore[arg-type]
    return proc.pid


def start(
    sub: Subscription | None = None,
    st: AppState | None = None,
    *,
    tun: bool | None = None,
    system_proxy: bool | None = None,
) -> dict[str, object]:
    """生成配置并启动内核.

    system_proxy: None = 自动(未启用 TUN 时开启系统代理),
                  True = 强制开启, False = 强制不碰系统代理设置。
    """
    paths.ensure_dirs()
    st = st or load_state()
    if is_running():
        raise Fail("内核已在运行, 请先执行 stop, 或使用 restart")

    binary = paths.core_binary()
    if not binary.exists():
        raise Fail("未安装内核, 请先执行: accesspilot core install")

    if sub is None:
        if not st.active_profile:
            # 没有任何订阅时的兜底: 只要还有"能承载流量的东西"
            # (免费 WARP 出口), 或者开了直连加速, 就自动建一个空配置档
            from . import warp as warp_mod

            has_warp = warp_mod.load_profile() is not None
            if st.accel_enable or has_warp:
                from .subscription import Subscription, save_profile

                name = "内置节点" if has_warp else "仅直连加速"
                sub = Subscription(name=name, proxies=[])
                st.active_profile = save_profile(sub)
                info(f"未选择配置档, 已自动创建「{name}」")
            else:
                raise Fail(
                    "尚未选择配置档。任选一种方式:\n"
                    "        accesspilot sub add <订阅链接>        # 用机场订阅\n"
                    "        accesspilot warp register             # 免费注册 Cloudflare WARP 出口\n"
                    "        accesspilot accel on                  # 免节点直连加速(仅 GitHub 系)"
                )
        else:
            sub = load_profile(st.active_profile)

    if tun is not None:
        st.tun_enable = bool(tun)
    if st.tun_enable:
        available, reason = sysproxy.tun_available()
        if not available:
            warn(f"TUN 模式不可用: {reason}")
            warn("将仅启用系统代理模式")
            st.tun_enable = False

    st.ensure_secret()
    save_state(st)

    reclaim_ports(st)

    info("生成配置 ...")
    cfg_path = config.render(sub, st)
    passed, output = config.test_config(cfg_path)
    if not passed:
        raise Fail(f"配置校验失败:\n{output}")
    ok(f"配置校验通过 ({len(sub.proxies)} 个节点)")

    log_size = paths.log_file().stat().st_size if paths.log_file().exists() else 0
    pid = _spawn(binary, ["-d", str(paths.runtime_dir()), "-f", str(cfg_path)])
    json_dump(
        paths.pid_file(),
        {
            "pid": pid,
            "started_at": time.time(),
            "config": str(cfg_path),
            "log_offset": log_size,
            "profile": sub.name,
        },
    )

    deadline = time.time() + 12
    while time.time() < deadline:
        if api.ping(st):
            break
        if not _pid_alive(pid):
            raise Fail(f"内核启动失败, 请查看日志: {paths.log_file()}")
        time.sleep(0.4)
    else:
        warn("内核已启动, 但控制接口响应较慢, 请稍后查看状态")

    st.last_start = time.time()
    save_state(st)

    if st.accel_enable:
        pid_accel = start_accel(st)
        ok(f"直连加速已启动 (pid={pid_accel}, socks5://127.0.0.1:{st.accel_port})")

    do_system_proxy = (not st.tun_enable) if system_proxy is None else bool(system_proxy)
    if do_system_proxy:
        detail = sysproxy.enable(st)
        st.system_proxy_on = True
        save_state(st)
        ok(detail)
        print(dim("    想恢复原状随时执行:  accesspilot stop   或   accesspilot proxy off"))
    # 无论是否开系统代理都挂上看门狗: 内核意外退出时由它负责收拾残局
    start_watchdog(pid)

    mode = "TUN 全局模式" if st.tun_enable else "系统代理模式"
    ok(f"内核已启动 (pid={pid}, {mode}), 代理端口 127.0.0.1:{st.mixed_port}")
    return status()


def stop(*, keep_system_proxy: bool = False, clean_orphans: bool = True) -> bool:
    st = load_state()
    pid = running_pid()
    stop_watchdog()
    if st.system_proxy_on and not keep_system_proxy:
        sysproxy.disable(st)
        save_state(st)
    stop_accel()
    killed = False
    if pid:
        _kill_pid(pid)
        deadline = time.time() + 8
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.3)
        killed = True
    paths.pid_file().unlink(missing_ok=True)

    # 清理任何仍在占用监听端口的残留内核(例如上一次异常退出),
    # 并且等端口真正释放 —— 否则紧接着的 start 会绑定失败
    if clean_orphans and sys.platform == "win32":
        for port in (st.mixed_port, st.api_port):
            for other in _listeners(port):
                if other == os.getpid():
                    continue
                if _process_name(other).lower().startswith(("mihomo", "clash")):
                    warn(f"清理残留内核进程 pid={other} (端口 {port})")
                    _kill_pid(other)
                    killed = True
            _wait_port_free(port)
    return killed


def restart(
    sub: Subscription | None = None,
    st: AppState | None = None,
    *,
    tun: bool | None = None,
    system_proxy: bool | None = None,
) -> dict[str, object]:
    was_running = is_running()
    if was_running:
        stop(keep_system_proxy=True)
        time.sleep(0.5)
    return start(sub, st, tun=tun, system_proxy=system_proxy)


def reload_config(sub: Subscription | None = None, st: AppState | None = None) -> None:
    """热重载配置(不断开进程).

    安全不变量: **先本地校验, 校验失败绝不提交**。否则一个坏节点会让正在
    运行的内核加载失败, 用户的所有代理流量瞬间全部中断。
    """
    st = st or load_state()
    if sub is None:
        if not st.active_profile:
            raise Fail("尚未选择配置档")
        sub = load_profile(st.active_profile)
    cfg_path = config.render(sub, st)
    passed, output = config.test_config(cfg_path)
    if not passed:
        raise Fail(
            f"新配置校验失败, 已保留正在运行的旧配置(代理不受影响):\n{output[:600]}"
        )
    if is_running():
        api.reload(st, cfg_path)
        ok("配置已热重载")
    else:
        ok(f"配置已生成: {cfg_path}")


def tail_log(lines: int = 60) -> str:
    path = paths.log_file()
    if not path.exists():
        return "(暂无日志)"
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.readlines()
    return "".join(content[-lines:])


# --------------------------------------------------------------------------- #
# 免节点直连加速(独立子进程, 与内核生命周期绑定)
# --------------------------------------------------------------------------- #


def _read_accel_pid() -> dict[str, object] | None:
    data = json_load(paths.accel_pid_file(), None)
    if isinstance(data, dict) and data.get("pid"):
        return data
    return None


def accel_running() -> bool:
    data = _read_accel_pid()
    return bool(data and _pid_alive(int(data["pid"])))  # type: ignore[arg-type]


def start_accel(st: AppState | None = None, *, port: int | None = None) -> int:
    """启动本地 IP 优选器(SOCKS5)。已在运行则直接返回其 pid。"""
    st = st or load_state()
    if accel_running():
        return int(_read_accel_pid()["pid"])  # type: ignore[arg-type]
    listen_port = port or st.accel_port
    # 端口被其它程序占用时给出明确报错, 而不是静默失败
    for other in _listeners(listen_port):
        if other != os.getpid() and _process_name(other).lower().startswith(("python", "accesspilot")):
            _kill_pid(other)
    env = os.environ.copy()
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    log = open(paths.accel_log_file(), "ab", buffering=0)
    kwargs: dict[str, object] = {
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "cwd": str(paths.runtime_dir()),
        "close_fds": True,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(  # type: ignore[arg-type]
        [sys.executable, "-m", "accesspilot", "accel", "serve", "--port", str(listen_port)],
        **kwargs,
    )
    json_dump(
        paths.accel_pid_file(),
        {"pid": proc.pid, "port": listen_port, "started_at": time.time()},
    )
    for _ in range(20):
        if _listeners(listen_port):
            return proc.pid
        if not _pid_alive(proc.pid):
            break
        time.sleep(0.2)
    warn(f"直连加速器未能在端口 {listen_port} 上就绪, 详见 {paths.accel_log_file()}")
    return proc.pid


def stop_accel() -> bool:
    data = _read_accel_pid()
    killed = False
    if data and _pid_alive(int(data["pid"])):  # type: ignore[arg-type]
        _kill_pid(int(data["pid"]))  # type: ignore[arg-type]
        killed = True
    paths.accel_pid_file().unlink(missing_ok=True)
    return killed


def accel_log_tail(lines: int = 40) -> str:
    path = paths.accel_log_file()
    if not path.exists():
        return "(暂无日志)"
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return "".join(fh.readlines()[-lines:])


# --------------------------------------------------------------------------- #
# 守护进程: 防止"内核已死但系统代理还开着"把整台机器搞断网
# --------------------------------------------------------------------------- #


def start_watchdog(core_pid: int) -> int | None:
    """启动看门狗: 内核进程消失时立刻还原系统代理.

    没有这道防护时的事故现场: 内核崩溃 -> 系统代理仍指向 127.0.0.1:7890
    -> 死端口 -> 整台机器所有网站都打不开(连本该直连的 B 站也打不开)。
    """
    stop_watchdog()
    env = os.environ.copy()
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_parent + os.pathsep + env.get("PYTHONPATH", "")
    kwargs: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "cwd": str(paths.runtime_dir()),
        "close_fds": True,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(  # type: ignore[arg-type]
            [sys.executable, "-m", "accesspilot", "_watchdog", "--pid", str(core_pid)],
            **kwargs,
        )
    except Exception:
        return None
    json_dump(paths.watchdog_pid_file(), {"pid": proc.pid, "core_pid": core_pid})
    return proc.pid


def stop_watchdog() -> None:
    data = json_load(paths.watchdog_pid_file(), None)
    if isinstance(data, dict) and data.get("pid"):
        try:
            pid = int(data["pid"])
            if _pid_alive(pid):
                _kill_pid(pid)
        except (TypeError, ValueError):
            pass
    paths.watchdog_pid_file().unlink(missing_ok=True)


def heal_if_broken(*, quiet: bool = False) -> bool:
    """自愈: 状态显示系统代理开着但内核已不在 -> 立刻还原系统代理.

    这是最后一道防线 —— 即使用户什么都没做, 只要他再调用一次本工具
    (或下次开机后第一次调用), 网络就会被修好, 而不是一直断着。
    """
    st = load_state()
    if not st.system_proxy_on:
        return False
    if is_running():
        return False
    enabled, server = sysproxy.status()
    st.system_proxy_on = False
    save_state(st)
    if enabled:
        sysproxy.disable(st)
        if not quiet:
            warn(
                f"发现内核已退出但系统代理仍指向 {server or '失效端口'}, "
                "已自动还原(否则整台机器都上不了网)"
            )
        return True
    return False
