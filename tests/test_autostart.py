"""回归测试: 计划任务(保活/节点刷新)的注册方式.

真实事故(2026-10-01): 保活任务 `AccessPilotEnsure` 的 Last Result **一直是 1**,
也就是说它从来没成功过。三个原因叠在一起:

1. **相对路径**。任务里存的是 `cmd /c "".\\accesspilot.CMD" ensure"`, 而任务
   计划的工作目录是 `C:\\Windows\\System32` —— 每次触发都是
   `'.\\accesspilot.CMD' is not recognized as an internal or external command`,
   退出码 1。实测换成绝对路径后退出码 0。
2. **电源条件**。`schtasks /Create /SC MINUTE` 建出来的任务默认带
   `DisallowStartIfOnBatteries=true` + `StopIfGoingOnBatteries=true`。这是台
   笔记本, 一拔电源两个任务就静默不跑(返回 0x800710E0「操作员或管理员拒绝了
   请求」)—— 而移动使用恰恰是这类客户端最需要保活的时候。
3. **`.cmd` 用了 LF 换行**。cmd.exe 对 LF-only 的批处理在带括号的
   `if errorlevel 1 (...)` 块上会解析错乱。已由 `.gitattributes` 的
   `*.cmd text eol=crlf` 锁住。
"""
from __future__ import annotations

import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot import cli  # noqa: E402

NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
ROOT = Path(__file__).resolve().parent.parent


def _render(**kw) -> str:
    """走真实的渲染路径(含 XML 转义), 不是直接 format 模板."""
    defaults = dict(desc="测试", minutes=5, limit_hours=1,
                    arguments='/c ""C:\\x\\accesspilot.cmd" ensure"')
    defaults.update(kw)
    return cli._task_xml(**defaults)


class TaskXmlTests(unittest.TestCase):
    def test_is_valid_xml(self) -> None:
        ET.fromstring(_render())

    def test_battery_conditions_disabled(self) -> None:
        """这是笔记本, 电源条件开着 = 拔了电就不保活."""
        s = next(e for e in ET.fromstring(_render()).iter(f"{NS}Settings"))
        self.assertEqual(s.findtext(f"{NS}DisallowStartIfOnBatteries"), "false")
        self.assertEqual(s.findtext(f"{NS}StopIfGoingOnBatteries"), "false")

    def test_starts_when_available(self) -> None:
        """错过了触发时刻(电脑在睡眠)要补跑, 否则醒来后没人保活."""
        s = next(e for e in ET.fromstring(_render()).iter(f"{NS}Settings"))
        self.assertEqual(s.findtext(f"{NS}StartWhenAvailable"), "true")

    def test_ignores_new_instances(self) -> None:
        """上一轮还没跑完时不要叠一个 —— free auto 要跑十几分钟."""
        s = next(e for e in ET.fromstring(_render()).iter(f"{NS}Settings"))
        self.assertEqual(s.findtext(f"{NS}MultipleInstancesPolicy"), "IgnoreNew")

    def test_repeat_interval_matches_minutes(self) -> None:
        s = _render(minutes=7)
        self.assertIn("<Interval>PT7M</Interval>", s)

    def test_execution_time_limit_set(self) -> None:
        """不设时限的话, 卡住的 free auto 会永远占着任务不让下一轮跑."""
        self.assertIn("<ExecutionTimeLimit>PT3H</ExecutionTimeLimit>",
                      _render(limit_hours=3))

    def test_absolute_path_survives_escaping(self) -> None:
        s = _render(arguments='/c ""C:\\Program Files\\ap\\accesspilot.cmd" ensure"')
        a = next(e for e in ET.fromstring(s).iter(f"{NS}Arguments"))
        self.assertIn("C:\\Program Files\\ap\\accesspilot.cmd", a.text or "")

    def test_xml_special_chars_escaped(self) -> None:
        """路径里带 & 或 < 不能把 XML 弄坏."""
        s = _render(arguments='/c "C:\\a&b\\c<d>"')
        ET.fromstring(s)  # 解析不了就说明没转义


class TaskRegistrationTests(unittest.TestCase):
    def test_status_uses_absolute_shim_path(self) -> None:
        """源码级锁: 拼 Arguments 之前必须把 which() 的结果 resolve 成绝对路径."""
        src = (ROOT / "accesspilot" / "cli.py").read_text(encoding="utf-8")
        self.assertRegex(
            src, r"shim\s*=\s*str\(Path\(shim\)\.resolve\(\)\)",
            "计划任务的工作目录是 System32, 相对路径每次都会失败(退出码 1)",
        )

    def test_uses_xml_not_sc_minute(self) -> None:
        """源码级锁: 必须走 /XML, 否则设不了电源条件."""
        src = (ROOT / "accesspilot" / "cli.py").read_text(encoding="utf-8")
        self.assertIn('"/XML"', src)
        self.assertNotIn('"/SC", "MINUTE"', src)


class CmdFileLineEndingsTests(unittest.TestCase):
    """`.cmd` 必须是 CRLF —— cmd.exe 对 LF-only 的括号块会解析错乱."""

    def test_gitattributes_forces_crlf(self) -> None:
        ga = ROOT / ".gitattributes"
        self.assertTrue(ga.is_file(), "缺 .gitattributes, .cmd 会被 git 检出成 LF")
        text = ga.read_text(encoding="utf-8")
        self.assertRegex(text, r"\*\.cmd\s+text\s+eol=crlf")

    def test_launcher_has_crlf(self) -> None:
        data = (ROOT / "accesspilot.cmd").read_bytes()
        self.assertIn(b"\r\n", data, "accesspilot.cmd 没有 CRLF 换行")
        self.assertNotRegex(
            data.decode("utf-8", "replace"),
            r"(?<!\r)\n",
            "accesspilot.cmd 里还有裸 LF 换行",
        )

    def test_launcher_is_small_and_has_no_bom(self) -> None:
        data = (ROOT / "accesspilot.cmd").read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "BOM 会让 @echo off 失效")
        self.assertLess(len(data), 2048)


if __name__ == "__main__":
    unittest.main()
