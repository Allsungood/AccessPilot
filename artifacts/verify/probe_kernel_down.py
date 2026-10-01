# -*- coding: utf-8 -*-
"""验收探针: 内核**未运行**时, control.py 契约层的每个入口是否抛异常?

契约(control.py 头部第 2 条): 可预期的失败一律返回带 `error` 字段的结果, 不抛。
用法: python artifacts/verify/probe_kernel_down.py
"""
from __future__ import annotations

import json
import sys
import time
import traceback

sys.path.insert(0, r"C:\Users\Administrator\AccessPilot")

from accesspilot import control, process  # noqa: E402


def probe(label, fn, *a, **kw):
    t0 = time.time()
    try:
        r = fn(*a, **kw)
    except BaseException as e:  # noqa: BLE001
        return {
            "label": label,
            "raised": True,
            "exc": f"{type(e).__name__}: {e}",
            "tb": traceback.format_exc()[-1200:],
            "ms": int((time.time() - t0) * 1000),
        }
    if hasattr(r, "to_dict"):
        r = r.to_dict()
    elif isinstance(r, list):
        r = [x.to_dict() if hasattr(x, "to_dict") else x for x in r]
    return {
        "label": label,
        "raised": False,
        "result": r,
        "ms": int((time.time() - t0) * 1000),
    }


def main() -> int:
    out = {
        "process_is_running": None,
        "probes": [],
    }
    try:
        out["process_is_running"] = process.is_running()
    except BaseException as e:  # noqa: BLE001
        out["process_is_running"] = f"RAISED {type(e).__name__}: {e}"

    out["probes"].append(probe("control.snapshot()", control.snapshot))
    out["probes"].append(probe("control.list_nodes()", control.list_nodes))
    out["probes"].append(probe("control.list_nodes(limit=200, alive_only=True)",
                               control.list_nodes, limit=200, alive_only=True))
    out["probes"].append(probe("control.test_platforms(timeout=2)",
                               control.test_platforms, timeout=2.0))
    out["probes"].append(probe("control.verify_ai(timeout=3)",
                               control.verify_ai, timeout=3.0))
    out["probes"].append(probe("control.set_mode('global')", control.set_mode, "global"))
    out["probes"].append(probe("control.set_mode('bogus')", control.set_mode, "bogus"))
    out["probes"].append(probe("control.select_node('nope')", control.select_node, "nope"))
    out["probes"].append(probe("control.pick_best_node()", control.pick_best_node))
    out["probes"].append(probe("control.health_state()", control.health_state))
    out["probes"].append(probe("control.autostart_status()", control.autostart_status))
    out["probes"].append(probe("control.list_profiles_nodes('free')",
                               control.list_profiles_nodes, "free"))
    out["probes"].append(probe("control.list_profiles_nodes('')",
                               control.list_profiles_nodes, ""))
    out["probes"].append(probe("control.set_tun(False)", control.set_tun, False))
    out["probes"].append(probe("control.tun_available()", control.tun_available))
    # toggle() 在断开状态下 = turn_on(), 这是恢复路径的最后手段, 单独看
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
