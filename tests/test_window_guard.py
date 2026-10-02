"""回归测试: 窗口守护的两个判断, 以及"守护绝不允许静默失效"。

## 背景(全部来自实测采样, 不是推演)

双击 exe 后窗口会**偶尔**自己最小化, 而进程还活着 —— 用户看到的是"双击没反应"。
根因至今没定位, 所以有一个兜底守护。采样(每 100ms/1s 一次)拿到的事实:

* 故障是**概率性**的: 发作时刻见过 t=3.77s, 也见过 **t=24.33s**; 而且 t=24.33s
  那次之后窗口**一直最小化到采样结束**, 没人管。
* 用户主动按最小化时, 窗口状态和故障**逐字节相同**(都是
  `iconic=True rect=(-32000,-32000) 237x39`), 所以靠状态区分不了。

对策是看**时间上的因果**: 用户按最小化之前必然有一次点击, 故障发作时周围没有
任何输入。归因只在**状态翻转的那一帧**做一次。

## 这里锁的第二件事: 守护不许静默失效

第一版"改进"把判断写在了重新排期之前, 而重新排期是普通语句:

    if not self._guard_alive(...):
        return                       # <- 永久停止
    try: ...
    self.root.after(...)             # <- 上面的 return 一执行, 这行永远到不了

结果: t=15 秒后只要有**一次**判断为假(比如用户恰好在动鼠标), 守护就彻底消失,
之后 t=24.33s 那次自己最小化再没人管。而"守护失效"和"故障本身"表现一模一样,
光看现象根本分不出来 —— 所以必须用测试把它钉死, 而不是靠 review 时想起来。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot.gui.app import App  # noqa: E402


class GuardAliveTests(unittest.TestCase):
    def test_alive_inside_window(self) -> None:
        self.assertTrue(App._guard_alive(0.0))
        self.assertTrue(App._guard_alive(1.0))
        self.assertTrue(App._guard_alive(App.WINDOW_GUARD_S - 0.01))

    def test_dead_after_window(self) -> None:
        self.assertFalse(App._guard_alive(App.WINDOW_GUARD_S + 0.01))
        self.assertFalse(App._guard_alive(9999.0))

    def test_window_covers_the_observed_late_strike(self) -> None:
        """实测有 t=24.33s 才发作的一次 —— 窗口必须盖住它。

        最初那版只盯 15 秒, 正好漏掉这一种, 而漏掉的表现就是窗口一直最小化着。
        """
        self.assertTrue(App._guard_alive(24.33))


class UserFightingTests(unittest.TestCase):
    """归因只看**重复的节奏**。

    原本的假设是"翻转那一帧 idle 小 = 用户按的", 但真机数据把它否了:
    窗口在 t=46.72s 自己最小化时, 守护读到 idle = 15ms —— 而同一台机器单独
    测 25 秒的 idle 分布是 2484~27406ms、0/248 次低于 1500ms、零输入事件,
    且 app.py 与采样脚本里都没有任何 keybd_event/SendInput/mouse_event。
    也就是说故障发作前**确实**有一次来路不明的真实输入, 那个推论直接失效。

    换成的判据: 用户要收起窗口会**连着按**, 故障则隔很久才来一次。
    """

    def test_first_ever_minimize_is_not_a_fight(self) -> None:
        """还没救过 -> 不可能是"较劲", 先救。"""
        self.assertFalse(App._is_user_fighting(None))

    def test_immediate_second_minimize_is_a_fight(self) -> None:
        """刚救回来用户又按 -> 他真的想收起来, 让位。"""
        self.assertTrue(App._is_user_fighting(0.0))
        self.assertTrue(App._is_user_fighting(1.5))
        self.assertTrue(App._is_user_fighting(App.FIGHT_WINDOW_S - 0.01))

    def test_late_second_minimize_is_the_bug(self) -> None:
        """实测故障两次发作相隔 22.4 秒(t=24.33 与 t=46.72)-> 必须判成故障。

        这条把"用重复节奏区分"这个设计钉死: 阈值一旦调到 22 秒以上, 故障就会
        被误判成"用户在按", 于是窗口一直最小化着 —— 正是要消灭的现象。
        """
        self.assertFalse(App._is_user_fighting(22.4))
        self.assertFalse(App._is_user_fighting(App.FIGHT_WINDOW_S))
        self.assertFalse(App._is_user_fighting(600.0))

    def test_fight_window_is_well_below_observed_gap(self) -> None:
        self.assertLess(App.FIGHT_WINDOW_S, 22.0)


class GuardNeverDiesSilentlyTests(unittest.TestCase):
    """守护最坏的失败模式是"静默失效" —— 它和故障本身表现一样, 看不出来。"""

    def _app(self):
        """不开窗地造一个 App: 只挂上 _guard_window 用到的那几个属性。"""
        app = object.__new__(App)
        app._closing = False
        rescheduled: list = []

        class FakeRoot:
            def after(self, ms, fn):
                rescheduled.append(ms)

        app.root = FakeRoot()
        return app, rescheduled

    def test_reschedules_after_a_normal_tick(self) -> None:
        app, resched = self._app()
        app._guard_tick = lambda: None
        App._guard_window(app)
        self.assertEqual(resched, [App.WINDOW_GUARD_MS])

    def test_reschedules_even_when_tick_raises(self) -> None:
        """核心回归: 一次异常不许让守护永远消失。

        原来重新排期是 try 之后的普通语句, 抛异常就再也排不上 —— 守护从此
        不再运行, 而日志里一个字都没有。
        """
        app, resched = self._app()

        def boom():
            raise RuntimeError("模拟守护内部出错")

        app._guard_tick = boom
        App._guard_window(app)          # 不许把异常抛出去
        self.assertEqual(resched, [App.WINDOW_GUARD_MS],
                         "守护抛异常后没有再排期 —— 它会静默失效")

    def test_does_not_reschedule_while_closing(self) -> None:
        """退出中不要再排期, 否则窗口销毁后回调还会被触发。"""
        app, resched = self._app()
        app._closing = True
        app._guard_tick = lambda: None
        App._guard_window(app)
        self.assertEqual(resched, [])

    def test_tick_stops_after_window_expires(self) -> None:
        """过了窗口就不再动作(排期仍在继续, 由 _guard_alive 每帧自己判断)。"""
        app = object.__new__(App)
        app._started_at = 0.0            # 很久以前启动
        app._guard_hits = 0
        app._window_was_ok = True
        app._rescue_this = True
        app._idle_ms = staticmethod(lambda: 0)
        app._window_on_screen = lambda: False
        app._native_hwnd = lambda: 0
        App._guard_tick(app)
        self.assertEqual(app._guard_hits, 0, "过了守护窗口还在动手")


class IdleProbeTests(unittest.TestCase):
    def test_idle_probe_returns_sane_value(self) -> None:
        v = App._idle_ms()
        if v is None:
            self.skipTest("这个环境拿不到 GetLastInputInfo")
        self.assertIsInstance(v, int)
        self.assertGreaterEqual(v, 0)
        self.assertLess(v, 2**32)


if __name__ == "__main__":
    unittest.main()
