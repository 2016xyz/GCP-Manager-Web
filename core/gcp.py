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

from google.cloud import compute_v1
from google.api_core.exceptions import GoogleAPIError

from . import catalog

DEFAULT_NETWORK = "global/networks/default"
DEFAULT_SUBNET = "regions/{region}/subnetworks/default"


# ---------------------------------------------------------------------------
# 代理解析（复用原版 v7.6 的格式生态）
# ---------------------------------------------------------------------------
def parse_proxy_input(proxy_text, fallback_proxy_type="HTTPS"):
    """把各种代理写法统一成 requests/环境变量可用的 URL"""
    raw = (proxy_text or "").strip()
    if not raw:
        return {"ok": True, "empty": True, "proxy_url": "", "proxy_type": fallback_proxy_type}

    ptype = (fallback_proxy_type or "HTTPS").upper()
    user = pw = None
    scheme = None

    if "://" in raw:
        scheme, _, rest = raw.partition("://")
        scheme = scheme.lower()
        if "@" in rest:
            cred, _, hostport = rest.rpartition("@")
            if ":" in cred:
                user, _, pw = cred.partition(":")
        else:
            hostport = rest
        if scheme in ("socks5", "socks", "socks5h"):
            ptype = "SOCKS5"
        else:
            ptype = "HTTPS"
        parts = [hostport]
    else:
        parts = raw.split(":")
        head = parts[0].lower()
        if head in ("http", "https", "socks", "socks5"):
            ptype = "SOCKS5" if head in ("socks", "socks5") else "HTTPS"
            parts = parts[1:]

    host = parts[0] if parts else ""
    port = parts[1] if len(parts) > 1 else ""
    if len(parts) >= 4:
        user, pw = parts[2], parts[3]

    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host or ""):
        return {"ok": False, "error": "代理 IP 格式不正确，应形如 1.2.3.4:8080:user:pass", "proxy_url": ""}
    if not (port or "").isdigit():
        return {"ok": False, "error": "代理端口不正确", "proxy_url": ""}

    scheme = "socks5" if ptype == "SOCKS5" else "http"
    if user:
        proxy_url = f"{scheme}://{user}:{pw}@{host}:{port}"
    else:
        proxy_url = f"{scheme}://{host}:{port}"
    return {"ok": True, "empty": False, "proxy_url": proxy_url, "proxy_type": ptype}


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
    spec["auto_open_firewall"] = bool(spec.get("auto_open_firewall", True))
    spec["disable_ops_agent"] = bool(spec.get("disable_ops_agent", True))
    # 省钱：数据保护 → 无备份
    spec["no_backup"] = bool(spec.get("no_backup", True))
    spec["no_snapshot_schedule"] = bool(spec.get("no_snapshot_schedule", True))
    spec["no_resource_policy"] = bool(spec.get("no_resource_policy", True))
    spec["deletion_protection"] = bool(spec.get("deletion_protection", False))
    return spec


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
    def create_open_firewall_rules(self, ingress=True, egress=True, priority=1000):
        def mk():
            a = compute_v1.Allowed()
            a.I_p_protocol = "all"
            return a

        def upsert(name, firewall):
            try:
                self.firewall_client.insert(project=self.project_id, firewall_resource=firewall).result()
                return True, f"{name} 创建成功"
            except Exception as e:
                if "already exists" not in str(e).lower():
                    return False, f"{name} 失败：{e}"
                try:
                    self.firewall_client.update(project=self.project_id, firewall=name,
                                                firewall_resource=firewall).result()
                    return True, f"{name} 已存在，已更新为全开放"
                except Exception as ue:
                    return False, f"{name} 更新失败：{ue}"

        def run():
            msgs = []
            ok_all = True
            if ingress:
                ok, m = upsert("allow-all-ingress", compute_v1.Firewall(
                    name="allow-all-ingress", network=DEFAULT_NETWORK, direction="INGRESS",
                    priority=priority, source_ranges=["0.0.0.0/0"], allowed=[mk()]))
                ok_all &= ok
                msgs.append(m)
            if egress:
                ok, m = upsert("allow-all-egress", compute_v1.Firewall(
                    name="allow-all-egress", network=DEFAULT_NETWORK, direction="EGRESS",
                    priority=priority, destination_ranges=["0.0.0.0/0"], allowed=[mk()]))
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
            if spec.get("auto_open_firewall"):
                fw_ok, fw_msg = self.create_open_firewall_rules()
                if not fw_ok:
                    return False, f"防火墙前置失败：{fw_msg}"

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
            ip = private_ip = ""
            try:
                info = self.instance_client.get(project=self.project_id, zone=zone, instance=name)
                if info.network_interfaces and info.network_interfaces[0].access_configs:
                    ip = info.network_interfaces[0].access_configs[0].nat_i_p
                private_ip = info.network_interfaces[0].network_i_p if info.network_interfaces else ""
            except Exception as get_err:
                return True, {"ip": ip, "private_ip": private_ip, "zone": zone,
                              "machine_type": spec["machine_type"], "spec": spec,
                              "warning": f"实例已创建，但读取 IP 失败：{get_err}"}
            return True, {"ip": ip, "private_ip": private_ip, "zone": zone,
                          "machine_type": spec["machine_type"], "spec": spec}

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
    def _operate(self, func, act):
        try:
            self._with_proxy(func)
            return True, f"{act}成功"
        except Exception as e:
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
                    ip = private_ip = ""
                    if inst.network_interfaces:
                        private_ip = inst.network_interfaces[0].network_i_p
                        if inst.network_interfaces[0].access_configs:
                            ip = inst.network_interfaces[0].access_configs[0].nat_i_p
                    mt = (inst.machine_type or "").rsplit("/", 1)[-1]
                    res.append({
                        "name": inst.name,
                        "ip": ip,
                        "private_ip": private_ip,
                        "zone": zone.lstrip("zones/") if zone else "",
                        "status": inst.status,
                        "machine_type": mt,
                        "created": inst.creation_timestamp,
                        "project_id": self.project_id,
                        "account_email": self.email,
                    })
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

    def get_instance(self, zone, name):
        def run():
            return self.instance_client.get(project=self.project_id,
                                            zone=zone.replace("zones/", ""), instance=name)

        return self._with_proxy(run)
