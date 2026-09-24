# -*- coding: utf-8 -*-
"""
GCP Manager Web 后端（FastAPI）

启动：
    python app.py                      # 默认 0.0.0.0:8000
    PORT=9000 python app.py
    python -m uvicorn app:app --host 0.0.0.0 --port 8000

鉴权：所有 /api/* 与页面默认需要登录；登录需 用户名 + 密码 + 图形验证码。
角色：admin（全部）/ operator（运维）/ viewer（只读）。
"""
import asyncio
import json
import os
import stat
import sys
import time

from fastapi import (FastAPI, UploadFile, File, Form, HTTPException, Request,
                     WebSocket, WebSocketDisconnect, Cookie, Response)
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from core import catalog                                   # noqa: E402
from core import version as ver                            # noqa: E402
from core import install_presets as presets                # noqa: E402
from core import gcp as gcp_mod                            # noqa: E402
from core import auth as auth_mod                          # noqa: E402
from core.auth import (captcha_store, login_guard, PERMISSIONS, ROLE_ADMIN,  # noqa: E402
                       ROLE_LABELS, ROLES, ROLE_OPERATOR, ROLE_VIEWER,
                       password_strength, random_password)
from core.store import Store                               # noqa: E402
from core.users import UserStore                           # noqa: E402
from core.tasks import TaskManager                         # noqa: E402
from core.gcp import GCPService, build_instance_spec       # noqa: E402
from core import inspect as gcp_inspect                    # noqa: E402
from core import ssh as ssh_mod                            # noqa: E402

# 默认配置一致性自检
# 「全开放防火墙」默认关闭：不自动放开 0.0.0.0/0，避免无意识的公网暴露。
# 省钱项默认开启：禁用 Ops Agent、无备份、无快照时间表、关闭删除保护。
# 这些默认值直接决定安全暴露面与是否意外扣费，改动需谨慎。
assert catalog.DEFAULT_CONFIG["auto_open_firewall"] is False, \
    "全开放防火墙默认必须为 False（不自动放开 0.0.0.0/0）"
for _k in ("disable_ops_agent", "no_backup", "no_snapshot_schedule"):
    assert catalog.DEFAULT_CONFIG[_k] is True, f"省钱默认值 {_k} 应为 True"
assert catalog.DEFAULT_CONFIG["deletion_protection"] is False, \
    "删除保护默认应为关闭（便于回收，避免持续计费）"

# 数据目录可通过环境变量覆盖，便于测试/多实例隔离，
# 避免测试脚本误删生产库（历史缺陷：tests_e2e.py 直接删除 data/gcp_web.db，
# 导致运行中的服务持有已删除 inode，表现为"数据凭空消失"）
DATA_DIR = os.environ.get("GCPWEB_DATA_DIR") or os.path.join(BASE_DIR, "data")
KEY_DIR = os.path.join(DATA_DIR, "keys")
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(KEY_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

COOKIE_NAME = "gcp_sid"

store = Store(os.path.join(DATA_DIR, "gcp_web.db"))
users_store = UserStore(store)
tm = TaskManager(store)

app = FastAPI(title=ver.APP_NAME, version=ver.VERSION,
              description="GCP 批量管理 Web 版 — 自定义服务器配置 + Vue3 响应式控制台 + 登录鉴权"
                          f"（{ver.REPO_URL}）")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# 首次启动引导：没有任何用户时创建 admin 并输出随机初始密码
# ---------------------------------------------------------------------------
def bootstrap_admin():
    if users_store.count_users() > 0:
        return
    pw = random_password(16)
    users_store.create_user("admin", pw, role=ROLE_ADMIN,
                            display_name="超级管理员", must_change=True,
                            created_by="bootstrap")
    path = os.path.join(DATA_DIR, "INITIAL_ADMIN.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("GCP Manager Web 初始管理员账号\n")
        f.write("=" * 44 + "\n")
        f.write(f"用户名: admin\n密码:   {pw}\n")
        f.write(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("首次登录后请立即修改密码；本文件仅在初始化时生成一次。\n")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    print("\n" + "=" * 66)
    print("  [初始化] 已创建管理员账号（首次启动）")
    print(f"  用户名: admin")
    print(f"  密码:   {pw}")
    print(f"  已写入: {path}")
    print("  请立即登录并修改密码。")
    print("=" * 66 + "\n")


bootstrap_admin()


# ---------------------------------------------------------------------------
# 鉴权：白名单之外的页面与接口一律要求登录
# ---------------------------------------------------------------------------
PUBLIC_PATHS = {
    "/login", "/api/auth/login", "/api/auth/captcha", "/api/auth/logout",
    "/api/auth/me", "/favicon.ico",
    # 登录页要在未登录状态下显示版本号与仓库地址，故此接口匿名可读。
    # 只回版本、仓库、更新日志，不含任何账号或环境信息。
    "/api/version",
}
PUBLIC_PREFIXES = ("/static/",)


def is_public(path):
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def client_ip(request):
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "-")


def require(request, perm):
    """登录后校验权限点；不通过直接抛 403"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "未登录")
    allowed = PERMISSIONS.get(perm, set())
    if user["role"] not in allowed:
        raise HTTPException(403, f"当前角色（{ROLE_LABELS.get(user['role'], user['role'])}）无权执行此操作")
    return user


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if is_public(path):
        return await call_next(request)

    token = request.cookies.get(COOKIE_NAME)
    if not token:
        bearer = request.headers.get("authorization") or ""
        if bearer.lower().startswith("bearer "):
            token = bearer[7:].strip()

    sess = users_store.get_session(token) if token else None
    if not sess:
        if path.startswith("/api/") or request.url.path.startswith("/ws"):
            return JSONResponse({"ok": False, "error": "未登录或会话已过期", "code": "unauthorized"},
                                status_code=401)
        return RedirectResponse("/login", status_code=302)

    request.state.user = sess
    request.state.token = token
    request.state.must_change = bool(sess.get("must_change"))
    return await call_next(request)


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str
    captcha_id: str | None = None
    captcha_code: str | None = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str | None = None      # 留空则自动生成
    role: str = ROLE_OPERATOR
    display_name: str | None = ""
    must_change: bool | None = True


class UpdateUserRequest(BaseModel):
    role: str | None = None
    display_name: str | None = None
    disabled: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str | None = None  # 留空则自动生成
    must_change: bool | None = True


class SpecModel(BaseModel):
    machine_type: str | None = None
    image_key: str | None = None
    disk_type: str | None = None
    disk_size_gb: int | None = None
    region_mode: str | None = "auto_free"
    region: str | None = None
    regions: list[str] | None = None
    max_per_region: int | None = 4
    network: str | None = "default"
    subnet: str | None = "default"
    network_tier: str | None = "STANDARD"
    tags: list[str] | str | None = None
    assign_public_ip: bool | None = True
    auto_open_firewall: bool | None = None
    disable_ops_agent: bool | None = True
    no_backup: bool | None = True
    no_snapshot_schedule: bool | None = True
    no_resource_policy: bool | None = True
    deletion_protection: bool | None = False
    preemptible: bool | None = False
    spot: bool | None = False
    labels: dict | None = None


class CreateRequest(BaseModel):
    account_ids: list[int | str] | None = None
    count: int = 1
    spec: SpecModel | None = None
    login_mode: str | None = "root_password"
    ssh_public_key: str | None = ""
    root_password: str | None = ""
    post_command: str | None = ""
    verify_command: str | None = ""
    # 机器备注（创建后可在实例列表修改）。
    # 注意：pydantic 会**丢弃**模型里没声明的字段，前端传了也到不了后端 ——
    # 少写这两个字段会导致备注和安装项静默消失（不是报错，更难发现）。
    note: str | None = ""
    # 创建后自动安装的预设 key 列表（docker / 3x-ui / nps / hermes / ekko）
    installs: list[str] | None = None
    concurrency: int | None = 3
    account_workers: int | None = 1
    retry_count: int | None = 2
    ssh_timeout: int | None = 300
    idle_timeout: int | None = 180
    command_timeout: int | None = 1800
    verify_timeout: int | None = 180
    dry_run: bool | None = False


class ExecuteRequest(BaseModel):
    command: str
    targets: list[str] | None = None
    all: bool | None = True
    concurrency: int | None = 10
    command_timeout: int | None = 600
    idle_timeout: int | None = 120


class ActionRequest(BaseModel):
    action: str
    targets: list[str]


def body(req):
    return req.model_dump() if hasattr(req, "model_dump") else req.dict()


def _cfg_key(user_id):
    """每个用户独立保存自己的默认创建配置，避免互相覆盖"""
    return f"create_config:user:{user_id}"


# ═══════════════════════════════════════════════════════════════════════════
# 鉴权接口
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/api/auth/captcha")
def api_captcha():
    cid, code = captcha_store.new()
    return {"ok": True, "captcha_id": cid, "image": captcha_store.render_png(code),
            "expires_in": auth_mod.CAPTCHA_TTL}


@app.get("/api/auth/me")
def api_me(request: Request):
    sess = users_store.get_session(request.cookies.get(COOKIE_NAME) or "")
    if not sess:
        return {"ok": True, "authenticated": False}
    perms = [p for p, roles in PERMISSIONS.items() if sess["role"] in roles]
    u = users_store.get_user(user_id=sess["user_id"]) or {}
    return {"ok": True, "authenticated": True,
            "user": {"id": sess["user_id"], "username": sess["username"],
                     "role": sess["role"], "role_label": ROLE_LABELS.get(sess["role"], sess["role"]),
                     "display_name": u.get("display_name", ""),
                     "last_login": u.get("last_login"),
                     "must_change_password": bool(sess.get("must_change"))},
            "permissions": perms, "roles": list(ROLES)}


@app.post("/api/auth/login")
def api_login(req: LoginRequest, request: Request, response: Response):
    ip = client_ip(request)
    ok, why = login_guard.check(req.username, ip)
    if not ok:
        users_store.audit(req.username, ip, "login", detail=why, ok=False)
        raise HTTPException(429, why)

    # 验证码校验必须通过，且无论成败都已消费，防重放
    cap_ok, cap_why = captcha_store.verify(req.captcha_id, req.captcha_code)
    if not cap_ok:
        login_guard.fail(req.username, ip)
        users_store.audit(req.username, ip, "login", detail=f"验证码失败：{cap_why}", ok=False)
        raise HTTPException(400, cap_why)

    user, why = users_store.verify_login(req.username, req.password)
    if not user:
        login_guard.fail(req.username, ip)
        users_store.audit(req.username, ip, "login", detail=why, ok=False)
        raise HTTPException(401, why)

    login_guard.reset(req.username, ip)
    users_store.mark_login(user["id"], ip)
    token, expires = users_store.create_session(
        user, ip, request.headers.get("user-agent", ""))
    users_store.audit(user["username"], ip, "login", detail="登录成功")

    perms = [p for p, roles in PERMISSIONS.items() if user["role"] in roles]
    resp = JSONResponse({
        "ok": True, "token": token,
        "user": {"id": user["id"], "username": user["username"], "role": user["role"],
                 "role_label": ROLE_LABELS.get(user["role"], user["role"]),
                 "display_name": user.get("display_name", ""),
                 "must_change_password": bool(user.get("must_change_password"))},
        "permissions": perms,
    })
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax",
                    max_age=auth_mod.SESSION_TTL, path="/")
    return resp


@app.post("/api/auth/logout")
def api_logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    sess = users_store.get_session(token) if token else None
    if sess:
        users_store.audit(sess["username"], client_ip(request), "logout")
        users_store.revoke_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.post("/api/auth/change_password")
def api_change_password(req: ChangePasswordRequest, request: Request):
    sess = request.state.user
    u = users_store.get_user(user_id=sess["user_id"])
    if not u or not auth_mod.verify_password(req.old_password, u["password_hash"], u["salt"]):
        users_store.audit(sess["username"], client_ip(request), "change_password",
                          detail="原密码错误", ok=False)
        raise HTTPException(400, "原密码错误")
    if req.old_password == req.new_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    score, label = password_strength(req.new_password)
    if len(req.new_password) < 8:
        raise HTTPException(400, "新密码至少 8 位")
    users_store.set_password(u["id"], req.new_password, must_change=False)
    # 改密后吊销其它会话，仅保留当前
    users_store.revoke_user_sessions(u["id"])
    new_token, _ = users_store.create_session(
        {**u, "must_change_password": 0}, client_ip(request),
        request.headers.get("user-agent", ""))
    users_store.audit(sess["username"], client_ip(request), "change_password",
                      detail=f"强度={label}")
    resp = JSONResponse({"ok": True, "token": new_token,
                         "message": "密码已修改，其它设备的登录已失效"})
    resp.set_cookie(COOKIE_NAME, new_token, httponly=True, samesite="lax",
                    max_age=auth_mod.SESSION_TTL, path="/")
    return resp


@app.get("/api/auth/password_policy")
def api_password_policy():
    return {"ok": True, "min_length": 8, "recommend_length": 12,
            "hint": "至少 8 位；含大小写字母 / 数字 / 符号中的 3 类更安全"}


# ═══════════════════════════════════════════════════════════════════════════
# 用户管理（仅 admin）
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/api/users")
def api_users(request: Request):
    require(request, "user")
    return {"ok": True, "users": users_store.list_users(),
            "roles": [{"value": r, "label": ROLE_LABELS[r]} for r in ROLES]}


@app.post("/api/users")
def api_create_user(req: CreateUserRequest, request: Request):
    admin = require(request, "user")
    if req.role not in ROLES:
        raise HTTPException(400, f"非法角色：{req.role}")
    generated = False
    pw = (req.password or "").strip()
    if not pw:
        pw, generated = random_password(14), True
    elif len(pw) < 8:
        raise HTTPException(400, "密码至少 8 位")
    try:
        uid = users_store.create_user(req.username, pw, role=req.role,
                                      display_name=req.display_name or "",
                                      must_change=bool(req.must_change) or generated,
                                      created_by=admin["username"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    users_store.audit(admin["username"], client_ip(request), "create_user",
                      target=req.username, detail=f"role={req.role}")
    out = {"ok": True, "user_id": uid, "username": req.username, "role": req.role,
           "generated": generated}
    if generated:
        out["password"] = pw
    return out


@app.patch("/api/users/{user_id}")
def api_update_user(user_id: int, req: UpdateUserRequest, request: Request):
    admin = require(request, "user")
    target = users_store.get_user(user_id=user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    kw = {}
    if req.role is not None:
        if req.role not in ROLES:
            raise HTTPException(400, f"非法角色：{req.role}")
        # 不允许把自己降级，避免把自己锁在门外
        if user_id == admin["user_id"] and req.role != ROLE_ADMIN:
            raise HTTPException(400, "不能修改自己的角色")
        kw["role"] = req.role
    if req.display_name is not None:
        kw["display_name"] = req.display_name
    if req.disabled is not None:
        if user_id == admin["user_id"] and req.disabled:
            raise HTTPException(400, "不能禁用当前登录的账号")
        kw["disabled"] = 1 if req.disabled else 0
        if req.disabled:
            users_store.revoke_user_sessions(user_id)
    users_store.update_user(user_id, **kw)
    users_store.audit(admin["username"], client_ip(request), "update_user",
                      target=target["username"], detail=json.dumps(kw, ensure_ascii=False))
    return {"ok": True}


@app.post("/api/users/{user_id}/password")
def api_reset_password(user_id: int, req: ResetPasswordRequest, request: Request):
    admin = require(request, "user")
    target = users_store.get_user(user_id=user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    generated = False
    pw = (req.new_password or "").strip()
    if not pw:
        pw, generated = random_password(14), True
    elif len(pw) < 8:
        raise HTTPException(400, "密码至少 8 位")
    users_store.set_password(user_id, pw, must_change=bool(req.must_change) or generated)
    users_store.revoke_user_sessions(user_id)
    users_store.audit(admin["username"], client_ip(request), "reset_password",
                      target=target["username"])
    out = {"ok": True, "username": target["username"], "generated": generated,
           "message": "密码已重置，该用户其它登录会话已失效"}
    if generated:
        out["password"] = pw
    return out


@app.delete("/api/users/{user_id}")
def api_delete_user(user_id: int, request: Request):
    admin = require(request, "user")
    if user_id == admin["user_id"]:
        raise HTTPException(400, "不能删除当前登录的账号")
    target = users_store.get_user(user_id=user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if target["role"] == ROLE_ADMIN and \
       sum(1 for u in users_store.list_users() if u["role"] == ROLE_ADMIN) <= 1:
        raise HTTPException(400, "系统必须保留至少一个管理员")
    users_store.delete_user(user_id)
    users_store.audit(admin["username"], client_ip(request), "delete_user",
                      target=target["username"])
    return {"ok": True}


@app.get("/api/sessions")
def api_sessions(request: Request):
    require(request, "user")
    items = users_store.list_sessions()
    cur = request.state.token
    for s in items:
        s["current"] = (s["token"] == cur)
        if not s["current"]:
            s["token_ref"] = s["token"][:12]
        s.pop("token", None)
    return {"ok": True, "sessions": items,
            "current_token": cur}


@app.delete("/api/sessions/{token_ref}")
def api_kill_session(token_ref: str, request: Request):
    require(request, "user")
    target = None
    for s in users_store.list_sessions():
        if s["token"] == token_ref or s["token"][:12] == token_ref:
            target = s["token"]
            break
    if not target:
        raise HTTPException(404, "会话不存在或已过期")
    if target == request.state.token:
        raise HTTPException(400, "不能吊销当前登录的会话，请直接退出登录")
    users_store.revoke_session(target)
    return {"ok": True}


@app.get("/api/audit")
def api_audit(request: Request, limit: int = 200):
    require(request, "user")
    return {"ok": True, "audit": users_store.get_audit(limit)}


# ═══════════════════════════════════════════════════════════════════════════
# 页面
# ═══════════════════════════════════════════════════════════════════════════
def _serve(name):
    path = os.path.join(STATIC_DIR, name)
    if not os.path.exists(path):
        return HTMLResponse(f"<h1>{name} 缺失</h1>", status_code=500)
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache"})


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return _serve("login.html")


@app.get("/", response_class=HTMLResponse)
def index():
    return _serve("console.html")


# ═══════════════════════════════════════════════════════════════════════════
# 目录 / 配置
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/api/catalog")
def api_catalog(request: Request, region: str = "us-central1", include_unavailable: bool = False):
    user = require(request, "view")
    payload = catalog.catalog_payload(region, include_unavailable)
    # 省钱清单按当前用户的默认配置实时计算
    saved = store.get_setting(_cfg_key(user["user_id"]), {}) or {}
    payload["savings"] = catalog.savings_status({**catalog.DEFAULT_CONFIG, **saved})
    return payload


@app.post("/api/savings")
def api_savings(request: Request, payload: dict):
    """按给定 spec 计算省钱项开关状态（前端表单变化时实时刷新）"""
    require(request, "view")
    return {"ok": True, "savings": catalog.savings_status({
        **catalog.DEFAULT_CONFIG, **(payload or {})})}


@app.get("/api/project_networks")
def api_project_networks(request: Request, account_id: int = 0, region: str = ""):
    """
    列出账号所在项目的真实 VPC 网络与子网。

    为什么需要：程序早年硬编码 network="default"/subnet="default"，
    但不少项目（尤其共享 VPC 或 default 网络被删除的项目）并不存在 default 网络，
    创建时会被 API 拒绝为
    "The referenced network resource cannot be found"，
    而界面上的文本框又让人以为 default 一定可用。
    """
    require(request, "view")
    accs = store.get_accounts()
    if account_id:
        accs = [a for a in accs if a["id"] == account_id]
    if not accs:
        return {"ok": False, "error": "没有可用账号"}
    acc = accs[0]
    region = (region or "").strip() or "us-central1"
    if region.count("-") >= 2:
        region = region.rsplit("-", 1)[0]
    try:
        gcp = GCPService(acc["key_path"], acc["project_id"], acc["email"],
                         acc.get("proxy", ""), acc.get("proxy_type", "HTTPS"))
        nets = gcp.list_networks()
        subs = gcp.list_subnetworks(region)
        return {"ok": True, "region": region, "networks": nets, "subnets": subs,
                "has_default_network": "default" in nets}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/project_zones")
def api_project_zones(request: Request, account_id: int = 0, region: str = ""):
    """
    列出区域内真实存在的 zone。

    为什么需要：各 region 的 zone 后缀不统一（us-west1 仅 a/b/c），
    早先按 a/b/c/d/f 硬编码猜测会撞上不存在的 zone，
    而 GCP 对不存在的 zone 返回 "Permission denied"，极易被误读为
    「服务账号权限不足」。这里直接向 GCP 要真实列表。
    """
    require(request, "view")
    accs = store.get_accounts()
    if account_id:
        accs = [a for a in accs if a["id"] == account_id]
    if not accs:
        return {"ok": False, "error": "没有可用账号"}
    acc = accs[0]
    region = (region or "").strip()
    if region.count("-") >= 2:
        region = region.rsplit("-", 1)[0]
    try:
        gcp = GCPService(acc["key_path"], acc["project_id"], acc["email"],
                         acc.get("proxy", ""), acc.get("proxy_type", "HTTPS"))
        return {"ok": True, "region": region, "zones": gcp.list_zones(region)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ----------------------------------------------------------------------
# GCP 资源勘察（只读）
#
# 目标：把服务账号能看到的项目信息尽量都呈现出来。
# 每节独立成败 —— 服务账号往往只有 compute 相关权限，
# 没有 resourcemanager / serviceusage / cloudbilling，
# 那种情况下只让对应那一节显示原因，不影响其余节。
# ----------------------------------------------------------------------
_INSPECT_CACHE = {}          # {(account_id, sections, region, zone): (ts, payload)}
INSPECT_TTL = 45             # 秒；重复刷新页面不必反复打 GCP


def _pick_account(account_id):
    accs = store.get_accounts()
    if account_id:
        accs = [a for a in accs if a["id"] == account_id]
    return accs[0] if accs else None


def _norm_region(r):
    r = (r or "").strip()
    if r.count("-") >= 2:               # 传进来的是 zone，归一到 region
        r = r.rsplit("-", 1)[0]
    return r


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """站点图标。复用原版 v7.6 的 app_icon.ico，避免每次开页面一个 404。"""
    path = os.path.join(STATIC_DIR, "favicon.ico")
    if not os.path.exists(path):
        raise HTTPException(404, "no favicon")
    return FileResponse(path, media_type="image/x-icon")


@app.get("/api/version")
def api_version():
    """版本与仓库信息。刻意不要求登录 —— 登录页也要展示版本号。"""
    return {"ok": True, **ver.info()}


@app.get("/api/inspect/sections")
def api_inspect_sections(request: Request):
    """列出可勘察的节，供前端渲染勾选项"""
    require(request, "view")
    return {"ok": True,
            "default": gcp_inspect.DEFAULT_SECTIONS,
            "deep": gcp_inspect.DEEP_SECTIONS,
            "all": sorted(gcp_inspect.SECTIONS)}


@app.get("/api/inspect")
def api_inspect(request: Request, account_id: int = 0, sections: str = "",
                region: str = "", zone: str = "", fresh: int = 0,
                quick: int = 0):
    """
    批量勘察 GCP 资源。

    sections  逗号分隔的节名；留空取默认节（quick=1 时只取默认节）
    region    限定区域（regions/zones/subnetworks 用）
    zone      限定可用区（machineTypes 用）
    fresh=1   绕过 45s 缓存，强制重新拉取
    """
    require(request, "view")
    acc = _pick_account(account_id)
    if not acc:
        return {"ok": False, "error": "没有可用账号"}

    if sections:
        want = [x.strip() for x in sections.split(",") if x.strip()]
    elif quick:
        want = list(gcp_inspect.DEFAULT_SECTIONS)
    else:
        want = list(gcp_inspect.DEFAULT_SECTIONS) + list(gcp_inspect.DEEP_SECTIONS)
    # 去重且保序
    seen, ordered = set(), []
    for w in want:
        if w not in seen:
            seen.add(w)
            ordered.append(w)

    region = _norm_region(region)
    zone = (zone or "").strip()
    if not zone and region:
        zone = region + "-a"

    ck = (acc["id"], tuple(ordered), region, zone)
    now = time.time()
    if not fresh and ck in _INSPECT_CACHE:
        ts, cached = _INSPECT_CACHE[ck]
        if now - ts < INSPECT_TTL:
            return {**cached, "cached": True, "age": int(now - ts)}

    try:
        res = gcp_inspect.inspect_sections(
            acc["key_path"], acc["project_id"], acc["email"], ordered,
            params={"region": region, "zone": zone},
            proxy=acc.get("proxy", ""), proxy_type=acc.get("proxy_type", "HTTPS"))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    payload = {
        "ok": True, "cached": False,
        "account_id": acc["id"],
        "project_id": acc["project_id"],
        "account_email": acc["email"],
        "region": region, "zone": zone,
        "sections": res,
        "failed": [k for k, v in res.items() if not v.get("ok")],
        "elapsed_ms": sum(v.get("ms", 0) for v in res.values()),
    }
    # 时间戳取「完成时刻」而非开始时刻：整套勘察可能跑 20-90 秒，
    # 若记开始时刻，条目一存进去就已经超过 TTL，缓存永远不命中。
    _INSPECT_CACHE[ck] = (time.time(), payload)
    # 缓存别无限涨
    if len(_INSPECT_CACHE) > 32:
        oldest = sorted(_INSPECT_CACHE.items(), key=lambda kv: kv[1][0])[:8]
        for k, _ in oldest:
            _INSPECT_CACHE.pop(k, None)
    return payload


@app.get("/api/config")
def api_get_config(request: Request):
    user = require(request, "view")
    saved = store.get_setting(_cfg_key(user["user_id"]), {}) or {}
    return {"ok": True, "config": {**catalog.DEFAULT_CONFIG, **saved},
            "scope": "user", "user": user["username"]}


@app.post("/api/config")
def api_set_config(request: Request, payload: dict):
    user = require(request, "settings")
    # 显式保存时才允许写入危险开关（用户主动点了「保存为默认配置」）
    data = {k: v for k, v in (payload or {}).items() if v is not None}
    store.set_setting(_cfg_key(user["user_id"]), data)
    users_store.audit(user["username"], client_ip(request), "save_config")
    return {"ok": True, "scope": "user"}


@app.post("/api/cost/estimate")
def api_cost_estimate(request: Request, payload: dict):
    require(request, "view")
    return {"ok": True, "estimate": catalog.estimate_monthly_cost(
        payload.get("machine_type", catalog.DEFAULT_CONFIG["machine_type"]),
        payload.get("disk_type", catalog.DEFAULT_CONFIG["disk_type"]),
        payload.get("disk_size_gb", catalog.DEFAULT_CONFIG["disk_size_gb"]),
        payload.get("region", "us-central1"),
        hours=int(payload.get("hours", 730)),
        count=int(payload.get("count", 1)),
        preemptible=bool(payload.get("preemptible")),
        spot=bool(payload.get("spot")),
    )}


# ═══════════════════════════════════════════════════════════════════════════
# 账号
# ═══════════════════════════════════════════════════════════════════════════
def _load_json_info(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("client_email", ""), data.get("project_id", "")


@app.get("/api/accounts")
def api_accounts(request: Request):
    require(request, "view")
    accounts = store.get_accounts()
    for a in accounts:
        a["key_exists"] = os.path.exists(a.get("key_path") or "")
        a["key_file"] = os.path.basename(a.get("key_path") or "")
        a.pop("key_path", None)
        # 备注：邮箱很长时界面上优先显示它。老库里该字段可能为 NULL。
        a["label"] = a.get("label") or ""

        # 代理：解析出类型标签，并**把密码打码后再返回**。
        # 账号页面只需要展示「用没用代理、是什么代理」，
        # 把明文密码塞进列表响应没有任何必要。
        raw_proxy = a.get("proxy") or ""
        a["proxy_set"] = bool(raw_proxy.strip())
        pinfo = gcp_mod.parse_proxy_input(raw_proxy, a.get("proxy_type") or "HTTPS")
        a["proxy_ok"] = bool(pinfo.get("ok"))
        a["proxy_error"] = pinfo.get("error") or ""
        a["proxy_type"] = pinfo.get("proxy_type") or (a.get("proxy_type") or "HTTPS")
        a["proxy_type_label"] = pinfo.get("proxy_type_label") or ""
        a["proxy_has_auth"] = bool(pinfo.get("has_auth"))
        a["proxy_display"] = gcp_mod.mask_proxy(raw_proxy)
        a["proxy_host"] = pinfo.get("host") or ""
        a["proxy_port"] = pinfo.get("port") or ""
        # 原始 proxy 字段（可能带明文密码）不外发
        a.pop("proxy", None)
    return {"ok": True, "accounts": accounts}


@app.post("/api/accounts")
def api_add_account(request: Request, payload: dict):
    user = require(request, "account")
    key_path = (payload.get("key_path") or "").strip()
    json_content = payload.get("json_content")
    if json_content and not key_path:
        try:
            info = json.loads(json_content)
        except Exception as exc:
            raise HTTPException(400, f"JSON 解析失败：{exc}")
        name = payload.get("filename") or f"{info.get('project_id', 'account')}-{int(time.time())}.json"
        name = os.path.basename(name)
        key_path = os.path.join(KEY_DIR, name)
        with open(key_path, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    if not key_path:
        raise HTTPException(400, "缺少 key_path 或 json_content")
    if not os.path.exists(key_path):
        raise HTTPException(400, f"文件不存在：{key_path}")
    try:
        email, project_id = _load_json_info(key_path)
    except Exception as exc:
        raise HTTPException(400, f"无法读取服务账号 JSON：{exc}")
    email = (payload.get("email") or email).strip()
    project_id = (payload.get("project_id") or project_id).strip()
    acc_id, created = store.upsert_account_by_key(
        email, project_id, key_path, payload.get("proxy", ""),
        payload.get("proxy_type", "HTTPS"), payload.get("label", ""))
    users_store.audit(user["username"], client_ip(request), "add_account",
                      target=email, detail=f"created={created}")
    return {"ok": True, "account_id": acc_id, "created": created,
            "email": email, "project_id": project_id, "key_path": key_path}


@app.post("/api/accounts/upload")
async def api_upload_account(request: Request, file: UploadFile = File(...),
                             proxy: str = Form(""), proxy_type: str = Form("HTTPS")):
    user = require(request, "account")
    raw = await file.read()
    try:
        info = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(400, f"JSON 解析失败：{exc}")
    name = os.path.basename(file.filename or "account.json")
    if not name.endswith(".json"):
        name += ".json"
    key_path = os.path.join(KEY_DIR, name)
    with open(key_path, "wb") as f:
        f.write(raw)
    email = info.get("client_email", "")
    project_id = info.get("project_id", "")
    acc_id, created = store.upsert_account_by_key(email, project_id, key_path, proxy, proxy_type)
    users_store.audit(user["username"], client_ip(request), "upload_account", target=email)
    return {"ok": True, "account_id": acc_id, "created": created,
            "email": email, "project_id": project_id}


@app.delete("/api/accounts/{acc_id}")
def api_delete_account(acc_id: int, request: Request):
    user = require(request, "account")
    acc = store.get_account(acc_id)
    store.delete_account(acc_id)
    users_store.audit(user["username"], client_ip(request), "delete_account",
                      target=(acc or {}).get("email", str(acc_id)))
    return {"ok": True}


@app.patch("/api/accounts/{acc_id}")
def api_update_account(acc_id: int, request: Request, payload: dict):
    require(request, "account")
    store.update_account(acc_id, **payload)
    return {"ok": True}


@app.post("/api/accounts/{acc_id}/test")
def api_test_account(acc_id: int, request: Request):
    require(request, "view")
    acc = store.get_account(acc_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    try:
        gcp = GCPService(acc["key_path"], acc["project_id"], acc["email"],
                         acc.get("proxy", ""), acc.get("proxy_type", "HTTPS"))
        t0 = time.time()
        insts = gcp.list_instances()
        pinfo = gcp_mod.parse_proxy_input(acc.get("proxy") or "", acc.get("proxy_type") or "HTTPS")
        return {"ok": True, "instances": len(insts),
                "latency_ms": round((time.time() - t0) * 1000),
                # 只回实例名，全量数据在 /api/instances；这里够证明连通性了
                "instance_names": [i["name"] for i in insts][:50],
                "via_proxy": bool(pinfo.get("proxy_url")),
                "proxy_type": pinfo.get("proxy_type") or ""}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/accounts/import_dir")
def api_import_dir(request: Request, payload: dict):
    user = require(request, "account")
    folder = (payload.get("folder") or "").strip()
    if not folder or not os.path.isdir(folder):
        raise HTTPException(400, "目录不存在")
    out = []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            email, project_id = _load_json_info(path)
        except Exception:
            continue
        acc_id, created = store.upsert_account_by_key(
            email, project_id, path, payload.get("proxy", ""), payload.get("proxy_type", "HTTPS"))
        out.append({"file": name, "email": email, "project_id": project_id,
                    "account_id": acc_id, "created": created})
    users_store.audit(user["username"], client_ip(request), "import_dir",
                      target=folder, detail=f"{len(out)} 个")
    return {"ok": True, "imported": len(out), "accounts": out}


# ═══════════════════════════════════════════════════════════════════════════
# 实例
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/api/instances")
def api_instances(request: Request, account_ids: str = "", sync: bool = True):
    require(request, "view")
    ids = [x for x in (account_ids or "").split(",") if x.strip()]
    if sync:
        return tm.list_all_instances(ids)
    return {"ok": True, "instances": store.get_all_vms()}


@app.get("/api/install_presets")
def api_install_presets(request: Request):
    """创建后可自动安装的预设清单（docker / 3x-ui / nps / hermes / ekko）"""
    require(request, "view")
    return {"ok": True, "presets": presets.preset_payload()}


@app.patch("/api/instances/note")
def api_update_instance_note(request: Request, payload: dict):
    """改实例备注。创建后才知道这台机器要干嘛，所以必须能改。"""
    user = require(request, "operate")
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "缺少实例名")
    note = (payload.get("note") or "").strip()
    if len(note) > 200:
        raise HTTPException(400, "备注过长（上限 200 字）")
    if not store.update_vm_note(name, note):
        # 本地没有记录时补一条，否则「GCP 上有、本地没记过」的实例改不了备注
        store.save_vm(name, "", "", None, None, None, None, None, None, note=note)
    users_store.audit(user["username"], client_ip(request), "instance_note",
                      target=name, detail=note[:60], ok=True)
    return {"ok": True, "name": name, "note": note}


@app.post("/api/instances/password")
def api_reveal_instance_password(request: Request, payload: dict):
    """
    二次验证用户登录密码后返回实例的 root 密码。

    为什么要有这一步：实例列表只需要 view 权限，而 viewer 是只读角色。
    如果列表接口直接把 root 密码明文吐出来，等于任何能登录的人（哪怕是
    只读账号）都能一次性拿到全部机器的 root。所以列表只回 has_password，
    要看明文必须重新输入**自己的登录密码**。

    这里复用登录的 LoginGuard 做限速 —— 否则这个接口就成了一个
    用别人的会话暴力猜密码的现成 oracle。
    """
    user = require(request, "view")
    ip = client_ip(request)
    name = (payload.get("name") or "").strip()
    password = payload.get("password") or ""
    if not name:
        raise HTTPException(400, "缺少实例名")
    if not password:
        raise HTTPException(400, "请输入当前账号的登录密码")

    ok, why = login_guard.check(user["username"], ip)
    if not ok:
        users_store.audit(user["username"], ip, "reveal_root_password",
                          target=name, detail=why, ok=False)
        raise HTTPException(429, why)

    # 用登录验密函数校验（内部含哈希比对 + 失败计数 + 恒定耗时路径）
    who, reason = users_store.verify_login(user["username"], password)
    if not who:
        login_guard.fail(user["username"], ip)
        users_store.audit(user["username"], ip, "reveal_root_password",
                          target=name, detail="密码校验失败", ok=False)
        raise HTTPException(403, "登录密码不正确")

    vm = store.get_vm(name)
    pwd = (vm or {}).get("password") or ""
    if not pwd:
        users_store.audit(user["username"], ip, "reveal_root_password",
                          target=name, detail="无密码记录", ok=False)
        raise HTTPException(404, "该实例没有 root 密码记录（可能创建时用的是 SSH 密钥模式）")

    users_store.audit(user["username"], ip, "reveal_root_password",
                      target=name, detail="已出示", ok=True)
    return {"ok": True, "name": name, "root_password": pwd}


@app.post("/api/refresh")
def api_refresh(request: Request, payload: dict | None = None):
    require(request, "view")
    return tm.submit_refresh((payload or {}).get("account_ids"))


@app.post("/api/create")
def api_create(req: CreateRequest, request: Request):
    user = require(request, "operate")
    payload = body(req)
    spec = payload.get("spec") or {}
    if req.spec is None:
        spec = store.get_setting(_cfg_key(user["user_id"]), {}) or {}
    payload["spec"] = build_instance_spec(spec)

    # 勾选了「创建后自动安装」时，用预设模块生成安装脚本。
    # 用户自己写的 post_command 优先级更高 —— 预设只是省去手写，
    # 不该覆盖掉用户显式填的命令。
    installs = presets.normalize(payload.get("installs"))
    payload["installs"] = installs
    if installs:
        preset_script = presets.build_script(installs)
        if preset_script:
            own = (payload.get("post_command") or "").strip()
            payload["post_command"] = (own + "\n\n" + preset_script) if own else preset_script
        if not (payload.get("verify_command") or "").strip():
            payload["verify_command"] = presets.verify_command(installs)
        payload.setdefault("ssh_timeout", 300)

    if not payload.get("dry_run"):
        # 持久化客户端真正提供的字段（过滤 None，避免用 None 覆盖默认值），
        # 但**危险开关不写回默认配置**：
        #   · auto_open_firewall —— 默认关闭；若某次创建勾选了它并被记住，
        #     之后每次创建都会默默放开 0.0.0.0/0 全协议，与默认收敛的意图相反
        #   · preemptible / spot —— 被记住会导致后续实例被意外抢占
        # 这些开关要成为默认，只能由用户主动点「保存为默认配置」。
        NO_PERSIST_ON_CREATE = {"auto_open_firewall", "preemptible", "spot"}
        clean = {k: v for k, v in spec.items()
                 if v is not None
                 and k not in ("region", "regions")
                 and k not in NO_PERSIST_ON_CREATE}
        if "tags" in clean and isinstance(clean["tags"], str):
            clean["tags"] = [t.strip() for t in clean["tags"].split(",") if t.strip()]
        store.set_setting(_cfg_key(user["user_id"]), clean)
        users_store.audit(user["username"], client_ip(request), "create_instances",
                          detail=json.dumps({"count": payload.get("count"),
                                             "machine": spec.get("machine_type"),
                                             "image": spec.get("image_key"),
                                             "accounts": len(payload.get("account_ids") or []),
                                             "note": (payload.get("note") or "")[:40],
                                             "installs": installs},
                                            ensure_ascii=False))
    return tm.submit_create(payload)


@app.post("/api/execute")
def api_execute(req: ExecuteRequest, request: Request):
    user = require(request, "operate")
    users_store.audit(user["username"], client_ip(request), "execute_command",
                      detail=(req.command or "")[:200])
    return tm.submit_execute(body(req))


@app.post("/api/instance_action")
def api_instance_action(req: ActionRequest, request: Request):
    user = require(request, "operate")
    if req.action not in ("start", "stop", "reset", "delete"):
        raise HTTPException(400, "action 必须是 start/stop/reset/delete")
    users_store.audit(user["username"], client_ip(request), f"instance_{req.action}",
                      target=",".join(req.targets[:10]), detail=f"{len(req.targets)} 台")
    return tm.submit_instance_action(req.action, req.targets)


# ═══════════════════════════════════════════════════════════════════════════
# 任务 / 日志
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/api/tasks")
def api_tasks(request: Request, limit: int = 100):
    require(request, "view")
    return {"ok": True, "tasks": tm.api_tasks_snapshot(limit), "db_tasks": store.get_tasks(limit)}


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str, request: Request):
    require(request, "view")
    task = tm.api_tasks.get(task_id) or store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {"ok": True, "task": task}


@app.post("/api/tasks/{task_id}/cancel")
def api_task_cancel(task_id: str, request: Request):
    user = require(request, "operate")
    users_store.audit(user["username"], client_ip(request), "cancel_task", target=task_id)
    return tm.cancel(task_id)


@app.get("/api/logs")
def api_logs(request: Request, since_id: int = 0, limit: int = 500, task_id: str = ""):
    require(request, "view")
    return {"ok": True, "logs": store.get_logs(since_id, limit, task_id or None)}


@app.delete("/api/logs")
def api_logs_clear(request: Request):
    user = require(request, "operate")
    store.clear_logs()
    users_store.audit(user["username"], client_ip(request), "clear_logs")
    return {"ok": True}


@app.get("/api/status")
def api_status(request: Request):
    require(request, "view")
    users = users_store.list_users()
    return {
        "ok": True,
        # 版本与仓库信息统一来自 core/version.py
        "version": ver.VERSION,
        "app_name": ver.APP_NAME,
        "app_name_cn": ver.APP_NAME_CN,
        "repo": ver.REPO_URL,
        "repo_name": ver.REPO_NAME,
        "issue_url": ver.ISSUE_URL,
        "changelog": ver.CHANGELOG,
        "accounts": len(store.get_accounts()),
        "vms_with_password": len(store.get_all_vms()),
        "paramiko": ssh_mod.check_paramiko(),
        "machine_types": len(catalog.MACHINE_TYPES),
        "images": len(catalog.IMAGES),
        "disk_types": len(catalog.DISK_TYPES),
        "regions": len(catalog.ALL_REGIONS),
        "users": len(users), "active_users": sum(1 for u in users if not u["disabled"]),
        "time": time.time(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# SSH 密钥
# ═══════════════════════════════════════════════════════════════════════════
@app.post("/api/sshkey/generate")
def api_generate_sshkey(request: Request, payload: dict | None = None):
    require(request, "operate")
    payload = payload or {}
    if not ssh_mod.check_paramiko():
        raise HTTPException(500, "需要 paramiko")
    import paramiko
    key = paramiko.RSAKey.generate(2048)
    comment = payload.get("comment") or "gcp-manager-web"
    pub = f"{key.get_name()} {key.get_base64()} {comment}"
    if payload.get("save"):
        kdir = os.path.join(DATA_DIR, "ssh_keys")
        os.makedirs(kdir, exist_ok=True)
        name = os.path.basename(payload.get("name") or f"gcp_key_{int(time.time())}")
        kpath = os.path.join(kdir, name)
        key.write_private_key_file(kpath)
        try:
            os.chmod(kpath, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
        with open(kpath + ".pub", "w", encoding="utf-8") as f:
            f.write(pub + "\n")
        return {"ok": True, "public_key": pub, "private_key_path": kpath}
    return {"ok": True, "public_key": pub}


class SSHKeyReadRequest(BaseModel):
    pubkey_path: str


@app.post("/api/sshkey/read")
def api_read_sshkey(req: SSHKeyReadRequest, request: Request):
    require(request, "operate")
    if not os.path.exists(req.pubkey_path):
        raise HTTPException(400, f"公钥文件不存在：{req.pubkey_path}")
    with open(req.pubkey_path, "r", encoding="utf-8") as f:
        return {"ok": True, "public_key": f.read().strip()}


# ═══════════════════════════════════════════════════════════════════════════
# WebSocket 实时日志（同样要求登录）
# ═══════════════════════════════════════════════════════════════════════════
@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    token = ws.cookies.get(COOKIE_NAME)
    sess = users_store.get_session(token) if token else None
    if not sess:
        await ws.close(code=4401)
        return
    await ws.accept()
    since = 0
    first = True
    try:
        try:
            init = await asyncio.wait_for(ws.receive_json(), timeout=1.5)
            since = int(init.get("since_id") or 0)
        except Exception:
            pass
        await ws.send_json({"type": "hello", "since_id": since, "user": sess["username"]})
        while True:
            logs = store.get_logs(since, 200)
            if logs:
                since = logs[-1]["id"]
                await ws.send_json({"type": "logs", "items": logs})
            elif first:
                await ws.send_json({"type": "logs", "items": []})
            first = False
            tasks = tm.api_tasks_snapshot(20)
            await ws.send_json({"type": "tasks", "items": [
                {"id": t["id"], "kind": t["kind"], "status": t["status"], "message": t["message"]}
                for t in tasks]})
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
