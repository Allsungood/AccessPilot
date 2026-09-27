"""端到端验证脚本 (真实内核 + 真实链路).

流程:
  1. 在临时目录里搭一个隔离的运行环境(复制已安装的内核)
  2. 启动一个本地 SOCKS5 服务器, 当作"节点"
  3. 生成配置 -> 调用 mihomo -t 校验 -> 启动内核
  4. 通过 127.0.0.1:7890 真实发起请求, 验证:
       * HTTP/HTTPS 链路可用(HTTP 代理 CONNECT)
       * 分流规则命中正确(从内核日志中读取 match 记录)
       * 系统代理开关可读写
       * TUN 适配器可创建(不接管路由, 避免中断当前网络)
  5. 清理并输出结论

用法: python tests/e2e_check.py
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

PASS: list[str] = []
FAIL: list[str] = []


def step(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""), flush=True)
    return ok


def main() -> int:
    real_home = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AccessPilot"

    # 测试开始前清理任何遗留内核, 避免端口冲突污染结果
    _cleanup_leftovers()

    tmp = tempfile.mkdtemp(prefix="accesspilot-e2e-")
    os.environ["ACCESSPILOT_HOME"] = tmp
    home = Path(tmp)

    # ------------------------------------------------------------------ #
    print("\n== 1. 准备隔离环境 ==")
    (home / "core").mkdir(parents=True, exist_ok=True)
    for name in ("mihomo.exe", "wintun.dll"):
        src = real_home / "core" / name
        if src.exists():
            shutil.copy2(src, home / "core" / name)
    (home / "runtime").mkdir(exist_ok=True)
    for geo in ("geoip.metadb", "geosite.dat", "country.mmdb"):
        src = real_home / "runtime" / geo
        if src.exists():
            shutil.copy2(src, home / "runtime" / geo)
    # 规则集缓存复用: 否则每次都要重新下载 ~20 份 provider(数十 MB), 很慢
    cache = ROOT / "tests" / ".cache" / "ruleset"
    if cache.exists():
        shutil.copytree(cache, home / "runtime" / "ruleset", dirs_exist_ok=True)
        print(f"  (复用规则集缓存: {len(list(cache.iterdir()))} 个文件)")
    from accesspilot import paths

    step("隔离环境就绪", paths.core_binary().exists(), str(home))

    # ------------------------------------------------------------------ #
    print("\n== 2. 启动本地 SOCKS5 测试节点 ==")
    from helpers.socks5_server import start_background

    proxy_port = 11080
    start_background(proxy_port)
    time.sleep(0.5)
    reachable = _tcp_ok("127.0.0.1", proxy_port)
    step("本地 SOCKS5 节点已监听", reachable, f"127.0.0.1:{proxy_port}")
    if not reachable:
        return _finish(tmp)

    # ------------------------------------------------------------------ #
    print("\n== 3. 生成并校验配置 ==")
    from accesspilot import config, process, sysproxy
    from accesspilot.state import load_state, save_state
    from accesspilot.subscription import Subscription, save_profile

    nodes = [
        {
            "name": "Local-SOCKS",
            "type": "socks5",
            "server": "127.0.0.1",
            "port": proxy_port,
            "udp": True,
        },
        {
            "name": "Dead-Node",
            "type": "socks5",
            "server": "127.0.0.1",
            "port": 1,
            "udp": True,
        },
    ]
    sub = Subscription(name="e2e", proxies=nodes, url="")
    save_profile(sub)
    st = load_state()
    st.active_profile = "e2e"
    st.tun_enable = False
    st.system_proxy_on = False
    save_state(st)

    cfg_path = config.render(sub, st)
    step("配置已生成", cfg_path.exists(), f"{cfg_path.stat().st_size} bytes")

    passed, out = config.test_config(cfg_path)
    step("mihomo 配置自检通过", passed, "" if passed else out[:300])
    if not passed:
        return _finish(tmp)

    # ------------------------------------------------------------------ #
    print("\n== 4. 启动内核 ==")
    try:
        st_info = process.start(st=st, tun=False, system_proxy=False)
        step("内核进程已启动", bool(st_info.get("running")), f"pid={st_info.get('pid')}")
    except Exception as e:
        step("内核进程已启动", False, str(e))
        return _finish(tmp)

    from accesspilot import api

    step("控制接口可用", api.ping(st), api.version(st))

    # ------------------------------------------------------------------ #
    print("\n== 4b. 规则集加载验证(格式必须被内核真正解析) ==")
    # 期望值: 名称 -> 最少条目数。内核会异步下载/解析 provider,
    # 必须等到全部就绪再断言, 否则会把"还在下载"误判成"格式错误"。
    expect = {
        "proxy": 1000,
        "reject": 100,
        "direct": 100,
        "cncidr": 1000,
        "lancidr": 5,
        "telegramcidr": 5,
        "openai": 10,
        "discord": 10,
        "twitter": 10,
        "google": 100,
        "youtube": 50,
        "github": 10,
    }
    providers: dict[str, dict] = {}
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            providers = api.rule_providers(st)
        except Exception:
            providers = {}
        counts_now = {k: int((v or {}).get("ruleCount") or 0) for k, v in providers.items()}
        if all(counts_now.get(k, 0) >= v for k, v in expect.items()):
            break
        time.sleep(4)

    counts = {k: int((v or {}).get("ruleCount") or 0) for k, v in providers.items()}
    print("    实际条目数: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for name, minimum in expect.items():
        got = counts.get(name, -1)
        step(f"规则集 {name} 已解析", got >= minimum, f"{got} 条 (期望 >= {minimum})")

    log_text = paths.log_file().read_text(encoding="utf-8", errors="replace")
    bad_lines = [
        ln
        for ln in log_text.splitlines()
        if "invalid Ipcidr" in ln or "skip invalid domain" in ln
    ]
    step(
        "内核未丢弃任何规则条目(格式正确)",
        not bad_lines,
        bad_lines[0][-120:] if bad_lines else "无解析告警",
    )

    # ------------------------------------------------------------------ #
    print("\n== 5. 链路实测(经 127.0.0.1:7890) ==")
    from accesspilot.util import http_request

    local_proxy = f"http://127.0.0.1:{st.mixed_port}"

    status, body = 0, b""
    for attempt in range(4):
        try:
            status, _, body = http_request(
                "https://testingcf.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/proxy.txt",
                timeout=30,
                proxy=local_proxy,
            )
            if status == 200:
                break
        except Exception as e:
            print(f"    第 {attempt + 1} 次尝试失败: {e}")
        time.sleep(2)
    step(
        "HTTP(S) 链路经代理可用 (CONNECT 隧道)",
        status == 200 and len(body) > 1000,
        f"HTTP {status}, {len(body)} bytes",
    )

    # 触发若干目标平台请求, 以便内核日志记录规则命中
    for url in (
        "https://x.com/",
        "https://discord.com/api/v9/gateway",
        "https://chatgpt.com/cdn-cgi/trace",
    ):
        try:
            http_request(url, timeout=12, proxy=local_proxy)
        except Exception:
            pass  # 上游是否可达不重要, 我们要验证的是分流决策

    time.sleep(1.0)
    log = paths.log_file().read_text(encoding="utf-8", errors="replace")

    expectations = [
        ("x.com", "社交平台"),
        ("discord.com", "社交平台"),
        ("chatgpt.com", "AI"),
    ]
    for domain, group_key in expectations:
        hit = [
            line
            for line in log.splitlines()
            if domain in line and "match" in line.lower()
        ]
        matched_group = any(group_key in line for line in hit)
        step(
            f"分流规则命中: {domain} -> {group_key}",
            bool(hit) and matched_group,
            (hit[0].split("msg=")[-1][:110] if hit else "日志中未找到 match 记录"),
        )

    dead_lines = [x for x in log.splitlines() if "Dead-Node" in x]
    step("自动选择未使用失效节点", not dead_lines, "Dead-Node 未被选中")

    # ------------------------------------------------------------------ #
    print("\n== 6. 系统代理 ==")
    # 教训: 端到端测试绝不能真改系统代理注册表 —— 测试被外部打断(超时/杀进程)
    # 时来不及还原, 用户整台机器会断网(真实发生过两次)。
    # 开关逻辑由单元测试(mock)与一次性的受控实机验证覆盖; 这里只断言
    # "测试全程没有碰过注册表"。想真机验证时: E2E_TOUCH_SYSPROXY=1。
    if os.environ.get("E2E_TOUCH_SYSPROXY") == "1":
        try:
            detail = sysproxy.enable(st)
            enabled, server = sysproxy.status()
            step("开启系统代理", enabled and str(st.mixed_port) in str(server), detail)
            detail = sysproxy.disable(st)
            enabled, server = sysproxy.status()
            step("关闭系统代理并还原", not enabled, detail)
        except Exception as e:
            step("系统代理开关", False, str(e))
    else:
        enabled, server = sysproxy.status()
        step(
            "测试全程未改动系统代理(受控验证需 E2E_TOUCH_SYSPROXY=1)",
            not enabled,
            f"当前状态: {'开启 ' + str(server) if enabled else '关闭(测试前即为关闭)'}",
        )

    # ------------------------------------------------------------------ #
    print("\n== 7. 节点测速接口 ==")
    from accesspilot.util import Fail

    try:
        d = api.delay(st, "Local-SOCKS", timeout_ms=6000)
        step("节点延迟测试 (内核 -> 节点 -> 探测地址)", True, f"{d} ms")
    except Fail as e:
        step("节点延迟测试", False, str(e))

    # ------------------------------------------------------------------ #
    print("\n== 8. TUN 适配器创建(不接管路由) ==")
    try:
        process.stop()
        time.sleep(0.8)
        cfg2 = config.build_config(sub, st, tun=True)
        cfg2["tun"]["auto-route"] = False
        cfg2["tun"]["dns-hijack"] = []
        p2 = paths.runtime_dir() / "config_tun.yaml"
        config.write_config(cfg2, p2)
        from accesspilot.util import run_hidden

        code, out = run_hidden(
            [str(paths.core_binary()), "-t", "-f", str(p2), "-d", str(paths.runtime_dir())],
            timeout=60,
        )
        step("TUN 配置通过内核校验", code == 0, out.strip()[-160:] if code else "")

        proc = process._spawn(
            paths.core_binary(),
            ["-d", str(paths.runtime_dir()), "-f", str(p2)],
        )
        from accesspilot.util import json_dump

        json_dump(
            paths.pid_file(),
            {"pid": proc, "started_at": time.time(), "config": str(p2), "profile": "e2e"},
        )
        # 内核要先下载/解析全部 rule-provider 才会拉起 TUN, 冷启动可达 20s+
        logp = paths.log_file()
        before = logp.stat().st_size if logp.exists() else 0
        tun_ok, waited, tail = False, 0, ""
        for waited in range(1, 61):
            time.sleep(1)
            tail = logp.read_text(encoding="utf-8", errors="replace")[before:]
            if "Tun adapter listening at" in tail:
                tun_ok = True
                break
        step("wintun 驱动加载 / TUN 栈启动", tun_ok, f"{waited}s: {_tun_evidence(tail)}")
        process.stop()
        try:
            import subprocess

            subprocess.run(["taskkill", "/PID", str(proc), "/T", "/F"], capture_output=True)
        except Exception:
            pass
    except Exception as e:
        step("TUN 适配器创建", False, str(e))

    # ------------------------------------------------------------------ #
    print("\n== 9. 停止与还原 ==")
    try:
        stopped = process.stop()
        time.sleep(0.5)
        step("内核已停止", not process.is_running())
        enabled, _ = sysproxy.status()
        step("系统代理未残留", not enabled)
    except Exception as e:
        step("停止与还原", False, str(e))

    return _finish(tmp)


def _tun_evidence(tail: str) -> str:
    for line in tail.splitlines():
        if "TUN" in line or "tun" in line:
            return line.split("msg=")[-1][:120]
    return tail.splitlines()[-1][:120] if tail else ""


def _cleanup_leftovers() -> None:
    """用真实 HOME 清理可能残留的内核进程(不触碰用户配置)."""
    try:
        sys.path.insert(0, str(ROOT))
        from accesspilot import process as proc_mod

        st = proc_mod.load_state()
        st.system_proxy_on = False
        proc_mod.stop(clean_orphans=True)
    except Exception as e:
        print(f"  (清理残留内核时忽略错误: {e})")


def _tcp_ok(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _finish(tmp: str) -> int:
    # 兜底清理: 不让测试在内核/系统代理上留下痕迹
    try:
        os.environ["ACCESSPILOT_HOME"] = tmp
        import accesspilot.process as proc_mod

        proc_mod.stop(keep_system_proxy=False)
    except Exception:
        pass
    # 保存规则集缓存供下次复用
    try:
        src = Path(tmp) / "runtime" / "ruleset"
        if src.exists():
            dst = ROOT / "tests" / ".cache" / "ruleset"
            shutil.copytree(src, dst, dirs_exist_ok=True)
    except Exception:
        pass
    print("\n" + "=" * 58)
    print(f"通过 {len(PASS)} 项, 失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
