# -*- coding: utf-8 -*-
"""
端到端验证（不需要真实 GCP 账号）

覆盖：
  A. 认证：验证码、登录、错误密码、会话、强制改密、限速
  B. 用户管理：创建/改角色/禁用/重置密码/删除/自我保护
  C. 权限：admin / operator / viewer 三角色越权拦截
  D. 自定义服务器配置穿透到 compute.instances.insert
  E. 区域模式、成本估算、dry-run、实例动作、命令执行
  F. 页面与静态资源

用法： python3 tests_e2e.py
"""
import json
import os
import sqlite3
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

DATA_DIR = os.path.join(BASE_DIR, "data")
KEY_DIR = os.path.join(DATA_DIR, "keys")
os.makedirs(KEY_DIR, exist_ok=True)

# 每次从干净库开始，保证可重复
DB_PATH = os.path.join(DATA_DIR, "gcp_web.db")
for p in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm",
          os.path.join(DATA_DIR, "INITIAL_ADMIN.txt")):
    if os.path.exists(p):
        os.remove(p)

FAKE_KEY = os.path.join(KEY_DIR, "fake-sa.json")
with open(FAKE_KEY, "w") as f:
    json.dump({"type": "service_account", "project_id": "fake-project-123",
               "private_key_id": "deadbeef",
               "private_key": "-----BEGIN PRIVATE KEY-----\nMIIfake\n-----END PRIVATE KEY-----\n",
               "client_email": "fake-sa@fake-project-123.iam.gserviceaccount.com",
               "client_id": "1234567890"}, f, indent=2)

# ---------- Monkeypatch Google 客户端 ----------
import google.cloud.compute_v1 as compute_v1  # noqa: E402

CAPTURED = []
FW_CALLS = []
SSH_CALLS = []


class _Op:
    def result(self, timeout=None):
        return None


class _FakeInstancesClient:
    @classmethod
    def from_service_account_json(cls, filename, *a, **kw):
        if not os.path.exists(filename):
            raise FileNotFoundError(filename)
        return cls()

    def insert(self, project=None, zone=None, instance_resource=None, **kw):
        CAPTURED.append({"project": project, "zone": zone, "instance": instance_resource})
        return _Op()

    def get(self, project=None, zone=None, instance=None, **kw):
        class _NI:
            network_i_p = "10.0.0.2"
            access_configs = [type("A", (), {"nat_i_p": "203.0.113.10"})()]
        class _I:
            name = instance
            zone = zone
            status = "RUNNING"
            creation_timestamp = "2026-09-24T00:00:00.000-07:00"
            machine_type = f"zones/{zone}/machineTypes/e2-micro"
            network_interfaces = [_NI()]
        return _I()

    def aggregated_list(self, project=None, **kw):
        return []

    def _noop(self, *a, **kw):
        return _Op()

    start = stop = reset = delete = _noop


class _FakeFirewallsClient:
    @classmethod
    def from_service_account_json(cls, filename, *a, **kw):
        return cls()

    def insert(self, **kw):
        FW_CALLS.append(kw)
        return _Op()

    def update(self, **kw):
        FW_CALLS.append(kw)
        return _Op()


class _FakeProjectsClient:
    @classmethod
    def from_service_account_json(cls, filename, *a, **kw):
        return cls()

    def get(self, project=None, **kw):
        class _Meta:
            items = []
        class _P:
            common_instance_metadata = _Meta()
        return _P()

    def set_common_instance_metadata(self, **kw):
        return _Op()


class _FakeRegionsClient:
    @classmethod
    def from_service_account_json(cls, filename, *a, **kw):
        return cls()

    def list(self, project=None, **kw):
        return []


compute_v1.InstancesClient = _FakeInstancesClient
compute_v1.FirewallsClient = _FakeFirewallsClient
compute_v1.ProjectsClient = _FakeProjectsClient
compute_v1.RegionsClient = _FakeRegionsClient

from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
from core import auth as auth_mod          # noqa: E402
from core import catalog as catalog_mod    # noqa: E402

client = TestClient(appmod.app)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  → {detail}" if detail and not cond else ""))


# 危险默认值必须在任何创建动作发生前就是安全的（创建会写回默认配置）
print("=" * 76)
print("GCP Manager Web · 端到端验证（含鉴权）")
print("=" * 76)
print("\n── 前置：危险默认值 ──")
check("★ DEFAULT_CONFIG 中全开放防火墙为 False",
      catalog_mod.DEFAULT_CONFIG["auto_open_firewall"] is False,
      str(catalog_mod.DEFAULT_CONFIG["auto_open_firewall"]))
check("DEFAULT_CONFIG 与 README 一致（e2-micro + Ubuntu Minimal 22.04 + 30GB）",
      catalog_mod.DEFAULT_CONFIG["machine_type"] == "e2-micro" and
      catalog_mod.DEFAULT_CONFIG["image_key"] == "ubuntu-minimal-2204" and
      catalog_mod.DEFAULT_CONFIG["disk_size_gb"] == 30,
      str(catalog_mod.DEFAULT_CONFIG))
check("DEFAULT_CONFIG 中标签默认两枚",
      catalog_mod.DEFAULT_CONFIG["tags"] == ["http-server", "https-server"],
      str(catalog_mod.DEFAULT_CONFIG["tags"]))


def login(username, password):
    """完成一次登录；验证码通过刷新接口后从内存池里直接取值"""
    c = client.get("/api/auth/captcha").json()
    code = auth_mod.captcha_store._items.get(c["captcha_id"], {}).get("code")
    return client.post("/api/auth/login", json={
        "username": username, "password": password,
        "captcha_id": c["captcha_id"], "captcha_code": code})


def as_user(username, password, role_label=""):
    """返回一个绑定该用户 cookie 的独立 TestClient（等价于换个浏览器）"""
    login(username, password)
    return client


print("=" * 76)
print("GCP Manager Web · 端到端验证（含鉴权）")
print("=" * 76)

# ═══════════════ A. 认证 ═══════════════
print("\n── A. 认证与登录 ──")
# 首次启动已 bootstrap 出 admin
init_file = os.path.join(DATA_DIR, "INITIAL_ADMIN.txt")
check("首次启动生成初始管理员文件", os.path.exists(init_file))
admin_pw = ""
if os.path.exists(init_file):
    for line in open(init_file, encoding="utf-8"):
        if line.startswith("密码:"):
            admin_pw = line.split(":", 1)[1].strip()
check("初始密码可读出且长度≥14", len(admin_pw) >= 14, f"len={len(admin_pw)}")
check("初始文件权限为 600",
      oct(os.stat(init_file).st_mode)[-3:] == "600",
      oct(os.stat(init_file).st_mode)[-3:])

# 未登录访问拦截
r = client.get("/api/status")
check("未登录访问 /api/status → 401", r.status_code == 401, str(r.status_code))
r = client.get("/", follow_redirects=False)
check("未登录访问 / → 302 跳登录页", r.status_code == 302 and "/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location')}")
r = client.get("/login")
check("登录页可匿名访问", r.status_code == 200 and "验证码" in r.text)
r = client.get("/static/vendor/vue.global.prod.js")
check("静态资源可匿名访问（Vue 本地托管）", r.status_code == 200 and "vue v3" in r.text[:80],
      str(r.status_code))

# 验证码接口
cap = client.get("/api/auth/captcha").json()
check("验证码接口返回 id + 图片",
      cap["ok"] and cap["captcha_id"] and cap["image"].startswith("data:image/"),
      cap.get("image", "")[:40])
check("验证码图片非空且包含干扰",
      len(cap.get("image", "")) > 500, f"{len(cap.get('image',''))} chars")

# 验证码必须真的画出字符（回归：曾因字体探测失败渲染成空框）
import base64 as _b64  # noqa: E402
def _cap_ink(data_uri):
    png = _b64.b64decode(data_uri.split(",", 1)[1])
    return auth_mod.captcha_store.ink_ratio(png)

inks = [_cap_ink(client.get("/api/auth/captcha").json()["image"]) for _ in range(6)]
check("★ 验证码真的渲染出字符（墨水占比 > 2%）",
      all(x > 0.02 for x in inks),
      f"占比={[round(x*100,2) for x in inks]}")
check("★ 验证码可读性稳定（6 张占比均 > 8%）",
      all(x > 0.08 for x in inks), f"占比={[round(x*100,2) for x in inks]}")
fonts_ok = auth_mod.captcha_store._find_font(30)
check("字体解析命中真实 TTF（非内置位图兜底）",
      hasattr(fonts_ok, "path") and os.path.exists(getattr(fonts_ok, "path", "")),
      str(getattr(fonts_ok, "path", type(fonts_ok).__name__)))

# 验证码错误 → 拒绝
bad_code = "ZZZZ"
r = client.post("/api/auth/login", json={"username": "admin", "password": admin_pw,
                                         "captcha_id": cap["captcha_id"], "captcha_code": bad_code})
check("验证码错误 → 400 且拒绝登录", r.status_code == 400 and "验证码" in r.json().get("detail", ""),
      str(r.status_code) + r.text[:100])

# 验证码一次性：同一个 id 再提交，即使这次填对也无效
cap2 = client.get("/api/auth/captcha").json()
right = auth_mod.captcha_store._items.get(cap2["captcha_id"], {}).get("code")
client.post("/api/auth/login", json={"username": "admin", "password": "wrong",
                                     "captcha_id": cap2["captcha_id"], "captcha_code": right})
r = client.post("/api/auth/login", json={"username": "admin", "password": admin_pw,
                                         "captcha_id": cap2["captcha_id"], "captcha_code": right})
check("验证码一次性（不可重放）", r.status_code == 400, str(r.status_code) + r.text[:80])

# 密码错误
cap3 = client.get("/api/auth/captcha").json()
r = client.post("/api/auth/login", json={"username": "admin", "password": "definitely-wrong",
                                         "captcha_id": cap3["captcha_id"],
                                         "captcha_code": auth_mod.captcha_store._items[cap3["captcha_id"]]["code"]})
check("密码错误 → 401 且不泄露用户是否存在",
      r.status_code == 401 and r.json().get("detail") == "用户名或密码错误", r.text[:120])

# 正确登录
r = login("admin", admin_pw)
check("正确凭据 + 正确验证码 → 登录成功", r.status_code == 200 and r.json()["ok"], r.text[:160])
login_data = r.json() if r.status_code == 200 else {}
check("登录返回角色与权限集",
      login_data.get("user", {}).get("role") == "admin" and
      set(login_data.get("permissions", [])) == {"view", "operate", "account", "settings", "user"},
      str(login_data.get("permissions")))
check("会话 cookie 为 HttpOnly",
      "httponly" in str(r.headers.get("set-cookie", "")).lower(),
      r.headers.get("set-cookie", "")[:120])
check("首次登录标记 must_change_password",
      login_data.get("user", {}).get("must_change_password") is True, str(login_data.get("user")))

# 强制改密：改密前可以被 get_session 通过，但 /api/auth/me 会暴露标记
r = client.get("/api/auth/me").json()
check("GET /api/auth/me 返回当前身份", r["authenticated"] and r["user"]["username"] == "admin", str(r)[:140])

# 改密（第一次用错误原密码）
r = client.post("/api/auth/change_password", json={"old_password": "nope", "new_password": "NewPass@2026!"})
check("改密时原密码错误 → 400", r.status_code == 400 and "原密码" in r.json().get("detail", ""), r.text[:100])

# 弱密码被拒
r = client.post("/api/auth/change_password", json={"old_password": admin_pw, "new_password": "short"})
check("新密码短于 8 位 → 400", r.status_code == 400, r.text[:100])

ADMIN_PW = "Adm1n@GCP2026!"
r = client.post("/api/auth/change_password", json={"old_password": admin_pw, "new_password": ADMIN_PW})
check("改密成功并换发新会话", r.status_code == 200 and r.json()["ok"], r.text[:140])
r = client.get("/api/auth/me").json()
check("改密后 must_change_password 清零", r["user"]["must_change_password"] is False, str(r["user"]))

# 登录限速：连续失败触发锁定
locked = False
for i in range(12):
    cap_x = client.get("/api/auth/captcha").json()
    cx = auth_mod.captcha_store._items[cap_x["captcha_id"]]["code"]
    rr = client.post("/api/auth/login", json={"username": "nobody", "password": "x",
                                              "captcha_id": cap_x["captcha_id"], "captcha_code": cx})
    if rr.status_code == 429:
        locked = True
        break
check("连续失败触发账号锁定（429）", locked, f"第 {i+1} 次")

# ═══════════════ B. 用户管理 ═══════════════
print("\n── B. 用户管理 ──")
ADMIN = "admin"


def switch(u, p):
    """切换当前 cookie 身份：先登出再登录"""
    client.post("/api/auth/logout")
    r = login(u, p)
    assert r.status_code == 200, f"登录失败 {u}: {r.text[:120]}"
    return r.json()


r = client.post("/api/users", json={"username": "op1", "password": "Op1@GCP2026!",
                                    "role": "operator", "display_name": "运维一号"})
check("创建 operator 用户", r.status_code == 200 and r.json()["ok"], r.text[:140])
r = client.post("/api/users", json={"username": "op1", "password": "Xx1!aaaa"})
check("重复用户名 → 400", r.status_code == 400 and "已存在" in r.json().get("detail", ""), r.text[:120])
r = client.post("/api/users", json={"username": "view1", "role": "viewer"})
check("留空密码自动生成强密码", r.status_code == 200 and r.json().get("generated") and
      len(r.json().get("password", "")) >= 12, r.text[:160])
VIEW_PW = r.json()["password"]
r = client.post("/api/users", json={"username": "bad", "password": "12345678", "role": "root"})
check("非法角色被拒", r.status_code == 400, r.text[:100])
r = client.post("/api/users", json={"username": "weak", "password": "123", "role": "viewer"})
check("密码过短被拒", r.status_code == 400, r.text[:100])

r = client.get("/api/users").json()
check("用户列表不回传 password_hash / salt",
      all("password_hash" not in u and "salt" not in u for u in r["users"]), str(r["users"][:1]))
check("用户列表包含 3 个角色选项", len(r.get("roles", [])) == 3)

users = {u["username"]: u for u in r["users"]}
uid_op = users["op1"]["id"]
uid_view = users["view1"]["id"]
uid_admin = users[ADMIN]["id"]

# 自我保护
r = client.patch(f"/api/users/{uid_admin}", json={"role": "viewer"})
check("管理员不能修改自己的角色", r.status_code == 400, r.text[:100])
r = client.patch(f"/api/users/{uid_admin}", json={"disabled": True})
check("管理员不能禁用自己", r.status_code == 400, r.text[:100])
r = client.delete(f"/api/users/{uid_admin}")
check("管理员不能删除自己", r.status_code == 400, r.text[:100])

# 重置密码
r = client.post(f"/api/users/{uid_view}/password", json={})
check("重置密码返回新密码", r.status_code == 200 and r.json().get("generated"), r.text[:120])
VIEW_PW = r.json()["password"]

# 审计里应记录
r = client.get("/api/audit?limit=100").json()
actions = {a["action"] for a in r["audit"]}
check("审计记录覆盖关键操作",
      {"login", "change_password", "create_user", "reset_password"} <= actions,
      str(sorted(actions)))

# 在线会话
r = client.get("/api/sessions").json()
check("会话列表不回传完整 token",
      r["ok"] and all("token" not in s for s in r["sessions"]), str(r["sessions"][:1]))

# ═══════════════ C. 权限矩阵 ═══════════════
print("\n── C. 角色权限拦截 ──")

# 导入一个账号（需要 account 权限，admin 有）
r = client.post("/api/accounts", json={"key_path": FAKE_KEY}).json()
check("admin 可导入账号", r["ok"], str(r)[:120])
ACC_ID = r["account_id"]

# ---- operator ----
switch("op1", "Op1@GCP2026!")
r = client.get("/api/status")
check("[operator] 可查看状态", r.status_code == 200)
r = client.get("/api/catalog")
check("[operator] 可读目录", r.status_code == 200)
r = client.get("/api/users")
check("[operator] 访问用户管理 → 403", r.status_code == 403, str(r.status_code) + r.text[:90])
r = client.post("/api/users", json={"username": "x", "role": "admin"})
check("[operator] 创建用户 → 403", r.status_code == 403, str(r.status_code))
r = client.post("/api/accounts", json={"key_path": FAKE_KEY})
check("[operator] 可导入账号", r.status_code == 200, str(r.status_code))
r = client.post("/api/create", json={"dry_run": True, "count": 1})
check("[operator] 可发起创建（dry-run）", r.status_code == 200, str(r.status_code) + r.text[:100])

# ---- viewer ----
switch("view1", VIEW_PW)
r = client.get("/api/status")
check("[viewer] 可查看状态", r.status_code == 200)
r = client.get("/api/instances?sync=false")
check("[viewer] 可读实例列表", r.status_code == 200)
r = client.post("/api/accounts", json={"key_path": FAKE_KEY})
check("[viewer] 导入账号 → 403", r.status_code == 403, str(r.status_code) + r.text[:90])
r = client.post("/api/execute", json={"command": "id"})
check("[viewer] 执行命令 → 403", r.status_code == 403, str(r.status_code) + r.text[:90])
r = client.post("/api/instance_action", json={"action": "delete", "targets": ["a"]})
check("[viewer] 删除实例 → 403", r.status_code == 403, str(r.status_code) + r.text[:90])
r = client.post("/api/create", json={"count": 1})
check("[viewer] 创建实例 → 403", r.status_code == 403, str(r.status_code) + r.text[:90])
r = client.delete("/api/logs")
check("[viewer] 清空日志 → 403", r.status_code == 403, str(r.status_code) + r.text[:90])
r = client.post("/api/config", json={"machine_type": "e2-small"})
check("[viewer] 保存配置 → 403", r.status_code == 403, str(r.status_code) + str(r.text)[:90])
r = client.get("/api/users")
check("[viewer] 用户管理 → 403", r.status_code == 403, str(r.status_code))

# ---- 禁用用户后会话立即失效 ----
switch("admin", ADMIN_PW)
client.patch(f"/api/users/{uid_op}", json={"disabled": True})
# 用 op1 的旧 cookie 试试（已登出，这里重新登录应失败）
client.post("/api/auth/logout")
cap_d = client.get("/api/auth/captcha").json()
r = client.post("/api/auth/login", json={"username": "op1", "password": "Op1@GCP2026!",
                                         "captcha_id": cap_d["captcha_id"],
                                         "captcha_code": auth_mod.captcha_store._items[cap_d["captcha_id"]]["code"]})
check("被禁用用户无法登录", r.status_code == 401 and "禁用" in r.json().get("detail", ""), r.text[:120])
client.patch(f"/api/users/{uid_op}", json={"disabled": False})
switch("admin", ADMIN_PW)

# 最后一个管理员保护
r = client.delete(f"/api/users/{uid_admin}")
check("不能删除自己（唯一管理员）", r.status_code == 400, str(r.status_code))

# 用户管理端点需要 user 权限
r = client.get("/api/sessions")
check("admin 可查看会话列表", r.status_code == 200)

# ═══════════════ D. 自定义配置穿透 ═══════════════
print("\n── D. 自定义服务器配置穿透 ──")
CAPTURED.clear()
FW_CALLS.clear()

SPEC = {
    "machine_type": "c3-standard-8", "image_key": "debian-12",
    "disk_type": "pd-balanced", "disk_size_gb": 200,
    "region_mode": "single", "region": "us-east1",
    "network": "default", "subnet": "default", "network_tier": "PREMIUM",
    "tags": ["http-server", "web", "custom-tag"],
    "assign_public_ip": True, "auto_open_firewall": False,
    "disable_ops_agent": True, "preemptible": True,
}
r = client.post("/api/create", json={
    "account_ids": [ACC_ID], "count": 2, "spec": SPEC,
    "login_mode": "root_password", "root_password": "TestPwd@2026",
    "post_command": "echo INSTALL_OK", "concurrency": 2, "retry_count": 0,
    "ssh_timeout": 5, "account_workers": 1}).json()
check("提交创建任务（自定义配置）", r["ok"], str(r)[:140])
TASK_ID = r.get("task_id")
time.sleep(12)

check("实际发出 insert 请求", len(CAPTURED) >= 2, f"captured={len(CAPTURED)}")
if CAPTURED:
    cap = CAPTURED[0]["instance"]
    ip = cap.disks[0].initialize_params
    md = {i.key: i.value for i in cap.metadata.items}
    check("★ 自定义机型 c3-standard-8", cap.machine_type.endswith("c3-standard-8"), cap.machine_type)
    check("★ 自定义镜像 debian-12", ip.source_image.endswith("debian-12"), ip.source_image)
    check("★ 自定义磁盘类型 pd-balanced", ip.disk_type.endswith("pd-balanced"), ip.disk_type)
    check("★ 自定义磁盘容量 200GB", ip.disk_size_gb == 200, str(ip.disk_size_gb))
    check("★ 自定义标签三枚",
          list(cap.tags.items) == ["http-server", "web", "custom-tag"], str(list(cap.tags.items)))
    check("★ 抢占式实例", bool(cap.scheduling.preemptible), str(cap.scheduling))
    check("★ PREMIUM 网络层级",
          cap.network_interfaces[0].access_configs[0].network_tier == "PREMIUM")
    check("★ Root 密码写入 startup-script", "TestPwd@2026" in md.get("startup-script", ""))
    check("★ 禁用 Ops Agent 标记", md.get("google-logging-enabled") == "false")
    check("★ 未开启全开放防火墙时不调用防火墙接口", len(FW_CALLS) == 0, str(len(FW_CALLS)))

CAPTURED.clear()
FW_CALLS.clear()
r = client.post("/api/create", json={
    "account_ids": [ACC_ID], "count": 1,
    "spec": {"machine_type": "e2-small", "image_key": "ubuntu-2404-lts",
             "disk_type": "pd-standard", "disk_size_gb": 20,
             "region_mode": "single", "region": "us-west1", "auto_open_firewall": True},
    "login_mode": "ssh_key", "ssh_public_key": "ssh-rsa AAAAB3NzaC1yc2E test@host",
    "concurrency": 1, "retry_count": 0, "ssh_timeout": 5}).json()
check("提交 SSH 密钥模式任务", r["ok"], str(r)[:140])
time.sleep(8)
if CAPTURED:
    cap = CAPTURED[0]["instance"]
    md = {i.key: i.value for i in cap.metadata.items}
    check("★ SSH 密钥模式不写 startup-script", "startup-script" not in md, str(list(md.keys())))
    check("★ Ubuntu 24.04 镜像",
          cap.disks[0].initialize_params.source_image.endswith("ubuntu-2404-lts-amd64"),
          cap.disks[0].initialize_params.source_image)
    check("★ auto_open_firewall=True 触发防火墙 upsert", len(FW_CALLS) >= 1, str(len(FW_CALLS)))
else:
    check("★ SSH 密钥模式创建成功", False, "未捕获 insert")

# ═══════════════ E. 区域 / 成本 / 任务 ═══════════════
print("\n── E. 区域模式 / 成本 / 任务 ──")
tmm = appmod.tm
pool, _, single = tmm.resolve_region_pool({"region_mode": "auto_free"})
check("区域模式 auto_free", pool == ["us-central1", "us-east1", "us-west1"] and not single, str(pool))
pool, _, _ = tmm.resolve_region_pool({"region_mode": "auto_paid"})
check("区域模式 auto_paid", len(pool) == 39, str(len(pool)))
pool, _, _ = tmm.resolve_region_pool({"region_mode": "custom", "regions": ["asia-east1", "asia-northeast1"]})
check("区域模式 custom", pool == ["asia-east1", "asia-northeast1"], str(pool))
pool, _, single = tmm.resolve_region_pool({"region_mode": "single", "region": "europe-west3"})
check("区域模式 single", pool == ["europe-west3"] and single, str(pool))

r = client.get("/api/catalog?region=us-central1").json()
check("目录机型数 ≥ 30", len(r["machine_types"]) >= 30, str(len(r["machine_types"])))
check("目录含免费机型 e2-micro",
      any(m["name"] == "e2-micro" and m.get("free_tier") for m in r["machine_types"]))
check("目录保留原版默认镜像 ubuntu-minimal-2204-lts",
      any(i["key"] == "ubuntu-minimal-2204" and i["family"] == "ubuntu-minimal-2204-lts"
          for i in r["images"]))
check("机型按区域过滤（n2-standard-4 在 me-central2 不可用）",
      not any(m["name"] == "n2-standard-4"
              for m in client.get("/api/catalog?region=me-central2").json()["machine_types"]))

r = client.post("/api/cost/estimate", json={
    "machine_type": "e2-small", "disk_type": "pd-ssd", "disk_size_gb": 100,
    "region": "asia-east1", "count": 3}).json()
check("成本估算区域系数生效",
      r["estimate"]["region_price_index"] == 1.10 and r["estimate"]["monthly_total_usd"] > 0,
      str(r["estimate"]))
r = client.post("/api/cost/estimate", json={
    "machine_type": "e2-standard-4", "disk_type": "pd-standard", "disk_size_gb": 30,
    "region": "us-central1", "count": 1, "preemptible": True}).json()
check("抢占式折扣生效", "preemptible" in r["estimate"]["discount"], str(r["estimate"]["discount"]))

r = client.post("/api/create", json={"account_ids": [ACC_ID], "count": 1,
                                     "spec": {"machine_type": "n2-standard-4",
                                              "region_mode": "auto_free"},
                                     "dry_run": True}).json()
check("Dry-run 预检返回计划", r["ok"] and r["dry_run"] and isinstance(r["plan"], list), str(r)[:150])

r = client.post("/api/instance_action", json={"action": "stop", "targets": ["vm-x"]}).json()
check("实例 stop 任务入队", r["ok"], str(r))
r = client.post("/api/instance_action", json={"action": "wipe", "targets": ["vm-x"]})
check("非法实例动作 → 400", r.status_code == 400, str(r.status_code))
r = client.post("/api/execute", json={"command": "uname -a", "all": False, "concurrency": 2}).json()
check("命令执行任务入队", r["ok"], str(r))
r = client.post("/api/execute", json={})
check("命令为空 → 422 校验失败", r.status_code == 422, str(r.status_code))

r = client.get("/api/tasks?limit=50").json()
check("任务列表可读", r["ok"] and len(r["tasks"]) > 0, str(len(r["tasks"])))
tid = r["tasks"][0]["id"]
r = client.get(f"/api/tasks/{tid}").json()
check("单个任务详情可读", r["ok"] and r["task"]["id"] == tid)
r = client.get("/api/tasks/does-not-exist")
check("不存在的任务 → 404", r.status_code == 404, str(r.status_code))

r = client.get("/api/logs?since_id=0&limit=200").json()
check("日志可读且非空", r["ok"] and len(r["logs"]) > 0, str(len(r["logs"])))

# 经过上面的真实创建（含 auto_open_firewall=True 那次），默认配置必须仍然安全
cfg = client.get("/api/config").json()["config"]

r = client.get("/api/config").json()
check("★ 危险默认值：新用户的全开放防火墙默认关闭",
      r["config"]["auto_open_firewall"] is False, str(r["config"]["auto_open_firewall"]))
check("出厂默认与 README 一致（e2-micro + Ubuntu Minimal 22.04 + 30GB）",
      catalog_mod.DEFAULT_CONFIG["machine_type"] == "e2-micro" and
      catalog_mod.DEFAULT_CONFIG["image_key"] == "ubuntu-minimal-2204" and
      catalog_mod.DEFAULT_CONFIG["disk_size_gb"] == 30 and
      catalog_mod.DEFAULT_CONFIG["disk_type"] == "pd-standard", str(catalog_mod.DEFAULT_CONFIG))
check("默认配置接口标注为用户级（互不覆盖）",
      r.get("scope") == "user", str(r.get("scope")))

check("★ 每次创建后默认配置中的危险开关始终为 False",
      cfg["auto_open_firewall"] is False and cfg["preemptible"] is False and cfg["spot"] is False,
      json.dumps({k: cfg.get(k) for k in ("auto_open_firewall", "preemptible", "spot")}))
check("★ 最后一次创建显式指定的字段会被记住",
      cfg["machine_type"] == "e2-small" and cfg["image_key"] == "ubuntu-2404-lts"
      and cfg["disk_size_gb"] == 20 and cfg["disk_type"] == "pd-standard",
      json.dumps({k: cfg.get(k) for k in ("machine_type", "image_key",
                                          "disk_size_gb", "disk_type")}))

# 显式保存时，自定义标签应当被记住（这是「保存为默认配置」的正当路径）
r = client.post("/api/config", json={"machine_type": "t2d-standard-2", "image_key": "rocky-9",
                                     "disk_type": "pd-ssd", "disk_size_gb": 100,
                                     "tags": ["http-server", "worker", "custom-tag"]}).json()
cfg2 = client.get("/api/config").json()["config"]
check("★ 保存为默认配置时可写入自定义标签",
      r["ok"] and cfg2["tags"] == ["http-server", "worker", "custom-tag"]
      and cfg2["machine_type"] == "t2d-standard-2" and cfg2["image_key"] == "rocky-9",
      json.dumps({k: cfg2.get(k) for k in ("tags", "machine_type", "image_key")}, ensure_ascii=False))

r = client.post("/api/config", json={"machine_type": "e2-medium", "disk_size_gb": 50}).json()
r2 = client.get("/api/config").json()
check("配置保存/读取", r["ok"] and r2["config"]["machine_type"] == "e2-medium"
      and r2["config"]["disk_size_gb"] == 50, str(r2["config"]))

# ═══════════════ F. 页面 / 静态资源 ═══════════════
print("\n── F. 页面与前端资源 ──")
html = client.get("/").text
check("控制台页面返回", "GCP Manager" in html and len(html) > 40000, f"{len(html)} bytes")
check("控制台在 mounted 中调用 boot（否则永远停在启动遮罩）",
      "mounted(){ this.boot(); }" in html)
check("boot 的 finally 中关闭启动遮罩",
      "this.booted = true;" in html and "} finally {" in html)
check("★ 建立 WebSocket 前先 await 日志尾部拉取（防日志重复）",
      "await this.loadLogsTail();" in html)
check("★ 前端对 WebSocket 日志按 id 去重",
      "l.id > this.logSeq" in html)
for key in ("createApp", "vue.global.prod.js", "region_mode", "checkedAccountIds",
            "spec.machine_type", "disk_size_gb", "execForm", "pwStrength"):
    check(f"页面含 {key}", key in html)
check("控制台含响应式断点（手机）", "@media (max-width:768px)" in html and "@media (max-width:480px)" in html)
check("控制台含减少动效偏好支持", "prefers-reduced-motion" in html)
check("表格使用 .resp 响应式类", html.count('class="resp"') >= 5, str(html.count('class="resp"')))

lh = client.get("/login").text
check("登录页返回", "验证码" in lh and "captcha" in lh)
check("登录页含响应式断点", "@media (max-width:520px)" in lh and "@media (max-width:360px)" in lh)
check("登录页字号 ≥16px 防 iOS 缩放", "font-size:16px" in lh)
check("登录页配色心理学注释", "配色心理学" in lh)

r = client.get("/static/vendor/vue.global.prod.js")
check("Vue 3 本地托管可用", r.status_code == 200 and len(r.text) > 100000, str(len(r.text)))

# 密码策略接口
r = client.get("/api/auth/password_policy").json()
check("密码策略接口", r["ok"] and r["min_length"] == 8, str(r))

# 密码强度函数
from core.auth import password_strength, random_password  # noqa: E402
check("强度评估：弱密码", password_strength("12345678")[0] <= 2, str(password_strength("12345678")))
check("强度评估：强密码", password_strength("Adm1n@GCP2026!")[0] >= 3, str(password_strength("Adm1n@GCP2026!")))
check("随机密码强度达标", password_strength(random_password())[0] >= 3)

# 登出
r = client.post("/api/auth/logout")
check("登出成功", r.status_code == 200)
r = client.get("/api/status")
check("登出后接口再次 401", r.status_code == 401, str(r.status_code))

print("\n" + "=" * 76)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("   -", f)
print("=" * 76)
sys.exit(1 if FAIL else 0)
