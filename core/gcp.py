# -*- coding: utf-8 -*-
"""
GCP 服务封装（Web 版）

相对原版 GCP_Manager_v7.6.py 的 GCPService，核心改动：
  create_instance() 不再读取硬编码常量，而是接收一个 spec（自定义服务器配置），
  机型 / 镜像 / 磁盘类型 / 磁盘容量 / 网络标签 / 抢占式 / 公共IP 全部由前端传入。
"""
import os
import re
import threading
import time

from google.cloud import compute_v1
from google.api_core.exceptions import GoogleAPIError

from . import catalog

DEFAULT_NETWORK = "global/networks/default"
DEFAULT_SUBNET = "regions/{region}/subnetworks/default"


def _network_short_name(network):
    """
    从各种 VPC 写法里取出短名。

    接受：'jxihegwg'、
         'global/networks/jxihegwg'、
         'projects/p/global/networks/jxihegwg'、
         'https://www.googleapis.com/compute/v1/projects/p/global/networks/jxihegwg'
    取不出来时返回 ''（调用方自行给默认值）。
    """
    s = str(network or "").strip().rstrip("/")
    if not s:
        return ""
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    return s


# ---------------------------------------------------------------------------
# 代理解析（复用原版 v7.6 的格式生态）
# ---------------------------------------------------------------------------
# 支持的代理协议。
#   http / https   —— 走 HTTP CONNECT，环境变量 HTTP_PROXY/HTTPS_PROXY
#   socks5h        —— SOCKS5 且 DNS 在代理端解析（**推荐**）
#   socks5         —— SOCKS5 但 DNS 在本地解析
#   socks4         —— 老协议，只支持 IPv4、无认证
# 说明：google-cloud-* 底层用 requests/urllib3，靠 PySocks 支持 socks。
# 选 socks5 还是 socks5h 的差别很实际：socks5 会先在本地做 DNS，
# 若本地 DNS 被污染或解析不了 compute.googleapis.com，连代理都发不出去；
# socks5h 把域名交给代理解析，通常才是想要的行为。
PROXY_SCHEMES = {
    "http": "HTTP", "https": "HTTPS",
    "socks4": "SOCKS4", "socks4a": "SOCKS4",
    "socks5": "SOCKS5", "socks5h": "SOCKS5H", "socks": "SOCKS5H",
}

PROXY_TYPE_LABELS = {
    "HTTP": "HTTP 代理",
    "HTTPS": "HTTPS 代理（HTTP CONNECT）",
    "SOCKS4": "SOCKS4（仅 IPv4，无认证）",
    "SOCKS5": "SOCKS5（本地解析 DNS）",
    "SOCKS5H": "SOCKS5（代理端解析 DNS，推荐）",
}


def parse_proxy_input(proxy_text, fallback_proxy_type="HTTPS"):
    """
    把各种代理写法统一成 requests / 环境变量可用的 URL。

    支持的输入形式：
        1.2.3.4:8080
        1.2.3.4:8080:user:pass
        http://1.2.3.4:8080
        socks5://user:pass@1.2.3.4:1080
        socks5h://proxy.example.com:1080
        socks5   1.2.3.4 1080 user pass      （空格分隔，兼容常见面板导出格式）

    返回 dict：ok / empty / proxy_url / proxy_type / proxy_type_label / host / port / has_auth
    """
    raw = (proxy_text or "").strip()
    if not raw:
        return {"ok": True, "empty": True, "proxy_url": "", "proxy_type": fallback_proxy_type,
                "proxy_type_label": PROXY_TYPE_LABELS.get(fallback_proxy_type, fallback_proxy_type)}

    ptype = (fallback_proxy_type or "HTTPS").upper()
    if ptype == "SOCKS5":
        ptype = "SOCKS5H"          # 老库里存的 SOCKS5 一律按推荐的 socks5h 处理
    user = pw = None
    host = port = ""

    if "://" in raw:
        scheme, _, rest = raw.partition("://")
        scheme = scheme.strip().lower()
        # 协议名不认识时**必须报错**，不能回退到下拉框的默认值。
        # 早先写成 `PROXY_SCHEMES.get(scheme, ptype)`：用户把 socks5 拼成
        # socks9、或写成 ftp://，都会被静默当成 HTTP/HTTPS 代理去连 ——
        # 配置表面上"成功"，直到调 GCP API 才失败，离现场很远。
        if scheme not in PROXY_SCHEMES:
            return {"ok": False, "proxy_url": "", "proxy_type": ptype,
                    "host": "", "port": "",
                    "error": (f"不认识的代理协议「{scheme}」；"
                              f"支持 {', '.join(sorted(set(PROXY_SCHEMES)))}")}
        # 支持 socks5h:// 这类带 h 后缀的写法
        ptype = PROXY_SCHEMES[scheme]
        if "@" in rest:
            cred, _, hostport = rest.rpartition("@")
            if ":" in cred:
                user, _, pw = cred.partition(":")
            else:
                user = cred
        else:
            hostport = rest
        # hostport 是 "host:port" 一个整体，这里必须拆开 ——
        # 早先把整串塞进 parts[0]，导致 parts[1] 不存在、端口永远为空。
        # IPv6 写法 [::1]:8080 用 rpartition 取最后一段当端口。
        if hostport.startswith("["):
            host, _, port = hostport.rpartition("]:")
            host = host.lstrip("[")
        else:
            host, _, port = hostport.rpartition(":")
        parts = []
    else:
        # 空格分隔：socks5 1.2.3.4 1080 user pass
        parts = raw.split()
        if len(parts) == 1:
            head = parts[0].strip().lower()
            if head in PROXY_SCHEMES:
                # 形如 "socks5:host:port:user:pass"
                parts = parts[0].split(":")
                ptype = PROXY_SCHEMES[head]
                parts = parts[1:]
            elif raw.startswith("["):
                # 括号包起来的 IPv6：[::1]:8080[:user:pass]
                host, _, rest2 = raw.partition("]:")
                host = host.lstrip("[")
                tail = rest2.split(":")
                port = tail[0] if tail else ""
                if len(tail) >= 3:
                    user, pw = tail[1], tail[2]
                parts = []
            else:
                parts = parts[0].split(":")
        else:
            head = parts[0].strip().lower()
            if head in PROXY_SCHEMES:
                ptype = PROXY_SCHEMES[head]
                parts = parts[1:]

    if parts:
        if len(parts) >= 1 and not host:
            host = (parts[0] or "").strip()
        if len(parts) >= 2 and not port:
            port = (parts[1] or "").strip()
        if len(parts) >= 4 and not user:
            user, pw = parts[2].strip(), parts[3].strip()
    host = (host or "").strip()
    port = (port or "").strip()

    # 主机名与 IPv4/IPv6 都接受 —— 老实现只认 IPv4 字面量，
    # 导致 socks5h://proxy.example.com:1080 这种完全合法的写法被拒。
    if not host:
        return {"ok": False, "error": "代理地址为空", "proxy_url": ""}
    is_ipv4 = re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host)
    is_host = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.[A-Za-z]{2,}$", host)
    is_ipv6 = ":" in host
    if not (is_ipv4 or is_host or is_ipv6):
        return {"ok": False, "proxy_url": "",
                "error": f"代理地址格式不正确：{host}（应为 IP 或域名）"}
    if is_ipv4 and any(int(x) > 255 for x in host.split(".")):
        return {"ok": False, "proxy_url": "", "error": f"IPv4 段超出 0-255：{host}"}
    if not port.isdigit() or not (0 < int(port) < 65536):
        return {"ok": False, "proxy_url": "", "error": f"代理端口不正确：{port or '(空)'}"}

    if ptype == "SOCKS4":
        if user:
            # SOCKS4 只有 userId，没有密码。给了密码说明用户想用的是 SOCKS5
            return {"ok": False, "proxy_url": "",
                    "error": "SOCKS4 不支持用户名/密码认证，请改用 socks5"}
        scheme = "socks4"
    elif ptype in ("SOCKS5", "SOCKS5H"):
        # 统一用 socks5h：让代理解析域名，避免本地 DNS 污染导致连不上
        scheme = "socks5h"
        # 输入写的是 socks5:// 也只能走代理端解析了，标签必须跟着改，
        # 否则界面上显示「本地解析 DNS」而实际不是，属于误导。
        ptype = "SOCKS5H"
    else:
        scheme = "http"

    if user:
        from urllib.parse import quote as _q
        cred = f"{_q(user, safe='')}:{_q(pw or '', safe='')}@"
    else:
        cred = ""
    # IPv6 地址在 URL 里必须用方括号包起来，否则 ::1:8080 无法被解析
    hostpart = f"[{host}]" if (":" in host and not host.startswith("[")) else host
    proxy_url = f"{scheme}://{cred}{hostpart}:{port}"

    return {
        "ok": True, "empty": False, "proxy_url": proxy_url, "proxy_type": ptype,
        "proxy_type_label": PROXY_TYPE_LABELS.get(ptype, ptype),
        "host": host, "port": port, "has_auth": bool(user),
    }


def mask_proxy(proxy_text):
    """展示用：把代理里的密码打码，避免账号页面上直接看到明文密码"""
    raw = (proxy_text or "").strip()
    if not raw or "@" not in raw:
        return raw
    head, _, tail = raw.rpartition("@")
    if "://" in head:
        scheme, _, cred = head.partition("://")
        if ":" in cred:
            u, _, _ = cred.partition(":")
            return f"{scheme}://{u}:***@{tail}"
    return raw


class ProxyEnvContext:
    """在 with 块内临时设置 HTTPS_PROXY 等环境变量（google 客户端会读取）"""

    KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

    def __init__(self, proxy_url):
        self.proxy_url = (proxy_url or "").strip()
        self.saved = {}

    def __enter__(self):
        if not self.proxy_url:
            return self
        for key in self.KEYS:
            self.saved[key] = os.environ.get(key)
            os.environ[key] = self.proxy_url
        return self

    def __exit__(self, *args):
        for key, old in self.saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        return False


def _norm(value, default=""):
    return value if value not in (None, "") else default


def build_instance_spec(user_spec):
    """
    把前端传来的配置规整成出参齐全的 spec。
    这里是「自定义选择服务器配置」的唯一入口。
    """
    user_spec = dict(user_spec or {})
    spec = dict(catalog.DEFAULT_CONFIG)
    spec.update({k: v for k, v in user_spec.items() if v is not None})

    region = _norm(spec.get("region") or spec.get("zone", "")[:0], "us-central1")
    if not spec.get("region"):
        zone = spec.get("zone") or ""
        if "-" in zone:
            spec["region"] = zone.rsplit("-", 1)[0]
        else:
            spec["region"] = region

    spec["machine_type"] = _norm(spec.get("machine_type"), catalog.DEFAULT_CONFIG["machine_type"])
    spec["image_key"] = _norm(spec.get("image_key"), catalog.DEFAULT_CONFIG["image_key"])
    spec["disk_type"] = _norm(spec.get("disk_type"), catalog.DEFAULT_CONFIG["disk_type"])
    spec["disk_size_gb"] = int(_norm(spec.get("disk_size_gb"), catalog.DEFAULT_CONFIG["disk_size_gb"]))

    img = catalog.IMAGES.get(spec["image_key"], {})
    spec["image_source"] = _norm(spec.get("image_source"), catalog.image_source(spec["image_key"]))
    spec["image_label"] = img.get("label", spec["image_key"])
    spec["image_os"] = img.get("os", "linux")
    spec["image_user"] = img.get("default_user", "ubuntu")

    # 自定义机型：允许直接填 n2-standard-4 这类字符串，避开本地目录的可用区白名单
    if spec["machine_type"] not in catalog.MACHINE_TYPES:
        spec["machine_type_source"] = "custom"
    else:
        spec["machine_type_source"] = "catalog"

    if spec.get("network") == "default":
        spec["network_url"] = DEFAULT_NETWORK
    elif str(spec.get("network", "")).startswith(("projects/", "global/")):
        spec["network_url"] = spec["network"]
    else:
        spec["network_url"] = f"global/networks/{spec.get('network')}"

    subnet = str(spec.get("subnet") or "default")
    # 子网 URL 必须与实例所在 region 匹配，否则 API 报
    # "Scope of the specified subnetwork doesn't match the scope of the instance"
    # 因此 region 必须存在：缺省时给出兜底，并接受 zone 形式入参。
    region = str(spec.get("region") or "").strip()
    if not region:
        region = "us-central1"
    if region.count("-") >= 2:          # 传的是 zone（us-west1-b）→ 归一化为 region
        region = region.rsplit("-", 1)[0]
    spec["region"] = region

    if subnet == "default":
        spec["subnet_url"] = DEFAULT_SUBNET.format(region=spec["region"])
    elif subnet.startswith("regions/"):
        spec["subnet_url"] = subnet
    else:
        spec["subnet_url"] = f"regions/{spec['region']}/subnetworks/{subnet}"

    tags = spec.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    spec["tags"] = tags or []

    spec["preemptible"] = bool(spec.get("preemptible"))
    spec["spot"] = bool(spec.get("spot"))
    spec["assign_public_ip"] = bool(spec.get("assign_public_ip", True))
    # 全开放防火墙默认关闭：不自动放开 0.0.0.0/0，需用户显式开启
    spec["auto_open_firewall"] = bool(spec.get("auto_open_firewall", False))
    spec["disable_ops_agent"] = bool(spec.get("disable_ops_agent", True))
    # 省钱：数据保护 → 无备份
    spec["no_backup"] = bool(spec.get("no_backup", True))
    spec["no_snapshot_schedule"] = bool(spec.get("no_snapshot_schedule", True))
    spec["no_resource_policy"] = bool(spec.get("no_resource_policy", True))
    spec["deletion_protection"] = bool(spec.get("deletion_protection", False))
    return spec



def parse_instance(inst, zone, project_id="", account_email=""):
    """
    把 GCP 的 Instance 对象解析成前端要用的扁平字典。

    抽成独立函数是为了能单测 —— 内联在 aggregated_list 的闭包里没法构造
    假对象验证字段提取，而这个函数里每个字段都可能因为 proto 字段名写错
    而静默拿到空值（最危险的一类 bug：界面显示空白而不是报错）。
    """
    ip = private_ip = ""
    if inst.network_interfaces:
        private_ip = inst.network_interfaces[0].network_i_p
        if inst.network_interfaces[0].access_configs:
            ip = inst.network_interfaces[0].access_configs[0].nat_i_p

    mt = (inst.machine_type or "").rsplit("/", 1)[-1]
    zname = (zone or "").lstrip("zones/")

    # 引导盘：类型 / 容量 / 来源镜像
    disk_type = ""
    disk_size = 0
    image_src = ""
    if inst.disks:
        boot = inst.disks[0]
        disk_size = int(boot.disk_size_gb or 0)
        params = getattr(boot, "initialize_params", None)
        if params is not None:
            disk_type = (params.disk_type or "").rsplit("/", 1)[-1]
            image_src = params.source_image or ""
            if not disk_size:
                disk_size = int(params.disk_size_gb or 0)
        # 已运行实例的 initialize_params 常为空，退而从 source / type_ 取
        if not disk_type:
            disk_type = (getattr(boot, "type_", "") or "").rsplit("/", 1)[-1]
        if not image_src:
            image_src = boot.source or ""

    # 抢占式 / Spot：GCP 用两个不同字段表达，两个都要看
    sched = inst.scheduling
    preemptible = bool(getattr(sched, "preemptible", False)) if sched else False
    provisioning = (getattr(sched, "provisioning_model", "") or "") if sched else ""
    spot = provisioning.upper() == "SPOT"

    # creation_timestamp 是 RFC3339（"2026-09-24T15:49:00.000-07:00"）
    created_ts = 0.0
    raw_created = inst.creation_timestamp or ""
    if raw_created:
        try:
            from datetime import datetime as _dt
            created_ts = _dt.fromisoformat(raw_created).timestamp()
        except ValueError:
            created_ts = 0.0

    region = catalog.region_of_zone(zname)
    return {
        "name": inst.name,
        "ip": ip,
        "private_ip": private_ip,
        "zone": zname,
        "region": region,
        # 所在地（人类可读，如 "us-central1 (爱荷华)"）
        "location": catalog.region_label(region),
        "status": inst.status,
        "machine_type": mt,
        "disk_type": disk_type,
        "disk_size_gb": disk_size,
        "image": image_src.rsplit("/", 1)[-1] if image_src else "",
        "image_source": image_src,
        "created": raw_created,
        "created_ts": created_ts,
        "preemptible": preemptible,
        "spot": spot,
        "project_id": project_id,
        "account_email": account_email,
    }


class GCPService:
    def __init__(self, key_path, project_id, email, proxy="", proxy_type="HTTPS"):
        self.key_path = key_path
        self.project_id = project_id
        self.email = email
        parsed = parse_proxy_input(proxy, fallback_proxy_type=proxy_type)
        self.proxy_url = parsed.get("proxy_url", "") if parsed.get("ok") else ""
        if not key_path or not os.path.exists(key_path):
            raise FileNotFoundError(f"服务账号 JSON 不存在：{key_path}")
        self.instance_client = compute_v1.InstancesClient.from_service_account_json(key_path)
        self.firewall_client = compute_v1.FirewallsClient.from_service_account_json(key_path)
        self.project_client = compute_v1.ProjectsClient.from_service_account_json(key_path)

    def _with_proxy(self, func):
        with ProxyEnvContext(self.proxy_url):
            return func()

    # ------------------------------------------------------------------
    # SSH 公钥
    # ------------------------------------------------------------------
    def add_ssh_key(self, pub_key, username="root"):
        def run():
            meta = self.project_client.get(project=self.project_id).common_instance_metadata
            key_line = f"{username}:{(pub_key or '').strip()}"
            item = next((i for i in meta.items if i.key.lower() == "ssh-keys"), None)
            if not item:
                meta.items.append(compute_v1.Items(key="ssh-keys", value=key_line))
            elif key_line not in item.value:
                item.value += "\n" + key_line
            self.project_client.set_common_instance_metadata(
                project=self.project_id, metadata_resource=meta).result()
            return True

        try:
            self._with_proxy(run)
            return True, "公钥注入成功"
        except Exception as e:
            return False, str(e)

    # ------------------------------------------------------------------
    # 防火墙
    # ------------------------------------------------------------------
    def firewall_coverage(self, network="", network_url=""):
        """
        探测指定 VPC 上是否**已经**存在覆盖 0.0.0.0/0 的放行规则。
        返回 (缺失方向列表, 说明)；列表为空表示两个方向都已覆盖。

        用途：勾了「放开全开防火墙」但该 VPC 本来就有全放行规则时
        （例如 jxihegwg 自带的 `aqq` 只放 tcp，但若有人已建过 allow-all），
        不必再建一遍。返回 (是否需要新建, 说明)。

        判断口径保守：只要有一条 ingress 规则同时满足
        「方向 INGRESS / 优先级 ≤1000 / 源 0.0.0.0/0 / 协议 all」就认为已覆盖；
        egress 同理。**返回缺失方向的列表**（可能为空 = 都已覆盖），
        这样调用方只补缺的那一侧，不会去动已经安全收敛的配置
        （实测某项目自带一条 `aqq` 已覆盖全协议入站；
          旧写法会去 UPDATE 它，等于把用户已收敛的规则重新铺开成 0.0.0.0/0）。
        """
        net_name = _network_short_name(network_url) or _network_short_name(network)
        if not net_name:
            return True, ""

        def run():
            try:
                rules = list(self.firewall_client.list(project=self.project_id))
            except Exception as e:
                return True, f"列举规则失败：{e}"
            have = {"INGRESS": [], "EGRESS": []}
            for fw in rules:
                if _network_short_name(getattr(fw, "network", "")) != net_name:
                    continue
                direction = (getattr(fw, "direction", "") or "INGRESS").upper()
                if (getattr(fw, "disabled", False)
                        or getattr(fw, "priority", 65535) > 1000):
                    continue
                # 协议必须是 all（只要有一条 all 就够）
                proto_all = any(
                    (getattr(a, "I_p_protocol", "") or "").lower() == "all"
                    for a in (getattr(fw, "allowed", None) or []))
                if not proto_all:
                    continue
                if direction == "INGRESS":
                    if "0.0.0.0/0" in (getattr(fw, "source_ranges", None) or []):
                        have["INGRESS"].append(fw.name)
                else:
                    if "0.0.0.0/0" in (getattr(fw, "destination_ranges", None) or []):
                        have["EGRESS"].append(fw.name)
            missing = [d for d in ("INGRESS", "EGRESS") if not have[d]]
            if not missing:
                return [], (f"INGRESS={have['INGRESS']} EGRESS={have['EGRESS']}")
            return missing, f"缺少 {'/'.join(missing)}"

        try:
            return self._with_proxy(run)
        except Exception as e:
            return ["INGRESS", "EGRESS"], f"探测异常：{e}"

    def create_open_firewall_rules(self, ingress=True, egress=True, priority=1000,
                                   network=""):
        """
        建立 allow-all-ingress / allow-all-egress 两条「全开放」规则。

        ★ 必须绑定实例真正所在的 VPC：原来硬编码 global/networks/default，
        而用自定义 VPC（例如 jxihegwg）的项目里没有 default 网络，
        GCP 直接返回 404 `The resource .../global/networks/default was not found`。
        更隐蔽的是防火墙属于**全局**资源、与 zone 无关，
        所以外层「换个区域重试」永远不会成功，只会把两次重试全部浪费掉。

        network 接受短名（jxihegwg）、global/networks/jxihegwg 或完整 URL。
        """
        net_name = _network_short_name(network) or "default"
        net_url = f"global/networks/{net_name}"
        created = []

        def existing_rule(name):
            """返回 (已存在?, 该规则绑定的网络短名)"""
            try:
                fw = self.firewall_client.get(project=self.project_id, firewall=name)
                return True, _network_short_name(getattr(fw, "network", ""))
            except Exception:
                return False, ""

        def upsert(base_name, build):
            name = base_name
            exists, bound_net = existing_rule(name)
            if exists and bound_net and bound_net != net_name:
                # 同名规则属于**别的** VPC。直接 UPDATE 会把它改绑到本网络，
                # 可能让原来依赖它的业务失联；因此改用带网络后缀的项目级唯一名。
                name = f"{base_name}-{net_name}"
                exists, bound_net = existing_rule(name)
                if exists and bound_net and bound_net != net_name:
                    name = f"{base_name}-{net_name}-{int(time.time())}"
            try:
                self.firewall_client.insert(project=self.project_id,
                                            firewall_resource=build(name)).result()
                created.append(name)
                return True, f"{name} 创建成功（网络 {net_name}）"
            except Exception as e:
                if "already exists" not in str(e).lower():
                    return False, (f"{name} 失败：{e}"
                                   if "not found" not in str(e).lower()
                                   else f"{name} 失败：VPC「{net_name}」不存在（{e}）")
                try:
                    self.firewall_client.update(project=self.project_id, firewall=name,
                                                firewall_resource=build(name)).result()
                    created.append(name)
                    return True, f"{name} 已存在，已更新为全开放（网络 {net_name}）"
                except Exception as ue:
                    return False, f"{name} 更新失败：{ue}"

        def mk_allowed():
            a = compute_v1.Allowed()
            a.I_p_protocol = "all"
            return a

        def build_ingress(name):
            return compute_v1.Firewall(
                name=name, network=net_url, direction="INGRESS",
                priority=priority, source_ranges=["0.0.0.0/0"], allowed=[mk_allowed()])

        def build_egress(name):
            return compute_v1.Firewall(
                name=name, network=net_url, direction="EGRESS",
                priority=priority, destination_ranges=["0.0.0.0/0"], allowed=[mk_allowed()])

        def run():
            msgs = []
            ok_all = True
            if ingress:
                ok, m = upsert("allow-all-ingress", build_ingress)
                ok_all &= ok
                msgs.append(m)
            if egress:
                ok, m = upsert("allow-all-egress", build_egress)
                ok_all &= ok
                msgs.append(m)
            return ok_all, " | ".join(msgs)

        try:
            return self._with_proxy(run)
        except Exception as e:
            return False, f"防火墙创建失败：{e}"

    # ------------------------------------------------------------------
    # 创建实例（★ 核心：spec 全自定义）
    # ------------------------------------------------------------------
    def create_instance(self, zone, name, startup_script="", spec=None):
        spec = build_instance_spec(spec or {})
        region = zone.rsplit("-", 1)[0]
        spec["zone"] = zone
        spec["region"] = region

        def run():
            # ★ 防火墙是**全局**资源，与 zone 无关；且它只是这台实例的配套设施，
            # 不该因为建规则失败就把整台实例判为创建失败
            # （原来那样做会让外层「换个区域重试」反复白试，因为换区不影响防火墙）。
            # 因此改为：先确保实例能建出来，建完再补防火墙，失败只警告。
            disk = compute_v1.AttachedDisk(
                boot=True, auto_delete=True,
                initialize_params=compute_v1.AttachedDiskInitializeParams(
                    source_image=spec["image_source"],
                    disk_size_gb=spec["disk_size_gb"],
                    disk_type=f"zones/{zone}/diskTypes/{spec['disk_type']}",
                    # 省钱：不绑定任何资源策略 → 不会挂上快照时间表(Snapshot Schedule)
                    # 与备份计划(Backup and DR)，磁盘不产生快照存储费用。
                    # 留空即等价于「数据保护 → 无备份」。
                    resource_policies=[] if spec.get("no_snapshot_schedule",
                                                      spec.get("no_backup", True)) else None,
                    # 不指定 source_snapshot / storage_pool，避免引入额外资源与费用
                )
            )

            access_configs = []
            if spec.get("assign_public_ip"):
                access_configs = [compute_v1.AccessConfig(
                    name="External NAT", network_tier=_norm(spec.get("network_tier"), "STANDARD"))]

            nic = compute_v1.NetworkInterface(
                network=spec["network_url"],
                subnetwork=spec["subnet_url"],
                access_configs=access_configs,
            )

            instance = compute_v1.Instance(
                name=name,
                machine_type=f"zones/{zone}/machineTypes/{spec['machine_type']}",
                disks=[disk],
                network_interfaces=[nic],
            )

            # 省钱：默认关闭删除保护，实例可随时删除回收，
            # 避免忘记清理导致僵尸实例持续计费。
            instance.deletion_protection = bool(spec.get("deletion_protection", False))

            if spec.get("tags"):
                instance.tags = compute_v1.Tags(items=spec["tags"])

            meta = []
            # 省钱：禁用 Google Cloud Ops Agent（日志 + 监控），
            # 不产生日志存储费与监控样本费。
            if spec.get("disable_ops_agent", True):
                meta += [compute_v1.Items(key="google-logging-enabled", value="false"),
                         compute_v1.Items(key="google-monitoring-enabled", value="false")]
                # 同时阻止控制台默认开启 Ops Agent 的注入
                meta.append(compute_v1.Items(key="google-ops-agent-enabled", value="false"))
            if startup_script:
                meta.append(compute_v1.Items(key="startup-script", value=startup_script))
            if meta:
                instance.metadata = compute_v1.Metadata(items=meta)

            if spec.get("preemptible") or spec.get("spot"):
                sched = compute_v1.Scheduling()
                if spec.get("spot"):
                    sched.provisioning_model = "SPOT"
                    sched.instance_termination_action = "STOP"
                else:
                    sched.preemptible = True
                sched.automatic_restart = False
                sched.on_host_maintenance = "TERMINATE"
                instance.scheduling = sched

            if spec.get("labels"):
                instance.labels = spec["labels"]

            self.instance_client.insert(
                project=self.project_id, zone=zone, instance_resource=instance).result()

            # 实例已插入成功。后续取 IP 属于「尽力而为」：
            # get 偶发失败（权限/瞬时错误）不应把已创建成功的实例报成失败。
            base = {"ip": "", "private_ip": "", "zone": zone,
                    "machine_type": spec["machine_type"], "spec": spec}
            try:
                info = self.instance_client.get(project=self.project_id, zone=zone, instance=name)
                # get 回来的对象里带着磁盘、镜像、抢占标志、真实创建时间，
                # 一次解析齐，省掉后面再查一次；created_ts 用来算「已用费用」。
                detail = parse_instance(info, zone, self.project_id, self.email)
                base.update({
                    "ip": detail.get("ip", ""),
                    "private_ip": detail.get("private_ip", ""),
                    "disk_type": detail.get("disk_type", ""),
                    "disk_size_gb": detail.get("disk_size_gb", 0),
                    "image": detail.get("image", ""),
                    "image_source": detail.get("image_source", ""),
                    "location": detail.get("location", ""),
                    "region": detail.get("region", ""),
                    "created_ts": detail.get("created_ts", 0.0),
                    "preemptible": detail.get("preemptible", False),
                    "spot": detail.get("spot", False),
                    "status": detail.get("status", ""),
                })
            except Exception as get_err:
                base["warning"] = f"实例已创建，但读取详情失败：{get_err}"

            # ★ 实例已确认建好，再补「全开放防火墙」。
            # 放在这个位置有两个理由：
            #   1. 防火墙是全局资源，与 zone/换区重试无关，放前置只会让
            #      换区重试做无用功（原来就是这样把两次重试全浪费掉的）；
            #   2. 它只是配套设施，建失败应当只提示、不影响实例本身。
            # 未显式开启时也不白建规则：先探测该 VPC 是否已有覆盖 0.0.0.0/0 的规则。
            if spec.get("auto_open_firewall") is not False:
                try:
                    missing, why = self.firewall_coverage(spec.get("network", ""),
                                                          spec.get("network_url", ""))
                    if missing:
                        fw_ok, fw_msg = self.create_open_firewall_rules(
                            ingress=("INGRESS" in missing),
                            egress=("EGRESS" in missing),
                            network=str(spec.get("network_url") or spec.get("network") or ""))
                        base["firewall_ok"] = fw_ok
                        base["firewall"] = fw_msg
                        if not fw_ok:
                            base["warning"] = ((base.get("warning", "") +
                                                "；防火墙未建立：" + fw_msg).strip("；"))
                    else:
                        base["firewall_ok"] = True
                        base["firewall"] = f"已存在覆盖 0.0.0.0/0 的规则，未重复创建（{why}）"
                except Exception as fw_err:
                    base["warning"] = ((base.get("warning", "") +
                                        f"；防火墙探测失败：{fw_err}").strip("；"))
            return True, base

        try:
            return self._with_proxy(run)
        except Exception as e:
            msg = str(e)
            if "resource_pool_exhausted" in msg.lower() or "ZONE_RESOURCE_POOL_EXHAUSTED" in msg:
                return False, "资源耗尽"
            return False, msg

    # ------------------------------------------------------------------
    # 实例操作 / 查询
    # ------------------------------------------------------------------
    # 关于「删除为什么要等两分半」：
    # 实测 asia-south2 的 e2-micro，两个事件几乎同时发生 ——
    #     实例从 get() 里消失   133.7 s
    #     operation 标记 done   134.1 s
    # 也就是说实例真的在这段时间内一直可见、可 get 到，
    # `.result()` 并没有多余等待，这就是该区域 GCP 的实际速度。
    # （曾经误判成「后台记账慢、可见性早就没了」，是一次错误的测量：
    #   那次是在 result() 已经返回之后才测可见性，当然只剩 1 秒。）
    # 因此这里保留 .result()——它等的是真实完成，报「成功」才诚实。
    def _operate(self, func, act):
        try:
            self._with_proxy(func)
            return True, f"{act}成功"
        except Exception as e:
            msg = str(e)
            # 「本来就已经是这个状态」不算失败
            if "already" in msg.lower():
                return True, f"{act}成功"
            if act == "删除" and ("not found" in msg.lower() or "404" in msg):
                return True, "实例不存在（视为已删除）"
            return False, f"{act}失败：{e}"

    def delete_instance(self, zone, name):
        return self._operate(lambda: self.instance_client.delete(
            project=self.project_id, zone=zone.replace("zones/", ""), instance=name).result(), "删除")

    def start_instance(self, zone, name):
        return self._operate(lambda: self.instance_client.start(
            project=self.project_id, zone=zone.replace("zones/", ""), instance=name).result(), "启动")

    def stop_instance(self, zone, name):
        return self._operate(lambda: self.instance_client.stop(
            project=self.project_id, zone=zone.replace("zones/", ""), instance=name).result(), "停止")

    def reset_instance(self, zone, name):
        return self._operate(lambda: self.instance_client.reset(
            project=self.project_id, zone=zone.replace("zones/", ""), instance=name).result(), "重启")

    def list_instances(self):
        def run():
            res = []
            for zone, resp in self.instance_client.aggregated_list(project=self.project_id):
                for inst in (resp.instances or []):
                    res.append(parse_instance(inst, zone, self.project_id, self.email))
            return res

        return self._with_proxy(run)

    def list_regions(self):
        """拉取项目可用区域（部分账号无权限，失败时返回空表）"""
        def run():
            out = []
            for item in self.project_client.get(project=self.project_id).quotas or []:
                pass
            try:
                client = compute_v1.RegionsClient.from_service_account_json(self.key_path)
                for region in client.list(project=self.project_id):
                    out.append(region.name)
            except Exception:
                pass
            return out

        try:
            return self._with_proxy(run)
        except Exception:
            return []

    def list_zones(self, region=""):
        """
        拉取项目真实存在的 zone（而非按后缀硬编码猜测）。

        为什么要这么做：GCP 各 region 的 zone 后缀并不统一，例如
        us-west1 只有 a/b/c（没有 d/f），us-east1 只有 b/c/d（没有 a）。
        早先按 ("a","b","c","d","f") 硬编码拼接，会生成
        us-west1-d 这类不存在的 zone，被 API 拒绝为
        "Permission denied on 'locations/us-west1-d'"，从而把「区域不存在」
        误判成「权限不足」，排查方向被严重误导。
        """
        def run():
            out = []
            client = compute_v1.ZonesClient.from_service_account_json(self.key_path)
            for z in client.list(project=self.project_id):
                if region and not z.name.startswith(region + "-"):
                    continue
                if z.status and z.status != "UP":
                    continue
                out.append(z.name)
            return sorted(out)

        try:
            return self._with_proxy(run)
        except Exception:
            return []

    def list_networks(self):
        """列出项目中的 VPC 网络（用于确认 default 网络是否存在）"""
        def run():
            client = compute_v1.NetworksClient.from_service_account_json(self.key_path)
            return sorted(n.name for n in client.list(project=self.project_id))

        try:
            return self._with_proxy(run)
        except Exception:
            return []

    def list_subnetworks(self, region):
        """列出指定区域中的子网（VPC 名与子网名常常一致，但不是必然）"""
        def run():
            client = compute_v1.SubnetworksClient.from_service_account_json(self.key_path)
            return sorted(s.name for s in client.list(project=self.project_id, region=region))

        try:
            return self._with_proxy(run)
        except Exception:
            return []

    def get_instance(self, zone, name):
        def run():
            return self.instance_client.get(project=self.project_id,
                                            zone=zone.replace("zones/", ""), instance=name)

        return self._with_proxy(run)
