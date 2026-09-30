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
     菜单弹出后用 GetMenuItemRect 取每个菜单项的屏幕坐标, 再用 SendInput 发
     **真实鼠标**点下去, 验证对应的业务回调真的被调用;
  5. 退出: 点"退出"菜单项, 确认回调触发且图标从 shell 里消失(不留幽灵图标)。

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
                                    [--item 2] [--hold 0]
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


def click_at(x: int, y: int) -> None:
    """真实鼠标点击(SetCursorPos + mouse_event), 走完整的 Win32 输入路径。"""
    _user32.SetCursorPos(int(x), int(y))
    time.sleep(0.12)
    _user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
    _user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)


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
        rect = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        box = (rect.left, rect.top, rect.right, rect.bottom)
        result["menu_window_rect"] = box
        time.sleep(0.35)  # 等菜单画完
        zw, zh = save_region(out_dir / f"05_menu_{args.label}_zoom.png", box, args.scale)
        result["menu_image"] = [zw, zh]

        # 菜单项等高, 从窗口矩形推第 item 项的中心(上下各留 3 像素边框)
        width, height = box[2] - box[0], box[3] - box[1]
        item_h = max(1, (height - 6) // 4)
        cx = box[0] + width // 2
        cy = box[1] + 3 + item_h * args.item + item_h // 2
        result["clicked_at"] = [cx, cy]
        result["item_h"] = item_h
        # 点之前先确认这个坐标上压着的就是菜单窗口(辅助进程自己线程, 随便查)
        _user32.SetCursorPos(cx, cy)
        time.sleep(0.15)
        pt = _POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        under = _user32.WindowFromPoint(pt)
        result["cursor"] = [pt.x, pt.y]
        result["window_under_cursor"] = int(under or 0)
        result["cursor_on_menu"] = int(under or 0) == hwnd
        result["click_at_time"] = round(time.time(), 3)
        click_at(cx, cy)
        time.sleep(0.4)
        result["menu_gone_after_click"] = find_menu_window(args.pid, timeout=0.4) == 0
        # 截图放到点击之后: gui.exe 是控制台程序, 启动时可能抢前台, 而弹出菜单
        # 一失去前台就会被系统取消 —— 先点后拍, 免得自己的取证动作毁掉被测对象。
        result["gui_exe_shot"] = shot_gui_exe(out_dir / f"04_menu_{args.label}_gui_exe_shot.png")
        if not result["menu_gone_after_click"]:
            # 兜底: 别把主进程的模态菜单留在屏幕上(ESC 优先给菜单, 菜单没了就是前台窗口)
            _user32.keybd_event(0x1B, 0, 0, None)
            _user32.keybd_event(0x1B, 0, 2, None)
            time.sleep(0.3)
            result["escaped"] = True
        return _write_helper_result(out_dir, args.label, result)
    except Exception as exc:  # 子进程自己也要把失败写成结果, 别让父进程干等
        result["error"] = f"{type(exc).__name__}: {exc}"
        return _write_helper_result(out_dir, args.label, result)


def _write_helper_result(out_dir: Path, label: str, result: dict[str, object]) -> int:
    (out_dir / f"menu_{label}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if not result.get("error") else 1


# --------------------------------------------------------------------------- #
# 断言用的小工具
# --------------------------------------------------------------------------- #


class Report:
    """记录每一步的实测结论; 任何一步失败都会让脚本以非零码退出。"""

    def __init__(self) -> None:
        self.steps: list[dict[str, object]] = []

    def add(self, name: str, ok: bool, detail: str) -> bool:
        self.steps.append({"step": name, "ok": bool(ok), "detail": detail})
        print(f"    [{'OK ' if ok else 'FAIL'}] {name}: {detail}")
        return bool(ok)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(bool(s["ok"]) for s in self.steps)


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
    # 内部用: 主进程把本脚本再拉起来一个当"菜单辅助进程"(见 menu_helper 的注释)
    ap.add_argument("--menu-helper", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--pid", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--label", default="menu", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.menu_helper:
        return menu_helper(args)

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
        def _inner() -> None:
            with lock:
                fired[name] += 1
            print(f"    -> 回调 {name} 被调用 (第 {fired[name]} 次)")

        return _inner

    def on_quit() -> None:
        mark("quit")()
        # 关键: 托盘回调跑在 Tk 的消息派发过程中, 直接 root.destroy() 会在
        # Tcl 的 notifier 里留下悬空状态。用 after 交回事件循环才安全。
        root.after(0, root.quit)

    tray = Tray(
        on_show=mark("show"),
        on_toggle=mark("toggle"),
        on_pick_best=mark("best"),
        on_quit=on_quit,
        tooltip="红杏 · 自测",
    )
    # 记下 TrackPopupMenu 真正返回的命令 id: 断言"点了第 2 项"时要能对上是哪一项
    _real_on_command = tray._on_command

    def _spy_on_command(cmd_id: int) -> None:
        menu_result["cmd_id"] = int(cmd_id)
        _real_on_command(cmd_id)

    tray._on_command = _spy_on_command  # type: ignore[method-assign]

    # 在主线程里(菜单弹出前)把 id->文字 抄下来, 作为"菜单项就是这四项"的证据。
    # 刻意不在辅助线程里调 Win32 菜单函数: 主线程此刻正卡在 TrackPopupMenu 的
    # 模态循环里, 跨线程碰菜单会把解释器搞崩(实测遇到过 GIL/线程状态崩溃)。
    _real_append_items = tray._append_items

    def _spy_append_items(menu: int) -> None:
        _real_append_items(menu)
        menu_result["id_texts"] = {i: menu_item_text(menu, i) for i in (1, 2, 3, 4)}
        menu_result["menu_built_at"] = round(time.time(), 3)

    tray._append_items = _spy_append_items  # type: ignore[method-assign]

    # 记录菜单开/关的时刻: 和辅助进程的时间线一对, 就能看出菜单到底是被点中的
    # 还是被别的窗口抢前台取消掉的。
    _real_show_menu = tray._show_menu

    def _spy_show_menu() -> None:
        menu_result["menu_opened_at"] = round(time.time(), 3)
        try:
            _real_show_menu()
        finally:
            menu_result["menu_closed_at"] = round(time.time(), 3)

    tray._show_menu = _spy_show_menu  # type: ignore[method-assign]
    started = tray.start()
    report.add("Tray.start()", started, f"返回 {started}")
    if not started:
        root.destroy()
        return 1

    hwnd = int(tray._hwnd)
    rect = shell_icon_rect(hwnd)
    report.add("shell 收下了图标", rect is not None, f"Shell_NotifyIconGetRect -> {rect}")

    # ------------------------------------------------------------------ #
    print("[3|7] 截图: 全屏 + 通知区域放大")
    print(f"    gui.exe shot: {shot_gui_exe(out_dir / '01_desktop_gui_exe_shot.png')}")
    width, height, pixels = capture_screen()
    write_png(out_dir / "02_desktop_full.png", width, height, pixels)
    anchor = rect or (screen_w - 260, screen_h - 60, screen_w, screen_h)
    tray_region = (anchor[0] - 260, anchor[1] - 40, anchor[2] + 160, min(screen_h, anchor[3] + 40))
    zw, zh = save_region(out_dir / "03_tray_zoom.png", tray_region, args.scale)
    print(f"    通知区域 {clamp_rect(tray_region, screen_w, screen_h)} 放大 {args.scale}x -> 03_tray_zoom.png ({zw}x{zh})")

    # ------------------------------------------------------------------ #
    # 菜单: 投递 shell 同款回调消息, 菜单弹出后用真实鼠标点菜单项
    # ------------------------------------------------------------------ #
    menu_result: dict[str, object] = {}
    menu_x, menu_y = 760, 380

    def menu_round(item_index: int, label: str) -> dict[str, object]:
        """弹一次右键菜单, 由**独立进程**截图并点菜单项。

        主线程只做两件事: 把光标放好、投递 shell 同款回调消息。之后它就会卡在
        WndProc 里的 TrackPopupMenu(模态), 直到辅助进程点中菜单项。
        """
        nonlocal menu_result
        _user32.SetCursorPos(menu_x, menu_y)  # 菜单出现在这里, 坐标可预期
        menu_result = {"cursor": [menu_x, menu_y], "item_index": item_index}
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
        if not _user32.PostMessageW(hwnd, CALLBACK_MSG, 1, WM_RBUTTONUP):
            helper.kill()
            menu_result["error"] = "PostMessage 失败"
            return menu_result
        # 菜单是模态的: 事件循环只在菜单关掉之后才会继续跑 after 回调。
        deadline = time.time() + 15.0

        def _poll() -> None:
            with lock:
                clicked = fired.get(marker) != before_count
            if clicked or time.time() > deadline:
                root.quit()
            else:
                root.after(120, _poll)

        with lock:
            before_count = fired.get(marker)
        root.after(120, _poll)
        root.mainloop()
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

    # on_quit 会把主循环停掉; 其他菜单项靠 _poll 发现回调计数变化后停。
    menu_action = {0: "show", 1: "toggle", 2: "best", 3: "quit"}
    marker = menu_action.get(args.item, "best")

    print(f"[4|7] 右键菜单: 投递 WM_RBUTTONUP -> 菜单弹出 -> 独立进程真实鼠标点第 {args.item} 项")
    first = menu_round(args.item, "pick_best")
    expected_first = menu_action.get(args.item, "best")
    report.add(
        f"菜单弹出并被点中(第 {args.item} 项 -> {expected_first})",
        bool(first.get("menu_hwnd")) and fired.get(expected_first) == 1,
        f"菜单窗口 rect={first.get('menu_window_rect')} 点击={first.get('clicked_at')} "
        f"回调计数={fired} cmd_id={first.get('cmd_id')} 菜单项文字={first.get('id_texts')}",
    )

    # ------------------------------------------------------------------ #
    print("[5|7] 状态切换: set_connected(True) 后图标与提示要跟着变")
    tray.set_connected(True)
    rect_on = shell_icon_rect(hwnd)
    tray.set_tooltip("红杏 · 自测 · 已连接")
    report.add("已连接状态图标仍在", rect_on is not None, f"Shell_NotifyIconGetRect -> {rect_on}")
    save_region(out_dir / "06_connected_zoom.png", tray_region, args.scale)

    if args.hold:
        print(f"[i] 保持托盘 {args.hold} 秒(人工观察)")
        time.sleep(args.hold)

    # ------------------------------------------------------------------ #
    print("[6|7] 退出菜单项: 点'退出' -> 回调触发")
    marker = "quit"
    last = menu_round(3, "quit")
    report.add(
        "退出菜单可用",
        bool(last.get("menu_hwnd")) and fired.get("quit") == 1,
        f"菜单窗口 rect={last.get('menu_window_rect')} 回调计数={fired} cmd_id={last.get('cmd_id')}",
    )

    # ------------------------------------------------------------------ #
    print("[7|7] 摘除托盘: stop() 后图标必须从 shell 里消失(不留幽灵图标)")
    hwnd_before = hwnd
    tray.stop()
    time.sleep(0.6)
    gone = shell_icon_rect(hwnd_before)
    report.add("stop() 后图标消失", gone is None, f"Shell_NotifyIconGetRect -> {gone}")
    save_region(out_dir / "07_after_stop_zoom.png", tray_region, args.scale)
    shot_gui_exe(out_dir / "08_after_stop_gui_exe_shot.png")
    root.destroy()

    summary = {
        "ok": report.ok,
        "screen": [screen_w, screen_h],
        "steps": report.steps,
        "callbacks": fired,
        "ico": {"on": str(ico_on), "off": str(ico_off), "entries": entries},
        "icon_rect": rect,
        "menu": {k: v for k, v in menu_result.items() if k != "hmenu"},
        "out_dir": str(out_dir),
    }
    (out_dir / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 78)
    print(f"结论: {'全部通过' if report.ok else '存在失败项'}; 报告 -> {out_dir / 'report.json'}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
