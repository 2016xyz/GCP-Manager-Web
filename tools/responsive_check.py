#!/usr/bin/env python3
"""
多视口响应式验证（桌面 / 平板横竖 / 手机）

用途：把控制台在若干典型屏宽下真实渲染一遍，检查
  · 是否出现横向溢出（scrollWidth > clientWidth）—— 手机端最常见的破版
  · 侧栏是否按断点切换（完整 / 图标 / 顶部横滚）
  · 表格是否在窄屏转成卡片流
  · 勘察页的 KPI、配额条、分区头是否合理收敛
并逐个截图留证。

前置：需要在 8001 起一个测试夹具（tools/ui_login_probe.py），
      用于在浏览器里直接读验证码明文完成登录。

用法：python3 tools/responsive_check.py [输出目录]
"""
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/shots"
os.makedirs(OUT, exist_ok=True)

from playwright.sync_api import sync_playwright

VIEWPORTS = [
    ("desktop-1440", 1440, 900, False),
    ("laptop-1280", 1280, 800, False),
    ("tablet-land-1024", 1024, 768, True),   # 平板横屏 → 触控
    ("tablet-port-768", 768, 1024, True),    # 平板竖屏 → 触控
    ("phone-390", 390, 844, True),           # 手机 → 触控
]

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _fixtures as FX  # noqa: E402

ADMIN = FX.ADMIN_USER
# ★ 不要在这里写死密码：改密后本文件会立刻失效（历史上就吃过这个亏，
# 5 个脚本集体登录超时，看起来像产品坏了）。统一走 _fixtures 的口径：
# 先读 GCPWEB_ADMIN_PW，再读 data/INITIAL_ADMIN.txt。
PW = FX.admin_password()
LOGIN_URL = "http://127.0.0.1:8001/login"
CONSOLE_URL = "http://127.0.0.1:8001/"   # 与夹具同源，便于继续读验证码

results = []


def probe_captcha(page):
    """借测试夹具读出验证码明文，并在页面上刷新出同一张"""
    return page.evaluate("""async () => {
        await refreshCaptcha();
        const r = await fetch('/__probe/captcha/' + encodeURIComponent(captchaId));
        return (await r.json()).code;
    }""")


def login(page):
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_function("typeof refreshCaptcha === 'function'")
    code = probe_captcha(page)
    page.fill("#username", ADMIN)
    page.fill("#password", FX.admin_password())
    page.fill("#captcha", code)
    page.evaluate("login()")
    page.wait_for_url(f"{CONSOLE_URL}*", timeout=20000)   # login() 跳同源首页
    page.wait_for_selector("#app .nav-i", timeout=15000)


def measure(page):
    """采集当前视口的布局指标"""
    return page.evaluate("""() => {
        const de = document.documentElement;
        const sb = document.querySelector('.sidebar');
        const cs = sb ? getComputedStyle(sb) : null;
        const btn = document.querySelector('button.p');
        const bcs = btn ? getComputedStyle(btn) : null;
        const tbl = document.querySelector('table.resp');
        const insp = document.querySelectorAll('.insp-sec');
        return {
            视口宽: window.innerWidth,
            横向溢出: de.scrollWidth - de.clientWidth,
            侧栏方向: cs ? cs.flexDirection : null,
            侧栏定位: cs ? cs.position : null,
            侧栏宽: sb ? Math.round(sb.getBoundingClientRect().width) : null,
            导航换行成横滚: cs ? (cs.flexDirection === 'row') : null,
            表格已卡片化: tbl ? getComputedStyle(tbl.querySelector('thead')).display === 'none' : null,
            分区数: insp.length,
            主色按钮高度: bcs ? Math.round(btn.getBoundingClientRect().height) : null,
            主色按钮圆角: bcs ? bcs.borderTopLeftRadius : null,
            主色按钮文字色: bcs ? bcs.color : null,
            触控命中: window.matchMedia('(hover:none)').matches,
            工具栏控件宽: [...document.querySelectorAll('.insp-in,.insp-in-acc')]
                             .map(e => Math.round(e.getBoundingClientRect().width)),
        };
    }""")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/usr/bin/chromium-browser",
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--force-device-scale-factor=1"],
        )
        # 桌面上下文
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  has_touch=False)
        page = ctx.new_page()
        login(page)

        for name, w, h, touch in VIEWPORTS:
            # 触屏设备必须用独立的 context 建（has_touch 只在 context 级生效），
            # 否则 @media (hover:none) 不命中，测不到真机上的 42px 触控高度
            if touch:
                ctx.close()
                ctx = browser.new_context(
                    viewport={"width": w, "height": h},
                    has_touch=True, is_mobile=(w <= 480),
                    device_scale_factor=2 if w <= 480 else 1)
                page = ctx.new_page()
                login(page)
            else:
                page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(400)

            # ① 创建页（看按钮）
            page.evaluate("""() => {
                document.querySelector('#app').__vue_app__
                  ._container._vnode.component.proxy.tab = 'create';
            }""")
            page.wait_for_timeout(500)
            page.screenshot(path=f"{OUT}/{name}-创建页.png", full_page=False)

            # ② 勘察页（看表格与 KPI）
            page.evaluate("""async () => {
                const vm = document.querySelector('#app').__vue_app__
                             ._container._vnode.component.proxy;
                vm.go('inspect');
                if (!Object.keys(vm.inspect).length) await vm.loadInspect(false, 'quick');
                Object.keys(vm.inspectOpen).forEach(k => vm.inspectOpen[k] = false);
                ['summary', 'firewalls'].forEach(k => vm.inspectOpen[k] = true);
                await new Promise(r => setTimeout(r, 400));
            }""")
            page.wait_for_selector(".insp-sec", timeout=30000)
            page.wait_for_timeout(700)
            m = measure(page)
            m["视口"] = name
            results.append(m)
            page.screenshot(path=f"{OUT}/{name}-勘察页.png", full_page=False)

        browser.close()


main()
print(f"\n{'视口':<18}{'横向溢出':>8}{'侧栏':>10}{'表格卡片化':>11}{'按钮高':>8}{'圆角':>8}")
print("-" * 70)
for r in results:
    print(f"{r['视口']:<18}{r['横向溢出']:>8}{('横滚' if r['导航换行成横滚'] else '竖列'):>10}"
          f"{('是' if r['表格已卡片化'] else '否'):>11}{r['主色按钮高度']:>8}{r['主色按钮圆角']:>8}")
print()
print(json.dumps(results, ensure_ascii=False, indent=1))
