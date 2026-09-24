#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  GCP Manager Web · 一键安装 / 部署脚本
#
#  用法：
#    直接运行（会自动识别系统、装依赖、建服务、启动）
#        bash install.sh
#
#    指定端口 / 监听地址 / 安装目录
#        PORT=9000 HOST=127.0.0.1 APP_DIR=/opt/gcp-manager-web bash install.sh
#
#    不装 systemd 服务，只把环境装好（适合容器或手动启动）
#        NO_SERVICE=1 bash install.sh
#
#  支持：Ubuntu / Debian / CentOS / RHEL / Rocky / AlmaLinux / Fedora
#  幂等：可重复执行，已存在的环境不会重复安装
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
SERVICE_NAME="${SERVICE_NAME:-gcp-manager-web}"
PY="${PYTHON:-python3}"

C_OK='\033[32m'; C_WARN='\033[33m'; C_ERR='\033[31m'; C_DIM='\033[2m'; C_0='\033[0m'
ok()   { printf "${C_OK}  ✓${C_0} %s\n" "$*"; }
warn() { printf "${C_WARN}  !${C_0} %s\n" "$*"; }
info() { printf "  ${C_DIM}·${C_0} %s\n" "$*"; }
die()  { printf "${C_ERR}  ✗${C_0} %s\n" "$*" >&2; exit 1; }
step() { printf "\n${C_DIM}── %s${C_0}\n" "$*"; }

# 依赖安装的总时长上限（秒）。超过即认为该源「过慢」，
# 杀掉并自动切换下一个镜像源。国内直连 PyPI 经常能连上但极慢，
# 只靠连通性探测判断不出来，必须用耗时兜底。
PIP_TIMEOUT="${PIP_TIMEOUT:-300}"

printf "\n===========================================\n"
printf "  GCP Manager Web · 安装程序\n"
printf "  目录 %s  端口 %s\n" "$APP_DIR" "$PORT"
printf "===========================================\n"

# ── 0. 前置检查 ────────────────────────────────────────────────────────────
step "检查运行环境"

[ -f "$APP_DIR/app.py" ] || die "在 $APP_DIR 找不到 app.py，请在项目根目录运行本脚本"

if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi

command -v "$PY" >/dev/null 2>&1 || die "未找到 $PY，请先安装 Python 3.10+"

PYVER="$($PY -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
PYMAJ="${PYVER%%.*}"; PYMIN="${PYVER##*.}"
if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 10 ]; }; then
  die "需要 Python 3.10+，当前 $PYVER"
fi
ok "Python $PYVER"

# ── 1. 系统包（venv 常常要单独装）────────────────────────────────────────────
step "检查系统依赖"

need_pkgs=()
$PY -c "import venv" >/dev/null 2>&1 || need_pkgs+=("python3-venv")

if [ "${#need_pkgs[@]}" -gt 0 ]; then
  if command -v apt-get >/dev/null 2>&1; then
    warn "安装系统包: ${need_pkgs[*]}"
    $SUDO apt-get update -qq
    $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need_pkgs[@]}"
  elif command -v dnf >/dev/null 2>&1; then
    warn "安装系统包: ${need_pkgs[*]}"
    $SUDO dnf install -y -q "${need_pkgs[@]}"
  elif command -v yum >/dev/null 2>&1; then
    warn "安装系统包: ${need_pkgs[*]}"
    $SUDO yum install -y -q "${need_pkgs[@]}"
  else
    die "缺少 ${need_pkgs[*]}，且不认识包管理器，请手动安装"
  fi
  ok "系统包已安装"
else
  ok "venv 模块可用"
fi

# ── 2. 虚拟环境 ────────────────────────────────────────────────────────────
step "准备 Python 虚拟环境"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$PY" -m venv "$VENV_DIR" || die "创建虚拟环境失败"
  ok "已创建 $VENV_DIR"
else
  ok "复用已有 $VENV_DIR"
fi

VPY="$VENV_DIR/bin/python"

# ── 3. 安装依赖（自动挑镜像源 + 失败自动换源重试）─────────────────────────
step "安装 Python 依赖"

PIP_BASE=(-q --disable-pip-version-check --timeout 20 --retries 2)
TSINGHUA="https://pypi.tuna.tsinghua.edu.cn/simple"
ALIYUN="https://mirrors.aliyun.com/pypi/simple/"

"$VPY" -m pip install "${PIP_BASE[@]}" --upgrade pip >/dev/null 2>&1 || true

pip_install() {   # $1 = 索引源 URL（空则用 pip 默认源）
  local extra=()
  [ -n "${1:-}" ] && extra=(-i "$1")
  # 用 timeout 兜底：能连上但极慢的源会被掐掉，交给下一个镜像
  timeout "$PIP_TIMEOUT" \
    "$VPY" -m pip install "${PIP_BASE[@]}" "${extra[@]}" -r "$APP_DIR/requirements.txt" --progress-bar off
}

installed=0
# 允许用 PIP_INDEX_URL 直接指定源
if [ -n "${PIP_INDEX_URL:-}" ]; then
  warn "使用指定的 PIP_INDEX_URL: $PIP_INDEX_URL"
  pip_install "$PIP_INDEX_URL" && installed=1
else
  # 先试默认源（PyPI）。网络抖动时连通性探测不可靠，
  # 所以不靠「探测」决定用哪个源，而是直接装，装不动/过慢再换源。
  info "尝试默认源 PyPI（最长 ${PIP_TIMEOUT}s）…"
  pip_install "" && installed=1
fi

if [ "$installed" -ne 1 ]; then
  warn "默认源失败或过慢，切换清华镜像重试…"
  pip_install "$TSINGHUA" && installed=1
fi

if [ "$installed" -ne 1 ]; then
  warn "清华镜像仍失败，切换阿里云镜像重试…"
  pip_install "$ALIYUN" && installed=1
fi

[ "$installed" -eq 1 ] || die "依赖安装失败（已尝试 PyPI / 清华 / 阿里云），请检查网络"
ok "依赖安装完成"

# ── 4. 数据目录 ────────────────────────────────────────────────────────────
step "准备数据目录"

mkdir -p "$APP_DIR/data/keys"
chmod 700 "$APP_DIR/data" 2>/dev/null || true
ok "$APP_DIR/data （已限制为 700，存放服务账号与密码）"

# 首次启动前清掉可能的旧库，保证会生成新的初始管理员
if [ ! -f "$APP_DIR/data/gcp_web.db" ]; then
  ok "首次安装，首次启动会生成随机管理员密码"
else
  warn "已存在 data/gcp_web.db，保留现有账号数据"
fi

# ── 5. systemd 服务 ────────────────────────────────────────────────────────
if [ "${NO_SERVICE:-0}" = "1" ]; then
  warn "NO_SERVICE=1，跳过 systemd 配置"
elif ! command -v systemctl >/dev/null 2>&1; then
  warn "没有 systemd，跳过服务配置（请用 run.sh 手动启动）"
else
  step "配置 systemd 服务"

  UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
  $SUDO tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=GCP Manager Web
Documentation=https://github.com/2016xyz/GCP-Manager-Web
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=GCPWEB_DATA_DIR=$APP_DIR/data
Environment=PORT=$PORT
ExecStart=$VPY -m uvicorn app:app --host $HOST --port $PORT --log-level warning
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# 基本加固：服务只需要写自己的数据目录
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$APP_DIR/data

[Install]
WantedBy=multi-user.target
EOF

  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
  $SUDO systemctl restart "$SERVICE_NAME"
  sleep 3
  if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "服务已启动并设为开机自启"
  else
    warn "服务未正常启动，查看日志：journalctl -u $SERVICE_NAME -n 50 --no-pager"
  fi
fi

# ── 6. 收尾信息 ────────────────────────────────────────────────────────────
step "完成"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$IP" ] || IP="127.0.0.1"

printf "\n"
printf "  控制台   ${C_OK}http://%s:%s/${C_0}\n" "$IP" "$PORT"
printf "  本机访问 http://127.0.0.1:%s/\n" "$PORT"
printf "  API 文档 http://127.0.0.1:%s/docs\n" "$PORT"
printf "  数据目录 %s/data\n" "$APP_DIR"

if [ -f "$APP_DIR/data/INITIAL_ADMIN.txt" ]; then
  printf "\n  ${C_WARN}初始管理员账号${C_0}\n"
  sed 's/^/    /' "$APP_DIR/data/INITIAL_ADMIN.txt"
  printf "\n  ${C_DIM}登录后请立即改密，并删除该文件${C_0}\n"
else
  printf "\n  ${C_DIM}管理员密码文件：%s/data/INITIAL_ADMIN.txt${C_0}\n" "$APP_DIR"
fi

if [ "${NO_SERVICE:-0}" != "1" ] && command -v systemctl >/dev/null 2>&1; then
  printf "\n  常用命令\n"
  printf "    查看状态  systemctl status %s\n" "$SERVICE_NAME"
  printf "    实时日志  journalctl -u %s -f\n" "$SERVICE_NAME"
  printf "    重启      systemctl restart %s\n" "$SERVICE_NAME"
fi

printf "\n  ${C_ERR}安全提醒${C_0}\n"
printf "    本服务自带登录鉴权，但设计前提是内网/本机使用。\n"
printf "    若要暴露公网，请在前面加反向代理 + TLS + IP 白名单（见 README）。\n"
printf "\n"
