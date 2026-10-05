#!/usr/bin/env python
"""红杏 一键打包: 把 AccessPilot 做成"双击就能用"的单文件 exe.

用法
====
    python packaging/build.py                 # 发布版 -> dist/红杏.exe (无控制台)
    python packaging/build.py --debug         # 排错版 -> dist/红杏-debug.exe (带控制台)
    python packaging/build.py --clean         # 构建前清掉 build/ 缓存(换了依赖再用)
    python packaging/build.py --no-icon       # 不尝试生成图标

这个脚本做四件事
================
1. 先检查 PyInstaller 在不在 —— 不在就给出**可照抄的安装命令**, 而不是让
   `python -m PyInstaller` 抛一句 ModuleNotFoundError 让人猜。
2. 尽量把 `accesspilot/gui/assets/hongxing.ico` 生成出来(调 gui/icon.py 的
   ensure_ico, 幂等)。拿不到就跳过, 不让构建失败 —— 但会在报告里点明这是
   构建缺陷(装了没有图标的 exe 不值得发), 而不是"别人还没写完"。
3. 调 PyInstaller 跑 `packaging/hongxing.spec`(onefile), 实时转发构建日志。
4. 把产物拷成中文名 `dist/红杏.exe`, 打印**绝对路径 / 字节数 / SHA256**。

构建失败时会把日志尾部打出来并给出常见原因, 而不是只丢一个非零退出码。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "accesspilot"
SPEC = ROOT / "packaging" / "hongxing.spec"
ENTRY = ROOT / "packaging" / "entry.py"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
BUILD_LOG = BUILD / "pyinstaller.log"

#: 国内直连 PyPI 经常超时, 这里给一条能用的镜像, 免得第一步就卡死
PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

#: spec 里 name= 的英文名 -> 拷给用户的最终中文名
ARTIFACTS = {
    False: ("hongxing.exe", "红杏.exe"),
    True: ("hongxing-debug.exe", "红杏-debug.exe"),
}

EXIT_PREFLIGHT = 2
EXIT_BUILD = 3
EXIT_PUBLISH = 4


def app_version() -> str:
    """版本号唯一来源: `accesspilot/__init__.py` 的 `__version__`(正则, 不 import)。

    不 import 是有意的: 这个脚本要在"源码可能还没完全就绪"的时候也能跑起来,
    读一行文本比执行整个包安全。
    """
    try:
        text = (PKG / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def say(message: str = "") -> None:
    print(message, flush=True)


def use_utf8() -> None:
    """让本脚本自己的中文输出不受控制台代码页影响(Hook: 构建日志要走 utf-8)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 1) 前置检查
# --------------------------------------------------------------------------- #


def check_pyinstaller() -> bool:
    import importlib.util

    try:
        spec = importlib.util.find_spec("PyInstaller")
    except Exception:
        spec = None
    if spec is None:
        say("=" * 70)
        say("[x] 没有找到 PyInstaller —— 它是**打包期**工具, 不装就没法生成 exe。")
        say("")
        say("    装它(如果直连 PyPI 超时, 用国内镜像):")
        say(f"        python -m pip install pyinstaller")
        say(f"        python -m pip install -i {PIP_MIRROR} pyinstaller")
        say("")
        say("    注意: 装了它**不会**给红杏增加运行时依赖。用户机器上依然不需要")
        say("    装 Python 和任何第三方包, 详见 packaging/README.md。")
        say("=" * 70)
        return False
    try:
        from PyInstaller import __version__ as pyi_version

        say(f"[1/4] PyInstaller {pyi_version} 已就绪 (Python {sys.version.split()[0]})")
    except Exception:
        say("[1/4] PyInstaller 已就绪")
    if sys.version_info < (3, 9):
        say(f"[!] Python {sys.version.split()[0]} 偏旧, 建议 3.9+ (冻结的是当前解释器)")
    return True


def check_layout() -> bool:
    problems = []
    for path in (SPEC, ENTRY, PKG / "__init__.py"):
        if not path.is_file():
            problems.append(f"缺少 {path.relative_to(ROOT)}")
    if problems:
        for line in problems:
            say(f"[x] {line}")
        return False
    return True


# --------------------------------------------------------------------------- #
# 2) 图标(可选)
# --------------------------------------------------------------------------- #


def ensure_icons(*, enabled: bool) -> None:
    if not enabled:
        say("[2/4] 按 --no-icon, 跳过图标生成")
        return
    if not (PKG / "gui" / "icon.py").is_file():
        say("[2/4] [!] accesspilot/gui/icon.py 不存在 —— 这是构建缺陷(它应该已经落地),")
        say("      本次用 PyInstaller 默认图标继续。请确认检出的源码是完整的。")
        return

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from accesspilot.gui import icon  # noqa: PLC0415 - 延迟导入: 允许它此刻还不存在
    except Exception as exc:  # noqa: BLE001
        say(f"[2/4] 无法导入 accesspilot.gui.icon({type(exc).__name__}: {exc}), 跳过图标生成")
        return

    say("[2/4] 图标")
    for connected, label in ((True, "已连接"), (False, "未连接")):
        try:
            path = icon.ensure_ico(connected=connected)
            size = path.stat().st_size if path.is_file() else 0
            say(f"      {label}: {path}  ({size:,} 字节)")
        except Exception as exc:  # noqa: BLE001
            say(f"      {label}: 生成失败({type(exc).__name__}: {exc}), 跳过")


# --------------------------------------------------------------------------- #
# 3) 构建
# --------------------------------------------------------------------------- #


def run_pyinstaller(*, debug: bool, clean: bool) -> tuple[int, list[str]]:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
        "--log-level",
        "INFO",
    ]
    if clean:
        cmd.append("--clean")

    env = os.environ.copy()
    env["HONGXING_CONSOLE"] = "1" if debug else "0"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    say(f"[3/4] 开始构建 ({'带控制台的排错版' if debug else '发布版, 无控制台'})")
    say(f"      $ {' '.join(cmd)}")
    say(f"      工作目录: {ROOT}")
    say("-" * 70)

    started = time.time()
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        say(f"[x] 无法启动 PyInstaller: {exc}")
        return EXIT_BUILD, []

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        lines.append(line)
        say(f"      | {line}")
    code = proc.wait()
    elapsed = time.time() - started
    say("-" * 70)
    say(f"      PyInstaller 退出码 {code}, 耗时 {elapsed:.1f}s")

    BUILD.mkdir(parents=True, exist_ok=True)
    try:
        BUILD_LOG.write_text("\n".join(lines), encoding="utf-8")
        say(f"      完整构建日志: {BUILD_LOG}")
    except OSError:
        pass
    return code, lines


def diagnose(code: int, lines: list[str]) -> None:
    """构建失败时给可读的原因, 而不是只说"失败了"。"""
    say("")
    say("=" * 70)
    say(f"[x] 构建失败 (PyInstaller 退出码 {code})。最后 30 行日志:")
    for line in lines[-30:]:
        say(f"      | {line}")
    say("")
    say("常见原因:")
    say("  * 缺模块: 日志里的 'ModuleNotFoundError' / 'Hidden import ... not found'")
    say("      -> 往 packaging/hongxing.spec 的 EXTRA_HIDDENIMPORTS 里补")
    say("  * 资源路径: 日志里的 'hongxing.spec' 警告行")
    say("      -> REQUIRED_DATA 少文件时会在收集阶段就打警告")
    say("  * 文件被占用: 上一次构建的 dist/红杏.exe 还在运行(WinError 5/32)")
    say("      -> 关掉它再构建")
    say("  * 权限/杀软: 安全软件锁住 build/ 或 dist/ 目录, 加白名单或换目录")
    say("=" * 70)


# --------------------------------------------------------------------------- #
# 4) 发布
# --------------------------------------------------------------------------- #


def publish(*, debug: bool) -> Path | None:
    raw_name, final_name = ARTIFACTS[debug]
    source = DIST / raw_name
    if not source.is_file():
        say(f"[x] PyInstaller 报告成功, 但没有找到产物: {source}")
        say(f"    请检查 {DIST} 下实际生成了什么(可能 spec 里的 name= 被改过)。")
        return None

    target = DIST / final_name
    if source.resolve() != target.resolve():
        try:
            shutil.copy2(source, target)
        except PermissionError:
            say(f"[x] 无法写入 {target} —— 文件被占用(红杏.exe 正在运行?)请先退出再重试。")
            return None
        except OSError as exc:
            say(f"[x] 拷贝产物失败: {exc}")
            return None
    if not debug:
        # 再拷一份**发布资产名**。更新器(packaging/installer.py 的 pick_asset)只认
        # 白名单里的文件名, 而 GitHub Release 的附件的名字就是本地文件名 —— 所以
        # 这一步不是"多拷一份好看", 而是让"上传哪个文件"和"更新器找哪个文件"变成
        # 同一个字符串。少了它, 更新器就只能在发布页上猜, 而发布页上同时躺着
        # 安装器 红杏-Setup-*.exe(见 PKG-02)。
        release_asset = DIST / f"红杏-v{app_version()}-win64.exe"
        try:
            shutil.copy2(source, release_asset)
        except OSError as exc:
            say(f"[!] 发布资产名({release_asset.name})没拷成: {exc}")
    return target


def report(path: Path, *, debug: bool) -> None:
    size = path.stat().st_size
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = "(读取失败)"

    say("")
    say("=" * 70)
    say("[√] 构建成功")
    say("=" * 70)
    say(f"    产物:   {path}")
    say(f"    体积:   {size:,} 字节 ({size / 1024 / 1024:.1f} MiB)")
    say(f"    SHA256: {digest}")
    say(f"    形态:   单文件 onefile, {'带控制台(排错版)' if debug else '无控制台(双击即用)'}")
    say("")
    say("    下一步自测:")
    if debug:
        say(f'        & "{path}" --version')
        say(f'        & "{path}" doctor')
    else:
        say(f'        & "{path}" --version      # 会挂到当前终端上打印, 不用开窗口')
        say("        python packaging/smoke_test.py        # 自动冒烟: 启动/资源/子命令")
    say("=" * 70)


# --------------------------------------------------------------------------- #


def main(argv: list[str]) -> int:
    use_utf8()
    parser = argparse.ArgumentParser(
        prog="python packaging/build.py",
        description="把红杏打包成单文件 exe",
    )
    parser.add_argument("--debug", action="store_true", help="产出带控制台的排错版")
    parser.add_argument("--clean", action="store_true", help="构建前清理 build/ 缓存")
    parser.add_argument("--no-icon", action="store_true", help="跳过图标生成")
    args = parser.parse_args(argv)

    say("红杏 打包 (PyInstaller onefile)")
    say(f"仓库: {ROOT}")
    say("")
    if not check_layout():
        return EXIT_PREFLIGHT
    if not check_pyinstaller():
        return EXIT_PREFLIGHT

    ensure_icons(enabled=not args.no_icon)

    code, lines = run_pyinstaller(debug=args.debug, clean=args.clean)
    if code != 0:
        diagnose(code, lines)
        return EXIT_BUILD

    artifact = publish(debug=args.debug)
    if artifact is None:
        return EXIT_PUBLISH
    report(artifact, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
