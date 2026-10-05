# -*- mode: python ; coding: utf-8 -*-
"""红杏 单文件 exe 的 PyInstaller 规格文件.

设计要点
========
* **onefile**: 用户机器上只需要一个 `红杏.exe`, 没有一堆 dll 和目录。
* **资源自动收集**: 不去手写一张容易过期的资源清单, 而是遍历 `accesspilot/`
  下所有"非 Python 代码"的文件, 按原目录结构打进包里 —— 这样以后谁往
  `accesspilot/**/assets/` 里加东西, 不需要记得回来改 spec。
  同时把**必须存在的**几个资源单独列出来做硬校验(见 REQUIRED_DATA),
  缺了会在构建日志里大喊, 而不是等用户双击之后白屏。
* **优雅降级**: 图标 / 可选资源缺失时只警告, 不让构建失败 —— 一个资源缺失不该
  让你连构建日志都拿不到。但**缺失必须当成构建缺陷处理**: `accesspilot/gui/` 与
  `accesspilot/health.py` 都已经完整落地, 这三个 `REQUIRED_DATA` 没有任何"暂时
  还没有"的正当理由, 看到警告就重新跑一次 `python packaging/build.py`。
* **console=False**, 但 `HONGXING_CONSOLE=1` 可以产出带控制台的排错版
  (见 packaging/build.py --debug)。

为什么资源收集是这里最要命的一步
================================
PyInstaller onefile 会把数据解到临时目录(运行时是 `sys._MEIPASS`), 源码里
任何 `Path(__file__).parent / "assets"` 都必须在包里能找到对应文件, 否则
源码里跑得好好的功能, 到了用户机器上就是"缺文件"。当前源码里被读取的资源:

    accesspilot/assets/dashboard.html        <- webgui.ASSET_DIR (__file__)
    accesspilot/gui/assets/hongxing.ico      <- gui/icon.py assets_dir()
    accesspilot/gui/assets/hongxing_off.ico  <- 同上(未连接状态)

`gui/icon.py` 已经自己处理了 `sys._MEIPASS`; `webgui.py` 用的是
`Path(__file__).parent`, 冻结后正好指向 `_MEIPASS/accesspilot/`, 所以只要
**目录结构保持一致**就能对上。
"""
import os
import re
from pathlib import Path

# --------------------------------------------------------------------------- #
# 路径与变体
# --------------------------------------------------------------------------- #

#: PyInstaller 注入的全局变量: 本文件所在目录(<仓库>/packaging)
ROOT = Path(SPECPATH).resolve().parent
PKG = ROOT / "accesspilot"
ENTRY = ROOT / "packaging" / "entry.py"

#: 带控制台的排错版(build.py --debug 会设它)
DEBUG_CONSOLE = os.environ.get("HONGXING_CONSOLE", "") == "1"
NAME = "hongxing-debug" if DEBUG_CONSOLE else "hongxing"


def log(message):
    print(f"[hongxing.spec] {message}")


if not ENTRY.is_file():
    raise SystemExit(f"入口脚本不存在: {ENTRY}")
if not PKG.is_dir():
    raise SystemExit(f"accesspilot 包不存在: {PKG}")


# --------------------------------------------------------------------------- #
# 资源收集
# --------------------------------------------------------------------------- #

_SKIP_DIRS = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".git"}
_CODE_SUFFIXES = {".py", ".pyc", ".pyo", ".pyd", ".pyi"}

#: 运行时真的会被读到的资源。缺任何一个, 打包版都会在用户机器上功能残缺,
#: 所以这里单独校验并大声报警。第二项是"为什么必需"。
REQUIRED_DATA = {
    "accesspilot/assets/dashboard.html": "webgui.dashboard_html() 读取, 缺了控制台首页白板",
    "accesspilot/gui/assets/hongxing.ico": "gui/icon.py 托盘/窗口图标(已连接)",
    "accesspilot/gui/assets/hongxing_off.ico": "gui/icon.py 托盘图标(未连接)",
}


def collect_package_data():
    """把 accesspilot/ 下的全部非代码文件按原目录结构收集为 datas。

    datas 元素是 (源文件绝对路径, 包内目标目录)。目标目录保持相对仓库根的
    结构, 这样 `_MEIPASS/accesspilot/assets/dashboard.html` 这类
    `__file__` 相对路径在冻结后依然成立。
    """
    collected = []
    for path in sorted(PKG.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in _CODE_SUFFIXES:
            continue
        collected.append((str(path), str(path.relative_to(ROOT).parent)))
    return collected


def collect_submodules():
    """按文件系统列出 accesspilot 的所有子模块, 作为 hiddenimports。

    为什么不直接用 `collect_submodules("accesspilot")`: 那个函数会真的去
    import 一遍。延迟导入的模块(例如只在需要托盘时才 import 的
    `accesspilot/gui/tray.py`)在"当前环境跑不起来"时会被漏掉, 而漏掉的后果是
    用户机器上 `ModuleNotFoundError`。扫文件名不会被执行环境带偏。
    """
    modules = set()
    for path in sorted(PKG.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            modules.add(".".join(parts))
    return sorted(modules)


#: GUI 侧有些模块是运行期才 import 的(tkinter 的子模块、ctypes.wintypes),
#: 静态分析不一定全抓得到, 这里显式列上。它们都是标准库, 不存在时会警告但不致命。
EXTRA_HIDDENIMPORTS = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.constants",
    "tkinter.messagebox",
    "tkinter.filedialog",
    "tkinter.simpledialog",
    "tkinter.colorchooser",
    "tkinter.scrolledtext",
    "ctypes.wintypes",
]

#: 明确排除的"大而无用"包: 本项目的运行时零第三方依赖, 出现它们只可能是
#: 构建机上装了别的包被顺带扫进来。排除能显著减小体积。
EXCLUDES = [
    "pytest",
    "numpy",
    "PIL",
    "pandas",
    "matplotlib",
    "IPython",
    "notebook",
    "pydoc_data",
    "lib2to3",
    "setuptools",
    "pip",
    "wheel",
    "test",
]


def _app_version():
    """从 accesspilot/__init__.py 里读版本号(正则, 不 import)。"""
    try:
        text = (PKG / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def _version_resource(version):
    """给 exe 加"属性 -> 详细信息"里的版本资源(纯锦上添花, 失败不影响构建)。"""
    try:
        from PyInstaller.utils.win32.versioninfo import (
            FixedFileInfo,
            StringFileInfo,
            StringStruct,
            StringTable,
            VarFileInfo,
            VarStruct,
            VSVersionInfo,
        )

        numbers = [int(part) for part in re.findall(r"\d+", version)[:4]]
        while len(numbers) < 4:
            numbers.append(0)
        quad = tuple(numbers)
        strings = [
            ("CompanyName", "红杏 / AccessPilot"),
            ("FileDescription", "红杏 - 一键翻墙客户端"),
            ("FileVersion", version),
            ("InternalName", NAME),
            ("OriginalFilename", "红杏.exe"),
            ("ProductName", "红杏"),
            ("ProductVersion", version),
            ("LegalCopyright", "AccessPilot"),
        ]
        return VSVersionInfo(
            ffi=FixedFileInfo(
                filevers=quad, prodvers=quad, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1
            ),
            kids=[
                StringFileInfo(
                    [StringTable("080404B0", [StringStruct(k, v) for k, v in strings])]
                ),
                VarFileInfo([VarStruct("Translation", [0x0804, 1200])]),
            ],
        )
    except Exception as exc:  # noqa: BLE001 - 版本资源永远不该让构建失败
        log(f"版本资源生成失败, 已跳过: {type(exc).__name__}: {exc}")
        return None


# --------------------------------------------------------------------------- #
# 开始收集
# --------------------------------------------------------------------------- #

VERSION = _app_version()
DATAS = collect_package_data()
HIDDEN = collect_submodules() + EXTRA_HIDDENIMPORTS

# --- 图标: 由 packaging/build.py 尽量提前生成; 这里没有就优雅跳过 ---
ICON = None
for candidate in (PKG / "gui" / "assets" / "hongxing.ico",):
    if candidate.is_file():
        ICON = str(candidate)
        break
if ICON is None:
    log("!! 未找到 accesspilot/gui/assets/hongxing.ico, 本次构建不带自定义图标(不致命)")
    log("   (该文件由 accesspilot/gui/icon.py 生成: python packaging/build.py 会自动尝试)")

# --- 缺失资源报警 ---
present = {str(Path(src).relative_to(ROOT)).replace("\\", "/") for src, _ in DATAS}
missing = [(key, why) for key, why in REQUIRED_DATA.items() if key not in present]
if missing:
    log("=" * 72)
    log("!! 警告: 以下运行时资源没有被打进 exe, 打包版会在用户机器上功能残缺:")
    for key, why in missing:
        log(f"!!   - {key}  ({why})")
    log("!! 构建仍然继续(缺资源不该让你连日志都拿不到), 但**这个包不要发布**。")
    log("!! 缺资源是构建缺陷, 不是「别人还没写完」: 重新执行 python packaging/build.py。")
    log("=" * 72)
else:
    log(f"运行时资源校验通过: {len(REQUIRED_DATA)}/{len(REQUIRED_DATA)} 个必需资源已收集")

log(f"版本: {VERSION}   变体: {NAME} (console={DEBUG_CONSOLE})")
log(f"数据文件 {len(DATAS)} 个:")
for src, dest in DATAS:
    size = Path(src).stat().st_size
    log(f"    {Path(src).relative_to(ROOT)!s:<48} -> {dest}  ({size:,} 字节)")
log(f"hiddenimports {len(HIDDEN)} 个: {', '.join(HIDDEN)}")


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #

a = Analysis(  # noqa: F821 - PyInstaller 注入
    [str(ENTRY)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # 不启用 UPX: 压缩后的 exe 更容易被国内安全软件误报, 体积不是这个项目的瓶颈
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=DEBUG_CONSOLE,
    # False = 崩溃时弹一个带 traceback 的对话框。对"非技术用户 + 没有控制台"
    # 的场景, 这是唯一能让用户把错误信息截图发过来的途径。
    disable_windowed_traceback=False,
    icon=ICON,
    version=_version_resource(VERSION),
)
