#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关键安全验证：未认证 WebSocket 客户端能不能拿到日志？must_change 会话是否被 4403 关闭？"""
import base64, os, socket, sqlite3, struct, time, json

HOST, PORT = "127.0.0.1", 9001
DB = "/tmp/apitest/gcp_php.db"

def handshake(cookie):
    s = socket.create_connection((HOST, PORT), timeout=6)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET /ws/logs HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n")
    if cookie:
        req += f"Cookie: {cookie}\r\n"
    s.sendall((req + "\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(4096)
        if not c: break
        buf += c
    return s, buf.decode("utf-8", "replace")

def read_frames(s, seconds=5):
    s.settimeout(seconds)
    out = []
    end = time.time() + seconds
    while time.time() < end:
        try:
            h = s.recv(2)
            if len(h) < 2: break
            op, ln = h[0] & 0x0F, h[1] & 0x7F
            if ln == 126: ln = struct.unpack("!H", s.recv(2))[0]
            elif ln == 127: ln = struct.unpack("!Q", s.recv(8))[0]
            data = b""
            while len(data) < ln:
                ch = s.recv(ln - len(data))
                if not ch: break
                data += ch
            out.append((op, data))
            if op == 0x8:
                break
        except socket.timeout:
            break
        except Exception:
            break
    return out

def send_text(s, text):
    p = text.encode(); m = os.urandom(4)
    masked = bytes(b ^ m[i % 4] for i, b in enumerate(p))
    n = len(p)
    hdr = struct.pack("!BB", 0x81, 0x80 | n) if n < 126 else struct.pack("!BBH", 0x81, 0x80 | 126, n)
    s.sendall(hdr + m + masked)

# 先往库里灌一条独特标记的日志，作为「有没有泄露」的探针
probe = f"SECRET-LOG-PROBE-{int(time.time())}"
cx = sqlite3.connect(DB); cx.execute(
    "INSERT INTO logs(ts,task_id,level,message) VALUES(?,?,?,?)",
    (time.time(), "", "info", probe)); cx.commit()

print("探针日志已写入：" + probe)

print("\n═══ ① 无 Cookie 的客户端 ═══")
s, head = handshake("")
code = head.split(" ")[1] if " " in head else "?"
print(f"  握手状态：{code}")
if code == "101":
    send_text(s, '{"type":"subscribe","since":0}')
    fr = read_frames(s, 6)
    leaked = any(probe.encode() in d for _, d in fr)
    closed = [struct.unpack("!H", d[:2])[0] for op, d in fr if op == 0x8 and len(d) >= 2]
    print(f"  收到 {len(fr)} 个帧；关闭码={closed}")
    print(f"  是否收到探针日志：{'❌ 是（信息泄露！）' if leaked else '✅ 否'}")
    for op, d in fr[:4]:
        print(f"    op={op:#x} {d[:120]!r}")
else:
    print(f"  ✅ 直接拒绝（HTTP {code}），未建立连接")
s.close()

print("\n═══ ② 伪造 Cookie ═══")
s, head = handshake("gcp_sid=" + "f" * 64)
code = head.split(" ")[1] if " " in head else "?"
print(f"  握手状态：{code}")
if code == "101":
    send_text(s, '{"type":"subscribe","since":0}')
    fr = read_frames(s, 6)
    leaked = any(probe.encode() in d for _, d in fr)
    closed = [struct.unpack("!H", d[:2])[0] for op, d in fr if op == 0x8 and len(d) >= 2]
    print(f"  收到 {len(fr)} 个帧；关闭码={closed}")
    print(f"  是否收到探针日志：{'❌ 是（信息泄露！）' if leaked else '✅ 否'}")
else:
    print(f"  ✅ 直接拒绝（HTTP {code}）")
s.close()

print("\n═══ ③ must_change 会话（应先放行再以 4403 关闭）═══")
# 造一个 must_change=1 的用户 + 会话
cx = sqlite3.connect(DB, timeout=5)
cur = cx.execute("SELECT id FROM users WHERE username='wsmu'").fetchone()
if not cur:
    cx.execute("INSERT INTO users(username,password_hash,salt,role,display_name,disabled,"
               "must_change_password,created_at,updated_at,created_by)"
               " VALUES(?,?,?,?,?,0,1,?,?,?)",
               ("wsmu", "x" * 64, "a" * 32, "viewer", "WS测试", time.time(), time.time(), "test"))
    cx.commit()
    cur = cx.execute("SELECT id FROM users WHERE username='wsmu'").fetchone()
uid = cur[0]
# must_change 不在 sessions 表里 —— 它来自 users 表（两端都是 LEFT JOIN users 取）
cx.execute("UPDATE users SET must_change_password=1 WHERE id=?", (uid,))
tok = "wsmustchange" + str(int(time.time()))
cx.execute("DELETE FROM sessions WHERE token=?", (tok,))
cx.execute("INSERT INTO sessions(token,user_id,username,role,ip,user_agent,created_at,last_seen,"
           "expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
           (tok, uid, "wsmu", "viewer", "127.0.0.1", "ws-test",
            time.time(), time.time(), time.time() + 3600))
cx.commit()
s, head = handshake(f"gcp_sid={tok}")
code = head.split(" ")[1] if " " in head else "?"
print(f"  握手状态：{code}")
fr = read_frames(s, 5)
leaked = any(probe.encode() in d for _, d in fr)
closed = [(struct.unpack("!H", d[:2])[0], d[2:].decode('utf-8','replace')) for op, d in fr if op == 0x8 and len(d) >= 2]
print(f"  收到 {len(fr)} 个帧；关闭码={closed}")
if any(c[0] == 4403 for c in closed):
    print("  ✅ 以 4403 关闭（与 Python 版一致）")
elif leaked:
    print("  ❌ 未改密会话仍能收到日志（越权！）")
else:
    print("  ⚠ 未收到 4403；需确认服务端行为")
s.close()
