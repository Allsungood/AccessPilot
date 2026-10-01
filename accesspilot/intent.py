"""用户开关意图 —— 让「保活」知道什么时候该闭嘴。

## 为什么需要这个模块

`AccessPilotEnsure` 每 5 分钟跑一次 `accesspilot ensure`。它原来的判断只有一句话:
**内核不在跑就拉起来**。缺的正好是另一半 —— *用户要不要它在跑*。于是：

    用户在界面上点了「关闭」 → 代理关掉 → 最多 5 分钟，自己又开了。

一个「一键开关」，点了关却会自己开回来，比根本没有开关更糟：用户会认定开关是坏的，
然后开始不信任界面上显示的任何状态。这是验收时实测到的行为，不是我推演出来的。

## 为什么记的是「本次开机」而不是永久

「大开关」和「开机自启」是两件事：

* 大开关   = 我**现在**要不要走代理
* 开机自启 = 每次开机**帮我弄好**

如果把「关」永久记住，那么用户关机前关掉代理，下次开机「开机自启」就形同虚设 ——
那是拿一个 bug 换另一个 bug。所以意图只在**同一次开机内**有效：这一次开机里用户
说过关，保活就让位；重启之后是全新的一回合，开机自启照常工作。

## 为什么用 uptime 判断"还是不是同一次开机"

最直觉的做法是存一个 `time.time()` 再看有没有跳变。但墙钟会被 NTP 校时、被用户
改表、被时区改动推动 —— 校时几十秒就足以让判断失效，而失效的表现就是**这个 bug
原地复活**，还很难复现。

`GetTickCount64()` 是内核维护的「开机以来的毫秒数」：一次开机内单调递增，重启后
归零。正好是这里需要的语义，而且不受改表、校时、时区影响。

判据：`当前 uptime >= 记录时的 uptime`  ⟹  还是同一次开机。
（重启后 uptime 归零，必然小于任何记录值，于是"不算是同一次"。）

## 拿不准的时候偏向哪边

`_uptime_ms()` 拿不到（非 Windows、调用失败）时，本模块选择**尊重用户意图**
（即认为"用户确实想关"）。因为两种选错的代价不对称：
误判成"想关"只是保活少跑一次，用户点一下开关就恢复；
误判成"没关"就是开关自己弹回去，用户看得见、且会认为软件坏了。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path
from typing import Any

from . import paths
from .util import json_dump, json_load

#: 意图文件名。放在 runtime/ 下（该目录已在 .gitignore 里）。
INTENT_FILE = "user_intent.json"


def intent_file() -> Path:
    return paths.runtime_dir() / INTENT_FILE


def _uptime_ms() -> int | None:
    """开机以来的毫秒数；拿不到（非 Windows / 调用失败）返回 None。

    为什么不用标准库的 `time.monotonic()`：它的起点只保证**进程内**单调，
    跨进程比较是未定义的。而这里恰恰要拿另一个进程（计划任务里的
    `accesspilot ensure`）写下的值来比 —— 必须用全机同一个数，
    也就是内核维护的 GetTickCount64。
    """
    if sys.platform != "win32":
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k32.GetTickCount64
        # restype 必须显式写成 64 位。ctypes 默认按 c_int 解释返回值，
        # 而 GetTickCount64 是 ULONGLONG：开机超过 2^31 毫秒（约 24.8 天）
        # 就会被截成负数，判据 `now >= saved` 整个反过来 —— 表现为"用了
        # 快一个月的电脑上，开关又开始自己弹回去了"。
        fn.restype = ctypes.c_ulonglong
        fn.argtypes = []
        return int(fn())
    except Exception:
        return None


def _write(action: str) -> None:
    payload: dict[str, Any] = {
        "action": action,
        "at": time.time(),
        "uptime_ms": _uptime_ms(),
    }
    try:
        json_dump(intent_file(), payload)
    except Exception:
        # 记意图失败**绝不能**反过来把开关本身弄失败：用户按下开关时关心的是
        # 代理通不通，不是我们有没有记下他的意图。丢一条记录的最坏后果是
        # 保活可能多拉起一次，比"点开关报错"轻得多。
        pass


def mark_off() -> None:
    """用户主动关。"""
    _write("off")


def mark_on() -> None:
    """用户主动开。"""
    _write("on")


def clear() -> None:
    """抹掉记录（测试与排障用）。"""
    try:
        intent_file().unlink(missing_ok=True)
    except Exception:
        pass


def user_wants_off() -> bool:
    """本次开机内，用户最后一次主动操作是不是「关」。"""
    data = json_load(intent_file(), None)
    if not isinstance(data, dict) or data.get("action") != "off":
        return False
    saved, now = data.get("uptime_ms"), _uptime_ms()
    if saved is None or now is None:
        return True
    try:
        return now >= int(saved)
    except (TypeError, ValueError):
        return True


def describe() -> dict[str, Any]:
    """给诊断/自检用：现在记的是什么，以及它算不算数。"""
    return {
        "file": str(intent_file()),
        "recorded": json_load(intent_file(), None),
        "user_wants_off": user_wants_off(),
    }
