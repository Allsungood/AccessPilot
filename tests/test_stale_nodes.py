"""回归测试: 节点名失效时要给人话、并且能自己恢复.

## 真实故障(2026-10-03, 用户截图来问)

界面上挂着一条红字:

    控制接口返回 HTTP 400: /proxies/%F0%9F%9A%80%20%E8%8A%82%E7%82%B9%E9%80%89%E6%8B%A9
    b'{"message":"Selector update error: proxy not exist"}\n'

两件事同时错了:

1. **不该把这个显示给用户。** 那是一段 URL 编码加内核原始 JSON, 既没说清
   发生了什么, 也没说该怎么办。
2. **它永远消不掉。** 后台每一轮都拿同一个已经不存在的名字再试一次, 而没有任何
   代码会把那个名字从记忆里清掉。

成因很普通: **节点名会随刷新变化**。`uniquify_names` 是按去重后的集合重新编号的,
所以上一轮叫 `🇸🇬 新加坡 | SGP #5` 的节点, 这一轮可能叫 `SGP #2`、`#5` 干脆不存在。
只要有任何地方钉住了一个名字(记忆、设置里存的"当前节点"), 刷新之后就必然失效。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot import api, health  # noqa: E402
from accesspilot.util import Fail  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class HumanReadableSelectErrorTests(unittest.TestCase):
    def _st(self):
        st = mock.MagicMock()
        st.api_secret = "s"
        return st

    def test_missing_node_becomes_a_sentence(self) -> None:
        raw = Fail('控制接口返回 HTTP 400: /proxies/x b\'{"message":'
                   '"Selector update error: proxy not exist"}\\n\'')
        with mock.patch.object(api, "_call", side_effect=raw):
            with self.assertRaises(Fail) as cm:
                api.select(self._st(), "🚀 节点选择", "🇸🇬 新加坡 | SGP #5")
        msg = str(cm.exception)
        self.assertIn("已经不在当前配置里", msg)
        # 节点名要带上 —— 排查时全靠它
        self.assertIn("SGP #5", msg)
        # 不能把内核原始报文漏出去
        self.assertNotIn("%F0%9F", msg)
        self.assertNotIn("Selector update error", msg)

    def test_other_errors_pass_through_unchanged(self) -> None:
        """只翻译"节点不存在"这一种。别的错要原样抛, 否则会把真问题盖掉。"""
        with mock.patch.object(api, "_call", side_effect=Fail("控制接口鉴权失败")):
            with self.assertRaises(Fail) as cm:
                api.select(self._st(), "g", "n")
        self.assertIn("鉴权失败", str(cm.exception))


class StaleSelectionRecoversTests(unittest.TestCase):
    """后台撞上失效的名字时, 要能自己忘掉它, 而不是每轮再试一次。"""

    def test_forgets_the_stale_name(self) -> None:
        st = mock.MagicMock()
        with mock.patch.object(health.api, "select",
                               side_effect=Fail("节点「X」已经不在当前配置里了")), \
             mock.patch.object(health, "_forget_ai_if") as forget:
            ok_ = health._select(st, "g", "X", quiet=True)
        self.assertFalse(ok_)
        forget.assert_called_once_with("X")

    def test_does_not_forget_on_unrelated_error(self) -> None:
        """别的错误不能顺手把记忆清了 —— 那会把一个好节点误忘掉。"""
        st = mock.MagicMock()
        with mock.patch.object(health.api, "select", side_effect=Fail("控制接口超时")), \
             mock.patch.object(health, "_forget_ai_if") as forget:
            health._select(st, "g", "X", quiet=True)
        forget.assert_not_called()


if __name__ == "__main__":
    unittest.main()
