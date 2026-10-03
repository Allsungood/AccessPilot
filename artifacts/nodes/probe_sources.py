"""批量探测候选节点源: 哪些**不开代理**就能下、能下多少、去重后净增多少。

## 为什么要这个脚本

用户的环境是「中国大陆 + 没有 VPS + 没有信用卡 + 没有付费机场」。对这种人来说,
**一个必须挂代理才能下载的源等于不存在** —— 那是死循环(要节点才能下节点)。

所以判断一个源值不值得加, 不能看它"存在", 只能看三个数:
    1. 不开代理能不能下到;
    2. 解析出多少条;
    3. **去重后净增多少** —— 很多 GitHub 聚合仓库是同一个上游的镜像, 加进去一条不涨。

这个脚本就是为了把这三个数一次测出来, 免得靠感觉往列表里塞源。

## 用法

    python artifacts/nodes/probe_sources.py --no-proxy        # 关键: 模拟用户的真实处境
    python artifacts/nodes/probe_sources.py --with-proxy      # 对照
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ACCESSPILOT_HOME", os.path.expandvars(r"%LOCALAPPDATA%\AccessPilot"))

from accesspilot import freenodes, paths  # noqa: E402
from accesspilot.util import http_request  # noqa: E402

#: 候选源。`gh/...` 走现有的镜像回退; `http...` 直接抓。
CANDIDATES: list[tuple[str, str]] = [
    # ---- 对照组: 已经在用的源, 用来看"净增"这个指标是否可信 ----
    ("[对照] peasoft/NoMoreWalls", "gh/peasoft/NoMoreWalls@master/list.txt"),
    ("[对照] Epodonios/v2ray-configs", "gh/Epodonios/v2ray-configs@main/All_Configs_Sub.txt"),

    # ---- GitHub 聚合候选 ----
    ("barry-far/V2ray-Config(单数)", "gh/barry-far/V2ray-Config@main/All_Configs_Sub.txt"),
    ("yebekhe/TelegramV2rayCollector", "gh/yebekhe/TelegramV2rayCollector@main/sub/normal/mix"),
    ("MatinGhanbari/v2ray-configs", "gh/MatinGhanbari/v2ray-configs@main/All_Configs_Sub.txt"),
    ("soroushmirzaei/tg-collector", "gh/soroushmirzaei/telegram-configs-collector@main/splitted/mixed"),
    ("mheidari98/.proxy", "gh/mheidari98/.proxy@main/all.txt"),
    ("Rokate/Proxy-Sub", "gh/Rokate/Proxy-Sub@main/v2ray.txt"),
    ("MrMohebi/xray-grabber", "gh/MrMohebi/xray-proxy-grabber-telegram@master/collected-proxies/row-url/all.txt"),
    ("mahdibland/ShadowsocksAggregator", "gh/mahdibland/ShadowsocksAggregator@master/Eternity"),
    ("Leon406/SubCrawler", "gh/Leon406/SubCrawler@main/sub/share.txt"),
    ("Surfboardv2ray/TGParse", "gh/Surfboardv2ray/TGParse@main/configs/sub.txt"),
    ("anaer/Sub(clash)", "gh/anaer/Sub@main/clash.yaml"),
    ("Jsnzkpg/Jsnzkpg", "gh/Jsnzkpg/Jsnzkpg@main/Jsnzkpg"),
    ("aiboboxx/clashfree", "gh/aiboboxx/clashfree@main/clash.yml"),
    ("ermaozi01/free_clash_vpn", "gh/ermaozi01/free_clash_vpn@main/README.md"),
    ("ALIILAPRO/v2rayNG-Config", "gh/ALIILAPRO/v2rayNG-Config@main/server.txt"),
    ("ndeal/v2ray", "gh/ndeal/v2ray@main/v2ray.txt"),
    ("V2RaySSR/Free-V2ray", "gh/V2RaySSR/Free-V2ray@master/README.md"),
    ("mermeroo/V2RAY-and-CLASH", "gh/mermeroo/V2RAY-and-CLASH-Configs@main/sub.txt"),
    ("ndsphonemy/proxy-sub", "gh/ndsphonemy/proxy-sub@main/README.md"),
    ("LearnHacking-net/Free-V2ray", "gh/LearnHacking-net/Free-V2ray-Config@main/README.md"),
    ("ts-sf/fly", "gh/ts-sf/fly@main/v2ray.txt"),
    ("hans-thomas/v2ray-subscription", "gh/hans-thomas/v2ray-subscription@main/v2ray"),
    ("ripaojiedian/freenode(v2)", "gh/ripaojiedian/freenode@main/v2"),
    ("mfuu/v2ray(mihomo)", "gh/mfuu/v2ray@master/mihomo.yaml"),
    ("v2rayfree/v2rayfree", "gh/v2rayfree/v2rayfree@main/v2"),
    ("free18/v2ray(yaml)", "gh/free18/v2ray@main/v.yaml"),
]

#: 国内直连的"每日文件"站 —— 这类最新鲜、最不依赖代理, 优先测。
DAILY_CANDIDATES: list[str] = [
    "https://nodefree.org/dy/{ym}/{ymd}.txt",
    "https://free.datiya.com/uploads/{ymd}.txt",
    "https://oneclash.cc/wp-content/uploads/{ym}/{ymd}.txt",
    "https://www.v2rayshare.com/wp-content/uploads/{ym}/{ymd}.txt",
    "https://node.freeclashnode.com/uploads/{ym}/{i}-{ymd}.txt",   # [对照] 已在用
]


def node_key(p: dict) -> str:
    """节点身份 —— 不含 name。两个节点只要连接参数一样就算同一个。"""
    d = {k: v for k, v in p.items() if k != "name"}
    return json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)


def existing_pool() -> tuple[set[str], int]:
    prof = paths.profiles_dir() / "free.json"
    if not prof.exists():
        return set(), 0
    data = json.loads(prof.read_text(encoding="utf-8"))
    px = data.get("proxies") or []
    return {node_key(p) for p in px}, len(px)


def fetch_daily(tpl: str) -> tuple[str, list[dict]]:
    """每日文件: 试最近 3 天 × 0~5 号。"""
    now = datetime.now()
    for back in range(3):
        d = now - timedelta(days=back)
        for i in range(6):
            url = tpl.format(ym=d.strftime("%Y%m"), ymd=d.strftime("%Y%m%d"), i=i)
            try:
                status, _, body = http_request(url, timeout=20)
            except Exception:
                continue
            if status != 200:
                continue
            text = body.decode("utf-8", errors="replace")
            if not text.strip():
                continue
            nodes = freenodes._extract(text, "daily")
            if nodes:
                return url, nodes
    return "", []


def probe(name: str, path: str, pool: set[str]) -> dict:
    try:
        if path.startswith("http"):
            url, nodes = fetch_daily(path)
            host = url.split("/")[2] if url else "-"
        else:
            text, host = freenodes._fetch_one(path, timeout=25)
            nodes = freenodes._extract(text, name)
        if not nodes:
            return {"name": name, "path": path, "ok": False, "why": "解析不出节点"}
        keys = {node_key(p) for p in nodes}
        new = keys - pool
        return {
            "name": name, "path": path, "ok": True, "host": host,
            "parsed": len(nodes), "uniq": len(keys), "new": len(new),
        }
    except Exception as e:
        return {"name": name, "path": path, "ok": False,
                "why": f"{type(e).__name__}: {str(e)[:60]}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--no-proxy", action="store_true", help="禁用系统代理(模拟用户真实处境)")
    g.add_argument("--with-proxy", action="store_true", help="走系统代理作对照")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    if a.no_proxy:
        # urllib 在 Windows 上默认会读注册表里的系统代理。要测"不开代理能不能下",
        # 必须把它按掉 —— 否则测出来的是"挂了代理能下", 那正是我们要避免的死循环。
        urllib.request.getproxies = lambda: {}  # type: ignore[assignment]
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(k, None)
        mode = "不开代理"
    elif a.with_proxy:
        mode = "走系统代理"
    else:
        mode = "系统默认"

    pool, pool_n = existing_pool()
    print(f"模式: {mode}   现有节点池: {pool_n} 个(去重键 {len(pool)})")
    print()

    jobs = [(n, p) for n, p in CANDIDATES] + [(f"[每日]{t}", t) for t in DAILY_CANDIDATES]
    results = []
    with futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(probe, n, p, pool): n for n, p in jobs}
        for f in futures.as_completed(futs):
            results.append(f.result())

    ok = [r for r in results if r["ok"]]
    ok.sort(key=lambda r: -r["new"])
    bad = [r for r in results if not r["ok"]]

    print(f"{'源':<34} {'镜像':<22} {'解析':>6} {'去重':>6} {'净增':>6}")
    print("-" * 80)
    for r in ok:
        print(f"{r['name'][:33]:<34} {str(r.get('host',''))[:21]:<22} "
              f"{r['parsed']:>6} {r['uniq']:>6} {r['new']:>6}")
    print()
    print(f"失败 {len(bad)} 个:")
    for r in sorted(bad, key=lambda r: r["name"]):
        print(f"  {r['name'][:44]:<46} {r.get('why','')}")
    print()
    total_new = sum(r["new"] for r in ok)
    print(f"所有可用源合计可带来的净增(未互相去重): {total_new}")
    if a.out:
        Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"原始结果 -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
