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


def select(st: AppState, group: str, node: str) -> None:
    _call(
        st,
        f"/proxies/{urllib.parse.quote(group, safe='')}",
        method="PUT",
        body={"name": node},
    )


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
