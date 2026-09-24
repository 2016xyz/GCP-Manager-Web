#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实测「账号可改代理」与「每账号机器数」两个新接口。

走夹具（8001）以便拿到验证码明文。
重点验证的是**边界**，不是happy path：
  · 非法代理要被拒绝（不是静默存进去，等创建时才炸）
  · 清空代理要真的变成直连
  · 明文代理密码不能出现在响应里
  · 统计接口要返回 live/local 两个数，且单账号失败不拖垮整体
"""
import sys

import httpx

sys.path.insert(0, "tools")
from _fixtures import ADMIN_USER, admin_password  # noqa: E402

B = "http://127.0.0.1:8001"
c = httpx.Client(base_url=B, timeout=120)
fails = []


def ck(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# ── 登录（夹具暴露验证码明文）──────────────────────────────
cap = c.get("/api/auth/captcha").json()
plain = c.get(f"/__probe/captcha/{cap['captcha_id']}").json().get("code")
r = c.post("/api/auth/login", json={"username": ADMIN_USER,
                                    "password": admin_password(),
                                    "captcha_id": cap["captcha_id"],
                                    "captcha_code": plain})
ck("登录成功", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

accs = c.get("/api/accounts").json()["accounts"]
if not accs:
    print("!! 库中无账号，无法继续；请先导入一个服务账号")
    sys.exit(2)
aid = accs[0]["id"]
print(f"\n测试账号 id={aid} label={accs[0].get('label')!r} proxy_set={accs[0].get('proxy_set')}")

print("\n【1. 改代理：合法值】")
r = c.patch(f"/api/accounts/{aid}", json={"proxy": "socks5h://u:p@127.0.0.1:1080",
                                          "proxy_type": "SOCKS5"})
ck("PATCH 返回 ok", r.status_code == 200 and r.json().get("ok"), r.text[:150])
a = [x for x in c.get("/api/accounts").json()["accounts"] if x["id"] == aid][0]
ck("代理已生效（proxy_set=True）", a["proxy_set"], str(a.get("proxy_set")))
ck("类型归一化为 SOCKS5H", a["proxy_type"] == "SOCKS5H", str(a.get("proxy_type")))
ck("明文密码不外发", "u:p" not in str(a), str(a.get("proxy_display")))
ck("密码已打码", "***" in (a.get("proxy_display") or ""), str(a.get("proxy_display")))

print("\n【2. 改代理：非法值必须被拒绝】")
for bad, why in [("1.2.3.4", "缺端口"), ("socks9://1.2.3.4:1080", "协议名错(拼错 socks5)"),
                 ("ftp://1.2.3.4:1080", "根本不支持的协议"),
                 ("socks4://u:p@1.2.3.4:1080", "socks4 不支持认证")]:
    r = c.patch(f"/api/accounts/{aid}", json={"proxy": bad, "proxy_type": "HTTPS"})
    ok = r.status_code == 400
    ck(f"拒绝「{why}」并给出原因", ok, f"{r.status_code} {r.text[:90]}")
    if ok:
        print(f"       → {r.json().get('detail')}")

print("\n【3. 非法值被拒后，原代理不能被破坏】")
a = [x for x in c.get("/api/accounts").json()["accounts"] if x["id"] == aid][0]
ck("原代理仍在", a["proxy_type"] == "SOCKS5H" and a["proxy_set"], str(a.get("proxy_type")))

print("\n【4. 清空代理 → 直连】")
r = c.patch(f"/api/accounts/{aid}", json={"proxy": "", "proxy_type": "HTTPS"})
ck("清空返回 ok", r.status_code == 200, r.text[:120])
a = [x for x in c.get("/api/accounts").json()["accounts"] if x["id"] == aid][0]
ck("proxy_set 变 False", not a["proxy_set"], str(a.get("proxy_set")))
ck("前端可据此显示「直连」", a["proxy_set"] is False)

print("\n【5. 备注修改仍正常（别被代理改动带坏）】")
r = c.patch(f"/api/accounts/{aid}", json={"label": "冒烟测试备注"})
ck("改备注 ok", r.status_code == 200, r.text[:120])
a = [x for x in c.get("/api/accounts").json()["accounts"] if x["id"] == aid][0]
ck("备注已保存", a["label"] == "冒烟测试备注", str(a.get("label")))
# 复原
c.patch(f"/api/accounts/{aid}", json={"label": accs[0].get("label") or ""})

print("\n【6. 备注超长要被拒】")
r = c.patch(f"/api/accounts/{aid}", json={"label": "x" * 101})
ck("拒绝 101 字备注", r.status_code == 400, f"{r.status_code}")

print("\n【7. 机器数统计接口】")
r = c.get("/api/accounts/instance_counts")
ck("统计返回 200", r.status_code == 200, f"{r.status_code}")
j = r.json() if r.status_code == 200 else {}
ck("有 counts 列表", isinstance(j.get("counts"), list), str(j)[:150])
if j.get("counts"):
    row = j["counts"][0]
    print(f"       样例: {row}")
    ck("含 account_id", "account_id" in row)
    ck("含 inst_count_live（可为 None）", "inst_count_live" in row)
    ck("含 inst_count_local", "inst_count_local" in row)
    ck("统计耗时可观测", isinstance(j.get("elapsed_ms"), int), str(j.get("elapsed_ms")))
    ck("带 cached 标记", "cached" in j, str(j.get("cached")))

print("\n【8. /api/accounts 也带机器数（供表格直接渲染）】")
a = [x for x in c.get("/api/accounts").json()["accounts"] if x["id"] == aid][0]
ck("/api/accounts 含 inst_count_local", "inst_count_local" in a, str(sorted(a.keys())))
ck("/api/accounts 含 inst_count_live", "inst_count_live" in a)

print("\n【9. 无代理时 label 回空串而非 None（前端 v-if 依赖）】")
ck("label 是字符串", isinstance(a.get("label"), str), repr(a.get("label")))

print()
if fails:
    print(f"❌ {len(fails)} 项失败：")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")