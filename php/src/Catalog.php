<?php
/**
 * Catalog —— GCP 资源配置目录（机型 / 镜像 / 磁盘 / 区域 / 价格）
 *
 * 逐条对照 core/catalog.py 移植。数据（机型单价、区域系数、镜像 family、
 * 磁盘类型）与 Python 版**完全一致**，因为前端会把这里的字段直接渲染，
 * 并且 estimate/savings 的结果要与 Python 版逐字段一致。
 *
 * 设计取舍：
 *   · 纯数据 + 纯函数，无任何 I/O、无网络、无随机 —— 保证同一入参永远同一出参。
 *   · 数据以类常量保存，避免每次请求重复构造大数组。
 *   · 方法名保持 Python 的 snake_case，便于与 core/catalog.py 逐条对照；
 *     同时提供 camelCase 别名给偏 PHP 风格的调用点。
 *
 * 诚实标注（与 Python 版一致）：hourly_usd 是 us-central1 按需参考单价，
 * 真实计费以 GCP 账单为准。
 */

declare(strict_types=1);

final class Catalog
{
    // ------------------------------------------------------------------
    // 1. 免费区 / 付费区（复用原版 v7.6 的区域池）
    // ------------------------------------------------------------------
    public const FREE_REGIONS = [
        'us-central1' => 'us-central1 (爱荷华)',
        'us-east1'    => 'us-east1 (南卡罗来纳)',
        'us-west1'    => 'us-west1 (俄勒冈)',
    ];

    public const PAID_REGIONS = [
        // 美国
        'us-central2' => 'us-central2 (达拉斯)', 'us-east2' => 'us-east2 (俄亥俄)',
        'us-east3' => 'us-east3 (南卡罗来纳)', 'us-east4' => 'us-east4 (北弗吉尼亚)',
        'us-east5' => 'us-east5 (俄亥俄)', 'us-west2' => 'us-west2 (洛杉矶)',
        'us-west3' => 'us-west3 (盐湖城)', 'us-west4' => 'us-west4 (拉斯维加斯)',
        'us-south1' => 'us-south1 (德克萨斯)',
        // 亚洲
        'asia-east1' => 'asia-east1 (台湾)', 'asia-east2' => 'asia-east2 (香港)',
        'asia-northeast1' => 'asia-northeast1 (东京)', 'asia-northeast2' => 'asia-northeast2 (大阪)',
        'asia-northeast3' => 'asia-northeast3 (首尔)', 'asia-south1' => 'asia-south1 (孟买)',
        'asia-south2' => 'asia-south2 (德里)', 'asia-southeast1' => 'asia-southeast1 (新加坡)',
        'asia-southeast2' => 'asia-southeast2 (雅加达)',
        // 欧洲
        'europe-west1' => 'europe-west1 (比利时)', 'europe-west2' => 'europe-west2 (伦敦)',
        'europe-west3' => 'europe-west3 (法兰克福)', 'europe-west4' => 'europe-west4 (荷兰)',
        'europe-west6' => 'europe-west6 (苏黎世)', 'europe-west8' => 'europe-west8 (米兰)',
        'europe-west9' => 'europe-west9 (巴黎)', 'europe-west10' => 'europe-west10 (柏林)',
        'europe-west12' => 'europe-west12 (都灵)', 'europe-central2' => 'europe-central2 (华沙)',
        'europe-north1' => 'europe-north1 (芬兰)', 'europe-southwest1' => 'europe-southwest1 (马德里)',
        // 其他
        'australia-southeast1' => 'australia-southeast1 (悉尼)', 'australia-southeast2' => 'australia-southeast2 (墨尔本)',
        'me-central1' => 'me-central1 (多哈)', 'me-central2' => 'me-central2 (利雅得)',
        'me-west1' => 'me-west1 (特拉维夫)', 'southamerica-east1' => 'southamerica-east1 (圣保罗)',
        'southamerica-west1' => 'southamerica-west1 (圣地亚哥)',
        'northamerica-northeast1' => 'northamerica-northeast1 (蒙特利尔)',
        'northamerica-northeast2' => 'northamerica-northeast2 (多伦多)',
    ];

    /** 区域价格系数（相对 us-central1 的粗略折算，仅用于界面估算） */
    public const REGION_PRICE_INDEX = [
        'us-central1' => 1.00, 'us-east1' => 1.00, 'us-west1' => 1.00,
        'us-central2' => 1.00, 'us-east2' => 1.00, 'us-east3' => 1.00,
        'us-east4' => 1.02, 'us-east5' => 1.02, 'us-west2' => 1.06, 'us-west3' => 1.05,
        'us-west4' => 1.05, 'us-south1' => 1.02,
        'asia-east1' => 1.10, 'asia-east2' => 1.20, 'asia-northeast1' => 1.12,
        'asia-northeast2' => 1.12, 'asia-northeast3' => 1.12, 'asia-south1' => 1.02,
        'asia-south2' => 1.02, 'asia-southeast1' => 1.10, 'asia-southeast2' => 1.10,
        'europe-west1' => 1.08, 'europe-west2' => 1.10, 'europe-west3' => 1.10,
        'europe-west4' => 1.08, 'europe-west6' => 1.13, 'europe-west8' => 1.10,
        'europe-west9' => 1.10, 'europe-west10' => 1.10, 'europe-west12' => 1.10,
        'europe-central2' => 1.08, 'europe-north1' => 1.05, 'europe-southwest1' => 1.08,
        'australia-southeast1' => 1.15, 'australia-southeast2' => 1.15,
        'me-central1' => 1.12, 'me-central2' => 1.12, 'me-west1' => 1.12,
        'southamerica-east1' => 1.20, 'southamerica-west1' => 1.20,
        'northamerica-northeast1' => 1.05, 'northamerica-northeast2' => 1.05,
    ];

    public const FREE_TIER_REGIONS = ['us-west1', 'us-central1', 'us-east1'];

    // ------------------------------------------------------------------
    // 2. 机器类型（机型）
    //    family 有 allowed_regions 时按白名单过滤；未登记视为全域可用。
    // ------------------------------------------------------------------
    public const MACHINE_TYPES = [
        // ---- E2 通用型（免费额度机型所在系列）----
        'e2-micro'     => ['family' => 'E2', 'vcpu' => 2, 'mem_gb' => 1.0, 'hourly_usd' => 0.008376, 'arch' => 'x86_64', 'free_tier' => true, 'note' => 'GCP 永久免费机型。额度按时间计：三个免费区域所有 e2-micro 运行小时数合并，当月累计到当月总小时数为止免费（us-central1 按需 $0.008376/h，取自官方计算器）'],
        'e2-small'     => ['family' => 'E2', 'vcpu' => 2, 'mem_gb' => 2.0, 'hourly_usd' => 0.016823, 'arch' => 'x86_64'],
        'e2-medium'    => ['family' => 'E2', 'vcpu' => 2, 'mem_gb' => 4.0, 'hourly_usd' => 0.033638, 'arch' => 'x86_64'],
        'e2-standard-2'  => ['family' => 'E2', 'vcpu' => 2, 'mem_gb' => 8.0, 'hourly_usd' => 0.067112, 'arch' => 'x86_64'],
        'e2-standard-4'  => ['family' => 'E2', 'vcpu' => 4, 'mem_gb' => 16.0, 'hourly_usd' => 0.134225, 'arch' => 'x86_64'],
        'e2-standard-8'  => ['family' => 'E2', 'vcpu' => 8, 'mem_gb' => 32.0, 'hourly_usd' => 0.268449, 'arch' => 'x86_64'],
        'e2-standard-16' => ['family' => 'E2', 'vcpu' => 16, 'mem_gb' => 64.0, 'hourly_usd' => 0.536899, 'arch' => 'x86_64'],
        'e2-standard-32' => ['family' => 'E2', 'vcpu' => 32, 'mem_gb' => 128.0, 'hourly_usd' => 1.073798, 'arch' => 'x86_64'],
        'e2-highcpu-2'   => ['family' => 'E2', 'vcpu' => 2, 'mem_gb' => 2.0, 'hourly_usd' => 0.049944, 'arch' => 'x86_64'],
        'e2-highcpu-4'   => ['family' => 'E2', 'vcpu' => 4, 'mem_gb' => 4.0, 'hourly_usd' => 0.099887, 'arch' => 'x86_64'],
        'e2-highcpu-8'   => ['family' => 'E2', 'vcpu' => 8, 'mem_gb' => 8.0, 'hourly_usd' => 0.199774, 'arch' => 'x86_64'],
        'e2-highmem-2'   => ['family' => 'E2', 'vcpu' => 2, 'mem_gb' => 16.0, 'hourly_usd' => 0.090044, 'arch' => 'x86_64'],
        'e2-highmem-4'   => ['family' => 'E2', 'vcpu' => 4, 'mem_gb' => 32.0, 'hourly_usd' => 0.180088, 'arch' => 'x86_64'],
        'e2-highmem-8'   => ['family' => 'E2', 'vcpu' => 8, 'mem_gb' => 64.0, 'hourly_usd' => 0.360176, 'arch' => 'x86_64'],

        // ---- N1 通用型 ----
        'n1-standard-1'  => ['family' => 'N1', 'vcpu' => 1, 'mem_gb' => 3.75, 'hourly_usd' => 0.0475, 'arch' => 'x86_64'],
        'n1-standard-2'  => ['family' => 'N1', 'vcpu' => 2, 'mem_gb' => 7.5, 'hourly_usd' => 0.0950, 'arch' => 'x86_64'],
        'n1-standard-4'  => ['family' => 'N1', 'vcpu' => 4, 'mem_gb' => 15.0, 'hourly_usd' => 0.1900, 'arch' => 'x86_64'],
        'n1-standard-8'  => ['family' => 'N1', 'vcpu' => 8, 'mem_gb' => 30.0, 'hourly_usd' => 0.3800, 'arch' => 'x86_64'],
        'n1-standard-16' => ['family' => 'N1', 'vcpu' => 16, 'mem_gb' => 60.0, 'hourly_usd' => 0.7600, 'arch' => 'x86_64'],

        // ---- N2 通用型 ----
        'n2-standard-2'  => ['family' => 'N2', 'vcpu' => 2, 'mem_gb' => 8.0, 'hourly_usd' => 0.0972, 'arch' => 'x86_64', 'allowed_regions' => ['us-central1', 'us-east1', 'us-west1', 'us-east4', 'us-west2', 'europe-west1', 'europe-west2', 'europe-west4', 'asia-east1', 'asia-northeast1', 'asia-southeast1']],
        'n2-standard-4'  => ['family' => 'N2', 'vcpu' => 4, 'mem_gb' => 16.0, 'hourly_usd' => 0.1943, 'arch' => 'x86_64', 'allowed_regions' => ['us-central1', 'us-east1', 'us-west1', 'us-east4', 'us-west2', 'europe-west1', 'europe-west2', 'europe-west4', 'asia-east1', 'asia-northeast1', 'asia-southeast1']],
        'n2-standard-8'  => ['family' => 'N2', 'vcpu' => 8, 'mem_gb' => 32.0, 'hourly_usd' => 0.3885, 'arch' => 'x86_64', 'allowed_regions' => ['us-central1', 'us-east1', 'us-west1', 'us-east4', 'europe-west1', 'europe-west4', 'asia-northeast1']],
        'n2-highmem-2'   => ['family' => 'N2', 'vcpu' => 2, 'mem_gb' => 16.0, 'hourly_usd' => 0.1311, 'arch' => 'x86_64', 'allowed_regions' => ['us-central1', 'us-east1', 'us-west1', 'us-east4', 'europe-west1', 'asia-northeast1']],

        // ---- T2D / T2A（AMD / ARM）----
        't2d-standard-1' => ['family' => 'T2D', 'vcpu' => 1, 'mem_gb' => 4.0, 'hourly_usd' => 0.0349, 'arch' => 'x86_64', 'allowed_regions' => ['us-central1', 'us-east1', 'us-west1', 'europe-west1', 'europe-west2', 'europe-west3', 'europe-west4', 'asia-southeast1']],
        't2d-standard-2' => ['family' => 'T2D', 'vcpu' => 2, 'mem_gb' => 8.0, 'hourly_usd' => 0.0698, 'arch' => 'x86_64', 'allowed_regions' => ['us-central1', 'us-east1', 'us-west1', 'europe-west1', 'europe-west2', 'europe-west3', 'europe-west4', 'asia-southeast1']],
        't2d-standard-4' => ['family' => 'T2D', 'vcpu' => 4, 'mem_gb' => 16.0, 'hourly_usd' => 0.1396, 'arch' => 'x86_64', 'allowed_regions' => ['us-central1', 'us-east1', 'us-west1', 'europe-west1', 'europe-west2', 'europe-west3', 'europe-west4', 'asia-southeast1']],
        't2a-standard-1' => ['family' => 'T2A', 'vcpu' => 1, 'mem_gb' => 4.0, 'hourly_usd' => 0.0383, 'arch' => 'arm64', 'allowed_regions' => ['us-central1', 'europe-west4', 'asia-southeast1']],
        't2a-standard-2' => ['family' => 'T2A', 'vcpu' => 2, 'mem_gb' => 8.0, 'hourly_usd' => 0.0766, 'arch' => 'arm64', 'allowed_regions' => ['us-central1', 'europe-west4', 'asia-southeast1']],

        // ---- C3 / C2 计算优化型 ----
        'c3-standard-4'  => ['family' => 'C3', 'vcpu' => 4, 'mem_gb' => 16.0, 'hourly_usd' => 0.2496, 'arch' => 'x86_64', 'allowed_regions' => ['us-central1', 'us-east1', 'us-east4', 'us-west1', 'us-west3', 'europe-west1', 'europe-west4', 'europe-west9', 'asia-southeast1']],
        'c3-standard-8'  => ['family' => 'C3', 'vcpu' => 8, 'mem_gb' => 32.0, 'hourly_usd' => 0.4992, 'arch' => 'x86_64', 'allowed_regions' => ['us-central1', 'us-east1', 'us-east4', 'us-west1', 'us-west3', 'europe-west1', 'europe-west4', 'europe-west9', 'asia-southeast1']],
        'c2-standard-4'  => ['family' => 'C2', 'vcpu' => 4, 'mem_gb' => 16.0, 'hourly_usd' => 0.2140, 'arch' => 'x86_64', 'allowed_regions' => ['us-central1', 'us-east1', 'us-west1', 'europe-west1', 'europe-west4', 'asia-southeast1']],

        // ---- M 内存优化型 ----
        'm1-megamem-96'  => ['family' => 'M1', 'vcpu' => 96, 'mem_gb' => 1433.6, 'hourly_usd' => 10.376, 'arch' => 'x86_64', 'allowed_regions' => ['us-central1', 'us-east1', 'europe-west1', 'asia-southeast1']],

        // ---- F1 / G2（FPGA / GPU）----
        'n1-standard-4-gpu-t4' => ['family' => 'GPU', 'vcpu' => 4, 'mem_gb' => 15.0, 'hourly_usd' => 0.35, 'arch' => 'x86_64', 'gpu' => 'nvidia-tesla-t4 x1', 'note' => '需申请 GPU 配额（accelerator）', 'allowed_regions' => ['us-central1', 'us-west1', 'asia-east1', 'asia-southeast1']],
    ];

    public const MACHINE_FAMILY_ORDER = ['E2', 'N1', 'N2', 'T2D', 'T2A', 'C2', 'C3', 'M1', 'GPU'];

    // ------------------------------------------------------------------
    // 3. 镜像
    // ------------------------------------------------------------------
    public const IMAGES = [
        'ubuntu-2404-lts'     => ['label' => 'Ubuntu 24.04 LTS (amd64)', 'project' => 'ubuntu-os-cloud', 'family' => 'ubuntu-2404-lts-amd64', 'os' => 'linux', 'default_user' => 'ubuntu', 'recommended' => true],
        'ubuntu-2204-lts'     => ['label' => 'Ubuntu 22.04 LTS (amd64)', 'project' => 'ubuntu-os-cloud', 'family' => 'ubuntu-2204-lts', 'os' => 'linux', 'default_user' => 'ubuntu', 'note' => '原版 v7.6 默认系列之一'],
        'ubuntu-minimal-2204' => ['label' => 'Ubuntu Minimal 22.04 LTS', 'project' => 'ubuntu-os-cloud', 'family' => 'ubuntu-minimal-2204-lts', 'os' => 'linux', 'default_user' => 'ubuntu', 'note' => '★ 原版 v7.6 硬编码默认镜像'],
        'ubuntu-2004-lts'     => ['label' => 'Ubuntu 20.04 LTS (amd64)', 'project' => 'ubuntu-os-cloud', 'family' => 'ubuntu-2004-lts', 'os' => 'linux', 'default_user' => 'ubuntu'],
        'debian-12'           => ['label' => 'Debian 12 (Bookworm)', 'project' => 'debian-cloud', 'family' => 'debian-12', 'os' => 'linux', 'default_user' => 'debian', 'recommended' => true],
        'debian-11'           => ['label' => 'Debian 11 (Bullseye)', 'project' => 'debian-cloud', 'family' => 'debian-11', 'os' => 'linux', 'default_user' => 'debian'],
        'rocky-9'             => ['label' => 'Rocky Linux 9', 'project' => 'rocky-linux-cloud', 'family' => 'rocky-linux-9', 'os' => 'linux', 'default_user' => 'rocky'],
        'rocky-8'             => ['label' => 'Rocky Linux 8', 'project' => 'rocky-linux-cloud', 'family' => 'rocky-linux-8', 'os' => 'linux', 'default_user' => 'rocky'],
        'almalinux-9'         => ['label' => 'AlmaLinux 9', 'project' => 'almalinux-cloud', 'family' => 'almalinux-9', 'os' => 'linux', 'default_user' => 'almalinux'],
        'centos-stream-9'     => ['label' => 'CentOS Stream 9', 'project' => 'centos-cloud', 'family' => 'centos-stream-9', 'os' => 'linux', 'default_user' => 'centos'],
        'cos-stable'          => ['label' => 'Container-Optimized OS (Docker)', 'project' => 'cos-cloud', 'family' => 'cos-stable', 'os' => 'linux', 'default_user' => 'root', 'note' => '容器优化系统，自带 Docker，SSH 用户为 root'],
        'freebsd-14'          => ['label' => 'FreeBSD 14', 'project' => 'freebsd-org-cloud-dev', 'family' => 'freebsd-14-0', 'os' => 'linux', 'default_user' => 'freebsd'],
        'windows-2022'        => ['label' => 'Windows Server 2022 Datacenter', 'project' => 'windows-cloud', 'family' => 'windows-2022', 'os' => 'windows', 'default_user' => 'Administrator', 'note' => 'Windows 镜像不支持 startup-script 的 bash 脚本，Root 密码模式不可用'],
        'windows-2019'        => ['label' => 'Windows Server 2019 Datacenter', 'project' => 'windows-cloud', 'family' => 'windows-2019', 'os' => 'windows', 'default_user' => 'Administrator', 'note' => '同上'],
    ];

    // 磁盘类型
    public const DISK_TYPES = [
        'pd-standard'         => ['label' => '标准盘 pd-standard (HDD)', 'hourly_usd_per_gb' => 0.0000548, 'min_gb' => 10, 'max_gb' => 65536, 'recommended' => true, 'note' => '免费额度覆盖 30GB/月（us 区域）'],
        'pd-balanced'         => ['label' => '均衡盘 pd-balanced (SSD)', 'hourly_usd_per_gb' => 0.0001096, 'min_gb' => 10, 'max_gb' => 65536],
        'pd-ssd'              => ['label' => 'SSD 盘 pd-ssd', 'hourly_usd_per_gb' => 0.0001877, 'min_gb' => 10, 'max_gb' => 65536],
        'pd-extreme'          => ['label' => '极速盘 pd-extreme', 'hourly_usd_per_gb' => 0.0001369, 'min_gb' => 500, 'max_gb' => 65536, 'allowed_regions' => ['us-central1', 'us-east1', 'us-west1', 'europe-west1', 'asia-southeast1']],
        'hyperdisk-balanced'  => ['label' => 'Hyperdisk Balanced', 'hourly_usd_per_gb' => 0.0001369, 'min_gb' => 10, 'max_gb' => 65536, 'allowed_regions' => ['us-central1', 'us-east1', 'us-west1', 'us-east4', 'europe-west1', 'europe-west4', 'asia-southeast1']],
    ];

    /**
     * 默认配置（用户在页面上不做任何自定义时的兜底）。
     * 取向：默认收敛暴露面 + 极致省钱（全开放防火墙默认关闭）。
     */
    public const DEFAULT_CONFIG = [
        'machine_type' => 'e2-micro',
        'image_key' => 'ubuntu-minimal-2204',
        'disk_type' => 'pd-standard',
        'disk_size_gb' => 30,
        'network' => 'default',
        'subnet' => 'default',
        'network_tier' => 'STANDARD',
        'assign_public_ip' => true,
        'tags' => ['http-server', 'https-server'],
        // --- 全开放防火墙：默认关闭（需显式开启）---
        'auto_open_firewall' => false,
        // --- 省钱相关 ---
        'disable_ops_agent' => true,        // 禁用 Google Cloud Ops Agent（日志/监控）
        'no_backup' => true,                // 数据保护 → 无备份
        'no_snapshot_schedule' => true,     // 不挂快照时间表（resource_policies 置空）
        'no_resource_policy' => true,       // 不绑定任何资源策略
        'deletion_protection' => false,     // 关闭删除保护，便于回收
        'preemptible' => false,
        'spot' => false,
    ];

    /** 省钱优化清单（key 与 DEFAULT_CONFIG / spec 字段同名） */
    public const SAVINGS_ITEMS = [
        ['key' => 'disable_ops_agent', 'label' => '禁用 Ops / 监控 Agent',
         'detail' => '写入 metadata google-logging-enabled=false、google-monitoring-enabled=false，不产生日志存储与监控费用',
         'saved' => '日志 0.50/GB + 监控 0.2580/百万样本'],
        ['key' => 'no_backup', 'label' => '数据保护 → 无备份',
         'detail' => '不创建快照时间表、不绑定备份策略（Backup and DR），磁盘不产生快照存储费',
         'saved' => '快照存储 0.026/GB·月'],
        ['key' => 'no_snapshot_schedule', 'label' => '无快照时间表',
         'detail' => '磁盘 resource_policies 置空，GCP 不会按计划自动生成快照',
         'saved' => '免去计划快照累积'],
        ['key' => 'deletion_protection_off', 'label' => '关闭删除保护',
         'detail' => '实例可随时删除回收，避免忘记清理导致持续计费',
         'saved' => '避免僵尸实例空转'],
        ['key' => 'standard_tier', 'label' => 'STANDARD 网络层级',
         'detail' => '出站流量每月 200GB 内免费（PREMIUM 不免费）',
         'saved' => '出站 0.085/GB（PREMIUM）→ 0'],
        ['key' => 'pd_standard_or_free', 'label' => '标准盘 + 免费机型',
         'detail' => 'e2-micro + pd-standard 30GB 命中 GCP 永久免费额度（限 us-west1/us-central1/us-east1）',
         'saved' => '整机免费额度内 $0'],
        ['key' => 'preemptible_or_spot', 'label' => '抢占式 / Spot 实例',
         'detail' => '计算价格约为按需的 20%（抢占式）或 35%（Spot）',
         'saved' => '计算费 -65% ~ -80%'],
    ];

    public const FREE_TIER_DOC = 'https://cloud.google.com/free/docs/free-cloud-features';

    // ------------------------------------------------------------------
    // 合并后的区域表 / 查询辅助
    // ------------------------------------------------------------------
    /** ALL_REGIONS = FREE + PAID（与 Python 的 {**FREE_REGIONS, **PAID_REGIONS} 一致） */
    public static function all_regions(): array
    {
        return self::FREE_REGIONS + self::PAID_REGIONS;
    }

    /** 区域 → 所在地（人类可读）。未登记的区域回退为区域名本身。 */
    public static function region_label(?string $region): string
    {
        $region = trim((string) $region);
        if ($region === '') {
            return '';
        }
        $all = self::all_regions();
        return $all[$region] ?? $region;
    }

    /** 可用区 → 区域（us-central1-a → us-central1） */
    public static function region_of_zone(?string $zone): string
    {
        $zone = trim((string) $zone);
        if (substr_count($zone, '-') >= 2) {
            return substr($zone, 0, (int) strrpos($zone, '-'));
        }
        return $zone;
    }

    /** 返回 compute API 可直接使用的 source_image 串 */
    public static function image_source(?string $imageKey): string
    {
        $key = (string) $imageKey;
        $img = self::IMAGES[$key] ?? null;
        if (!$img) {
            // 允许直接传 projects/xxx/global/images/family/yyy
            return $key;
        }
        return "projects/{$img['project']}/global/images/family/{$img['family']}";
    }

    /** 机型是否在指定区域可用（无 allowed_regions 视为全域可用） */
    public static function machine_allowed_in_region(?string $machineType, ?string $region): bool
    {
        $spec = self::MACHINE_TYPES[(string) $machineType] ?? null;
        if (!$spec) {
            return true; // 未知机型交给 GCP 校验
        }
        $allowed = $spec['allowed_regions'] ?? null;
        if (!$allowed) {
            return true;
        }
        return in_array($region, $allowed, true);
    }

    /** 某区域可用机型列表（排序与 Python 完全一致） */
    public static function machine_types_for_region(?string $region, bool $includeUnavailable = false): array
    {
        $out = [];
        foreach (self::MACHINE_TYPES as $name => $spec) {
            $ok = self::machine_allowed_in_region($name, $region);
            if (!$ok && !$includeUnavailable) {
                continue;
            }
            $item = ['name' => $name];
            foreach ($spec as $k => $v) {
                if ($k !== 'allowed_regions') {
                    $item[$k] = $v;
                }
            }
            $item['region_available'] = $ok;
            if (!empty($spec['free_tier'])) {
                $item['free_tier_region_ok'] = in_array($region, self::FREE_TIER_REGIONS, true);
            }
            $out[] = $item;
        }
        // 排序键：family 顺序 → vcpu → mem_gb（与 Python lambda 一致）
        usort($out, static function (array $a, array $b): int {
            $ia = array_search($a['family'], self::MACHINE_FAMILY_ORDER, true);
            $ib = array_search($b['family'], self::MACHINE_FAMILY_ORDER, true);
            $ia = $ia === false ? 99 : $ia;
            $ib = $ib === false ? 99 : $ib;
            if ($ia !== $ib) {
                return $ia <=> $ib;
            }
            if ($a['vcpu'] !== $b['vcpu']) {
                return $a['vcpu'] <=> $b['vcpu'];
            }
            return $a['mem_gb'] <=> $b['mem_gb'];
        });
        return $out;
    }

    /** 某区域可用磁盘类型列表 */
    public static function disk_types_for_region(?string $region, bool $includeUnavailable = false): array
    {
        $out = [];
        foreach (self::DISK_TYPES as $name => $spec) {
            $allowed = $spec['allowed_regions'] ?? null;
            $ok = (!$allowed) || in_array($region, $allowed, true);
            if (!$ok && !$includeUnavailable) {
                continue;
            }
            $item = ['name' => $name];
            foreach ($spec as $k => $v) {
                if ($k !== 'allowed_regions') {
                    $item[$k] = $v;
                }
            }
            $item['region_available'] = $ok;
            $out[] = $item;
        }
        return $out;
    }

    // ------------------------------------------------------------------
    // 省钱项状态
    // ------------------------------------------------------------------
    public static function savings_status(?array $spec): array
    {
        $spec = $spec ?? [];
        $on = static fn(string $k, bool $default = false): bool => (bool) ($spec[$k] ?? $default);

        $states = [
            'disable_ops_agent'    => $on('disable_ops_agent', true),
            'no_backup'            => $on('no_backup', true),
            'no_snapshot_schedule' => $on('no_snapshot_schedule', true),
            'deletion_protection_off' => !(bool) ($spec['deletion_protection'] ?? false),
            'standard_tier'        => (($spec['network_tier'] ?? 'STANDARD') === 'STANDARD'),
            'pd_standard_or_free'  => (
                ($spec['machine_type'] ?? null) === 'e2-micro'
                && ($spec['disk_type'] ?? null) === 'pd-standard'
                && (int) ($spec['disk_size_gb'] ?? 0) <= 30
            ),
            'preemptible_or_spot'  => $on('preemptible') || $on('spot'),
        ];
        $items = [];
        foreach (self::SAVINGS_ITEMS as $it) {
            $items[] = $it + ['enabled' => (bool) ($states[$it['key']] ?? false)];
        }
        $enabled = 0;
        foreach ($items as $i) {
            if ($i['enabled']) {
                $enabled++;
            }
        }
        return ['items' => $items, 'enabled' => $enabled, 'total' => count($items)];
    }

    // ------------------------------------------------------------------
    // 费用估算（月成本）
    // ------------------------------------------------------------------
    public static function estimate_monthly_cost(
        ?string $machineType,
        ?string $diskType,
        $diskSizeGb,
        ?string $region,
        int $hours = 730,
        int $count = 1,
        bool $preemptible = false,
        bool $spot = false
    ): array {
        $mt = self::MACHINE_TYPES[(string) $machineType] ?? [];
        $dt = self::DISK_TYPES[(string) $diskType] ?? [];
        $idx = self::REGION_PRICE_INDEX[(string) $region] ?? 1.0;
        $compute = (float) ($mt['hourly_usd'] ?? 0) * $hours * $idx;
        if ($preemptible) {
            $compute *= 0.2;
        }
        if ($spot) {
            $compute *= 0.35; // spot 折扣波动大，取常见区间
        }
        $disk = (float) ($dt['hourly_usd_per_gb'] ?? 0) * (float) ($diskSizeGb ?? 0) * $hours * $idx;
        $cnt = max(1, (int) $count);
        $total = ($compute + $disk) * $cnt;
        return [
            'region_price_index'  => $idx,
            'monthly_compute_usd' => round($compute * $cnt, 2),
            'monthly_disk_usd'    => round($disk * $cnt, 2),
            'monthly_total_usd'   => round($total, 2),
            'discount'            => $preemptible ? 'preemptible(80% off)' : ($spot ? 'spot' : '按需'),
            'disclaimer'          => '参考价（us-central1 按需单价 × 区域系数），实际以 GCP 账单为准',
        ];
    }

    /** 目录响应体（前端 /api/catalog 直接消费） */
    public static function catalog_payload(?string $region = null, bool $includeUnavailable = false): array
    {
        $region = $region ?: 'us-central1';
        $free = [];
        foreach (self::FREE_REGIONS as $k => $v) {
            $free[] = ['value' => $k, 'label' => $v];
        }
        $paid = [];
        foreach (self::PAID_REGIONS as $k => $v) {
            $paid[] = ['value' => $k, 'label' => $v];
        }
        $images = [];
        foreach (self::IMAGES as $k => $v) {
            $images[] = ['key' => $k] + $v;
        }
        return [
            'ok' => true,
            'region' => $region,
            'free_regions' => $free,
            'paid_regions' => $paid,
            'free_tier_regions' => self::FREE_TIER_REGIONS,
            'machine_types' => self::machine_types_for_region($region, $includeUnavailable),
            'images' => $images,
            'disk_types' => self::disk_types_for_region($region, $includeUnavailable),
            'defaults' => self::DEFAULT_CONFIG,
            'notes' => [
                'e2-micro + pd-standard 30GB 在 us-west1/us-central1/us-east1 可命中 GCP 永久免费额度',
                '原版 v7.6 硬编码：e2-micro + ubuntu-minimal-2204-lts + pd-standard 30GB',
            ],
        ];
    }

    // ------------------------------------------------------------------
    // camelCase 别名（给偏 PHP 风格的调用点，行为完全一致）
    // ------------------------------------------------------------------
    public static function regionLabel(?string $r): string { return self::region_label($r); }
    public static function regionOfZone(?string $z): string { return self::region_of_zone($z); }
    public static function imageSource(?string $k): string { return self::image_source($k); }
    public static function machineTypesForRegion(?string $r, bool $i = false): array { return self::machine_types_for_region($r, $i); }
    public static function diskTypesForRegion(?string $r, bool $i = false): array { return self::disk_types_for_region($r, $i); }
    public static function savingsStatus(?array $s): array { return self::savings_status($s); }
    public static function catalogPayload(?string $r = null, bool $i = false): array { return self::catalog_payload($r, $i); }
    public static function estimateMonthlyCost(?string $m, ?string $d, $s, ?string $r, int $h = 730, int $c = 1, bool $p = false, bool $sp = false): array
    {
        return self::estimate_monthly_cost($m, $d, $s, $r, $h, $c, $p, $sp);
    }
}
