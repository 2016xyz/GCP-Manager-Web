#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC：只读角色（viewer）能否拿到实例 root 密码明文？

设计意图（app.py:1228 注释原文）：
  「实例列表只需要 view 权限，而 viewer 是只读角色。如果列表接口直接把 root 密码
    明文吐出来，等于任何能登录的人（哪怕是只读账号）都能一次性拿到全部机器的
    root。所以列表只回 has_password，要看明文必须重新输入**自己的登录密码**。」

本脚本实测这条设计意图是否成立：不做推测，直接打接口。

用法：python3 tools/poc_instances_password_leak.py
"""
import os
import re
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

TMP = tempfile.mkdtemp(prefix="gw_pwleak_")
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
admin_pw = re.search(r"密码:\s*(\S+)", raw).group(1) or ""


def login(u, p):
    cap = client.get("/api/auth/captcha").json()
    code = auth_mod.captcha_store._items[cap["captcha_id"]]["code"]
    return client.post("/api/auth/login", json={
        "username": u, "password": p,
        "captcha_id": cap["captcha_id"], "captcha_code": code})


SECRET = "R00tPw@SuperSecret-9f3a2b"

print("=" * 70)
print("PoC：只读角色经 GET /api/instances?sync=false 读取 root 密码明文")
print("=" * 70)

login("admin", admin_pw)
# 首次启动的管理员自身也处于「必须先改密」状态，先改掉
_r = client.post("/api/auth/change_password",
                 json={"old_password": admin_pw, "new_password": "Adm1n@GCP2026!"})
check("管理员完成首次改密", _r.status_code == 200, _r.text[:120])
ADMIN_PW = "Adm1n@GCP2026!"

# 建一个**只读** viewer 账号
r = client.post("/api/users", json={"username": "ro_viewer", "password": "View@GCP2026",
                                    "role": "viewer", "must_change": False})
check("创建 viewer（只读角色）", r.status_code == 200, r.text[:120])

# 云端放一台「带 root 密码记录」的实例（模拟真实创建后的状态）
appmod.store.save_vm("prod-db-01", "10.128.0.9", SECRET,
                        account_id=1, zone="asia-south2-a",
                        machine_type="e2-micro", note="生产数据库")
vm = appmod.store.get_vm("prod-db-01")
check("已写入一条带 root 密码的实例记录",
      vm and vm.get("password") == SECRET, str(vm)[:120])

# 切到 viewer 身份
client.post("/api/auth/logout")
r = login("ro_viewer", "View@GCP2026")
check("viewer 登录成功", r.status_code == 200, r.text[:120])
me = client.get("/api/auth/me").json()
check("确认角色是 viewer（只读）", me["user"]["role"] == "viewer"
      and me.get("permissions") == ["view"], str(me.get("permissions")))

print("\n-- ① 设计要求的路径：二次验证（应能挡住 viewer 无密码时的直接取用）--")
r = client.post("/api/instances/password", json={"name": "prod-db-01"})
check("不提供登录密码 → 被拒", r.status_code == 400, f"{r.status_code} {r.text[:80]}")
r = client.post("/api/instances/password", json={"name": "prod-db-01", "password": "wrong"})
check("提供错误登录密码 → 403", r.status_code == 403, f"{r.status_code} {r.text[:80]}")

print("\n-- ② 实时列表 sync=true（走 GCP，应脱敏）--")
r = client.get("/api/instances?sync=true")
check("sync=true 返回 200（无真实凭据时可能是错误对象）", r.status_code == 200, str(r.status_code))
check("★ sync=true 列表不含 root 密码明文", SECRET not in r.text,
      "响应体里出现了明文密码！")
check("★ sync=true 响应体里没有 password 字段名",
      '"password"' not in r.text, "出现了 password 字段")

print("\n-- ③ 关键：sync=false 走 store.get_all_vms() --")
r = client.get("/api/instances?sync=false")
body2 = r.text
check("sync=false 返回 200", r.status_code == 200, str(r.status_code))
leaked2 = SECRET in body2
check("★ ★ viewer 经 sync=false 拿到 root 密码明文", not leaked2,
      f"响应体里出现了明文密码 {SECRET}")
check("★ ★ sync=false 响应体里不含 password 字段名",
      '"password"' not in body2, "出现了 password 字段")
check("★ 仍保留 has_password 标记（前端要靠它显示图标）",
      '"has_password"' in body2, "has_password 丢了，会把功能改坏")

print("\n-- ④ 顺带核对其它可能泄露点 --")
for path, label in [("/api/tasks?limit=50", "任务列表"),
                    ("/api/logs?limit=200", "日志"),
                    ("/api/status", "状态")]:
    rr = client.get(path)
    if SECRET in rr.text:
        check(f"★ {label} 未泄露密码", False, f"{path} 含明文密码")
    else:
        check(f"{label} 未泄露密码", True)

print("\n" + "=" * 70)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
print("=" * 70)
if leaked2:
    print("""
结论：**设计意图被绕过**。
  · 二次验证接口 /api/instances/password 工作正常（挡住无密码/错密码）
  · 但 GET /api/instances?sync=false 直接返回 store.get_all_vms() 的结果，
    而该方法执行的是 SELECT * FROM vm_passwords —— 含 password 列，
    没有任何脱敏。
  · 该接口只需 view 权限 → 只读 viewer 一次请求即可拿到**全部实例的
    root 密码明文**，二次验证形同虚设。
""")
else:
    print("\n结论：未复现，列表接口已脱敏。")

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
