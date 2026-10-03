"""基线/净增测量脚本: 逐源抓取 + 去重后净增计算.

为什么不直接用 `free auto`: 它会写 `profiles/free.json` 并热重载内核, 而用户
此刻正在用这台机器。这里只读网络、只写 artifacts/nodes/。

关键点: **所有抓取都不走代理**(http_request 的 proxy 参数留空 =>
ProxyHandler({}) => 直连), 所以任何一条成功的结果都证明"不开代理也能下到"。

用法:
    python artifacts/nodes/measure.py baseline
    python artifacts/nodes/measure.py candidates cand2.txt
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from accesspilot import freenodes, subscription as sub_mod  # noqa: E402
from accesspilot.util import http_request  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
CACHE.mkdir(exist_ok=True)


def cache_key(url: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in url)[-120:]
    return CACHE / f"{abs(hash(url)) % 10**10}_{safe}"


def fetch_direct(url: str, *, timeout: float = 30.0, use_cache: bool = True) -> tuple[int, bytes]:
    """不开代理取 URL; 结果落盘缓存, 便于反复算净增而不重复打网络."""
    cp = cache_key(url)
    if use_cache and cp.exists():
        raw = cp.read_bytes()
        if raw.startswith(b"STATUS "):
            head, _, body = raw.partition(b"\n")
            return int(head.split()[1]), body
        return 200, raw
    try:
        status, _, body = http_request(url, timeout=timeout)
    except Exception:  # noqa: PERF203
        # 失败**不落盘**: 网络抖动导致的失败被缓存下来会污染后续所有净增计算
        return 0, b""
    if status == 200 and body:
        cp.write_bytes(b"STATUS 200\n" + body)
    return status, body or b""


def gh_path_to_urls(path: str) -> list[str]:
    return freenodes._urls_for(path)


def fetch_gh(path: str, *, timeout: float = 40.0) -> tuple[str, str, int]:
    """按生产顺序(jsDelivr -> ghfast -> gh-proxy -> raw)取一个 gh/ 路径."""
    last = ""
    for url in gh_path_to_urls(path):
        status, body = fetch_direct(url, timeout=timeout)
        if status == 200 and body.strip():
            return body.decode("utf-8", errors="replace"), url.split("/")[2], status
        last = f"HTTP {status}"
    return "", "", 0


def ident_set(proxies: list[dict]) -> set:
    return {sub_mod._ident(p) for p in proxies}


def sanitize_count(proxies: list[dict]) -> int:
    return len(freenodes.sanitize(proxies))


def cmd_baseline(argv: list[str]) -> int:
    """逐源抓取现有全部源, 落盘每个源的原始条数 + 命中镜像."""
    report: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "sources": [], "daily": []}
    all_nodes: list[dict] = []

    for name, path in freenodes.SOURCES:
        t0 = time.time()
        text, host, status = fetch_gh(path)
        n_raw, n_parsed = 0, 0
        if text:
            nodes = freenodes._extract(text, name)
            n_raw, n_parsed = len(text), len(nodes)
            all_nodes += nodes
        rec = {"name": name, "path": path, "via": host, "status": status,
               "bytes": n_raw, "parsed": n_parsed,
               "secs": round(time.time() - t0, 1)}
        report["sources"].append(rec)
        print(f"  {name:<34} parsed={n_parsed:>6}  via={host or '-':<22} {rec['secs']}s")
        Path(HERE / "baseline_sources.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    # 每日文件 + 文章页(目前国内直连状态也要如实记录)
    for url in freenodes.daily_file_urls():
        status, body = fetch_direct(url, timeout=20.0)
        nodes = freenodes._extract(body.decode("utf-8", errors="replace"), "daily") if status == 200 else []
        if nodes:
            all_nodes += nodes
        report["daily"].append({"url": url, "status": status, "parsed": len(nodes)})

    base = freenodes.sanitize(sub_mod.dedupe(all_nodes))
    report["collected"] = len(all_nodes)
    report["deduped_sanitized"] = len(base)
    report["idents"] = len(ident_set(base))
    Path(HERE / "baseline.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (HERE / "baseline_idents.json").write_text(
        json.dumps(sorted(str(i) for i in ident_set(base)), ensure_ascii=False), encoding="utf-8")
    print(f"\n合计抓到 {len(all_nodes)} -> 去重/过滤后 {len(base)}")
    return 0


def load_baseline_idents() -> set:
    p = HERE / "baseline_idents.json"
    if not p.exists():
        raise SystemExit("先跑: python artifacts/nodes/measure.py baseline")
    return set(json.loads(p.read_text(encoding="utf-8")))


def cmd_candidates(argv: list[str]) -> int:
    """对候选源逐条算净增(相对基线).

    候选行支持两种写法:
      * `gh/owner/repo@ref/path` —— 走生产的镜像回退链, 与真实抓取完全一致;
      * `https://...`            —— 直连 URL(用于每日文件类站点)。
    """
    urls = [ln.strip() for ln in Path(argv[0]).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    base = load_baseline_idents()
    out = []
    for item in urls:
        t0 = time.time()
        if item.startswith("gh/"):
            text, host, status = fetch_gh(item)
            body = text.encode("utf-8")
            label = f"{item} (via {host or '-'})"
        else:
            status, body = fetch_direct(item, timeout=40.0)
            label = item
        parsed = freenodes._extract(body.decode("utf-8", errors="replace"), item) if status == 200 else []
        clean = freenodes.sanitize(sub_mod.dedupe(parsed))
        idents = ident_set(clean)
        net = idents - base
        rec = {"item": item, "url": label, "status": status, "bytes": len(body),
               "parsed": len(parsed), "deduped": len(clean), "net_new": len(net),
               "secs": round(time.time() - t0, 1)}
        out.append(rec)
        print(f"  {status:>3} {rec['bytes']:>9}B  parsed={len(parsed):>6}  "
              f"dedup={len(clean):>6}  NET-NEW={len(net):>5}  {label}")
        Path(HERE / "candidates.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    if cmd == "baseline":
        raise SystemExit(cmd_baseline(sys.argv[2:]))
    if cmd == "candidates":
        raise SystemExit(cmd_candidates(sys.argv[2:]))
    raise SystemExit(f"未知命令: {cmd}")
