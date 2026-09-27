"""从缓存续跑: 平台验证 -> 只留真正可用的 -> 设自动选择链条.

用于 free auto 被外部打断(超时)后从测速结果处继续, 避免重跑 2 分钟测速。
"""
import sys
import time

sys.path.insert(0, r"C:\Users\Administrator\AccessPilot")

from accesspilot import api, freenodes, rules  # noqa: E402
from accesspilot.state import load_state, save_state  # noqa: E402
from accesspilot.subscription import load_profile  # noqa: E402
from accesspilot.util import json_dump, json_load, ok, warn  # noqa: E402
from accesspilot import paths, process  # noqa: E402

st = load_state()
data = json_load(paths.cache_dir() / "free_latency.json", {}) or {}
alive = {k: v for k, v in (data.get("alive") or {}).items() if v > 0}
if not alive:
    print("缓存里没有测速结果, 请先跑 accesspilot free test")
    sys.exit(1)

candidates = [n for n, d in sorted(alive.items(), key=lambda kv: kv[1]) if d <= 4000][:25]
print(f"测速存活 {len(alive)} 个, 平台验证候选 {len(candidates)} 个 (延迟<=4000ms)")
if not candidates:
    print("所有存活节点都超过 4000ms, 免费池当前质量很差")
    sys.exit(1)

t0 = time.time()
results = freenodes.verify_many(st, candidates, timeout=8.0)
good = [r for r in results if freenodes.fully_usable(r)]
good.sort(key=lambda r: r.get("latency_ms") or 99999)
print(f"验证完成 {time.time()-t0:.0f}s: {len(good)}/{len(candidates)} 真正可用")
for r in good:
    print(f"  可用 {r.get('latency_ms')}ms  {r['name']}")

if not good:
    warn("本轮全部不可用, 免费节点质量波动, 建议过几小时重试 accesspilot free auto")
    sys.exit(1)

# 只保留验证通过的
keep = {r["name"]: max(int(r.get("latency_ms") or 1), 1) for r in good}
keep = dict(sorted(keep.items(), key=lambda kv: kv[1]))
before, after = freenodes.prune_profile("free", keep)
ok(f"已清理节点: {before} -> {after}")

try:
    api.select(st, rules.G_SELECT, rules.G_AUTO)
except Exception:
    pass
for g in (rules.G_AI, rules.G_SOCIAL, rules.G_MEDIA):
    try:
        api.select(st, g, rules.G_SELECT)
    except Exception:
        pass
save_state(st)
try:
    process.reload_config(load_profile(st.active_profile), st)
except Exception as e:
    print("热重载失败:", e)
best = next(iter(keep))
ok(f"策略组已指向自动选择(当前最快: {best}, {keep[best]} ms)")
