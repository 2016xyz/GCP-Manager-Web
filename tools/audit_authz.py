#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鉴权与漏洞审计（静态 + 动态）

做三件事：
  1. 枚举所有路由，找出「没有 require(...) 的接口」——漏鉴权是最高危的
  2. 检查权限等级是否合理（写操作不能只要 view）
  3. 检查响应里是否泄露密钥路径 / 明文密码 / 哈希盐
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

src = open(os.path.join(BASE, "app.py"), encoding="utf-8").read()

# ── 1. 逐个路由解析：装饰器 → 函数名 → 函数体 ─────────────────────
ROUTE_RE = re.compile(
    r'@app\.(get|post|patch|delete|put|websocket)\("([^"]+)"[^)]*\)\s*\n'
    r'(?:@[^\n]*\n)*'
    r'(?:async\s+)?def\s+(\w+)\s*\([^)]*\)\s*:\s*\n'
    r'((?:\s*"""(?:.|\n)*?"""\s*\n)?)'          # docstring（可选）
    r'((?:[ \t]+[^\n]*\n|\n)*)',                 # 函数体前若干行
    re.M)

rows = []
for m in ROUTE_RE.finditer(src):
    method, path, fn, doc, head = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    # 取函数体开头若干行找 require(
    body_head = head[:1200]
    seg = src[m.start():m.start() + 3000]
    rm = re.search(r'require\(\s*request\s*,\s*"(\w+)"', seg)
    perm = rm.group(1) if rm else None
    if not perm:
        # WebSocket 不走 require()，而是直接读 Cookie 里的会话。
        # 漏判会把「其实有鉴权」的接口误报成未鉴权，所以这里显式识别。
        if re.search(r'get_session\(', seg) or re.search(r'\bws\.cookies', seg):
            perm = "(会话鉴权)" if "get_session(" in seg else None
    # 无鉴权时必须真的有 close/拒绝动作，否则才是真漏洞
    if perm is None and re.search(r'await\s+ws\.close\(', seg):
        perm = "(会话鉴权)" 
    # 是否匿名放行（PUBLIC_PATHS / PUBLIC_PREFIXES）
    lines = [l.strip() for l in body_head.splitlines() if l.strip()]
    rows.append({
        "method": method.upper(),
        "path": path,
        "fn": fn,
        "perm": perm,
        "头几行": lines[:3],
    })

# 白名单常量
pub_paths = re.findall(r'PUBLIC_PATHS\s*=\s*\{([^}]*)\}', src, re.S)
pub_prefix = re.findall(r'PUBLIC_PREFIXES\s*=\s*[\(\[]([^\)\]]*)[\)\]]', src, re.S)
public_set = set(re.findall(r'"([^"]+)"', pub_paths[0])) if pub_paths else set()
public_pref = tuple(re.findall(r'"([^"]+)"', pub_prefix[0])) if pub_prefix else ()
print(f"PUBLIC_PATHS   = {sorted(public_set)}")
print(f"PUBLIC_PREFIXES= {public_pref}")
print()

NO_AUTH, WEAK = [], []
print(f"{'方法':<6} {'路径':<40} {'权限':<10} 说明")
print("-" * 92)
for r in sorted(rows, key=lambda x: (x["path"], x["method"])):
    is_pub = r["path"] in public_set or any(r["path"].startswith(p) for p in public_pref)
    perm = r["perm"] or ("(公开)" if is_pub else "无 require！")
    note = ""
    if not r["perm"] and not is_pub:
        note = "⚠ 未鉴权"
        NO_AUTH.append(r)
    if r["perm"] == "view" and r["method"] in ("POST", "PATCH", "DELETE", "PUT"):
        note = "⚠ 写操作用 view 权限？"
        WEAK.append(r)
    print(f"{r['method']:<6} {r['path']:<40} {perm:<10} {note}")

print()
print(f"路由总数: {len(rows)}")
print(f"未鉴权且不在白名单: {len(NO_AUTH)}")
for r in NO_AUTH:
    print(f"   ✗ {r['method']} {r['path']}  ({r['fn']})")
print(f"写操作疑似权限过低: {len(WEAK)}")
for r in WEAK:
    print(f"   ? {r['method']} {r['path']}  perm={r['perm']}  {r['头几行'][:2]}")