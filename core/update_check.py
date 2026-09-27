# -*- coding: utf-8 -*-
"""
Update —— 检查有没有新版本（走 GitHub API）

背景：控制台的「关于」卡片原本只显示**本地**的 changelog，
没有任何东西去比对远端 —— 它永远只显示"当前版本的一些说明"，
用户根本无从知道有没有新版。这里补上"去问一次 GitHub"。

★ 设计取舍（与 php/src/Update.php 完全一致，两版对同一情况说同样的话）

1) 两段式取数：先 `/releases/latest`（正式 Release，带发布说明）；
   404 说明仓库只有 tag 没建过 Release，就退回 `/tags` 取最高版本。
   两条路都走不通才算失败。

2) 一定要缓存。GitHub 未认证调用是**每 IP 每小时 60 次**，
   而这是个用户随手会点的按钮。默认缓存 1 小时，force 才穿透。

3) 失败时不抛异常、不返回空：把原因说清楚，并且手上如果有旧缓存，
   把旧结果一起带上（标明是旧的）—— 比只说一句"失败"有用。

4) 只做「检查」，不做「自动升级」。升级要动部署目录里的文件，
   那不是点个按钮就该干的事；这里只回答"有没有新版"和"怎么升"。
"""
import json
import re
import time
import urllib.error
import urllib.request

from . import version as version_mod

# 结果缓存多久（秒）。GitHub 未认证限流 60 次/小时，缓存是必须的
TTL = 3600

_CACHE_KEY = "update_check"
_UA = "GCP-Manager-Web-update-check"
_API = "https://api.github.com"


def norm(s):
    """去掉 tag 名里的 v 前缀、去空白"""
    s = (s or "").strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    return s.strip()


def _vkey(s):
    """把版本号变成可比较的元组。

    ("1.4.8", 1) vs ("1.4.8-rc1", 0) —— 正式版大于同号预发布版。
    非数字段当 0 处理，段数不足按 3 段补齐（1.5 == 1.5.0）。
    """
    s = norm(s)
    main, _, pre = s.partition("-")
    parts = []
    for p in main.split("."):
        parts.append(int(p) if p.isdigit() else 0)
    while len(parts) < 3:
        parts.append(0)
    return (tuple(parts), 0 if pre else 1)


def newer(a, b):
    """a 是否比 b 新"""
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    return _vkey(a) > _vkey(b)


def behind():
    """本地 changelog 里比当前版本新的条目（保持新→旧顺序）"""
    cur = version_mod.VERSION
    return [e for e in version_mod.CHANGELOG if newer(e.get("version", ""), cur)]


def _releases_url():
    return version_mod.REPO_URL + "/releases"


def _http(url):
    """发一个 GET，返回 (code, body, err, head)"""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,                                  # GitHub 强制要求 UA
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return (r.getcode(), r.read().decode("utf-8", "replace"), "",
                    str(r.headers).lower())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return (e.code, body, "", str(e.headers).lower() if e.headers else "")
    except Exception as e:                                   # 超时 / DNS / 拒绝等
        return (0, "", "%s: %s" % (type(e).__name__, e), "")


def _fetch():
    repo = version_mod.REPO_NAME                              # owner/name
    now = int(time.time())
    base = {
        "current": version_mod.VERSION,
        "repo": repo,
        "checked_at": now,
        "cached": False,
        "upgrade_hint": "bash update.sh",
    }

    # ① 正式 Release（带发布说明）
    code, body, err, head = _http("%s/repos/%s/releases/latest" % (_API, repo))
    if code == 200:
        try:
            d = json.loads(body)
        except Exception:
            d = None
        if isinstance(d, dict) and d.get("tag_name"):
            latest = norm(d["tag_name"])
            out = dict(base)
            out.update({
                "ok": True,
                "latest": latest,
                "has_update": newer(latest, version_mod.VERSION),
                "source": "releases",
                "release_name": d.get("name") or d.get("tag_name") or "",
                "release_url": d.get("html_url") or _releases_url(),
                "published_at": d.get("published_at") or "",
                "notes": (d.get("body") or "").strip(),
                "release_url_all": _releases_url(),
            })
            return out
    # 403 + 限流要单独说，不然用户以为是网络坏了
    if code == 403 and "x-ratelimit-remaining: 0" in head:
        out = dict(base)
        out.update({
            "ok": False,
            "reason": "GitHub API 调用次数已用完（未认证调用每 IP 每小时 60 次）",
            "detail": "GitHub API 调用次数已用完（未认证调用每 IP 每小时 60 次）",
            "hint": "等一小时再试，或直接打开仓库的 Releases 页看。",
            "release_url": _releases_url(),
        })
        return out

    # ② 退回 tag 列表（仓库只打 tag、没建 Release 时走这里）
    code2, body2, err2, _h2 = _http("%s/repos/%s/tags?per_page=100" % (_API, repo))
    if code2 == 200:
        try:
            tags = json.loads(body2)
        except Exception:
            tags = None
        if isinstance(tags, list) and tags:
            best = ""
            for t in tags:
                n = norm((t or {}).get("name", ""))
                if n and (not best or newer(n, best)):
                    best = n
            if best:
                out = dict(base)
                out.update({
                    "ok": True,
                    "latest": best,
                    "has_update": newer(best, version_mod.VERSION),
                    "source": "tags",
                    "release_name": "v" + best,
                    "release_url": version_mod.REPO_URL + "/releases/tag/v" + best,
                    "published_at": "",
                    # tags 接口没有发布说明 —— 如实留空，不编
                    "notes": "",
                    "notes_note": "这个仓库没有建 GitHub Release，只能从 tag 读出最新版本号，"
                                  "因此拿不到发布说明。",
                    "release_url_all": _releases_url(),
                })
                return out

    # ③ 都失败：把两条路各自的失败原因都带上，便于定位
    if err:
        why = "无法连接 GitHub API：%s" % err
    elif code == 404:
        why = "GitHub 上找不到这个仓库（404）—— 仓库可能改名、转私有，或 REPO_NAME 配错了"
    else:
        why = "GitHub API 返回意外状态：HTTP %s" % code
    out = dict(base)
    out.update({
        "ok": False,
        "reason": why,
        "detail": why,
        "hint": "服务器可能无法直连 GitHub API。可以直接打开仓库的 Releases 页手动确认。",
        "tried": {
            "releases/latest": "HTTP %s%s" % (code, (" / " + err) if err else ""),
            "tags": "HTTP %s%s" % (code2, (" / " + err2) if err2 else ""),
        },
        "release_url": _releases_url(),
    })
    return out


def _load_cache(st):
    """读缓存。st 是 Store 实例（由调用方注入 —— 本模块不 import app，
    否则会形成循环依赖）。没传就跳缓存，功能不受影响。"""
    if st is None:
        return None
    try:
        v = st.get_setting(_CACHE_KEY, None)
    except Exception:
        return None
    return v if isinstance(v, dict) and v.get("ok") else None


def _save_cache(st, res):
    if st is None:
        return
    try:
        st.set_setting(_CACHE_KEY, res)
    except Exception:
        pass            # 缓存写失败不该让"检查更新"失败


def check(force=False, st=None):
    """检查更新。

    force=True 时忽略缓存，真去问 GitHub。
    st 传入 Store 实例则启用结果缓存（必须传 —— GitHub 未认证限流 60 次/小时）。
    """
    now = int(time.time())
    cached = _load_cache(st)

    if not force and cached is not None and (now - int(cached.get("checked_at") or 0)) < TTL:
        out = dict(cached)
        out["cached"] = True
        out["age_sec"] = now - int(cached.get("checked_at") or 0)
        return out

    res = _fetch()

    if res.get("ok"):
        # 顺便把「本地 changelog 里比当前版本新的条目」一起给出：
        # 用户想知道的不只是"有新版本"，还有"新在哪"
        res["behind"] = behind()
        res["behind_count"] = len(res["behind"])
        _save_cache(st, res)
        return res

    # 失败：有旧缓存就把旧结果一并带上（标明是旧的），别只丢一句"失败"
    if cached is not None:
        res["stale"] = cached
        res["stale_age_sec"] = now - int(cached.get("checked_at") or 0)
    return res
