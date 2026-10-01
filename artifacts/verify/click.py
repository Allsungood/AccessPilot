# -*- coding: utf-8 -*-
"""验收用真实鼠标点击器: 物理屏幕坐标 (与本进程 DPI 感知无关, SetCursorPos 就是物理坐标).

用法: python click.py <x> <y> [left|right|double]
"""
from __future__ import annotations

import ctypes
import sys
import time

u = ctypes.windll.user32
try:
    u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010


def move(x: int, y: int) -> None:
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.25)


def click(button: str = "left") -> None:
    down, up = ((MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP) if button == "left"
                else (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP))
    u.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.06)
    u.mouse_event(up, 0, 0, 0, 0)


def main() -> int:
    x, y = int(sys.argv[1]), int(sys.argv[2])
    kind = sys.argv[3] if len(sys.argv) > 3 else "left"
    move(x, y)
    if kind == "double":
        click("left")
        time.sleep(0.09)
        click("left")
    else:
        click(kind)
    time.sleep(0.35)
    print(f"clicked {kind} at ({x},{y}); cursor now = {u.GetCursorPos(ctypes.byref(ctypes.wintypes.POINT())) if False else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
