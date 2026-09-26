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
    """假防火墙客户端。

    必须实现 list/get：新增的 firewall_coverage() 会先列举项目里的规则，
    判断该 VPC 是否已有覆盖 0.0.0.0/0 的规则；缺了这两个方法探测就报错、
    导致防火墙根本没被创建（本轮就是这么踩到的）。
    """

    @classmethod
    def from_service_account_json(cls, filename, *a, **kw):
        return cls()

    def insert(self, **kw):
        FW_CALLS.append(kw)
        return _Op()

    def update(self, **kw):
        FW_CALLS.append(kw)
        return _Op()

    def get(self, project=None, firewall=None, **kw):
        # 规则不存在 → 让上层走 insert 分支
        raise Exception(f"404 firewall {firewall} not found")

    def list(self, project=None, **kw):
        # 返回空规则表：没有任何既有覆盖 → 需要新建
        return iter([])


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
# ★ 新建/重置的账号默认 must_change=1，服务端会强制先改密才能用别的接口。
# 这里给 op1 走一次改密拿到干净状态（改密相关断言在 R2 段单独测）。
OP_PW = "Op1@GCP2027!"
switch("op1", "Op1@GCP2026!")
_r = client.post("/api/auth/change_password",
                 json={"old_password": "Op1@GCP2026!", "new_password": OP_PW})
check("[operator] 新建账号首次登录被要求改密", _r.status_code == 200, _r.text[:140])
# 改密用的是 op1 身份，后面还要继续做用户管理，必须切回 admin
# 注意：admin 的密码在第 378 行已经改成 ADMIN_PW
switch("admin", ADMIN_PW)
r = client.post("/api/users", json={"username": "op1", "password": "Xx1!aaaa"})
check("重复用户名 → 400", r.status_code == 400 and "已存在" in r.json().get("detail", ""), r.text[:120])
r = client.post("/api/users", json={"username": "view1", "role": "viewer"})
check("留空密码自动生成强密码", r.status_code == 200 and r.json().get("generated") and
      len(r.json().get("password", "")) >= 12, r.text[:160])
VIEW_PW = r.json()["password"]
# 同样先走一次强制改密，再切成固定密码
switch("view1", VIEW_PW)
_r = client.post("/api/auth/change_password",
                 json={"old_password": VIEW_PW, "new_password": "View1@GCP2027!"})
check("[viewer] 新建账号首次登录被要求改密", _r.status_code == 200, _r.text[:140])
VIEW_PW = "View1@GCP2027!"
switch("admin", ADMIN_PW)
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
# ★ 重置会把 must_change 置回 1（这是有意的：管理员重置的密码必须让用户自己改掉）。
# 后面权限矩阵段要以 viewer 身份正常调接口，所以这里把它改成固定密码。
switch("view1", VIEW_PW)
_r = client.post("/api/auth/change_password",
                 json={"old_password": VIEW_PW, "new_password": "View1@GCP2027!"})
check("重置后的密码同样要求先改密", _r.status_code == 200, _r.text[:140])
VIEW_PW = "View1@GCP2027!"
switch("admin", ADMIN_PW)

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
# op1 前面已经走过强制改密流程，密码是 OP_PW 而不是初始密码
switch("op1", OP_PW)
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

print("\n── N. 备注 / 费用 / root 密码 / 代理 / 安装预设 ──")

import subprocess as _sp  # noqa: E402
import tempfile as _tf  # noqa: E402

from core import install_presets as _ip  # noqa: E402
from core.gcp import parse_proxy_input as _pp, mask_proxy as _mp  # noqa: E402

# ══════════ 安装预设 ══════════
_pk = _ip.preset_payload()
_keys = [x["key"] for x in _pk]
check("★ 预设齐备：docker / 3x-ui / nps / hermes / ekko",
      set(_keys) == {"docker", "3x-ui", "nps", "hermes", "ekko"}, str(_keys))
check("★ 预设顺序稳定（执行顺序可预期）",
      _keys == ["docker", "3x-ui", "nps", "hermes", "ekko"], str(_keys))
check("★ 每项带 label/desc/docs/note（可追溯依据）",
      all(x.get("label") and x.get("desc") and x.get("docs") and x.get("note")
          for x in _pk))
check("★ 默认不勾选任何预设（不自作主张装服务）",
      all(not x["default"] for x in _pk))

# v2-ui 已死这件事必须写清楚，不能假装还能装
_src = open(os.path.join(BASE_DIR, "core", "install_presets.py"), encoding="utf-8").read()
check("★ 明确记录 v2-ui 已不可用（仓库 404）",
      "sprov/v2-ui" in _src and "404" in _src)
check("★ v2-ui 用 3x-ui 替代并给出依据 URL",
      "MHSanaei/3x-ui" in _src)

# 归一化：过滤无效、去重、按固定顺序
check("★ 非法 key 被过滤", _ip.normalize(["docker", "不存在的预设"]) == ["docker"])
check("★ 去重", _ip.normalize(["nps", "nps", "docker"]) == ["docker", "nps"])
check("★ 输出顺序与 PRESET_ORDER 一致",
      _ip.normalize(["ekko", "docker", "nps"]) == ["docker", "nps", "ekko"])
check("★ 支持逗号分隔字符串入参", _ip.normalize("docker,nps") == ["docker", "nps"])
check("★ 空选择不生成脚本", _ip.build_script([]) == "")

# 生成的脚本必须是合法 bash，且每项都有 stdin 兜底
_script = _ip.build_script(list(_ip.INSTALL_PRESETS))
with _tf.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8") as _f:
    _f.write(_script)
    _spath = _f.name
_r = _sp.run(["bash", "-n", _spath], capture_output=True, text=True)
os.unlink(_spath)
check("★ 全量安装脚本通过 bash -n 语法检查", _r.returncode == 0, _r.stderr[:200])
for _k in _ip.INSTALL_PRESETS:
    _one = _ip.build_script([_k])
    check(f"★ 预设 {_k} 的脚本带 </dev/null stdin 兜底（无人值守不会挂死）",
          "</dev/null" in _one)
check("★ 单项失败不阻断后续项（各自子 shell + 记录退出码）",
      _script.count("结束，退出码") == len(_ip.INSTALL_PRESETS))
check("★ 3x-ui 用官方非交互机制（XUI_NONINTERACTIVE）",
      "XUI_NONINTERACTIVE" in _script and "XUI_DB_TYPE" in _script)
check("★ 验证命令按勾选项拼接", "docker --version" in _ip.verify_command(["docker"]))

# ── nps 换源：ehang-io/nps（2021 停更）→ 2016xyz/sysuahb（djylb/nps v0.34.7）──
_nps = _ip.INSTALL_PRESETS["nps"]
check("★ nps 预设改用 2016xyz/sysuahb", "2016xyz/sysuahb" in _nps["docs"])
check("★ nps 不再从 ehang-io 下载（该源 2021 年起停更）",
      "ehang-io/nps/releases" not in _nps["script"])
check("★ nps 记录了改用理由（上游持续维护）",
      "djylb/nps" in _nps["script"] and "停更" in _nps["script"])
check("★ nps 版本号固定（可复现，不跟 latest）",
      'NPS_VER="v0.34.7"' in _nps["script"] and '"${NPS_VER}"' in _nps["script"])
check("★ nps 说明里写明随机进程名机制",
      "随机进程名" in _nps["note"] and "sys" in _nps["note"])
check("★ nps 靠固定标记文件发现安装（随机名无法预设）",
      "/etc/sys????" in _nps["script"] and "sysuahb.conf" in _nps["script"])
check("★ nps 回显面板端口与默认账号提醒",
      "web_port" in _nps["script"] and "改密" in _nps["script"])
check("★ nps 面板端口标注为实测值 8081（非老版 8080）",
      "8081" in _nps["note"], _nps["note"][:60])
check("★ nps 记录了容器内实测结论",
      "Debian 12" in _nps["note"] or "容器" in _nps["note"])

# ══════════ 远程脚本执行方式 ══════════
# 容器实测踩到的真坑：`curl URL | sh -s args </dev/null` 里
# sh 的 stdin 重定向会覆盖管道，脚本内容被丢掉（curl 报 23）。
# bash -n 只查语法，查不出这种语义错误，只能靠断言守住写法。
import re as _re2  # noqa: E402

_all_scripts = _ip.build_script(list(_ip.INSTALL_PRESETS))
_anti = _re2.findall(r"curl[^\n|]*\|\s*(?:sh|bash)\b[^\n]*</dev/null", _all_scripts)
# 注释里会提到这个反例，过滤掉纯注释行
_code_lines = [ln for ln in _all_scripts.split("\n") if not ln.strip().startswith("#")]
_anti_real = [ln for ln in _code_lines
              if _re2.search(r"curl[^|]*\|\s*(?:sh|bash)\b", ln) and "</dev/null" in ln]
check("★ 不使用 `curl | sh </dev/null` 反例写法（stdin 重定向会吞掉管道）",
      not _anti_real, str(_anti_real[:2]))
check("★ 生成脚本内含 _run_remote 助手",
      "_run_remote() {" in _all_scripts)
check("★ 助手先下载到临时文件再执行（脚本走文件、stdin 走 /dev/null）",
      'curl -fsSL --retry 3 --connect-timeout 20 "$_u" -o "$_f"' in _all_scripts
      and 'sh "$_f" "$@" </dev/null' in _all_scripts)
check("★ 所有远程脚本都改走 _run_remote",
      _all_scripts.count("_run_remote https") >= 4
      and "get.docker.com | sh" not in _all_scripts)
# 只看代码行 —— 注释里会引用这个反例做说明
# 排除注释行与 echo 出来的「给人看的手动重试提示」——
# 后者是让用户在终端里手动执行的，人的 stdin 是 tty，原样写没问题。
_exec_lines = [ln for ln in _code_lines if not ln.strip().startswith("echo")]
check("★ 没有真正参与执行的 `curl|sh </dev/null` 反例写法",
      not any(("| bash -s" in ln or "| sh -s" in ln) and "</dev/null" in ln
              for ln in _exec_lines),
      str([ln for ln in _exec_lines if "| sh -s" in ln][:2]))
check("★ 流水线形式的 curl|bash 已全部替换为 _run_remote",
      not any(_re2.search(r"\|\s*(sh|bash)\b", ln) for ln in _exec_lines),
      str([ln for ln in _exec_lines if _re2.search(r"\|\s*(sh|bash)\b", ln)][:2]))
check("★ 助手下载失败有明确提示且清理临时文件",
      "下载失败" in _all_scripts and "rm -f" in _all_scripts)
for _k in _ip.INSTALL_PRESETS:
    check(f"★ 预设 {_k} 的脚本仍带 stdin 兜底",
          "</dev/null" in _ip.build_script([_k]))


# ══════════ 费用与免费额度 ══════════
from core import catalog as _cat  # noqa: E402

_c = _cat.instance_cost("e2-micro", "pd-standard", 30, "us-central1",
                        created_at=1700000000, now=1700000000 + 86400 * 3)
check("★ 每小时单价为正", _c["hourly_usd"] > 0)
check("★ 每天 = 每小时 × 24",
      abs(_c["daily_usd"] - _c["hourly_usd"] * 24) < 0.001, str(_c["daily_usd"]))
check("★ 已用费用 = 已用小时 × 每小时",
      abs(_c["used_usd"] - _c["used_hours"] * _c["hourly_usd"]) < 0.01, str(_c["used_usd"]))
check("★ 已用 3 天 ≈ 72 小时", abs(_c["used_hours"] - 72) < 0.1, str(_c["used_hours"]))
check("★ e2-micro + 免费区域 + 30GB 标准盘 → 免费机型", _c["free_tier"] is True)
check("★ 自带口径说明（参考价而非账单）", "GCP 账单" in _c["disclaimer"])

# 免费额度是「按时间」而非「按实例」—— 这一点必须判对
_cat_src = open(os.path.join(BASE_DIR, "core", "catalog.py"), encoding="utf-8").read()
check("★ 免费额度按时间计（当月总小时数），不是「前 1 台免费」",
      "by time, not by instance" in _cat_src and "按时间" in _cat_src)
_hours = _cat.free_tier_hours_in_month()
check("★ 当月小时数在合理区间（28~31 天）",
      28 * 24 <= _hours <= 31 * 24, str(_hours))
check("★ 抢占式不享受免费额度",
      _cat.free_tier_reason("e2-micro", "us-central1", True)[0] is False)
check("★ 非免费区域不享受",
      _cat.free_tier_reason("e2-micro", "asia-east1")[0] is False)
check("★ 磁盘超 30GB 不享受",
      _cat.free_tier_reason("e2-micro", "us-west1", False, "pd-standard", 50)[0] is False)
check("★ 非 e2-micro 机型不享受",
      _cat.free_tier_reason("e2-small", "us-central1")[0] is False)
check("★ 免费地区清单与官方一致",
      _cat.FREE_TIER_REGIONS == ["us-west1", "us-central1", "us-east1"],
      str(_cat.FREE_TIER_REGIONS))
check("★ 所处地有中文可读名",
      "爱荷华" in _cat.region_label("us-central1"), _cat.region_label("us-central1"))
check("★ zone 能归一化成 region",
      _cat.region_of_zone("us-central1-a") == "us-central1")

# 停机只算磁盘费
_run = _cat.instance_cost("e2-micro", "pd-standard", 30, "us-central1", status="RUNNING")
_stop = _cat.instance_cost("e2-micro", "pd-standard", 30, "us-central1", status="TERMINATED")
check("★ 停机状态只计磁盘费（GCP 停机不收算力费）",
      _stop["hourly_usd"] < _run["hourly_usd"] and _stop["compute_hourly_usd"] > 0
      and _stop["hourly_usd"] == _stop["disk_hourly_usd"],
      f"run={_run['hourly_usd']} stop={_stop['hourly_usd']}")
check("★ 未知机型标记为无单价而不是当作 0",
      _cat.instance_cost("未知机型-x", "pd-standard", 30, "us-central1")["priced"] is False)
check("★ 无创建时间时 used 为 None（不拿当前时间冒充）",
      _cat.instance_cost("e2-micro", "pd-standard", 30, "us-central1")["used_usd"] is None)

# ══════════ 代理解析 ══════════
_ok_cases = [
    "1.2.3.4:8080", "1.2.3.4:8080:user:pass", "http://1.2.3.4:8080",
    "https://1.2.3.4:8443", "socks5://user:pass@1.2.3.4:1080",
    "socks5h://proxy.example.com:1080", "socks4://1.2.3.4:1080",
    "socks5 9.9.9.9 1080 u1 p1", "[2001:db8::1]:1080",
]
for _raw in _ok_cases:
    check(f"★ 代理解析接受：{_raw}", _pp(_raw)["ok"] is True, str(_pp(_raw)))
_bad_cases = ["1.2.3.4:99999", "1.2.3.999:8080", "not-an-ip", "1.2.3.4"]
for _raw in _bad_cases:
    check(f"★ 代理解析拒绝非法：{_raw}", _pp(_raw)["ok"] is False)
check("★ SOCKS5 统一走代理端解析 DNS（socks5h，避免本地 DNS 污染）",
      _pp("socks5://1.2.3.4:1080")["proxy_url"].startswith("socks5h://"))
check("★ SOCKS4 带认证被拒并提示改用 socks5",
      _pp("socks4://u:p@1.2.3.4:1080")["ok"] is False
      and "socks5" in _pp("socks4://u:p@1.2.3.4:1080")["error"])
check("★ 支持域名（老实现只认 IPv4 字面量）",
      _pp("socks5h://proxy.example.com:1080")["ok"] is True)
check("★ 代理密码在展示时打码",
      "***" in _mp("socks5h://user:secret@1.2.3.4:1080")
      and "secret" not in _mp("socks5h://user:secret@1.2.3.4:1080"),
      _mp("socks5h://user:secret@1.2.3.4:1080"))
check("★ 类型标签不再误导（socks5 实为代理端解析）",
      _pp("socks5://1.2.3.4:1080")["proxy_type"] == "SOCKS5H")

# ══════════ 存储层 ══════════
_cols = {r["name"] for r in appmod.store.conn.execute("PRAGMA table_info(vm_passwords)")}
check("★ vm 表已加 note 列", "note" in _cols)
check("★ vm 表已加 created_at 列（算已用费用用）", "created_at" in _cols)
check("★ vm 表已加 installs 列（回溯装了什么）", "installs" in _cols)
_acols = {r["name"] for r in appmod.store.conn.execute("PRAGMA table_info(accounts)")}
check("★ accounts 表有 label 列（账号备注）", "label" in _acols)

appmod.store.save_vm("vm-n-1", "1.2.3.4", "pw", 1, "us-central1-a", "e2-micro",
              "ubuntu-2204-lts", "pd-standard", 30, note="备注A",
              created_at=1700000000.0, installs="docker,nps")
check("★ 备注/创建时间/安装项已写入",
      appmod.store.get_vm("vm-n-1")["note"] == "备注A"
      and appmod.store.get_vm("vm-n-1")["installs"] == "docker,nps"
      and appmod.store.get_vm("vm-n-1")["created_at"] == 1700000000.0)
# 关键：创建流程里 save_vm 会被调用多次，第二次不能把备注冲掉
appmod.store.save_vm("vm-n-1", "1.2.3.4", "pw", 1, "us-central1-a", "e2-micro",
              "ubuntu-2204-lts", "pd-standard", 30)
check("★ 二次保存不覆盖已有备注（创建流程会存多次）",
      appmod.store.get_vm("vm-n-1")["note"] == "备注A", repr(appmod.store.get_vm("vm-n-1")["note"]))
check("★ 二次保存不覆盖创建时间",
      appmod.store.get_vm("vm-n-1")["created_at"] == 1700000000.0)
check("★ update_vm_note 命中判断正确",
      appmod.store.update_vm_note("vm-n-1", "新备注") is True
      and appmod.store.get_vm("vm-n-1")["note"] == "新备注")
check("★ update_vm_note 未命中返回 False",
      appmod.store.update_vm_note("不存在的实例-xyz", "x") is False)

# 老库迁移：缺列的老表也要能升上来
import sqlite3 as _sq  # noqa: E402
_old = os.path.join(BASE_DIR, "data", "_mig_test.db")
try:
    os.unlink(_old)
except OSError:
    pass
_cx = _sq.connect(_old)
_cx.execute("""CREATE TABLE vm_passwords(name TEXT PRIMARY KEY, ip TEXT, password TEXT,
   account_id TEXT, zone TEXT, machine_type TEXT, image_key TEXT, disk_type TEXT,
   disk_size_gb INTEGER, updated_at REAL)""")
_cx.execute("INSERT INTO vm_passwords VALUES('old-1','9.9.9.9','secret',1,'z','m','i','d',10,1.0)")
_cx.commit(); _cx.close()
from core.store import Store as _Store  # noqa: E402
_st2 = _Store(_old)
check("★ 老库能自动补齐新列（升级不丢数据）",
      "note" in {r["name"] for r in _st2.conn.execute("PRAGMA table_info(vm_passwords)")}
      and _st2.get_vm("old-1")["password"] == "secret")
_Store(_old)   # 再开一次，迁移必须幂等
check("★ 迁移幂等（重复启动不报错）", True)
os.unlink(_old)

# ══════════ 接口 ══════════
_r = client.get("/api/install_presets")
check("★ GET /api/install_presets 返回 5 项",
      _r.status_code == 200 and len(_r.json()["presets"]) == 5)

appmod.store.save_vm("vm-n-api", "1.2.3.4", "RootPw!9", 1, "us-central1-a", "e2-micro",
              "ubuntu-2204-lts", "pd-standard", 30, note="接口备注")
_r = client.patch("/api/instances/note", json={"name": "vm-n-api", "note": "改过"})
check("★ PATCH /api/instances/note 可用",
      _r.status_code == 200 and appmod.store.get_vm("vm-n-api")["note"] == "改过")
check("★ 超长备注被拒", client.patch("/api/instances/note",
      json={"name": "vm-n-api", "note": "x" * 201}).status_code == 400)
check("★ 缺实例名被拒", client.patch("/api/instances/note",
      json={"name": "", "note": "x"}).status_code == 400)

# root 密码：必须二次验证登录密码
_r = client.post("/api/instances/password", json={"name": "vm-n-api", "password": "错的"})
check("★ 错误登录密码被拒（403）", _r.status_code == 403, str(_r.status_code))
check("★ 拒绝时不泄露 root 密码", "RootPw!9" not in _r.text)
check("★ 空密码被拒", client.post("/api/instances/password",
      json={"name": "vm-n-api", "password": ""}).status_code == 400)
_r = client.post("/api/instances/password", json={"name": "vm-n-api", "password": ADMIN_PW})
check("★ 正确登录密码后返回 root 密码",
      _r.status_code == 200 and _r.json()["root_password"] == "RootPw!9", str(_r.json()))
check("★ 无密码记录的实例返回 404",
      client.post("/api/instances/password",
                  json={"name": "没有这个实例", "password": ADMIN_PW}).status_code == 404)

# 实例列表接口不得直接吐明文密码
_lt_src = open(os.path.join(BASE_DIR, "core", "tasks.py"), encoding="utf-8").read()
check("★ list_all_instances 不再返回 root 密码明文",
      '"root_password"] = vm.get("password")' not in _lt_src
      and '"has_password"' in _lt_src)
check("★ 实例列表带费用与所在地字段",
      'inst["cost"] = catalog.instance_cost' in _lt_src and '"account_label"' in _lt_src)

# 账号接口：备注、代理打码、不外发原始 proxy
appmod.store.add_account("n-a@example.com", "p-n1", "/tmp/n1.json",
                  "socks5://u:TopSecret@1.2.3.4:1080", "SOCKS5", "N段账号")
_r = client.get("/api/accounts").json()
_na = [a for a in _r["accounts"] if a["email"] == "n-a@example.com"]
check("★ 账号接口返回备注", _na and _na[0]["label"] == "N段账号")
check("★ 账号接口不外发明文代理密码",
      _na and "TopSecret" not in json.dumps(_na[0], ensure_ascii=False))
check("★ 账号接口给出代理类型标签与打码展示",
      _na and _na[0]["proxy_type"] == "SOCKS5H" and "***" in _na[0]["proxy_display"])
check("★ 账号接口不再外发原始 proxy 字段", _na and "proxy" not in _na[0])

# ══════════ 前端 ══════════
_ct4 = client.get("/static/console.html").text
check("★ 创建页有机器备注输入", 'v-model.trim="instNote"' in _ct4)
check("★ 创建页安装预设为多选（checkbox 绑定数组）",
      'v-model="installPicked"' in _ct4 and "installPresets" in _ct4)
check("★ 安装卡带选中态样式", 'class="pk"' in _ct4 and ".pk.on{" in _ct4)
check("★ 提交创建时带上备注与安装项",
      "note: this.instNote" in _ct4 and "installs: this.installPicked" in _ct4)

check("★ 实例表含所在地列", "公网 IP / 所在地" in _ct4 and "i.location" in _ct4)
check("★ 实例表含镜像名称列", "{{ i.image ||" in _ct4)
check("★ 实例表含磁盘大小列", "i.disk_size_gb ? i.disk_size_gb+' GB'" in _ct4)
check("★ Root 密码默认掩码显示",
      "••••••••" in _ct4 and 'class="pwd-mask"' in _ct4)
check("★ 显示密码走二次验证接口", "/api/instances/password" in _ct4 and "askReveal" in _ct4)
check("★ 出示后 15 分钟自动隐藏", "15*60*1000" in _ct4)
check("★ 实例表含费用列（每小时/每天/已用）",
      "每小时" in _ct4 and "每天" in _ct4 and "已用" in _ct4)
check("★ 免费机型有专门徽章", "免费机型" in _ct4 and ".badge.free{" in _ct4)
check("★ 备注可就地编辑", "saveNote" in _ct4 and 'v-model="noteDraft"' in _ct4)

check("★ 账号页可加/改备注", "saveAccNote" in _ct4 and "accNoteDraft" in _ct4)
check("★ 账号页显示代理类型徽章", 'class="badge proxy"' in _ct4)
check("★ 账号页区分「直连」与「用代理」", "直连（未用代理）" in _ct4)
check("★ 长邮箱可折叠/展开", "expandedMail" in _ct4 and 'class="mail-tg"' in _ct4)
check("★ 代理协议下拉含 HTTP/HTTPS/SOCKS5/SOCKS4",
      all(x in _ct4 for x in ("SOCKS5H", "SOCKS4", "HTTP 代理")))
check("★ 窄屏对长机器串允许折行（防表格被顶宽）",
      "overflow-wrap:anywhere" in _ct4)
# 又踩了一次「api() 只接一个参数」的坑，用断言锁住
check("★ PATCH/POST 用专用助手而非两参 api()",
      "this.patch('/api/instances/note'" in _ct4
      and "this.post('/api/instances/password'" in _ct4)

# pydantic 会丢弃模型未声明的字段 —— 少写就静默失效，不报错，最难查
_M = appmod.CreateRequest.model_fields
check("★ CreateRequest 声明了 note（否则备注传不到后端，且不报错）",
      "note" in _M, str(list(_M)))
check("★ CreateRequest 声明了 installs（否则勾选的预设静默丢失）",
      "installs" in _M, str(list(_M)))

# 端到端跑一遍 dry-run，确认安装脚本真的被拼进 post_command
_seen = {}
_orig = appmod.tm.submit_create
appmod.tm.submit_create = lambda p: (_seen.update(p), {"ok": True})[1]
try:
    client.post("/api/create", json={"count": 1, "spec": {"machine_type": "e2-micro"},
                                     "dry_run": True, "note": "N段备注",
                                     "installs": ["docker", "nps"],
                                     "post_command": "", "verify_command": ""})
finally:
    appmod.tm.submit_create = _orig
check("★ dry-run 时备注到达后端", _seen.get("note") == "N段备注", repr(_seen.get("note")))
check("★ dry-run 时安装项已归一化", _seen.get("installs") == ["docker", "nps"],
      str(_seen.get("installs")))
_pc = _seen.get("post_command") or ""
check("★ 勾选的预设被拼进 post_command",
      "===== [docker]" in _pc and "===== [nps]" in _pc and "[3x-ui]" not in _pc)
check("★ 未勾选验证命令时自动从预设生成",
      "docker --version" in (_seen.get("verify_command") or ""))

# 自己写了命令时，预设追加而非覆盖
_seen.clear()
appmod.tm.submit_create = lambda p: (_seen.update(p), {"ok": True})[1]
try:
    client.post("/api/create", json={"count": 1, "spec": {"machine_type": "e2-micro"},
                                     "dry_run": True, "installs": ["docker"],
                                     "post_command": "echo 我的命令"})
finally:
    appmod.tm.submit_create = _orig
_pc2 = _seen.get("post_command") or ""
check("★ 用户自写的安装命令不被预设覆盖（预设追加在后）",
      "echo 我的命令" in _pc2 and _pc2.index("echo 我的命令") < _pc2.index("[docker]"))

check("★ favicon 路由已注册（消除每页一个 404）",
      client.get("/favicon.ico").status_code == 200)

print("\n── M. 版本号 / 导航分组 / 按钮排序 ──")

from core import version as ver_mod  # noqa: E402

# ── 版本号：单一来源 ────────────────────────────────────────────────
check("★ core/version.py 定义版本号且符合语义化格式",
      bool(__import__("re").fullmatch(r"\d+\.\d+\.\d+", ver_mod.VERSION)),
      ver_mod.VERSION)
check("★ 版本从 v1.0.1 起算", ver_mod.VERSION >= "1.0.1", ver_mod.VERSION)

# 三处必须一致：FastAPI 元数据 / /api/status / /api/version
check("★ FastAPI 版本号取自 version 模块（不再硬编码）",
      appmod.app.version == ver_mod.VERSION,
      f"fastapi={appmod.app.version} module={ver_mod.VERSION}")

r = client.get("/api/version")
check("★ /api/version 返回 ok", r.status_code == 200 and r.json().get("ok") is True)
_v = r.json()
check("★ /api/version 的版本与模块一致", _v.get("version") == ver_mod.VERSION)
check("★ /api/version 带仓库地址",
      _v.get("repo") == ver_mod.REPO_URL and "github.com" in _v.get("repo", ""))
check("★ /api/version 带反馈地址与更新说明",
      _v.get("issue_url", "").endswith("/issues") and len(_v.get("latest_notes") or []) > 0)
check("★ 更新日志最新一条与当前版本一致",
      ver_mod.CHANGELOG[0]["version"] == ver_mod.VERSION,
      f"{ver_mod.CHANGELOG[0]['version']} vs {ver_mod.VERSION}")

# 未登录也能拿到版本（登录页要显示版本号）
_r0 = client.get("/api/version")
check("★ /api/version 匿名可读（登录页展示需要）", _r0.status_code == 200, str(_r0.status_code))
check("★ /api/version 不泄露账号或环境信息",
      not any(k in _r0.json() for k in ("accounts", "users", "vms_with_password", "key_path")))

login("admin", ADMIN_PW)
_st = client.get("/api/status").json()
check("★ /api/status 与 /api/version 版本一致",
      _st.get("version") == _v.get("version"), f"{_st.get('version')} vs {_v.get('version')}")
check("★ /api/status 也带仓库与更新日志",
      _st.get("repo") == ver_mod.REPO_URL and isinstance(_st.get("changelog"), list))

# ── 导航分组与顺序 ──────────────────────────────────────────────────
_ct3 = client.get("/static/console.html").text
check("★ 导航定义了分组字段",
      "group:'运维'" in _ct3 and "group:'资源'" in _ct3 and "group:'系统'" in _ct3)
check("★ 侧栏按分组渲染（模板 v-for 分组 + 小标题）",
      'v-for="g in navGroups"' in _ct3 and 'class="sb-group"' in _ct3)
check("★ 有 navGroups 计算属性按顺序聚合",
      "navGroups(){" in _ct3 and "this.visibleTabs.forEach" in _ct3)

# 顺序断言：运维主线在前，系统管理在后
_order = ["{id:'create'", "{id:'instances'", "{id:'exec'", "{id:'tasks'",
          "{id:'inspect'", "{id:'accounts'", "{id:'users'", "{id:'profile'"]
_pos = [_ct3.find(x) for x in _order]
check("★ 导航顺序为：创建→实例→执行→任务→资源→账号→用户→设置",
      all(p_ > 0 for p_ in _pos) and _pos == sorted(_pos), str(_pos))
check("★ 创建实例排在第一位（主流程入口）",
      _ct3.find("{id:'create'") < _ct3.find("{id:'accounts'"))

# ── 版本号与仓库地址的展示位 ────────────────────────────────────────
check("★ 侧栏品牌显示版本号", "WEB CONSOLE · v{{ ver.version" in _ct3)
check("★ 侧栏页脚有仓库链接 + 版本徽章",
      'class="sb-link"' in _ct3 and 'class="ver"' in _ct3
      and "ver.repo_name" in _ct3)
check("★ 侧栏页脚链接带 rel=noopener noreferrer（防 tabnabbing）",
      'rel="noopener noreferrer"' in _ct3)
check("★ 个人设置有「关于」卡片（版本/仓库/反馈/更新内容）",
      "关于 <small>" in _ct3 and 'class="repo-link"' in _ct3
      and "about-ul" in _ct3 and "ver.issue_url" in _ct3)
check("★ 启动时并行加载版本信息", "this.loadVersion()" in _ct3)

# 登录页
_lh3 = client.get("/login").text
check("★ 登录页页脚有版本与仓库",
      'class="wow-login-footer"' in _lh3 and 'id="verText"' in _lh3
      and 'id="repoLink"' in _lh3)
check("★ 登录页主动拉取 /api/version", "loadVersion()" in _lh3
      and "fetch('/api/version')" in _lh3)
check("★ 登录页链接同样带 rel=noopener noreferrer",
      'rel="noopener noreferrer"' in _lh3)
_lcs = client.get("/static/login.css").text
check("★ 登录页页脚样式已定义且尊重减少动效",
      ".wow-login-footer" in _lcs and "prefers-reduced-motion" in _lcs)

# ── 按钮排序：分组与分隔线 ──────────────────────────────────────────
check("★ 定义了按钮分隔线样式", ".br{width:1px;height:22px" in _ct3)
check("★ 窄屏分隔线转横向（跟随换行）",
      ".br{width:100%;height:1px;margin:1px 0}" in _ct3)

# 破坏性操作必须与常规操作之间隔一个分隔线
def _sep_between(text, left, right):
    """判断 left 与 right 之间是否夹着一条按钮分隔线"""
    i, j = text.find(left), text.find(right)
    return i > 0 and j > i and '<span class="br"></span>' in text[i:j]


# 按标签页切片，避免同名按钮在别处先被匹配到
def _tab_slice(text, start_tab, end_tab=None):
    i = text.find("tab==='" + start_tab + "'")
    j = text.find("tab==='" + end_tab + "'") if end_tab else len(text)
    return text[i:j] if i >= 0 else ''


_inst = _tab_slice(_ct3, 'instances', 'exec')
check("★ 实例列表：重启与删除之间有分隔线",
      _sep_between(_inst, '🔄 重启</button>', '🗑 删除</button>'))
check("★ 实例列表：同步与全选之间有分隔线",
      _sep_between(_inst, '↻ 同步云端实例', '全选</button>'))

_acct = _tab_slice(_ct3, 'accounts', 'inspect')
check("★ 账号管理：删除勾选前有分隔线",
      _sep_between(_acct, '🔌 测试全部连通性</button>', '🗑 删除勾选</button>'))

_task = _tab_slice(_ct3, 'tasks', 'users')
check("★ 任务日志：清空后端日志前有分隔线",
      _sep_between(_task, '↻ 刷新</button>', '清空后端日志</button>'))

_exec = _tab_slice(_ct3, 'exec', 'tasks')
check("★ 命令执行：清空结果前有分隔线",
      _sep_between(_exec, '↻ 刷新任务</button>', '清空结果</button>'))

# 创建页：危险的全开防火墙不再紧挨极速预设
_create = _tab_slice(_ct3, 'create', 'accounts')
check("★ 创建页：危险动作与极速预设之间已隔开",
      _sep_between(_create, '⚡ 极速部署预设</button>', '🔥 放开全开防火墙</button>'))

# 创建页：危险的全开防火墙不再紧挨极速预设
_i = _ct3.find('<button class="p" @click="quickMode">⚡ 极速部署预设</button>')
_j = _ct3.find('<button class="d" @click="openFirewallMode">🔥 放开全开防火墙</button>')
check("★ 创建页：危险动作与预设之间已隔开",
      _i > 0 and _j > _i and '<span class="br"></span>' in _ct3[_i:_j])

# 实例列表四段划分
_inst = _ct3[_ct3.find("tab==='instances'"):]
check("★ 实例列表按钮分为四段（同步/选择/运维/删除）",
      _inst.count('<span class="br"></span>') >= 3,
      f"分隔线 {_inst.count('<span class="br"></span>')} 条")

# ── 版本号到处散落容易漂移，用测试守住一致性 ────────────────────────
import re as _re  # noqa: E402

_df = open(os.path.join(BASE_DIR, "Dockerfile"), encoding="utf-8").read()
_rd = open(os.path.join(BASE_DIR, "README.md"), encoding="utf-8").read()
_m = _re.search(r"image\.version=\"([^\"]+)\"", _df)
_mb = _re.search(r"version-([\d.]+)-1a73e8", _rd)
check("★ Dockerfile 的 OCI 版本标签与 version 模块一致",
      bool(_m) and _m.group(1) == ver_mod.VERSION,
      f"Dockerfile={_m.group(1) if _m else None} module={ver_mod.VERSION}")
check("★ README 徽章版本与 version 模块一致",
      bool(_mb) and _mb.group(1) == ver_mod.VERSION,
      f"README={_mb.group(1) if _mb else None} module={ver_mod.VERSION}")
check("★ README 里写的仓库地址与 version 模块一致",
      ver_mod.REPO_URL in _rd)
check("★ 页面/接口里的版本号不是硬编码",
      '"2.0.0"' not in open(os.path.join(BASE_DIR, "app.py"), encoding="utf-8").read())

# ── 安装与升级脚本也要报版本 ────────────────────────────────────────
_u3 = open(os.path.join(BASE_DIR, "update.sh"), encoding="utf-8").read()
_i3 = open(os.path.join(BASE_DIR, "install.sh"), encoding="utf-8").read()
check("★ update.sh 升级后打印产品版本",
      "core/version.py" in _u3 and "当前版本 v" in _u3)
check("★ install.sh 安装后打印版本与仓库地址",
      "core/version.py" in _i3 and "GCP-Manager-Web" in _i3)

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
    # 显式指定 APP_DIR：管道模式默认已改为绝对路径（root → /opt/gcp-manager-web），
    # 不再落在 cwd 下。测试要的是「能不能自举出源码」，所以自己指定隔离目录，
    # 不依赖默认值、也不污染 /opt。
    _dst_dir = os.path.join(_tmp, "gcp-manager-web")
    _pipe = _sp.run(
        ["bash", "-c",
         f'cat "{BASE_DIR}/install.sh" | ONLY_FETCH=1 APP_DIR="{_dst_dir}" '
         f'REPO_URL="{BASE_DIR}" bash'],
        cwd=_tmp, capture_output=True, text=True, timeout=180)
    _dst = os.path.join(_dst_dir, "app.py")
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

# ══════════ O. 账号可改代理 / 创建页账号列精简 / 每账号机器数 ══════════
print("\n── O. 账号代理可改 · 账号列精简 · 机器数 ──")

login("admin", admin_pw)
_accs = client.get("/api/accounts").json()["accounts"]
_acc = _accs[0] if _accs else None
_aid = _acc["id"] if _acc else None
_orig_label = (_acc or {}).get("label") or ""
_orig_proxy = None  # 只记「有没有」，明文不回显

if _aid is None:
    check("★ 账号列表非空（后续断言依赖）", False, "库中无账号")
else:
    # ── 未知协议必须被拒（本轮修掉的真实缺陷）──────────────────
    # 老实现 `PROXY_SCHEMES.get(scheme, ptype)` 在协议名不认识时静默回退，
    # 拼错 socks5 → socks9 会被当成 HTTP 代理存下去，直到调 GCP 才炸。
    for _raw, _why in [("socks9://1.2.3.4:1080", "拼错 socks5"),
                       ("ftp://1.2.3.4:1080", "根本不支持的协议"),
                       ("1.2.3.4:1080:u:p", None)]:
        _r = _pp(_raw)
        if _why:
            check(f"★ 未知协议被拒（{_why}）",
                  _r["ok"] is False and _r.get("error"),
                  str(_r))
    check("★ 未知协议的报错里列出可用协议（便于自查）",
          "socks5" in (_pp("socks9://1.2.3.4:1080").get("error") or ""),
          str(_pp("socks9://1.2.3.4:1080").get("error")))
    check("★ 协议名大小写不敏感（SOCKS5:// 合法）",
          _pp("SOCKS5://1.2.3.4:1080")["ok"] is True)

    # ── PATCH /api/accounts/{id} 支持改代理 ─────────────────
    _r = client.patch(f"/api/accounts/{_aid}",
                      json={"proxy": "socks5h://u:pw@127.0.0.1:1080", "proxy_type": "SOCKS5"})
    check("★ 已导入账号可事后改代理（PATCH 接口）",
          _r.status_code == 200 and _r.json().get("ok"), _r.text[:120])
    _a = [x for x in client.get("/api/accounts").json()["accounts"] if x["id"] == _aid][0]
    check("★ 改完后代理生效", _a["proxy_set"] is True, str(_a.get("proxy_set")))
    check("★ 类型归一化为 SOCKS5H", _a["proxy_type"] == "SOCKS5H", str(_a.get("proxy_type")))
    check("★ 账号接口仍不外发明文代理密码",
          "pw@" not in json.dumps(_a) and "***" in (_a.get("proxy_display") or ""),
          _a.get("proxy_display"))

    # 非法代理必须被拒，且不能破坏已有的代理
    _r = client.patch(f"/api/accounts/{_aid}", json={"proxy": "socks9://1.2.3.4:1080"})
    check("★ 改代理时非法值被拒（不是静默存进去）", _r.status_code == 400, str(_r.status_code))
    check("★ 拒绝后给出可读原因", "协议" in _r.json().get("detail", ""), _r.text[:120])
    _a = [x for x in client.get("/api/accounts").json()["accounts"] if x["id"] == _aid][0]
    check("★ 非法值被拒后原代理未被破坏", _a["proxy_type"] == "SOCKS5H", str(_a.get("proxy_type")))
    _r = client.patch(f"/api/accounts/{_aid}", json={"proxy": "socks4://u:p@1.2.3.4:1080"})
    check("★ 改代理时 SOCKS4 带认证同样被拒", _r.status_code == 400, str(_r.status_code))

    # 清空 → 直连
    _r = client.patch(f"/api/accounts/{_aid}", json={"proxy": ""})
    check("★ 可清空代理改为直连", _r.status_code == 200, _r.text[:120])
    _a = [x for x in client.get("/api/accounts").json()["accounts"] if x["id"] == _aid][0]
    check("★ 清空后 proxy_set=False（前端据此显示「直连」）",
          _a["proxy_set"] is False, str(_a.get("proxy_set")))

    # 备注改动别被代理逻辑带坏 + 超长拒绝
    _r = client.patch(f"/api/accounts/{_aid}", json={"label": "O段测试备注"})
    _a = [x for x in client.get("/api/accounts").json()["accounts"] if x["id"] == _aid][0]
    check("★ 改代理的新逻辑没带坏改备注", _a["label"] == "O段测试备注", str(_a.get("label")))
    _r = client.patch(f"/api/accounts/{_aid}", json={"label": "x" * 101})
    check("★ 账号备注超长被拒", _r.status_code == 400, str(_r.status_code))
    _r = client.patch(f"/api/accounts/{_aid}", json={})
    check("★ 空 PATCH 被拒（没有要改的字段）", _r.status_code == 400, str(_r.status_code))
    _r = client.patch("/api/accounts/999999", json={"label": "x"})
    check("★ 改不存在的账号 → 404", _r.status_code == 404, str(_r.status_code))
    client.patch(f"/api/accounts/{_aid}", json={"label": _orig_label})

    # 代理变更要留审计（敏感配置改动）
    _ad = client.get("/api/audit?limit=100").json().get("audit", [])
    check("★ 代理变更写入审计日志",
          any(x.get("action") == "update_account_proxy" for x in _ad), str(len(_ad)))

    # ── 每账号机器数 ─────────────────────────────────────
    check("★ tasks 提供并发统计（不逐账号串行拖慢页面）",
          "def count_instances_per_account" in _lt_src
          and "ThreadPoolExecutor" in _lt_src)
    check("★ 统计区分 live 与 local 两个口径（不合并、不取最大值）",
          "inst_count_live" in _lt_src and "inst_count_local" in _lt_src)
    check("★ 单账号查询失败不影响其它账号",
          "errors.append" in _lt_src.split("def count_instances_per_account", 1)[1][:3000])
    _r = client.get("/api/accounts/instance_counts")
    check("★ /api/accounts/instance_counts 可用", _r.status_code == 200, str(_r.status_code))
    _j = _r.json()
    check("★ 统计返回 counts + 耗时 + 缓存标记",
          isinstance(_j.get("counts"), list) and "elapsed_ms" in _j and "cached" in _j,
          str(sorted(_j.keys())))
    if _j.get("counts"):
        _row = _j["counts"][0]
        check("★ 每条含 account_id / live / local",
              all(k in _row for k in ("account_id", "inst_count_live", "inst_count_local")),
              str(sorted(_row.keys())))
        check("★ 查询失败时 live 为 None 而非冒充 0",
              _row["inst_count_live"] is None or isinstance(_row["inst_count_live"], int),
              repr(_row["inst_count_live"]))
    _a = [x for x in client.get("/api/accounts").json()["accounts"] if x["id"] == _aid][0]
    check("★ /api/accounts 也带机器数（表格可直接渲染）",
          "inst_count_local" in _a and "inst_count_live" in _a)
    check("★ 无备注时 label 回空串（前端 v-if 依赖）",
          isinstance(_a.get("label"), str), repr(_a.get("label")))

# ══════════ 创建页账号列 / 账号页代理编辑入口（前端）══════════
_ct5 = client.get("/static/console.html").text
# 创建页账号表：只要 备注 + 已有机器
check("★ 创建页账号表头只剩「备注」「已有机器」",
      "<th>备注</th><th>已有机器</th>" in _ct5)
# 注意：不能在整个文件里 grep "<th>Project</th>" —— 账号管理页的表头
# 合法地也有这几列。这里按创建页原先那整行表头精确匹配，只断言它被换掉了。
check("★ 创建页不再有 邮箱/Project/代理/密钥文件 列",
      '<th style="width:42px"></th><th>邮箱</th><th>Project</th><th>代理</th>'
      '<th>密钥文件</th>' not in _ct5,
      "创建页旧表头仍在")
# 顺带锁住一个真实缺陷：旧列渲染 `{{ a.proxy }}`，但 /api/accounts 为防泄露
# 已经把 proxy 字段 pop 掉了 → 那一列永远显示 "-"，是死的
check("★ 创建页账号表不再引用被接口抹掉的 a.proxy（否则永远显示 -）",
      "{{ a.proxy ||" not in _ct5 and "{{ a.proxy }}" not in _ct5)
check("★ 创建页账号表保留密钥缺失警告",
      "密钥缺失" in _ct5 and "!a.key_exists" in _ct5)
check("★ 创建页可手动查询机器数",
      "loadAccountCounts" in _ct5 and "查询机器数" in _ct5)
# 机器数只在创建页显示，就不该在每次打开控制台时都向 GCP 打一轮统计
_la = _ct5.split("async loadAccounts(){", 1)[1].split("},", 1)[0]
check("★ 机器数查询不挂在 loadAccounts 上（避免每次开控制台都打 GCP）",
      "loadAccountCounts" not in _la, _la[:120])
check("★ 进入创建页时才自动查机器数",
      "id==='create'" in _ct5 and "loadAccountCounts(false)" in _ct5)

# 账号页：可改代理
check("★ 账号页有「设代理/改代理」入口",
      "'设代理'" in _ct5 or "设代理" in _ct5)
check("★ 账号页代理可就地编辑（输入框 + 协议下拉）",
      'class="proxy-edit"' in _ct5 and 'v-model.trim="proxyDraft"' in _ct5)
check("★ 账号页代理编辑框不回填打码值（否则把 *** 当密码存进去）",
      "this.proxyDraft = ''" in _ct5)
check("★ 账号页提供「清空(改直连)」", "清空(改直连)" in _ct5 and "clearAccProxy" in _ct5)
check("★ 改代理后提示去测连通性（改了不等于通了）",
      "建议点「测试」验证连通性" in _ct5)
check("★ 代理编辑走 PATCH 专用助手（不是两参 api()）",
      "this.patch('/api/accounts/'" in _ct5)

_app = open(os.path.join(BASE_DIR, "app.py"), encoding="utf-8").read()
# ══════════ P. 窄屏重叠 / sshkey 任意文件读取修复 ══════════
print("\n── P. 手机窄屏重叠 · sshkey 任意文件读取 ──")

# ── 1. 手机版「目标账号」表格把下方内容压住（实测重叠 135×9px）──────
# 根因：容器用内联 style="max-height:210px"，内联优先级高于媒体查询，
# 窄屏那条 `.tw{max-height:none}` 盖不住 → 盒子 210px、内容 361px、
# overflow:visible → 内容直接压到下面的「机器备注」上。
check("★ 账号表限高改用 CSS 类而非内联 style（内联会压制媒体查询）",
      'class="tw tw-acc"' in _ct5 and 'style="max-height:210px"' not in _ct5)
check("★ .tw-acc 限高已定义", ".tw-acc{max-height:210px}" in _ct5)
check("★ 窄屏同时放开 .tw 与 .tw-acc 的限高",
      ".tw,.tw-acc{border:0;max-height:none;overflow:visible}" in _ct5)

# ── 2. /api/sshkey/read 任意文件读取（本次修掉的高危）────────────
check("★ sshkey/read 有独立的路径校验函数（不再直接 open 用户给的路径）",
      "def _is_readable_pubkey" in _app)
check("★ 只放行公钥内容（挡住 /etc/passwd、数据库、源码等非公钥文件）",
      "_PUBKEY_PREFIXES" in _app and "ssh-ed25519" in _app
      and "文件内容不是 SSH 公钥" in _app)
check("★ 按文件名拒私钥（id_rsa / *.pem / *.key …）",
      "_PRIVATE_KEY_HINTS" in _app and "'.pem'" in _app and "'.key'" in _app or
      ("_PRIVATE_KEY_HINTS" in _app and ".pem" in _app and ".key" in _app))
check("★ data/ 目录整体保护（含管理员密码与数据库）",
      "拒绝读取 data/ 目录下的文件" in _app)
check("★ 用 realpath 解析后再比对（防符号链接与 ../ 穿越）",
      "os.path.realpath" in _app and "def _is_readable_pubkey" in _app)
check("★ 限制文件大小（公钥不会超过 8KB）",
      "_MAX_PUBKEY_BYTES" in _app and "文件过大" in _app)
check("★ 二进制文件不再抛异常（原来会 UnicodeDecodeError 打 500）",
      "UnicodeDecodeError" in _app and "不是文本格式" in _app)
check("★ 拒绝读取时写审计（有人在探测这个接口）",
      "read_sshkey_denied" in _app)

# 端到端：真的读不出敏感文件
login("admin", admin_pw)
import tempfile as _tf  # noqa: E402
_pw_file = os.path.join(DATA_DIR, "INITIAL_ADMIN.txt")
for _p, _why in [("/etc/passwd", "系统账户文件"),
                 (_pw_file, "管理员密码文件"),
                 (os.path.join(BASE_DIR, "app.py"), "应用源码"),
                 (os.path.join(DATA_DIR, "gcp_web.db"), "数据库")]:
    if not os.path.exists(_p):
        continue
    try:
        _r = client.post("/api/sshkey/read", json={"pubkey_path": _p})
        _code = _r.status_code
    except Exception as _e:
        _code = f"异常 {type(_e).__name__}"
    check(f"★ 端到端拒绝读取{_why}", _code == 400, str(_code))

# 正常功能不能被改坏：白名单目录里的公钥要读得出来
_skdir = os.path.join(DATA_DIR, "ssh_keys")
os.makedirs(_skdir, exist_ok=True)
_good = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPTestKeyOnlyForRegressionXXXX gcp"
_goodp = os.path.join(_skdir, "_regress_ok.pub")
with open(_goodp, "w", encoding="utf-8") as _f:
    _f.write(_good + "\n")
_r = client.post("/api/sshkey/read", json={"pubkey_path": _goodp})
check("★ 正常功能保留：可读取 .pub 公钥",
      _r.status_code == 200 and _r.json().get("public_key", "").startswith("ssh-ed25519"),
      f"{_r.status_code} {_r.text[:80]}")

# 私钥放在白名单目录里也不能读
_privp = os.path.join(_skdir, "_regress_priv")
with open(_privp, "w", encoding="utf-8") as _f:
    _f.write("ssh-rsa AAAAB3NotARealKeyForTest gcp\n")   # 内容伪装成公钥
_r = client.post("/api/sshkey/read", json={"pubkey_path": _privp})
check("★ 私钥即使内容伪装成公钥也被拒（文件名判定）",
      _r.status_code == 400, f"{_r.status_code} {_r.text[:80]}")

_r = client.post("/api/sshkey/read", json={"pubkey_path": os.path.join(_skdir, "..", "INITIAL_ADMIN.txt")})
check("★ 目录穿越写法同样被拒", _r.status_code == 400, str(_r.status_code))

for _p in (_goodp, _privp):
    try:
        os.remove(_p)
    except OSError:
        pass

check("★ viewer 无权调用 sshkey/read（需 operate）",
      True)   # 由 C 段权限矩阵覆盖，这里仅作声明

# ══════════ Q. 防火墙绑定 VPC / 自定义配置穿透 ══════════
print("\n── Q. 防火墙绑定 VPC · 自定义配置真实穿透 ──")

_gp = open(os.path.join(BASE_DIR, "core", "gcp.py"), encoding="utf-8").read()
_tk = open(os.path.join(BASE_DIR, "core", "tasks.py"), encoding="utf-8").read()

# ── 1. 防火墙必须绑定实例所在的 VPC（真实踩坑）─────────────────────
# 现象：用自定义 VPC（jxihegwg）的项目里没有 default 网络，
#   旧代码硬编码 global/networks/default → 404
#   "The resource projects/x/global/networks/default was not found"，
#   而且防火墙是全局资源、与 zone 无关，外层换区重试全是白试。
check("★ ★ 防火墙不再硬编码 DEFAULT_NETWORK",
      "network=DEFAULT_NETWORK, direction=" not in _gp)
check("★ ★ 防火墙函数接收 network 参数",
      "def create_open_firewall_rules(self, ingress=True, egress=True, priority=1000,\n"
      in _gp and "network=""" in _gp)
check("★ 防火墙 URL 由所选 VPC 拼出（含短名归一化）",
      "_network_short_name" in _gp and 'net_url = f"global/networks/{net_name}"' in _gp)
check("★ ★ 防火墙在实例创建成功之后才建立（不再前置阻断）",
      "防火墙是**全局**资源，与 zone 无关" in _gp
      and "self.instance_client.insert(" in _gp
      and _gp.index("self.instance_client.insert(") < _gp.index("self.create_open_firewall_rules("))
check("★ 防火墙失败只警告、不把实例判为失败",
      'base["firewall_ok"] = fw_ok' in _gp and 'base["warning"]' in _gp)
check("★ 建规则前先探测是否已有全放行规则（避免重复建/误改）",
      "def firewall_coverage" in _gp and "0.0.0.0/0" in _gp)
check("★ 只补缺失的方向（不把已收敛的配置重新铺开）",
      'ingress=("INGRESS" in missing)' in _gp and 'egress=("EGRESS" in missing)' in _gp)
check("★ 同名规则若属于别的 VPC，改用带网络后缀的名字（不越权改绑）",
      'name = f"{base_name}-{net_name}"' in _gp)

# ── 2. 网络短名解析 ────────────────────────────────────────────────
from core.gcp import _network_short_name as _nsn  # noqa: E402
check("★ 短名解析：短名原样返回", _nsn("jxihegwg") == "jxihegwg")
check("★ 短名解析：global/networks/ 形式",
      _nsn("global/networks/jxihegwg") == "jxihegwg")
check("★ 短名解析：完整 URL",
      _nsn("https://www.googleapis.com/compute/v1/projects/p/global/networks/jxihegwg")
      == "jxihegwg")
check("★ 短名解析：projects/ 形式",
      _nsn("projects/p/global/networks/jxihegwg") == "jxihegwg")
check("★ 短名解析：空值返回空串", _nsn("") == "" and _nsn(None) == "")

# ── 3. 自定义配置必须真的穿透到 spec ───────────────────────────────
from core.gcp import build_instance_spec as _bis  # noqa: E402
_sp = _bis({
    "machine_type": "e2-highcpu-4", "image_key": "centos-stream-9",
    "disk_type": "pd-standard", "disk_size_gb": 30,
    "region_mode": "single", "region": "asia-south2",
    "network": "jxihegwg", "subnet": "jxihegwg",
    "assign_public_ip": True, "auto_open_firewall": True,
})
check("★ ★ 自定义机型穿透（e2-highcpu-4）",
      _sp["machine_type"] == "e2-highcpu-4", _sp["machine_type"])
check("★ ★ 指定单区穿透（asia-south2）",
      _sp["region"] == "asia-south2", _sp["region"])
check("★ ★ network_url 用所选 VPC（不是 default 兜底）",
      _sp["network_url"] == "global/networks/jxihegwg", _sp["network_url"])
check("★ ★ subnet_url 用所选子网且带正确 region",
      _sp["subnet_url"] == "regions/asia-south2/subnetworks/jxihegwg",
      _sp["subnet_url"])
check("★ 目录内机型标记为 catalog 来源",
      _sp["machine_type_source"] == "catalog", _sp["machine_type_source"])
_spx = _bis({"machine_type": "n2-custom-nonexistent-99"})
check("★ 目录外机型标记为 custom 来源（允许直接填字符串）",
      _spx["machine_type_source"] == "custom", _spx["machine_type_source"])

# 未指定 region 时不得把 region_mode 带进 region 字段
_sp2 = _bis({"machine_type": "e2-micro", "network": "vpc-a"})
check("★ 未指定 region 时给兜底而不是空串", bool(_sp2["region"]), _sp2["region"])
check("★ 显式指定 network 时如实使用（不做 default 兜底覆盖）",
      _sp2["network_url"] == "global/networks/vpc-a", _sp2["network_url"])
_sp3 = _bis({})
check("★ 未指定 network 时兜底为 default", 
      _bis({"machine_type": "e2-micro"})["network_url"] == "global/networks/default",
      _bis({"machine_type": "e2-micro"})["network_url"])
check("★ 完全空 spec 也能兜底出完整 URL",
      _sp3["network_url"].startswith("global/networks/")
      and "/subnetworks/" in _sp3["subnet_url"],
      f"{_sp3['network_url']} | {_sp3['subnet_url']}")

# ── 4. 任务日志要能看见实际用的 VPC（否则 404 排查只能靠猜）──────
check("★ ★ 任务日志打印网络与子网",
      "网络={spec.get('network')" in _tk and "指定区域=" in _tk)

# ── 5. 面板默认值：极速部署不得静默打开全开放防火墙 ────────────────
check("★ 极速部署预设保持防火墙收敛（不打开全开放）",
      "保持默认收敛" in _ct5 and "Object.assign(this.spec, EMPTY_SPEC(), keep)" in _ct5)
check("★ 开启全开放防火墙必须二次确认（弹窗说明风险）",
      "openFirewallMode()" in _ct5 and "确认开启" in _ct5
      and "公网暴露面最大" in _ct5)

# ══════════ R. 后台任务的终态兜底 ══════════
print("\n── R. 后台任务必须写终态（不能卡在运行中）──")

_tk2 = open(os.path.join(BASE_DIR, "core", "tasks.py"), encoding="utf-8").read()

# 背景：任务跑在裸 daemon 线程里，run() 抛异常会静默杀死线程，
# 任务状态永远停在 running —— 而实际操作可能已经生效
# （实测 delete 真删掉了实例，界面却一直显示「运行中」）。
check("★ ★ 实例动作任务有异常兜底", "completed = _run_action()" in _tk2
      and "任务异常：" in _tk2)
check("★ ★ 兜底里保证写终态（不只是打日志）",
      "任务未正常结束（详见日志）" in _tk2)
check("★ ★ 命令执行任务有异常兜底", "_run_execute_inner" in _tk2
      and "执行异常：" in _tk2)
check("★ ★ 刷新任务有异常兜底", "_refresh_inner" in _tk2 and "刷新异常：" in _tk2)
check("★ 创建任务本就有兜底（不要退化）",
      "任务异常：{exc}" in _tk2 and "_run_create_batch" in _tk2)
check("★ 兜底里用 store.conn.commit()（Store 没有 commit() 方法）",
      "self.store.commit()" not in _tk2)

# 真跑一次：让底层抛异常，任务必须变成 failed 而不是停在 running
import threading as _th  # noqa: E402
_esc = []
_old_hook = _th.excepthook
_th.excepthook = lambda a: _esc.append(f"{a.exc_type.__name__}: {a.exc_value}")


class _BoomGCP:
    def __init__(self, *a, **kw):
        pass

    def list_instances(self):
        return []

    def start_instance(self, z, n):
        raise RuntimeError("模拟瞬时故障")

    def stop_instance(self, z, n):
        raise RuntimeError("模拟瞬时故障")

    def reset_instance(self, z, n):
        raise RuntimeError("模拟瞬时故障")

    def delete_instance(self, z, n):
        raise RuntimeError("模拟瞬时故障")


_orig_acct = tmm.account_service
tmm.account_service = lambda acc: _BoomGCP()
try:
    tmm.store.save_vm("_rt_test_vm", "1.2.3.4", "Pw!x", ACC_ID, "us-central1-a",
                     "e2-micro", "ubuntu-minimal-2204", "pd-standard", 30)
    _r = client.post("/api/instance_action",
                     json={"action": "delete", "targets": ["_rt_test_vm"]}).json()
    _tid = _r.get("task_id")
    _st = "running"
    for _ in range(30):
        time.sleep(0.5)
        _tt = client.get(f"/api/tasks/{_tid}").json().get("task") or {}
        _st = _tt.get("status")
        if _st in ("done", "failed", "error", "cancelled"):
            break
    check("★ ★ 底层抛异常时任务写成终态（不再停留在 running）",
          _st == "failed", f"status={_st}")
    check("★ ★ 异常没有逃逸出后台线程（会被静默吞掉）", not _esc, str(_esc[:2]))
finally:
    tmm.account_service = _orig_acct
    _th.excepthook = _old_hook
    try:
        tmm.store.conn.execute("DELETE FROM vms WHERE name=?", ("_rt_test_vm",))
        tmm.store.conn.commit()
    except Exception:
        pass

# ══════════ S. 安全审计修复回归 ══════════
print("\n── S. 安全审计修复回归 ──")

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
      'peer = (request.client.host if request.client else "")' in _app
      and "if _TRUSTED_PROXIES and _ip_in_trusted(peer):" in _app)

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
      # audit 已改为服务端分页：page_size 上限 200，向后兼容的 limit 上限 1000
      "page_size: int = Query(5, ge=1, le=200)" in _app
      and "limit: int = Query(0, ge=0, le=1000)" in _app
      and "Query(500, ge=1, le=5000)" in _app)
check("★ ★ 操作审计是服务端分页（不再一次拉 200 条）",
      "def api_audit(request: Request, page: int = Query(1, ge=1)," in _app
      and "count_audit()" in _app
      and "get_audit(page_size, (page - 1) * page_size)" in _app)
check("★ 用户名校验在服务端做（前端正则可绕过）",
      "用户名只能包含字母、数字、下划线、点、横线或 @" in
      open(os.path.join(BASE_DIR, "core", "users.py"), encoding="utf-8").read())

# ── 6. 认证与并发 ─────────────────────────────────────────────────
check("★ ★ SSH 不再静默信任任何主机密钥（原 AutoAddPolicy）",
      "set_missing_host_key_policy(paramiko.AutoAddPolicy())" not in _ssh
      and "_tofu_policy_cls" in _ssh
      and "MissingHostKeyPolicy" in _ssh)
check("★ ★ 验证码用加密安全随机源",
      "secrets.choice(_CAPTCHA_ALPHABET)" in _auth
      and "random.choice(_CAPTCHA_ALPHABET)" not in _auth)
check("★ 验证码清理持锁（原为数据竞争）",
      # 不用固定长度窗口找锁：_gc 的注释长短会变，窗口会误判。
      # 改为取出整个 _gc 函数体再找 with self._lock:
      "_gc" in _auth and "with self._lock:" in
      _auth.split("def _gc")[1].split("\n    def ")[0])
check("★ 验证码表有容量上限（未认证接口，防内存膨胀）",
      "MAX_ITEMS" in _auth and "MAX_ITEMS = 20000" in _auth)
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
login("admin", ADMIN_PW)
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

# ── 9. WebSocket 也要受强制改密约束（PoC 实测过：HTTP 拦、WS 曾放行）─────
login("admin", ADMIN_PW)     # 上一段结束时是 mc_test 身份，建号需要管理员
_r = client.post("/api/users", json={"username": "ws_mc", "password": "Init@GCP2026",
                                     "role": "operator", "must_change": True})
check("创建用于 WS 测试的待改密账号", _r.status_code == 200, _r.text[:100])
switch("ws_mc", "Init@GCP2026")
check("WS 用例：确认处于未改密状态",
      client.get("/api/auth/me").json()["user"]["must_change_password"] is True)
_ws_blocked = False
try:
    with client.websocket_connect("/ws/logs"):
        pass
except Exception:
    _ws_blocked = True
check("★ ★ 未改密用户连 /ws/logs 被拒（HTTP 中间件管不到 WS 握手）", _ws_blocked)
check("★ WS 端点自身带 must_change 判断",
      'await ws.close(code=4403)' in _app
      and "WebSocket 是独立的握手路径" in _app)

# ═══════════════════════════════════════════════════════════════════════════
# T 段：本轮迭代（审计分页 / 代理探测 / 命令执行页机器列表 / 状态中文
#       + 顺带查出的两个前端缺陷：代理密码打码、fmtTime 重复定义）
# ═══════════════════════════════════════════════════════════════════════════
print("\n" + "-" * 76)
print("T 段：本轮 4 项改动 + 2 项顺带修复")
print("-" * 76)

_html = open(os.path.join(BASE_DIR, "static", "console.html"), encoding="utf-8").read()
_gcp_src = open(os.path.join(BASE_DIR, "core", "gcp.py"), encoding="utf-8").read()

# 前面的 WS 用例把会话切成了「未改密用户」，先切回管理员，
# 否则 T 段的 /api/audit 会被 must_change_password 中间件 403 掉。
login("admin", ADMIN_PW)
check("★ T0 已切回管理员身份（T 段前置条件）",
      client.get("/api/status").json().get("ok") is True)

# ── T1. 操作审计分页 ──────────────────────────────────────────────────────
_r = client.get("/api/audit?page=1&page_size=5").json()
check("★ T1 审计分页：默认每页 5 条", len(_r.get("audit", [])) == 5, str(len(_r.get("audit", []))))
check("★ T1 审计分页：响应带 total/page/pages/page_size",
      all(k in _r for k in ("total", "page", "pages", "page_size")),
      str(sorted(_r.keys())))
check("★ T1 审计分页：页数与总数自洽",
      _r["pages"] == max(1, -(-_r["total"] // 5)), f"{_r['pages']} vs {_r['total']}")
_oob = client.get("/api/audit?page=99999&page_size=5").json()
check("★ T1 审计分页：越界页码被夹回合法范围", 1 <= _oob["page"] <= _oob["pages"])
check("★ T1 审计分页：page=0 / 超大 page_size 被拒",
      client.get("/api/audit?page=0&page_size=5").status_code == 422
      and client.get("/api/audit?page=1&page_size=99999").status_code == 422)
check("★ T1 审计分页：兼容旧调用 ?limit=N", "audit" in client.get("/api/audit?limit=10").json())
check("★ T1 前端默认每页 5 条 + 有翻页控件",
      "auditPageSize:5" in _html and "上一页" in _html and "下一页" in _html and "跳转" in _html)
check("★ T1 前端不再硬拉 200 条", "?limit=200" not in _html)

# ── T2. 账号代理探测 ──────────────────────────────────────────────────────
check("★ T2 后端有 test_proxy 实现", "def test_proxy(" in _gcp_src)
check("★ T2 有 /api/accounts/{id}/test_proxy 路由",
      "/api/accounts/{acc_id}/test_proxy" in _app)
check("★ T2 前端有「测代理」「测试全部代理」",
      "测代理" in _html and "测试全部代理" in _html
      and "testProxyAccount" in _html and "testAllProxies" in _html)
check("★ ★ T2 探测目标硬编码为模块常量（不接受请求方 URL → 无 SSRF）",
      'PROXY_TEST_URL = "https://www.googleapis.com/discovery/v1/apis"' in _gcp_src)
try:
    from core import gcp as _g
    check("★ T2 非法代理配置被拒（不发外连）", _g.test_proxy("不是代理", "HTTPS")["ok"] is False)
    check("★ ★ T2 不存在的代理 → 探测失败（证明真发请求）",
          _g.test_proxy("127.0.0.1:9", "HTTPS", timeout=3)["ok"] is False)
    check("★ T2 未配置代理时标注 empty=True（不冒充代理可用）",
          _g.test_proxy("", "HTTPS", timeout=3).get("empty") is True)
    # 顺带修复：代理密码打码（原来 host:port:user:pass 完全不遮）
    check("★ ★ 代理密码打码：host:port:user:pass 形态",
          _g.mask_proxy("1.2.3.4:8080:user:secretPw") == "1.2.3.4:8080:user:***")
    check("★ ★ 代理密码打码：scheme://user:pass@host 形态",
          _g.mask_proxy("socks5h://u:secretPw@h:1080") == "socks5h://u:***@h:1080")
    check("★ 无密码的 host:port 不误伤", _g.mask_proxy("1.2.3.4:8080") == "1.2.3.4:8080")
except Exception as _e:
    check(f"★ T2 代理探测逻辑可调用（{type(_e).__name__}）", False, str(_e)[:80])

# ── T3. 命令执行页机器列表 ────────────────────────────────────────────────
try:
    _exec_tpl = _html.split("tab==='exec'")[1].split("tab==='tasks'")[0]
except IndexError:
    _exec_tpl = ""
check("★ ★ T3 命令执行页有「目标实例」卡片", "目标实例" in _exec_tpl)
check("★ ★ T3 命令执行页渲染实例列表（v-for instances）", 'v-for="i in instances"' in _exec_tpl)
check("★ T3 复选框与实例列表页共用 checkedInst",
      ':value="i.name" v-model="checkedInst"' in _exec_tpl)
check("★ T3 有全选/全不选/只选运行中", all(t in _exec_tpl for t in ("全选", "全不选", "只选运行中")))
check("★ T3 空列表有引导文案", "暂无实例" in _exec_tpl)

# ── T4. 实例状态中文 ──────────────────────────────────────────────────────
check("★ T4 有 vmStatusLabel", "vmStatusLabel(s){" in _html)
_zh = {"RUNNING": "运行中", "PROVISIONING": "创建中", "STAGING": "准备中", "STOPPING": "停止中",
       "STOPPED": "已停止", "SUSPENDING": "挂起中", "SUSPENDED": "已挂起",
       "REPAIRING": "修复中", "TERMINATED": "已终止"}
_body = _html.split("vmStatusLabel(s){")[1].split("},")[0].replace(" ", "")
check("★ ★ T4 九种 GCP 状态全部有中文映射",
      all(f"{k}:'{v}'" in _body for k, v in _zh.items()),
      str([k for k, v in _zh.items() if f"{k}:'{v}'" not in _body]))
check("★ T4 实例列表状态列改用中文",
      "vmStatusLabel(i.status)" in _html and _html.count("vmStatusLabel(i.status)") >= 3)

# ── T5. 顺带修复：fmtTime 重复定义（数字时间戳被打回原形）─────────────────
import re as _re
_t5_cnt = len(_re.findall(r"\n    fmtTime\(", _html))
check("★ ★ T5 fmtTime 只定义一次（原来定义了两次，后者覆盖前者）",
      _t5_cnt == 1, f"实际 {_t5_cnt} 处")
check("★ ★ T5 fmtTime 同时支持数字时间戳与 ISO 字符串",
      "t < 1e12 ? t*1000 : t" in _html.replace(" ", " ")
      or ("<1e12" in _html.replace(" ", "") and "Date.parse" in _html))


# ═══════════════════════════════════════════════════════════════════════════
# U 段：安装/升级路径一致性（用户按文档升级报 cd: 没有那个文件或目录）
# ═══════════════════════════════════════════════════════════════════════════
print("\n" + "-" * 76)
print("U 段：安装路径与文档一致性")
print("-" * 76)

_inst = open(os.path.join(BASE_DIR, "install.sh"), encoding="utf-8").read()
_upd = open(os.path.join(BASE_DIR, "update.sh"), encoding="utf-8").read()
_rd = open(os.path.join(BASE_DIR, "README.md"), encoding="utf-8").read()

# ── U1. install.sh：默认安装目录必须是绝对路径，不能再依赖 $PWD ──
_inst_code = "\n".join(l for l in _inst.split("\n")
                            if not l.lstrip().startswith("#"))
check("★ ★ U1 install.sh 不再把 $PWD/gcp-manager-web 当默认（代码行，不含注释）",
      "SRC_DIR:-$PWD/gcp-manager-web" not in _inst_code
      and '$PWD/gcp-manager-web' not in _inst_code,
      "仍出现在代码里")
check("★ ★ U1 管道模式 root 默认装到 /opt/gcp-manager-web（与 README 一致）",
      'APP_DIR="/opt/gcp-manager-web"' in _inst)
check("★ U1 非 root 默认装到 $HOME/gcp-manager-web（/opt 不可写时不报错）",
      'APP_DIR="${HOME:-$PWD}/gcp-manager-web"' in _inst)
check("★ U1 在克隆仓库里执行仍是就地安装",
      'APP_DIR="$SRC_DIR"' in _inst)
check("★ U1 APP_DIR 显式指定优先级最高",
      'if [ -n "${APP_DIR:-}" ]; then' in _inst)
check("★ U1 安装目录落成绝对路径（避免后续 cd 后相对路径失效）",
      "落成绝对路径" in _inst or 'APP_DIR="$PWD/$APP_DIR"' in _inst)

# ── U2. install.sh：写路径标记 + 收尾打印 ──
check("★ ★ U2 install.sh 把安装路径写入标记文件",
      "INSTALL_MARKER" in _inst and "/etc/gcp-manager-web.path" in _inst)
check("★ U2 收尾信息打印「安装目录」",
      'printf "  安装目录 ' in _inst)
check("★ U2 收尾打印的升级命令用的是真实 APP_DIR（非写死）",
      'printf "    升级      bash %s/update.sh' in _inst)

# ── U3. update.sh：部署目录自动定位 ──
check("★ ★ U3 update.sh 有 locate_app_dir 定位函数", "locate_app_dir()" in _upd)
check("★ ★ U3 定位覆盖三条路径：标记文件 / 常见位置 / 浅层搜索",
      "/etc/gcp-manager-web.path" in _upd and "/opt/gcp-manager-web" in _upd
      and "-maxdepth 4 -name app.py" in _upd)
check("★ U3 浅层搜索排除备份与临时目录（不误认 .bak/.old）",
      ".bak" in _upd and ".old" in _upd and "/tmp" in _upd)
check("★ U3 定位结果必须同时有 app.py 与 core/version.py（防认错目录）",
      'core/version.py" ]' in _upd)
check("★ ★ U3 确实没装过时给出「更新是升级通道、不能代替首次安装」的指引",
      "不能代替首次安装" in _upd or "升级通道" in _upd)
check("★ U3 指引里含首次安装命令与查找命令",
      "install.sh | bash" in _upd and "gcp-manager-web.path" in _upd)
check("★ U3 该分支退出码非 0", "    exit 1\n  fi\nfi\ncd \"$APP_DIR\"" in _upd
      or _upd.count("exit 1") >= 2)
# 定位必须发生在「报错退出」之前 —— 原实现是直接 die，用户看不到任何帮助
_p_loc = _upd.find("locate_app_dir()")
_p_die = _upd.find("找不到任何已部署的实例")
check("★ ★ U3 定位逻辑在报错之前（用户不会只看到一句「找不到 app.py」）",
      0 < _p_loc < _p_die, f"locate@{_p_loc} die@{_p_die}")
check("★ U3 老的裸 die 已移除", 'die "在 $APP_DIR 找不到 app.py' not in _upd)

# ── U4. README：安装目录说明 + 升级章节自洽 ──
check("★ ★ U4 README 有「安装目录」对照表（说明不同执行方式装到哪）",
      "**安装目录（很重要，升级时要用）**" in _rd and "`/opt/gcp-manager-web`" in _rd)
check("★ U4 README 说明路径写入 /etc/gcp-manager-web.path",
      "/etc/gcp-manager-web.path" in _rd)
check("★ U4 README 升级章节给了「先确认装在哪」的查找命令",
      'cat /etc/gcp-manager-web.path 2>/dev/null' in _rd)
check("★ U4 README 明说 update.sh 是升级通道、前提是已装过",
      "升级通道" in _rd)
check("★ U4 README 不再出现「管道模式装到 ./gcp-manager-web」的旧描述",
      "会把源码下载到 `./gcp-manager-web`" not in _rd)
check("★ U4 README 回滚章节不再写死路径",
      'cd "$(cat /etc/gcp-manager-web.path' in _rd)

# ── U5. 两个脚本语法与版本一致性 ──
import subprocess as _sp
_r1 = _sp.run(["bash", "-n", os.path.join(BASE_DIR, "install.sh")],
              capture_output=True, text=True)
_r2 = _sp.run(["bash", "-n", os.path.join(BASE_DIR, "update.sh")],
              capture_output=True, text=True)
check("★ U5 install.sh 通过 bash -n", _r1.returncode == 0, _r1.stderr[:120])
check("★ U5 update.sh 通过 bash -n", _r2.returncode == 0, _r2.stderr[:120])


print("\n" + "=" * 76)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("   -", f)
print("=" * 76)
sys.exit(1 if FAIL else 0)
