"""按**实际吞吐**而不是延迟挑节点。

## 为什么要单独做这件事

内核的 url-test 测的是**延迟**(TCP 握手 + 一个 204 响应), 而用户感受到的是
**吞吐**。这两件事在免费节点上几乎不相关 —— 实测过 246 ms 的节点只有 12 KB/s,
10 MB 下了 60 秒都没完。

所以"自动选择最快的节点"在用户体验上是错的: 它挑的是**响应最快**的, 不是
**下得动**的。

## 用法

    python artifacts/nodes/speedtest.py --bytes 3000000 --cap 15
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ACCESSPILOT_HOME", os.path.expandvars(r"%LOCALAPPDATA%\AccessPilot"))

from accesspilot import api  # noqa: E402
from accesspilot.state import load_state  # noqa: E402

NON_NODE = ("Selector", "URLTest", "Fallback", "LoadBalance", "Direct", "Reject",
            "Compatible", "Pass", "PassRule", "RejectDrop", "GLOBAL")

#: 测速目标。
#:
#: ⚠️ 别用 speed.cloudflare.com —— 它会给**反复请求**回 HTTP 429, 而 429 的响应体
#: 只有 1 字节, 于是测出来是"0 KB/s"。我第一版就是用它, 结果把测速目标打成了
#: 限流状态, 读到的数字全部不可信(还有一次读出 291 KB/s, 而同一节点换 cachefly
#: 实测只有 31 KB/s)。**测速工具本身被限流, 是最容易被忽略的假数据来源。**
TARGET = "https://cachefly.cachefly.net/10mb.test"


def measure(st, node: str, size: int, cap: float) -> dict:
    """把策略组切到这个节点, 下载 size 字节, 返回实测吞吐。"""
    try:
        api.select(st, "🚀 节点选择", node)
    except Exception as e:
        return {"node": node, "ok": False, "why": f"切换失败 {type(e).__name__}"}
    time.sleep(0.6)  # 给内核一点时间切过去

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{st.mixed_port}",
                                     "https": f"http://127.0.0.1:{st.mixed_port}"}))
    req = urllib.request.Request(TARGET, headers={"User-Agent": "Mozilla/5.0"})
    got, t0 = 0, time.time()
    try:
        with opener.open(req, timeout=cap) as r:
            while True:
                if time.time() - t0 > cap or got >= size:
                    break
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
    except Exception as e:
        el = time.time() - t0
        return {"node": node, "ok": False, "bytes": got,
                "kbps": round(got / el / 1024, 1) if el > 0 and got else 0,
                "why": f"{type(e).__name__}"}
    el = time.time() - t0
    return {"node": node, "ok": got > 0, "bytes": got,
            "kbps": round(got / el / 1024, 1), "secs": round(el, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bytes", type=int, default=3_000_000)
    ap.add_argument("--cap", type=float, default=15.0)
    ap.add_argument("--top", type=int, default=0, help="只测前 N 个(按延迟), 0=全测")
    ap.add_argument("--out", default="artifacts/nodes/speed.json")
    a = ap.parse_args()

    st = load_state()
    d = api.proxies(st)
    nodes = [k for k, v in d.items() if v.get("type") not in NON_NODE]
    # 按内核已测的延迟排序, 只测前面这些 —— 延迟太高的连握手都费劲
    def delay(n):
        h = (d[n].get("history") or [{}])[-1]
        return h.get("delay") or 99999
    nodes.sort(key=delay)
    if a.top:
        nodes = nodes[: a.top]

    print(f"逐节点实测吞吐: {len(nodes)} 个候选, 每个下 {a.bytes//1000} KB, 上限 {a.cap}s")
    print()
    rows = []
    for i, n in enumerate(nodes, 1):
        r = measure(st, n, a.bytes, a.cap)
        rows.append(r)
        flag = "OK " if r["ok"] else "×  "
        print(f"  [{i:>2}/{len(nodes)}] {flag} {n[:34]:<36} "
              f"{r['kbps']:>9.1f} KB/s  {r.get('why','')}")

    good = [r for r in rows if r["ok"] and r["kbps"] > 0]
    good.sort(key=lambda r: -r["kbps"])
    print()
    if good:
        print("按吞吐排序 (前 8):")
        for r in good[:8]:
            print(f"    {r['kbps']:>9.1f} KB/s  {r['node']}")
        best = good[0]["node"]
        api.select(st, "🚀 节点选择", best)
        print()
        print(f"已切到最快的: {best}  ({good[0]['kbps']} KB/s)")
    else:
        print("!! 没有一个节点下得动 —— 今天这批免费节点整体不可用")

    Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"明细 -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
