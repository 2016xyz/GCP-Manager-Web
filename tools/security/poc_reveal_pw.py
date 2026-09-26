#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC：POST /api/instances/password 的登录密码二次验证 + 限速

漏洞（修复前）：PHP 版只搬了「出示 + 审计」，漏掉「登录密码复核 + 限速」。
  前端因为 Python 版要求密码，照样弹框让用户输入 —— 用户以为有防护，
  而后端直接忽略该字段，只凭会话 Cookie 就回明文 root 密码。

用法： python3 poc_reveal_pw.py
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "/root/.hermes/profiles/2/workspace/gcp-manager-web"
DATA = "/tmp/pocdata"
PORT = 8097
ROOT = f"http://127.0.0.1:{PORT}"
PW_FILE = os.path.join(DATA, "INITIAL_ADMIN.txt")


def http(path, body=None, cookie=None, method=None):
    url = ROOT + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, raw, r.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers.get_all("Set-Cookie") or []
    except Exception as e:
        return -1, str(e), []


def db(sql, args=()):
    c = sqlite3.connect(os.path.join(DATA, "gcp_php.db"))
    try:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        c.commit()
        return rows
    finally:
        c.close()


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)
    env = dict(os.environ, GCPWEB_DATA_DIR=DATA)
    subprocess.run(["php", "bin/init.php"], cwd=f"{BASE}/php", env=env,
                   capture_output=True, timeout=120)
    txt = open(PW_FILE, encoding="utf-8").read()
    m = re.search(r"密码[：:]\s*(\S+)", txt)
    pw = m.group(1) if m else None
    if not pw:
        print("!! 读不到初始密码\n" + txt[:300]); return 1
    print(f"  测试管理员密码: {pw}")

    # 放一条带 root 密码的实例记录（模拟真建过机器）
    db("INSERT OR REPLACE INTO vm_passwords"
       "(name,ip,password,account_id,zone,machine_type,image_key,disk_type,disk_size_gb,"
       "note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
       ("vm-poc", "1.2.3.4", "ROOT_PW_LEAKED_12345", 1, "z", "e2", "k", "pd", 10, "", 1, 1))
    db("UPDATE users SET must_change_password=0")

    srv = subprocess.Popen([ "php", "-S", f"127.0.0.1:{PORT}", "-t", "public", "public/router.php" ],
                           cwd=f"{BASE}/php", env=env,
                           stdout=open("/tmp/poc_srv.log", "w"), stderr=subprocess.STDOUT)
    try:
        time.sleep(2.5)
        # 登录
        _, cap, _ = http("/api/auth/captcha")
        cid = json.loads(cap)["captcha_id"]
        code = db("SELECT code FROM captcha WHERE cid=?", (cid,))[0][0]
        st, body, ck = http("/api/auth/login",
                            {"username": "admin", "password": pw,
                             "captcha_id": cid, "captcha_code": code})
        if st != 200:
            print(f"!! 登录失败 HTTP {st}: {body[:200]}"); return 1
        cookie = "; ".join(c.split(";")[0] for c in ck if c.startswith("gcp_sid"))
        print(f"  登录成功（会话 {cookie[:24]}…）")

        def req(label, payload):
            st, body, _ = http("/api/instances/password", payload, cookie=cookie)
            short = body[:118].replace("\n", " ")
            print(f"  {label:<44} HTTP {st:<4} {short}")
            return st, body

        print("\n═══ 测试 1：不带 password 字段 ═══")
        print("  （修复前：直接回 root_password 明文；修复后：400 要求输入密码）")
        req('{"name":"vm-poc"}', {"name": "vm-poc"})

        print("\n═══ 测试 2：password 错误 ═══")
        req('{name, password:"wrong"}', {"name": "vm-poc", "password": "wrong"})

        print("\n═══ 测试 3：password 正确 ═══")
        req("{name, password:正确}", {"name": "vm-poc", "password": pw})

        print("\n═══ 测试 4：限速（连续 6 次错误密码）═══")
        for i in range(1, 7):
            req(f"第 {i} 次错误密码", {"name": "vm-poc", "password": "bad"})
        print("  —— 锁定期内改用【正确】密码，应被 429 挡住（证明与登录共用限速）——")
        req("正确密码但已锁定", {"name": "vm-poc", "password": pw})

        print("\n═══ 测试 5：审计记录 ═══")
        for r in db("SELECT detail,ok,ts FROM audit WHERE action='reveal_root_password' "
                    "ORDER BY id DESC LIMIT 10"):
            print(f"    ok={r[1]}  {r[0][:46]}")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
    print("\nPoC 结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
