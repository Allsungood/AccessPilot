"""测候选源相对「现有全部源的并集」的**边际贡献**。

## 为什么不能用 free.json 当基线

`profiles/free.json` 是 `free auto` 跑完 **TCP 预筛 + 失效剔除** 之后的产物,
它是个**子集**, 不是"我们已经拥有的全部节点"。拿它当基线会得出荒唐的结论:
一个本来就在用的源, 因为它的节点大部分被预筛掉了, 会被算成"净增 3000+"。

真正有意义的问题是: **这个源能带来多少我们现在没有的节点?**
所以要拿"现有全部源的并集"当基线。

## 用法

    python artifacts/nodes/measure_gain.py --no-proxy
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

from accesspilot import freenodes  # noqa: E402
from accesspilot.util import http_request  # noqa: E402
sys.path.insert(0, str(ROOT / "artifacts" / "nodes"))
from probe_sources import CANDIDATES, DAILY_CANDIDATES, node_key  # noqa: E402


def fetch_daily(tpl: str) -> list[dict]:
    now = datetime.now()
    for back in range(3):
        d = now - timedelta(days=back)
        for i in range(6):
            url = tpl.format(ym=d.strftime("%Y%m"), ymd=d.strftime("%Y%m%d"), i=i)
            try:
                status, _, body = http_request(url, timeout=20)
            except Exception:
                continue
            if status != 200 or not body.strip():
                continue
            nodes = freenodes._extract(body.decode("utf-8", errors="replace"), "daily")
            if nodes:
                return nodes
    return []


def keys_of(name: str, path: str) -> set[str]:
    try:
        if path.startswith("http"):
            nodes = fetch_daily(path)
        else:
            text, _ = freenodes._fetch_one(path, timeout=30)
            nodes = freenodes._extract(text, name)
        return {node_key(p) for p in nodes}
    except Exception:
        return set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--out", default="artifacts/nodes/gain.json")
    ap.add_argument("--discovered", default="",
                    help="用 discover_sources.py 的产物当候选(而不是手写清单)")
    a = ap.parse_args()

    if a.no_proxy:
        urllib.request.getproxies = lambda: {}  # type: ignore[assignment]
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(k, None)

    # ---- 1. 现有全部源 -> 并集 ----
    existing_jobs = [(n, p) for n, p in freenodes.SOURCES]
    existing_jobs += [(f"[每日]{t}", t) for t in freenodes.DAILY_FILE_TEMPLATES]
    existing_jobs += [(n, u) for n, u in
                      [(n, t.format(date=datetime.now().strftime("%Y-%m-%d")))
                       for n, t in freenodes.ARTICLE_SOURCES]]

    print(f"[1/3] 抓现有 {len(existing_jobs)} 个源, 建并集 ...")
    existing_union: set[str] = set()
    per_existing = {}
    with futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(keys_of, n, p): (n, p) for n, p in existing_jobs}
        for f in futures.as_completed(futs):
            n, p = futs[f]
            ks = f.result()
            per_existing[n] = len(ks)
            existing_union |= ks
    print(f"      现有源并集: {len(existing_union)} 个唯一节点")
    print()

    # ---- 2. 每个候选相对现有并集的边际 ----
    if a.discovered:
        # 路径来自 jsDelivr 的文件树, 不是猜的 —— 上一轮手写 28 个候选里 24 个
        # 是 404, 那种"测不出来"证明不了源没用, 只证明我猜错了文件名。
        disc = json.loads(Path(a.discovered).read_text(encoding="utf-8"))
        cand_jobs = []
        for r in disc:
            ref = r.get("ref") or ""
            ref = "" if ref.startswith("(") else ref
            for pick in r.get("picks", []):
                cand_jobs.append((f"{r['repo']}/{pick}",
                                  f"gh/{r['repo']}{ref}/{pick}"))
    else:
        cand_jobs = list(CANDIDATES) + [(f"[每日]{t}", t) for t in DAILY_CANDIDATES]
    print(f"[2/3] 测 {len(cand_jobs)} 个候选的边际贡献 ...")
    rows = []
    with futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(keys_of, n, p): (n, p) for n, p in cand_jobs}
        for f in futures.as_completed(futs):
            n, p = futs[f]
            ks = f.result()
            rows.append({"name": n, "path": p, "uniq": len(ks),
                         "marginal": len(ks - existing_union),
                         "overlap": len(ks & existing_union)})
    rows.sort(key=lambda r: -r["marginal"])

    print()
    print(f"{'候选源':<36} {'去重':>6} {'已有':>6} {'边际':>6}")
    print("-" * 62)
    for r in rows:
        if r["uniq"] == 0:
            continue
        print(f"{r['name'][:35]:<36} {r['uniq']:>6} {r['overlap']:>6} {r['marginal']:>6}")
    print()

    # ---- 3. 全加进去之后的总并集 ----
    total = set(existing_union)
    for r in rows:
        pass  # 已经在上面算出 marginal, 但要算"全加"需要真实并集
    print("[3/3] 计算全加之后的并集 ...")
    all_union = set(existing_union)
    with futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(keys_of, r["name"], r["path"]) for r in rows if r["uniq"] > 0}
        for f in futures.as_completed(futs):
            all_union |= f.result()

    print()
    print("=" * 62)
    print(f"现有源并集          : {len(existing_union)}")
    print(f"全部候选都加上之后  : {len(all_union)}")
    print(f"总增益              : +{len(all_union) - len(existing_union)}")
    print("=" * 62)

    Path(a.out).write_text(json.dumps(
        {"existing_union": len(existing_union), "all_union": len(all_union),
         "rows": rows, "per_existing": per_existing},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细 -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
