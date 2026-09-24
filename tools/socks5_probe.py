#!/usr/bin/env python3
"""
最小 SOCKS5 服务端（仅用于验证代理链路真的通，不做认证）

用途：起在本地，然后让 parse_proxy_input 解析出的 socks5h:// URL 走它去访问外网。
如果这样能拿到响应，说明「代理解析 → 环境变量 → requests/PySocks → 实际连接」
整条链路是通的，而不只是「协议名被认识」。

支持 CONNECT 命令 + 无认证 + 域名地址类型（ATYP=0x03），
后者正是 socks5h 的关键：由代理端解析域名。
"""
import socket
import struct
import sys
import threading

HOST, PORT = "127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 11080
seen = []


def pipe(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(conn, addr):
    try:
        ver, n = conn.recv(2)
        methods = conn.recv(n)
        if ver != 5:
            return
        conn.sendall(b"\x05\x00")              # 无需认证
        head = conn.recv(4)
        ver, cmd, _, atyp = head
        if atyp == 1:                          # IPv4
            host = socket.inet_ntoa(conn.recv(4))
        elif atyp == 3:                        # 域名 ← socks5h 走这条
            ln = conn.recv(1)[0]
            host = conn.recv(ln).decode()
        elif atyp == 4:                        # IPv6
            host = socket.inet_ntop(socket.AF_INET6, conn.recv(16))
        else:
            return
        port = struct.unpack("!H", conn.recv(2))[0]
        seen.append((host, port, addr))
        # 关键证据：域名是在这里（代理端）解析的，说明 socks5h 语义正确
        print(f"[socks5] CONNECT 目标 {host}:{port}  来自 {addr}", flush=True)
        try:
            up = socket.create_connection((host, port), timeout=10)
        except OSError as e:
            conn.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
            print(f"[socks5] 连接目标失败：{e}", flush=True)
            return
        conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        t = threading.Thread(target=pipe, args=(conn, up), daemon=True)
        t.start()
        pipe(up, conn)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(16)
    print(f"[socks5] 监听 {HOST}:{PORT}", flush=True)
    while True:
        c, a = srv.accept()
        threading.Thread(target=handle, args=(c, a), daemon=True).start()


if __name__ == "__main__":
    main()