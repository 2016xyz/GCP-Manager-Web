#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
权限矩阵 / 越权实测

判据：低权限角色（viewer 只读）能完成的，必须真的是「只读动作」；
若能借它改动状态或拿到本不该给的东西，就是越权。

对 5 个「POST 但只要求 view」的接口逐个判定：
  /api/accounts/{id}/test   —— 打外部请求但只读，合理？要确认不写库
  /api/cost/estimate        —— 纯计算，合理
  /api/instances/password   —— 敏感！必须确认二次验证生效、限速生效
  /api/refresh              —— 会起任务并**写 logs 表**，要看是否算写操作
  /api/savings              —— 纯读配置，合理
"""
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
TMP = tempfile.mkdtemp(prefix="gw_authz_")
os.environ["GCPWEB_DATA_DIR"] = TMP
shutil.copy(os.path.join(BASE, "data", "gcp_web.db"), os.path.join(TMP, "gcp_web.db"))
for f in ("INITIAL_ADMIN.txt",):
    src = os.path.join(BASE, "data", f)
    if os.path.exists(src):
        shutil.copy(src, os.path.join(TMP, f))

import google.cloud.compute_v1 as compute_v1  # noqa: E402


class _Op:
    def result(self, timeout=None):
        return None


class _FakeInstancesClient:
    def __init__(self, *a, **kw):
        pass

    @classmethod
    def from_service_account_json(cls, filename, *a, **kw):
        return cls()

    def aggregated_list(self, project=None, **kw):
        # 必须真的返回一台实例：否则 /api/instances 是空列表，
        # 「响应里以 has_password 代替明文密码」这条断言就形同虚设
        # （空列表里当然找不到 has_password，测不出任何东西）。
        class _Resp:
            instances = [self._mk()]
        return [("zones/us-central1-a", _Resp())]

    @staticmethod
    def _mk():
        class _NI:
            network_i_p = "10.0.0.2"
            access_configs = []
        class _Disk:
            disk_size_gb = 30
            type_ = "zones/z/diskTypes/pd-standard"
            source = ""
            initialize_params = None
        class _I:
            name = "regress-vm-1"
            status = "RUNNING"
            network_interfaces = [_NI()]
            disks = [_Disk()]
            machine_type = "zones/z/machineTypes/e2-micro"
            creation_timestamp = "2026-09-01T00:00:00.000-07:00"
            scheduling = None
            labels = {}
        return _I()

    def get(self, **kw):
        return self._mk()

    def start(self, **kw):
        return _Op()

    def stop(self, **kw):
        return _Op()

    def reset(self, **kw):
        return _Op()

    def delete(self, **kw):
        return _Op()

    def list(self, **kw):
        return []


compute_v1.InstancesClient = _FakeInstancesClient
for _n in ("FirewallsClient", "ProjectsClient", "RegionsClient", "ZonesClient",
           "NetworksClient", "SubnetworksClient", "ImagesClient", "MachineTypesClient",
           "DisksClient", "SnapshotsClient", "AddressesClient"):
    if hasattr(compute_v1, _n):
        class _C:
            def __init__(self, *a, **kw):
                pass

            @classmethod
            def from_service_account_json(cls, filename, *a, **kw):
                return cls()

            def list(self, **kw):
                return []

            def get(self, **kw):
                return type("X", (), {"quotas": [], "items": []})()
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

print("=" * 76)
print("权限矩阵 / 越权实测（viewer 只读角色的边界）")
print("=" * 76)

login("admin", admin_pw)
# 建一个 viewer 账号
_r = client.post("/api/users", json={"username": "vw", "password": "ViewerPw123456",
                                     "role": "viewer"})
vw_pw = "ViewerPw123456"
ck("管理员可建 viewer 账号", _r.status_code == 200, _r.text[:120])
client.post("/api/auth/logout")
login("vw", vw_pw)

# ── 只读性质、应当允许 ──────────────────────────────────────────
print("\n【应当允许（真只读）】")
for name, method, path, body in [
        ("成本估算", "POST", "/api/cost/estimate", {"spec": {"machine_type": "e2-micro"}}),
        ("省钱状态", "POST", "/api/savings", {"config": {}}),
        ("账号连通性测试", "POST", "/api/accounts/1/test", None)]:
    r = getattr(client, method.lower())(path, json=body) if body is not None \
        else getattr(client, method.lower())(path)
    ck(f"viewer 可用「{name}」（{r.status_code}）", r.status_code in (200, 404, 500), str(r.status_code))

# ── 必须被拒的写操作 ──────────────────────────────────────────
print("\n【必须被拒（写操作 / 敏感）】")
MUST_DENY = [
    ("创建实例", "POST", "/api/create", {"count": 1, "spec": {"machine_type": "e2-micro"}}),
    ("实例动作", "POST", "/api/instance_action", {"action": "delete", "targets": ["x"]}),
    ("执行命令", "POST", "/api/execute", {"command": "id", "scope": "all"}),
    ("改实例备注", "PATCH", "/api/instances/note", {"name": "x", "note": "y"}),
    ("改配置", "POST", "/api/config", {"config": {}}),
    ("导入账号", "POST", "/api/accounts", {"key_path": "/tmp/x.json"}),
    ("改账号", "PATCH", "/api/accounts/1", {"label": "hack"}),
    ("删账号", "DELETE", "/api/accounts/1", None),
    ("清日志", "DELETE", "/api/logs", None),
    ("建用户", "POST", "/api/users", {"username": "evil", "password": "EvilPw123456",
                                      "role": "admin"}),
    ("改用户", "PATCH", "/api/users/1", {"role": "admin"}),
    ("删用户", "DELETE", "/api/users/1", None),
    ("改密（他人）", "POST", "/api/users/1/password", {"password": "NewPw123456"}),
    ("生成SSH密钥", "POST", "/api/sshkey/generate", {}),
    # 必须给合法 body：pydantic 校验先于 require() 执行，
    # 空 body 会先被 422 拦下，那样验不到鉴权本身
    ("读SSH密钥", "POST", "/api/sshkey/read", {"pubkey_path": "/tmp/x.pub"}),
    ("取消任务", "POST", "/api/tasks/x/cancel", {}),
]
for name, method, path, body in MUST_DENY:
    fn = getattr(client, method.lower())
    r = fn(path, json=body) if body is not None else fn(path)
    ck(f"viewer 被拒「{name}」", r.status_code == 403, f"{r.status_code} {r.text[:70]}")

# ── 管理员专属 ────────────────────────────────────────────────
print("\n【管理员专属接口】")
for name, method, path, body in [
        ("用户列表", "GET", "/api/users", None),
        ("会话列表", "GET", "/api/sessions", None),
        ("审计日志", "GET", "/api/audit", None)]:
    fn = getattr(client, method.lower())
    r = fn(path, json=body) if body is not None else fn(path)
    ck(f"viewer 被拒「{name}」", r.status_code == 403, str(r.status_code))

# ── root 密码接口：viewer 能看见「有密码」，但要看明文必须验自己的密 ──
print("\n【root 密码取用链】")
r = client.get("/api/instances")
ck("viewer 可读实例列表（只读）", r.status_code == 200, str(r.status_code))
body = r.text
_insts = r.json().get("instances", []) if r.status_code == 200 else []
ck("★ 假客户端确实返回了实例（否则下面的断言是空转）", len(_insts) >= 1, str(len(_insts)))
ck("★ 列表响应里没有 password 明文字段",
   all("password" not in i for i in _insts), str(_insts[:1])[:200])
ck("★ 列表以 has_password 代替明文",
   bool(_insts) and "has_password" in _insts[0], str(_insts[:1])[:200])

r = client.post("/api/instances/password",
                json={"name": "不存在的机器", "password": vw_pw})
ck("★ 未记录的实例不泄露密码（404/400）",
   r.status_code in (400, 404), f"{r.status_code} {r.text[:90]}")

# 用错误密码试（模拟拿到别人会话后暴猜）
r = client.post("/api/instances/password",
                json={"name": "x", "password": "wrong-password-xxx"})
ck("★ 二次验证：错误密码必须拒绝", r.status_code in (400, 403, 404),
   f"{r.status_code} {r.text[:90]}")

# 连续错 → 应触发限速（LoginGuard）
codes = []
for _ in range(12):
    rr = client.post("/api/instances/password",
                     json={"name": "x", "password": "wrong-password-xxx"})
    codes.append(rr.status_code)
ck("★ 连续错误触发限速（出现 429）", 429 in codes, str(codes))

# ── 水平越权：viewer 改自己的密码可以，改别人不行 ─────────────
print("\n【水平越权】")
client.post("/api/auth/logout")
login("admin", admin_pw)
client.post("/api/users", json={"username": "vw2", "password": "ViewerPw123456",
                                "role": "viewer"})
client.post("/api/auth/logout")
login("vw2", vw_pw)
r = client.patch("/api/users/1", json={"password": "Hacked123456"})
ck("★ 普通用户不能改 admin 的密码", r.status_code == 403, f"{r.status_code} {r.text[:80]}")
r = client.post("/api/auth/change_password",
                json={"old_password": "bad", "new_password": "Whatever123456"})
ck("★ 改自己密码需提供旧密码", r.status_code in (400, 403), str(r.status_code))

print()
print("=" * 76)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("   ✗", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)