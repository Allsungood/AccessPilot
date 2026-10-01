"""对抗性验收: 内核没起来时, 契约层不许抛异常, 必须返回带 error 的结果。"""
import sys, traceback
sys.path.insert(0, r"C:\Users\Administrator\AccessPilot")
from accesspilot import control

CASES = [
    ("snapshot()",        lambda: control.snapshot()),
    ("list_nodes()",      lambda: control.list_nodes(limit=5)),
    ("test_platforms()",  lambda: control.test_platforms(timeout=3)),
    ("verify_ai()",       lambda: control.verify_ai(timeout=3)),
    ("turn_on()",         lambda: control.turn_on()),
    ("turn_off()",        lambda: control.turn_off()),
    ("toggle()",          lambda: control.toggle()),
    ("set_mode('global')",lambda: control.set_mode("global")),
    ("select_node('x')",  lambda: control.select_node("x")),
    ("pick_best_node()",  lambda: control.pick_best_node()),
    ("set_auto_failover", lambda: control.set_auto_failover(False)),
    ("health_state()",    lambda: control.health_state()),
    ("autostart_status()",lambda: control.autostart_status()),
    ("tun_available()",   lambda: control.tun_available()),
]
raised = []
for name, fn in CASES:
    try:
        r = fn()
        if hasattr(r, "error"):
            desc = "error=%r" % (r.error[:50] if r.error else "")
        elif hasattr(r, "ok"):
            desc = "ok=%s detail=%r" % (r.ok, getattr(r, "detail", "")[:30])
        elif isinstance(r, list):
            desc = "%d 项" % len(r)
        else:
            desc = repr(r)[:50]
        print("  %-22s 返回正常  %s" % (name, desc))
    except Exception as e:
        raised.append((name, e))
        print("  %-22s !!! 抛异常 %s: %s" % (name, type(e).__name__, e))
print("")
print("抛异常的调用: %d 个" % len(raised))
sys.exit(1 if raised else 0)
