#!/usr/bin/env python
"""红杏 轻量安装程序 (安装 / 卸载 / 更新).

它是什么
========
一个**用户级**(不需要管理员)的小安装器, 做成单文件 exe 后双击即用:

    红杏-Setup-v<版本>.exe                 # 安装(交互式)
    红杏-Setup-v<版本>.exe --silent        # 静默安装(更新流程内部也用这个)
    红杏-Setup-v<版本>.exe --dir D:\\Apps  # 指定安装目录
    红杏-Setup-v<版本>.exe --uninstall     # 卸载(保留节点/配置)
    红杏-Setup-v<版本>.exe --uninstall --purge   # 卸载并删除全部用户数据
    红杏-Setup-v<版本>.exe --update        # 检查 GitHub 最新版并就地更新

设计上的三条硬约定
==================
1. **程序与数据分开**, 这是卸载/更新能安全工作的前提:
     程序 -> %LOCALAPPDATA%\\Programs\\Hongxing\\红杏.exe
     数据 -> %LOCALAPPDATA%\\AccessPilot\\            (见 accesspilot/paths.py)
   更新只换程序、绝不动数据; 卸载默认也只删程序。

2. **内核不在 exe 旁边, 而在数据目录**。`paths.core_binary()` 指的是
   `%LOCALAPPDATA%\\AccessPilot\\core\\mihomo.exe`, `wintun.dll` 同理。所以带内核的
   安装包必须把这两个文件放进**数据目录**, 放进程序目录等于没装。

3. **卸载/更新前先让红杏自己收尾**(`红杏.exe stop`)。系统代理模式开着时直接删程序,
   用户会留下一个指向死端口的代理设置 —— 整台机器都上不了网。这一步不能省。

payload(打包进安装器 exe 的东西)
================================
    payload/version.txt          版本号
    payload/红杏.exe              主程序(必需)
    payload/core/mihomo.exe      内核(可选; 不带就是"轻量包", 首次使用需 accesspilot init)
    payload/core/wintun.dll      TUN 驱动(可选)
    payload/geodata/*            地理数据(完整包; 内核启动必需, 缺了直接 exit)
    payload/profiles/*.json      节点档(完整包; 没有节点就无从连接)
    payload/ruleset/*.yaml       规则集缓存(可选, --with-cache 才有)
    payload/state.json           端口/镜像/选中节点等偏好(见下面第 4 条)

第 4 条硬约定: **不信任 payload 里的任何凭据**
=============================================
payload 里那个 state.json 是打包机上的文件, 它的 `api_secret` 曾经就是维护者本机的
真实 external-controller 密钥, 随公开发布的 Setup.exe 一起泄露过。所以:

  * 打包侧(packaging/build_installer.py)会把 api_secret 清空并断言真的清空了;
  * 安装侧(本文件的 seed_runtime)在目标机器上**无条件**重新随机生成一个。

两道都留着是有意的: 少任何一道, 下一次有人改了拷贝逻辑就又漏了。

从源码直接跑(不开 PyInstaller 也能测):
    python packaging/installer.py --app-exe dist/红杏.exe --core-from "%LOCALAPPDATA%\\AccessPilot\\core"
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

APP_NAME = "红杏"
PROG_DIR_NAME = "Hongxing"
EXE_NAME = "红杏.exe"
UNINSTALLER_NAME = "红杏-卸载.exe"
DATA_DIR_NAME = "AccessPilot"
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Hongxing"
RELEASES_API = "https://api.github.com/repos/Allsungood/AccessPilot/releases"
FALLBACK_VERSION = "0.0.0"

#: 卸载时要杀掉的主程序镜像名(用户可能把它改过名, 所以多列几个)
KILL_IMAGES = ("红杏.exe", "hongxing.exe", "hongxing-debug.exe", "红杏-debug.exe")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


#: 输出汇(GUI 模式下把 say() 的输出接到界面日志框里; 命令行模式为 None)
_SINK = None


def say(message: str = "") -> None:
    if _SINK is not None:
        _SINK(message)
        return
    print(message, flush=True)


def use_utf8() -> None:
    """让中文输出不受控制台代码页影响。

    没有这一句时, 子进程按 GBK 写、调用方按 UTF-8 读, 在 CI / 其它终端里
    看到的就是一串乱码 —— 与 packaging/build.py 里同一处理。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def is_windows() -> bool:
    return sys.platform == "win32"


def pause_if_double_clicked(args: argparse.Namespace) -> None:
    """双击运行时别一闪而过: 让用户看得到结果。

    只在"没有 --silent"且 stdin 是终端时等回车 —— 双击=有终端, 脚本/CI=管道,
    所以在自动化里不会被卡住。
    """
    if args.silent:
        return
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n按回车键退出 …")
    except Exception:
        pass


def payload_root() -> Path | None:
    """冻结后 payload 在 sys._MEIPASS 下; 源码运行时没有。"""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    candidate = Path(base) / "payload"
    return candidate if candidate.is_dir() else None


def local_appdata() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        raise SystemExit("[x] 找不到 LOCALAPPDATA, 无法确定安装位置")
    return Path(base)


def data_dir() -> Path:
    """与 accesspilot/paths.py 的 home() 保持一致(也要认 ACCESSPILOT_HOME)。"""
    env = os.environ.get("ACCESSPILOT_HOME")
    if env:
        return Path(env).expanduser()
    return local_appdata() / DATA_DIR_NAME


def default_prog_dir() -> Path:
    return local_appdata() / "Programs" / PROG_DIR_NAME


def installed_prog_dir() -> Path | None:
    """从注册表反查当前装在哪(卸载/更新用)。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "InstallLocation")
        path = Path(str(value))
        return path if path.is_dir() else None
    except Exception:
        return None


def installed_version() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "DisplayVersion")
        return str(value)
    except Exception:
        return ""


def find_version(prog_dir: Path | None) -> str:
    for candidate in (
        (prog_dir / "version.txt") if prog_dir else None,
        (payload_root() / "version.txt") if payload_root() else None,
    ):
        if candidate and candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
    try:  # 源码运行时直接读包里的版本号
        root = Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from accesspilot import __version__  # noqa: PLC0415

        return str(__version__)
    except Exception:
        return FALLBACK_VERSION


def run(cmd: list[str], *, timeout: int = 25, quiet: bool = False) -> int:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            say(f"    (执行失败: {' '.join(cmd)} -> {type(exc).__name__}: {exc})")
        return 1
    if not quiet and proc.stdout.strip():
        for line in proc.stdout.strip().splitlines():
            say(f"    {line}")
    return proc.returncode


def ps(script: str) -> tuple[int, str, str]:
    """跑一段 PowerShell, 返回 (退出码, 输出, 实际用的解释器)。

    两个坑, 都踩过:

    1. **必须走 `-EncodedCommand`**: 脚本里含中文(快捷方式名、目标路径), 而
       `-Command` 会按控制台代码页再转一道, 中文变乱码 —— 现象是"快捷方式静默没建成"。
       base64(UTF-16LE) 是唯一不被代码页影响的传参方式。
    2. **不能只认 `powershell.exe`**: 有些机器上(装了 WDAC/AppLocker 或做过加固)
       Windows PowerShell 5.1 从子进程里根本起不来 —— 连 `Write-Output hi` 都返回
       rc=5 / "One or more errors occurred.", 而 PowerShell 7(`pwsh`)正常。
       所以先试 pwsh, 再退回 powershell。
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    last = (1, "(没有可用的 PowerShell 解释器)", "")
    for exe in ("pwsh", "powershell"):
        try:
            proc = subprocess.run(
                [
                    exe,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encoded,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=40,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            last = (1, f"{type(exc).__name__}: {exc}", exe)
            continue
        out = (proc.stdout + proc.stderr).strip()
        if proc.returncode == 0:
            return proc.returncode, out, exe
        last = (proc.returncode, out, exe)
    return last


def make_shortcut(link: Path, target: Path, *, workdir: Path, icon: Path | None) -> bool:
    """建 .lnk。三级降级: pwsh -> powershell -> 同目录 .cmd 兜底。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    icon_arg = f"$s.IconLocation='{icon},0';" if icon and icon.is_file() else ""
    script = (
        "$ErrorActionPreference='Stop';"
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
        f"$s.TargetPath='{target}';"
        f"$s.WorkingDirectory='{workdir}';"
        f"$s.Description='{APP_NAME} - 一键通行';"
        f"{icon_arg}"
        f"$s.Save()"
    )
    rc, out, used = ps(script)
    if link.is_file():
        return True

    # 连 PowerShell 都用不了时, 退化成 .cmd(双击同样能启动, 只是会闪一下黑框)
    say(f"    (.lnk 创建失败 rc={rc} via {used or '?'}: {out[:200] or '(无输出)'})")
    fallback = link.with_suffix(".cmd")
    try:
        fallback.write_text(
            "@echo off\r\n"
            f'start "" "{target}"\r\n',
            encoding="utf-8",
        )
        say(f"    已退化为 {fallback.name}(双击可用, 但没有图标)")
    except Exception as exc:  # noqa: BLE001
        say(f"    (.cmd 兜底也失败: {exc})")
    return False


def seed_runtime(args: argparse.Namespace) -> None:
    """把"离线可用"需要的数据铺进数据目录 —— 只装内核是不够的。

    新机器上还缺三样东西, 缺任何一样都表现为"装完了但点连接没反应":
      * `runtime/` 下的 geodata(geosite.dat / geoip.metadb / country.mmdb):
        内核启动时要加载它们, 没有就起不来;
      * `profiles/` 里的节点档: 没有节点就无从连接;
      * `state.json`: 里面记着 active_profile(用哪个档)和选中节点。
        这个文件里的 `api_secret` 是**每台机器自己的**本地 API 凭据, 所以铺过去时
        重新随机生成, 不沿用源机器的值。
    """
    root = payload_root()
    if root is None:
        return
    data = data_dir()
    jobs = (
        (root / "geodata", data / "runtime", "geodata(内核启动必需)"),
        (root / "ruleset", data / "runtime" / "ruleset", "规则集缓存"),
        (root / "profiles", data / "profiles", "节点档"),
    )
    for src, dst, label in jobs:
        if not src.is_dir():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        copied = 0
        for item in sorted(src.iterdir()):
            if not item.is_file():
                continue
            target = dst / item.name
            if not args.force_core and target.is_file() and target.stat().st_size == item.stat().st_size:
                continue
            shutil.copy2(item, target)
            copied += 1
        if copied:
            say(f"[+] 已铺 {label}: {copied} 个文件 -> {dst}")

    # state.json 里的 `api_secret` 是每台机器**自己的** external-controller 凭据。
    # 这里无条件重新随机生成, 不管包里带的是什么值 —— 原因是一次真实事故:
    # 打包脚本原先把 `%LOCALAPPDATA%\AccessPilot\state.json` 原样拷进 payload,
    # 于是维护者本机的真实密钥被冻进了公开发布的 Setup.exe, 谁下载都能从二进制里
    # 解出来。把真实凭据发布出去这件事本身不可接受, 与"它只监听 127.0.0.1、实际
    # 可利用性低"无关 —— 凭据一旦公开就必须当作已泄露处理。
    #
    # 多生成一次的代价是零; 漏一次的代价是发布一个泄露的凭据。所以这里是
    # **重新生成**, 而不是"包里没有才生成"。
    src_state = root / "state.json"
    target = data / "state.json"
    if not src_state.is_file():
        return
    if target.exists():
        # 目标机器上已经有自己的 state.json(升级/重装), 绝不能覆盖 ——
        # 那会把用户自己的节点选择、端口和密钥一起冲掉。
        say("[=] state.json 已存在, 保留本机配置(不覆盖)")
        return
    try:
        payload = json.loads(src_state.read_text(encoding="utf-8"))
        payload["api_secret"] = secrets.token_hex(16)  # 不沿用任何来源的凭据
        payload.pop("last_start", None)  # 打包机上的时间戳, 与用户无关
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        say("[+] 已写入 state.json(api_secret 已重新随机生成)")
    except Exception as exc:  # noqa: BLE001
        say(f"    (state.json 写入失败: {exc})")


def known_folder(name: str, fallback: Path) -> Path:
    """从注册表取"已知文件夹"的真实路径, 拿不到才用 fallback。

    **不能假设 `%USERPROFILE%\\Desktop`**: 实测有机器把 Desktop / Documents /
    Pictures 整体重定向到别的盘(注册表 Explorer\\User Shell Folders)。
    硬拼默认路径的后果是"快捷方式建成了、但用户在桌面上永远看不到",
    而且全程不报错 —— 这种静默失效最难查。
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            raw, _ = winreg.QueryValueEx(key, name)
        path = Path(os.path.expandvars(str(raw)))
        if path.is_dir():
            return path
    except Exception:
        pass
    return fallback


def shortcut_targets() -> tuple[Path, Path]:
    """(开始菜单快捷方式, 桌面快捷方式) —— 都取注册表里的**真实**路径。"""
    appdata = Path(os.environ.get("APPDATA", ""))
    profile = Path(os.environ.get("USERPROFILE", ""))
    start_menu = known_folder("Programs", appdata / "Microsoft/Windows/Start Menu/Programs")
    desktop = known_folder("Desktop", profile / "Desktop")
    return start_menu / f"{APP_NAME}.lnk", desktop / f"{APP_NAME}.lnk"


def all_shortcut_links() -> list[Path]:
    """所有**可能**存在过的快捷方式路径(真实路径 + 传统默认路径)。

    卸载时要全扫一遍: 老版本把 .lnk 建在默认位置上(比如 `%USERPROFILE%\\Desktop`),
    升级/卸载时不能因为"现在路径变了"就把那个旧文件留在用户桌面上。
    """
    appdata = Path(os.environ.get("APPDATA", ""))
    profile = Path(os.environ.get("USERPROFILE", ""))
    start_menu_dirs = [
        known_folder("Programs", appdata / "Microsoft/Windows/Start Menu/Programs"),
        appdata / "Microsoft/Windows/Start Menu/Programs",
    ]
    desktop_dirs = [known_folder("Desktop", profile / "Desktop"), profile / "Desktop"]
    links: list[Path] = []
    for directory in [*start_menu_dirs, *desktop_dirs]:
        link = directory / f"{APP_NAME}.lnk"
        if link not in links:
            links.append(link)
    return links


# --------------------------------------------------------------------------- #
# 注册表(设置 -> 应用 里的"卸载"入口)
# --------------------------------------------------------------------------- #


def register_uninstall(prog_dir: Path, version: str, *, uninstaller: Path, size_kb: int) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_WRITE) as key:
        values = {
            "DisplayName": (f"{APP_NAME} (AccessPilot)", winreg.REG_SZ),
            "DisplayVersion": (version, winreg.REG_SZ),
            "Publisher": ("Allsungood", winreg.REG_SZ),
            "InstallLocation": (str(prog_dir), winreg.REG_SZ),
            "DisplayIcon": (str(prog_dir / EXE_NAME), winreg.REG_SZ),
            "UninstallString": (f'"{uninstaller}" --uninstall', winreg.REG_SZ),
            "QuietUninstallString": (f'"{uninstaller}" --uninstall --silent', winreg.REG_SZ),
            "InstallDate": (time.strftime("%Y%m%d"), winreg.REG_SZ),
            "EstimatedSize": (int(size_kb), winreg.REG_DWORD),
            "NoModify": (1, winreg.REG_DWORD),
            "NoRepair": (1, winreg.REG_DWORD),
        }
        for name, (value, kind) in values.items():
            winreg.SetValueEx(key, name, 0, kind, value)


def unregister_uninstall() -> None:
    try:
        import winreg

        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_KEY)
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        say(f"    (清理注册表失败: {exc})")


# --------------------------------------------------------------------------- #
# 让红杏自己收尾
# --------------------------------------------------------------------------- #


def stop_app(prog_dir: Path | None) -> None:
    """先 stop 再杀进程 —— 顺序反了会把系统代理留在死端口上。"""
    exe = (prog_dir / EXE_NAME) if prog_dir else None
    if exe and exe.is_file():
        say("[i] 让红杏停止内核并还原系统代理 …")
        run([str(exe), "stop"], timeout=40, quiet=False)
    for image in KILL_IMAGES:
        run(["taskkill", "/IM", image, "/F"], quiet=True)
    time.sleep(0.6)


def purge_data() -> None:
    target = data_dir()
    if not target.is_dir():
        return
    shutil.rmtree(target, ignore_errors=True)
    say(f"[+] 已删除用户数据: {target}")


def schedule_self_delete() -> None:
    """删掉"卸载时复制到 %TEMP% 的那个自己"。

    正在运行的 exe 在 Windows 上删不掉, 所以交给一个独立的 cmd: 它先等本进程
    退出(用 ping 计时, 不依赖需要控制台的 timeout.exe), 再删文件。不这么做的话,
    每次卸载都会在 %TEMP% 里留下一个 24~56 MB 的 exe —— 文件名带 pid, 下一次
    卸载也不会覆盖它, 只会再留一个。
    """
    target = os.environ.get("HONGXING_UNINSTALL_SELF")
    if not target:
        return
    path = Path(target)
    if not path.is_file():
        return
    try:
        subprocess.Popen(
            ["cmd", "/c", f'ping -n 8 127.0.0.1 >nul & del /f /q "{path}"'],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0),
            close_fds=True,
        )
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 安装
# --------------------------------------------------------------------------- #


def payload_app_exe(args: argparse.Namespace) -> Path:
    if args.app_exe:
        path = Path(args.app_exe).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"[x] --app-exe 指向的文件不存在: {path}")
        return path
    root = payload_root()
    if root:
        # 版本号**不能写死**。原来这里写的是 "hongxing-v0.9.0-win64.exe",
        # 发 1.0.0 的时候它当然找不到, 于是退化成下面那个"随便挑一个 *.exe"
        # 的兜底分支 —— 在同时放了安装器和主程序的包里会挑错东西。
        names = [EXE_NAME, "hongxing.exe", f"hongxing-v{find_version(None)}-win64.exe"]
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return candidate
        for candidate in sorted(root.glob("hongxing-v*-win64.exe")):
            return candidate
        for candidate in sorted(root.glob("*.exe")):
            return candidate
    raise SystemExit(
        "[x] 安装包里没有主程序。\n"
        "    打包时请把 红杏.exe 放进 payload/(见 packaging/installer.spec);\n"
        "    源码调试时用 --app-exe dist/红杏.exe"
    )


def payload_core_dir(args: argparse.Namespace) -> Path | None:
    if args.no_core:
        return None
    if args.core_from:
        path = Path(args.core_from).expanduser().resolve()
        return path if path.is_dir() else None
    root = payload_root()
    if root:
        candidate = root / "core"
        if candidate.is_dir():
            return candidate
    return None


def install_core(source: Path, *, force: bool = False) -> int:
    """把内核放进**数据目录**(不是程序目录, 见文件头第 2 条)。"""
    core_dir = data_dir() / "core"
    core_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in ("mihomo.exe", "wintun.dll"):
        src = source / name
        if not src.is_file():
            continue
        dst = core_dir / name
        # 源就是目标(比如 --core-from 直接指向数据目录)时不能拷自己
        try:
            if src.resolve() == dst.resolve():
                say(f"[=] 内核已在位(源=目标), 跳过: {dst}")
                continue
        except OSError:
            pass
        if dst.is_file() and not force and dst.stat().st_size == src.stat().st_size:
            say(f"[=] 内核已就绪, 跳过: {dst}")
            continue
        say(f"[i] 安装内核: {name} ({src.stat().st_size / 1048576:.1f} MB)")
        shutil.copy2(src, dst)
        copied += 1
    if copied:
        say(f"[+] 内核已装到 {core_dir}")
    return copied


def do_install(args: argparse.Namespace) -> int:
    version = args.version or find_version(None)
    app_exe = payload_app_exe(args)
    prog_dir = Path(args.dir).expanduser().resolve() if args.dir else default_prog_dir()

    say(f"{APP_NAME} 安装程序  v{version}")
    say("=" * 66)
    say(f"程序目录: {prog_dir}")
    say(f"数据目录: {data_dir()}  (节点/订阅/配置, 卸载时默认保留)")
    say("")

    stop_app(installed_prog_dir())
    prog_dir.mkdir(parents=True, exist_ok=True)

    # 1) 主程序
    target = prog_dir / EXE_NAME
    say(f"[i] 复制主程序 -> {target}")
    shutil.copy2(app_exe, target)
    (prog_dir / "version.txt").write_text(version + "\n", encoding="utf-8")

    # 2) 卸载器: 冻结后把安装器自己复制一份进去, 这样原 Setup.exe 删了也能卸载。
    #    源码模式下没有可复制的 exe, 就写一个 .cmd 代替(仅调试用)。
    uninstaller = prog_dir / UNINSTALLER_NAME
    if getattr(sys, "frozen", False):
        try:
            shutil.copy2(Path(sys.executable), uninstaller)
        except Exception as exc:  # noqa: BLE001
            say(f"    (复制卸载器失败: {exc}; 可用原安装包卸载)")
            uninstaller = Path(sys.executable)
    else:
        uninstaller = prog_dir / "红杏-卸载.cmd"
        uninstaller.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "{Path(__file__).resolve()}" --uninstall %*\r\n',
            encoding="utf-8",
        )
        say(f"    (源码模式: 卸载器写成了 {uninstaller.name}, 冻结成 exe 后会自动变成真 exe)")

    # 3) 内核(可选)
    core_source = payload_core_dir(args)
    if core_source:
        install_core(core_source, force=args.force_core)
    else:
        say("[i] 本次不带内核(轻量包)。首次使用需执行:  红杏.exe init")

    # 3.5) 让新机器"装完即用": geodata / 节点档 / 一份换过密钥的 state.json
    seed_runtime(args)

    # 4) 快捷方式(路径从注册表取, 见 known_folder 的说明)
    if not args.no_shortcuts:
        start_menu, desktop = shortcut_targets()
        ok1 = make_shortcut(start_menu, target, workdir=prog_dir, icon=target)
        ok2 = make_shortcut(desktop, target, workdir=prog_dir, icon=target)
        say(f"[{'+' if ok1 else '!'}] 开始菜单快捷方式: {start_menu}")
        say(f"[{'+' if ok2 else '!'}] 桌面快捷方式: {desktop}")

    # 5) 注册"设置 -> 应用"里的卸载入口
    total = sum(f.stat().st_size for f in prog_dir.rglob("*") if f.is_file())
    register_uninstall(prog_dir, version, uninstaller=uninstaller, size_kb=int(total / 1024))

    say("")
    say("=" * 66)
    say(f"[√] 安装完成  v{version}")
    say(f"    启动: 双击桌面「{APP_NAME}」或开始菜单里的「{APP_NAME}」")
    say(f"    命令行: \"{target}\" --help      (不需要装 Python)")
    say(f"    卸载: 设置 -> 应用 -> {APP_NAME}  或运行 \"{uninstaller}\" --uninstall")
    say(f"    更新: \"{uninstaller}\" --update   (带 --purge 可连数据一起删)")
    say("=" * 66)

    if not args.silent and not args.no_launch:
        say("[i] 正在启动 …")
        subprocess.Popen([str(target)], cwd=str(prog_dir), close_fds=True)
    return 0


# --------------------------------------------------------------------------- #
# 卸载
# --------------------------------------------------------------------------- #


def do_uninstall(args: argparse.Namespace) -> int:
    prog_dir = Path(args.dir).expanduser().resolve() if args.dir else installed_prog_dir()
    if prog_dir is None:
        prog_dir = default_prog_dir()

    # 卸载器自己就在要删的目录里时, Windows 不允许删正在运行的 exe。
    # 标准做法: 把自己复制到临时目录再重新执行一次, 让原进程退出。
    me = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    try:
        inside = me.is_relative_to(prog_dir.resolve())
    except Exception:
        inside = False
    if inside and not os.environ.get("HONGXING_UNINSTALL_RELAUNCHED"):
        temp_copy = Path(tempfile.gettempdir()) / f"hongxing-uninstall-{os.getpid()}.exe"
        try:
            shutil.copy2(me, temp_copy)
            env = os.environ.copy()
            env["HONGXING_UNINSTALL_RELAUNCHED"] = "1"
            # 让副本结束时把自己删掉(见 schedule_self_delete 的说明)。
            # 原来的实现在这里留了一份 24~56 MB 的 exe 在 %TEMP% 里, 而且再也
            # 没有人会去删它 —— 每次卸载都多留一份。
            env["HONGXING_UNINSTALL_SELF"] = str(temp_copy)
            subprocess.Popen([str(temp_copy), *sys.argv[1:]], env=env, close_fds=True)
            say("[i] 已把卸载任务交给临时副本, 本进程退出 …")
            return 0
        except Exception as exc:  # noqa: BLE001
            say(f"    (无法自我转交: {exc}; 继续直接卸载)")

    say(f"{APP_NAME} 卸载程序")
    say("=" * 66)
    say(f"程序目录: {prog_dir}")
    say(f"数据目录: {data_dir()}")
    say("")

    stop_app(prog_dir if prog_dir.is_dir() else None)

    # 快捷方式(注册表真实路径 + 传统默认路径, 都扫一遍, 避免留垃圾)
    for link in all_shortcut_links():
        try:
            if link.is_file():
                link.unlink()
                say(f"[+] 已删除快捷方式: {link}")
        except Exception as exc:  # noqa: BLE001
            say(f"    (删除快捷方式失败: {exc})")

    # 程序目录
    if prog_dir.is_dir():
        # 正在运行的卸载器就是 prog_dir 里的那个, 自己删不掉自己 -> 先改名再删
        shutil.rmtree(prog_dir, ignore_errors=True)
        if prog_dir.is_dir():
            say(f"[!] 部分文件被占用, 未能完全删除: {prog_dir}(重启后再删一次即可)")
        else:
            say(f"[+] 已删除程序目录: {prog_dir}")

    unregister_uninstall()
    say("[+] 已从「设置 -> 应用」移除")

    if args.purge:
        purge_data()
    else:
        keep = data_dir()
        if keep.is_dir():
            size = sum(f.stat().st_size for f in keep.rglob("*") if f.is_file()) / 1048576
            say(f"[i] 用户数据保留在 {keep} ({size:.1f} MB)")
            say(f"    想一起删掉: \"{UNINSTALLER_NAME}\" --uninstall --purge")

    say("")
    say(f"[√] 卸载完成")
    schedule_self_delete()
    return 0


# --------------------------------------------------------------------------- #
# 更新
# --------------------------------------------------------------------------- #


def _ver_tuple(text: str) -> tuple:
    numbers = []
    for part in str(text).lstrip("vV").replace("-", ".").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        numbers.append(int(digits) if digits else 0)
    return tuple(numbers + [0, 0, 0])[:4]


#: 主程序资产的**白名单**, 必须与 packaging/build.py 实际产出的文件名一致。
#: `{ver}` 是 release tag 去掉前导 v 的版本号。
#:
#: 这张表就是 PKG-02 的修复本身。原来的实现是"先找名字里带 win64 的 exe, 找不到
#: 就挑第一个 exe", 而这个仓库**从来没有任何产物带 win64** —— 于是第一个循环永远
#: 匹配不上, 每次都落到兜底分支。发布页上同时躺着 红杏.exe 和 红杏-Setup-*.exe,
#: 谁排在前面全看 GitHub 的返回顺序: 用户点一次「检查更新」, 主程序就可能被换成
#: 安装器。之后「红杏」快捷方式打开的是安装向导, 保活任务调用的
#: `红杏.exe ensure` 也没人认识 —— 而且是静默发生的。
APP_ASSET_PATTERNS = (
    "红杏-v{ver}-win64.exe",
    "hongxing-v{ver}-win64.exe",
    "红杏-v{ver}.exe",
    "hongxing-v{ver}.exe",
    "红杏.exe",
    "hongxing.exe",
)

#: 名字里出现这些词的资产**一定不是**主程序。显式排除比"看起来像主程序"更可靠:
#: 白名单负责"只认对的", 这张表负责"绝不认错的", 两道都要有。
INSTALLER_ASSET_MARKERS = ("setup", "installer", "uninstall", "安装", "卸载")

#: 版本资源(属性 -> 详细信息)里出现这些词就说明它是个安装器
INSTALLER_INFO_MARKERS = ("安装", "卸载", "setup", "installer", "uninstall")


def is_installer_asset(name: str) -> bool:
    """这个资产名看起来是安装器吗?"""
    lowered = str(name).lower()
    return any(marker in lowered for marker in INSTALLER_ASSET_MARKERS)


def pick_asset(release: dict) -> tuple[str, str, int] | None:
    """挑出 Windows **主程序**资产; 挑不出来就返回 None。

    为什么宁可不更新也不能猜: 主程序资产和安装器资产都是 `.exe`, 名字里都带
    "红杏"。猜错的后果(把 56 MB 的安装器写成用户的主程序)远大于"这次没更新"。
    猜不出来时 do_update() 会明确报错并退出, 用户手上的红杏保持原样。
    """
    tag = str(release.get("tag_name", "")).lstrip("vV")
    assets = [a for a in release.get("assets", []) if isinstance(a, dict)]
    for pattern in APP_ASSET_PATTERNS:
        wanted = pattern.format(ver=tag)
        for asset in assets:
            name = str(asset.get("name", ""))
            if name != wanted or is_installer_asset(name):
                continue
            return name, str(asset.get("browser_download_url", "")), int(asset.get("size", 0))
    return None


def app_asset_names(version: str) -> str:
    """给"没找到主程序"那条报错用的可读清单。"""
    return " / ".join(pattern.format(ver=version) for pattern in APP_ASSET_PATTERNS[:4])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _discard(path: Path) -> None:
    """删掉下载下来的中间文件。校验失败时**必须**删 —— 一个没通过校验的 exe
    留在 %TEMP% 里, 早晚会有人双击它。"""
    try:
        path.unlink()
    except Exception:  # noqa: BLE001
        pass


def expected_digest(release: dict, name: str) -> str | None:
    """取该资产**期望的** SHA256, 拿不到就返回 None。

    三个来源, 依次尝试(GitHub 只会提供其中一部分, 多一条路就多一分能校验的机会):

    1. `asset["digest"]` —— GitHub Releases API 自带的 `sha256:<hex>`。这是最权威
       的: 它由 GitHub 自己算, 不像发布说明那样靠人维护, 也不会随正文编辑而漂移。
    2. release 正文里同时出现**该文件名**和 64 位十六进制的行 —— 项目习惯把哈希
       贴在说明里(`SHA256(红杏-v1.0.0-win64.exe) = ...`)。
    3. 发布页上的校验和附件(`SHA256SUMS.txt` / `checksums.txt` / `<name>.sha256`)。

    为什么必须校验: 更新是**就地覆盖用户正在用的主程序**。没有校验时, 一个被劫持
    的下载地址、一次中途断流截断、甚至发布方自己传错文件, 都会原样落进
    `%LOCALAPPDATA%\\Programs\\Hongxing\\红杏.exe`。原来只检查"字节数 ≥ 声明值的
    95%", 那挡不住任何有意义的篡改。
    """
    lowered = name.lower()
    for asset in release.get("assets", []):
        if not isinstance(asset, dict) or str(asset.get("name", "")) != name:
            continue
        digest = str(asset.get("digest") or "")
        if digest.lower().startswith("sha256:"):
            value = digest.split(":", 1)[1].strip()
            if re.fullmatch(r"[0-9a-fA-F]{64}", value):
                return value.lower()

    body = str(release.get("body") or "")
    for line in body.splitlines():
        if name not in line and lowered not in line.lower():
            continue
        match = re.search(r"\b([0-9a-fA-F]{64})\b", line)
        if match:
            return match.group(1).lower()

    for asset in release.get("assets", []):
        if not isinstance(asset, dict):
            continue
        check_name = str(asset.get("name", ""))
        low = check_name.lower()
        if not (low.endswith(".sha256") or low.endswith(".txt") or low.endswith("sums")):
            continue
        if lowered not in low and "sum" not in low and "sha" not in low:
            continue
        text = fetch_text(str(asset.get("browser_download_url", "")))
        if not text:
            continue
        for line in text.splitlines():
            match = re.search(r"\b([0-9a-fA-F]{64})\b", line)
            if not match:
                continue
            # 校验和文件里每行都带文件名; 只认提到本资产(或不提任何名字)的行
            if lowered in line.lower() or name in line or not line.strip():
                return match.group(1).lower()
    return None


def fetch_text(url: str, *, timeout: int = 25) -> str:
    if not url:
        return ""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "hongxing-installer"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def win_version_info(path: Path) -> dict:
    """读下载下来的 exe 的版本资源(属性 -> 详细信息)。读不到就返回空 dict。

    这是 PKG-02 的第二道闸: 白名单 + SHA256 已经能钉死"下载到的就是这个资产",
    这一层再独立地看一眼"它到底是不是主程序"。**读不到不算失败** —— 有些加固过的
    机器上 PowerShell 起不来(见 ps() 的说明), 那不该让更新用不了; 但只要能读到,
    产品名里带"安装/卸载"就一定是认错了, 必须停下来。
    """
    script = (
        "$ErrorActionPreference='Stop';"
        f"$i=(Get-Item -LiteralPath '{path}').VersionInfo;"
        "Write-Output ($i.ProductName + [char]9 + $i.FileDescription + [char]9 + $i.OriginalFilename)"
    )
    rc, out, _ = ps(script)
    if rc != 0 or not out:
        return {}
    parts = (out.splitlines()[-1] if out.splitlines() else "").split("\t")
    keys = ("ProductName", "FileDescription", "OriginalFilename")
    return {key: (parts[i].strip() if i < len(parts) else "") for i, key in enumerate(keys)}


def looks_like_installer_binary(path: Path) -> bool:
    """版本资源表明它是个安装器吗?(读不到版本资源 -> False)"""
    info = win_version_info(path)
    if not info:
        return False
    blob = " ".join(info.values()).lower()
    return any(marker in blob for marker in INSTALLER_INFO_MARKERS)


def fetch_latest_release(*, timeout: int = 25) -> dict | None:
    """取最新的 Release(含 Pre-release —— 本项目的发布目前都是预发布)。"""
    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "User-Agent": "hongxing-installer",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            releases = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        say(f"[x] 查询更新失败: {type(exc).__name__}: {exc}")
        say("    (更新需要能访问 api.github.com; 若本机代理可用, 先打开红杏再重试)")
        return None
    candidates = [r for r in releases if isinstance(r, dict) and not r.get("draft")]
    if not candidates:
        return None
    return max(candidates, key=lambda r: _ver_tuple(r.get("tag_name", "")))


def do_update(args: argparse.Namespace) -> int:
    prog_dir = Path(args.dir).expanduser().resolve() if args.dir else installed_prog_dir()
    if prog_dir is None:
        prog_dir = default_prog_dir()
    current = installed_version() or find_version(prog_dir)

    say(f"{APP_NAME} 更新程序   (当前安装版: {current or '未安装'})")
    say("=" * 66)

    release = fetch_latest_release()
    if release is None:
        return 1
    tag = str(release.get("tag_name", ""))
    latest = tag.lstrip("vV")
    say(f"最新发布: {tag}   {release.get('name', '')}")

    if current and _ver_tuple(latest) <= _ver_tuple(current):
        say(f"[=] 已是最新版本 (v{current}), 无需更新")
        return 0

    asset = pick_asset(release)
    if asset is None:
        # 失败方向必须选"什么都没发生"。猜错的后果是把安装器写成用户的主程序,
        # 而且完全静默; 不更新的后果只是用户再等一个版本。
        say("[x] 这个发布里没有找到**主程序**资产, 本次不更新。")
        say(f"    认得的文件名: {app_asset_names(latest)}")
        say("    (安装器 红杏-Setup-*.exe 绝不会被当成主程序下载)")
        return 1
    name, url, size = asset
    say(f"[i] 下载 {name} ({size / 1048576:.1f} MB) …")

    tmp = Path(tempfile.gettempdir()) / f"hongxing-update-{re.sub(r'[^0-9A-Za-z._-]', '_', tag)}.exe"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "hongxing-installer"})
        with urllib.request.urlopen(request, timeout=180) as response, tmp.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except Exception as exc:  # noqa: BLE001
        say(f"[x] 下载失败: {type(exc).__name__}: {exc}")
        return 1
    if size and tmp.stat().st_size < size * 0.95:
        say(f"[x] 下载不完整: {tmp.stat().st_size}/{size} 字节")
        return 1

    # ---- 完整性校验: 校验不过就什么都不做, 绝不进入替换流程 ----
    actual = sha256_file(tmp)
    expected = expected_digest(release, name)
    if expected is None:
        if not getattr(args, "insecure_skip_verify", False):
            say("[x] 这个发布没有提供 SHA256 校验值, 无法确认下载到的确实是主程序。")
            say("    没有校验就等于「下载什么装什么」, 而这一步会覆盖用户正在用的程序。")
            say("    发布方: 在 release 说明里写 SHA256(文件名) = <64位十六进制>, 或")
            say("    传一个 SHA256SUMS.txt; GitHub 附件自带的 digest 也会被自动采用。")
            say("    确实要跳过校验(自担风险): --insecure-skip-verify")
            _discard(tmp)
            return 1
        say("[!] 发布未提供校验值, 按 --insecure-skip-verify 继续(不建议)")
    elif actual.lower() != expected.lower():
        say("[x] SHA256 校验失败, 已放弃更新(用户的主程序未被改动):")
        say(f"    期望 {expected}")
        say(f"    实际 {actual}")
        _discard(tmp)
        return 1
    else:
        say(f"[+] SHA256 校验通过: {actual}")

    # ---- 第二道闸: 就算名字对上了, 也要独立确认它不是安装器 ----
    if looks_like_installer_binary(tmp):
        say("[x] 下载到的文件版本资源显示它是**安装器**, 已放弃更新。")
        say("    (这属于发布资产命名/校验值配错, 请反馈给发布方)")
        _discard(tmp)
        return 1

    stop_app(prog_dir)
    target = prog_dir / EXE_NAME
    prog_dir.mkdir(parents=True, exist_ok=True)
    old = prog_dir / (EXE_NAME + ".old")
    try:
        if old.is_file():
            old.unlink()
        if target.is_file():
            target.rename(old)
        shutil.copy2(tmp, target)
        if old.is_file():
            old.unlink()
    except Exception as exc:  # noqa: BLE001
        say(f"[x] 替换主程序失败: {type(exc).__name__}: {exc}")
        if old.is_file() and not target.is_file():
            old.rename(target)
        return 1

    # 卸载器自己也更新一份(它才是用户以后要点的那个)
    try:
        me = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
        if me.is_file() and me.resolve() != (prog_dir / UNINSTALLER_NAME).resolve():
            shutil.copy2(me, prog_dir / UNINSTALLER_NAME)
    except Exception:
        pass

    (prog_dir / "version.txt").write_text(latest + "\n", encoding="utf-8")
    total = sum(f.stat().st_size for f in prog_dir.rglob("*") if f.is_file())
    register_uninstall(prog_dir, latest, uninstaller=prog_dir / UNINSTALLER_NAME, size_kb=int(total / 1024))
    try:
        tmp.unlink()
    except Exception:
        pass

    say("")
    say(f"[√] 已更新到 v{latest}   (用户数据未动: {data_dir()})")
    if not args.silent and not args.no_launch:
        subprocess.Popen([str(target)], cwd=str(prog_dir), close_fds=True)
    return 0


# --------------------------------------------------------------------------- #
# 图形向导(可选 —— 命令行永远等价可用)
# --------------------------------------------------------------------------- #


def gui_available() -> bool:
    try:
        import tkinter  # noqa: F401

        return True
    except Exception:
        return False


def run_gui() -> int:
    """双击时的向导: 选目录 -> 安装 / 卸载 / 检查更新。

    **刻意的约束**: 界面只把命令行已有的三件事包一层, 不提供任何界面独有的能力。
    理由很实际 —— 更新要给脚本调(`--update --silent`)、卸载要被"设置→应用"调
    (`--uninstall`), 而这些场景没有窗口可用; 界面一旦多出"只有点按钮才行"的功能,
    命令行就会慢慢漂移成二等公民。
    """
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    global _SINK
    root = tk.Tk()
    root.title(f"{APP_NAME} 安装程序  v{find_version(None)}")
    root.geometry("760x520")
    root.minsize(660, 460)

    box: queue.Queue = queue.Queue()
    _SINK = box.put
    state = {"busy": False}

    head = ttk.Frame(root, padding=10)
    head.pack(fill="x")
    ttk.Label(head, text="安装目录").grid(row=0, column=0, sticky="w")
    dir_var = tk.StringVar(value=str(installed_prog_dir() or default_prog_dir()))
    ttk.Entry(head, textvariable=dir_var, width=62).grid(row=0, column=1, padx=6, sticky="we")
    head.columnconfigure(1, weight=1)

    def browse() -> None:
        chosen = filedialog.askdirectory(initialdir=dir_var.get() or str(default_prog_dir()))
        if chosen:
            dir_var.set(chosen)

    ttk.Button(head, text="浏览…", command=browse).grid(row=0, column=2)

    opts = ttk.Frame(root, padding=(10, 2))
    opts.pack(fill="x")
    opt_desktop = tk.BooleanVar(value=True)
    opt_data = tk.BooleanVar(value=True)
    opt_launch = tk.BooleanVar(value=False)
    ttk.Checkbutton(opts, text="桌面快捷方式", variable=opt_desktop).pack(side="left")
    ttk.Checkbutton(opts, text="铺内置资源(内核/节点, 离线可用)", variable=opt_data).pack(side="left", padx=14)
    ttk.Checkbutton(opts, text="装完立即启动", variable=opt_launch).pack(side="left")

    log = tk.Text(root, height=17, wrap="word", state="disabled")
    log.pack(fill="both", expand=True, padx=10, pady=(6, 0))
    bar = ttk.Progressbar(root, mode="indeterminate")
    bar.pack(fill="x", padx=10, pady=6)

    def drain() -> None:
        try:
            while True:
                line = box.get_nowait()
                log.configure(state="normal")
                log.insert("end", f"{line}\n")
                log.see("end")
                log.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(120, drain)

    def build_args(extra: list[str]) -> argparse.Namespace:
        argv = [*extra, "--dir", dir_var.get()]
        if not opt_desktop.get():
            argv.append("--no-shortcuts")
        if not opt_data.get():
            argv.append("--no-core")
        if not opt_launch.get():
            argv.append("--no-launch")
        return build_parser().parse_args(argv)

    def start_task(fn, extra: list[str], label: str) -> None:
        if state["busy"]:
            messagebox.showinfo(APP_NAME, "上一件事还没结束, 请稍候")
            return
        state["busy"] = True
        bar.start(12)
        say(f"\n===== {label} =====")

        def worker() -> None:
            try:
                code = fn(build_args(extra))
                say(f"[{'√' if code == 0 else '×'}] {label} 结束 (退出码 {code})")
            except SystemExit as exc:
                say(f"[×] {label} 中止: {exc}")
            except Exception as exc:  # noqa: BLE001
                say(f"[×] {label} 失败: {type(exc).__name__}: {exc}")
            finally:
                def finish() -> None:
                    state["busy"] = False
                    bar.stop()

                root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    bar_row = ttk.Frame(root, padding=(10, 0, 10, 10))
    bar_row.pack(fill="x")
    ttk.Button(bar_row, text="安装", command=lambda: start_task(do_install, [], "安装")).pack(side="left")
    ttk.Button(bar_row, text="卸载", command=lambda: start_task(do_uninstall, ["--uninstall"], "卸载")).pack(side="left", padx=8)
    ttk.Button(bar_row, text="检查更新", command=lambda: start_task(do_update, ["--update"], "检查更新")).pack(side="left")
    ttk.Button(bar_row, text="退出", command=root.destroy).pack(side="right")

    say(f"{APP_NAME} 安装程序  v{find_version(None)}")
    say(f"数据目录: {data_dir()}(节点/订阅/配置, 卸载默认保留)")
    say("选好目录后点「安装」。命令行等价: --uninstall / --update / --silent")
    drain()
    root.mainloop()
    _SINK = None
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="红杏-Setup",
        description="红杏 (AccessPilot) 轻量安装程序:安装 / 卸载 / 更新",
    )
    parser.add_argument("--version", default="", help="覆盖要写入的版本号(打包时用)")
    parser.add_argument("--dir", default="", help="安装目录(默认 %%LOCALAPPDATA%%\\Programs\\Hongxing)")
    parser.add_argument("--uninstall", action="store_true", help="卸载")
    parser.add_argument("--update", action="store_true", help="检查并安装最新版")
    parser.add_argument(
        "--insecure-skip-verify",
        action="store_true",
        help="更新时跳过 SHA256 校验(发布没提供哈希时才会用到; 自担风险)",
    )
    parser.add_argument("--purge", action="store_true", help="卸载时连用户数据一起删")
    parser.add_argument("--silent", action="store_true", help="静默:不弹窗、装完不启动")
    parser.add_argument("--gui", action="store_true", help="强制图形向导(双击时的默认行为)")
    parser.add_argument("--console", action="store_true", help="不加参数时也走命令行安装(不进图形向导)")
    parser.add_argument("--no-launch", action="store_true", help="装完不启动")
    parser.add_argument("--no-shortcuts", action="store_true", help="不建快捷方式")
    parser.add_argument("--no-core", action="store_true", help="不安装内核(轻量)")
    parser.add_argument("--force-core", action="store_true", help="内核已存在也覆盖")
    # 仅源码调试用
    parser.add_argument("--app-exe", default="", help="(调试)主程序路径")
    parser.add_argument("--core-from", default="", help="(调试)内核目录, 内含 mihomo.exe / wintun.dll")
    return parser


def main(argv: list[str] | None = None) -> int:
    use_utf8()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw)
    if not is_windows():
        raise SystemExit("[x] 这个安装器只面向 Windows")

    # 双击(零参数)= 图形向导; 带任何参数 = 命令行。
    # 更新(`--update --silent`)与"设置→应用"的卸载(`--uninstall`)都属于后者,
    # 所以它们在没有窗口的环境里照样能用。
    if args.gui or (not raw and not args.console):
        if gui_available():
            return run_gui()
        say("[i] 本机没有可用的图形界面(tkinter 不可用), 自动退回命令行安装")

    try:
        if args.uninstall:
            code = do_uninstall(args)
        elif args.update:
            code = do_update(args)
        else:
            code = do_install(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        say("\n[i] 已取消")
        return 130
    except Exception as exc:  # noqa: BLE001
        say(f"[x] 失败: {type(exc).__name__}: {exc}")
        return 1
    pause_if_double_clicked(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
