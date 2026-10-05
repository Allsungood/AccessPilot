"""SuperLantern: 多后端抗审查总控层.

为什么是"总控"而不是"新协议":
  * 抗封锁能力 = 客户端技术 + **服务器舰队**。蓝灯/Geph/Psiphon 的客户端开源,
    但服务器舰队不开源 —— 没有服务器就不可能"融合"出更强的隧道。
  * 真正缺的是**编排**: 这些工具互相冲突、要手动切换、挂了不会自恢复。
    把这一层做好, 用户拿到的稳定性提升是实打实的。

设计要点(每一条都来自这几天踩过的坑):
  1. **同时只允许一个后端在跑** —— TUN/系统代理会互相打架, 两个一起开必断网;
  2. **健康判定用真实平台**, 不看"进程在不在" —— 进程活着但隧道死了是常态;
  3. **切换失败必须能回到直连** —— 绝不把系统代理指向死端口;
  4. **故障自动转移** —— 当前后端不健康时, 按优先级尝试下一个。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import paths, process, sysproxy
from .util import Fail, info, ok, run_hidden, warn

#: 健康判定用的目标: 能打开 X/Discord/Google 才算"能用"
HEALTH_TARGETS = ["x", "discord", "google"]

#: 通过阈值: 至少这么多目标通过
HEALTH_MIN_PASS = 2


@dataclass
class Backend:
    """一个可管理的抗审查后端."""

    key: str
    name: str
    note: str
    exe_candidates: list[str] = field(default_factory=list)
    process_names: list[str] = field(default_factory=list)
    service: str = ""
    priority: int = 50
    internal: bool = False  # True = 由 AccessPilot 自己拉起(mihomo 免费节点池)
    manual_connect: bool = False  # True = 启动后需要用户在它自己的界面点「连接」
    stop_hint: str = ""

    # ------------------------------------------------------------------ #
    def exe(self) -> Path | None:
        for cand in self.exe_candidates:
            p = Path(os.path.expandvars(cand))
            if p.exists():
                return p
        return None

    def installed(self) -> bool:
        if self.internal:
            try:
                from .subscription import list_profiles

                return bool(list_profiles())
            except Exception:
                return False
        if self.exe() is not None:
            return True
        if self.service:
            code, _ = run_hidden(["sc", "query", self.service], timeout=15)
            return code == 0
        return False

    def running(self) -> bool:
        if self.internal:
            return process.is_running()
        for name in self.process_names:
            code, out = run_hidden(
                ["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/NH"], timeout=15
            )
            if code == 0 and name.lower() in out.lower():
                return True
        return False


BACKENDS: list[Backend] = [
    Backend(
        key="lantern",
        name="蓝灯 Lantern",
        note="域名前置; 免费有月度额度",
        exe_candidates=[r"C:\Program Files\Lantern\lantern.exe"],
        process_names=["lantern", "lanternd"],
        service="LanternSvc",
        priority=10,
        # 实测: 只启动 lantern.exe 不会建隧道, 必须在它窗口里点一次「连接」。
        # 程序点不了别的 GUI 按钮, 所以这里如实标注, 由 CLI 提示用户。
        manual_connect=True,
    ),
    Backend(
        key="geph",
        name="迷霧通 Geph",
        note="抗封锁口碑最好; 免费限速",
        exe_candidates=[
            r"C:\Program Files\Geph\geph.exe",
            r"C:\Program Files (x86)\Geph\geph.exe",
            r"%LOCALAPPDATA%\Programs\Geph\geph.exe",
        ],
        process_names=["geph", "geph4-client"],
        priority=20,
    ),
    Backend(
        key="psiphon",
        name="赛风 Psiphon",
        note="免安装单文件; 流量基本不限",
        exe_candidates=[
            r"%USERPROFILE%\Downloads\psiphon3.exe",
            r"%USERPROFILE%\Desktop\psiphon3.exe",
        ],
        process_names=["psiphon3", "psiphon"],
        priority=30,
    ),
    Backend(
        key="mihomo",
        name="AccessPilot 免费节点池",
        note="我们自己的免费节点(需 accesspilot free auto)",
        internal=True,
        priority=40,
    ),
]

BACKEND_BY_KEY = {b.key: b for b in BACKENDS}


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #


def probe_proxy() -> str | None:
    """当前探测应该用的代理.

    TUN 类后端(蓝灯/Geph)在网络层接管, 直连请求就会走它 —— 返回 None。
    代理类后端(Psiphon)会改系统代理 —— 需要显式用它。
    """
    try:
        enabled, server = sysproxy.status()
    except Exception:
        return None
    if enabled and server:
        s = str(server)
        if "=" in s:  # http=127.0.0.1:8080;https=...
            s = s.split("=", 1)[1].split(";", 1)[0]
        return f"http://{s}" if not s.startswith("http") else s
    return None


def health(*, timeout: float = 15.0, proxy: str | None = None) -> dict[str, Any]:
    """用真实平台探测当前链路是否可用."""
    from . import diag

    if proxy is None:
        proxy = probe_proxy()
    results = diag.probe_sites(proxy, keys=HEALTH_TARGETS, timeout=timeout)
    passed = [r for r in results if r.ok]
    lat = [r.latency_ms for r in passed]
    return {
        "proxy": proxy,
        "passed": len(passed),
        "total": len(results),
        "ok": len(passed) >= HEALTH_MIN_PASS,
        "avg_ms": int(sum(lat) / len(lat)) if lat else -1,
        "detail": {r.key: ("ok" if r.ok else r.detail[:40]) for r in results},
    }


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #


def stop_backend(b: Backend, *, force: bool = True) -> bool:
    """停掉一个后端(进程 + 服务 + 可能的系统代理)."""
    stopped = False
    if b.internal:
        try:
            if process.is_running():
                process.stop()
                stopped = True
        except Exception:
            pass
        return stopped
    for name in b.process_names:
        code, out = run_hidden(["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/NH"], timeout=15)
        if code == 0 and name.lower() in out.lower():
            run_hidden(["taskkill", "/IM", f"{name}.exe", "/T", "/F"], timeout=20)
            stopped = True
    if b.service:
        code, _ = run_hidden(["sc", "query", b.service], timeout=15)
        if code == 0:
            run_hidden(["sc", "stop", b.service], timeout=30)
    return stopped


def stop_all_except(key: str | None = None) -> list[str]:
    """保证只有一个后端在跑 —— 这是不断网的前提."""
    stopped: list[str] = []
    for b in BACKENDS:
        if b.key == key:
            continue
        if b.running():
            stop_backend(b)
            stopped.append(b.key)
    if stopped:
        time.sleep(2)
    return stopped


def start_backend(b: Backend) -> bool:
    """启动一个后端. 内部后端走 AccessPilot, 外部后端直接拉起 exe."""
    if b.internal:
        from .state import load_state
        from .subscription import list_profiles

        st = load_state()
        if not st.active_profile:
            subs = list_profiles()
            if not subs:
                raise Fail("没有配置档, 请先执行 accesspilot free auto")
            st.active_profile = subs[0].name
        # 内部后端不碰系统代理: mihomo 走混合端口, 需要用户显式 proxy on
        process.start(st=st, tun=st.tun_enable, system_proxy=False)
        return True

    exe = b.exe()
    if exe is None:
        raise Fail(f"{b.name} 未安装")

    # 有 Windows 服务的后端(蓝灯): 切换回来时必须先把服务拉起来 ——
    # 否则只启动界面进程, 隧道内核不会跟着起, 表现为"进程在跑但没网"。
    if b.service:
        code, out = run_hidden(["sc", "query", b.service], timeout=15)
        running = code == 0 and "RUNNING" in out.upper()
        if not running:
            run_hidden(["sc", "start", b.service], timeout=60)
            time.sleep(4)

    try:
        subprocess.Popen(
            [str(exe)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(exe.parent),
            creationflags=0x00000008 | 0x00000200 | 0x08000000 if sys.platform == "win32" else 0,
        )
    except Exception as e:
        raise Fail(f"启动 {b.name} 失败: {e}") from e
    return True


def wait_ready(b: Backend, *, timeout: float = 45.0) -> bool:
    """等后端起来(进程出现)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if b.running():
            return True
        time.sleep(1.5)
    return False


def wait_healthy(
    b: Backend,
    *,
    timeout: float = 150.0,
    interval: float = 12.0,
    verbose: bool = True,
) -> dict[str, Any]:
    """等后端真正能通 —— 必须轮询, 不能只探一次.

    实测: 蓝灯/赛风这类工具从进程出现到隧道建立要 30~90 秒(要先做
    域名前置引导、找可用服务器、协商隧道)。只探一次会把它们误判为
    "不可用", 然后被无谓地杀掉。
    """
    deadline = time.time() + timeout
    last: dict[str, Any] = {"ok": False, "passed": 0, "total": len(HEALTH_TARGETS)}
    round_no = 0
    while time.time() < deadline:
        round_no += 1
        last = health(timeout=12.0)
        if last["ok"]:
            return last
        if not b.running():
            return last
        if verbose:
            info(
                f"  {b.name}: 第 {round_no} 次探测 {last['passed']}/{last['total']}, "
                f"继续等待隧道建立 ..."
            )
        time.sleep(interval)
    return last


def restore_direct() -> None:
    """兜底: 保证回到直连状态(关系统代理, 停掉所有后端).

    判据必须用**实时注册表**, 不能只看 state.json 的标记。只看标记的话,
    "注册表指向我们、标记却是 false"的状态永远收不回来 —— 整台机器会一直
    指着一个已经不存在的端口。而且原来整个函数体套在 `except: pass` 里,
    连"没恢复成功"都不会说一声。
    """
    from .state import load_state, save_state

    st = load_state()
    try:
        live_ours, _ = sysproxy.effective(st)
    except Exception:  # noqa: BLE001
        live_ours = False
    if not (st.system_proxy_on or live_ours):
        return
    try:
        sysproxy.disable(st)
        st.system_proxy_on = False
        save_state(st)
    except Exception as e:  # noqa: BLE001
        warn(
            f"恢复直连失败: {e} —— 系统代理可能仍指向失效端口, "
            "请手动执行: accesspilot proxy off"
        )


# --------------------------------------------------------------------------- #
# 故障转移
# --------------------------------------------------------------------------- #


def auto(
    *,
    prefer: str | None = None,
    timeout: float = 75.0,
    budget: float = 220.0,
    verbose: bool = True,
) -> dict[str, Any]:
    """按优先级依次尝试各后端, 直到有一个真正能用.

    timeout: 单个后端最多等多久建立隧道
    budget : 整轮总预算 —— 必须设上限, 否则三个后端各等 150 秒会把
             调用方(终端/定时任务)拖到超时被掐断, 用户看到的就是"卡死"。

    返回 {"backend": key|None, "health": {...}, "tried": [...]}
    """
    order = sorted(BACKENDS, key=lambda b: b.priority)
    if prefer and prefer in BACKEND_BY_KEY:
        order = [BACKEND_BY_KEY[prefer]] + [b for b in order if b.key != prefer]

    started = time.time()
    tried: list[dict[str, Any]] = []
    for b in order:
        left = budget - (time.time() - started)
        if left <= 15:
            if verbose:
                warn(f"总预算 {budget:.0f}s 用尽, 停止尝试")
            break
        if not b.installed():
            tried.append({"key": b.key, "skip": "未安装"})
            if verbose:
                info(f"{b.name}: 未安装, 跳过")
            continue

        if verbose:
            info(f"尝试 {b.name} (最多等 {min(timeout, left):.0f}s) ...")
        stop_all_except(b.key)
        try:
            start_backend(b)
        except Fail as e:
            tried.append({"key": b.key, "error": str(e)})
            if verbose:
                warn(f"{b.name}: 启动失败 {e}")
            continue

        if not wait_ready(b, timeout=20.0):
            tried.append({"key": b.key, "error": "启动超时"})
            if verbose:
                warn(f"{b.name}: 启动超时")
            stop_backend(b)
            continue

        # 等隧道真正建立(轮询), 而不是探一次就判死
        h = wait_healthy(b, timeout=min(timeout, left), verbose=verbose)
        tried.append({"key": b.key, "health": h})
        if h["ok"]:
            if verbose:
                ok(f"{b.name} 可用 (通过 {h['passed']}/{h['total']}, 平均 {h['avg_ms']}ms)")
            return {"backend": b.key, "health": h, "tried": tried}
        if verbose:
            warn(f"{b.name} 不健康 ({h['passed']}/{h['total']}): {h['detail']}")
            if b.manual_connect:
                warn(f"  → {b.name} 需要你手动操作: 打开它的窗口, 点一下「连接」")
        stop_backend(b)
        time.sleep(2)

    restore_direct()
    if verbose:
        warn("所有后端都不可用, 已恢复直连 (国内站点不受影响)")
    return {"backend": None, "health": health(timeout=10.0), "tried": tried}


def status_all() -> list[dict[str, Any]]:
    """列出所有后端的状态."""
    out = []
    for b in sorted(BACKENDS, key=lambda x: x.priority):
        out.append(
            {
                "key": b.key,
                "name": b.name,
                "note": b.note,
                "installed": b.installed(),
                "running": b.running(),
                "path": str(b.exe()) if b.exe() else "",
                "priority": b.priority,
            }
        )
    return out


def current() -> str | None:
    """当前正在跑的后端(若多个, 返回优先级最高的那个 —— 那本身是异常状态)."""
    running = [b for b in BACKENDS if b.running()]
    if not running:
        return None
    running.sort(key=lambda b: b.priority)
    return running[0].key


def watch(*, interval: float = 60.0, rounds: int = 0, verbose: bool = True) -> None:
    """守护模式: 定期体检, 不健康就自动转移."""
    count = 0
    while True:
        count += 1
        cur = current()
        h = health(timeout=15.0) if cur else {"ok": False, "passed": 0, "total": 3}
        if verbose:
            info(f"[第 {count} 轮] 当前后端={cur or '无'} 健康={h['passed']}/{h['total']}")
        if not h["ok"]:
            if verbose:
                warn("当前链路不健康, 开始自动转移 ...")
            auto(verbose=verbose)
        if rounds and count >= rounds:
            return
        time.sleep(interval)
