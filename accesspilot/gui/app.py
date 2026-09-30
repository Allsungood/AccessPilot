"""红杏主窗口(Tkinter, 零第三方依赖).

一屏之内办完四件事: 开关、换节点、看自检、改设置。用户是不懂代理的普通人,
所以这里有三条贯穿全文件的取舍:

* 顶部只有一个大圆钮, 亮蓝=已连接 / 灰=未连接。用户不需要知道什么是节点、
  什么是系统代理 —— 那是引擎的事。
* 每个开关后面都跟一句人话解释, 不让 "TUN / 分流 / 延迟" 这类术语裸奔。
* 任何失败都必须**看得见**: 错误条 + 底部状态栏, 绝不静默吞掉。

为什么业务逻辑一行都不写在这里
==============================
界面只调 accesspilot/control.py。引擎换实现、加接口, 这里不用跟着改;
control 层保证"可预期的失败返回带 error 的结果而不是抛异常", 界面就只负责
把 error 画出来。因此本文件里**没有** import process / sysproxy / api。

线程模型(这里最容易踩坑)
========================
tkinter 不是线程安全的。所有会阻塞的调用(snapshot / turn_on / list_nodes /
test_platforms / pick_best_node / set_autostart)一律走 control.run_bg 丢进
后台线程, 再用 root.after(0, ...) 把结果搬回主线程 —— **只有主线程碰控件**。
后台线程直接改控件, 在 Tk 上表现为随机崩溃或界面静默卡死, 而且不留
traceback, 事后极难定位, 所以本文件把所有跨线程回主线程的动作都收敛到
`App._call_soon` 一个出口上, 不散落 after 调用。

托盘由 gui/__init__.py 接线, 本模块**不 import tray**: 没有托盘时要能退化成
普通窗口(关窗即退出), 而不是变成一个没有入口的隐藏进程。
"""
from __future__ import annotations

import sys
import tkinter as tk
import traceback
from tkinter import ttk
from types import TracebackType
from typing import Any, Callable

from .. import control
from . import theme

#: 每个工作模式对应的一句人话解释。切换模式时显示在单选下方 ——
#: 用户看不懂 "rule/global/direct", 但看得懂"只有被墙的网站走代理"。
MODE_HINTS: dict[str, str] = {
    "rule": "只有被墙的网站走代理，国内网站直连。（推荐）",
    "global": "所有网站都走代理，国外站点最稳，国内会变慢。",
    "direct": "完全不使用代理，用来排查“是不是代理的问题”。",
}

#: 节点列表一次最多拉多少个。免费池有六千个节点, 全渲染进 Treeview 会卡,
#: 而用户只关心最快的那几十个。
NODE_LIMIT = 200

#: 前台/后台的轮询间隔。control.snapshot() 实测约 82ms(内核满载时),
#: 前台 2 秒一次够用; 藏到托盘后没人看, 降到 10 秒, 省掉无谓的后台负载。
POLL_VISIBLE_MS = 2000
POLL_HIDDEN_MS = 10000


def _poll_payload() -> tuple[Any, dict[str, Any]]:
    """一次轮询要取回的全部状态。

    为什么把 health_state 和 snapshot 放同一个工作线程: 两者要一起渲染,
    分两次 run_bg 会出现"开关已经亮了, 自动切换还显示旧的"这种中间态。
    health_state 在 health.py 还没落地时是纯本地兜底(不会拖慢轮询), 但它
    万一抛了异常也不能让整屏状态作废, 所以单独兜住。
    """
    status = control.snapshot()
    try:
        health = control.health_state()
    except Exception:  # noqa: PERF203
        health = {}
    return status, health


class App:
    """红杏主窗口。构造不阻塞、不进 mainloop, 慢操作全部丢后台。"""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(f"{control.BRAND_NAME} · {control.BRAND_TAGLINE}")
        self.root.configure(bg=theme.BG)
        # 关窗 = 隐藏到托盘; 没有托盘时退化成真退出, 见 _on_close。
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Tk 回调里逃出来的异常默认只打 stderr —— 打包成 exe 后没有 stderr,
        # 用户只会看到"点了没反应"。挂上自己的处理器, 让它出现在错误条上。
        self.root.report_callback_exception = self._on_callback_error

        # ---- 运行时状态 ----
        self._closing = False
        self._hidden = False
        self._task_busy = False
        self._nodes_loading = False
        self._poll_busy = False
        self._poll_job: str | None = None
        self._tray: Any = None
        self._tray_connected: bool | None = None
        self._tray_tip = ""
        self._status: Any = control.Status()
        self._nodes: list[Any] = []
        self._node_iids: dict[str, str] = {}
        self._node_latency: dict[str, int] = {}
        self._current_iid: str | None = None
        self._switch_key: tuple[bool, bool, str] | None = None
        self._last_error = ""
        self._health_note = ""
        #: 自动切换已经帮用户换过几次节点(health_state 的 switches)
        self._health_switches = 0
        #: 程序化改控件时置位, 防止把自己的更新当成用户操作又回调一次。
        self._syncing = False

        self._build()
        # 先藏起来, 等 run() 里全部布局完成再显示 —— 免得用户看到一个
        # 控件还没摆好的半成品窗口。
        self.root.withdraw()

    # ------------------------------------------------------------------ #
    # 界面搭建
    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        theme.setup(self.root)
        self.var_mode = tk.StringVar(master=self.root, value=self._status.mode)
        self.var_autostart = tk.BooleanVar(master=self.root, value=False)
        self.var_tun = tk.BooleanVar(master=self.root, value=False)
        self.var_failover = tk.BooleanVar(master=self.root, value=False)

        outer = ttk.Frame(self.root, style="App.TFrame", padding=(14, 12, 14, 10))
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        outer.columnconfigure(0, minsize=336)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)

        self._build_header(outer)
        self._build_left(outer)
        self._build_tabs(outer)
        self._build_error_bar(outer)
        self._build_status_bar(outer)

        self._action_widgets: list[Any] = [
            self.btn_pick, self.btn_refresh, self.btn_check, self.btn_verify,
        ]
        self._action_widgets += list(self.radios)
        self._action_widgets += [self.chk_autostart, self.chk_tun, self.chk_failover]

    def _build_header(self, outer: ttk.Frame) -> None:
        head = ttk.Frame(outer, style="App.TFrame")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        head.columnconfigure(2, weight=1)
        ttk.Label(head, text=control.BRAND_NAME, style="Brand.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(head, text=control.BRAND_TAGLINE, style="Hint.TLabel").grid(
            row=0, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(head, text=f"v{control.BRAND_VERSION}", style="Hint.TLabel").grid(
            row=0, column=3, sticky="e", pady=(8, 0))

    def _build_left(self, outer: ttk.Frame) -> None:
        left = ttk.Frame(outer, style="App.TFrame")
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        left.columnconfigure(0, weight=1)
        # 内容固定在顶部, 多出来的高度给空白, 免得控件被拉得东一块西一块。
        left.rowconfigure(4, weight=1)

        # ---- 大圆钮 ----
        self.switch = tk.Canvas(left, width=106, height=106, highlightthickness=0,
                                bg=theme.BG, takefocus=1, cursor="hand2")
        self.switch.grid(row=0, column=0)
        self.switch.bind("<Button-1>", lambda _e: self._on_switch_click())
        self.switch.bind("<Return>", lambda _e: self._on_switch_click())
        self.switch.bind("<space>", lambda _e: self._on_switch_click())
        self.switch_hint = ttk.Label(left, text="正在读取状态…", style="Hint.TLabel",
                                     anchor="center", wraplength=320,
                                     justify="center")
        self.switch_hint.grid(row=1, column=0, sticky="ew", pady=(0, 6))

        # ---- 当前状态 ----
        card = ttk.Labelframe(left, text=" 当前状态 ", padding=(10, 3, 10, 7))
        card.grid(row=2, column=0, sticky="ew")
        self.lbl_node = ttk.Label(card, text="—", style="Value.TLabel")
        self.lbl_latency = ttk.Label(card, text="—", style="Value.TLabel")
        self.lbl_count = ttk.Label(card, text="0", style="Value.TLabel")
        self.lbl_ai = ttk.Label(card, text="尚未验证", style="CardHint.TLabel")
        self._grid_kv(card, 0, "当前节点", self.lbl_node, span=3)
        self._grid_kv(card, 1, "延迟", self.lbl_latency)
        self._grid_kv(card, 1, "节点总数", self.lbl_count, col=2)
        self._grid_kv(card, 2, "ChatGPT", self.lbl_ai, span=3)

        # ---- 工作模式 ----
        mode = ttk.Labelframe(left, text=" 工作模式 ", padding=(10, 3, 10, 7))
        mode.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        mode.columnconfigure(0, weight=1)
        self.radios: list[ttk.Radiobutton] = []
        # 三个选项**横着排**: 1280x720 的屏幕上竖排会把底部的"设置"挤出窗口,
        # 而设置里的开机自启是用户最常找的开关之一。
        for i, (key, label) in enumerate(control.MODES.items()):
            rb = ttk.Radiobutton(mode, text=label, value=key, variable=self.var_mode,
                                 command=self._on_mode_click)
            rb.grid(row=0, column=i, sticky="w", padx=(0, 12))
            self.radios.append(rb)
        self.mode_hint = ttk.Label(mode, text=MODE_HINTS[self._status.mode],
                                   style="CardHint.TLabel", wraplength=300,
                                   justify="left")
        self.mode_hint.grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))

        # ---- 设置 ----
        box = ttk.Labelframe(left, text=" 设置 ", padding=(10, 3, 10, 7))
        box.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        box.columnconfigure(0, weight=1)
        self.chk_autostart = ttk.Checkbutton(
            box, text="开机自启（登录后自动在后台运行）", variable=self.var_autostart,
            command=self._on_autostart_click)
        self.chk_tun = ttk.Checkbutton(
            box, text="TUN 全局接管（需管理员权限）", variable=self.var_tun,
            command=self._on_tun_click)
        self.chk_failover = ttk.Checkbutton(
            box, text="自动切换节点（掉线时自动换一个）", variable=self.var_failover,
            command=self._on_failover_click)
        for i, chk in enumerate((self.chk_autostart, self.chk_tun, self.chk_failover)):
            chk.grid(row=i, column=0, sticky="w")
        self.settings_note = ttk.Label(box, text="TUN 打开后，所有程序都会走代理。",
                                       style="CardHint.TLabel", wraplength=300,
                                       justify="left")
        self.settings_note.grid(row=3, column=0, sticky="w", pady=(3, 0))

    @staticmethod
    def _grid_kv(card: ttk.Labelframe, row: int, key: str, value: ttk.Label,
                 *, col: int = 0, span: int = 1) -> None:
        """状态卡片里的一对"名称: 值"。

        名称列的宽度交给 grid 自己算(取同列最宽的那个), 不写死像素 ——
        用户机器上的字体宽度不一样, 写死了要么挤要么空一大块。
        """
        lbl = ttk.Label(card, text=key, style="Key.TLabel", anchor="w")
        lbl.grid(row=row, column=col, sticky="w", pady=1, padx=(0, 8))
        value.grid(row=row, column=col + 1, columnspan=span, sticky="w", pady=1,
                   padx=(0, 8))

    def _build_tabs(self, outer: ttk.Frame) -> None:
        nb = ttk.Notebook(outer)
        nb.grid(row=1, column=1, sticky="nsew")
        self._build_nodes_tab(nb)
        self._build_check_tab(nb)

    def _build_nodes_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, style="Card.TFrame", padding=10)
        nb.add(tab, text="节点")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        bar = ttk.Frame(tab, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(2, weight=1)
        self.btn_refresh = ttk.Button(bar, text="刷新节点", command=self._do_refresh_nodes)
        self.btn_refresh.grid(row=0, column=0)
        self.btn_pick = ttk.Button(bar, text="自动选最优", style="Accent.TButton",
                                   command=self.pick_best)
        self.btn_pick.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(bar, text="双击某一行，就把它设为当前出口",
                  style="CardHint.TLabel").grid(row=0, column=2, sticky="e")

        self.tree_nodes = ttk.Treeview(
            tab, columns=("name", "ms", "cur", "ai"), show="headings",
            selectmode="browse")
        for col, text, width, anchor in (
            ("name", "节点", 300, "w"), ("ms", "延迟", 80, "center"),
            ("cur", "当前", 56, "center"), ("ai", "ChatGPT", 84, "center"),
        ):
            self.tree_nodes.heading(col, text=text, anchor=anchor)
            self.tree_nodes.column(col, width=width, anchor=anchor,
                                   stretch=(col == "name"))
        self.tree_nodes.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.tree_nodes.tag_configure("current", background=theme.ROW_CURRENT)
        self.tree_nodes.bind("<Double-1>", self._on_node_double_click)
        self.tree_nodes.bind("<Return>", self._on_node_double_click)
        sb = ttk.Scrollbar(tab, orient="vertical", command=self.tree_nodes.yview)
        sb.grid(row=2, column=1, sticky="ns", pady=(8, 0))
        self.tree_nodes.configure(yscrollcommand=sb.set)

        self.nodes_note = ttk.Label(tab, text="正在获取节点列表…",
                                    style="CardHint.TLabel")
        self.nodes_note.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _build_check_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, style="Card.TFrame", padding=10)
        nb.add(tab, text="平台自检")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        bar = ttk.Frame(tab, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(2, weight=1)
        self.btn_check = ttk.Button(bar, text="开始自检", style="Accent.TButton",
                                    command=self._do_selfcheck)
        self.btn_check.grid(row=0, column=0)
        self.btn_verify = ttk.Button(bar, text="验证 ChatGPT 出口",
                                     command=self._do_verify_ai)
        self.btn_verify.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(bar, text="自检会真的去访问这些平台，需要几十秒",
                  style="CardHint.TLabel").grid(row=0, column=2, sticky="e")

        self.tree_checks = ttk.Treeview(
            tab, columns=("name", "ok", "ms", "detail"), show="headings",
            selectmode="browse")
        for col, text, width, anchor in (
            ("name", "平台", 150, "w"), ("ok", "结果", 72, "center"),
            ("ms", "延迟", 76, "center"), ("detail", "说明", 320, "w"),
        ):
            self.tree_checks.heading(col, text=text, anchor=anchor)
            self.tree_checks.column(col, width=width, anchor=anchor,
                                    stretch=(col == "detail"))
        self.tree_checks.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        self.tree_checks.tag_configure("ok", foreground=theme.OK)
        self.tree_checks.tag_configure("bad", foreground=theme.DANGER)
        sb = ttk.Scrollbar(tab, orient="vertical", command=self.tree_checks.yview)
        sb.grid(row=2, column=1, sticky="ns", pady=(8, 0))
        self.tree_checks.configure(yscrollcommand=sb.set)

        self.check_note = ttk.Label(tab, text="点「开始自检」看看哪些平台能通。",
                                    style="CardHint.TLabel")
        self.check_note.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _build_error_bar(self, outer: ttk.Frame) -> None:
        # 错误条平时不占位置(grid_remove), 出事时才出现 —— 而不是永远留一条
        # 空白的红框吓唬用户。
        self.error_bar = ttk.Frame(outer, style="Error.TFrame", padding=(10, 3))
        self.error_bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.error_bar.columnconfigure(0, weight=1)
        self.lbl_error = ttk.Label(self.error_bar, text="", style="Error.TLabel",
                                   wraplength=880, justify="left")
        self.lbl_error.grid(row=0, column=0, sticky="w")
        ttk.Button(self.error_bar, text="知道了", style="Small.TButton",
                   command=self._clear_error).grid(
            row=0, column=1, sticky="e", padx=(10, 0))
        self.error_bar.grid_remove()

    def _build_status_bar(self, outer: ttk.Frame) -> None:
        bar = ttk.Frame(outer, style="App.TFrame")
        bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text=f"{control.BRAND_NAME} v{control.BRAND_VERSION}",
                  style="Status.TLabel").grid(row=0, column=0, sticky="w")
        self.lbl_status = ttk.Label(bar, text="正在读取状态…", style="Status.TLabel")
        self.lbl_status.grid(row=0, column=2, sticky="e")
        self.lbl_status_err = ttk.Label(bar, text="", style="Bad.TLabel")
        self.lbl_status_err.grid(row=0, column=3, sticky="e", padx=(10, 0))

    # ------------------------------------------------------------------ #
    # 对外接口(托盘和 gui/__init__.py 用)
    # ------------------------------------------------------------------ #

    def run(self, tray: Any = None) -> int:
        """建窗口并进 mainloop, 返回进程退出码。"""
        self._tray = tray
        self._sync_tray(force=True)
        self._place_window()
        self.root.deiconify()
        self._bring_to_front()
        self._render_switch()
        # 首屏立刻来一次状态 + 节点列表, 都走后台线程: 主循环必须马上转起来,
        # 否则双击 exe 后会先卡几秒才出窗口。
        self._schedule_poll(150)
        self._load_nodes()
        try:
            self.root.mainloop()
        finally:
            self._closing = True
        return 0

    # ---- 把窗口顶到最前面 ---------------------------------------------- #
    #
    # Windows 有"前台锁": SetForegroundWindow 只对已经拥有前台的进程放行,
    # 别的进程调用会被系统忽略(只闪一下任务栏)。双击 exe 启动时, 我们的进程
    # 显然不在前台, 于是窗口乖乖排在浏览器后面 —— 用户看到的是"双击了没反应",
    # 对一键客户端来说这是最致命的失败模式(实测: 只 deiconify+lift 时, 屏幕上
    # 一直是被最大化的浏览器盖着)。
    #
    # 可靠做法是**短暂置顶再取消**: 置顶(-topmost)不受前台锁限制, 取消置顶后
    # 窗口自然留在最前。250ms 足够它被系统绘制出来, 又不会永远压着别人。

    def _bring_to_front(self) -> None:
        for fn in (self.root.lift, self.root.focus_force, self._force_foreground):
            try:
                fn()
            except tk.TclError:  # pragma: no cover - 窗口已销毁
                pass
            except Exception:  # noqa: PERF203 pragma: no cover
                pass
        try:
            self.root.attributes("-topmost", True)
            self.root.after(250, self._drop_topmost)
        except tk.TclError:  # pragma: no cover
            pass

    def _drop_topmost(self) -> None:
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:  # pragma: no cover
            pass

    def _native_hwnd(self) -> int:
        """取真正的顶层窗口句柄, 取不到返回 0。

        坑: Windows 上 `winfo_id()` 返回的是 Tk 内部那个子窗口, 拿它去调
        SetForegroundWindow 不生效(或作用在错误的窗口上)。必须用 GetAncestor
        一路找到顶层窗口。
        """
        try:
            import ctypes

            hwnd = int(self.root.winfo_id())
            top = int(ctypes.windll.user32.GetAncestor(hwnd, 2) or 0)  # GA_ROOT
            return top or hwnd
        except Exception:  # noqa: PERF203 - 非 Windows 或 Tk 未就绪
            return 0

    def _force_foreground(self) -> None:
        """ctypes 兜底: 直接把顶层窗口提到前台。失败也不影响主流程。"""
        hwnd = self._native_hwnd()
        if not hwnd:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        except Exception:  # noqa: PERF203 pragma: no cover
            pass

    def show(self) -> None:
        """显示并前置窗口(托盘双击)。可能被托盘线程调用。"""
        self._call_soon(self._do_show)

    def hide(self) -> None:
        """隐藏到托盘。可能被托盘线程调用。"""
        self._call_soon(self._do_hide)

    def quit(self) -> None:
        """真正退出进程(托盘菜单)。可能被托盘线程调用。"""
        self._call_soon(self._do_quit)

    def toggle(self) -> None:
        """一键开关(托盘菜单/大圆钮共用)。"""
        self._call_soon(self._do_toggle)

    def pick_best(self) -> None:
        """自动选最优节点(托盘菜单/按钮共用)。"""
        self._call_soon(self._do_pick_best)

    def set_tray(self, tray: Any) -> None:
        """保存托盘引用, 之后连接状态变化时同步图标。"""
        self._tray = tray
        self._tray_connected = None
        self._sync_tray(force=True)

    # ------------------------------------------------------------------ #
    # 主线程动作
    # ------------------------------------------------------------------ #

    def _do_show(self) -> None:
        self._hidden = False
        try:
            self.root.deiconify()
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            return
        # 从托盘回来时同样要抢前台: 用户点了托盘图标却看到一个躲在浏览器
        # 后面的窗口, 和"没反应"没区别。
        self._bring_to_front()
        # 从托盘回到前台时立刻刷一次: 藏着的这段时间状态可能已经变了,
        # 让用户第一眼看到的就是真的。
        self._poll_now()

    def _do_hide(self) -> None:
        self._hidden = True
        try:
            self.root.withdraw()
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            pass

    def _do_quit(self) -> None:
        self._closing = True
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:  # noqa: PERF203
                pass
            self._poll_job = None
        try:
            self.root.quit()
        except tk.TclError:  # pragma: no cover
            pass
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    def _on_close(self) -> None:
        """点标题栏的 X。

        有托盘就只隐藏 —— 用户以为关掉了, 其实还在后台保护网络, 这正是
        这类客户端该有的行为。没有托盘时**必须真退出**: 否则窗口没了又
        没有入口再打开, 进程就变成一个用户杀不掉的幽灵。
        """
        if self._tray is None:
            self._do_quit()
        else:
            self._do_hide()
            self._set_hint("已最小化到托盘，双击托盘图标可以再打开。")

    def _on_switch_click(self) -> None:
        if self._task_busy:
            self._set_hint("正在处理上一个操作，请稍候…")
            return
        self._do_toggle()

    def _do_toggle(self) -> None:
        target_on = not bool(self._status.connected)
        label = "正在连接…" if target_on else "正在断开…"
        ok = "已连接，可以用了" if target_on else "已断开"
        self._status_task(label, control.toggle, ok_hint=ok)

    # ---- 模式 / 设置 ----

    def _on_mode_click(self) -> None:
        if self._syncing:
            return
        mode = str(self.var_mode.get())
        if mode == self._status.mode:
            return
        self.mode_hint.configure(text=MODE_HINTS.get(mode, ""))
        label = control.MODES.get(mode, mode)
        self._status_task(
            f"正在切换到「{label}」…",
            lambda: control.set_mode(mode),
            ok_hint=f"已切换到「{label}」",
            after=self._revert_setting_widgets,
        )

    def _on_autostart_click(self) -> None:
        if self._syncing:
            return
        self._do_autostart(bool(self.var_autostart.get()))

    def _do_autostart(self, on: bool) -> None:
        frozen = bool(getattr(sys, "frozen", False))

        def work() -> dict[str, Any]:
            # exe 版本让计划任务直接指向 exe 自己, 用户机器上就不需要装 Python;
            # 源码运行时留空, control 会用当前解释器。
            res = control.set_autostart(on, exe=sys.executable if frozen else None)
            # 自启状态在 snapshot 里有 30 秒缓存, 不清掉界面会继续显示旧值,
            # 用户会以为"点了没用"。
            control.invalidate_cache()
            return res

        self._run_task("正在设置开机自启…", work, on_done=self._autostart_done)

    def _autostart_done(self, res: Any) -> None:
        err = (res or {}).get("error") if isinstance(res, dict) else None
        if err:
            self._revert_setting_widgets()
            self._show_error(f"开机自启设置失败：{err}")
        else:
            self._clear_error()
            self._set_hint("开机自启已开启，下次开机自动在后台运行。"
                           if self.var_autostart.get() else "已关闭开机自启。")
        self._poll_now()

    def _on_tun_click(self) -> None:
        if self._syncing:
            return
        on = bool(self.var_tun.get())
        if on and not self._status.running:
            self._revert_setting_widgets()
            self._show_error("TUN 要在连接之后才能打开：请先点左边的大圆钮连接。")
            return
        # TUN 需要驱动/管理员权限, 不是每台机器都能用。先问清楚再动内核,
        # 免得用户点了一下等半天只等到一句看不懂的报错。
        self._run_task("正在检查这台机器能不能用 TUN…", control.tun_available,
                       on_done=lambda res: self._tun_checked(on, res))

    def _tun_checked(self, on: bool, res: Any) -> None:
        ok, why = res if isinstance(res, tuple) and len(res) == 2 else (False, str(res))
        if not ok:
            self._revert_setting_widgets()
            self._show_error(
                f"这台机器用不了 TUN 全局接管：{why or '原因未知'}。"
                "（常见原因：红杏没有以管理员身份运行）")
            return
        self._status_task(
            "正在切换 TUN（要重启内核，可能十几秒）…",
            lambda: control.set_tun(on),
            ok_hint="TUN 全局接管已开启" if on else "TUN 全局接管已关闭",
            after=self._revert_setting_widgets,
        )

    def _on_failover_click(self) -> None:
        if self._syncing:
            return
        on = bool(self.var_failover.get())
        self._status_task(
            "正在设置自动切换…",
            lambda: control.set_auto_failover(on),
            ok_hint="自动切换已开启" if on else "已关闭自动切换",
            after=self._revert_setting_widgets,
        )

    def _revert_setting_widgets(self, _st: Any = None) -> None:
        """把控件拉回引擎里的真实值。操作失败后必须调, 否则勾选框会一直
        显示用户点了但没生效的状态 —— 那是最容易让人误判的一种界面谎言。"""
        self._sync_widgets(self._status)

    # ---- 节点 ----

    def _do_refresh_nodes(self) -> None:
        self._load_nodes(manual=True)

    def _load_nodes(self, *, manual: bool = False) -> None:
        """拉节点列表。必须在后台线程 —— 免费池六千个节点时响应有几 MB。"""
        if self._nodes_loading or self._closing:
            return
        self._nodes_loading = True
        if manual or not self._nodes:
            self.nodes_note.configure(text="正在获取节点列表…")
        control.run_bg(control.list_nodes, limit=NODE_LIMIT,
                       on_done=self._nodes_ready, on_error=self._nodes_failed)

    def _nodes_ready(self, nodes: Any) -> None:
        self._call_soon(lambda: self._render_nodes(nodes if isinstance(nodes, list) else []))

    def _nodes_failed(self, exc: BaseException) -> None:
        self._call_soon(lambda: self._nodes_failed_ui(exc))

    def _nodes_failed_ui(self, exc: BaseException) -> None:
        self._nodes_loading = False
        self.nodes_note.configure(text="节点列表获取失败")
        self._show_error(f"获取节点列表失败：{exc}")

    def _render_nodes(self, nodes: list[Any]) -> None:
        self._nodes_loading = False
        self._nodes = nodes
        self._node_iids = {}
        self._node_latency = {}
        tree = self.tree_nodes
        children = tree.get_children()
        if children:
            tree.delete(*children)
        for idx, n in enumerate(nodes):
            iid = f"n{idx}"
            self._node_iids[iid] = n.name
            self._node_latency[n.name] = n.latency_ms
            tree.insert("", "end", iid=iid, values=(
                n.name,
                theme.latency_text(n.latency_ms),
                "✓" if n.current else "",
                "✓" if n.ai_capable else "",
            ), tags=("current",) if n.current else ())
        self._current_iid = next(
            (iid for iid, name in self._node_iids.items()
             if name and name == self._status.node), None)
        self._update_nodes_note()
        self._update_latency_label()

    def _update_nodes_note(self) -> None:
        total = max(int(self._status.node_count or 0), len(self._nodes))
        if not self._nodes:
            text = "还没有节点列表。连接后点「刷新节点」，或点「自动选最优」。"
        elif total > len(self._nodes):
            text = f"共 {total} 个节点，这里列出最快的 {len(self._nodes)} 个（按延迟排序）"
        else:
            text = f"共 {len(self._nodes)} 个节点（按延迟排序）"
        self.nodes_note.configure(text=text)

    def _on_node_double_click(self, event: tk.Event) -> None:
        iid = self.tree_nodes.identify_row(event.y) if hasattr(event, "y") else ""
        if not iid:
            iid = (self.tree_nodes.selection() or [""])[0]
        name = self._node_iids.get(iid, "")
        if not name or self._task_busy:
            return
        if iid == self._current_iid:
            self._set_hint(f"「{name}」已经是当前出口了。")
            return
        if not self._status.running:
            self._show_error("还没连接到内核：请先点左边的大圆钮连接，再切换节点。")
            return
        self._status_task(
            f"正在切换到「{name}」…",
            lambda: control.select_node(name),
            ok_hint=f"已切换到「{name}」",
        )

    def _do_pick_best(self) -> None:
        if not self._status.running:
            self._show_error("还没连接到内核：请先点左边的大圆钮连接，再自动选节点。")
            return
        self._status_task(
            "正在逐个实测节点（最多几分钟）…",
            control.pick_best_node,
            ok_hint="已自动切到最优节点",
        )

    # ---- 自检 ----

    def _do_selfcheck(self) -> None:
        self.check_note.configure(text="正在逐个平台实测…")
        self._run_task("正在逐个平台实测（可能要几十秒）…", control.test_platforms,
                       on_done=self._selfcheck_done)

    def _selfcheck_done(self, checks: Any) -> None:
        rows = checks if isinstance(checks, list) else []
        tree = self.tree_checks
        children = tree.get_children()
        if children:
            tree.delete(*children)
        ok_count = 0
        for i, c in enumerate(rows):
            ok = bool(getattr(c, "ok", False))
            ok_count += 1 if ok else 0
            tree.insert("", "end", values=(
                getattr(c, "name", "?"),
                "可用" if ok else "不可用",
                theme.check_latency_text(int(getattr(c, "latency_ms", -1) or -1), ok),
                str(getattr(c, "detail", "") or ""),
            ), tags=("ok",) if ok else ("bad",))
        if not rows:
            self.check_note.configure(text="自检没有返回结果。")
            self._show_error("自检没有返回任何结果：请确认已经连接，然后再试一次。")
            return
        bad = [c for c in rows if not bool(getattr(c, "ok", False))]
        self.check_note.configure(
            text=f"共 {len(rows)} 项，{ok_count} 项可用，{len(bad)} 项不可用。")
        if bad:
            names = "、".join(str(getattr(c, "name", "?")) for c in bad[:6])
            self._show_error(f"这些平台当前不可用：{names}。可以换一个节点再试。")
        else:
            self._clear_error()
            self._set_hint("自检全部通过，可以正常使用。")

    def _do_verify_ai(self) -> None:
        if not self._status.running:
            self._show_error("内核没在跑：先点大圆钮连接，再验证 ChatGPT 出口。")
            return
        self._run_task("正在实测 ChatGPT 出口（约十几秒）…", control.verify_ai,
                       on_done=self._verify_ai_done)

    def _verify_ai_done(self, ai: Any) -> None:
        country = theme.country_label(str(getattr(ai, "exit_country", "") or ""))
        if bool(getattr(ai, "ok", False)):
            self._clear_error()
            self._set_hint(
                f"ChatGPT 可以正常访问，出口是 {country or '未知地区'}。")
        else:
            detail = str(getattr(ai, "detail", "") or "")
            self._show_error(
                f"ChatGPT 现在打不开（{detail or '未知原因'}）："
                "多半是这个节点的地区不被支持，换一个节点再试。")
        # verify_ai 会把结果写进缓存, 重新拉一次快照就能显示"几分钟前验证"。
        self._poll_now()

    # ------------------------------------------------------------------ #
    # 任务调度: 所有阻塞调用都从这里进后台线程
    # ------------------------------------------------------------------ #

    def _call_soon(self, fn: Callable[[], None]) -> None:
        """把工作线程的结果搬回 Tk 主线程 —— 本文件唯一的跨线程出口。

        窗口已经销毁时静默丢弃: 这时候再 after 会抛 TclError, 而且也没人
        需要这个结果了。回调里逃出来的异常直接显示到错误条上, 界面自身的
        bug 同样不能静默。
        """
        if self._closing:
            return

        def run() -> None:
            if self._closing:
                return
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                try:
                    self._show_error(f"界面内部错误：{type(e).__name__}: {e}")
                except Exception:  # noqa: PERF203
                    pass

        try:
            self.root.after(0, run)
        except (tk.TclError, RuntimeError):  # pragma: no cover - 窗口已销毁
            pass

    def _run_task(self, label: str, fn: Callable[..., Any],
                  on_done: Callable[[Any], None] | None = None) -> bool:
        """后台跑一个阻塞操作, 全程不冻界面。

        同一时刻只允许一个写操作: 用户连点两下"自动选最优"会把内核里的
        选择反复覆盖, 而且两个线程同时改配置档是真实的损坏风险。
        """
        if self._closing:
            return False
        if self._task_busy:
            self._set_hint("正在处理上一个操作，请稍候…")
            return False
        self._task_busy = True
        self._set_hint(label)
        self._sync_controls()
        self.root.configure(cursor="watch")
        control.run_bg(
            fn,
            on_done=lambda res: self._task_done(res, on_done),
            on_error=self._task_failed,
        )
        return True

    def _task_done(self, result: Any, on_done: Callable[[Any], None] | None) -> None:
        def finish() -> None:
            # 先解除忙碌再回调: 回调里经常要接着起下一个任务(比如切完 TUN 再刷新),
            # 还占着忙碌标记的话第二个任务会被自己挡掉。
            self._task_busy = False
            try:
                self.root.configure(cursor="")
            except tk.TclError:  # pragma: no cover
                pass
            self._sync_controls()
            if on_done is not None:
                on_done(result)
        self._call_soon(finish)

    def _task_failed(self, exc: BaseException) -> None:
        def finish() -> None:
            self._task_busy = False
            try:
                self.root.configure(cursor="")
            except tk.TclError:  # pragma: no cover
                pass
            self._sync_controls()
            self._show_error(f"操作失败：{type(exc).__name__}: {exc}")
        self._call_soon(finish)

    def _status_task(self, label: str, fn: Callable[..., Any], *, ok_hint: str = "",
                     after: Callable[[Any], None] | None = None) -> None:
        """跑一个返回 control.Status 的操作, 统一处理 error 和提示语。"""

        def on_done(st: Any) -> None:
            self._finish_status(st, ok_hint=ok_hint)
            if after is not None:
                after(st)

        self._run_task(label, fn, on_done=on_done)

    def _finish_status(self, st: Any, *, ok_hint: str = "") -> None:
        # 失败的结果里带着引擎的原始 error, 先渲染(它会写错误条), 再刷新界面。
        if isinstance(st, control.Status) and st.error:
            self._show_error(st.error)
            self._apply_status(st, keep_error=True)
        else:
            self._clear_error()
            if isinstance(st, control.Status):
                self._apply_status(st)
            if ok_hint:
                self._set_hint(ok_hint)

    # ------------------------------------------------------------------ #
    # 状态渲染
    # ------------------------------------------------------------------ #

    def _schedule_poll(self, delay_ms: int) -> None:
        if self._closing:
            return
        try:
            self._poll_job = self.root.after(delay_ms, self._poll)
        except tk.TclError:  # pragma: no cover
            self._poll_job = None

    def _poll_now(self) -> None:
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:  # noqa: PERF203
                pass
            self._poll_job = None
        self._poll()

    def _poll(self) -> None:
        """定时把 control.snapshot() 搬上来。它约 82ms, 仍必须走后台线程:
        主线程卡 82ms 就是一次肉眼可见的掉帧, 而且内核满载时会明显更久。"""
        if self._closing:
            return
        self._poll_job = None
        if not self._poll_busy:
            self._poll_busy = True
            control.run_bg(_poll_payload, on_done=self._poll_done,
                           on_error=self._poll_failed)
        self._schedule_poll(POLL_HIDDEN_MS if self._hidden else POLL_VISIBLE_MS)

    def _poll_done(self, payload: Any) -> None:
        def apply() -> None:
            self._poll_busy = False
            status, health = payload if isinstance(payload, tuple) else (payload, {})
            if isinstance(health, dict):
                self._health_note = str(health.get("note") or "")
                try:
                    self._health_switches = int(health.get("switches") or 0)
                except (TypeError, ValueError):
                    self._health_switches = 0
                self.var_failover.set(bool(health.get("enabled", False)))
            self._apply_status(status)
        self._call_soon(apply)

    def _poll_failed(self, exc: BaseException) -> None:
        def apply() -> None:
            self._poll_busy = False
            self._set_hint("读取状态失败，正在重试…")
            self._show_error(f"读取状态失败：{type(exc).__name__}: {exc}")
        self._call_soon(apply)

    def _apply_status(self, st: Any, *, keep_error: bool = False) -> None:
        if not isinstance(st, control.Status):
            return
        was_connected = bool(self._status.connected)
        self._status = st
        self._render_switch()
        self._sync_widgets(st)
        self.lbl_node.configure(
            text=theme.shorten_display(st.node, 24)
            or ("—" if not st.connected else "自动选择"))
        self._update_latency_label()
        self.lbl_count.configure(text=str(st.node_count or len(self._nodes) or 0))
        self.lbl_ai.configure(text=self._ai_text(st))
        # 圆钮下面那句话平时应该说的是"现在能干什么", 只有正在干活时才让位给
        # 进度提示 —— 否则用户连接完看到的还是"正在读取状态…", 会以为没成功。
        if not self._task_busy:
            self._set_hint(self._default_hint())
        self._update_nodes_note()
        self._sync_current_row(st.node)
        self._set_status_text()
        if st.error and not keep_error:
            self._show_error(st.error)
        # 连接状态一变, 节点列表里的"延迟/当前"就过期了, 重新拉一份(后台线程)。
        if bool(st.connected) != was_connected:
            self._load_nodes()
        self._sync_tray()

    def _ai_text(self, st: Any) -> str:
        country = theme.country_label(str(st.ai_exit or ""))
        ago = theme.format_ago(float(st.ai_checked_at or 0.0))
        if country:
            return f"出口 {country} · {ago}"
        if st.ai_checked_at:
            return f"地区未知 · {ago}"
        return "尚未验证（去「平台自检」里实测）"

    def _default_hint(self) -> str:
        """圆钮下面那句常态提示: 说清楚"现在能干什么"。"""
        if self._task_busy:
            return str(self.switch_hint.cget("text"))
        if self._status.connected:
            return "已经可以正常上网了，再点一下就断开。"
        return "点一下就能用，不用做别的设置。"

    def _update_latency_label(self) -> None:
        ms = self._node_latency.get(str(self._status.node or ""), -1)
        self.lbl_latency.configure(text=theme.latency_text(ms),
                                   style=theme.latency_style(ms))

    def _sync_widgets(self, st: Any) -> None:
        """把控件拉到和引擎一致。全部包在 _syncing 里, 避免和用户点击打架。"""
        self._syncing = True
        try:
            if self.var_mode.get() != st.mode:
                self.var_mode.set(st.mode)
            self.mode_hint.configure(text=MODE_HINTS.get(st.mode, ""))
            self.var_autostart.set(bool(st.autostart))
            self.var_tun.set(bool(st.tun))
            # health.py 缺位时的 note 是"自动切换未启用"这类内部说明, 贴在设置卡
            # 里会把一句话挤成三行、把勾选框顶出卡片。只在它真的表示故障时才显示。
            note = "TUN 打开后，所有程序都会走代理。"
            if self._health_switches > 0:
                # "稳定"是看不见的, 用户只有在"节点挂了但我没感觉"时才会想起它。
                # 把换过几次摆出来, 用户才知道这功能真的在干活。
                note = f"自动切换已帮你换过 {self._health_switches} 次节点。"
            elif self._health_note and ("不可用" in self._health_note
                                        or "失败" in self._health_note):
                note = theme.shorten(self._health_note, 22)
            self.settings_note.configure(text=note)
        finally:
            self._syncing = False
        self._sync_controls()

    def _sync_controls(self) -> None:
        """忙碌时禁用会改引擎状态的控件。

        只读的东西(节点列表、自检结果)不锁, 用户随时可以翻; 但写操作必须
        互斥, 否则连点会互相覆盖。
        """
        state = "disabled" if self._task_busy else "normal"
        for w in getattr(self, "_action_widgets", []):
            try:
                w.configure(state=state)
            except tk.TclError:  # pragma: no cover
                pass

    def _sync_current_row(self, node: str) -> None:
        """只更新变化的那两行 —— 每 2 秒重建 200 行会让列表滚动位置乱跳。"""
        node = str(node or "")
        new_iid = None
        if node:
            new_iid = next((iid for iid, name in self._node_iids.items()
                            if name == node), None)
        if new_iid == self._current_iid:
            return
        for iid in (self._current_iid, new_iid):
            if iid is None or iid not in self._node_iids:
                continue
            is_cur = iid == new_iid
            try:
                self.tree_nodes.set(iid, "cur", "✓" if is_cur else "")
                self.tree_nodes.item(iid, tags=("current",) if is_cur else ())
            except tk.TclError:  # pragma: no cover - 列表刚好被重建
                continue
        self._current_iid = new_iid
        if new_iid is not None:
            try:
                self.tree_nodes.see(new_iid)
            except tk.TclError:  # pragma: no cover
                pass

    def _render_switch(self) -> None:
        """画大圆钮。状态没变就跳过重画 —— 每 2 秒重画一次会闪。"""
        st = self._status
        key = (bool(st.connected), self._task_busy, self.switch_hint.cget("text"))
        if key == self._switch_key:
            return
        self._switch_key = key
        face, text_color, label = theme.switch_face(bool(st.connected))
        if self._task_busy:
            # 处理中: 外圈用品牌蓝提示"在动了", 但文字仍然如实显示当前状态,
            # 不假装已经连上。
            ring = theme.ACCENT
        else:
            ring = theme.ACCENT if st.connected else theme.BORDER
        c = self.switch
        c.delete("all")
        c.create_oval(4, 4, 102, 102, fill="", outline=ring, width=3)
        c.create_oval(12, 12, 94, 94, fill=face, outline=face)
        c.create_text(53, 47, text=label, fill=text_color, font=theme.font(13, "bold"))
        c.create_text(53, 67, text="点击关闭" if st.connected else "点一下就能用",
                      fill=text_color, font=theme.font(8))

    def _set_hint(self, text: str) -> None:
        self.switch_hint.configure(text=text)
        self._render_switch()

    def _set_status_text(self) -> None:
        st = self._status
        if st.connected:
            parts = ["已连接"]
            country = theme.country_label(str(st.ai_exit or ""))
            if country:
                parts.append(f"ChatGPT 出口 {country}")
            core = theme.version_text(str(st.core_version or ""))
            if core:
                parts.append(f"内核 {core}")
            up = theme.format_uptime(float(st.uptime_s or 0.0))
            if up:
                parts.append(f"已用 {up}")
        else:
            parts = ["未连接"]
            if st.tun:
                parts.append("TUN 已开")
        self.lbl_status.configure(text=" · ".join(parts))

    # ---- 错误显示 ----

    def _show_error(self, msg: str) -> None:
        """错误必须看得见: 横幅给完整信息, 状态栏留一行简版长期可见。"""
        msg = str(msg).strip()
        if not msg:
            return
        self._last_error = msg
        self.lbl_error.configure(text=f"⚠ {msg}")
        self.error_bar.grid()
        short = msg if len(msg) <= 42 else msg[:41] + "…"
        self.lbl_status_err.configure(text=f"最近错误：{short}")

    def _clear_error(self, *_args: Any) -> None:
        self._last_error = ""
        self.lbl_error.configure(text="")
        self.error_bar.grid_remove()
        self.lbl_status_err.configure(text="")

    # ---- 托盘同步 ----

    def _sync_tray(self, *, force: bool = False) -> None:
        """把连接状态同步给托盘图标。

        托盘是另一条线程里的 Win32 消息循环, 它自己会做线程切换, 所以这里
        直接调; 但它一旦抛异常就当作"没有托盘", 免得每次轮询都抛一遍。
        """
        tray = self._tray
        if tray is None:
            return
        connected = bool(self._status.connected)
        if force or connected != self._tray_connected:
            self._tray_connected = connected
            try:
                tray.set_connected(connected)
            except Exception:  # noqa: PERF203
                self._tray = None
                return
        tip = (f"{control.BRAND_NAME} · {control.BRAND_TAGLINE} · "
               f"{'已连接' if connected else '未连接'}")
        country = theme.country_label(str(self._status.ai_exit or ""))
        if connected and country:
            tip += f" · {country}"
        if tip != self._tray_tip:
            self._tray_tip = tip
            try:
                tray.set_tooltip(tip)
            except Exception:  # noqa: PERF203
                self._tray = None

    # ---- 其他 ----

    def _place_window(self) -> None:
        """把窗口摆到屏幕中间, 并保证**完整可见**。

        屏幕只有 1280x720, 底部的任务栏(实测约 41px)和标题栏(约 31px)都要
        占地方。窗口一旦比工作区高, 底部的状态栏和错误条就永远露不出来 ——
        而那恰好是用户最需要看到的东西, 所以这里按工作区高度封顶, 而不是
        按屏幕高度。
        """
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        work_h = sh - 48
        w = max(760, min(1060, sw - 120))
        h = max(520, min(620, work_h - 40))
        x = max(0, (sw - w) // 2)
        y = max(0, (work_h - (h + 40)) // 2 + 8)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(880, 560)

    def _on_callback_error(self, exc: type[BaseException], val: BaseException,
                           tb: TracebackType | None) -> None:
        """Tk 回调里逃出来的异常。默认只打 stderr, 打包后等于被吞掉。"""
        try:
            print("".join(traceback.format_exception(exc, val, tb)), file=sys.stderr)
        except Exception:  # noqa: PERF203
            pass
        try:
            self._show_error(f"界面内部错误：{type(val).__name__}: {val}")
        except Exception:  # noqa: PERF203
            pass


def main(argv: list[str] | None = None) -> int:
    """单文件入口: `python -m accesspilot.gui.app` 可以直接开窗调界面。

    这里**不挂托盘** —— 托盘接线只在 gui/__init__.py 里有一份, 在本文件
    再写一份迟早会漂移。正式启动请用 `accesspilot gui`(等价于
    `python -m accesspilot.gui`), 那条路会把托盘接上。
    """
    app = App()
    return app.run(tray=None)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
