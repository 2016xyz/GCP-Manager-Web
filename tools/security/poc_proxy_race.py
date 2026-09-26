#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PoC：代理环境变量的跨线程污染（F2）

修复前的实测结果（W2 的 V2 证据）：
    线程B 仍在自己 with 块内时 HTTPS_PROXY = None      ← B 的代理被 A 的 __exit__ 清掉了
    两线程都退出后 HTTPS_PROXY = http://proxyA:1        ← 永久残留，后续所有调用都走 A 的代理

后果：
  · B 的账号调用变直连（代理隔离被打破，服务端真实 IP 暴露）
  · 无代理账号的调用走 A 的代理 → 它的 OAuth 凭据被送到**别的账号**的第三方代理上

修复后应满足：
  ① 任一时刻，正在 with 块内的线程看到的代理必须是**自己的**
  ② 全部退出后环境变量回到初始状态（不留残留）
  ③ 线程内嵌套（同线程重复进入）不死锁
"""
import os
import sys
import threading
import time

BASE = "/root/.hermes/profiles/2/workspace/gcp-manager-web"
sys.path.insert(0, BASE)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("https_proxy", None)
os.environ.pop("http_proxy", None)

from core.gcp import ProxyEnvContext, _note_proxy_account   # noqa: E402

# 模拟「进程里存在带代理的账号」——这是需要串行化的前提
_note_proxy_account(1)

OBSERVED = {}
errors = []
barrier = threading.Barrier(2, timeout=10)


def worker(name, proxy, hold, out):
    try:
        with ProxyEnvContext(proxy):
            # 让两个线程都进入 with 块之后再观察（确保交错）
            try:
                barrier.wait()
            except Exception:
                pass
            time.sleep(hold)
            seen = os.environ.get("HTTPS_PROXY")
            out[name] = seen
            if seen != proxy:
                errors.append(f"{name} 在自己 with 内看到 {seen!r}，期望 {proxy!r}")
    except Exception as e:
        errors.append(f"{name} 异常：{e!r}")


def case1():
    """两个线程各用不同代理，交错进入/退出"""
    OBSERVED.clear()
    errors.clear()
    a = threading.Thread(target=worker, args=("A", "http://proxyA:1", 0.6, OBSERVED))
    b = threading.Thread(target=worker, args=("B", "http://proxyB:2", 0.2, OBSERVED))
    a.start()
    # 让 A 先进 with，再起 B
    time.sleep(0.15)
    b.start()
    a.join(10)
    b.join(10)
    print("  A 观察到的代理:", OBSERVED.get("A"))
    print("  B 观察到的代理:", OBSERVED.get("B"))
    print("  全部退出后 HTTPS_PROXY =", repr(os.environ.get("HTTPS_PROXY")))
    return not errors and os.environ.get("HTTPS_PROXY") is None


def case2():
    """无代理调用不得被别的线程留下的代理带走"""
    errors.clear()
    OBSERVED.clear()
    got = {}

    def with_proxy():
        with ProxyEnvContext("http://proxyA:1"):
            time.sleep(0.5)

    def without_proxy():
        time.sleep(0.1)
        with ProxyEnvContext(""):
            got["none"] = os.environ.get("HTTPS_PROXY")

    t1 = threading.Thread(target=with_proxy)
    t2 = threading.Thread(target=without_proxy)
    t1.start(); t2.start(); t1.join(10); t2.join(10)
    print("  无代理线程在 with 内看到 HTTPS_PROXY =", repr(got.get("none")))
    return got.get("none") is None


def case3():
    """同线程嵌套（重入）不得死锁"""
    try:
        with ProxyEnvContext("http://p1:1"):
            with ProxyEnvContext("http://p2:2"):
                inner = os.environ.get("HTTPS_PROXY")
            outer = os.environ.get("HTTPS_PROXY")
        print(f"  嵌套：内层={inner!r} 外层恢复={outer!r} 退出后={os.environ.get('HTTPS_PROXY')!r}")
        return inner == "http://p2:2" and outer == "http://p1:1" and os.environ.get("HTTPS_PROXY") is None
    except Exception as e:
        print(f"  ❌ 嵌套异常（可能死锁）：{e!r}")
        return False


print("═══ 用例 1：两个线程各用不同代理、交错进入退出 ═══")
r1 = case1()
print("\n═══ 用例 2：无代理调用不得被别的线程的代理带走 ═══")
r2 = case2()
print("\n═══ 用例 3：同线程嵌套不重入死锁 ═══")
r3 = case3()

print("\n═══ 判定 ═══")
for n, ok in (("交错不互相污染", r1), ("无代理不被带走", r2), ("嵌套不死锁", r3)):
    print(f"  {'✅' if ok else '❌'} {n}")
if errors:
    for e in errors:
        print("    !", e)
sys.exit(0 if (r1 and r2 and r3) else 3)
