"""通用工具: 终端输出、HTTP 下载、压缩包解压、校验和、管理员检测."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import ssl
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------------- #
# 终端输出
# --------------------------------------------------------------------------- #

# 没有控制台时 sys.stdout 会是 None(pythonw.exe / 打包成窗口化 exe 后的运行方式),
# 直接 sys.stdout.isatty() 会 AttributeError —— 而且是**在 import 阶段**炸,
# 进程秒退、没有窗口、没有任何提示。用 getattr 兜一层。
_COLOR = bool(getattr(sys.stdout, "isatty", lambda: False)()) and (
    os.environ.get("NO_COLOR") is None
)


def _reconfigure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


_reconfigure_stdout()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t: str) -> str:
    return c(t, "1")


def green(t: str) -> str:
    return c(t, "32")


def red(t: str) -> str:
    return c(t, "31")


def yellow(t: str) -> str:
    return c(t, "33")


def cyan(t: str) -> str:
    return c(t, "36")


def dim(t: str) -> str:
    return c(t, "2")


def info(msg: str) -> None:
    print(f"{cyan('[i]')} {msg}")


def ok(msg: str) -> None:
    print(f"{green('[+]')} {msg}")


def warn(msg: str) -> None:
    print(f"{yellow('[!]')} {msg}")


def err(msg: str) -> None:
    print(f"{red('[x]')} {msg}", file=sys.stderr)


class Fail(Exception):
    """可预期的用户级错误, CLI 捕获后以简洁信息退出."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: 订阅服务商会按 UA 返回不同格式, 使用各客户端常见 UA 兼容性最好
SUB_UA = "clash-verge/v2.0.0"


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    return ctx


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 30.0,
    proxy: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """发起 HTTP 请求, 返回 (状态码, 响应头, 响应体)."""
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, resp_headers, resp.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body


def http_text(url: str, **kw: Any) -> str:
    _, _, body = http_request(url, **kw)
    return body.decode("utf-8", errors="replace")


def fetch_json(url: str, timeout: float = 10.0) -> Any:
    status, _, body = http_request(url, timeout=timeout)
    if status >= 400:
        raise Fail(f"HTTP {status}: {url}")
    return json.loads(body.decode("utf-8", errors="replace"))


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def download(
    url: str,
    dest: Path,
    *,
    timeout: float = 60.0,
    show_progress: bool = True,
    headers: dict[str, str] | None = None,
) -> Path:
    """下载到 dest, 带进度显示与重定向."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with opener.open(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            start = time.time()
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    if show_progress and total:
                        pct = got * 100 / total
                        speed = got / max(time.time() - start, 0.001)
                        sys.stdout.write(
                            f"\r    {pct:5.1f}%  {human_size(got)}/{human_size(total)}"
                            f"  {human_size(speed)}/s   "
                        )
                        sys.stdout.flush()
        if show_progress and total:
            sys.stdout.write("\r" + " " * 60 + "\r")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)
    return dest


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_archive(archive: Path, dest: Path) -> list[Path]:
    """解压 zip / tar.gz / tar.xz, 返回解出的文件列表."""
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    out: list[Path] = []
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                target = _safe_join(dest, member)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                out.append(target)
    elif name.endswith((".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar")):
        mode = "r:gz" if name.endswith((".tar.gz", ".tgz")) else (
            "r:xz" if name.endswith((".tar.xz", ".txz")) else "r:"
        )
        with tarfile.open(archive, mode) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                target = _safe_join(dest, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                out.append(target)
    else:
        raise Fail(f"不支持的压缩格式: {archive.name}")
    return out


def _safe_join(root: Path, member: str) -> Path:
    target = (root / member).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise Fail(f"压缩包包含非法路径: {member}")
    return target


# --------------------------------------------------------------------------- #
# 平台能力
# --------------------------------------------------------------------------- #


def is_admin() -> bool:
    if sys.platform != "win32":
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def is_windows() -> bool:
    return sys.platform == "win32"


def run_hidden(cmd: Iterable[str], timeout: float = 20.0) -> tuple[int, str]:
    """静默执行命令, 返回 (返回码, 合并输出). 不弹黑窗."""
    import subprocess

    creation = 0
    if sys.platform == "win32":
        creation = 0x08000000  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(
            list(cmd),
            capture_output=True,
            timeout=timeout,
            creationflags=creation,
        )
    except FileNotFoundError:
        return 127, ""
    except Exception as e:  # pragma: no cover
        return 1, str(e)
    text = (p.stdout or b"").decode("utf-8", errors="replace") + (
        p.stderr or b""
    ).decode("utf-8", errors="replace")
    return p.returncode, text


def open_in_browser(url: str) -> None:
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:
        pass


def json_dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def json_load(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def retry(fn: Callable[[], Any], times: int = 3, delay: float = 1.0) -> Any:
    last: Exception | None = None
    for i in range(times):
        try:
            return fn()
        except Exception as e:  # noqa: PERF203
            last = e
            if i < times - 1:
                time.sleep(delay * (i + 1))
    assert last is not None
    raise last


class Progress:
    """极简 spinner, 用于不确定耗时的步骤."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._start = time.time()

    def __enter__(self) -> "Progress":
        sys.stdout.write(f"    {self.label} ...")
        sys.stdout.flush()
        return self

    def __exit__(self, *exc: Any) -> None:
        dt = time.time() - self._start
        tail = "ok" if exc[0] is None else "fail"
        sys.stdout.write(f"\r    {self.label} ... {tail} ({dt:.1f}s)\n")
        sys.stdout.flush()


def read_bytes(p: Path) -> bytes:
    with open(p, "rb") as fh:
        return fh.read()
