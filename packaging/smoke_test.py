#!/usr/bin/env python
"""红杏 打包产物冒烟测试: **真的把 exe 跑起来**, 逐项核对。

为什么不能只看"dist/红杏.exe 存在"
==================================
onefile 的失败模式几乎都发生在**运行期**: 少收一个资源文件、tkinter 的 tcl/tk
没打进包、某个模块是延迟 import 的没被静态分析抓到 —— 构建日志一片绿, 到了
用户机器上双击就是白屏或者"什么都没发生"。所以这里每一项都要求真实执行:

    1. exe 存在且体积合理(几 MB 以下说明 Python 运行时根本没进去)
    2. `红杏.exe --version`            -> 冻结运行时能起来, cli 能 import
    3. `红杏.exe -m accesspilot ...`   -> accesspilot 内部那三处
       `sys.executable -m accesspilot` 调用(看门狗 / 直连加速 / 计划任务)
       在打包版里依然可用
    4. `红杏.exe --selftest`           -> 在冻结环境**内部**读一遍 dashboard.html、
       gui 图标, 并真的创建一个 Tk 根窗口(tcl/tk 数据缺失只会在这一刻报错)
    5. `HONGXING_STREAM=log`           -> "双击启动没有控制台"时, 输出能落到
       日志文件(这是 GUI 崩溃后唯一的现场)

每一项都会把 exe 的原始输出打出来, 方便直接贴进验收记录。

用法
====
    python packaging/smoke_test.py                 # 测 dist/红杏.exe (发布版)
    python packaging/smoke_test.py --debug         # 测 dist/红杏-debug.exe
    python packaging/smoke_test.py --exe <路径>    # 测指定文件
    python packaging/smoke_test.py --keep-temp     # 保留临时目录, 便于排查
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

EXE_NAMES = {False: ("红杏.exe", "hongxing.exe"), True: ("红杏-debug.exe", "hongxing-debug.exe")}

#: 单次调用的超时。首次运行要解压 + 可能被杀软全盘扫一遍, 给宽一点。
TIMEOUT = 240

#: 打印输出时最多回显多少行
MAX_ECHO_LINES = 60


def use_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def say(message: str = "") -> None:
    print(message, flush=True)


class Check:
    def __init__(self, name: str, *, required: bool = True) -> None:
        self.name = name
        self.required = required
        self.ok = False
        self.detail = ""

    def set(self, ok: bool, detail: str) -> "Check":
        self.ok = bool(ok)
        self.detail = detail
        return self


def echo(text: str, *, limit: int = MAX_ECHO_LINES) -> None:
    lines = (text or "").rstrip().splitlines()
    if not lines:
        say("      (无输出)")
        return
    for line in lines[:limit]:
        say(f"      | {line}")
    if len(lines) > limit:
        say(f"      | ...(还有 {len(lines) - limit} 行, 已截断)")


def run_exe(
    exe: Path,
    args: list[str],
    *,
    cwd: Path,
    env_extra: dict[str, str] | None = None,
    env_replace: dict[str, str] | None = None,
):
    """跑一次 exe, 返回 (CompletedProcess, 耗时秒)。

    env_replace 给定时**完全替换**环境变量(用于模拟"用户机器上没有 Python"),
    否则在 os.environ 之上叠加 env_extra。
    """
    env = dict(env_replace) if env_replace is not None else os.environ.copy()
    env["HONGXING_NO_MESSAGEBOX"] = "1"  # 自动化里绝不能被模态框卡住
    env["PYTHONUTF8"] = "1"
    env.update(env_extra or {})
    started = time.time()
    proc = subprocess.run(
        [str(exe), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT,
    )
    return proc, time.time() - started


def pythonless_env() -> dict[str, str]:
    """一个"看起来像没装过 Python 的用户机"的环境。"""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env["PATH"] = r"C:\Windows\system32;C:\Windows;C:\Windows\System32\Wbem"
    env["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
    env["TEMP"] = env["TMP"] = tempfile.gettempdir()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV", "CONDA_PREFIX"):
        env.pop(key, None)
    return env


def first_shell() -> str | None:
    """优先 pwsh(7): Windows PowerShell 5.1 读 `-Command` 时按 OEM 代码页解释,
    带中文路径(红杏.exe)的命令串会直接失败(实测 rc=5)。路径一律用环境变量传。"""
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


def find_exe(*, debug: bool, explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    for name in EXE_NAMES[debug]:
        candidate = DIST / name
        if candidate.is_file():
            return candidate
    return None


def main(argv: list[str]) -> int:
    use_utf8()
    parser = argparse.ArgumentParser(prog="python packaging/smoke_test.py")
    parser.add_argument("--debug", action="store_true", help="测带控制台的排错版")
    parser.add_argument("--exe", help="直接指定要测的 exe")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时目录")
    args = parser.parse_args(argv)

    say("红杏 打包产物冒烟测试")
    say("=" * 70)

    exe = find_exe(debug=args.debug, explicit=args.exe)
    checks: list[Check] = []
    temp_root = Path(tempfile.mkdtemp(prefix="hongxing-smoke-"))

    try:
        # ---------------- 1. 产物存在 ----------------
        exists = Check("产物存在且体积合理")
        if exe is None:
            exists.set(False, f"没找到 exe(找过 {[n for n in EXE_NAMES[args.debug]]}), 请先跑 packaging/build.py")
        else:
            size = exe.stat().st_size
            exists.set(size > 5 * 1024 * 1024, f"{exe}  {size:,} 字节 ({size / 1024 / 1024:.1f} MiB)")
        checks.append(exists)
        say(f"[1] {exists.name}: {'OK' if exists.ok else '失败'} - {exists.detail}")
        if not exists.ok:
            return finish(checks)

        assert exe is not None
        # exe 之外的一切都在临时目录里: 顺便证明程序不依赖当前工作目录
        # (用户从快捷方式双击时 cwd 是任意的)。ACCESSPILOT_HOME 隔离掉真实用户数据。
        workdir = temp_root / "cwd"
        workdir.mkdir(parents=True, exist_ok=True)
        home = temp_root / "home"
        base_env = {"ACCESSPILOT_HOME": str(home)}

        # ---------------- 2. --version ----------------
        check = Check("红杏.exe --version")
        proc, cost = run_exe(exe, ["--version"], cwd=workdir, env_extra=base_env)
        output = (proc.stdout or "") + (proc.stderr or "")
        check.set(proc.returncode == 0 and "AccessPilot" in output, f"退出码 {proc.returncode}, {cost:.1f}s")
        say(f"\n[2] {check.name}: {'OK' if check.ok else '失败'} - {check.detail}")
        echo(output)
        checks.append(check)

        # ---------------- 3. 冻结后的 `-m accesspilot` 再入口 ----------------
        check = Check("红杏.exe -m accesspilot --version (冻结后再入口)")
        proc, cost = run_exe(exe, ["-m", "accesspilot", "--version"], cwd=workdir, env_extra=base_env)
        output = (proc.stdout or "") + (proc.stderr or "")
        check.set(
            proc.returncode == 0 and "AccessPilot" in output,
            f"退出码 {proc.returncode}, {cost:.1f}s "
            f"(accesspilot 内部用 `sys.executable -m accesspilot` 拉起看门狗/加速器)",
        )
        say(f"\n[3] {check.name}: {'OK' if check.ok else '失败'} - {check.detail}")
        echo(output)
        checks.append(check)

        # ---------------- 4. 冻结环境内部自检 ----------------
        check = Check("红杏.exe --selftest (资源 + tkinter)")
        proc, cost = run_exe(exe, ["--selftest"], cwd=workdir, env_extra=base_env)
        output = (proc.stdout or "") + (proc.stderr or "")
        detail = f"退出码 {proc.returncode}, {cost:.1f}s"
        report = None
        try:
            report = json.loads(proc.stdout or "")
            inner = report.get("checks", {})
            bad = [k for k, v in inner.items() if v.get("required") and not v.get("ok")]
            detail = (
                f"{detail}, frozen={report.get('frozen')}, "
                f"meipass={report.get('meipass')}, 失败项={bad or '无'}"
            )
            check.set(proc.returncode == 0 and not bad, detail)
        except Exception as exc:
            check.set(False, f"{detail}, 无法解析自检 JSON({type(exc).__name__}: {exc})")
        say(f"\n[4] {check.name}: {'OK' if check.ok else '失败'} - {check.detail}")
        echo(output)
        if report:
            say("      自检明细:")
            for name, item in report.get("checks", {}).items():
                flag = "OK  " if item.get("ok") else "FAIL"
                optional = "" if item.get("required", True) else " (可选)"
                say(f"        {flag} {name}{optional}: {item.get('detail')}")
        checks.append(check)

        # ---------------- 5. 双击启动(无控制台)时的日志退路 ----------------
        # 这里必须用 Start-Process **不带重定向** 来启动: Python 的 subprocess
        # 总会给子进程一个有效的 stdout 句柄, 那样 exe 里 sys.stdout 就不是 None,
        # 走的不是"双击"那条路。Start-Process 不传标准句柄, 才是真实的
        # "资源管理器里双击"场景 —— 也正是 windowed 构建下 sys.stdout 变 None、
        # accesspilot/util.py 顶层 isatty() 会炸的那个场景。
        check = Check("双击启动(无控制台)时输出落到日志文件")
        log_path = temp_root / "logs" / "hongxing.log"
        script = (
            "$p = Start-Process -FilePath $env:HX_EXE -ArgumentList '--version' "
            "-Wait -PassThru; exit $p.ExitCode"
        )
        shell = first_shell()
        proc = None
        if shell is None:
            check.set(False, "找不到 pwsh / powershell, 无法模拟双击启动")
        else:
            try:
                proc = subprocess.run(
                    [shell, "-NoProfile", "-Command", script],
                    cwd=str(workdir),
                    env={
                        **pythonless_env(),
                        "HX_EXE": str(exe),
                        "HONGXING_LOG": str(log_path),
                        "HONGXING_NO_MESSAGEBOX": "1",
                    },
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=TIMEOUT,
                )
            except Exception as exc:
                check.set(False, f"启动失败: {type(exc).__name__}: {exc}")
        text = ""
        if log_path.is_file():
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        if proc is not None:
            exited = proc.returncode
            check.set(
                exited == 0 and "红杏 启动" in text and "AccessPilot" in text,
                f"{log_path} ({log_path.stat().st_size if log_path.is_file() else 0} 字节), "
                f"退出码 {exited} (shell={Path(shell).name if shell else 'n/a'})",
            )
        say(f"\n[5] {check.name}: {'OK' if check.ok else '失败'} - {check.detail}")
        echo(text)
        checks.append(check)

        # ---------------- 6. 用户机器上没有 Python 也能跑 ----------------
        check = Check("不依赖宿主 Python (PATH 无 python, 清空 PYTHON*)")
        clean = pythonless_env()
        proc, cost = run_exe(exe, ["--selftest"], cwd=workdir, env_replace=clean)
        output = (proc.stdout or "") + (proc.stderr or "")
        shadow = shutil.which("python", path=clean["PATH"]) or shutil.which(
            "python3", path=clean["PATH"]
        )
        feasible = shadow is None
        check.set(
            feasible and proc.returncode == 0 and '"ok": true' in output.replace("'", '"'),
            f"PATH 里可见的 python={shadow!r}, 退出码 {proc.returncode}, {cost:.1f}s",
        )
        say(f"\n[6] {check.name}: {'OK' if check.ok else '失败'} - {check.detail}")
        echo(output, limit=12)
        checks.append(check)

        # ---------------- 7. 版本资源(信息性) ----------------
        check = Check("exe 版本资源", required=False)
        shell = first_shell()
        try:
            if shell is None:
                raise RuntimeError("找不到 pwsh / powershell")
            info = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-Command",
                    "(Get-Item -LiteralPath $env:HX_EXE).VersionInfo | "
                    "Select-Object FileDescription,FileVersion,ProductName | ConvertTo-Json -Compress",
                ],
                env={**os.environ, "HX_EXE": str(exe)},
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            payload = json.loads(info.stdout or "{}")
            check.set(bool(payload.get("FileDescription")), json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            check.set(False, f"读取失败: {type(exc).__name__}: {exc}")
        say(f"\n[7] {check.name}: {'OK' if check.ok else '跳过/失败'} - {check.detail}")
        checks.append(check)

        return finish(checks)
    finally:
        if args.keep_temp:
            say(f"\n临时目录保留在: {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)


def finish(checks: list[Check]) -> int:
    say("")
    say("=" * 70)
    failed = [c for c in checks if c.required and not c.ok]
    for check in checks:
        flag = "OK  " if check.ok else ("FAIL" if check.required else "SKIP")
        say(f"  [{flag}] {check.name}")
    say("=" * 70)
    if failed:
        say(f"结果: 失败 {len(failed)} 项 / 共 {len(checks)} 项")
        for check in failed:
            say(f"  - {check.name}: {check.detail}")
        return 1
    say(f"结果: 全部通过 ({len(checks)} 项)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
