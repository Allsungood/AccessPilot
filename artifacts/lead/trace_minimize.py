"""窗口自己最小化的根因排查: 把「输入事件」和「窗口状态翻转」放在同一条时间线上。

## 为什么要写这个

已有的两条事实对不上:

1. 守护在窗口自己最小化的那一刻读到 `idle = 0~15 ms` —— 意思是**刚刚有键鼠输入**;
2. 但我单独测这台机器 25 秒的 idle 分布是 2484~27406ms、**0/248 次**低于 1500ms、
   零输入事件, 而且 `app.py` / 采样脚本里都没有 `keybd_event`/`SendInput`/`mouse_event`。

"没人操作"和"刚刚有输入"不可能同时成立, 所以必有一条测得不对。

这个脚本同时高频采两样东西, 用**同一条时间轴**对齐:

* `GetLastInputInfo` 的 idle —— 输入事件在哪个时刻发生, 看 idle **回落**就知道;
* `IsIconic` + rect —— 窗口在哪个时刻变得不正常;
* 是否存在 `MenuWindowClass` / `EmbeddedMenuWindowClass` —— 之前那次翻转到
  t=24.33s 时, 同一帧冒出了这两个类, 那是"弹了菜单"的特征, 值得盯住。

翻转发那一刻把前后几帧的 idle 一起打出来 —— 单看一个瞬时值分不清
"输入导致了最小化"还是"最小化恰好和输入撞在一起"。

## 用法

    python artifacts/lead/trace_minimize.py --seconds 150 -- <exe 路径>
"""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes

u = ctypes.windll.user32
k = ctypes.windll.kernel32
k.GetTickCount64.restype = ctypes.c_ulonglong
k.GetTickCount64.argtypes = []


class LII(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_ms() -> int | None:
    li = LII()
    li.cbSize = ctypes.sizeof(LII)
    if not u.GetLastInputInfo(ctypes.byref(li)):
        return None
    return (int(k.GetTickCount64()) - int(li.dwTime)) & 0xFFFFFFFF


def windows():
    """当前所有可见/有尺寸的顶层窗口 -> [(class, title, iconic, rect)]"""
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _l):
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(h, cls, 256)
        r = wintypes.RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        vis = bool(u.IsWindowVisible(h))
        area = (r.right - r.left) * (r.bottom - r.top)
        if vis or area > 0:
            out.append({
                "cls": cls.value,
                "iconic": bool(u.IsIconic(h)),
                "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top],
            })
        return True

    u.EnumWindows(cb, 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=150.0)
    ap.add_argument("--interval", type=float, default=0.1)
    ap.add_argument("--label", default="trace")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = [c for c in a.cmd if c != "--"]
    if not cmd:
        print("需要给一个要启动的命令")
        return 2

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    samples = []
    try:
        while time.time() - t0 < a.seconds:
            t = round(time.time() - t0, 3)
            ws = windows()
            main = None
            for w in ws:
                if w["cls"] == "TkTopLevel":
                    main = w
                    break
            samples.append({
                "t": t,
                "idle": idle_ms(),
                "main": main,
                "menus": sorted({w["cls"] for w in ws
                                 if "Menu" in w["cls"] or "Thumbnail" in w["cls"]}),
            })
            time.sleep(a.interval)
    finally:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        except Exception:
            pass

    # 找翻转点
    flips = []
    prev = None
    for i, s in enumerate(samples):
        m = s["main"]
        cur = None if m is None else (m["iconic"], tuple(m["rect"]))
        if cur != prev and cur is not None:
            flips.append(i)
            prev = cur

    print(f"采样 {len(samples)} 帧 / {a.seconds:.0f} 秒")
    bad = sum(1 for s in samples if s["main"] and s["main"]["iconic"])
    print(f"主窗口被最小化的帧: {bad} / {len(samples)}")
    print()
    print("=== 状态翻转点 (带前后各 3 帧的 idle, 单位 ms) ===")
    for i in flips:
        m = samples[i]["main"]
        near = [samples[j]["idle"] for j in range(max(0, i - 3), min(len(samples), i + 4))]
        menus = samples[i]["menus"]
        print(f"  t={samples[i]['t']:>7.2f}  iconic={m['iconic']!s:<5} rect={m['rect']}  "
              f"idle={samples[i]['idle']}")
        print(f"          前后 idle: {near}")
        if menus:
            print(f"          同帧菜单类: {menus}")
    print()
    print("=== 全程出现过的菜单/缩略图类 ===")
    seen = {}
    for s in samples:
        for c in s["menus"]:
            seen.setdefault(c, []).append(s["t"])
    for c, ts in seen.items():
        print(f"  {c}: {len(ts)} 帧, 首次 t={ts[0]:.2f}, 末次 t={ts[-1]:.2f}")
    if not seen:
        print("  (一个都没有)")

    with open(f"artifacts/lead/{a.label}.json", "w", encoding="utf-8") as fh:
        json.dump(samples, fh, ensure_ascii=False)
    print(f"\n原始帧 -> artifacts/lead/{a.label}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
