"""扫节点 IP 的"身份" —— 找出不是机房段的那些。

## 为什么要做这件事

Google / X 对**新账号注册**的风控里, 权重最大的一项是出口 IP 的类型:

    hosting: true   -> 机房/VPS 段。注册基本直接被拒, 或掉进无解的验证码循环。
    mobile:  true   -> 移动运营商段。最容易被放行。
    两者都 false    -> 住宅/企业宽带。也很好。

而**公开分享的免费节点几乎全是 VPS** —— 所以这个扫描的期望值不高。
但它值得做: 真有那么一个住宅节点, 就顶得上试一百次。

## 为什么扫"原始池"而不是当前配置

当前配置是 `free auto` 跑完 **TCP 预筛 + 平台验证 + 失效剔除** 之后的产物, 通常
只剩几十个甚至十几个节点。而住宅 IP 的节点很可能**因为延迟高或某个平台验不过
而被筛掉了** —— 在剩下的十几个里找, 等于在别人挑完的菜叶里找肉。

所以这里直接把所有源抓一遍, 拿**全部** server 地址去查类型。

## 数据源

ip-api.com 的批量接口: POST http://ip-api.com/batch, 一次最多 100 个 IP,
免费额度 45 次/分钟。批量请求按 1 次计, 所以 5000 个 IP 只要 50 次 —— 够用。
(注意免费额度只支持 http, 不支持 https。)
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import ipaddress
import json
import os
import socket
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ACCESSPILOT_HOME", os.path.expandvars(r"%LOCALAPPDATA%\AccessPilot"))

from accesspilot import freenodes  # noqa: E402
from accesspilot.util import http_request  # noqa: E402

BATCH = "http://ip-api.com/batch"
FIELDS = "status,country,isp,org,as,mobile,proxy,hosting,query"


def collect_servers(with_proxy: bool) -> tuple[set[str], int]:
    """把所有源抓一遍, 返回 (唯一 server 集合, 节点总数)。"""
    jobs: list[tuple[str, str]] = list(freenodes.SOURCES)
    jobs += [(f"[每日]{t}", t) for t in freenodes.DAILY_FILE_TEMPLATES]
    jobs += [(n, t.format(date=datetime.now().strftime("%Y-%m-%d")))
             for n, t in freenodes.ARTICLE_SOURCES]

    nodes: list[dict] = []
    lock = __import__("threading").Lock()

    def grab(item: tuple[str, str]) -> None:
        name, path = item
        try:
            if path.startswith("http"):
                now = datetime.now()
                text = ""
                for back in range(3):
                    d = now - timedelta(days=back)
                    for i in range(6):
                        url = path.format(ym=d.strftime("%Y%m"), ymd=d.strftime("%Y%m%d"),
                                          i=i)
                        try:
                            st, _, body = http_request(url, timeout=20)
                        except Exception:
                            continue
                        if st == 200 and body.strip():
                            text = body.decode("utf-8", errors="replace")
                            break
                    if text:
                        break
                got = freenodes._extract(text, name) if text else []
            else:
                body, _ = freenodes._fetch_one(path, timeout=30)
                got = freenodes._extract(body, name)
        except Exception:
            return
        with lock:
            nodes.extend(got)
        print(f"   [{len(nodes):>6}] {name[:40]}", flush=True)

    with futures.ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(grab, jobs))

    servers = {str(p.get("server") or "").strip() for p in nodes}
    servers.discard("")
    return servers, len(nodes)


def to_ips(servers: set[str]) -> dict[str, str]:
    """把 server 归一成 IP。域名做 DNS 解析(节点地址本来就是本地解析的)。"""
    out: dict[str, str] = {}
    domains: list[str] = []
    for s in servers:
        try:
            ipaddress.ip_address(s)
            out[s] = s
        except ValueError:
            domains.append(s)

    print(f"   已是 IP: {len(out)}   需要 DNS: {len(domains)}", flush=True)

    def resolve(d: str) -> tuple[str, str]:
        try:
            return d, socket.gethostbyname(d)
        except Exception:
            return d, ""

    done = 0
    with futures.ThreadPoolExecutor(max_workers=60) as ex:
        for d, ip in ex.map(resolve, domains):
            done += 1
            if ip:
                out[d] = ip
            if done % 200 == 0:
                print(f"      DNS {done}/{len(domains)}", flush=True)
    return out


def query_batch(ips: list[str]) -> list[dict]:
    body = json.dumps([{"query": ip, "fields": FIELDS} for ip in ips]).encode()
    req = urllib.request.Request(
        BATCH, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="artifacts/nodes/ip_types.json")
    a = ap.parse_args()

    print("[1/4] 抓所有源 ...")
    servers, n_nodes = collect_servers(False)
    print(f"   节点 {n_nodes} 个, 唯一 server {len(servers)} 个")

    print("[2/4] 归一成 IP ...")
    mapping = to_ips(servers)
    uniq = sorted(set(mapping.values()))
    print(f"   唯一 IP: {len(uniq)}")

    print("[3/4] 批量查 IP 类型 ...")
    results: list[dict] = []
    for i in range(0, len(uniq), 100):
        chunk = uniq[i:i + 100]
        try:
            results.extend(query_batch(chunk))
        except Exception as e:
            print(f"      批次 {i//100} 失败: {type(e).__name__}", flush=True)
        if (i // 100) % 5 == 0:
            print(f"      {min(i+100, len(uniq))}/{len(uniq)}", flush=True)
        time.sleep(1.4)          # 45 次/分钟的额度, 留足余量

    print("[4/4] 统计 ...")
    ok = [r for r in results if r.get("status") == "success"]
    hosting = [r for r in ok if r.get("hosting")]
    mobile = [r for r in ok if r.get("mobile")]
    clean = [r for r in ok if not r.get("hosting") and not r.get("proxy")]

    print()
    print("=" * 66)
    print(f"查到类型        : {len(ok)}")
    print(f"机房段 (hosting): {len(hosting)}   ({100*len(hosting)//max(len(ok),1)}%)")
    print(f"移动段 (mobile) : {len(mobile)}")
    print(f"**非机房非代理** : {len(clean)}")
    print("=" * 66)

    if clean:
        print("\n非机房段 IP:")
        for r in sorted(clean, key=lambda r: (not r.get("mobile"), r.get("country") or "")):
            tag = "移动" if r.get("mobile") else "住宅/企业"
            print(f"   {tag:>9}  {r['query']:<16} {r.get('country','?'):<14} "
                  f"{(r.get('isp') or '')[:30]}")

    Path(a.out).write_text(json.dumps(
        {"total": len(ok), "clean": clean, "mobile": mobile,
         "server_to_ip": mapping, "all": results},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细 -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
