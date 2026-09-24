# -*- coding: utf-8 -*-
"""
GCP 资源配置目录（机型 / 镜像 / 磁盘 / 区域 / 价格）

设计目标：让「服务器配置」完全由前端自定义选择，而不是像原版 v7.6 那样
硬编码在 GCP_Manager_v7.6.py 的常量里（DEFAULT_MACHINE_TYPE / DEFAULT_IMAGE_FAMILY /
DEFAULT_DISK_SIZE_GB 等）。

数据说明（诚实标注）：
  - hourly_usd 为 us-central1 区域的按需(list price)参考单价，单位 美元/小时。
  - 实际计费以 GCP 账单为准；不同区域按 region_price_index 折算，仅供参考。
  - free_tier 标记 GCP 永久免费额度覆盖的机型（e2-micro，限 us-west1/us-central1/us-east1）。
"""

# ---------------------------------------------------------------------------
# 1. 免费区 / 付费区（复用原版 v7.6 的区域池）
# ---------------------------------------------------------------------------
FREE_REGIONS = {
    "us-central1": "us-central1 (爱荷华)",
    "us-east1": "us-east1 (南卡罗来纳)",
    "us-west1": "us-west1 (俄勒冈)",
}

PAID_REGIONS = {
    # 美国
    "us-central2": "us-central2 (达拉斯)", "us-east2": "us-east2 (俄亥俄)",
    "us-east3": "us-east3 (南卡罗来纳)", "us-east4": "us-east4 (北弗吉尼亚)",
    "us-east5": "us-east5 (俄亥俄)", "us-west2": "us-west2 (洛杉矶)",
    "us-west3": "us-west3 (盐湖城)", "us-west4": "us-west4 (拉斯维加斯)",
    "us-south1": "us-south1 (德克萨斯)",
    # 亚洲
    "asia-east1": "asia-east1 (台湾)", "asia-east2": "asia-east2 (香港)",
    "asia-northeast1": "asia-northeast1 (东京)", "asia-northeast2": "asia-northeast2 (大阪)",
    "asia-northeast3": "asia-northeast3 (首尔)", "asia-south1": "asia-south1 (孟买)",
    "asia-south2": "asia-south2 (德里)", "asia-southeast1": "asia-southeast1 (新加坡)",
    "asia-southeast2": "asia-southeast2 (雅加达)",
    # 欧洲
    "europe-west1": "europe-west1 (比利时)", "europe-west2": "europe-west2 (伦敦)",
    "europe-west3": "europe-west3 (法兰克福)", "europe-west4": "europe-west4 (荷兰)",
    "europe-west6": "europe-west6 (苏黎世)", "europe-west8": "europe-west8 (米兰)",
    "europe-west9": "europe-west9 (巴黎)", "europe-west10": "europe-west10 (柏林)",
    "europe-west12": "europe-west12 (都灵)", "europe-central2": "europe-central2 (华沙)",
    "europe-north1": "europe-north1 (芬兰)", "europe-southwest1": "europe-southwest1 (马德里)",
    # 其他
    "australia-southeast1": "australia-southeast1 (悉尼)", "australia-southeast2": "australia-southeast2 (墨尔本)",
    "me-central1": "me-central1 (多哈)", "me-central2": "me-central2 (利雅得)",
    "me-west1": "me-west1 (特拉维夫)", "southamerica-east1": "southamerica-east1 (圣保罗)",
    "southamerica-west1": "southamerica-west1 (圣地亚哥)",
    "northamerica-northeast1": "northamerica-northeast1 (蒙特利尔)",
    "northamerica-northeast2": "northamerica-northeast2 (多伦多)",
}

ALL_REGIONS = {**FREE_REGIONS, **PAID_REGIONS}

# 区域价格系数（相对 us-central1 的粗略折算，仅用于界面估算）
REGION_PRICE_INDEX = {
    "us-central1": 1.00, "us-east1": 1.00, "us-west1": 1.00,
    "us-central2": 1.00, "us-east2": 1.00, "us-east3": 1.00,
    "us-east4": 1.02, "us-east5": 1.02, "us-west2": 1.06, "us-west3": 1.05,
    "us-west4": 1.05, "us-south1": 1.02,
    "asia-east1": 1.10, "asia-east2": 1.20, "asia-northeast1": 1.12,
    "asia-northeast2": 1.12, "asia-northeast3": 1.12, "asia-south1": 1.02,
    "asia-south2": 1.02, "asia-southeast1": 1.10, "asia-southeast2": 1.10,
    "europe-west1": 1.08, "europe-west2": 1.10, "europe-west3": 1.10,
    "europe-west4": 1.08, "europe-west6": 1.13, "europe-west8": 1.10,
    "europe-west9": 1.10, "europe-west10": 1.10, "europe-west12": 1.10,
    "europe-central2": 1.08, "europe-north1": 1.05, "europe-southwest1": 1.08,
    "australia-southeast1": 1.15, "australia-southeast2": 1.15,
    "me-central1": 1.12, "me-central2": 1.12, "me-west1": 1.12,
    "southamerica-east1": 1.20, "southamerica-west1": 1.20,
    "northamerica-northeast1": 1.05, "northamerica-northeast2": 1.05,
}

FREE_TIER_REGIONS = ["us-west1", "us-central1", "us-east1"]

# ---------------------------------------------------------------------------
# 2. 机器类型（机型）
#    每个区域可用的机型不同，用 family_allowed_regions 做过滤；未登记的 family 视为全域可用。
# ---------------------------------------------------------------------------
MACHINE_TYPES = {
    # ---- E2 通用型（免费额度机型所在系列）----
    "e2-micro":     {"family": "E2", "vcpu": 2, "mem_gb": 1.0,  "hourly_usd": 0.008411, "arch": "x86_64", "free_tier": True,  "note": "GCP 永久免费机型（每月 1 台，限 us-west1/us-central1/us-east1）"},
    "e2-small":     {"family": "E2", "vcpu": 2, "mem_gb": 2.0,  "hourly_usd": 0.016823, "arch": "x86_64"},
    "e2-medium":    {"family": "E2", "vcpu": 2, "mem_gb": 4.0,  "hourly_usd": 0.033638, "arch": "x86_64"},
    "e2-standard-2":  {"family": "E2", "vcpu": 2,  "mem_gb": 8.0,   "hourly_usd": 0.067112, "arch": "x86_64"},
    "e2-standard-4":  {"family": "E2", "vcpu": 4,  "mem_gb": 16.0,  "hourly_usd": 0.134225, "arch": "x86_64"},
    "e2-standard-8":  {"family": "E2", "vcpu": 8,  "mem_gb": 32.0,  "hourly_usd": 0.268449, "arch": "x86_64"},
    "e2-standard-16": {"family": "E2", "vcpu": 16, "mem_gb": 64.0,  "hourly_usd": 0.536899, "arch": "x86_64"},
    "e2-standard-32": {"family": "E2", "vcpu": 32, "mem_gb": 128.0, "hourly_usd": 1.073798, "arch": "x86_64"},
    "e2-highcpu-2":   {"family": "E2", "vcpu": 2,  "mem_gb": 2.0,   "hourly_usd": 0.049944, "arch": "x86_64"},
    "e2-highcpu-4":   {"family": "E2", "vcpu": 4,  "mem_gb": 4.0,   "hourly_usd": 0.099887, "arch": "x86_64"},
    "e2-highcpu-8":   {"family": "E2", "vcpu": 8,  "mem_gb": 8.0,   "hourly_usd": 0.199774, "arch": "x86_64"},
    "e2-highmem-2":   {"family": "E2", "vcpu": 2,  "mem_gb": 16.0,  "hourly_usd": 0.090044, "arch": "x86_64"},
    "e2-highmem-4":   {"family": "E2", "vcpu": 4,  "mem_gb": 32.0,  "hourly_usd": 0.180088, "arch": "x86_64"},
    "e2-highmem-8":   {"family": "E2", "vcpu": 8,  "mem_gb": 64.0,  "hourly_usd": 0.360176, "arch": "x86_64"},

    # ---- N1 通用型 ----
    "n1-standard-1":  {"family": "N1", "vcpu": 1,  "mem_gb": 3.75,  "hourly_usd": 0.0475,   "arch": "x86_64"},
    "n1-standard-2":  {"family": "N1", "vcpu": 2,  "mem_gb": 7.5,   "hourly_usd": 0.0950,   "arch": "x86_64"},
    "n1-standard-4":  {"family": "N1", "vcpu": 4,  "mem_gb": 15.0,  "hourly_usd": 0.1900,   "arch": "x86_64"},
    "n1-standard-8":  {"family": "N1", "vcpu": 8,  "mem_gb": 30.0,  "hourly_usd": 0.3800,   "arch": "x86_64"},
    "n1-standard-16": {"family": "N1", "vcpu": 16, "mem_gb": 60.0,  "hourly_usd": 0.7600,   "arch": "x86_64"},

    # ---- N2 通用型 ----
    "n2-standard-2":  {"family": "N2", "vcpu": 2,  "mem_gb": 8.0,   "hourly_usd": 0.0972,   "arch": "x86_64", "allowed_regions": ["us-central1", "us-east1", "us-west1", "us-east4", "us-west2", "europe-west1", "europe-west2", "europe-west4", "asia-east1", "asia-northeast1", "asia-southeast1"]},
    "n2-standard-4":  {"family": "N2", "vcpu": 4,  "mem_gb": 16.0,  "hourly_usd": 0.1943,   "arch": "x86_64", "allowed_regions": ["us-central1", "us-east1", "us-west1", "us-east4", "us-west2", "europe-west1", "europe-west2", "europe-west4", "asia-east1", "asia-northeast1", "asia-southeast1"]},
    "n2-standard-8":  {"family": "N2", "vcpu": 8,  "mem_gb": 32.0,  "hourly_usd": 0.3885,   "arch": "x86_64", "allowed_regions": ["us-central1", "us-east1", "us-west1", "us-east4", "europe-west1", "europe-west4", "asia-northeast1"]},
    "n2-highmem-2":   {"family": "N2", "vcpu": 2,  "mem_gb": 16.0,  "hourly_usd": 0.1311,   "arch": "x86_64", "allowed_regions": ["us-central1", "us-east1", "us-west1", "us-east4", "europe-west1", "asia-northeast1"]},

    # ---- T2D / T2A（AMD / ARM）----
    "t2d-standard-1": {"family": "T2D", "vcpu": 1, "mem_gb": 4.0,   "hourly_usd": 0.0349,   "arch": "x86_64", "allowed_regions": ["us-central1", "us-east1", "us-west1", "europe-west1", "europe-west2", "europe-west3", "europe-west4", "asia-southeast1"]},
    "t2d-standard-2": {"family": "T2D", "vcpu": 2, "mem_gb": 8.0,   "hourly_usd": 0.0698,   "arch": "x86_64", "allowed_regions": ["us-central1", "us-east1", "us-west1", "europe-west1", "europe-west2", "europe-west3", "europe-west4", "asia-southeast1"]},
    "t2d-standard-4": {"family": "T2D", "vcpu": 4, "mem_gb": 16.0,  "hourly_usd": 0.1396,   "arch": "x86_64", "allowed_regions": ["us-central1", "us-east1", "us-west1", "europe-west1", "europe-west2", "europe-west3", "europe-west4", "asia-southeast1"]},
    "t2a-standard-1": {"family": "T2A", "vcpu": 1, "mem_gb": 4.0,   "hourly_usd": 0.0383,   "arch": "arm64",  "allowed_regions": ["us-central1", "europe-west4", "asia-southeast1"]},
    "t2a-standard-2": {"family": "T2A", "vcpu": 2, "mem_gb": 8.0,   "hourly_usd": 0.0766,   "arch": "arm64",  "allowed_regions": ["us-central1", "europe-west4", "asia-southeast1"]},

    # ---- C3 / C2 计算优化型 ----
    "c3-standard-4":  {"family": "C3", "vcpu": 4,  "mem_gb": 16.0,  "hourly_usd": 0.2496,   "arch": "x86_64", "allowed_regions": ["us-central1", "us-east1", "us-east4", "us-west1", "us-west3", "europe-west1", "europe-west4", "europe-west9", "asia-southeast1"]},
    "c3-standard-8":  {"family": "C3", "vcpu": 8,  "mem_gb": 32.0,  "hourly_usd": 0.4992,   "arch": "x86_64", "allowed_regions": ["us-central1", "us-east1", "us-east4", "us-west1", "us-west3", "europe-west1", "europe-west4", "europe-west9", "asia-southeast1"]},
    "c2-standard-4":  {"family": "C2", "vcpu": 4,  "mem_gb": 16.0,  "hourly_usd": 0.2140,   "arch": "x86_64", "allowed_regions": ["us-central1", "us-east1", "us-west1", "europe-west1", "europe-west4", "asia-southeast1"]},

    # ---- M 内存优化型 ----
    "m1-megamem-96":  {"family": "M1", "vcpu": 96, "mem_gb": 1433.6, "hourly_usd": 10.376,  "arch": "x86_64", "allowed_regions": ["us-central1", "us-east1", "europe-west1", "asia-southeast1"]},

    # ---- F1 / G2（FPGA / GPU）----
    "n1-standard-4-gpu-t4": {"family": "GPU", "vcpu": 4, "mem_gb": 15.0, "hourly_usd": 0.35, "arch": "x86_64", "gpu": "nvidia-tesla-t4 x1", "note": "需申请 GPU 配额（accelerator）", "allowed_regions": ["us-central1", "us-west1", "asia-east1", "asia-southeast1"]},
}

# family 有 allowed_regions 时按白名单过滤
MACHINE_FAMILY_ORDER = ["E2", "N1", "N2", "T2D", "T2A", "C2", "C3", "M1", "GPU"]

# ---------------------------------------------------------------------------
# 3. 镜像
# ---------------------------------------------------------------------------
IMAGES = {
    "ubuntu-2404-lts":       {"label": "Ubuntu 24.04 LTS (amd64)",       "project": "ubuntu-os-cloud",     "family": "ubuntu-2404-lts-amd64",   "os": "linux", "default_user": "ubuntu",  "recommended": True},
    "ubuntu-2204-lts":       {"label": "Ubuntu 22.04 LTS (amd64)",       "project": "ubuntu-os-cloud",     "family": "ubuntu-2204-lts",         "os": "linux", "default_user": "ubuntu",  "note": "原版 v7.6 默认系列之一"},
    "ubuntu-minimal-2204":   {"label": "Ubuntu Minimal 22.04 LTS",       "project": "ubuntu-os-cloud",     "family": "ubuntu-minimal-2204-lts", "os": "linux", "default_user": "ubuntu",  "note": "★ 原版 v7.6 硬编码默认镜像"},
    "ubuntu-2004-lts":       {"label": "Ubuntu 20.04 LTS (amd64)",       "project": "ubuntu-os-cloud",     "family": "ubuntu-2004-lts",         "os": "linux", "default_user": "ubuntu"},
    "debian-12":             {"label": "Debian 12 (Bookworm)",           "project": "debian-cloud",        "family": "debian-12",               "os": "linux", "default_user": "debian",  "recommended": True},
    "debian-11":             {"label": "Debian 11 (Bullseye)",           "project": "debian-cloud",        "family": "debian-11",               "os": "linux", "default_user": "debian"},
    "rocky-9":               {"label": "Rocky Linux 9",                  "project": "rocky-linux-cloud",   "family": "rocky-linux-9",           "os": "linux", "default_user": "rocky"},
    "rocky-8":               {"label": "Rocky Linux 8",                  "project": "rocky-linux-cloud",   "family": "rocky-linux-8",           "os": "linux", "default_user": "rocky"},
    "almalinux-9":           {"label": "AlmaLinux 9",                    "project": "almalinux-cloud",     "family": "almalinux-9",             "os": "linux", "default_user": "almalinux"},
    "centos-stream-9":       {"label": "CentOS Stream 9",                "project": "centos-cloud",        "family": "centos-stream-9",         "os": "linux", "default_user": "centos"},
    "cos-stable":            {"label": "Container-Optimized OS (Docker)", "project": "cos-cloud",           "family": "cos-stable",              "os": "linux", "default_user": "root", "note": "容器优化系统，自带 Docker，SSH 用户为 root"},
    "freebsd-14":            {"label": "FreeBSD 14",                     "project": "freebsd-org-cloud-dev", "family": "freebsd-14-0",          "os": "linux", "default_user": "freebsd"},
    "windows-2022":          {"label": "Windows Server 2022 Datacenter", "project": "windows-cloud",       "family": "windows-2022",            "os": "windows", "default_user": "Administrator", "note": "Windows 镜像不支持 startup-script 的 bash 脚本，Root 密码模式不可用"},
    "windows-2019":          {"label": "Windows Server 2019 Datacenter", "project": "windows-cloud",       "family": "windows-2019",            "os": "windows", "default_user": "Administrator", "note": "同上"},
}

# 磁盘类型
DISK_TYPES = {
    "pd-standard": {"label": "标准盘 pd-standard (HDD)",   "hourly_usd_per_gb": 0.0000548, "min_gb": 10,  "max_gb": 65536, "recommended": True, "note": "免费额度覆盖 30GB/月（us 区域）"},
    "pd-balanced": {"label": "均衡盘 pd-balanced (SSD)",   "hourly_usd_per_gb": 0.0001096, "min_gb": 10,  "max_gb": 65536},
    "pd-ssd":      {"label": "SSD 盘 pd-ssd",             "hourly_usd_per_gb": 0.0001877, "min_gb": 10,  "max_gb": 65536},
    "pd-extreme":  {"label": "极速盘 pd-extreme",         "hourly_usd_per_gb": 0.0001369, "min_gb": 500, "max_gb": 65536, "allowed_regions": ["us-central1", "us-east1", "us-west1", "europe-west1", "asia-southeast1"]},
    "hyperdisk-balanced": {"label": "Hyperdisk Balanced", "hourly_usd_per_gb": 0.0001369, "min_gb": 10, "max_gb": 65536, "allowed_regions": ["us-central1", "us-east1", "us-west1", "us-east4", "europe-west1", "europe-west4", "asia-southeast1"]},
}

# 默认配置（当用户在页面上不做任何自定义时的兜底）
#
# 本默认值取向：**默认收敛暴露面 + 极致省钱**
#   · 全开放防火墙默认【关闭】—— 不自动放开 0.0.0.0/0；需要时由用户显式勾选
#   · 禁用 Ops Agent —— 避免日志存储 / 监控产生附加费用
#   · 无备份 —— 不挂快照时间表 / 备份策略，避免磁盘快照存储费用
#   · 关闭删除保护 —— 便于随时回收实例，避免忘记清理而持续计费
DEFAULT_CONFIG = {
    "machine_type": "e2-micro",
    "image_key": "ubuntu-minimal-2204",
    "disk_type": "pd-standard",
    "disk_size_gb": 30,
    "network": "default",
    "subnet": "default",
    "network_tier": "STANDARD",
    "assign_public_ip": True,
    "tags": ["http-server", "https-server"],
    # --- 全开放防火墙：默认关闭（需显式开启）---
    "auto_open_firewall": False,
    # --- 省钱相关 ---
    "disable_ops_agent": True,        # 禁用 Google Cloud Ops Agent（日志/监控）
    "no_backup": True,                # 数据保护 → 无备份
    "no_snapshot_schedule": True,     # 不挂快照时间表（resource_policies 置空）
    "no_resource_policy": True,       # 不绑定任何资源策略
    "deletion_protection": False,     # 关闭删除保护，便于回收
    "preemptible": False,
    "spot": False,
}

# ---------------------------------------------------------------------------
# 省钱优化清单
#   每一项都对应 create_instance 里的一处真实实现，用于前端展示「已省下什么」。
#   key 与 DEFAULT_CONFIG / spec 字段同名，enabled 由 spec 实时计算。
# ---------------------------------------------------------------------------
SAVINGS_ITEMS = [
    {
        "key": "disable_ops_agent",
        "label": "禁用 Ops / 监控 Agent",
        "detail": "写入 metadata google-logging-enabled=false、google-monitoring-enabled=false，"
                  "不产生日志存储与监控费用",
        "saved": "日志 0.50/GB + 监控 0.2580/百万样本",
    },
    {
        "key": "no_backup",
        "label": "数据保护 → 无备份",
        "detail": "不创建快照时间表、不绑定备份策略（Backup and DR），磁盘不产生快照存储费",
        "saved": "快照存储 0.026/GB·月",
    },
    {
        "key": "no_snapshot_schedule",
        "label": "无快照时间表",
        "detail": "磁盘 resource_policies 置空，GCP 不会按计划自动生成快照",
        "saved": "免去计划快照累积",
    },
    {
        "key": "deletion_protection_off",
        "label": "关闭删除保护",
        "detail": "实例可随时删除回收，避免忘记清理导致持续计费",
        "saved": "避免僵尸实例空转",
    },
    {
        "key": "standard_tier",
        "label": "STANDARD 网络层级",
        "detail": "出站流量每月 200GB 内免费（PREMIUM 不免费）",
        "saved": "出站 0.085/GB（PREMIUM）→ 0",
    },
    {
        "key": "pd_standard_or_free",
        "label": "标准盘 + 免费机型",
        "detail": "e2-micro + pd-standard 30GB 命中 GCP 永久免费额度（限 us-west1/us-central1/us-east1）",
        "saved": "整机免费额度内 $0",
    },
    {
        "key": "preemptible_or_spot",
        "label": "抢占式 / Spot 实例",
        "detail": "计算价格约为按需的 20%（抢占式）或 35%（Spot）",
        "saved": "计算费 -65% ~ -80%",
    },
]


def savings_status(spec):
    """按 spec 计算每个省钱项的开关状态，供前端展示"""
    spec = spec or {}
    def on(k, default=False):
        return bool(spec.get(k, default))

    states = {
        "disable_ops_agent": on("disable_ops_agent", True),
        "no_backup": on("no_backup", True),
        "no_snapshot_schedule": on("no_snapshot_schedule", True),
        "deletion_protection_off": not bool(spec.get("deletion_protection", False)),
        "standard_tier": (spec.get("network_tier") or "STANDARD") == "STANDARD",
        "pd_standard_or_free": (
            spec.get("machine_type") == "e2-micro"
            and spec.get("disk_type") == "pd-standard"
            and int(spec.get("disk_size_gb") or 0) <= 30
        ),
        "preemptible_or_spot": on("preemptible") or on("spot"),
    }
    items = []
    for it in SAVINGS_ITEMS:
        items.append({**it, "enabled": bool(states.get(it["key"]))})
    enabled = sum(1 for i in items if i["enabled"])
    return {"items": items, "enabled": enabled, "total": len(items)}


def image_source(image_key):
    """返回 compute_v1 可直接使用的 source_image 串"""
    img = IMAGES.get(image_key)
    if not img:
        # 允许直接传 projects/xxx/global/images/family/yyy
        return image_key
    return f"projects/{img['project']}/global/images/family/{img['family']}"


def machine_allowed_in_region(machine_type, region):
    spec = MACHINE_TYPES.get(machine_type)
    if not spec:
        return True  # 未知机型交给 GCP 校验
    allowed = spec.get("allowed_regions")
    if not allowed:
        return True
    return region in allowed


def machine_types_for_region(region, include_unavailable=False):
    out = []
    for name, spec in MACHINE_TYPES.items():
        ok = machine_allowed_in_region(name, region)
        if not ok and not include_unavailable:
            continue
        item = {"name": name}
        item.update({k: v for k, v in spec.items() if k != "allowed_regions"})
        item["region_available"] = ok
        if spec.get("free_tier"):
            item["free_tier_region_ok"] = region in FREE_TIER_REGIONS
        out.append(item)
    out.sort(key=lambda x: (MACHINE_FAMILY_ORDER.index(x["family"]) if x["family"] in MACHINE_FAMILY_ORDER else 99,
                            x["vcpu"], x["mem_gb"]))
    return out


def disk_types_for_region(region, include_unavailable=False):
    out = []
    for name, spec in DISK_TYPES.items():
        allowed = spec.get("allowed_regions")
        ok = (not allowed) or (region in allowed)
        if not ok and not include_unavailable:
            continue
        item = {"name": name}
        item.update({k: v for k, v in spec.items() if k != "allowed_regions"})
        item["region_available"] = ok
        out.append(item)
    return out


def estimate_monthly_cost(machine_type, disk_type, disk_size_gb, region, hours=730, count=1,
                          preemptible=False, spot=False):
    """粗略成本估算，仅用于界面提示"""
    mt = MACHINE_TYPES.get(machine_type, {})
    dt = DISK_TYPES.get(disk_type, {})
    idx = REGION_PRICE_INDEX.get(region, 1.0)
    compute = float(mt.get("hourly_usd", 0)) * hours * idx
    if preemptible:
        compute *= 0.2
    if spot:
        compute *= 0.35  # spot 折扣波动大，取常见区间
    disk = float(dt.get("hourly_usd_per_gb", 0)) * float(disk_size_gb or 0) * hours * idx
    total = (compute + disk) * max(1, int(count))
    return {
        "region_price_index": idx,
        "monthly_compute_usd": round(compute * max(1, int(count)), 2),
        "monthly_disk_usd": round(disk * max(1, int(count)), 2),
        "monthly_total_usd": round(total, 2),
        "discount": "preemptible(80% off)" if preemptible else ("spot" if spot else "按需"),
        "disclaimer": "参考价（us-central1 按需单价 × 区域系数），实际以 GCP 账单为准",
    }


def catalog_payload(region=None, include_unavailable=False):
    region = region or "us-central1"
    return {
        "ok": True,
        "region": region,
        "free_regions": [{"value": k, "label": v} for k, v in FREE_REGIONS.items()],
        "paid_regions": [{"value": k, "label": v} for k, v in PAID_REGIONS.items()],
        "free_tier_regions": FREE_TIER_REGIONS,
        "machine_types": machine_types_for_region(region, include_unavailable),
        "images": [{"key": k, **v} for k, v in IMAGES.items()],
        "disk_types": disk_types_for_region(region, include_unavailable),
        "defaults": DEFAULT_CONFIG,
        "notes": [
            "e2-micro + pd-standard 30GB 在 us-west1/us-central1/us-east1 可命中 GCP 永久免费额度",
            "原版 v7.6 硬编码：e2-micro + ubuntu-minimal-2204-lts + pd-standard 30GB",
        ],
    }
