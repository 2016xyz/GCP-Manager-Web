#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
/api/sshkey/read 任意文件读取 —— PoC 与修复验证

背景：该接口原本把用户给的路径直接 open() 返回，等于给「operate」权限
（operator 角色，非管理员）开了任意文件读取。实测可读出本工具自己生成的
管理员初始密码文件 data/INITIAL_ADMIN.txt，即一条 operator → admin 的提权路径。

本脚本对**修复后的**代码做验证：
  A. 正常功能仍可用（读服务器上真实存在的 .pub）
  B. 敏感文件读取必须被拒（含绕过尝试）
  C. 私钥即使位于白名单目录也必须被拒
全局只读，不写任何文件。
"""
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
TMP = tempfile.mkdtemp(prefix="gw_poc_")
os.environ["GCPWEB_DATA_DIR"] = TMP
for f in ("gcp_web.db", "INITIAL_ADMIN.txt"):
    s = os.path.join(BASE, "data", f)
    if os.path.exists(s):
        shutil.copy(s, os.path.join(TMP, f))

import google.cloud.compute_v1 as compute_v1  # noqa: E402


class _C:
    def __init__(self, *a, **kw):
        pass

    @classmethod
    def from_service_account_json(cls, filename, *a, **kw):
        return cls()

    def list(self, **kw):
        return []

    def aggregated_list(self, project=None, **kw):
        return []

    def get(self, **kw):
        return type("X", (), {"quotas": [], "items": []})()


for _n in dir(compute_v1):
    if _n.endswith("Client"):
        setattr(compute_v1, _n, _C)

from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
from core import auth as auth_mod  # noqa: E402

client = TestClient(appmod.app)
PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   [{detail}]" if detail and not cond else ""))


def login(u, p):
    c = client.get("/api/auth/captcha").json()
    code = auth_mod.captcha_store._items.get(c["captcha_id"], {}).get("code")
    return client.post("/api/auth/login", json={
        "username": u, "password": p,
        "captcha_id": c["captcha_id"], "captcha_code": code})


admin_pw = ""
for line in open(os.path.join(TMP, "INITIAL_ADMIN.txt"), encoding="utf-8"):
    if line.startswith("密码:"):
        admin_pw = line.split(":", 1)[1].strip()
PWD = "OperatorPw123456"

print("=" * 76)
print("/api/sshkey/read 任意文件读取 —— 修复验证")
print("=" * 76)

login("admin", admin_pw)
_r = client.post("/api/users", json={"username": "op1", "password": PWD, "role": "operator"})
ck("准备：创建 operator 账号", _r.status_code == 200, _r.text[:80])
client.post("/api/auth/logout")
ck("准备：operator 登录", login("op1", PWD).status_code == 200)

# ── 准备一个真实公钥（放进白名单目录，验证正常功能）────────────
from core.store import Store  # noqa: E402
kdir = os.path.join(TMP, "ssh_keys")
os.makedirs(kdir, exist_ok=True)
good_pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGoodKeyForTestingOnlyTESTKEY123 gcp-manager-web"
good_path = os.path.join(kdir, "gcp_key_test.pub")
with open(good_path, "w", encoding="utf-8") as f:
    f.write(good_pub + "\n")
# 同目录放一个「私钥」模拟物，验证私钥被拒
priv_path = os.path.join(kdir, "gcp_key_test")
with open(priv_path, "w", encoding="utf-8") as f:
    f.write("-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n")

print("\n【A. 正常功能仍可用（不能把功能改坏）】")
r = client.post("/api/sshkey/read", json={"pubkey_path": good_path})
ck("★ 可读取白名单目录内的 .pub", r.status_code == 200 and r.json().get("ok"),
   f"{r.status_code} {r.text[:100]}")
if r.status_code == 200:
    ck("★ 返回内容正确", r.json()["public_key"].startswith("ssh-ed25519"),
       r.json().get("public_key", "")[:40])

# 服务器上真实存在的公钥文件（前端默认值就是它）
home_pub = os.path.expanduser("~/.ssh/id_rsa.pub")
if os.path.exists(home_pub):
    r = client.post("/api/sshkey/read", json={"pubkey_path": home_pub})
    ck("★ 仍可读取服务器上已有的公钥（前端默认路径）",
       r.status_code == 200, f"{r.status_code} {r.text[:100]}")
else:
    print("  · 跳过（本机无 ~/.ssh/id_rsa.pub）")

print("\n【B. 敏感文件必须被拒】")
DENY = [
    ("/etc/passwd", "系统账户文件"),
    ("/etc/hostname", "系统文件"),
    (os.path.join(TMP, "INITIAL_ADMIN.txt"), "★ 管理员初始密码"),
    (os.path.join(TMP, "gcp_web.db"), "★ 数据库（含 root 密码）"),
    (os.path.join(BASE, "app.py"), "应用源码"),
    (os.path.join(BASE, ".git", "config"), "git 配置"),
    (os.path.join(kdir, "..", "..", "INITIAL_ADMIN.txt"), "目录穿越到密码文件"),
    ("/etc/../etc/passwd", "穿越写法"),
]
for path, why in DENY:
    try:
        r = client.post("/api/sshkey/read", json={"pubkey_path": path})
        code = r.status_code
        detail = r.json().get("detail", "") if code != 200 else ""
    except Exception as exc:
        code, detail = "异常", f"{type(exc).__name__}: {exc}"
    ck(f"★ 拒绝读取{why}", code == 400, f"{code} {detail[:90]}")

print("\n【C. 私钥不得外发（即使位于白名单目录）】")
r = client.post("/api/sshkey/read", json={"pubkey_path": priv_path})
ck("★ 拒读同目录下的私钥文件", r.status_code == 400, f"{r.status_code} {r.text[:90]}")
for nm in ("id_rsa", "id_ed25519", "server.pem", "deploy.key"):
    p = os.path.join(kdir, nm)
    with open(p, "w", encoding="utf-8") as f:
        f.write("ssh-rsa AAAAB3 faked-for-test\n")
    r = client.post("/api/sshkey/read", json={"pubkey_path": p})
    ck(f"★ 按文件名拒私钥：{nm}", r.status_code == 400, str(r.status_code))

print("\n【D. 内容伪装为公钥的敏感文件也要被挡】")
# 把密码文件的「内容」改成公钥样式，但路径在 data/ 根部（不在白名单子目录）
fake = os.path.join(TMP, "fake.pub")
with open(fake, "w", encoding="utf-8") as f:
    f.write("ssh-rsa AAAAB3NzaFakeKeyForTestingOnlyAAAA gcp-manager-web\n")
r = client.post("/api/sshkey/read", json={"pubkey_path": fake})
ck("★ 拒读 data/ 根部文件（即使内容是公钥样式）", r.status_code == 400,
   f"{r.status_code} {r.text[:90]}")

print("\n【E. 越权：无 operate 权限不得调用】")
client.post("/api/auth/logout")
login("admin", admin_pw)
client.post("/api/users", json={"username": "vw9", "password": "ViewerPw123456",
                                "role": "viewer"})
client.post("/api/auth/logout")
login("vw9", "ViewerPw123456")
r = client.post("/api/sshkey/read", json={"pubkey_path": good_path})
ck("★ viewer 被拒（403）", r.status_code == 403, str(r.status_code))

print("\n【F. 拒绝行为要留审计】")
client.post("/api/auth/logout")
login("admin", admin_pw)
aud = client.get("/api/audit?limit=200").json().get("audit", [])
ck("★ 存在 read_sshkey_denied 审计记录",
   any(x.get("action") == "read_sshkey_denied" for x in aud),
   str([x.get("action") for x in aud][:12]))

print()
print("=" * 76)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("   ✗", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)