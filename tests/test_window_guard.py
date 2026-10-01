"""回归测试: 窗口守护"还要不要继续盯"的判断。

## 背景(全部来自独立验收的实测数据, 不是我推演的)

双击 exe 后窗口会**偶尔**自己最小化, 而进程还活着 —— 用户看到的是"双击没反应"。
根因至今没定位, 所以有一个兜底守护: 发现窗口被最小化就恢复回来。

验收方用 100ms 间隔采样 3 轮冷启动, 拿到两个关键事实:

1. **故障是概率性的** —— 3 轮里复现 1 轮(窗口首帧 t=1.45s, 自己最小化在 t=3.77s)。
   所以"修好之后跑 N 次都正常"这种说法在 N 小的时候区分不了"修好了"和"运气好"。
2. **它不只在头几秒** —— 另有一轮在 **t=32.31s** 才自己最小化, 那时固定的 15 秒
   守护窗口早已过期, 窗口就一直最小化着。

第 2 条说明"只盯 15 秒"覆盖不到。但如果无脑延长守护时间, 又会去抢用户主动按的
最小化(验收方实测: 守护窗口内按最小化会被弹回来)。

判据是:**"从我启动到现在一次键鼠输入都没有" ⟹ 窗口不可能是用户按小的。**
这种情况下才放宽守护时长, 两种情况就都照顾到了。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot.gui.app import App  # noqa: E402


class GuardAliveTests(unittest.TestCase):
    def test_inside_regular_window_always_guards(self) -> None:
        """常规窗口内不看输入 —— 哪怕用户正在疯狂动鼠标也要兜住故障。"""
        for idle in (None, 0, 50, 10_000):
            self.assertTrue(App._guard_alive(1.0, idle), f"idle={idle}")
            self.assertTrue(App._guard_alive(14.9, idle), f"idle={idle}")

    def test_after_regular_window_user_input_stops_the_guard(self) -> None:
        """用户动过键鼠 -> 过了常规窗口就收手, 不抢他的最小化。"""
        # age=20s, 最后一次输入在 1 秒前 -> 用户在操作
        self.assertFalse(App._guard_alive(20.0, 1_000))

    def test_after_regular_window_no_input_keeps_guarding(self) -> None:
        """启动至今无人碰过键鼠 -> 可以放心继续盯(实测故障最晚在 t=32s)。"""
        # age=32s, 最后一次输入在 300 秒前(= 启动之前) -> 至今无人操作
        self.assertTrue(App._guard_alive(32.0, 300_000))

    def test_idle_exactly_at_launch_counts_as_no_input(self) -> None:
        """边界: idle == age 表示最后一次输入正好在启动那一刻(双击图标那次)。"""
        self.assertTrue(App._guard_alive(30.0, 30_000))
        # 早 1ms 也算"启动之前"
        self.assertTrue(App._guard_alive(30.0, 30_001))
        # 晚 1ms 就是启动之后有人动过
        self.assertFalse(App._guard_alive(30.0, 29_999))

    def test_unknown_idle_stops_after_regular_window(self) -> None:
        """拿不到 idle 时按保守处理: 过了常规窗口就收手。

        这里和 intent.py 的取舍**方向相反**, 是刻意的: 那边的误判代价是
        "保活少跑一次", 这边的误判代价是"去抢用户的操作权"。抢用户的窗口
        比漏兜一次更糟, 所以拿不准时选择不抢。
        """
        self.assertFalse(App._guard_alive(20.0, None))
        self.assertFalse(App._guard_alive(20.0, None))

    def test_hard_upper_bound(self) -> None:
        """再久也要停 —— 否则一个忘了关的守护会永远盯着窗口。"""
        self.assertFalse(App._guard_alive(App.WINDOW_GUARD_IDLE_S + 1, 10**9))
        self.assertTrue(App._guard_alive(App.WINDOW_GUARD_IDLE_S - 1, 10**9))

    def test_idle_window_is_longer_than_regular(self) -> None:
        """源码级锁: 两档必须是"放宽"而不是写反了。"""
        self.assertGreater(App.WINDOW_GUARD_IDLE_S, App.WINDOW_GUARD_S)


class IdleProbeTests(unittest.TestCase):
    def test_idle_probe_returns_sane_value(self) -> None:
        """真调一次 GetLastInputInfo。拿不到(None)也算合法, 但不能是负数。"""
        v = App._idle_ms()
        if v is None:
            self.skipTest("这个环境拿不到 GetLastInputInfo")
        self.assertIsInstance(v, int)
        self.assertGreaterEqual(v, 0)
        self.assertLess(v, 2**32)


if __name__ == "__main__":
    unittest.main()
