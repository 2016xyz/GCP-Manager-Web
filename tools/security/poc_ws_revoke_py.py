#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC（Python 版）：WebSocket 长连接在「会话被吊销」后是否会被断开

修复前：app.py 的 ws_logs 只在握手时校验会话（第 1680 行），之后死循环推送。
  管理员踢掉被窃的会话后，那条 WebSocket 仍在持续收到任务日志。

做法：直接往库里插一条会话（Python 的验证码是内存态，读不到码，插会话等价）
      → 连上 WS → 删掉该会话（等价于 DELETE /api/sessions/{ref}）→ 等断开。

前置：已用 GCPWEB_DATA_DIR=<dir> GCPWEB_WS_AUTH_EVERY=2 起了 uvicorn。
用法：PY_DATA=/tmp/wspy PY_PORT=8077 python3 ws_revoke_check_py.py
"""
import base64
import os
import secrets
import socket
import sqlite3
import sys
import time

BASE = "/root/.hermes/profiles/2/workspace/gcp-manager-web"
DATA = os.environ.get("PY_DATA") or f"{BASE}/data"
PORT = int(os.environ.get("PY_PORT") or 8000)
DB = f"{DATA}/gcp_web.db"


def db(sql, args=()):
    c = sqlite3.connect(DB)
    try:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        c.commit()
        return rows
    finally:
        c.close()


class WS:
    """极简 WS 客户端：只做握手 + 收帧（含 close 帧解析）"""

    def __init__(self, port, path, cookie):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Cookie: {cookie}\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = self.sock.recv(4096)
            if not d:
                raise RuntimeError("握手期间被关闭")
            buf += d
        head, _, self.buf = buf.partition(b"\r\n\r\n")
        self.status = head.split(b"\r\n")[0].decode()
        self.close_code = None
        self.close_reason = ""

    def _need(self, n):
        while len(self.buf) < n:
            d = self.sock.recv(65536)
            if not d:
                return False
            self.buf += d
        return True

    def frame(self, timeout=30):
        self.sock.settimeout(timeout)
        try:
            if not self._need(2):
                return None, None
            b0, b1 = self.buf[0], self.buf[1]
            op, masked, ln, off = b0 & 0x0F, b1 & 0x80, b1 & 0x7F, 2
            if ln == 126:
                if not self._need(4):
                    return None, None
                ln, off = int.from_bytes(self.buf[2:4], "big"), 4
            elif ln == 127:
                if not self._need(10):
                    return None, None
                ln, off = int.from_bytes(self.buf[2:10], "big"), 10
            mask = b""
            if masked:
                if not self._need(off + 4):
                    return None, None
                mask = self.buf[off:off + 4]
                off += 4
            if not self._need(off + ln):
                return None, None
            pay = self.buf[off:off + ln]
            self.buf = self.buf[off + ln:]
            if masked:
                pay = bytes(c ^ mask[i % 4] for i, c in enumerate(pay))
            if op == 0x8:
                self.close_code = int.from_bytes(pay[:2], "big") if len(pay) >= 2 else None
                self.close_reason = pay[2:].decode("utf-8", "replace") if len(pay) > 2 else ""
                return None, None
            return op, pay
        except (socket.timeout, TimeoutError):
            return "timeout", b""
        except OSError:
            return None, None


def main():
    # 首启会置 must_change_password=1，那会被 4403 挡住（与本次要测的无关）
    db("UPDATE users SET must_change_password=0")
    u = db("SELECT id,username,role FROM users WHERE username='admin'")
    if not u:
        print("!! 库里没有 admin"); return 1
    token = secrets.token_urlsafe(24)
    db("INSERT INTO sessions(token,user_id,username,role,ip,user_agent,"
       "created_at,last_seen,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
       (token, u[0][0], u[0][1], u[0][2], "127.0.0.1", "poc",
        time.time(), time.time(), time.time() + 3600))
    print(f"  造了一条会话（等价登录）：{token[:16]}…")

    ws = WS(PORT, "/ws/logs", f"gcp_sid={token}")
    print(f"  WS 握手: {ws.status}")
    if "101" not in ws.status:
        print("!! 握手失败"); return 1
    op, pay = ws.frame(timeout=8)
    print(f"  首帧 op={op} {pay.decode()[:80] if isinstance(pay, bytes) else ''}")

    print("\n═══ 模拟管理员踢掉该会话（等价 DELETE /api/sessions/{ref}）═══")
    db("DELETE FROM sessions WHERE token=?", (token,))
    print(f"  剩余会话数: {len(db('SELECT token FROM sessions'))}")

    print("\n═══ 等服端主动断开（复检 2 秒，最多等 25 秒）═══")
    t0 = time.time()
    while time.time() - t0 < 25:
        op, pay = ws.frame(timeout=25)
        if op is None:
            break
    el = time.time() - t0

    print("\n═══ 判定 ═══")
    if ws.close_code == 4401:
        print(f"  ✅ 服务端在 {el:.1f}s 内以 4401 断开（原因: {ws.close_reason}）")
        print("     → 会话被吊销后长连接不再继续收任务日志")
        return 0
    if ws.close_code is not None:
        print(f"  ⚠ 被断开但 close code={ws.close_code}（期望 4401）")
        return 2
    print(f"  ❌ 等了 {el:.1f}s 仍未断开 —— 会话已吊销但连接还在收数据（漏洞）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
