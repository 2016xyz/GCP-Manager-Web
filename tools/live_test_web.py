#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
走完整 Web 接口链路做真实端到端验证。

与 live_test_create.py 的区别：那个直接调 core.gcp，这个走 HTTP
（导入账号 → 读目录 → 提交创建 → 轮询任务 → 云端核对 → 删除），
验证的是「前端点下去」的那条路径。

⚠ 会真实创建实例并自动删除。
用法：python3 tools/live_test_web.py /tmp/sa.json
"""
import json
import os
import shutil
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

TMP = tempfile.mkdtemp(prefix="gw_live_web_")
os.environ["GCPWEB_DATA_DIR"] = TMP
for f in ("gcp_web.db", "INITIAL_ADMIN.txt"):
    s = os.path.join(BASE, "data", f)
    if os.path.exists(s):
        shutil.copy(s, os.path.join(TMP, f))
shutil.copytree(os.path.join(BASE, "data", "keys"), os.path.join(TMP, "keys"),
                dirs_exist_ok=True)

import google.cloud.compute_v1 as cv  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
from core import auth as auth_mod  # noqa: E402

client = TestClient(appmod.app)
PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   [{str(detail)[:180]}]" if detail else ""))


def login(u, p):
    c = client.get("/api/auth/captcha").json()
    code = auth_mod.captcha_store._items.get(c["captcha_id"], {}).get("code")
    return client.post("/api/auth/login", json={
        "username": u, "password": p,
        "captcha_id": c["captcha_id"], "captcha_code": code})


sa_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sa_test.json"
sa = json.load(open(sa_path, encoding="utf-8"))
proj = sa["project_id"]

admin_pw = ""
for line in open(os.path.join(TMP, "INITIAL_ADMIN.txt"), encoding="utf-8"):
    if line.startswith("密码:"):
        admin_pw = line.split(":", 1)[1].strip()

print("=" * 78)
print(f"Web 接口链路真实端到端   项目 {proj}")
print("=" * 78)

print("\n【1. 导入服务账号】")
r = login("admin", admin_pw)
ck("管理员登录", r.status_code == 200, r.text[:100])
accounts = client.get("/api/accounts").json().get("accounts", [])
if not accounts:
    with open(sa_path, "rb") as f:
        r = client.post("/api/accounts/upload", files={"file": ("sa.json", f, "application/json")})
    ck("上传服务账号 JSON", r.status_code == 200, r.text[:160])
    accounts = client.get("/api/accounts").json().get("accounts", [])
ck("账号已就绪", bool(accounts), str([a.get("email") for a in accounts]))
if not accounts:
    sys.exit(1)
acc = accounts[0]
print(f"  · 账号 id={acc['id']}  {acc.get('email')}")

print("\n【2. 读取项目真实 VPC / 子网（前端「刷新网络」走的接口）】")
r = client.get(f"/api/project_networks?account_id={acc['id']}&region=asia-south2")
d = r.json()
print(f"  · networks={d.get('networks')}  subnets={d.get('subnets')}  "
      f"has_default={d.get('has_default_network')}")
ck("★ 接口正确报告没有 default 网络", d.get("has_default_network") is False,
   str(d.get("has_default_network")))
ck("★ 返回真实自定义 VPC", "jxihegwg" in (d.get("networks") or []), str(d.get("networks")))
net = (d.get("networks") or [""])[0]
sub = (d.get("subnets") or [""])[0]

print("\n【3. 提交创建任务（自定义配置 + 全开放防火墙）】")
payload = {
    "account_ids": [acc["id"]], "count": 1,
    "spec": {
        "machine_type": "e2-micro", "image_key": "ubuntu-minimal-2204",
        "disk_type": "pd-standard", "disk_size_gb": 30,
        "region_mode": "single", "region": "asia-south2",
        "network": net, "subnet": sub, "network_tier": "STANDARD",
        "tags": ["http-server", "https-server"], "assign_public_ip": True,
        "auto_open_firewall": True,
        "disable_ops_agent": True, "no_backup": True,
        "no_snapshot_schedule": True, "no_resource_policy": True,
        "deletion_protection": False, "preemptible": False, "spot": False,
    },
    "login_mode": "root_password", "pwdMode": "random",
    "note": "web-livetest", "installs": [],
    "post_command": "", "verify_command": "",
    "concurrency": 1, "account_workers": 1, "retry_count": 2,
}
r = client.post("/api/create", json=payload)
res = r.json()
ck("★ 创建任务已受理", res.get("ok"), str(res)[:160])
task_id = res.get("task_id")
print(f"  · 任务 {task_id}")

print("\n【4. 轮询任务直到结束】")
vm_name = ""
vm_ip = ""
vm_zone = ""
final = {}
# ★ 响应是 {"ok":true,"task":{...}}，状态在 task.status 里（第一版直接读顶层
# 的 status/done，永远读不到 → 只能干等满超时，还差点误判成失败）
for _ in range(90):
    time.sleep(5)
    rr = client.get(f"/api/tasks/{task_id}").json()
    final = rr.get("task") or {}
    if final.get("status") in ("done", "failed", "finished", "error", "cancelled"):
        break
print(f"  · 状态 {final.get('status')}  结果 {final.get('result') or final.get('summary')}")
# /api/logs 返回的是日志对象列表（不是字符串），按 task_id 过滤后逐条打印
log_items = client.get(f"/api/logs?task_id={task_id}").json().get("logs") or []
log_text = "\n".join(
    (x.get("message") or x.get("msg") or str(x)) if isinstance(x, dict) else str(x)
    for x in log_items)
print("  ---- 相关日志 ----")
for line in log_text.splitlines()[-14:]:
    print("   ", line[:210])
ck("★ ★ 日志已打印实际网络（原来只能靠猜）", f"网络={net}/" in log_text or f"网络={net}" in log_text,
   f"共 {len(log_items)} 条")

# 真实结构：result.results[].instances[]（第一版猜的 items 字段不存在）
_ctr = 1
if _ctr:
    items = []
    for blk in ((final.get("result") or {}).get("results") or []):
        items.extend(blk.get("instances") or [])
created = [i for i in items if i.get("ok") and i.get("ip")]
ck("★ ★ 至少 1 台创建成功（修复前 0 台）", bool(created),
   str([(i.get("name"), i.get("error")) for i in items])[:300])
ck("★ ★ 没有出现 networks/default not found",
   "networks/default" not in log_text and "was not found" not in log_text, "")
if created:
    vm_name = created[0].get("name", "")
    vm_ip = created[0].get("ip", "")
    vm_zone = created[0].get("zone", "")
    print(f"  · 实例 {vm_name} @ {vm_zone}  IP {vm_ip}")

print("\n【5. 云端核对（直接查 GCP）】")
dc = cv.DisksClient.from_service_account_json(sa_path)
if vm_name:
    inst = None
    for z, agg in cv.InstancesClient.from_service_account_json(sa_path).aggregated_list(project=proj):
        for i in (agg.instances or []):
            if i.name == vm_name:
                inst, zone_hit = i, z.split("/")[-1]
    ck("★ 实例真的存在于云端", inst is not None, vm_name)
    if inst:
        nic = inst.network_interfaces[0]
        ck("★ ★ 云端网络 = 所选 VPC", nic.network.rsplit("/", 1)[-1] == net,
           nic.network)
        ck("★ ★ 云端子网 = 所选子网", nic.subnetwork.rsplit("/", 1)[-1] == sub,
           nic.subnetwork)
        ck("★ 机型一致", inst.machine_type.rsplit("/", 1)[-1] == "e2-micro",
           inst.machine_type)
    rules = list(cv.FirewallsClient.from_service_account_json(sa_path).list(project=proj))
    on_net = [f for f in rules if f.network.rsplit("/", 1)[-1] == net]
    allproto = [f for f in on_net if any((a.I_p_protocol or "").lower() == "all"
                                        for a in (f.allowed or []))]
    ck("★ ★ 全开规则绑定在所选 VPC 上",
       any("0.0.0.0/0" in (f.source_ranges or []) for f in allproto),
       str([f.name for f in allproto]))
    ck("★ 没有规则被建到不存在的 default 网络上",
       not [f for f in rules if f.network.rsplit("/", 1)[-1] == "default"
            and "allow-all" in f.name], "")

print("\n【6. 清理（走 Web 接口删除）】")
if vm_name:
    # ★ targets 传**实例名**（前端就是这么传的：oneAction/instanceAction 都传 name）
    r = client.post("/api/instance_action", json={
        "action": "delete", "targets": [vm_name]})
    print(f"  · 删除响应 {r.status_code} {r.text[:160]}")
    act_id = (r.json() or {}).get("task_id")
    # 等删除任务本身结束（比反复查列表可靠）
    # 注意：asia-south2 上删除实例实测要 ~134 秒（实例可见性一直保持到
    # 133.7s 才消失，operation.done() 在 134.1s），这是 GCP 的真实速度，
    # 不是代码在空等，所以窗口要给足。
    act = {}
    for _ in range(80):
        time.sleep(4)
        act = client.get(f"/api/tasks/{act_id}").json().get("task") or {}
        if act.get("status") in ("done", "failed", "error"):
            break
    print(f"  · 删除任务 {act.get('status')}：{act.get('message')}")
    ck("★ 删除任务执行成功", act.get("status") == "done", str(act.get("message")))
    # 以云端为准核对（不信本地列表缓存）
    gone = False
    left = 0
    for _ in range(24):
        time.sleep(5)
        left = 0
        for z, agg in cv.InstancesClient.from_service_account_json(sa_path).aggregated_list(project=proj):
            for i in (agg.instances or []):
                if i.name == vm_name:
                    left += 1
        if left == 0:
            gone = True
            break
    ck("★ ★ 实例已从云端删除", gone, f"剩余同名实例 {left}")
    total = 0
    for z, agg in cv.InstancesClient.from_service_account_json(sa_path).aggregated_list(project=proj):
        total += len(agg.instances or [])
    ck("★ 云上无实例残留", total == 0, f"残留 {total}")

print("\n" + "=" * 78)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("   ✗", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)