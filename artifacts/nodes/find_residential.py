"""把扫描出来的"住宅/移动 IP"节点捞出来, 看有没有真能用的。

## 为什么要单独捞

`scan_ip_types.py` 扫的是**所有源的全部 server**, 而当前配置是 `free auto` 筛完的
产物 —— 住宅 IP 的节点很可能因为延迟高或某个平台验不过而被筛掉了。所以要回到
原始节点集合里按 IP 反查。

## 判据

捞出来只是第一步, **能不能用要另外测**: 免费节点里挂着一堆"看着漂亮但已经死了"
的地址, 而住宅 IP 的节点尤其容易死(它就是某个人家里的机器, 关机就没了)。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "artifacts" / "nodes"))
os.environ.setdefault("ACCESSPILOT_HOME", os.path.expandvars(r"%LOCALAPPDATA%\AccessPilot"))

from accesspilot import freenodes, paths  # noqa: E402

#: 只留**境外**的真实住宅/移动 ISP。国内的中国移动地址要排除 ——
#: 拿它当出口等于没翻墙, 对访问 Google 毫无用处。
KEEP_ISP = (
    "Charter", "Cox Communications", "TELUS", "VNPT", "IDC Frontier",
    "EN Technologies", "Bangmod", "Comcast", "Verizon", "AT&T", "Bell Canada",
    "Rogers", "CenturyLink", "Frontier", "Windstream", "Mediacom", "KDDI",
    "SoftBank", "NTT", "Singtel", "StarHub", "True Internet", "AIS",
)
DROP_COUNTRY = ("China", "Hong Kong", "Macau")


def main() -> int:
    types = json.loads(Path("artifacts/nodes/ip_types.json").read_text(encoding="utf-8"))
    resi = [r for r in (types["clean"] + types["mobile"])
            if any(k.lower() in (r.get("isp") or "").lower() for k in KEEP_ISP)
            and (r.get("country") or "") not in DROP_COUNTRY]
    want = {r["query"]: r for r in resi}
    print(f"候选住宅/移动 IP: {len(want)} 个")
    for ip, r in sorted(want.items()):
        print(f"   {ip:<16} {r.get('country','?'):<14} {(r.get('isp') or '')[:34]}")

    print("\n抓所有源, 按 server 反查 ...")
    servers, n_nodes = __import__("scan_ip_types").collect_servers(False)
    print(f"   共 {n_nodes} 个节点, {len(servers)} 个唯一 server")

    hits: list[dict] = []
    for job in list(freenodes.SOURCES) + [
            (f"[每日]{t}", t) for t in freenodes.DAILY_FILE_TEMPLATES]:
        pass  # collect_servers 已经抓过一遍; 这里只做反查
    # 重新抓一遍拿节点对象(collect_servers 只回了 server 集合)
    import concurrent.futures as futures

    def grab(item):
        name, path = item
        try:
            if path.startswith("http"):
                return []
            body, _ = freenodes._fetch_one(path, timeout=30)
            return freenodes._extract(body, name)
        except Exception:
            return []

    jobs = list(freenodes.SOURCES)
    with futures.ThreadPoolExecutor(max_workers=12) as ex:
        for got in ex.map(grab, jobs):
            for p in got:
                if str(p.get("server") or "") in want:
                    hits.append(p)

    print(f"\n命中节点: {len(hits)} 个")
    seen = set()
    for p in hits:
        key = (str(p.get("server")), p.get("port"))
        if key in seen:
            continue
        seen.add(key)
        r = want[str(p.get("server"))]
        print(f"   {str(p.get('name'))[:30]:<32} {p.get('type'):<9} "
              f"{p.get('server')}:{p.get('port')}  [{r.get('country')} {(r.get('isp') or '')[:22]}]")

    out = Path("artifacts/nodes/residential_nodes.json")
    out.write_text(json.dumps(hits, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
