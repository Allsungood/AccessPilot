"""回归测试: 节点名的显示清理(theme.clean_node_name).

背景: 免费节点池的名字里混着 emoji 和国旗, 而微软雅黑没有这些字形, Tk 只能
画成 "?" 或空心方块 —— 用户看到"乱码"会以为客户端坏了。所以要清理。

但第一版清理**太粗暴**, 反而制造了新问题(Lead 在验收截图里抓到的):
    免费池里大量节点本来就叫 "🇺🇸US_237|..." / "🇮🇩ID_6|..."
    —— 国旗后面**已经跟着国家码**了。
    无脑把国旗换成国家码就得到 "USUS_237" / "IDID_6", 比乱码还难读。

这个文件把两类坑都钉住:
  1. 国旗后面已经有国家码时, 国旗要**丢掉**而不是补一个;
  2. 国旗后面没有国家码时(如 "🇭🇰 香港 01"), 才补成 "HK";
  3. 变体选择符 U+FE0F 要单独清掉 —— 否则 "☁️ WARP" 去掉 ☁ 之后还剩一个
     看不见但占位的字符, 显示成 "️ WARP"。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot.gui.theme import clean_node_name  # noqa: E402


class FlagHandling(unittest.TestCase):
    def test_flag_already_followed_by_code_is_dropped(self) -> None:
        """🇺🇸US_237 -> US_237, 不能变成 USUS_237."""
        self.assertEqual(clean_node_name("\U0001F1FA\U0001F1F8US_237|777KB/s"),
                         "US_237|777KB/s")
        self.assertEqual(clean_node_name("\U0001F1EE\U0001F1E9ID_6|643KB/s"),
                         "ID_6|643KB/s")
        self.assertEqual(clean_node_name("\U0001F1EF\U0001F1F5JP_60|570KB/s"),
                         "JP_60|570KB/s")

    def test_flag_without_code_becomes_code(self) -> None:
        """🇭🇰 香港 01 -> HK 香港 01(这里没有重复, 该补上)."""
        self.assertEqual(clean_node_name("\U0001F1ED\U0001F1F0 香港 01"),
                         "HK 香港 01")
        self.assertEqual(clean_node_name("\U0001F1F0\U0001F1F7 韩国 | KOR #2"),
                         "KR 韩国 | KOR #2")

    def test_case_insensitive_match(self) -> None:
        """小写国家码也算已经跟了 —— '🇺🇸us_1' 不该变成 'USus_1'."""
        self.assertEqual(clean_node_name("\U0001F1FA\U0001F1F8us_1"), "us_1")

    def test_no_flag_untouched(self) -> None:
        for s in ("TW台湾(mibei77.com 米贝节点分享)", "🇯🇵JP_60".replace("🇯🇵", ""),
                  "US_492|786KB/s|R002-260618 01"):
            self.assertEqual(clean_node_name(s), s)


class VariationSelector(unittest.TestCase):
    def test_variation_selector_removed(self) -> None:
        """'☁️ WARP' 去掉 ☁ 之后不能剩一个看不见的 FE0F."""
        out = clean_node_name("\u2601\ufe0f WARP")
        self.assertEqual(out, "WARP")
        self.assertNotIn("\ufe0f", out)

    def test_other_symbols_removed(self) -> None:
        self.assertEqual(clean_node_name("\u2753Other_5|x"), "Other_5|x")
        self.assertEqual(clean_node_name("\u267b\ufe0f 节点"), "节点")


class GeneralShape(unittest.TestCase):
    def test_whitespace_collapsed_and_stripped(self) -> None:
        self.assertEqual(clean_node_name("  a   b  "), "a b")

    def test_empty_and_none_safe(self) -> None:
        self.assertEqual(clean_node_name(""), "")
        self.assertEqual(clean_node_name(None), "")  # type: ignore[arg-type]

    def test_never_returns_broken_surrogates(self) -> None:
        """清理后不该留下孤立代理项(Tk 画它同样会出问题)."""
        s = "\U0001F1FA\U0001F1F8US_1 \U0001F600 emoji"
        out = clean_node_name(s)
        out.encode("utf-8")  # 编不出来就说明留了坏字符
        self.assertEqual(out, "US_1 emoji")


if __name__ == "__main__":
    unittest.main()
