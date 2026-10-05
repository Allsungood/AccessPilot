"""对照实验: 用一个**已知能用**的节点验证测试装置本身是对的。

## 为什么必须做这一步

上一步测出"12 个住宅节点全部打不开"。但一个**全部为 0** 的结果, 有两种可能:

    1. 那 12 个节点确实都死了 (很可能 —— 免费节点里住宅 IP 的尤其短命);
    2. 我这个测试装置本身是坏的 (配置写错、端口选错、选择器没切过去…)。

两种情况的表象**完全一样**, 而结论天差地别。所以必须放一个**已知能用**的节点
进去 —— 它要是也测不出来, 那就说明是装置的问题, 前面那个结论作废。

这是今天反复踩到的同一个教训: "测不出来"不等于"不存在", 得先证明尺子是准的。
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

from accesspilot import paths  # noqa: E402

HERE = ROOT / "artifacts" / "nodes"


def main() -> int:
    prof = json.loads((paths.profiles_dir() / "free.json").read_text(encoding="utf-8"))
    px = prof.get("proxies") or []
    live = [p for p in px if "NL_198" in str(p.get("name"))]
    if not live:
        print("!! 当前配置里找不到对照组节点, 换一个")
        live = px[:1]
    print(f"对照组: {live[0].get('name')}")

    resi = json.loads((HERE / "residential_nodes.json").read_text(encoding="utf-8"))
    (HERE / "_resi_backup.json").write_text(
        json.dumps(resi, ensure_ascii=False), encoding="utf-8")

    combined = live[:1] + resi
    (HERE / "residential_nodes.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   写入 {len(combined)} 条 (1 对照 + {len(resi)} 候选)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
