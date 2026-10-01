"""回归测试: 用户主动关掉之后, 保活任务不许把它偷偷打开.

真实缺陷(2026-10-01, 独立验收时实测到): 界面上点「关闭」, 代理确实关了;
**但最多 5 分钟它自己又开了**。原因是计划的保活任务 `AccessPilotEnsure`
每 5 分钟跑一次 `accesspilot ensure`, 而 cmd_ensure 的判断只有一句话 ——
"内核不在跑就拉起来", 从来没有问过"用户要不要它在跑"。

一个"一键开关"点了关却会自己开回来, 比没有开关更糟: 用户会认定开关坏了,
连带不再相信界面上显示的任何状态。所以这里锁两层:

* `intent` 模块本身的语义(记录、读取、"是不是同一次开机");
* `cmd_ensure` 真的**不再调用** `process.start()` —— 前面那条是单元,
  后面这条才是用户能感知到的那条。
"""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot import cli, control, intent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class _Recorder:
    """记下 process.start() 被调用了几次、参数是什么。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kw) -> None:
        self.calls.append(kw)


def _fake_state():
    st = mock.MagicMock()
    st.active_profile = "测试档"
    st.tun_enable = False
    st.system_proxy_on = False
    st.last_start = 0.0
    return st


class IntentUnitTests(unittest.TestCase):
    """intent 模块自己的语义。"""

    def setUp(self) -> None:
        intent.clear()

    def tearDown(self) -> None:
        intent.clear()

    def test_no_record_means_not_off(self) -> None:
        self.assertFalse(intent.user_wants_off())

    def test_mark_off_is_visible(self) -> None:
        intent.mark_off()
        self.assertTrue(intent.user_wants_off())

    def test_mark_on_clears_off(self) -> None:
        intent.mark_off()
        intent.mark_on()
        self.assertFalse(intent.user_wants_off())

    def test_reading_does_not_consume_the_record(self) -> None:
        """保活每 5 分钟问一次 —— 读操作必须是幂等的, 不能问一次就没了。"""
        intent.mark_off()
        for _ in range(5):
            self.assertTrue(intent.user_wants_off())

    def test_same_boot_keeps_intent(self) -> None:
        with mock.patch.object(intent, "_uptime_ms", return_value=1_000):
            intent.mark_off()
        with mock.patch.object(intent, "_uptime_ms", return_value=9_000_000):
            self.assertTrue(intent.user_wants_off())

    def test_reboot_drops_intent(self) -> None:
        """重启后 uptime 归零 -> 旧记录不再算数。

        这条是防"修一个 bug 换一个 bug": 如果永久记住"关", 用户关机前关了代理,
        下次开机「开机自启」就永远不会工作。
        """
        with mock.patch.object(intent, "_uptime_ms", return_value=10_000_000):
            intent.mark_off()
            self.assertTrue(intent.user_wants_off())
        with mock.patch.object(intent, "_uptime_ms", return_value=30_000):
            self.assertFalse(intent.user_wants_off())

    def test_unknown_uptime_now_respects_user(self) -> None:
        """读的时候拿不到 uptime -> 偏向"用户确实想关"。

        两种误判的代价不对称: 误判成"想关"只是保活少跑一次(用户点一下就好);
        误判成"没关"就是开关自己弹回去, 用户看得见。
        """
        with mock.patch.object(intent, "_uptime_ms", return_value=1_000):
            intent.mark_off()
        with mock.patch.object(intent, "_uptime_ms", return_value=None):
            self.assertTrue(intent.user_wants_off())

    def test_unknown_uptime_at_write_time_respects_user(self) -> None:
        with mock.patch.object(intent, "_uptime_ms", return_value=None):
            intent.mark_off()
            self.assertTrue(intent.user_wants_off())

    def test_corrupt_record_is_not_off(self) -> None:
        """记录文件坏了就当没记过 —— 不能因为一个坏文件让保活永远不干活。"""
        intent.intent_file().parent.mkdir(parents=True, exist_ok=True)
        intent.intent_file().write_text("{ 这不是 json", encoding="utf-8")
        self.assertFalse(intent.user_wants_off())

    def test_write_failure_never_raises(self) -> None:
        """写意图失败绝不能反过来把开关本身弄失败。"""
        with mock.patch.object(intent, "json_dump", side_effect=OSError("disk full")):
            intent.mark_off()  # 抛了就是 bug
            intent.mark_on()

    def test_uptime_is_positive_int(self) -> None:
        if sys.platform != "win32":
            self.skipTest("uptime 只在 Windows 上有意义")
        v = intent._uptime_ms()
        self.assertIsInstance(v, int)
        self.assertGreater(v, 0)

    def test_uptime_restype_is_64bit(self) -> None:
        """源码级锁: GetTickCount64 是 ULONGLONG。

        ctypes 默认按 c_int 解释返回值, 开机超过 2^31 毫秒(约 24.8 天)就会被
        截成负数, 判据 `now >= saved` 整个反过来 —— 表现为"用久了开关又开始
        自己弹回去", 而且极难复现。必须显式写 restype。
        """
        src = (ROOT / "accesspilot" / "intent.py").read_text(encoding="utf-8")
        self.assertIn("c_ulonglong", src)
        self.assertIn("GetTickCount64", src)


class EnsureRespectsIntentTests(unittest.TestCase):
    """`accesspilot ensure` 是保活任务的入口 —— 它必须让位于用户主动的关。"""

    def setUp(self) -> None:
        intent.clear()

    def tearDown(self) -> None:
        intent.clear()

    def _ensure(self, *, force: bool = False, running: bool = False):
        rec = _Recorder()
        args = argparse.Namespace(sysproxy=False, force=force)
        with mock.patch.object(cli.process, "is_running", return_value=running), \
             mock.patch.object(cli.process, "start", rec), \
             mock.patch.object(cli, "load_state", return_value=_fake_state()), \
             mock.patch.object(cli, "save_state"):
            rc = cli.cmd_ensure(args)
        return rc, rec

    def test_off_intent_blocks_ensure(self) -> None:
        """核心回归: 用户在本次开机内关过, 保活就不许再拉起内核。"""
        intent.mark_off()
        rc, rec = self._ensure()
        self.assertEqual(rc, 0)
        self.assertEqual(rec.calls, [], "保活把用户刚关掉的代理又打开了")

    def test_no_intent_still_ensures(self) -> None:
        """没记录时保活照常工作 —— 别把保活整个弄瘸了。"""
        rc, rec = self._ensure()
        self.assertEqual(rc, 0)
        self.assertEqual(len(rec.calls), 1)

    def test_on_intent_still_ensures(self) -> None:
        intent.mark_on()
        rc, rec = self._ensure()
        self.assertEqual(len(rec.calls), 1)

    def test_force_overrides_off_intent(self) -> None:
        """`ensure --force` 是留给用户的逃生口(和排障用的)。"""
        intent.mark_off()
        rc, rec = self._ensure(force=True)
        self.assertEqual(len(rec.calls), 1)

    def test_reboot_lets_autostart_work_again(self) -> None:
        """重启之后保活必须照常拉起 —— 这就是「开机自启」的实现方式。"""
        with mock.patch.object(intent, "_uptime_ms", return_value=10_000_000):
            intent.mark_off()
        with mock.patch.object(intent, "_uptime_ms", return_value=1_000):
            rc, rec = self._ensure()
        self.assertEqual(len(rec.calls), 1, "重启后开机自启失效了")

    def test_already_running_short_circuits(self) -> None:
        intent.mark_off()
        rc, rec = self._ensure(running=True)
        self.assertEqual(rc, 0)
        self.assertEqual(rec.calls, [])


class ControlMarksIntentTests(unittest.TestCase):
    """界面上的大开关(走 control.turn_on/turn_off)必须留下意图记录。

    否则"记录"这一层永远没被写过, 上面那些测试全绿也没有意义。
    """

    def setUp(self) -> None:
        intent.clear()

    def tearDown(self) -> None:
        intent.clear()

    def test_turn_off_marks_intent(self) -> None:
        with mock.patch.object(control, "load_state", return_value=_fake_state()), \
             mock.patch.object(control, "save_state"), \
             mock.patch.object(control.process, "stop", return_value=True), \
             mock.patch.object(control, "snapshot", return_value=mock.MagicMock()):
            control.turn_off()
        self.assertTrue(intent.user_wants_off(),
                        "界面点「关闭」没有留下意图记录 —— 保活会把它打开")

    def test_turn_on_clears_intent(self) -> None:
        intent.mark_off()
        with mock.patch.object(control, "load_state", return_value=_fake_state()), \
             mock.patch.object(control, "save_state"), \
             mock.patch.object(control.process, "is_running", return_value=False), \
             mock.patch.object(control.process, "start"), \
             mock.patch.object(control, "snapshot", return_value=mock.MagicMock()):
            control.turn_on()
        self.assertFalse(intent.user_wants_off(),
                         "界面点「打开」之后保活仍然被挡着")

    def test_failed_turn_off_does_not_mark(self) -> None:
        """关失败了就不该记「用户想关」—— 否则保活被一条假记录挡住。"""
        with mock.patch.object(control, "load_state", return_value=_fake_state()), \
             mock.patch.object(control.process, "stop", side_effect=OSError("锁住了")), \
             mock.patch.object(control, "snapshot", return_value=mock.MagicMock()):
            control.turn_off()
        self.assertFalse(intent.user_wants_off())


class CliStopStartIntentTests(unittest.TestCase):
    """命令行 start/stop 与界面开关是同一件事, 也要记意图。"""

    def setUp(self) -> None:
        intent.clear()

    def tearDown(self) -> None:
        intent.clear()

    def test_cmd_stop_marks_off(self) -> None:
        args = argparse.Namespace()
        with mock.patch.object(cli.process, "stop", return_value=True):
            cli.cmd_stop(args)
        self.assertTrue(intent.user_wants_off())

    def test_cmd_start_marks_on(self) -> None:
        intent.mark_off()
        args = argparse.Namespace(tun=False, no_tun=False, profile=None,
                                  sysproxy=False, no_sysproxy=False)
        with mock.patch.object(cli, "load_state", return_value=_fake_state()), \
             mock.patch.object(cli, "save_state"), \
             mock.patch.object(cli.process, "start"):
            cli.cmd_start(args)
        self.assertFalse(intent.user_wants_off())


if __name__ == "__main__":
    unittest.main()
