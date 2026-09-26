#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC：WebSocket 长连接在「会话被吊销」后是否会被断开

漏洞（修复前）：两版都只在握手时校验会话，之后死循环推送。
  管理员踢掉一个被窃的会话（DELETE /api/sessions/{ref}）之后，
  那条 WebSocket 仍然在持续收到任务日志（日志含 SSH 命令回显）。

做法：把复检间隔调到 2 秒（GCPWEB_WS_AUTH_EVERY=2），连上 WS →
      直接从库里删掉该会话（模拟管理员踢人）→ 等待连接被服务端关闭。

用法：python3 ws_revoke_check.py php     # 测 PHP 版
      python3 ws_revoke_check.py py      # 测 Python 版（需先起 uvicorn）
"""
import base64
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request

BASE = "/root/.hermes/profiles/2/workspace/gcp-manager-web"
MODE = (sys.argv[1] if len(sys.argv) > 1 else "php").lower()


# ── 极简 WebSocket 客户端（只做握手 + 收帧，够用即可）────────────────────
class WSClient:
    def __init__(self, host, port, path, cookie):
        self.sock = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Cookie: {cookie}\r\n\r\n")
        self.sock.sendall(req.encode())
        self.buf = b""
        self.closed_code = None
        self.closed_reason = ""
        # 读握手响应
        while b"\r\n\r\n" not in self.buf:
            d = self.sock.recv(4096)
            if not d:
                raise RuntimeError("握手期间连接被关闭")
            self.buf += d
        head, _, rest = self.buf.partition(b"\r\n\r\n")
        self.status = head.split(b"\r\n")[0].decode()
        self.buf = rest

    def _need(self, n):
        while len(self.buf) < n:
            d = self.sock.recv(65536)
            if not d:
                return False
            self.buf += d
        return True

    def recv_frame(self, timeout=40):
        """返回 (opcode, payload)；连接关闭返回 (None, None)"""
        self.sock.settimeout(timeout)
        try:
            if not self._need(2):
                return None, None
            b0, b1 = self.buf[0], self.buf[1]
            op = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            off = 2
            if ln == 126:
                if not self._need(4):
                    return None, None
                ln = int.from_bytes(self.buf[2:4], "big")
                off = 4
            elif ln == 127:
                if not self._need(10):
                    return None, None
                ln = int.from_bytes(self.buf[2:10], "big")
                off = 10
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
            if op == 0x8:      # close
                code = int.from_bytes(pay[:2], "big") if len(pay) >= 2 else None
                reason = pay[2:].decode("utf-8", "replace") if len(pay) > 2 else ""
                self.closed_code, self.closed_reason = code, reason
                return None, None
            return op, pay
        except (socket.timeout, TimeoutError):
            return "timeout", b""
        except OSError:
            return None, None


def main():
    data = "/tmp/wsrev_" + MODE
    shutil.rmtree(data, ignore_errors=True)
    os.makedirs(data, exist_ok=True)
    env = dict(os.environ, GCPWEB_DATA_DIR=data, GCPWEB_WS_AUTH_EVERY="2")

    if MODE == "php":
        subprocess.run(["php", "bin/init.php"], cwd=f"{BASE}/php", env=env,
                       capture_output=True, timeout=120)
        dbp = f"{data}/gcp_php.db"
        http_port, ws_port = 8096, 9096
        procs = [
            subprocess.Popen(["php", "-S", f"127.0.0.1:{http_port}", "-t", "public", "public/router.php"],
                             cwd=f"{BASE}/php", env=env,
                             stdout=open("/tmp/wsrev_http.log", "w"), stderr=subprocess.STDOUT),
            subprocess.Popen(["php", "bin/ws-server.php", "--host", "127.0.0.1", "--port", str(ws_port)],
                             cwd=f"{BASE}/php", env=env,
                             stdout=open("/tmp/wsrev_ws.log", "w"), stderr=subprocess.STDOUT),
        ]
        pwfile = f"{data}/INITIAL_ADMIN.txt"
    else:
        # Python 版：要求外部已起服务（本脚本不负责拉起 uvicorn）
        pydata = os.environ.get("PY_DATA") or f"{BASE}/data"
        dbp = f"{pydata}/gcp_web.db"
        http_port = int(os.environ.get("PY_PORT") or 8000)
        ws_port = http_port
        procs = []
        pwfile = f"{pydata}/INITIAL_ADMIN.txt"

    try:
        time.sleep(2.5)
        txt = open(pwfile, encoding="utf-8").read()
        pw = re.search(r"密码[：:]\s*(\S+)", txt).group(1)

        # 关掉强制改密并登录
        c = sqlite3.connect(dbp)
        c.execute("UPDATE users SET must_change_password=0")
        c.commit()

        def db(sql, args=()):
            cc = sqlite3.connect(dbp)
            try:
                cur = cc.execute(sql, args)
                rows = cur.fetchall()
                cc.commit()
                return rows
            finally:
                cc.close()

        if MODE == "py":
            # Python 版验证码是内存态（不在库里），取不到码 → 直接造一条等价会话
            import secrets as _secrets
            token = _secrets.token_urlsafe(24)
            u = db("SELECT id,username,role FROM users WHERE username='admin'")[0]
            db("INSERT INTO sessions(token,user_id,username,role,ip,user_agent,"
               "created_at,last_seen,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
               (token, u[0], u[1], u[2], "127.0.0.1", "poc", time.time(), time.time(),
                time.time() + 3600))
            print(f"  直接造会话（绕过内存态验证码）：{token[:16]}…")
            return _finish(ws_port, token, db, data_cleanup=None)

        print(f"  [debug] 将 GET http://127.0.0.1:{http_port}/api/auth/captcha")
        if MODE == "php":
            cap = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{http_port}/api/auth/captcha").read())
            code = db("SELECT code FROM captcha WHERE cid=?", (cap["captcha_id"],))[0][0]
        else:
            cap = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{http_port}/api/auth/captcha").read())
            c2 = sqlite3.connect(f"{BASE}/data/gcp_web.db")
            code = c2.execute("SELECT code FROM captcha WHERE cid=?", (cap["captcha_id"],)).fetchone()[0]
            c2.close()

        print(f"  [debug] 将 POST http://127.0.0.1:{http_port}/api/auth/login")
        login = json.dumps({"username": "admin", "password": pw,
                            "captcha_id": cap["captcha_id"], "captcha_code": code}).encode()
        rq = urllib.request.Request(f"http://127.0.0.1:{http_port}/api/auth/login", data=login,
                                    method="POST", headers={"Content-Type": "application/json"})
        r = urllib.request.urlopen(rq, timeout=20)
        setck = r.headers.get_all("Set-Cookie") or []
        token = None
        for sc in setck:
            if sc.startswith("gcp_sid="):
                token = sc.split(";")[0].split("=", 1)[1]
        if not token:
            print("!! 登录未拿到会话"); return 1
        print(f"  登录成功，会话 {token[:16]}…")

        print(f"  [debug] 将连 WS 127.0.0.1:{ws_port}")
        ws = WSClient("127.0.0.1", ws_port, "/ws/logs", f"gcp_sid={token}")
        print(f"  WS 握手: {ws.status}")
        if "101" not in ws.status:
            print(f"!! 握手失败：{ws.status}"); return 1
        op, pay = ws.recv_frame(timeout=8)
        if op == 1:
            print(f"  收到 hello: {pay.decode()[:90]}")
        else:
            print(f"  首帧 op={op}（非预期，继续）")

        print("\n═══ 模拟管理员踢掉这个会话（直接删库，等价于 DELETE /api/sessions/{ref}）═══")
        db("DELETE FROM sessions WHERE token=?", (token,))
        print(f"  剩余会话数: {len(db('SELECT token FROM sessions'))}")

        print("\n═══ 等待服务端主动断开（复检间隔已设为 2 秒，最多等 25 秒）═══")
        t0 = time.time()
        code_got = None
        while time.time() - t0 < 25:
            op, pay = ws.recv_frame(timeout=25)
            if op is None:
                code_got = ws.closed_code
                break
            if op == "timeout":
                continue
        el = time.time() - t0

        print(f"\n═══ 判定 ═══")
        if code_got == 4401:
            print(f"  ✅ 服务端在 {el:.1f}s 内以 4401（会话已失效）主动断开")
            print(f"     断开原因: {ws.closed_reason}")
            print("     → 会话被吊销后，长连接不再继续收到任务日志")
            ok = True
        elif ws.closed_code is not None:
            print(f"  ⚠ 被断开但 close code = {ws.closed_code}（期望 4401）")
            ok = False
        else:
            print(f"  ❌ 等了 {el:.1f}s 仍未断开 —— 会话已吊销但连接还在收数据（漏洞）")
            ok = False
        return 0 if ok else 2
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=8)
            except Exception:
                p.kill()
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
