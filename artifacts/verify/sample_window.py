# -*- coding: utf-8 -*-
"""验收采样器: 启动一个窗口程序, 按固定间隔采样它的窗口状态 (原始序列).

用法:
    python sample_window.py --seconds 30 --interval 2 -- <cmd> [args...]
    python sample_window.py --seconds 30 --interval 2 --minimize-at 8 -- <cmd> ...

输出: JSON, 含每一帧的 (t, hwnd, title, visible, iconic, rect) + 进程存活情况。
只杀**自己 spawn 出来的 PID**(以及它的子进程) —— 绝不安名字/标题匹配杀进程。

DPI: 本进程声明 per-monitor-v2, 拿到的 rect / 屏幕尺寸都是**物理像素**。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32


def set_dpi_aware() -> str:
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "shcore=2"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return "system-aware"
    except Exception:
        return "none"


def proc_tree(root: int) -> set[int]:
    """CreateToolhelp32Snapshot 走一遍进程树, 拿到 root 及其全部后代."""
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = ctypes.c_void_p(-1).value

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID:
        return {root}
    parent: dict[int, int] = {}
    try:
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            parent[int(e.th32ProcessID)] = int(e.th32ParentProcessID)
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    out = {root}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parent.items():
            if ppid in out and pid not in out:
                out.add(pid)
                changed = True
    return out


WNDENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def windows_of(pids: set[int]) -> list[dict]:
    found: list[dict] = []

    def cb(hwnd, _lp):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        if not user32.IsWindowVisible(hwnd):
            # 不可见的也记一条(托盘隐藏窗口), 但标记出来
            vis = False
        else:
            vis = True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 2)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.UINT),
                ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT),
            ]

        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
        found.append({
            "hwnd": int(hwnd),
            "title": buf.value,
            "class": cls.value,
            "visible": vis,
            "iconic": bool(user32.IsIconic(hwnd)),
            "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top],
            # showCmd: 1=SW_NORMAL 2=SW_MINIMIZE 3=SW_MAXIMIZE
            "showCmd": int(wp.showCmd),
            "normal_rect": [wp.rcNormalPosition.left, wp.rcNormalPosition.top,
                            wp.rcNormalPosition.right - wp.rcNormalPosition.left,
                            wp.rcNormalPosition.bottom - wp.rcNormalPosition.top],
        })
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def alive(pid: int) -> bool:
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--minimize-at", type=float, default=0.0)
    ap.add_argument("--label", default="")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("need a command", file=sys.stderr)
        return 2

    aware = set_dpi_aware()
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)

    p = subprocess.Popen(
        cmd,
        stdout=open(
            r"C:\Users\Administrator\AccessPilot\artifacts\verify\_child_"
            + (a.label or "run") + ".log", "w",
            encoding="utf-8", errors="replace"),
        stderr=subprocess.STDOUT,
        creationflags=0x00000200,  # CREATE_NEW_PROCESS_GROUP
    )
    report: dict = {
        "label": a.label,
        "dpi_awareness": aware,
        "screen": [sw, sh],
        "cmd": cmd,
        "spawn_pid": p.pid,
        "frames": [],
    }
    t0 = time.time()
    minimized = False
    while time.time() - t0 < a.seconds:
        time.sleep(a.interval)
        t = round(time.time() - t0, 2)
        tree = proc_tree(p.pid)
        wins = windows_of(tree)
        # 只关心有标题的顶层窗口(Tk 的 TkTopLevel); 记录全部, 便于排查
        report["frames"].append({
            "t": t,
            "child_alive": alive(p.pid),
            "pids": sorted(tree),
            "windows": wins,
        })
        if a.minimize_at and not minimized and t >= a.minimize_at:
            for w in wins:
                if w["title"].startswith("红杏"):
                    user32.ShowWindow(w["hwnd"], 6)  # SW_MINIMIZE
                    report["minimized_hwnd"] = w["hwnd"]
                    report["minimized_at"] = t
                    minimized = True
                    break
    # 收尾: 只杀自己 spawn 的进程树
    report["kill"] = {"pid": p.pid}
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                   capture_output=True, text=True)
    report["exit_code_after_kill"] = p.wait(timeout=20)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
