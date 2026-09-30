"""红杏 · GUI 与引擎之间的**唯一**接口(契约层).

为什么要有这一层
================
界面代码最怕底层一动就崩。这里把界面需要的全部能力收成一组
**稳定、可测、不抛异常**的函数, GUI 只认这一层, 不直接碰
process / sysproxy / api / subscription:

    快照(轮询用)   snapshot()               只读, 实测中位 82ms(内核满载时)
    一键开关       turn_on() / turn_off() / toggle()
    工作模式       set_mode()               rule / global / direct
    节点           list_nodes() / select_node() / pick_best_node()
    自检           test_platforms() / verify_ai()
    开机自启       autostart_status() / set_autostart()

三条硬约定(改动本文件必须遵守)
------------------------------
1. **不导入 tkinter**, 不做任何界面相关的事 —— 这样它能在无图形环境下
   被单元测试完整覆盖。
2. **可预期的失败一律返回带 `error` 字段的结果, 不抛异常**。界面只需要
   把 error 渲染出来, 不需要 try/except 满天飞。真正意外的情况才让它炸。
3. **snapshot() 绝不允许拉全量 /proxies**。免费节点池动辄六千个节点,
   那个响应有几 MB, 每秒轮询一次会把界面拖死。要节点列表就调
   list_nodes(), 而且要在后台线程里调。

线程模型
--------
tkinter 不是线程安全的。会阻塞的操作(turn_on / turn_off / list_nodes /
test_platforms / pick_best_node)必须在后台线程里跑:

    control.run_bg(control.turn_on, on_done=lambda st: root.after(0, render, st))

`on_done` 是在**工作线程**里被调用的, 界面代码必须自己用 `root.after`
把结果搬回主线程。这条写在这里是因为踩过: 直接在工作线程里改控件
会让 Tk 随机崩溃或静默卡死。
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from . import api, diag, paths, process, rules, sysproxy
from .state import load_state, save_state
from .util import json_dump, json_load

# --------------------------------------------------------------------------- #
# 品牌
# --------------------------------------------------------------------------- #

BRAND_NAME = "红杏"
BRAND_TAGLINE = "一键通行"
BRAND_VERSION = "1.0.0"

#: 开机自启/保活用的计划任务名(与 cli.cmd_autostart 保持一致)
TASK_ENSURE = "AccessPilotEnsure"
TASK_REFRESH = "AccessPilotRefresh"

#: 内核策略组里"真正承载流量的节点"之外的条目 —— 这些是组, 不是节点
_GROUP_TYPES = frozenset({
    "Selector", "URLTest", "Fallback", "LoadBalance",
    "Direct", "Reject", "RejectDrop", "Compatible", "Pass", "Dns",
})
_GROUP_NAMES = frozenset({
    rules.G_SELECT, rules.G_AUTO, rules.G_AI, rules.G_SOCIAL,
    rules.G_MEDIA, rules.G_DIRECT, rules.G_REJECT, rules.G_FINAL,
    "GLOBAL", "DIRECT", "REJECT", rules.ACCEL_PROXY_NAME,
})

MODES: dict[str, str] = {
    "rule": "智能分流",
    "global": "全局",
    "direct": "直连",
}


# --------------------------------------------------------------------------- #
# 数据结构 —— 界面只认这几个
# --------------------------------------------------------------------------- #


@dataclass
class Status:
    """界面渲染一屏所需的全部状态。"""

    running: bool = False                 # 内核在跑
    connected: bool = False               # 一键开关的"开"状态
    system_proxy: bool = False            # 系统代理已设置
    proxy_server: str = ""
    tun: bool = False                     # TUN 全局接管
    mode: str = "rule"
    mode_label: str = MODES["rule"]
    profile: str = ""
    node: str = ""                        # 当前出口节点(通用流量)
    ai_node: str = ""                     # ChatGPT 走的节点
    ai_exit: str = ""                     # ChatGPT 出口国家代码
    ai_checked_at: float = 0.0            # 上次实测 ChatGPT 的时间
    node_count: int = 0
    core_version: str = ""
    autostart: bool = False
    uptime_s: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NodeInfo:
    name: str
    latency_ms: int = -1
    alive: bool = False
    current: bool = False
    ai_capable: bool = False
    exit_country: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlatformCheck:
    key: str
    name: str
    ok: bool
    latency_ms: int = -1
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AiStatus:
    """ChatGPT 出口的实测结果(带时间戳, 界面据此显示"几分钟前验证")."""

    node: str = ""
    exit_country: str = ""
    ok: bool = False
    checked_at: float = 0.0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.checked_at) if self.checked_at else 0.0


# --------------------------------------------------------------------------- #
# 内部小工具
# --------------------------------------------------------------------------- #


def _ai_cache_file():
    return paths.cache_dir() / "ai_status.json"


def _load_ai_cache() -> AiStatus:
    data = json_load(_ai_cache_file(), None)
    if not isinstance(data, dict):
        return AiStatus()
    ai = AiStatus()
    for k, v in data.items():
        if hasattr(ai, k):
            setattr(ai, k, v)
    return ai


def _save_ai_cache(ai: AiStatus) -> None:
    try:
        paths.ensure_dirs()
        json_dump(_ai_cache_file(), ai.to_dict())
    except Exception:  # noqa: PERF203
        pass


def _now(px: dict[str, Any], group: str) -> str:
    """内核里某个策略组当前选中的条目名(可能是节点, 也可能还是另一个组)."""
    node = (px.get(group) or {}).get("now")
    return str(node) if node else ""


def _is_real_node(name: str) -> bool:
    return bool(name) and name not in _GROUP_NAMES


def _resolve_current(
    st: Any, group: str, *, depth: int = 4, memo: dict[str, dict[str, Any]] | None = None
) -> tuple[str, int]:
    """把策略组链解析成**真正的节点名**。

    界面上"当前节点"必须是节点, 不能是 `♻️ 自动选择` —— 用户看到组名
    只会困惑。返回 (节点名, 该链上第一个组的候选数); 解析不出来时给 ("", n)。

    `memo` 是一次快照内的查询缓存: 🤖 AI 服务 和 🚀 节点选择 常常指向同一条
    链(都指向 ♻️ 自动选择), 不缓存的话同一条链会被走两遍, 白白多出好几次
    HTTP 往返 —— 实测占掉一次 snapshot() 的一半时间。
    """
    memo = {} if memo is None else memo
    name = group
    cand = 0
    for _ in range(depth):
        info = memo.get(name)
        if info is None:
            try:
                info = api.proxy(st, name)
            except Exception:  # noqa: PERF203
                break
            memo[name] = info
        if not cand:
            alls = info.get("all")
            if isinstance(alls, list):
                cand = len(alls)
        nxt = str(info.get("now") or "")
        if not nxt:
            break
        if _is_real_node(nxt):
            return nxt, cand
        name = nxt
    return "", cand


def _fail(msg: str) -> Status:
    st = Status(error=msg)
    try:
        st = snapshot()
        st.error = msg
    except Exception:  # noqa: PERF203
        pass
    return st


#: 极少变化、但取一次很贵的东西的短缓存。
#:
#: 为什么必须有: `schtasks /Query` 要**起一个进程**(实测 100~300ms),
#: `api.version` 要一次 HTTP。界面每 1~2 秒轮询一次 snapshot(), 不缓存
#: 的话光这两项就把延迟顶到 500ms —— 实测过一次 947ms。
_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
    now = time.time()
    hit = _CACHE.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception:  # noqa: PERF203
        return hit[1] if hit is not None else None
    _CACHE[key] = (now, val)
    return val


def invalidate_cache() -> None:
    """改过自启/重启内核之后调一下, 免得界面还显示旧值。"""
    _CACHE.clear()


# --------------------------------------------------------------------------- #
# 快照
# --------------------------------------------------------------------------- #


def snapshot() -> Status:
    """只读快照。界面按 1~2 秒轮询它。

    **刻意不拉全量 /proxies**: 免费节点池有六千个节点, 那份响应几 MB,
    轮询它界面会卡死。当前节点用单组查询拿。
    """
    st = load_state()
    out = Status(
        profile=st.active_profile,
        tun=bool(st.tun_enable),
        ai_checked_at=0.0,
    )

    try:
        out.running = process.is_running()
    except Exception:  # noqa: PERF203
        out.running = False

    try:
        out.system_proxy, out.proxy_server = sysproxy.status()
    except Exception:  # noqa: PERF203
        pass

    # 一键开关的语义: 内核在跑 **且** 流量确实被接管了
    out.connected = bool(out.running and (out.system_proxy or out.tun))

    if out.running:
        # mode 只有用户点切换时才会变, 5 秒缓存足够, 省掉一次 HTTP
        out.mode = str(_cached("mode", 5.0, lambda: api.mode(st)) or "rule")
        out.mode_label = MODES.get(out.mode, out.mode)
        # 只做单组查询 + 组链解析。**绝不能**在这里拉全量 /proxies:
        # 免费池六千个节点时那份响应有几 MB, 实测让 snapshot() 从 <20ms
        # 涨到 947ms, 每秒轮询一次界面就废了。
        memo: dict[str, dict[str, Any]] = {}
        try:
            out.node, cnt = _resolve_current(st, rules.G_SELECT, memo=memo)
            out.node_count = cnt or out.node_count
        except Exception:  # noqa: PERF203
            pass
        try:
            ai_pick, acnt = _resolve_current(st, rules.G_AI, memo=memo)
            out.ai_node = ai_pick
            if not out.node_count:
                out.node_count = acnt
        except Exception:  # noqa: PERF203
            pass
        try:
            out.core_version = _cached(
                "core_version", 60.0, lambda: api.version(st)) or ""
        except Exception:  # noqa: PERF203
            pass

    if st.last_start:
        out.uptime_s = max(0.0, time.time() - float(st.last_start))

    ai = _load_ai_cache()
    if ai.node and (not out.ai_node or ai.node == out.ai_node):
        out.ai_exit = ai.exit_country
        out.ai_checked_at = ai.checked_at

    # 自启状态要起进程去问 schtasks, 30 秒内没必要问第二次
    out.autostart = bool(_cached("autostart", 30.0,
                                 lambda: _task_exists(TASK_ENSURE)))

    return out


def list_profiles_nodes(profile: str) -> list[str]:
    """配置档里的节点名(不碰内核)。空配置档返回空表。"""
    if not profile:
        return []
    try:
        from .subscription import load_profile

        return [str(p.get("name")) for p in load_profile(profile).proxies]
    except Exception:  # noqa: PERF203
        return []


# --------------------------------------------------------------------------- #
# 一键开关
# --------------------------------------------------------------------------- #


def turn_on(*, tun: bool | None = None, system_proxy: bool = True) -> Status:
    """打开。= 内核起来 + 系统代理生效 + 保活任务就位。

    这是「蓝灯那样一个大开关」的实现: 用户不需要知道什么是代理、什么是节点。
    """
    st = load_state()
    try:
        if process.is_running():
            if system_proxy and not st.system_proxy_on:
                sysproxy.enable(st)
                st.system_proxy_on = True
                save_state(st)
        else:
            process.start(st=st, tun=bool(st.tun_enable if tun is None else tun),
                          system_proxy=system_proxy)
        # 计时起点由 process.start() 自己写(见 process.py 里 st.last_start)。
        # 这里只在"内核早就在跑、但状态里没有起点"时补一个, 免得界面上的
        # "已用 N 天"显示成空白或负数。
        if not st.last_start:
            st.last_start = time.time()
        save_state(st)
    except Exception as e:  # noqa: PERF203
        return _fail(str(e) or type(e).__name__)
    return snapshot()


def turn_off() -> Status:
    """关闭。内核停掉, 系统代理还原 —— 不留任何残留设置。"""
    st = load_state()
    try:
        process.stop()
        st.system_proxy_on = False
        save_state(st)
    except Exception as e:  # noqa: PERF203
        return _fail(str(e) or type(e).__name__)
    return snapshot()


def toggle() -> Status:
    return turn_off() if snapshot().connected else turn_on()


# --------------------------------------------------------------------------- #
# 模式
# --------------------------------------------------------------------------- #


def set_mode(mode: str) -> Status:
    if mode not in MODES:
        return _fail(f"未知模式: {mode}")
    st = load_state()
    try:
        if not process.is_running():
            return _fail("内核未运行, 请先打开开关")
        api.set_mode(st, mode)
    except Exception as e:  # noqa: PERF203
        return _fail(str(e) or type(e).__name__)
    return snapshot()


def set_tun(on: bool) -> Status:
    """TUN 全局接管开关。需要管理员权限, 且会改配置 → 必须重启内核。"""
    st = load_state()
    try:
        st.tun_enable = bool(on)
        save_state(st)
        if process.is_running():
            process.restart(st=st, tun=bool(on), system_proxy=not on)
    except Exception as e:  # noqa: PERF203
        return _fail(str(e) or type(e).__name__)
    return snapshot()


def tun_available() -> tuple[bool, str]:
    try:
        return sysproxy.tun_available()
    except Exception as e:  # noqa: PERF203
        return False, str(e)


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #


def list_nodes(*, limit: int = 0, alive_only: bool = False) -> list[NodeInfo]:
    """节点列表(带延迟)。

    **必须在后台线程调用**: 免费节点池六千个节点时这个响应有几 MB。
    界面应该 `limit` 一下(默认给前 200 个就够了)。
    """
    st = load_state()
    try:
        px = api.proxies(st)
    except Exception:  # noqa: PERF203
        return []

    cur = _now(px, rules.G_SELECT)
    ai_pick = _now(px, rules.G_AI)
    out: list[NodeInfo] = []
    for name, info in px.items():
        if not isinstance(info, dict):
            continue
        if str(info.get("type")) in _GROUP_TYPES or name in _GROUP_NAMES:
            continue
        hist = info.get("history") or []
        delay = -1
        if hist and isinstance(hist[-1], dict):
            try:
                delay = int(hist[-1].get("delay") or -1)
            except (TypeError, ValueError):
                delay = -1
        if alive_only and delay <= 0:
            continue
        out.append(NodeInfo(
            name=name, latency_ms=delay, alive=delay > 0,
            current=(name == cur), ai_capable=(name == ai_pick),
        ))

    out.sort(key=lambda n: (
        not n.current, not n.alive,
        n.latency_ms if n.latency_ms > 0 else 10 ** 9, n.name,
    ))
    return out[:limit] if limit and limit > 0 else out


def select_node(name: str) -> Status:
    st = load_state()
    try:
        if not process.is_running():
            return _fail("内核未运行")
        api.select(st, rules.G_SELECT, name)
        st.selected[rules.G_SELECT] = name
        save_state(st)
    except Exception as e:  # noqa: PERF203
        return _fail(str(e) or type(e).__name__)
    return snapshot()


def pick_best_node(*, limit: int = 40) -> Status:
    """把当前最快的可用节点设为出口。

    为什么不用内核的 url-test: 它只看延迟, 不看**能不能真的打开网页**
    (实测出现过"测速 30ms 但打开 X 要十几秒")。这里用平台级验证过的节点。
    """
    st = load_state()
    try:
        if not process.is_running():
            return _fail("内核未运行")
        from . import freenodes

        nodes = list_nodes(limit=limit, alive_only=True)
        if not nodes:
            return _fail("没有可用节点, 请先刷新节点池")
        for n in nodes[:limit]:
            r = freenodes.verify_node(st, n.name, timeout=8.0)
            if freenodes.fully_usable(r):
                api.select(st, rules.G_SELECT, n.name)
                st.selected[rules.G_SELECT] = n.name
                save_state(st)
                return snapshot()
    except Exception as e:  # noqa: PERF203
        return _fail(str(e) or type(e).__name__)
    return _fail("测了一圈没有真正可用的节点, 建议刷新节点池")


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #


def test_platforms(*, timeout: float = 10.0) -> list[PlatformCheck]:
    """平台连通性自检(ChatGPT / Discord / X / Google ...)。后台线程调用。"""
    st = load_state()
    proxy = f"http://127.0.0.1:{st.mixed_port}" if process.is_running() else None
    out: list[PlatformCheck] = []
    try:
        for r in diag.probe_sites(proxy, timeout=timeout):
            out.append(PlatformCheck(
                key=r.key, name=r.name, ok=bool(r.ok),
                latency_ms=int(getattr(r, "latency_ms", -1) or -1),
                detail=str(getattr(r, "detail", "") or ""),
            ))
    except Exception as e:  # noqa: PERF203
        out.append(PlatformCheck(key="__error__", name="自检失败", ok=False,
                                 detail=str(e) or type(e).__name__))
    return out


def verify_ai(*, timeout: float = 12.0) -> AiStatus:
    """实测**当前** ChatGPT 出口能不能用, 并把结果缓存下来给界面显示。

    判据是 chatgpt.com/cdn-cgi/trace 的状态码: 200 = 可用, 403 = 该出口
    地区不受 OpenAI 支持(网页会显示 "Unable to load site")。
    背景: 免费节点里最快的那批往往是香港节点, X/Discord 全绿但 ChatGPT
    403 —— 所以"能上 X"绝不能当成"能上 ChatGPT"。
    """
    st = load_state()
    node = ""
    ai = AiStatus(checked_at=time.time())
    try:
        if not process.is_running():
            ai.detail = "内核未运行"
            return ai
        pick = str(api.proxy(st, rules.G_AI).get("now") or "")
        node = pick if _is_real_node(pick) else ""
        ai.node = node
        from .util import http_request

        status, _, body = http_request(
            "https://chatgpt.com/cdn-cgi/trace", timeout=timeout,
            headers=diag.BROWSER_HEADERS,
        )
        ai.ok = status == 200
        if status == 200:
            for line in body.decode("utf-8", "replace").splitlines():
                if line.startswith("loc="):
                    ai.exit_country = line[4:].strip().upper()
        ai.detail = f"HTTP {status}"
    except Exception as e:  # noqa: PERF203
        ai.ok = False
        ai.detail = str(e) or type(e).__name__
    _save_ai_cache(ai)
    return ai


# --------------------------------------------------------------------------- #
# 开机自启 / 保活
# --------------------------------------------------------------------------- #


def _run_hidden(args: list[str], timeout: float = 30.0) -> tuple[int, str]:
    from .util import run_hidden

    return run_hidden(args, timeout=timeout)


def _task_exists(name: str) -> bool:
    code, _ = _run_hidden(["schtasks", "/Query", "/TN", name, "/FO", "LIST"], timeout=15)
    return code == 0


def autostart_status() -> dict[str, Any]:
    try:
        return {
            "ensure": _task_exists(TASK_ENSURE),
            "refresh": _task_exists(TASK_REFRESH),
        }
    except Exception as e:  # noqa: PERF203
        return {"ensure": False, "refresh": False, "error": str(e)}


def set_autostart(
    on: bool,
    *,
    exe: str | None = None,
    minutes: int = 5,
    refresh_minutes: int = 0,
) -> dict[str, Any]:
    """注册/取消开机保活(Windows 计划任务)。

    exe: 要保活的命令行。源码运行时留空会自动用当前 Python 调 accesspilot;
         打包成 exe 后由调用方传 sys.executable, 这样任务指向 exe 本身,
         用户机器上不需要装 Python, 也不需要全局 accesspilot 命令。
    """
    import sys as _sys

    if on:
        if exe:
            ensure_cmd = f'cmd /c ""{exe}" ensure"'
            refresh_cmd = f'cmd /c ""{exe}" free auto --workers 96 --timeout 5 --verify-top 30"'
        else:
            ensure_cmd = f'cmd /c ""{_sys.executable}" -m accesspilot ensure"'
            refresh_cmd = (f'cmd /c ""{_sys.executable}" -m accesspilot '
                           f'free auto --workers 96 --timeout 5 --verify-top 30"')
        code, out = _run_hidden(
            ["schtasks", "/Create", "/F", "/TN", TASK_ENSURE, "/SC", "MINUTE",
             "/MO", str(minutes), "/TR", ensure_cmd, "/RL", "LIMITED"],
        )
        if code != 0:
            return {"ok": False, "error": out.strip()[:300]}
        if refresh_minutes > 0:
            _run_hidden(
                ["schtasks", "/Create", "/F", "/TN", TASK_REFRESH, "/SC", "MINUTE",
                 "/MO", str(refresh_minutes), "/TR", refresh_cmd, "/RL", "LIMITED"],
            )
        return {"ok": True, **autostart_status()}

    for name in (TASK_ENSURE, TASK_REFRESH):
        _run_hidden(["schtasks", "/Delete", "/F", "/TN", name], timeout=20)
    return {"ok": True, **autostart_status()}


# --------------------------------------------------------------------------- #
# 自动切换 / 保活(实现在 health.py, 这里只是契约)
# --------------------------------------------------------------------------- #
#
# health.py 必须提供:
#     state() -> dict     {enabled, running, last_check, switches, last_error, note}
#     set_enabled(bool)   -> None
#     start() / stop()    -> None
# 这里用延迟导入 + 兜底, 是为了让界面在 health.py 还没落地时也能正常跑 ——
# 界面代码永远不该因为一个可选模块缺失就崩掉。


def health_state() -> dict[str, Any]:
    try:
        from . import health

        return dict(health.state())
    except Exception:  # noqa: PERF203
        return {"enabled": False, "running": False, "note": "自动切换未启用"}


def set_auto_failover(on: bool) -> Status:
    try:
        from . import health

        health.set_enabled(bool(on))
        if on:
            health.start()
    except Exception as e:  # noqa: PERF203
        return _fail(f"自动切换不可用: {e}")
    invalidate_cache()
    return snapshot()


# --------------------------------------------------------------------------- #
# 线程helper
# --------------------------------------------------------------------------- #


def run_bg(
    fn: Callable[..., Any],
    *args: Any,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    **kwargs: Any,
) -> threading.Thread:
    """在后台线程里跑一个阻塞操作。

    `on_done` 在**工作线程**里被调用 —— tkinter 不是线程安全的, 界面代码
    必须自己用 `root.after(0, ...)` 把结果搬回主线程再碰控件。
    """

    def worker() -> None:
        try:
            result = fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001
            if on_error:
                on_error(e)
            return
        if on_done:
            on_done(result)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t
