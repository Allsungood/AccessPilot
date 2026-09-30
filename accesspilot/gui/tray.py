"""红杏系统托盘: 纯 ctypes 调 Shell_NotifyIconW, 零第三方依赖.

为什么不用 pystray
------------------
pystray 会连带拉进 Pillow(为了画图标), 而本项目的硬约束是**运行时零依赖**:
目标机器常常访问不了 PyPI。托盘需要的 Win32 接口其实只有十来个
(RegisterClassExW / CreateWindowExW / Shell_NotifyIconW / TrackPopupMenu),
ctypes 是标准库, 自己接一遍比引两个第三方包更划算, 也更好排错 —— 出问题时
栈里全是我们自己的代码。

线程模型(这里的选择是刻意的)
----------------------------
托盘窗口必须在**有消息泵的线程**上创建, 因为 shell 通过 PostMessage 把点击
事件投给这个窗口, 没人 DispatchMessage 就永远不会触发回调。

红杏的做法是: **在调用 start() 的那个线程上建窗口**, 不起自己的线程。
理由:

* 主窗口是 tkinter, 它的 mainloop 本身就是 Tcl 的 Windows 消息循环, 会
  派发本线程**所有**窗口的消息 —— 我们借用它, 不必再养一条线程;
* 回调因此天然跑在 Tk 主线程上, 回调里可以直接碰控件, 不需要
  `root.after(0, ...)` 那套跨线程搬运, 也不会出现"两个线程同时碰 Tk"
  这种最难查的崩溃;
* 反过来说: 如果回调跑在别的线程上, 那么任何一次 `app.show()` 都可能让
  tkinter 崩掉。这是不用独立线程的主要代价考量。

代价: 如果调用方在没有消息泵的线程里调 start(), 图标会出现但点不动。所以
`start()` 在非主线程上会打印一条明确的提示, 而不是静静地失效。

菜单为什么会"点不掉"
--------------------
`TrackPopupMenu` 是个模态循环: 它自己抓鼠标键盘, 直到用户选择或点击别处。
Win32 的规矩是 —— 弹菜单前必须 `SetForegroundWindow(我们的窗口)`, 弹完必须
往该窗口 Post 一个空消息。少任何一步, 菜单就会**点不掉也关不掉**, 只能等
用户切窗口。这不是可选优化, 是必需的配对动作。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import traceback
from typing import Any, Callable

from . import icon as _icon

__all__ = ["Tray", "available"]

_IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------------- #
# Win32 常量
# --------------------------------------------------------------------------- #

_WM_APP = 0x8000
_WM_NULL = 0x0000
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_COMMAND = 0x0111
_WM_CONTEXTMENU = 0x007B
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205

_NIM_ADD = 0x00000000
_NIM_MODIFY = 0x00000001
_NIM_DELETE = 0x00000002

_NIF_MESSAGE = 0x00000001
_NIF_ICON = 0x00000002
_NIF_TIP = 0x00000004

_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_SM_CXSCREEN = 0
_SM_CXSMICON = 49
_SM_CYSMICON = 50
_DESKTOPHORZRES = 118

_MF_STRING = 0x00000000
_MF_GRAYED = 0x00000001

_TPM_RIGHTBUTTON = 0x0002
_TPM_NONOTIFY = 0x0080
_TPM_RETURNCMD = 0x0100

#: 菜单命令 id(自己发给自己, 不经过 shell)
_MENU_SHOW = 1
_MENU_TOGGLE = 2
_MENU_BEST = 3
_MENU_QUIT = 4

#: 托盘回调消息: WM_APP 之后是留给应用自己的
_CALLBACK_MSG = _WM_APP + 1
_UID = 1

# --------------------------------------------------------------------------- #
# Win32 绑定
# --------------------------------------------------------------------------- #
#
# 全部 argtypes/restype 都显式声明: ctypes 默认把返回值当 C int, 在 64 位
# Python 上会把 HWND/HICON 这类指针截断成 32 位 —— 偶发但极难查的崩溃。

if _IS_WINDOWS:  # pragma: no cover - 平台分支
    from ctypes import wintypes

    _LRESULT = ctypes.c_ssize_t
    _WPARAM = ctypes.c_size_t
    _LPARAM = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM)

    class _WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    class _NOTIFYICONDATAW(ctypes.Structure):
        """NOTIFYICONDATAW(Vista 之后的完整版)。

        cbSize 填整个结构体大小 —— 填小了 shell 会按老版本解析, szTip 后面
        的字段全错位。
        """

        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    class _NOTIFYICONIDENTIFIER(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("guidItem", ctypes.c_byte * 16),
        ]

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    try:
        _user32: Any = ctypes.WinDLL("user32", use_last_error=True)
        _shell32: Any = ctypes.WinDLL("shell32", use_last_error=True)
        _kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        _gdi32: Any = ctypes.WinDLL("gdi32", use_last_error=True)
    except OSError:  # 极少数被裁剪过的系统
        _user32 = _shell32 = _kernel32 = _gdi32 = None

    if _user32 is not None:
        _u = _user32
        _gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
        _gdi32.GetDeviceCaps.restype = ctypes.c_int
        _u.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
        _u.RegisterClassExW.restype = wintypes.ATOM
        _u.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        _u.UnregisterClassW.restype = wintypes.BOOL
        _u.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        _u.CreateWindowExW.restype = wintypes.HWND
        _u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM]
        _u.DefWindowProcW.restype = _LRESULT
        _u.DestroyWindow.argtypes = [wintypes.HWND]
        _u.DestroyWindow.restype = wintypes.BOOL
        _u.IsWindow.argtypes = [wintypes.HWND]
        _u.IsWindow.restype = wintypes.BOOL
        _u.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
        _u.GetCursorPos.restype = wintypes.BOOL
        _u.CreatePopupMenu.argtypes = []
        _u.CreatePopupMenu.restype = wintypes.HMENU
        _u.DestroyMenu.argtypes = [wintypes.HMENU]
        _u.DestroyMenu.restype = wintypes.BOOL
        _u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        _u.AppendMenuW.restype = wintypes.BOOL
        _u.TrackPopupMenu.argtypes = [
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.LPVOID,
        ]
        _u.TrackPopupMenu.restype = wintypes.BOOL
        _u.SetForegroundWindow.argtypes = [wintypes.HWND]
        _u.SetForegroundWindow.restype = wintypes.BOOL
        _u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM]
        _u.PostMessageW.restype = wintypes.BOOL
        _u.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        _u.LoadImageW.restype = wintypes.HANDLE
        _u.DestroyIcon.argtypes = [wintypes.HICON]
        _u.DestroyIcon.restype = wintypes.BOOL
        _u.GetSystemMetrics.argtypes = [ctypes.c_int]
        _u.GetSystemMetrics.restype = ctypes.c_int
        _u.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        _u.RegisterWindowMessageW.restype = wintypes.UINT
        _u.GetDC.argtypes = [wintypes.HWND]
        _u.GetDC.restype = wintypes.HDC
        _u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        _u.ReleaseDC.restype = ctypes.c_int
        _u.GetMenuItemRect.argtypes = [
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.POINTER(wintypes.RECT),
        ]
        _u.GetMenuItemRect.restype = wintypes.BOOL

        _shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
        _shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        if hasattr(_shell32, "Shell_NotifyIconGetRect"):
            _shell32.Shell_NotifyIconGetRect.argtypes = [
                ctypes.POINTER(_NOTIFYICONIDENTIFIER),
                ctypes.POINTER(wintypes.RECT),
            ]
            _shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long

        _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        _kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    #: 窗口类名计数器: 同一进程里起第二个托盘(自测脚本会这么干)时不能撞名
    _class_seq = 0
    _class_lock = threading.Lock()
else:  # pragma: no cover - 平台分支
    _user32 = _shell32 = _kernel32 = _gdi32 = None
    _class_seq = 0
    _class_lock = threading.Lock()


def available() -> bool:
    """当前环境能不能挂托盘(非 Windows / 缺 DLL 时为 False)。"""
    return _user32 is not None and _shell32 is not None


# --------------------------------------------------------------------------- #
# Tray
# --------------------------------------------------------------------------- #


class Tray:
    """一个常驻的托盘图标 + 右键菜单。

    接口是固定的(accesspilot/gui/__init__.py 按它接线):

        Tray(*, on_show, on_toggle, on_pick_best, on_quit, tooltip="红杏")
        start() -> bool         失败返回 False 并打印原因, 不抛异常
        stop() -> None
        set_connected(bool) / update_menu_state(connected=...) / set_tooltip(str)

    回调都在**创建窗口的那个线程**(对红杏来说就是 Tk 主线程)上被调用,
    所以回调里可以直接操作控件。唯一要注意的是 `on_quit`: 它是在 Tk 的消息
    派发过程中被调用的, 直接 `root.destroy()` 会有风险, 请用
    `root.after(0, ...)` 把销毁动作交回事件循环。
    """

    def __init__(
        self,
        *,
        on_show: Callable[[], None] | None,
        on_toggle: Callable[[], None] | None,
        on_pick_best: Callable[[], None] | None,
        on_quit: Callable[[], None] | None,
        tooltip: str = "红杏",
    ) -> None:
        # on_* 允许为 None: 接线层用 getattr(app, "toggle", None) 取, 主窗口
        # 还没实现某个动作时传进来就是 None。菜单项要灰掉而不是崩掉。
        self._on_show = on_show
        self._on_toggle = on_toggle
        self._on_pick_best = on_pick_best
        self._on_quit = on_quit
        self._tooltip = tooltip or "红杏"

        self._connected = False
        self._started = False
        self._hwnd: int | None = None
        self._hicons: dict[bool, int] = {}
        self._class_name = ""
        self._owner_thread = 0
        self._taskbar_created_msg = 0
        self._wndproc: Any = None  # 必须留引用, 否则回调对象被 GC 后 WndProc 变野指针
        self._last_menu: int | None = None  # 自测脚本用它取菜单句柄
        self._menu_open = False

    # -- 对外接口 ----------------------------------------------------------- #

    def start(self) -> bool:
        """挂上托盘图标。失败只返回 False, 绝不抛异常。

        这是刻意设计的: 托盘挂不上(远程桌面、安全软件、Explorer 没跑)时,
        红杏应该退化成普通窗口继续可用, 而不是打不开。
        """
        if self._started:
            return True
        if not available():
            print("[x] 红杏托盘: 当前环境不支持系统托盘(仅 Windows 可用)")
            return False
        if threading.current_thread() is not threading.main_thread():
            # 说清楚失败原因, 而不是让用户对着一个点不动的图标发呆
            print(
                "[!] 红杏托盘: 在非主线程上启动, 该线程必须有消息泵"
                "(例如 tkinter mainloop), 否则图标可见但点击无响应"
            )
        try:
            if not self._create_window():
                return False
            if not self._load_icons():
                return False
            if not self._notify(_NIM_ADD, _NIF_MESSAGE | _NIF_ICON | _NIF_TIP):
                # Explorer 刚重启/刚登录时 taskbar 可能还没准备好, 短暂重试
                for _ in range(2):
                    time.sleep(0.25)
                    if self._notify(_NIM_ADD, _NIF_MESSAGE | _NIF_ICON | _NIF_TIP):
                        break
                else:
                    print("[x] 红杏托盘: Shell_NotifyIcon(NIM_ADD) 失败, 无法挂载图标")
                    self._destroy_window()
                    self._destroy_icons()
                    return False
            self._started = True
            return True
        except Exception as exc:  # 任何意外都退化成"没有托盘"
            print(f"[x] 红杏托盘: 挂载失败 {type(exc).__name__}: {exc}")
            traceback.print_exc()
            self._teardown()
            return False

    def stop(self) -> None:
        """摘掉图标并销毁窗口 —— 不能给用户留一个点不动的幽灵图标。"""
        self._teardown()

    def set_connected(self, connected: bool) -> None:
        """切换"已连接/未连接"两枚图标(契约方法, 等价于 update_menu_state)。"""
        self.update_menu_state(connected=connected)

    def set_tooltip(self, text: str) -> None:
        """改悬停提示。实际提示 = 传入文字 + 当前状态后缀。"""
        self._tooltip = text or "红杏"
        if self._started:
            self._notify(_NIM_MODIFY, _NIF_TIP)

    def update_menu_state(self, *, connected: bool) -> None:
        """连接状态变化时刷新图标和菜单文案。

        菜单每次弹出都是现建的(见 _show_menu), 所以这里只需要改图标和提示;
        留住这个方法是为了让接线层的调用点读起来有意义, 也避免 app.py 需要
        知道内部实现。
        """
        connected = bool(connected)
        if connected == self._connected and self._started:
            return
        self._connected = connected
        if self._started:
            self._notify(_NIM_MODIFY, _NIF_ICON | _NIF_TIP)

    # -- 内部: 窗口 --------------------------------------------------------- #

    def _create_window(self) -> bool:
        global _class_seq
        u = _user32
        with _class_lock:
            _class_seq += 1
            seq = _class_seq
        self._class_name = f"HongXingTrayWnd_{os.getpid()}_{seq}"

        self._wndproc = _WNDPROC(self._wnd_proc)
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = _kernel32.GetModuleHandleW(None)
        wc.lpszClassName = self._class_name
        if not u.RegisterClassExW(ctypes.byref(wc)):
            print(f"[x] 红杏托盘: RegisterClassExW 失败 (err={ctypes.get_last_error()})")
            return False

        # 普通隐藏窗口, 刻意**不用** HWND_MESSAGE 消息窗口:
        # 消息窗口不能成为前台窗口, SetForegroundWindow 会失败, 于是菜单
        # 又会掉进"点了不消失"的坑里。
        hwnd = u.CreateWindowExW(
            0, self._class_name, "红杏托盘", 0, 0, 0, 0, 0, None, None, wc.hInstance, None
        )
        if not hwnd:
            print(f"[x] 红杏托盘: CreateWindowExW 失败 (err={ctypes.get_last_error()})")
            u.UnregisterClassW(self._class_name, wc.hInstance)
            return False
        self._hwnd = int(hwnd)
        self._owner_thread = threading.get_ident()

        # Explorer 重启后会广播这条注册消息, 收到就得重新 NIM_ADD, 否则
        # 图标永远消失(用户只能重启客户端)。
        self._taskbar_created_msg = u.RegisterWindowMessageW("TaskbarCreated")
        return True

    def _destroy_window(self) -> None:
        u = _user32
        hwnd, self._hwnd = self._hwnd, None
        if not hwnd:
            return
        if threading.get_ident() == self._owner_thread:
            if u.IsWindow(hwnd):
                u.DestroyWindow(hwnd)
            if self._class_name:
                u.UnregisterClassW(self._class_name, _kernel32.GetModuleHandleW(None))
        else:
            # Win32 规定窗口只能由创建它的线程销毁, 跨线程 DestroyWindow 会
            # 失败并留下窗口。这里退一步: 让拥有者线程自己收到 WM_CLOSE 后销毁。
            u.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        self._class_name = ""

    # -- 内部: 图标与 shell 交互 -------------------------------------------- #

    def _icon_size(self) -> int:
        """通知区域里图标的边长(物理像素)。

        高 DPI 下通知区域的图标是 16 * (dpi/96) 像素(150% 缩放 = 24), 但
        **DPI 不感知**的进程调 GetSystemMetrics(SM_CXSMICON) 只会拿到被系统
        虚拟化后的 16。交一张 16x16 的位图给一个要 24x24 的 shell, 系统只能
        把它拉大, 图标就发虚。这里用"物理屏宽 / 逻辑屏宽"反推缩放比, 进程
        感知不感知 DPI 都算得对。
        """
        u, g = _user32, _gdi32
        base = u.GetSystemMetrics(_SM_CXSMICON) or 16
        try:
            hdc = u.GetDC(None)
            try:
                physical = g.GetDeviceCaps(hdc, _DESKTOPHORZRES)
            finally:
                u.ReleaseDC(None, hdc)
            logical = u.GetSystemMetrics(_SM_CXSCREEN) or 0
            if physical and logical and physical > logical:
                base = int(round(base * physical / logical))
        except Exception:
            pass
        return max(8, min(base, 256))

    def _load_icons(self) -> bool:
        """把两枚图标从 .ico 载入成 HICON。

        尺寸按通知区域的实际像素来(见 _icon_size), 而不是写死 16 —— 我们的
        .ico 里有 16/32/48/256 四档, 让 Windows 自己挑最合适的一档再缩。
        """
        u = _user32
        size = self._icon_size()
        ok = False
        for connected in (True, False):
            try:
                path = _icon.ensure_ico(connected=connected)
            except Exception as exc:
                print(f"[x] 红杏托盘: 生成图标失败 {type(exc).__name__}: {exc}")
                continue
            handle = u.LoadImageW(None, str(path), _IMAGE_ICON, size, size, _LR_LOADFROMFILE)
            if handle:
                self._hicons[connected] = int(handle)
                ok = True
            else:
                print(f"[x] 红杏托盘: 载入图标失败 {path} (err={ctypes.get_last_error()})")
        if not ok:
            return False
        if len(self._hicons) == 1:  # 有一枚没载入就先用另一枚顶着, 别整个挂掉
            only = next(iter(self._hicons.values()))
            self._hicons.setdefault(True, only)
            self._hicons.setdefault(False, only)
        return True

    def _destroy_icons(self) -> None:
        u = _user32
        for handle in set(self._hicons.values()):
            if handle:
                u.DestroyIcon(handle)
        self._hicons.clear()

    def _tip(self) -> str:
        state = "已连接" if self._connected else "未连接"
        return f"{self._tooltip} · {state}"[:127]

    def _notify(self, message: int, flags: int) -> bool:
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = _UID
        nid.uFlags = flags
        nid.uCallbackMessage = _CALLBACK_MSG
        nid.hIcon = self._hicons.get(self._connected) or self._hicons.get(True)
        nid.szTip = self._tip()
        ok = bool(_shell32.Shell_NotifyIconW(message, ctypes.byref(nid)))
        if not ok and message != _NIM_DELETE:
            print(f"[x] 红杏托盘: Shell_NotifyIcon(0x{message:X}) 失败 (err={ctypes.get_last_error()})")
        return ok

    def _teardown(self) -> None:
        if self._hwnd and available():
            try:
                self._notify(_NIM_DELETE, 0)
            except Exception:
                pass
        self._started = False
        try:
            self._destroy_window()
        except Exception:
            pass
        try:
            self._destroy_icons()
        except Exception:
            pass

    # -- 内部: 菜单 --------------------------------------------------------- #

    def _show_menu(self) -> None:
        """弹右键菜单(模态, 会一直阻塞到用户选完或点别处)。"""
        u = _user32
        if not self._hwnd:
            return
        menu = u.CreatePopupMenu()
        if not menu:
            return
        self._last_menu = int(menu)
        self._menu_open = True
        cmd = 0
        try:
            self._append_items(menu)
            pt = _POINT()
            u.GetCursorPos(ctypes.byref(pt))

            # 这两句是配套的, 缺一个菜单就关不掉:
            #   SetForegroundWindow 让菜单属于前台窗口, 点击别处才会产生
            #   WM_ACTIVATE 把它关掉; 之后那个空消息让菜单管理器退出模态循环。
            u.SetForegroundWindow(self._hwnd)
            cmd = u.TrackPopupMenu(
                menu,
                _TPM_RIGHTBUTTON | _TPM_RETURNCMD | _TPM_NONOTIFY,
                pt.x,
                pt.y,
                0,
                self._hwnd,
                None,
            )
            u.PostMessageW(self._hwnd, _WM_NULL, 0, 0)
        finally:
            u.DestroyMenu(menu)
            self._last_menu = None
            self._menu_open = False
        if cmd:
            self._on_command(int(cmd))

    def _append_items(self, menu: int) -> None:
        u = _user32
        toggle_text = "断开连接" if self._connected else "一键连接"
        items = (
            (_MENU_SHOW, "显示主窗口", self._on_show),
            (_MENU_TOGGLE, toggle_text, self._on_toggle),
            (_MENU_BEST, "自动选最优节点", self._on_pick_best),
            (_MENU_QUIT, "退出", self._on_quit),
        )
        for cmd_id, text, handler in items:
            flags = _MF_STRING | (0 if callable(handler) else _MF_GRAYED)
            u.AppendMenuW(menu, flags, cmd_id, text)

    def _on_command(self, cmd_id: int) -> None:
        handlers = {
            _MENU_SHOW: (self._on_show, "显示主窗口"),
            _MENU_TOGGLE: (self._on_toggle, "打开/关闭"),
            _MENU_BEST: (self._on_pick_best, "自动选最优节点"),
            _MENU_QUIT: (self._on_quit, "退出"),
        }
        entry = handlers.get(cmd_id)
        if entry is None:
            return
        self._invoke(entry[0], entry[1])

    def _invoke(self, handler: Callable[[], None] | None, what: str) -> None:
        """调用业务回调。回调里的异常必须在这里被吃掉:

        我们是在 WndProc 里被 Tk 的消息循环调起来的, 异常穿出去轻则打印一堆
        看不懂的栈, 重则让 Tcl 的 notifier 处于未定义状态。托盘宁可少做一件
        事, 也不能把主循环搞崩。
        """
        if not callable(handler):
            return  # 接线层可能传 None(主窗口还没实现该动作)
        try:
            handler()
        except Exception as exc:
            print(f"[x] 红杏托盘: {what} 回调异常 {type(exc).__name__}: {exc}")
            traceback.print_exc()

    # -- 内部: 窗口过程 ----------------------------------------------------- #

    def _wnd_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        u = _user32
        try:
            if msg == _CALLBACK_MSG:
                # lParam 是鼠标消息 id。取低 16 位: 老版本 shell 会带上高位,
                # 而消息 id 本身都在低 16 位里, 屏蔽掉更稳。
                event = lparam & 0xFFFF
                if event in (_WM_RBUTTONUP, _WM_CONTEXTMENU):
                    # 菜单是模态的: 它开着的时候再投一次右键(用户连点/程序重复投递)
                    # 不该叠出第二个菜单 —— 那会让第一个菜单永远收不到选择。
                    if not self._menu_open:
                        self._show_menu()
                elif event in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                    # 双击是契约要求; 单击也打开主窗口 —— 用户点托盘就是想看
                    # 窗口, 让单击"什么都不发生"只会让人以为程序卡了。
                    self._invoke(self._on_show, "显示主窗口")
                return 0

            if self._taskbar_created_msg and msg == self._taskbar_created_msg:
                # Explorer 重启了: 旧图标随 shell 一起没了, 必须重新登记
                self._notify(_NIM_ADD, _NIF_MESSAGE | _NIF_ICON | _NIF_TIP)
                return 0

            if msg == _WM_COMMAND:
                self._on_command(wparam & 0xFFFF)
                return 0

            if msg == _WM_CLOSE:
                u.DestroyWindow(hwnd)
                return 0

            if msg == _WM_DESTROY:
                # 刻意不 PostQuitMessage: 消息泵是 tkinter 的, 往线程队列里塞
                # WM_QUIT 会把主窗口一起干掉。
                self._hwnd = None
                return 0
        except Exception:
            traceback.print_exc()
        return u.DefWindowProcW(hwnd, msg, wparam, lparam)
