# -*- coding: utf-8 -*-
"""验收探针 2: 把 D(垃圾字段) 那一条做扎实 —— 用**真实节点形状**而不是手搓的.

并验证补救方案(在 sanitize 之后再做一次 uniquify_names)确实能救回整份配置。
一律渲染到临时文件, 不碰 runtime/config.yaml。
"""
from __future__ import annotations

import collections
import json
import os
import sys
import tempfile

sys.path.insert(0, r"C:\Users\Administrator\AccessPilot")

from pathlib import Path  # noqa: E402

from accesspilot import config  # noqa: E402
from accesspilot.state import load_state  # noqa: E402
from accesspilot.subscription import Subscription, load_profile, uniquify_names  # noqa: E402

OUT = []


def core_ok(cfg, tag):
    d = tempfile.mkdtemp(prefix="ap-v2-")
    t = Path(d) / "config.yaml"
    config.write_config(cfg, t)
    ok, out = config.test_config(t)
    key = [ln for ln in out.splitlines()
           if "level=error" in ln or "test failed" in ln or "test is successful" in ln][:4]
    try:
        os.remove(t)
        os.rmdir(d)
    except Exception:
        pass
    return ok, key


def main() -> int:
    st = load_state()
    st.accel_enable = False
    sub = load_profile("free")

    # ---- 真实节点形状: 每种类型取一个真节点 ----
    by_type = {}
    for p in sub.proxies:
        by_type.setdefault(str(p.get("type")), p)
    OUT.append({
        "real_types": {k: sorted(v.keys()) for k, v in sorted(by_type.items())},
        "type_counts": dict(collections.Counter(str(p.get("type")) for p in sub.proxies)),
    })

    # 每种类型各取 1 个真节点 -> 渲染 -> 内核收不收(应当收)
    one_each = list(by_type.values())
    ok, key = core_ok(config.build_config(Subscription(name="one", proxies=one_each), st),
                      "one-each")
    OUT.append({"case": "每种类型各取 1 个真实节点", "n": len(one_each),
                "core_accepts": ok, "key": key})

    # ---- D2: 真实 vmess 节点去掉 uuid / 真实节点加一个不存在的字段 ----
    vm = by_type.get("vmess")
    if vm:
        bad = dict(vm)
        bad.pop("uuid", None)
        bad["name"] = "vmess-no-uuid"
        ok, key = core_ok(config.build_config(
            Subscription(name="vm", proxies=[bad]), st), "vmess-no-uuid")
        OUT.append({"case": "真实 vmess 去掉 uuid", "core_accepts": ok, "key": key})

    # ---- 关键验证: 现有坏配置档 + 一次名字去重 -> 能不能救回来 ----
    deduped = uniquify_names([dict(p) for p in sub.proxies])
    ok, key = core_ok(config.build_config(Subscription(name="fixed", proxies=deduped), st),
                      "dedup")
    OUT.append({
        "case": "现有 free.json 经 uniquify_names() 去重后渲染",
        "core_accepts": ok, "key": key,
    })
    # 排在最前的那种做法: 先 sanitize 再造唯一名
    san = config.sanitize_proxies([dict(p) for p in sub.proxies])
    fixed = uniquify_names(san)
    ok, key = core_ok(config.build_config(Subscription(name="fixed2", proxies=fixed), st),
                      "dedup2")
    OUT.append({
        "case": "sanitize_proxies() 之后再 uniquify_names() 渲染",
        "core_accepts": ok, "key": key,
    })

    print(json.dumps(OUT, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
