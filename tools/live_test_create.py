#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用真实 GCP 服务账号做端到端实测。

覆盖本轮修复的问题：
  1. 全开防火墙硬编码 global/networks/default → 自定义 VPC 项目 404
  2. 防火墙失败被当作「实例创建失败」并触发换区重试（全局资源换区无用）
  3. 用户选的自定义参数（机型/区域/网络）是否真的落到 GCP 上

⚠ 会真实创建 1 台实例 → 默认跑完自动删除并核对无残留。
   加 --keep 可保留（便于人工核对），此时会打印删除命令。
用法：
  python3 tools/live_test_create.py /tmp/sa.json
  python3 tools/live_test_create.py /tmp/sa.json --region asia-south2 --keep
"""
import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from google.cloud import compute_v1 as _cv  # noqa: E402
from core.gcp import GCPService, build_instance_spec, _network_short_name  # noqa: E402

PASS, FAIL, INFO = [], [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   [{detail}]" if detail else ""))


def info(msg):
    INFO.append(msg)
    print(f"  · {msg}")


ap = argparse.ArgumentParser()
ap.add_argument("sa_json")
ap.add_argument("--region", default="asia-south2")
ap.add_argument("--machine", default="e2-micro")
ap.add_argument("--image", default="ubuntu-minimal-2204")
ap.add_argument("--disk-type", default="pd-standard")
ap.add_argument("--disk-size", type=int, default=30)
ap.add_argument("--keep", action="store_true", help="不自动删除实例")
args = ap.parse_args()

sa = json.load(open(args.sa_json, encoding="utf-8"))
g = GCPService(args.sa_json, sa["project_id"], sa["client_email"])
print("=" * 78)
print(f"真实 GCP 端到端实测   项目 {g.project_id}")
print("=" * 78)

# ── 0. 环境勘察（只读）──────────────────────────────────────────────
print("\n【0. 环境勘察（只读）】")
nets = g.list_networks()
info(f"VPC 列表：{nets}")
ck("★ 项目里没有 default VPC（复现用户环境 → 旧代码必失败）",
   "default" not in nets, f"nets={nets}")
target_net = "jxihegwg" if "jxihegwg" in nets else (nets[0] if nets else "")
ck("★ 找到目标自定义 VPC", bool(target_net), target_net)

subnets = g.list_subnetworks(args.region)
info(f"{args.region} 子网：{subnets}")
target_sub = subnets[0] if subnets else ""
ck("★ 找到该区域可用子网", bool(target_sub), target_sub)

zones = g.list_zones(args.region)
info(f"{args.region} 可用区：{zones}")
ck("★ 区域可用", bool(zones), str(zones))

# ── 1. 构建 spec（模拟前端传参）─────────────────────────────────────
print("\n【1. 构建 spec（完全模拟前端提交自定义配置）】")
raw_spec = {
    "machine_type": args.machine, "image_key": args.image,
    "disk_type": args.disk_type, "disk_size_gb": args.disk_size,
    "region_mode": "single", "region": args.region,
    "network": target_net, "subnet": target_sub, "network_tier": "STANDARD",
    "tags": ["http-server", "https-server"], "assign_public_ip": True,
    "auto_open_firewall": True,          # 用户勾了「全开放防火墙」
    "disable_ops_agent": True, "no_backup": True,
    "no_snapshot_schedule": True, "no_resource_policy": True,
    "deletion_protection": False, "preemptible": False, "spot": False,
}
spec = build_instance_spec(raw_spec)
ck("★ 机型保留为自定义值", spec["machine_type"] == args.machine, spec["machine_type"])
ck("★ 区域保留为指定值（非 auto_free 兜底）",
   spec["region"] == args.region, spec["region"])
ck("★ ★ network_url 指向用户选的 VPC（旧代码防火墙用的是 default）",
   spec["network_url"] == f"global/networks/{target_net}", spec["network_url"])
ck("★ subnet_url 指向该 VPC 的子网",
   target_sub in spec["subnet_url"], spec["subnet_url"])

# ── 2. 防火墙覆盖探测 ───────────────────────────────────────────────
print("\n【2. 防火墙覆盖探测】")
missing, why = g.firewall_coverage(spec["network"], spec["network_url"])
info(f"缺失的全开规则方向：{missing or '（都已有）'}（{why}）")
ck("★ 探测函数可用且指向正确的 VPC", isinstance(missing, list), f"{missing} {why}")

# ── 3. 真实创建 ─────────────────────────────────────────────────────
name = f"livetest-{int(time.time())}"
zone = zones[0]
print(f"\n【3. 真实创建】{name}  @ {zone}")
print(f"    机型 {args.machine} / 镜像 {args.image} / "
      f"{args.disk_type} {args.disk_size}GB / 网络 {target_net}/{target_sub}")
t0 = time.time()
ok, res = g.create_instance(zone, name, startup_script="", spec=spec)
dt = time.time() - t0
ck(f"★ ★ 实例创建成功（修复前必然失败）（{dt:.1f}s）", ok,
   str(res)[:400] if not ok else "")
if not ok:
    print("\n创建失败，终止。")
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str)[:1500])
    sys.exit(1)

ip = res.get("ip", "")
z_actual = res.get("zone", zone)
info(f"实例名 {name}")
info(f"公网 IP {ip or '(无)'}")
info(f"实际区域 {z_actual}")
if res.get("warning"):
    info(f"警告 {res['warning']}")
info(f"防火墙 {res.get('firewall', '(未处理)')}")

ck("★ 拿到了公网 IP", bool(ip), ip or "无")
ck("★ 落在指定区域（未被换区）",
   z_actual.rsplit("-", 1)[0] == args.region, f"{z_actual} vs {args.region}")
ck("★ 防火墙不是致命错误（实例照样建出来）", ok)
if "firewall_ok" in res:
    ck("★ 防火墙建立成功", bool(res["firewall_ok"]), res.get("firewall", ""))

# ── 4. 云端核对（不信本地返回值，直接查 GCP）────────────────────────
print("\n【4. 云端核对（绕开本地返回值，直接查 GCP）】")
try:
    inst = g.instance_client.get(project=g.project_id, zone=z_actual, instance=name)
    mt = inst.machine_type.rsplit("/", 1)[-1]
    nic = inst.network_interfaces[0]
    net_hit = _network_short_name(nic.network)
    sub_hit = _network_short_name(nic.subnetwork)
    disk = inst.disks[0]
    ck("★ ★ 云端网络是 jxihegwg（不是 default）", net_hit == target_net,
       f"{net_hit} vs {target_net}")
    ck("★ 云端子网正确", sub_hit == target_sub, f"{sub_hit} vs {target_sub}")
    has_ip = bool(list(nic.access_configs)) and bool(nic.access_configs[0].nat_i_p)
    ck("★ 公有 IP 已分配", has_ip, nic.access_configs[0].nat_i_p if has_ip else "无")
    # ★ 磁盘属性必须查盘本身：GCP 的 instances.get 对已挂载盘
    # 不返回 initialize_params（那里是 0/空），只有 disks.get 才有真实值。
    disk_name = disk.source.rsplit("/", 1)[-1]
    dsk = _cv.DisksClient.from_service_account_json(args.sa_json).get(
        project=g.project_id, zone=z_actual, disk=disk_name)
    ck("★ 磁盘容量正确", dsk.size_gb == args.disk_size,
       f"{dsk.size_gb}GB vs 请求 {args.disk_size}GB")
    ck("★ ★ 磁盘类型正确（pd-standard 而非默认 pd-balanced）",
       args.disk_type in str(dsk.type_), str(dsk.type_))
    ck("★ ★ 磁盘确实为 pd-standard（按小时费率档位）",
       str(dsk.type_).endswith("pd-standard"), str(dsk.type_))
    ck("★ 标签已设置", "http-server" in (inst.tags.items or []),
       str(inst.tags.items))
    # 省钱项核对
    meta = {i.key: i.value for i in (inst.metadata.items or [])}
    ck("★ 省钱：Ops Agent 已禁用",
       meta.get("google-logging-enabled") == "false", str(meta)[:120])
    rp = getattr(dsk, "resource_policies", None) or []
    ck("★ 省钱：未挂资源策略（无备份）", not rp, str(list(rp)))
    ck("★ 删除保护已关闭", not inst.deletion_protection,
       str(inst.deletion_protection))
except Exception as e:
    ck("云端核对", False, f"{type(e).__name__}: {e}")

# ── 5. 防火墙云端核对 ──────────────────────────────────────────────
print("\n【5. 全开防火墙云端核对】")
try:
    rules = list(g.firewall_client.list(project=g.project_id))
    mine = [f for f in rules
            if _network_short_name(getattr(f, "network", "")) == target_net]
    all_rules = [f for f in mine
                 if any((a.I_p_protocol or "").lower() == "all" for a in (f.allowed or []))]
    info(f"该 VPC 上协议为 all 的规则：{[f.name for f in all_rules]}")
    ing = [f.name for f in all_rules
           if (f.direction or "INGRESS").upper() == "INGRESS"
           and "0.0.0.0/0" in (f.source_ranges or [])]
    egr = [f.name for f in all_rules
           if (f.direction or "").upper() == "EGRESS"
           and "0.0.0.0/0" in (f.destination_ranges or [])]
    ck("★ ★ 入站有全放行规则（绑定 jxihegwg，不是 default）", bool(ing), str(ing))
    ck("★ ★ 出站有全放行规则（绑定 jxihegwg）", bool(egr), str(egr))
    bad = [f.name for f in rules
           if _network_short_name(getattr(f, "network", "")) == "default"]
    ck("★ 没有跑到不存在的 default 网络上", not bad, str(bad))
except Exception as e:
    ck("防火墙核对", False, f"{type(e).__name__}: {e}")

# ── 6. 清理（默认）──────────────────────────────────────────────────
print("\n【6. 清理】")
if args.keep:
    print(f"  ⚠ --keep：保留实例 {name} ({ip})")
    print(f"     删除命令：")
    print(f"       gcloud compute instances delete {name} --zone={z_actual} --quiet")
    print(f"     或在本工具「实例列表」中勾选删除。")
else:
    ok_del, msg_del = g.delete_instance(z_actual, name)
    ck("★ 实例已删除", ok_del, msg_del)
    # 核对无残留（实例 + 磁盘）
    deadline = time.time() + 90
    gone = False
    while time.time() < deadline:
        try:
            g.instance_client.get(project=g.project_id, zone=z_actual, instance=name)
            time.sleep(5)
        except Exception:
            gone = True
            break
    ck("★ 实例确实已不存在", gone)
    # 孤儿磁盘核对：用临时 DisksClient 直接查（GCPService 没有暴露磁盘客户端）
    orphans = []
    try:
        from google.cloud import compute_v1 as _cv
        dc = _cv.DisksClient.from_service_account_json(args.sa_json)
        for z in zones:
            try:
                for d in dc.list(project=g.project_id, zone=z):
                    if name in d.name:
                        orphans.append(f"{z}/{d.name}")
            except Exception:
                pass
    except Exception as e:
        info(f"孤儿磁盘核对跳过：{e}")
    ck("★ 无孤儿磁盘残留", not orphans, str(orphans))

print("\n" + "=" * 78)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("   ✗", f)
sys.exit(1 if FAIL else 0)