# -*- coding: utf-8 -*-
"""验收用键盘输入: python keys.py down 4 enter"""
from __future__ import annotations

import ctypes
import sys
import time

u = ctypes.windll.user32
try:
    u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

KEYEVENTF_KEYUP = 0x0002
VK = {"down": 0x28, "up": 0x26, "enter": 0x0D, "return": 0x0D,
      "esc": 0x1B, "end": 0x23, "home": 0x24, "tab": 0x09}


def tap(vk: int) -> None:
    u.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    u.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.12)


def main() -> int:
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        name = args[i].lower()
        n = 1
        if i + 1 < len(args) and args[i + 1].isdigit():
            n = int(args[i + 1])
            i += 1
        vk = VK.get(name)
        if vk is None:
            print("unknown key", name)
            return 2
        for _ in range(n):
            tap(vk)
        print(f"pressed {name} x{n}")
        i += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
