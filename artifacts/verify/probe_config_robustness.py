# -*- coding: utf-8 -*-
"""验收探针 (task-5 第 8 条 /头号历史事故): 单个坏节点能不能让整份配置失效?

做法: 一律**渲染到临时文件**, 绝不碰 runtime/config.yaml, 然后用内核自检
`mihomo -t -f <tmp>` 判定 "这份配置内核收不收"。

覆盖场景:
  A. 现有配置档 free.json 原样渲染(它当前含 10 组重名, 见 VERIFICATION.md)
  B. 注入 "名字里带控制字符" 的孪生节点: 名字经 sanitize 后与另一个节点撞名
  C. 注入 6000 个节点的免费池(延迟 / 体积)
  D. 注入其它垃圾字段(server 缺失 / ss 密码垃圾 / alpn 是字符串 / sni 是 URL)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback

sys.path.insert(0, r"C:\Users\Administrator\AccessPilot")

from accesspilot import config, paths  # noqa: E402
from accesspilot.state import load_state  # noqa: E402
from accesspilot.subscription import Subscription, load_profile  # noqa: E402

REPORT: list[dict] = []


def check(label: str, sub: Subscription, st) -> dict:
    entry: dict = {"label": label, "n_proxies": len(sub.proxies)}
    t0 = time.time()
    try:
        cfg = config.build_config(sub, st)
        entry["build_ms"] = int((time.time() - t0) * 1000)
        names = [str(p.get("name")) for p in cfg.get("proxies") or []]
        entry["rendered_proxies"] = len(names)
        entry["unique_names"] = len(set(names))
        dups = sorted({n for n in names if names.count(n) > 1})
        entry["dup_names"] = dups[:12]
        entry["n_dup_groups"] = len(dups)
    except BaseException as e:  # noqa: BLE001
        entry["build_raised"] = f"{type(e).__name__}: {e}"
        entry["tb"] = traceback.format_exc()[-800:]
        REPORT.append(entry)
        return entry

    tmpdir = tempfile.mkdtemp(prefix="ap-verify-cfg-")
    target = os.path.join(tmpdir, "config.yaml")
    try:
        from pathlib import Path

        config.write_config(cfg, Path(target))
        entry["bytes"] = os.path.getsize(target)
        ok, out = config.test_config(Path(target))
        entry["core_accepts"] = bool(ok)
        # 只留关键行, 别把 4MB 的输出灌进来
        key = [ln for ln in out.splitlines()
               if "level=error" in ln or "test failed" in ln or "test is successful" in ln
               or "duplicate" in ln]
        entry["core_output_keylines"] = key[:8]
    except BaseException as e:  # noqa: BLE001
        entry["test_raised"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            os.remove(target)
            os.rmdir(tmpdir)
        except Exception:
            pass
    REPORT.append(entry)
    return entry


def main() -> int:
    st = load_state()
    st.accel_enable = False  # 排除无关变量

    # ---- A. 现有配置档原样 ----
    try:
        live = load_profile("free")
        check("A. 现有 profile free.json 原样渲染", live, st)
    except BaseException as e:  # noqa: BLE001
        REPORT.append({"label": "A", "raised": f"{type(e).__name__}: {e}"})

    # ---- B. 控制字符撞名 ----
    base = [
        {"name": "🇭🇰 香港 | HKG #3", "type": "http", "server": "1.2.3.4",
         "port": 443, "tls": True},
        {"name": "🇭🇰 香港 | HKG #3\u009f", "type": "http", "server": "5.6.7.8",
         "port": 443, "tls": True},
        {"name": "正常节点", "type": "http", "server": "9.9.9.9", "port": 443},
    ]
    check("B. 注入控制字符孪生名 (X 与 X+\\x9f)", Subscription(name="t", proxies=base), st)

    # ---- B2. 名字里带换行/NUL/零宽, 看渲染出来的 YAML 还能不能读 ----
    nasty = [
        {"name": "bad\nname", "type": "http", "server": "1.1.1.1", "port": 80},
        {"name": "sni\u009fnode", "type": "http", "server": "2.2.2.2", "port": 80,
         "sni": "https://t.me/xxx"},
        {"name": "ok", "type": "http", "server": "3.3.3.3", "port": 80},
    ]
    check("B2. 名字含换行/控制字符 + sni 是 URL", Subscription(name="t2", proxies=nasty), st)

    # ---- D. 其它垃圾字段 ----
    junk = [
        {"name": "no-server", "type": "http", "port": 80},
        {"name": "no-port", "type": "http", "server": "1.1.1.1"},
        {"name": "ss-bad-cipher", "type": "ss", "server": "1.1.1.1", "port": 80,
         "cipher": "auth_aes128_md5", "password": "x"},
        {"name": "vmess-bad-cipher", "type": "vmess", "server": "1.1.1.1", "port": 80,
         "cipher": "garbage", "uuid": "b831381d-6324-4d53-ad4f-8cda48b30811"},
        {"name": "wg-no-key", "type": "wireguard", "server": "1.1.1.1", "port": 80},
        {"name": "alpn-str", "type": "trojan", "server": "1.1.1.1", "port": 443,
         "password": "x", "alpn": "h2"},
    ]
    check("D. 垃圾字段 (缺 server/port, ss 垃圾密码, vmess 垃圾加密, wg 缺密钥, alpn 字符串)",
          Subscription(name="t3", proxies=junk), st)

    # ---- C. 6000 节点 ----
    big = []
    for i in range(6000):
        big.append({"name": f"n{i:05d}", "type": "http", "server": f"10.{i // 250}.{i % 250}.1",
                    "port": 8080})
    t0 = time.time()
    check("C. 6000 个合成节点", Subscription(name="big", proxies=big), st)
    REPORT[-1]["wall_ms_including_test"] = int((time.time() - t0) * 1000)

    print(json.dumps(REPORT, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
