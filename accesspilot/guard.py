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

#: 深检查的目标。**必须包含产品真正要服务的站点** —— 不能只测一个"哪儿都能通"的。
#:
#: 血的教训: 原来只测 `gstatic.com/generate_204`, 而**国内节点也能通过它**。
#: 于是自愈层一直报"健康", 而用户实际打不开 GitHub(TLS 握手 20 秒超时);
#: 同一个盲区还让一个国内节点凭延迟赢下 url-test、当上了通用出口。
#: 健康判据如果和用户的目标不一致, 它就只是在自我安慰。
TRAFFIC_TARGETS: tuple[tuple[str, str], ...] = (
    ("github", "https://github.com/robots.txt"),
    ("huggingface", "https://huggingface.co/robots.txt"),
    ("gstatic", "https://www.gstatic.com/generate_204"),
)

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
    """发真实请求, 确认流量真的被送出去了。返回 (至少一个通, 逐站详情)。

    TUN 模式在网络层接管, 直连请求自然走隧道, 所以不传 proxy;
    系统代理模式则**显式**指定 mihomo 的混合端口 —— 不依赖 urllib 自己去读
    Windows 注册表, 那样判据会和我们正在验证的东西绕成循环。

    判据是"至少一个目标通": 个别站点被墙或抖动不该触发重启内核。但详情里会
    带上每一站的结果 —— 只有 GitHub 不通, 那是**换节点**的问题(见
    repin_for_sites), 不是重启内核能解决的。
    """
    res = probe_targets(st, timeout=timeout)
    detail = " ".join(f"{k}={'ok' if v else 'x'}" for k, v in res.items())
    return any(res.values()), detail


def probe_targets(st: AppState, *, timeout: float = 6.0) -> dict[str, bool]:
    """逐个探测 TRAFFIC_TARGETS, 返回 {名字: 通不通}。

    分站点探测是有意的: "全都不通"是链路断了(该重启内核), 而"只有 GitHub 不通"
    是**出口节点选错了**(该换节点) —— 两种故障的修法完全不同, 混在一起就只能
    猜。原来只有一个目标, 连区分都做不到。
    """
    proxy = None if st.tun_enable else f"http://127.0.0.1:{st.mixed_port}"
    out: dict[str, bool] = {}
    for key, url in TRAFFIC_TARGETS:
        try:
            status, _, _ = http_request(
                url, headers={"User-Agent": "AccessPilot-guard"},
                timeout=timeout, proxy=proxy,
            )
            out[key] = status in (200, 204, 301, 302)
        except Exception:  # noqa: BLE001
            out[key] = False
    return out


def repin_for_sites(st: AppState | None = None, *, want: str = "github",
                    candidates: int = 12) -> str | None:
    """把通用出口换成一个**真能打开 GitHub** 的节点, 返回选中的节点名。

    为什么需要它: url-test 只按延迟挑, 而"延迟低"完全不保证"能打开被墙的站点";
    上游节点还会随时失效。所以当 github 探测失败、而链路本身是通的时候, 正确
    动作是换节点, 不是重启内核。

    探测用的是 mihomo 自己的 delay 接口, 并且**用 GitHub 的地址当目标** ——
    这才是"这个节点能不能打开 GitHub"的唯一直接答案。
    """
    from . import api, intent, rules

    st = st or load_state()
    try:
        from .subscription import load_profile

        names = [str(p.get("name")) for p in load_profile(st.active_profile).proxies]
    except Exception:  # noqa: BLE001
        return None

    # 候选顺序是这件事成败的关键。**先把已知能用的排前面**:
    # 别的策略组当前钉着的节点是"已经被选中用过"的, 大概率还活着。
    # 而只按 profile 顺序取的话, 拿到的是从没验证过的头部节点 —— 实测前 8 个
    # 全是死的 github.com/freefq, 于是"换出口"每次都失败(这正是第一版没生效
    # 的原因: 探测全返回 503/504, 挑不出任何节点)。
    known: list[str] = []
    try:
        for g in (rules.G_AI, rules.G_SOCIAL, rules.G_MEDIA, rules.G_SELECT):
            cur = str(api.proxy(st, g).get("now") or "")
            if cur and cur not in known and cur not in (rules.G_AUTO, rules.G_DIRECT):
                known.append(cur)
    except Exception:  # noqa: BLE001
        pass
    ordered = known + [n for n in names if n not in known]

    url = dict(TRAFFIC_TARGETS).get(want, "https://github.com/robots.txt")
    # 只从海外的候选里挑: 名字里带中国大陆标记的直接跳过(国内出口连不上 GitHub)
    bad = ("🇨🇳", "中国 #", "中国#")
    tried = 0
    for name in ordered:
        if tried >= candidates:
            break
        if any(b in name for b in bad) or name.rstrip().endswith("中国"):
            continue
        tried += 1
        try:
            ms = api.delay(st, name, url=url, timeout_ms=6000)
        except Exception:  # noqa: BLE001
            continue
        if ms <= 0:
            continue
        # 通用组和 AI 组钉同一个节点 —— 两个出口会让同一站点看到两个来源 IP,
        # 之前就因为这件事触发过 Google 的"异常流量"判定。
        for group in (rules.G_SELECT, rules.G_AI):
            try:
                api.select(st, group, name)
            except Exception:  # noqa: BLE001
                pass
        st.selected = dict(st.selected or {})
        st.selected[rules.G_SELECT] = name
        st.selected[rules.G_AI] = name
        save_state(st)
        record("repin_for_sites", f"{want} 不通 -> 换成 {name[:50]} ({ms}ms)")
        try:
            intent.mark_on()
        except Exception:  # noqa: BLE001
            pass
        return name
    record("repin_for_sites_failed",
           f"{want} 不通, 试了 {tried} 个候选节点都不行")
    return None


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


def domestic_pinned(st: AppState) -> str | None:
    """通用组是不是被**显式钉**在一个国内节点上? 是就返回那个节点名。

    这是排查到最后才发现的坑, 也是最隐蔽的一个:
    `exclude-filter` 只作用于 url-test 组, 它**管不到 state.json 里显式保存的
    选择**。一旦 `🚀 节点选择` 被钉在 `🇨🇳_CN_中国` 上, 排除规则就被整个绕过 ——
    出口是国内节点, GitHub 必然打不开, 而"自动选择"那边看起来一切正常
    (实测: G_AUTO 已经正确落在美国节点, G_SELECT 却还是国内节点)。
    """
    from . import rules

    name = str((st.selected or {}).get(rules.G_SELECT) or "")
    if not name:
        return None
    if "🇨🇳" in name or name.rstrip().endswith("中国"):
        return name
    return None


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

    # 2.5) 通用组被显式钉在国内节点上 -> 改回自动选择。
    #      这一条是浅检查(只读 state.json), 但要放在深检查之前 —— 否则
    #      "GitHub 不通"会被误判成节点质量问题, 而真正的原因是钉错了出口。
    if res["kernel"] and not st.tun_enable:
        bad_pin = domestic_pinned(st)
        if bad_pin:
            from . import api, rules

            try:
                api.select(st, rules.G_SELECT, rules.G_AUTO)
            except Exception:  # noqa: BLE001
                pass
            st.selected = dict(st.selected or {})
            st.selected.pop(rules.G_SELECT, None)
            save_state(st)
            reps.append(f"通用出口原钉在国内节点({bad_pin[:20]}), 已改回自动选择")
            record("unpin_domestic",
                   f"🚀 节点选择 原来固定在 {bad_pin[:50]} —— 国内出口连不上 GitHub")
            if not quiet:
                warn(f"通用出口原来固定在国内节点 {bad_pin[:30]}, 已改回自动选择")

    # 3) 深检查。这里要区分两种完全不同的故障:
    #      * 全都不通   -> 链路断了, 重启内核
    #      * 只有 GitHub 不通 -> 链路是好的, 是**出口节点选错了**, 换节点
    #    混在一起就只能猜, 而原来只有一个探测目标, 连区分都做不到。
    if deep and res["effective"]:
        sites = probe_targets(st, timeout=traffic_timeout)
        res["sites"] = sites
        res["traffic"] = any(sites.values())
        res["traffic_detail"] = " ".join(f"{k}={'ok' if v else 'x'}" for k, v in sites.items())
        if not res["traffic"] and res["kernel"] and allow_restart:
            try:
                process.restart(st=st, tun=st.tun_enable, system_proxy=st.system_proxy_on)
                reps.append("重启内核(流量不通)")
                record("kernel_restart_dead_traffic", f"流量探测失败: {res['traffic_detail']}")
                sites = probe_targets(st, timeout=traffic_timeout)
                res["sites"] = sites
                res["traffic"] = any(sites.values())
                res["traffic_detail"] = " ".join(
                    f"{k}={'ok' if v else 'x'}" for k, v in sites.items()
                )
            except Exception as e:  # noqa: BLE001
                res["traffic_detail"] += f"; 重启失败: {e}"
        elif res["traffic"] and not sites.get("github", True) and st.system_proxy_on:
            picked = repin_for_sites(st, want="github")
            if picked:
                reps.append(f"换出口 -> {picked[:28]}")
                if not quiet:
                    warn(f"GitHub 不通而链路正常 —— 已把出口换成 {picked[:40]}")
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
