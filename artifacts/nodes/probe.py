"""一次性探测脚本: 不开代理地抓候选源, 报告条数与解析结果.

刻意**不**走 `accesspilot free auto`, 也不碰 `profiles/free.json` —— 用户此刻
正在用这台机器上网, 任何热重载都会打断他。

用法:
    python artifacts/nodes/probe.py urls.txt
    python artifacts/nodes/probe.py --inline https://a https://b
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from accesspilot import freenodes  # noqa: E402
from accesspilot.util import http_request  # noqa: E402

OUT = Path(__file__).resolve().parent / "probe_results.json"


def probe(url: str, timeout: float = 20.0) -> dict:
    """不开代理地取一个 URL, 返回原始条数与解析条数."""
    rec: dict = {"url": url, "status": 0, "bytes": 0, "nodes": 0,
                 "proxy_used": False, "error": "", "secs": 0.0}
    t0 = time.time()
    try:
        # http_request(proxy=None) -> ProxyHandler({}) -> 直连, 不走系统代理
        status, _, body = http_request(url, timeout=timeout)
        rec["status"] = status
        rec["bytes"] = len(body or b"")
        if status == 200 and body:
            text = body.decode("utf-8", errors="replace")
            nodes = freenodes._extract(text, url)
            rec["nodes"] = len(nodes)
    except Exception as e:  # noqa: PERF203
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["secs"] = round(time.time() - t0, 2)
    return rec


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--inline":
        urls = argv[1:]
    else:
        urls = [ln.strip() for ln in Path(argv[0]).read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for rec in ex.map(probe, urls):
            results.append(rec)
            print(f"{rec['status']:>4} {rec['bytes']:>9} B  {rec['nodes']:>6} nodes  "
                  f"{rec['secs']:>6.2f}s  {rec['url']}"
                  + (f"   !! {rec['error']}" if rec["error"] else ""))
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n-> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
