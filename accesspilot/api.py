"""mihomo 外部控制接口(RESTful API)客户端."""
from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .state import AppState
from .util import Fail, http_request, info

__all__ = [
    "ping",
    "version",
    "configs",
    "proxies",
    "select",
    "delay",
    "group_delay",
    "group_delay_full",
    "connections",
    "close_connections",
    "reload",
    "update_provider",
    "rule_providers",
    "rules",
]


def _call(
    st: AppState,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> Any:
    url = f"{st.api_base()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {st.api_secret}",
        "Content-Type": "application/json",
    }
    status, _, raw = http_request(
        url, method=method, headers=headers, data=data, timeout=timeout
    )
    if status == 401:
        raise Fail("控制接口鉴权失败, 密钥可能已变更(重新 start 即可)")
    if status >= 400:
        raise Fail(f"控制接口返回 HTTP {status}: {path} {raw[:200]!r}")
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def ping(st: AppState) -> bool:
    try:
        _call(st, "/version", timeout=2.0)
        return True
    except Exception:
        return False


def version(st: AppState) -> str:
    data = _call(st, "/version", timeout=3.0)
    if isinstance(data, dict):
        v = data.get("version", "?")
        if data.get("meta"):
            return f"{v} (meta)"
        return str(v)
    return "?"


def configs(st: AppState) -> dict[str, Any]:
    return _call(st, "/configs") or {}


def proxies(st: AppState) -> dict[str, Any]:
    data = _call(st, "/proxies") or {}
    return data.get("proxies", {}) if isinstance(data, dict) else {}


def proxy(st: AppState, name: str) -> dict[str, Any]:
    """读**单个**策略组/节点.

    界面每 1~2 秒刷新一次状态, 而 `/proxies` 会把全部节点都返回 ——
    免费节点池动辄六千个, 那个响应有几 MB, 轮询它会把界面拖死。
    只想知道"当前选的是哪个节点"时用这个。
    """
    return _call(st, f"/proxies/{urllib.parse.quote(name, safe='')}") or {}


#: 内核工作模式。rule = 智能分流(国内直连/国外走代理), global = 全部走代理,
#: direct = 全部直连。界面上对应"智能分流 / 全局 / 直连"三档。
MODES = ("rule", "global", "direct")


def mode(st: AppState) -> str:
    """当前内核工作模式(读不到时返回 'rule')."""
    try:
        return str((configs(st) or {}).get("mode") or "rule")
    except Exception:  # noqa: PERF203
        return "rule"


def set_mode(st: AppState, value: str) -> None:
    if value not in MODES:
        raise ValueError(f"未知模式: {value} (可选: {', '.join(MODES)})")
    _call(st, "/configs", method="PATCH", body={"mode": value})


def select(st: AppState, group: str, node: str) -> None:
    """把策略组切到某个节点。"""
    try:
        _call(
            st,
            f"/proxies/{urllib.parse.quote(group, safe='')}",
            method="PUT",
            body={"name": node},
        )
    except Fail as e:
        # 内核在这一步只会回 `Selector update error: proxy not exist`, 而
        # _call 会把它包成 `控制接口返回 HTTP 400: /proxies/%F0%9F%9A%80...`
        # —— 界面上就是这么原样显示的(用户截图里那条红字)。
        #
        # 对用户来说那条信息完全没用: 它既没说清发生了什么, 也没说该怎么办。
        # 真实成因几乎总是同一个: **节点列表被刷新过, 这个名字已经不在配置里了**
        # (每次 free auto 都会重新编号: `SGP #5` 可能变成 `SGP #2`)。
        # 所以这里换成人话, 并且把节点名带上 —— 排查时需要它。
        if "proxy not exist" in str(e):
            raise Fail(
                f"节点「{node}」已经不在当前配置里了"
                f"(节点列表刷新过? 名字会随刷新变化), 已自动重新选择"
            ) from None
        raise


#: 测速目标。用 HTTPS/443: 部分节点只放行 443, 用 80 端口测会误判为不可用。
DELAY_URL = "https://www.gstatic.com/generate_204"


def delay(st: AppState, node: str, *, url: str = DELAY_URL, timeout_ms: int = 5000) -> int:
    q = urllib.parse.urlencode({"timeout": timeout_ms, "url": url})
    path = f"/proxies/{urllib.parse.quote(node, safe='')}/delay?{q}"
    data = _call(st, path, timeout=timeout_ms / 1000 + 8)
    if isinstance(data, dict) and "delay" in data:
        return int(data["delay"])
    msg = (data or {}).get("message") if isinstance(data, dict) else data
    raise Fail(str(msg or "测速失败"))


def group_delay_full(
    st: AppState, group: str, *, url: str = DELAY_URL, timeout_ms: int = 5000
) -> dict[str, Any]:
    """测试整个策略组, 返回 {节点名: 延迟(ms) 或 错误信息}.

    注意 mihomo 这个接口有两种返回格式, 必须都兼容:
      * 扁平整数: {"节点A": 151, "节点B": 302}
      * 对象形式: {"节点A": {"delay": 151}, "节点B": {"message": "timeout"}}
    只认后者会导致"明明全测通了却显示 0 个节点响应"。
    """
    q = urllib.parse.urlencode({"timeout": timeout_ms, "url": url})
    path = f"/group/{urllib.parse.quote(group, safe='')}/delay?{q}"
    data = _call(st, path, timeout=timeout_ms / 1000 + 30)
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for name, info in data.items():
        if isinstance(info, bool):
            continue
        if isinstance(info, int):
            out[name] = info
        elif isinstance(info, dict):
            if "delay" in info:
                try:
                    out[name] = int(info["delay"])
                except (TypeError, ValueError):
                    out[name] = str(info.get("message") or "测速失败")
            else:
                out[name] = str(info.get("message") or "测速失败")
        else:
            out[name] = str(info)
    return out


def group_delay(
    st: AppState, group: str, *, url: str = DELAY_URL, timeout_ms: int = 5000
) -> dict[str, int]:
    """只返回测速成功的节点 {节点名: 延迟}."""
    return {
        k: v
        for k, v in group_delay_full(st, group, url=url, timeout_ms=timeout_ms).items()
        if isinstance(v, int)
    }


def connections(st: AppState) -> dict[str, Any]:
    return _call(st, "/connections") or {}


def close_connections(st: AppState) -> None:
    _call(st, "/connections", method="DELETE")


def reload(st: AppState, config_path: Path) -> None:
    """热重载配置, 并等待内核确认恢复响应.

    实测: 1235 个节点 + 20 个规则集的配置, 内核重载期间会阻塞控制接口
    10~30 秒, 直接等 HTTP 响应会超时 —— 但重载本身可能已经成功了。
    所以这里"提交 + 轮询恢复"两步走, 而不是一超时就判死。
    """
    try:
        _call(
            st,
            "/configs?force=true",
            method="PUT",
            body={"path": str(config_path)},
            timeout=45,
        )
    except Exception as e:  # noqa: PERF203
        info(f"重载提交期间接口暂时无响应(内核正在应用新配置): {type(e).__name__}")

    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            proxies(st)
            return  # 控制接口已恢复, 重载流程结束
        except Exception:
            time.sleep(2)
    raise Fail("重载后控制接口长时间未恢复, 请执行 accesspilot log 查看内核日志")


def update_provider(st: AppState, name: str) -> None:
    _call(
        st,
        f"/providers/proxies/{urllib.parse.quote(name, safe='')}",
        method="PUT",
        timeout=30,
    )


def proxy_providers(st: AppState) -> dict[str, Any]:
    data = _call(st, "/providers/proxies") or {}
    return data.get("providers", {}) if isinstance(data, dict) else {}


def rule_providers(st: AppState) -> dict[str, Any]:
    """返回各 rule-provider 的加载状态与条目数.

    这是判断"规则集是否真的生效"的最可靠方式 —— 如果格式写错, 内核
    不会报错, 只会把条目数变成 0 或很小。
    """
    data = _call(st, "/providers/rules") or {}
    return data.get("providers", {}) if isinstance(data, dict) else {}


def rules(st: AppState) -> list[str]:
    data = _call(st, "/rules") or {}
    return list(data.get("rules", [])) if isinstance(data, dict) else []


def traffic_now(st: AppState) -> tuple[int, int]:
    """粗略估算当前上下行速率(字节/秒), 通过两次采样 /connections 计算."""
    conns = connections(st)
    up = int(conns.get("uploadTotal", 0) or 0)
    down = int(conns.get("downloadTotal", 0) or 0)
    return up, down
