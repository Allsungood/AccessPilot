"""单元测试: 节点健康监控与单实例(全部离线, 用 mock 顶掉真实网络与内核).

重点锁死真实发生过的坑:
  * "测速 30ms 但打开 X 要十几秒" —— 判定必须走平台级验证, 不能只看延迟;
  * 香港节点 X/Discord 全绿但 ChatGPT 403 —— AI 组必须单独验证后再钉,
    而且 verify_node 的"顺手改策略组"副作用不能把用户本来能用的 AI 节点弄坏;
  * 一次超时就切 = 网络一抖用户就被切走 —— 连续失败达阈值 + 切换冷却;
  * 后台线程静默死掉 —— 异常必须被捕获、记进 last_error 并继续下一轮;
  * 单实例 —— 第二个 acquire 必须返回 False(拿不到就说拿不到)。
"""
from __future__ import annotations

import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401  隔离数据目录(必须在 accesspilot 之前导入)

from accesspilot import api, control, freenodes, health, process, rules  # noqa: E402


# --------------------------------------------------------------------------- #
# 脚手架: 一个离线的"假内核"
# --------------------------------------------------------------------------- #


def ok_result(name: str, *, chatgpt: bool = True, loc: str = "KR") -> dict:
    """X 主页 + 静态资源 + Discord 全通, ChatGPT 可选。"""
    return {
        "name": name, "latency_ms": 150, "x_ok": True, "x_asset_ok": True,
        "discord_ok": True, "chatgpt_ok": bool(chatgpt),
        "chatgpt_loc": loc if chatgpt else "", "chatgpt_status": 200 if chatgpt else 403,
        "detail": "",
    }


def dead_result(name: str, detail: str = "TimeoutError") -> dict:
    """节点死了: 四个平台全不通。"""
    return {
        "name": name, "latency_ms": -1, "x_ok": False, "x_asset_ok": False,
        "discord_ok": False, "chatgpt_ok": False, "chatgpt_loc": "",
        "chatgpt_status": 0, "detail": detail,
    }


def hk_result(name: str) -> dict:
    """香港出口: X/Discord 全绿, ChatGPT 403(OpenAI 不支持中国香港)。"""
    return {
        "name": name, "latency_ms": 121, "x_ok": True, "x_asset_ok": True,
        "discord_ok": True, "chatgpt_ok": False, "chatgpt_loc": "HK",
        "chatgpt_status": 403, "detail": "",
    }


class FakeKernel:
    """离线假内核: 只实现 health 用到的那几个调用, 其余一律不发请求。

    刻意模拟真实 verify_node 的副作用(把 💬 社交平台 / 🤖 AI 服务 切到被测
    节点) —— 这正是"健康检查本身会把用户 ChatGPT 弄坏"的根源, 必须能测。
    """

    def __init__(self, node: str = "节点A") -> None:
        self.running = True
        self.chain: dict[str, str] = {
            rules.G_SELECT: node,
            rules.G_AI: rules.G_SELECT,
            rules.G_SOCIAL: rules.G_SELECT,
        }
        self.selects: list[tuple[str, str]] = []
        self.verify_calls: list[str] = []
        self.verify_timeouts: list[float] = []
        self.verify_map: dict[str, dict] = {}
        self.candidates: list[str] = []
        self.select_error: Exception | None = None
        self.verify_error: Exception | None = None

    # ---- 桩实现 ----
    def proxy(self, st, name: str) -> dict:
        return {"now": self.chain.get(name, ""), "type": "Selector",
                "all": list(self.chain)}

    def select(self, st, group: str, node: str) -> None:
        if self.select_error is not None:
            raise self.select_error
        self.selects.append((group, node))
        self.chain[group] = node          # 真内核里 PUT 之后 now 立刻变

    def list_nodes(self, *, limit: int = 0, alive_only: bool = False) -> list:
        names = self.candidates[:limit] if limit else list(self.candidates)
        return [control.NodeInfo(name=n, latency_ms=100, alive=True) for n in names]

    def verify(self, st, node: str, *, timeout: float = 10.0) -> dict:
        self.verify_calls.append(node)
        self.verify_timeouts.append(timeout)
        if self.verify_error is not None:
            raise self.verify_error
        # 真 verify_node 的副作用: 把社交组和 AI 组切到被测节点
        self.chain[rules.G_SOCIAL] = node
        self.chain[rules.G_AI] = node
        return dict(self.verify_map.get(node) or dead_result(node))

    def install(self, case: unittest.TestCase) -> None:
        patches = [
            mock.patch.object(process, "is_running", side_effect=lambda: self.running),
            mock.patch.object(api, "proxy", side_effect=self.proxy),
            mock.patch.object(api, "select", side_effect=self.select),
            mock.patch.object(freenodes, "verify_node", side_effect=self.verify),
            mock.patch.object(control, "list_nodes", side_effect=self.list_nodes),
        ]
        for p in patches:
            p.start()
            case.addCleanup(p.stop)

    # ---- 断言辅助 ----
    def selects_for(self, group: str) -> list[str]:
        return [name for g, name in self.selects if g == group]


class HealthTestCase(unittest.TestCase):
    """公共脚手架: 每个用例一套干净的假内核 + 干净的计数器。"""

    def setUp(self) -> None:
        health.stop()                      # 上一个用例万一留下线程
        health.configure(
            period=health.DEFAULT_PERIOD_S,
            timeout=health.DEFAULT_TIMEOUT_S,
            failures_to_switch=health.DEFAULT_FAILURES,
            cooldown=health.DEFAULT_COOLDOWN_S,
            max_candidates=health.DEFAULT_MAX_CANDIDATES,
            ai_recheck=health.DEFAULT_AI_RECHECK_S,
            ai_search=health.DEFAULT_AI_SEARCH_S,
            reset=True,
        )
        self._forbid_network()
        self.kernel = FakeKernel()
        self.kernel.install(self)
        self.addCleanup(health.stop)
        self.addCleanup(health.configure, reset=True)

    def _forbid_network(self) -> None:
        """兜底: 万一有哪个桩漏了, 立刻炸出来, 而不是悄悄去连真网络。

        这套测试必须在**完全无网络**的机器上也能跑通(目标机器常常连不上
        PyPI/GitHub, CI 也没有外网)。
        """
        p = mock.patch("accesspilot.util.http_request",
                       side_effect=AssertionError("单元测试不允许发真实网络请求"))
        p.start()
        self.addCleanup(p.stop)

    def wait_until(self, cond, timeout: float = 3.0, interval: float = 0.01) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(interval)
        return bool(cond())


# --------------------------------------------------------------------------- #
# 契约与正常情况
# --------------------------------------------------------------------------- #


class TestContract(HealthTestCase):
    def test_state_has_exact_contract_keys(self) -> None:
        """control.py 按这六个键渲染界面, 少一个界面就崩。"""
        self.assertEqual(
            set(health.state()),
            {"enabled", "running", "last_check", "switches", "last_error", "note"},
        )

    def test_state_does_not_raise_without_kernel(self) -> None:
        self.kernel.running = False
        self.assertFalse(health.state()["running"])
        self.assertEqual(health.state()["switches"], 0)

    def test_failover_once_returns_result_dict(self) -> None:
        self.kernel.verify_map["节点A"] = ok_result("节点A")
        r = health.failover_once(timeout=1.0)
        self.assertIn("ok", r)
        self.assertIn("switched", r)
        self.assertIn("failures", r)
        self.assertEqual(r["node"], "节点A")
        self.assertEqual(self.kernel.verify_timeouts, [1.0], "超时要透传给验证")

    def test_default_timeout_is_eight_seconds(self) -> None:
        """契约里的默认值: failover_once(*, timeout: float = 8.0)。"""
        self.kernel.verify_map["节点A"] = ok_result("节点A")
        health.failover_once()
        self.assertEqual(self.kernel.verify_timeouts, [8.0])


class TestHealthyNode(HealthTestCase):
    def test_healthy_node_is_not_switched(self) -> None:
        self.kernel.verify_map["节点A"] = ok_result("节点A")
        r = health.failover_once()
        self.assertTrue(r["ok"])
        self.assertFalse(r["switched"])
        self.assertEqual(r["failures"], 0)
        self.assertEqual(health.state()["switches"], 0)
        self.assertEqual(self.kernel.selects_for(rules.G_SELECT), [],
                         "节点正常时绝不允许碰 🚀 节点选择")

    def test_healthy_node_pins_ai_group(self) -> None:
        self.kernel.verify_map["节点A"] = ok_result("节点A")
        health.failover_once()
        self.assertEqual(self.kernel.chain[rules.G_AI], "节点A")
        self.assertEqual(health.state()["last_error"], "")

    def test_group_chain_is_resolved_to_real_node(self) -> None:
        """🚀 节点选择 指向 ♻️ 自动选择 时, 要顺着链拿到真正的节点。"""
        self.kernel.chain[rules.G_SELECT] = rules.G_AUTO
        self.kernel.chain[rules.G_AUTO] = "节点A"
        self.kernel.verify_map["节点A"] = ok_result("节点A")
        r = health.failover_once()
        self.assertEqual(r["node"], "节点A")
        self.assertEqual(self.kernel.verify_calls, ["节点A"])

    def test_core_not_running_skips_round(self) -> None:
        """内核没起来时切节点毫无意义, 而且不该把"内核没跑"算成节点失败。"""
        self.kernel.running = False
        r = health.failover_once()
        self.assertEqual(r["reason"], "内核未运行")
        self.assertEqual(r["failures"], 0)
        self.assertEqual(self.kernel.verify_calls, [])

    def test_unreadable_node_skips_without_counting_failure(self) -> None:
        """读不到当前节点(热重载中)= 未知, 不是死 —— 绝不能因此切节点。"""
        self.kernel.chain[rules.G_SELECT] = ""
        r = health.failover_once()
        self.assertEqual(r["reason"], "读不到当前节点")
        self.assertEqual(r["failures"], 0)
        self.assertEqual(self.kernel.verify_calls, [])


# --------------------------------------------------------------------------- #
# 抖动保护: 阈值 + 冷却
# --------------------------------------------------------------------------- #


class TestJitterProtection(HealthTestCase):
    def test_single_failure_does_not_switch(self) -> None:
        self.kernel.verify_map["节点A"] = dead_result("节点A")
        self.kernel.candidates = ["节点B"]
        self.kernel.verify_map["节点B"] = ok_result("节点B")

        r = health.failover_once()
        self.assertFalse(r["switched"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["failures"], 1)
        self.assertEqual(self.kernel.verify_calls, ["节点A"],
                         "没到阈值就不该去试别的节点(省请求, 也避免惊动用户)")
        self.assertIn("继续观察", health.state()["note"])

    def test_fast_but_dead_node_still_counts_as_failure(self) -> None:
        """本机实测的坑: 测速 30ms 的节点打开 X 要十几秒。

        延迟低只是握手快, 不代表整条链路能承载真实页面 —— 判定必须看平台级
        验证的结果, 而且**绝不能**去问内核的延迟(url-test 的延迟就是元凶)。
        """
        quick_but_dead = dead_result("节点A")
        quick_but_dead["latency_ms"] = 30          # url-test 眼里的"好节点"
        self.kernel.verify_map["节点A"] = quick_but_dead
        with mock.patch.object(api, "delay") as delay:
            r = health.failover_once()
            delay.assert_not_called()
        self.assertFalse(r["ok"], "30ms 但打不开页面 = 不可用")
        self.assertEqual(r["failures"], 1)

    def test_threshold_reached_switches(self) -> None:
        self.kernel.verify_map["节点A"] = dead_result("节点A")
        self.kernel.candidates = ["节点B"]
        self.kernel.verify_map["节点B"] = ok_result("节点B")

        self.assertFalse(health.failover_once()["switched"])
        r = health.failover_once()
        self.assertTrue(r["switched"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["new_node"], "节点B")
        self.assertEqual(health.state()["switches"], 1)
        self.assertEqual(self.kernel.chain[rules.G_SELECT], "节点B")
        self.assertIn((rules.G_SELECT, "节点B"), self.kernel.selects)
        # 💬 社交平台 要跟随顶层组, 不能留在刚测死的节点上
        self.assertIn((rules.G_SOCIAL, rules.G_SELECT), self.kernel.selects)
        self.assertIn("已自动切换", health.state()["note"], "界面要能说明白发生了什么")

    def test_failures_reset_after_switch(self) -> None:
        self.kernel.verify_map["节点A"] = dead_result("节点A")
        self.kernel.candidates = ["节点B"]
        self.kernel.verify_map["节点B"] = ok_result("节点B")
        health.failover_once()
        r = health.failover_once()
        self.assertEqual(r["failures"], 0, "切换成功后连续失败计数要清零")

    def test_cooldown_blocks_repeat_switch(self) -> None:
        """切换后立刻又失败时, 冷却期内不许再切(否则会在坏节点间来回跳)。"""
        self.kernel.verify_map["节点A"] = dead_result("节点A")
        self.kernel.candidates = ["节点B"]
        self.kernel.verify_map["节点B"] = ok_result("节点B")
        health.failover_once()
        self.assertTrue(health.failover_once()["switched"])

        # B 上台后也死了: 再连续失败两次, 应该因为冷却而不切
        self.kernel.verify_map["节点B"] = dead_result("节点B")
        health.failover_once()
        r = health.failover_once()
        self.assertFalse(r["switched"])
        self.assertEqual(health.state()["switches"], 1)
        self.assertIn("冷却", health.state()["note"])

    def test_switch_allowed_after_cooldown(self) -> None:
        self.kernel.verify_map["节点A"] = dead_result("节点A")
        self.kernel.candidates = ["节点B"]
        self.kernel.verify_map["节点B"] = ok_result("节点B")
        health.failover_once()
        self.assertTrue(health.failover_once()["switched"])

        self.kernel.verify_map["节点B"] = dead_result("节点B")
        self.kernel.candidates = ["节点C"]
        self.kernel.verify_map["节点C"] = ok_result("节点C")
        health.failover_once()                      # 失败 1 次
        health.configure(cooldown=0.0)              # 让冷却立刻过去
        r = health.failover_once()
        self.assertTrue(r["switched"])
        self.assertEqual(r["new_node"], "节点C")
        self.assertEqual(health.state()["switches"], 2)

    def test_no_replacement_settles_groups_and_keeps_original(self) -> None:
        """候选全死时: 保持原节点, 而且不能把社交/AI 组留在刚测死的节点上。"""
        self.kernel.verify_map["节点A"] = dead_result("节点A")
        self.kernel.candidates = ["死节点"]
        self.kernel.verify_map["死节点"] = dead_result("死节点")

        health.failover_once()
        r = health.failover_once()
        self.assertFalse(r["switched"])
        self.assertIn("没找到实测可用", health.state()["note"])
        self.assertEqual(self.kernel.chain[rules.G_SELECT], "节点A")
        self.assertEqual(self.kernel.chain[rules.G_SOCIAL], rules.G_SELECT)
        self.assertEqual(self.kernel.chain[rules.G_AI], rules.G_SELECT)

    def test_switch_failure_does_not_count(self) -> None:
        """控制接口在切的那一刻挂了: 不能假装切成功, 也不能把错误吞掉。"""
        self.kernel.verify_map["节点A"] = dead_result("节点A")
        self.kernel.candidates = ["节点B"]
        self.kernel.verify_map["节点B"] = ok_result("节点B")
        health.failover_once()
        self.kernel.select_error = RuntimeError("control api down")
        r = health.failover_once()
        self.assertFalse(r["switched"])
        self.assertEqual(health.state()["switches"], 0)
        self.assertIn("control api down", health.state()["last_error"])


# --------------------------------------------------------------------------- #
# ChatGPT / 🤖 AI 服务
# --------------------------------------------------------------------------- #


class TestAiPinning(HealthTestCase):
    def test_switch_pins_ai_to_chatgpt_capable_node(self) -> None:
        """换上去的出口能上 X 但上不了 ChatGPT 时, AI 组要钉到实测可用的节点。"""
        self.kernel.verify_map["节点A"] = dead_result("节点A")
        self.kernel.candidates = ["香港节点", "韩国节点"]
        self.kernel.verify_map["香港节点"] = hk_result("香港节点")
        self.kernel.verify_map["韩国节点"] = ok_result("韩国节点", loc="KR")

        health.failover_once()
        r = health.failover_once()
        self.assertTrue(r["switched"])
        self.assertEqual(r["new_node"], "香港节点", "通用出口按延迟顺序取第一个可用的")
        self.assertEqual(self.kernel.chain[rules.G_AI], "韩国节点",
                         "AI 组必须钉在实测能上 ChatGPT 的节点上")
        self.assertIn((rules.G_AI, "韩国节点"), self.kernel.selects)
        self.assertNotEqual(self.kernel.chain[rules.G_AI], "香港节点")

    def test_ai_pin_survives_hk_exit_node(self) -> None:
        """香港节点当出口时, AI 组不能被 verify_node 的副作用带到香港去。"""
        health._remember_ai("韩国节点")            # 之前实测过、还新鲜
        self.kernel.chain[rules.G_SELECT] = "香港节点"
        self.kernel.verify_map["香港节点"] = hk_result("香港节点")

        r = health.failover_once()
        self.assertTrue(r["ok"], "出口本身(X/Discord)是好的, 不该被切掉")
        self.assertEqual(r["ai_node"], "韩国节点")
        self.assertEqual(self.kernel.chain[rules.G_AI], "韩国节点")
        self.assertEqual(self.kernel.verify_calls, ["香港节点"],
                         "记忆里的 AI 节点还没过期, 不该再花请求重测它")
        self.assertIn("ChatGPT 不可用", r["reason"])

    def test_stale_ai_pin_is_rechecked(self) -> None:
        health._remember_ai("韩国节点")
        health._ai_checked_at = 0.0                # 记忆过期
        self.kernel.chain[rules.G_SELECT] = "香港节点"
        self.kernel.verify_map["香港节点"] = hk_result("香港节点")
        self.kernel.verify_map["韩国节点"] = ok_result("韩国节点", loc="KR")

        health.failover_once()
        self.assertEqual(self.kernel.verify_calls, ["香港节点", "韩国节点"])
        self.assertEqual(self.kernel.chain[rules.G_AI], "韩国节点")

    def test_no_chatgpt_node_falls_back_to_follow_select(self) -> None:
        """免费节点里一个能上 ChatGPT 的都没有时: 明确降级, 不假装成功。"""
        self.kernel.chain[rules.G_SELECT] = "香港节点"
        self.kernel.verify_map["香港节点"] = hk_result("香港节点")
        self.kernel.candidates = ["美国节点"]
        self.kernel.verify_map["美国节点"] = ok_result("美国节点", chatgpt=False)

        r = health.failover_once()
        self.assertTrue(r["ok"])
        self.assertEqual(r["ai_node"], "")
        self.assertEqual(self.kernel.chain[rules.G_AI], rules.G_SELECT)
        self.assertIn("没找到实测能上 ChatGPT", health.state()["note"])

    def test_chatgpt_capable_exit_node_is_pinned_directly(self) -> None:
        self.kernel.verify_map["节点A"] = ok_result("节点A", loc="KR")
        r = health.failover_once()
        self.assertEqual(r["ai_node"], "节点A")
        self.assertEqual(self.kernel.chain[rules.G_AI], "节点A")
        self.assertIn("ChatGPT 可用", r["reason"])


# --------------------------------------------------------------------------- #
# "慢"不等于"死" / 绝不抛异常
# --------------------------------------------------------------------------- #


class TestSlowIsNotDead(HealthTestCase):
    def test_verify_exception_is_not_a_node_failure(self) -> None:
        """控制接口繁忙导致验证做不成 -> 只记错误, 不算失败(否则会误切)。"""
        self.kernel.verify_error = RuntimeError("control api busy")
        r = health.failover_once()
        self.assertEqual(r["failures"], 0)
        self.assertFalse(r["switched"])
        self.assertIn("control api busy", health.state()["last_error"])
        self.assertIn("不记失败", health.state()["note"])

    def test_is_running_exception_is_handled(self) -> None:
        with mock.patch.object(process, "is_running", side_effect=OSError("no pid file")):
            r = health.failover_once()
        self.assertEqual(r["reason"], "无法确认内核状态")
        self.assertIn("no pid file", health.state()["last_error"])

    def test_outer_safety_net_never_raises(self) -> None:
        """连内部状态读取都炸了, 也必须返回结果而不是把异常扔给调用方/线程。"""
        with mock.patch.object(health, "load_state", side_effect=RuntimeError("state broken")):
            r = health.failover_once()
        self.assertEqual(r["reason"], "检查异常")
        self.assertIn("state broken", r["error"])
        # 锁必须被释放, 否则后面所有检查都会一直返回 busy
        self.kernel.verify_map["节点A"] = ok_result("节点A")
        self.assertTrue(health.failover_once()["ok"])

    def test_concurrent_round_returns_busy_instead_of_queueing(self) -> None:
        """界面点"立即检查"时后台正好在跑: 立刻返回忙, 不能把界面卡几十秒。"""
        health._round_lock.acquire()
        try:
            r = health.failover_once()
        finally:
            health._round_lock.release()
        self.assertTrue(r["busy"])
        self.assertEqual(self.kernel.verify_calls, [])


# --------------------------------------------------------------------------- #
# 后台线程
# --------------------------------------------------------------------------- #


class TestBackgroundThread(HealthTestCase):
    def test_start_is_idempotent(self) -> None:
        health.start()
        first = health._thread
        health.start()
        self.assertIs(health._thread, first, "重复 start 不能起第二个线程")
        self.assertTrue(health.state()["running"])
        self.assertTrue(health.state()["enabled"])

    def test_stop_stops_thread_and_clears_flag(self) -> None:
        health.start()
        health.stop()
        self.assertFalse(health.state()["running"])
        self.assertFalse(health.state()["enabled"])

    def test_thread_survives_exception_and_records_error(self) -> None:
        """后台线程最常见的故障是"静默死掉": 异常必须被吃掉、记下来、继续跑。"""
        calls: list[float] = []

        def boom(**kwargs):
            calls.append(time.time())
            raise RuntimeError("boom inside round")

        with mock.patch.object(health, "failover_once", side_effect=boom):
            health.configure(period=0.02)
            health.start()
            self.assertTrue(self.wait_until(lambda: len(calls) >= 3),
                            f"线程没有继续下一轮(只跑了 {len(calls)} 轮)")
            st = health.state()
            self.assertTrue(st["running"], "线程抛异常后仍然活着")
            self.assertIn("boom inside round", st["last_error"])
            self.assertIn("继续下一轮", st["note"])

    def test_thread_keeps_working_after_a_failed_round(self) -> None:
        """真跑一轮: 第一轮发现节点死, 第二轮切换成功 —— 线程要一直活着。"""
        self.kernel.verify_map["节点A"] = dead_result("节点A")
        self.kernel.candidates = ["节点B"]
        self.kernel.verify_map["节点B"] = ok_result("节点B")
        health.configure(period=0.02)
        health.start()
        self.assertTrue(self.wait_until(lambda: health.state()["switches"] >= 1))
        self.assertTrue(health.state()["running"])

    def test_thread_is_daemon(self) -> None:
        health.start()
        self.assertTrue(health._thread.daemon, "daemon 线程才不会拖住进程退出")

    def test_enabled_flag_is_persisted(self) -> None:
        """开关要落盘: 客户端重启后界面不能把"上次开着"显示成关着。"""
        health.start()
        self.assertTrue(health._load_prefs(), "开启后必须落盘")
        health.stop()
        self.assertFalse(health._load_prefs(), "关闭后也要落盘")

    def test_persisted_enabled_resumes_monitor(self) -> None:
        """最坑的一种状态是"开关显示开着, 其实没有线程在监控" —— 必须自愈。"""
        health.stop()
        health._set_enabled_flag(True)      # 模拟上次运行留下的"开"
        st = health.state()
        self.assertTrue(st["enabled"])
        self.assertTrue(st["running"], "state() 应当把持久化的开关真的拉起来")


# --------------------------------------------------------------------------- #
# 单实例
# --------------------------------------------------------------------------- #


@unittest.skipUnless(sys.platform == "win32", "命名互斥体是 Windows 专有实现")
class TestSingleInstance(unittest.TestCase):
    def setUp(self) -> None:
        # 用随机名字: 免得和用户真正开着的红杏(或另一个测试进程)撞车
        self.name = "test-" + uuid.uuid4().hex
        self.addCleanup(health.release_single_instance)

    def test_second_acquire_returns_false(self) -> None:
        self.assertTrue(health.acquire_single_instance(self.name))
        self.assertFalse(health.acquire_single_instance(self.name),
                         "第二个实例必须拿不到名额")

    def test_release_allows_reacquire(self) -> None:
        self.assertTrue(health.acquire_single_instance(self.name))
        health.release_single_instance()
        self.assertTrue(health.acquire_single_instance(self.name))

    def test_different_names_do_not_collide(self) -> None:
        other = self.name + "-b"
        self.assertTrue(health.acquire_single_instance(self.name))
        self.assertTrue(health.acquire_single_instance(other))
        health.release_single_instance()

    def test_activate_missing_window_is_silent(self) -> None:
        """没有客户端窗口时(纯命令行启动)也必须能正常拿到名额。"""
        with mock.patch.object(health, "WINDOW_TITLE_HINTS", ("绝不存在-xyz",)), \
                mock.patch.object(health, "WINDOW_CLASS_HINTS", ("NoSuchClassXyz",)):
            self.assertEqual(health._find_existing_window(), 0)
            self.assertFalse(health._activate_existing_window())
            self.assertTrue(health.acquire_single_instance(self.name))

    def test_release_is_idempotent(self) -> None:
        health.release_single_instance()   # 没拿过也不能炸
        self.assertTrue(health.acquire_single_instance(self.name))
        health.release_single_instance()
        health.release_single_instance()

    def test_loser_calls_activation(self) -> None:
        """拿不到名额时必须去叫已有窗口, 而不是安静退出。"""
        self.assertTrue(health.acquire_single_instance(self.name))
        with mock.patch.object(health, "_activate_existing_window") as activate:
            self.assertFalse(health.acquire_single_instance(self.name))
        activate.assert_called_once()


# --------------------------------------------------------------------------- #
# "把已有窗口叫到最前面": 前台锁 + 托盘窗口(全部离线, 用假 user32 顶掉 Win32)
# --------------------------------------------------------------------------- #


class FakeWindow:
    """一个假窗口: 句柄 + 标题 + 类名 + 是否可见 + 属于哪个进程。"""

    def __init__(self, hwnd: int, title: str, cls: str = "TkTopLevel",
                 *, visible: bool = True, pid: int = 4321) -> None:
        self.hwnd = int(hwnd)
        self.title = title
        self.cls = cls
        self.visible = visible
        self.pid = int(pid)


class FakeUser32:
    """离线假 user32, **如实建模 Windows 的前台锁**。

    前台锁的语义: 目标窗口不是前台时, 别的进程调 SetForegroundWindow 会被
    直接拒绝(返回 0, 前台不变); 只有把自己的输入队列挂到前台线程上
    (AttachThreadInput) 之后才放行。`lock_active=False` 用来模拟"用户刚
    双击启动, 前台本来就该给它"的情况。
    """

    def __init__(self, windows: list[FakeWindow], *, foreground: int = 0,
                 lock_active: bool = True, attach_grants: bool = True) -> None:
        self.windows = list(windows)
        self.foreground = int(foreground or 0)
        self.lock_active = lock_active
        self.attach_grants = attach_grants
        self.attached = False
        self.calls: list[tuple] = []

    # ---- 查询 ----
    def _find(self, hwnd) -> FakeWindow | None:
        for w in self.windows:
            if w.hwnd == int(hwnd or 0):
                return w
        return None

    def GetForegroundWindow(self) -> int:
        return self.foreground

    def IsWindowVisible(self, hwnd) -> bool:
        w = self._find(hwnd)
        return bool(w and w.visible)

    def GetWindowTextLengthW(self, hwnd) -> int:
        w = self._find(hwnd)
        return len(w.title) if w else 0

    def GetWindowTextW(self, hwnd, buf, count) -> int:
        w = self._find(hwnd)
        buf.value = w.title if w else ""
        return len(buf.value)

    def GetClassNameW(self, hwnd, buf, count) -> int:
        w = self._find(hwnd)
        buf.value = w.cls if w else ""
        return len(buf.value)

    def GetWindowThreadProcessId(self, hwnd, pid_ptr):
        w = self._find(hwnd)
        if pid_ptr is not None and w is not None:
            pid_ptr._obj.value = w.pid
        return 9000 + (w.pid if w else 0)

    def FindWindowW(self, cls, title) -> int:
        for w in self.windows:
            if cls and w.cls == str(cls) and (title is None or w.title == str(title)):
                return w.hwnd
            if not cls and title and w.title == str(title):
                return w.hwnd
        return 0

    def EnumWindows(self, callback, lparam) -> bool:
        for w in self.windows:
            if not callback(w.hwnd, lparam):
                break
        return True

    # ---- 动作 ----
    def ShowWindow(self, hwnd, cmd) -> bool:
        self.calls.append(("ShowWindow", int(hwnd), int(cmd)))
        return True

    def BringWindowToTop(self, hwnd) -> bool:
        self.calls.append(("BringWindowToTop", int(hwnd)))
        return True

    def SetForegroundWindow(self, hwnd) -> bool:
        self.calls.append(("SetForegroundWindow", int(hwnd)))
        if self.lock_active and not self.attached:
            return False                      # 前台锁: 别的进程说了不算
        self.foreground = int(hwnd)
        return True

    def AttachThreadInput(self, mine, theirs, attach) -> bool:
        self.calls.append(("AttachThreadInput", int(mine), int(theirs), bool(attach)))
        self.attached = bool(attach) and self.attach_grants
        return True

    def SetWindowPos(self, hwnd, after, x, y, cx, cy, flags) -> bool:
        self.calls.append(("SetWindowPos", int(hwnd), int(after)))
        return True

    def FlashWindowEx(self, info_ptr) -> bool:
        self.calls.append(("FlashWindowEx", int(self.foreground)))
        return True

    # ---- 断言辅助 ----
    def called(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    def topmost_flags(self) -> list[int]:
        return [c[2] for c in self.called("SetWindowPos")]


class FakeKernel32:
    def GetCurrentThreadId(self) -> int:
        return 4242


@unittest.skipUnless(sys.platform == "win32", "窗口/前台锁是 Windows 专有实现")
class TestActivateExistingWindow(unittest.TestCase):
    """题目: 第二个实例启动时, 必须把**已有**窗口真的叫到最前面。

    真实故障: 只调一次 SetForegroundWindow 会被前台锁挡掉, 屏幕上前面
    依然是被最大化的浏览器 —— 用户看到"双击了没反应"。
    """

    def setUp(self) -> None:
        self.browser = FakeWindow(0x2002, "DeepSeek Harness - 浏览器",
                                  "Chrome_WidgetWin_1", pid=999)
        self.main = FakeWindow(0x1001, "红杏 · 一键通行", "TkTopLevel", pid=4321)
        # 托盘隐藏消息窗口: 同进程、标题也带自家名字, 但它**不是**主窗口
        self.tray = FakeWindow(0x3003, "HongXingTraySink", "HongXingTrayWnd_4321_1",
                               visible=False, pid=4321)
        self.u32 = FakeUser32([self.browser, self.main, self.tray],
                              foreground=self.browser.hwnd)
        for target, value in (("_user32_dll", lambda: self.u32),
                              ("_kernel32_dll", FakeKernel32)):
            p = mock.patch.object(health, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_finds_main_window_not_tray(self) -> None:
        """托盘那个隐藏窗口绝不能当目标 —— 点了它等于什么都没发生。"""
        self.assertEqual(health._find_existing_window(), self.main.hwnd)

    def test_tray_only_windows_means_no_target(self) -> None:
        self.u32.windows = [self.tray]
        self.assertEqual(health._find_existing_window(), 0)
        self.assertFalse(health._activate_existing_window())

    def test_no_window_is_a_noop(self) -> None:
        with mock.patch.object(health, "_find_existing_window", return_value=0):
            self.assertFalse(health._activate_existing_window())
        self.assertEqual(self.u32.calls, [])

    def test_already_foreground_does_nothing(self) -> None:
        """已经在前台就别动 —— 抢前台等于跟正在打字的用户抢键盘。"""
        self.u32.foreground = self.main.hwnd
        self.assertTrue(health._activate_existing_window())
        self.assertEqual(self.u32.calls, [])

    def test_tray_foreground_counts_as_ours(self) -> None:
        """托盘窗口偶尔会拿到前台, 它也是"我们自己人"。

        否则主窗口会误判"我没在前台", 于是一直抢、一直保持置顶 ——
        用户看到的就是一个赖在最前面不走的窗口。
        """
        self.u32.foreground = self.tray.hwnd
        self.assertTrue(health._owns_foreground(self.main.hwnd))
        self.assertTrue(health._activate_existing_window())
        self.assertEqual(self.u32.calls, [], "自家窗口在前台时不该有任何动作")

    def test_other_process_foreground_is_not_ours(self) -> None:
        self.assertFalse(health._owns_foreground(self.main.hwnd))
        self.assertFalse(health.foreground_is_ours(self.main.hwnd))

    def test_public_helper_matches_internal(self) -> None:
        """给 GUI 用的公开版本: 托盘窗口当前台时也算"自己人"。"""
        self.u32.foreground = self.tray.hwnd
        self.assertTrue(health.foreground_is_ours(self.main.hwnd))
        self.u32.foreground = self.main.hwnd
        self.assertTrue(health.foreground_is_ours(self.main.hwnd))

    def test_plain_activation_when_not_locked(self) -> None:
        """用户双击启动时前台本来就该给它: 一次 SetForegroundWindow 就够。"""
        self.u32.lock_active = False
        self.assertTrue(health._activate_existing_window())
        self.assertEqual(self.u32.foreground, self.main.hwnd)
        self.assertEqual(self.u32.called("AttachThreadInput"), [])
        self.assertEqual(self.u32.called("SetWindowPos"), [], "没被挡住就不用置顶")

    def test_attach_thread_input_beats_the_lock(self) -> None:
        """前台锁挡路时的正规解法: 挂到前台线程的输入队列上再来一次。"""
        self.assertTrue(health._activate_existing_window())
        self.assertEqual(self.u32.foreground, self.main.hwnd)
        attaches = self.u32.called("AttachThreadInput")
        self.assertEqual([c[3] for c in attaches], [True, False],
                         "挂接之后必须解除, 否则两个进程的输入队列会绑在一起")
        self.assertEqual(self.u32.called("SetWindowPos"), [],
                         "已经真的拿到前台了, 不需要置顶兜底")

    def test_topmost_and_flash_when_lock_wins(self) -> None:
        """连挂接都不管用时, 也要保证用户看得见: 短暂置顶 + 闪任务栏。"""
        self.u32.attach_grants = False
        ok = health._activate_existing_window(wait=0.05, hold=0.15)
        self.assertFalse(ok, "确实没拿到前台就如实返回 False")
        self.assertEqual(self.u32.topmost_flags(), [-1, -2],
                         "先置顶(HWND_TOPMOST=-1), 结束必须取消(-2)")
        self.assertTrue(self.u32.called("FlashWindowEx"), "前台锁挡着时闪任务栏")
        self.assertEqual(self.u32.foreground, self.browser.hwnd)

    def test_topmost_is_dropped_even_if_wait_loop_breaks(self) -> None:
        """轮询里出任何岔子, 也绝不能把窗口留成永久置顶。"""
        self.u32.attach_grants = False
        seen = {"n": 0}

        def flaky(hwnd: int) -> bool:
            seen["n"] += 1
            if seen["n"] <= 3:          # 前三次: 确实还没拿到前台
                return False
            raise RuntimeError("查询炸了")

        with mock.patch.object(health, "_owns_foreground", side_effect=flaky):
            ok = health._activate_existing_window(wait=0.05, hold=0.12)
        self.assertFalse(ok)
        self.assertEqual(self.u32.topmost_flags(), [-1, -2])

    def test_title_priority_prefers_brand(self) -> None:
        """标题里带项目路径的终端窗口不该盖过真正的客户端窗口。"""
        terminal = FakeWindow(0x4004, "C:\\Users\\x\\AccessPilot - PowerShell",
                              "CASCADIA_HOSTING_WINDOW_CLASS", pid=777)
        self.u32.windows = [terminal, self.main, self.tray]
        self.u32.foreground = self.browser.hwnd
        self.assertEqual(health._find_existing_window(), self.main.hwnd)

    def test_pick_main_window_prefers_full_product_title(self) -> None:
        """真实踩过: 同事的"红杏托盘自测"窗口也含"红杏", 不能叫错窗口。"""
        windows = [(0x6006, "TkTopLevel", "红杏托盘自测"),
                   (0x1001, "TkTopLevel", "红杏 · 一键通行"),
                   (0x7007, "Chrome_WidgetWin_1", "AccessPilot 文档")]
        self.assertEqual(health._pick_main_window(windows), 0x1001)

    def test_pick_main_window_ignores_unrelated_titles(self) -> None:
        windows = [(0x8008, "Notepad", "购物清单")]
        self.assertEqual(health._pick_main_window(windows), 0)

    def test_brand_windows_beat_english_name_windows(self) -> None:
        terminal = FakeWindow(0x4004, "C:\\src\\AccessPilot - PowerShell",
                              "CASCADIA_HOSTING_WINDOW_CLASS", pid=777)
        selftest = FakeWindow(0x6006, "红杏托盘自测", "TkTopLevel", pid=23356)
        self.u32.windows = [terminal, selftest, self.main]
        self.assertEqual(health._find_existing_window(), self.main.hwnd)

    def test_invisible_windows_are_skipped(self) -> None:
        hidden = FakeWindow(0x5005, "红杏(隐藏)", "TkTopLevel", visible=False, pid=4321)
        self.u32.windows = [hidden, self.main]
        self.assertEqual(health._find_existing_window(), self.main.hwnd)

    def test_hidden_to_tray_window_is_still_brought_back(self) -> None:
        """收进托盘的客户端(窗口隐藏)同样要被叫回来, 而不是"双击没反应"。"""
        hidden = FakeWindow(0x5005, "红杏 · 一键通行", "TkTopLevel",
                            visible=False, pid=4321)
        self.u32.windows = [hidden]
        self.u32.foreground = self.browser.hwnd
        self.u32.lock_active = False
        self.assertEqual(health._find_existing_window(), 0, "默认只认可见窗口")
        self.assertTrue(health._activate_existing_window())
        self.assertEqual(self.u32.foreground, hidden.hwnd)
        self.assertIn(("ShowWindow", hidden.hwnd, 9), self.u32.calls,
                      "必须先 SW_RESTORE 把隐藏窗口还原出来")


if __name__ == "__main__":
    unittest.main()
