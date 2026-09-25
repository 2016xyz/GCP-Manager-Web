#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用「文字重叠」检测器（手机窄屏）

思路：把页面上所有可见文字节点的矩形取出来（用 Range.getClientRects，
连 td::before 这种伪元素生成的文字也能通过 textContent 覆盖到），
然后两两判断是否相交。相交且**互不为祖先/后代**才算真重叠 ——
祖先包住子元素是正常布局，不是重叠。

这样可以避开「靠读 CSS 猜」的坑：改动前先量出真实重叠，改完再量一次确认消失。
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, "tools")
from _fixtures import ADMIN_USER, admin_password, FAKE_ACCOUNTS  # noqa: E402

PW = admin_password()
VM = "document.querySelector('#app').__vue_app__._container._vnode.component.proxy"


def J(js):
    return js.replace("__VM__", VM)


# 只关心「互相压住」而不是「盒子包住盒子」：
# 1) 跳过祖先-后代关系
# 2) 要求相交面积占较窄那个元素的宽度比例 > 25%，过滤掉 1~2px 的舍入噪声
OVERLAP_JS = """() => {
    const rects = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
        const t = (n.nodeValue || '').trim();
        if (!t) continue;
        const el = n.parentElement;
        if (!el) continue;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) continue;
        // 把矩形裁剪到所有会裁剪的祖先框内。
        // Range.getClientRects() 给的是**未裁剪**的文字矩形：overflow:hidden/auto
        // 的祖先不会裁掉它，于是滚出可视区的文字、以及被 ellipsis 截断后
        // 「看不见的那部分」仍会报出矩形，与下方元素相交 —— 全是假阳性。
        let clip = {l: -1e9, t: -1e9, r: 1e9, b: 1e9};
        let anc = el;
        while (anc && anc !== document.documentElement) {
            const a = getComputedStyle(anc);
            if (a.overflow !== 'visible' || a.overflowX !== 'visible'
                || a.overflowY !== 'visible') {
                const ar = anc.getBoundingClientRect();
                // 只在确实发生溢出时才当作裁剪框（没溢出的容器不裁任何东西）
                if (anc.scrollHeight > anc.clientHeight + 1
                    || anc.scrollWidth > anc.clientWidth + 1) {
                    clip.l = Math.max(clip.l, ar.left);
                    clip.t = Math.max(clip.t, ar.top);
                    clip.r = Math.min(clip.r, ar.right);
                    clip.b = Math.min(clip.b, ar.bottom);
                }
            }
            anc = anc.parentElement;
        }
        // 用 Range 取该文字片段真实占用的矩形（可能换行成多个）
        const rg = document.createRange();
        rg.selectNodeContents(n);
        for (const r0 of rg.getClientRects()) {
            const x1 = Math.max(r0.left, clip.l), y1 = Math.max(r0.top, clip.t);
            const x2 = Math.min(r0.right, clip.r), y2 = Math.min(r0.bottom, clip.b);
            const w = x2 - x1, h = y2 - y1;
            if (w < 2 || h < 2) continue;      // 整段已被裁掉
            rects.push({el, txt: t.slice(0, 24), x: x1, y: y1, w, h});
        }
    }
    const hits = [];
    for (let i = 0; i < rects.length; i++) {
        for (let j = i + 1; j < rects.length; j++) {
            const a = rects[i], b = rects[j];
            // 同一个元素内部的换行片段不算
            if (a.el === b.el) continue;
            // 祖先-后代：包含关系属正常
            if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
            const ix = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
            const iy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
            if (ix <= 0 || iy <= 0) continue;
            const narrowW = Math.min(a.w, b.w);
            const narrowH = Math.min(a.h, b.h);
            // 行盒有 3~4px 的行距，1~3px 的相交属于印刷性间隙，不是字形压字。
            // 两个方向都要求成比例相交，才算「看得见的压字」。
            if (ix < 4 || iy < 4) continue;
            if (ix / narrowW < 0.25 || iy / narrowH < 0.3) continue;
            // 取交集中心，问浏览器「该点最上层是谁」——这是判断用户到底
            // 看见哪一层的 ground truth，比我猜 z-index/顺序可靠
            const cx = (Math.max(a.x, b.x) + Math.min(a.x + a.w, b.x + b.w)) / 2;
            const cy = (Math.max(a.y, b.y) + Math.min(a.y + a.h, b.y + b.h)) / 2;
            // 先滚到该点所在位置：elementFromPoint 只认视口内坐标，
            // 长页面里直接传文档坐标会返回 null（误判成「无重叠」）。
            const sy = window.scrollY;
            if (cy - sy > window.innerHeight - 20 || cy - sy < 20) {
                window.scrollTo(0, Math.max(0, cy - window.innerHeight / 2));
            }
            const sy2 = window.scrollY;
            const topEl = document.elementFromPoint(cx, cy - sy2);
            if (sy2 !== sy) window.scrollTo(0, sy);
            let topDesc = '(无)';
            if (topEl) {
                const aHit = topEl === a.el || a.el.contains(topEl) || topEl.contains(a.el);
                const bHit = topEl === b.el || b.el.contains(topEl) || topEl.contains(b.el);
                topDesc = topEl.tagName.toLowerCase() + '.' + (topEl.className || '')
                    + (aHit ? '  ← 上层是 A' : (bHit ? '  ← 上层是 B' : '  ← 都不是'));
            }
            hits.push({
                a: a.txt, b: b.txt,
                ael: a.el.tagName.toLowerCase() + '.' + (a.el.className || ''),
                bel: b.el.tagName.toLowerCase() + '.' + (b.el.className || ''),
                ax: Math.round(a.x), ay: Math.round(a.y),
                bx: Math.round(b.x), by: Math.round(b.y),
                ow: Math.round(ix), oh: Math.round(iy),
                上层: topDesc,
            });
        }
    }
    return {重叠对: hits.slice(0, 25), 总数: hits.length,
            文字块数: rects.length,
            文档宽: document.documentElement.clientWidth,
            滚动宽: document.documentElement.scrollWidth};
}"""

VIEWPORTS = [(390, True), (360, True), (320, True), (768, True), (1440, False)]

with sync_playwright() as p:
    br = p.chromium.launch(executable_path="/usr/bin/chromium-browser",
                           args=["--no-sandbox", "--disable-dev-shm-usage"])
    for w, touch in VIEWPORTS:
        ctx = br.new_context(viewport={"width": w, "height": 900}, has_touch=touch,
                             is_mobile=touch, device_scale_factor=1)
        pg = ctx.new_page()
        pg.goto("http://127.0.0.1:8001/login", wait_until="domcontentloaded")
        pg.wait_for_function("typeof refreshCaptcha === 'function'")
        code = pg.evaluate("""async () => {
            await refreshCaptcha();
            const r = await fetch('/__probe/captcha/' + encodeURIComponent(captchaId));
            return (await r.json()).code;
        }""")
        pg.fill("#username", ADMIN_USER)
        pg.fill("#password", PW)
        pg.fill("#captcha", code)
        pg.evaluate("login()")
        pg.wait_for_url("http://127.0.0.1:8001/*", timeout=20000)
        pg.wait_for_selector("#app .nav-i", timeout=15000)
        pg.wait_for_timeout(900)

        pg.evaluate(J("() => { __VM__.go('create'); }"))
        pg.wait_for_timeout(700)
        pg.evaluate(J("(d) => { const v = __VM__; v.accounts = d; v.checkedAccountIds = []; }"),
                    json.loads(json.dumps(FAKE_ACCOUNTS, ensure_ascii=False)))
        pg.wait_for_timeout(900)

        r = pg.evaluate(J(OVERLAP_JS))
        flag = "⚠ 横向溢出" if r["滚动宽"] > r["文档宽"] else "✓ 无横向溢出"
        print("=" * 78)
        print(f"【创建页 {w}px】文字块 {r['文字块数']}  重叠对 {r['总数']}  {flag}")
        for h in r["重叠对"]:
            print(f"   ✗ 「{h['a']}」({h['ael']} @{h['ax']},{h['ay']})"
                  f"  ×  「{h['b']}」({h['bel']} @{h['bx']},{h['by']})"
                  f"  交叠 {h['ow']}×{h['oh']}px  {h.get('上层','')}")
        pg.screenshot(path=f"/tmp/ov_shots/full-{w}.png", full_page=True)
        ctx.close()
    br.close()