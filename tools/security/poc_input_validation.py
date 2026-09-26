#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC：输入校验相关的 500 与「配置投毒」（F5）

修复前实测：
  POST /api/cost/estimate {"hours":"abc"}        -> 500
  POST /api/savings       {"disk_size_gb":"abc"} -> 500
  POST /api/config        {"disk_size_gb":"abc"} -> 200 且**持久化**
    → 之后 POST /api/create（spec 为空时回落到已保存配置）-> 500，每次必炸

修复后应全部变成 4xx 或 200+规整值，且不再出现 500。

用法：PY_DATA=/tmp/pocf5 PY_PORT=8082 python3 poc_input_validation.py
"""
import json
import os
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DATA = os.environ.get("PY_DATA") or "/root/.hermes/profiles/2/workspace/gcp-manager-web/data"
PORT = int(os.environ.get("PY_PORT") or 8000)
DB = f"{DATA}/gcp_web.db"


def db(sql, args=()):
    c = sqlite3.connect(DB, timeout=20)
    try:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        c.commit()
        return rows
    finally:
        c.close()


def call(method, path, body=None, cookie=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


def main():
    db("UPDATE users SET must_change_password=0")
    u = db("SELECT id FROM users WHERE username='admin'")[0]
    token = secrets.token_urlsafe(24)
    db("INSERT INTO sessions(token,user_id,username,role,ip,user_agent,created_at,last_seen,"
       "expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
       (token, u[0], "admin", "admin", "127.0.0.1", "poc", time.time(), time.time(),
        time.time() + 3600))
    ck = f"gcp_sid={token}"

    cases = [
        ("POST", "/api/cost/estimate", {"hours": "abc", "count": "xyz"}),
        ("POST", "/api/cost/estimate", {"disk_size_gb": "abc"}),
        ("POST", "/api/cost/estimate", {"disk_size_gb": -1, "count": 10 ** 9}),
        ("POST", "/api/savings", {"disk_size_gb": "abc"}),
        ("POST", "/api/savings", {"disk_size_gb": [], "machine_type": "e2-micro"}),
        ("POST", "/api/config", {"disk_size_gb": "abc"}),
        ("POST", "/api/config", {"max_per_region": "abc"}),
        ("POST", "/api/config", {"evil_key_9x8y": "1", "disk_size_gb": 20}),
        ("POST", "/api/create", {"count": 1, "dry_run": True}),
    ]

    print("═══ 逐条打（期望：无 500）═══")
    bad = []
    for m, p, b in cases:
        st, body = call(m, p, b, ck)
        tag = "❌ 500" if st >= 500 else "✅"
        if st >= 500:
            bad.append((p, b, st))
        print(f"  {tag} {m} {p:24s} {json.dumps(b, ensure_ascii=False)[:46]:48s} → {st}  {body[:70]}")

    print("\n═══ 检查 /api/config 是否真的没把 'abc' 存进去 ═══")
    st, body = call("GET", "/api/config", None, ck)
    print(f"  GET /api/config → {st}")
    stored = None
    try:
        stored = json.loads(body).get("config") or json.loads(body)
    except Exception:
        pass
    print(f"  返回片段: {body[:200]}")

    print("\n═══ 判定 ═══")
    if bad:
        print(f"  ❌ 仍有 {len(bad)} 个入口返回 5xx（输入校验缺口）")
        return 3
    print("  ✅ 所有非法输入都不再触发 5xx（回落默认值 / 400 拒绝）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
