"""下载 mihomo 的 Android 二进制并放进 jniLibs.

为什么需要这个脚本
==================
内核二进制单个 61 MB —— GitHub 单文件超过 50 MB 会告警, 而且它是**可再生成**
的(官方发布页一直在更新), 所以仓库里不放它, 只放"怎么拿到它"。

    用法: python android/tools/fetch_core.py

放哪、叫什么名, 是有讲究的
==========================
Android 10 起有 W^X 限制: **只能执行 nativeLibraryDir 里的文件**。所以不能把
二进制塞进 assets 再解压出来 exec —— 那样会 EACCES。正确做法是放进
`jniLibs/<abi>/` 并命名成 `lib*.so`, 打包后它会被放进 APK 的 `lib/<abi>/`,
安装后位于 `/data/app/.../lib/<abi>/`, 那是可执行目录。

    源码里的相对路径: process 启动时用 applicationInfo.nativeLibraryDir
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JNI = ROOT / "app" / "src" / "main" / "jniLibs"
CORE_BIN = ROOT / "core-bin"

VERSION = "v1.19.32"
BASE = f"https://github.com/MetaCubeX/mihomo/releases/download/{VERSION}"

#: ABI -> (官方产物名, 放进 jniLibs 后的文件名)
#:
#: 只装 arm64: 现在还在用 32 位安卓的手机基本没有了, 而单个二进制 61 MB,
#: 两套一起打 APK 会翻倍。要支持 32 位时在这里加一行、并在 app/build.gradle.kts
#: 的 abiFilters 里放开即可。
TARGETS: dict[str, tuple[str, str]] = {
    "arm64-v8a": (f"mihomo-android-arm64-v8-{VERSION}.gz", "libmihomo.so"),
}

#: 下载镜像。官方 GitHub 在国内经常连不上, 按顺序回退。
MIRRORS = (
    "{base}/{name}",
    "https://ghfast.top/{base}/{name}",
    "https://gh-proxy.com/{base}/{name}",
)


def _download(name: str, dest: Path, *, proxy: str | None = None) -> None:
    last = ""
    for tpl in MIRRORS:
        url = tpl.format(base=BASE, name=name)
        try:
            print(f"    试 {url.split('/')[2]} ...", flush=True)
            handlers = []
            if proxy:
                handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            opener = urllib.request.build_opener(*handlers)
            with opener.open(url, timeout=180) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            if dest.stat().st_size > 1_000_000:
                return
            last = f"内容太小({dest.stat().st_size} 字节)"
        except Exception as e:  # noqa: PERF203
            last = f"{type(e).__name__}: {e}"
    raise SystemExit(f"[x] 下载 {name} 失败: {last}")


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 mihomo 的 Android 二进制")
    ap.add_argument("--proxy", default="", help="HTTP 代理, 例如 http://127.0.0.1:7890")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = ap.parse_args()

    CORE_BIN.mkdir(parents=True, exist_ok=True)
    for abi, (name, out_name) in TARGETS.items():
        out = JNI / abi / out_name
        if out.is_file() and not args.force:
            print(f"[=] {abi}/{out_name} 已存在 ({out.stat().st_size / 1048576:.1f} MB), 跳过")
            continue

        print(f"[i] {abi}: {name}")
        gz = CORE_BIN / name
        _download(name, gz, proxy=args.proxy or None)

        out.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(gz, "rb") as fi, open(out, "wb") as fo:
            shutil.copyfileobj(fi, fo)

        b = out.read_bytes()[:20]
        if b[:4] != b"\x7fELF":
            out.unlink()
            raise SystemExit("[x] 解压出来不是 ELF, 下载内容可能被劫持了")

        mach = {0x3E: "x86-64", 0xB7: "AArch64", 0x28: "ARM32"}.get(b[18] | (b[19] << 8), "?")
        size = out.stat().st_size
        sha = hashlib.sha256(out.read_bytes()).hexdigest()[:16]
        print(f"[+] {out.relative_to(ROOT)}  {size / 1048576:.1f} MB  "
              f"架构={mach}  sha256[:16]={sha}")
        if abi == "arm64-v8a" and mach != "AArch64":
            raise SystemExit(f"[x] 期望 AArch64, 拿到 {mach}")

    print("\n[i] 下一步: python android/tools/build_assets.py  (生成 assets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
