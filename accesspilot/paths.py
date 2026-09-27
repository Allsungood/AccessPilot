"""运行时目录与路径管理."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "AccessPilot"


def home() -> Path:
    """用户级应用根目录, 跨平台."""
    env = os.environ.get("ACCESSPILOT_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_DIR_NAME
    return Path.home() / (".accesspilot" if os.name != "nt" else APP_DIR_NAME)


def core_dir() -> Path:
    return home() / "core"


def profiles_dir() -> Path:
    return home() / "profiles"


def runtime_dir() -> Path:
    return home() / "runtime"


def ruleset_dir() -> Path:
    """mihomo 的 rule-provider 缓存目录."""
    return runtime_dir() / "ruleset"


def logs_dir() -> Path:
    return home() / "logs"


def cache_dir() -> Path:
    return home() / "cache"


def state_file() -> Path:
    return home() / "state.json"


def config_file() -> Path:
    return runtime_dir() / "config.yaml"


def pid_file() -> Path:
    return runtime_dir() / "core.pid"


def log_file() -> Path:
    return logs_dir() / "core.log"


def accel_pid_file() -> Path:
    return runtime_dir() / "accel.pid"


def accel_log_file() -> Path:
    return logs_dir() / "accel.log"


def watchdog_pid_file() -> Path:
    return runtime_dir() / "watchdog.pid"


def ensure_dirs() -> None:
    for d in (
        home(),
        core_dir(),
        profiles_dir(),
        runtime_dir(),
        ruleset_dir(),
        logs_dir(),
        cache_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)


def core_binary() -> Path:
    """mihomo 可执行文件路径."""
    name = "mihomo.exe" if sys.platform == "win32" else "mihomo"
    return core_dir() / name


def wintun_dll() -> Path:
    return core_dir() / "wintun.dll"
