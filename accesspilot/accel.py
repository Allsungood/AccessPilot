"""免节点直连加速 —— FastGithub 思路的工程化实现.

背景(为什么有的站能"换 IP 救活", 有的不能):
  * 中国的网络封锁有三种手段, 只有第一种能用换 IP 绕过:
      1. DNS 污染 / 特定 IP 段被丢弃   -> 换一个可用 IP 就能通(GitHub / Fastly 属于此类)
      2. SNI 阻断                     -> 无论换哪个 IP, TLS 握手都会被切断
      3. 应用层地区封禁(OpenAI)      -> 即使 TLS 通了, 服务端也会按出口 IP 拒绝
  * 本模块只能解决第 1 类, 因此加速对象是 **GitHub / Docker / npm / PyPI 等开发资源**,
    对 ChatGPT / X / Discord 无效(它们属于第 2、3 类, 必须有境外节点)。

工作原理(不修改 DNS、不装根证书、不做中间人):
     应用 --SOCKS5--> [本模块] --TCP--> 选出的最优真实 IP
  1. 从多个 DoH 源并发查询域名的**真实 IP**(绕过本地 DNS 污染);
  2. 对候选 IP 并发做 TCP 建连 + TLS 握手探测, 按延迟排序, 挑最快且真正可用的;
  3. 建立 TCP 连接后原样转发字节流 —— TLS 是端到端的, 证书校验照常工作;
  4. 结果按域名缓存, 过期自动重新优选。

可以作为 mihomo 的上游(SOCKS5 出站)接入现有分流体系, 也可以被 git/npm/docker
等工具直接使用。
"""
from __future__ import annotations

import concurrent.futures as futures
import json
import select
import socket
import ssl
import struct
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

from .util import info, ok, warn

# --------------------------------------------------------------------------- #
# 加速域名与候选 IP 池
# --------------------------------------------------------------------------- #

#: 需要"IP 优选"的域名后缀。
#:
#: 这里刻意只收录**实测能靠换 IP 救回**的域名。实测结论(本机 2026-09):
#:   * GitHub 系(Fastly/N 段) —— 系统 DNS 给的 IP 被丢弃, 换一个就好: 有效 ✅
#:   * huggingface.co / registry-1.docker.io —— TLS 被切断: 换 IP 无效 ❌
#:   * chatgpt.com / discord.com / x.com —— SNI 阻断或地区封禁: 换 IP 无效 ❌
#: 对 ❌ 的域名, 正确做法是走境外节点(HuggingFace 可用 hf-mirror.com 镜像,
#: Docker 可配置 registry-mirrors)。
ACCEL_SUFFIXES: tuple[str, ...] = (
    "github.com",
    "githubusercontent.com",
    "githubassets.com",
    "github.io",
    "ghcr.io",
    "github.dev",
    "githubapp.com",
)

#: 已知可用的固定 IP 池(GitHub 官方公布的网段 / Fastly 承载), 与 DoH 结果合并使用。
#: 必须内置的原因: 国内 DoH 对被封域名会返回空答案, 拿不到真实 IP。
IP_POOLS: dict[str, list[str]] = {
    "raw.githubusercontent.com": [
        "185.199.108.133", "185.199.109.133", "185.199.110.133", "185.199.111.133",
    ],
    "objects.githubusercontent.com": [
        "185.199.108.133", "185.199.109.133", "185.199.110.133", "185.199.111.133",
    ],
    "gist.githubusercontent.com": [
        "185.199.108.133", "185.199.109.133", "185.199.110.133", "185.199.111.133",
    ],
    "avatars.githubusercontent.com": [
        "185.199.108.133", "185.199.109.133", "185.199.110.133", "185.199.111.133",
    ],
    "camo.githubusercontent.com": [
        "185.199.108.133", "185.199.109.133", "185.199.110.133", "185.199.111.133",
    ],
    "github.githubassets.com": [
        "185.199.108.154", "185.199.109.154", "185.199.110.154", "185.199.111.154",
        "185.199.108.215", "185.199.109.215", "185.199.110.215", "185.199.111.215",
    ],
    "github.com": [
        "20.205.243.166", "20.205.243.168", "20.27.177.113", "4.237.22.38",
        "140.82.112.3", "140.82.113.3", "140.82.114.3", "140.82.121.3",
    ],
    "api.github.com": [
        "20.205.243.168", "20.27.177.116", "140.82.112.6", "140.82.113.6",
    ],
    "codeload.github.com": [
        "20.205.243.165", "20.27.177.114", "140.82.112.10", "140.82.113.10",
    ],
    "ghcr.io": [
        "20.205.243.164", "20.27.177.115", "140.82.112.34", "140.82.113.34",
    ],
    "github.io": [
        "185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153",
    ],
}

#: DoH 解析源。只有国内可直连的才真正可用, 实测结果(2026-09 本机):
#:   doh.pub / 1.12.12.12 可用;  阿里 DoH 返回 400;  Cloudflare / Google DoH 被阻断。
#: 仍然把它们列在后面 —— 换一个网络环境可用性会变, 失败的会被自动跳过。
DOH_ENDPOINTS: tuple[str, ...] = (
    "https://doh.pub/dns-query",         # 腾讯 DNSPod
    "https://1.12.12.12/dns-query",      # DNSPod 备用 IP
    "https://120.53.53.53/dns-query",    # DNSPod 备用 IP
    "https://223.5.5.5/dns-query",       # 阿里
    "https://dns.alidns.com/dns-query",
    "https://1.0.0.1/dns-query",         # Cloudflare(常被阻断, 失败即跳过)
    "https://dns.google/dns-query",      # Google(常被阻断)
)

PROBE_PORT = 443


# --------------------------------------------------------------------------- #
# DNS(DoH)查询
# --------------------------------------------------------------------------- #


def parse_dns_a(raw: bytes) -> list[str] | None:
    """从 DNS 报文里取出所有 A 记录; 报文非法时返回 None."""
    if len(raw) < 12:
        return None
    flags = int.from_bytes(raw[2:4], "big")
    if not flags & 0x8000:  # 不是响应
        return None
    if flags & 0x000F:      # RCODE != 0 (NXDOMAIN/SERVFAIL...)
        return []
    qd = int.from_bytes(raw[4:6], "big")
    an = int.from_bytes(raw[6:8], "big")
    i = 12

    def skip_name(pos: int) -> int:
        hops = 0
        while True:
            if pos >= len(raw):
                raise ValueError("越界")
            length = raw[pos]
            if length == 0:
                return pos + 1
            if length & 0xC0 == 0xC0:
                return pos + 2
            pos += length + 1
            hops += 1
            if hops > 128:
                raise ValueError("域名过长")

    try:
        for _ in range(qd):
            i = skip_name(i) + 4
        out: list[str] = []
        for _ in range(an):
            i = skip_name(i)
            if i + 10 > len(raw):
                break
            rtype = int.from_bytes(raw[i : i + 2], "big")
            rdlen = int.from_bytes(raw[i + 8 : i + 10], "big")
            i += 10
            if rtype == 1 and rdlen == 4 and i + 4 <= len(raw):
                out.append(".".join(str(b) for b in raw[i : i + 4]))
            i += rdlen
        return out
    except ValueError:
        return None


def parse_doh_payload(raw: bytes) -> list[str]:
    """解析 DoH 响应, 兼容 DNS wireformat 与 JSON 两种返回格式."""
    text = raw.lstrip()
    if text.startswith(b"{"):  # 部分服务商忽略 Accept 头, 直接返回 JSON
        try:
            data = json.loads(text.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return []
        if int(data.get("Status", 0) or 0) != 0:
            return []
        return [
            a["data"]
            for a in data.get("Answer", [])
            if isinstance(a, dict) and a.get("type") == 1 and isinstance(a.get("data"), str)
        ]
    return parse_dns_a(raw) or []


def doh_query(domain: str, endpoint: str, *, timeout: float = 3.0) -> list[str]:
    """向单个 DoH 源查询 A 记录."""
    url = f"{endpoint}?name={domain}&type=A"
    req = urllib.request.Request(
        url, headers={"accept": "application/dns-message", "User-Agent": "AccessPilot"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception:
        return []
    return parse_doh_payload(raw)


def _pool_for(domain: str) -> list[str]:
    ips: list[str] = []
    for pattern, pool in IP_POOLS.items():
        if domain == pattern or domain.endswith("." + pattern):
            ips += pool
    return ips


def resolve_candidates(
    domain: str,
    *,
    endpoints: Iterable[str] = DOH_ENDPOINTS,
    timeout: float = 3.0,
    use_system_dns: bool = True,
    prefer_pool: bool = True,
) -> list[str]:
    """汇总一个域名的候选真实 IP(内置池 / DoH / 系统 DNS 兜底).

    prefer_pool=True 时, 若该域名有内置 IP 池就**完全跳过 DoH** —— 原因:
      1. 国内 DoH 对被封域名一律返回空答案, 查了也白查;
      2. DoH 失败时要等满超时, 会把首次请求拖到内核拨号超时(5s)之外。
    """
    pool = _pool_for(domain)
    candidates: list[str] = list(pool)

    if not (prefer_pool and pool):
        endpoints = list(endpoints)
        with futures.ThreadPoolExecutor(max_workers=max(len(endpoints), 1)) as pool_exec:
            for ips in pool_exec.map(
                lambda ep: doh_query(domain, ep, timeout=timeout), endpoints
            ):
                candidates += ips

    if use_system_dns:
        try:
            infos = socket.getaddrinfo(domain, PROBE_PORT, proto=socket.IPPROTO_TCP)
            candidates += [i[4][0] for i in infos if ":" not in i[4][0]]
        except OSError:
            pass

    seen: set[str] = set()
    out: list[str] = []
    for ip in candidates:
        if ip and ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


# --------------------------------------------------------------------------- #
# IP 优选
# --------------------------------------------------------------------------- #


def tcp_latency(ip: str, port: int = PROBE_PORT, timeout: float = 4.0) -> float | None:
    """TCP 建连耗时(毫秒); 失败返回 None."""
    start = time.time()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return (time.time() - start) * 1000
    except OSError:
        return None


def tls_ok(ip: str, domain: str, port: int = PROBE_PORT, timeout: float = 6.0) -> float | None:
    """带 SNI 的 TLS 握手探测; 用于排除"TCP 通但 SNI 被阻断"的 IP."""
    start = time.time()
    sock = None
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(sock, server_hostname=domain) as ss:
            ss.do_handshake()
        return (time.time() - start) * 1000
    except Exception:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        return None


@dataclass
class ProbeResult:
    ip: str
    tcp_ms: float | None = None
    tls_ms: float | None = None

    @property
    def usable(self) -> bool:
        """传输层可达(TCP 建连成功)."""
        return self.tcp_ms is not None

    @property
    def verified(self) -> bool:
        """TLS 握手成功 —— 只有这种 IP 才真正能承载业务流量.

        区分这两个概念很重要: 有些 IP 能建 TCP 但 TLS 会被切断(SNI 阻断),
        用它做代理只会让上层莫名超时。
        """
        return self.tls_ms is not None

    @property
    def score(self) -> float:
        if self.tls_ms is not None:
            return self.tls_ms
        if self.tcp_ms is not None:
            return self.tcp_ms
        return float("inf")


def rank_ips(
    domain: str,
    candidates: list[str],
    *,
    port: int = PROBE_PORT,
    timeout: float = 2.5,
    verify_tls: bool = True,
    tls_top: int = 4,
) -> list[ProbeResult]:
    """并发探测候选 IP, 返回按可用性/延迟排序的结果.

    先用 TCP 快速筛掉不可达的, 再对最快的若干个做 TLS 验证 —— 避免选中
    "TCP 能连但 SNI 被切断"的 IP。全部 TLS 失败时退回 TCP 结果。
    """
    if not candidates:
        return []
    with futures.ThreadPoolExecutor(max_workers=min(32, len(candidates))) as pool:
        tcp_results = list(
            pool.map(lambda ip: ProbeResult(ip, tcp_latency(ip, port, timeout)), candidates)
        )
    alive = [r for r in tcp_results if r.tcp_ms is not None]
    alive.sort(key=lambda r: r.tcp_ms or 1e9)

    if verify_tls and alive:
        head = alive[: max(tls_top, 1)]
        with futures.ThreadPoolExecutor(max_workers=len(head)) as pool:
            tls_ms = list(
                pool.map(
                    lambda r: tls_ok(r.ip, domain, port, max(timeout, 4.0)),
                    head,
                )
            )
        for r, ms in zip(head, tls_ms):
            r.tls_ms = ms
        verified = [r for r in head if r.verified]
        rest = [r for r in alive if r not in head]
        if verified:
            verified.sort(key=lambda r: r.score)
            return verified + rest
        warn(f"{domain}: 候选 IP 的 TLS 握手全部失败(疑似 SNI 阻断), 本域名无法靠换 IP 加速")
        return alive
    return alive


def quick_candidates(domain: str, *, port: int = PROBE_PORT, timeout: float = 1.5) -> list[str]:
    """冷启动快速路径: 只靠内置 IP 池 + 短超时 TCP 探测, 保证 2 秒内出结果.

    内核给上游的拨号超时只有 5 秒, 所以第一次请求不能等完整优选流程。
    """
    pool = _pool_for(domain)
    if not pool:
        try:
            infos = socket.getaddrinfo(domain, port, proto=socket.IPPROTO_TCP)
            pool = [i[4][0] for i in infos if ":" not in i[4][0]]
        except OSError:
            return []
    with futures.ThreadPoolExecutor(max_workers=min(16, len(pool))) as pool_exec:
        probed = list(
            pool_exec.map(lambda ip: (tcp_latency(ip, port, timeout), ip), pool)
        )
    alive = sorted([(ms, ip) for ms, ip in probed if ms is not None])
    return [ip for _, ip in alive]


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #


@dataclass
class CacheEntry:
    results: list[ProbeResult] = field(default_factory=list)
    expires: float = 0.0


class BestIPCache:
    """域名 -> 优选 IP 列表, 带 TTL 与失败降级."""

    def __init__(self, ttl: float = 600.0, bad_ttl: float = 60.0) -> None:
        self.ttl = ttl
        self.bad_ttl = bad_ttl
        self._data: dict[str, CacheEntry] = {}
        self._pending: set[str] = set()
        self._lock = threading.Lock()

    def peek(self, domain: str) -> list[ProbeResult] | None:
        with self._lock:
            entry = self._data.get(domain)
            if entry and entry.expires > time.time():
                return list(entry.results)
        return None

    def resolve_async(self, domain: str) -> None:
        """后台做完整优选(含 TLS 验证), 不阻塞当前请求.

        这是关键设计: 内核给上游的拨号超时只有 5 秒, 而一次完整优选
        (DoH + TCP + TLS 探测)要 3~10 秒。所以首次请求走快速路径,
        同时后台把结果算好放进缓存, 之后的请求就是毫秒级。
        """
        with self._lock:
            if domain in self._pending:
                return
            self._pending.add(domain)

        def work() -> None:
            try:
                self.resolve(domain)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._pending.discard(domain)

        threading.Thread(target=work, daemon=True, name=f"accel-{domain}").start()

    def resolve(
        self,
        domain: str,
        *,
        port: int = PROBE_PORT,
        verify_tls: bool = True,
        timeout: float = 4.0,
    ) -> list[ProbeResult]:
        cached = self.peek(domain)
        if cached is not None:
            return cached
        candidates = resolve_candidates(domain)
        results = rank_ips(domain, candidates, port=port, verify_tls=verify_tls,
                           timeout=timeout)
        ttl = self.ttl if results else self.bad_ttl
        with self._lock:
            self._data[domain] = CacheEntry(results, time.time() + ttl)
        return results

    def mark_bad(self, domain: str, ip: str) -> None:
        """连接失败时把该 IP 从缓存剔除, 下次会重新优选."""
        with self._lock:
            entry = self._data.get(domain)
            if not entry:
                return
            entry.results = [r for r in entry.results if r.ip != ip]
            if not entry.results:
                entry.expires = 0.0

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            return {
                "domains": len(self._data),
                "entries": {
                    d: {
                        "ips": [r.ip for r in e.results][:3],
                        "ttl_left": max(int(e.expires - now), 0),
                    }
                    for d, e in self._data.items()
                },
            }


# --------------------------------------------------------------------------- #
# SOCKS5 智能拨号器
# --------------------------------------------------------------------------- #

CACHE = BestIPCache()


def is_accelerated(domain: str) -> bool:
    domain = domain.lower().strip(".")
    return any(domain == s or domain.endswith("." + s) for s in ACCEL_SUFFIXES)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已关闭")
        buf += chunk
    return buf


def dial_best(domain: str, port: int, *, timeout: float = 8.0) -> tuple[socket.socket, str]:
    """按"缓存优选 IP -> 快速优选 -> 系统解析"的顺序建立到 domain:port 的连接."""
    if is_accelerated(domain):
        cached = CACHE.peek(domain)
        if cached is None:
            CACHE.resolve_async(domain)              # 后台算完整结果
            ips = quick_candidates(domain, port=port)  # 本次用 2 秒内的快速结果
        else:
            ips = [r.ip for r in cached]
        for ip in ips[:3]:
            try:
                return socket.create_connection((ip, port), timeout=min(timeout, 5.0)), ip
            except OSError:
                CACHE.mark_bad(domain, ip)
    # 未加速域名, 或优选全部失败: 走系统 DNS 的正常解析
    infos = socket.getaddrinfo(domain, port, proto=socket.IPPROTO_TCP)
    last: Exception | None = None
    for info in infos:
        try:
            sock = socket.create_connection(info[4][:2], timeout=timeout)
            return sock, info[4][0]
        except OSError as e:  # noqa: PERF203
            last = e
    raise last or OSError(f"无法连接 {domain}:{port}")


def warm_up() -> None:
    """启动时预热: 让第一次真实请求就能命中缓存."""
    for domain in IP_POOLS:
        CACHE.resolve_async(domain)


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            readable, _, _ = select.select([src], [], [], 60)
            if not readable:
                continue
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _handle_client(client: socket.socket, *, verbose: bool = False) -> None:
    remote: socket.socket | None = None
    try:
        client.settimeout(20)
        ver, nmethods = _recv_exact(client, 2)
        if ver != 5:
            raise ConnectionError("非 SOCKS5")
        _recv_exact(client, nmethods)
        client.sendall(b"\x05\x00")

        _ver, cmd, _rsv, atyp = _recv_exact(client, 4)
        if cmd != 1:
            raise ConnectionError("仅支持 CONNECT")
        if atyp == 1:
            host = socket.inet_ntoa(_recv_exact(client, 4))
        elif atyp == 3:
            host = _recv_exact(client, _recv_exact(client, 1)[0]).decode(
                "utf-8", errors="replace"
            )
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
        else:
            raise ConnectionError(f"未知 atyp={atyp}")
        port = struct.unpack(">H", _recv_exact(client, 2))[0]

        remote, used_ip = dial_best(host, port)
        client.settimeout(None)
        client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        if verbose:
            info(f"{host}:{port} -> {used_ip}")

        t = threading.Thread(target=_pipe, args=(client, remote), daemon=True)
        t.start()
        _pipe(remote, client)
        t.join(timeout=1)
    except Exception:
        try:
            client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
        except OSError:
            pass
    finally:
        for s in (client, remote):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def serve(port: int = 7895, *, host: str = "127.0.0.1", verbose: bool = False) -> None:
    """启动 SOCKS5 智能拨号器(阻塞)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Windows 上 SO_REUSEADDR 的语义和 Linux **完全不同**: Linux 上它只允许复用
    # 处于 TIME_WAIT 的地址, 而 Windows 上它允许绑定一个**别的进程正在监听的
    # 端口** —— 也就是把别人的端口抢过来, 之后发给对方的连接会跑到我们这里。
    # 这是极难排查的故障源。Windows 上正确的做法是显式声明独占。
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(128)
    ok(f"免节点直连加速已启动: socks5://{host}:{port}")
    info(f"加速域名: {len(ACCEL_SUFFIXES)} 类(GitHub / raw.githubusercontent / githubassets / ghcr 等)")
    warm_up()
    info(f"已开始预热 {len(IP_POOLS)} 个域名的 IP 优选缓存")
    while True:
        try:
            client, _ = srv.accept()
        except OSError:
            break
        threading.Thread(
            target=_handle_client, args=(client,), kwargs={"verbose": verbose}, daemon=True
        ).start()


# --------------------------------------------------------------------------- #
# 诊断
# --------------------------------------------------------------------------- #


def bench(domain: str, *, top: int = 8) -> dict[str, Any]:
    """对指定域名做一次完整的优选过程, 返回可读结果(供 CLI 展示)."""
    candidates = resolve_candidates(domain)
    results = rank_ips(domain, candidates)
    usable = [r for r in results if r.verified]
    return {
        "domain": domain,
        "accelerated": is_accelerated(domain),
        "candidates": len(candidates),
        "doh_ips": candidates[: top * 2],
        "usable": [
            {"ip": r.ip, "tcp_ms": round(r.tcp_ms or 0), "tls_ms": round(r.tls_ms or 0)}
            for r in usable[:top]
        ],
        "best": usable[0].ip if usable else None,
        "all_failed_reason": None
        if usable
        else "所有候选 IP 的 TLS 握手均失败(疑似 SNI 阻断或该域名 IP 段被封)",
    }
