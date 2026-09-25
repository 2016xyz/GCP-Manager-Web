#!/usr/bin/env python3
"""
UI 重排 / 版本号 / GitHub 地址 的实测校验

检查项：
  · 登录页：版本号与仓库地址是否真的被 /api/version 填上（不是占位符）
  · 侧栏：分组标题、导航顺序、页脚仓库链接与版本徽章
  · 按钮：各工具栏的分隔线数量与段数、危险按钮是否独立成段
  · 「关于」卡片：版本 / 仓库 / 反馈地址 / 更新内容
并输出截图。
"""
import json
import sys

from playwright.sync_api import sync_playwright

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ui_shots"
import os
os.makedirs(OUT, exist_ok=True)

# ★ 同 responsive_check.py：密码不写死，统一从 _fixtures 读
import _fixtures as FX  # noqa: E402
ADMIN, PW = FX.ADMIN_USER, FX.admin_password()
LOGIN = "http://127.0.0.1:8001/login"
CONSOLE = "http://127.0.0.1:8001/"


def login(pg):
    pg.goto(LOGIN, wait_until="domcontentloaded")
    pg.wait_for_function("typeof refreshCaptcha === 'function'")
    code = pg.evaluate("""async()=>{await refreshCaptcha();
        return (await (await fetch('/__probe/captcha/'+encodeURIComponent(captchaId))).json()).code;}""")
    pg.fill("#username", ADMIN); pg.fill("#password", PW); pg.fill("#captcha", code)
    pg.evaluate("login()")
    pg.wait_for_url(f"{CONSOLE}*", timeout=20000)
    pg.wait_for_selector("#app .nav-i", timeout=15000)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/chromium-browser", headless=True,
                          args=["--no-sandbox", "--disable-dev-shm-usage"])
    pg = b.new_context(viewport={"width": 1440, "height": 950}).new_page()

    # ── ① 登录页（未登录状态）──
    pg.goto(LOGIN, wait_until="domcontentloaded")
    pg.wait_for_timeout(1500)
    logininfo = pg.evaluate("""() => {
        const v = document.getElementById('verText');
        const r = document.getElementById('repoLink');
        const t = document.getElementById('repoText');
        return {版本文本: v ? v.textContent : null,
                仓库文本: t ? t.textContent : null,
                仓库href: r ? r.getAttribute('href') : null,
                新窗口: r ? r.getAttribute('target') : null,
                防钓鱼: r ? r.getAttribute('rel') : null,
                页脚可见: !!document.querySelector('.wow-login-footer')};
    }""")
    print("【登录页】", json.dumps(logininfo, ensure_ascii=False))
    pg.screenshot(path=f"{OUT}/登录页-版本与仓库.png")

    # ── ② 控制台 ──
    login(pg)
    pg.wait_for_timeout(1200)
    nav = pg.evaluate("""() => {
        const groups = [...document.querySelectorAll('.sb-nav .sb-group')].map(e => e.textContent.trim());
        const items = [...document.querySelectorAll('.sb-nav .nav-i')].map(e => {
            const lb = e.querySelector('.lb');
            return lb ? lb.textContent.trim() : e.getAttribute('title');
        });
        const link = document.querySelector('.sb-link');
        const brand = document.querySelector('.sb-brand .tx small');
        return {分组: groups, 导航顺序: items,
                品牌副标题: brand ? brand.textContent.trim() : null,
                页脚可见: !!link,
                页脚仓库: link ? (link.querySelector('.lb')||{}).textContent : null,
                页脚版本: link ? (link.querySelector('.ver')||{}).textContent : null,
                页脚href: link ? link.getAttribute('href') : null,
                页脚rel: link ? link.getAttribute('rel') : null};
    }""")
    print("\n【侧栏】", json.dumps(nav, ensure_ascii=False, indent=1))
    pg.screenshot(path=f"{OUT}/侧栏-分组与版本.png")

    # ── ③ 各页按钮分隔线 ──
    print("\n【按钮分组】（每行列出各段按钮，| 表示分隔线）")
    for tab, label in [("create", "创建实例"), ("accounts", "账号管理"),
                       ("instances", "实例列表"), ("exec", "命令执行"),
                       ("tasks", "任务日志")]:
        pg.evaluate("""(t) => {
            document.querySelector('#app').__vue_app__
              ._container._vnode.component.proxy.go(t);
        }""", tab)
        pg.wait_for_timeout(900)
        segs = pg.evaluate("""() => {
            const rows = [...document.querySelectorAll('.content .row')];
            const out = [];
            rows.forEach(r => {
                const btns = [...r.querySelectorAll(':scope > button')];
                if (btns.length < 2) return;
                const parts = [[]];
                [...r.children].forEach(c => {
                    if (c.classList.contains('br')) parts.push([]);
                    else if (c.tagName === 'BUTTON')
                        parts[parts.length-1].push((c.textContent||'').trim().slice(0,14));
                });
                const nonEmpty = parts.filter(x => x.length);
                if (nonEmpty.length > 1)
                    out.push(nonEmpty.map(x => x.join(' / ')).join('   |   '));
            });
            return out;
        }""")
        for s_ in segs:
            print(f"  {label:<8} {s_}")
        pg.screenshot(path=f"{OUT}/{tab}-按钮分组.png")

    # ── ④ 关于卡片 ──
    pg.evaluate("""() => {
        document.querySelector('#app').__vue_app__
          ._container._vnode.component.proxy.go('profile');
    }""")
    pg.wait_for_timeout(900)
    about = pg.evaluate("""() => {
        const cards = [...document.querySelectorAll('.card')];
        const c = cards.find(x => (x.querySelector('h2')||{}).textContent?.includes('关于'));
        if (!c) return null;
        const links = [...c.querySelectorAll('a.repo-link')].map(a => ({文本:a.textContent.trim(), href:a.getAttribute('href'), rel:a.getAttribute('rel')}));
        return {版本徽章: (c.querySelector('.badge')||{}).textContent,
                链接: links,
                更新条目数: c.querySelectorAll('.about-ul li').length,
                首条更新: (c.querySelector('.about-ul li')||{}).textContent};
    }""")
    print("\n【关于卡片】", json.dumps(about, ensure_ascii=False, indent=1))
    pg.screenshot(path=f"{OUT}/关于卡片.png")

    b.close()

print(f"\n截图已存 {OUT}")