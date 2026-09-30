"""公开免费节点源: 零成本、无需 VPS、无需信用卡的最后一条路.

现实情况(本机实测, 2026-09):
  * 从 8 个公开源抓到 369 个去重节点, **只有 13 个真正可用(3.5%)**;
  * 这 13 个里, Discord 与 X 都能打开 ✅;
  * ChatGPT 绝大多数打不开 ❌, 但**并非全军覆没**: 实测确实存在能用的免费
    节点(如韩国 KT 出口, chatgpt.com 返回 200 + loc=KR)。失败的原因有两类,
    必须分开看: (a) 出口地区不在 OpenAI 支持列表(香港/大陆 → HTTP 403
    "Unable to load site"); (b) 节点自身连不通。
  * 结论: 免费节点上 Discord / X / Google / YouTube 很稳; ChatGPT 能用但要
    **逐个实测 + 钉住**, 且会随节点池刷新而失效 —— 所以 AccessPilot 在
    `free auto` 里自动挑出能上 ChatGPT 的节点并把它钉进 🤖 AI 服务 组。

安全提醒: 这些节点由陌生人运营, 对方能看到你的流量去向(HTTPS 内容看不到)。
不要在用它们时登录银行、邮箱等敏感账号。AccessPilot 会在使用时提示这一点。
"""
from __future__ import annotations

import concurrent.futures as futures
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from . import subscription as sub_mod
from .subscription import Subscription
from .util import Fail, http_request, info, ok, warn

#: 每日更新的免费节点网站(不是 GitHub!)。
#:
#: 为什么单独做这类源: 实测这些站点**从国内可以直接访问**(它们本来就是给
#: 国内用户做的), 所以不需要任何节点就能下载到当天的订阅文件。文件按日期
#: 组织, URL 可预测, 内容每天刷新 —— 比 GitHub 聚合源更新鲜。
DAILY_FILE_TEMPLATES: list[str] = [
    # freeclashnode.com: 每天发布 0~5 号 txt / yaml
    "https://node.freeclashnode.com/uploads/{ym}/{i}-{ymd}.txt",
    "https://node.freeclashnode.com/uploads/{ym}/{i}-{ymd}.yaml",
]

#: 需要"先抓文章、再从文章里找订阅文件"的两级源
ARTICLE_SOURCES: list[tuple[str, str]] = [
    ("freeclashnode.com", "https://freeclashnode.com/free-node/{date}-free-node-subscribe-links.htm"),
    ("clashnode.cc", "https://clashnode.cc/free-node/clash-node-daily-updates-{date}.htm"),
]

_SUB_FILE_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:txt|ya?ml)(?:\?[^\s\"'<>]*)?", re.I
)

#: 公开免费节点源。多重镜像回退: jsDelivr(国内可直连) -> ghproxy -> raw
SOURCES: list[tuple[str, str]] = [
    ("freefq/free", "gh/freefq/free@master/v2"),
    ("ripaojiedian/freenode", "gh/ripaojiedian/freenode@main/sub"),
    ("Pawdroid/Free-servers", "gh/Pawdroid/Free-servers@main/sub"),
    ("ermaozi/get_subscribe", "gh/ermaozi/get_subscribe@main/subscribe/v2ray.txt"),
    ("peasoft/NoMoreWalls", "gh/peasoft/NoMoreWalls@master/list.txt"),
    ("aiboboxx/v2rayfree", "gh/aiboboxx/v2rayfree@main/v2"),
    ("mfuu/v2ray", "gh/mfuu/v2ray@master/v2ray"),
    ("vveg26/getProxy", "gh/vveg26/getProxy@main/dist/v2ray.config.txt"),
    ("barry-far/V2ray-Configs", "gh/barry-far/V2ray-Configs@main/All_Configs_Sub.txt"),
    # 以下为补充源(实测可用, 2026-09), 让节点池更大、更抗"整批失效":
    ("mahdibland/V2RayAggregator", "gh/mahdibland/V2RayAggregator@master/Eternity"),
    ("w1770946466/Auto_proxy", "gh/w1770946466/Auto_proxy@main/Long_term_subscription_num"),
    ("free18/v2ray", "gh/free18/v2ray@main/v.txt"),
    # README 里直接列节点的仓库也能用(靠正则兜底提取)
    ("lza6/free-VPN", "gh/lza6/free-VPN@main/README.md"),
    ("0xRadikal/Free-v2ray-Configs", "gh/0xRadikal/Free-v2ray-Configs@main/README.md"),
    # 超大聚合源(单源就有数千节点, 靠 TCP 预筛控制规模)
    ("mahdibland/sub_merge", "gh/mahdibland/V2RayAggregator@master/sub/sub_merge_base64.txt"),
    ("Epodonios/v2ray-configs", "gh/Epodonios/v2ray-configs@main/All_Configs_Sub.txt"),
]

_MIRRORS = [
    "https://testingcf.jsdelivr.net/{path}",
    "https://ghfast.top/https://raw.githubusercontent.com/{raw}",
    "https://gh-proxy.com/https://raw.githubusercontent.com/{raw}",
    "https://raw.githubusercontent.com/{raw}",
]

_LINK_RE = re.compile(
    r"(?:ss|ssr|vmess|vless|trojan|hysteria2?|hy2|tuic)://[^\s\"'<>\\]+"
)


def _urls_for(path: str) -> list[str]:
    raw = path[3:] if path.startswith("gh/") else path
    raw = raw.replace("@", "/", 1)  # owner/repo@ref/... -> owner/repo/ref/...
    return [m.format(path=path, raw=raw) for m in _MIRRORS]


def _fetch_one(path: str, *, timeout: float = 25.0) -> tuple[str, str]:
    """带镜像回退地取回源文本, 返回 (正文, 命中的镜像主机)."""
    last = ""
    for url in _urls_for(path):
        try:
            status, _, body = http_request(url, timeout=timeout)
        except Exception as e:  # noqa: PERF203
            last = type(e).__name__
            continue
        if status != 200:
            last = f"HTTP {status}"
            continue
        text = body.decode("utf-8", errors="replace")
        if text.strip():
            return text, url.split("/")[2]
        last = "内容为空"
    raise Fail(last or "抓取失败")


def fetch_source(name: str, path: str, *, timeout: float = 25.0) -> tuple[int, str]:
    """抓取单个源, 返回 (节点数, 命中的镜像主机)."""
    text, host = _fetch_one(path, timeout=timeout)
    nodes = _extract(text, name)
    if not nodes:
        raise Fail("内容无法识别")
    return len(nodes), host


def _extract(text: str, name: str) -> list[dict[str, Any]]:
    """解析订阅内容; 标准格式失败时用正则兜底捞出所有分享链接."""
    try:
        nodes, _ = sub_mod.parse_content(text, name)
        if nodes:
            return nodes
    except Fail:
        pass
    links = _LINK_RE.findall(text)
    if not links:
        return []
    return sub_mod.sharelink.parse_links("\n".join(links))


def sanitize(proxies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤内核不支持的节点参数(与配置生成共用同一套规则).

    真实事故: 一个 SSR 节点带了 auth_aes128_md5 密码, 让 1242 个节点的
    整个配置热重载失败; 另一个 hysteria2 节点有 obfs 无密码, 同样让
    校验失败。规范实现见 config.sanitize_proxies, 这里只是套一层计数。
    """
    from .config import sanitize_proxies

    before = len(proxies)
    out = sanitize_proxies(proxies)
    dropped = before - len(out)
    if dropped and before > 1000:
        warn(f"过滤掉 {dropped} 个内核不支持的节点")
    return out


def daily_file_urls(days_back: int = 2, indices: range = range(6)) -> list[str]:
    """构造"每日更新网站"的候选文件地址(今天/昨天/前天)."""
    urls: list[str] = []
    seen: set[str] = set()
    for delta in range(days_back + 1):
        d = datetime.now() - timedelta(days=delta)
        for i in indices:
            for tpl in DAILY_FILE_TEMPLATES:
                u = tpl.format(ym=d.strftime("%Y/%m"), ymd=d.strftime("%Y%m%d"), i=i)
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
    return urls


def _article_urls(days_back: int = 2) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for delta in range(days_back + 1):
        d = datetime.now() - timedelta(days=delta)
        date = f"{d.year}-{d.month}-{d.day}"
        for name, tpl in ARTICLE_SOURCES:
            out.append((name, tpl.format(date=date)))
    return out


def fetch_daily(*, timeout: float = 20.0, verbose: bool = True) -> list[dict[str, Any]]:
    """抓取"每日更新网站"当天的节点.

    分两步:
      1. 直接按日期拼文件地址(freeclashnode 的 node.* 域名), 命中率最高;
      2. 抓文章页, 从里面正则捞出所有 .txt/.yaml 订阅文件再抓 —— 这样
         换站点、改文件名也能自动适应。
    """
    collected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    # 第一步: 直接拼地址
    hit = 0
    for url in daily_file_urls():
        try:
            status, _, body = http_request(url, timeout=timeout)
        except Exception:  # noqa: PERF203
            continue
        if status != 200:
            continue
        nodes = _extract(body.decode("utf-8", errors="replace"), "daily")
        if nodes:
            collected += nodes
            hit += 1
            seen_urls.add(url)
    if verbose and hit:
        ok(f"{'每日文件(直接地址)':<26} {hit:>4} 个文件命中")

    # 第二步: 文章页 -> 订阅文件
    for name, art_url in _article_urls():
        try:
            status, _, body = http_request(art_url, timeout=timeout)
        except Exception:  # noqa: PERF203
            continue
        if status != 200:
            continue
        html = body.decode("utf-8", errors="replace")
        sub_urls = [
            u for u in dict.fromkeys(_SUB_FILE_RE.findall(html))
            if u not in seen_urls and "favicon" not in u
        ]
        got = 0
        for u in sub_urls[:16]:
            seen_urls.add(u)
            try:
                s2, _, b2 = http_request(u, timeout=timeout)
            except Exception:  # noqa: PERF203
                continue
            if s2 != 200:
                continue
            nodes = _extract(b2.decode("utf-8", errors="replace"), name)
            if nodes:
                collected += nodes
                got += len(nodes)
        if verbose and got:
            ok(f"{name:<26} {got:>4} 个节点  (取自文章页)")
    return collected


def fetch_all(
    sources: Iterable[tuple[str, str]] | None = None,
    *,
    timeout: float = 25.0,
    verbose: bool = True,
) -> Subscription:
    """抓取全部公开源, 去重、过滤后返回一个订阅对象."""
    sources = list(sources if sources is not None else SOURCES)
    collected: list[dict[str, Any]] = []
    for name, path in sources:
        try:
            text, host = _fetch_one(path, timeout=timeout)
        except Fail as e:
            if verbose:
                warn(f"{name:<26} 跳过: {e}")
            continue
        nodes = _extract(text, name)
        if not nodes:
            if verbose:
                warn(f"{name:<26} 跳过: 内容无法识别")
            continue
        if verbose:
            ok(f"{name:<26} {len(nodes):>4} 个节点  (via {host})")
        collected += nodes

    # 每日更新网站(国内直连可达, 内容每天刷新)
    try:
        daily = fetch_daily(timeout=timeout, verbose=verbose)
        if daily:
            collected += daily
    except Exception as e:  # noqa: PERF203
        if verbose:
            warn(f"每日更新网站抓取失败: {type(e).__name__}")

    if not collected:
        raise Fail("所有公开源都抓取失败, 请检查网络或稍后重试")

    proxies = sub_mod.uniquify_names(sub_mod.dedupe(collected))
    proxies = sanitize(proxies)
    if verbose:
        from collections import Counter

        dist = Counter(str(p.get("type")) for p in proxies)
        info(f"合计 {len(collected)} 个 -> 去重/过滤后 {len(proxies)} 个: {dict(dist)}")
    return Subscription(name="free", proxies=proxies, url="", updated=time.time())


# --------------------------------------------------------------------------- #
# 批量测速与筛选
# --------------------------------------------------------------------------- #

#: 走 UDP/QUIC 的协议不能做 TCP 预筛(它们的端口上根本没有 TCP 监听)
UDP_BASED_TYPES = {"hysteria", "hysteria2", "tuic"}


def tcp_prefilter(
    proxies: list[dict[str, Any]],
    *,
    workers: int = 256,
    timeout: float = 2.5,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """用裸 TCP 连接先筛掉"服务器已经死了"的节点.

    为什么需要: 大源动辄上万个节点, 全部塞进内核会让配置加载要几分钟、
    内存暴涨。而其中绝大多数节点是**连服务器都连不上**的, 用不着内核出手。
    实测这一步能淘汰 90%+ 的垃圾节点, 剩下的再交给内核做真实协议测试。

    UDP 类协议(hysteria2/tuic)无法用 TCP 探测判断, 一律保留。
    """
    import socket

    keep_always = [p for p in proxies if str(p.get("type")) in UDP_BASED_TYPES]
    to_probe = [p for p in proxies if str(p.get("type")) not in UDP_BASED_TYPES]
    total = len(to_probe)
    if not total:
        return list(proxies), 0

    def one(p: dict[str, Any]) -> bool:
        host = str(p.get("server") or "")
        try:
            port = int(p.get("port") or 0)
        except (TypeError, ValueError):
            return False
        if not host or not port:
            return False
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    alive: list[dict[str, Any]] = []
    done = 0
    with futures.ThreadPoolExecutor(max_workers=max(1, min(workers, total))) as ex:
        for p, ok in zip(to_probe, ex.map(one, to_probe)):
            done += 1
            if ok:
                alive.append(p)
            if progress and (done % 500 == 0 or done == total):
                progress(done, total)
    return alive + keep_always, total - len(alive)


def bulk_test(
    st: Any,
    names: list[str],
    *,
    workers: int = 48,
    timeout_ms: int = 6000,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """并发测试大量节点, 返回 {节点名: 延迟ms}.

    刻意不用内核对外的 `/group/{name}/delay` —— 实测它只返回**部分**节点的
    结果(369 个节点只回了 13 个), 会让人误判为"节点全挂了"。
    """
    from . import api

    alive: dict[str, int] = {}
    total = len(names)
    done = 0

    def one(node: str) -> tuple[str, int]:
        try:
            return node, api.delay(st, node, timeout_ms=timeout_ms)
        except Exception:
            return node, -1

    with futures.ThreadPoolExecutor(max_workers=max(1, min(workers, total or 1))) as ex:
        for node, delay in ex.map(one, names):
            done += 1
            if delay > 0:
                alive[node] = delay
            if progress and (done % 25 == 0 or done == total):
                progress(done, total)
    return dict(sorted(alive.items(), key=lambda kv: kv[1]))


def prune_profile(name: str, alive: dict[str, int], *, keep_min: int = 1) -> tuple[int, int]:
    """把配置档里失效的节点删掉, 保留测速通过的(按延迟排序). 返回 (前, 后)."""
    if len(alive) < keep_min:
        raise Fail(f"可用节点太少({len(alive)} 个), 拒绝清理以免把配置档清空")
    sub = sub_mod.load_profile(name)
    before = len(sub.proxies)
    ordered = [n for n, _ in sorted(alive.items(), key=lambda kv: kv[1])]
    by_name = {str(p.get("name")): p for p in sub.proxies}
    sub.proxies = [by_name[n] for n in ordered if n in by_name]
    sub.updated = time.time()
    sub_mod.save_profile(sub)
    return before, len(sub.proxies)


# --------------------------------------------------------------------------- #
# 平台级验证: "延迟快" ≠ "能用"
# --------------------------------------------------------------------------- #

#: 验证 X 是否真的可用时, 除了主页还要拉一个静态资源 —— 只测主页会出现
#: "HTTP 200 但页面永远加载不出来" 的假阳性(之前真实发生过的坑)。
X_CHECK_URLS = (
    "https://x.com/",
    "https://abs.twimg.com/favicons/twitter.3.ico",
)
DISCORD_CHECK_URL = "https://discord.com/api/v9/gateway"

#: 探测 ChatGPT **真实可用性**的地址。
#:
#: 为什么必须是 `/cdn-cgi/trace` 而不是 `https://chatgpt.com/`:
#:   * 它是 Cloudflare 的明文回显, 只有几百字节, 但**只在被允许的地区才 200**;
#:     不受支持的地区 OpenAI 直接回 403 ("Unable to load site")。
#:     所以一次请求同时给出两个关键信息: 能不能用 + 出口落在哪个国家(loc=XX)。
#:   * 实测(2026-09): 香港节点 -> 403; 韩国节点 -> 200 + loc=KR。
#:
#: 血泪教训: **内核的 url-test 健康检查把 403 当成"通"** —— 实测把探测地址
#: 换成 chatgpt.com 后, 香港节点依然报 "121 ms 成功"。所以"让 AI 组用
#: chatgpt.com 做 url-test 就能自动跳过被封地区"是**错的**, 必须显式读状态码。
CHATGPT_TRACE_URL = "https://chatgpt.com/cdn-cgi/trace"

#: 已知**不受支持**的地区(命中时给出人话解释, 而不是只说"失败")。
#: 判据始终以 trace 的 200/403 为准, 这张表只用于把原因说清楚。
CHATGPT_BLOCKED_REGIONS: dict[str, str] = {
    "CN": "中国大陆",
    "HK": "中国香港",
    "MO": "中国澳门",
    "RU": "俄罗斯",
    "IR": "伊朗",
    "KP": "朝鲜",
    "CU": "古巴",
    "SY": "叙利亚",
    "SD": "苏丹",
    "VE": "委内瑞拉",
    "BY": "白俄罗斯",
}


def verify_node(
    st: Any, node: str, *, timeout: float = 10.0
) -> dict[str, Any]:
    """把一个节点切到 AI/社交组, 实测它能否真正服务 X、Discord 与 ChatGPT.

    返回 {name, latency_ms, x_ok, x_asset_ok, discord_ok,
          chatgpt_ok, chatgpt_loc, chatgpt_status, detail}。

    为什么要连 ChatGPT 一起测: X 能打开**不代表** ChatGPT 能打开 ——
    ChatGPT 额外受"出口国家是否在 OpenAI 支持列表"限制。香港节点 X/Discord
    全绿、ChatGPT 却是 403 "Unable to load site", 这是真实踩过的坑。
    """
    from . import api
    from .util import http_request

    failed = {"name": node, "latency_ms": -1, "x_ok": False,
              "x_asset_ok": False, "discord_ok": False,
              "chatgpt_ok": False, "chatgpt_loc": "", "chatgpt_status": 0,
              "detail": ""}

    proxy = f"http://127.0.0.1:{st.mixed_port}"
    try:
        api.select(st, "💬 社交平台", node)
        api.select(st, "🤖 AI 服务", node)
    except Exception as e:  # noqa: PERF203
        failed["detail"] = f"选择失败: {e}"
        return failed

    results: dict[str, Any] = dict(failed)
    t0 = time.time()
    try:
        s, _, _ = http_request(X_CHECK_URLS[0], timeout=timeout, proxy=proxy)
        results["x_ok"] = s in (200, 301, 302, 304)
        results["latency_ms"] = int((time.time() - t0) * 1000)
    except Exception as e:  # noqa: PERF203
        results["detail"] = type(e).__name__
    try:
        s, _, _ = http_request(X_CHECK_URLS[1], timeout=timeout, proxy=proxy)
        results["x_asset_ok"] = s in (200, 301, 302, 304)
    except Exception:  # noqa: PERF203
        pass
    try:
        s, _, _ = http_request(DISCORD_CHECK_URL, timeout=timeout, proxy=proxy)
        results["discord_ok"] = s in (200, 429)
    except Exception:  # noqa: PERF203
        pass

    # ChatGPT: 只有 200 才算数。403 = 出口地区不受支持, 网页会显示
    # "Unable to load site", 这正是用户截图里的那个报错。
    try:
        s, _, body = http_request(CHATGPT_TRACE_URL, timeout=timeout, proxy=proxy)
        results["chatgpt_status"] = s
        results["chatgpt_ok"] = s == 200
        if s == 200:
            m = re.search(rb"loc=([A-Za-z]{2})", body or b"")
            if m:
                results["chatgpt_loc"] = m.group(1).decode().upper()
    except Exception:  # noqa: PERF203
        pass

    return results


def chatgpt_usable(r: dict[str, Any]) -> bool:
    """这个节点能否真正打开 ChatGPT(而不只是"连得上")。"""
    return bool(r.get("chatgpt_ok"))


def chatgpt_reason(r: dict[str, Any]) -> str:
    """给"ChatGPT 用不了"一个能看懂的原因。"""
    st = int(r.get("chatgpt_status") or 0)
    loc = str(r.get("chatgpt_loc") or "")
    if st == 403:
        where = CHATGPT_BLOCKED_REGIONS.get(loc)
        return f"地区不受支持{f'({where})' if where else ''} - HTTP 403"
    if st == 0:
        return "连接失败/超时"
    if st == 200:
        return f"可用 (出口 {loc or '?'})"
    return f"HTTP {st}"


def current_selection(st: Any, group: str) -> str:
    """读内核里某个策略组当前选中的节点名(读不到返回空串)."""
    from . import api

    try:
        return str((api.proxies(st).get(group) or {}).get("now") or "")
    except Exception:  # noqa: PERF203
        return ""


def verify_many(
    st: Any, nodes: list[str], *, workers: int = 16, timeout: float = 10.0,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """对一批节点做平台级验证.

    必须**顺序**执行: 每个节点要先把策略组切到自己再发请求, 并发会让
    请求走到别的节点上, 结果是"测了一堆节点其实都在测同一个"。
    """
    out: list[dict[str, Any]] = []
    for i, node in enumerate(nodes, 1):
        out.append(verify_node(st, node, timeout=timeout))
        if progress and (i % 5 == 0 or i == len(nodes)):
            progress(i, len(nodes))
    return out


def fully_usable(r: dict[str, Any]) -> bool:
    return bool(r.get("x_ok") and r.get("x_asset_ok") and r.get("discord_ok"))


SECURITY_NOTICE = (
    "这些节点由陌生人运营, 对方能看到你的流量去向(HTTPS 内容看不到)。\n"
    "      请勿在使用免费节点时登录银行/邮箱等敏感账号; ChatGPT 账号尤其不建议。"
)
