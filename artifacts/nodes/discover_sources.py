"""自动发现候选仓库里**真正的**订阅文件路径, 而不是靠猜。

## 为什么需要它

第一轮我手写了 28 个候选路径, 其中 24 个是 HTTP 404 —— 因为我是**猜**文件名
(`sub.txt` / `v2ray.txt` / `README.md` ...), 而这些仓库的目录结构和文件名各不相同。
猜路径既慢又不诚实: 404 只能说明"我猜错了", 不能说明"这个源没用"。

jsDelivr 有一个数据接口能直接列出某个仓库的文件树:
    https://data.jsdelivr.com/v1/packages/gh/<owner>/<repo>@<ref>
而且 jsDelivr 在国内可直连 —— 用来做发现是最合适的。

## 用法

    python artifacts/nodes/discover_sources.py --out artifacts/nodes/discovered.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import re
import urllib.request
from pathlib import Path

#: 候选仓库。前一批实测有边际贡献的已落库, 这里放的是**还没试过的**。
REPOS: list[str] = [
    "snakem982/proxypool",
    "vpei/Free-Node-Merge",
    "vpei/free-node",
    "chengaopan/AutoMergePublicNodes",
    "abshare/abshare.github.io",
    "Flik6/getNode",
    "Argh94/ProxyCollector",
    "MhdiTaheri/V2rayCollector",
    "zhangkaiitugithub/passcro",
    "mksshare/mksshare.github.io",
    "Leon406/SubCrawler",
    "yebekhe/TelegramV2rayCollector",
    "MatinGhanbari/v2ray-configs",
    "soroushmirzaei/telegram-configs-collector",
    "mheidari98/.proxy",
    "Rokate/Proxy-Sub",
    "MrMohebi/xray-proxy-grabber-telegram",
    "Surfboardv2ray/TGParse",
    "anaer/Sub",
    "Jsnzkpg/Jsnzkpg",
    "aiboboxx/clashfree",
    "ermaozi01/free_clash_vpn",
    "V2RaySSR/Free-V2ray",
    "mermeroo/V2RAY-and-CLASH-Configs",
    "ndsphonemy/proxy-sub",
    "ts-sf/fly",
    "v2rayfree/v2rayfree",
    "Alvin9999/new-pac",
    "hans-thomas/v2ray-subscription",
    "ALIILAPRO/v2rayNG-Config",     # 已收录, 作为对照看发现器准不准
    "Epodonios/v2ray-configs",     # 同上
]

#: 文件名里出现这些词, 就更可能是"节点清单"而不是规则/配置样例
GOOD_HINT = re.compile(
    r"(sub|v2ray|node|proxy|all|mix|list|config|clash|server|免费|节点)", re.I)
BAD_HINT = re.compile(r"(rule|geo|dns|script|test|example|sample|\.md$|\.json$)", re.I)
OK_EXT = (".txt", ".yaml", ".yml")


def _get(url: str, timeout: float = 25.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def flatten(node: dict, prefix: str = "") -> list[str]:
    out = []
    for f in node.get("files", []):
        name = f.get("name", "")
        if f.get("type") == "directory":
            out += flatten(f, f"{prefix}{name}/")
        else:
            out.append(f"{prefix}{name}")
    return out


def discover(repo: str) -> dict:
    for ref in ("", "@main", "@master"):
        url = f"https://data.jsdelivr.com/v1/packages/gh/{repo}{ref}"
        try:
            data = json.loads(_get(url).decode("utf-8", "replace"))
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:50]}"
            continue
        files = flatten(data)
        if not files:
            continue
        picks = [
            f for f in files
            if f.lower().endswith(OK_EXT)
            and GOOD_HINT.search(f)
            and not BAD_HINT.search(f)
            # 太深的多半是历史归档, 取浅的更稳
            and f.count("/") <= 3
        ]
        # 大的、名字像"全量/合并"的排前面
        picks.sort(key=lambda f: (
            -int(bool(re.search(r"(all|mix|merge|sub|total)", f, re.I))),
            f.count("/"), len(f)))
        return {"repo": repo, "ref": ref or "(default)", "files": len(files),
                "picks": picks[:6]}
    return {"repo": repo, "ref": "", "files": 0, "picks": [], "why": last}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="artifacts/nodes/discovered.json")
    a = ap.parse_args()

    rows = []
    with futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for r in ex.map(discover, REPOS):
            rows.append(r)

    total = 0
    for r in rows:
        if not r["picks"]:
            print(f"  {r['repo']:<46} 无候选  ({r.get('why','')})")
            continue
        total += len(r["picks"])
        print(f"  {r['repo']:<46} 共 {r['files']:>5} 个文件 -> 候选 {len(r['picks'])}")
        for p in r["picks"]:
            print(f"        {p}")
    print(f"\n合计候选路径: {total}")
    Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
