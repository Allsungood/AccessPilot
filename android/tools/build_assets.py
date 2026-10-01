"""从桌面端已经验证过的配置生成 Android 端要用的资源.

为什么要这么做, 而不是在 Kotlin 里重写一遍配置生成
====================================================
`accesspilot/config.py` + `rules.py` 那份配置是**真机验证过的**: 策略组、
rule-providers、DNS 防污染、fake-ip、以及那次 6027 个节点整份报废之后加的
两层转义 —— 这些坑都踩过一遍了。在安卓端用 Kotlin 重写一遍, 等于把同样的坑
再踩一次, 而且两边会慢慢漂移(桌面改了规则, 安卓没跟上)。

所以走"模板 + 运行时注入":
    config.template.yaml   <- 这里生成, 除了三个占位符之外和桌面端一致
    nodes.yaml             <- 节点列表, 由桌面端挑好的可用节点
    安卓端运行时只做替换:   {{FD}} / {{SECRET}} / {{PROXIES}}

桌面端改了规则, 重跑一次这个脚本就行, 安卓端零改动。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from accesspilot import config, paths, subscription  # noqa: E402
from accesspilot.state import load_state  # noqa: E402

#: 安卓端 assets 目录
ASSETS = ROOT / "android" / "app" / "src" / "main" / "assets"

#: 必须随包的 geodata —— 缺了内核会直接 exit 1, 所以不能依赖首启下载。
#: (国内网络下 GitHub 本来就常常拿不到, 首启下载是"连不上就永远起不来"。)
GEODATA = ("geosite.dat", "geoip.metadb", "country.mmdb")

#: 安卓端的 tun 段。和桌面端唯一的实质区别就是 file-descriptor ——
#: VpnService 建好 TUN 之后把 fd 号通过配置交给内核, 内核不去自己建设备。
#: 这样就不需要 root、不需要额外驱动、也不需要 NDK 编 tun2socks。
ANDROID_TUN = """tun:
  enable: true
  stack: mixed
  device: hongxing
  # 由 VpnService.establish() 拿到的 fd, 运行时注入
  file-descriptor: {{FD}}
  auto-route: false
  auto-detect-interface: false
  dns-hijack:
    - "any:53"
    - "tcp://any:53"
  mtu: 8500
  strict-route: false
  endpoint-independent-nat: false
"""


def build_template(sub: subscription.Subscription, st) -> str:
    """把完整配置渲染成安卓模板。"""
    cfg = config.build_config(sub, st)

    # 1) tun 段换成安卓版(fd 模式)
    text = config.miniyaml.dump(cfg)
    text = re.sub(r"^tun:\n(?:[ \t]+.*\n|\n)*", ANDROID_TUN, text, count=1, flags=re.M)

    # 2) 控制端口: 安卓端也走 127.0.0.1, 端口固定 9090 方便界面找它。
    #    secret 每次安装随机, 由运行时注入 —— 不能把开发机上的密钥打进 APK。
    text = re.sub(r'^secret: .*$', 'secret: "{{SECRET}}"', text, count=1, flags=re.M)

    # 3) DNS 里如果有监听地址, 安卓上要绑到本机
    text = re.sub(r'^listen: 0\.0\.0\.0:(.*)$', r'listen: 127.0.0.1:\1', text, flags=re.M)

    # 4) 节点列表整体换成占位符 —— 节点是运行时注入的, 不编进模板。
    #    占位符必须顶格且是合法 YAML: 我们让它替换整个 proxies 块。
    text = re.sub(r"^proxies:\n(?:[ \t]+.*\n|\n)*",
                  "proxies:\n{{PROXIES}}\n", text, count=1, flags=re.M)

    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 Android 端资源")
    ap.add_argument("--profile", default="", help="配置档名, 默认用当前启用的")
    ap.add_argument("--out", default=str(ASSETS), help="输出目录")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "ruleset").mkdir(exist_ok=True)

    st = load_state()
    name = args.profile or st.active_profile
    if not name:
        print("[x] 没有启用中的配置档")
        return 1
    sub = subscription.load_profile(name)
    print(f"[i] 配置档 {name}: {len(sub.proxies)} 个节点")

    # --- 配置模板 ---
    tpl = build_template(sub, st)
    (out / "config.template.yaml").write_text(tpl, encoding="utf-8")
    print(f"[+] config.template.yaml  {len(tpl)} 字符")
    for ph in ("{{FD}}", "{{SECRET}}", "{{PROXIES}}"):
        n = tpl.count(ph)
        print(f"      占位符 {ph}: {n} 处" + ("" if n else "  ← 缺失!"))

    # --- 节点列表 ---
    nodes = config.miniyaml.dump({"x": sub.proxies})  # 只为拿到正确缩进的列表
    # miniyaml 会把列表放在 key 下面, 这里取列表体并缩进两格, 直接插进 proxies:
    body = nodes.split("x:\n", 1)[1]
    indented = "\n".join(("  " + ln) if ln.strip() else ln for ln in body.splitlines())
    (out / "nodes.yaml").write_text(indented.rstrip() + "\n", encoding="utf-8")
    print(f"[+] nodes.yaml  {len(sub.proxies)} 个节点")

    # --- geodata ---
    total = 0
    for f in GEODATA:
        src = paths.runtime_dir() / f
        if not src.is_file():
            print(f"[!] 缺 {f} (内核缺 geodata 会直接 exit 1)")
            continue
        shutil.copy2(src, out / f)
        total += src.stat().st_size
        print(f"[+] {f}  {src.stat().st_size / 1048576:.1f} MB")

    # --- 规则集 ---
    rs_src = paths.runtime_dir() / "ruleset"
    n = 0
    rs_bytes = 0
    if rs_src.is_dir():
        for f in sorted(rs_src.glob("*.yaml")):
            shutil.copy2(f, out / "ruleset" / f.name)
            n += 1
            rs_bytes += f.stat().st_size
    print(f"[+] ruleset/  {n} 个文件 {rs_bytes / 1048576:.1f} MB")
    print(f"[i] 随包资源合计 {(total + rs_bytes) / 1048576:.1f} MB "
          f"(加上 mihomo 二进制后 APK 大约 30~40 MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
