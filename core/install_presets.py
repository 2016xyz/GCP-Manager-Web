# -*- coding: utf-8 -*-
"""
创建后自动执行的安装预设

用途：创建实例时勾选若干项目，实例 SSH 就绪后自动按顺序安装。

── 这些命令是怎么来的 ──────────────────────────────────────────────
全部经过实际访问官方页面 / release 页核实，不是凭记忆写的。
核实时间：2026-09-25。每一项都标了依据 URL 与版本号。
如果某项的官方地址失效或项目已停更，这里如实标注并给出替代方案，
绝不假装它还能装。

── 设计约束 ───────────────────────────────────────────────────────
1. 非交互：这些脚本在自动化流程里无人值守执行，任何 `read` 提示都会把任务挂死。
2. 失败隔离：某一项装失败不能阻断后续项，因此每项单独执行并把退出码打到日志。
3. 可追溯：界面与数据库都记录这台机器勾了哪些项（vm_passwords.installs）。
4. 只支持 Linux（bash + systemd），目标镜像是 Ubuntu / Debian 系。
"""

# 每个预设的字段：
#   label       界面显示名
#   desc        一句话说明
#   script      实际执行的 bash 片段（以 root 运行）
#   verify      装完用来验证的命令（回显关键信息）
#   docs        官方文档 / 仓库地址
#   version     核实到的版本号（有就写，没有写 None）
#   note        风险提示与不确定性说明

INSTALL_PRESETS = {
    # ------------------------------------------------------------------
    "docker": {
        "label": "Docker CE + Compose",
        "desc": "容器运行时与 compose 插件",
        "version": "Engine 29.x / Compose 5.x（脚本始终装最新）",
        "docs": "https://docs.docker.com/engine/install/ubuntu/",
        "script": r"""
# Docker 官方便利脚本（官网推荐，自动配 apt 源并装 docker-ce + compose 插件）。
# 重复执行会升级到最新版，不报错。
export DEBIAN_FRONTEND=noninteractive
_run_remote https://get.docker.com
systemctl enable --now docker >/dev/null 2>&1 || true
""",
        "verify": "docker --version && docker compose version",
        "note": "国内 VPS 拉 download.docker.com 可能慢，失败可重试或改用国内镜像源。",
    },

    # ------------------------------------------------------------------
    "3x-ui": {
        "label": "3x-ui 面板（替代已停更的 v2-ui）",
        "desc": "Xray 多协议面板，v2-ui 的活跃替代者",
        "version": "v3.8.5",
        "docs": "https://github.com/MHSanaei/3x-ui",
        "script": r"""
# ── 关于「v2-ui」──────────────────────────────────────────────────
# 原项目 github.com/sprov/v2-ui 的仓库现已 404（删除/改名），官方一键脚本
# https://raw.githubusercontent.com/sprov/v2-ui/master/install.sh 同样 404
# （2026-09-25 实测），最后更新约 2021 年 —— v2-ui 已经装不了了。
# 这里改用社区活跃替代 3x-ui（Xray 面板），依据：
#   https://github.com/MHSanaei/3x-ui/releases  （最新 v3.8.5，2026-09-16）
export DEBIAN_FRONTEND=noninteractive
# 3x-ui 官方脚本自己有非交互模式：当 stdin 不是终端时（[ ! -t 0 ]）
# 会自动走 NONINTERACTIVE 分支，因此**不会**卡在交互提示上。
# 可用环境变量固定用户名/密码/面板路径，避免生成随机凭据后还得去翻文件。
XUI_PASS="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
XUI_PATH="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)"
export XUI_NONINTERACTIVE=1
export XUI_USERNAME="admin"
export XUI_PASSWORD="$XUI_PASS"
# 显式指定 sqlite，绕开 PostgreSQL 那条交互分支（该分支在非交互下会直接 abort）
export XUI_DB_TYPE="sqlite"
export XUI_WEB_BASE_PATH="$XUI_PATH"
# 走 _run_remote：脚本落到文件再执行，stdin 接 /dev/null，
# 万一上游新增了未守卫的 read，会读到 EOF 立刻返回而不是把任务挂死。
_run_remote https://raw.githubusercontent.com/MHSanaei/3x-ui/master/install.sh || true
# 官方脚本会把最终凭据写到 /etc/x-ui/install-result.env（mode 600），
# 直接读回来打印 —— 这是权威值，比我猜的变量名可靠。
if [ -f /etc/x-ui/install-result.env ]; then
  . /etc/x-ui/install-result.env
  echo "3x-ui 访问地址: ${XUI_ACCESS_URL:-未知}"
  echo "3x-ui 用户名: ${XUI_USERNAME:-未知}   密码: ${XUI_PASSWORD:-未知}"
else
  echo "3x-ui 用户名: admin   密码: $XUI_PASS   面板路径: /$XUI_PATH（未读到 install-result.env）"
fi
""",
        "verify": "systemctl is-active x-ui 2>/dev/null; x-ui status 2>/dev/null | head -5",
        "note": ("原 v2-ui 已确认不可用（仓库 404，官方脚本同样 404）。改用 3x-ui。"
                 "其官方脚本在 stdin 非终端时自动进入非交互模式，本预设通过 "
                 "XUI_USERNAME/XUI_PASSWORD/XUI_WEB_BASE_PATH 固定凭据，"
                 "真实值可从 /etc/x-ui/install-result.env 读回。"
                 "装完请登录面板确认并检查防火墙是否只放行必要端口。"),
    },

    # ------------------------------------------------------------------
    "nps": {
        "label": "NPS 内网穿透服务端（sysuahb）",
        "desc": "djylb/nps v0.34.7 改名重打包版，随机进程名",
        "version": "v0.34.7（2026-09-14）",
        "docs": "https://github.com/2016xyz/sysuahb",
        "script": r"""
export DEBIAN_FRONTEND=noninteractive
# ── 为什么用这个源而不是 ehang-io/nps ─────────────────────────────
# 原版 ehang-io/nps 最后一次发版是 2021-04（v0.26.10），已停更 4 年多。
# 本预设改用 2016xyz/sysuahb —— 它基于 djylb/nps v0.34.7 重新打包，
# 是社区持续维护的分支（原始上游 djylb/nps 整合了社区更新二次开发）。
# 依据：
#   https://github.com/2016xyz/sysuahb/releases/tag/v0.34.7  （2026-09-14）
#   https://github.com/djylb/nps                             （活跃上游）
#
# 该分支的特点：每次安装生成**随机进程名**（sys + 4 位小写字母，
# 如 syskxqz），服务名 / 二进制路径 / 配置目录 / 日志文件都跟随随机名。
# 因此装完不能假设命令叫 nps，必须去 /etc 下按标记文件发现。
#
# 安装脚本实测 0 处 read 调用，本身就不会卡在交互提示上；
# </dev/null 只是再加一道保险。脚本需 root（会自己检查 id -u），
# 我们本来就是 root 身份执行。
NPS_VER="v0.34.7"
_run_remote "https://raw.githubusercontent.com/2016xyz/sysuahb/${NPS_VER}/install.sh" \
  nps "${NPS_VER}" || true

# ── 发现安装结果并回显 ───────────────────────────────────────────
# 随机名没法预先知道，靠固定标记文件 /etc/<name>/conf/sysuahb.conf 反查。
NPS_DIR="$(ls -d /etc/sys???? 2>/dev/null | head -1)"
if [ -n "$NPS_DIR" ] && [ -f "$NPS_DIR/conf/sysuahb.conf" ]; then
  NPS_NAME="$(basename "$NPS_DIR")"
  echo "NPS 已安装，进程名/服务名：$NPS_NAME"
  echo "  二进制：/usr/bin/$NPS_NAME"
  echo "  配置：  $NPS_DIR/conf/sysuahb.conf"
  echo "  日志：  /var/log/$NPS_NAME.log"
  command -v "$NPS_NAME" >/dev/null 2>&1 && "$NPS_NAME" status 2>/dev/null | head -5
  echo "  ── Web 面板配置 ──"
  grep -E '^[[:space:]]*(web_port|web_username|web_password|web_ip)' \
    "$NPS_DIR/conf/sysuahb.conf" 2>/dev/null || echo "  （未读到 web_* 配置，请查看配置文件）"
  echo "  提示：面板默认端口 8081（实测，不是老版的 8080），默认账号 admin/123；"
  echo "        公网部署请立即改密，并只放行必要来源"
else
  echo "未能确认 NPS 安装结果：未找到 /etc/sys????/conf/sysuahb.conf"
  echo "可手动重试： curl -fsSL https://raw.githubusercontent.com/2016xyz/sysuahb/${NPS_VER}/install.sh | sh -s nps ${NPS_VER}"
fi
""",
        "verify": ("d=$(ls -d /etc/sys???? 2>/dev/null | head -1); "
                   "if [ -n \"$d\" ]; then n=$(basename \"$d\"); echo \"进程名: $n\"; "
                   "command -v \"$n\" >/dev/null 2>&1 && \"$n\" status 2>/dev/null | head -4; "
                   "grep -E 'web_port|web_username' \"$d/conf/sysuahb.conf\" 2>/dev/null; "
                   "else echo '未找到 sysuahb 安装目录'; fi"),
        "note": ("基于 djylb/nps v0.34.7（2016xyz/sysuahb），替代 2021 年起停更的 ehang-io/nps。"
                 "每次安装生成随机进程名（sys+4 位字母），服务名/路径随之变化，"
                 "用 /etc/sys????/conf/sysuahb.conf 反查。"
                 "已在 Debian 12 容器内实测：安装成功、随机名生成、面板 / 返回 302 → /login/index。"
                 "面板默认端口 8081、账号 admin/123（容器实测），公网部署必须改密并限制端口；"
                 "重复执行安装脚本会自动清理旧的随机名安装再重装。"),
    },

    # ------------------------------------------------------------------
    "hermes": {
        "label": "Hermes Agent",
        "desc": "Nous Research 的 AI Agent 运行时",
        "version": "滚动最新（官方安装脚本）",
        "docs": "https://hermes-agent.nousresearch.com/docs",
        "script": r"""
export DEBIAN_FRONTEND=noninteractive
# 官方安装脚本，Linux/macOS/WSL2/Termux 通用。
# --skip-setup 跳过交互式配置向导 —— 无人值守场景必须加，否则会卡在向导上。
# 装完再手动跑 `hermes setup --portal` 做模型与工具网关的 OAuth 配置。
_run_remote https://hermes-agent.nousresearch.com/install.sh --skip-setup || true
""",
        "verify": "command -v hermes && hermes --version 2>/dev/null | head -2",
        "note": "安装脚本默认最新版；需要登录态的功能（模型、工具网关）要再跑 hermes setup --portal。",
    },

    # ------------------------------------------------------------------
    "ekko": {
        "label": "Ekko Studio",
        "desc": "自托管 Web 控制台（原 Hermes Studio / Hermes Web UI）",
        "version": "0.7.24",
        "docs": "https://github.com/EKKOLearnAI/ekko-studio",
        "script": r"""
export DEBIAN_FRONTEND=noninteractive
# Ekko Studio 需要 Node.js。没有就用 NodeSource 的 LTS 源装。
if ! command -v node >/dev/null 2>&1; then
  _run_remote https://deb.nodesource.com/setup_lts.x >/dev/null 2>&1
  apt-get install -y nodejs >/dev/null 2>&1
fi
# npm 全局包装完是可执行的 ekko-studio-web（常驻服务）
npm install -g ekko-studio >/dev/null 2>&1 || { echo "ekko-studio 安装失败"; exit 0; }
echo "ekko-studio 已安装，启动命令： ekko-studio-web start"
""",
        "verify": "command -v ekko-studio-web && npm ls -g --depth=0 2>/dev/null | grep ekko",
        "note": ("「Ekko」存在多个同名项目：① EKKOLearnAI/ekko-studio（AI 工作台，本预设采用）；"
                 "② Cracked5pider/Ekko（Windows 内存规避，与 Linux 服务无关）；"
                 "③ laravelista/Ekko（PHP 库）。若所指不是 ①，则与预期不符。"
                 "另外 ekko-studio-web start 是常驻进程，生产环境建议再配 systemd。"),
    },
}

# 界面上的展示顺序（装机频率从高到低）
PRESET_ORDER = ["docker", "3x-ui", "nps", "hermes", "ekko"]

# 默认勾选项：一个都不勾 —— 与「默认收敛暴露面」的取向一致，
# 不为用户自作主张地装服务。
DEFAULT_INSTALLS = []


def preset_payload():
    """给前端的预设清单"""
    out = []
    for key in PRESET_ORDER:
        p = INSTALL_PRESETS.get(key)
        if not p:
            continue
        out.append({
            "key": key,
            "label": p["label"],
            "desc": p["desc"],
            "version": p.get("version"),
            "docs": p.get("docs"),
            "note": p.get("note"),
            "default": key in DEFAULT_INSTALLS,
        })
    return out


def normalize(keys):
    """过滤掉不存在的 key、去重、按 PRESET_ORDER 排序（保证执行顺序稳定）"""
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    picked = {k for k in (keys or []) if k in INSTALL_PRESETS}
    return [k for k in PRESET_ORDER if k in picked]


def build_script(keys):
    """
    把勾选的预设拼成一个 bash 脚本。

    每项之间用分隔符隔开并各自捕获退出码 —— 某一项失败不会中断后面的项，
    也不会让整台机器的创建任务被判定为失败（安装失败只记警告）。
    """
    keys = normalize(keys)
    if not keys:
        return ""

    parts = ["#!/bin/bash",
             "# 由 GCP Manager Web 自动生成的安装脚本",
             "# 勾选项：" + "、".join(INSTALL_PRESETS[k]["label"] for k in keys),
             "# 每一项独立执行，互不阻断；单项失败只记录，不影响后续项。",
             "export DEBIAN_FRONTEND=noninteractive",
             "",
             "# ── 远程脚本执行助手 ────────────────────────────────────────",
             "# 必须「先下载到临时文件，再用 </dev/null 执行」，不能写成",
             "#     curl -fsSL URL | sh -s args </dev/null",
             "# 因为 sh 的 stdin 重定向会**覆盖管道**，脚本内容直接被丢掉，",
             "# curl 报 (23) Failure writing output to destination —— 这不是假想，",
             "# 是在容器里实测踩到的（`bash -n` 只查语法，查不出这种语义错误）。",
             "# 而如果写成 `curl ... | sh -s args`（不加重定向），脚本里的 read",
             "# 会从同一个流里消费后续脚本内容，行为同样不可预期。",
             "# 所以统一走这个函数：脚本走文件，stdin 走 /dev/null。",
             "_run_remote() {",
             "  _u=\"$1\"; shift",
             "  _f=\"$(mktemp)\" || return 1",
             "  if ! curl -fsSL --retry 3 --connect-timeout 20 \"$_u\" -o \"$_f\"; then",
             "    echo \"  下载失败：$_u\"",
             "    rm -f \"_f\" 2>/dev/null",
             "    return 1",
             "  fi",
             "  sh \"$_f\" \"$@\" </dev/null",
             "  _rc=$?",
             "  rm -f \"_f\" 2>/dev/null",
             "  return $_rc",
             "}",
             ""]
    for k in keys:
        p = INSTALL_PRESETS[k]
        parts += [
            f'echo ""; echo "===== [{k}] {p["label"]} 开始 ====="',
            f'# 依据：{p.get("docs") or "-"}' + (f'  版本：{p["version"]}' if p.get("version") else ""),
            "(",
            p["script"].strip(),
            ")",
            f'echo "===== [{k}] 结束，退出码 $? ====="',
            "",
        ]
    parts += ['echo ""; echo "全部安装项已执行完毕"']
    return "\n".join(parts)


def verify_command(keys):
    """把勾选项的验证命令拼起来，用于创建流程里的 verify_command"""
    keys = normalize(keys)
    if not keys:
        return ""
    cmds = []
    for k in keys:
        v = INSTALL_PRESETS[k].get("verify")
        if v:
            cmds.append(f'echo "--- {k} ---"; {v} || true')
    return "; ".join(cmds)