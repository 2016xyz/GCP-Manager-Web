#!/usr/bin/env python3
"""
登录页端到端验证夹具（仅测试用，不属于产品代码）

⚠️ 安全警告：本脚本会暴露 /__probe/captcha/<id> 读出验证码明文。
   它只能在本机、短时间、测试用途下运行 —— 因此本脚本**强制只监听
   127.0.0.1**，不接受外部地址，也不应部署到任何服务器上。
   产品代码 app.py 里没有任何 __probe 路由（可自行 grep 确认）。

为什么需要它：
  产品的验证码是进程内存里的，浏览器端无法读到明文，
  而本机浏览器会话在多次工具调用之间会被重置，无法完成
  「先看验证码 → 再输入 → 再提交」这种多步交互。

  因此这里起一个只用于测试的实例：
    · 复用产品自身的 app.py（一行不改）
    · 额外挂一个 /__probe/captcha/<id> 路由，把验证码明文读出来

  这样就能在浏览器的一次 evaluate 调用里完成整个登录流程，
  从而真正验证「登录页 JS → 后端接口」这条链路是通的。

用法：
  python3 tools/ui_login_probe.py [port]     # 仅 127.0.0.1
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# 用独立数据目录，避免污染正在运行的主实例
os.environ.setdefault("GCPWEB_DATA_DIR", os.path.join(BASE, "data"))

HOST = "127.0.0.1"        # 强制本机，不给任何改成 0.0.0.0 的机会

import app as appmod          # noqa: E402
from core import auth as auth_mod  # noqa: E402

# 把夹具路由加进白名单（否则会被登录中间件重定向到 /login，
# 而我们要验证的正是「未登录时」的登录流程）
appmod.PUBLIC_PREFIXES = tuple(appmod.PUBLIC_PREFIXES) + ("/__probe/",)


@appmod.app.get("/__probe/captcha/{cid}")
def _probe_captcha(cid: str):
    """⚠️ 仅测试夹具：读出验证码明文（产品代码里没有这个接口）"""
    item = auth_mod.captcha_store._items.get(cid) or {}
    return {"cid": cid, "code": item.get("code"), "found": bool(item)}


if __name__ == "__main__":
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    bar = "!" * 68
    print(bar)
    print("!! 测试夹具启动 —— 会暴露验证码明文，切勿用于生产环境")
    print(f"!! 已强制只监听 {HOST}:{port}（不可从外部访问）")
    print(f"!! 验证码探针： http://{HOST}:{port}/__probe/captcha/<id>")
    print(bar)
    uvicorn.run(appmod.app, host=HOST, port=port, log_level="warning")
