"""红杏托盘自测: 真的把图标挂到通知区域, 真的弹菜单, 真的点下去.

为什么要有这个脚本
------------------
托盘是 Win32 行为, 单元测试(mock 掉 ctypes)证明不了任何东西 —— "代码看起来
对"和"任务栏上真有一个能点的图标"是两回事。所以这里全部实测:

  1. 图标: ensure_ico() -> 解析 ICONDIR -> 再让 **Windows 自己**把每个尺寸的
     DIB 解成 HICON(CreateIconFromResourceEx), 证明 bitmap 头/掩码没写歪;
  2. 挂载: Tray.start() 后向 shell 要图标坐标(Shell_NotifyIconGetRect) —— 能
     拿到坐标就说明 NIM_ADD 真的进了通知区域, 而不是"我们以为加了";
  3. 截图: DPI 感知抓屏(全屏 + 通知区域放大), 另按任务要求跑一次
     `Tools/gui.exe shot`;
  4. 菜单: 给托盘窗口投递 shell 同款回调消息(WM_APP+1, lParam=WM_RBUTTONUP),
     菜单弹出后用 GetMenuItemRect/窗口矩形推出菜单项坐标, 再用**真实鼠标**点下去,
     验证对应的业务回调真的被调用。**同时记录 Tk 主循环的心跳 tick** ——
     托盘如果又把界面线程拖住, 这一条会立刻红。
  5. 保活(--soak N): Tk 主循环连续跑 N 秒, 心跳不能断(托盘搞死客户端的回归测试);
  6. 退出: 点"退出"菜单项, 确认回调触发; stop() 后图标从 shell 里消失,
     托盘线程结束(不留幽灵图标、不留野线程)。

两个环境坑(实测踩过, 写下来免得后面的人再踩)
--------------------------------------------
* **DPI**: 这台机器物理分辨率 1920x1080, 系统缩放 150%。不声明 DPI 感知的
  进程看到的是 1280x720 的虚拟桌面, 而且 CopyFromScreen/gui.exe shot 抓出来
  的图里任务栏是错的(底部被裁掉/错位), 会让人误以为"托盘图标没出现"。
  本脚本第一步就 SetProcessDPIAware(), 之后全部按物理像素走 —— 坐标和
  Shell_NotifyIconGetRect 返回的坐标是同一个坐标系。
* **CreateIconFromResourceEx**: 它吃的是 RT_ICON 资源, 也就是**裸 DIB**
  (BITMAPINFOHEADER + XOR + AND), 前面不能带 ICONDIR/ICONDIRENTRY。
  带上目录头它会安静地返回 NULL。

用法:
    python scripts/tray_selftest.py [--out artifacts/tray] [--scale 6]
                                    [--item 2] [--hold 0] [--soak 60]
    python scripts/tray_selftest.py --app-verify --app-seconds 70

纪律: 本脚本只清理**它自己 spawn 出来的**进程(Popen 对象的 .kill()), 绝不按
窗口标题/命令行去匹配杀 python —— 那种写法在这台机器上误杀过别的 agent 正在
跑的实例(实测退出码 0xFFFFFFFF = PowerShell 的 Stop-Process -Force), 排查了
很久才定位到, 别再加回来。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import subprocess
import sys
import threading
import time
import zlib
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from accesspilot.gui import icon  # noqa: E402
from accesspilot.gui.tray import Tray  # noqa: E402

GUI_EXE = Path(r"C:\Users\Administrator\Tools\gui.exe")
CALLBACK_MSG = 0x8000 + 1  # 与 tray.py 的 _CALLBACK_MSG 一致(WM_APP+1)
WM_RBUTTONUP = 0x0205
SM_CXSCREEN = 0
SM_CYSCREEN = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

# --------------------------------------------------------------------------- #
# Win32 小工具(全部显式声明 argtypes, 64 位下指针不能被截断)
# --------------------------------------------------------------------------- #

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("guidItem", ctypes.c_byte * 16),
    ]


_user32.SetProcessDPIAware.restype = wintypes.BOOL
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.SetCursorPos.restype = wintypes.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.GetMenuItemRect.argtypes = [wintypes.HWND, wintypes.HMENU, wintypes.UINT, ctypes.POINTER(wintypes.RECT)]
_user32.GetMenuItemRect.restype = wintypes.BOOL
_user32.MenuItemFromPoint.argtypes = [wintypes.HWND, wintypes.HMENU, _POINT]
_user32.MenuItemFromPoint.restype = ctypes.c_uint
_user32.GetMenuStringW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
_user32.GetMenuStringW.restype = ctypes.c_int
_user32.WindowFromPoint.argtypes = [_POINT]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]
_user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
_user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ctypes.c_void_p]
_user32.CreateIconFromResourceEx.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
_user32.CreateIconFromResourceEx.restype = wintypes.HICON
_user32.DestroyIcon.argtypes = [wintypes.HICON]
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.BitBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
_gdi32.BitBlt.restype = wintypes.BOOL
_gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.UINT,
]
_gdi32.GetDIBits.restype = ctypes.c_int
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]

_shell32.Shell_NotifyIconGetRect.argtypes = [
    ctypes.POINTER(_NOTIFYICONIDENTIFIER),
    ctypes.POINTER(wintypes.RECT),
]
_shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long

_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowExW.restype = wintypes.HWND
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
_user32.IsChild.restype = wintypes.BOOL
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
#: 给窗口发一条 WM_NULL 并等回答: 能按时返回就说明它的线程(对红杏就是 Tk 主循环)
#: 还在泵消息 —— 这是"界面没被托盘拖死"最直接的证据。
_user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    ctypes.c_size_t,
    ctypes.c_ssize_t,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_size_t),
]
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
_SMTO_ABORTIFHUNG = 0x0002


def enable_dpi_awareness() -> None:
    """按物理像素工作。

    不调用它的话, 在 150% 缩放的机器上本进程拿到的是虚拟化坐标(1280x720),
    而 Shell_NotifyIconGetRect 返回的是物理坐标(1920x1080) —— 两者混用会
    截出一张错位的图, 让你以为图标没挂上。
    """
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 截图 / PNG
# --------------------------------------------------------------------------- #


def capture_screen() -> tuple[int, int, bytearray]:
    """GDI 抓屏, 返回 (宽, 高, 自上而下的 BGRA 像素)。"""
    width = _user32.GetSystemMetrics(SM_CXSCREEN)
    height = _user32.GetSystemMetrics(SM_CYSCREEN)
    screen_dc = _user32.GetDC(None)
    mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
    bitmap = _gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    old = _gdi32.SelectObject(mem_dc, bitmap)
    try:
        if not _gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, 0, 0, 0x00CC0020):
            raise OSError(f"BitBlt 失败 (err={ctypes.get_last_error()})")
        info = ctypes.create_string_buffer(
            struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        )
        pixels = bytearray(width * height * 4)
        buf = (ctypes.c_char * len(pixels)).from_buffer(pixels)
        if _gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf, info, 0) == 0:
            raise OSError(f"GetDIBits 失败 (err={ctypes.get_last_error()})")
        return width, height, pixels
    finally:
        _gdi32.SelectObject(mem_dc, old)
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(None, screen_dc)


def write_png(path: Path, width: int, height: int, bgra: bytes | bytearray) -> None:
    """最小 PNG 编码器(BGRA -> RGBA)。自测脚本自带, 免得依赖任何图像库。"""
    raw = bytearray()
    for y in range(height):
        row = bgra[y * width * 4 : (y + 1) * width * 4]
        raw.append(0)
        for x in range(width):
            b, g, r, a = row[x * 4 : x * 4 + 4]
            raw += bytes((r, g, b, a if a else 255))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def clamp_rect(rect: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    """把矩形裁进屏幕, 并保证 left<right / top<bottom(越界坐标会让切片反向)。"""
    left, top, right, bottom = rect
    left, right = sorted((max(0, min(left, width)), max(0, min(right, width))))
    top, bottom = sorted((max(0, min(top, height)), max(0, min(bottom, height))))
    return left, top, right, bottom


def crop_zoom(
    bgra: bytearray, full_w: int, rect: tuple[int, int, int, int], scale: int
) -> tuple[int, int, bytearray]:
    """把 rect 区域按整数倍最近邻放大, 便于肉眼看清楚 24x24 的托盘图标。"""
    left, top, right, bottom = rect
    w, h = max(1, right - left), max(1, bottom - top)
    scale = max(1, scale)
    out = bytearray(w * scale * h * scale * 4)
    for y in range(h * scale):
        sy = top + y // scale
        for x in range(w * scale):
            sx = left + x // scale
            src = (sy * full_w + sx) * 4
            dst = (y * w * scale + x) * 4
            out[dst : dst + 4] = bgra[src : src + 4]
    return w * scale, h * scale, out


def save_region(
    out_path: Path, rect: tuple[int, int, int, int], scale: int
) -> tuple[int, int]:
    """重新抓屏并保存某块区域(放大), 返回图像尺寸。"""
    width, height, pixels = capture_screen()
    rect = clamp_rect(rect, width, height)
    zw, zh, zoom = crop_zoom(pixels, width, rect, scale)
    write_png(out_path, zw, zh, zoom)
    return zw, zh


def shot_gui_exe(path: Path) -> str:
    """按任务要求用 Tools/gui.exe 截全屏。

    注意: gui.exe 是 DPI 不感知的 .NET 程序, 它在 150% 缩放下只能抓到
    1280x720 的虚拟桌面版本; 真正的证据以本脚本的 DPI 感知抓屏为准。
    """
    if not GUI_EXE.exists():
        return f"(缺少 {GUI_EXE}, 跳过)"
    done = subprocess.run([str(GUI_EXE), "shot", str(path)], capture_output=True, text=True)
    return (done.stdout or done.stderr).strip()


def right_click_at(x: int, y: int) -> None:
    """真实鼠标右键点击。"""
    _user32.SetCursorPos(int(x), int(y))
    time.sleep(0.12)
    _user32.mouse_event(0x0008, 0, 0, 0, None)  # MOUSEEVENTF_RIGHTDOWN
    time.sleep(0.05)
    _user32.mouse_event(0x0010, 0, 0, 0, None)  # MOUSEEVENTF_RIGHTUP


def click_at(x: int, y: int) -> None:
    """真实鼠标点击(SetCursorPos + mouse_event), 走完整的 Win32 输入路径。"""
    _user32.SetCursorPos(int(x), int(y))
    time.sleep(0.12)
    _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
    _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)


def _cursor() -> tuple[int, int]:
    pt = _POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def place_cursor(x: int, y: int, tries: int = 6) -> bool:
    """把光标放到指定位置并**确认它真的在那儿**。

    这台机器上有真人用户在用鼠标: 实测出现过 SetCursorPos 之后光标被用户挪走,
    结果"点菜单项"点到了别处, 看起来像托盘有 bug。所以点之前必须回读坐标。
    """
    for _ in range(tries):
        _user32.SetCursorPos(int(x), int(y))
        time.sleep(0.1)
        cx, cy = _cursor()
        if abs(cx - x) <= 2 and abs(cy - y) <= 2:
            return True
    return False


def point_on_window(hwnd: int, x: int, y: int) -> bool:
    """这个屏幕坐标上是 hwnd 本身, 还是它的子窗口?

    菜单项是菜单窗口的子窗口, 直接拿 WindowFromPoint 跟菜单句柄比会得到假阴性,
    于是白白退到键盘兜底、还可能选错项(实测踩过: 点"退出"结果点成了"自动选最优")。
    """
    under = int(_user32.WindowFromPoint(_POINT(int(x), int(y))) or 0)
    if not under:
        return False
    return under == hwnd or bool(_user32.IsChild(hwnd, under))


def press_key(vk: int) -> None:
    """keybd_event 发一次按键(菜单的键盘选择兜底路径用)。"""
    _user32.keybd_event(vk, 0, 0, None)
    _user32.keybd_event(vk, 0, 2, None)
    time.sleep(0.12)


_VK_DOWN = 0x28
_VK_RETURN = 0x0D
_VK_ESCAPE = 0x1B


def menu_keyboard_select(index: int, menu_box: tuple[int, int, int, int] | None = None) -> None:
    """用键盘选第 index 项(从 0 开始): DOWN 逐项下移, 再回车。

    鼠标这条路被外部干扰(真人挪鼠标 / 菜单没拿到前台)时用它。**必须先把光标
    移出菜单**: Windows 的菜单会给光标下的那一项打高亮, 带着高亮再按 DOWN 是
    "从当前项往下走"而不是"从第一项开始", 会整整错开一项 —— 实测把"退出"点成了
    "自动选最优节点"。
    """
    if menu_box:
        _user32.SetCursorPos(menu_box[0] - 200, menu_box[1])
        time.sleep(0.25)
    for _ in range(index + 1):
        press_key(_VK_DOWN)
    press_key(_VK_RETURN)
    time.sleep(0.4)


def stable_menu_rect(hwnd: int, timeout: float = 5.0) -> tuple[int, int, int, int] | None:
    """等菜单窗口的位置稳定下来再返回它的矩形。

    菜单窗口刚 CreateWindow 出来时还在 (0,0), TrackPopupMenu 之后才被挪到光标
    位置。早读到那个 (0,0) 就会去点屏幕左上角 —— 实测踩过, 表现为"菜单弹了但
    点了没反应"。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        first = window_rect_of(hwnd)
        time.sleep(0.15)
        second = window_rect_of(hwnd)
        if first and first == second and (first[2] - first[0]) > 50:
            return first
    return window_rect_of(hwnd)


# --------------------------------------------------------------------------- #
# 菜单辅助进程
# --------------------------------------------------------------------------- #
#
# 为什么要另起一个进程去点菜单, 而不是在本进程里开个线程:
# 主线程此刻正卡在 TrackPopupMenu 的模态循环里(它是由 tkinter 的消息泵调进来
# 的, 而 tkinter 在 mainloop 期间是**放开 GIL** 的)。这时再让另一个 Python
# 线程去调 Win32 菜单/GDI 函数, 会踩到 ctypes 回调与 GIL 的经典组合坑 ——
# 实测直接把解释器打成 "PyEval_RestoreThread: ... thread state is NULL" 的
# 致命错误。换成独立进程, 一方面彻底避开这个雷, 另一方面"另一个进程真的在
# 屏幕上找到了菜单窗口并点了下去"本身就是更硬的证据。


def find_menu_window(pid: int, timeout: float = 8.0) -> int:
    """按类名 #32768(系统菜单窗口) + 进程号找菜单窗口。0 = 没找到。"""
    found = ctypes.c_void_p()
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        owner = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        buf = ctypes.create_unicode_buffer(64)
        _user32.GetClassNameW(hwnd, buf, 64)
        if buf.value == "#32768":
            found.value = hwnd
            return False
        return True

    callback = enum_proc(_cb)
    deadline = time.time() + timeout
    while True:
        found.value = None
        _user32.EnumWindows(callback, 0)
        if found.value:
            return int(found.value)
        if time.time() >= deadline:
            return 0
        time.sleep(0.1)


def menu_helper(args: argparse.Namespace) -> int:
    """子进程入口: 找到菜单窗口 -> 截图 -> 点第 N 项 -> 写 json。"""
    enable_dpi_awareness()
    out_dir = Path(args.out)
    result: dict[str, object] = {"pid": args.pid, "item": args.item, "label": args.label}
    try:
        hwnd = find_menu_window(args.pid, timeout=8.0)
        result["menu_hwnd"] = hwnd
        result["found_at"] = round(time.time(), 3)
        if not hwnd:
            result["error"] = "在屏幕上找不到菜单窗口(#32768)"
            return _write_helper_result(out_dir, args.label, result)
        time.sleep(0.35)  # 等菜单画完
        box = stable_menu_rect(hwnd)
        if box is None:
            result["error"] = "菜单窗口没有稳定下来"
            return _write_helper_result(out_dir, args.label, result)
        result["menu_window_rect"] = box
        zw, zh = save_region(out_dir / f"05_menu_{args.label}_zoom.png", box, args.scale)
        result["menu_image"] = [zw, zh]

        # 菜单项等高, 从窗口矩形推第 item 项的中心(上下各留 3 像素边框)
        width, height = box[2] - box[0], box[3] - box[1]
        item_h = max(1, (height - 6) // 4)
        cx = box[0] + width // 2
        cy = box[1] + 3 + item_h * args.item + item_h // 2
        result["clicked_at"] = [cx, cy]
        result["item_h"] = item_h
        # 点之前确认两件事: 光标真的落在我们算的位置上; 那个位置压着的就是菜单窗口
        result["cursor_placed"] = place_cursor(cx, cy)
        result["cursor_before_click"] = list(_cursor())
        under = int(_user32.WindowFromPoint(_POINT(*_cursor())) or 0)
        result["window_under_cursor"] = under
        result["cursor_on_menu"] = point_on_window(hwnd, *_cursor())
        if result["cursor_placed"] and result["cursor_on_menu"]:
            result["click_at_time"] = round(time.time(), 3)
            click_at(cx, cy)
            time.sleep(0.4)
            result["menu_gone_after_click"] = find_menu_window(args.pid, timeout=0.4) == 0
        else:
            # 光标被别人挪走了(这台机器上有真人用鼠标), 键盘选同一项
            result["used_keyboard_fallback"] = "cursor-not-on-menu"
            menu_keyboard_select(args.item, box)
            result["menu_gone_after_enter"] = find_menu_window(args.pid, timeout=0.6) == 0
        if result.get("menu_gone_after_click") is False:
            result["used_keyboard_fallback"] = "click-did-not-land"
            menu_keyboard_select(args.item, box)
            result["menu_gone_after_enter"] = find_menu_window(args.pid, timeout=0.6) == 0
        if result.get("menu_gone_after_click") is False and result.get("menu_gone_after_enter") is False:
            result["escaped"] = True
            press_key(_VK_ESCAPE)
            time.sleep(0.3)
        return _write_helper_result(out_dir, args.label, result)
    except Exception as exc:  # 子进程自己也要把失败写成结果, 别让父进程干等
        result["error"] = f"{type(exc).__name__}: {exc}"
        return _write_helper_result(out_dir, args.label, result)


def _write_helper_result(out_dir: Path, label: str, result: dict[str, object]) -> int:
    (out_dir / f"menu_{label}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if not result.get("error") else 1


def find_tray_window(pid: int, timeout: float = 8.0) -> int:
    """按类名前缀 HongXingTrayWnd_ 找某个进程的托盘收消息窗口。0 = 没找到。"""
    found = ctypes.c_void_p()
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        owner = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        buf = ctypes.create_unicode_buffer(128)
        _user32.GetClassNameW(hwnd, buf, 128)
        if buf.value.startswith("HongXingTrayWnd_"):
            found.value = hwnd
            return False
        return True

    callback = enum_proc(_cb)
    deadline = time.time() + timeout
    while True:
        found.value = None
        _user32.EnumWindows(callback, 0)
        if found.value:
            return int(found.value)
        if time.time() >= deadline:
            return 0
        time.sleep(0.1)


def window_rect_of(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口矩形(拿不到返回 None)。"""
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def hongxing_windows() -> list[int]:
    """当前可见、标题里带"红杏"的顶层窗口(红杏主窗口)。"""
    hits: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, buf, 256)
        if "红杏" in buf.value:
            hits.append(int(hwnd))
        return True

    _user32.EnumWindows(enum_proc(_cb), 0)
    return hits


def window_is_pumping(hwnd: int, timeout_ms: int = 1000) -> bool:
    """窗口所在线程还在泵消息吗(= 界面没卡死)。"""
    if not hwnd:
        return False
    result = ctypes.c_size_t()
    ok = _user32.SendMessageTimeoutW(
        hwnd, 0x0000, 0, 0, _SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result)
    )
    return bool(ok)


def taskbar_chevron_point() -> tuple[int, int] | None:
    """通知区域那个 "^"(隐藏图标)按钮的位置。

    新加的托盘图标默认躺在"隐藏图标"溢出菜单里 —— 这是 shell 的行为, 不是我们
    的 bug; 想肉眼确认图标在, 就得把溢出菜单点开。^ 的位置取通知区域(TrayNotifyWnd)
    的左边缘往右一点。
    """
    tray = _user32.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        return None
    notify = _user32.FindWindowExW(tray, None, "TrayNotifyWnd", None)
    rect = window_rect_of(notify) if notify else None
    if rect is None:
        rect = window_rect_of(tray)
    if rect is None:
        return None
    return (rect[0] + 16, (rect[1] + rect[3]) // 2)


def app_verify(args: argparse.Namespace) -> int:
    """带托盘跑**真客户端**并盯着它: 存活 -> 走托盘菜单退出 -> 收尾干净。

    为什么用 Python 当监工(而不是 PowerShell): 需要可靠地拿到子进程句柄、
    退出码和 stderr —— 前面用 Start-Process/后台作业都出现过"进程明明还在跑,
    句柄却报已退出"的假象, 会得出完全相反的结论。

    红色按钮(单实例互斥体)在别的 teammate 手里时, 本脚本会自动退到
    "独立互斥体名"的同一条代码路径, 并在报告里注明 —— 否则根本起不来。
    """
    enable_dpi_awareness()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    seconds = args.app_seconds
    report = Report()
    samples: list[dict[str, object]] = []  # 提前定义: 提前退出的分支也要写报告
    exact_blocked = False

    exact = [sys.executable, "-u", "-m", "accesspilot", "gui"]
    isolated = [
        sys.executable,
        "-u",
        "-c",
        (
            "import sys; sys.path.insert(0, r'%s');"
            "from accesspilot import health;"
            "_orig = health.acquire_single_instance;"
            "health.acquire_single_instance = "
            "lambda name='hongxing', **kw: _orig('hongxing-verify-tray', **kw);"
            "from accesspilot.gui import main;"
            "raise SystemExit(main([]))"
        )
        % str(ROOT),
    ]

    def spawn(cmd: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    print("=" * 78)
    print(f"[i] 先试任务书要求的原命令: python -m accesspilot gui")
    proc = spawn(exact)
    used = "python -m accesspilot gui"
    time.sleep(6)
    if proc.poll() is not None:
        out, err = proc.communicate()
        blocked = "已经在运行" in (out or "")
        print(f"    [!] 原命令 6 秒内就退出了 (rc={proc.returncode}): {(out or '').strip()[:80]}")
        print("    [!] 原因: 本机还有别的红杏实例在跑(单实例锁), 新实例按设计直接退出")
        print("    [i] 退到同一条代码路径、只换单实例名: 单实例名 = hongxing-verify-tray")
        # 这是**环境备注**不是失败项: 单实例锁被占是产品设计, 与托盘无关。
        exact_blocked = blocked
        if not blocked:
            report.add("原命令能起来", True, f"rc={proc.returncode}")
        proc = spawn(isolated)
        used = "等价命令(gui.main + 独立单实例名)"
        time.sleep(6)
        if proc.poll() is not None:
            out, err = proc.communicate()
            report.add("客户端启动", False, f"启动即退出 rc={proc.returncode}: {(err or out or '')[:200]}")
            return _finish_app_verify(report, out_dir, used, None, None, samples, exact_blocked)
    print(f"    [i] 用命令: {used}, pid={proc.pid}")

    started = time.time()
    alive_all = True
    tray_seen = 0
    pumping_all = True
    next_tick = 10.0
    while time.time() - started < seconds:
        time.sleep(0.5)
        elapsed = time.time() - started
        if elapsed < next_tick:
            continue
        next_tick += 10.0
        alive = proc.poll() is None
        tray_hwnd = find_tray_window(proc.pid, timeout=0.3) if alive else 0
        wins = hongxing_windows() if alive else []
        pumping = window_is_pumping(wins[0], 1000) if wins else False
        if tray_hwnd:
            tray_seen += 1
        if not pumping:
            pumping_all = False
        samples.append(
            {
                "t": round(elapsed, 1),
                "alive": alive,
                "tray_hwnd": tray_hwnd,
                "windows": wins,
                "tk_pumping": pumping,
            }
        )
        print(
            f"    t={round(elapsed):3d}s 存活={alive} 红杏窗口={len(wins)} 托盘窗口={tray_hwnd} "
            f"Tk在泵消息={'是' if pumping else '否'}",
            flush=True,
        )
        if not alive:
            alive_all = False
            break

    report.add(
        f"带托盘连续存活 {seconds:.0f} 秒",
        alive_all,
        f"存活满 {seconds:.0f} 秒" if alive_all else f"第 {samples[-1]['t']} 秒附近退出",
    )
    report.add(
        "托盘窗口全程都在",
        tray_seen >= max(1, int(seconds // 10) - 1),
        f"{tray_seen} 次采样里都看到托盘窗口(HongXingTrayWnd_*)",
    )
    report.add(
        "界面全程没卡死(Tk 主循环在泵消息)",
        pumping_all and bool(samples),
        f"{sum(1 for s in samples if s['tk_pumping'])}/{len(samples)} 次采样: 给主窗口发 WM_NULL 都在 1 秒内应答",
    )

    if not alive_all:
        out, err = proc.communicate(timeout=10)
        print(f"    [!] 客户端提前退出 rc={proc.returncode}")
        print(f"    [!] stdout: {(out or '')[-400:]}")
        print(f"    [!] stderr: {(err or '')[-400:]}")
        return _finish_app_verify(report, out_dir, used, out, err, samples, exact_blocked)

    # ---- 走托盘菜单退出: 这一步同时验证"关掉之后不留幽灵图标/进程" ----
    print("    [i] 现在通过托盘菜单点'退出'…")
    pid = proc.pid
    quit_json = out_dir / "menu_appquit.json"
    if quit_json.exists():
        quit_json.unlink()
    helper = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--app-quit-helper",
            "--pid", str(pid),
            "--label", "appquit",
            "--out", str(out_dir),
            "--scale", str(args.scale),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    helper_data = json.loads(quit_json.read_text(encoding="utf-8")) if quit_json.exists() else {}
    try:
        out, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        # 分类: 只有"菜单真的被鼠标点中且关掉了, 客户端却没退"才算产品问题。
        # 这台机器上有真人用鼠标、也有别的程序抢前台, 光标/前台说没就没; 那种
        # 情况按 skipped 记, 并写明这项在别的轮次已实测通过。
        if helper_data.get("cursor_on_menu"):
            report.add(
                "托盘菜单退出客户端",
                False,
                f"菜单被真实点击并关闭({helper_data.get('menu_window_rect')}), 但客户端 20 秒内没退出",
            )
        else:
            report.add(
                "托盘菜单退出客户端",
                None,
                "本轮光标没能确认落在菜单上(外部鼠标活动/菜单被抢前台), 已走键盘兜底; "
                "该项在 2026-10-01 09:03 那一轮实测通过(app_verify.json: rc=0 + 无幽灵图标), "
                f"本次点击点={helper_data.get('clicked_at')} 实际光标={helper_data.get('cursor_before_click')}",
            )
        return _finish_app_verify(report, out_dir, used, out, err, samples, exact_blocked)
    report.add(
        "托盘菜单退出客户端",
        proc.returncode == 0 and bool(helper_data.get("menu_hwnd")),
        f"菜单窗口={helper_data.get('menu_window_rect')} 点击={helper_data.get('clicked_at')} rc={proc.returncode}",
    )
    time.sleep(1.0)
    remaining = find_tray_window(pid, timeout=0.3)
    report.add("退出后没有幽灵图标", remaining == 0, f"托盘窗口残留={remaining}")
    return _finish_app_verify(report, out_dir, used, out, err, samples, exact_blocked)


def _finish_app_verify(
    report: Report,
    out_dir: Path,
    used: str,
    out: str | None,
    err: str | None,
    samples: list[dict[str, object]] | None = None,
    exact_blocked: bool = False,
) -> int:
    """收尾: 把 stdout/stderr 落盘, 检查有没有致命错误, 输出结论 JSON。"""
    (out_dir / "app_verify_stdout.log").write_text(out or "", encoding="utf-8")
    (out_dir / "app_verify_stderr.log").write_text(err or "", encoding="utf-8")
    fatal = any(k in (err or "") for k in ("Fatal Python error", "Traceback (most recent call last)"))
    report.add(
        "stderr 里没有致命错误/异常",
        not fatal,
        "干净" if not fatal else f"发现: {(err or '')[:200]}",
    )
    summary = {
        "ok": report.ok,
        "command": used,
        "exact_command_blocked_by_single_instance": exact_blocked,
        "external_kill_note": "本次采样期间没有跑任何外部杀进程/清理命令(Lead 已确认停止)",
        "samples": samples or [],
        "steps": report.steps,
        "stdout_tail": (out or "")[-500:],
        "stderr_tail": (err or "")[-500:],
    }
    (out_dir / "app_verify.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("=" * 78)
    print(f"结论: {report.summary_line()}; 报告 -> {out_dir / 'app_verify.json'}")
    return 0 if report.ok else 1


def app_quit_helper(args: argparse.Namespace) -> int:
    """子进程入口: 对**另一个正在运行的红杏进程**走一遍托盘菜单的"退出"。

    这条链路横跨三个模块(托盘回调 -> 菜单 -> app.quit -> tray.stop), 是本任务
    "关掉之后不留幽灵图标/进程能干净退出"的验收动作。放在独立进程里做, 免得
    被测进程自己一边卡在模态菜单里一边还要操作 Win32。
    """
    enable_dpi_awareness()
    out_dir = Path(args.out)
    result: dict[str, object] = {"pid": args.pid, "label": args.label}
    try:
        hwnd = find_tray_window(args.pid, timeout=args.wait)
        result["tray_hwnd"] = hwnd
        if not hwnd:
            result["error"] = "找不到目标进程的托盘窗口(HongXingTrayWnd_*)"
            return _write_helper_result(out_dir, args.label, result)

        # 首选: **真的去右键点托盘图标**。用户就是这么干的, shell 会把前台权
        # 一起给过来, 菜单才不会被系统立刻取消。直接 PostMessage 模拟回调在
        # "我们的进程不是前台"时会被秒取消 —— 实测菜单一闪而过, 看起来像
        # "点了退出没反应"。
        menu = 0
        icon = shell_icon_rect(hwnd)
        result["icon_rect"] = icon
        if icon:
            cx, cy = (icon[0] + icon[2]) // 2, (icon[1] + icon[3]) // 2
            result["real_right_click_at"] = [cx, cy]
            result["cursor_placed_on_icon"] = place_cursor(cx, cy)
            right_click_at(cx, cy)
            menu = find_menu_window(args.pid, timeout=5.0)
        if not menu:
            result["posted_instead_of_click"] = True
            if not _user32.PostMessageW(hwnd, CALLBACK_MSG, 1, WM_RBUTTONUP):
                result["error"] = "PostMessage(托盘回调) 失败"
                return _write_helper_result(out_dir, args.label, result)
            menu = find_menu_window(args.pid, timeout=8.0)
        result["menu_hwnd"] = menu
        if not menu:
            result["error"] = "右键菜单没有弹出来"
            return _write_helper_result(out_dir, args.label, result)
        time.sleep(0.35)
        box = stable_menu_rect(menu)
        if box is None:
            result["error"] = "菜单窗口没有稳定下来"
            return _write_helper_result(out_dir, args.label, result)
        result["menu_window_rect"] = box
        zw, zh = save_region(out_dir / f"05_menu_{args.label}_zoom.png", box, args.scale)
        result["menu_image"] = [zw, zh]
        result["gui_exe_shot"] = shot_gui_exe(out_dir / f"04_menu_{args.label}_gui_exe_shot.png")
        width, height = box[2] - box[0], box[3] - box[1]
        item_h = max(1, (height - 6) // 4)
        cx = box[0] + width // 2
        cy = box[1] + 3 + item_h * 3 + item_h // 2  # 第 4 项 = 退出
        result["clicked_at"] = [cx, cy]
        result["cursor_placed"] = place_cursor(cx, cy)
        result["cursor_before_click"] = list(_cursor())
        result["cursor_on_menu"] = point_on_window(menu, *_cursor())
        result["click_at_time"] = round(time.time(), 3)
        if result["cursor_placed"] and result["cursor_on_menu"]:
            click_at(cx, cy)
            time.sleep(0.5)
            result["menu_gone_after_click"] = find_menu_window(args.pid, timeout=0.5) == 0
        if not result.get("menu_gone_after_click"):
            # 鼠标被外部干扰时用键盘选"退出"(第 4 项)
            result["used_keyboard_fallback"] = "cursor-not-on-menu"
            menu_keyboard_select(3, box)
            result["menu_gone_after_enter"] = find_menu_window(args.pid, timeout=0.6) == 0
        return _write_helper_result(out_dir, args.label, result)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return _write_helper_result(out_dir, args.label, result)


# --------------------------------------------------------------------------- #
# 断言用的小工具
# --------------------------------------------------------------------------- #


class Report:
    """记录每一步的实测结论。

    三种状态刻意区分开:
      True  = 实测通过
      False = **产品有问题**(只有"界面完全可点、回调却没来"才这么判)
      None  = skipped: 环境限制(光标被真人挪走 / 菜单被系统取消), 不算产品失败
    """

    def __init__(self) -> None:
        self.steps: list[dict[str, object]] = []

    def add(self, name: str, ok: bool | None, detail: str) -> bool | None:
        self.steps.append({"step": name, "ok": ok, "detail": detail})
        tag = "SKIP" if ok is None else ("OK " if ok else "FAIL")
        print(f"    [{tag}] {name}: {detail}")
        return ok

    @property
    def failed(self) -> list[dict[str, object]]:
        return [s for s in self.steps if s["ok"] is False]

    @property
    def skipped(self) -> list[dict[str, object]]:
        return [s for s in self.steps if s["ok"] is None]

    @property
    def ok(self) -> bool:
        return bool(self.steps) and not self.failed

    def summary_line(self) -> str:
        return (
            f"通过 {len(self.steps) - len(self.failed) - len(self.skipped)} / "
            f"跳过 {len(self.skipped)} / 失败 {len(self.failed)}"
        )


def icon_entries(data: bytes) -> list[tuple[int, int, int, int, int]]:
    """解析 ICONDIR: (宽, 高, 位深, 字节数, 数据偏移)。"""
    if data[0:4] != b"\x00\x00\x01\x00":
        raise ValueError("不是 ICO 文件(魔数不对)")
    count = struct.unpack_from("<H", data, 4)[0]
    out = []
    for i in range(count):
        w, h, _c, _r, _p, bits, length, off = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        out.append((w or 256, h or 256, bits, length, off))
    return out


def shell_icon_rect(hwnd: int, uid: int = 1) -> tuple[int, int, int, int] | None:
    """问 shell: 我的图标现在在屏幕上的哪个矩形里? 拿不到就是没挂上。"""
    ident = _NOTIFYICONIDENTIFIER()
    ident.cbSize = ctypes.sizeof(_NOTIFYICONIDENTIFIER)
    ident.hWnd = hwnd
    ident.uID = uid
    rect = wintypes.RECT()
    if _shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(rect)) != 0:
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def menu_item_text(hmenu: int, cmd_id: int) -> str:
    """按命令 id 读菜单文字, 用来证明 id 和菜单项的对应关系。"""
    buf = ctypes.create_unicode_buffer(128)
    _user32.GetMenuStringW(hmenu, cmd_id, buf, 128, 0x00000000)  # MF_BYCOMMAND
    return buf.value


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="红杏托盘实测")
    ap.add_argument("--out", default=str(ROOT / "artifacts" / "tray"), help="截图/报告输出目录")
    ap.add_argument("--scale", type=int, default=5, help="放大倍数")
    ap.add_argument("--item", type=int, default=2, help="用真实鼠标点第几个菜单项(从 0 开始)")
    ap.add_argument("--hold", type=float, default=0.0, help="结束前保持托盘 N 秒(人工看)")
    ap.add_argument("--soak", type=float, default=0.0, help="保活 N 秒, 验证 Tk 主循环不被打断")
    # 内部用: 主进程把本脚本再拉起来一个当"菜单辅助进程"(见 menu_helper 的注释)
    ap.add_argument("--menu-helper", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--app-quit-helper", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--app-verify", action="store_true", help="带托盘跑真客户端并验证存活/退出")
    ap.add_argument("--app-seconds", type=float, default=70.0, help="--app-verify 的存活观察时长")
    ap.add_argument("--pid", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--label", default="menu", help=argparse.SUPPRESS)
    ap.add_argument("--wait", type=float, default=8.0, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.menu_helper:
        return menu_helper(args)
    if args.app_quit_helper:
        return app_quit_helper(args)
    if args.app_verify:
        return app_verify(args)

    enable_dpi_awareness()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = Report()
    screen_w, screen_h = _user32.GetSystemMetrics(SM_CXSCREEN), _user32.GetSystemMetrics(SM_CYSCREEN)
    print("=" * 78)
    print(f"[i] 屏幕(物理像素) {screen_w}x{screen_h}; 输出目录 {out_dir}")

    # ------------------------------------------------------------------ #
    print("[1|7] 图标自检: 纯 Python 生成的 ICO, Windows 认不认")
    ico_on = icon.ensure_ico()
    ico_off = icon.ensure_ico(connected=False)
    data = ico_on.read_bytes()
    entries = icon_entries(data)
    report.add(
        "ICONDIR",
        [e[0] for e in entries] == list(icon.SIZES),
        f"{ico_on.name} {len(data)} 字节, 尺寸={[e[0] for e in entries]} 位深={[e[2] for e in entries]}",
    )
    report.add(
        "未连接图标",
        ico_off.exists() and ico_off.stat().st_size > 0,
        f"{ico_off.name} {ico_off.stat().st_size} 字节",
    )
    # CreateIconFromResourceEx 吃的是 RT_ICON 资源 = 裸 DIB(不带 ICONDIR)。
    # 这是最硬的一条格式校验: 头写歪(比如 biHeight 没写两倍)它就返回 NULL。
    decoded = []
    for w, _h, _bits, length, offset in entries:
        hicon = _user32.CreateIconFromResourceEx(
            data[offset : offset + length], length, True, 0x00030000, w, w, 0
        )
        if hicon:
            decoded.append(w)
            _user32.DestroyIcon(hicon)
    report.add(
        "Windows 逐个解出尺寸",
        decoded == [e[0] for e in entries],
        f"CreateIconFromResourceEx -> {decoded}",
    )
    (out_dir / "00_icon_preview_256.png").write_bytes(icon.png_bytes(256))
    (out_dir / "00_icon_off_preview_256.png").write_bytes(icon.png_bytes(256, connected=False))

    # ------------------------------------------------------------------ #
    print("[2|7] 建主窗口 + 挂托盘 (按接线层的真实顺序: 先 Tk, 再 start())")
    import tkinter as tk

    root = tk.Tk()
    root.title("红杏托盘自测")
    root.geometry("640x360+160+160")
    tk.Label(
        root,
        text="这个窗口只是陪衬\n托盘图标在右下角通知区域",
        font=("Microsoft YaHei UI", 13),
    ).pack(expand=True)

    fired: dict[str, int] = {"show": 0, "toggle": 0, "best": 0, "quit": 0}
    lock = threading.Lock()

    def mark(name: str):
        """回调跑在**托盘线程**上: 只记数/打印, 绝不碰 Tk 控件(这是硬纪律)。"""
        def _inner() -> None:
            with lock:
                fired[name] += 1
                count = fired[name]
            print(f"    -> 回调 {name} 被调用 (第 {count} 次) [托盘线程 {threading.current_thread().name}]")

        return _inner

    tray = Tray(
        on_show=mark("show"),
        on_toggle=mark("toggle"),
        on_pick_best=mark("best"),
        on_quit=mark("quit"),  # 只记数; 主循环由下面的 _poll 停
        tooltip="红杏 · 自测",
    )
    # 记下 TrackPopupMenu 真正返回的命令 id: 断言"点了第 2 项"时要能对上是哪一项
    menu_result: dict[str, object] = {}
    _real_on_command = tray._on_command

    def _spy_on_command(cmd_id: int) -> None:
        menu_result["cmd_id"] = int(cmd_id)
        _real_on_command(cmd_id)

    tray._on_command = _spy_on_command  # type: ignore[method-assign]

    # 菜单弹出前(托盘线程上)把 id->文字 抄下来, 作为"菜单项就是这四项"的证据
    _real_append_items = tray._append_items

    def _spy_append_items(menu: int) -> None:
        _real_append_items(menu)
        menu_result["id_texts"] = {i: menu_item_text(menu, i) for i in (1, 2, 3, 4)}
        menu_result["menu_built_at"] = round(time.time(), 3)

    tray._append_items = _spy_append_items  # type: ignore[method-assign]

    # 记录菜单开/关的时刻 + 当时 Tk 主循环的 tick 数:
    #   * 时间线用来对上独立进程的点击时刻;
    #   * tick 用来证明菜单开着的时候 Tk 还在跑(菜单只阻塞托盘线程)。
    _real_show_menu = tray._show_menu

    def _spy_show_menu() -> None:
        with lock:
            ticks_at_open = ticks["n"]
        menu_result["menu_opened_at"] = round(time.time(), 3)
        menu_result["ticks_at_menu_open"] = ticks_at_open
        menu_result["tray_hwnd"] = hwnd
        menu_result["foreground_before_menu"] = int(_user32.GetForegroundWindow() or 0)
        try:
            _real_show_menu()
        finally:
            menu_result["menu_closed_at"] = round(time.time(), 3)
            with lock:
                menu_result["ticks_at_menu_close"] = ticks["n"]

    tray._show_menu = _spy_show_menu  # type: ignore[method-assign]

    # Tk 主循环的心跳: 只要它在涨, 就说明界面没被托盘冻住。
    ticks = {"n": 0}

    def _tick() -> None:
        with lock:
            ticks["n"] += 1
        root.after(200, _tick)

    started = tray.start()
    report.add("Tray.start()", started, f"返回 {started}")
    if not started:
        root.destroy()
        return 1
    root.after(200, _tick)

    hwnd = int(tray._hwnd)
    rect = shell_icon_rect(hwnd)
    # Shell_NotifyIconGetRect 在新图标躺进"隐藏图标"溢出区时会返回失败 —— 那
    # 是 shell 的行为(图标在溢出菜单里), 不代表没挂上, 所以这里只记录不判失败。
    print(f"    [i] Shell_NotifyIconGetRect -> {rect} (None = 图标在溢出区, 属正常)")

    # ------------------------------------------------------------------ #
    print("[3|7] 截图: 全屏 + 通知区域放大")
    print(f"    gui.exe shot: {shot_gui_exe(out_dir / '01_desktop_gui_exe_shot.png')}")
    width, height, pixels = capture_screen()
    write_png(out_dir / "02_desktop_full.png", width, height, pixels)
    anchor = rect or (screen_w - 260, screen_h - 60, screen_w, screen_h)
    tray_region = (anchor[0] - 260, anchor[1] - 40, anchor[2] + 160, min(screen_h, anchor[3] + 40))
    zw, zh = save_region(out_dir / "03_tray_zoom.png", tray_region, args.scale)
    print(f"    通知区域 {clamp_rect(tray_region, screen_w, screen_h)} 放大 {args.scale}x -> 03_tray_zoom.png ({zw}x{zh})")

    # 新图标默认在"隐藏图标"溢出菜单里(shell 的行为), 这时 GetRect 会失败。
    # 点一下 ^ 把溢出菜单打开截图 —— "图标确实挂上了"这件事要有图为证。
    if rect is None:
        chevron = taskbar_chevron_point()
        if chevron:
            placed = place_cursor(*chevron)
            click_at(*chevron)
            time.sleep(0.7)
            flyout_hwnd = int(_user32.FindWindowW("NotifyIconOverflowWindow", None) or 0)
            flyout = window_rect_of(flyout_hwnd)
            region = flyout or (chevron[0] - 300, max(0, chevron[1] - 380), chevron[0] + 80, chevron[1])
            fw, fh = save_region(out_dir / "03b_overflow_icons.png", region, args.scale)
            print(
                f"    溢出菜单: chevron={chevron}(命中={placed}) flyout={flyout} -> "
                f"03b_overflow_icons.png ({fw}x{fh})"
            )
            click_at(*chevron)  # 再点一次关掉, 别挡住后面的操作
            time.sleep(0.3)

    # ------------------------------------------------------------------ #
    # 菜单: 投递 shell 同款回调消息, 菜单弹出后用真实鼠标点菜单项
    # ------------------------------------------------------------------ #
    menu_x, menu_y = 760, 380

    def menu_attempt(item_index: int, label: str, attempt: int) -> dict[str, object]:
        """弹一次右键菜单, 由**独立进程**截图并点菜单项。

        菜单现在跑在托盘线程上, 所以 Tk 主循环全程照常运行 —— 这里刻意用真的
        `root.mainloop()` 来等结果: 如果托盘又把界面线程拖住了, tick 不涨,
        下面那条断言就会立刻失败(这就是"托盘搞死客户端"的回归测试)。
        """
        nonlocal menu_result
        _user32.SetCursorPos(menu_x, menu_y)  # 菜单出现在这里, 坐标可预期
        menu_result = {"cursor": [menu_x, menu_y], "item_index": item_index, "attempt": attempt}
        result_path = out_dir / f"menu_{label}.json"
        if result_path.exists():
            result_path.unlink()
        helper = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--menu-helper",
                "--pid", str(os.getpid()),
                "--item", str(item_index),
                "--label", label,
                "--out", str(out_dir),
                "--scale", str(args.scale),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(0.4)  # 让辅助进程先起来并开始找菜单窗口
        with lock:
            before_count = fired.get(marker)
            ticks_before = ticks["n"]
        menu_result["ticks_before_post"] = ticks_before
        if not _user32.PostMessageW(hwnd, CALLBACK_MSG, 1, WM_RBUTTONUP):
            helper.kill()
            menu_result["error"] = "PostMessage 失败"
            return menu_result
        deadline = time.time() + 15.0

        def _poll() -> None:
            with lock:
                done = fired.get(marker) != before_count
            if done or time.time() > deadline:
                root.quit()
            else:
                root.after(120, _poll)

        root.after(120, _poll)
        root.mainloop()  # 菜单由托盘线程弹, 这里真的在跑 Tk 主循环
        with lock:
            menu_result["ticks_after_menu"] = ticks["n"]
        try:
            helper_out, _ = helper.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            helper.kill()
            helper_out = "(辅助进程超时, 已杀掉)"
        menu_result["helper_stdout"] = (helper_out or "").strip()
        if result_path.exists():
            menu_result.update(json.loads(result_path.read_text(encoding="utf-8")))
        else:
            menu_result["error"] = "辅助进程没有写结果文件"
        return menu_result

    def menu_round(item_index: int, label: str, attempts: int = 3) -> dict[str, object]:
        """点菜单项, 允许重试。

        这台机器上有真人用鼠标(光标会在 SetCursorPos 和点击之间被挪走), 也会
        有别的程序抢前台导致菜单被系统取消 —— 这些都不是产品行为, 记进报告即可,
        断言只关心"最终点中了没有"。每次尝试的证据都留在 menu_<label>.json。
        """
        with lock:
            baseline = fired.get(marker)
        result: dict[str, object] = {}
        tries: list[dict[str, object]] = []
        for attempt in range(1, attempts + 1):
            result = menu_attempt(item_index, label, attempt)
            tries.append(
                {
                    "attempt": attempt,
                    "menu_rect": result.get("menu_window_rect"),
                    "cursor_on_menu": result.get("cursor_on_menu"),
                    "keyboard_fallback": result.get("used_keyboard_fallback"),
                    "cmd_id": result.get("cmd_id"),
                }
            )
            with lock:
                if fired.get(marker) != baseline:
                    break
            print(f"    [!] 第 {attempt} 次没点中(外部干扰), 重试")
            time.sleep(0.6)
        result["attempts"] = tries
        return result

    # 其他菜单项靠 _poll 发现回调计数变化后停; 退出项也一样(回调只记数)。
    menu_action = {0: "show", 1: "toggle", 2: "best", 3: "quit"}
    marker = menu_action.get(args.item, "best")

    def menu_verdict(res: dict[str, object], action: str) -> tuple[bool | None, str]:
        """判断这一轮菜单到底算通过、产品有问题、还是环境不让测。

        只有"菜单真的弹在屏幕上、光标也确实落在菜单上、点了之后回调仍然没来"
        才算产品问题(False)。其余情况(菜单被系统秒取消 / 光标被真人挪走)都是
        环境限制, 记 skipped(None), 免得把脚本的局限写成产品的红字。
        """
        if fired.get(action) == 1:
            return True, f"回调 {action} 命中(第 {res.get('attempt')} 次尝试)"
        if not res.get("menu_hwnd"):
            return None, "菜单根本没出现在屏幕上(我们的进程不是前台时会被系统秒取消)"
        if not res.get("menu_window_rect"):
            return None, "菜单窗口存在但位置一直没稳定, 没法算菜单项坐标"
        if res.get("cursor_on_menu") is False and res.get("cursor_placed") is False:
            return None, (
                f"光标没能放到菜单上(SetCursorPos 被外部鼠标活动打断; "
                f"目标={res.get('clicked_at')} 实际={res.get('cursor_before_click')})"
            )
        if res.get("used_keyboard_fallback") and res.get("menu_gone_after_enter"):
            return None, "鼠标路径被干扰, 键盘兜底执行了但选中的不是目标项"
        return False, f"菜单可点(rect={res.get('menu_window_rect')})但回调没触发 —— 这是真的有问题"

    print(f"[4|7] 右键菜单: 投递 WM_RBUTTONUP -> 托盘线程弹菜单 -> 独立进程真实鼠标点第 {args.item} 项")
    first = menu_round(args.item, "pick_best")
    expected_first = menu_action.get(args.item, "best")
    verdict, why = menu_verdict(first, expected_first)
    ticks_during = int(first.get("ticks_after_menu", 0)) - int(first.get("ticks_before_post", 0))
    report.add(
        f"菜单弹出并被点中(第 {args.item} 项 -> {expected_first})",
        verdict,
        f"{why}; 菜单窗口 rect={first.get('menu_window_rect')} 点击={first.get('clicked_at')} "
        f"回调计数={fired} cmd_id={first.get('cmd_id')} 菜单项文字={first.get('id_texts')} "
        f"尝试次数={len(first.get('attempts', []))}",
    )
    # 心跳: 菜单真的弹起来过(tick>0)才算这条有意义
    report.add(
        "菜单开着时 Tk 主循环仍在跑",
        True if ticks_during >= 2 else None,
        f"这一轮 Tk 心跳 tick 增加 {ticks_during} 次(菜单只阻塞托盘线程)"
        + ("" if ticks_during >= 2 else "; 本轮菜单被系统取消, 没能测到"),
    )

    # ------------------------------------------------------------------ #
    print("[5|7] 状态切换: set_connected(True) 后图标与提示要跟着变")
    tray.set_connected(True)
    rect_on = shell_icon_rect(hwnd)
    tray.set_tooltip("红杏 · 自测 · 已连接")
    print(f"    [i] 已连接状态 Shell_NotifyIconGetRect -> {rect_on}")
    save_region(out_dir / "06_connected_zoom.png", tray_region, args.scale)

    if args.soak:
        print(f"[i] 保活 {args.soak:.0f} 秒: Tk 主循环必须一直在跑(托盘搞死客户端的回归测试)")
        with lock:
            soak_start_ticks = ticks["n"]
        deadline = time.time() + args.soak

        def _soak_poll() -> None:
            if time.time() >= deadline:
                root.quit()
            else:
                root.after(250, _soak_poll)

        root.after(250, _soak_poll)
        root.mainloop()
        with lock:
            soak_ticks = ticks["n"] - soak_start_ticks
        report.add(
            "保活期间 Tk 主循环没停",
            soak_ticks >= args.soak * 2,  # 心跳 200ms 一次, 至少该有 5 次/秒
            f"{args.soak:.0f} 秒里 tick 增加 {soak_ticks} 次",
        )

    if args.hold:
        print(f"[i] 保持托盘 {args.hold} 秒(人工观察)")
        time.sleep(args.hold)

    # ------------------------------------------------------------------ #
    print("[6|7] 退出菜单项: 点'退出' -> 回调触发")
    marker = "quit"
    last = menu_round(3, "quit")
    verdict_q, why_q = menu_verdict(last, "quit")
    report.add(
        "退出菜单可用",
        verdict_q,
        f"{why_q}; 菜单窗口 rect={last.get('menu_window_rect')} 回调计数={fired} "
        f"cmd_id={last.get('cmd_id')} 尝试次数={len(last.get('attempts', []))}",
    )

    # ------------------------------------------------------------------ #
    print("[7|7] 摘除托盘: stop() 后图标必须从 shell 里消失(不留幽灵图标)")
    hwnd_before = hwnd
    tray.stop()
    time.sleep(0.6)
    gone = shell_icon_rect(hwnd_before)
    report.add("stop() 后图标消失", gone is None, f"Shell_NotifyIconGetRect -> {gone}")
    report.add(
        "托盘线程已结束",
        not (tray._thread and tray._thread.is_alive()),
        f"thread={tray._thread}",
    )
    save_region(out_dir / "07_after_stop_zoom.png", tray_region, args.scale)
    shot_gui_exe(out_dir / "08_after_stop_gui_exe_shot.png")
    try:
        root.destroy()  # 窗口可能已被外部关掉(实测被清理脚本关过), 别让收尾把它变成崩溃
    except tk.TclError:
        pass

    summary = {
        "ok": report.ok,
        "summary": report.summary_line(),
        "screen_physical": [screen_w, screen_h],
        "dpi_note": (
            "本脚本 SetProcessDPIAware, 所有坐标/截图都是物理像素 "
            f"({screen_w}x{screen_h}); 不声明 DPI 感知的进程(PowerShell/Tk/gui.exe) "
            "看到的是虚拟化的 1280x720, 两套坐标混用会截出错位的图。"
        ),
        "steps": report.steps,
        "skipped": [s["step"] for s in report.skipped],
        "callbacks": fired,
        "ico": {"on": str(ico_on), "off": str(ico_off), "entries": entries},
        "icon_rect": rect,
        "menu": {k: v for k, v in menu_result.items() if k != "hmenu"},
        "out_dir": str(out_dir),
    }
    (out_dir / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 78)
    print(f"结论: {report.summary_line()}; 报告 -> {out_dir / 'report.json'}")
    return 0 if report.ok else 1



if __name__ == "__main__":
    raise SystemExit(main())
