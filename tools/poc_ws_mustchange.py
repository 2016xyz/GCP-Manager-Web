#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC：未改密用户能否连接 /ws/logs？

背景：v1.2.4 给 HTTP 加了强制改密中间件（未改密前除改密/登出/查自己外全部 403）。
但 WebSocket 端点 /ws/logs 是独立握手路径，中间件不一定覆盖。
本脚本实测这一点：不做推测。

用法：python3 tools/poc_ws_mustchange.py
"""
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

TMP = tempfile.mkdtemp(prefix="gw_ws_")
os.environ["GCPWEB_DATA_DIR"] = TMP

from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
from core import auth as auth_mod  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  → {extra}" if extra and not cond else ""))


client = TestClient(appmod.app)
admin_pw = open(os.path.join(TMP, "INITIAL_ADMIN.txt"), encoding="utf-8").read()
admin_pw = [l for l in admin_pw.splitlines() if l.startswith("密码")][0].split(":", 1)[1].strip()


def login(u, p):
    cap = client.get("/api/auth/captcha").json()
    code = auth_mod.captcha_store._items[cap["captcha_id"]]["code"]
    return client.post("/api/auth/login", json={
        "username": u, "password": p,
        "captcha_id": cap["captcha_id"], "captcha_code": code})


print("=" * 68)
print("PoC：未改密用户能否连 /ws/logs")
print("=" * 68)

# 管理员先改密（否则它自己也被拦）
login("admin", admin_pw)
r = client.post("/api/auth/change_password",
                json={"old_password": admin_pw, "new_password": "Adm1n@GCP2026!"})
check("管理员改密", r.status_code == 200, r.text[:100])

# 建一个必须改密的账号
r = client.post("/api/users", json={"username": "mc_user", "password": "Init@GCP2026",
                                    "role": "operator", "must_change": True})
check("创建 must_change 账号", r.status_code == 200, r.text[:100])

# 以该账号登录（此时处于「未改密」状态）
r = login("mc_user", "Init@GCP2026")
check("登录成功", r.status_code == 200, r.text[:120])
me = client.get("/api/auth/me").json()
check("确认当前处于未改密状态",
      me["user"].get("must_change_password") is True, str(me["user"]))

# ① HTTP 侧应当被拦（对照组）
r = client.get("/api/status")
check("HTTP /api/status 被拦（403）", r.status_code == 403, str(r.status_code))

# ② WebSocket 侧 —— 这是被测点
print("\n-- 关键：WebSocket /ws/logs --")
try:
    with client.websocket_connect("/ws/logs") as ws:
        ws.accept() if False else None
        # 能进 with 就说明握手被接受（没被 4401 关掉）
        check("★ 未改密用户能连上 /ws/logs（说明 WS 未被强制改密约束）",
              True, "")
        ws_accepted = True
except Exception as e:
    ws_accepted = False
    check("未改密用户连 /ws/logs 被拒", True, str(type(e).__name__))

# ③ 对照组：完全未认证能否连
print("\n-- 对照：未认证 --")
client.cookies.clear()
try:
    with client.websocket_connect("/ws/logs") as ws:
        check("未认证竟然能连上（严重）", False, "握手被接受")
except Exception as e:
    check("未认证连接被拒（正常）", True, str(type(e).__name__))

print("\n" + "=" * 68)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
print("=" * 68)
if ws_accepted:
    print("\n结论：/ws/logs 未受 must_change_password 约束 —— HTTP 被拦、WS 放行，")
    print("      属「强制策略覆盖不全」，应把同一判断加到 ws 握手处。")
else:
    print("\n结论：/ws/logs 同样受控，中间件已覆盖 WebSocket 握手。")

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
