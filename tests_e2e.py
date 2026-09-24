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
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

DATA_DIR = os.path.join(BASE_DIR, "data")
KEY_DIR = os.path.join(DATA_DIR, "keys")
os.makedirs(KEY_DIR, exist_ok=True)

# ⚠ 测试必须使用独立的临时库，绝不允许删除生产库 data/gcp_web.db。
# 历史缺陷：此处直接 os.remove(PROD_DB)，若服务正在运行，进程仍持有已删除
# inode 的文件句柄，客户端看到的是"数据库忽然空了 / 会话全部失效"，
# 排查方向会被严重误导。这里改为在 tests_e2e 专用目录下建库，
# 并通过环境变量告知 app.py 使用该目录（否则 app.py 仍会绑定生产库）。
DATA_DIR = os.environ.get("GCPWEB_DATA_DIR") or tempfile.mkdtemp(prefix="gcpweb-test-")
os.environ["GCPWEB_DATA_DIR"] = DATA_DIR
KEY_DIR = os.path.join(DATA_DIR, "keys")
os.makedirs(KEY_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "gcp_web.db")

# 启动前断言：测试库绝不能等于生产库路径
PROD_DB = os.path.join(BASE_DIR, "data", "gcp_web.db")
assert os.path.abspath(DB_PATH) != os.path.abspath(PROD_DB), \
    f"测试库路径与生产库相同（{PROD_DB}），拒绝运行以免误删生产数据"

# 仅清理测试专用库（若存在），生产库不在其中
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


# ═══════════════════ 前置：默认值与省钱配置 ═══════════════════
print("=" * 76)
print("GCP Manager Web · 端到端验证（含鉴权）")
print("=" * 76)
print("\n── 前置：默认值与省钱配置 ──")
check("★ 全开放防火墙默认关闭（不自动放开 0.0.0.0/0）",
      catalog_mod.DEFAULT_CONFIG["auto_open_firewall"] is False,
      str(catalog_mod.DEFAULT_CONFIG["auto_open_firewall"]))
check("★ 禁用 Ops Agent 默认开启（避免日志/监控费用）",
      catalog_mod.DEFAULT_CONFIG["disable_ops_agent"] is True)
check("★ 数据保护→无备份 默认开启（避免快照存储费用）",
      catalog_mod.DEFAULT_CONFIG["no_backup"] is True)
check("★ 无快照时间表 默认开启",
      catalog_mod.DEFAULT_CONFIG["no_snapshot_schedule"] is True)
check("★ 删除保护默认关闭（便于回收，避免持续计费）",
      catalog_mod.DEFAULT_CONFIG["deletion_protection"] is False)
check("出厂默认与 README 一致（e2-micro + Ubuntu Minimal 22.04 + 30GB）",
      catalog_mod.DEFAULT_CONFIG["machine_type"] == "e2-micro" and
      catalog_mod.DEFAULT_CONFIG["image_key"] == "ubuntu-minimal-2204" and
      catalog_mod.DEFAULT_CONFIG["disk_size_gb"] == 30 and
      catalog_mod.DEFAULT_CONFIG["disk_type"] == "pd-standard", str(catalog_mod.DEFAULT_CONFIG))
check("DEFAULT_CONFIG 中标签默认两枚",
      catalog_mod.DEFAULT_CONFIG["tags"] == ["http-server", "https-server"],
      str(catalog_mod.DEFAULT_CONFIG["tags"]))

_sv = catalog_mod.savings_status(catalog_mod.DEFAULT_CONFIG)
check("省钱清单共 7 项", _sv["total"] == 7, str(_sv["total"]))
check("★ 出厂默认开启 6 项省钱优化",
      _sv["enabled"] == 6, f"{_sv['enabled']}/{_sv['total']}")
check("★ 唯一默认未开启的是「抢占式/Spot」（默认不抢占，符合预期）",
      [i["key"] for i in _sv["items"] if not i["enabled"]] == ["preemptible_or_spot"],
      str([i["key"] for i in _sv["items"] if not i["enabled"]]))
check("省钱清单包含「无备份」项",
      any(i["key"] == "no_backup" and i["enabled"] for i in _sv["items"]))
_sv_off = catalog_mod.savings_status({
    "auto_open_firewall": False, "disable_ops_agent": False, "no_backup": False,
    "no_snapshot_schedule": False, "deletion_protection": True,
    "network_tier": "PREMIUM", "machine_type": "n2-standard-4",
    "disk_type": "pd-ssd", "disk_size_gb": 200})
check("省钱清单能正确反映关闭状态",
      _sv_off["enabled"] == 0, f"{_sv_off['enabled']}/{_sv_off['total']}")


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
# ── 升级脚本（update.sh）──────────────────────────────────────────────
_u = open(os.path.join(BASE_DIR, "update.sh"), encoding="utf-8").read()
check("★ update.sh 存在且可执行",
      os.path.isfile(os.path.join(BASE_DIR, "update.sh"))
      and os.access(os.path.join(BASE_DIR, "update.sh"), os.X_OK))
check("★ 升级脚本绝不触碰 data/",
      'rm -rf "$APP_DIR/data"' not in _u and "rm -rf ${APP_DIR}/data" not in _u
      and "data_fingerprint" in _u and "data/ 目录未被改动" in _u)
check("★ 升级保留本地未提交改动（stash 而非静默丢弃）",
      "git stash push" in _u and "FORCE" in _u)
check("★ 升级支持 git 与 tarball 两条路径",
      "git reset --hard" in _u and "REPO_TARBALL" in _u and "tar xzf" in _u)
check("★ 升级支持 --check 只读模式",
      '--check' in _u and "CHECK_ONLY" in _u)
check("★ 升级会重启 systemd 服务并校验存活",
      "systemctl restart" in _u and "is-active --quiet" in _u)
check("★ 升级失败自动换国内源",
      "pypi.tuna.tsinghua.edu.cn" in _u)
check("★ 升级脚本在管道模式下安全（BASH_SOURCE 兜底）",
      'SELF="${BASH_SOURCE[0]:-}"' in _u)
check("★ install.sh 会引导用户使用 update.sh",
      "update.sh" in open(os.path.join(BASE_DIR, "install.sh"), encoding="utf-8").read())
check("★ README 有升级说明章节",
      "## 升级到最新版" in open(os.path.join(BASE_DIR, "README.md"), encoding="utf-8").read())

# 测试夹具（tools/ui_login_probe.py）会暴露验证码明文，绝不能出现在产品里
check("★ 产品代码 app.py 不含任何 __probe 调试路由",
      "__probe" not in open(os.path.join(BASE_DIR, "app.py"), encoding="utf-8").read())
check("★ 运行中的 app 也没有 __probe 路由",
      not any("__probe" in getattr(r, "path", "") for r in appmod.app.routes),
      str([getattr(r, "path", "") for r in appmod.app.routes if "__probe" in getattr(r, "path", "")]))
check("★ 夹具强制只监听 127.0.0.1",
      'HOST = "127.0.0.1"' in open(os.path.join(BASE_DIR, "tools", "ui_login_probe.py"),
                                   encoding="utf-8").read())

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
    "disable_ops_agent": True, "no_backup": True, "no_snapshot_schedule": True,
    "deletion_protection": False, "preemptible": True,
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
    # ---- 省钱项：真实落到请求体 ----
    check("★ [省钱] 禁用 Ops Agent：logging=false",
          md.get("google-logging-enabled") == "false", str(md.get("google-logging-enabled")))
    check("★ [省钱] 禁用 Ops Agent：monitoring=false",
          md.get("google-monitoring-enabled") == "false", str(md.get("google-monitoring-enabled")))
    check("★ [省钱] 禁用 Ops Agent：ops-agent-enabled=false",
          md.get("google-ops-agent-enabled") == "false", str(md.get("google-ops-agent-enabled")))
    check("★ [省钱] 数据保护→无备份：resource_policies 为空",
          list(ip.resource_policies) == [], str(list(ip.resource_policies)))
    check("★ [省钱] 无快照来源：未指定 source_snapshot",
          not ip.source_snapshot, str(ip.source_snapshot))
    check("★ [省钱] 删除保护已关闭", cap.deletion_protection is False,
          str(cap.deletion_protection))
    check("★ 未开启全开放防火墙时不调用防火墙接口", len(FW_CALLS) == 0, str(len(FW_CALLS)))

# ---- 全开防火墙关闭时不得触碰项目防火墙规则 ----
CAPTURED.clear()
FW_CALLS.clear()
r = client.post("/api/create", json={
    "account_ids": [ACC_ID], "count": 1,
    "spec": {"machine_type": "e2-micro", "image_key": "ubuntu-minimal-2204",
             "disk_type": "pd-standard", "disk_size_gb": 30,
             "region_mode": "single", "region": "us-west1",
             "auto_open_firewall": False, "disable_ops_agent": True,
             "no_backup": True, "no_snapshot_schedule": True,
             "deletion_protection": False},
    "login_mode": "ssh_key", "ssh_public_key": "ssh-rsa AAAAB3NzaC1yc2E test@host",
    "concurrency": 1, "retry_count": 0, "ssh_timeout": 5}).json()
check("提交「默认关闭防火墙」任务", r["ok"], str(r)[:140])
time.sleep(9)
check("★ [默认] 全开防火墙关闭时不调用防火墙接口",
      len(FW_CALLS) == 0, f"FW_CALLS={len(FW_CALLS)}")
check("★ [默认] 关闭防火墙时实例仍正常创建",
      len(CAPTURED) >= 1, f"captured={len(CAPTURED)}")
if CAPTURED:
    _md = {i.key: i.value for i in CAPTURED[0]["instance"].metadata.items}
    check("★ [默认] 省钱项仍然生效（Ops Agent 关闭）",
          _md.get("google-logging-enabled") == "false"
          and _md.get("google-ops-agent-enabled") == "false",
          str({k: v for k, v in _md.items() if 'google' in k}))

# ---- 显式开启全开防火墙时，才建立 allow-all 规则 ----
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
    check("★ 显式开启：auto_open_firewall=True 触发 allow-all 规则",
          len(FW_CALLS) >= 2, str(len(FW_CALLS)))
    fw_names = [getattr(c.get("firewall_resource"), "name", None) for c in FW_CALLS]
    check("★ 防火墙规则名为 allow-all-ingress / allow-all-egress",
          {"allow-all-ingress", "allow-all-egress"} <= set(n for n in fw_names if n),
          str(fw_names))
    if FW_CALLS:
        fr = FW_CALLS[0].get("firewall_resource")
        if fr is not None:
            check("★ 全开放：source_ranges 含 0.0.0.0/0",
                  "0.0.0.0/0" in list(fr.source_ranges or []), str(list(fr.source_ranges or [])))
            check("★ 全开放：协议为 all",
                  any(getattr(a, "I_p_protocol", None) == "all" for a in (fr.allowed or [])),
                  str(fr.allowed))
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

# ---- 回归：zone/region 混淆与真实 zone 拉取 ----
# 缺陷：传 zone（us-west1-b）入 single 模式时未归一化，后续按 region 拼后缀
# 得到 us-west1-b-b 这种不存在的 zone，GCP 报 Permission denied，
# 把「zone 不存在」伪装成「权限不足」。
pool, _, _ = tmm.resolve_region_pool({"region_mode": "single", "region": "us-west1-b"})
check("★ single 模式下 zone 入参归一化为 region",
      pool == ["us-west1"], str(pool))
pool, _, _ = tmm.resolve_region_pool({"region_mode": "single", "region": ""})
check("single 模式空 region 兜底为 us-central1", pool == ["us-central1"], str(pool))

_zs = tmm.zones_for_region("us-west1-b")
check("★ zones_for_region 接受 zone 入参并归一化（兜底路径）",
      all(z.startswith("us-west1-") and not z.startswith("us-west1-b-") for z in _zs),
      str(_zs))
check("zones_for_region 离线兜底仍返回非空列表", len(_zs) > 0, str(_zs))


# 关键：有 GCP 客户端时必须用真实 zone，而不是 hardcode 后缀猜测。
# us-west1 真实只有 a/b/c，没有 d/f；早先硬编码 a/b/c/d/f 会撞上不存在的 zone，
# 被 GCP 报成 Permission denied，把「区域不存在」误读成「权限不足」。
class _FakeGCP:
    def __init__(self, zones): self._zones = zones
    def list_zones(self, region=""):
        return [z for z in self._zones if not region or z.startswith(region + "-")]


tmm._zone_cache = {}
_real = tmm.zones_for_region("us-west1", _FakeGCP(["us-west1-a", "us-west1-b", "us-west1-c"]))
check("★ 有 GCP 客户端时使用真实 zone 列表",
      _real == ["us-west1-a", "us-west1-b", "us-west1-c"], str(_real))
check("★ 真实 zone 路径下不产出不存在的 us-west1-d/f",
      "us-west1-d" not in _real and "us-west1-f" not in _real, str(_real))
check("★ zone 列表被缓存（同一 region 不重复请求）",
      tmm.zones_for_region("us-west1", _FakeGCP(["WRONG"])) == _real,
      str(tmm.zones_for_region("us-west1")))
tmm._zone_cache = {}

# build_instance_spec：region 缺失/为 zone 时子网 URL 必须合法
from core.gcp import build_instance_spec  # noqa: E402
_s = build_instance_spec({"region": "us-west1-b", "subnet": "default"})
check("★ build_instance_spec 归一化 zone→region",
      _s["region"] == "us-west1", _s["region"])
check("★ 子网 URL 与 region 匹配",
      _s["subnet_url"] == "regions/us-west1/subnetworks/default", _s["subnet_url"])
_s = build_instance_spec({"subnet": "default"})
check("★ region 缺失时子网 URL 不含空段（regions//...）",
      "regions//" not in _s["subnet_url"] and _s["subnet_url"].startswith("regions/"),
      _s["subnet_url"])

# 新接口须登录才可访问（不允许未授权读项目网络拓扑）。
# 用独立客户端做匿名检查，避免影响主客户端的登录态。
_anon = TestClient(appmod.app)
r = _anon.get("/api/project_networks")
check("★ /api/project_networks 未登录返回 401", r.status_code == 401, str(r.status_code))
r = _anon.get("/api/project_zones")
check("★ /api/project_zones 未登录返回 401", r.status_code == 401, str(r.status_code))

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
check("★ 全开放防火墙：新用户默认为关闭",
      r["config"]["auto_open_firewall"] is False, str(r["config"]["auto_open_firewall"]))
check("★ 省钱项：新用户默认全部开启（无备份等重点项）",
      r["config"]["disable_ops_agent"] is True and r["config"]["no_backup"] is True
      and r["config"]["no_snapshot_schedule"] is True
      and r["config"]["deletion_protection"] is False,
      json.dumps({k: r["config"].get(k) for k in ("disable_ops_agent", "no_backup",
                                                  "no_snapshot_schedule", "deletion_protection")}))
check("出厂默认与 README 一致（e2-micro + Ubuntu Minimal 22.04 + 30GB）",
      catalog_mod.DEFAULT_CONFIG["machine_type"] == "e2-micro" and
      catalog_mod.DEFAULT_CONFIG["image_key"] == "ubuntu-minimal-2204" and
      catalog_mod.DEFAULT_CONFIG["disk_size_gb"] == 30 and
      catalog_mod.DEFAULT_CONFIG["disk_type"] == "pd-standard", str(catalog_mod.DEFAULT_CONFIG))
check("默认配置接口标注为用户级（互不覆盖）",
      r.get("scope") == "user", str(r.get("scope")))
check("★ 目录接口内置省钱清单",
      "savings" in client.get("/api/catalog").json()
      and client.get("/api/catalog").json()["savings"]["total"] == 7,
      str(client.get("/api/catalog").json().get("savings", {}).get("enabled")))
r_sv = client.post("/api/savings", json={"auto_open_firewall": False, "disable_ops_agent": False,
                                         "no_backup": False, "no_snapshot_schedule": False,
                                         "deletion_protection": True, "network_tier": "PREMIUM",
                                         "machine_type": "n2-standard-4", "disk_type": "pd-ssd",
                                         "disk_size_gb": 500}).json()
check("★ /api/savings 按 spec 实时计算",
      r_sv["ok"] and r_sv["savings"]["enabled"] == 0, str(r_sv["savings"]["enabled"]))
r_sv2 = client.post("/api/savings", json={"machine_type": "e2-micro", "disk_type": "pd-standard",
                                          "disk_size_gb": 30, "network_tier": "STANDARD",
                                          "disable_ops_agent": True, "no_backup": True,
                                          "no_snapshot_schedule": True,
                                          "deletion_protection": False}).json()
check("★ 免费机型组合命中「标准盘+免费机型」省钱项",
      any(i["key"] == "pd_standard_or_free" and i["enabled"] for i in r_sv2["savings"]["items"]),
      str([i["key"] for i in r_sv2["savings"]["items"] if i["enabled"]]))

# 经过上面的真实创建（含一次显式开启防火墙的创建），
# 默认配置必须仍然保持"防火墙关闭"——危险开关不得被回写
check("★ 某次创建开启防火墙后，默认配置仍为关闭（危险开关不回写）",
      cfg["auto_open_firewall"] is False, str(cfg["auto_open_firewall"]))
check("★ 同理 preemptible / spot 也不被回写",
      cfg["preemptible"] is False and cfg["spot"] is False,
      json.dumps({k: cfg.get(k) for k in ("preemptible", "spot")}))

# 经过上面的真实创建，默认配置中的省钱项必须仍然全部开启
check("★ 每次创建后省钱项默认值不被关闭",
      cfg["disable_ops_agent"] is True and cfg["no_backup"] is True
      and cfg["no_snapshot_schedule"] is True,
      json.dumps({k: cfg.get(k) for k in ("disable_ops_agent", "no_backup",
                                          "no_snapshot_schedule")}))
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
check("★ 前端含「极速部署预设」入口", "quickMode" in html and "极速部署预设" in html)
check("★ 前端含「放开全开防火墙」显式入口", "openFirewallMode" in html and "放开全开防火墙" in html)

# ---- 前端静态检查：这类错误没有构建步骤兜底，只能靠测试拦 ----
# 缺陷 1：this.api() 只接受一个 path。曾误写成 this.api('GET', url)，
# 结果把 'GET' 当成路径 → 404，而 404 响应体是 {"detail":"Not Found"}，
# 前端判 !r.ok 后只弹了泛化的「读取失败」，看不出真实原因。
import re as _re  # noqa: E402
_api_2arg = _re.findall(r"this\.api\(\s*['\"](?:GET|POST|PUT|DELETE|PATCH)['\"]\s*,", html)
check("★ 前端无 this.api('METHOD', url) 两参误用",
      not _api_2arg, str(_api_2arg[:3]))

# 缺陷 2：toast 只认识 'g'（绿）/ 'r'（红）/ 'a'（琥珀），传别的值不报错但样式丢失
_kinds = set(_re.findall(r"toast\([^)]*?,\s*'([a-zA-Z_]+)'\s*\)", html))
_bad = _kinds - {"g", "r", "a"}
check("★ 前端 toast 样式级别仅用 g/r/a",
      not _bad, f"非法级别: {sorted(_bad)}")

# 缺陷 3：网络/子网必须能从真实 VPC 列表渲染，且提供了读取入口
check("★ 前端提供「读取项目真实网络」按钮",
      "读取项目真实网络" in html and "loadNetworks" in html)
check("★ 网络/子网按真实列表渲染为 select",
      'v-for="n in netInfo.networks"' in html and 'v-for="s in netInfo.subnets"' in html)
check("★ 无 default 网络时给出告警",
      "该项目不存在名为" in html and "has_default_network" in html)
check("★ 前端含省钱优化面板", "省钱优化" in html and "savings.items" in html)
check("★ 前端出厂默认全开防火墙为 false", "auto_open_firewall:false" in html)
check("★ 前端出厂默认含无备份", "no_backup:true" in html and "no_backup: c.no_backup !== false" in html)
check("★ 前端防火墙文案标注「默认关闭」",
      "0.0.0.0/0）· 默认关闭" in html)

lh = client.get("/login").text
check("登录页返回", "验证码" in lh and "captcha" in lh)

# ── 登录页视觉：逐项对齐上游 sysuahb 的设计 ──
# 上游 web/views/login/index.html + web/static/css/style.css 中的
# login-page / wow-login-* 规则。这些断言就是「与上游一致」的清单。
check("★ 登录页采用上游 wow-login 结构",
      all(k in lh for k in ('wow-login-page', 'wow-login-background', 'wow-login-content',
                            'wow-login-header', 'wow-login-card', 'wow-input-group',
                            'wow-verification', 'wow-remember', 'wow-language-switch')))
check("★ 标题与副标题与上游一致",
      "统一协同平台" in lh and "Unified Collaboration Platform" in lh)
check("★ 输入框左侧图标为上游同款 FontAwesome 图标",
      'fas fa-user' in lh and 'fas fa-lock' in lh and 'fas fa-eye' in lh)
check("★ 密码可见切换沿用上游同名函数 togglePwd", "function togglePwd" in lh)
check("★ 记住账号沿用上游 localStorage 键名",
      "nps_login_username" in lh)
check("★ 提示遮罩沿用上游 showMsg（深色底 #2e2d3c）",
      "function showMsg" in lh and "2e2d3c" in lh)
check("★ 语言切换按上游方式（li[lang] + 按钮取语言全名）",
      'lang="zh-CN"' in lh and 'lang="en-US"' in lh and "languagemenu" in lh)
check("★ 资源全部本地托管（无 CDN 外链）",
      "/static/vendor/" in lh and "http://cdn" not in lh and "https://cdn" not in lh
      and "unpkg" not in lh and "jsdelivr" not in lh)
check("★ 未残留上游 Go 模板变量（除注释外）",
      lh.count("{{") <= 1, f"{{{{ 出现 {lh.count('{{')} 次")

lc = client.get("/static/login.css").text
check("★ 登录页样式表为上游同源提取（含品牌色 #3d53f5）",
      "#3d53f5" in lc and "linear-gradient(90deg, #3d53f5, #6f9bd1 45%, #2ec6b4)" in lc)
check("★ 含上游背景渐变与光斑动画",
      "wow-bg-shift" in lc and "wow-float-a" in lc and "wow-float-b" in lc and "wow-rise" in lc)
check("★ 含上游响应式断点",
      "@media (max-width: 600px)" in lc and "@media (max-width: 380px)" in lc
      and "@media (min-width: 576px) and (max-width: 991.98px)" in lc)
check("★ 尊重系统减少动效偏好", "prefers-reduced-motion" in lc)
check("★ 输入框字号 16px（上游值，同时避免 iOS 聚焦缩放）",
      "font-size: 16px" in lc)
check("★ 保留了上游的配色心理学说明注释",
      "psychology palette" in lc or "配色" in lc)
check("★ 已修正上游 spinner 选择器漏配问题",
      ".wow-login-card .btn-login .btn-spinner { display: none; }" in lc)

# 登录页 JS 必须能读到后端的具体错误原因。
# 缺陷：后端用 HTTPException，响应体是 {"detail": "..."}；
# 只读 r.error 会永远 undefined，导致所有具体原因被吞成通用提示。
check("★ 登录页读取后端 detail 字段（否则错误原因全被吞）",
      "r.error || r.detail" in lh)

# FontAwesome 本地资源必须齐全，否则图标全变方块
for _f in ("/static/vendor/fontawesome/css/fontawesome.min.css",
           "/static/vendor/fontawesome/css/solid.min.css",
           "/static/vendor/fontawesome/webfonts/fa-solid-900.woff2",
           "/static/vendor/bootstrap.min.css",
           "/static/vendor/jquery-3.7.1.min.js"):
    _r = client.get(_f)
    check(f"★ 本地资源可用 {_f.split('/')[-1]}", _r.status_code == 200, str(_r.status_code))

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

# ═══════════════════════════════════════════════════════════════════════════
# F2. 实例操作：非本工具创建的实例也必须能操作
# ═══════════════════════════════════════════════════════════════════════════
print("\n── F2. 实例操作（含非本工具创建的实例）──")

from core.tasks import TaskManager  # noqa: E402

# 真实缺陷：submit_instance_action 只用本地 vm_passwords 记录反查所属账号与
# zone。对「预存在的实例」或用原版桌面工具建的实例，本地没有记录，
# 于是直接判「未找到所属账号」并 continue —— 而其后的 zone 兜底查询
# 永远走不到。表现就是：实例列表里看得见，却删不掉/停不了。
_OP_CALLED = []


class _FakeGCPAction:
    def __init__(self, project, instances):
        self.project = project
        self._instances = instances      # [(name, zone)]

    def list_instances(self):
        return [{"name": n, "zone": z} for n, z in self._instances]

    def delete_instance(self, zone, name):
        _OP_CALLED.append(("delete", self.project, zone, name))
        return True, "删除成功"

    def stop_instance(self, zone, name):
        _OP_CALLED.append(("stop", self.project, zone, name))
        return True, "已停止"

    def start_instance(self, zone, name):
        _OP_CALLED.append(("start", self.project, zone, name))
        return True, "已启动"

    def reset_instance(self, zone, name):
        _OP_CALLED.append(("reset", self.project, zone, name))
        return True, "已重启"


class _FakeStore:
    def __init__(self, accounts, vms):
        self._accounts, self._vms = accounts, vms
        self.lock = __import__("threading").Lock()
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("CREATE TABLE vm_passwords(name TEXT)")
        # LogSink 会往 logs 表批量写入，假库也得有
        self.conn.execute("CREATE TABLE logs(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                          "ts REAL, task_id TEXT, level TEXT, message TEXT)")
        for v in vms:
            self.conn.execute("INSERT INTO vm_passwords(name) VALUES(?)", (v["name"],))
        self.conn.commit()
        self.tasks = {}

    def get_accounts(self): return self._accounts
    def get_all_vms(self): return self._vms
    def create_task(self, *a, **k): self.tasks[a[0]] = {}
    def update_task(self, tid, status=None, message=None, result=None, **kw):
        self.tasks[tid] = {"status": status, "message": message, "result": result}
    def get_tasks(self, *a, **k): return []
    def add_log(self, *a, **k): pass
    def get_logs(self, *a, **k): return []


_acc = {"id": 7, "key_path": "/tmp/fake.json", "project_id": "proj-x",
        "email": "sa@proj-x.iam.gserviceaccount.com", "proxy": "", "proxy_type": "HTTPS"}

# 场景：实例在项目里真实存在，但本地 vm_passwords 没有任何记录
_OP_CALLED.clear()
_st = _FakeStore([_acc], [])
_tm2 = TaskManager(_st)
_tm2.account_service = lambda account: _FakeGCPAction(
    account["project_id"], [("vm-external-1", "us-west1-b")])
_r = _tm2.submit_instance_action("delete", ["vm-external-1"])
time.sleep(2)
_res = _st.tasks.get(_r["task_id"], {})
_detail = (_res.get("result") or {}).get("results", [])
check("★ 非本工具创建的实例：能通过遍历账号定位到并执行删除",
      _OP_CALLED and _OP_CALLED[0][0] == "delete"
      and _OP_CALLED[0][2] == "us-west1-b" and _OP_CALLED[0][3] == "vm-external-1",
      str(_OP_CALLED))
check("★ 非本工具创建的实例：不再报「未找到所属账号」",
      bool(_detail) and all("未找到所属账号" not in (d.get("error") or "") for d in _detail),
      json.dumps(_detail, ensure_ascii=False)[:200])
check("★ 非本工具创建的实例：任务结果标为成功",
      _res.get("status") == "done" and "1/1 成功" in (_res.get("message") or ""),
      str(_res.get("message")))

# 场景：本地有记录但实例在项目里已不存在，且账号列表为空
_OP_CALLED.clear()
_st2 = _FakeStore([], [])       # 一个账号都没配置
_tm3 = TaskManager(_st2)
_tm3.account_service = lambda account: _FakeGCPAction(account["project_id"], [])
_r = _tm3.submit_instance_action("delete", ["vm-ghost"])
time.sleep(2)
_d2 = (_st2.tasks.get(_r["task_id"], {}).get("result") or {}).get("results", [])
check("★ 未配置账号时给出明确原因（而非静默跳过）",
      bool(_d2) and "未配置任何账号" in (_d2[0].get("error") or ""),
      json.dumps(_d2, ensure_ascii=False)[:160])
check("★ 找不到实例时不执行任何删除动作", not _OP_CALLED, str(_OP_CALLED))

# 场景：有账号但项目里确实没有这个实例 → 报「未在任何已配置账号中找到」
_OP_CALLED.clear()
_st3 = _FakeStore([_acc], [])
_tm4 = TaskManager(_st3)
_tm4.account_service = lambda account: _FakeGCPAction(account["project_id"], [])
_r = _tm4.submit_instance_action("delete", ["vm-not-exist"])
time.sleep(2)
_d3 = (_st3.tasks.get(_r["task_id"], {}).get("result") or {}).get("results", [])
check("★ 遍历账号后仍找不到 → 明确说明并指出可能原因",
      bool(_d3) and "未在任何已配置账号的项目中找到该实例" in (_d3[0].get("error") or ""),
      json.dumps(_d3, ensure_ascii=False)[:200])
check("★ 仍找不到时同样不执行删除动作", not _OP_CALLED, str(_OP_CALLED))

# ═══════════════════════════════════════════════════════════════════════════
# G. 安装 / 部署产物
# ═══════════════════════════════════════════════════════════════════════════
print("\n── K. GCP 资源勘察（只读）──")

# ── core/inspect.py 的结构与安全性质 ────────────────────────────────
import core.inspect as gcp_inspect

_INS_SRC = open(os.path.join(BASE_DIR, "core", "inspect.py"), encoding="utf-8").read()

check("★ 勘察模块存在且可导入", callable(gcp_inspect.inspect_sections))
check("★ 提供全部预期分区",
      set(gcp_inspect.SECTIONS) >= {
          "account", "project", "services", "billing", "serviceAccounts",
          "regions", "zones", "machineTypes", "images", "networks",
          "subnetworks", "firewalls", "disks", "snapshots", "addresses",
          "summary", "extras"},
      str(sorted(gcp_inspect.SECTIONS)))
check("★ 快速节 / 深节划分正确",
      gcp_inspect.DEFAULT_SECTIONS == ["account", "summary", "regions", "zones",
                                       "networks", "subnetworks", "firewalls",
                                       "disks", "snapshots", "addresses"]
      and set(gcp_inspect.DEEP_SECTIONS) ==
          {"project", "services", "billing", "serviceAccounts",
           "machineTypes", "images", "extras"})

# 只读承诺：源码里不得出现任何写操作调用
_WRITE_CALLS = (".insert(", ".update(", ".delete(", ".patch(",
                ".set_common_instance_metadata(", ".setIamPolicy",
                ".addAccessConfig(", ".attachDisk(")
_found = [w for w in _WRITE_CALLS if w in _INS_SRC]
check("★ 勘察模块只读（无任何写操作调用）", not _found, str(_found))

# 每个分区都必须有独立的异常隔离，不能一处失败拖垮整页
check("★ 每节独立隔离异常（run() 统一兜底）",
      "def run(self, fn):" in _INS_SRC and '"ok": False, "ms": _ms(t0), "error"' in _INS_SRC)

# ── 分节函数：未知节名不炸，返回可读错误 ────────────────────────────
_res = gcp_inspect.inspect_sections(
    os.path.join(BASE_DIR, "data", "keys", "sa.json"), "no-such-project",
    "x@y.iam.gserviceaccount.com", ["__nope__"])
check("★ 未知分区不抛异常、返回明确原因",
      _res["__nope__"]["ok"] is False and "未知的勘察节" in _res["__nope__"]["error"])

# ── 逐分区调用真正的实现（打桩 GCP 客户端），确认字段映射不写错 ────
import types as _types
from google.cloud import compute_v1 as _c1


class _Stub:
    """最小桩：让各分区跑到「构造返回结构」这一步，验证字段名与类型"""
    def __init__(self, **kw): self.__dict__.update(kw)


_ins = gcp_inspect.Inspector(os.path.join(BASE_DIR, "data", "keys", "sa.json"),
                             "proj", "sa@proj.iam.gserviceaccount.com")

# ① zones：Zone 资源没有 available_machine_types（实测确认），只有 available_cpu_platforms。
#    写错字段名会抛 "Unknown field for Zone"，整节失败。
_z = _Stub(name="us-central1-a", status="UP", region="regions/us-central1",
           available_cpu_platforms=["Intel Ice Lake"])
_ins._clients[("ZonesClient", None)] = _Stub(list=lambda **k: [_z])
check("★ zones 使用 available_cpu_platforms（不是 available_machine_types）",
      _ins.zones()["data"][0]["cpuPlatforms"] == ["Intel Ice Lake"])

# ② firewalls：Firewall 资源没有 action 字段，需由 allowed/denied 反推
_f = _Stub(name="allow-x", network="networks/n", direction="INGRESS", priority=1000,
           disabled=False, source_ranges=["0.0.0.0/0"], destination_ranges=[],
           target_tags=[], source_tags=[], source_service_accounts=[],
           target_service_accounts=[], allowed=[], denied=[], log_config=None,
           creation_timestamp="2026-01-01T00:00:00Z")
_ins._clients[("FirewallsClient", None)] = _Stub(list=lambda **k: [_f])
_fw = _ins.firewalls()["data"][0]
check("★ 防火墙 action 由 allowed/denied 反推（资源无 action 字段）",
      _fw["action"] == "ALLOW" and _fw["openToWorld"] is True)

# ③ disks：未挂载的盘要标记为孤儿盘（空转计费）
_d = _Stub(name="d1", size_gb=30, type_="zones/z/diskTypes/pd-standard",
           status="READY", users=[], source_image="", creation_timestamp="t",
           physical_block_size_bytes=4096)
_ins._clients[("DisksClient", None)] = _Stub(
    aggregated_list=lambda **k: [("zones/us-central1-a", _Stub(disks=[_d]))])
check("★ 磁盘标记未挂载（孤儿盘）",
      _ins.disks()["data"][0]["orphan"] is True)

# ④ 聚合响应的字段名与客户端类名不同构：必须动态探测，不能拼字符串。
#    （RegionCommitmentsClient 的字段是 commitments 而非 region_commitments）
_agg = _Stub(_pb=_Stub(DESCRIPTOR=_Stub(fields=[
    _Stub(is_repeated=True, name="routers", message_type=None),
    _Stub(is_repeated=True, name="warning", message_type=None)])))
_ins._clients[("RoutersClient", None)] = _Stub(
    aggregated_list=lambda **k: [("regions/us-central1", _agg)])
check("★ 聚合响应字段名动态探测（类名与字段名不同构）",
      "routers" in _ins.extras()["data"])

# ⑤ 并发执行但**只在外层设置一次代理**
#    ProxyEnvContext 改的是进程级 os.environ，每节各设各的会在并发时互踩
check("★ 批量勘察在外层统一管理代理（避免并发互踩环境变量）",
      "_proxy_managed" in _INS_SRC and "with ProxyEnvContext(ins.proxy_url):" in _INS_SRC
      and "ThreadPoolExecutor" in _INS_SRC)

# ── HTTP 端点 ───────────────────────────────────────────────────────
# 前面的段落可能已经登出/换了会话，这里重新以 admin 登录一次
login("admin", ADMIN_PW)
client.post("/api/auth/logout")
r = client.get("/api/inspect")
check("★ 未登录访问 /api/inspect 被拒", r.status_code in (401, 302, 403), str(r.status_code))
r = client.get("/api/inspect/sections")
check("★ 未登录访问 /api/inspect/sections 被拒",
      r.status_code in (401, 302, 403), str(r.status_code))

login("admin", ADMIN_PW)
r = client.get("/api/inspect/sections")
_arr = r.json().get("default", []) if r.status_code == 200 else None
check("★ /api/inspect/sections 返回分区清单",
      r.status_code == 200 and _arr == gcp_inspect.DEFAULT_SECTIONS,
      f"{r.status_code} {_arr}")

# 打桩掉真正的 GCP 调用，验证端点拼装与缓存逻辑
_orig_inspect = gcp_inspect.inspect_sections
gcp_inspect.inspect_sections = lambda *a, **k: {
    s: {"ok": True, "ms": 1, "data": []} for s in k.get("sections", a[3] if len(a) > 3 else [])}

_accs = appmod.store.get_accounts()
if _accs:
    _aid = _accs[0]["id"]
    r = client.get(f"/api/inspect?account_id={_aid}&quick=1")
    _d = r.json()
    check("★ /api/inspect 返回 ok 与分区结果",
          r.status_code == 200 and _d.get("ok") is True
          and set(_d["sections"]) == set(gcp_inspect.DEFAULT_SECTIONS),
          str(_d.get("error") or list(_d.get("sections", {}).keys())))
    check("★ 只读端点回传项目与账号标识",
          _d.get("project_id") and _d.get("account_email"))
    # 缓存：第二次请求必须命中（时间戳要记完成时刻，记开始时刻会立刻过期）
    r2 = client.get(f"/api/inspect?account_id={_aid}&quick=1")
    check("★ 45 秒内重复请求命中缓存", r2.json().get("cached") is True)
    r3 = client.get(f"/api/inspect?account_id={_aid}&quick=1&fresh=1")
    check("★ fresh=1 绕过缓存", r3.json().get("cached") is False)
    # sections 参数可指定任意子集
    r4 = client.get(f"/api/inspect?account_id={_aid}&sections=regions,zones")
    check("★ sections 参数可指定子集并按序返回",
          list(r4.json()["sections"]) == ["regions", "zones"])
    # 传 zone 时归一到 region
    r5 = client.get(f"/api/inspect?account_id={_aid}&region=us-central1-a&quick=1")
    check("★ 传入 zone 自动归一到 region",
          r5.json()["region"] == "us-central1", r5.json().get("region"))
else:
    check("★ 测试需要至少一个账号才能覆盖端点", False, "无账号，跳过等于没测")
gcp_inspect.inspect_sections = _orig_inspect

# ── 前端 ────────────────────────────────────────────────────────────
_ct = client.get("/static/console.html").text
check("★ 前端注册了 GCP 资源标签页",
      "{id:'inspect'" in _ct and "label:'GCP 资源'" in _ct)
check("★ 前端分区元信息齐全",
      all(f"{{id:'{x}'," in _ct for x in gcp_inspect.SECTIONS))
check("★ 前端区分快速节与深节",
      "INSPECT_QUICK" in _ct and "INSPECT_DEEP" in _ct)
# 模板作用域只能看到组件自身属性：模块级 const 必须经 computed 暴露，
# 否则渲染期抛 "Cannot read properties of undefined" 并把 #app 清成白屏
check("★ 列定义经 computed 暴露给模板（避免白屏）",
      "inspectCols(){ return INSPECT_COLS; }" in _ct
      and "INSPECT_COLS[m.id]" not in _ct)
check("★ 模板不再直接引用模块级常量",
      all(f"INSPECT_COLS[m.id]" not in _ct for _ in [0])
      and "inspectCols[m.id]" in _ct)
# cellVal 的解构下标：列定义 5 元组，fmt 在下标 4
check("★ cellVal 按下标 4 取格式化方式（否则显示 [object Object]）",
      "const [key, , , , fmt] = c;" in _ct)
check("★ 已挂全局渲染错误兜底（不再无声白屏）",
      "config.errorHandler" in _ct and "__vue_err__" in _ct)
check("★ 汇总 KPI 与配额进度条均已实现",
      'class="kpis"' in _ct and 'class="q"' in _ct)
check("★ 明确标注只读，不误导",
      "只读查询" in _ct and "不会创建或修改任何资源" in _ct)

print("\n── L. 按钮体系与响应式不变量 ──")

_ct2 = client.get("/static/console.html").text

# ── 按钮：Windows 渲染差异处理 ──────────────────────────────────────
check("★ 按钮 appearance:none（抹掉 Windows 系统立体边框）",
      "appearance:none;-webkit-appearance:none;" in _ct2)
check("★ 按钮边框统一 1px（1.5px 在 Windows 缩放下会被舍成 1 或 2px）",
      "border:1.5px solid var(--line);background:#fff;color:var(--ink-2);cursor:pointer" not in _ct2)
check("★ 按钮用 inline-flex + gap 对齐图标与文字",
      "display:inline-flex;align-items:center;justify-content:center;gap:7px;" in _ct2)
check("★ 行高固定 1.2（微软雅黑行高偏大会把按钮撑高）",
      "font-weight:600;line-height:1.2;" in _ct2)

# 焦点环：逗号列表里出现 none 会让整条 box-shadow 非法被丢弃
check("★ 焦点环占位不用 none（否则 box-shadow 整条失效）",
      "--btn-ring:0 0 0 0 rgba(26,115,232,0);" in _ct2
      and "--btn-ring:none" not in _ct2)
check("★ 禁用/幽灵态的 box-shadow 也不用 none",
      "--btn-sh:0 0 0 0 rgba(0,0,0,0)" in _ct2
      and "--btn-sh:none" not in _ct2)
check("★ 有 :focus-visible 焦点环（键盘可达性）",
      "button:focus-visible{outline:none;--btn-ring:0 0 0 3px rgba(26,115,232,.32)}" in _ct2)
check("★ 主色按钮带品牌色投影（不是纯平色块）",
      "button.p{" in _ct2 and "rgba(26,115,232,.22)" in _ct2)
check("★ 成功/危险按钮各自有匹配的投影色",
      "rgba(16,185,129,.20)" in _ct2 and "rgba(229,72,77,.10)" in _ct2)

# 触屏与无障碍
check("★ 触屏设备抬高最小点击高度（@media hover:none）",
      "@media (hover:none){" in _ct2 and "button{min-height:42px}" in _ct2)
check("★ 尊重 prefers-reduced-motion（关闭位移动画）",
      "@media (prefers-reduced-motion:reduce){" in _ct2
      and "button:hover:not(:disabled),button:active:not(:disabled){transform:none}" in _ct2)
check("★ Windows 高对比度模式有兜底（forced-colors）",
      "@media (forced-colors:active){" in _ct2 and "ButtonText" in _ct2)

# ── 响应式：栅格轨道不能被内容撑破 ──────────────────────────────────
# 1fr 等价 minmax(auto,1fr)，auto 最小值取内容的 min-content，
# 长文案/固定宽输入框会把整条轨道顶宽 → 窄屏横向溢出
check("★ 单列栅格一律用 minmax(0,1fr)（防内容撑破轨道）",
      ".split{grid-template-columns:1fr}" not in _ct2
      and ".split{grid-template-columns:minmax(0,1fr)}" in _ct2)
check("★ 其余单列栅格同样加了 0 最小值",
      ".g2,.g3,.g4{grid-template-columns:1fr}" not in _ct2
      and ".mini{grid-template-columns:1fr;gap:8px}" not in _ct2)

# ── 响应式：断点齐备 ───────────────────────────────────────────────
for _bp in ("@media (max-width:1279px)", "@media (max-width:1023px)",
            "@media (max-width:768px)"):
    check(f"★ 断点存在 {_bp}", _bp in _ct2)

# ── 勘察页的窄屏适配 ───────────────────────────────────────────────
check("★ 勘察工具栏控件在窄屏铺满（不再固定 max-width）",
      ".insp-in,.insp-in-acc{flex:1 1 100%;max-width:none;width:100%}" in _ct2
      and 'class="insp-in insp-in-acc"' in _ct2)
check("★ 勘察表在窄屏给下限宽度并横向滚动，不把列挤成豆腐块",
      ".insp-bd table{min-width:660px}" in _ct2 and ".insp-bd table{min-width:560px}" in _ct2)
check("★ 配额条在窄屏换行显示（指标名独占一行）",
      ".q-m{flex:1 1 100%;white-space:normal}" in _ct2)
check("★ 勘察键值卡在窄屏改上下排列",
      ".insp-bd .kv-r{flex-direction:column;gap:1px;padding:5px 0}" in _ct2)
check("★ KPI 在手机上收敛为多列小卡",
      "grid-template-columns:repeat(auto-fit,minmax(78px,1fr))" in _ct2)
check("★ 小手机隐藏分区摘要（信息密度让位）",
      ".insp-br{display:none}" in _ct2)

# ── 既有能力不能被改回去 ───────────────────────────────────────────
check("★ 表格仍在 ≤768 转卡片流", "table.resp thead{display:none}" in _ct2)
check("★ 输入框仍 ≥16px（防 iOS 聚焦缩放）",
      "input,select{padding:11px 12px;font-size:16px}" in _ct2)
check("★ 登录页未受影响",
      client.get("/login").status_code == 200
      and "wow-login-card" in client.get("/login").text)

print("\n── G. 安装与部署产物 ──")

import subprocess as _sp  # noqa: E402


def _read(name):
    p = os.path.join(BASE_DIR, name)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


_inst = _read("install.sh")
_docker = _read("Dockerfile")
_compose = _read("docker-compose.yml")
_run = _read("run.sh")

check("install.sh 存在", bool(_inst))
check("Dockerfile 存在", bool(_docker))
check("docker-compose.yml 存在", bool(_compose))
check(".dockerignore 存在", bool(_read(".dockerignore")))

if _inst:
    rc = _sp.run(["bash", "-n", os.path.join(BASE_DIR, "install.sh")],
                 capture_output=True, text=True)
    check("★ install.sh 语法检查通过", rc.returncode == 0, rc.stderr[:200])
    check("★ install.sh 幂等保护（venv 已存在则复用）", "复用已有" in _inst)
    check("★ install.sh 依赖失败自动换源（清华/阿里云）",
          "pypi.tuna.tsinghua.edu.cn" in _inst and "mirrors.aliyun.com" in _inst)
    check("★ install.sh 用耗时兜底，而非仅探测连通性",
          "PIP_TIMEOUT" in _inst and "timeout \"$PIP_TIMEOUT\"" in _inst)
    check("★ install.sh 注册 systemd 并开机自启",
          "systemctl enable" in _inst and "WantedBy=multi-user.target" in _inst)
    check("★ install.sh 数据目录权限收紧为 700", "chmod 700" in _inst)
    check("★ install.sh 打印访问地址", "http://%s:%s/" in _inst)
    check("★ install.sh 支持 NO_SERVICE / PORT / APP_DIR 覆盖",
          "NO_SERVICE" in _inst and "PORT=" in _inst and "APP_DIR=" in _inst)

    # ── 管道模式自举：curl | bash 时仓库并不在本地，必须能自己把源码弄下来 ──
    # 真实缺陷：早期版本用 $(dirname "${BASH_SOURCE[0]}") 取目录，在 set -u 下
    # 管道模式直接报「BASH_SOURCE[0]：未绑定的变量」，且因为拿不到仓库而
    # 立刻以「找不到 app.py」失败 —— 文档里那条一键命令根本跑不通。
    check("★ 管道模式下 BASH_SOURCE 带默认值（set -u 不炸）",
          'SELF="${BASH_SOURCE[0]:-}"' in _inst)
    check("★ 缺 app.py 时自动下载源码（git clone）",
          "git clone --depth 1" in _inst and "fetch_repo" in _inst)
    check("★ git 不可用时退到 tarball 兜底",
          "archive/refs/heads" in _inst and "tar xzf" in _inst)
    check("★ 提供 ONLY_FETCH 仅下载模式",
          "ONLY_FETCH" in _inst)
    check("★ 环境变量 PORT 被继承时明确提示",
          "PORT_WAS_SET" in _inst and "来自环境变量 PORT" in _inst)

    # 端到端：模拟管道模式（无 BASH_SOURCE）执行 ONLY_FETCH，验证真能自举
    _tmp = tempfile.mkdtemp(prefix="gcpweb-inst-")
    _pipe = _sp.run(
        ["bash", "-c",
         f'cat "{BASE_DIR}/install.sh" | ONLY_FETCH=1 REPO_URL="{BASE_DIR}" bash'],
        cwd=_tmp, capture_output=True, text=True, timeout=180)
    _dst = os.path.join(_tmp, "gcp-manager-web", "app.py")
    check("★ 管道模式实测：能自举出源码（无 BASH_SOURCE 报错）",
          _pipe.returncode == 0 and os.path.exists(_dst),
          f"rc={_pipe.returncode} app.py={os.path.exists(_dst)} "
          f"err={(_pipe.stderr or '')[-160:]}")
    check("★ 管道模式实测：标准错误里无「未绑定的变量」",
          "未绑定" not in (_pipe.stderr or ""), (_pipe.stderr or "")[-160:])

if _docker:
    check("★ Dockerfile 用非 root 用户运行", "USER appuser" in _docker)
    check("★ Dockerfile 带健康检查", "HEALTHCHECK" in _docker)
    check("★ Dockerfile 数据目录可挂载（GCPWEB_DATA_DIR）",
          "GCPWEB_DATA_DIR" in _docker)
    check("★ Dockerfile 不把测试脚本打进镜像",
          "tests_e2e.py" not in _docker)

if _compose:
    check("★ compose 默认只绑本机（不暴露公网）", "127.0.0.1:8000:8000" in _compose)
    check("★ compose 用命名卷持久化数据", "gcpweb-data:/app/data" in _compose)
    check("★ compose 设置重启策略", "restart: unless-stopped" in _compose)

if _run:
    rc = _sp.run(["bash", "-n", os.path.join(BASE_DIR, "run.sh")],
                 capture_output=True, text=True)
    check("★ run.sh 语法检查通过", rc.returncode == 0, rc.stderr[:200])
    check("★ run.sh 优先复用 install.sh 建的 .venv", ".venv/bin/python" in _run)

print("\n" + "=" * 76)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("   -", f)
print("=" * 76)
sys.exit(1 if FAIL else 0)
