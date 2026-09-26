#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第二轮审计 PoC：账号导入面（任意路径 JSON）、上传上限、验证码表容量。

覆盖 4 个面：
  A. POST /api/accounts 的 key_path 是否为任意 JSON 读取/注册原语
  B. POST /api/accounts/import_dir 是否会回显任意目录里 JSON 的字段
  C. POST /api/accounts/upload 是否有大小上限
  D. GET  /api/auth/captcha（未认证）是否会无界占用内存

用法：python3 tools/poc_account_import_surface.py
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

TMP = tempfile.mkdtemp(prefix="gw_acct_")
os.environ["GCPWEB_DATA_DIR"] = TMP

from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
from core import auth as auth_mod  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  → {extra}" if extra and not cond else ""))


client = TestClient(appmod.app)
raw = open(os.path.join(TMP, "INITIAL_ADMIN.txt"), encoding="utf-8").read()
admin_pw = re.search(r"密码:\s*(\S+)", raw).group(1)


def login(u, p):
    cap = client.get("/api/auth/captcha").json()
    code = auth_mod.captcha_store._items[cap["captcha_id"]]["code"]
    return client.post("/api/auth/login", json={
        "username": u, "password": p,
        "captcha_id": cap["captcha_id"], "captcha_code": code})


login("admin", admin_pw)
client.post("/api/auth/change_password",
            json={"old_password": admin_pw, "new_password": "Adm1n@GCP2026!"})
ADMIN_PW = "Adm1n@GCP2026!"

# ---------------------------------------------------------------- 造样本文件
# ① 一个**无关的普通 JSON**（模拟服务器上别的程序的配置/凭据）
OTHER = os.path.join(TMP, "some_other_app_config.json")
with open(OTHER, "w", encoding="utf-8") as f:
    json.dump({"client_email": "leaked-victim@corp.example.com",
               "project_id": "victim-secret-project",
               "api_token": "super-secret-token"}, f)

# ② 一个真的服务账号形状的密钥
GOOD = os.path.join(TMP, "keys", "real-sa.json")
os.makedirs(os.path.dirname(GOOD), exist_ok=True)
with open(GOOD, "w", encoding="utf-8") as f:
    json.dump({"type": "service_account", "project_id": "good-project",
               "private_key": "-----BEGIN PRIVATE KEY-----\nAAA\n-----END PRIVATE KEY-----\n",
               "client_email": "good@good-project.iam.gserviceaccount.com",
               "client_id": "1"}, f)

print("=" * 72)
print("第二轮审计 PoC：账号导入面 / 上传上限 / 验证码表容量")
print("=" * 72)

# ══════════════════════════════ A. key_path ══════════════════════════════
print("\n── A. POST /api/accounts 的 key_path ──")

r = client.post("/api/accounts", json={"key_path": OTHER})
check("★ ★ 普通 JSON 不能被当作服务账号密钥注册",
      r.status_code == 400, f"{r.status_code} {r.text[:140]}")
check("★ ★ 报错不回显该文件里的 client_email",
      "leaked-victim@corp.example.com" not in r.text, r.text[:160])
check("★ ★ 报错不回显该文件里的 project_id",
      "victim-secret-project" not in r.text, r.text[:160])
check("★ 拒绝了会留审计",
      any(a["action"] == "add_account_rejected"
          for a in client.get("/api/audit?limit=50").json()["audit"]))

r = client.post("/api/accounts", json={"key_path": "/etc/passwd"})
check("★ /etc/passwd 被拒", r.status_code == 400, f"{r.status_code} {r.text[:100]}")

r = client.post("/api/accounts", json={"key_path": GOOD})
check("★ 真服务账号密钥仍可正常导入（不能把功能改坏）",
      r.status_code == 200 and r.json()["ok"], r.text[:140])
check("★ 导入审计里带上了文件路径",
      any("real-sa.json" in (a.get("detail") or "")
          for a in client.get("/api/audit?limit=50").json()["audit"]))

# ═════════════════════════════ B. import_dir ═════════════════════════════
print("\n── B. POST /api/accounts/import_dir ──")

# 扫 keys/ 子目录（真密钥在那儿）
r = client.post("/api/accounts/import_dir", json={"folder": os.path.dirname(GOOD)}).json()
check("import_dir 仍能工作", r.get("ok") is True, str(r)[:140])
names = [a["file"] for a in r.get("accounts", [])]
check("★ 只导入了真正是服务账号密钥的文件", names == ["real-sa.json"], str(names))

# 再扫顶层目录：那里只有普通 JSON，应当一个都不导入、且逐个给出跳过原因
r2 = client.post("/api/accounts/import_dir", json={"folder": TMP}).json()
check("★ 顶层目录（只有普通 JSON）一个都不导入",
      r2.get("imported") == 0, str(r2.get("imported")))
skipped = {s["file"] for s in r2.get("skipped", [])}
check("★ 普通 JSON 被跳过并给出原因", "some_other_app_config.json" in skipped, str(skipped))
check("★ 跳过原因不回显被跳过文件的字段值",
      "leaked-victim@corp.example.com" not in json.dumps(r2, ensure_ascii=False))
check("★ 跳过原因也不回显 api_token",
      "super-secret-token" not in json.dumps(r2, ensure_ascii=False))

r = client.post("/api/accounts/import_dir", json={"folder": "/etc"})
check("★ 指向 /etc 不会导入任何东西",
      r.json().get("imported") == 0, str(r.json())[:140])

# ═════════════════════════════ C. upload 上限 ════════════════════════════
print("\n── C. POST /api/accounts/upload 大小上限 ──")

big = b'{"type":"service_account","private_key":"' + b"a" * (3 * 1024 * 1024) + b'"}'
r = client.post("/api/accounts/upload",
                files={"file": ("big.json", io.BytesIO(big), "application/json")})
check("★ ★ 超大上传被拒（413）", r.status_code == 413, f"{r.status_code} {r.text[:100]}")

junk = b'{"hello":"world","api_token":"x"}'
r = client.post("/api/accounts/upload",
                files={"file": ("junk.json", io.BytesIO(junk), "application/json")})
check("★ 非服务账号 JSON 上传被拒", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

good_bytes = open(GOOD, "rb").read()
r = client.post("/api/accounts/upload",
                files={"file": ("up.json", io.BytesIO(good_bytes), "application/json")})
check("★ 合法密钥上传仍成功（功能未破坏）",
      r.status_code == 200 and r.json()["ok"], r.text[:140])

# 路径穿越文件名
r = client.post("/api/accounts/upload",
                files={"file": ("../../evil.json", io.BytesIO(good_bytes), "application/json")})
wrote = os.path.exists(os.path.join(TMP, "..", "evil.json"))
check("★ 文件名里的 ../ 不会穿越写出目录", not wrote, "在 data 目录外写出了文件！")

# ═════════════════════════════ D. 验证码表容量 ═══════════════════════════
print("\n── D. /api/auth/captcha（未认证）内存占用 ──")

cs = auth_mod.captcha_store
before = len(cs._items)
for _ in range(cs.MAX_ITEMS + 500):
    cs.new()
after = len(cs._items)
check(f"★ ★ 灌入 {cs.MAX_ITEMS + 500} 次后表仍有界（{after} ≤ {cs.MAX_ITEMS}）",
      after <= cs.MAX_ITEMS, f"{after}")
check("★ 上限生效后新验证码依然可用（淘汰不该影响登录）",
      cs.verify(*cs.new())[0], "刚生成的验证码校验失败")

# 真实 HTTP 走一遍，确认未认证接口不会把内存撑爆
import threading  # noqa: E402
print("   并发打 /api/auth/captcha（真实 HTTP，4 线程 × 300 次）…")
errs = []


def hammer():
    try:
        for _ in range(300):
            client.get("/api/auth/captcha")
    except Exception as exc:
        errs.append(str(exc))


ts = [threading.Thread(target=hammer) for _ in range(4)]
[t.start() for t in ts]
[t.join() for t in ts]
check("并发取验证码无异常", not errs, str(errs[:2]))
check("★ 并发后表仍然有界", len(cs._items) <= cs.MAX_ITEMS, str(len(cs._items)))

print("\n" + "=" * 72)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
print("=" * 72)
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
