#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC：三条低危项的加固验证

① 账号枚举：禁用账号的返回必须与「密码错」**文案一致、耗时可比**。
   修复前：禁用 → 直接 return「该账号已被禁用」（不哈希、文案不同）
   → 可以枚举出「存在且被禁用」的用户名，且时间差明显。
② 角色变更吊销会话：把某人降级后，他的旧会话必须立刻失效。
   修复前：会话里冻结了登录时的 role，降级不生效直到重新登录。
③ 开机脚本不把密码写进日志：`set -x` 会回显 `root:<明文>` 到 stderr，
   而 stderr 被重定向进 /root/gcp_root_mode.log。修复后该行被 set +x 包住。

用法：python3 tools/security/poc_low_hardening.py
"""
import os
import shutil
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
DATA = "/tmp/poclow"
os.environ["GCPWEB_DATA_DIR"] = DATA

from core import ssh as ssh_mod            # noqa: E402
from core.store import Store               # noqa: E402
from core.users import UserStore           # noqa: E402

PASS = FAIL = 0


def ok(m):
    global PASS
    PASS += 1
    print(f"  ✅ {m}")


def bad(m):
    global FAIL
    FAIL += 1
    print(f"  ❌ {m}")


def main():
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)
    st = Store(os.path.join(DATA, "gcp_web.db"))
    us = UserStore(st)

    print("═══ ① 账号枚举：禁用账号 vs 密码错 vs 用户不存在 ═══")
    us.create_user("alice", "AlicePw#12345678", "operator", "A", must_change=False)
    us.create_user("banned", "BannedPw#12345678", "operator", "B", must_change=False)
    us.update_user(us.get_user(username="banned")["id"], disabled=1)

    cases = [
        ("已禁用账号 + 正确密码", "banned", "BannedPw#12345678"),
        ("已禁用账号 + 错误密码", "banned", "wrong"),
        ("正常账号 + 错误密码",   "alice",  "wrong"),
        ("不存在的用户名",        "ghost",  "whatever"),
    ]
    msgs, times = set(), []
    for label, u, p in cases:
        t0 = time.perf_counter()
        r, why = us.verify_login(u, p)
        el = (time.perf_counter() - t0) * 1000
        times.append((label, el))
        msgs.add(why)
        print(f"    {label:22s} → {why}   ({el:.0f} ms)")
    # 文案必须完全一致
    ok("四种情况的文案完全一致（无法据此区分账号是否存在/被禁用）") if len(msgs) == 1 \
        else bad(f"文案不一致：{msgs}")
    # 耗时：禁用分支不能明显快于其它分支
    t_banned = dict(times)["已禁用账号 + 正确密码"]
    t_wrong = dict(times)["正常账号 + 错误密码"]
    delta = abs(t_banned - t_wrong)
    print(f"    禁用分支 {t_banned:.0f}ms vs 密码错分支 {t_wrong:.0f}ms，差 {delta:.0f}ms")
    ok("禁用分支同样走了哈希（时间差 < 60ms）") if delta < 60 else \
        bad(f"时间差过大（{delta:.0f}ms）→ 仍可枚举")

    print("\n═══ ② 角色变更吊销会话 ═══")
    alice_id = us.get_user(username="alice")["id"]
    tok = us.create_session(us.get_user(username="alice"), "127.0.0.1", "poc")[0]
    n_before = len([s for s in us.list_sessions() if s["username"] == "alice"])
    us.revoke_user_sessions(alice_id)          # 这是 API 层改角色时调用的
    n_after = len([s for s in us.list_sessions() if s["username"] == "alice"])
    print(f"    吊销前 alice 会话 {n_before} 条 → 吊销后 {n_after} 条")
    ok("降级会带走旧会话") if (n_before >= 1 and n_after == 0) else bad("旧会话没被清掉")
    # 并确认 API 层真的在改角色时调用了它
    app = open(os.path.join(BASE, "app.py"), encoding="utf-8").read()
    seg = app[app.find('kw["role"] = req.role'):][:400]
    ok("API 层改角色后确实调用了 revoke_user_sessions") if "revoke_user_sessions" in seg \
        else bad("API 层改角色后没有吊销会话")
    php = open(os.path.join(BASE, "php/src/ApiAuth.php"), encoding="utf-8").read()
    ok("PHP 侧改角色后也确实吊销了会话") if "array_key_exists('role', $kw)" in php \
        and "revokeUserSessions($uid)" in php else bad("PHP 侧没有吊销")

    print("\n═══ ③ 开机脚本不把密码写进日志 ═══")
    SECRET = "TraceLeakCheck#7788"
    for label, script in (
        ("Python", ssh_mod.build_root_startup_script(SECRET)),
        ("PHP",    None),
    ):
        if script is None:
            import subprocess
            script = subprocess.run(
                ["php", "-r", 'require "php/src/bootstrap.php"; echo Ssh::buildRootStartupScript($argv[1]);', SECRET],
                cwd=BASE, capture_output=True, text=True).stdout
        lines = script.split("\n")
        idx = [i for i, l in enumerate(lines) if "chpasswd" in l]
        if not idx:
            bad(f"{label}：脚本里找不到 chpasswd 行")
            continue
        i = idx[0]
        guarded = (i > 0 and lines[i - 1].strip() == "set +x"
                   and i + 1 < len(lines) and lines[i + 1].strip() == "set -x")
        print(f"    {label}: 密码行 = {lines[i].strip()!r}")
        print(f"           前一行 = {lines[i-1].strip()!r} / 后一行 = {lines[i+1].strip()!r}")
        ok(f"{label}：密码行被 set +x / set -x 包住（trace 不会回显它）") if guarded \
            else bad(f"{label}：密码行没有被关掉 trace")
        ok(f"{label}：日志文件设了 600") if "chmod 600" in script else bad(f"{label}：日志未设 600")

    print("\n═══ 判定 ═══")
    print(f"  通过 {PASS} 项，失败 {FAIL} 项")
    shutil.rmtree(DATA, ignore_errors=True)
    return 0 if FAIL == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
