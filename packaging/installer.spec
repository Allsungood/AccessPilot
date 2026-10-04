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
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
ENTRY = ROOT / "packaging" / "installer.py"
PAYLOAD = Path(os.environ.get("HONGXING_INSTALLER_PAYLOAD", str(ROOT / "build" / "installer-payload")))
NAME = os.environ.get("HONGXING_INSTALLER_NAME", "hongxing-setup")

if not ENTRY.is_file():
    raise SystemExit(f"缺少入口脚本: {ENTRY}")
if not PAYLOAD.is_dir():
    raise SystemExit(
        f"payload 目录不存在: {PAYLOAD}\n"
        "    先跑: python packaging/build_installer.py [--full]"
    )

# 把 payload/ 下所有文件按原结构打进包, 运行时在 sys._MEIPASS/payload 下
DATAS = []
for _path in sorted(PAYLOAD.rglob("*")):
    if _path.is_file():
        _dest = Path("payload") / _path.relative_to(PAYLOAD).parent
        DATAS.append((str(_path), str(_dest)))

_total = sum(Path(src).stat().st_size for src, _ in DATAS)
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
    icon=None,
)
