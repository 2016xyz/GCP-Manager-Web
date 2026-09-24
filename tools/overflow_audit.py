#!/usr/bin/env python3
"""
窄屏破版审计：在手机/平板宽度下逐元素找溢出与截断

比目测更可靠 —— 目测容易漏掉被 overflow:hidden 裁掉的文字、
或超出容器但恰好被父级滚动的元素。这里直接量三个指标：

  1. 文档级横向溢出（scrollWidth > clientWidth）
     手机端最常见的破版，页面能左右拖动就是它
  2. 元素自身内容溢出（scrollWidth > clientWidth + 1）
     且 overflow 是 visible/hidden —— 说明内容被裁或顶出容器
  3. 元素右边界超出视口（getBoundingClientRect().right > innerWidth）
     排除掉本就用于横向滚动的容器（.tw / .sb-nav / pre）

用法：python3 tools/overflow_audit.py
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fixtures as FX  # noqa: E402

ADMIN, PW = FX.ADMIN_USER, FX.admin_password()
LOGIN = "http://127.0.0.1:8001/login"
CONSOLE = "http://127.0.0.1:8001/"

VIEWPORTS = [("tablet-land-1024", 1024, 768), ("tablet-port-768", 768, 1024),
             ("phone-390", 390, 844), ("phone-360", 360, 800)]

AUDIT_JS = r"""() => {
  const IW = window.innerWidth;
  const SCROLLERS = ['tw','sb-nav','log'];   // 本就该横向滚动的容器
  // 判断某个元素是否处在横向滚动容器内部：处在其内的元素超出视口是正常的，
  // 用户横向滑动即可看到，不算破版。只排除容器自身是不够的 ——
  // 导航项（.nav-i）是 .sb-nav 的子元素，必须沿祖先链判断。
  const inScroller = el => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const cs = getComputedStyle(p);
      const inList = SCROLLERS.some(s => String(p.className||'').split(/\s+/).includes(s));
      if (inList || cs.overflowX === 'auto' || cs.overflowX === 'scroll') return true;
    }
    return false;
  };
  const bad = [], clipped = [];
  document.querySelectorAll('*').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    const cls = String(el.className || '');
    const isScroller = SCROLLERS.some(s => cls.split(/\s+/).includes(s))
                       || el.tagName === 'PRE' || inScroller(el);
    // ① 顶出视口
    if (!isScroller && r.right > IW + 1.5) {
      bad.push({选择器: el.tagName + (cls ? '.' + cls.trim().split(/\s+/)[0] : ''),
                文本: (el.textContent || '').trim().slice(0, 24),
                右边界: Math.round(r.right), 视口宽: IW});
    }
    // ② 内容被裁：只认 overflow-x 为 hidden/clip 的情况。
    //    overflow:visible 时内容只是「溢出显示」，并没有被裁掉 ——
    //    文字行常有 1~3px 的舍入差，算进来全是假阳性；
    //    真溢出视口的情况由 ① 兜住。
    const c = getComputedStyle(el);
    // 带 text-overflow:ellipsis 的元素是「故意截断」（例如侧栏里过长的仓库名），
    // 属于设计意图而非破版，必须排除，否则满屏假阳性。
    const ellipsis = c.textOverflow === 'ellipsis';
    if (!isScroller && !ellipsis && el.children.length === 0
        && (c.overflowX === 'hidden' || c.overflowX === 'clip')
        && el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0) {
      clipped.push({选择器: el.tagName + (cls ? '.' + cls.trim().split(/\s+/)[0] : ''),
                    文本: (el.textContent || '').trim().slice(0, 24),
                    内容宽: el.scrollWidth, 可见宽: el.clientWidth,
                    overflowX: c.overflowX});
    }
  });
  const de = document.documentElement;
  return {视口宽: IW,
          文档横向溢出: de.scrollWidth - de.clientWidth,
          顶出视口: bad.slice(0, 12), 顶出总数: bad.length,
          内容被裁: clipped.slice(0, 12), 被裁总数: clipped.length};
}"""


def login(pg):
    pg.goto(LOGIN, wait_until="domcontentloaded")
    pg.wait_for_function("typeof refreshCaptcha === 'function'")
    code = pg.evaluate("""async()=>{await refreshCaptcha();
        return (await (await fetch('/__probe/captcha/'+encodeURIComponent(captchaId))).json()).code;}""")
    pg.fill("#username", ADMIN); pg.fill("#password", PW); pg.fill("#captcha", code)
    pg.evaluate("login()")
    pg.wait_for_url(f"{CONSOLE}*", timeout=20000)
    pg.wait_for_selector("#app .nav-i", timeout=15000)


def main():
    rows = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/chromium-browser", headless=True,
                              args=["--no-sandbox", "--disable-dev-shm-usage"])
        for name, w, h in VIEWPORTS:
            ctx = b.new_context(viewport={"width": w, "height": h},
                                has_touch=True, is_mobile=(w <= 480))
            pg = ctx.new_page()
            login(pg)
            # 覆盖各主要页面，不只测一页
            for tab, label, extra in [
                ("create", "创建实例", ""),
                ("instances", "实例列表", ""),
                ("accounts", "账号管理", ""),
                ("tasks", "任务日志", ""),
                # 注意：这里不要再 const vm —— 外层已经声明过，重复声明会
                # SyntaxError 让整个 evaluate 失败
                ("inspect", "GCP 资源", """
                    if (!Object.keys(vm.inspect).length) await vm.loadInspect(false, 'quick');
                    Object.keys(vm.inspectOpen).forEach(k => vm.inspectOpen[k] = true);
                """),
            ]:
                pg.evaluate("""async ([t]) => {
                    const vm = document.querySelector('#app').__vue_app__
                                 ._container._vnode.component.proxy;
                    vm.go(t);
                    %s
                }""".replace("%s", extra), [tab])
                pg.wait_for_timeout(1100)
                # 空表的窄屏表现和满数据的窄屏表现完全不同，
                # 新列的破版只有填了数据才测得出来
                if tab in ("instances", "accounts"):
                    pg.evaluate("""async ([t, journal]) => {
                        const vm = document.querySelector('#app').__vue_app__
                                     ._container._vnode.component.proxy;
                        if (t === 'instances') {
                            vm.instances = JSON.parse(journal).instances;
                            vm.instErrors = [];
                        } else {
                            vm.accounts = JSON.parse(journal).accounts;
                        }
                    }""", [tab, json.dumps({"instances": FX.FAKE_INSTANCES,
                                            "accounts": FX.FAKE_ACCOUNTS},
                                           ensure_ascii=False)])
                    pg.wait_for_timeout(500)
                r = pg.evaluate(AUDIT_JS)
                r["视口"], r["页面"] = name, label
                rows.append(r)
            ctx.close()
        b.close()

    print(f"{'视口':<18}{'页面':<10}{'文档溢出':>9}{'顶出视口':>9}{'内容被裁':>9}")
    print("-" * 58)
    bad_total = 0
    for r in rows:
        flag = ""
        if r["文档横向溢出"] > 0 or r["顶出总数"] or r["被裁总数"]:
            flag = "  ← 有问题"
            bad_total += 1
        print(f"{r['视口']:<18}{r['页面']:<10}{r['文档横向溢出']:>9}"
              f"{r['顶出总数']:>9}{r['被裁总数']:>9}{flag}")
    print()
    for r in rows:
        if r["顶出总数"] or r["被裁总数"]:
            print(json.dumps(r, ensure_ascii=False, indent=1))
    print(f"\n{'✅ 全部 %d 个「视口×页面」组合无破版' % len(rows) if not bad_total else '❌ %d 个组合存在破版' % bad_total}")
    return bad_total


sys.exit(0 if main() == 0 else 1)
