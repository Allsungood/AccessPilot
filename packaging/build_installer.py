#!/usr/bin/env python
"""构建红杏安装程序(单文件 Setup.exe)。

用法
====
    python packaging/build_installer.py                 # 轻量包: 只含主程序(约 13 MB)
    python packaging/build_installer.py --full          # 完整包: 带上内核, 离线可用(约 70 MB)
    python packaging/build_installer.py --full --core "D:\\somewhere\\core"

它做三件事
==========
1. 准备 payload/: 主程序 + version.txt (+ 内核, 如果用 --full)
2. 调 PyInstaller(installer.spec)把安装器冻成单文件
3. 拷成中文名 `dist/红杏-Setup-v<版本>[-full].exe`, 打印体积与 SHA256

前置: 主程序得先有 —— 也就是 `python packaging/build.py` 的产物 `dist/红杏.exe`。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
SPEC = ROOT / "packaging" / "installer.spec"
PAYLOAD = ROOT / "build" / "installer-payload"
APP_EXE_NAMES = ("红杏.exe", "hongxing.exe")
CORE_FILES = ("mihomo.exe", "wintun.dll")
#: 内核启动必需的地理数据(缺了内核直接起不来, 所以离线即用包必须带上)
GEODATA_FILES = ("geosite.dat", "geoip.metadb", "country.mmdb")


def say(message: str = "") -> None:
    print(message, flush=True)


def use_utf8() -> None:
    """中文输出不受控制台代码页影响(否则在管道/CI 里会看到乱码)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def app_version() -> str:
    try:
        sys.path.insert(0, str(ROOT))
        from accesspilot import __version__  # noqa: PLC0415

        return str(__version__)
    except Exception:
        return "0.0.0"


def find_app_exe(explicit: str) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"[x] 找不到主程序: {path}")
        return path
    for name in APP_EXE_NAMES:
        candidate = DIST / name
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "[x] dist/ 下没有主程序。先跑:\n"
        "        python packaging/build.py\n"
        "    或用 --app-exe 指定一个 exe。"
    )


def default_core_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    return Path(base) / "AccessPilot" / "core"


def default_home_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    return Path(base) / "AccessPilot"


def stage_offline_extras(home: Path, *, with_cache: bool) -> None:
    """为"装完即用"准备离线资源: geodata / 节点档 / state.json(去密钥)。

    只有内核是不够的 —— 新机器上还缺:
      * geodata: 内核启动时加载, 缺了就 exit;
      * profiles/: 没有节点就无从连接;
      * state.json: 记录 active_profile(用哪个档)与选中节点。
    """
    runtime = home / "runtime"
    geodata_dst = PAYLOAD / "geodata"
    geodata_dst.mkdir(parents=True, exist_ok=True)
    got = 0
    for name in GEODATA_FILES:
        source = runtime / name
        if source.is_file():
            shutil.copy2(source, geodata_dst / name)
            say(f"[1/3] payload: 地理数据 {name} ({source.stat().st_size / 1048576:.1f} MB)")
            got += 1
    if not got:
        raise SystemExit(
            f"[x] {runtime} 里没有地理数据({GEODATA_FILES})—— 没有它内核起不来。\n"
            "    先在本机跑一次 accesspilot init 再打包。"
        )

    profiles_src = home / "profiles"
    if profiles_src.is_dir():
        dst = PAYLOAD / "profiles"
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for item in sorted(profiles_src.glob("*.json")):
            shutil.copy2(item, dst / item.name)
            n += 1
        say(f"[1/3] payload: 节点档 {n} 个 ({sum(f.stat().st_size for f in dst.iterdir()) / 1048576:.1f} MB)")

    state_src = home / "state.json"
    if state_src.is_file():
        shutil.copy2(state_src, PAYLOAD / "state.json")
        say("[1/3] payload: state.json(安装时会重新随机生成 api_secret)")

    if with_cache:
        ruleset_src = runtime / "ruleset"
        if ruleset_src.is_dir():
            dst = PAYLOAD / "ruleset"
            dst.mkdir(parents=True, exist_ok=True)
            n = 0
            for item in sorted(ruleset_src.glob("*.yaml")):
                shutil.copy2(item, dst / item.name)
                n += 1
            say(f"[1/3] payload: 规则集缓存 {n} 个(首次启动更快、更少依赖网络)")


def stage_payload(*, app_exe: Path, version: str, core_dir: Path | None, home: Path, with_cache: bool) -> None:
    if PAYLOAD.exists():
        shutil.rmtree(PAYLOAD)
    PAYLOAD.mkdir(parents=True, exist_ok=True)
    shutil.copy2(app_exe, PAYLOAD / "红杏.exe")
    (PAYLOAD / "version.txt").write_text(version + "\n", encoding="utf-8")
    say(f"[1/3] payload: 主程序 {app_exe.name} ({app_exe.stat().st_size / 1048576:.1f} MB)")
    if core_dir is None:
        say("[1/3] payload: 不带内核 -> 轻量包(首次使用需 accesspilot init)")
        return
    if not core_dir.is_dir():
        raise SystemExit(f"[x] 内核目录不存在: {core_dir}")
    target = PAYLOAD / "core"
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in CORE_FILES:
        source = core_dir / name
        if source.is_file():
            shutil.copy2(source, target / name)
            say(f"[1/3] payload: 内核 {name} ({source.stat().st_size / 1048576:.1f} MB)")
            copied += 1
    if not copied:
        raise SystemExit(f"[x] {core_dir} 里没有 {CORE_FILES} —— 完整包做不出来")
    # 完整包 = 内核 + 离线资源, 目标是"装完就能连"(不跑 init、不联网下数据)
    stage_offline_extras(home, with_cache=with_cache)


def run_pyinstaller(*, name: str, full: bool) -> int:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--distpath",
        str(DIST),
        "--workpath",
        str(ROOT / "build"),
        "--log-level",
        "WARN",
    ]
    env = os.environ.copy()
    env["HONGXING_INSTALLER_PAYLOAD"] = str(PAYLOAD)
    env["HONGXING_INSTALLER_NAME"] = name
    env["PYTHONUTF8"] = "1"
    say(f"[2/3] PyInstaller 构建 {name} ({'完整包' if full else '轻量包'}) …")
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env)
    return proc.returncode


def publish(*, name: str, full: bool, version: str) -> Path | None:
    source = DIST / f"{name}.exe"
    if not source.is_file():
        say(f"[x] 没找到产物 {source}")
        return None
    suffix = "-full" if full else ""
    target = DIST / f"红杏-Setup-v{version}{suffix}.exe"
    shutil.copy2(source, target)
    size = target.stat().st_size
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    say("")
    say("[√] 安装程序就绪")
    say(f"    产物:   {target}")
    say(f"    体积:   {size:,} 字节 ({size / 1048576:.1f} MiB)")
    say(f"    SHA256: {digest}")
    say(f"    形态:   {'完整包(带内核, 离线可用)' if full else '轻量包(不含内核, 首次需 accesspilot init)'}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python packaging/build_installer.py", description="构建红杏安装程序")
    parser.add_argument("--full", action="store_true", help="带上内核(完整包)")
    parser.add_argument("--app-exe", default="", help="主程序路径(默认 dist/红杏.exe)")
    parser.add_argument("--core", default="", help="内核目录(默认 %%LOCALAPPDATA%%\\AccessPilot\\core)")
    parser.add_argument("--home", default="", help="取资源的数据目录(默认 %%LOCALAPPDATA%%\\AccessPilot)")
    parser.add_argument("--with-cache", action="store_true", help="连规则集缓存一起打(首次启动更快, 包大 ~13 MB)")
    parser.add_argument(
        "--version",
        default="",
        help="覆盖版本号(默认取包里的 __version__); 发布时建议与 git tag 一致, 例如 --version 0.9.0",
    )
    args = parser.parse_args(argv)

    use_utf8()
    version = args.version or app_version()
    app_exe = find_app_exe(args.app_exe)
    home = Path(args.home).expanduser().resolve() if args.home else default_home_dir()
    core_dir = None
    if args.full:
        core_dir = Path(args.core).expanduser().resolve() if args.core else default_core_dir()

    stage_payload(
        app_exe=app_exe,
        version=version,
        core_dir=core_dir,
        home=home,
        with_cache=args.with_cache,
    )
    name = "hongxing-setup-full" if args.full else "hongxing-setup-lite"
    if run_pyinstaller(name=name, full=args.full) != 0:
        say("[x] PyInstaller 失败")
        return 1
    say("[3/3] 收尾")
    return 0 if publish(name=name, full=args.full, version=version) else 1


if __name__ == "__main__":
    raise SystemExit(main())
