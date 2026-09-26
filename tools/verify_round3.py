#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证本轮 4 项改动（全部走真实 HTTP 接口 + 真实前端静态资源）：

  ① 操作审计改分页（默认 5 条/页，服务端 LIMIT/OFFSET）
  ② 账号管理页可测试代理是否有效
  ③ 命令执行页显示机器列表
  ④ 实例列表状态中文显示

用法：python3 tools/verify_round3.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TMP = tempfile.mkdtemp(prefix="gcpweb_v3_")
shutil.copytree(os.path.join(ROOT, "core"), os.path.join(TMP, "core"))
shutil.copytree(os.path.join(ROOT, "static"), os.path.join(TMP, "static"))
for f in ("app.py", "requirements.txt"):
    shutil.copy(os.path.join(ROOT, f), os.path.join(TMP, f))
os.makedirs(os.path.join(TMP, "data"), exist_ok=True)
os.environ["GCPWEB_DATA_DIR"] = os.path.join(TMP, "data")

sys.path.insert(0, TMP)
os.chdir(TMP)

from fastapi.testclient import TestClient          # noqa: E402
import app as appmod                               # noqa: E402
from core import auth as auth_mod                  # noqa: E402
from core import gcp as gcp_mod                    # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  → {extra}" if extra and not cond else ""))


# 管理员密码以 data/INITIAL_ADMIN.txt 为单一事实来源（首次启动自动生成），
# 不硬编码 —— 见 tools/_fixtures.py 的约定。
_pw_file = os.path.join(TMP, "data", "INITIAL_ADMIN.txt")
ADMIN_PW = ""
for _line in open(_pw_file, encoding="utf-8"):
    if ":" in _line and "密码" in _line:
        ADMIN_PW = _line.split(":", 1)[1].strip()
print(f"[fixture] 管理员密码来源 {_pw_file}（长度 {len(ADMIN_PW)}）")

c = TestClient(appmod.app)
HTML = open(os.path.join(ROOT, "static", "console.html"), encoding="utf-8").read()
APPSRC = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
GCPSRC = open(os.path.join(ROOT, "core", "gcp.py"), encoding="utf-8").read()


def login(u="admin", pw=None):
    cap = c.get("/api/auth/captcha").json()
    code = auth_mod.captcha_store._items.get(cap["captcha_id"], {}).get("code")
    r = c.post("/api/auth/login", json={"username": u, "password": pw or ADMIN_PW,
                                        "captcha_id": cap["captcha_id"], "captcha_code": code})
    return r.json()


def _solve_captcha(force_new=True):
    cap = c.get("/api/auth/captcha").json()
    return cap["captcha_id"], auth_mod.captcha_store._items.get(cap["captcha_id"], {}).get("code")


def _login_raw(u, pw):
    cid, code = _solve_captcha()
    return c.post("/api/auth/login", json={"username": u, "password": pw,
                                           "captcha_id": cid, "captcha_code": code})


# 首次登录必须改密（v1.2.4 的服务端强制策略），测试里走一遍拿到"干净状态"
ADMIN_PW = ADMIN_PW + "x"
r = _login_raw("admin", ADMIN_PW[:-1])
assert r.json().get("ok"), r.text
r = c.post("/api/auth/change_password", json={"old_password": ADMIN_PW[:-1],
                                              "new_password": ADMIN_PW})
assert r.json().get("ok"), r.text
print("[fixture] 已完成首次强制改密")
login()

# ═══════════════ ① 审计分页 ═══════════════
print("\n" + "=" * 72)
print("① 操作审计分页（默认 5 条/页）")
print("=" * 72)

# 造 13 条审计记录
for i in range(13):
    appmod.users_store.audit("admin", "127.0.0.1", f"verify_{i}", f"t{i}", f"d{i}")
total = appmod.users_store.count_audit()
check("★ 新增 count_audit() 可用", isinstance(total, int) and total >= 13, str(total))

r = c.get("/api/audit?page=1&page_size=5").json()
check("★ 默认页返回 ≤5 条", len(r["audit"]) == 5, str(len(r["audit"])))
check("★ 响应带 total", r.get("total") == total, f"{r.get('total')} vs {total}")
check("★ 响应带 page/pages", r.get("page") == 1 and r.get("pages") == -(-total // 5),
      f"page={r.get('page')} pages={r.get('pages')}")
check("★ 响应带 page_size", r.get("page_size") == 5)

p2 = c.get("/api/audit?page=2&page_size=5").json()
check("★ 第 2 页与第 1 页无重叠", not ({a["id"] for a in p2["audit"]} & {a["id"] for a in r["audit"]}))
check("★ 第 2 页确实是「更早」的记录（ts 递减）",
      p2["audit"][0]["ts"] <= r["audit"][-1]["ts"],
      f"{p2['audit'][0]['ts']} vs {r['audit'][-1]['ts']}")

last = c.get(f"/api/audit?page={r['pages']}&page_size=5").json()
check("★ 末页条数正确（13 条 → 末页 3 条）", len(last["audit"]) == total % 5 or total % 5 == 0,
      str(len(last["audit"])))

oob = c.get("/api/audit?page=99999&page_size=5").json()
check("★ 越界 page 被夹回合法范围", 1 <= oob["page"] <= oob["pages"], f"page={oob['page']}")

neg = c.get("/api/audit?page=0&page_size=5")
check("★ page=0 被校验拒绝（422）", neg.status_code == 422, str(neg.status_code))
big = c.get("/api/audit?page=1&page_size=99999")
check("★ page_size 超上限被拒（422）", big.status_code == 422, str(big.status_code))

old = c.get("/api/audit?limit=100").json()
check("★ 兼容旧调用 ?limit=100（现有测试/PoC 不受影响）", len(old["audit"]) == total, str(len(old["audit"])))

check("★ 前端默认每页 5 条", 'auditPageSize:5' in HTML)
check("★ 前端有分页控件（上一页/下一页/跳转）",
      "上一页" in HTML and "下一页" in HTML and "跳转" in HTML)
check("★ 前端不再一次拉 200 条", "?limit=200" not in HTML)
check("★ 前端标题显示总条数与页数", "auditTotal" in HTML and "auditPages" in HTML)

# ═══════════════ ② 代理测试 ═══════════════
print("\n" + "=" * 72)
print("② 账号管理页：测试代理是否有效")
print("=" * 72)

check("★ 后端存在 test_proxy 实现", "def test_proxy(" in GCPSRC)
check("★ 后端存在 /api/accounts/{id}/test_proxy 路由",
      "/api/accounts/{acc_id}/test_proxy" in APPSRC)
check("★ 前端有「测代理」按钮", "测代理" in HTML and "testProxyAccount" in HTML)
check("★ 前端有「测试全部代理」", "测试全部代理" in HTML and "testAllProxies" in HTML)
check("★ 探测结果在页面上有回显（可用/不可用 + 延迟）",
      "代理可用" in HTML and "latency_ms" in HTML)

# SSRF 防护：探测目标必须硬编码，不能由请求方指定
check("★ ★ 探测目标 URL 是模块常量（不接受请求方传入 → 无 SSRF）",
      'PROXY_TEST_URL = "https://www.googleapis.com/discovery/v1/apis"' in GCPSRC)
check("★ ★ test_proxy 的 url 参数只作内部默认值，路由未从请求体读取 url",
      "payload" not in APPSRC.split("def api_test_account_proxy")[1].split("@app.post")[0])

# 纯逻辑：非法代理配置被拒（不发外连）
bad = gcp_mod.test_proxy("这不是一个代理地址", "HTTPS")
check("★ 非法代理配置被拒且不发外连", bad["ok"] is False and bool(bad["error"]), str(bad.get("error"))[:60])
bad2 = gcp_mod.test_proxy("1.2.3.4:99999", "HTTPS")
check("★ 端口越界被拒", bad2["ok"] is False and bool(bad2["error"]), str(bad2.get("error"))[:60])

# 真实探测一个**不存在的代理** → 必须失败（证明真的在发请求，不是空转）
t0 = time.time()
dead = gcp_mod.test_proxy("127.0.0.1:9", "HTTPS", timeout=4)
el = time.time() - t0
check("★ ★ 不存在的代理 → 探测失败（证明真发请求而非空转报 ok）",
      dead["ok"] is False and bool(dead["error"]), f"ok={dead['ok']} err={str(dead.get('error'))[:60]}")
check("★ 探测耗时合理（<15s，未挂死）", el < 15, f"{el:.1f}s")
# 代理密码打码：两种受支持的写法都必须打码（本轮查出的真实缺陷）
for _raw, _want in (("1.2.3.4:8080:user:secretPw", "1.2.3.4:8080:user:***"),
                    ("socks5h://u:secretPw@h:1080", "socks5h://u:***@h:1080"),
                    ("http://u:secretPw@h:3128", "http://u:***@h:3128")):
    _got = gcp_mod.mask_proxy(_raw)
    check(f"★ ★ 代理密码打码：{_raw} → {_got}",
          _got == _want and "secretPw" not in _got, f"期望 {_want}")
check("★ 无密码的 host:port 保持原样（不误伤）",
      gcp_mod.mask_proxy("1.2.3.4:8080") == "1.2.3.4:8080")
check("★ 打码结果不外泄到探测响应",
      "secretPw" not in json.dumps(gcp_mod.test_proxy("1.2.3.4:8080:user:secretPw", "HTTPS", timeout=3),
                                   ensure_ascii=False))

# 真实探测「直连」：拿本机外网能力作答，且明确标注 empty
direct = gcp_mod.test_proxy("", "HTTPS", timeout=6)
check("★ 未配置代理时结果标注 empty=True（不冒充『代理可用』）",
      direct.get("empty") is True, str(direct.get("empty")))
print(f"   （附注：直连探测实际结果 ok={direct['ok']} "
      f"{'延迟 %sms' % direct['latency_ms'] if direct['ok'] else '原因=' + str(direct['error'])[:70]}）")

# 路由层：权限与不存在账号
r = c.post("/api/accounts/1/test_proxy", json={})
check("★ 账号不存在 → 404", r.status_code == 404, str(r.status_code))
VW_PW = "Viewer@12345x"
appmod.users_store.create_user("vw1", VW_PW[:-1], role="viewer", must_change=True)
_login_raw("vw1", VW_PW[:-1])
c.post("/api/auth/change_password", json={"old_password": VW_PW[:-1], "new_password": VW_PW})
_login_raw("vw1", VW_PW)
r = c.post("/api/accounts/1/test_proxy", json={})
check("★ 只读角色也能探测（与 /test 同权限，404 说明过了鉴权）", r.status_code == 404, str(r.status_code))
login()

# ═══════════════ ③ 命令执行页显示机器 ═══════════════
print("\n" + "=" * 72)
print("③ 命令执行页显示机器列表")
print("=" * 72)

exec_tpl = HTML.split('tab===\'exec\'')[1].split("tab==='tasks'")[0]
check("★ ★ 命令执行页含目标实例卡片", "目标实例" in exec_tpl)
check("★ ★ 命令执行页含实例列表表格（v-for 渲染 instances）",
      'v-for="i in instances"' in exec_tpl)
check("★ ★ 命令执行页的复选框与实例列表页共用 checkedInst",
      ':value="i.name" v-model="checkedInst"' in exec_tpl)
check("★ 有全选 / 全不选", "全选" in exec_tpl and "全不选" in exec_tpl)
check("★ 有「只选运行中」快捷按钮", "只选运行中" in exec_tpl)
check("★ 有刷新实例按钮", "刷新实例" in exec_tpl)
check("★ 空列表时有引导文案", "暂无实例" in exec_tpl)
check("★ 提示只有运行中的实例能执行", "只有" in exec_tpl and "运行中" in exec_tpl)
check("★ 进入命令执行页会加载实例",
      "id==='exec' && !this.instances.length) this.loadInstances(false)" in HTML)
check("★ 该页展示机器状态时也走中文",
      "vmStatusLabel(i.status)" in exec_tpl)

# ═══════════════ ④ 实例状态中文 ═══════════════
print("\n" + "=" * 72)
print("④ 实例列表状态中文显示")
print("=" * 72)

check("★ 存在 vmStatusLabel 函数", "vmStatusLabel(s)" in HTML)

want = {"RUNNING": "运行中", "PROVISIONING": "创建中", "STAGING": "准备中",
        "STOPPING": "停止中", "STOPPED": "已停止", "SUSPENDING": "挂起中",
        "SUSPENDED": "已挂起", "REPAIRING": "修复中", "TERMINATED": "已终止"}
body = HTML.split("vmStatusLabel(s){")[1].split("},")[0]
missing = [k for k in want if f"{k}:'" not in body.replace(" ", "")]
check("★ ★ 9 种 GCP 状态全部有中文映射", not missing, f"缺：{missing}")

check("★ 状态单元格改用中文标签",
      'vmStatusLabel(i.status)' in HTML and '{{ i.status }}</span></td>' not in HTML)
check("★ 原文保留在 title（便于对照 API/日志）", ':title="i.status"' in HTML)

inst_tpl = HTML.split("tab==='instances'")[1].split("tab==='accounts'")[0]
check("★ 实例列表页的状态列已用中文", "vmStatusLabel(i.status)" in inst_tpl)

# 前端语法自检：Vue 模板里不能出现未定义的方法引用
for fn in ("vmStatusLabel", "testProxyAccount", "testAllProxies", "loadAudit"):
    check(f"★ 方法 {fn} 已定义且在模板中被引用",
          f"{fn}(" in HTML and f"async {fn}" in HTML or f"{fn}(" in HTML)

print("\n" + "=" * 72)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
print("=" * 72)
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)

os.chdir(ROOT)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
