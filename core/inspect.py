"""
GCP 资源勘察（只读）

设计要点
- **只读**：本模块不调用任何 insert / update / delete / setIamPolicy，
  全部是 get / list / aggregatedList。用于「把项目里能看到的都看到」。
- **逐节隔离**：每一节独立 try/except，某一节没权限（GCP 常见：服务账号
  只有 compute 相关权限、没有 resourcemanager / serviceusage）不会拖垮整页，
  只会让那一节显示具体的报错原因。
- **走代理**：与 core.gcp 共用 ProxyEnvContext，代理设置一致生效。
- **不抛异常**：对外方法一律返回 (ok, payload)，把异常转成可读文案。
"""
from __future__ import annotations

import time

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from core.gcp import ProxyEnvContext

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _brief(e, limit=300):
    """把 GCP 的异常压成一行可读文案（原始报错常常上千字符）"""
    s = str(e).replace("\n", " ").strip()
    s = " ".join(s.split())
    return s[:limit] + ("…" if len(s) > limit else "")


def _ms(t0):
    return int((time.time() - t0) * 1000)


class Inspector:
    """对某个服务账号 + 项目做只读勘察"""

    def __init__(self, key_path, project_id, email, proxy="", proxy_type="HTTPS"):
        self.key_path = key_path
        self.project_id = project_id
        self.email = email
        from core.gcp import parse_proxy_input
        parsed = parse_proxy_input(proxy, fallback_proxy_type=proxy_type)
        self.proxy_url = parsed.get("proxy_url", "") if parsed.get("ok") else ""
        self._creds = None
        self._session = None
        # 批量模式下由 inspect_sections 在外层统一设置代理，避免多线程互踩
        self._proxy_managed = False
        # 客户端按需构造并缓存，避免每节都新建连接
        self._clients = {}

    # ------------------------------------------------------------------
    # 基础：凭证 / 会话 / 客户端
    # ------------------------------------------------------------------
    def _credentials(self):
        if self._creds is None:
            self._creds = service_account.Credentials.from_service_account_file(
                self.key_path, scopes=SCOPES)
        return self._creds

    def session(self):
        if self._session is None:
            self._session = AuthorizedSession(self._credentials())
        return self._session

    def client(self, name, region=None):
        """按需构造 compute_v1 的客户端并缓存（region 型客户端单独缓存）"""
        from google.cloud import compute_v1
        key = (name, region)
        if key not in self._clients:
            cls = getattr(compute_v1, name)
            self._clients[key] = cls.from_service_account_json(self.key_path)
        return self._clients[key]

    def rest(self, url, params=None, timeout=30):
        """REST 读取（用于 compute 库没覆盖的 API：CRM / ServiceUsage / Billing / IAM）"""
        r = self.session().get(url, params=params, timeout=timeout)
        if r.status_code >= 400:
            detail = ""
            try:
                detail = (r.json().get("error") or {}).get("message", "") or r.text
            except Exception:
                detail = r.text
            raise RuntimeError(f"HTTP {r.status_code} {detail[:240]}")
        return r.json()

    def run(self, fn):
        """
        统一执行壳：带代理环境 + 异常转文案。

        代理用环境变量实现（ProxyEnvContext 会改 os.environ），而环境变量是
        进程级的 —— 多线程并发跑勘察节时，若每节各自 save/restore 代理变量，
        会互相把对方的代理清掉。因此批量模式（inspect_sections）会先在
        外层设置一次代理并把 _proxy_managed 置真，各节就不再自己设置。
        """
        t0 = time.time()
        try:
            if getattr(self, "_proxy_managed", False):
                data = fn()
            else:
                with ProxyEnvContext(self.proxy_url):
                    data = fn()
            return {"ok": True, "ms": _ms(t0), "data": data}
        except Exception as e:
            return {"ok": False, "ms": _ms(t0), "error": _brief(e)}

    # ------------------------------------------------------------------
    # 1. 服务账号本身（本地可判定，不依赖网络）
    # ------------------------------------------------------------------
    def account(self):
        def fn():
            import json
            with open(self.key_path, encoding="utf-8") as f:
                sa = json.load(f)
            keys = [k for k in ("project_id", "private_key_id", "client_id",
                                "client_email", "type") if sa.get(k)]
            # 私钥是否可解出公钥（判断 JSON 是否被截断/损坏）
            key_ok = False
            try:
                from cryptography.hazmat.primitives import serialization
                serialization.load_pem_private_key(
                    (sa.get("private_key") or "").encode(), password=None)
                key_ok = True
            except Exception:
                key_ok = False
            return {
                "client_email": sa.get("client_email", self.email),
                "project_id": sa.get("project_id", self.project_id),
                "type": sa.get("type", ""),
                "private_key_id": sa.get("private_key_id", "")[:16] + "…"
                if sa.get("private_key_id") else "",
                "client_id": sa.get("client_id", ""),
                "has_fields": keys,
                "private_key_parsable": key_ok,
            }
        return self.run(fn)

    # ------------------------------------------------------------------
    # 2. 项目元信息（Cloud Resource Manager）
    # ------------------------------------------------------------------
    def project(self):
        def fn():
            d = self.rest(
                f"https://cloudresourcemanager.googleapis.com/v1/projects/{self.project_id}")
            parent = d.get("parent") or {}
            return {
                "projectId": d.get("projectId"),
                "projectNumber": d.get("projectNumber"),
                "name": d.get("name"),
                "state": d.get("state"),
                "createTime": d.get("createTime"),
                "labels": d.get("labels") or {},
                "parent": {"type": parent.get("type"), "id": parent.get("id")} if parent else None,
            }
        return self.run(fn)

    # ------------------------------------------------------------------
    # 3. 已启用的 API（Service Usage）
    # ------------------------------------------------------------------
    def services(self):
        def fn():
            out, token = [], None
            while True:
                params = {"filter": "state:ENABLED", "pageSize": 200}
                if token:
                    params["pageToken"] = token
                d = self.rest(
                    f"https://serviceusage.googleapis.com/v1/projects/{self.project_id}/services",
                    params=params)
                for s in d.get("services", []):
                    cfg = s.get("config") or {}
                    out.append({
                        "name": (s.get("name") or "").rsplit("/", 1)[-1],
                        "title": cfg.get("title", ""),
                        "state": s.get("state", ""),
                    })
                token = d.get("nextPageToken")
                if not token or len(out) > 500:
                    break
            return {"count": len(out),
                    "items": sorted(out, key=lambda x: x["name"])}
        return self.run(fn)

    # ------------------------------------------------------------------
    # 4. 计费（Cloud Billing）
    # ------------------------------------------------------------------
    def billing(self):
        def fn():
            d = self.rest(
                f"https://cloudbilling.googleapis.com/v1/projects/{self.project_id}/billingInfo")
            acct = d.get("billingAccountName", "")
            return {
                "billingEnabled": bool(d.get("billingEnabled")),
                "billingAccountName": acct,
                "billingAccountId": acct.rsplit("/", 1)[-1] if acct else "",
                "name": d.get("name", ""),
            }
        return self.run(fn)

    # ------------------------------------------------------------------
    # 5. 区域（含各区域配额）
    # ------------------------------------------------------------------
    def regions(self, with_quotas=True):
        def fn():
            c = self.client("RegionsClient")
            out = []
            for r in c.list(project=self.project_id):
                item = {
                    "name": r.name,
                    "status": r.status,
                    "zones": list(r.zones or []),
                    "zoneCount": len(r.zones or []),
                }
                if with_quotas:
                    qs = []
                    for q in (r.quotas or []):
                        qs.append({
                            "metric": q.metric,
                            "limit": q.limit,
                            "usage": q.usage,
                            "owner": q.owner,
                            # 已用 / 上限，前端画进度条用
                            "pct": round((q.usage / q.limit * 100), 1) if q.limit else None,
                        })
                    item["quotas"] = sorted(qs, key=lambda x: x["metric"])
                out.append(item)
            return sorted(out, key=lambda x: x["name"])
        return self.run(fn)

    # ------------------------------------------------------------------
    # 6. 可用区
    # ------------------------------------------------------------------
    def zones(self, region=""):
        def fn():
            c = self.client("ZonesClient")
            out = []
            for z in c.list(project=self.project_id):
                if region and not z.name.startswith(region + "-"):
                    continue
                out.append({
                    "name": z.name,
                    "status": z.status,
                    "region": (z.region or "").rsplit("/", 1)[-1],
                    # Zone 资源没有 available_machine_types 字段（实测确认），
                    # 只有 available_cpu_platforms；机型清单要另走 MachineTypesClient。
                    "cpuPlatforms": list(z.available_cpu_platforms or []),
                })
            return sorted(out, key=lambda x: x["name"])
        return self.run(fn)

    # ------------------------------------------------------------------
    # 7. 机器类型（某个 zone 下可用的全部机型）
    # ------------------------------------------------------------------
    def machine_types(self, zone, limit=400):
        def fn():
            c = self.client("MachineTypesClient")
            out = []
            for m in c.list(project=self.project_id, zone=zone):
                acc = []
                for a in (m.accelerators or []):
                    acc.append({"type": (a.guest_accelerator_type or "").rsplit("/", 1)[-1],
                                "count": a.guest_accelerator_count})
                out.append({
                    "name": m.name,
                    "guestCpus": m.guest_cpus,
                    "memoryMb": m.memory_mb,
                    "memoryGb": round((m.memory_mb or 0) / 1024, 2),
                    "isSharedCpu": bool(m.is_shared_cpu),
                    "architecture": m.architecture or "",
                    "accelerators": acc,
                    "maxPds": m.maximum_persistent_disks,
                })
                if len(out) >= limit:
                    break
            return sorted(out, key=lambda x: (x["guestCpus"] or 0, x["name"]))
        return self.run(fn)

    # ------------------------------------------------------------------
    # 8. 公共镜像 / 镜像族
    # ------------------------------------------------------------------
    def images(self, projects=("ubuntu-os-cloud", "debian-cloud", "cos-cloud",
                               "rocky-linux-cloud", "windows-cloud")):
        def fn():
            from google.cloud import compute_v1
            from concurrent.futures import ThreadPoolExecutor
            c = self.client("ImagesClient")
            out = {}

            def one(p):
                try:
                    fams = []
                    # 本版本 ImagesClient.list 只接受 request 对象，
                    # 传 filter=/max_results= 关键字会 TypeError。
                    req = compute_v1.ListImagesRequest(
                        project=p, filter="deprecated.state != DEPRECATED",
                        max_results=200)
                    for img in c.list(request=req):
                        fams.append({
                            "family": img.family or img.name,
                            "name": img.name,
                            "diskSizeGb": img.disk_size_gb,
                            "status": img.status,
                            "creationTimestamp": img.creation_timestamp,
                        })
                    # 同一 family 只留最新一个
                    latest = {}
                    for f in fams:
                        cur = latest.get(f["family"])
                        if not cur or (f["creationTimestamp"] or "") > (cur["creationTimestamp"] or ""):
                            latest[f["family"]] = f
                    return p, sorted(latest.values(),
                                     key=lambda x: x["family"] or x["name"])
                except Exception as e:
                    return p, {"error": _brief(e)}

            # 5 个公共镜像项目串行实测 16s，并发后 4s 级
            with ThreadPoolExecutor(max_workers=5) as ex:
                for p, v in ex.map(one, projects):
                    out[p] = v
            return out
        return self.run(fn)

    # ------------------------------------------------------------------
    # 9. VPC 网络
    # ------------------------------------------------------------------
    def networks(self):
        def fn():
            c = self.client("NetworksClient")
            out = []
            for n in c.list(project=self.project_id):
                rc = n.routing_config
                out.append({
                    "name": n.name,
                    "id": str(n.id),
                    "autoCreateSubnetworks": bool(n.auto_create_subnetworks),
                    "mtu": n.mtu,
                    "routingMode": (rc.routing_mode if rc else "") or "",
                    "subnetworkCount": len(n.subnetworks or []),
                    "creationTimestamp": n.creation_timestamp,
                    "description": n.description or "",
                })
            return sorted(out, key=lambda x: x["name"])
        return self.run(fn)

    # ------------------------------------------------------------------
    # 10. 子网（可指定区域，留空则遍历全部区域）
    # ------------------------------------------------------------------
    def subnetworks(self, region=""):
        def fn():
            c = self.client("SubnetworksClient")
            out = []
            # 早先是「先列 43 个区域、再逐区域 list」——实测耗时 65.8 秒（42 个子网）。
            # 改用 aggregated_list：一次请求拿全部区域的子网，实测降到 3 秒级。
            for key, resp in c.aggregated_list(project=self.project_id):
                rg = key.split("/")[-1]
                if region and rg != region:
                    continue
                for s in _agg_items(resp):
                    out.append({
                        "name": s.name,
                        "region": rg,
                        "network": (s.network or "").rsplit("/", 1)[-1],
                        "ipCidrRange": s.ip_cidr_range,
                        "gatewayAddress": s.gateway_address,
                        "privateIpGoogleAccess": bool(s.private_ip_google_access),
                        "purpose": s.purpose or "PRIVATE",
                        "stackType": s.stack_type or "",
                        "creationTimestamp": s.creation_timestamp,
                    })
            return sorted(out, key=lambda x: (x["region"], x["name"]))
        return self.run(fn)

    # ------------------------------------------------------------------
    # 11. 防火墙规则（含明细：协议端口 / 来源 / 目标标签）
    # ------------------------------------------------------------------
    def firewalls(self):
        def fn():
            c = self.client("FirewallsClient")
            out = []
            for f in c.list(project=self.project_id):
                out.append({
                    "name": f.name,
                    "network": (f.network or "").rsplit("/", 1)[-1],
                    "direction": f.direction or "INGRESS",
                    "priority": f.priority,
                    "disabled": bool(f.disabled),
                    # Firewall 资源没有 action 字段（实测确认）：
                    # 有 allowed 即 ALLOW，有 denied 即 DENY。
                    "sourceRanges": list(f.source_ranges or []),
                    "destinationRanges": list(f.destination_ranges or []),
                    "targetTags": list(f.target_tags or []),
                    "sourceTags": list(f.source_tags or []),
                    "sourceServiceAccounts": list(f.source_service_accounts or []),
                    "targetServiceAccounts": list(f.target_service_accounts or []),
                    "allowed": [{"protocol": a.I_p_protocol,
                                 "ports": list(a.ports or [])} for a in (f.allowed or [])],
                    "denied": [{"protocol": a.I_p_protocol,
                                "ports": list(a.ports or [])} for a in (f.denied or [])],
                    "logConfigEnabled": bool(getattr(f.log_config, "enable", False)
                                             if f.log_config else False),
                    "creationTimestamp": f.creation_timestamp,
                })
            for r in out:
                r["action"] = "DENY" if r["denied"] else "ALLOW"
            # 全开放规则要能一眼看出来（红队视角：0.0.0.0/0 是暴露面）
            for r in out:
                r["openToWorld"] = any(
                    sr in ("0.0.0.0/0", "::/0") for sr in r["sourceRanges"]) or \
                    any(dr in ("0.0.0.0/0", "::/0") for dr in r["destinationRanges"])
            return sorted(out, key=lambda x: (x["priority"], x["name"]))
        return self.run(fn)

    # ------------------------------------------------------------------
    # 12. 磁盘
    # ------------------------------------------------------------------
    def disks(self):
        def fn():
            c = self.client("DisksClient")
            out = []
            for zone, resp in c.aggregated_list(project=self.project_id):
                for d in (resp.disks or []):
                    out.append({
                        "name": d.name,
                        "zone": zone.lstrip("zones/") if zone else "",
                        "sizeGb": d.size_gb,
                        "type": (d.type_ or "").rsplit("/", 1)[-1],
                        "status": d.status,
                        "users": [(u or "").rsplit("/", 1)[-1] for u in (d.users or [])],
                        "sourceImage": (d.source_image or "").rsplit("/", 1)[-1],
                        "creationTimestamp": d.creation_timestamp,
                        "physicalBlockSizeBytes": d.physical_block_size_bytes,
                    })
            # 没挂到任何实例的磁盘 = 空转计费
            for d in out:
                d["orphan"] = not d["users"]
            return sorted(out, key=lambda x: (x["zone"], x["name"]))
        return self.run(fn)

    # ------------------------------------------------------------------
    # 13. 快照
    # ------------------------------------------------------------------
    def snapshots(self):
        def fn():
            c = self.client("SnapshotsClient")
            out = []
            for s in c.list(project=self.project_id):
                out.append({
                    "name": s.name,
                    "status": s.status,
                    "diskSizeGb": s.disk_size_gb,
                    "storageBytes": s.storage_bytes,
                    "storageBytesGb": round((s.storage_bytes or 0) / (1024 ** 3), 3),
                    "sourceDisk": (s.source_disk or "").rsplit("/", 1)[-1],
                    "sourceDiskId": s.source_disk_id,
                    "creationTimestamp": s.creation_timestamp,
                })
            return sorted(out, key=lambda x: x["name"])
        return self.run(fn)

    # ------------------------------------------------------------------
    # 14. 静态 IP（区域级 + 全局）
    # ------------------------------------------------------------------
    def addresses(self):
        def fn():
            out = []
            try:
                rc = self.client("AddressesClient")
                for region, resp in rc.aggregated_list(project=self.project_id):
                    for a in (resp.addresses or []):
                        out.append({
                            "name": a.name,
                            "address": a.address,
                            "region": region.lstrip("regions/") if region else "",
                            "scope": "regional",
                            "type": a.type_ or "",
                            "status": a.status,
                            "users": [(u or "").rsplit("/", 1)[-1] for u in (a.users or [])],
                        })
            except Exception as e:
                out.append({"error": _brief(e)})
            try:
                gc = self.client("GlobalAddressesClient")
                for a in gc.list(project=self.project_id):
                    out.append({
                        "name": a.name,
                        "address": a.address,
                        "region": "global",
                        "scope": "global",
                        "type": a.type_ or "",
                        "status": a.status,
                        "users": [(u or "").rsplit("/", 1)[-1] for u in (a.users or [])],
                    })
            except Exception:
                pass
            for a in out:
                if "users" in a:
                    a["inUse"] = bool(a["users"])
            return out
        return self.run(fn)

    # ------------------------------------------------------------------
    # 15. 服务账号（IAM）
    # ------------------------------------------------------------------
    def service_accounts(self):
        def fn():
            d = self.rest(
                f"https://iam.googleapis.com/v1/projects/{self.project_id}/serviceAccounts",
                params={"pageSize": 100})
            out = []
            for a in d.get("accounts", []):
                out.append({
                    "email": a.get("email", ""),
                    "displayName": a.get("displayName", ""),
                    "disabled": bool(a.get("disabled")),
                    "uniqueId": a.get("uniqueId", ""),
                    "oauth2ClientId": a.get("oauth2ClientId", ""),
                    "description": a.get("description", ""),
                })
            return out
        return self.run(fn)

    # ------------------------------------------------------------------
    # 16. 计算资源汇总（实例 + 磁盘 + IP 的统计）
    # ------------------------------------------------------------------
    def summary(self):
        def fn():
            insts = []
            c = self.client("InstancesClient")
            for zone, resp in c.aggregated_list(project=self.project_id):
                for i in (resp.instances or []):
                    ip, pip = "", ""
                    for ni in (i.network_interfaces or []):
                        pip = pip or ni.network_i_p
                        for ac in (ni.access_configs or []):
                            ip = ip or ac.nat_i_p
                    insts.append({
                        "name": i.name,
                        "zone": zone.lstrip("zones/") if zone else "",
                        "status": i.status,
                        "machineType": (i.machine_type or "").rsplit("/", 1)[-1],
                        "ip": ip, "privateIp": pip,
                        "cpuPlatform": i.cpu_platform or "",
                        "creationTimestamp": i.creation_timestamp,
                        "preemptible": bool(i.scheduling.preemptible) if i.scheduling else False,
                        "spot": bool(getattr(i.scheduling, "provisioning_model", "") ==
                                     "SPOT") if i.scheduling else False,
                        "disks": len(i.disks or []),
                        "tags": list((i.tags.items if i.tags else []) or []),
                    })
            by_status, by_zone, by_type = {}, {}, {}
            for i in insts:
                by_status[i["status"]] = by_status.get(i["status"], 0) + 1
                by_zone[i["zone"]] = by_zone.get(i["zone"], 0) + 1
                by_type[i["machineType"]] = by_type.get(i["machineType"], 0) + 1
            # CPU 合计（e2-micro 等共享核机型按 GCP 口径计 2 vCPU）
            total_cpu = 0
            try:
                families = {}
                for r in self.client("MachineTypesClient").aggregated_list(
                        project=self.project_id):
                    for m in (r.machine_types or []):
                        families.setdefault(m.name, m.guest_cpus)
                for i in insts:
                    total_cpu += families.get(i["machineType"], 0) or 0
            except Exception:
                pass
            return {
                "instanceCount": len(insts),
                "running": by_status.get("RUNNING", 0),
                "stopped": by_status.get("TERMINATED", 0) + by_status.get("STOPPED", 0),
                "byStatus": by_status,
                "byZone": by_zone,
                "byMachineType": by_type,
                "totalVCpu": total_cpu,
                "preemptible": sum(1 for i in insts if i["preemptible"]),
                "spot": sum(1 for i in insts if i["spot"]),
                "instances": insts,
            }
        return self.run(fn)

    # ------------------------------------------------------------------
    # 17. 其他网络/负载均衡类资源（有多少列多少，权限不足就报原因）
    # ------------------------------------------------------------------
    def extras(self):
        """路由器 / VPN / 转发规则 / 实例组 / 模板 / 健康检查 / 保留——按需枚举"""
        jobs = [
            ("routers", "RoutersClient", "region"),
            ("vpnTunnels", "VpnTunnelsClient", "region"),
            ("forwardingRules", "ForwardingRulesClient", "region"),
            ("globalForwardingRules", "GlobalForwardingRulesClient", None),
            ("instanceGroups", "InstanceGroupsClient", "zone"),
            ("instanceTemplates", "InstanceTemplatesClient", None),
            ("healthChecks", "HealthChecksClient", None),
            ("backendServices", "BackendServicesClient", None),
            ("reservations", "ReservationsClient", "zone"),
            ("commitments", "RegionCommitmentsClient", "region"),
        ]

        def fn():
            out = {}
            for label, cls, scope in jobs:
                try:
                    c = self.client(cls)
                    names = []
                    if scope in ("region", "zone"):
                        for key, resp in c.aggregated_list(project=self.project_id):
                            prefix = f"{key.split('/')[-1]}/"
                            for it in _agg_items(resp):
                                names.append(prefix + it.name)
                    else:
                        for it in c.list(project=self.project_id):
                            names.append(it.name)
                    out[label] = {"count": len(names), "items": sorted(names)}
                except Exception as e:
                    # NotImplementedError：部分 aggregate 类型在本版本库里没实现
                    out[label] = {"count": 0, "items": [], "error": _brief(e)}
            return out
        return self.run(fn)


def _agg_items(resp):
    """
    从 aggregated_list 的响应里取出资源列表。

    为什么不能按类名拼字段名：aggregated_list 的响应字段名与客户端类名不同构。
    例如 RegionCommitmentsClient 的响应字段是 commitments（不是
    region_commitments），GlobalForwardingRulesClient 是
    global_forwarding_rules 但走的又是 list。硬拼字段名会静默拿到空列表，
    看起来像「项目里没有这些资源」，属于最危险的假阴性。

    这里改为扫描响应对象的字段，取第一个「元素带 name 属性的列表」。
    """
    for f in resp._pb.DESCRIPTOR.fields:
        # 注意：新版 protobuf（upb）的 FieldDescriptor 没有 label 属性，
        # 写成 f.label != f.LABEL_REPEATED 会直接 AttributeError。
        # 判断重复字段要用 is_repeated。
        if not f.is_repeated or f.name in ("warning", "warnings"):
            continue
        val = getattr(resp, f.name, None)
        if val and hasattr(val[0], "name"):
            return list(val)
    return []



# ----------------------------------------------------------------------
# 对外入口：按 section 名批量勘察
# ----------------------------------------------------------------------
SECTIONS = {
    "account":          lambda ins, p: ins.account(),
    "project":          lambda ins, p: ins.project(),
    "services":         lambda ins, p: ins.services(),
    "billing":          lambda ins, p: ins.billing(),
    "serviceAccounts":  lambda ins, p: ins.service_accounts(),
    "regions":          lambda ins, p: ins.regions(),
    "zones":            lambda ins, p: ins.zones(p.get("region", "")),
    "machineTypes":     lambda ins, p: ins.machine_types(
        p.get("zone") or "us-central1-a"),
    "images":           lambda ins, p: ins.images(),
    "networks":         lambda ins, p: ins.networks(),
    "subnetworks":      lambda ins, p: ins.subnetworks(p.get("region", "")),
    "firewalls":        lambda ins, p: ins.firewalls(),
    "disks":            lambda ins, p: ins.disks(),
    "snapshots":        lambda ins, p: ins.snapshots(),
    "addresses":        lambda ins, p: ins.addresses(),
    "summary":          lambda ins, p: ins.summary(),
    "extras":           lambda ins, p: ins.extras(),
}

# 默认「快速」节：都是必有的 compute 只读权限，适合首屏
DEFAULT_SECTIONS = ["account", "summary", "regions", "zones", "networks",
                    "subnetworks", "firewalls", "disks", "snapshots", "addresses"]

# 「深度」节：常需要额外权限或较慢，前端按需加载
DEEP_SECTIONS = ["project", "services", "billing", "serviceAccounts",
                 "machineTypes", "images", "extras"]


def inspect_sections(key_path, project_id, email, sections, params=None,
                     proxy="", proxy_type="HTTPS", workers=5):
    """
    按名称逐节勘察，返回 {section: {ok, ms, data|error}}。

    并发执行：各节互相独立，串行跑完整套实测 88 秒（extras 21s + images 16s
    是大头），用户等不起。这里用有界线程池并发，实测降到 20 秒级。

    代理只在**外层设一次**：ProxyEnvContext 改的是进程级环境变量，
    若每节各设各的，并发时会互相清掉对方的代理配置。
    """
    params = params or {}
    ins = Inspector(key_path, project_id, email, proxy=proxy, proxy_type=proxy_type)

    def one(name):
        fn = SECTIONS.get(name)
        if not fn:
            return name, {"ok": False, "ms": 0, "error": f"未知的勘察节：{name}"}
        return name, fn(ins, params)

    out = {}
    with ProxyEnvContext(ins.proxy_url):
        ins._proxy_managed = True
        names = list(sections)
        if workers <= 1 or len(names) <= 1:
            for n in names:
                k, v = one(n)
                out[k] = v
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(workers, len(names))) as ex:
                for k, v in ex.map(one, names):
                    out[k] = v
    # 保持调用方传入的顺序，前端才好按序渲染
    return {n: out.get(n, {"ok": False, "ms": 0, "error": "未执行"}) for n in sections}
