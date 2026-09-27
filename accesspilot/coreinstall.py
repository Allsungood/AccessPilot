"""内核安装器: 下载 mihomo 与 Windows TUN 驱动(wintun).

网络现实: GitHub Releases / raw 在国内经常不可达, 因此所有下载都走
"多镜像顺序回退", 并且支持用户提供本地文件或自定义镜像。
"""
from __future__ import annotations

import platform
import re
import shutil
import sys
import tempfile
from pathlib import Path

from . import paths
from .util import (
    Fail,
    Progress,
    download,
    extract_archive,
    fetch_json,
    info,
    ok,
    run_hidden,
    warn,
)

MIHOMO_REPO = "MetaCubeX/mihomo"
#: 上游接口不可用时的兜底版本(经过验证可用)
FALLBACK_VERSION = "v1.19.31"

#: 元数据获取镜像
META_MIRRORS = [
    "https://api.github.com",
    "https://ghfast.top/https://api.github.com",
    "https://gh-proxy.com/https://api.github.com",
]

#: 下载加速前缀, 按顺序尝试
DOWNLOAD_MIRRORS = [
    "",  # 直连
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://ghproxy.net/",
]

WINTUN_URLS = [
    "https://www.wintun.net/builds/wintun-0.14.1.zip",
    "https://ghfast.top/https://www.wintun.net/builds/wintun-0.14.1.zip",
]

GEO_FILES = {
    "geoip.metadb": [
        "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geoip.metadb",
        "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/release/geoip.metadb",
        "https://ghfast.top/https://github.com/MetaCubeX/meta-rules-dat/raw/release/geoip.metadb",
    ],
    "geosite.dat": [
        "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/geosite.dat",
        "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/release/geosite.dat",
        "https://ghfast.top/https://github.com/MetaCubeX/meta-rules-dat/raw/release/geosite.dat",
    ],
    "country.mmdb": [
        "https://testingcf.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@release/country.mmdb",
        "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/release/country.mmdb",
    ],
}


def _arch_tag() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "amd64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86", "i386", "i686"):
        return "386"
    raise Fail(f"不支持的 CPU 架构: {machine}")


def asset_name(version: str) -> str:
    arch = _arch_tag()
    if sys.platform == "win32":
        # compatible 版本兼容性最好(不依赖新指令集)
        return f"mihomo-windows-{arch}-compatible-{version}.zip"
    if sys.platform == "darwin":
        return f"mihomo-darwin-{arch}-{version}.zip"
    return f"mihomo-linux-{arch}-{version}.gz"


def latest_version() -> str:
    for host in META_MIRRORS:
        try:
            data = fetch_json(f"{host}/repos/{MIHOMO_REPO}/releases/latest", timeout=12)
            tag = data.get("tag_name")
            if tag:
                return str(tag)
        except Exception:
            continue
    warn(f"无法获取最新版本号, 使用内置版本 {FALLBACK_VERSION}")
    return FALLBACK_VERSION


def _download_with_mirrors(url_path: str, dest: Path, label: str) -> None:
    """url_path 形如 'https://github.com/...' , 会自动套用加速前缀."""
    last_err: Exception | None = None
    for prefix in DOWNLOAD_MIRRORS:
        url = f"{prefix}{url_path}" if prefix else url_path
        try:
            download(url, dest, show_progress=True)
            return
        except Exception as e:  # noqa: PERF203
            last_err = e
            warn(f"{label} 下载失败({url.split('/')[2]}): {e}")
    raise Fail(f"{label} 全部镜像下载失败: {last_err}")


def installed_version() -> str | None:
    binary = paths.core_binary()
    if not binary.exists():
        return None
    code, out = run_hidden([str(binary), "-v"], timeout=20)
    if code != 0:
        return None
    m = re.search(r"v\d+\.\d+\.\d+", out)
    return m.group(0) if m else out.strip()[:40]


def install_core(version: str | None = None, *, force: bool = False) -> str:
    """安装/升级 mihomo 内核, 返回版本号."""
    paths.ensure_dirs()
    current = installed_version()
    if current and not force:
        ok(f"内核已安装: {current} (使用 --force 重新安装)")
    else:
        ver = version or latest_version()
        asset = asset_name(ver)
        url = f"https://github.com/{MIHOMO_REPO}/releases/download/{ver}/{asset}"
        info(f"准备下载 {asset}")
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            archive = tmpdir / asset
            with Progress(f"下载内核 {ver}"):
                _download_with_mirrors(url, archive, "内核")
            files = extract_archive(archive, tmpdir / "x")
            exe = _pick_core(files)
            if exe is None:
                raise Fail("压缩包中未找到内核可执行文件")
            target = paths.core_binary()
            if target.exists():
                target.unlink()
            shutil.copy2(exe, target)
            if sys.platform != "win32":
                target.chmod(0o755)
        ok(f"内核安装完成: {installed_version() or ver}")

    if sys.platform == "win32":
        install_wintun()
    ensure_geo_files()
    return installed_version() or "unknown"


def _pick_core(files: list[Path]) -> Path | None:
    for f in files:
        n = f.name.lower()
        if n.endswith(".exe"):
            return f
    for f in files:
        if f.name.lower().startswith("mihomo") and f.suffix in ("", ".gz"):
            return f
    # 处理 .gz 单文件
    gz = [f for f in files if f.suffix == ".gz"]
    if gz:
        import gzip

        out = gz[0].with_suffix("")
        with gzip.open(gz[0], "rb") as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return out
    return None


def install_wintun() -> bool:
    """Windows TUN 模式依赖 wintun.dll, 放在内核同目录即可."""
    if sys.platform != "win32":
        return False
    target = paths.wintun_dll()
    if target.exists():
        ok(f"wintun.dll 已就绪 ({target})")
        return True
    info("安装 TUN 驱动 (wintun)")
    last_err: Exception | None = None
    for url in WINTUN_URLS:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "wintun.zip"
                download(url, archive, show_progress=True)
                files = extract_archive(archive, Path(tmp) / "x")
                arch = _arch_tag()
                cand = [
                    f
                    for f in files
                    if f.name.lower() == "wintun.dll" and arch in str(f).lower()
                ]
                if not cand:
                    cand = [f for f in files if f.name.lower() == "wintun.dll"]
                if not cand:
                    raise Fail("wintun.zip 中未找到 wintun.dll")
                shutil.copy2(cand[0], target)
            ok("wintun.dll 安装完成")
            return True
        except Exception as e:  # noqa: PERF203
            last_err = e
            continue
    warn(f"wintun.dll 安装失败, TUN 模式将不可用: {last_err}")
    return False


def ensure_geo_files(*, force: bool = False) -> None:
    """预置 GeoIP/GeoSite 数据, 避免内核首次启动时联网失败."""
    paths.ensure_dirs()
    for name, urls in GEO_FILES.items():
        dest = paths.runtime_dir() / name
        if dest.exists() and not force and dest.stat().st_size > 1024:
            continue
        for url in urls:
            try:
                download(url, dest, show_progress=False, timeout=90)
                if dest.stat().st_size > 1024:
                    ok(f"{name} 已就绪 ({dest.stat().st_size // 1024} KB)")
                    break
            except Exception:
                dest.unlink(missing_ok=True)
                continue
        else:
            warn(f"{name} 下载失败, 分流精度会下降(但不影响基本使用)")
