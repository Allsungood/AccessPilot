"""mihomo 配置生成.

这里是本工具的核心价值: 无论用户拿到的是哪家机场的订阅, 最终都产出
一份针对 ChatGPT / Discord / X 优化过的、带 TUN 与 DNS 防污染的配置。
"""
from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from . import miniyaml, paths, rules
from .state import AppState
from .subscription import Subscription, uniquify_names
from .util import Fail, ok, run_hidden

#: 用于 url-test 的探测地址(国内可直连, 返回 204)
#: 用于 url-test 的探测地址。刻意用 **HTTPS/443** 而不是 80 端口:
#: 实测有相当一部分节点只放行 443, 用 http://.../generate_204 会把它们
#: 误判为"不可用", 白白漏掉可用节点。
TEST_URL = "https://www.gstatic.com/generate_204"
TEST_URL_ALT = "https://cp.cloudflare.com/generate_204"

FAKE_IP_FILTER = [
    "*.lan",
    "*.local",
    "*.localdomain",
    "*.localhost",
    "localhost.ptlogin2.qq.com",
    "+.msftconnecttest.com",
    "+.msftncsi.com",
    "+.pool.ntp.org",
    "time.*.com",
    "time.*.gov",
    "ntp.*.com",
    "+.stun.*.*",
    "+.stun.*.*.*",
    "stun.*.*",
    "*.srv.nintendo.net",
    "*.stun.playstation.net",
    "xbox.*.microsoft.com",
    "+.xboxlive.com",
    "*.n.n.srv.nintendo.net",
    "+.stun.playstation.net",
    "workstation.*",
    "*.workgroup",
]

CN_DNS = ["https://223.5.5.5/dns-query", "https://doh.pub/dns-query"]
CN_DNS_BOOTSTRAP = ["223.5.5.5", "119.29.29.29", "180.76.76.76"]
FOREIGN_DNS = [
    "https://1.1.1.1/dns-query",
    "https://8.8.8.8/dns-query",
    "tls://8.8.4.4:853",
]

#: 内核内置 DNS 的监听端口。TUN 的 dns-hijack 与 diag.dns_via_local() 都认这个端口。
DNS_PORT = 1053

LOOPBACK = "127.0.0.1"


def lan_address() -> str | None:
    """本机在局域网里的地址; 判断不出来就返回 None.

    做法: 建一个 UDP socket 并 connect() 到一个外网地址。UDP 的 connect 不会
    发出任何数据包, 它只是让内核按路由表选一张网卡, 于是 getsockname() 给出的
    就是这张网卡上的地址 —— 零依赖拿到"本机 LAN IP"的标准做法, 比去解析
    ipconfig / netstat 的本地化输出可靠得多(那些输出还会随系统语言变)。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        address = str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()
    if address.startswith("127."):
        return None  # 只有回环可用 = 现在根本没连局域网
    return address


def dns_listen(st: AppState) -> str:
    """DNS 监听地址(以及为什么不能写死 0.0.0.0).

    真实事故: 这里曾经无条件写死 `0.0.0.0:1053`, 即使 allow_lan=false,
    `netstat` 也能看到 `0.0.0.0:1053 LISTENING`。于是同一张网(咖啡厅 / 宿舍 /
    办公室 / 手机热点)里的任何人都能把它当免费 DNS 用: 既可以被拿去做 DNS
    放大攻击的跳板(受害者看到的是你的 IP), 也能反过来观察、甚至投毒这台机器
    的解析结果 —— 而用户以为自己只是开了个代理, 什么都没对外提供。

    所以: 默认只监听回环; 只有用户显式开了局域网共享才对外, 并且优先绑定具体
    的网卡地址而不是通配地址 —— `0.0.0.0` 会把 DNS 同时暴露到**所有**网卡上
    (虚拟网卡、VPN、热点都算), 那比用户想要的"给手机用一下"大得多。
    """
    if not st.allow_lan:
        return f"{LOOPBACK}:{DNS_PORT}"
    address = lan_address()
    if address:
        return f"{address}:{DNS_PORT}"
    # 兜底: 判断不出局域网地址(没默认路由 / 断网)。这里仍然对外监听, 因为
    # allow_lan 是用户显式打开的; 但代价必须写清楚, 免得下次又被当成"默认行为"。
    return f"0.0.0.0:{DNS_PORT}"


def _geox(mirror: str = "jsdelivr") -> dict[str, str]:
    base = (
        "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release"
        if mirror == "jsdelivr"
        else "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/release"
    )
    return {
        "geoip": f"{base}/geoip.dat",
        "geosite": f"{base}/geosite.dat",
        "mmdb": f"{base}/country.mmdb",
        "asn": f"{base}/GeoLite2-ASN.mmdb",
    }


def proxy_names(sub: Subscription) -> list[str]:
    return [str(p["name"]) for p in sub.proxies if p.get("name")]


def build_proxy_groups(
    real_names: list[str], st: AppState, *, accel: bool = False
) -> list[dict[str, Any]]:
    """构造策略组.

    real_names: 真正能承载流量的节点(WARP / 订阅节点)。刻意不含 IP 优选器 ——
                它固定走直连, 放进 url-test 没有意义。
    """
    if not real_names and not accel:
        raise Fail(
            "没有可用节点。请添加订阅, 开启免节点直连加速(accesspilot accel on), "
            "或注册免费的 WARP 出口(accesspilot warp register)"
        )
    # 免节点模式: 没有任何能承载流量的节点, 只有本地 IP 优选器可用
    no_node_mode = not real_names
    names = real_names + ([rules.ACCEL_PROXY_NAME] if accel else [])
    auto_pool = real_names or names

    def sel(group: str, extra_prepend: list[str] | None = None) -> dict[str, Any]:
        return {
            "name": group,
            "type": "select",
            "proxies": (extra_prepend or []) + names,
        }

    groups: list[dict[str, Any]] = []

    if accel:
        groups.append(
            {
                "name": rules.G_ACCEL,
                "type": "select",
                "proxies": [rules.ACCEL_PROXY_NAME, "DIRECT"],
            }
        )

    if no_node_mode:
        # 免节点模式: 默认全部直连(等价于没开代理), 只有命中 ⚡ 直连加速
        # 规则的域名才交给 IP 优选器 —— 不做任何越权的事, 也不假装能加速
        # 那些必须依赖境外节点的平台。
        groups.append(
            {
                "name": rules.G_SELECT,
                "type": "select",
                "proxies": ["DIRECT"] + names,
            }
        )
        groups.append(
            {
                "name": rules.G_AUTO,
                "type": "url-test",
                "url": TEST_URL,
                "interval": 300,
                "tolerance": 50,
                "lazy": False,
                "proxies": auto_pool,
            }
        )
        for group in (rules.G_AI, rules.G_SOCIAL, rules.G_MEDIA):
            groups.append(
                {
                    "name": group,
                    "type": "select",
                    "proxies": [rules.G_SELECT, "DIRECT"],
                }
            )
        groups.append({"name": rules.G_DIRECT, "type": "select", "proxies": ["DIRECT"]})
        groups.append(
            {
                "name": rules.G_REJECT,
                "type": "select",
                "proxies": ["REJECT", "DIRECT"],
            }
        )
        groups.append(
            {
                "name": rules.G_FINAL,
                "type": "select",
                "proxies": [rules.G_SELECT, "DIRECT"],
            }
        )
        return groups

    groups.append(
        {
            "name": rules.G_SELECT,
            "type": "select",
            "proxies": [rules.G_AUTO, rules.G_DIRECT] + names,
        }
    )
    groups.append(
        {
            "name": rules.G_AUTO,
            "type": "url-test",
            "url": TEST_URL,
            "interval": 300,
            "tolerance": 50,
            "lazy": False,
            "proxies": auto_pool,
        }
    )
    groups.append(
        sel(
            rules.G_AI,
            [
                rules.G_SELECT,
                rules.G_AUTO,
            ],
        )
    )
    groups.append(
        sel(
            rules.G_SOCIAL,
            [
                rules.G_SELECT,
                rules.G_AUTO,
            ],
        )
    )
    groups.append(
        sel(
            rules.G_MEDIA,
            [
                rules.G_SELECT,
                rules.G_AUTO,
            ],
        )
    )
    # 注意: 这里不能引用 G_SELECT, 否则与 G_SELECT 中的 G_DIRECT 形成环,
    # 内核会以 "loop is detected in ProxyGroup" 拒绝加载。
    groups.append(
        {
            "name": rules.G_DIRECT,
            "type": "select",
            "proxies": ["DIRECT"],
        }
    )
    groups.append(
        {
            "name": rules.G_REJECT,
            "type": "select",
            "proxies": ["REJECT", "DIRECT"],
        }
    )
    groups.append(
        {
            "name": rules.G_FINAL,
            "type": "select",
            "proxies": [rules.G_SELECT, "DIRECT"],
        }
    )
    return groups


def build_dns(st: AppState) -> dict[str, Any]:
    if st.dns_mode == "redir-host":
        mode: dict[str, Any] = {"enhanced-mode": "redir-host"}
    else:
        mode = {
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "fake-ip-filter": list(FAKE_IP_FILTER),
        }
    dns: dict[str, Any] = {
        "enable": True,
        "ipv6": st.ipv6,
        "listen": dns_listen(st),
        "prefer-h3": False,
        "use-hosts": True,
        "use-system-hosts": True,
        "respect-rules": True,
        "default-nameserver": list(CN_DNS_BOOTSTRAP),
        "nameserver": list(CN_DNS),
        "proxy-server-nameserver": list(CN_DNS),
        "direct-nameserver": list(CN_DNS),
        "fallback": list(FOREIGN_DNS),
        "fallback-filter": {
            "geoip": True,
            "geoip-code": "CN",
            "ipcidr": ["240.0.0.0/4"],
        },
        "nameserver-policy": _nameserver_policy(),
    }
    dns.update(mode)
    return dns


def _nameserver_policy() -> dict[str, Any]:
    """按域名决定用哪组 DNS.

    关键点: ChatGPT / Discord / X 的域名强制使用境外 DoH, 避免 DNS 污染
    导致的"能连上但登录失败/资源加载不出来"。
    """
    policy: dict[str, Any] = {
        "geosite:cn": list(CN_DNS),
        "geosite:geolocation-!cn": list(FOREIGN_DNS),
        "geosite:gfw": list(FOREIGN_DNS),
    }
    for domain in (
        "openai.com",
        "chatgpt.com",
        "oaistatic.com",
        "oaiusercontent.com",
        "auth0.com",
        "discord.com",
        "discord.gg",
        "discordapp.com",
        "discordapp.net",
        "discordcdn.com",
        "x.com",
        "twitter.com",
        "twimg.com",
        "t.co",
        "telegram.org",
        "t.me",
    ):
        policy[f"+.{domain}"] = list(FOREIGN_DNS)
    return policy


def sanitize_proxies(proxies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤/修正内核不支持的节点参数, 防止单个坏节点让整个配置加载失败.

    这些规则全部来自真实事故(见 README 修复记录 14):
      * ss/ssr 密码不在内核支持列表 -> 丢弃(实测: auth_aes128_md5、'origin' 等)
      * hysteria2 有 obfs 无密码   -> 去掉 obfs(内核要求两者必须同时出现)
      * alpn 为字符串             -> 转成列表
      * wireguard 缺密钥         -> 丢弃
      * vmess 密码是垃圾值        -> 改回 auto
      * 缺 server/port            -> 丢弃
    """
    # mihomo 支持的 SS/SSR 密码(白名单之外的免费节点垃圾值一律丢弃)
    SS_CIPHERS = {
        "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
        "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305",
        "aes-128-gcm-2022", "aes-256-gcm-2022",
        "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
        "2022-blake3-chacha20-poly1305", "2022-blake3-chacha8-poly1305",
        "2022-blake3-chacha12-poly1305",
        "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
        "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
        "aes-128-cfb1", "aes-192-cfb1", "aes-256-cfb1",
        "aes-128-cfb8", "aes-192-cfb8", "aes-256-cfb8",
        "aes-128-ofb", "aes-192-ofb", "aes-256-ofb",
        "chacha20", "chacha20-ietf", "xchacha20", "salsa20",
        "rc4", "rc4-md5", "rc4-md5-6", "bf-cfb",
        "camellia-128-cfb", "camellia-192-cfb", "camellia-256-cfb",
        "cast5-cfb", "des-cfb", "idea-cfb", "seed-cfb", "none",
    }
    VMESS_CIPHERS = {"auto", "none", "zero", "aes-128-gcm", "chacha20-poly1305"}

    out: list[dict[str, Any]] = []
    for p in proxies:
        p = dict(p)
        t = str(p.get("type") or "")
        if not p.get("server") or not p.get("port"):
            continue
        if t in ("ss", "ssr"):
            if str(p.get("cipher") or "").lower() not in SS_CIPHERS:
                continue
        if t == "vmess":
            if str(p.get("cipher") or "auto").lower() not in VMESS_CIPHERS:
                p["cipher"] = "auto"
        if t in ("hysteria", "hysteria2") and p.get("obfs") and not p.get("obfs-password"):
            p.pop("obfs")
        if "alpn" in p and isinstance(p.get("alpn"), str):
            p["alpn"] = [p["alpn"]]
        if t == "wireguard" and not (p.get("private-key") and p.get("public-key")):
            continue
        if t in ("vmess", "ss") and isinstance(p.get("port"), str) and p["port"].isdigit():
            p["port"] = int(p["port"])
        # 清洗控制字符。订阅源把 UTF-8 的 emoji 按 Latin-1 解码时会产出
        # U+0080~U+009F, YAML 不接受 —— 一个坏节点就能让整份配置加载失败
        # (真实事故: 某个节点的 sni 带了 ð\x9f\x87, 6000 个节点全部作废)。
        for k, v in list(p.items()):
            if not isinstance(v, str) or not miniyaml.has_control_chars(v):
                continue
            cleaned = miniyaml.strip_control_chars(v)
            if k in ("sni", "servername", "host") and not _is_hostname(cleaned):
                p.pop(k)  # 洗出来也不是合法主机名(如 sni=https://t.me/xxx) -> 丢掉该字段
            else:
                p[k] = cleaned
        out.append(p)
    return out


def _is_hostname(s: str) -> bool:
    """够用的主机名判断: 只允许字母数字和 . _ -"""
    return bool(s) and all(c.isalnum() or c in "._-" for c in s)


def build_config(sub: Subscription, st: AppState, *, tun: bool | None = None) -> dict[str, Any]:
    """组装完整的 mihomo 配置字典."""
    paths.ensure_dirs()
    tun_enabled = st.tun_enable if tun is None else tun
    # 防御性过滤: 任何单个节点的非法参数都会让内核拒绝**整个**配置,
    # 不能因为一颗老鼠屎坏掉一锅汤(实测: 一个 auth_aes128_md5 的 SSR 节点
    # 让 1242 个节点的配置全部加载失败)。
    proxies = sanitize_proxies(sub.proxies)
    # 清洗之后**必须**再消一次重名, 顺序不能反也不能省。
    #
    # 为什么这不是多此一举 —— 两层原因, 都是实测出来的:
    #
    # 1. sanitize 会**改变** name(去掉控制字符、emoji 变体选择符等), 于是两个
    #    本来不同的名字可能在清洗后撞成一个。历史事故的原始形态就是这个:
    #    一个节点的 sni 里混进控制字符, 清洗后与另一个重名, 内核拒绝整份配置,
    #    6027 个节点全部不可用。当时只在 freenodes.fetch_free() 里补了去重,
    #    而 process.start() / turn_on() / 订阅导入走的是**这条路**。
    # 2. 上游给的订阅本来就可能自带重名, 不是所有调用方都记得先 uniquify。
    #
    # 所以这里补的是"最后一道防线": 不管上游怎么调、传进来什么, 渲染出来的
    # 配置里名字一定唯一。防线的意义就在于它不依赖调用方守规矩。
    proxies = uniquify_names(proxies)
    for p in proxies:
        p.pop("dialer-proxy", None)
        # mihomo >= 1.19 已移除全局 global-client-fingerprint, 改为逐节点设置。
        # 统一使用 chrome 指纹可显著提升 TLS 握手的兼容性(尤其对 ChatGPT)。
        if p.get("type") in ("vmess", "vless", "trojan") and not p.get(
            "client-fingerprint"
        ):
            p["client-fingerprint"] = "chrome"

    # 免节点直连加速: 把本地 IP 优选器注册成一个 SOCKS5 出站
    if st.accel_enable:
        proxies.append(
            {
                "name": rules.ACCEL_PROXY_NAME,
                "type": "socks5",
                "server": "127.0.0.1",
                "port": st.accel_port,
                "udp": False,
            }
        )

    # 免费 WARP 出口(如果注册过)。它算"真节点", 能承载全部流量。
    warp_profile = None
    try:
        from . import warp as warp_mod

        warp_profile = warp_mod.load_profile()
    except Exception:
        warp_profile = None
    if warp_profile is not None:
        proxies.append(
            warp_profile.to_mihomo_proxy(
                dialer_proxy=st.warp_dialer or None
            )
        )

    # 能承载流量的节点(不含 IP 优选器 —— 它只走直连)
    real_names = [
        str(p["name"]) for p in proxies if p.get("name") != rules.ACCEL_PROXY_NAME
    ]

    cfg: dict[str, Any] = {
        "mixed-port": st.mixed_port,
        "socks-port": st.mixed_port + 1,
        "port": 0,
        # 入站端口(混合 / socks)的暴露范围由这两项决定, 缺一不可:
        #   * allow-lan=false 时内核只监听回环;
        #   * bind-address 再钉一次 127.0.0.1 作为双保险(它的默认值是 `*`,
        #     而且**只在 allow-lan=true 时才生效**, 依赖默认值太危险)。
        # 开了局域网共享就交给 `*` —— 这正是 allow-lan 的语义: 用户要的是
        # "其它设备也能连", 此时换成某一台网卡的地址反而会让多网卡机器
        # (有线 + 无线 + 热点)上的设备连不上。
        # 注意 DNS 的 listen 是**独立**配置项, 不受 bind-address 约束,
        # 它以前无条件 0.0.0.0 —— 见 dns_listen()。
        "allow-lan": st.allow_lan,
        "bind-address": "*" if st.allow_lan else LOOPBACK,
        "mode": "rule",
        "log-level": "info",
        "ipv6": st.ipv6,
        "unified-delay": True,
        "tcp-concurrent": True,
        "find-process-mode": "strict",
        "keep-alive-idle": 30,
        "keep-alive-interval": 30,
        "external-controller": f"127.0.0.1:{st.api_port}",
        "secret": st.api_secret,
        "profile": {
            "store-selected": True,
            "store-fake-ip": True,
        },
        "sniffer": {
            "enable": True,
            "force-dns-mapping": True,
            "parse-pure-ip": True,
            "override-destination": False,
            "sniff": {
                "HTTP": {"ports": [80, "8080-8880"], "override-destination": True},
                "TLS": {"ports": [443, 8443]},
                "QUIC": {"ports": [443, 8443]},
            },
            "skip-domain": ["Mijia Cloud", "+.push.apple.com"],
        },
        "geodata-mode": False,
        "geo-auto-update": True,
        "geo-update-interval": 168,
        "geox-url": _geox(st.mirror),
        "dns": build_dns(st),
        "tun": {
            "enable": tun_enabled,
            "stack": st.tun_stack,
            "device": st.tun_device,
            "auto-route": True,
            "auto-detect-interface": True,
            "dns-hijack": ["any:53", "tcp://any:53"],
            "mtu": 9000,
            "strict-route": False,
            "endpoint-independent-nat": False,
        },
        "proxies": proxies,
        "proxy-groups": build_proxy_groups(real_names, st, accel=st.accel_enable),
        "rule-providers": rules.rule_providers(st.mirror),
        "rules": rules.build_rules(accel=st.accel_enable),
    }
    # 只有面板已下载到本地时才声明 external-ui —— 否则内核会在启动时
    # 尝试联网下载 metacubexd, 在受限网络下只会白等并刷错误日志。
    if (paths.runtime_dir() / "ui" / "index.html").exists():
        cfg["external-ui"] = "ui"
    return cfg


def write_config(cfg: dict[str, Any], path: Path | None = None) -> Path:
    target = path or paths.config_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = "# 由 AccessPilot 自动生成, 请勿手工修改 (改这里会被下次生成覆盖)\n"
    text += miniyaml.dump(cfg)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
    return target


def render(sub: Subscription, st: AppState, *, tun: bool | None = None) -> Path:
    """生成配置, **校验通过之后才原子替换**线上那份。

    ## 为什么不能直接覆盖 runtime/config.yaml

    真实事故(2026-10-01): `runtime/config.yaml` 被写成了带重名的坏配置, 而
    校验是在**写完之后**才做的。当时内核还在跑(用的是启动时读进内存的那份),
    所以表面上一切正常 —— 但只要内核因为任何原因重启(用户点开关 / exe 重启 /
    保活任务拉起 / 重启机器), 它就会读到这份坏文件, 校验失败、起不来,
    **用户直接断网, 而且开关打不开**(故障现象离起因差了几个小时, 极难排查)。

    所以校验必须在文件落地之前完成: 先写候选, 校验候选, 过了才 rename 顶替。
    同目录 rename 在 Windows 上也是原子的, 不会有"写了一半"的中间态。

    校验失败时**保留原来那份**: 原来那份也许是好的, 而留一份也许能用,
    永远好过留一份确定不能用。
    """
    cfg = build_config(sub, st, tun=tun)
    target = paths.config_file()
    # 候选文件必须和目标**同目录**, 否则 replace 可能跨卷而失去原子性。
    candidate = target.with_name(target.name + ".candidate")
    write_config(cfg, candidate)
    try:
        passed, output = test_config(candidate)
    except Exception:
        candidate.unlink(missing_ok=True)
        raise
    if not passed:
        candidate.unlink(missing_ok=True)
        raise Fail(f"配置校验失败, 已保留原有配置:\n{output}")
    candidate.replace(target)
    return target


def test_config(path: Path | None = None) -> tuple[bool, str]:
    """调用内核自检配置, 这是最可靠的正确性验证方式."""
    binary = paths.core_binary()
    if not binary.exists():
        raise Fail("未找到内核, 请先运行: accesspilot core install")
    target = path or paths.config_file()
    code, out = run_hidden(
        [str(binary), "-t", "-f", str(target), "-d", str(paths.runtime_dir())],
        timeout=90,
    )
    return code == 0, out.strip()
