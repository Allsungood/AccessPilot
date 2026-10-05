"""链路自愈 —— 让「已连接」在下一秒依然成立。

## 为什么需要这一层

原来的三处保护, 判据全都是同一个问题: **内核进程还活着吗?**

    cli.cmd_ensure          内核在跑 -> 直接 return 0     (计划任务, 每 5 分钟一次)
    cli.cmd_watchdog        内核在跑 -> 什么都不做        (常驻看门狗)
    process.heal_if_broken  内核在跑 -> 立刻 return False

而实测到的真实掉线形态恰恰是反过来的 —— 内核活得好好的, 是 **Windows 的
ProxyEnable 被别人改回了 0**:

    state.json  system_proxy_on = true   -> 状态/界面显示"已连接"
    注册表      ProxyEnable = 0          -> 实际全部直连, 代理形同不存在

实测数据(probe-proxy-revert.py): 手动把 ProxyEnable 置 1 之后 **19.28 秒**就被
改回 0, 而 ProxyServer / AutoConfigURL 都没被动过 —— 有程序在专门关这个开关。
360 主动防御、蓝灯、FastGithub、浏览器「重置代理设置」都观察到过同类行为。

三处保护因为"内核还在跑"而全部提前返回, 没有任何一处会发现这件事。用户看到的
现象就是"红杏又断了", 而且重开一下就好、过一会儿又断 —— 因为它从来没被真正
修好过, 只是被用户手动重启了一次。

## 判据

这一层把判据换成**端到端**的两问:

1. 注册表里的代理设置, 此刻是不是我们要的那一个? (`sysproxy.effective`)
2. 通过它发一个真实请求, 通不通? (`probe_traffic`, 只在深检查时做)

两问都过才算"已连接"; 任何一问不过就当场修, 并记进历史 —— 用户因此能看到
"是别的程序在关我的代理", 而不是以为软件本身坏了。

## 成本

浅检查 = 读一个注册表键 + 判一次 pid 存活, 微秒级, 10 秒一次毫无压力。
深检查要发一个真实 HTTPS 请求, 所以默认 60 秒一次, 且只在需要时才升级过去。
"""
from __future__ import annotations

import time
from typing import Any

from . import paths, process, sysproxy
from .state import AppState, load_state, save_state
from .util import http_request, info, json_dump, json_load, warn

#: 深检查的目标。选它是因为: 直连通常打不开(gstatic 在国内被污染), 走通隧道
#: 才会返回干净的 204 —— 所以它能区分"代理端口开着"和"代理真的能把流量送出去"。
TRAFFIC_TARGET = "https://www.gstatic.com/generate_204"

#: 历史文件。只留最近这么多条, 免得无限增长。
HISTORY_FILE = "guard-history.json"
HISTORY_MAX = 200

#: 一小时内自动重贴系统代理超过这个次数, 就认定"有程序在跟我们抢"
FIGHT_THRESHOLD = 3


# --------------------------------------------------------------------------- #
# 历史
# --------------------------------------------------------------------------- #


def _history_path():
    return paths.cache_dir() / HISTORY_FILE


def history(limit: int = 50) -> list[dict[str, Any]]:
    data = json_load(_history_path(), None)
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)][-limit:]


def record(kind: str, detail: str) -> dict[str, Any]:
    """记一次修复。这是给用户看的证据链, 不是日志噪音。"""
    entry = {"t": time.time(), "kind": kind, "detail": detail}
    items = history(limit=HISTORY_MAX)
    items.append(entry)
    try:
        paths.cache_dir().mkdir(parents=True, exist_ok=True)
        json_dump(_history_path(), items[-HISTORY_MAX:])
    except Exception:  # noqa: BLE001
        pass
    return entry


def flap_count(*, window: float = 3600.0, kind: str = "sysproxy_reapply") -> int:
    """最近 window 秒内, 某类修复发生了几次。"""
    cutoff = time.time() - window
    return sum(1 for e in history(limit=HISTORY_MAX)
               if e.get("kind") == kind and float(e.get("t") or 0) >= cutoff)


def fighting(*, window: float = 3600.0) -> bool:
    """是不是有别的程序在反复关掉我们的系统代理。"""
    return flap_count(window=window) >= FIGHT_THRESHOLD


#: fighting() 的短缓存。看门狗最快 2 秒一轮, 每轮都读一次历史文件没必要。
_fight_cache: dict[str, float | bool] = {"at": 0.0, "val": False}


def fighting_cached(*, ttl: float = 60.0) -> bool:
    now = time.time()
    if now - float(_fight_cache["at"]) > ttl:
        _fight_cache["at"] = now
        _fight_cache["val"] = fighting()
    return bool(_fight_cache["val"])


def recommended_interval(default: float = 10.0, under_fire: float = 2.0) -> float:
    """检查间隔。**曝光窗口 = 检查间隔** —— 有人在跟我们抢开关时压到最短。

    实测外部程序会在 19~40 秒内把 ProxyEnable 改回 0, 所以 10 秒一轮意味着
    每轮最多有 10 秒是断的; 压到 2 秒就只剩 2 秒。读一个注册表键而已, 2 秒
    一次的开销可以忽略。
    """
    return under_fire if fighting_cached() else default


# --------------------------------------------------------------------------- #
# 流量探测
# --------------------------------------------------------------------------- #


def probe_traffic(st: AppState, *, timeout: float = 6.0) -> tuple[bool, str]:
    """发一个真实请求, 确认流量真的被送出去了。

    TUN 模式在网络层接管, 直连请求自然走隧道, 所以不传 proxy;
    系统代理模式则**显式**指定 mihomo 的混合端口 —— 不依赖 urllib 自己去读
    Windows 注册表, 那样判据会和我们正在验证的东西绕成循环。
    """
    proxy = None if st.tun_enable else f"http://127.0.0.1:{st.mixed_port}"
    t0 = time.time()
    try:
        status, _, _ = http_request(
            TRAFFIC_TARGET,
            headers={"User-Agent": "AccessPilot-guard"},
            timeout=timeout,
            proxy=proxy,
        )
        ms = int((time.time() - t0) * 1000)
        if status in (200, 204):
            return True, f"{ms}ms"
        return False, f"HTTP {status}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:60]}"


# --------------------------------------------------------------------------- #
# 检查与修复
# --------------------------------------------------------------------------- #


def check(st: AppState | None = None, *, deep: bool = False,
          traffic_timeout: float = 6.0) -> dict[str, Any]:
    """体检。只读, 不改任何东西。"""
    st = st or load_state()
    res: dict[str, Any] = {
        "kernel": False,
        "mode": "tun" if st.tun_enable else "sysproxy",
        "configured": bool(st.tun_enable or st.system_proxy_on),
        "effective": False,
        "reason": "",
        "traffic": None,
        "traffic_detail": "",
        "healthy": False,
    }
    try:
        res["kernel"] = process.is_running()
    except Exception:  # noqa: BLE001
        res["kernel"] = False

    if not res["kernel"]:
        res["reason"] = "内核未运行"
        return res

    if st.tun_enable:
        # TUN 不看注册表 —— 它本来就不该占用系统代理设置
        res["effective"] = True
        res["reason"] = "TUN 模式"
    elif st.system_proxy_on:
        res["effective"], res["reason"] = sysproxy.effective(st)
    else:
        # 没打算开系统代理: 只要内核在跑就算符合预期
        res["effective"] = True
        res["reason"] = "未启用系统代理(符合预期)"

    if deep and res["effective"]:
        ok_t, why = probe_traffic(st, timeout=traffic_timeout)
        res["traffic"] = ok_t
        res["traffic_detail"] = why
        res["healthy"] = bool(ok_t)
    else:
        res["healthy"] = bool(res["effective"])

    return res


def port_serving(st: AppState) -> bool:
    """内核的混合端口此刻**真的**在监听吗。

    这是本项目那条铁律("绝不把系统代理指向死端口")的落地点。真实故障:
    `config set-port` 会把新端口写进 state.json, 而内核仍然在旧端口上服务 ——
    此时若无条件按 state 把系统代理改成新端口, 10 秒内所有浏览器都打不开任何
    网页, 而且事后检查还会显示"已生效", 没有任何东西会去纠正它。
    """
    try:
        return bool(process._listeners(st.mixed_port))  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return False


def repair(st: AppState | None = None, *, deep: bool = False, quiet: bool = True,
           traffic_timeout: float = 6.0, allow_restart: bool = True) -> dict[str, Any]:
    """体检并当场修好。返回最后一次的检查结果, 附 repairs 列表。

    allow_restart=False 时不重启内核。看门狗必须用这个模式: 它是由
    `process.start()` 拉起来的, 而 `process.start()` 内部会调用
    `start_watchdog()` -> `stop_watchdog()`, 那会**杀掉正在执行修复的看门狗自己**。
    """
    st = st or load_state()
    reps: list[str] = []
    res = check(st, deep=False)

    # 1) 内核不在 -> 按用户意图决定要不要拉起来。
    #    用户主动点过「关闭」时绝不擅自拉起来(那是 intent 记录的用途)。
    if not res["kernel"]:
        if not allow_restart:
            res["repairs"] = reps
            return res
        try:
            from . import intent

            wants_off = intent.user_wants_off()
        except Exception:  # noqa: BLE001
            wants_off = False
        if wants_off or not res["configured"]:
            res["repairs"] = reps
            return res
        try:
            process.start(st=st, tun=st.tun_enable, system_proxy=st.system_proxy_on)
            reps.append("重启内核")
            record("kernel_restart", "体检发现内核不在运行, 已重新拉起")
        except Exception as e:  # noqa: BLE001
            res["repairs"] = reps
            res["reason"] = f"重启内核失败: {e}"
            return res
        res = check(st, deep=False)

    # 2) 内核在跑但代理设置没生效 —— 这就是"红杏又断了"的真身。
    if res["kernel"] and not res["effective"] and st.system_proxy_on and not st.tun_enable:
        if not port_serving(st):
            # 端口没人监听。这时候**绝不能**把系统代理指过去 —— 那会让整台机器
            # 断网。正确动作是反过来的: 把系统代理摘掉, 退回直连。
            sysproxy.disable(st)
            st.system_proxy_on = False
            save_state(st)
            res["effective"] = False
            res["reason"] = f"内核端口 {st.mixed_port} 没有监听"
            reps.append("内核端口未监听, 已摘掉系统代理")
            record("sysproxy_detached_dead_port",
                   f"端口 {st.mixed_port} 无监听, 已关闭系统代理以免全机断网")
            if not quiet:
                warn(f"内核端口 {st.mixed_port} 没有在监听, 已摘掉系统代理(否则整机断网)")
        else:
            detail = sysproxy.enable(st)
            ok_now, why = sysproxy.effective(st)
            # 标记要跟着**校验结果**走, 不能跟着"我们调用过 enable"走 ——
            # 写进去但被外部改回来的情况实测 19 秒就会发生一次。
            st.system_proxy_on = bool(ok_now)
            save_state(st)
            res["effective"] = ok_now
            res["reason"] = why
            reps.append("重贴系统代理")
            record("sysproxy_reapply", f"{res['reason']} -> {detail}")
            if not quiet:
                warn(f"系统代理被外部改掉了({res['reason']}), 已自动恢复")

    # 3) 深检查: 设置对但流量不通 -> 内核可能是僵死的, 重启一次。
    if deep and res["effective"]:
        ok_t, why = probe_traffic(st, timeout=traffic_timeout)
        res["traffic"] = ok_t
        res["traffic_detail"] = why
        if not ok_t and res["kernel"] and allow_restart:
            try:
                process.restart(st=st, tun=st.tun_enable, system_proxy=st.system_proxy_on)
                reps.append("重启内核(流量不通)")
                record("kernel_restart_dead_traffic", f"流量探测失败: {why}")
                ok_t2, why2 = probe_traffic(st, timeout=traffic_timeout)
                res["traffic"] = ok_t2
                res["traffic_detail"] = why2
            except Exception as e:  # noqa: BLE001
                res["traffic_detail"] = f"{why}; 重启失败: {e}"
        res["healthy"] = bool(res["traffic"])
    else:
        res["healthy"] = bool(res["effective"])

    res["repairs"] = reps
    res["fighting"] = fighting()
    return res


# --------------------------------------------------------------------------- #
# 常驻循环(给看门狗用)
# --------------------------------------------------------------------------- #


def watch(*, interval: float = 10.0, deep_every: float = 60.0, rounds: int = 0,
          quiet: bool = True, on_event: Any = None) -> None:
    """常驻自愈循环。

    interval  : 浅检查间隔(读注册表, 极便宜)
    deep_every: 深检查间隔(发真实请求, 别太频繁)
    rounds    : 0 = 一直跑
    """
    count = 0
    last_deep = 0.0
    while True:
        count += 1
        now = time.time()
        deep = (now - last_deep) >= deep_every
        if deep:
            last_deep = now
        try:
            res = repair(deep=deep, quiet=quiet)
        except Exception as e:  # noqa: BLE001
            res = {"healthy": False, "reason": f"自愈循环异常: {e}", "repairs": []}
        if res.get("repairs"):
            if not quiet:
                info(f"[自愈] {'; '.join(res['repairs'])}")
            if on_event is not None:
                try:
                    on_event(res)
                except Exception:  # noqa: BLE001
                    pass
        if fighting() and not quiet:
            warn(
                f"检测到有外部程序在反复关闭系统代理(近 1 小时 "
                f"{flap_count()} 次)。建议: 在 360 等安全软件里放行本程序, "
                "或改用 `accesspilot tun on`(它在网络层接管, 不依赖这个开关)"
            )
        if rounds and count >= rounds:
            return
        time.sleep(interval)


def diagnose_text(st: AppState | None = None) -> str:
    """给人看的一行诊断。"""
    st = st or load_state()
    res = check(st, deep=False)
    if res["healthy"]:
        return "链路正常"
    return f"链路异常: {res['reason']}"
