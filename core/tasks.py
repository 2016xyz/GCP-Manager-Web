# -*- coding: utf-8 -*-
"""
任务引擎（Web 版）

职责：
  - 批量创建实例（每个账号可指定数量），支持并发、区域配额规划、失败重试、换区重试
  - 创建后可选：等待 SSH → 执行安装命令 → 执行验证命令
  - 对已有实例批量执行命令
  - 全部过程写入 tasks/logs 表，前端轮询 / WebSocket 获取

与原版 v7.6 的差异：
  - 不再依赖 Qt 信号，改为 store + 回调
  - create 的服务器规格来自前端 spec（机型/镜像/磁盘/网络/标签/抢占式…全自定义）
  - 区域选择支持：auto_free / auto_paid / custom(指定列表) / single(指定单区)
"""
import random
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import catalog
from . import ssh as ssh_mod
from .gcp import GCPService, build_instance_spec


class LogSink:
    def __init__(self, store):
        self.store = store
        self._buf = []
        self._lock = threading.Lock()
        self._last_flush = 0

    def __call__(self, message, task_id=None, level="info"):
        msg = (message or "").rstrip("\n")
        if not msg:
            return
        with self._lock:
            self._buf.append((time.time(), task_id, level, msg))
            now = time.time()
            if len(self._buf) >= 20 or (now - self._last_flush) > 1.0:
                self._flush_locked()
        if level in ("error", "warn", "success"):
            self._flush()

    def _flush_locked(self):
        if not self._buf:
            return
        batch = self._buf
        self._buf = []
        self._last_flush = time.time()
        with self.store.lock:
            self.store.conn.executemany(
                "INSERT INTO logs(ts,task_id,level,message) VALUES(?,?,?,?)", batch)
            self.store.conn.commit()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush(self):
        self.flush()


class TaskManager:
    """所有耗时操作都在后台线程/线程池里跑，HTTP 层只负责排队与查询"""

    def __init__(self, store):
        self.store = store
        self.log = LogSink(store)
        self.lock = threading.RLock()
        self.seq = int(time.time() * 1000) % 100000000
        self.executors = {}
        self.cancel_flags = {}
        self._ssh_stopper = None
        self.api_lock = threading.Lock()
        self.api_tasks = {}

    # ------------------------------------------------------------------
    def new_task_id(self, prefix="t"):
        with self.lock:
            self.seq += 1
            return f"{prefix}-{int(time.time())}-{self.seq}"

    def _track_api_task(self, task_id, kind, payload):
        with self.api_lock:
            self.api_tasks[task_id] = {
                "id": task_id, "kind": kind, "status": "queued",
                "payload": payload, "created_at": time.time(),
                "updated_at": time.time(), "message": "",
            }

    def update_task(self, task_id, status=None, message=None, result=None):
        kw = {}
        if status:
            kw["status"] = status
        if message is not None:
            kw["message"] = message
        if result is not None:
            kw["result"] = result
        if kw:
            self.store.update_task(task_id, **kw)
        with self.api_lock:
            t = self.api_tasks.get(task_id)
            if t:
                if status:
                    t["status"] = status
                if message is not None:
                    t["message"] = message
                if result is not None:
                    t["result"] = result
                t["updated_at"] = time.time()

    def api_tasks_snapshot(self, limit=200):
        with self.api_lock:
            items = sorted(self.api_tasks.values(), key=lambda x: x["created_at"], reverse=True)
        return items[:limit]

    def cancel(self, task_id):
        self.cancel_flags[task_id] = True
        if self._ssh_stopper:
            self._ssh_stopper.stop()
        self.log(f"[{task_id}] 已请求取消", task_id, "warn")
        return {"ok": True, "task_id": task_id, "cancel": True}

    def _is_cancelled(self, task_id):
        return bool(self.cancel_flags.get(task_id))

    # ------------------------------------------------------------------
    # 账号 / GCP 服务
    # ------------------------------------------------------------------
    def account_service(self, account):
        return GCPService(account["key_path"], account["project_id"], account["email"],
                          account.get("proxy", ""), account.get("proxy_type", "HTTPS"))

    # ------------------------------------------------------------------
    # 区域规划
    # ------------------------------------------------------------------
    def resolve_region_pool(self, spec):
        """
        返回 (可创建区域列表, 每区上限, 是否限定单区)
        支持：
          region_mode = auto_free | auto_paid | custom | single
        """
        region_mode = (spec.get("region_mode") or "auto_free").strip()
        max_per_region = int(spec.get("max_per_region") or 4)
        if region_mode == "auto_free":
            pool = list(catalog.FREE_REGIONS.keys())
            return pool, max_per_region, False
        if region_mode == "auto_paid":
            pool = list(catalog.PAID_REGIONS.keys())
            return pool, max_per_region, False
        if region_mode == "custom":
            pool = [r for r in (spec.get("regions") or []) if r]
            pool = [r for r in pool if r in catalog.ALL_REGIONS] or pool
            return pool or list(catalog.FREE_REGIONS.keys()), max_per_region, False
        if region_mode == "single":
            # region 可能被写成 zone（us-west1-b）或空值：统一归一化为 region，
            # 否则后续按 region 拼 zone 会得到 us-west1-b-b 这类不存在的 zone
            region = str(spec.get("region") or "").strip() or "us-central1"
            if region.count("-") >= 2:
                region = region.rsplit("-", 1)[0]
            return [region], 10 ** 6, True
        pool = list(catalog.FREE_REGIONS.keys())
        return pool, max_per_region, False

    # 已知各 region 的 zone 后缀并不统一（us-west1 无 d/f，us-east1 无 a）。
    # 这里作为**离线兜底**仅用于无网络/无权限时；运行时优先向 GCP 拉真实 zone。
    ZONE_SUFFIX_FALLBACK = ("a", "b", "c", "d", "f")

    def zones_for_region(self, region, gcp=None):
        """
        解析某个 region 下真实存在的 zone 列表。

        修复两个真实缺陷：
          1) 入参可能是 region（us-west1）也可能是 zone（us-west1-b）。
             早先不做区分，把 us-west1-b 当 region 再拼后缀，得到
             us-west1-b-a / us-west1-b-b 这种不存在的 zone，
             API 报 "Permission denied on 'locations/us-west1-b-b'"，
             把「zone 不存在」伪装成「权限不足」。
          2) 后缀硬编码为 a/b/c/d/f，但各 region 实际后缀不同，
             us-west1 只有 a/b/c —— 总会先撞上不存在的 d/f。
        现在：优先用 GCP 返回的真实 zone；拿不到再退回后缀拼接，
        且拼接前先把入参归一化为 region。
        """
        region = (region or "").strip()
        # 若传入的是 zone（形如 us-west1-b），归一化为 region
        if region.count("-") >= 2:
            region = region.rsplit("-", 1)[0]

        cache = getattr(self, "_zone_cache", None)
        if cache is None:
            cache = self._zone_cache = {}
        if region in cache:
            return list(cache[region])

        real = []
        try:
            if gcp is None:
                # 无 gcp 实例时（例如单元测试）直接走兜底
                raise RuntimeError("no gcp client")
            real = gcp.list_zones(region) or []
        except Exception:
            real = []

        if real:
            cache[region] = list(real)
            return list(real)

        fallback = [f"{region}-{s}" for s in self.ZONE_SUFFIX_FALLBACK]
        return fallback

    # ------------------------------------------------------------------
    # 创建任务
    # ------------------------------------------------------------------
    def submit_create(self, payload):
        """
        payload:
          account_ids: [1,2]            # 空=全部账号
          count: 1                      # 每个账号创建台数
          spec: {...}                   # 自定义服务器配置（机型/镜像/磁盘/区域/标签/抢占…）
          login_mode: "ssh_key" | "root_password"
          ssh_public_key: "ssh-rsa AAA..."
          root_password: "xxx"          # 空则随机生成
          post_command: "curl ... | bash"
          verify_command: "docker ps"
          note: "这台机器的备注"        # 创建时填的备注，建完可在列表里改
          installs: ["docker","nps"]   # 创建后自动安装的预设（见 install_presets）
          concurrency: 3                # 同时创建的实例数
          account_workers: 1            # 同时处理的账号数
          retry_count: 2
          ssh_timeout: 300, idle_timeout: 180, command_timeout: 1800, verify_timeout: 180
          dry_run: false
        """
        payload = dict(payload or {})
        spec = build_instance_spec(payload.get("spec") or {})
        count = max(1, int(payload.get("count") or 1))
        retries = max(0, min(int(payload.get("retry_count") or 2), 5))
        concurrency = max(1, min(int(payload.get("concurrency") or 3), 30))
        account_workers = max(1, min(int(payload.get("account_workers") or 1), 10))
        dry_run = bool(payload.get("dry_run"))

        accounts = self.store.get_accounts()
        wanted = [str(x) for x in (payload.get("account_ids") or [])]
        if wanted:
            accounts = [a for a in accounts if str(a["id"]) in wanted]
        if not accounts:
            return {"ok": False, "error": "没有匹配的账号，请先导入 GCP 服务账号 JSON"}

        task_id = self.new_task_id("create")
        self.store.create_task(task_id, "create", payload)
        self._track_api_task(task_id, "create", payload)
        self.log(f"=== 任务 {task_id}：{len(accounts)} 个账号 × {count} 台 ===", task_id)
        self.log(f"[规格] 机型={spec['machine_type']} 镜像={spec['image_label']} "
                 f"磁盘={spec['disk_type']} {spec['disk_size_gb']}GB 区域模式={payload.get('spec', {}).get('region_mode', 'auto_free')}",
                 task_id)

        if dry_run:
            plan = self._plan_preview(accounts, count, spec, payload)
            self.update_task(task_id, "done", "dry-run 预览完成", {"plan": plan})
            return {"ok": True, "task_id": task_id, "dry_run": True, "plan": plan}

        t = threading.Thread(target=self._run_create_batch,
                             args=(task_id, accounts, count, spec, payload, retries,
                                   concurrency, account_workers),
                             daemon=True)
        t.start()
        return {"ok": True, "task_id": task_id, "accounts": len(accounts), "count": count}

    def _plan_preview(self, accounts, count, spec, payload):
        out = []
        for acc in accounts:
            try:
                gcp = self.account_service(acc)
                instances = gcp.list_instances()
            except Exception as exc:
                out.append({"account": acc["email"], "error": str(exc)})
                continue
            pool, max_per_region, _ = self.resolve_region_pool(payload.get("spec") or {})
            used = {}
            for inst in instances:
                region = (inst.get("zone") or "").rsplit("-", 1)[0]
                used[region] = used.get(region, 0) + 1
            avail = [r for r in pool if used.get(r, 0) < max_per_region]
            out.append({
                "account": acc["email"], "project_id": acc["project_id"],
                "existing_instances": len(instances),
                "region_usage": used,
                "available_regions": avail,
                "planned_instances": count,
                "can_create": len(avail) > 0,
                "machine_type": spec["machine_type"],
            })
        return out

    def _run_create_batch(self, task_id, accounts, count, spec, payload, retries,
                          concurrency, account_workers):
        started = time.time()
        results = []
        try:
            self.update_task(task_id, "running", f"开始创建 {len(accounts)} 个账号")
            login_mode = (payload.get("login_mode") or "root_password").strip()
            ssh_public_key = (payload.get("ssh_public_key") or "").strip()
            root_password_in = (payload.get("root_password") or "").strip()
            post_command = (payload.get("post_command") or "").strip()
            verify_command = (payload.get("verify_command") or "").strip()

            if spec.get("image_os") == "windows" and login_mode == "root_password":
                self.log("[警告] 镜像为 Windows，startup-script 不会执行 bash，Root 密码模式无效，已按 SSH 密钥模式处理", task_id, "warn")

            def handle_account(acc):
                if self._is_cancelled(task_id):
                    return {"account": acc["email"], "ok": False, "error": "已取消"}
                return self._create_for_account(
                    task_id, acc, count, spec, payload, retries, concurrency,
                    login_mode, ssh_public_key, root_password_in, post_command, verify_command)

            if account_workers <= 1 or len(accounts) == 1:
                for acc in accounts:
                    if self._is_cancelled(task_id):
                        break
                    results.append(handle_account(acc))
            else:
                with ThreadPoolExecutor(max_workers=min(account_workers, len(accounts)),
                                        thread_name_prefix="acct") as ex:
                    futs = {ex.submit(handle_account, a): a for a in accounts}
                    for fut in as_completed(futs):
                        try:
                            results.append(fut.result())
                        except Exception as exc:
                            acc = futs[fut]
                            results.append({"account": acc["email"], "ok": False, "error": str(exc)})

            total_ok = sum(r.get("created", 0) for r in results)
            total_fail = sum(r.get("failed", 0) for r in results)
            status = "done" if not self._is_cancelled(task_id) else "cancelled"
            summary = {
                "accounts": len(accounts), "per_account_count": count,
                "created": total_ok, "failed": total_fail,
                "elapsed_sec": round(time.time() - started, 1),
                "results": results,
            }
            self.update_task(task_id, status,
                             f"完成：成功 {total_ok} 台，失败 {total_fail} 台，耗时 {summary['elapsed_sec']}s",
                             summary)
            self.log(f"=== 任务 {task_id} 结束：成功 {total_ok} / 失败 {total_fail} ===",
                     task_id, "success" if total_fail == 0 else "warn")
        except Exception as exc:
            self.log(f"任务异常：{exc}\n{traceback.format_exc()}", task_id, "error")
            self.update_task(task_id, "failed", str(exc), {"results": results})
        finally:
            self.log.flush()

    def _create_for_account(self, task_id, acc, count, spec, payload, retries, concurrency,
                            login_mode, ssh_public_key, root_password_in, post_command,
                            verify_command):
        label = acc["email"] or acc["project_id"]
        result = {"account": label, "account_id": acc["id"], "project_id": acc["project_id"],
                  "created": 0, "failed": 0, "instances": []}
        try:
            gcp = self.account_service(acc)
        except Exception as exc:
            self.log(f"[{label}] 初始化 GCP 客户端失败：{exc}", task_id, "error")
            result["error"] = str(exc)
            return result

        if login_mode == "ssh_key" and ssh_public_key:
            ok, msg = gcp.add_ssh_key(ssh_public_key, "root")
            self.log(f"[{label}] SSH 公钥注入{'成功' if ok else '失败：' + msg}", task_id,
                     "success" if ok else "warn")

        pool, max_per_region, single = self.resolve_region_pool(payload.get("spec") or {})
        region_count = {r: 0 for r in pool}
        try:
            existing = gcp.list_instances()
            for inst in existing:
                region = (inst.get("zone") or "").rsplit("-", 1)[0]
                if region in region_count:
                    region_count[region] += 1
        except Exception as exc:
            self.log(f"[{label}] 实例清点失败：{exc}", task_id, "error")
            result["error"] = str(exc)
            return result

        self.log(f"[{label}] 现有实例区域分布：{region_count}", task_id)

        planning_lock = threading.Lock()
        created_lock = threading.Lock()

        def reserve():
            with planning_lock:
                avail = [r for r in pool if region_count[r] < max_per_region]
                if not avail:
                    return None, []
                if single:
                    chosen = pool[0]
                elif spec.get("region"):
                    chosen = spec["region"] if spec["region"] in avail else random.choice(avail)
                else:
                    chosen = random.choice(avail)
                region_count[chosen] += 1
                return chosen, avail[:]

        def release(region):
            if region and region in region_count:
                with planning_lock:
                    region_count[region] = max(0, region_count[region] - 1)

        def run_one(idx):
            reserved, candidates = reserve()
            if not reserved:
                return {"ok": False, "error": "所有可用区域配额已满（每区上限 %d）" % max_per_region}
            name = f"vm-{acc['id']}-{int(time.time()) % 100000}-{idx}-{random.randint(1000, 9999)}"
            root_password = ""
            startup_script = ""
            if login_mode == "root_password":
                root_password = root_password_in or self._rand_password()
                startup_script = ssh_mod.build_root_startup_script(root_password)

            tried = set()
            ordered_regions = [reserved] + [r for r in candidates if r != reserved]
            random.shuffle(ordered_regions[1:])
            last_err = ""
            res = None
            for region in ordered_regions:
                zone_list = [z for z in self.zones_for_region(region, gcp) if z not in tried]
                random.shuffle(zone_list)
                for zone in zone_list:
                    if self._is_cancelled(task_id):
                        release(reserved)
                        return {"ok": False, "error": "已取消"}
                    attempt_spec = dict(spec)
                    attempt_spec["region"] = region
                    ok, out = gcp.create_instance(zone, name, startup_script=startup_script,
                                                  spec=attempt_spec)
                    if ok:
                        res = out
                        break
                    last_err = str(out)
                    tried.add(zone)
                    if last_err != "资源耗尽":
                        break
                if res:
                    break
                if last_err != "资源耗尽":
                    break

            if not res:
                release(reserved)
                # 换区重试
                for attempt in range(retries):
                    if self._is_cancelled(task_id):
                        break
                    r2, _ = reserve()
                    if not r2:
                        break
                    z2 = random.choice(self.zones_for_region(r2, gcp))
                    a_spec = dict(spec)
                    a_spec["region"] = r2
                    self.log(f"[{label}] {name} 重试 {attempt + 1}/{retries} → {z2}：{last_err}", task_id, "warn")
                    ok, out = gcp.create_instance(z2, name, startup_script=startup_script, spec=a_spec)
                    if ok:
                        res = out
                        break
                    last_err = str(out)
                    release(r2)
                    time.sleep(3)
            if not res:
                return {"ok": False, "name": name, "error": last_err or "创建失败"}

            ip = res.get("ip", "")
            zone = res.get("zone", "")
            actual_region = zone.rsplit("-", 1)[0]
            item = {"ok": True, "name": name, "ip": ip, "private_ip": res.get("private_ip", ""),
                    "zone": zone, "region": actual_region, "machine_type": spec["machine_type"],
                    "image": spec["image_label"], "disk": f"{spec['disk_type']} {spec['disk_size_gb']}GB",
                    "stage": "created"}
            # 备注与安装项来自本次任务参数；创建时间优先用 GCP 返回的真实时间
            note = (payload.get("note") or "").strip()
            installs = ",".join(payload.get("installs") or [])
            created_ts = float(res.get("created_ts") or 0) or None
            self.store.save_vm(name, ip,
                               root_password if login_mode == "root_password" else "",
                               acc["id"], zone, spec["machine_type"], spec["image_key"],
                               spec["disk_type"], spec["disk_size_gb"],
                               note=note, created_at=created_ts, installs=installs)

            with created_lock:
                result["created"] += 1
            self.log(f"[{label}] ✅ {name} 创建成功 | {actual_region}({zone}) | IP {ip} | "
                     f"{spec['machine_type']} | {spec['image_label']}"
                     + (f" | Root密码 {root_password}" if root_password else ""),
                     task_id, "success")

            # ---------- 创建后 SSH 阶段 ----------
            if post_command or verify_command:
                user = "root" if login_mode == "root_password" else spec.get("image_user", "ubuntu")
                pwd = root_password
                ssh_timeout = int(payload.get("ssh_timeout") or 300)
                if not ip:
                    item.update({"ok": False, "stage": "ssh", "error": "实例无公网 IP，无法 SSH"})
                    return item
                self.log(f"[{label}] {name} 等待 SSH 就绪（{user}@{ip}）…", task_id)
                ok_ssh, msg = ssh_mod.wait_ssh_ready(ip, user, pwd, timeout=ssh_timeout,
                                                     log_callback=lambda t, tid=task_id: self.log(t, tid))
                item["ssh_ok"] = ok_ssh
                if not ok_ssh:
                    item.update({"ok": False, "stage": "ssh", "error": f"SSH 不可用：{msg}"})
                    self.log(f"[{label}] ❌ {name} SSH 不可用：{msg}", task_id, "error")
                    return item
                self.log(f"[{label}] {name} SSH 就绪", task_id, "success")
                if post_command:
                    ok_c, out_c = ssh_mod.run_ssh_command(
                        ip, user, pwd, post_command,
                        connect_timeout=15,
                        idle_timeout=int(payload.get("idle_timeout") or 180),
                        total_timeout=int(payload.get("command_timeout") or 1800),
                        log_callback=lambda t, tid=task_id, nm=name, lb=label: self.log(
                            f"[{lb}][{nm}] {t}", tid))
                    item["post_command_ok"] = ok_c
                    item["post_command_tail"] = (out_c or "")[-4000:]
                    # 不传 note/created_at/installs —— save_vm 会保留原值，
                    # 否则第二次保存会把用户填的备注冲成空
                    self.store.save_vm(name, ip, root_password or "", acc["id"], zone,
                                       spec["machine_type"], spec["image_key"],
                                       spec["disk_type"], spec["disk_size_gb"])
                    if not ok_c:
                        item.update({"ok": False, "stage": "command"})
                        self.log(f"[{label}] ⚠️ {name} 安装命令执行失败", task_id, "error")
                    else:
                        item["stage"] = "installed"
                        self.log(f"[{label}] ✅ {name} 安装命令执行完成", task_id, "success")
                if verify_command:
                    ok_v, out_v = ssh_mod.run_ssh_command(
                        ip, user, pwd, verify_command, connect_timeout=15,
                        idle_timeout=60, total_timeout=int(payload.get("verify_timeout") or 180))
                    item["verify_ok"] = ok_v
                    item["verify_output"] = (out_v or "")[-2000:]
                    self.log(f"[{label}] 验证命令 {'通过' if ok_v else '未通过'}：{(out_v or '')[-300:]}",
                             task_id, "success" if ok_v else "warn")
                    if not ok_v:
                        item.update({"ok": False, "stage": "verify"})
                    else:
                        item["stage"] = "done"
            return item

        workers = max(1, min(concurrency, count))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vm") as ex:
            futs = [ex.submit(run_one, i) for i in range(1, count + 1)]
            for fut in as_completed(futs):
                try:
                    item = fut.result()
                except Exception as exc:
                    item = {"ok": False, "error": str(exc)}
                result["instances"].append(item)
                if not item.get("ok", False):
                    result["failed"] += 1
        self.log(f"[{label}] 小结：成功 {result['created']} 台 / 失败 {result['failed']} 台", task_id)
        return result

    @staticmethod
    def _rand_password(length=16):
        import string as _s
        import secrets as _se
        alphabet = _s.ascii_letters + _s.digits + "!@#%^*-_"
        return "".join(_se.choice(alphabet) for _ in range(length))

    # ------------------------------------------------------------------
    # 对已有实例批量执行命令
    # ------------------------------------------------------------------
    def submit_execute(self, payload):
        payload = dict(payload or {})
        command = (payload.get("command") or "").strip()
        if not command:
            return {"ok": False, "error": "命令为空"}
        targets = payload.get("targets") or []
        all_instances = bool(payload.get("all", True))
        concurrency = max(1, min(int(payload.get("concurrency") or 10), 50))
        timeout = int(payload.get("command_timeout") or 600)
        idle = int(payload.get("idle_timeout") or 120)

        task_id = self.new_task_id("exec")
        self.store.create_task(task_id, "execute", payload)
        self._track_api_task(task_id, "execute", payload)
        t = threading.Thread(target=self._run_execute,
                             args=(task_id, command, targets, all_instances, concurrency, timeout, idle),
                             daemon=True)
        t.start()
        return {"ok": True, "task_id": task_id}

    def _run_execute(self, task_id, command, targets, all_instances, concurrency, timeout, idle):
        stopper = ssh_mod.SSHStopper()
        self._ssh_stopper = stopper
        self.update_task(task_id, "running", "开始执行命令")
        accounts = {a["id"]: a for a in self.store.get_accounts()}
        vms = {v["name"]: v for v in self.store.get_all_vms()}

        # 目标列表：显式指定优先，否则按账号拉取全部实例
        target_list = []
        wanted = list(targets)
        if wanted:
            for name in wanted:
                vm = vms.get(name, {})
                target_list.append({"name": name, "ip": vm.get("ip", ""),
                                    "password": vm.get("password") or "",
                                    "zone": vm.get("zone", ""),
                                    "account_id": vm.get("account_id")})
        if all_instances or not target_list:
            scoped = [a for a in accounts.values()
                      if not wanted or str(a["id"]) in {str(t.get("account_id")) for t in target_list}]
            for acc in (scoped or list(accounts.values())):
                try:
                    gcp = self.account_service(acc)
                    for inst in gcp.list_instances():
                        name = inst["name"]
                        if any(t["name"] == name for t in target_list):
                            continue
                        vm = vms.get(name, {})
                        target_list.append({"name": name, "ip": inst.get("ip", ""),
                                            "password": vm.get("password") or "",
                                            "zone": inst.get("zone", ""),
                                            "account_id": acc["id"],
                                            "account": acc["email"]})
                except Exception as exc:
                    self.log(f"列取实例失败（{acc['email']}）：{exc}", task_id, "error")

        if not target_list:
            self.update_task(task_id, "failed", "没有可执行的目标实例")
            return

        self.log(f"任务 {task_id}：在 {len(target_list)} 台实例上执行命令：{command[:200]}", task_id)
        results = []

        def one(tgt):
            ip = tgt.get("ip")
            if not ip:
                return {"name": tgt["name"], "ok": False, "output": "缺少 IP"}
            pwd = tgt.get("password") or ""
            user = "root" if pwd else "ubuntu"
            ok, out = ssh_mod.run_ssh_command(
                ip, user, pwd, command, connect_timeout=15,
                idle_timeout=idle, total_timeout=timeout,
                log_callback=lambda t, tid=task_id, nm=tgt["name"]: self.log(f"[{nm}] {t}", tid),
                stopper=stopper)
            return {"name": tgt["name"], "ip": ip, "ok": ok, "output": (out or "")[-4000:]}

        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="exec") as ex:
            futs = [ex.submit(one, t) for t in target_list]
            for fut in as_completed(futs):
                try:
                    results.append(fut.result())
                except Exception as exc:
                    results.append({"ok": False, "output": str(exc)})

        ok_count = sum(1 for r in results if r.get("ok"))
        self.update_task(task_id, "done",
                         f"执行完成：{ok_count}/{len(results)} 成功", {"results": results})
        self.log(f"任务 {task_id} 执行完成：{ok_count}/{len(results)} 成功", task_id, "success")

    # ------------------------------------------------------------------
    # 实例管理
    # ------------------------------------------------------------------
    def submit_instance_action(self, action, targets):
        """action: start / stop / reset / delete"""
        task_id = self.new_task_id("act")
        self.store.create_task(task_id, f"instance_{action}", {"targets": targets})
        self._track_api_task(task_id, f"instance_{action}", {"targets": targets})

        def run():
            self.update_task(task_id, "running", f"{action} {len(targets)} 台实例")
            accounts = {str(a["id"]): a for a in self.store.get_accounts()}
            vms = {v["name"]: v for v in self.store.get_all_vms()}
            results = []
            by_account = {}

            def locate(gcp, name, zone_hint=""):
                """在项目里定位实例的真实 zone；找不到返回空串"""
                if zone_hint:
                    return zone_hint
                try:
                    for inst in gcp.list_instances():
                        if inst.get("name") == name:
                            return inst.get("zone") or ""
                except Exception:
                    pass
                return ""

            # 先按本地记录归组
            unresolved = []
            for tgt in targets:
                vm = vms.get(tgt) or {}
                acc_id = str(vm.get("account_id") or "")
                if acc_id and acc_id in accounts:
                    by_account.setdefault(acc_id, []).append(
                        {"name": tgt, "zone": vm.get("zone") or ""})
                else:
                    unresolved.append({"name": tgt, "zone": vm.get("zone") or ""})

            # 本地没有记录的实例（预存在的、或用原版桌面工具建的），
            # 不能直接判「未找到所属账号」—— 界面能列出它，就该能操作它。
            # 逐个账号去各自项目里按名字找，找到即认领。
            if unresolved and accounts:
                for item in unresolved:
                    placed = False
                    for acc in accounts.values():
                        try:
                            gcp = self.account_service(acc)
                        except Exception:
                            continue
                        zone = locate(gcp, item["name"], item["zone"])
                        if zone:
                            by_account.setdefault(str(acc["id"]), []).append(
                                {"name": item["name"], "zone": zone})
                            placed = True
                            break
                    if not placed:
                        results.append({"name": item["name"], "ok": False,
                                        "error": "未在任何已配置账号的项目中找到该实例"
                                                 "（可能所属账号已删除或密钥已失效）"})
            elif unresolved:
                results += [{"name": i["name"], "ok": False, "error": "未配置任何账号"}
                            for i in unresolved]

            for acc_id, items in by_account.items():
                acc = accounts.get(acc_id)
                if not acc:
                    results += [{"name": i["name"], "ok": False, "error": "未找到所属账号"}
                                for i in items]
                    continue
                try:
                    gcp = self.account_service(acc)
                except Exception as exc:
                    results += [{"name": i["name"], "ok": False, "error": str(exc)}
                                for i in items]
                    continue
                fn = {"start": gcp.start_instance, "stop": gcp.stop_instance,
                      "reset": gcp.reset_instance, "delete": gcp.delete_instance}.get(action)
                if not fn:
                    self.update_task(task_id, "failed", f"不支持的操作 {action}")
                    return
                for i in items:
                    zone = locate(gcp, i["name"], i["zone"])
                    if not zone:
                        results.append({"name": i["name"], "ok": False, "error": "未知 zone"})
                        continue
                    ok, msg = fn(zone, i["name"])
                    self.log(f"[{acc['email']}] {action} {i['name']}：{msg}", task_id,
                             "success" if ok else "error")
                    results.append({"name": i["name"], "ok": ok, "message": msg})
                    if ok and action == "delete":
                        with self.store.lock:
                            self.store.conn.execute("DELETE FROM vm_passwords WHERE name=?",
                                                    (i["name"],))
                            self.store.conn.commit()
            ok_count = sum(1 for r in results if r.get("ok"))
            self.update_task(task_id, "done", f"{action}：{ok_count}/{len(results)} 成功",
                             {"results": results})
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "task_id": task_id}

    # ------------------------------------------------------------------
    def submit_refresh(self, account_ids=None):
        """刷新实例列表（同时触发一次快照缓存）"""
        task_id = self.new_task_id("refresh")
        self.store.create_task(task_id, "refresh", {"account_ids": account_ids})
        self._track_api_task(task_id, "refresh", {"account_ids": account_ids})

        def run():
            self.update_task(task_id, "running", "刷新中")
            accounts = self.store.get_accounts()
            wanted = [str(x) for x in (account_ids or [])]
            if wanted:
                accounts = [a for a in accounts if str(a["id"]) in wanted]
            all_inst = []
            for acc in accounts:
                try:
                    gcp = self.account_service(acc)
                    insts = gcp.list_instances()
                    for inst in insts:
                        inst["account_email"] = acc["email"]
                        inst["account_id"] = acc["id"]
                    all_inst += insts
                    self.log(f"刷新 {acc['email']}：{len(insts)} 台实例", task_id)
                except Exception as exc:
                    self.log(f"刷新失败 {acc['email']}：{exc}", task_id, "error")
            self.update_task(task_id, "done", f"共 {len(all_inst)} 台实例", {"instances": all_inst})
            self.log.flush()

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "task_id": task_id}

    def list_all_instances(self, account_ids=None):
        """
        同步拉取所有账号实例（供页面刷新）。

        这里刻意**不返回 root 密码明文**，只回 has_password 标记。
        原因：这个接口只需要 view 权限，而 viewer 是最低权限角色；
        把全账号的 root 密码明文塞进列表响应，等于任何能登录的人都能
        一次性拿到所有机器的 root。要看密码必须再走
        POST /api/instances/password 二次验证登录密码。
        """
        accounts = self.store.get_accounts()
        wanted = [str(x) for x in (account_ids or [])]
        if wanted:
            accounts = [a for a in accounts if str(a["id"]) in wanted]
        vms = {v["name"]: v for v in self.store.get_all_vms()}
        out = []
        errors = []
        now = time.time()
        for acc in accounts:
            try:
                gcp = self.account_service(acc)
                for inst in gcp.list_instances():
                    vm = vms.get(inst["name"], {})
                    inst["account_email"] = acc["email"]
                    inst["account_id"] = acc["id"]
                    # 账号备注：邮箱很长时界面上优先展示它
                    inst["account_label"] = acc.get("label") or ""
                    inst["has_password"] = bool(vm.get("password"))
                    inst["note"] = vm.get("note") or ""
                    inst["installs"] = [x for x in (vm.get("installs") or "").split(",") if x]

                    # 本地记录里有的字段优先（创建时的规格），GCP 实时数据兜底
                    machine_type = vm.get("machine_type") or inst.get("machine_type") or ""
                    disk_type = vm.get("disk_type") or inst.get("disk_type") or ""
                    disk_size = vm.get("disk_size_gb") or inst.get("disk_size_gb") or 0
                    inst["machine_type"] = machine_type
                    inst["disk_type"] = disk_type
                    inst["disk_size_gb"] = disk_size

                    # 创建时间：优先用本地记录（创建时从 GCP 抓的），
                    # 老记录没有就用 GCP 实时返回的 creation_timestamp
                    created_ts = vm.get("created_at") or inst.get("created_ts") or 0
                    inst["created_ts"] = created_ts

                    # 费用估算（参考价，非账单）
                    inst["cost"] = catalog.instance_cost(
                        machine_type, disk_type, disk_size,
                        inst.get("region") or inst.get("zone"),
                        created_at=created_ts or None,
                        now=now,
                        preemptible=bool(inst.get("preemptible")),
                        spot=bool(inst.get("spot")),
                        status=inst.get("status"),
                    )

                    inst["spec"] = {
                        "machine_type": machine_type,
                        "image_key": vm.get("image_key"),
                        "disk_type": disk_type,
                        "disk_size_gb": disk_size,
                    }
                    out.append(inst)
            except Exception as exc:
                errors.append({"account": acc["email"], "error": str(exc)})
        return {"ok": True, "instances": out, "errors": errors}
