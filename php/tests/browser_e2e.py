#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用 Playwright 驱动真实浏览器，验证「PHP 后端 + 原封不动的 console.html」能不能跑通。
这是最关键的一条验收：前端一行没改，后端换成 PHP 后必须完全可用。
"""
import json, os, re, sqlite3, subprocess, sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8099"
DB   = os.environ.get("PHP_DATA", "/tmp/apitest") + "/gcp_php.db"
# 冒烟脚本改过密码，优先用改后的
# 密码由 smoke_1_public.sh 随机生成并持久化到 /tmp/apitest_pw.env
# （仓库里不留任何明文凭据）
import os
PW = os.environ.get("TEST_PW", "")
if not PW and os.path.exists("/tmp/apitest_pw.env"):
    for line in open("/tmp/apitest_pw.env"):
        if line.startswith("TEST_PW="):
            PW = line.split("=", 1)[1].strip().strip("'\"")
if not PW:
    import re as _re
    for line in open("/tmp/apitest/INITIAL_ADMIN.txt", encoding="utf-8"):
        if line.startswith("密码"):
            PW = _re.sub(r"^密码[：:]\s*", "", line).strip()
assert PW, "拿不到测试密码：先跑 smoke_1_public.sh"
# 之前那轮冒烟改过密码，以实际库里的为准
conn = sqlite3.connect(DB)
row = conn.execute("SELECT username FROM users WHERE role='admin' LIMIT 1").fetchone()
USER = row[0]

PASS = FAIL = 0
def ok(m):
    global PASS; PASS += 1; print(f"  \033[32m✅\033[0m {m}")
def bad(m):
    global FAIL; FAIL += 1; print(f"  \033[31m❌\033[0m {m}")

def captcha_code(cid):
    kid = cid
    c = sqlite3.connect(DB)
    r = c.execute("SELECT code FROM captcha WHERE cid=?", (kid,)).fetchone()
    return r[0] if r else None

with sync_playwright() as pw:
    br = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()

    console_errors, failed_reqs = [], []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("requestfailed", lambda r: failed_reqs.append(f"{r.method} {r.url} {r.failure}"))

    print("\n═══ 1. 登录页 ═══")
    page.goto(f"{BASE}/login", wait_until="networkidle")
    title = page.title()
    ok(f"页面标题：{title}") if title else bad("标题为空")
    has_captcha_img = page.evaluate("""() => {
        const el = document.querySelector('img[src^="data:image"], .captcha-img img, img[alt*="验证码"]');
        return !!el;
    }""")
    ok("图形验证码已渲染") if has_captcha_img else bad("没找到验证码图")
    login_vue = page.evaluate("() => typeof window.Vue !== 'undefined'")
    ok(f"登录页是原生 JS（Vue 存在={login_vue}，与本页无关——控制台页才用 Vue）")

    # 取验证码答案的正确姿势：
    #   页面加载时自己已经 fetch 了一个验证码并把 captcha_id 存进闭包变量，
    #   我再独立 fetch 一个只会让「我手里的答案」和「页面的 captcha_id」错位
    #   （实测就是这么失败的：填进去的答案对应的是另一个 captcha_id → 400）。
    #   所以改为：点击验证码图触发页面自己的刷新，然后从库里取**最新一条**记录。
    page.click("#captchaImg")
    page.wait_for_timeout(800)
    c2 = sqlite3.connect(DB)
    rowc = c2.execute("SELECT cid, code FROM captcha ORDER BY created DESC LIMIT 1").fetchone()
    cid, code = (rowc[0], rowc[1]) if rowc else (None, None)
    ok(f"验证码：id={str(cid)[:12]}… 答案={code}") if code else bad("库里读不到验证码答案")

    print("\n═══ 2. 提交登录 ═══")
    page.fill('input[name="username"], #username, input[type="text"]', USER)
    page.fill('input[name="password"], #password, input[type="password"]', PW)
    # 验证码输入框：找 placeholder 或 captcha 相关
    filled = page.evaluate(f"""() => {{
        const ins = [...document.querySelectorAll('input')];
        const box = ins.find(i => /验证码|captcha/i.test(i.placeholder || '') || /captcha/i.test(i.name || i.id || ''));
        if (!box) return false;
        box.value = '{code}';
        box.dispatchEvent(new Event('input', {{bubbles:true}}));
        return true;
    }}""")
    ok("已填入验证码") if filled else bad("找不到验证码输入框")
    page.screenshot(path="/tmp/php_login_filled.png")

    page.evaluate("""() => {
        const btn = [...document.querySelectorAll('button, input[type=submit]')]
            .find(b => /登\\s*录|login/i.test(b.textContent || b.value || ''));
        if (btn) btn.click();
    }""")
    # 注意：不能用 wait_for_url(r"/(?!login)") —— "http://" 里那个 "/" 也满足
    # 「/ 后面不是 login」，会立刻返回，等于没等跳转（我第一版就是这么写错的）。
    # 用 location.pathname 判定最可靠。
    try:
        page.wait_for_function("() => location.pathname !== '/login'", timeout=10000)
        ok(f"登录成功，已跳转：{page.url}")
    except Exception:
        err = page.evaluate("() => document.body.innerText.slice(0,200)")
        bad(f"登录后仍在登录页。页面文字：{err[:160]}")

    print("\n═══ 3. 控制台（SPA）加载 ═══")
    page.wait_for_timeout(2500)
    page.screenshot(path="/tmp/php_console.png", full_page=False)

    vue_ok = page.evaluate("() => typeof window.Vue !== 'undefined'")
    ok("控制台页 Vue 本地加载成功（vendor/vue.global.prod.js）") if vue_ok else bad("控制台页 Vue 未加载 → 会白屏")
    info = page.evaluate("""() => {
        const t = document.body.innerText;
        return {
            hasVersion: /v?1\\.2\\.7/.test(t),
            navItems: [...document.querySelectorAll('.sb-nav a, nav a, aside a')].map(a=>a.textContent.trim()).filter(Boolean).slice(0,20),
            cards: document.querySelectorAll('.card, .panel, .grid > *').length,
            text: t.slice(0, 400)
        };
    }""")
    ok(f"页面含版本号") if info["hasVersion"] else bad("页面没显示版本号")
    ok(f"侧栏导航项 {len(info['navItems'])} 个：{info['navItems'][:6]}") if info["navItems"] else bad("侧栏导航为空")
    ok(f"内容区块 {info['cards']} 个") if info["cards"] > 0 else bad("页面无内容区块")

    print("\n═══ 4. 页面内逐页切换（验证 API 全部可用）═══")
    # 依次点击侧栏各项，看每次是否有失败的请求或 JS 报错
    for name in info["navItems"][:12]:
        before_err = len(console_errors)
        before_req = len(failed_reqs)
        clicked = page.evaluate(f"""() => {{
            const a = [...document.querySelectorAll('.sb-nav a, nav a, aside a')]
                .find(x => x.textContent.trim() === {json.dumps(name)});
            if (a) {{ a.click(); return true; }} return false;
        }}""")
        if not clicked:
            continue
        page.wait_for_timeout(1200)
        new_err = console_errors[before_err:]
        new_req = failed_reqs[before_req:]
        # 过滤掉 WebSocket 404：PHP 内置服务器（php -S）是单线程 HTTP 服务，
        # 无法把 /ws/logs 升级成 WS 并转发给 bin/ws-server.php —— 这由生产环境的
        # nginx 伪静态规则完成（见 php/bt/nginx-rewrite.conf）。
        # 真正要断言的是「前端的轮询降级能正常工作」，所以这里只标注不判失败，
        # 并在下面单独验证 /api/logs 轮询接口确实可用。
        ws_noise = [e for e in new_err if "/ws/logs" in e]
        real_err = [e for e in new_err if "/ws/logs" not in e]
        if ws_noise and not real_err:
            ok(f"「{name}」切换正常（仅 WS 404，属 php -S 限制，见 nginx 伪静态）")
        elif not real_err and not new_req:
            ok(f"「{name}」切换正常，无报错")
        else:
            bad(f"「{name}」有问题：JS错误={real_err[:1]} 失败请求={new_req[:1]}")

    print("\n═══ 5. 关键 API 调用（在页面上下文里真跑）═══")
    api_probe = page.evaluate("""async () => {
        const out = {};
        for (const p of ['/api/status','/api/accounts','/api/instances?sync=false','/api/tasks',
                         '/api/logs?limit=3','/api/audit?page=1&page_size=3','/api/users','/api/sessions',
                         '/api/catalog','/api/install_presets','/api/inspect/sections','/api/config']) {
            try {
                const r = await fetch(p, {credentials:'include'});
                const j = await r.json().catch(()=>null);
                out[p] = {status: r.status, keys: j ? Object.keys(j).sort().join(',') : '(非JSON)'};
            } catch (e) { out[p] = {status: 'ERR', keys: String(e)}; }
        }
        return out;
    }""")
    for path, r in api_probe.items():
        if r["status"] == 200:
            ok(f"{path:36} 200  [{r['keys'][:52]}]")
        else:
            bad(f"{path:36} {r['status']}  {r['keys'][:60]}")

    print("\n═══ 5b. WebSocket 不可用时的轮询降级链路 ═══")
    poll = page.evaluate("""async () => {
        const r1 = await fetch('/api/logs?limit=1', {credentials:'include'});
        const j1 = await r1.json();
        let lastId = 0;
        if (j1.logs && j1.logs.length) lastId = Number(j1.logs[j1.logs.length-1].id) || 0;
        const r2 = await fetch('/api/logs?since_id=' + lastId + '&limit=10', {credentials:'include'});
        const j2 = await r2.json();
        return {s1: r1.status, s2: r2.status, hasLogs: Array.isArray(j2.logs), since: lastId};
    }""")
    if poll["s1"] == 200 and poll["s2"] == 200 and poll["hasLogs"]:
        ok(f"轮询降级可用：/api/logs 与 since_id 增量拉取都 200（lastId={poll['since']}）")
    else:
        bad(f"轮询降级不可用：{poll}")

    print("\n═══ 6. 安全头与 Cookie ═══")
    cookies = ctx.cookies()
    ck = [c for c in cookies if c["name"] == "gcp_sid"]
    if ck:
        c = ck[0]
        ok(f"会话 Cookie：httpOnly={c['httpOnly']} sameSite={c['sameSite']} secure={c['secure']}")
        if not c["httpOnly"]:
            bad("Cookie 缺 HttpOnly")
    else:
        bad("没有 gcp_sid Cookie")
    hdrs = page.evaluate("""async () => {
        const r = await fetch('/api/version');
        return [...r.headers.entries()].reduce((a,[k,v])=>(a[k]=v,a),{});
    }""")
    for h in ["x-content-type-options", "x-frame-options", "content-security-policy", "referrer-policy"]:
        ok(f"响应头 {h}: {hdrs.get(h,'')[:44]}") if h in hdrs else bad(f"缺响应头 {h}")

    print("\n═══ 7. 汇总 ═══")
    real_errs = [e for e in console_errors if "favicon" not in e.lower()]
    if real_errs:
        print("  控制台错误（去重后最多 5 条）：")
        for e in sorted(set(real_errs))[:5]:
            print("   ", e[:150])
    else:
        ok("浏览器控制台全程无 JS 错误")
    if failed_reqs:
        print("  失败请求：")
        for r in sorted(set(failed_reqs))[:5]:
            print("   ", r[:150])
    else:
        ok("无失败的网络请求")

    br.close()

print(f"\n{'='*62}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*62}")
sys.exit(0 if FAIL == 0 else 1)
