"""回归测试: YAML 控制字符必须转义/清洗, 否则整份配置报废.

真实事故(2026-09-30):
    某个免费节点的 `sni` 字段里带了 `ð\\x9f\\x87` —— 那是 UTF-8 的 emoji
    字节被按 Latin-1 解码的产物, 落在 C1 区(U+0080~U+009F)。
    `miniyaml._emit_scalar` 当时只转义 `\\` `"` 和换行, 于是原样写进了 YAML;
    内核直接报:

        yaml: control characters are not allowed

    **整份 6027 个节点的配置**因此加载失败。后果比"少一个节点"严重得多:
      * `free auto` 在配置校验那一步中断, 连"抓完节点立刻测速 + 清理"都没跑到,
        配置档被留在 2.6 MB 的中间状态;
      * 现网 config.yaml 已经变成那份坏配置 —— 内核内存里还是旧的所以没断,
        但**下一次重启或看门狗拉起就会全网断**。

两道防线(测试都要覆盖):
  1. miniyaml: 控制字符一律转义成 \\xNN / \\uNNNN, 保证配置**能加载**;
  2. config.sanitize_proxies: 字符串字段里的控制字符直接洗掉, 保证字段值
     **有意义**(转义后语法合法但 SNI 仍然是垃圾, 握手指令会失败)。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot import config, miniyaml  # noqa: E402

#: 复现事故用的真实字节: \xf0\x9f\x87\xa6 (🇦) 被 Latin-1 解码
MANGLED = "https://t.me/wangcai2" + "\xf0\x9f\x87\xa6".encode("latin-1").decode("latin-1")

#: YAML 禁止的字符(Go yaml.v3 的 c-printable)
ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffe\uffff]")


class TestMiniYamlControlChars(unittest.TestCase):
    def test_mangled_emoji_is_the_real_world_case(self) -> None:
        """确认复现串确实含 C1 字符, 否则后面的断言就是空的."""
        self.assertTrue(ILLEGAL.search(MANGLED))

    def test_control_chars_are_escaped_in_output(self) -> None:
        out = miniyaml.dump({"sni": MANGLED})
        self.assertIsNone(
            ILLEGAL.search(out),
            f"输出里仍有 YAML 非法控制字符: {out!r}",
        )
        self.assertIn("\\x9f", out)
        self.assertIn("\\x87", out)

    def test_roundtrip_preserves_value(self) -> None:
        """转义后必须能原样解析回来, 不能丢信息."""
        data = {"sni": MANGLED, "name": "节点\x01A", "note": "tab\there"}
        self.assertEqual(miniyaml.load(miniyaml.dump(data)), data)

    def test_every_c0_and_c1_char_is_escaped(self) -> None:
        for cp in list(range(0x00, 0x20)) + list(range(0x7F, 0xA0)):
            ch = chr(cp)
            out = miniyaml.dump({"k": "a" + ch + "b"})
            self.assertIsNone(
                ILLEGAL.search(out), f"U+{cp:04X} 没被转义: {out!r}")
            self.assertEqual(miniyaml.load(out)["k"], "a" + ch + "b",
                             f"U+{cp:04X} 往返失败")

    def test_normal_text_untouched(self) -> None:
        """不能因为加了转义就把正常中文/emoji 弄坏."""
        for s in ("香港节点", "🇰🇷 韩国", "US-West #3", "a.b.com", "1.2.3.4"):
            self.assertEqual(miniyaml.load(miniyaml.dump({"k": s}))["k"], s)

    def test_helpers_agree(self) -> None:
        self.assertTrue(miniyaml.has_control_chars(MANGLED))
        self.assertFalse(miniyaml.has_control_chars("正常节点名"))
        # 只洗掉控制字符: ð(U+00F0) 和 ¦(U+00A6) 是可打印字符, 要留着。
        # 正因为洗出来还是个非主机名, sanitize_proxies 才会把 sni 整个丢掉。
        self.assertEqual(miniyaml.strip_control_chars(MANGLED),
                         "https://t.me/wangcai2ð¦")


class TestSanitizeProxiesControlChars(unittest.TestCase):
    def _node(self, **kw):
        base = {"name": "n", "type": "ss", "server": "1.2.3.4", "port": 443,
                "cipher": "aes-128-gcm", "password": "p"}
        base.update(kw)
        return base

    def test_control_chars_stripped_from_plain_field(self) -> None:
        out = config.sanitize_proxies([self._node(name="节点\x01A")])
        self.assertEqual(out[0]["name"], "节点A")

    def test_bogus_sni_is_dropped_entirely(self) -> None:
        """洗出来不是合法主机名 -> 丢字段, 而不是留个垃圾 SNI 去握手."""
        out = config.sanitize_proxies(
            [self._node(sni=MANGLED, **{"skip-cert-verify": True})])
        self.assertNotIn("sni", out[0])

    def test_clean_sni_survives(self) -> None:
        out = config.sanitize_proxies([self._node(sni="images.ctfassets.net")])
        self.assertEqual(out[0]["sni"], "images.ctfassets.net")

    def test_generated_config_has_no_illegal_chars(self) -> None:
        """端到端: 带毒节点经过 sanitize + dump 后配置必须干净."""
        proxies = config.sanitize_proxies([
            self._node(sni=MANGLED, name="坏\x9f节点"),
            self._node(server="5.6.7.8", name="好节点"),
        ])
        text = miniyaml.dump({"proxies": proxies})
        self.assertIsNone(ILLEGAL.search(text), "生成的配置里仍有非法字符")

    def test_two_layers_are_both_present(self) -> None:
        """单纯依赖转义或单纯依赖清洗都不够, 两层都要在."""
        self.assertTrue(hasattr(miniyaml, "strip_control_chars"))
        # sanitize 必须真的调用清洗
        src = (Path(__file__).resolve().parent.parent
               / "accesspilot" / "config.py").read_text(encoding="utf-8")
        self.assertIn("miniyaml.has_control_chars", src)
        self.assertIn("_is_hostname", src)


if __name__ == "__main__":
    unittest.main()
