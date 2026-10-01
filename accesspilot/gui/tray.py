"""红杏系统托盘: 纯 ctypes 调 Shell_NotifyIconW, 零第三方依赖.

为什么不用 pystray
------------------
pystray 会连带拉进 Pillow(为了画图标), 而本项目的硬约束是**运行时零依赖**:
目标机器常常访问不了 PyPI。托盘需要的 Win32 接口其实只有十来个
(RegisterClassExW / CreateWindowExW / Shell_NotifyIconW / TrackPopupMenu),
ctypes 是标准库, 自己接一遍比引两个第三方包更划算, 也更好排错 —— 出问题时
栈里全是我们自己的代码。

线程模型: 托盘必须有自己的消息泵线程(实测踩过的致命坑)
------------------------------------------------------
最自然的写法是"借用 tkinter 的消息泵": 在 Tk 主线程建隐藏窗口, 反正 mainloop
本来就在泵 Windows 消息。**这个写法会把整个客户端搞死**, 而且是静默的:

  tkinter 在 mainloop 期间是**放开 GIL** 的。Tcl 派发消息时调到我们的
  ctypes WNDPROC, ctypes 会调 PyGILState_Ensure **临时造一个线程状态**,
  回调返回时再 PyGILState_Release 把它删掉。只要回调里再碰一下 Tk
  (after/quit/destroy), 主线程的线程状态就被搅乱, 最后在 mainloop 返回时
  炸成 `Fatal Python error: PyEval_RestoreThread: ... thread state is NULL`。
  进程直接死, 没有 traceback, 用户看到的是"点一下托盘, 程序没了"。

所以这里的选择是: **托盘窗口和它的消息泵跑在一条自己的线程上**,
主线程(Tk)从头到尾不出现任何 ctypes 回调, 线程状态没人能碰。
代价与对策:

* 菜单回调跑在托盘线程上, 因此**回调里绝对不能碰 Tk 控件**。红杏的接线是
  `app.show/hide/toggle/pick_best/quit`, 这些方法内部统一走 `control.run_bg`
  那条"跨线程出口"(`root.after`), 这正是它们被设计成线程安全的原因。
  托盘只负责"把用户的点击翻译成一次方法调用", 不碰界面。
* `TrackPopupMenu` 是模态循环, 现在它只会阻塞托盘线程, 主窗口照常响应。
* 退出/异常路径都由托盘线程自己收尾(NIM_DELETE -> DestroyWindow ->
  UnregisterClass -> DestroyIcon), 不会留下幽灵图标。

菜单为什么会"点不掉"
--------------------
弹菜单前必须 `SetForegroundWindow(我们的窗口)`, 弹完必须往该窗口 Post 一个
空消息。少任何一步, 菜单就会**点不掉也关不掉**, 只能等用户切窗口。这不是
可选优化, 是必需的配对动作。

隐藏窗口的纪律
--------------
* 标题故意不含"红杏/AccessPilot" —— 客户端是单实例的, 它靠"按标题找已存在的
  窗口"把老窗口叫到前台; 一个叫"红杏…"的隐藏窗口会被它当成主窗口认领。
* 带 `WS_EX_TOOLWINDOW`, 不进 Alt+Tab、不进任务栏。
* 只认自己人发的消息: 托盘回调消息要求 wParam 等于自己的图标 id;
  `WM_COMMAND` 只在菜单真的开着时才认(TrackPopupMenu 用的是 TPM_RETURNCMD,
  正常路径根本不会有 WM_COMMAND)。隐藏窗口对路过的任何消息都做出反应,
  是这类"托盘把主程序搞挂"事故的另一个常见根因。
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

#: 隐藏窗口的标题: 刻意不含品牌词, 免得被"按标题找主窗口"的逻辑认领
_SINK_TITLE = "HongXingTraySink"

#: 等托盘线程把图标挂上去的上限。首次运行要现生成 .ico(约 1 秒), 留够余量。
_START_TIMEOUT = 10.0

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
_WS_EX_TOOLWINDOW = 0x00000080
_SM_CXSCREEN = 0
_SM_CXSMICON = 49
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
        """NOTIFYICONDATAW(Vista 之后的完整版, x64 上 976 字节)。

        cbSize 填整个结构体大小 —— 填小了 shell 会按老版本解析, szTip 后面的
        字段全错位。
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

    class _POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class _MSG(ctypes.Structure):
        # 末尾多留一个 DWORD: 系统的 MSG 比 SDK 头文件里写的多一个私有字段,
        # 缓冲区给小了会被 GetMessage 写越界。
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", _WPARAM),
            ("lParam", _LPARAM),
            ("time", wintypes.DWORD),
            ("pt", _POINT),
            ("lPrivate", wintypes.DWORD),
        ]

    try:
        _user32: Any = ctypes.WinDLL("user32", use_last_error=True)
        _shell32: Any = ctypes.WinDLL("shell32", use_last_error=True)
        _kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        _gdi32: Any = ctypes.WinDLL("gdi32", use_last_error=True)
    except OSError:  # 极少数被裁剪过的系统
        _user32 = _shell32 = _kernel32 = _gdi32 = None

    if _user32 is not None:
        _u = _user32
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
        _u.GetMessageW.argtypes = [ctypes.POINTER(_MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        _u.GetMessageW.restype = wintypes.BOOL
        _u.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
        _u.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
        _u.DispatchMessageW.restype = _LRESULT
        _u.PostQuitMessage.argtypes = [ctypes.c_int]
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
        _u.GetForegroundWindow.argtypes = []
        _u.GetForegroundWindow.restype = wintypes.HWND
        _u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        _u.GetWindowThreadProcessId.restype = wintypes.DWORD
        _u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        _u.AttachThreadInput.restype = wintypes.BOOL
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

        _gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
        _gdi32.GetDeviceCaps.restype = ctypes.c_int

        _shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
        _shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        _kernel32.GetCurrentThreadId.argtypes = []
        _kernel32.GetCurrentThreadId.restype = wintypes.DWORD

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

    线程约定(**很重要**):

    * 托盘自己有一条消息泵线程, 窗口和菜单都在那条线程上;
    * 菜单回调因此在**托盘线程**上被调用 —— 回调里不要直接碰 Tk 控件,
      要像 app.py 那样把结果丢回主线程(`root.after` / `control.run_bg`);
    * `start/stop/set_connected/set_tooltip` 可以从任何线程调用,
      `Shell_NotifyIconW` 本身是线程安全的, 内部状态有锁保护。
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

        self._lock = threading.RLock()
        self._connected = False
        self._started = False
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error = ""
        self._hwnd = 0
        self._hicons: dict[bool, int] = {}
        self._class_name = ""
        self._taskbar_created_msg = 0
        self._menu_open = False
        self._last_menu = 0  # 自测脚本用它取菜单句柄
        self._wndproc: Any = None  # 必须留引用, 否则回调对象被 GC 后 WndProc 变野指针

    # -- 对外接口 ----------------------------------------------------------- #

    def start(self) -> bool:
        """挂上托盘图标, 成功返回 True。失败只返回 False, 绝不抛异常。

        这是刻意设计的: 托盘挂不上(远程桌面、安全软件、Explorer 没跑)时,
        红杏应该退化成普通窗口继续可用, 而不是打不开。

        真正的挂载在托盘线程里做, 这里只是等它报结果(首次运行要现生成 .ico,
        所以上限给到 10 秒), 主线程不会因为消息泵被拖住。
        """
        with self._lock:
            if self._started:
                return True
        if not available():
            print("[x] 红杏托盘: 当前环境不支持系统托盘(仅 Windows 可用)")
            return False

        self._ready.clear()
        self._start_error = ""
        thread = threading.Thread(target=self._pump, name="hongxing-tray", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        if not self._ready.wait(_START_TIMEOUT):
            print("[x] 红杏托盘: 启动超时(图标没能挂上)")
            return False
        with self._lock:
            error = self._start_error
            ok = self._started
        if error:
            print(f"[x] 红杏托盘: 挂载失败 {error}")
            return False
        return ok

    def stop(self) -> None:
        """摘掉图标并结束托盘线程 —— 不给用户留一个点不动的幽灵图标。

        可以从任何线程调用; 幂等。
        """
        with self._lock:
            thread, hwnd = self._thread, self._hwnd
            self._started = False
            self._thread = None
        if thread is None:
            return
        if thread is threading.current_thread():
            # 从托盘线程自己调过来(回调里直接 stop): 只能请消息泵收工
            if hwnd:
                _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            return
        if hwnd:
            # 让窗口的拥有者线程自己销毁窗口(Win32 规定跨线程 DestroyWindow 无效)
            _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        thread.join(timeout=5.0)
        if thread.is_alive():  # pragma: no cover - 极端情况下的兜底
            print("[!] 红杏托盘: 托盘线程没能在 5 秒内结束")

    def set_connected(self, connected: bool) -> None:
        """切换"已连接/未连接"两枚图标(契约方法, 等价于 update_menu_state)。"""
        self.update_menu_state(connected=connected)

    def set_tooltip(self, text: str) -> None:
        """改悬停提示。实际提示 = 传入文字 + 当前状态后缀。"""
        with self._lock:
            self._tooltip = text or "红杏"
        self._notify(_NIM_MODIFY, _NIF_TIP)

    def update_menu_state(self, *, connected: bool) -> None:
        """连接状态变化时刷新图标和菜单文案。

        菜单每次弹出都是现建的(见 _show_menu), 所以这里只需要改图标和提示。
        """
        connected = bool(connected)
        with self._lock:
            if connected == self._connected:
                return
            self._connected = connected
        self._notify(_NIM_MODIFY, _NIF_ICON | _NIF_TIP)

    # -- 托盘线程 ----------------------------------------------------------- #

    def _pump(self) -> None:
        """托盘线程主体: 建窗口 -> 挂图标 -> 自己泵消息 -> 收尾清理。"""
        try:
            if not self._create_window() or not self._load_icons():
                self._ready.set()
                self._cleanup()
                return
            if not self._add_icon():
                self._start_error = "Shell_NotifyIcon(NIM_ADD) 失败"
                self._ready.set()
                self._cleanup()
                return
            with self._lock:
                self._started = True
            self._ready.set()
        except Exception as exc:  # 任何意外都退化成"没有托盘"
            self._start_error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            self._ready.set()
            self._cleanup()
            return

        # 自己的消息泵: 只服务自己的窗口, 不碰 Tk 的队列。
        msg = _MSG()
        try:
            while True:
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):  # 0 = WM_QUIT, -1 = 出错
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            traceback.print_exc()
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        """托盘线程的收尾: 摘图标 -> 销毁窗口 -> 注销类 -> 释放图标句柄。

        顺序不能反: NIM_DELETE 必须在把 _hwnd 清成 0 **之前**做, 否则
        shell 里会留下一个点不动的幽灵图标(用户只能重启 Explorer)。
        """
        with self._lock:
            hwnd = self._hwnd
            self._started = False
        if hwnd and available():
            try:
                self._notify(_NIM_DELETE, 0)
            except Exception:
                pass
        with self._lock:
            self._hwnd = 0
        if hwnd:
            try:
                if _user32.IsWindow(hwnd):
                    _user32.DestroyWindow(hwnd)
            except Exception:
                pass
        if self._class_name:
            try:
                _user32.UnregisterClassW(self._class_name, _kernel32.GetModuleHandleW(None))
            except Exception:
                pass
            self._class_name = ""
        with self._lock:
            handles = set(self._hicons.values())
            self._hicons.clear()
        for handle in handles:
            try:
                _user32.DestroyIcon(handle)
            except Exception:
                pass

    # -- 窗口 --------------------------------------------------------------- #

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
            self._start_error = f"RegisterClassExW 失败 (err={ctypes.get_last_error()})"
            return False

        # 普通隐藏窗口, 刻意**不用** HWND_MESSAGE 消息窗口:
        # 消息窗口不能成为前台窗口, SetForegroundWindow 会失败, 菜单又会掉进
        # "点了不消失"的坑里。
        # WS_EX_TOOLWINDOW: 不占 Alt+Tab、不占任务栏 —— 它只是收消息的。
        hwnd = u.CreateWindowExW(
            _WS_EX_TOOLWINDOW, self._class_name, _SINK_TITLE, 0, 0, 0, 0, 0, None, None, wc.hInstance, None
        )
        if not hwnd:
            self._start_error = f"CreateWindowExW 失败 (err={ctypes.get_last_error()})"
            return False
        with self._lock:
            self._hwnd = int(hwnd)

        # Explorer 重启后会广播这条注册消息, 收到就得重新 NIM_ADD, 否则图标
        # 永远消失(用户只能重启客户端)。
        self._taskbar_created_msg = u.RegisterWindowMessageW("TaskbarCreated")
        return True

    # -- 图标与 shell ------------------------------------------------------- #

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
        """把两枚图标从 .ico 载入成 HICON(在托盘线程里做)。"""
        u = _user32
        size = self._icon_size()
        loaded: dict[bool, int] = {}
        for connected in (True, False):
            try:
                path = _icon.ensure_ico(connected=connected)
            except Exception as exc:
                print(f"[x] 红杏托盘: 生成图标失败 {type(exc).__name__}: {exc}")
                continue
            handle = u.LoadImageW(None, str(path), _IMAGE_ICON, size, size, _LR_LOADFROMFILE)
            if handle:
                loaded[connected] = int(handle)
            else:
                print(f"[x] 红杏托盘: 载入图标失败 {path} (err={ctypes.get_last_error()})")
        if not loaded:
            self._start_error = "两枚图标都没能载入"
            return False
        if len(loaded) == 1:  # 有一枚没载入就先用另一枚顶着, 别整个挂掉
            only = next(iter(loaded.values()))
            loaded.setdefault(True, only)
            loaded.setdefault(False, only)
        with self._lock:
            self._hicons = loaded
        return True

    def _add_icon(self) -> bool:
        """NIM_ADD。Explorer 刚重启/刚登录时 taskbar 可能还没准备好, 重试两次。"""
        flags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        if self._notify(_NIM_ADD, flags):
            return True
        for _ in range(2):
            time.sleep(0.25)
            if self._notify(_NIM_ADD, flags):
                return True
        return False

    def _tip(self) -> str:
        with self._lock:
            base, connected = self._tooltip, self._connected
        return f"{base} · {'已连接' if connected else '未连接'}"[:127]

    def _notify(self, message: int, flags: int) -> bool:
        with self._lock:
            hwnd, connected = self._hwnd, self._connected
            hicon = self._hicons.get(connected) or next(iter(self._hicons.values()), 0)
        if not hwnd:
            return False
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = _UID
        nid.uFlags = flags
        nid.uCallbackMessage = _CALLBACK_MSG
        nid.hIcon = hicon
        nid.szTip = self._tip()
        ok = bool(_shell32.Shell_NotifyIconW(message, ctypes.byref(nid)))
        if not ok and message != _NIM_DELETE:
            print(f"[x] 红杏托盘: Shell_NotifyIcon(0x{message:X}) 失败 (err={ctypes.get_last_error()})")
        return ok

    # -- 菜单(全部在托盘线程上) -------------------------------------------- #

    def _show_menu(self) -> None:
        """弹右键菜单(模态, 阻塞的是托盘线程, 不是界面线程)。"""
        u = _user32
        with self._lock:
            hwnd = self._hwnd
        if not hwnd:
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
            u.SetForegroundWindow(hwnd)
            if u.GetForegroundWindow() != hwnd:
                self._force_foreground(hwnd)
            cmd = u.TrackPopupMenu(
                menu,
                _TPM_RIGHTBUTTON | _TPM_RETURNCMD | _TPM_NONOTIFY,
                pt.x,
                pt.y,
                0,
                hwnd,
                None,
            )
            u.PostMessageW(hwnd, _WM_NULL, 0, 0)
        finally:
            u.DestroyMenu(menu)
            self._last_menu = 0
            self._menu_open = False
        if cmd:
            self._on_command(int(cmd))

    def _force_foreground(self, hwnd: int) -> bool:
        """把我们的窗口顶到前台; 顶不上去就借前台线程的输入队列。

        为什么需要这一手: 菜单不是前台窗口时, 系统会**立刻把它取消** —— 用户
        看到的是"右键点了托盘, 什么都没弹"。正常路径(用户真的点了托盘图标)
        Shell 会把前台权给我们, 所以平时轮不到这里; 但在"前台被别的程序抢走"
        或者自测脚本直接投递消息时就会踩到。AttachThreadInput 是托盘菜单的
        标准绕法: 临时附到前台线程的输入队列上, 借它的前台权。
        """
        u = _user32
        try:
            if u.SetForegroundWindow(hwnd) and u.GetForegroundWindow() == hwnd:
                return True
            fg = u.GetForegroundWindow()
            tid_fg = u.GetWindowThreadProcessId(fg, None) if fg else 0
            tid_me = _kernel32.GetCurrentThreadId()
            if not tid_fg or tid_fg == tid_me:
                return False
            if not u.AttachThreadInput(tid_me, tid_fg, True):
                return False
            try:
                u.SetForegroundWindow(hwnd)
            finally:
                u.AttachThreadInput(tid_me, tid_fg, False)
            return u.GetForegroundWindow() == hwnd
        except Exception:
            return False

    def _append_items(self, menu: int) -> None:
        u = _user32
        with self._lock:
            connected = self._connected
        toggle_text = "断开连接" if connected else "一键连接"
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
        """调用业务回调。

        运行在**托盘线程**上, 所以这里的纪律是:
          * 不碰 Tk 控件(接线层的方法自己会 marshal 回主线程);
          * 回调抛出的异常必须在这里吃掉 —— 它逃到 WndProc 外面会污染
            消息泵, 之后托盘就再也不响应了, 而用户只会看到"点了没反应"。
        """
        if not callable(handler):
            return  # 接线层可能传 None(主窗口还没实现该动作)
        try:
            handler()
        except Exception as exc:
            print(f"[x] 红杏托盘: {what} 回调异常 {type(exc).__name__}: {exc}")
            traceback.print_exc()

    # -- 窗口过程(托盘线程) ------------------------------------------------- #

    def _wnd_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        u = _user32
        try:
            if msg == _CALLBACK_MSG:
                # wParam 必须是本图标自己的 id。不加这道校验的话, 任何一条
                # 碰巧撞上 WM_APP+1 的消息都会被当成"用户点了托盘"。
                if wparam != _UID:
                    return 0
                # lParam 是鼠标消息 id。取低 16 位: 老版本 shell 会带上高位,
                # 而消息 id 本身都在低 16 位里, 屏蔽掉更稳。
                event = lparam & 0xFFFF
                if event in (_WM_RBUTTONUP, _WM_CONTEXTMENU):
                    # 菜单是模态的: 它开着的时候再投一次右键(用户连点/程序重复
                    # 投递)不该叠出第二个菜单, 那会让第一个永远收不到选择。
                    if not self._menu_open:
                        self._show_menu()
                elif event in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                    # 双击是契约要求; 单击也打开主窗口 —— 用户点托盘就是想看
                    # 窗口, 让单击"什么都不发生"只会让人以为程序卡了。
                    self._invoke(self._on_show, "显示主窗口")
                return 0

            if self._taskbar_created_msg and msg == self._taskbar_created_msg:
                # Explorer 重启了: 旧图标随 shell 一起没了, 必须重新登记
                self._add_icon()
                return 0

            if msg == _WM_COMMAND:
                # 只在菜单真的开着时才认: 我们用的是 TPM_RETURNCMD(菜单项 id
                # 由 TrackPopupMenu 直接返回), 正常路径根本不会有 WM_COMMAND。
                # 不加这个判断, 任何一条 wParam 低 16 位等于菜单 id 的杂散
                # WM_COMMAND 都能凭空触发"退出程序"。
                if self._menu_open:
                    self._on_command(wparam & 0xFFFF)
                return 0

            if msg == _WM_CLOSE:
                # 消息泵是我们自己的, 这里 DestroyWindow 是正常收尾路径:
                # WM_DESTROY 里 PostQuitMessage 结束循环, 线程再做 NIM_DELETE。
                u.DestroyWindow(hwnd)
                return 0

            if msg == _WM_DESTROY:
                u.PostQuitMessage(0)
                return 0
        except Exception:
            traceback.print_exc()
        return u.DefWindowProcW(hwnd, msg, wparam, lparam)
