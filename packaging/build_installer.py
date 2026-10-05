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

两个**发布前必须过**的闸(都是被真实事故换来的):

* **版本闸**: 不传 `--version` 时版本号只来自 `accesspilot/__init__.py`; 传了就必须
  和它一致, 否则拒绝构建(除非显式 `--force-version`)。曾经发出去的安装包叫
  `红杏-Setup-v0.9.0.exe`, 而程序本体早就是 1.0.0 —— 因为版本号在 9 个地方各写
  一份, 没有任何东西对账。
* **凭据闸**: payload 里的 `state.json` 不许带 `api_secret`。曾经发出去的
  Setup.exe 里就冻着维护者本机的真实密钥。

前置: 主程序得先有 —— 也就是 `python packaging/build.py` 的产物 `dist/红杏.exe`。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
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

#: 打包 state.json 时**必须清空**的字段。
#:
#: `api_secret` 是维护者本机 external-controller 的真实凭据。原来这里直接
#: `shutil.copy2(state_src, PAYLOAD / "state.json")`, 于是它被冻进公开发布的
#: Setup.exe, 谁下载都能从二进制里解出来。**把真实密钥发布出去这件事本身不可
#: 接受**, 与"它只监听 127.0.0.1、实际可利用性低"无关 —— 凭据一旦公开就必须当作
#: 已泄露处理; 而且每台机器本来就该有各自不同的密钥。
#:
#: 置成空串而不是删掉键: `accesspilot/state.py:46` 见到空值会自己
#: `secrets.token_hex(16)` 生成一个, 安装器(installer.py:seed_runtime)也会再生成
#: 一次 —— 两道都指向"密钥只在本机诞生", 不依赖任何打包进来的值。
STATE_SECRET_KEYS = ("api_secret",)

#: 纯粹属于打包机运行痕迹的字段, 跟着安装包发给陌生人没有任何意义。
STATE_MACHINE_KEYS = ("last_start",)


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
    """唯一版本来源: `accesspilot/__init__.py` 的 `__version__`。

    读不到就**直接失败**, 不返回 "0.0.0" 之类的占位值 —— 占位值会让构建照常
    产出 `红杏-Setup-v0.0.0.exe` 这种谁也看不懂的东西, 而真正的错误(路径不对、
    包被改名)要等到用户装上才发现。
    """
    try:
        sys.path.insert(0, str(ROOT))
        from accesspilot import __version__  # noqa: PLC0415

        version = str(__version__).strip()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"[x] 读不到 accesspilot.__version__({type(exc).__name__}: {exc})\n"
            "    版本号只有一个来源: accesspilot/__init__.py。读不到就不构建,"
            "免得产出一个版本号是猜出来的安装包。"
        ) from exc
    if not version:
        raise SystemExit("[x] accesspilot.__version__ 是空的, 拒绝构建")
    return version


def exe_file_version(path: Path) -> str:
    """读 exe 版本资源里的 FileVersion(拿不到就返回空串)。

    只用来核对"安装器包进去的主程序"与"安装包自己的版本号"是否一致 —— 这正是
    发出去的 v0.9.0 安装包所暴露的问题: 里面的主程序是 1.0.0, 外层的版本号却是
    0.9.0, 而 `--update` 会拿这个错的基线去比"是不是最新版"。
    读不到不算失败(有些机器上 PowerShell 起不来), 但读到了就必须对上。
    """
    if not is_windows():
        return ""
    script = (
        "$ErrorActionPreference='Stop';"
        f"Write-Output (Get-Item -LiteralPath '{path}').VersionInfo.FileVersion"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    for exe in ("pwsh", "powershell"):
        try:
            proc = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-EncodedCommand", encoded],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=40,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:  # noqa: BLE001
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().splitlines()[-1].strip()
    return ""


def is_windows() -> bool:
    return sys.platform == "win32"


def _ver_tuple(text: str) -> tuple:
    """把 "1.0.0" / "1.0.0.0" 归一成可比较的元组。

    版本资源的 FileVersion 常常是四段(1.0.0.0), 而 `__version__` 是三段, 直接
    比字符串会误报, 所以统一补零到四段再比。
    """
    numbers = []
    for part in str(text).lstrip("vV").replace("-", ".").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        numbers.append(int(digits) if digits else 0)
    return tuple(numbers + [0, 0, 0, 0])[:4]


def sanitize_state(raw: dict) -> dict:
    """剥掉 state.json 里属于**打包机**的字段(见 STATE_SECRET_KEYS 的说明)。"""
    clean = dict(raw)
    for key in STATE_SECRET_KEYS:
        if key in clean:
            clean[key] = ""
    for key in STATE_MACHINE_KEYS:
        clean.pop(key, None)
    # 打包机当时"系统代理是开着的", 那是**这台机器**的状态, 不是新用户的。
    # 不清掉的话, 新用户第一次运行任意一条命令都会走到 heal_if_broken():
    # 它看到 system_proxy_on=true 而内核没跑, 就会去关掉系统代理 ——
    # 如果这位新用户本来用着别的代理, 就被我们顺手关掉了。
    clean["system_proxy_on"] = False
    return clean


def assert_no_secret(path: Path) -> None:
    """发布前的硬断言: payload 里不许有任何形式的真实凭据。

    上面 `sanitize_state()` 是"主动剥掉", 这里是"证明真的剥掉了"。只做前者的话,
    将来谁把拷贝逻辑改回 `shutil.copy2(state_src, ...)`, 没有任何东西会拦他 ——
    一直到某个用户把 Setup.exe 解包为止。
    """
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"[x] payload/state.json 不是合法 JSON, 不能发布: {exc}") from exc
    for key in STATE_SECRET_KEYS:
        value = str(data.get(key) or "").strip()
        if value:
            raise SystemExit(
                f"[x] 拒绝构建: payload/state.json 里带着非空的 {key}(长度 {len(value)})。\n"
                "    这是维护者本机的真实凭据, 一旦随安装包发布就等于已泄露。\n"
                "    打包时必须清空(见 packaging/build_installer.py 的 STATE_SECRET_KEYS)。"
            )
    # 顺带扫一遍原始字节: 密钥被塞进别的字段/嵌套结构里也拦得住
    if re.search(r'api_secret"\s*:\s*"[0-9a-fA-F]{8,}"', text):
        raise SystemExit("[x] 拒绝构建: payload/state.json 的原始文本里还有 api_secret 值")


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
        try:
            raw = json.loads(state_src.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"[x] {state_src} 读不出来, 不能直接打进包里: {exc}") from exc
        dropped = [k for k in (*STATE_SECRET_KEYS, *STATE_MACHINE_KEYS) if k in raw]
        payload = sanitize_state(raw)
        (PAYLOAD / "state.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        assert_no_secret(PAYLOAD / "state.json")
        say(f"[1/3] payload: state.json(已剥掉本机字段: {', '.join(dropped) or '无'})")
        say("      api_secret 置空, 安装时在**目标机器**上重新随机生成")

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


def artifact_name(*, version: str, full: bool) -> str:
    """发布产物名。构建前就要算出来, 因为它要写进 exe 的版本资源
    (`OriginalFilename`) —— 用户在"属性 -> 详细信息"里应该看到自己手上那个文件的
    名字, 而不是构建流水线内部的英文名。"""
    return f"红杏-Setup-v{version}{'-full' if full else ''}.exe"


def run_pyinstaller(*, name: str, full: bool, filename: str) -> int:
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
    env["HONGXING_INSTALLER_FILENAME"] = filename
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
    target = DIST / artifact_name(version=version, full=full)
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
    # 先修输出编码再建 parser: 否则 `--help` 里的中文会按控制台代码页打出去,
    # 在 UTF-8 终端/PowerShell 里就是乱码(argparse 是边解析边打印的)。
    use_utf8()
    parser = argparse.ArgumentParser(prog="python packaging/build_installer.py", description="构建红杏安装程序")
    parser.add_argument("--full", action="store_true", help="带上内核(完整包)")
    parser.add_argument("--app-exe", default="", help="主程序路径(默认 dist/红杏.exe)")
    parser.add_argument("--core", default="", help="内核目录(默认 %%LOCALAPPDATA%%\\AccessPilot\\core)")
    parser.add_argument("--home", default="", help="取资源的数据目录(默认 %%LOCALAPPDATA%%\\AccessPilot)")
    parser.add_argument("--with-cache", action="store_true", help="连规则集缓存一起打(首次启动更快, 包大 ~13 MB)")
    parser.add_argument(
        "--version",
        default="",
        help="覆盖版本号。必须与 accesspilot/__init__.py 的 __version__ 一致, 否则拒绝构建",
    )
    parser.add_argument(
        "--force-version",
        action="store_true",
        help="明知与包内版本不一致也照建(只在刻意造一个版本号不同的包时才用)",
    )
    parser.add_argument(
        "--check-version-only",
        action="store_true",
        help="只做版本一致性检查然后退出(不构建). 给 CI 用 —— 版本号漂移过一次, 不能只靠人记得",
    )
    args = parser.parse_args(argv)

    source_version = app_version()
    version = args.version.strip() or source_version
    if version != source_version and not args.force_version:
        # 这就是 PKG-01 的成因: 版本号写在不同地方, 谁也没对账, 于是发出去的
        # 安装包叫 v0.9.0 而里面的程序是 1.0.0。加了这道闸之后, 想让两者不一致
        # 必须显式写 --force-version, 不可能再"顺手"发生。
        raise SystemExit(
            "[x] 拒绝构建: --version 与包内版本不一致\n"
            f"    --version              = {version}\n"
            f"    accesspilot.__version__ = {source_version}  (唯一版本来源)\n"
            "    改版本号请改 accesspilot/__init__.py; 确实要造一个不一致的包,"
            "加 --force-version。"
        )
    if args.check_version_only:
        say(f"[√] 版本一致性检查通过: accesspilot.__version__ = {source_version}")
        return 0
    app_exe = find_app_exe(args.app_exe)
    if not args.force_version:
        exe_version = exe_file_version(app_exe)
        if exe_version and _ver_tuple(exe_version) != _ver_tuple(version):
            raise SystemExit(
                "[x] 拒绝构建: 主程序的版本资源与安装包版本不一致\n"
                f"    {app_exe.name} 的 FileVersion = {exe_version}\n"
                f"    安装包版本                    = {version}\n"
                "    先重新跑 python packaging/build.py, 或加 --force-version。"
            )
        if exe_version:
            say(f"[0/3] 主程序版本资源核对通过: {app_exe.name} FileVersion={exe_version}")
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
    filename = artifact_name(version=version, full=args.full)
    if run_pyinstaller(name=name, full=args.full, filename=filename) != 0:
        say("[x] PyInstaller 失败")
        return 1
    say("[3/3] 收尾")
    return 0 if publish(name=name, full=args.full, version=version) else 1


if __name__ == "__main__":
    raise SystemExit(main())
