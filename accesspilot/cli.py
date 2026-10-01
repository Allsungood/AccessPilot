"""AccessPilot 命令行入口."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, api, config, coreinstall, diag, intent, paths, process, rules, sysproxy
from .state import load_state, save_state
from .subscription import (
    delete_profile,
    fetch,
    list_profiles,
    load_profile,
    save_profile,
)
from .util import (
    Fail,
    bold,
    cyan,
    dim,
    err,
    green,
    info,
    is_admin,
    ok,
    red,
    run_hidden,
    warn,
    yellow,
)


# --------------------------------------------------------------------------- #
# 输出辅助
# --------------------------------------------------------------------------- #


def table(headers: list[str], rows: list[list[Any]], *, padding: int = 2) -> str:
    if not rows:
        return dim("  (无数据)")
    widths = [len(str(h)) for h in headers]
    cells = [[str(c) for c in row] for row in rows]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], _display_width(c))
    lines = []
    head = (" " * padding).join(
        _pad(str(h), widths[i]) for i, h in enumerate(headers)
    )
    lines.append(bold(head))
    lines.append(dim("-" * _display_width(head)))
    for row in cells:
        lines.append(
            (" " * padding).join(_pad(c, widths[i]) for i, c in enumerate(row))
        )
    return "\n".join(lines)


def _display_width(text: str) -> int:
    """粗略计算显示宽度(中日韩字符算 2 列)."""
    width = 0
    for ch in text:
        width += 2 if _is_wide(ch) else 1
    return width


def _is_wide(ch: str) -> bool:
    code = ord(ch)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x1F300 <= code <= 0x1FAFF
        or 0x2600 <= code <= 0x27BF
    )


def _pad(text: str, width: int) -> str:
    return text + " " * max(width - _display_width(text), 0)


def latency_badge(ms: int) -> str:
    text = f"{ms} ms"
    if ms < 0:
        return red("超时")
    if ms < 200:
        return green(text)
    if ms < 500:
        return yellow(text)
    return red(text)


def _json_out(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# core
# --------------------------------------------------------------------------- #


def cmd_core(args: argparse.Namespace) -> int:
    if args.action in ("install", "update"):
        version = coreinstall.install_core(args.version, force=args.action == "update" or args.force)
    elif args.action == "version":
        v = coreinstall.installed_version()
        print(v or "未安装")
    elif args.action == "path":
        print(paths.core_binary())
    return 0


# --------------------------------------------------------------------------- #
# sub
# --------------------------------------------------------------------------- #


def cmd_sub(args: argparse.Namespace) -> int:
    st = load_state()
    paths.ensure_dirs()

    if args.action == "add":
        info(f"拉取订阅: {args.url}")
        sub = fetch(args.url, args.name)
        slug = save_profile(sub)
        ok(
            f"已保存配置档 {bold(slug)}: {len(sub.proxies)} 个节点 "
            f"[{sub.source_format}]"
        )
        print(f"    流量: {sub.traffic_text()}")
        print(f"    到期: {sub.expire_text()}")
        if not st.active_profile:
            st.active_profile = slug
            save_state(st)
            ok(f"已设为当前配置档: {slug}")
        elif args.use:
            st.active_profile = slug
            save_state(st)
            ok(f"已切换当前配置档: {slug}")
        return 0

    if args.action == "list":
        subs = list_profiles()
        if args.json:
            _json_out(
                [
                    {
                        "name": s.name,
                        "nodes": len(s.proxies),
                        "traffic": s.traffic_text(),
                        "expire": s.expire_text(),
                        "updated": time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime(s.updated)
                        ),
                        "active": s.name == st.active_profile,
                    }
                    for s in subs
                ]
            )
            return 0
        rows = []
        for s in subs:
            rows.append(
                [
                    ("* " if s.name == st.active_profile else "  ") + s.name,
                    len(s.proxies),
                    s.traffic_text(),
                    s.expire_text(),
                    time.strftime("%m-%d %H:%M", time.localtime(s.updated)),
                ]
            )
        print(table(["配置档", "节点", "流量", "到期", "更新时间"], rows))
        print(dim("  * 表示当前使用中的配置档"))
        return 0

    if args.action in ("update", "refresh"):
        targets = [args.name] if args.name else [s.name for s in list_profiles()]
        if not targets:
            raise Fail("没有可更新的配置档")
        for name in targets:
            sub = load_profile(name)
            if not sub.url:
                warn(f"{name}: 没有订阅地址, 跳过")
                continue
            try:
                fresh = fetch(sub.url, name)
            except Fail as e:
                warn(f"{name} 更新失败: {e}")
                continue
            if not fresh.proxies:
                warn(f"{name} 更新后没有节点, 保留原数据")
                continue
            save_profile(fresh)
            ok(f"{name}: {len(fresh.proxies)} 个节点 | {fresh.traffic_text()}")
        if process.is_running():
            st = load_state()
            if st.active_profile in targets:
                process.reload_config(load_profile(st.active_profile), st)
        return 0

    if args.action == "use":
        load_profile(args.name)  # 校验存在
        st.active_profile = args.name
        save_state(st)
        ok(f"当前配置档: {args.name}")
        if process.is_running():
            process.reload_config(load_profile(args.name), st)
        return 0

    if args.action == "rm":
        delete_profile(args.name)
        if st.active_profile == args.name:
            st.active_profile = ""
            save_state(st)
        return 0

    if args.action == "show":
        name = args.name or st.active_profile
        if not name:
            raise Fail("未指定配置档, 且当前没有使用中的配置档")
        sub = load_profile(name)
        print(bold(f"配置档 {sub.name}"))
        print(f"  订阅地址: {sub.url or '(本地导入)'}")
        print(f"  节点数量: {len(sub.proxies)}")
        print(f"  流量    : {sub.traffic_text()}")
        print(f"  到期    : {sub.expire_text()}")
        print(f"  更新时间: {time.strftime('%Y-%m-%d %H:%M', time.localtime(sub.updated))}")
        rows = []
        for i, p in enumerate(sub.proxies, 1):
            rows.append([i, p.get("name", ""), p.get("type", ""), p.get("server", "")])
        print(table(["#", "名称", "协议", "服务器"], rows[: args.limit]))
        if len(sub.proxies) > args.limit:
            print(dim(f"  ... 共 {len(sub.proxies)} 个, 已显示前 {args.limit} 个"))
        return 0

    raise Fail(f"未知操作: {args.action}")


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def cmd_config(args: argparse.Namespace) -> int:
    st = load_state()
    if args.action == "build":
        name = args.name or st.active_profile
        if not name:
            raise Fail("未指定配置档")
        sub = load_profile(name)
        tun = None if args.tun is None else args.tun
        cfg_path = config.render(sub, st, tun=tun)
        ok(f"配置已生成: {cfg_path}")
        return 0
    if args.action == "show":
        print(paths.config_file().read_text(encoding="utf-8"))
        return 0
    if args.action == "test":
        passed, output = config.test_config()
        if passed:
            ok("配置校验通过")
        else:
            err("配置校验失败")
            print(output)
            return 1
        return 0
    if args.action == "path":
        print(paths.config_file())
        return 0
    if args.action == "set-port":
        if not args.port:
            raise Fail("请提供端口号, 例如: accesspilot config set-port 7897")
        st.mixed_port = args.port
        if args.api_port:
            st.api_port = args.api_port
        save_state(st)
        ok(f"代理端口已设置为 {st.mixed_port}, 控制端口 {st.api_port}")
        if process.is_running():
            info("内核正在运行, 执行 accesspilot restart 生效")
        return 0
    if args.action == "lan":
        st.allow_lan = bool(args.on)
        save_state(st)
        ok(f"局域网共享已{'开启' if st.allow_lan else '关闭'}")
        return 0
    raise Fail(f"未知操作: {args.action}")


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #


def cmd_start(args: argparse.Namespace) -> int:
    st = load_state()
    if args.tun:
        st.tun_enable = True
    if args.no_tun:
        st.tun_enable = False
    if args.profile:
        load_profile(args.profile)
        st.active_profile = args.profile
    save_state(st)
    system_proxy: bool | None = None
    if args.sysproxy:
        system_proxy = True
    if args.no_sysproxy:
        system_proxy = False
    process.start(st=st, tun=st.tun_enable, system_proxy=system_proxy)
    # 命令行 start 也是"用户主动开", 要覆盖掉之前可能存在的"关"记录。
    intent.mark_on()
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    if process.stop():
        ok("内核已停止")
    else:
        info("内核未在运行")
    # 命令行 stop 和界面上点「关闭」是同一件事, 都要记进意图 —— 否则
    # 命令行关掉之后保活又会把它拉回来, 用户看到的是"stop 不管用"。
    intent.mark_off()
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    st = load_state()
    if args.tun:
        st.tun_enable = True
    if args.no_tun:
        st.tun_enable = False
    save_state(st)
    process.restart(st=st, tun=st.tun_enable, system_proxy=not st.tun_enable)
    intent.mark_on()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    st = load_state()
    data = process.status()
    if args.json:
        _json_out(data)
        return 0
    _print_status(st, data)
    return 0


def _print_status(st: Any, data: dict[str, Any]) -> None:
    running = data["running"]
    head = f"{bold('AccessPilot')} v{__version__}"
    print(head)
    print(dim("-" * 46))
    state_text = green("运行中") if running else red("未运行")
    pid = f" pid={data['pid']}" if data.get("pid") else ""
    print(f"  内核状态 : {state_text}{pid}  {data.get('core_version') or ''}")
    profile = data.get("profile") or ""
    nodes = ""
    if profile:
        try:
            nodes = f" ({len(load_profile(profile).proxies)} 节点)"
        except Fail:
            nodes = " (配置档缺失)"
    print(f"  当前配置 : {profile or '(未选择)'}{nodes}")
    print(f"  代理端口 : 127.0.0.1:{st.mixed_port}   [socks5/http 混合]")
    print(f"  控制面板 : http://127.0.0.1:{st.api_port}/ui")
    sp = green("已开启") if data["system_proxy"] else dim("已关闭")
    tun = green("已开启") if st.tun_enable else dim("已关闭")
    print(f"  系统代理 : {sp}    TUN: {tun}")
    print(f"  日志文件 : {data['log']}")


def cmd_log(args: argparse.Namespace) -> int:
    if args.follow:
        path = paths.log_file()
        if not path.exists():
            raise Fail("暂无日志文件")
        info(f"跟踪日志 {path} (Ctrl+C 退出)")
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, 2)
            try:
                while True:
                    line = fh.readline()
                    if not line:
                        time.sleep(0.5)
                        continue
                    sys.stdout.write(line)
                    sys.stdout.flush()
            except KeyboardInterrupt:
                return 0
    print(process.tail_log(args.lines))
    return 0


# --------------------------------------------------------------------------- #
# proxy / tun
# --------------------------------------------------------------------------- #


def cmd_proxy(args: argparse.Namespace) -> int:
    st = load_state()
    if args.action == "on":
        detail = sysproxy.enable(st)
        save_state(st)
        ok(detail)
    elif args.action == "off":
        detail = sysproxy.disable(st)
        save_state(st)
        ok(detail)
    elif args.action == "status":
        enabled, server = sysproxy.status()
        if enabled:
            ok(f"系统代理已开启: {server}")
        else:
            info("系统代理未开启")
    elif args.action == "env":
        if args.off:
            sysproxy.clear_env_proxy()
            ok("已清除终端代理环境变量")
        else:
            sysproxy.set_env_proxy(f"127.0.0.1:{st.mixed_port}")
            ok("已为终端写入 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY")
    return 0


def cmd_tun(args: argparse.Namespace) -> int:
    st = load_state()
    if args.action in ("on", "off"):
        available, reason = sysproxy.tun_available()
        if args.action == "on" and not available:
            raise Fail(f"TUN 不可用: {reason}")
        st.tun_enable = args.action == "on"
        save_state(st)
        if process.is_running():
            process.restart(st=st, tun=st.tun_enable, system_proxy=not st.tun_enable)
        else:
            ok(f"已设置 TUN={'开启' if st.tun_enable else '关闭'}, 下次启动生效")
    elif args.action == "status":
        available, reason = sysproxy.tun_available()
        print(f"  TUN 配置 : {'开启' if st.tun_enable else '关闭'}")
        print(f"  可用性   : {'可用' if available else '不可用'} ({reason})")
    return 0


# --------------------------------------------------------------------------- #
# node
# --------------------------------------------------------------------------- #


def cmd_node(args: argparse.Namespace) -> int:
    st = load_state()
    if not process.is_running():
        raise Fail("内核未运行, 请先执行: accesspilot start")
    data = api.proxies(st)
    groups = {k: v for k, v in data.items() if v.get("all")}

    if args.action == "list":
        rows = []
        for name, g in groups.items():
            rows.append([name, g.get("type", ""), g.get("now", ""), len(g.get("all") or [])])
        print(table(["策略组", "类型", "当前节点", "候选数"], rows))
        return 0

    if args.action == "use":
        if args.group not in groups:
            raise Fail(f"策略组不存在: {args.group}")
        if args.node not in (groups[args.group].get("all") or []):
            raise Fail(f"节点不在该策略组候选中: {args.node}")
        api.select(st, args.group, args.node)
        st.selected[args.group] = args.node
        save_state(st)
        ok(f"{args.group} -> {args.node}")
        return 0

    if args.action == "delay":
        group = args.group
        if group:
            if group not in groups:
                raise Fail(f"策略组不存在: {group}")
            info(f"正在测试 {group} 下所有节点 ...")
            results = api.group_delay(st, group, timeout_ms=args.timeout)
            names = list(results.items())
        else:
            targets = groups.get(rules.G_AUTO, {}).get("all") or []
            if not targets:
                targets = list(config.proxy_names(load_profile(st.active_profile)))
            info(f"正在测试 {len(targets)} 个节点 ...")
            names = []
            for n in targets:
                try:
                    names.append((n, api.delay(st, n, timeout_ms=args.timeout)))
                except Fail:
                    names.append((n, -1))
        names.sort(key=lambda kv: (kv[1] < 0, kv[1]))
        rows = [
            [i, n, (f"{d} ms" if d >= 0 else "超时")] for i, (n, d) in enumerate(names, 1)
        ]
        print(table(["#", "节点", "延迟"], rows))
        if args.select_first and names and names[0][1] >= 0:
            best = names[0][0]
            api.select(st, rules.G_AUTO, best)
            ok(f"已选择最快节点: {best}")
        return 0

    if args.action == "groups":
        for name, g in groups.items():
            print(f"{bold(name)} [{g.get('type')}] 当前: {g.get('now')}")
            for n in g.get("all") or []:
                mark = "*" if n == g.get("now") else " "
                print(f"   {mark} {n}")
        return 0

    raise Fail(f"未知操作: {args.action}")


# --------------------------------------------------------------------------- #
# 诊断
# --------------------------------------------------------------------------- #


def cmd_test(args: argparse.Namespace) -> int:
    st = load_state()
    running = process.is_running()
    proxy = diag.proxy_url(st) if running else None
    if not running:
        warn("内核未运行, 本次为直连(未加速)测试, 用于对照")

    keys = args.site or None
    info("检测出口 IP ...")
    exit_ip = diag.exit_info(proxy)
    trace = diag.chatgpt_trace(proxy)

    info("检测目标平台连通性 ...")
    results = diag.probe_sites(proxy, keys=keys, timeout=args.timeout)

    if args.json:
        _json_out(
            {
                "proxy": proxy,
                "exit": exit_ip,
                "chatgpt_trace": trace,
                "results": [r.as_dict() for r in results],
            }
        )
        return 0

    print()
    print(bold("出口信息"))
    print(dim("  通用流量与 ChatGPT 走不同的策略组, 出口很可能是两个国家 —— 分开看"))
    if exit_ip.get("error"):
        print(f"  通用流量     : {red('查询失败')}: {exit_ip['error']}")
    else:
        print(
            f"  通用流量     : {exit_ip.get('query')}  "
            f"{exit_ip.get('country')} {exit_ip.get('city') or ''}"
        )
        print(f"                 {exit_ip.get('isp')}")
        if exit_ip.get("hosting"):
            warn("该 IP 属于数据中心/机房, ChatGPT 风控可能更严, 住宅 IP 成功率更高")

    # ChatGPT 的出口必须以 CF trace 为准: 它经 🤖 AI 服务 组出去, 和通用流量
    # 落在不同国家是常态。以前这里只打通用出口, 于是会看到
    # "ChatGPT: 不受支持 - 中国香港" 紧挨着 "ChatGPT 网页版 通过" 的矛盾输出,
    # 让人误以为 ChatGPT 走的是香港(用户截图里那个香港 IPv6 就是这么来的)。
    if trace and not trace.get("error"):
        loc = str(trace.get("loc") or "?").upper()
        verdict = (
            red("不受支持") + f" - {trace.get('region_note')}"
            if trace.get("openai_blocked")
            else green("可用")
        )
        print(f"  ChatGPT 出口 : {trace.get('ip')}  {loc}")
        print(f"                 {verdict}   (warp={trace.get('warp', '-')})")
    elif trace:
        print(f"  ChatGPT 出口 : {red('读取失败')}: {trace['error']}")

    print()
    print(bold("目标平台连通性"))
    rows = []
    for r in results:
        rows.append(
            [
                r.name,
                green("通过") if r.ok else red("失败"),
                latency_badge(r.latency_ms),
                r.detail,
            ]
        )
    print(table(["平台", "结果", "延迟", "说明"], rows))

    failed = [r for r in results if not r.ok]
    print()
    if not failed:
        ok("全部目标平台连通正常")
        return 0
    warn(f"{len(failed)} 个平台未通过: {', '.join(r.name for r in failed)}")
    print(dim("  排查建议: 1) 换一个节点重试  2) accesspilot node delay  3) 查看日志"))
    return 1


def cmd_ip(args: argparse.Namespace) -> int:
    st = load_state()
    proxy = diag.proxy_url(st) if process.is_running() else None
    data = diag.exit_info(proxy)
    if args.json:
        _json_out(data)
        return 0
    if data.get("error"):
        raise Fail(f"查询失败: {data['error']}")
    print(f"出口 IP  : {data.get('query')}")
    print(f"位置     : {data.get('country')} {data.get('regionName')} {data.get('city')}")
    print(f"运营商   : {data.get('isp')} / {data.get('org')}")
    print(f"机房属性 : {'是(数据中心 IP)' if data.get('hosting') else '否'}")
    print(f"代理标记 : {'是' if data.get('proxy') else '否'}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []

    checks.append(
        ("Python 版本", sys.version_info >= (3, 9), f"{sys.version.split()[0]}")
    )

    v = coreinstall.installed_version()
    checks.append(
        ("内核 mihomo", bool(v), v or "未安装, 执行 accesspilot core install")
    )
    checks.append(("内核路径", paths.core_binary().exists(), str(paths.core_binary())))

    if sys.platform == "win32":
        checks.append(
            (
                "TUN 驱动 wintun.dll",
                paths.wintun_dll().exists(),
                str(paths.wintun_dll()),
            )
        )
        checks.append(
            ("管理员权限", is_admin(), "TUN 模式必需, 系统代理模式不要求")
        )
    else:
        checks.append(("root 权限", is_admin(), "TUN 模式必需"))

    for geo in ("geoip.metadb", "geosite.dat", "country.mmdb"):
        p = paths.runtime_dir() / geo
        checks.append((f"GeoIP 数据 {geo}", p.exists() and p.stat().st_size > 1024, str(p)))

    st = load_state()
    checks.append(("配置文件", bool(st.active_profile), st.active_profile or "未选择配置档"))

    # 端口占用
    import socket

    for label, port in (("代理端口", st.mixed_port), ("控制端口", st.api_port)):
        free = True
        detail = f"{port} 可用"
        try:
            with socket.socket() as s:
                s.settimeout(0.5)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    free = False
                    detail = f"{port} 已被占用" + (
                        " (本工具内核)" if process.is_running() else " (可能是其他程序)"
                    )
        except Exception:
            pass
        checks.append((label, free, detail))

    # 系统代理状态一致性
    enabled, server = sysproxy.status()
    if enabled and not process.is_running():
        checks.append(
            ("系统代理一致性", False, f"系统代理指向 {server} 但内核未运行, 会导致断网")
        )
        checks.append(("修复建议", True, "执行 accesspilot proxy off"))
    else:
        checks.append(("系统代理一致性", True, f"{'已开启 ' + server if enabled else '未开启'}"))

    # DNS 污染
    dns = diag.system_dns_probe("www.google.com")
    checks.append(("系统 DNS 抗污染", dns["ok"], f"{dns['detail']}: {dns['ips'][:3]}"))

    # 镜像可达性
    try:
        from .util import http_request

        status, _, _ = http_request(
            "https://testingcf.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/proxy.txt",
            timeout=8,
        )
        checks.append(("规则集镜像 (jsDelivr)", status == 200, f"HTTP {status}"))
    except Exception as e:
        checks.append(("规则集镜像 (jsDelivr)", False, str(e)[:60]))

    if args.json:
        _json_out([{"name": n, "ok": o, "detail": d} for n, o, d in checks])
        return 0

    rows = [
        [n, green("通过") if o else red("异常"), d] for n, o, d in checks
    ]
    print(table(["检查项", "状态", "说明"], rows))
    bad = [n for n, o, _ in checks if not o]
    print()
    if bad:
        warn(f"存在 {len(bad)} 项异常: {', '.join(bad)}")
        return 1
    ok("环境自检全部通过")
    return 0


# --------------------------------------------------------------------------- #
# 面板 / 杂项
# --------------------------------------------------------------------------- #


def cmd_ui(args: argparse.Namespace) -> int:
    from . import webgui

    if args.action == "install":
        webgui.install_dashboard_ui(force=args.force)
    elif args.action == "open":
        from .util import open_in_browser

        st = load_state()
        url = f"http://127.0.0.1:{st.api_port}/ui"
        info(f"打开内核自带面板: {url}")
        open_in_browser(url)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from . import webgui

    return webgui.serve(port=args.port, open_browser=args.open)


def cmd_gui(args: argparse.Namespace) -> int:
    """红杏: 桌面客户端(主窗口 + 系统托盘).

    和 `dashboard` 的区别: dashboard 是本地网页控制台(要自己开浏览器),
    gui 是双击就能用的原生客户端 —— 一个大开关、一个托盘图标, 面向
    不懂代理的用户。托盘挂不起来时会自动退化成普通窗口, 不会启动失败。
    """
    from . import gui

    argv = ["--no-tray"] if getattr(args, "no_tray", False) else []
    return gui.main(argv)


def cmd_accel(args: argparse.Namespace) -> int:
    """免节点直连加速: 靠 IP 优选救回被 DNS 污染/丢包的资源(GitHub 系)."""
    from . import accel

    if args.action == "serve":  # 内部命令: 由 start_accel 拉起
        accel.serve(port=args.port, verbose=args.verbose)
        return 0

    st = load_state()

    if args.action in ("on", "off"):
        st.accel_enable = args.action == "on"
        save_state(st)
        if st.accel_enable:
            pid = process.start_accel(st)
            ok(f"直连加速已开启: socks5://127.0.0.1:{st.accel_port} (pid={pid})")
            print(dim("  加速对象: GitHub / raw.githubusercontent / githubassets / ghcr 等"))
            print(dim("  说明: 对 ChatGPT/Discord/X 无效 —— 它们被 SNI 阻断或地区封禁, 必须有境外节点"))
        else:
            process.stop_accel()
            ok("直连加速已关闭")
        if process.is_running() and st.active_profile:
            try:
                process.reload_config(load_profile(st.active_profile), st)
                ok("配置已热重载")
            except Fail as e:
                warn(f"配置热重载失败, 请手动 restart: {e}")
        return 0

    if args.action == "status":
        running = process.accel_running()
        rows = [
            ["开关", "开启" if st.accel_enable else "关闭"],
            ["进程", f"运行中 (pid={process.status().get('pid')})" if running else "未运行"],
            ["监听", f"socks5://127.0.0.1:{st.accel_port}"],
        ]
        print(table(["项目", "状态"], rows))
        print()
        print(bold("当前优选的域名缓存"))
        stats = accel.CACHE.stats()
        if not stats["entries"]:
            print(dim("  (本次进程尚无缓存; 可运行 accesspilot accel bench 实测)"))
        else:
            for d, info_ in stats["entries"].items():
                print(f"  {d:<34} {', '.join(info_['ips']) or '(无可用 IP)'}")
        if args.log:
            print()
            print(bold("加速器日志"))
            print(process.accel_log_tail(40))
        return 0

    if args.action == "bench":
        domains = args.domain or list(dict.fromkeys([
            "raw.githubusercontent.com", "github.com", "github.githubassets.com",
            "codeload.github.com", "ghcr.io",
        ]))
        rows = []
        for d in domains:
            info(f"优选 {d} ...")
            r = accel.bench(d, top=3)
            if r["usable"]:
                best = r["usable"][0]
                rows.append([
                    d,
                    green("可加速"),
                    best["ip"],
                    f"{best['tcp_ms']}ms",
                    f"{best['tls_ms']}ms",
                ])
            else:
                rows.append([d, red("不可加速"), "-", "-", r["all_failed_reason"][:28]])
        print()
        print(table(["域名", "结论", "最优 IP", "TCP", "TLS"], rows))
        return 0

    if args.action == "log":
        print(process.accel_log_tail(args.lines))
        return 0

    raise Fail(f"未知操作: {args.action}")


def cmd_warp(args: argparse.Namespace) -> int:
    """Cloudflare WARP: 零账号零信用卡的免费境外出口.

    实测提醒: 在中国大陆多数网络下, WARP 的 WireGuard 握手包会被直接丢弃
    (内核日志停在 "Sending handshake initiation" 就再无下文), 因此不一定可用。
    但它零成本、不需要任何付款方式, 值得一试。
    """
    from . import warp

    st = load_state()

    if args.action == "register":
        if warp.load_profile() and not args.force:
            raise Fail("已注册过 WARP 设备, 如需重新注册请加 --force")
        info("正在向 Cloudflare 注册 WARP 设备(无需邮箱/账号/付款方式) ...")
        prof = warp.register()
        ok(f"注册成功: {prof.device_id}")
        print(f"    出口地址 : {prof.address_v4}  {prof.address_v6}")
        print(f"    接入点   : {prof.endpoint}")
        print(f"    账户类型 : {prof.account_type}")
        print()
        info("WARP 已作为节点 ☁️ WARP 加入配置, 可在 🤖 AI 服务 组里选中它")
        print(dim("    验证是否真的可用(握手被墙会显示超时):"))
        print(dim("      accesspilot start && accesspilot node delay"))
        return 0

    if args.action == "status":
        prof = warp.load_profile()
        if not prof:
            info("尚未注册 WARP。运行 accesspilot warp register 免费注册一个出口")
            return 0
        rows = [
            ["设备 ID", prof.device_id],
            ["账户类型", prof.account_type],
            ["出口 IPv4", prof.address_v4],
            ["出口 IPv6", prof.address_v6],
            ["接入点", prof.endpoint],
            ["借道节点", st.warp_dialer or "(直连)"],
            ["注册时间", time.strftime("%Y-%m-%d %H:%M", time.localtime(prof.created))],
        ]
        print(table(["项目", "值"], rows))
        return 0

    if args.action == "license":
        prof = warp.load_profile()
        if not prof:
            raise Fail("请先执行 accesspilot warp register")
        if not args.key:
            raise Fail("请提供 WARP+ / Zero Trust 的 license key")
        warp.apply_license(prof, args.key)
        ok("license 已绑定, 账户类型: " + prof.account_type)
        return 0

    if args.action == "chain":
        if args.node:
            from .subscription import load_profile as _load

            if not st.active_profile:
                raise Fail("请先选择一个配置档, 才能指定借道节点")
            sub = _load(st.active_profile)
            if args.node not in [str(p.get("name")) for p in sub.proxies]:
                raise Fail(f"配置档中没有名为 {args.node} 的节点")
        st.warp_dialer = args.node or ""
        save_state(st)
        ok(f"WARP 握手将借道: {st.warp_dialer or '直连'}")
        if process.is_running() and st.active_profile:
            process.reload_config(load_profile(st.active_profile), st)
            ok("配置已热重载")
        return 0

    if args.action == "endpoint":
        prof = warp.load_profile()
        if not prof:
            raise Fail("请先执行 accesspilot warp register")
        if not args.value:
            print("可用接入点:")
            for ep in warp.DEFAULT_ENDPOINTS:
                print(f"  {ep}")
            return 0
        prof.endpoint = args.value
        warp.save_profile(prof)
        ok(f"接入点已设为 {prof.endpoint}")
        if process.is_running() and st.active_profile:
            process.reload_config(load_profile(st.active_profile), st)
            ok("配置已热重载")
        return 0

    if args.action == "rm":
        if warp.delete_profile():
            ok("已删除 WARP 配置")
        else:
            info("没有 WARP 配置")
        return 0

    raise Fail(f"未知操作: {args.action}")


def cmd_free(args: argparse.Namespace) -> int:
    """公开免费节点: 抓取 -> 并发测速 -> 只保留可用的.

    注意: 免费节点实测可以上 Discord / X / Google, 但**上不了 ChatGPT**
    (出口 IP 被 OpenAI 以 403 拒绝)。详见 README。
    """
    from . import freenodes

    st = load_state()
    cache = paths.cache_dir() / "free_latency.json"

    if args.action == "sources":
        rows = [[i, name, f"https://testingcf.jsdelivr.net/{path}"]
                for i, (name, path) in enumerate(freenodes.SOURCES, 1)]
        print(table(["#", "公开源", "镜像地址"], rows))
        return 0

    if args.action == "fetch":
        sub = freenodes.fetch_all()
        slug = save_profile(sub)
        ok(f"已保存配置档 {slug}: {len(sub.proxies)} 个节点")
        if not st.active_profile or args.use:
            st.active_profile = slug
            save_state(st)
            ok(f"已设为当前配置档: {slug}")
        print(dim(f"  下一步: accesspilot start  然后  accesspilot free test"))
        return 0

    if args.action in ("test", "auto"):
        if args.action == "auto":
            sub = freenodes.fetch_all(verbose=True)
            # 大源动辄上万个节点: 先用裸 TCP 淘汰"服务器都连不上"的,
            # 否则内核配置要几分钟才能加载完, 内存也会暴涨。
            before = len(sub.proxies)
            if before > 800:
                print(bold(f"TCP 预筛 {before} 个节点(淘汰连不上服务器的) ..."))
                t0 = time.time()

                def pshow(done: int, all_: int) -> None:
                    sys.stdout.write(f"\r    {done}/{all_}  已用 {time.time()-t0:.0f}s   ")
                    sys.stdout.flush()

                kept, dropped = freenodes.tcp_prefilter(sub.proxies, progress=pshow)
                sys.stdout.write("\r" + " " * 60 + "\r")
                ok(f"预筛完成: {before} -> {len(kept)} 个 (淘汰 {dropped} 个死节点, "
                   f"用时 {time.time()-t0:.0f}s)")
                sub.proxies = kept
            save_profile(sub)
            st.active_profile = "free"
            save_state(st)
            if process.is_running():
                # 内核已在运行: 必须热重载, 否则测速用的是旧节点列表,
                # 新抓的节点一个都测不到
                info("内核已在运行, 热重载新节点 ...")
                process.reload_config(sub, st)
            else:
                info("正在启动内核 ...")
                # 刻意不动系统代理: 免费节点极不稳定, 擅自改系统代理会导致
                # 用户整台机器断网。要用的时候由用户自己执行 accesspilot proxy on。
                process.start(st=st, tun=False, system_proxy=False)
                print(dim("  提示: 内核只是跑起来了, 你的系统代理没有被改动。"))
                print(dim("        确定要用时执行:  accesspilot proxy on"))

        if not process.is_running():
            raise Fail("内核未运行, 请先执行 accesspilot start")

        try:
            sub = load_profile("free")
        except Fail:
            raise Fail("尚未抓取免费节点, 请先执行 accesspilot free fetch") from None
        names = [str(p["name"]) for p in sub.proxies]
        if not names:
            raise Fail("配置档 free 里没有节点")

        print(bold(f"开始并发测速 {len(names)} 个节点 (超时 {args.timeout}s, {args.workers} 并发)"))
        total = len(names)
        t0 = time.time()

        def show(done: int, all_: int) -> None:
            pct = done * 100 // max(all_, 1)
            sys.stdout.write(f"\r    进度 {done}/{all_} ({pct}%)  已用 {time.time()-t0:.0f}s   ")
            sys.stdout.flush()

        alive = freenodes.bulk_test(
            st, names, workers=args.workers,
            timeout_ms=int(args.timeout * 1000), progress=show,
        )
        sys.stdout.write("\r" + " " * 60 + "\r")
        util_json = {"tested_at": time.time(), "alive": alive}
        from .util import json_dump

        json_dump(cache, util_json)
        print()
        ok(f"测速完成, 用时 {time.time()-t0:.0f}s: {len(alive)}/{total} 个可用 "
           f"({len(alive)*100//max(total,1)}%)")

        if not alive:
            warn("没有任何可用节点。公开免费源随时可能整批失效, 建议稍后重试")
            print(dim(f"  {freenodes.SECURITY_NOTICE}"))
            return 1

        rows = [[i, n[:44], f"{d} ms"] for i, (n, d) in
                enumerate(list(alive.items())[: args.top], 1)]
        print(table(["#", "节点", "延迟"], rows))

        # "延迟快"不等于"能用": 对最快的若干节点做真实平台验证
        keep: dict[str, int] = dict(alive)
        verified_ok = False  # 是否有节点通过"平台级验证"(能真正打开 X/Discord)
        ai_node: str = ""    # 实测能真正打开 ChatGPT 的最快节点(钉进 🤖 AI 服务)
        if args.action == "auto":
            candidates = [n for n, d in alive.items() if d <= args.min_ms][: args.verify_top]
            if candidates:
                print()
                print(bold(f"平台级验证 {len(candidates)} 个最快节点 (X 主页 + X 静态资源 + Discord)"))
                print(dim("  顺序验证, 每个节点约 5~15 秒, 请耐心等待 ..."))
                t1 = time.time()

                def vshow(done: int, all_: int) -> None:
                    sys.stdout.write(f"\r    验证进度 {done}/{all_}  已用 {time.time()-t1:.0f}s   ")
                    sys.stdout.flush()

                results = freenodes.verify_many(
                    st, candidates, timeout=args.verify_timeout, progress=vshow,
                )
                sys.stdout.write("\r" + " " * 60 + "\r")
                good = [r for r in results if freenodes.fully_usable(r)]
                if good:
                    # 按平台验证实测的 X 延迟排序 —— 不能用初筛测速的顺序,
                    # 否则会选中"测速快但实际打开 X 要十几秒"的节点(真实踩过)
                    keep = {
                        r["name"]: max(int(r.get("latency_ms") or 1), 1)
                        for r in good
                    }
                    keep = dict(sorted(keep.items(), key=lambda kv: kv[1]))
                    verified_ok = True

                    vrows = []
                    for r in sorted(good, key=lambda r: r.get("latency_ms") or 99999):
                        vrows.append([
                            r["name"][:40],
                            f"{r.get('latency_ms')} ms" if r.get("latency_ms", -1) > 0 else "-",
                            green("V") if r["x_ok"] else red("x"),
                            green("V") if r["x_asset_ok"] else red("x"),
                            green("V") if r["discord_ok"] else red("x"),
                            (green("V") + dim(f" {r.get('chatgpt_loc') or ''}")
                             if freenodes.chatgpt_usable(r) else red("x")),
                        ])
                    print(table(["节点", "X主页", "X静态", "Discord", "ChatGPT"],
                                [[r[0], r[2], r[3], r[4], r[5]] for r in vrows]))
                    ok(f"平台验证: {len(good)}/{len(candidates)} 个节点真正可用")

                    # ChatGPT 走独立的出口要求(X/Discord 全绿**不代表**能上
                    # ChatGPT: 香港节点就是 X/Discord 全过、ChatGPT 403)。
                    # 挑最快的可用节点钉进 AI 组。
                    ai_capable = sorted(
                        (r for r in good if freenodes.chatgpt_usable(r)),
                        key=lambda r: r.get("latency_ms") or 99999,
                    )
                    if ai_capable:
                        ai_node = ai_capable[0]["name"]
                        ok(f"ChatGPT 可用节点: {ai_node} "
                           f"(出口 {ai_capable[0].get('chatgpt_loc') or '?'}, "
                           f"{ai_capable[0].get('latency_ms')} ms) -> 将钉进 🤖 AI 服务")
                    else:
                        warn("这批节点里没有一个能打开 ChatGPT(出口地区不受支持)")
                        for r in sorted(good, key=lambda r: r.get("latency_ms") or 99999)[:3]:
                            warn(f"  {r['name'][:40]} -> {freenodes.chatgpt_reason(r)}")
                else:
                    warn("平台验证无一通过: 当前这批免费节点只能连上, 不能真正使用")
                    warn("将保留纯测速结果; 建议过几小时再 accesspilot free auto")

                # 即使 X/Discord 没过, 只要 ChatGPT 实测可用也要留下来 ——
                # 否则下一步 prune 会把它删掉, 钉进 AI 组的选择就失效了。
                for r in results:
                    if freenodes.chatgpt_usable(r) and r["name"] not in keep:
                        keep[r["name"]] = 99999
                        if not ai_node:
                            ai_node = r["name"]
            else:
                warn(f"没有延迟低于 {args.min_ms}ms 的节点, 免费节点当前整体很慢, 建议稍后再试")

        # 保护: 本批没有一个能上 ChatGPT 时, 如果**当前钉住的**那个节点还能用,
        # 就留着它 —— 每小时一次的自动刷新不该把本来能用的东西弄坏。
        # (用户遇到的故障正是这个形态: 刷新把可用的 AI 节点换成了香港节点。)
        if args.action == "auto" and process.is_running() and not ai_node:
            pinned = freenodes.current_selection(st, rules.G_AI)
            try:
                known = {str(p.get("name")) for p in load_profile("free").proxies}
            except Exception:  # noqa: PERF203
                known = set()
            if pinned and pinned in known:
                r = freenodes.verify_node(st, pinned, timeout=args.verify_timeout)
                if freenodes.chatgpt_usable(r):
                    ai_node = pinned
                    keep.setdefault(pinned, 99998)
                    verified_ok = True
                    ok(f"本批没有更好的 ChatGPT 节点, 保留现有的: {pinned[:44]} "
                       f"(出口 {r.get('chatgpt_loc') or '?'})")

        if args.action == "auto" or args.prune:
            before, after = freenodes.prune_profile("free", keep)
            ok(f"已清理节点: {before} -> {after}")

        # 安全保护: 一个通过平台验证的节点都没有时, 系统代理开着只会拖慢
        # 本来能直连的站点(实测 GitHub 从 1.1 秒变 8.5 秒)。关掉是安全方向
        # —— 只会恢复连通性, 不会弄断任何东西。
        if args.action == "auto" and not verified_ok:
            if st.system_proxy_on:
                sysproxy.disable(st)
                st.system_proxy_on = False
                save_state(st)
                warn("没有通过平台验证的节点, 已自动关闭系统代理")
                print(dim("  (否则 GitHub 等本来能直连的站点会被拖慢；"
                          "等有可用节点时再 accesspilot proxy on)"))

        if args.action == "auto" and process.is_running() and keep:
            best = next(iter(keep))
            # 先热重载让内核拿到新节点列表, 再做选择 —— 反过来的话选择会被重载冲掉。
            process.reload_config(load_profile(st.active_profile), st)

            # 顶层组指向 url-test(自动选择), 它每 5 分钟重新选最快的;
            # 社交/流媒体跟随顶层, 形成"总是走当前最快验证节点"的链条。
            for group in (rules.G_SELECT,):
                try:
                    api.select(st, group, rules.G_AUTO)
                except Exception:
                    continue
            for group in (rules.G_SOCIAL, rules.G_MEDIA):
                try:
                    api.select(st, group, rules.G_SELECT)
                except Exception:
                    continue

            # 🤖 AI 服务 **绝不能**跟随自动选择: url-test 只挑最快, 完全不看
            # 出口地区。真实故障: AI 组跟着自动选择 -> 选中香港节点 ->
            # ChatGPT 403 "Unable to load site"(用户截图里就是这个)。
            if not ai_node:
                try:
                    api.select(st, rules.G_AI, rules.G_SELECT)
                except Exception:
                    pass
                warn("没有实测能打开 ChatGPT 的节点, 🤖 AI 服务 暂时跟随自动选择")
                warn("  (自动选择只保证最快, 不保证出口地区在 OpenAI 支持列表里)")
            else:
                try:
                    api.select(st, rules.G_AI, ai_node)
                    ok(f"🤖 AI 服务 已钉住 ChatGPT 可用节点: {ai_node[:48]}")
                except Exception as e:  # noqa: PERF203
                    warn(f"钉住 AI 节点失败({e}), 回退到自动选择")
                    ai_node = ""

            ok(f"策略组已指向自动选择(当前最快: {best}, {keep[best]} ms, 每 5 分钟重选)")

        print()
        print(dim(f"  安全提醒: {freenodes.SECURITY_NOTICE}"))
        if ai_node:
            print(dim(f"  ChatGPT: 走 {ai_node[:44]} (已钉住; 该节点失效时重跑 free auto)"))
        else:
            print(dim("  ChatGPT: 本批节点都打不开(出口地区不受支持), 需等下一批节点"))
        return 0

    if args.action == "clean":
        from .util import json_load

        data = json_load(cache, {}) or {}
        alive = data.get("alive") or {}
        if not alive:
            raise Fail("还没有测速结果, 请先执行 accesspilot free test")
        before, after = freenodes.prune_profile("free", alive)
        ok(f"已清理失效节点: {before} -> {after}")
        return 0

    raise Fail(f"未知操作: {args.action}")


def cmd_ensure(args: argparse.Namespace) -> int:
    """确保内核在运行; 已在运行则直接返回(供定时保活/开机自启调用).

    ⚠️ 这个命令**不能**只看"内核在不在跑"。它由计划任务每 5 分钟调一次,
    如果无条件拉起, 用户在界面上点了「关闭」之后最多 5 分钟代理就自己回来了 ——
    实测到的真实行为, 用户会认为开关是坏的。

    所以先问一句 intent: 这次开机里用户最后一次主动操作是不是「关」?
    是就什么都不做。判据只在**同一次开机内**有效, 重启后自然失效,
    这样「开机自启」不会被误伤。
    """
    if process.is_running():
        return 0
    if not getattr(args, "force", False) and intent.user_wants_off():
        # 说人话, 而且给出出路: 用户可能正想知道"为什么它不自己起来了"。
        info("本次开机内用户已主动关闭代理, 保活不动它")
        print(dim("  (想让它无视这条: accesspilot ensure --force; 或在界面上点一下「打开」)"))
        return 0
    st = load_state()
    if not st.active_profile:
        subs = list_profiles()
        if not subs:
            raise Fail("没有任何配置档, 请先执行 accesspilot free auto 或 accesspilot sub add")
        st.active_profile = subs[0].name
        save_state(st)
        info(f"未选择配置档, 使用: {st.active_profile}")
    # 保活场景默认不碰系统代理; 只有显式 --sysproxy 才开启
    process.start(st=st, tun=st.tun_enable, system_proxy=True if args.sysproxy else False)
    return 0


#: 计划任务模板.
#:
#: 为什么不用 `schtasks /Create /SC MINUTE`: 那条路**没法设置电源条件**, 而
#: 新建任务的默认值是 DisallowStartIfOnBatteries=true / StopIfGoingOnBatteries
#: =true。在笔记本上一拔电源, 保活和节点刷新就静默不跑了 —— 实测返回
#: 0x800710E0「操作员或管理员拒绝了请求」。而这两个任务恰恰是移动使用时
#: 最需要的。用 XML 建任务能一次把电源条件、执行时限、并发策略都写对。
_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{desc}</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT{minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2020-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT{limit}H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _task_xml(*, arguments: str, minutes: int, limit_hours: int, desc: str) -> str:
    """渲染计划任务的 XML(在这里做转义, 所以不需要碰 schtasks 就能单测)."""
    from xml.sax.saxutils import escape

    return _TASK_XML.format(
        desc=escape(desc), minutes=int(minutes), limit=int(limit_hours),
        command="cmd", arguments=escape(arguments),
    )


def _register_task(
    name: str, *, arguments: str, minutes: int, limit_hours: int, desc: str
) -> tuple[int, str]:
    """用 XML 注册一个每 N 分钟重复的计划任务(关掉电源条件)."""
    import shutil as _shutil
    import tempfile

    xml = _task_xml(arguments=arguments, minutes=minutes,
                    limit_hours=limit_hours, desc=desc)
    tmpdir = Path(tempfile.mkdtemp(prefix="ap-task-"))
    tmp = tmpdir / f"{name}.xml"
    try:
        # schtasks /XML 要求 Unicode 文件(UTF-16 + BOM)
        tmp.write_text(xml, encoding="utf-16")
        return run_hidden(
            ["schtasks", "/Create", "/F", "/TN", name, "/XML", str(tmp)], timeout=30
        )
    finally:
        _shutil.rmtree(tmpdir, ignore_errors=True)


def cmd_autostart(args: argparse.Namespace) -> int:
    """用 Windows 计划任务定期保活内核.

    背景: 本机环境下, 内核进程会随终端会话结束而被系统回收(已发生两次),
    用户每次回来都要手动 start。计划任务拉起的内核独立于任何会话。
    默认每 5 分钟检查一次; 内核在跑则立即退出, 不产生任何影响。
    """
    if sys.platform != "win32":
        raise Fail("autostart 目前仅支持 Windows")
    task = "AccessPilotEnsure"
    shim = shutil.which("accesspilot")
    if not shim:
        raise Fail("未找到全局命令 accesspilot, 请先执行 accesspilot install-cmd")
    # 必须是**绝对**路径。真实事故: 计划任务里存的是 `.\accesspilot.CMD`,
    # 而任务计划的工作目录是 C:\Windows\System32, 于是每次触发都是
    # "'.\\accesspilot.CMD' is not recognized as an internal or external command"
    # (退出码 1) —— 保活任务从来没成功过。实测改用绝对路径后退出码 0。
    shim = str(Path(shim).resolve())
    action = args.action
    refresh_task = "AccessPilotRefresh"
    if action == "status":
        for name in (task, refresh_task):
            code, out = run_hidden(["schtasks", "/Query", "/TN", name, "/FO", "LIST"], timeout=20)
            if code == 0:
                nxt = next((ln for ln in out.splitlines() if "Next Run" in ln), "").strip()
                ok(f"{name} 已注册  {nxt}")
            else:
                info(f"{name} 未注册")
        return 0
    if action == "on":
        minutes = args.minutes or 5
        code, out = _register_task(
            task, arguments=f'/c ""{shim}" ensure"', minutes=minutes, limit_hours=1,
            desc="红杏/AccessPilot 内核保活: 每 N 分钟检查一次, 不在运行就拉起",
        )
        if code != 0:
            raise Fail(f"创建计划任务失败: {out.strip()[:300]}")
        ok(f"保活任务已注册: 每 {minutes} 分钟检查一次内核, 不在运行就自动拉起")
        print(dim(f"  (指向 {shim}; 已关闭「只在交流电源时启动」, 拔电池也照跑)"))

        # 可选的节点自动刷新: 免费池质量按小时波动, 让它自己抓好的时段
        if args.refresh_minutes:
            rm = int(args.refresh_minutes)
            code, out = _register_task(
                refresh_task,
                arguments=f'/c ""{shim}" free auto --workers 96 --timeout 5 --verify-top 30"',
                minutes=rm, limit_hours=3,
                desc="红杏/AccessPilot 节点池刷新: 重新抓取+测速+平台验证",
            )
            if code == 0:
                ok(f"节点自动刷新已注册: 每 {rm} 分钟重新抓取+测速+验证一次")
                print(dim("  (只刷新节点, 不会碰系统代理; 掉线的节点池会自己恢复)"))
            else:
                warn(f"节点自动刷新注册失败: {out.strip()[:200]}")
        print(dim("  不想要时: accesspilot autostart off"))
        return 0
    if action == "off":
        removed = []
        for name in (task, refresh_task):
            code, _ = run_hidden(["schtasks", "/Delete", "/F", "/TN", name], timeout=20)
            if code == 0:
                removed.append(name)
        if removed:
            ok("已删除计划任务: " + ", ".join(removed))
        else:
            info("没有需要删除的计划任务")
        return 0
    raise Fail(f"未知操作: {action}")


def cmd_sl(args: argparse.Namespace) -> int:
    """SuperLantern: 多后端抗审查总控.

    三个免费工具(蓝灯/迷霧通/赛风)加上我们自己的免费节点池, 统一管理:
    一次只跑一个、用真实平台打分、挂了自动换下一个、切换失败必定回到直连。
    """
    from . import supervisor as sl

    action = args.action

    if action == "list":
        rows = []
        for b in sl.status_all():
            rows.append([
                b["key"],
                b["name"],
                green("已安装") if b["installed"] else dim("未安装"),
                green("运行中") if b["running"] else dim("-"),
                b["note"],
            ])
        print(table(["key", "后端", "安装", "状态", "说明"], rows))
        print(dim("  用法: superlantern auto        # 自动挑一个能用的"))
        print(dim("        superlantern use <key>   # 指定后端"))
        return 0

    if action in ("status", "health"):
        cur = sl.current()
        h = sl.health(timeout=15.0)
        if args.json:
            _json_out({"current": cur, "health": h})
            return 0
        print(bold("SuperLantern 状态"))
        print(dim("-" * 46))
        print(f"  当前后端 : {cur or '(无 / 直连)'}")
        print(f"  探测代理 : {h.get('proxy') or '直连(由 TUN 后端接管)'}")
        state = green("可用") if h["ok"] else red("不可用")
        print(f"  链路健康 : {state}  通过 {h['passed']}/{h['total']}"
              + (f"  平均 {h['avg_ms']}ms" if h["avg_ms"] > 0 else ""))
        for k, v in (h.get("detail") or {}).items():
            tag = green("通过") if v == "ok" else red("失败")
            print(f"    {k:<9} {tag}  {'' if v == 'ok' else v}")
        return 0

    if action == "use":
        key = args.backend
        if not key or key not in sl.BACKEND_BY_KEY:
            raise Fail("请指定后端 key, 用 superlantern list 查看")
        b = sl.BACKEND_BY_KEY[key]
        if not b.installed():
            raise Fail(f"{b.name} 未安装")
        info(f"切换到 {b.name} ...")
        stopped = sl.stop_all_except(key)
        if stopped:
            ok(f"已停止其它后端: {', '.join(stopped)}")
        sl.start_backend(b)
        if not sl.wait_ready(b, timeout=30):
            sl.stop_backend(b)
            sl.restore_direct()
            raise Fail(f"{b.name} 启动超时, 已恢复直连")
        print(dim(f"  等待 {b.name} 建立隧道(最多 90 秒)..."))
        h = sl.wait_healthy(b, timeout=90.0, verbose=True)
        if h["ok"]:
            ok(f"{b.name} 已就绪 (通过 {h['passed']}/{h['total']}, {h['avg_ms']}ms)")
            if b.internal:
                print(dim("  提示: 这是本地代理后端, 还需要 accesspilot proxy on 才会走它"))
        else:
            warn(f"{b.name} 已启动但链路不健康: {h['detail']}")
            print(dim("  提示: 有些工具需要你在它的界面里点一次「连接」; "
                      "或用 superlantern auto 换下一个"))
        return 0

    if action == "auto":
        r = sl.auto(prefer=args.backend, verbose=True)
        if args.json:
            _json_out(r)
        if r["backend"]:
            b = sl.BACKEND_BY_KEY[r["backend"]]
            print()
            ok(f"当前使用: {b.name}")
            if b.internal:
                print(dim("  提示: 还需要 accesspilot proxy on"))
        return 0 if r["backend"] else 1

    if action == "stop":
        stopped = sl.stop_all_except(None)
        sl.restore_direct()
        ok(f"已停止所有后端{('(' + ', '.join(stopped) + ')') if stopped else ''}, 已恢复直连")
        return 0

    if action == "watch":
        info(f"守护模式: 每 {args.interval:.0f} 秒体检一次, 不健康就自动转移")
        print(dim("  Ctrl+C 退出; 想开机常驻请用 superlantern 的计划任务(见 README)"))
        sl.watch(interval=args.interval, rounds=args.rounds)
        return 0

    raise Fail(f"未知操作: {action}")


def cmd_mirror(args: argparse.Namespace) -> int:
    st = load_state()
    if args.value:
        if args.value not in ("jsdelivr", "raw", "ghproxy"):
            raise Fail("镜像只能是 jsdelivr / raw / ghproxy")
        st.mirror = args.value
        save_state(st)
        ok(f"镜像已设置为 {args.value}(下次生成配置生效)")
    else:
        print(f"当前镜像: {st.mirror}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    print(bold(f"AccessPilot 初始化 v{__version__}"))
    print(dim("-" * 46))
    coreinstall.install_core(force=args.force)
    st = load_state()
    save_state(st)
    ok(f"数据目录: {paths.home()}")
    if not list_profiles():
        print()
        print(bold("下一步: 添加一个订阅(或导入节点链接)"))
        print(f"  {cyan('accesspilot sub add <你的订阅链接>')}")
    return 0


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="accesspilot",
        description="AccessPilot - 加速访问 ChatGPT / Discord / X 等平台的客户端管理器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用流程:\n"
            "  accesspilot init                          # 安装内核\n"
            "  accesspilot sub add <订阅链接>            # 添加订阅\n"
            "  accesspilot start                         # 一键启动(自动开系统代理)\n"
            "  accesspilot test                          # 验证目标平台是否可用\n"
            "  accesspilot dashboard --open              # 打开图形控制台\n"
        ),
    )
    p.add_argument("-V", "--version", action="version", version=f"AccessPilot {__version__}")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("init", help="初始化: 下载内核并准备运行环境")
    sp.add_argument("--force", action="store_true", help="强制重新下载内核")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("core", help="内核管理")
    sp.add_argument("action", choices=["install", "update", "version", "path"])
    sp.add_argument("--version", dest="version", help="指定版本号, 如 v1.19.31")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_core)

    sp = sub.add_parser("sub", help="订阅管理")
    sp.add_argument(
        "action", choices=["add", "list", "update", "use", "rm", "show", "refresh"]
    )
    sp.add_argument("name", nargs="?", help="配置档名称")
    sp.add_argument("url", nargs="?", help="订阅链接 (add 时必填)")
    sp.add_argument("--use", action="store_true", help="添加后切换为当前配置档")
    sp.add_argument("--limit", type=int, default=30, help="show 时展示的节点数")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sub)

    sp = sub.add_parser("config", help="配置生成与校验")
    sp.add_argument("action", choices=["build", "show", "test", "path", "set-port", "lan"])
    sp.add_argument("--name", help="使用指定配置档")
    sp.add_argument("--port", type=int, help="set-port: 新的代理端口")
    sp.add_argument("--api-port", type=int, help="set-port: 新的控制端口")
    sp.add_argument("--on", action="store_true", help="lan: 开启局域网共享")
    sp.add_argument("--tun", dest="tun", action="store_true", default=None)
    sp.add_argument("--no-tun", dest="tun", action="store_false")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("start", help="启动内核(默认开启系统代理)")
    sp.add_argument("--tun", action="store_true", help="启用 TUN 全局模式(需管理员)")
    sp.add_argument("--no-tun", action="store_true", help="强制关闭 TUN")
    sp.add_argument("--sysproxy", action="store_true", help="同时开启系统代理")
    sp.add_argument("--no-sysproxy", action="store_true", help="不动系统代理设置")
    sp.add_argument("--profile", help="指定配置档")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="停止内核并还原系统代理")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("restart", help="重启内核")
    sp.add_argument("--tun", action="store_true")
    sp.add_argument("--no-tun", action="store_true")
    sp.set_defaults(func=cmd_restart)

    sp = sub.add_parser("sl", help="SuperLantern: 多后端总控(蓝灯/迷霧通/赛风/免费节点池)")
    sp.add_argument(
        "action", choices=["list", "status", "health", "use", "auto", "stop", "watch"]
    )
    sp.add_argument("backend", nargs="?", help="use: 要切换到的后端 key")
    sp.add_argument("--interval", type=float, default=60.0, help="watch: 体检间隔(秒)")
    sp.add_argument("--rounds", type=int, default=0, help="watch: 跑几轮(0=一直跑)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sl)

    sp = sub.add_parser("status", help="查看运行状态")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("log", help="查看内核日志")
    sp.add_argument("-n", "--lines", type=int, default=60)
    sp.add_argument("-f", "--follow", action="store_true")
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("proxy", help="系统代理开关")
    sp.add_argument("action", choices=["on", "off", "status", "env"])
    sp.add_argument("--off", action="store_true", help="env 时表示清除环境变量")
    sp.set_defaults(func=cmd_proxy)

    sp = sub.add_parser("tun", help="TUN 模式开关")
    sp.add_argument("action", choices=["on", "off", "status"])
    sp.set_defaults(func=cmd_tun)

    sp = sub.add_parser("node", help="节点选择与测速")
    sp.add_argument("action", choices=["list", "use", "delay", "groups"])
    sp.add_argument("group", nargs="?")
    sp.add_argument("node", nargs="?")
    sp.add_argument("--timeout", type=int, default=5000, help="测速超时(毫秒)")
    sp.add_argument("--select-first", action="store_true", help="测速后自动选最快")
    sp.set_defaults(func=cmd_node)

    sp = sub.add_parser("test", help="目标平台连通性诊断")
    sp.add_argument("--site", action="append", help="只测试指定站点, 可重复")
    sp.add_argument("--timeout", type=float, default=12.0)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_test)

    sp = sub.add_parser("ip", help="查看当前出口 IP")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_ip)

    sp = sub.add_parser("doctor", help="环境自检")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("dashboard", help="启动本地图形控制台")
    sp.add_argument("--port", type=int, default=9099)
    sp.add_argument("--open", action="store_true", help="自动打开浏览器")
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("gui", help="红杏: 桌面客户端(窗口 + 系统托盘)")
    sp.add_argument("--no-tray", action="store_true",
                    help="不挂系统托盘, 只开窗口(排错用)")
    sp.set_defaults(func=cmd_gui)

    sp = sub.add_parser("ui", help="内核自带面板(metacubexd)")
    sp.add_argument("action", choices=["install", "open"])
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_ui)

    sp = sub.add_parser("mirror", help="规则集下载镜像")
    sp.add_argument("value", nargs="?", choices=["jsdelivr", "raw", "ghproxy"])
    sp.set_defaults(func=cmd_mirror)

    sp = sub.add_parser("accel", help="免节点直连加速(IP 优选, 面向 GitHub 系资源)")
    sp.add_argument("action", choices=["on", "off", "status", "bench", "log", "serve"])
    sp.add_argument("domain", nargs="*", help="bench: 指定要优选的域名")
    sp.add_argument("--port", type=int, default=7895, help="serve: 监听端口")
    sp.add_argument("--verbose", action="store_true", help="serve: 打印每次连接")
    sp.add_argument("--log", action="store_true", help="status: 附带日志")
    sp.add_argument("-n", "--lines", type=int, default=40)
    sp.set_defaults(func=cmd_accel)

    sp = sub.add_parser("warp", help="Cloudflare WARP: 零账号零信用卡的免费境外出口")
    sp.add_argument(
        "action", choices=["register", "status", "license", "chain", "endpoint", "rm"]
    )
    sp.add_argument("--force", action="store_true", help="register: 重新注册")
    sp.add_argument("--key", help="license: WARP+ / Zero Trust 的 license key")
    sp.add_argument("--node", help="chain: 让 WARP 握手借道这个节点")
    sp.add_argument("value", nargs="?", help="endpoint: 接入点地址")
    sp.set_defaults(func=cmd_warp)

    sp = sub.add_parser("free", help="公开免费节点: 抓取 + 测速 + 平台验证 + 只留真正可用的(无需 VPS/信用卡)")
    sp.add_argument("action", choices=["sources", "fetch", "test", "clean", "auto"])
    sp.add_argument("--use", action="store_true", help="fetch: 设为当前配置档")
    sp.add_argument("--prune", action="store_true", help="test: 顺便清理失效节点")
    sp.add_argument("--workers", type=int, default=48, help="测速并发数")
    sp.add_argument("--timeout", type=float, default=6.0, help="单节点测速超时(秒)")
    sp.add_argument("--top", type=int, default=15, help="显示前 N 个")
    sp.add_argument("--min-ms", type=int, default=4000, help="auto: 只平台验证延迟低于此值的节点")
    sp.add_argument("--verify-top", type=int, default=25, help="auto: 最多平台验证几个最快节点")
    sp.add_argument("--verify-timeout", type=float, default=10.0, help="auto: 平台验证单请求超时(秒)")
    sp.set_defaults(func=cmd_free)

    sp = sub.add_parser("_watchdog", help=argparse.SUPPRESS)
    sp.add_argument("--pid", type=int, required=True)
    sp.set_defaults(func=cmd_watchdog)

    sp = sub.add_parser("install-cmd", help="把 accesspilot 注册成全局命令(写到 PATH 目录)")
    sp.add_argument("--dir", help="指定写入目录(默认用 Python 的 scripts 目录)")
    sp.set_defaults(func=cmd_install_cmd)

    sp = sub.add_parser("ensure", help="确保内核在运行(已在运行则不做任何事)")
    sp.add_argument("--sysproxy", action="store_true", help="内核不在运行时, 启动后开启系统代理")
    sp.add_argument("--force", action="store_true",
                    help="无视「用户本次开机内主动关过」这条记录, 照样拉起")
    sp.set_defaults(func=cmd_ensure)

    sp = sub.add_parser("autostart", help="Windows 计划任务保活: 内核不在就自动拉起(默认每 5 分钟)")
    sp.add_argument("action", choices=["on", "off", "status"])
    sp.add_argument("--minutes", type=int, default=5, help="on: 检查间隔(分钟)")
    sp.add_argument(
        "--refresh-minutes", type=int, default=0,
        help="on: 同时注册节点自动刷新(分钟, 0=不注册), 例如 120 表示每 2 小时重新抓节点",
    )
    sp.set_defaults(func=cmd_autostart)

    return p


def cmd_install_cmd(args: argparse.Namespace) -> int:
    """把 accesspilot 注册成全局命令(往 PATH 目录里写一个包装器).

    为什么不用 `pip install -e .`: 本机的 Python 是特殊布局(scripts 目录被
    重定向到 E:\\Scripts), pip 会报 "No pyvenv.cfg file" 而失败。写一个
    几行的包装器更可靠, 也不依赖任何构建工具链。
    """
    import sysconfig

    project = str(Path(__file__).resolve().parent.parent)
    targets: list[Path] = []
    if args.dir:
        targets.append(Path(args.dir))
    else:
        scripts = sysconfig.get_path("scripts")
        if scripts:
            targets.append(Path(scripts))
        if sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA")
            if local:
                targets.append(Path(local) / "Microsoft" / "WindowsApps")

    if not targets:
        raise Fail("找不到可写且位于 PATH 中的目录, 请用 --dir 指定")

    written: list[Path] = []
    # cmd.exe 按系统 ANSI 代码页解释 .cmd 文件, 所以必须用 mbcs 而不是 utf-8,
    # 否则路径或注释里的中文会变成乱码甚至导致命令失效。
    shim_encoding = "mbcs" if sys.platform == "win32" else "utf-8"
    for target in targets:
        try:
            target.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                # 主命令 + SuperLantern 快捷命令
                shims = {
                    "accesspilot.cmd": "%*",
                    "superlantern.cmd": "sl %*",
                }
                for fname, args in shims.items():
                    shim = target / fname
                    body = (
                        "@echo off\r\n"
                        f"rem {fname} (generated by `accesspilot install-cmd`)\r\n"
                        "setlocal\r\n"
                        f'set "PYTHONPATH={project};%PYTHONPATH%"\r\n'
                        f'"{sys.executable}" -m accesspilot {args}\r\n'
                        "exit /b %ERRORLEVEL%\r\n"
                    )
                    try:
                        shim.write_text(body, encoding=shim_encoding)
                    except UnicodeEncodeError:
                        warn(f"路径包含当前代码页无法表示的字符, 已尽力写入: {project}")
                        shim.write_text(body, encoding=shim_encoding, errors="replace")
                    written.append(shim)
            else:
                for fname, args in {"accesspilot": "", "superlantern": "sl "}.items():
                    shim = target / fname
                    shim.write_text(
                        "#!/bin/sh\n"
                        f'PYTHONPATH="{project}:${{PYTHONPATH}}" exec "{sys.executable}" -m accesspilot {args}"$@"\n',
                        encoding="utf-8",
                    )
                    shim.chmod(0o755)
                    written.append(shim)
        except Exception as e:  # noqa: PERF203
            warn(f"{target} 写入失败: {e}")

    if not written:
        raise Fail("所有候选目录都写入失败, 请用 --dir 指定一个可写目录")
    for shim in written:
        ok(f"已安装: {shim}")
    print()
    info("在任意目录直接运行:  accesspilot --version")
    print(dim("  如果提示找不到命令, 关掉重开一个终端窗口即可(PATH 需要刷新)"))
    return 0


def cmd_watchdog(args: argparse.Namespace) -> int:
    """内部命令: 看门狗。监控内核进程, 它一消失就还原系统代理。

    存在的意义: 内核崩溃后如果不管, 系统代理会一直指向死端口, 结果是
    **整台机器都上不了网**(连本来直连的国内站点也打不开)。
    """
    core_pid = int(args.pid)
    deadline = time.time() + 7 * 24 * 3600
    while time.time() < deadline:
        time.sleep(2)
        if not process._pid_alive(core_pid):  # noqa: SLF001
            st = load_state()
            if st.system_proxy_on:
                sysproxy.disable(st)
                st.system_proxy_on = False
                save_state(st)
            process.stop_accel()
            paths.pid_file().unlink(missing_ok=True)
            paths.watchdog_pid_file().unlink(missing_ok=True)
            return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 自愈: 只要用户再次调用本工具, 就先把"内核已死但系统代理还开着"的
    # 断网状态修好 —— 这是防止用户整台机器失联的最后一道防线。
    if getattr(args, "command", None) != "_watchdog":
        try:
            process.heal_if_broken()
        except Exception:
            pass

    if not getattr(args, "command", None):
        st = load_state()
        _print_status(st, process.status())
        print()
        print(dim("  执行 accesspilot -h 查看全部命令, 或 accesspilot dashboard --open 打开控制台"))
        return 0
    try:
        return int(args.func(args) or 0)
    except Fail as e:
        err(str(e))
        return 2
    except KeyboardInterrupt:
        print()
        info("已取消")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
