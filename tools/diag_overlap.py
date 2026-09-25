#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定点诊断：360px 下「账号表第二行邮箱」与「机器备注」到底谁压谁"""
import json
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, "tools")
from _fixtures import ADMIN_USER, admin_password, FAKE_ACCOUNTS  # noqa: E402

PW = admin_password()
VM = "document.querySelector('#app').__vue_app__._container._vnode.component.proxy"


def J(js):
    return js.replace("__VM__", VM)


DIAG = """() => {
    const v = __VM__;
    // 账号表（创建页第一张 .tw 内的表）
    const tw = document.querySelector('.tw[style*="max-height"]');
    const tbl = tw.querySelector('table');
    const mails = [...tbl.querySelectorAll('span.mail')];
    const lastMail = mails[mails.length - 1];

    // 「机器备注」
    const lab = [...document.querySelectorAll('label.f')]
        .find(x => (x.textContent || '').includes('机器备注'));
    const inp = lab.querySelector('input');

    const box = el => {
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return {x: Math.round(r.x), y: Math.round(r.y),
                w: Math.round(r.width), h: Math.round(r.height),
                display: cs.display, overflow: cs.overflow,
                maxHeight: cs.maxHeight, position: cs.position,
                zIndex: cs.zIndex};
    };

    // 祖先链：谁身上有 overflow/max-height 约束
    const chain = el => {
        const out = [];
        let n = el;
        while (n && n !== document.documentElement) {
            const cs = getComputedStyle(n);
            out.push({
                标签: n.tagName.toLowerCase(),
                类: (n.className || '').toString().slice(0, 34),
                y: Math.round(n.getBoundingClientRect().y),
                h: Math.round(n.getBoundingClientRect().height),
                overflow: cs.overflow, maxHeight: cs.maxHeight,
                display: cs.display,
            });
            n = n.parentElement;
        }
        return out;
    };

    const mr = lastMail.getBoundingClientRect();
    const lr = lab.getBoundingClientRect();
    // 重叠中心取两矩形交集的中点
    const cx = (Math.max(mr.x, lr.x) + Math.min(mr.right, lr.right)) / 2;
    const cy = (Math.max(mr.y, lr.y) + Math.min(mr.bottom, lr.bottom)) / 2;
    const top = document.elementFromPoint(cx, cy);
    const topPath = [];
    let t = top;
    while (t && t !== document.body) {
        topPath.push(t.tagName.toLowerCase() + '.' + (t.className || '').toString().slice(0, 30));
        t = t.parentElement;
    }

    // 各 td 的「真实内容高度」vs「盒子高度」——判断是否被裁剪
    const tds = [...tbl.querySelectorAll('tbody td')].map(td => {
        const r = td.getBoundingClientRect();
        return {类: (td.className || ''), dataL: td.dataset.l || '',
                boxH: Math.round(r.height),
                scrollH: td.scrollHeight,
                overflow: getComputedStyle(td).overflow};
    });

    return {
        视口: innerWidth,
        表容器: box(tw),
        表: box(tbl),
        表格_是否被裁: tbl.scrollHeight > tw.clientHeight,
        表容器_scrollH: tw.scrollHeight, 表容器_clientH: tw.clientHeight,
        最后一个邮箱: box(lastMail),
        机器备注label: box(lab), 备注输入框: box(inp),
        交集中心: [Math.round(cx), Math.round(cy)],
        该点最上层元素: topPath,
        邮箱祖先链: chain(lastMail),
        备注祖先链: chain(lab),
        各td: tds,
    };
}"""

with sync_playwright() as p:
    br = p.chromium.launch(executable_path="/usr/bin/chromium-browser",
                           args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = br.new_context(viewport={"width": 360, "height": 900}, has_touch=True, is_mobile=True)
    pg = ctx.new_page()
    pg.goto("http://127.0.0.1:8001/login", wait_until="domcontentloaded")
    pg.wait_for_function("typeof refreshCaptcha === 'function'")
    code = pg.evaluate("""async () => {
        await refreshCaptcha();
        const r = await fetch('/__probe/captcha/' + encodeURIComponent(captchaId));
        return (await r.json()).code;
    }""")
    pg.fill("#username", ADMIN_USER); pg.fill("#password", PW); pg.fill("#captcha", code)
    pg.evaluate("login()")
    pg.wait_for_url("http://127.0.0.1:8001/*", timeout=20000)
    pg.wait_for_selector("#app .nav-i", timeout=15000)
    pg.wait_for_timeout(800)
    pg.evaluate(J("() => { __VM__.go('create'); }"))
    pg.wait_for_timeout(700)
    pg.evaluate(J("(d) => { const v = __VM__; v.accounts = d; v.checkedAccountIds = []; }"),
                json.loads(json.dumps(FAKE_ACCOUNTS, ensure_ascii=False)))
    pg.wait_for_timeout(900)
    r = pg.evaluate(J(DIAG))
    print(json.dumps(r, ensure_ascii=False, indent=2))
    br.close()