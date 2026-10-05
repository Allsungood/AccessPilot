# -*- mode: python ; coding: utf-8 -*-
"""红杏 安装程序 的 PyInstaller 规格.

payload/ 里装的是"要被安装的东西"(由 packaging/build_installer.py 准备好):

    payload/红杏.exe            主程序(必需)
    payload/version.txt         版本号
    payload/core/mihomo.exe     内核  ) 有 -> 完整包(离线可用)
    payload/core/wintun.dll     驱动  ) 无 -> 轻量包(首次使用需 accesspilot init)

为什么要单独一个 spec 而不是复用 hongxing.spec: 两者冻结的是**不同的东西** ——
hongxing.spec 冻的是红杏本体(用户日常跑的那个), 这个冻的是安装器。
安装器是 console=True(要让用户看到安装过程), 红杏本体是 console=False。
"""
import os
import re
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
ENTRY = ROOT / "packaging" / "installer.py"
PAYLOAD = Path(os.environ.get("HONGXING_INSTALLER_PAYLOAD", str(ROOT / "build" / "installer-payload")))
NAME = os.environ.get("HONGXING_INSTALLER_NAME", "hongxing-setup")
#: 用户实际下载到的那个文件名(`红杏-Setup-v1.0.0.exe` / `-full.exe`), 由
#: build_installer.py 传进来。spec 自己算不出中文名和 -full 后缀, 所以只能由外面给;
#: 给不到就退化成英文内部名(不影响功能, 只是"属性 -> 详细信息"里不好看)。
FILENAME = os.environ.get("HONGXING_INSTALLER_FILENAME", f"{NAME}.exe")

if not ENTRY.is_file():
    raise SystemExit(f"缺少入口脚本: {ENTRY}")
if not PAYLOAD.is_dir():
    raise SystemExit(
        f"payload 目录不存在: {PAYLOAD}\n"
        "    先跑: python packaging/build_installer.py [--full]"
    )


def _app_version():
    """版本号取自 payload/version.txt(安装器实际会写进注册表/程序目录的那个值),
    拿不到才回退到 `accesspilot/__init__.py`。

    为什么不直接读 `__init__.py`: 这个 spec 冻的是安装器, 它对外宣称的版本必须
    与 build_installer.py 传进来的完全一致。两者不一致时(比如有人在 build 之后
    手工改了 payload)宁可按 payload 走 —— 至少"属性里看到的版本"和"装了之后
    「设置→应用」显示的版本"是同一个数, 而不是两个数。
    """
    version_file = PAYLOAD / "version.txt"
    try:
        text = version_file.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    try:
        source = (ROOT / "accesspilot" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', source)
    return match.group(1) if match else "0.0.0"


def _version_resource(version):
    """给 Setup.exe 加版本资源。

    hongxing.spec 一直有这一段, 而这个 spec 原来 `version=` / `icon=` 都没传 ——
    结果是用户**第一个下载的那个文件**在"属性 -> 详细信息"里一片空白: 看不出是
    什么产品、哪个版本, 企业资产盘点/杀软信誉也拿不到任何信息。装完之后
    「设置 -> 应用」里显示的版本(installer.py 的 register_uninstall)本来就有,
    只有 exe 自己没有 —— 这就是纯疏漏。
    生成失败不影响构建(版本资源永远不该让构建挂掉)。
    """
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
            ("FileDescription", "红杏 安装程序 - 安装 / 卸载 / 更新"),
            ("FileVersion", version),
            ("InternalName", NAME),
            ("OriginalFilename", FILENAME),
            ("ProductName", "红杏 安装程序"),
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
    except Exception as exc:  # noqa: BLE001
        print(f"[installer.spec] 版本资源生成失败, 已跳过: {type(exc).__name__}: {exc}")
        return None


VERSION = _app_version()

# 图标与主程序同一个 .ico: 用户在"下载 -> 属性"里看到的就是产品图标, 而不是
# PyInstaller 的默认图标。找不到就不带(不致命)。
ICON = None
_candidate = ROOT / "accesspilot" / "gui" / "assets" / "hongxing.ico"
if _candidate.is_file():
    ICON = str(_candidate)
else:
    print(f"[installer.spec] 未找到 {_candidate}, 本次构建不带自定义图标(不致命)")

# 把 payload/ 下所有文件按原结构打进包, 运行时在 sys._MEIPASS/payload 下
DATAS = []
for _path in sorted(PAYLOAD.rglob("*")):
    if _path.is_file():
        _dest = Path("payload") / _path.relative_to(PAYLOAD).parent
        DATAS.append((str(_path), str(_dest)))

_total = sum(Path(src).stat().st_size for src, _ in DATAS)
print(f"[installer.spec] 安装器版本: {VERSION}   图标: {ICON or '(无)'}")
print(f"[installer.spec] payload {len(DATAS)} 个文件, 共 {_total / 1048576:.1f} MB")
for src, dest in DATAS:
    print(f"[installer.spec]   {Path(src).name:<24} -> {dest}")

a = Analysis(  # noqa: F821 - PyInstaller 注入
    [str(ENTRY)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 注意: **不能排除 tkinter** —— 安装器的图形向导就是它实现的项目本来就零依赖用 Tk,
    # 这里排除掉的话, 双击 Setup.exe 会退化成控制台安装。
    excludes=["pytest", "numpy", "PIL", "pandas", "matplotlib", "IPython", "test"],
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
    upx=False,
    runtime_tmpdir=None,
    # 安装过程要让用户看见(装到哪、装没装内核、快捷方式成没成)
    console=True,
    disable_windowed_traceback=False,
    # 与主程序同一个图标 + 一份真实的版本资源(见 _version_resource 的说明)
    icon=ICON,
    version=_version_resource(VERSION),
)
