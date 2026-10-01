"""回归测试: 配置渲染必须"要么是好的, 要么别动线上那份".

两个真实缺陷(2026-10-01 独立验收时实测到), 都在**渲染层**, 而之前的修复
只补在了 `freenodes.fetch_free()` 里 —— 于是 `process.start()` / `turn_on()` /
订阅导入这几条路依然是裸的。

## 缺陷一: 渲染层没有消重名

上游给的节点名里混一个控制字符(或 emoji 变体选择符), `sanitize_proxies()`
会把它洗掉, 于是两个**本来不同**的名字在渲染阶段撞成一个。内核只要发现一个
重名, 拒绝的是**整份配置**(`proxy X is the duplicate name`), 不是那一个节点。

历史事故(6002 个节点全废)就是这个形态, 而当时只在抓取路径上补了去重。

## 缺陷二: 先写坏文件, 再校验

`render()` 原来是直接覆盖 `runtime/config.yaml`, 校验是**写完之后**才做的。
真实后果: 磁盘上躺着一份坏配置, 而当时内核还在跑(用的是启动时读进内存的那份),
表面一切正常 —— 直到内核因任何原因重启, 读到坏文件, 起不来,
**用户直接断网且开关打不开**。故障现象离起因差了好几个小时。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

from accesspilot import config, paths  # noqa: E402
from accesspilot.state import AppState  # noqa: E402
from accesspilot.subscription import Subscription, uniquify_names  # noqa: E402
from accesspilot.util import Fail  # noqa: E402


def _ss(name: str, server: str = "1.2.3.4") -> dict:
    return {"name": name, "type": "ss", "server": server, "port": 443,
            "cipher": "aes-128-gcm", "password": "pw"}


def _sub(proxies: list[dict]) -> Subscription:
    return Subscription(name="测试订阅", url="", proxies=proxies)


class UniquifyNamesTests(unittest.TestCase):
    def test_plain_duplicates(self) -> None:
        out = uniquify_names([{"name": "X"}, {"name": "X"}, {"name": "X"}])
        self.assertEqual([p["name"] for p in out], ["X", "X #2", "X #3"])

    def test_generated_name_must_not_collide(self) -> None:
        """原实现自己造重名: ['X','X','X #2'] -> ['X','X #2','X #2'].

        `used` 只记录 base, 而生成出来的 `f"{base} #{n}"` 从不检查是否已被占用,
        可输入里本来就完全可能有一个节点就叫 `X #2`。
        """
        out = uniquify_names([{"name": "X"}, {"name": "X"}, {"name": "X #2"}])
        names = [p["name"] for p in out]
        self.assertEqual(len(names), len(set(names)), f"还有重名: {names}")

    def test_output_is_always_unique(self) -> None:
        """性质测试: 无论输入长什么样, 输出必须无重名。

        这条是硬保证 —— 上层所有"名字一定唯一"的假设都建立在它上面。
        """
        inputs = [
            ["X", "X", "X #2", "X #2"],
            ["X #2", "X", "X"],
            ["A", "A #2", "A #3", "A"],
            ["", "", "node"],
            ["X ", "X", " X"],
        ]
        for raw in inputs:
            out = uniquify_names([{"name": n} for n in raw])
            names = [p["name"] for p in out]
            self.assertEqual(len(names), len(set(names)),
                             f"输入 {raw} -> 输出有重名 {names}")


class BuildConfigDedupesTests(unittest.TestCase):
    def test_sanitize_collision_is_resolved(self) -> None:
        """两个不同名字清洗后撞车 —— build_config 必须把它们分开。

        U+009F 是 C1 控制字符, `sanitize_proxies()` 会把它洗掉, 于是
        `X` 和 `X\\x9f` 都会变成 `X`。
        """
        cfg = config.build_config(_sub([_ss("X", "1.1.1.1"),
                                        _ss("X\u009f", "2.2.2.2")]), AppState())
        names = [p["name"] for p in cfg["proxies"]]
        self.assertEqual(len(names), len(set(names)), f"渲染出了重名: {names}")

    def test_upstream_duplicates_are_resolved(self) -> None:
        """调用方没先 uniquify 也不能让整份配置报废 —— 防线不能依赖调用方守规矩。"""
        cfg = config.build_config(
            _sub([_ss("香港 #3", "1.1.1.1"), _ss("香港 #3", "2.2.2.2")]), AppState())
        names = [p["name"] for p in cfg["proxies"]]
        self.assertEqual(len(names), len(set(names)), f"渲染出了重名: {names}")

    def test_group_members_all_exist(self) -> None:
        """消重名不能把策略组的成员改丢 —— 那会让内核报 'proxy not found'。

        成员既可以是节点, 也可以是**另一个策略组**(例如 🚀 节点选择 里引用
        ♻️ 自动选择), 所以两边都要算进来。
        """
        cfg = config.build_config(
            _sub([_ss("X", "1.1.1.1"), _ss("X\u009f", "2.2.2.2")]), AppState())
        groups = cfg.get("proxy-groups", [])
        declared = {p["name"] for p in cfg["proxies"]}
        declared |= {g["name"] for g in groups}
        declared |= {"DIRECT", "REJECT", "PASS", "COMPATIBLE", "GLOBAL"}
        for grp in groups:
            for member in grp.get("proxies", []):
                self.assertIn(member, declared,
                              f"策略组 {grp.get('name')} 引用了不存在的 {member!r}")


class RenderNeverClobbersTests(unittest.TestCase):
    """校验必须在文件落地**之前**。"""

    def setUp(self) -> None:
        self.target = paths.config_file()
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.good = "# 上一份好配置\n"
        self.target.write_text(self.good, encoding="utf-8")

    def tearDown(self) -> None:
        for p in self.target.parent.glob("*.candidate"):
            p.unlink(missing_ok=True)

    def test_validates_the_candidate_not_the_live_file(self) -> None:
        """校验的对象必须是候选文件 —— 否则"先写后验"根本没被修掉。"""
        seen: list[str] = []

        def fake_test(path=None):
            seen.append(str(path) if path is not None else "<default>")
            return True, ""

        with mock.patch.object(config, "test_config", side_effect=fake_test):
            out = config.render(_sub([_ss("A")]), AppState())
        self.assertTrue(seen, "render 压根没校验")
        self.assertTrue(seen[0].endswith(".candidate"),
                        f"校验的是 {seen[0]}, 不是候选文件")
        self.assertEqual(out, self.target)

    def test_bad_candidate_keeps_the_old_config(self) -> None:
        with mock.patch.object(config, "test_config",
                               return_value=(False, "proxy X is the duplicate name")):
            with self.assertRaises(Fail):
                config.render(_sub([_ss("A")]), AppState())
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.good,
                         "候选校验失败却把线上配置覆盖掉了")

    def test_bad_candidate_is_cleaned_up(self) -> None:
        with mock.patch.object(config, "test_config", return_value=(False, "坏的")):
            with self.assertRaises(Fail):
                config.render(_sub([_ss("A")]), AppState())
        leftovers = list(self.target.parent.glob("*.candidate"))
        self.assertEqual(leftovers, [], f"候选文件没清掉: {leftovers}")

    def test_validation_exception_keeps_the_old_config(self) -> None:
        """内核不存在 / 超时都会抛 —— 那也不能把线上配置弄没。"""
        with mock.patch.object(config, "test_config", side_effect=OSError("内核不见了")):
            with self.assertRaises(OSError):
                config.render(_sub([_ss("A")]), AppState())
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.good)

    def test_good_candidate_replaces_the_file(self) -> None:
        with mock.patch.object(config, "test_config", return_value=(True, "")):
            config.render(_sub([_ss("A")]), AppState())
        body = self.target.read_text(encoding="utf-8")
        self.assertNotEqual(body, self.good)
        self.assertIn("proxies", body)


if __name__ == "__main__":
    unittest.main()
