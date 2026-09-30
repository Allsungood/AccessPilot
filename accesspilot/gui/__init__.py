"""红杏图形界面: Tkinter 主窗口 + Win32 原生托盘(零第三方依赖).

包内分工
--------
    theme.py   配色与 ttk 样式
    app.py     主窗口; 业务逻辑全部委托给 accesspilot/control.py
    tray.py    系统托盘; 纯 ctypes 调 Shell_NotifyIcon
    icon.py    纯 Python 生成 .ico(打包 exe 也要用同一个图标)

为什么托盘是**可选**的
----------------------
托盘走的是 Win32 原生接口。它在某些环境下会挂不起来 —— 远程桌面会话、
被安全软件拦、或者 Explorer 没在跑。托盘挂了**不该让整个客户端打不开**,
所以这里的接线是: 先建主窗口, 再尽力挂托盘; 托盘失败就退化成普通窗口,
用户顶多少一个常驻入口, 而不是面对一个打不开的程序。

谁负责接线
----------
app.py 和 tray.py 互不 import —— 两边都只认 control.py 那一层契约, 由本模块
把它们接起来。这样其中一个写坏了, 另一个还能单独跑、单独测。

约定的接口(改这里就要同步改 app.py / tray.py)
---------------------------------------------
    app.App:
        App()                       # 构造, 不阻塞
        App.run(tray=None) -> int   # 进入主循环; 返回进程退出码
        App.show() / App.hide()     # 供托盘调用
        App.quit()                  # 供托盘调用
        App.toggle()                # 一键开关(内部自己丢后台线程)
        App.pick_best()             # 自动选最优节点
        App.set_tray(tray)          # 可选; 有就用来同步托盘图标状态

    tray.Tray:
        Tray(*, on_show, on_toggle, on_pick_best, on_quit, tooltip="红杏")
        Tray.start() -> bool        # 失败返回 False, 不抛
        Tray.stop() -> None
        Tray.set_connected(bool) -> None
        Tray.set_tooltip(str) -> None
"""
from __future__ import annotations

from typing import Any

__all__ = ["main", "start"]


def _start_tray(app: Any) -> Any:
    """尽力把托盘挂起来。任何失败都只是"没有托盘", 不影响主窗口。"""
    try:
        from .tray import Tray
    except Exception:  # noqa: PERF203
        return None
    try:
        from ..control import BRAND_NAME, BRAND_TAGLINE

        tray = Tray(
            on_show=app.show,
            on_toggle=getattr(app, "toggle", None),
            on_pick_best=getattr(app, "pick_best", None),
            on_quit=app.quit,
            tooltip=f"{BRAND_NAME} · {BRAND_TAGLINE}",
        )
        if not tray.start():
            return None
        # 让主窗口在连接状态变化时同步托盘图标; 没有这个方法也不影响
        setter = getattr(app, "set_tray", None)
        if callable(setter):
            setter(tray)
        return tray
    except Exception:  # noqa: PERF203
        return None


def main(argv: list[str] | None = None) -> int:
    """红杏 GUI 入口。返回进程退出码。

    argv 目前只认 `--no-tray`: 不挂托盘, 只开窗口(排错用 —— 怀疑托盘
    把主循环搞挂时, 用它把变量摘掉)。
    """
    argv = list(argv or [])
    want_tray = "--no-tray" not in argv

    try:
        from .app import App
    except Exception as e:  # noqa: PERF203
        print(f"[x] 红杏界面加载失败: {type(e).__name__}: {e}")
        print("    (如果是在源码目录外运行, 请确认当前目录或 PYTHONPATH 里有 accesspilot 包)")
        return 2

    try:
        app = App()
    except Exception as e:  # noqa: PERF203
        print(f"[x] 红杏启动失败: {type(e).__name__}: {e}")
        return 2

    tray = _start_tray(app) if want_tray else None
    try:
        return int(app.run(tray=tray) or 0)
    finally:
        if tray is not None:
            try:
                tray.stop()
            except Exception:  # noqa: PERF203
                pass


#: `accesspilot gui` 走这个别名, 免得命令行层直接依赖 .app
start = main
