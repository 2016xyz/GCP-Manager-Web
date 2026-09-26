#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  GCP Manager Web · 一键安装 / 部署脚本
#
#  用法：
#    一键（自动下载源码 + 装环境 + 建服务 + 启动）
#        curl -fsSL https://raw.githubusercontent.com/2016xyz/GCP-Manager-Web/main/install.sh | bash
#
#    在已克隆的仓库里运行
#        bash install.sh
#
#    指定端口 / 监听地址 / 安装目录
#        PORT=9000 HOST=127.0.0.1 APP_DIR=/opt/gcp-manager-web bash install.sh
#
#    不装 systemd 服务，只把环境装好（适合容器或手动启动）
#        NO_SERVICE=1 bash install.sh
#
#    只下载源码不安装
#        ONLY_FETCH=1 bash install.sh
#
#  支持：Ubuntu / Debian / CentOS / RHEL / Rocky / AlmaLinux / Fedora
#  幂等：可重复执行，已存在的环境不会重复安装，不会覆盖既有数据
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/2016xyz/GCP-Manager-Web.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_TARBALL="${REPO_TARBALL:-https://github.com/2016xyz/GCP-Manager-Web/archive/refs/heads/${REPO_BRANCH}.tar.gz}"

# 关键：管道模式（curl | bash）下 BASH_SOURCE[0] 未定义，
# 而 set -u 会让直接引用直接报错退出。必须带默认值取。
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
  SRC_DIR="$(cd "$(dirname "$SELF")" && pwd)"
else
  SRC_DIR=""            # 管道模式：没有本地源码，待会下载
fi

PORT_WAS_SET="${PORT:+yes}"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
# 安装目录解析优先级：APP_DIR 环境变量 > 本地源码所在目录 > 默认绝对路径。
#
# ★ 为什么管道模式要用绝对路径：
#   原来是 `${SRC_DIR:-$PWD/gcp-manager-web}` —— 管道模式（README 推荐的一行命令
#   `curl … | bash`）下 SRC_DIR 为空，于是装到「执行时的当前目录」下。
#   用户在 /root 执行 → 装到 /root/gcp-manager-web；而 README 的升级指引写的是
#   `cd /opt/gcp-manager-web && bash update.sh` —— 路径对不上，升级时直接
#   `cd: 没有那个文件或目录`。安装路径依赖「你在哪执行」，对用户不可预测。
#   现在管道模式固定装到绝对路径：root 用 /opt/gcp-manager-web（与文档一致），
#   非 root 用 $HOME/gcp-manager-web（/opt 通常不可写）。
if [ -n "${APP_DIR:-}" ]; then
  APP_DIR="$APP_DIR"
elif [ -n "$SRC_DIR" ]; then
  APP_DIR="$SRC_DIR"                      # 在克隆的仓库里就地安装
elif [ "$(id -u)" = "0" ]; then
  APP_DIR="/opt/gcp-manager-web"
else
  APP_DIR="${HOME:-$PWD}/gcp-manager-web"
fi
# 落成绝对路径，避免后续 cd 到别处后相对路径失效
case "$APP_DIR" in
  /*) : ;;
  *)  APP_DIR="$PWD/$APP_DIR" ;;
esac
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
SERVICE_NAME="${SERVICE_NAME:-gcp-manager-web}"
PY="${PYTHON:-python3}"
# 安装路径标记文件：装完写一份，之后「忘了装在哪」可直接读它定位
INSTALL_MARKER="${INSTALL_MARKER:-/etc/gcp-manager-web.path}"

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
printf "  目录 %s\n" "$APP_DIR"
if [ -n "$PORT_WAS_SET" ]; then
  printf "  端口 %s ${C_DIM}(来自环境变量 PORT，非默认 8000)${C_0}\n" "$PORT"
else
  printf "  端口 %s\n" "$PORT"
fi
printf "===========================================\n"

# ── 0. 前置检查 + 自举下载源码 ─────────────────────────────────────────────
step "检查运行环境"

fetch_repo() {
  # 优先 git clone（能带出 .git 便于后续升级）；无 git 则退到 tarball
  mkdir -p "$(dirname "$APP_DIR")"
  if command -v git >/dev/null 2>&1; then
    info "git clone --depth 1 $REPO_URL"
    if git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR" 2>&1 | tail -2; then
      return 0
    fi
    rm -rf "$APP_DIR" 2>/dev/null || true
    warn "git clone 失败，改用 tarball"
  fi

  command -v curl >/dev/null 2>&1 || die "需要 git 或 curl 之一下载源码"
  local tmp
  tmp="$(mktemp -d)"
  info "下载 $REPO_TARBALL"
  curl -fsSL "$REPO_TARBALL" -o "$tmp/src.tar.gz" || { rm -rf "$tmp"; return 1; }
  tar xzf "$tmp/src.tar.gz" -C "$tmp" || { rm -rf "$tmp"; return 1; }
  local inner
  inner="$(find "$tmp" -maxdepth 1 -mindepth 1 -type d | head -1)"
  [ -n "$inner" ] || { rm -rf "$tmp"; return 1; }
  mkdir -p "$APP_DIR"
  cp -a "$inner"/. "$APP_DIR"/
  rm -rf "$tmp"
  return 0
}

if [ ! -f "$APP_DIR/app.py" ]; then
  # 本地源码目录直接就地用；否则下载
  if [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/app.py" ] && [ "$SRC_DIR" != "$APP_DIR" ]; then
    info "从 $SRC_DIR 复制源码到 $APP_DIR"
    mkdir -p "$APP_DIR"
    cp -a "$SRC_DIR"/. "$APP_DIR"/
  else
    warn "在 $APP_DIR 未找到 app.py，开始下载项目源码…"
    fetch_repo || die "下载源码失败，请检查网络或手动 git clone 后重试"
  fi
fi

[ -f "$APP_DIR/app.py" ] || die "在 $APP_DIR 仍找不到 app.py，安装中止"
ok "源码就绪：$APP_DIR"

if [ "${ONLY_FETCH:-0}" = "1" ]; then
  printf "\n  ${C_OK}ONLY_FETCH=1${C_0}：源码已下载到 %s，未执行安装\n\n" "$APP_DIR"
  exit 0
fi

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

# 统一的包安装入口（apt / dnf / yum）。返回非 0 表示装不上或没有包管理器。
#
# ★ 必须用 `env` 传 DEBIAN_FRONTEND，不能写成
#       $SUDO DEBIAN_FRONTEND=noninteractive apt-get install …
#   原因：bash 只在「字面量出现在命令词位置」时才把 VAR=value 当赋值前缀。
#   这里命令词位置是 `$SUDO`（展开出来的），所以 $SUDO 为空（root 时就是这样）
#   会让 bash 把 DEBIAN_FRONTEND=noninteractive 当成**命令名**，报
#       DEBIAN_FRONTEND=noninteractive: command not found
#   于是安装静默失败。实测 root 下的 debian:12 必现。
#   包一层 env 后，两种情形都正确。
pkg_install() {
  [ "$#" -gt 0 ] || return 0
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -qq >/dev/null 2>&1 || true
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null 2>&1
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y -q "$@" >/dev/null 2>&1
  elif command -v yum >/dev/null 2>&1; then
    $SUDO yum install -y -q "$@" >/dev/null 2>&1
  else
    return 1
  fi
}

# ★ 关键：判断「能不能建 venv」不能只看 `import venv`。
#   Debian/Ubuntu/Kali 上 `import venv` **会成功**，但真正建环境时依赖
#   ensurepip（它由 python3-venv 包提供）。只判 venv 会误判为「可用」，
#   于是跳过装包，随后 `python -m venv` 直接失败，报的却是
#     "ensurepip is not available … apt install python3.11-venv"
#   —— 明明脚本自己有能力装，却没装。实测：debian:12 必现。
need_pkgs=()
$PY -c "import venv"      >/dev/null 2>&1 || need_pkgs+=("python3-venv")
$PY -c "import ensurepip" >/dev/null 2>&1 || need_pkgs+=("python3-venv")
# 去重
if [ "${#need_pkgs[@]}" -gt 1 ]; then
  need_pkgs=("$(printf '%s\n' "${need_pkgs[@]}" | sort -u | tr '\n' ' ' | sed 's/ $//')")
fi

if [ "${#need_pkgs[@]}" -gt 0 ]; then
  warn "安装系统包: ${need_pkgs[*]}"
  pkg_install "${need_pkgs[@]}" || die "缺少 ${need_pkgs[*]}，且安装失败，请手动安装"
  ok "系统包已安装"
else
  ok "venv 模块可用"
fi

# ── 2. 虚拟环境 ────────────────────────────────────────────────────────────
step "准备 Python 虚拟环境"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  if ! "$PY" -m venv "$VENV_DIR" 2>/tmp/.gcpweb_venv_err; then
    # 兜底：某些发行版把 venv/ensurepip 拆成带版本号的包
    # （如 python3.11-venv），上面那个通用名装不上时再按版本号试一次。
    _vmaj="$($PY -c 'import sys;print("python%d.%d-venv" % sys.version_info[:2])' 2>/dev/null || true)"
    warn "创建虚拟环境失败，尝试安装 ${_vmaj:-python3-venv} 后重试…"
    if [ -n "$_vmaj" ]; then
      pkg_install "$_vmaj" || pkg_install python3-venv || true
    else
      pkg_install python3-venv || true
    fi
    rm -rf "$VENV_DIR"
    if ! "$PY" -m venv "$VENV_DIR"; then
      printf "\n" >&2
      sed 's/^/    /' /tmp/.gcpweb_venv_err >&2 2>/dev/null || true
      die "创建虚拟环境失败（已尝试自动安装 python3-venv；请手动执行 apt install python3-venv 后重跑）"
    fi
  fi
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

  # ★ 服务降权：默认不要让控制台以 root 跑。
  # 原因：这个进程能读 data/ 下的管理员密码、数据库、GCP 服务账号私钥，
  # 还能被「读公钥」这类接口触碰文件系统。以 root 运行意味着一旦出现
  # 任意文件读取/写入类缺陷，影响面直接是整台机器（/etc/shadow、SSH 私钥）。
  # 容器部署本来就用非 root（见 Dockerfile 的 appuser），这里保持一致。
  SVC_USER="${SERVICE_USER:-gcpweb}"
  if [ "$SVC_USER" != "root" ] && id "$SVC_USER" >/dev/null 2>&1; then
    ok "服务将使用既有用户 $SVC_USER"
  elif [ "$SVC_USER" != "root" ]; then
    if $SUDO useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin \
                    --no-create-home "$SVC_USER" >/dev/null 2>&1; then
      ok "已创建系统用户 $SVC_USER（无登录 shell）"
    else
      warn "创建用户 $SVC_USER 失败，将回退到 root 运行（建议手工创建后重装）"
      SVC_USER="root"
    fi
  fi
  # 数据目录与代码目录都要让该用户可读写：data 下要写库、密码与密钥
  if [ "$SVC_USER" != "root" ]; then
    $SUDO chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR/data" 2>/dev/null || true
    $SUDO chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR/.venv" 2>/dev/null || true
    $SUDO chown "$SVC_USER":"$SVC_USER" "$APP_DIR" 2>/dev/null || true
    $SUDO chmod 700 "$APP_DIR/data" 2>/dev/null || true
  fi
  if [ "$SVC_USER" = "root" ]; then
    warn "服务以 root 运行 —— 生产环境建议设 SERVICE_USER=<专用用户> 重装"
  fi

  UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
  $SUDO tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=GCP Manager Web
Documentation=https://github.com/2016xyz/GCP-Manager-Web
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
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
ProtectHome=read-only
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
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

# 记录安装路径，解决「装完过一阵忘了装在哪」。
# 之前安装路径取决于执行时的当前目录，而 README 的升级指引写死了
# /opt/gcp-manager-web —— 用户按文档升级会直接 `cd: 没有那个文件或目录`。
# 写一份标记后，update.sh 能在找不到 app.py 时自动定位到这里。
if [ "$(id -u)" = "0" ] && [ -n "${INSTALL_MARKER:-}" ]; then
  if printf '%s\n' "$APP_DIR" > "$INSTALL_MARKER" 2>/dev/null; then
    chmod 644 "$INSTALL_MARKER" 2>/dev/null || true
    info "安装路径已记录到 $INSTALL_MARKER（忘了装在哪可 cat 它）"
  fi
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$IP" ] || IP="127.0.0.1"

printf "\n"
APP_VER="$(grep -m1 '^VERSION = ' "$APP_DIR/core/version.py" 2>/dev/null | cut -d'"' -f2 || true)"
printf "  版本     ${C_OK}v%s${C_0}\n" "${APP_VER:-unknown}"
printf "  仓库     ${C_DIM}https://github.com/2016xyz/GCP-Manager-Web${C_0}\n"
printf "  安装目录 ${C_OK}%s${C_0}\n" "$APP_DIR"
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
  printf "    升级      bash %s/update.sh    ${C_DIM}(更新到最新版，不动 data/)\n" "$APP_DIR"
fi

printf "\n  ${C_ERR}安全提醒${C_0}\n"
printf "    本服务自带登录鉴权，但设计前提是内网/本机使用。\n"
printf "    若要暴露公网，请在前面加反向代理 + TLS + IP 白名单（见 README）。\n"
printf "\n"
