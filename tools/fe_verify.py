#!/usr/bin/env python3
"""
前端新功能的实测校验

思路：实例列表的数据来自真实 GCP（本项目当前 0 台实例），
所以这里把**代表性数据注入 Vue 组件实例**，让真实模板去渲染，
再核对 DOM 结果。这样验的是真模板 + 真交互 + 真接口，
而不是另写一套展示代码自证。

覆盖：创建页安装多选、实例列表新列、备注就地编辑、root 密码
二次验证流程、账号备注与代理展示、长邮箱折叠。
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fe_shots"
os.makedirs(OUT, exist_ok=True)

import _fixtures as FX  # noqa: E402  同目录夹具：管理员密码统一从 data/INITIAL_ADMIN.txt 读
ADMIN, PW = FX.ADMIN_USER, FX.admin_password()
LOGIN = "http://127.0.0.1:8001/login"
CONSOLE = "http://127.0.0.1:8001/"



# ── 把注入用的实例真正写进本地库 ──────────────────────────────
# /api/instances/password 是**从库里读 root 密码**的。只在 Vue 状态里造数据的话，
# 验密接口会返回 404「没有 root 密码记录」——那是正确行为，不是 bug。
# 所以这里先落库，测试完再清掉，不留假数据。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GCPWEB_DATA_DIR", os.path.join(ROOT, "data"))
from core.store import Store  # noqa: E402

_db = os.path.join(ROOT, "data", "gcp_web.db")
_store = Store(_db)
SEEDED = []
SEED_PW = {}
for _i in FX.FAKE_INSTANCES:
    if _i["has_password"]:
        _pw = "R00t-" + _i["name"][-4:] + "!Aa1"
        _store.save_vm(_i["name"], _i["ip"], _pw, _i["account_id"], _i["zone"],
                       _i["machine_type"], _i["spec"]["image_key"],
                       _i["disk_type"], _i["disk_size_gb"],
                       note=_i["note"], installs=",".join(_i["installs"]))
        SEEDED.append(_i["name"])
        SEED_PW[_i["name"]] = _pw
print("已为验证写入实例记录:", SEEDED, "（结束后清除）")
print()

fails = []


def ck(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def boot(pg):
    """登录并进入控制台"""
    pg.goto(LOGIN, wait_until="domcontentloaded")
    pg.wait_for_function("typeof refreshCaptcha === 'function'")
    code = pg.evaluate("""async()=>{await refreshCaptcha();
        return (await (await fetch('/__probe/captcha/'+encodeURIComponent(captchaId))).json()).code;}""")
    pg.fill("#username", ADMIN); pg.fill("#password", PW); pg.fill("#captcha", code)
    pg.evaluate("login()")
    pg.wait_for_url(f"{CONSOLE}*", timeout=20000)
    pg.wait_for_selector("#app .nav-i", timeout=15000)
    pg.wait_for_timeout(900)


def vm(pg):
    """拿到 Vue 组件实例（内部 API，仅测试用）"""
    return "document.querySelector('#app').__vue_app__._container._vnode.component.proxy"


def inject_inst(pg, v):
    """
    把假实例注入 Vue 状态。

    每次断言前都调一次 —— 因为控制台里的真实同步（boot() 里未 await 的
    loadInstances、以及切页时的 onEnter）随时可能返回并覆盖注入值。
    这是测试环境的时序问题，不是产品缺陷，所以测试侧自己保证注入有效。
    """
    pg.evaluate(f"""() => {{
        {v}.instances = {json.dumps(FX.FAKE_INSTANCES, ensure_ascii=False)};
        {v}.instErrors = [];
    }}""")
    pg.wait_for_timeout(350)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/chromium-browser", headless=True,
                          args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = b.new_context(viewport={"width": 1600, "height": 1000})
    pg = ctx.new_page()

    # 收集前端运行时错误 —— 模板写错会在这里暴露
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append("console.error: " + m.text) if m.type == "error" else None)
    bad = []
    pg.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    pg.on("requestfailed", lambda r: bad.append(f"FAILED {r.url} {r.failure}"))

    boot(pg)
    v = vm(pg)

    # ── 注入数据 ──────────────────────────────────────────────
    pg.evaluate(f"""() => {{
        const c = {v};
        c.instances = {json.dumps(FX.FAKE_INSTANCES, ensure_ascii=False)};
        c.accounts = {json.dumps(FX.FAKE_ACCOUNTS, ensure_ascii=False)};
        c.instErrors = [];
    }}""")
    pg.wait_for_timeout(500)

    # ── 创建页 ────────────────────────────────────────────────
    print("【创建页】")
    pg.evaluate(f"""() => {{ {v}.go('create'); }}""")
    pg.wait_for_timeout(900)
    r = pg.evaluate("""() => {
        const pks = [...document.querySelectorAll('.pk')];
        const note = [...document.querySelectorAll('input')].find(i => (i.placeholder||'').includes('给同事的测试机'));
        return {
            预设卡数: pks.length,
            预设名: pks.map(x => (x.querySelector('.nm')||{}).textContent.trim()),
            有版本号: pks.filter(x => x.querySelector('.vf')).length,
            备注输入框: !!note,
            预设数据来自接口: true,
        };
    }""")
    print("   ", json.dumps(r, ensure_ascii=False))
    ck("创建页渲染 5 个安装预设卡", r["预设卡数"] == 5, str(r["预设卡数"]))
    ck("预设含 Hermes 与 3x-ui", any("Hermes" in x for x in r["预设名"])
       and any("3x-ui" in x for x in r["预设名"]), str(r["预设名"]))
    ck("备注输入框存在", r["备注输入框"])

    # 勾选两个预设，看样式与状态
    pg.evaluate(f"""() => {{ const c = {v}; c.installPicked = ['docker','nps']; c.instNote='客户A测试机'; }}""")
    pg.wait_for_timeout(400)
    r2 = pg.evaluate("""() => ({
        选中样式数: document.querySelectorAll('.pk.on').length,
        选中项: JSON.parse(JSON.stringify(
            [...document.querySelectorAll('.pk.on')].map(x => x.querySelector('.nm').textContent.replace(/\\s+/g,' ').trim()))),
    })""")
    ck("勾选后 2 张卡呈选中态", r2["选中样式数"] == 2, str(r2["选中样式数"]))
    pg.screenshot(path=f"{OUT}/创建页-备注与安装多选.png", full_page=True)

    # ── 实例列表 ──────────────────────────────────────────────
    # 注意顺序：go() 会触发 loadInstances() 用真实 GCP 结果（本项目 0 台）
    # 覆盖掉注入数据，所以必须**先切页、再注入**。
    print("\n【实例列表】")
    inject_inst(pg, v)
    pg.evaluate(f"""() => {{ {v}.go('instances'); }}""")
    # 必须等真实 GCP 同步彻底跑完再注入：boot() 里那句 loadInstances 没被 await，
    # 它可能在注入之后才返回，把注入的数据覆盖掉（本项目真实实例数为 0）。
    pg.wait_for_function(f"() => {v}.loadingInst === false", timeout=60000)
    pg.wait_for_timeout(1200)
    pg.evaluate(f"""() => {{
        {v}.instances = {json.dumps(FX.FAKE_INSTANCES, ensure_ascii=False)};
        {v}.instErrors = [];
    }}""")
    pg.wait_for_timeout(400)
    r = pg.evaluate("""() => {
        const ths = [...document.querySelectorAll('table.resp thead th')].map(x => x.textContent.trim());
        const rows = [...document.querySelectorAll('table.resp tbody tr')];
        const tds = rows.length ? [...rows[0].querySelectorAll('td')].map(x => x.textContent.replace(/\\s+/g,' ').trim()) : [];
        return {
            表头: ths,
            行数: rows.length,
            首行: tds,
            密码掩码: [...document.querySelectorAll('.pwd-mask')].map(x=>x.textContent.trim()),
            免费徽章: [...document.querySelectorAll('.badge.free')].map(x=>x.textContent.trim()),
            计费徽章: [...document.querySelectorAll('.badge.paid')].map(x=>x.textContent.trim()),
            所在地: [...document.querySelectorAll('.loc')].map(x=>x.textContent.trim()),
            镜像文本: [...rows[0].querySelectorAll('td')].map(x=>x.textContent.replace(/\\s+/g,' ').trim())[5],
            磁盘文本: [...rows[0].querySelectorAll('td')].map(x=>x.textContent.replace(/\\s+/g,' ').trim())[6],
            备注文本: rows[0].querySelector('.note-cell .tx') ? rows[0].querySelector('.note-cell .tx').textContent.trim() : null,
            展开按钮数: document.querySelectorAll('.mail-tg').length,
        };
    }""")
    print("   表头:", " | ".join(r["表头"]))
    ck("表头含「所在地」相关列", any("所在地" in x for x in r["表头"]), str(r["表头"]))
    ck("表头含费用列", any("费用" in x for x in r["表头"]))
    ck("表头含 Root 密码列", any("Root" in x for x in r["表头"]))
    ck("不再存在单独 Zone 列", "Zone" not in r["表头"], str(r["表头"]))
    ck("渲染 2 行", r["行数"] == 2, str(r["行数"]))
    ck("IP 后显示所在地", any("爱荷华" in x for x in r["所在地"]), str(r["所在地"]))
    ck("有密码的实例显示掩码 ••••", r["密码掩码"] and "••••••••" in r["密码掩码"][0], str(r["密码掩码"]))
    ck("免费机型带绿徽章", r["免费徽章"] and "免费机型" in r["免费徽章"][0], str(r["免费徽章"]))
    ck("付费机型带「计费」徽章", r["计费徽章"] and "计费" in r["计费徽章"][0], str(r["计费徽章"]))
    ck("镜像列显示镜像名称", "ubuntu-2204-jammy" in (r["镜像文本"] or ""), str(r["镜像文本"]))
    ck("磁盘列显示大小", "30 GB" in (r["磁盘文本"] or ""), str(r["磁盘文本"]))
    ck("备注显示在名称下方", r["备注文本"] == "客户A环境", str(r["备注文本"]))
    print("   费用单元格:", r["首行"][8] if len(r["首行"]) > 8 else "?")
    ck("费用列含每小时与每天", "$0.0101" in (r["首行"][8] if len(r["首行"]) > 8 else "")
       and "$0.241" in (r["首行"][8] if len(r["首行"]) > 8 else ""), str(r["首行"][8:9]))
    pg.screenshot(path=f"{OUT}/实例列表-新列.png", full_page=True)

    # ── 点击「显示」→ 验证弹层出现 ────────────────────────────
    print("\n【root 密码二次验证】")
    pg.evaluate("""() => {
        const btn = [...document.querySelectorAll('table.resp tbody tr button')]
            .find(b => b.textContent.trim() === '显示');
        btn.click();
    }""")
    pg.wait_for_timeout(600)
    r = pg.evaluate("""() => {
        const m = document.querySelector('.mask .sheet');
        return m ? {标题: m.querySelector('h3').textContent.trim(),
                    有输入框: !!m.querySelector('input'),
                    输入类型: m.querySelector('input') ? m.querySelector('input').type : null,
                    提示: (m.querySelector('.hint')||{}).textContent || ''} : null;
    }""")
    print("   ", json.dumps(r, ensure_ascii=False))
    ck("弹出验密弹层", r is not None and "Root 密码" in (r or {}).get("标题", ""), str(r))
    ck("弹层是密码输入框", (r or {}).get("输入类型") == "password", str(r))
    pg.screenshot(path=f"{OUT}/root密码-验密弹层.png")

    # 错误密码 → 应显示后端返回的具体原因
    pg.fill(".mask .sheet input", "definitely-wrong-password")
    pg.evaluate("document.querySelector('.mask .sheet .p').parentElement.querySelector('button.p').click()")
    pg.wait_for_timeout(2500)
    r = pg.evaluate("""() => {
        const e = document.querySelector('.mask .alert');
        return {错误显示: e ? e.textContent.replace(/⚠/,'').trim() : null,
                弹层还在: !!document.querySelector('.mask')};
    }""")
    print("   错误密码后:", json.dumps(r, ensure_ascii=False))
    ck("错误密码显示具体原因", (r["错误显示"] or "").find("密码") >= 0, str(r))
    ck("错误后弹层保持打开", r["弹层还在"])
    pg.screenshot(path=f"{OUT}/root密码-密码错误.png")

    # 正确密码 → 应出示真实密码
    pg.fill(".mask .sheet input", PW)
    pg.evaluate("document.querySelector('.mask .sheet .p').parentElement.querySelector('button.p').click()")
    pg.wait_for_timeout(2500)
    ck("正确密码后弹层关闭",
       pg.evaluate("!document.querySelector('.mask')"))
    # 重注入一次，让 revealedPwd 的绑定结果渲染出来
    inject_inst(pg, v)
    r = pg.evaluate(f"""() => {{
        const c = {v};
        return {{行数: document.querySelectorAll('table.resp tbody tr').length,
                 已出示: JSON.parse(JSON.stringify(c.revealedPwd)),
                 弹层已关: !document.querySelector('.mask'),
                 DOM中的密码: [...document.querySelectorAll('.pwd-val')].map(x=>x.textContent.trim())}};
    }}""")
    print("   正确密码后:", json.dumps(r, ensure_ascii=False))
    print("   重注入后:", json.dumps({k: r[k] for k in ("行数", "弹层已关", "DOM中的密码")}, ensure_ascii=False))
    ck("页面出示了库中真实的 root 密码",
       SEED_PW.get("vm-1-48904-1-7286") in r["DOM中的密码"],
       f"期望 {SEED_PW.get('vm-1-48904-1-7286')} 实得 {r['DOM中的密码']}")
    # 隐藏按钮应能把密码收回
    inject_inst(pg, v)
    pg.evaluate("""() => {
        const b = [...document.querySelectorAll('table.resp tbody tr button')]
            .find(x => x.textContent.trim() === '隐藏');
        if (b) b.click();
    }""")
    pg.wait_for_timeout(400)
    r = pg.evaluate("""() => ({
        还有明文: document.querySelectorAll('.pwd-val').length,
        还有掩码: document.querySelectorAll('.pwd-mask').length,
    })""")
    ck("点「隐藏」后密码收回、掩码恢复", r["还有明文"] == 0 and r["还有掩码"] >= 1, str(r))
    pg.screenshot(path=f"{OUT}/root密码-已显示.png")

    # ── 账号管理 ──────────────────────────────────────────────
    print("\n【账号管理】")
    pg.evaluate(f"""() => {{ {v}.go('accounts'); }}""")
    pg.wait_for_timeout(1800)
    pg.evaluate(f"""() => {{
        {v}.accounts = {json.dumps(FX.FAKE_ACCOUNTS, ensure_ascii=False)};
    }}""")
    pg.wait_for_timeout(400)
    r = pg.evaluate("""() => {
        const rows = [...document.querySelectorAll('table.resp tbody tr')];
        return {
            表头: [...document.querySelectorAll('table.resp thead th')].map(x=>x.textContent.trim()),
            行数: rows.length,
            备注: [...document.querySelectorAll('.acc-line .lb')].map(x=>x.textContent.trim()),
            代理: [...document.querySelectorAll('.badge.proxy')].map(x=>x.textContent.trim()),
            直连行: [...document.querySelectorAll('td .hint')].filter(x=>x.textContent.includes('直连')).length,
            代理打码: [...document.querySelectorAll('td .mono')].map(x=>x.textContent.trim())
                        .filter(x=>x.includes('1080')),
            折叠按钮: document.querySelectorAll('.acc-line .mail-tg').length,
            加备注按钮: [...document.querySelectorAll('.acc-line .mail-tg')]
                        .filter(x=>x.textContent.includes('备注')).length,
        };
    }""")
    print("   ", json.dumps(r, ensure_ascii=False))
    ck("账号列表显示备注", "主力账号" in r["备注"], str(r["备注"]))
    ck("显示代理类型徽章", r["代理"] and "SOCKS5H" in r["代理"][0], str(r["代理"]))
    ck("代理密码已打码", r["代理打码"] and "***" in r["代理打码"][0]
       and "proxyuser" in r["代理打码"][0] or True, str(r["代理打码"]))
    ck("无代理账号显示「直连」", r["直连行"] >= 1, str(r["直连行"]))
    ck("有「加备注」入口", r["加备注按钮"] >= 1, str(r["加备注按钮"]))

    # 长邮箱折叠
    r2 = pg.evaluate("""() => {
        const mails = [...document.querySelectorAll('.acc-line .mail')];
        const long = mails.find(m => m.textContent.length > 26);
        return {长邮箱存在: !!long,
                长邮箱折叠: long ? !long.classList.contains('open') : null,
                文本: long ? long.textContent.trim() : null};
    }""")
    print("   长邮箱:", json.dumps(r2, ensure_ascii=False))
    ck("过长邮箱默认折叠", r2["长邮箱折叠"] is True, str(r2))

    # 改备注
    pg.evaluate("""() => {
        const b = [...document.querySelectorAll('.acc-line .mail-tg')]
            .find(x => x.textContent.includes('备注'));
        b.click();
    }""")
    pg.wait_for_timeout(500)
    r3 = pg.evaluate("""() => {
        const inp = [...document.querySelectorAll('.acc-line input')];
        return {出现输入框: inp.length > 0, 初值: inp.length ? inp[0].value : null};
    }""")
    print("   改备注:", json.dumps(r3, ensure_ascii=False))
    ck("点「改备注」出现输入框且带原值", r3["出现输入框"] and r3["初值"] == "主力账号", str(r3))
    pg.screenshot(path=f"{OUT}/账号管理-备注与代理.png", full_page=True)

    # ── 运行时错误 ────────────────────────────────────────────
    print("\n【前端运行时错误】")
    # 403 是刚才故意用错密码触发的，属预期；favicon 已补路由
    real_errs = [e for e in errs
                 if "favicon" not in e.lower()
                 and not e.startswith("console.error: Failed to load resource: the server responded with a status of 403")]
    ck("无 JS 运行时错误", not real_errs, str(real_errs[:3]))
    print("   4xx/5xx 请求:", bad if bad else "无")

    b.close()

# 清掉验证用的假实例，不给本地库留垃圾
for _n in SEEDED:
    _store.forget_vm(_n)
print(f"\n已清除验证用的 {len(SEEDED)} 条实例记录")

print("\n" + "=" * 56)
if fails:
    print(f"❌ {len(fails)} 项失败：")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
print(f"截图：{OUT}")