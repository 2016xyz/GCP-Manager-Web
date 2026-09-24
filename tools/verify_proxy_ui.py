#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器实测本轮两处改动：
  ① 创建页「目标账号」只显示 备注 + 已有机器数
  ② 账号管理页：已导入账号可改代理（含清空成直连）

走真模板 + 真点击 + 真接口（夹具 8001）。
登录与 vm 取法沿用 fe_verify.py 里已验证过的写法。
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/proxy_shots"
os.makedirs(OUT, exist_ok=True)

sys.path.insert(0, "tools")
from _fixtures import ADMIN_USER, admin_password, FAKE_ACCOUNTS  # noqa: E402

LOGIN = "http://127.0.0.1:8001/login"
CONSOLE = "http://127.0.0.1:8001/"
PW = admin_password()

VM = "document.querySelector('#app').__vue_app__._container._vnode.component.proxy"
fails = []


def J(js):
    """把 JS 里的 __VM__ 占位符换成真实取法（不用 f-string，避开花括号转义）"""
    return js.replace("__VM__", VM)


def ck(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(name)


with sync_playwright() as p:
    br = p.chromium.launch(executable_path="/usr/bin/chromium-browser",
                           args=["--no-sandbox", "--disable-dev-shm-usage"])
    pg = br.new_page(viewport={"width": 1440, "height": 900})
    errs, bad = [], []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("response", lambda r: bad.append(f"{r.status} {r.url}")
          if r.status >= 400 and "/__probe/" not in r.url else None)

    # ── 登录（沿用 fe_verify 已验证的方式）─────────────────
    pg.goto(LOGIN, wait_until="domcontentloaded")
    pg.wait_for_function("typeof refreshCaptcha === 'function'")
    code = pg.evaluate("""async () => {
        await refreshCaptcha();
        const r = await fetch('/__probe/captcha/' + encodeURIComponent(captchaId));
        const j = await r.json();
        return j.code;
    }""")
    pg.fill("#username", ADMIN_USER)
    pg.fill("#password", PW)
    pg.fill("#captcha", code)
    pg.evaluate("login()")
    pg.wait_for_url(f"{CONSOLE}*", timeout=20000)
    pg.wait_for_selector("#app .nav-i", timeout=15000)
    pg.wait_for_timeout(900)
    print("已登录控制台")

    # ══════════════════════════════════════════════════════
    # ① 创建页目标账号表
    # ══════════════════════════════════════════════════════
    print("\n【创建页 · 目标账号】")
    pg.evaluate(J("() => { __VM__.go('create'); }"))
    pg.wait_for_timeout(1200)
    pg.evaluate(J("(data) => { const v = __VM__; v.accounts = data; v.checkedAccountIds = []; }"),
                json.loads(json.dumps(FAKE_ACCOUNTS, ensure_ascii=False)))
    pg.wait_for_timeout(500)
    # 真接口查一次机器数
    pg.evaluate(J("() => { __VM__.loadAccountCounts(true); }"))
    pg.wait_for_timeout(7000)

    r = pg.evaluate("""() => {
        const t = [...document.querySelectorAll('table.resp')]
            .find(x => x.querySelector('input[type=checkbox]'));
        if (!t) return {找不到: true};
        const rows = [...t.querySelectorAll('tbody tr')];
        return {
            表头: [...t.querySelectorAll('thead th')].map(x => x.textContent.trim()).filter(x => x),
            行数: rows.length,
            机器数列: rows.map(tr => {
                const tds = [...tr.querySelectorAll('td')];
                return (tds[2] || {}).textContent.replace(/\\s+/g, ' ').trim();
            }),
            备注列: rows.map(tr => {
                const tds = [...tr.querySelectorAll('td')];
                return (tds[1] || {}).textContent.replace(/\\s+/g, ' ').trim();
            }),
        };
    }""")
    print("   表头:", " | ".join(r.get("表头", [])))
    print("   备注列:", r.get("备注列"))
    print("   机器数列:", r.get("机器数列"))
    ck("目标账号表只剩 2 个数据列", r.get("表头") == ["备注", "已有机器"], str(r.get("表头")))
    ck("不再有「邮箱/Project/代理/密钥文件」列",
       not any(x in r.get("表头", []) for x in ("邮箱", "Project", "代理", "密钥文件")),
       str(r.get("表头")))
    ck("备注列优先显示备注", r.get("备注列") and "主力账号" in r["备注列"][0],
       str(r.get("备注列")))
    ck("机器数列显示「N 台」",
       any("台" in x for x in r.get("机器数列", [])), str(r.get("机器数列")))
    ck("无备注账号回退显示邮箱",
       any("@" in x for x in r.get("备注列", [])), str(r.get("备注列")))
    pg.screenshot(path=f"{OUT}/创建页-目标账号精简.png", full_page=True)

    # 逐个点击并重新查询元素：Vue 重渲染会让上一次拿到的 DOM 引用失效，
    # 一次拿到两个引用再连点，第二次会点在已脱离文档的节点上（测试坑，非产品缺陷）
    n = pg.evaluate(J("""() => {
        const v = __VM__;
        const cbs = () => [...document.querySelectorAll('table.resp input[type=checkbox]')];
        cbs()[0].click();
        return new Promise(res => setTimeout(() => {
            cbs()[1].click();
            setTimeout(() => res(v.checkedAccountIds.length), 120);
        }, 120));
    }"""))
    ck("勾选仍能联动 checkedAccountIds（全选 2 个）", n == 2, str(n))
    n2 = pg.evaluate(J("() => __VM__.checkedAccountIds.slice().sort().join(',')"))
    print("   checkedAccountIds =", n2)

    # ══════════════════════════════════════════════════════
    # ② 账号管理页 · 改代理
    # ══════════════════════════════════════════════════════
    print("\n【账号管理 · 改代理】")
    pg.evaluate(J("() => { __VM__.go('accounts'); }"))
    pg.wait_for_timeout(2500)
    pg.evaluate(J("(data) => { const v = __VM__; v.accounts = data; }"),
                json.loads(json.dumps(FAKE_ACCOUNTS, ensure_ascii=False)))
    pg.wait_for_timeout(700)

    r = pg.evaluate("""() => ({
        账号数: document.querySelectorAll('table.resp tbody tr').length,
        设代理按钮数: [...document.querySelectorAll('button')].filter(b => /设代理|改代理/.test(b.textContent)).length,
        代理列: [...document.querySelectorAll('table.resp tbody tr')].map(tr => {
            const tds = [...tr.querySelectorAll('td')];
            return (tds[4] || {}).textContent.replace(/\\s+/g, ' ').trim();
        }),
    })""")
    print("   账号数:", r["账号数"], " 设代理按钮:", r["设代理按钮数"])
    print("   代理列:", r["代理列"])
    ck("每行都有「设代理/改代理」入口", r["设代理按钮数"] >= r["账号数"], str(r["设代理按钮数"]))
    ck("无代理账号显示「直连」", any("直连" in x for x in r["代理列"]), str(r["代理列"]))
    ck("有代理账号显示协议徽章（SOCKS5H）", any("SOCKS5H" in x for x in r["代理列"]), str(r["代理列"]))

    # 点开编辑框
    pg.evaluate("""() => {
        [...document.querySelectorAll('button')].find(b => /设代理|改代理/.test(b.textContent)).click();
    }""")
    pg.wait_for_timeout(600)
    r = pg.evaluate("""() => ({
        出现输入框: !!document.querySelector('.proxy-edit input'),
        输入框值为空: (document.querySelector('.proxy-edit input') || {}).value === '',
        有协议下拉: !!document.querySelector('.proxy-edit select'),
        下拉选项: [...document.querySelectorAll('.proxy-edit select option')].map(o => o.textContent.trim()),
        有保存按钮: !!([...document.querySelectorAll('.proxy-edit button')].find(b => b.textContent.includes('保存'))),
    })""")
    print("   协议下拉选项:", r.get("下拉选项"))
    ck("点开出现代理输入框", r["出现输入框"])
    ck("输入框初始为空（不回填打码值）", r["输入框值为空"],
       "否则保存时会把 *** 当密码存进去")
    ck("有协议下拉", r["有协议下拉"])
    ck("下拉含 SOCKS5（代理端解析 DNS）",
       any("SOCKS5" in x for x in r.get("下拉选项", [])), str(r.get("下拉选项")))
    ck("有保存按钮", r["有保存按钮"])

    # 非法值 → 应报错且不落库、不退出编辑态
    pg.fill(".proxy-edit input", "socks9://1.2.3.4:1080")
    pg.evaluate("""() => {
        [...document.querySelectorAll('.proxy-edit button')].find(b => b.textContent.includes('保存')).click();
    }""")
    pg.wait_for_timeout(2000)
    r = pg.evaluate(J("""() => ({
        还在编辑态: !!document.querySelector('.proxy-edit input'),
        toasts: __VM__.toasts.map(t => t.text),
        dom提示: [...document.querySelectorAll('.toasts .t')].map(x => x.textContent.trim()),
    })"""))
    ck("非法协议被拒后仍留在编辑态", r["还在编辑态"], str(r))
    tips = " ".join(r.get("toasts", []) + r.get("dom提示", []))
    ck("给出了拒绝原因（含协议名与可选值）",
       "socks9" in tips and "socks5" in tips, repr(tips[:160]))
    print("   提示:", r.get("提示", "")[:100])

    # 合法值 → 保存
    pg.fill(".proxy-edit input", "socks5h://proxyuser:secretpw@127.0.0.1:1080")
    pg.evaluate("""() => {
        [...document.querySelectorAll('.proxy-edit button')].find(b => b.textContent.includes('保存')).click();
    }""")
    pg.wait_for_timeout(3000)
    r = pg.evaluate(J("""() => {
        const v = __VM__;
        const a = v.accounts.find(x => x.proxy_set);
        return {还开着: !!document.querySelector('.proxy-edit input'),
                代理: a ? {display: a.proxy_display, type: a.proxy_type, set: a.proxy_set} : null};
    }"""))
    ck("保存成功后编辑框关闭", not r["还开着"], str(r))
    ck("代理已写入且类型为 SOCKS5H", r["代理"] and r["代理"]["type"] == "SOCKS5H", str(r["代理"]))
    ck("密码在界面上打码", r["代理"] and "***" in r["代理"]["display"], str(r["代理"]))
    pg.screenshot(path=f"{OUT}/账号页-改代理.png", full_page=True)

    # 清空成直连
    pg.evaluate("""() => {
        [...document.querySelectorAll('button')].find(b => /改代理/.test(b.textContent)).click();
    }""")
    pg.wait_for_timeout(600)
    has_clear = pg.evaluate("""() => {
        const b = [...document.querySelectorAll('.proxy-edit button')].find(x => x.textContent.includes('清空'));
        if (b) b.click();
        return !!b;
    }""")
    ck("有「清空(改直连)」按钮", has_clear)
    pg.wait_for_timeout(2500)
    r = pg.evaluate(J("""() => {
        const v = __VM__;
        return {还有代理账号: v.accounts.filter(x => x.proxy_set).length,
                直连账号: v.accounts.filter(x => !x.proxy_set).length};
    }"""))
    ck("清空后该账号变为直连", r["还有代理账号"] == 0, str(r))

    print("\n【前端运行时错误】")
    ck("无 JS 运行时错误", not errs, str(errs[:2]))
    # 故意用非法代理打出的 400 是**预期**的拒绝行为，从这个断言里排除
    expected = [b for b in bad if b.startswith("400") and "/api/accounts/" in b]
    real_bad = [b for b in bad
                if "/api/auth/captcha" not in b and b not in expected]
    print(f"   预期内的拒绝请求 {len(expected)} 条（非法代理 → 400）")
    ck("无非预期 4xx/5xx", not real_bad, str(real_bad[:4]))
    if real_bad:
        print("       ", real_bad[:4])

    br.close()

print()
if fails:
    print(f"❌ {len(fails)} 项失败：")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过\n截图：{OUT}")