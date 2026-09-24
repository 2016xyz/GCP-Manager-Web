# -*- coding: utf-8 -*-
"""
测试工具的共享夹具

为什么需要这个：多个浏览器端校验脚本都要登录控制台，
把密码硬编码在各脚本里，一旦开发库改过密码就集体失效
（本轮就踩到了：改了 admin 密码后 overflow_audit 直接超时）。

现在统一从 `data/INITIAL_ADMIN.txt` 读取，并允许用环境变量
`GCPWEB_ADMIN_PW` 覆盖，避免密码散落在多处。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("GCPWEB_DATA_DIR") or os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "gcp_web.db")
ADMIN_FILE = os.path.join(DATA_DIR, "INITIAL_ADMIN.txt")

ADMIN_USER = "admin"


def admin_password():
    """优先环境变量，其次读 INITIAL_ADMIN.txt"""
    env = os.environ.get("GCPWEB_ADMIN_PW")
    if env:
        return env
    try:
        with open(ADMIN_FILE, encoding="utf-8") as f:
            m = re.search(r"密码:\s*(\S+)", f.read())
            if m:
                return m.group(1)
    except OSError:
        pass
    raise SystemExit(
        f"读不到管理员密码。请设置 GCPWEB_ADMIN_PW 环境变量，"
        f"或确认 {ADMIN_FILE} 存在。")


# ── 展示用假实例 ──────────────────────────────────────────────
# 实例列表的数据来自真实 GCP，本项目当前 0 台；要在浏览器里验证
# 新列（备注/所在地/费用/密码…）就必须注入代表性数据。
INST_1 = "vm-1-48904-1-7286"
INST_2 = "vm-2-48904-3-9911"
SEED_PW_1 = "R00t-7286!Aa1"

FAKE_INSTANCES = [
    {"name": INST_1, "ip": "34.136.20.11", "private_ip": "10.128.0.2",
     "zone": "us-central1-a", "region": "us-central1",
     "location": "us-central1 (爱荷华)", "status": "RUNNING",
     "machine_type": "e2-micro", "disk_type": "pd-standard", "disk_size_gb": 30,
     "image": "ubuntu-2204-jammy-v20260901", "image_source": "", "created": "", "created_ts": 0,
     "preemptible": False, "spot": False, "project_id": "p1",
     "account_email": "gcp-manager-svc@sincere-axon-354618.iam.gserviceaccount.com",
     "account_id": 1, "account_label": "主力账号", "has_password": True,
     "note": "客户A环境", "installs": ["docker", "nps"],
     "cost": {"hourly_usd": 0.010055, "daily_usd": 0.2413, "used_usd": 3.618,
              "used_hours": 359.8, "free_tier": True,
              "free_tier_reason": "规格落在 Always Free 额度内", "priced": True},
     "spec": {"machine_type": "e2-micro", "image_key": "ubuntu-2204-lts",
              "disk_type": "pd-standard", "disk_size_gb": 30}},
    {"name": INST_2, "ip": "104.199.7.88", "private_ip": "10.140.0.3",
     "zone": "asia-east1-b", "region": "asia-east1",
     "location": "asia-east1 (台湾)", "status": "TERMINATED",
     "machine_type": "n2-standard-4", "disk_type": "pd-ssd", "disk_size_gb": 100,
     "image": "debian-12-bookworm-v20260101", "image_source": "", "created": "", "created_ts": 0,
     "preemptible": False, "spot": True, "project_id": "p2",
     "account_email": "very-long-service-account-name@extremely-long-project-id-123456"
                      ".iam.gserviceaccount.com",
     "account_id": 2, "account_label": "", "has_password": False,
     "note": "", "installs": [],
     "cost": {"hourly_usd": 0.0207, "daily_usd": 0.497, "used_usd": 12.44,
              "used_hours": 601.2, "free_tier": False,
              "free_tier_reason": "不免费：机型 n2-standard-4 不是 e2-micro", "priced": True},
     "spec": {"machine_type": "n2-standard-4", "image_key": "debian-12",
              "disk_type": "pd-ssd", "disk_size_gb": 100}},
]

FAKE_ACCOUNTS = [
    {"id": 1, "email": "gcp-manager-svc@sincere-axon-354618.iam.gserviceaccount.com",
     "project_id": "sincere-axon-354618", "label": "主力账号", "key_file": "sa1.json",
     "key_exists": True, "proxy_set": True, "proxy_ok": True, "proxy_error": "",
     "proxy_type": "SOCKS5H", "proxy_type_label": "SOCKS5（代理端解析 DNS，推荐）",
     "proxy_has_auth": True, "proxy_display": "socks5h://proxyuser:***@203.0.113.9:1080",
     "proxy_host": "203.0.113.9", "proxy_port": "1080"},
    {"id": 2, "email": "another-very-long-account-address@another-long-project-99"
                       ".iam.gserviceaccount.com",
     "project_id": "another-long-project-99", "label": "", "key_file": "sa2.json",
     "key_exists": True, "proxy_set": False, "proxy_ok": True, "proxy_error": "",
     "proxy_type": "HTTPS", "proxy_type_label": "HTTPS 代理（HTTP CONNECT）",
     "proxy_has_auth": False, "proxy_display": "", "proxy_host": "", "proxy_port": ""},
]


def seed_vm(store_path=None):
    """把假实例写进本地库（root 密码二次验证需要库里有记录），返回写入的名字"""
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from core.store import Store
    st = Store(store_path or DB_PATH)
    seeded = []
    for i in FAKE_INSTANCES:
        if not i["has_password"]:
            continue
        st.save_vm(i["name"], i["ip"], SEED_PW_1, i["account_id"], i["zone"],
                   i["machine_type"], i["spec"]["image_key"],
                   i["disk_type"], i["disk_size_gb"],
                   note=i["note"], installs=",".join(i["installs"]))
        seeded.append(i["name"])
    return st, seeded


def inject_js(var_expr, what="instances"):
    """生成把夹具注入 Vue 状态的 JS 片段（要嵌在 () => {} 里）"""
    import json
    data = FAKE_INSTANCES if what == "instances" else FAKE_ACCOUNTS
    return f"{var_expr}.{what} = {json.dumps(data, ensure_ascii=False)};"


VUE_EXPR = "document.querySelector('#app').__vue_app__._container._vnode.component.proxy"