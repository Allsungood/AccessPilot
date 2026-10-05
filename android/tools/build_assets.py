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


def _write_text(path: Path, text: str) -> None:
    """写文本文件, **强制 LF**, 不用平台默认换行.

    真实事故(2026-10-01): `Path.write_text()` 在 Windows 上会把 `\\n` 翻译成
    `\\r\\n`, 于是生成出来的 config.template.yaml(28,732 行) 和 nodes.yaml
    (69,826 行) 全变成 CRLF。YAML 本身能读 CRLF, 所以它不会立刻炸 —— 但:
      * 文件凭空大 2~3%;
      * 更重要的是**它就躺在仓库和 APK 里**, 而任何人用 `read_text()` 去核对
        换行时都会被 Python 的通用换行转换骗过去(我第一次核对就得出"没有 CRLF"
        的错误结论, 是对方用 read_bytes 才查出来的)。
    显式写 newline="\\n" 就没有这个问题。
    """
    path.write_text(text, encoding="utf-8", newline="\n")


def build_template(sub: subscription.Subscription, st) -> tuple[str, list[dict]]:
    """把完整配置渲染成安卓模板, 并返回**同一份** proxies 列表.

    为什么要把 proxies 一起返回(真实事故, 2026-10-01):
    最初只返回模板文本, nodes.yaml 是另外从 `sub.proxies` 生成的。但
    `config.build_config()` 会往 proxies 里**追加本机才有的东西** ——
    例如注册过的 `☁️ WARP` 出口。于是模板里的策略组引用了 `☁️ WARP`,
    而 nodes.yaml 里根本没有它, 内核直接 fatal:

        Parse config error: proxy group[1]: ♻️ 自动选择: '☁️ WARP' not found

    这类"两个文件各自生成、边界对不上"的 bug 在真机上才暴露, 而且报错指向
    策略组、真因在生成器。所以现在**只有一个事实来源**: 从 build_config 出来的
    那份 proxies, 模板和 nodes.yaml 都用它。
    """
    cfg = config.build_config(sub, st)
    proxies = list(cfg.get("proxies") or [])

    # 1) tun 段换成安卓版(fd 模式)
    text = config.miniyaml.dump(cfg)
    text = re.sub(r"^tun:\n(?:[ \t]+.*\n|\n)*", ANDROID_TUN, text, count=1, flags=re.M)

    # 2) 控制端口: 安卓端也走 127.0.0.1, 端口固定 9090 方便界面找它。
    #    secret 每次安装随机, 由运行时注入 —— 不能把开发机上的密钥打进 APK。
    text = re.sub(r'^secret: .*$', 'secret: "{{SECRET}}"', text, count=1, flags=re.M)

    # 3) DNS 里如果有监听地址, 安卓上要绑到本机。
    #
    # 这里的 bug 值得记一笔 (审计 N8): 原来的正则只匹配**不带引号**的
    # `listen: 0.0.0.0:1053`, 而 miniyaml.dump 输出的是带引号的
    # `listen: "0.0.0.0:1053"` —— 于是这条"加固"从来没生效过, 打出来的
    # config.template.yaml 里 dns 服务一直监听着 0.0.0.0:1053: 同一个 Wi-Fi 下
    # 任何设备都能把这部手机当**开放 DNS 解析器**用 (走用户的代理出网, 还能当
    # DNS 放大反射器)。安卓这条路根本不需要那个 socket —— 隧道里的域名解析由
    # tun.dns-hijack 截走, 所以绑回 127.0.0.1 是纯收益。
    #
    # 现在两种写法都能匹配, 并统一输出带引号的形式 (原来的替换结果不带引号,
    # 值里含冒号时对 YAML 解析器是个隐患, 不该在这里赌)。
    text = re.sub(r'^listen:\s*"?0\.0\.0\.0:([^"\n]+)"?\s*$',
                  r'listen: "127.0.0.1:\1"', text, flags=re.M)
    # 兜底: 任何形式的 0.0.0.0 监听都不该出现在随包模板里。
    leftover = [ln for ln in text.splitlines() if "0.0.0.0" in ln]
    if leftover:
        raise SystemExit("[x] 模板里还有监听 0.0.0.0 的行, 拒绝生成:\n    " + "\n    ".join(leftover))

    # 4) 节点列表整体换成占位符 —— 节点是运行时注入的, 不编进模板。
    text = re.sub(r"^proxies:\n(?:[ \t]+.*\n|\n)*",
                  "proxies:\n{{PROXIES}}\n", text, count=1, flags=re.M)

    return text, proxies


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

    # --- 配置模板 + 与其**一致**的节点列表 ---
    tpl, proxies = build_template(sub, st)
    _write_text(out / "config.template.yaml", tpl)
    print(f"[+] config.template.yaml  {len(tpl)} 字符")
    for ph in ("{{FD}}", "{{SECRET}}", "{{PROXIES}}"):
        n = tpl.count(ph)
        print(f"      占位符 {ph}: {n} 处" + ("" if n else "  ← 缺失!"))

    # --- 节点列表 ---
    #
    # 用 `proxies` 作 key 去 dump, 然后**只把 key 那一行切掉** —— 剩下的列表体
    # 缩进就是"插在 proxies: 下面"应该有的缩进, 一个空格都不用自己加。
    #
    # 真实事故一(缩进): 原来写的是 dump({"x": ...}) 再手工给每行加两格, 但 dump
    # 出来本来就有一层缩进, 再加两格变成 4/6 格 —— 插进模板后节点被 YAML 解析成
    # `proxies` 映射里的兄弟键, 内核报 "yaml: line N: did not find expected key",
    # 而 N 指向 proxies: **上面**那一行, 真正的原因在 7 万行外的一个空格上。
    #
    # 真实事故二(不一致): 这份列表原来是从 `sub.proxies` 生成的, 而模板来自
    # build_config() —— 后者会追加本机才有的 WARP 出口, 于是策略组引用了
    # nodes.yaml 里没有的名字, 内核 fatal "proxy group[1]: ... '☁️ WARP' not found"。
    # 现在两者共用 build_template() 返回的**同一份** proxies。
    nodes = config.miniyaml.dump({"proxies": proxies})
    body = nodes.split("proxies:\n", 1)[1]
    _write_text(out / "nodes.yaml", body.rstrip() + "\n")
    print(f"[+] nodes.yaml  {len(proxies)} 个节点 (与模板同一份数据源)")

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
