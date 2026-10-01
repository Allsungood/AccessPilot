"""红杏 桌面客户端 —— 单文件 exe 的冻结入口点(PyInstaller 专用).

这个文件**只属于打包链路**, 不是 accesspilot 运行时包的一部分。放在
packaging/ 下面, 是为了让 `accesspilot/` 里的源码一行都不用为打包而改
(打包是开发期工具, 不该反过来污染运行时结构)。

它替 accesspilot 挡掉三件"只有冻结之后才会发生"的事
=====================================================

1) **stdout / stderr 变成 None**

   `console=False` 的窗口化构建没有控制台, Python 会把 `sys.stdout` /
   `sys.stderr` 置为 `None`。而 `accesspilot/util.py` 在**模块顶层**就执行
   `_COLOR = sys.stdout.isatty() and ...` —— 不先把流对象补上, import 阶段
   就 `AttributeError` 直接崩, 用户双击之后看到的是"什么都没发生"。

   这里按三级退路把流接回去(见 `_install_streams`):
     a. 标准句柄被父进程重定向到管道/文件 → 直接包一层用;
     b. 从 cmd / PowerShell 里调用(父进程有控制台) → AttachConsole 挂上去,
        于是 `红杏.exe --version`、`红杏.exe doctor` 在终端里能正常打印;
     c. 双击启动(没有控制台也没有重定向) → 落到
        `%LOCALAPPDATA%\\AccessPilot\\logs\\hongxing.log`,
        这样 GUI 万一崩了, 用户/客服至少有一份现场日志可看。

2) **sys.executable 变成了 exe 自己**

   accesspilot 内部有三处用 `sys.executable -m accesspilot <子命令>` 拉起
   子进程或写计划任务:
     * `process.start_watchdog()`  → `-m accesspilot _watchdog --pid N`
     * `process.start_accel()`     → `-m accesspilot accel serve --port N`
     * `control.set_autostart()`   → `cmd /c ""<exe>" ensure"`(传了 exe 路径时)
   冻结之后 `sys.executable` 就是本 exe, 这些调用会把 `-m accesspilot` 原样
   当成 exe 的命令行参数递进来。`_normalized_argv()` 把这段前缀剥掉再转交给
   cli, 于是 exe 能像 `python -m accesspilot` 那样被子进程复用 —— 用户机器上
   既不需要装 Python, 也不需要全局 accesspilot 命令。
   (这条参数通路由 `packaging/smoke_test.py` 第 3 项实测; 看门狗/加速器**自身
   的业务逻辑**要真实内核, 不在打包验证范围内。)

3) **双击时命令行是空的**

   cli 在没有子命令时只打印状态就退出(而窗口化构建连打印都看不见)。所以这里
   默认补上 `gui` 子命令。顺带做了优雅降级: 如果 cli 里还没有 `gui`(其它
   teammate 还没写完), 就直接调 `accesspilot.gui`, 再不行退到老的网页控制台
   `dashboard --open` —— 构建/试运行永远不该因为一个子命令缺失而"打不开"。
"""
from __future__ import annotations

import io
import locale
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

#: 弹错误框、写日志时用的产品名
EXE_TITLE = "红杏"

#: 窗口化构建的日志上限: 超过就清空重来, 免得用户机器上滚出一个 GB 级日志
MAX_LOG_BYTES = 4 * 1024 * 1024

#: 会被 cli 原样转交给 GUI 的开关。只给这些参数时, 等同于"启动界面"
_GUI_FLAGS = ("--no-tray",)

#: `python -m accesspilot` 的等价写法(冻结后由 accesspilot 内部自己传进来)
_MODULE_PREFIXES = ("accesspilot", "accesspilot.__main__")

#: _install_streams() 实际走了哪条退路: native / handle / console / log / null
_stream_mode = "native"


# --------------------------------------------------------------------------- #
# 1) 把 stdout / stderr 接回来
# --------------------------------------------------------------------------- #


class _StreamShim:
    """包在真实流外面的一层壳, 目的是**让 reconfigure() 变成空操作**。

    为什么需要: `accesspilot/util.py` 在 import 时会执行
    `stream.reconfigure(encoding="utf-8")`。对本进程自己接管的流来说, 编码是
    这里根据"流到底通向哪里"选好的 —— 挂到 Windows 控制台时必须用控制台代码页
    (简体中文机器上是 cp936), 强行改成 utf-8 会让中文变成乱码。所以这里把
    reconfigure 吃掉, 其余属性原样透传。
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.encoding = getattr(stream, "encoding", "utf-8")
        self.errors = getattr(stream, "errors", "replace")

    # --- 真正要用的三个 ---
    def write(self, text: str) -> int:
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        # 一律按"不是终端"处理: Windows 10 的控制台默认没开 VT 序列,
        # 返回 True 会让 util 往输出里塞 ANSI 颜色码, 在 cmd 里就是一堆 [32m。
        return False

    # --- 被吃掉的那个 ---
    def reconfigure(self, **kwargs: Any) -> None:  # noqa: ARG002
        """空操作 —— 见类文档。"""

    def close(self) -> None:
        """只 flush, 不真关: 这是进程的标准输出。

        真关掉的后果是后续输出静默丢失, 而且 fd 1/2 一关, 进程退出码会变成 1
        (看起来像"程序失败了", 其实只是输出流被自己人掐了)。
        """
        try:
            self._stream.flush()
        except Exception:
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"<hongxing stream {self._stream!r}>"


def _log_file() -> Path | None:
    """窗口化构建的兜底日志位置(与 paths.logs_dir() 的规则保持一致)。"""
    override = os.environ.get("HONGXING_LOG") or os.environ.get("ACCESSPILOT_LOG")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        return None
    return Path(base) / "AccessPilot" / "logs" / "hongxing.log"


def _text_wrapper(raw: Any, *, encoding: str) -> io.TextIOWrapper:
    return io.TextIOWrapper(
        raw, encoding=encoding, errors="replace", line_buffering=True, write_through=True
    )


def _wrap_inherited_handles() -> dict[str, Any] | None:
    """a) 父进程已经把标准句柄重定向到管道/文件(Start-Process -RedirectStandardOutput 等)。

    窗口化构建里 Python 不认这些句柄, 但它们确实是有效的 —— 用 msvcrt 接回来,
    `红杏.exe --version > out.txt` 才能真正落盘。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        import msvcrt

        kernel32 = ctypes.windll.kernel32
        # 句柄是 64 位指针, 不声明 restype 会被 ctypes 默认的 c_int 截断 —— 这是
        # 个只在部分机器上才现形的经典坑。
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetStdHandle.argtypes = [ctypes.c_int]
        kernel32.GetFileType.restype = ctypes.c_uint32
        kernel32.GetFileType.argtypes = [ctypes.c_void_p]
        invalid = ctypes.c_void_p(-1).value
    except Exception:
        return None

    streams: dict[str, Any] = {}
    for key, std_id in (("out", -11), ("err", -12)):  # STD_OUTPUT/STD_ERROR_HANDLE
        try:
            handle = kernel32.GetStdHandle(std_id)
            if handle in (None, 0, invalid):
                continue
            if kernel32.GetFileType(handle) == 0:  # FILE_TYPE_UNKNOWN
                continue
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
            # 与 util._reconfigure_stdout() 的意图一致: 管道/文件一律写 utf-8
            streams[key] = _text_wrapper(os.fdopen(fd, "wb", 0), encoding="utf-8")
        except Exception:
            continue
    return streams or None


def _attach_parent_console() -> dict[str, Any] | None:
    """b) 从 cmd / PowerShell 里被调用 —— 挂到父进程的控制台上。

    窗口化 exe 没有自己的控制台, 所以 `红杏.exe --version` 默认什么都不会显示。
    AttachConsole(ATTACH_PARENT_PROCESS) 挂上之后 CONOUT$ 才存在, 用它重开
    stdout/stderr, 命令行子命令就都能正常输出了。
    """
    if os.name != "nt" or os.environ.get("HONGXING_NO_CONSOLE") == "1":
        return None
    try:
        import ctypes

        if not ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            return None
        encoding = _console_encoding()
        out = open("CONOUT$", "w", encoding=encoding, errors="replace", buffering=1)
        err = open("CONOUT$", "w", encoding=encoding, errors="replace", buffering=1)
        return {"out": out, "err": err}
    except Exception:
        return None


def _console_encoding() -> str:
    """控制台当前代码页对应的编码(cp936 / cp65001...).

    用代码页而不是硬编码 utf-8: 控制台是按代码页解释写入的字节的, 在简体中文
    机器上写 utf-8 的中文会变成乱码。
    """
    override = os.environ.get("HONGXING_STDOUT_ENCODING")
    if override:
        return override
    try:
        enc = locale.getpreferredencoding(False)
        if enc:
            return enc
    except Exception:
        pass
    return "utf-8"


def _open_log_stream() -> dict[str, Any] | None:
    """c) 双击启动(没有控制台) —— 输出落到日志文件, 保证崩了有据可查。"""
    target = _log_file()
    if target is not None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.stat().st_size > MAX_LOG_BYTES:
                target.unlink()
            raw = open(target, "ab", buffering=0)  # noqa: SIM115 - 进程全程持有
            stream = _text_wrapper(raw, encoding="utf-8")
            stream.write(
                f"\n===== {EXE_TITLE} 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"pid={os.getpid()} argv={sys.argv[1:]!r} =====\n"
            )
            return {"out": stream, "err": stream}
        except Exception:
            pass
    try:
        stream = _text_wrapper(open(os.devnull, "wb"), encoding="utf-8")  # noqa: SIM115
        return {"out": stream, "err": stream}
    except Exception:  # pragma: no cover - 理论上到不了
        return None


class _NullStream:
    """最后的最后: 连 devnull 都开不了时的哑流(至少别让程序崩在输出上)。"""

    encoding = "utf-8"
    errors = "replace"

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **kwargs: Any) -> None:  # noqa: ARG002
        pass


def _install_streams(*, want_console: bool = True) -> str:
    """补齐 sys.stdout / sys.stderr / sys.stdin, 返回实际走了哪条退路。

    want_console: GUI 路径必须传 False —— 挂父控制台会让主窗口以最小化状态
                  出现(见 chain 处的实测记录)。命令行子命令保持默认 True。
    """
    global _stream_mode

    # HONGXING_STREAM=log 强制走"双击启动"那条退路 —— 排查"用户说双击没反应"时,
    # 让用户带着这个变量运行一次就能拿到日志, 哪怕他是在终端里跑的。
    # 这是一个显式的用户要求, 所以它会**覆盖**已经可用的 stdout。
    forced = (os.environ.get("HONGXING_STREAM") or "auto").strip().lower()
    if forced in ("log", "file"):
        streams = _open_log_stream()
        if streams:
            sys.stdout = _StreamShim(streams["out"])
            sys.stderr = _StreamShim(streams["err"])
            if sys.stdin is None:
                sys.stdin = io.StringIO("")
            _stream_mode = "log"
            return _stream_mode

    if sys.stdout is not None and sys.stderr is not None:
        _stream_mode = "native"
        return _stream_mode

    chain = [
        ("handle", _wrap_inherited_handles),
        ("console", _attach_parent_console),
        ("log", _open_log_stream),
    ]
    if not want_console:
        # GUI 路径不挂父控制台 —— 理由是**设计**, 不是实测:
        # 一个窗口化程序没有理由把自己关联到调用者的控制台; 而
        # _attach_parent_console 存在的唯一目的就是让 `红杏.exe --version`
        # / `doctor` 这类命令行子命令能在终端里打印, GUI 路径不需要它。
        #
        # 诚实记录一段走过的弯路(2026-10-01): 我曾观察到"从终端启动 exe 时
        # 主窗口以最小化状态出现", 并把它归因于 AttachConsole。但换新 exe 做
        # A/B 时结论**反了过来**(默认正常、加 HONGXING_NO_CONSOLE=1 反而最小化),
        # 说明那个现象的真正变量不是这里 —— 后来查明是另一个 teammate 的测试
        # 脚本在用 keybd_event 发真实按键, 干扰了前台窗口。所以**这条改动并不
        # 是那个现象的修复**, 只是按设计把 GUI 与 CLI 的输出路径分开。
        # 干净复测(无测试脚本干扰)下, exe 的窗口从 3.0s 起一直是
        # iconic=False rect=(110,14,1076,659) 且是前台, 存活 60 秒以上。
        chain = [item for item in chain if item[0] != "console"]
    if forced in ("console", "handle"):
        chain = [item for item in chain if item[0] == forced] + [("log", _open_log_stream)]

    used = "null"
    for name, provider in chain:
        if sys.stdout is not None and sys.stderr is not None:
            break
        streams = provider()
        if not streams:
            continue
        used = name
        if sys.stdout is None and streams.get("out") is not None:
            sys.stdout = _StreamShim(streams["out"])
        if sys.stderr is None and streams.get("err") is not None:
            sys.stderr = _StreamShim(streams["err"])

    if sys.stdout is None:
        sys.stdout = _StreamShim(_NullStream())
    if sys.stderr is None:
        sys.stderr = _StreamShim(_NullStream())
    if sys.stdin is None:
        sys.stdin = io.StringIO("")  # 别让 input() 抛 TypeError
    _stream_mode = used
    return used


def _ensure_source_path() -> None:
    """`python packaging/entry.py ...` 这种源码方式运行时, 把仓库根挂进 sys.path。

    冻结后什么都不做: 包已经在 exe 自己的归档里, 那时再往 sys.path 里塞目录
    只会有意外覆盖的风险。
    """
    if getattr(sys, "frozen", False):
        return
    try:
        import accesspilot  # noqa: F401

        return
    except Exception:
        pass
    root = Path(__file__).resolve().parent.parent
    if (root / "accesspilot" / "__init__.py").is_file() and str(root) not in sys.path:
        sys.path.insert(0, str(root))


# --------------------------------------------------------------------------- #
# 兜底的错误提示: 窗口化构建里 print 是没人看得见的
# --------------------------------------------------------------------------- #


def _fatal(message: str, detail: str = "") -> int:
    text = f"{EXE_TITLE} 启动失败:\n\n{message}"
    if detail:
        text += f"\n\n{detail}"
    text += "\n\n详细信息见 %LOCALAPPDATA%\\AccessPilot\\logs\\hongxing.log"
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    # 弹窗只在"双击启动"时用: 那种场景下 print 没人看得见。从命令行调用时
    # (挂到了父控制台/管道)不该再弹一个模态框 —— 既打断脚本, 也可能把自动化
    # 测试卡死在等人点"确定"上。
    if os.name == "nt" and _stream_mode in ("log", "null"):
        if os.environ.get("HONGXING_NO_MESSAGEBOX") != "1":
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(None, text, EXE_TITLE, 0x10)  # MB_ICONERROR
            except Exception:
                pass
    return 3


# --------------------------------------------------------------------------- #
# 打包自检(`红杏.exe --selftest`): 只读检查, 不开窗口、不联网、不写用户数据
# --------------------------------------------------------------------------- #


def _selftest() -> int:
    """在**冻结环境内部**核对那些"漏了就只在用户机器上炸"的东西。

    日常的 `--version` 证明不了资源打进去了: cli 是被 import 了, 但
    `webgui` / `gui.icon` 都是延迟 import 的, dashboard.html 到底在不在包里
    要真的读一次才知道。所以这里把运行时真正用到的读取路径挨个走一遍。

    输出是一段 JSON(packaging/smoke_test.py 解析它)。
    """
    import json

    report: dict[str, Any] = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "meipass": getattr(sys, "_MEIPASS", None),
        "python": sys.version.split()[0],
        "stream_mode": _stream_mode,
        "checks": {},
    }

    def record(name: str, ok: bool, detail: str, *, required: bool = True) -> None:
        report["checks"][name] = {"ok": bool(ok), "detail": detail, "required": required}

    # 1) 运行时能不能 import(缺 hiddenimport 会在这里现形)
    try:
        from accesspilot import __version__ as version, cli

        record("runtime", True, f"accesspilot {version}, cli={cli.__file__}")
    except Exception as exc:
        record("runtime", False, f"{type(exc).__name__}: {exc}")

    # 2) dashboard.html —— webgui 用 Path(__file__).parent/"assets" 读它
    try:
        from accesspilot import webgui

        asset = Path(webgui.ASSET_DIR) / "dashboard.html"
        body = webgui.dashboard_html()
        ok = asset.is_file() and len(body) > 1024 and b"missing" not in body[:64]
        record("dashboard_asset", ok, f"{asset} -> {len(body)} 字节")
    except Exception as exc:
        record("dashboard_asset", False, f"{type(exc).__name__}: {exc}")

    # 3) 托盘 / 窗口图标
    try:
        from accesspilot.gui import icon

        directory = Path(icon.assets_dir())
        found = {name: (directory / name).is_file() for name in (icon.ICO_NAME, icon.ICO_NAME_OFF)}
        missing = [name for name, present in found.items() if not present]
        record("gui_icons", not missing, f"{directory} -> " + (", ".join(found) if not missing else f"缺少 {missing}"))
    except Exception as exc:
        record("gui_icons", False, f"{type(exc).__name__}: {exc}")

    # 4) tkinter 真的能起一个根窗口 —— tcl/tk 数据文件没打进去的典型症状是
    #    "Can't find a usable init.tcl", 而那个错误只在创建窗口时才报。
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()  # 别闪一个窗
        root.update_idletasks()
        root.destroy()
        record("tkinter", True, f"Tk {tkinter.TkVersion} 可用")
    except Exception as exc:
        record("tkinter", False, f"{type(exc).__name__}: {exc}")

    # 5) 主窗口模块(缺 app.py 时 GUI 会退化成网页控制台, 值得单独报出来)
    try:
        from accesspilot.gui import app

        record("gui_app", hasattr(app, "App"), f"App={'有' if hasattr(app, 'App') else '缺失'}")
    except Exception as exc:
        record("gui_app", False, f"{type(exc).__name__}: {exc}")

    # 6) 托盘模块。托盘挂不起来时程序会自动降级成普通窗口 —— 那是设计好的行为,
    #    但"模块压根没打进包"是打包漏收, 必须报出来, 否则会静默少一个入口。
    try:
        from accesspilot.gui import tray

        record("gui_tray", hasattr(tray, "Tray"), "Tray 可导入")
    except Exception as exc:
        record("gui_tray", False, f"{type(exc).__name__}: {exc}")

    # 7) 可选模块: health.py 由别的 teammate 提供, 没有不算失败
    try:
        from accesspilot import health  # noqa: F401

        record("health_module", True, "已随包提供", required=False)
    except Exception as exc:
        record("health_module", True, f"未随包提供({type(exc).__name__}), 属正常降级", required=False)

    failures = [
        name for name, item in report["checks"].items() if item["required"] and not item["ok"]
    ]
    report["ok"] = not failures
    report["failed"] = failures

    try:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    return 0 if report["ok"] else 1


# --------------------------------------------------------------------------- #
# 2) + 3) 参数规整与派发
# --------------------------------------------------------------------------- #


def _normalized_argv() -> list[str]:
    """剥掉 `-m accesspilot` 前缀, 并把"只有界面开关"视为"启动界面"。"""
    args = list(sys.argv[1:])
    if len(args) >= 2 and args[0] == "-m" and args[1] in _MODULE_PREFIXES:
        args = args[2:]
    if args and args[0] in _GUI_FLAGS:
        args = ["gui", *args]
    return args


def _subcommands() -> set[str]:
    """cli 当前注册了哪些子命令(拿不到就返回空集合, 交给降级逻辑)。"""
    try:
        import argparse

        from accesspilot.cli import build_parser

        for action in build_parser()._actions:
            if isinstance(action, argparse._SubParsersAction):
                return set(action.choices)
    except Exception:
        pass
    return set()


def _run_gui_package(argv: list[str]) -> int | None:
    """退路: cli 里还没有 `gui` 子命令时, 直接调 accesspilot.gui。

    返回 None 表示"界面模块压根没装上", 调用方应继续往下降级。
    """
    try:
        from accesspilot.gui import main as gui_main
    except Exception as exc:
        try:
            sys.stderr.write(f"[i] 桌面界面不可用({type(exc).__name__}: {exc}), 改用网页控制台\n")
        except Exception:
            pass
        return None
    return int(gui_main(list(argv)) or 0)


def main() -> int:
    # 先算出这次要干什么, 再决定要不要挂父控制台 —— GUI 路径挂控制台会让
    # 主窗口以最小化状态出现(见 _install_streams 里的实测记录), 而命令行
    # 子命令需要控制台才能打印。_normalized_argv() 是纯函数, 先调它没有副作用。
    argv = _normalized_argv() or ["gui"]
    want_console = argv[0] != "gui"
    mode = _install_streams(want_console=want_console)
    _ensure_source_path()

    # 打包自检: 由 packaging/smoke_test.py 驱动, 普通用户不会用到这个开关
    if (argv and argv[0] == "--selftest") or os.environ.get("HONGXING_SELFTEST") == "1":
        return _selftest()

    try:
        from accesspilot.cli import main as cli_main
    except Exception as exc:
        return _fatal(
            f"无法加载 AccessPilot 运行时: {type(exc).__name__}: {exc}",
            "打包产物不完整或已损坏, 请重新下载。",
        )

    try:
        if argv == ["gui"] and "gui" not in _subcommands():
            # 优雅降级: 界面包 → 网页控制台。构建时 gui 可能还没写完。
            code = _run_gui_package([])
            if code is None:
                code = int(cli_main(["dashboard", "--open"]) or 0)
            return code
        return int(cli_main(argv) or 0)
    except SystemExit as exc:  # argparse 的 --version / --help / 参数错误走这里
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        traceback.print_exc()
        return _fatal(
            f"{type(exc).__name__}: {exc}",
            f"启动方式: 输出流={mode}, 参数={argv!r}",
        )


if __name__ == "__main__":
    raise SystemExit(main())
