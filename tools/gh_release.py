#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 core/version.py 里的版本补成 GitHub Release，并确保 /releases/latest 指向最新版。

用法：
    GITHUB_TOKEN=ghp_xxx python3 tools/gh_release.py

★ Token 只从环境变量读，**不写进代码**（本仓库是公开的）。
★ 幂等：已有 release 的版本会跳过，可以反复执行。

为什么需要它（两个踩过的坑）：

1) Release ≠ tag。`git push origin v1.2.3` 只产生 tag，
   `/releases/latest` 对只有 tag 的仓库返回 **404**。
   「检查更新」这类功能必须先有 Release 才拿得到发布说明。

2) GitHub 的「latest」是按 **created_at** 排序的，不是按版本号。
   批量补建时（新版本→旧版本顺序），**最后创建的最旧版本会成为 latest**。
   后果很坏：检查更新会拿一个很旧的版本号当「最新」，然后显示「已是最新」——
   看着完全正常，但是全错。所以每次都要 PATCH make_latest 钉回去并复核。
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import version as v  # noqa: E402

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
REPO = v.REPO_NAME


def call(method, path, payload=None):
    if not TOKEN:
        raise SystemExit("请先设置 GITHUB_TOKEN 环境变量")
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + TOKEN,
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "gcp-manager-web-release",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.getcode(), json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main():
    _, tags = call("GET", f"/repos/{REPO}/tags?per_page=100")
    tagset = {t["name"] for t in tags} if isinstance(tags, list) else set()
    _, rels = call("GET", f"/repos/{REPO}/releases?per_page=100")
    rels = rels if isinstance(rels, list) else []
    have = {r["tag_name"] for r in rels}

    newest = v.CHANGELOG[0]["version"]
    created = 0
    for e in v.CHANGELOG:                       # 新 → 旧
        ver = e["version"]
        tag = "v" + ver
        if tag not in tagset:
            print(f"  - {tag}  无此 tag，跳过")
            continue
        if tag in have:
            continue
        body = "\n".join("- " + n for n in e.get("notes", []))
        payload = {
            "tag_name": tag,
            "name": f"{tag}（{e.get('date', '')}）",
            "body": f"## {v.APP_NAME_CN} {tag}\n\n**发布 {e.get('date', '')}**\n\n{body}\n\n"
                    f"部署与升级见仓库 README；已装版本可在控制台"
                    f"「个人设置 → 关于 → 检查更新」里核对。",
            "draft": False, "prerelease": False,
        }
        if ver == newest:
            payload["make_latest"] = "true"
        code, res = call("POST", f"/repos/{REPO}/releases", payload)
        if code in (200, 201):
            print(f"  ✅ {tag}  {res.get('html_url', '')}")
            created += 1
        else:
            print(f"  ❌ {tag}  HTTP {code}: {str(res.get('message'))[:80]}")
    print(f"新建 {created} 个 release")

    # ★ 必须钉 latest：批量补建会让「最后创建的」变成 latest（见模块注释）
    _, rels = call("GET", f"/repos/{REPO}/releases?per_page=100")
    want = "v" + newest
    for r in (rels if isinstance(rels, list) else []):
        if r["tag_name"] == want:
            call("PATCH", f"/repos/{REPO}/releases/{r['id']}", {"make_latest": "true"})
    for r in (rels if isinstance(rels, list) else []):
        if r["tag_name"] != want:
            call("PATCH", f"/repos/{REPO}/releases/{r['id']}", {"make_latest": "false"})

    code, cur = call("GET", f"/repos/{REPO}/releases/latest")
    ok = cur.get("tag_name") == want
    print(f"\n/releases/latest → HTTP {code}  tag={cur.get('tag_name')}")
    print("  ", "✅ 正确" if ok else f"❌ 应为 {want}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
