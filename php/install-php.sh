#!/usr/bin/env bash
# =============================================================================
# GCP Manager Web · PHP 版 一键安装（裸机 / VPS）
#
#   curl -fsSL https://raw.githubusercontent.com/2016xyz/GCP-Manager-Web/main/php/install-php.sh | bash
#
# 装完在 http://<你的IP>:8080/login 打开控制台。
#
# ★ 宝塔面板用户不要用这个脚本 —— 用 php/bt/bt-install.sh + bt/GUIDE.md，
#   宝塔的 Nginx/PHP-FPM 由面板管理，本脚本的 systemd 服务会与它打架。
#
# 安装目录（升级时要用）：
#   APP_DIR 环境变量 > 本地源码目录（在克隆的仓库里就地安装）> 绝对路径默认
#   root → /opt/gcp-manager-php；非 root → $HOME/gcp-manager-php
#   路径同时写入 /etc/gcp-php-web.path（忘了装在哪就 cat 它）
#
# 环境变量：
#   APP_DIR=…      安装目录        PORT=…        监听端口（默认 8080）
#   HOST=…         监听地址（默认 0.0.0.0）         NO_SERVICE=1  不注册 systemd
#   ONLY_FETCH=1   只下载源码不安装  SKIP_PKG=1    不自动装系统包
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/2016xyz/GCP-Manager-Web.git}"
APP_DIR="${APP_DIR:-}"
PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"
NO_SERVICE="${NO_SERVICE:-0}"
ONLY_FETCH="${ONLY_FETCH:-0}"
SKIP_PKG="${SKIP_PKG:-0}"
INSTALL_MARKER="${INSTALL_MARKER:-/etc/gcp-php-web.path}"
MIN_PHP="8.0"

B="\033[1m"; G="\033[32m"; Y="\033[33m"; R="\033[31m"; N="\033[0m"
ok()   { printf "  ${G}✓${N} %s\n" "$*"; }
warn() { printf "  ${Y}!${N} %s\n" "$*"; }
err()  { printf "  ${R}✗${N} %s\n" "$*" >&2; }
step() { printf "\n${B}── %s${N}\n" "$*"; }
die()  { err "$*"; exit 1; }
[ "$(id -u)" -eq 0 ] && SUDO="" || SUDO="sudo"

printf "\n${B}═══════════════════════════════════════════${N}\n"
printf "${B}  GCP Manager Web · PHP 版 安装程序${N}\n"
printf "${B}═══════════════════════════════════════════${N}\n"

# ── 0. 定位安装目录 ─────────────────────────────────────────────────────────
SELF="${BASH_SOURCE[0]:-}"
SRC_DIR=""
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
  SRC_DIR="$(cd "$(dirname "$SELF")" && pwd)"
  # install-php.sh 在仓库的 php/ 下，源码根就是它所在目录
  [ -f "$SRC_DIR/public/index.php" ] || SRC_DIR=""
fi

if [ -n "$APP_DIR" ]; then
  :                                   # 显式指定，最高优先级
elif [ -n "$SRC_DIR" ]; then
  APP_DIR="$SRC_DIR"                  # 在克隆的仓库里就地安装
elif [ "$(id -u)" -eq 0 ]; then
  APP_DIR="/opt/gcp-manager-php"      # 绝对路径默认（管道模式下也能预测）
else
  APP_DIR="${HOME:-$PWD}/gcp-manager-php"
fi
case "$APP_DIR" in
  /*) : ;;
  *)  APP_DIR="$PWD/$APP_DIR" ;;
esac

printf "  ${B}安装目录${N} %s\n" "$APP_DIR"
printf "  ${B}端口${N}     %s（监听 %s）\n" "$PORT" "$HOST"

# ── 1. 下载源码 ─────────────────────────────────────────────────────────────
step "获取源码"
if [ -f "$APP_DIR/public/index.php" ]; then
  ok "已存在源码，跳过下载（就地安装 / 重复执行）"
else
  command -v git >/dev/null 2>&1 || command -v curl >/dev/null 2>&1 \
    || die "需要 git 或 curl 之一下载源码"
  mkdir -p "$(dirname "$APP_DIR")"
  if command -v git >/dev/null 2>&1; then
    ok "git clone --depth 1"
    git clone --depth 1 "$REPO_URL" "$APP_DIR.tmp" >/dev/null 2>&1 || die "git clone 失败"
    # 仓库根下的 php/ 才是应用目录
    if [ -f "$APP_DIR.tmp/php/public/index.php" ]; then
      mv "$APP_DIR.tmp/php" "$APP_DIR"
      rm -rf "$APP_DIR.tmp"
    else
      mv "$APP_DIR.tmp" "$APP_DIR"
    fi
  else
    ok "curl 下载 tarball"
    mkdir -p "$APP_DIR.tmp"
    curl -fsSL "${REPO_URL%.git}/archive/refs/heads/main.tar.gz" \
      | tar xz -C "$APP_DIR.tmp" --strip-components=1 || die "下载源码失败"
    if [ -f "$APP_DIR.tmp/php/public/index.php" ]; then
      mv "$APP_DIR.tmp/php" "$APP_DIR"
      rm -rf "$APP_DIR.tmp"
    else
      mv "$APP_DIR.tmp" "$APP_DIR"
    fi
  fi
  ok "源码就绪：$APP_DIR"
fi

if [ "$ONLY_FETCH" = "1" ]; then
  printf "\n  已按要求只下载源码（ONLY_FETCH=1）：%s\n\n" "$APP_DIR"
  exit 0
fi

# ── 2. PHP 与扩展 ───────────────────────────────────────────────────────────
step "检查 PHP 环境"
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

have_php() {
  command -v php >/dev/null 2>&1 || return 1
  v="$(php -r 'echo PHP_VERSION;' 2>/dev/null || echo 0)"
  [ "$(printf '%s\n%s\n' "$MIN_PHP" "$v" | sort -V | head -n1)" = "$MIN_PHP" ]
}

if ! have_php; then
  if [ "$SKIP_PKG" = "1" ]; then
    die "未找到 PHP ≥ $MIN_PHP（已设 SKIP_PKG=1，请自行安装）"
  fi
  warn "未找到 PHP ≥ $MIN_PHP，尝试自动安装…"
  if command -v apt-get >/dev/null 2>&1; then
    pkg_install php-cli php-sqlite3 php-curl php-mbstring php-json php-xml php-zip php-sockets \
      || die "apt 安装 PHP 失败，请手动安装 PHP ≥ $MIN_PHP 后重跑"
  else
    (pkg_install php-cli php-pdo php-openssl php-curl php-mbstring php-json php-sockets \
      || pkg_install php-cli php php-sqlite3 php-mbstring) \
      || die "包管理器安装 PHP 失败，请手动安装 PHP ≥ $MIN_PHP 后重跑"
  fi
  ok "PHP 已安装：$(php -v 2>/dev/null | head -n1)"
fi
PHP_BIN="$(command -v php)"
PHP_VER="$(php -r 'echo PHP_VERSION;')"
[ "$(printf '%s\n%s\n' "$MIN_PHP" "$PHP_VER" | sort -V | head -n1)" = "$MIN_PHP" ] \
  || die "PHP 版本过低：$PHP_VER（需要 ≥ $MIN_PHP）"
ok "PHP $PHP_VER（$PHP_BIN）"

# 扩展体检。缺关键扩展时尝试自动装一次，仍缺则报错并给出包名。
missing_req=""
for e in pdo_sqlite sqlite3 openssl curl mbstring json zlib; do
  php -m 2>/dev/null | grep -qix "$e" || missing_req="$missing_req $e"
done
if [ -n "$missing_req" ]; then
  warn "缺少扩展：$missing_req —— 尝试自动安装…"
  if command -v apt-get >/dev/null 2>&1; then
    for e in $missing_req; do
      case "$e" in
        pdo_sqlite|sqlite3) pkg_install php-sqlite3 || true ;;
        openssl)  pkg_install php-openssl || true ;;
        curl)     pkg_install php-curl || true ;;
        mbstring) pkg_install php-mbstring || true ;;
        json)     pkg_install php-json || true ;;
        zlib)     pkg_install php-zip || true ;;
      esac
    done
  else
    pkg_install php-pdo php-sqlite3 || true
  fi
  for e in pdo_sqlite sqlite3 openssl curl mbstring json zlib; do
    php -m 2>/dev/null | grep -qix "$e" || die "仍缺少必需扩展 $e，请手动安装后重跑"
  done
fi
ok "必需扩展齐全（pdo_sqlite/sqlite3/openssl/curl/mbstring/json/zlib）"
php -m 2>/dev/null | grep -qix sockets \
  && ok "sockets 可用 → WebSocket 实时日志可用" \
  || warn "缺 sockets → 实时日志自动退化为轮询（不影响功能）"

# 被禁用的关键函数（有些发行版/面板默认禁）
_dis="$( { php -i 2>/dev/null | grep -i '^disable_functions' || true; } | head -n1 | cut -d'>' -f2- | tr -d ' ')"
_blk=""
for fn in proc_open proc_get_status escapeshellarg; do
  printf '%s' ",$_dis," | grep -q ",$fn," && _blk="$_blk $fn"
done
[ -n "$_blk" ] && warn "php.ini 里禁用了：$_blk（后台任务/SSH 执行会失败，请放行）" \
               || ok "关键函数未被禁用"

# ── 3. 数据目录 ─────────────────────────────────────────────────────────────
step "准备数据目录"
mkdir -p "$APP_DIR/data" "$APP_DIR/data/keys"
chmod 700 "$APP_DIR/data" "$APP_DIR/data/keys"
ok "数据目录 $APP_DIR/data（700）"

# ── 4. 初始化 ───────────────────────────────────────────────────────────────
step "初始化数据库与管理员"
if [ -f "$APP_DIR/bin/init.php" ]; then
  if (cd "$APP_DIR" && "$PHP_BIN" bin/init.php); then
    ok "初始化完成"
  else
    warn "初始化脚本返回非 0（若数据库已存在属正常）"
  fi
else
  warn "未找到 bin/init.php（首次访问时会自动建库）"
fi
if [ -f "$APP_DIR/data/INITIAL_ADMIN.txt" ]; then
  chmod 600 "$APP_DIR/data/INITIAL_ADMIN.txt"
fi

# ── 5. systemd 服务 ─────────────────────────────────────────────────────────
if [ "$NO_SERVICE" = "1" ]; then
  step "跳过 systemd 注册（NO_SERVICE=1）"
  warn "手动启动： cd $APP_DIR && php -S $HOST:$PORT -t public public/router.php"
else
  step "注册 systemd 服务"
  SVC=gcp-php-web
  if command -v systemctl >/dev/null 2>&1; then
    # 降权：优先建一个 nologin 系统账号（与 Python 版 install.sh 同一取舍）
    SVC_USER="${SERVICE_USER:-gcpweb}"
    if ! id "$SVC_USER" >/dev/null 2>&1; then
      $SUDO useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER" 2>/dev/null \
        || SVC_USER=root
    fi
    $SUDO chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR/data" 2>/dev/null || true

    $SUDO tee "/etc/systemd/system/$SVC.service" >/dev/null <<UNIT
[Unit]
Description=GCP Manager Web (PHP)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$APP_DIR
ExecStart=$PHP_BIN -S $HOST:$PORT -t public public/router.php
Restart=on-failure
RestartSec=3
# 加固（与 Python 版 install.sh 保持同一取舍）
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
UNIT
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now "$SVC" >/dev/null 2>&1 || warn "服务启动失败，请查看 journalctl -u $SVC"
    sleep 2
    if $SUDO systemctl is-active --quiet "$SVC"; then
      ok "服务已启动并设为开机自启（$SVC，运行用户 $SVC_USER）"
    else
      warn "服务未处于 active，排查： systemctl status $SVC"
    fi
  else
    warn "没有 systemd，跳过；手动启动： cd $APP_DIR && php -S $HOST:$PORT -t public public/router.php"
  fi
fi

# ── 6. 记录路径 ─────────────────────────────────────────────────────────────
printf '%s\n' "$APP_DIR" > "$INSTALL_MARKER" 2>/dev/null \
  && ok "安装路径已记录到 $INSTALL_MARKER（忘了装在哪可 cat 它）" || true

# ── 收尾 ────────────────────────────────────────────────────────────────────
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
printf "\n${B}═══════════════════════════════════════════${N}\n"
printf "  ${G}${B}安装完成${N}\n"
printf "═══════════════════════════════════════════\n"
printf "  安装目录 %s\n" "$APP_DIR"
printf "  控制台   http://%s:%s/login\n" "${IP:-127.0.0.1}" "$PORT"
printf "  数据目录 %s/data\n" "$APP_DIR"
printf "  初始密码 %s/data/INITIAL_ADMIN.txt\n" "$APP_DIR"
printf "\n  升级     bash %s/update-php.sh\n" "$APP_DIR"
printf "  查位置   cat %s\n" "$INSTALL_MARKER"
printf "\n  ${Y}生产环境别忘了${N}\n"
printf "    · 用 Nginx + php-fpm 反代（PHP 内置服务器是单进程，只适合验证）\n"
printf "    · 前面加 TLS；默认监听 %s 会暴露到公网，建议 HOST=127.0.0.1\n" "$HOST"
printf "    宝塔面板用户请改用： bash %s/bt/bt-install.sh\n\n" "$APP_DIR"
