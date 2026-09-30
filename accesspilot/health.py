"""节点健康监控: 发现"节点死了"就自动换一个 + 单实例保护.

为什么需要这一层
================
用户的原话是「稳定是关键」。红杏目前最大的不稳定来源是免费节点 —— 它们
随时会失效, 而内核自带的 url-test **只看延迟, 不看能不能真的打开网页**。
本机实测踩过的坑:

  * 一个测速 30ms 的节点, 打开 X 要十几秒(握手快 ≠ 整条链路能承载真实页面);
  * 香港节点 X / Discord / Google 全绿, 但 ChatGPT 返回 403 —— OpenAI 不
    支持中国香港, 网页显示 "Unable to load site";
  * 更坑的是: 内核的 url-test 健康检查**把 403 当成成功** —— 实测把探测地址
    换成 chatgpt.com 后, 香港节点依然报 "121ms 成功"。所以"让 url-test 去
    过滤"这条路是死的。

结论: 判定"节点还能不能用"必须用**平台级验证**(真的发请求、真的读状态码),
也就是 freenodes.verify_node(): X 主页 + X 静态资源 + Discord + ChatGPT
四项。本模块只做三件事:

  1. 后台线程定期(默认 60 秒)检查当前出口节点是否**真的**还能用;
  2. 连续失败到阈值后自动换节点, 并把 🤖 AI 服务 钉到实测能上 ChatGPT
     的节点上(参考 cli `free auto` 的那套链条);
  3. 单实例: 同一台机器只允许开一个红杏, 第二次启动把已有窗口叫到前台。

为什么必须做"抖动保护"
----------------------
网络本来就会抖: 一次超时、一次 DNS 抽风、控制接口忙 200ms, 都不代表节点
死了。一发现失败就切会造成两个后果: 用户正在下载/看视频时被切走; 频繁对
内核发 select + 平台验证(每个节点 4 个真实请求)反而把链路压得更差。所以
这里要求**连续 N 次(默认 2 次)失败**才动手, 切换后还有冷却时间(默认
30 秒), 避免在几个"都不太好"的节点之间来回跳。

为什么"慢"不算"死"
------------------
内核满载时控制接口实测要 82ms, 刷新节点池时会到秒级; 免费节点打开一个真实
页面要几秒到十几秒。慢是常态, 因此: 单次失败只累计不清零、读不到当前节点
就跳过本轮(**不记失败**)、验证超时留得足够宽、控制接口报错也不算节点失败。

线程安全与"绝不静默死掉"
------------------------
后台线程是 daemon; 任何异常都被捕获、记进 last_error, 然后**继续下一轮**
—— 后台线程静默退出是"自动切换突然不工作了"最常见的原因。stop() 只发信号
+ 短暂 join(2 秒), 绝不长时间阻塞界面。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Any

from . import api, freenodes, paths, process, rules
from .state import AppState, load_state
from .util import json_dump, json_load

# --------------------------------------------------------------------------- #
# 可调参数(见 configure())
# --------------------------------------------------------------------------- #

#: 后台轮询周期。60 秒是"够快能救回来"和"够省不打扰内核"的折中: 免费节点
#: 失效往往持续几十分钟, 早 30 秒发现没有意义; 但每轮要做 4 个真实请求,
#: 太频繁对本机和免费节点都是负担。
DEFAULT_PERIOD_S = 60.0

#: 单个请求的超时。故意留得比"正常应该多快"宽 —— 免费节点慢是常态, 把超时
#: 压到 3 秒会把"只是慢"的节点误判成死(这正是 url-test 的毛病)。
DEFAULT_TIMEOUT_S = 8.0

#: 连续失败多少次才切换。1 次太敏感(网络抖一下就切), 4 次以上恢复太慢。
#: 2 次 = 连续两轮(默认约 2 分钟)都验证不通过才动手。
DEFAULT_FAILURES = 2

#: 切换后的冷却时间: 防止在几个"都不太好"的节点之间来回跳。
DEFAULT_COOLDOWN_S = 30.0

#: 一轮最多实测几个候选节点。每个候选最多 4 个请求 × 超时, 所以这个数字
#: 直接决定"最坏情况下多久能切过去"。
DEFAULT_MAX_CANDIDATES = 5

#: 记住的"能上 ChatGPT 的节点"多久算过期。过期后才重新实测它。
DEFAULT_AI_RECHECK_S = 900.0

#: 两次"为 AI 组扫候选节点"之间的最小间隔。免费节点里能上 ChatGPT 的是
#: 少数, 每一轮都扫会让后台线程一直占着链路(用户在打游戏时尤其明显)。
DEFAULT_AI_SEARCH_S = 600.0

#: 解析策略组链的最大深度(G_SELECT -> 自动选择 -> 节点)。
_RESOLVE_DEPTH = 4

#: 内核里的组名 —— 沿链解析时"这不是节点, 继续往下走"。
#: 与 control.py 的 _GROUP_NAMES 保持一致(分成两个模块是不想让 health 依赖
#: 界面契约层: 这里要能在没有 GUI 的环境里独立测试)。
_GROUP_NAMES = frozenset({
    rules.G_SELECT, rules.G_AUTO, rules.G_AI, rules.G_SOCIAL, rules.G_MEDIA,
    rules.G_DIRECT, rules.G_REJECT, rules.G_FINAL, "GLOBAL", "DIRECT",
    "REJECT", rules.ACCEL_PROXY_NAME,
})

# --------------------------------------------------------------------------- #
# 状态(GUI 通过 state() 读, 全部加锁)
# --------------------------------------------------------------------------- #

_lock = threading.RLock()

#: 是否开着自动切换。持久化在 cache/health.json: 用户开了之后重启客户端,
#: 界面还能把开关恢复成"开"。
_enabled = False
_enabled_loaded = False

_last_check = 0.0
_switches = 0
_last_error = ""
_note = "自动切换未启用"
_failures = 0
_last_switch_at = 0.0

#: 最近一次实测能上 ChatGPT 的节点 —— 用来避免每轮都重新扫一遍候选。
_ai_node = ""
_ai_checked_at = 0.0
_ai_search_at = 0.0

_thread: threading.Thread | None = None
_stop = threading.Event()

#: 同一时刻只允许一轮检查。拿不到就立刻返回"忙", 不排队 —— 排队会把
#: 界面上"立即检查"这类调用卡住几十秒。
_round_lock = threading.Lock()

_period = DEFAULT_PERIOD_S
_timeout = DEFAULT_TIMEOUT_S
_failures_to_switch = DEFAULT_FAILURES
_cooldown = DEFAULT_COOLDOWN_S
_max_candidates = DEFAULT_MAX_CANDIDATES
_ai_recheck = DEFAULT_AI_RECHECK_S
_ai_search = DEFAULT_AI_SEARCH_S


def _prefs_file():
    return paths.cache_dir() / "health.json"


def _load_prefs() -> bool:
    data = json_load(_prefs_file(), None)
    return bool(data.get("enabled")) if isinstance(data, dict) else False


def _save_prefs() -> None:
    try:
        paths.ensure_dirs()
        json_dump(_prefs_file(), {"enabled": bool(_enabled)})
    except Exception:  # noqa: PERF203
        pass  # 落盘失败绝不能影响主流程(用户开不开得成自动切换更重要)


def _ensure_loaded() -> None:
    """首次用到时才读持久化开关 —— 模块导入不做任何文件 IO。"""
    global _enabled, _enabled_loaded, _note
    if _enabled_loaded:
        return
    _enabled_loaded = True
    try:
        _enabled = _load_prefs()
    except Exception:  # noqa: PERF203
        _enabled = False
    if _enabled:
        _note = "自动切换已开启, 等待启动监控线程"


def _set_enabled_flag(on: bool) -> None:
    global _enabled
    with _lock:
        _enabled = bool(on)
    _save_prefs()


def configure(
    *,
    period: float | None = None,
    timeout: float | None = None,
    failures_to_switch: int | None = None,
    cooldown: float | None = None,
    max_candidates: int | None = None,
    ai_recheck: float | None = None,
    ai_search: float | None = None,
    reset: bool = False,
) -> None:
    """调整监控参数(周期/超时/阈值/冷却/候选个数)。

    `reset=True` 额外清空连续失败次数、最近错误、切换计数和 AI 记忆 ——
    单元测试每个用例开始时调一次, 免得用例之间互相污染。所有数值都有
    下限保护: 周期被压到 0 会把免费节点和内核打爆。
    """
    global _period, _timeout, _failures_to_switch, _cooldown, _max_candidates
    global _ai_recheck, _ai_search, _failures, _last_error, _switches
    global _last_switch_at, _ai_node, _ai_checked_at, _ai_search_at
    with _lock:
        if period is not None:
            _period = max(0.01, float(period))
        if timeout is not None:
            _timeout = max(1.0, float(timeout))
        if failures_to_switch is not None:
            _failures_to_switch = max(1, int(failures_to_switch))
        if cooldown is not None:
            _cooldown = max(0.0, float(cooldown))
        if max_candidates is not None:
            _max_candidates = max(1, int(max_candidates))
        if ai_recheck is not None:
            _ai_recheck = max(60.0, float(ai_recheck))
        if ai_search is not None:
            _ai_search = max(60.0, float(ai_search))
        if reset:
            _failures = 0
            _last_error = ""
            _switches = 0
            _last_switch_at = 0.0
            _ai_node = ""
            _ai_checked_at = 0.0
            _ai_search_at = 0.0


# --------------------------------------------------------------------------- #
# 对外契约: state / set_enabled / start / stop
# --------------------------------------------------------------------------- #


def state() -> dict[str, Any]:
    """当前监控状态(GUI 轮询用)。只读、绝不抛异常。

    enabled = 用户开着自动切换(持久化); running = 后台线程真的活着。
    两者分开报是为了能看出"开关开着但线程死了"这种最坑的状态。

    如果持久化里记着"用户开过自动切换", 这里会**顺手把线程拉起来**: 客户端
    重启后界面上的开关不能变成一个骗人的"开着但没人监控" —— 那是用户最不
    容易发现、后果最严重的一类故障。反过来, 用户关掉之后 enabled 就是
    False, 不会再被拉起来。
    """
    try:
        _ensure_loaded()
    except Exception:  # noqa: PERF203
        pass
    try:
        _resume_if_enabled()
    except Exception:  # noqa: PERF203
        pass
    with _lock:
        alive = _thread is not None and _thread.is_alive()
        return {
            "enabled": bool(_enabled),
            "running": bool(alive),
            "last_check": float(_last_check),
            "switches": int(_switches),
            "last_error": str(_last_error),
            "note": str(_note),
        }


def _resume_if_enabled() -> None:
    with _lock:
        want = _enabled
        alive = _thread is not None and _thread.is_alive()
    if want and not alive:
        start()


def set_enabled(on: bool) -> None:
    """开/关自动切换。关闭时真的把后台线程停掉, 不留残余线程。"""
    if on:
        start()
    else:
        stop()


def start() -> None:
    """启动后台健康线程(幂等: 已经在跑就什么都不做)。

    调用即视为"用户要开自动切换", 所以顺手把 enabled 标记与持久化一起更新
    —— 这样 `control.set_auto_failover(True)` 里 set_enabled + start 的
    两连调用和单独调 start() 的行为完全一致。
    """
    global _thread, _note, _stop
    try:
        _ensure_loaded()
    except Exception:  # noqa: PERF203
        pass
    with _lock:
        _set_enabled_flag(True)
        if _thread is not None and _thread.is_alive() and not _stop.is_set():
            return  # 已经在跑: 幂等返回, 绝不起第二个线程
        # 每次启动用**新的** Event: 正在收尾的旧线程拿着自己那个已经 set 的
        # event, 一定会退出, 不会被新线程误"复活"。
        ev = threading.Event()
        _stop = ev
        _note = "自动切换已开启, 等待第一轮检查"
        t = threading.Thread(target=_loop, args=(ev,), name="accesspilot-health",
                             daemon=True)
        _thread = t
    t.start()


def stop() -> None:
    """停止后台线程(幂等)。只发信号 + 短暂 join, 不阻塞界面。"""
    global _thread, _note
    with _lock:
        _set_enabled_flag(False)
        t = _thread
        _stop.set()
        _note = "自动切换已关闭"
    if t is None or not t.is_alive() or t is threading.current_thread():
        return
    # 线程可能正在做一轮平台级验证(最坏几十秒)。只等 2 秒: 界面不能卡,
    # 剩下的部分由它自己跑完 —— 它是 daemon, 不会拖住进程退出。
    t.join(timeout=2.0)
    with _lock:
        if _thread is not None and not _thread.is_alive():
            _thread = None


# --------------------------------------------------------------------------- #
# 后台线程
# --------------------------------------------------------------------------- #


def _loop(stop_ev: threading.Event) -> None:
    """后台线程主体: 一轮接一轮, **绝不因为异常退出**。

    这里刻意用 BaseException 兜底: 后台线程静默死掉以后, 界面上"自动切换"
    还显示开着, 但再也不会有人救节点 —— 这是最难排查的一类故障。
    """
    while not stop_ev.is_set():
        started = time.time()
        try:
            failover_once(timeout=_current("timeout"))
        except BaseException as e:  # noqa: BLE001
            _finish(note=f"本轮检查异常({type(e).__name__}), 已跳过并继续下一轮",
                    error=_err_text(e))
        # 周期从"本轮开始"算起: 一轮本身可能因为验证慢花掉十几秒, 不应该
        # 再额外等满一个周期(那会让自动恢复变慢)。
        stop_ev.wait(max(0.0, _current("period") - (time.time() - started)))


def _current(name: str) -> float:
    """读一个可调参数(加锁, 免得读到 configure() 改了一半的值)。"""
    with _lock:
        return {
            "period": _period,
            "timeout": _timeout,
            "cooldown": _cooldown,
            "max_candidates": _max_candidates,
            "failures_to_switch": _failures_to_switch,
        }[name]


# --------------------------------------------------------------------------- #
# 一轮检查
# --------------------------------------------------------------------------- #


def failover_once(*, timeout: float | None = None) -> dict[str, Any]:
    """跑一轮健康检查(需要时会切换节点), 返回本轮结果。

    结果里几个关键字段: ok(当前出口最终是否可用)、switched(本轮是否真的切了)、
    node/new_node、failures(连续失败次数)、switches(累计切换次数)、reason。

    这个函数**不抛异常**: 后台线程、CLI、GUI 都可能直接调它。同一时刻只允许
    一轮, 已经在跑时立刻返回 busy=True, 不排队(排队会把界面卡住几十秒)。
    """
    if not _round_lock.acquire(blocking=False):
        return {
            "checked_at": time.time(), "busy": True, "core": False, "node": "",
            "ok": False, "switched": False, "new_node": "", "ai_node": "",
            "failures": _count("failures"), "switches": _count("switches"),
            "reason": "已有一轮检查在进行中", "error": "",
        }
    try:
        return _run_round(_timeout_arg(timeout))
    except BaseException as e:  # noqa: BLE001
        err = _err_text(e)
        _finish(note=f"检查异常, 已跳过: {err}", error=err)
        return {
            "checked_at": time.time(), "busy": False, "core": False, "node": "",
            "ok": False, "switched": False, "new_node": "", "ai_node": "",
            "failures": _count("failures"), "switches": _count("switches"),
            "reason": "检查异常", "error": err,
        }
    finally:
        _round_lock.release()


def _timeout_arg(timeout: float | None) -> float:
    if timeout is None:
        return _current("timeout")
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return _current("timeout")
    return value if value > 0 else _current("timeout")


def _run_round(timeout: float) -> dict[str, Any]:
    out: dict[str, Any] = {
        "checked_at": time.time(), "busy": False, "core": False, "node": "",
        "ok": False, "switched": False, "new_node": "", "ai_node": "",
        "failures": 0, "switches": 0, "reason": "", "error": "",
    }
    try:
        st = load_state()

        # 1) 内核还在不在 —— 最便宜的一步, 先做。
        try:
            core = bool(process.is_running())
        except Exception as e:  # noqa: PERF203
            _finish(note="无法确认内核状态, 本轮跳过", error=_err_text(e))
            out["reason"] = "无法确认内核状态"
            return out
        out["core"] = core
        if not core:
            # 内核没起来时切节点毫无意义(选择会随内核重启丢失), 而且拉起内核
            # 是 process/control 的职责 —— 两个模块同时操作内核只会互相打架。
            _finish(note="内核未运行, 本轮跳过节点检查")
            out["reason"] = "内核未运行"
            return out

        # 2) 当前出口节点是谁(沿组链解析到真正的节点)。
        node = _current_node(st)
        out["node"] = node
        if not node:
            # 读不到(控制接口忙 / 配置正在热重载 / 用户选了直连): 这是"未知",
            # 不是"死"。记失败会让一次热重载直接触发切换, 那是错的。
            _finish(note="读不到当前出口节点(直连或控制接口忙), 本轮跳过(不记失败)")
            out["reason"] = "读不到当前节点"
            return out

        # 3) 平台级验证 —— 这一层唯一可信的判据。
        try:
            probe = freenodes.verify_node(st, node, timeout=timeout)
        except Exception as e:  # noqa: PERF203
            # 验证本身炸了(控制接口繁忙等): 只记错误, **不算节点失败**。
            _finish(note=f"验证 {node} 时出错({type(e).__name__}), 本轮跳过(不记失败)",
                    error=_err_text(e))
            _settle_ai(st, timeout=timeout)   # 别把 verify_node 动过的组留在半路
            out["reason"] = "验证异常"
            out["error"] = _err_text(e)
            return out
        out["probe"] = probe

        if freenodes.fully_usable(probe):
            _reset_failures()
            out["ok"] = True
            chatgpt_ok = freenodes.chatgpt_usable(probe)
            ai, why = _settle_ai(
                st, timeout=timeout, general=node, general_ai_ok=chatgpt_ok,
                probed={node: probe},
                # 只有"出口上不了 ChatGPT"时才值得去扫候选(香港出口的典型
                # 场景); 出口本身能上就别再烧请求了。
                allow_scan=not chatgpt_ok,
            )
            out["ai_node"] = ai
            if chatgpt_ok:
                loc = str(probe.get("chatgpt_loc") or "")
                note = (f"{node} 正常: X/Discord 通过, ChatGPT 可用"
                        f"(出口 {loc or '?'})")
                if ai and ai != node:
                    note += f"; AI 组 -> {ai}"
            else:
                # 能上 X/Discord 但上不了 ChatGPT(典型: 香港出口 403)。这时
                # verify_node 的副作用已经把 AI 组切到它身上了, 必须拨回去,
                # 否则"检查"本身就会把用户的 ChatGPT 弄坏 —— 用户截图里的
                # "Unable to load site" 就是这么来的。
                note = (f"{node} 正常(X/Discord), 但 ChatGPT 不可用: "
                        f"{freenodes.chatgpt_reason(probe)}; "
                        f"AI 组 -> {ai or '跟随 🚀 节点选择'}")
                if not ai:
                    note += f" ({why})"
            out["reason"] = note
            _finish(note=note, error="")   # 恢复正常: 清掉历史错误
            return out

        return _round_failed(st, out, node=node, probe=probe, timeout=timeout)
    finally:
        # 计数同步进结果: 界面和测试都从这里读。
        out["failures"] = _count("failures")
        out["switches"] = _count("switches")


def _round_failed(
    st: AppState,
    out: dict[str, Any],
    *,
    node: str,
    probe: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """当前节点没通过平台级验证: 先累计, 够阈值 + 不在冷却期才真的切。"""
    fails = _bump_failures()
    why = _explain(probe)
    out["reason"] = why
    threshold = int(_current("failures_to_switch") or DEFAULT_FAILURES)
    # 这个节点刚被证明不可用: 别再把 AI 组钉在它身上(记忆里可能正是它)。
    _forget_ai_if(node)

    if fails < threshold:
        # 这就是"抖动保护": 单次失败绝不动手, 网络抖一下太正常了。
        _settle_ai(st, timeout=timeout, probed={node: probe})
        _finish(note=f"{node} 第 {fails}/{threshold} 次检查不通过({why}), "
                     f"继续观察(单次失败不切换)")
        return out

    left = _cooldown_left()
    if left > 0:
        _settle_ai(st, timeout=timeout, probed={node: probe})
        _finish(note=f"连续 {fails} 次不通过, 但距上次切换只有 "
                     f"{_current('cooldown') - left:.0f}s"
                     f"(冷却 {_current('cooldown'):.0f}s), 暂不切换")
        out["reason"] = f"{why}; 冷却中"
        return out

    general, ai, probed = _pick_replacement(st, timeout=timeout, exclude={node})
    target = general or ai
    if not target:
        # 一个替代节点都没找到: 保持原样, 但把 verify_node 动过的 AI/社交组
        # 摆回确定状态(否则一次失败的检查反而会把用户从能用的 AI 节点上挪走)。
        _settle_ai(st, timeout=timeout, probed=probed)
        note = (f"连续 {fails} 次不通过({why}), 但没找到实测可用的替代节点, "
                f"保持原样")
        out["reason"] = f"{why}; 无可用替代节点"
        _finish(note=note)
        return out

    if not _select(st, rules.G_SELECT, target):
        _settle_ai(st, timeout=timeout, probed=probed)
        note = f"连续 {fails} 次不通过, 但切换 {target} 失败(控制接口无响应)"
        out["reason"] = "切换失败"
        _finish(note=note)
        return out

    ai_pin, ai_why = _settle_ai(
        st, timeout=timeout, general=target,
        general_ai_ok=freenodes.chatgpt_usable(probed.get(target) or {}),
        probed=probed,
        allow_scan=False,   # 本轮已经扫过候选, 不再重复烧请求
    )
    count = _note_switch()
    out.update(ok=True, switched=True, new_node=target, ai_node=ai_pin,
               reason=why)
    # note 里保留"连续 N 次失败"这个因: 界面(和用户)需要知道为什么被换掉。
    note = (f"已自动切换(第 {count} 次, 原节点连续 {fails} 次失败): "
            f"{node} -> {target}; AI 组 -> {ai_pin or '跟随 🚀 节点选择'}")
    if not ai_pin:
        note += f" ({ai_why})"
    _finish(note=note)
    return out


# --------------------------------------------------------------------------- #
# 节点发现与选择
# --------------------------------------------------------------------------- #


def _group_now(st: AppState, group: str) -> str:
    """读某个策略组当前选中的条目(可能是节点, 也可能是另一个组)。"""
    try:
        return str((api.proxy(st, group) or {}).get("now") or "")
    except Exception:  # noqa: PERF203
        return ""


def _current_node(st: AppState) -> str:
    """沿策略组链解析出**真正的节点名**(🚀 节点选择 -> ♻️ 自动选择 -> 节点)。

    与 control._resolve_current 同一个思路, 但刻意不复用: health 要能在没有
    界面层的环境里独立测试, 而那边是给 GUI 渲染用的(还带快照缓存)。
    """
    name = rules.G_SELECT
    for _ in range(_RESOLVE_DEPTH):
        try:
            info = api.proxy(st, name) or {}
        except Exception:  # noqa: PERF203
            return ""
        nxt = str(info.get("now") or "")
        if not nxt:
            return ""
        if nxt in _GROUP_NAMES:
            name = nxt
            continue
        return nxt
    return ""


def _select(st: AppState, group: str, name: str, *, quiet: bool = False) -> bool:
    """切一个策略组。失败只记错误、返回 False —— 后台线程不许因此死掉。"""
    if not name:
        return False
    try:
        api.select(st, group, name)
        return True
    except Exception as e:  # noqa: PERF203
        if not quiet:
            _record_error(e)
        return False


def _make_social_follow(st: AppState) -> None:
    """让 💬 社交平台 跟随 🚀 节点选择(cli free auto 的同一套链条)。

    verify_node() 每测一个节点都会把社交组切到那个节点(它必须这么做才能测出
    X 到底通不通), 所以一轮结束后社交组很可能指着**刚测死的**节点。这里统一
    拨回"跟随顶层组", 但已经是跟随状态时不发多余的 PUT。
    """
    if _group_now(st, rules.G_SOCIAL) != rules.G_SELECT:
        _select(st, rules.G_SOCIAL, rules.G_SELECT, quiet=True)


def _settle_ai(
    st: AppState,
    *,
    timeout: float,
    general: str = "",
    general_ai_ok: bool = False,
    probed: dict[str, dict[str, Any]] | None = None,
    allow_scan: bool = False,
) -> tuple[str, str]:
    """一轮结束时把 🤖 AI 服务 / 💬 社交平台 摆回确定状态。

    为什么不是"恢复成本轮开始前的值": verify_node 每测一个节点都会把这两个组
    切到被测节点, 所以上一轮留下的"原值"本身就是它上次切过的结果 —— 恢复它
    等于把用户钉在一个刚被证明不可用的节点上。统一按不变量收尾更可靠:

        💬 社交平台 -> 跟随 🚀 节点选择
        🤖 AI 服务 -> 实测能上 ChatGPT 的节点; 找不到就跟随 🚀 节点选择

    返回 (AI 节点名, 说明); AI 组只能跟随顶层组时节点名返回空串。
    """
    ai, why = _ensure_ai_pin(st, timeout=timeout, general=general,
                             general_ai_ok=general_ai_ok, probed=probed,
                             allow_scan=allow_scan)
    # 社交组的收尾必须放在最后 —— _ensure_ai_pin 可能又实测了节点, 每次
    # 实测都会把社交组切走。
    _make_social_follow(st)
    return ai, why


def _candidates(st: AppState, *, limit: int, exclude: set[str]) -> list[str]:
    """按延迟顺序取候选节点名(延迟只用来**排序**, 不用来判定可用)。

    借内核 url-test 的历史延迟是为了把平台级验证先花在最可能可用的节点上;
    判定本身一律交给 verify_node —— 延迟 30ms 但打不开页面的节点实测存在。
    """
    from . import control  # 延迟导入: 避免 health <-> control 互相 import

    want = limit + len(exclude)
    try:
        nodes = control.list_nodes(limit=want, alive_only=True)
        if not nodes:
            # url-test 还没跑过(刚热重载完 history 是空的)时不要就此放弃:
            # 退回全量, 顺序差一点不影响正确性(每个都要过平台级验证)。
            nodes = control.list_nodes(limit=want, alive_only=False)
    except Exception as e:  # noqa: PERF203
        _record_error(e)
        return []

    out: list[str] = []
    for n in nodes:
        name = str(getattr(n, "name", "") or "")
        if name and name not in exclude and name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def _pick_replacement(
    st: AppState, *, timeout: float, exclude: set[str]
) -> tuple[str, str, dict[str, dict[str, Any]]]:
    """实测候选节点, 返回 (通用出口节点, ChatGPT 出口节点, 本轮已测结果)。

    **必须串行**: verify_node 要先把策略组切到自己, 并发会让请求跑到别的
    节点上, 结果全成了假的(cli 的 free auto 里踩过这个坑)。
    """
    general = ""
    ai = ""
    probed: dict[str, dict[str, Any]] = {}
    for name in _candidates(st, limit=int(_current("max_candidates") or 1),
                            exclude=exclude):
        try:
            r = freenodes.verify_node(st, name, timeout=timeout)
        except Exception as e:  # noqa: PERF203
            _record_error(e)
            continue
        probed[name] = r
        if not general and freenodes.fully_usable(r):
            general = name
        if not ai and freenodes.chatgpt_usable(r):
            ai = name
        if general and ai:
            break
    return general, ai, probed


def _ensure_ai_pin(
    st: AppState,
    *,
    timeout: float,
    general: str,
    general_ai_ok: bool,
    probed: dict[str, dict[str, Any]] | None = None,
    allow_scan: bool = True,
) -> tuple[str, str]:
    """保证 🤖 AI 服务 指向**实测**能打开 ChatGPT 的节点; 返回 (节点, 说明)。

    为什么 AI 组不能跟着 🚀/url-test 走: 出口地区不满足时 OpenAI 直接 403
    ("Unable to load site"), 而 url-test 只看延迟、还把 403 当成功。所以 AI
    组的出口必须单独验证、单独钉住。

    顺序即优先级, 前面几步都是"免费"的, 只有第 4 步会真的发请求:
      1. 本轮已实测且 ChatGPT 可用的节点(含当前出口) —— 0 个额外请求;
      2. 记忆里那个节点还新鲜(默认 15 分钟内验证过) —— 1 条 PUT, 不重测;
      3. 记忆里的节点过期了 —— 单独重测它一次(4 个请求, 摊到 15 分钟很便宜);
      4. 限频(默认 10 分钟一次)扫候选节点;
      5. 都不行 -> 退回跟随 🚀 节点选择, 并把原因说清楚(绝不假装成功)。
    """
    probed = dict(probed or {})
    with _lock:
        remembered = _ai_node
        remembered_at = _ai_checked_at
        scan_due = time.time() - _ai_search_at >= _ai_search

    if general and general_ai_ok:
        if _select(st, rules.G_AI, general):
            _remember_ai(general)
            return general, "当前出口本身就能上 ChatGPT"

    for name, r in probed.items():
        if freenodes.chatgpt_usable(r) and _select(st, rules.G_AI, name):
            _remember_ai(name)
            return name, f"{name} 本轮实测可用({freenodes.chatgpt_reason(r)})"

    if remembered and remembered not in probed:
        if time.time() - remembered_at < _ai_recheck:
            if _select(st, rules.G_AI, remembered, quiet=True):
                return remembered, f"{remembered} 上次实测可用(未过期)"
        else:
            r = _probe(st, remembered, timeout=timeout)
            probed[remembered] = r
            if freenodes.chatgpt_usable(r) and _select(st, rules.G_AI, remembered):
                _remember_ai(remembered)
                return remembered, f"{remembered} 复测可用({freenodes.chatgpt_reason(r)})"
            # 过期且复测不通过: 必须忘掉它, 否则每一轮都会再白测一次(4 个请求)。
            _forget_ai_if(remembered)

    if allow_scan and scan_due:
        _mark_ai_search()
        names: list[str] = []
        names += [n for n in _candidates(
            st, limit=int(_current("max_candidates") or 1),
            exclude=set(probed) | {general},
        ) if n not in names]
        for name in names[: int(_current("max_candidates") or 1)]:
            r = _probe(st, name, timeout=timeout)
            probed[name] = r
            if freenodes.chatgpt_usable(r) and _select(st, rules.G_AI, name):
                _remember_ai(name)
                return name, f"{name} 实测可用({freenodes.chatgpt_reason(r)})"

    if _select(st, rules.G_AI, rules.G_SELECT, quiet=True):
        return "", "没找到实测能上 ChatGPT 的节点, AI 组暂时跟随 🚀 节点选择"
    return "", "没找到可用节点, 且无法改回跟随 🚀 节点选择"


def _probe(st: AppState, node: str, *, timeout: float) -> dict[str, Any]:
    """实测一个节点。异常时返回"未通过"的结果而不是抛出去。"""
    try:
        return freenodes.verify_node(st, node, timeout=timeout)
    except Exception as e:  # noqa: PERF203
        _record_error(e)
        return {"name": node, "x_ok": False, "x_asset_ok": False,
                "discord_ok": False, "chatgpt_ok": False, "latency_ms": -1,
                "detail": _err_text(e)}


def _explain(probe: dict[str, Any]) -> str:
    """把验证结果翻成一句人话(进 note, 界面直接显示)。"""
    bad: list[str] = []
    if not probe.get("x_ok"):
        bad.append("X 主页")
    if not probe.get("x_asset_ok"):
        bad.append("X 静态资源")
    if not probe.get("discord_ok"):
        bad.append("Discord")
    detail = str(probe.get("detail") or "")
    why = ("打不开: " + "/".join(bad)) if bad else "平台级验证未通过"
    return f"{why}({detail})" if detail else why


# --------------------------------------------------------------------------- #
# 计数/错误记录(全部加锁)
# --------------------------------------------------------------------------- #


def _count(name: str) -> int:
    with _lock:
        return int({"failures": _failures, "switches": _switches}[name])


def _bump_failures() -> int:
    global _failures
    with _lock:
        _failures += 1
        return _failures


def _reset_failures() -> None:
    global _failures
    with _lock:
        _failures = 0


def _note_switch() -> int:
    global _switches, _last_switch_at, _failures
    with _lock:
        _switches += 1
        _last_switch_at = time.time()
        _failures = 0
        return _switches


def _cooldown_left() -> float:
    with _lock:
        return max(0.0, _cooldown - (time.time() - _last_switch_at))


def _remember_ai(node: str) -> None:
    global _ai_node, _ai_checked_at
    with _lock:
        _ai_node = str(node)
        _ai_checked_at = time.time()


def _forget_ai_if(node: str) -> None:
    """当前出口刚被证明不可用时, 别再把 AI 组钉回同一个节点。

    否则会出现"记忆里那个节点还没过期 -> 每轮都把 AI 组钉到一个已经死掉的
    节点上", 看起来像自动切换失灵。
    """
    global _ai_node, _ai_checked_at
    with _lock:
        if _ai_node and _ai_node == node:
            _ai_node = ""
            _ai_checked_at = 0.0


def _mark_ai_search() -> None:
    global _ai_search_at
    with _lock:
        _ai_search_at = time.time()


def _finish(*, note: str, error: str | None = None) -> None:
    """一轮结束: 更新 last_check / note / last_error(GUI 全靠这几个字段)。

    `error=None` 表示"不动 last_error": 这样轮内 `_select` 失败记下的错误
    不会被收尾时无声抹掉(界面要能看到"切不过去"的真正原因)。只有一轮
    **彻底正常**时才显式传 error="" 把它清空。
    """
    global _last_check, _note, _last_error
    with _lock:
        _last_check = time.time()
        _note = str(note)
        if error is not None:
            _last_error = str(error)


def _record_error(exc: BaseException | str) -> None:
    global _last_error
    with _lock:
        _last_error = _err_text(exc)


def _err_text(exc: BaseException | str) -> str:
    if isinstance(exc, str):
        return exc
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# --------------------------------------------------------------------------- #
# 单实例: Windows 命名互斥体
# --------------------------------------------------------------------------- #
#
# 为什么用互斥体而不是文件锁: 进程崩溃/被任务管理器结束后, 文件锁会留下一个
# "锁文件", 下次启动会被自己挡住(用户只能手动删文件)。命名互斥体由内核持有,
# 进程一退出(哪怕崩溃)内核自动释放, 不留任何垃圾。

ERROR_ALREADY_EXISTS = 183

#: Local\ = 当前登录会话。桌面客户端是"一个会话一个窗口", 用 Global\ 反而会
#: 让不同用户的会话互相挡住, 而且创建 Global 对象还需要额外权限。
_MUTEX_PREFIX = "Local\\AccessPilot."

#: 找"已有窗口"时按优先级尝试的窗口类名。Tk 客户端可以传
#: className="AccessPilotGui", 这样即使标题被改过也认得出来。
WINDOW_CLASS_HINTS: tuple[str, ...] = ("AccessPilotGui", "AccessPilot", "HongxingGui")

#: 兜底: 标题里带这些字样的可见窗口(客户端标题形如 "红杏 ...")。
WINDOW_TITLE_HINTS: tuple[str, ...] = ("红杏", "AccessPilot")

_mutex_handle: int | None = None
_kernel32: Any = None
_user32: Any = None


def _kernel32_dll() -> Any:
    """加载 kernel32 并声明用到的函数原型(只做一次)。

    必须显式声明 restype: HANDLE 在 64 位下是 8 字节, 而 ctypes 默认按
    C int(4 字节)返回 —— 句柄会被截断, 后面 CloseHandle 必然失败, 于是
    互斥体永远不释放(下次启动会被一个"幽灵实例"挡住)。
    """
    global _kernel32
    if _kernel32 is None:
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL,
                                     wintypes.LPCWSTR)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        _kernel32 = k32
    return _kernel32


def _user32_dll() -> Any:
    """加载 user32(窗口相关函数在这里, **不在 kernel32**)。"""
    global _user32
    if _user32 is None:
        from ctypes import wintypes

        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        u32.FindWindowW.restype = wintypes.HWND
        u32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        u32.SetForegroundWindow.restype = wintypes.BOOL
        u32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        u32.ShowWindow.restype = wintypes.BOOL
        u32.IsWindowVisible.argtypes = (wintypes.HWND,)
        u32.IsWindowVisible.restype = wintypes.BOOL
        u32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        u32.GetWindowTextLengthW.restype = ctypes.c_int
        u32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR,
                                       ctypes.c_int)
        u32.GetWindowTextW.restype = ctypes.c_int
        _user32 = u32
    return _user32


def acquire_single_instance(name: str = "hongxing", *, activate: bool = True) -> bool:
    """抢占单实例名额。返回 False = **已经有红杏在跑**。

    拿到 False 时本函数已经尽力把已有窗口叫到前台了, 调用方直接退出即可。
    同一个进程里第二次调用同样返回 False(内核那边的互斥体确实已被占用),
    这时应当由调用方忽略, 而不是退出 —— 正常流程只会调一次。

    非 Windows 平台直接放行: 这套实现依赖 Win32 命名互斥体, 没有它宁可不管,
    也不要退化成"崩溃后留垃圾"的文件锁。
    """
    global _mutex_handle
    if sys.platform != "win32":
        return True
    try:
        k32 = _kernel32_dll()
        handle = k32.CreateMutexW(None, False, f"{_MUTEX_PREFIX}{name}")
        err = ctypes.get_last_error()
    except Exception:  # noqa: PERF203
        return True  # Win32 调用本身失败(权限/内存): 绝不能因此不让人用软件
    if not handle:
        return True
    if err == ERROR_ALREADY_EXISTS:
        try:
            k32.CloseHandle(handle)  # 我们没拿到所有权, 关掉自己这份句柄
        except Exception:  # noqa: PERF203
            pass
        if activate:
            _activate_existing_window()
        return False
    _mutex_handle = int(handle)
    return True


def release_single_instance() -> None:
    """释放单实例名额(进程退出前调用; 不调也会被内核自动回收)。"""
    global _mutex_handle
    handle, _mutex_handle = _mutex_handle, None
    if not handle or sys.platform != "win32":
        return
    try:
        _kernel32_dll().CloseHandle(handle)
    except Exception:  # noqa: PERF203
        pass


def _find_existing_window() -> int:
    """找客户端主窗口句柄(找不到返回 0)。

    两步: 先用 FindWindowW 按类名/精确标题找(快); 找不到再枚举顶层窗口按
    标题关键字模糊匹配 —— 标题常带版本号/后缀, 精确匹配容易落空。
    """
    if sys.platform != "win32":
        return 0
    try:
        u32 = _user32_dll()
        for cls in WINDOW_CLASS_HINTS:
            hwnd = u32.FindWindowW(cls, None)
            if hwnd:
                return int(hwnd)
        for title in WINDOW_TITLE_HINTS:
            hwnd = u32.FindWindowW(None, title)
            if hwnd:
                return int(hwnd)
        return _find_window_by_title(u32)
    except Exception:  # noqa: PERF203
        return 0


def _find_window_by_title(u32: Any) -> int:
    """枚举可见顶层窗口, 按标题关键字匹配(标题不完全一致时也能找到)。"""
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                       ctypes.c_void_p)
    found: list[int] = []

    def visit(hwnd: Any, _lparam: Any) -> bool:
        if found:
            return False
        try:
            if not u32.IsWindowVisible(hwnd):
                return True
            length = int(u32.GetWindowTextLengthW(hwnd) or 0)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            u32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
        except Exception:  # noqa: PERF203
            return True
        if any(hint in title for hint in WINDOW_TITLE_HINTS):
            found.append(int(hwnd))
            return False
        return True

    try:
        u32.EnumWindows.argtypes = (callback_type, ctypes.c_void_p)
        u32.EnumWindows.restype = ctypes.c_bool
        u32.EnumWindows(callback_type(visit), None)
    except Exception:  # noqa: PERF203
        return 0
    return found[0] if found else 0


def _activate_existing_window() -> bool:
    """把已有客户端窗口叫到前台。失败只返回 False, 绝不抛异常。

    注意 Windows 的前台锁定: 调用方不是前台进程时 SetForegroundWindow 可能被
    系统忽略(只在任务栏闪一下)。所以先 ShowWindow(SW_RESTORE) 把最小化的窗口
    还原, 再 SetForegroundWindow —— 这是成功率高、又不需要 hack 的组合。
    """
    hwnd = _find_existing_window()
    if not hwnd:
        return False
    try:
        u32 = _user32_dll()
        SW_RESTORE = 9
        u32.ShowWindow(hwnd, SW_RESTORE)
        return bool(u32.SetForegroundWindow(hwnd))
    except Exception:  # noqa: PERF203
        return False
