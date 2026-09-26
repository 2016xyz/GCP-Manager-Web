#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC：POST /api/execute 的 all 语义（F3）

漏洞（修复前）：all 默认 True + `if all_instances or not target_list:`，
  于是「给了 targets 但没写 all」会把该账号下**全部**实例一并执行。
  前端显式传 all 所以 UI 看不出问题，但 curl / 脚本 / PHP 版会误伤生产机。

做法：桩掉 account_service / run_ssh_command，只观察「实际被执行的实例 IP 集合」。
"""
import os
import sys
import threading
import time

BASE = "/root/.hermes/profiles/2/workspace/gcp-manager-web"
sys.path.insert(0, BASE)
DATA = "/tmp/pocf3"
os.environ["GCPWEB_DATA_DIR"] = DATA
os.makedirs(DATA, exist_ok=True)

from core import tasks as ct          # noqa: E402
from core import ssh as ssh_mod       # noqa: E402
from core.store import Store          # noqa: E402

EXECUTED = []
_lock = threading.Lock()


class FakeGcp:
    def list_instances(self):
        return [{"name": "t1", "ip": "10.0.0.1", "zone": "z"},
                {"name": "t2", "ip": "10.0.0.2", "zone": "z"},
                {"name": "t3", "ip": "10.0.0.3", "zone": "z"}]


def fake_run_ssh(ip, user, pwd, command, **kw):
    with _lock:
        EXECUTED.append(ip)
    return True, "ok"


def new_env():
    import importlib
    importlib.reload(ct)
    importlib.reload(ssh_mod)
    st = Store(os.path.join(DATA, "gcp_web.db"))
    # 清空并写入 1 个账号 + 3 台实例（带 IP，才能被执行）
    st.conn.execute("DELETE FROM accounts")
    st.conn.execute("DELETE FROM vm_passwords")
    st.conn.commit()
    acc_id = st.add_account("a@b.c", "proj", "/tmp/k.json")
    for n, ip in (("t1", "10.0.0.1"), ("t2", "10.0.0.2"), ("t3", "10.0.0.3")):
        st.save_vm(n, ip, "pw", acc_id, "z", "e2", "k", "pd", 10)
    tm = ct.TaskManager(st)
    tm.account_service = lambda acc: FakeGcp()
    ssh_mod.run_ssh_command = fake_run_ssh
    tm._ssh_stopper = None
    return tm


def run_case(name, payload, expect):
    with _lock:
        EXECUTED.clear()
    tm = new_env()
    r = tm.submit_execute(payload)
    if not r.get("ok"):
        print(f"  {name:44s} submit 失败: {r}")
        return None
    tid = r["task_id"]
    for _ in range(60):          # 等后台线程跑完
        t = tm.store.get_task(tid) or {}
        if t.get("status") in ("done", "failed", "cancelled"):
            break
        time.sleep(0.2)
    got = sorted(set(EXECUTED))
    mark = "✅" if got == sorted(expect) else "❌"
    print(f"  {mark} {name:44s} 实际执行={got}  期望={sorted(expect)}")
    return got == sorted(expect)


def main():
    print("═══ 桩测试：POST /api/execute 的 all 语义 ═══")
    print("  （账号下有 3 台实例：t1=10.0.0.1  t2=10.0.0.2  t3=10.0.0.3）\n")
    results = []
    # 1) 给了 targets、不传 all —— 修复后只打 t1（修复前会打全部 3 台）
    results.append(run_case('{"command":"x","targets":["t1"]}  不传 all',
                            {"command": "x", "targets": ["t1"]},
                            ["10.0.0.1"]))
    # 2) 给了 targets、显式 all=false —— 只打 t1
    results.append(run_case('{"command":"x","targets":["t1"],"all":false}',
                            {"command": "x", "targets": ["t1"], "all": False},
                            ["10.0.0.1"]))
    # 3) 给了 targets、显式 all=true —— 明确要求扩散，打全部
    results.append(run_case('{"command":"x","targets":["t1"],"all":true}',
                            {"command": "x", "targets": ["t1"], "all": True},
                            ["10.0.0.1", "10.0.0.2", "10.0.0.3"]))
    # 4) 不给 targets、不传 all —— 打全部（保持原有便利语义）
    results.append(run_case('{"command":"x"}  不给 targets',
                            {"command": "x"},
                            ["10.0.0.1", "10.0.0.2", "10.0.0.3"]))
    print("\n═══ 判定 ═══")
    if all(x for x in results if x is not None):
        print("  ✅ 三态语义正确：给了 targets 就只打 targets；要打全部必须显式 all=true")
        return 0
    print("  ❌ 有场景不符合预期（见上）")
    return 3


if __name__ == "__main__":
    sys.exit(main())
