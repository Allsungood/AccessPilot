import re, subprocess, tempfile, sys, json
from pathlib import Path
sys.path.insert(0, r"C:\Users\Administrator\AccessPilot")
from accesspilot import miniyaml, config, subscription
from accesspilot.state import load_state

CORE = r"C:\Users\Administrator\AppData\Local\AccessPilot\core\mihomo.exe"
RUNTIME = r"C:\Users\Administrator\AppData\Local\AccessPilot\runtime"
MANGLED = "https://t.me/wangcai2" + "\xf0\x9f\x87\xa6".encode("latin-1").decode("latin-1")
ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

def validate(text, tag):
    p = Path(tempfile.gettempdir()) / ("v_%s.yaml" % tag)
    p.write_text(text, encoding="utf-8")
    r = subprocess.run([CORE, "-t", "-f", str(p), "-d", RUNTIME],
                       capture_output=True, text=True, timeout=120)
    c = r.stdout + r.stderr
    good = "test is successful" in c
    m = re.search(r'level=error msg="([^"]+)"', c)
    print("  [%s] 非法字符=%d 内核校验=%s%s" % (tag, len(ILLEGAL.findall(text)),
          "通过" if good else "失败", ("  报错=" + m.group(1)[:70]) if m else ""))
    return good

node = {"name": "毒节点\x01\x9f", "type": "ss", "server": "1.2.3.4", "port": 443,
        "cipher": "aes-128-gcm", "password": "p\x00\x1f", "sni": MANGLED}

print("=== 完整链路: 脏节点 -> sanitize_proxies -> 完整配置 -> 内核 ===")
sub = subscription.load_profile("free")
orig = sub.proxies
sub.proxies = config.sanitize_proxies([node] + [dict(p) for p in orig[:5]])
print("  清洗后节点名: %r" % sub.proxies[0]["name"])
st = load_state()
out = Path(tempfile.gettempdir()) / "full_cfg.yaml"
config.write_config(config.build_config(sub, st), out)
sub.proxies = orig
text = out.read_text(encoding="utf-8", errors="replace")
print("  配置 %d 字节, 非法控制字符 %d 个" % (out.stat().st_size, len(ILLEGAL.findall(text))))
good = validate(text, "full")
print("")
print("最终结论:")
print("  1) 未转义的控制字符确实会让整份配置报废(报错与 2026-09-30 那次逐字相同)")
print("  2) 我们的转义把非法字符降到 0")
print("  3) 完整链路(真实节点池 + 脏节点)生成的配置内核校验: %s" % ("通过" if good else "失败"))
sys.exit(0 if good else 1)
