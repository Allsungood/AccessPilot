"""真对照: 证明「控制字符会让整份配置报废」这个失败模式确实存在,
以及我们的两层防线确实挡住了它。"""
import re, subprocess, tempfile, sys
from pathlib import Path
sys.path.insert(0, r"C:\Users\Administrator\AccessPilot")
from accesspilot import miniyaml, config

CORE = r"C:\Users\Administrator\AppData\Local\AccessPilot\core\mihomo.exe"
RUNTIME = r"C:\Users\Administrator\AppData\Local\AccessPilot\runtime"
MANGLED = "https://t.me/wangcai2" + "\xf0\x9f\x87\xa6".encode("latin-1").decode("latin-1")
ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

def validate(text, tag):
    p = Path(tempfile.gettempdir()) / ("ctl_%s.yaml" % tag)
    p.write_text(text, encoding="utf-8")
    r = subprocess.run([CORE, "-t", "-f", str(p), "-d", RUNTIME],
                       capture_output=True, text=True, timeout=120)
    c = r.stdout + r.stderr
    good = "test is successful" in c
    m = re.search(r'level=error msg="([^"]+)"', c)
    extra = ("  报错=" + m.group(1)[:60]) if m else ""
    print("  [%s] 非法字符=%d  内核校验=%s%s" % (tag, len(ILLEGAL.findall(text)),
                                                "通过" if good else "失败", extra))
    return good

base = "proxies:\n  - name: 正常节点\n    type: ss\n    server: 1.2.3.4\n    port: 443\n"

print("=== 对照组: 手工写入未转义的控制字符, 模拟修复前的生成器 ===")
bad = validate(base + '    sni: "' + MANGLED + '"\n', "broken")
print("  -> " + ("确实会报废, 失败模式真实存在" if not bad else "竟然通过了"))

print("")
print("=== 实验组: 走现在的 miniyaml 转义 ===")
escaped = miniyaml.dump({"proxies": [{"name": "正常节点", "type": "ss",
                                      "server": "1.2.3.4", "port": 443,
                                      "sni": MANGLED}]})
good = validate(escaped, "escaped")

print("")
print("=== 数据层: sanitize_proxies 会不会把垃圾 SNI 整个丢掉 ===")
out = config.sanitize_proxies([{"name": "n", "type": "ss", "server": "1.2.3.4",
                                "port": 443, "cipher": "aes-128-gcm",
                                "password": "p", "sni": MANGLED}])
print("  清洗后节点: %s" % (out[0] if out else "(被丢弃)"))
print("  sni 字段还在吗: %s  (应该是 False)" % ("sni" in out[0] if out else False))
print("")
print("结论: 修复前 %s 报废 -> 修复后 %s" % ("会" if not bad else "不会",
                                            "通过" if good else "失败"))
sys.exit(0 if (not bad and good) else 1)
