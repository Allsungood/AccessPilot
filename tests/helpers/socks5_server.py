"""测试用最小 SOCKS5 代理服务器(仅 CONNECT, TCP).

用途: 在没有真实机场订阅的环境下, 为端到端测试提供一个可用的"节点",
从而验证 AccessPilot -> mihomo -> 节点 -> 目标站 的完整链路。
"""
from __future__ import annotations

import select
import socket
import struct
import sys
import threading

__all__ = ["serve_forever", "start_background"]


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已关闭")
        buf += chunk
    return buf


def _handshake(client: socket.socket) -> tuple[str, int]:
    ver, nmethods = _recv_exact(client, 2)
    if ver != 5:
        raise ConnectionError(f"不支持的 SOCKS 版本: {ver}")
    _recv_exact(client, nmethods)
    client.sendall(b"\x05\x00")  # 无需认证

    ver, cmd, _, atyp = _recv_exact(client, 4)
    if cmd != 1:
        raise ConnectionError(f"仅支持 CONNECT, 收到 cmd={cmd}")
    if atyp == 1:
        host = socket.inet_ntoa(_recv_exact(client, 4))
    elif atyp == 3:
        length = _recv_exact(client, 1)[0]
        host = _recv_exact(client, length).decode("utf-8", errors="replace")
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
    else:
        raise ConnectionError(f"未知地址类型: {atyp}")
    port = struct.unpack(">H", _recv_exact(client, 2))[0]
    return host, port


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            readable, _, _ = select.select([src], [], [], 30)
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


def _handle(client: socket.socket) -> None:
    remote: socket.socket | None = None
    try:
        host, port = _handshake(client)
        remote = socket.create_connection((host, port), timeout=15)
        remote.settimeout(None)
        client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
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


def serve_forever(port: int = 1080, host: str = "127.0.0.1") -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)
    print(f"[socks5] listening on {host}:{port}", flush=True)
    while True:
        try:
            client, _ = srv.accept()
        except OSError:
            break
        threading.Thread(target=_handle, args=(client,), daemon=True).start()


def start_background(port: int = 1080) -> threading.Thread:
    """在后台线程启动代理(供测试脚本内嵌使用)."""
    t = threading.Thread(target=serve_forever, args=(port,), daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    serve_forever(int(sys.argv[1]) if len(sys.argv) > 1 else 1080)
