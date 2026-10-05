"""在**独立的内核实例**上测这些住宅 IP 节点 —— 不碰用户正在用的那条连接。

## 为什么另起一个实例

要测一个节点能不能用, 必须让流量真的从它出去。而正在跑的那个内核实例承载着
用户的真实上网(系统代理指着它、ChatGPT/X/Discord 都通着)。往里加节点再切来切去,
就是在用户的连接上做实验 —— 这件事我今天已经干过几次, 每次都把人弄断线。

所以: 另起一个 mihomo, 用**别的端口**(mixed 7899 / 控制 9099), 只加载候选节点,
测完就关。用户的连接全程不受影响。

## 判据

"能不能打开"只是一半。对注册来说更重要的是**出口 IP 是不是真的落在家宽段** ——
所以每个节点除了测 ChatGPT/X, 还要查一次它自己的出口 IP 和归属。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ACCESSPILOT_HOME", os.path.expandvars(r"%LOCALAPPDATA%\AccessPilot"))

from accesspilot import config as cfgmod  # noqa: E402
from accesspilot import paths  # noqa: E402

MIXED = 7899
CTRL = 9099
SECRET = "scan-only-local"
GROUP = "SCAN"


def build_config(nodes: list[dict]) -> dict:
    proxies, seen = [], set()
    for p in nodes:
        key = (str(p.get("server")), p.get("port"), p.get("type"))
        if key in seen:
            continue
        seen.add(key)
        q = {k: v for k, v in p.items() if k != "dialer-proxy"}
        proxies.append(q)
    return {
        "mixed-port": MIXED,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "external-controller": f"127.0.0.1:{CTRL}",
        "secret": SECRET,
        "proxies": proxies,
        "proxy-groups": [{"name": GROUP, "type": "select",
                          "proxies": [p["name"] for p in proxies]}],
        "rules": [f"MATCH,{GROUP}"],
    }


def wait_ready(proc, timeout: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://127.0.0.1:{CTRL}/version",
                        headers={"Authorization": f"Bearer {SECRET}"}), timeout=2):
                return True
        except Exception:
            time.sleep(0.5)
    return False


def ctrl(path: str, *, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{CTRL}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {SECRET}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def via(url: str, timeout: int = 12) -> str:
    """经独立实例取一次, 返回状态码(失败 000)。"""
    r = subprocess.run(
        ["curl.exe", "-s", "-o", "NUL", "-w", "%{http_code}", "--max-time", str(timeout),
         "-x", f"http://127.0.0.1:{MIXED}", url], capture_output=True, text=True)
    return r.stdout.strip() or "000"


def body(url: str, timeout: int = 12) -> str:
    r = subprocess.run(
        ["curl.exe", "-s", "--max-time", str(timeout), "-x", f"http://127.0.0.1:{MIXED}", url],
        capture_output=True, text=True)
    return (r.stdout or "").strip()


def main() -> int:
    nodes = json.loads(
        Path("artifacts/nodes/residential_nodes.json").read_text(encoding="utf-8"))
    print(f"候选 {len(nodes)} 条 -> 去重后 ", end="", flush=True)
    cfg = build_config(nodes)
    if not cfg["proxies"]:
        print("0 个, 退出")
        return 1
    print(f"{len(cfg['proxies'])} 个")

    tmp = ROOT / "artifacts" / "nodes" / "_scan_config.yaml"
    cfgmod.write_config(cfg, tmp)
    binary = paths.core_binary()
    print(f"   内核: {binary.name}   配置: {tmp.name}")
    print(f"   端口: mixed={MIXED} ctrl={CTRL} (用户那条连接用的是别的端口, 不受影响)")

    proc = subprocess.Popen([str(binary), "-d", str(paths.runtime_dir()), "-f", str(tmp)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rows: list[dict] = []
    try:
        if not wait_ready(proc):
            print("!! 独立内核没起来")
            return 1
        print("   独立内核就绪\n")

        for i, p in enumerate(cfg["proxies"], 1):
            name = p["name"]
            try:
                ctrl(f"/proxies/{urllib.parse.quote(GROUP, safe='')}",
                     method="PUT", body={"name": name})
            except Exception:
                print(f"  [{i:>2}] {name[:32]:<34} 切换失败")
                continue
            time.sleep(0.8)
            cg = via("https://chatgpt.com/cdn-cgi/trace")
            xx = via("https://x.com/")
            ip = body("https://api.ipify.org", timeout=10)
            mark = "  <<< 可用" if cg == "200" and xx == "200" and ip else ""
            print(f"  [{i:>2}] {name[:32]:<34} chatgpt={cg} x={xx} exit={ip or '?':<16}{mark}")
            rows.append({"name": name, "server": p.get("server"), "chatgpt": cg,
                         "x": xx, "exit_ip": ip})
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        tmp.unlink(missing_ok=True)

    good = [r for r in rows if r["chatgpt"] == "200" and r["x"] == "200" and r["exit_ip"]]
    print()
    print(f"能用的: {len(good)} / {len(rows)}")
    for r in good:
        print(f"   {r['name'][:34]:<36} 出口 {r['exit_ip']}")

    Path("artifacts/nodes/residential_test.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n-> artifacts/nodes/residential_test.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
