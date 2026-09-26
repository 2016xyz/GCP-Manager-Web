#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC：只读账号能否从「任务」相关接口读到 root 密码明文（F1）

漏洞：创建任务的 payload 里存着 root_password 明文（worker 执行需要它），
      而 /api/tasks、/api/tasks/{id} 的权限点是 view（viewer 是只读角色）。
      实测：viewer 一次请求即拿到全部实例 root 密码。

覆盖渠道：/api/tasks、/api/tasks/{id}、/api/logs

用法：python3 poc_task_payload_leak.py php     # 自起 PHP 服务
      python3 poc_task_payload_leak.py py      # 需外部已起 uvicorn（PY_DATA/PY_PORT）
"""
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "/root/.hermes/profiles/2/workspace/gcp-manager-web"
MODE = (sys.argv[1] if len(sys.argv) > 1 else "php").lower()
SECRET = "ROOT_PW_IN_TASK_PAYLOAD_7d21"
TASK_ID = "poc-task-f1"


def db_connect(path):
    return sqlite3.connect(path, timeout=20)


def q(path, sql, args=()):
    c = db_connect(path)
    try:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        c.commit()
        return rows
    finally:
        c.close()


def http(port, path, cookie=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    if MODE == "php":
        data = "/tmp/pocf1"
        shutil.rmtree(data, ignore_errors=True)
        os.makedirs(data, exist_ok=True)
        env = dict(os.environ, GCPWEB_DATA_DIR=data)
        subprocess.run(["php", "bin/init.php"], cwd=f"{BASE}/php", env=env,
                       capture_output=True, timeout=120)
        dbp = f"{data}/gcp_php.db"
        port = 8095
        srv = subprocess.Popen(
            ["php", "-S", f"127.0.0.1:{port}", "-t", "public", "public/router.php"],
            cwd=f"{BASE}/php", env=env,
            stdout=open("/tmp/pocf1.log", "w"), stderr=subprocess.STDOUT)
        payload_col = "payload"
    else:
        data = os.environ.get("PY_DATA") or f"{BASE}/data"
        dbp = f"{data}/gcp_web.db"
        port = int(os.environ.get("PY_PORT") or 8000)
        srv = None
        payload_col = "payload"

    try:
        if srv:
            time.sleep(2.5)

        # 1) 造一条「真实形态」的创建任务：payload 含 root_password
        payload = {"count": 1, "root_password": SECRET,
                   "spec": {"machine_type": "e2-highcpu-4"},
                   "ssh_public_key": "ssh-ed25519 AAAAC3Nz..."}
        now = time.time()
        q(dbp, "DELETE FROM tasks WHERE id=?", (TASK_ID,))
        q(dbp, "INSERT INTO tasks(id,kind,status,payload,result,message,created_at,updated_at)"
               " VALUES(?,?,?,?,?,?,?,?)",
          (TASK_ID, "create", "done", json.dumps(payload), "{}", "done", now, now))
        # 2) 造一条含明文密码的日志（老代码会写这种）
        q(dbp, "INSERT INTO logs(ts,task_id,level,message) VALUES(?,?,?,?)",
          (now, TASK_ID, "success", f"[acct] ✅ vm-1 创建成功 | Root密码 {SECRET}"))

        # 3) 造一个只读 viewer 会话
        u = q(dbp, "SELECT id,username,role FROM users WHERE username='admin'")[0]
        if not q(dbp, "SELECT id FROM users WHERE username='pocviewer'"):
            q(dbp, "INSERT INTO users(username,password_hash,salt,role,display_name,"
                   "must_change_password,disabled,created_at,updated_at,failed_count)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?)",
              ("pocviewer", "x" * 64, "y" * 32, "viewer", "PoC 只读", 0, 0, now, now, 0))
        vu = q(dbp, "SELECT id FROM users WHERE username='pocviewer'")[0]
        token = secrets.token_urlsafe(24)
        q(dbp, "INSERT INTO sessions(token,user_id,username,role,ip,user_agent,"
               "created_at,last_seen,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
          (token, vu[0], "pocviewer", "viewer", "127.0.0.1", "poc", now, now, now + 3600))
        ck = f"gcp_sid={token}"
        print(f"  已以 viewer（只读）建立会话；已写入含明文密码的任务与日志")

        print("\n═══ 以 viewer 身份逐条打接口 ═══")
        leaks = []
        for path in ["/api/tasks", f"/api/tasks/{TASK_ID}", "/api/logs?since_id=0&limit=200"]:
            st, body = http(port, path, cookie=ck)
            hit = SECRET in body
            print(f"  {path:38s} HTTP {st:<4} 含 root 密码明文: {hit}")
            if st == 200 and hit:
                i = body.find(SECRET)
                print(f"      泄漏片段: …{body[max(0,i-80):i+len(SECRET)+10]}…")
                leaks.append(path)

        print("\n═══ 判定 ═══")
        if leaks:
            print(f"  ❌ 高危：只读账号从 {len(leaks)} 个渠道读到 root 密码明文")
            for x in leaks:
                print(f"       - {x}")
            print("     → 绕过 /api/instances/password 的登录密码二次验证")
            return 3
        print("  ✅ 三个渠道均未出现密码明文（payload 与日志都已脱敏）")
        return 0
    finally:
        if srv:
            srv.terminate()
            try:
                srv.wait(timeout=8)
            except Exception:
                srv.kill()
        shutil.rmtree("/tmp/pocf1", ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
