#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""追加 S 段：本轮安全审计修复的回归断言（幂等）"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(BASE, "tests_e2e.py")
s = open(P, encoding="utf-8").read()

MARK = "# ══════════ S. 安全审计修复回归 ══════════"
if MARK in s:
    print("已存在，跳过")
    sys.exit(0)

ANCHOR = 'print("\\n" + "=" * 76)\nprint(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")'
if ANCHOR not in s:
    print("❌ 未找到汇总锚点")
    sys.exit(1)

NEW = '''# ══════════ S. 安全审计修复回归 ══════════
print("\\n── S. 安全审计修复回归 ──")

_app = open(os.path.join(BASE_DIR, "app.py"), encoding="utf-8").read()
_auth = open(os.path.join(BASE_DIR, "core", "auth.py"), encoding="utf-8").read()
_ssh = open(os.path.join(BASE_DIR, "core", "ssh.py"), encoding="utf-8").read()
_inst = open(os.path.join(BASE_DIR, "install.sh"), encoding="utf-8").read()
_req = open(os.path.join(BASE_DIR, "requirements.txt"), encoding="utf-8").read()

# ── 1. 限速绕过（X-Forwarded-For 可伪造）───────────────────────────
# 实测：伪造 XFF + 轮换用户名 → 连续 60 次爆破零限速（修复后第 21 次被拦）
check("★ ★ 只在直连来源可信时才采信 XFF", "_ip_in_trusted(peer)" in _app)
check("★ ★ 有受信任代理网段配置（默认本机+私有网段）",
      "GCPWEB_TRUSTED_PROXIES" in _app and "127.0.0.0/8" in _app)
check("★ 不可信来源的 XFF 被忽略（用直连 IP）",
      "return peer or \"-\"" in _app)

# ── 2. must_change_password 服务端强制 ────────────────────────────
check("★ ★ 未改密前拦截其它接口（不只是前端提示）",
      "must_change_password" in _app and "allowed_when_must_change" in _app)
_mc_block = ("/api/auth/change_password" in _app and "'/api/auth/logout'" in _app
             or '"/api/auth/logout"' in _app)
check("★ 改密/登出/查自己仍放行（不会把自己锁死）", _mc_block)

# ── 3. 会话凭据不再回传响应体 ─────────────────────────────────────
check("★ ★ 登录响应不再回传 token（只走 HttpOnly Cookie）",
      '"token": token' not in _app and '"token": new_token' not in _app)
check("★ 会话列表不再回传当前 token", '"current_token": cur' not in _app)
check("★ Cookie 带 secure 标记（HTTPS 下）",
      "_cookie_secure(request)" in _app and "secure=_secure" in _app)

# ── 4. 安全响应头 ─────────────────────────────────────────────────
check("★ ★ 有 CSP 响应头", "Content-Security-Policy" in _app and "frame-ancestors 'none'" in _app)
check("★ 有 X-Frame-Options / nosniff / Referrer-Policy",
      "X-Frame-Options" in _app and "X-Content-Type-Options" in _app
      and "Referrer-Policy" in _app)

# ── 5. 输入上限（DoS / 费用灾难）──────────────────────────────────
check("★ ★ count 有上限（防误建海量实例烧钱）",
      "le=200" in _app and "Field(default=1, ge=1, le=200)" in _app)
check("★ audit/tasks/logs 的 limit 有上限",
      "Query(200, ge=1, le=1000)" in _app and "Query(500, ge=1, le=5000)" in _app)
check("★ 用户名校验在服务端做（前端正则可绕过）",
      "用户名只能包含字母、数字、下划线、点、横线或 @" in
      open(os.path.join(BASE_DIR, "core", "users.py"), encoding="utf-8").read())

# ── 6. 认证与并发 ─────────────────────────────────────────────────
check("★ ★ SSH 不再静默信任任何主机密钥（原 AutoAddPolicy）",
      "AutoAddPolicy" not in _ssh and "_tofu_policy_cls" in _ssh)
check("★ ★ 验证码用加密安全随机源",
      "secrets.choice(_CAPTCHA_ALPHABET)" in _auth
      and "random.choice(_CAPTCHA_ALPHABET)" not in _auth)
check("★ 验证码清理持锁（原为数据竞争）",
      "_gc" in _auth and "with self._lock:" in _auth.split("def _gc")[1][:400])
check("★ 登录限速表有容量上限（原为无界增长）",
      "MAX_TRACKED" in _auth and "_prune" in _auth)
check("★ 缓存 _INSPECT_CACHE 有锁（原为并发裸字典）",
      "_INSPECT_LOCK" in _app)

# ── 7. 部署加固 ───────────────────────────────────────────────────
check("★ ★ systemd 服务降权（不再以 root 运行）", "User=$SVC_USER" in _inst)
check("★ 服务用户无登录 shell", "--shell /usr/sbin/nologin" in _inst)
check("★ 应用默认绑回环而非 0.0.0.0",
      'os.environ.get("HOST") or "127.0.0.1"' in _app)
check("★ python-multipart 版本下限（CVE-2024-53981 DoS）",
      "python-multipart>=0.0.18" in _req)
check("★ 依赖全部锁了上限区间（防漂移）",
      all(("<" in line) for line in _req.splitlines()
          if line.strip() and not line.startswith("#")))

# ── 8. 端到端验证：强制改密真的拦得住 ─────────────────────────────
client.post("/api/auth/logout")
login("admin", admin_pw)
_r = client.post("/api/users", json={"username": "mc_test", "password": "Init@GCP2026",
                                     "role": "operator", "must_change": True})
check("创建待改密账号", _r.status_code == 200, _r.text[:120])
switch("mc_test", "Init@GCP2026")
_r = client.get("/api/status")
check("★ ★ 未改密时 /api/status 被拦（403）", _r.status_code == 403, str(_r.status_code))
_r = client.get("/api/accounts")
check("★ ★ 未改密时 /api/accounts 被拦（403）", _r.status_code == 403, str(_r.status_code))
_r = client.post("/api/create", json={"count": 1, "dry_run": True})
check("★ ★ 未改密时 /api/create 被拦（403）", _r.status_code == 403, str(_r.status_code))
_r = client.get("/api/auth/me")
check("★ 未改密时查自己仍可用（前端才好弹改密页）", _r.status_code == 200, str(_r.status_code))
_r = client.post("/api/auth/change_password",
                 json={"old_password": "Init@GCP2026", "new_password": "NewPw@GCP2026"})
check("★ 改密本身放行", _r.status_code == 200, _r.text[:120])
_r = client.get("/api/status")
check("★ 改密后恢复正常访问", _r.status_code == 200, str(_r.status_code))

print("\\n" + "=" * 76)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")'''

s = s.replace(ANCHOR, NEW, 1)
open(P, "w", encoding="utf-8").write(s)
print(f"已追加 S 段，tests_e2e.py {len(s)} B")