#!/usr/bin/env bash
# =============================================================================
# GCP Manager Web · PHP 版 升级
#
#   cd <安装目录> && bash update-php.sh
#   bash update-php.sh --check          # 只比对版本，不做改动
#   APP_DIR=/你的/路径 bash update-php.sh
#
# 与 Python 版同一套纪律：
#   · 升级**绝不碰 data/**（账号、密钥、数据库都在那里）
#   · 部署目录不靠「你在哪执行」—— 先查标记文件 /etc/gcp-php-web.path，再探常见位置
#   · 确实没装过会明确告诉你「先装再用」，而不是丢一句「找不到」
#
# 环境变量：APP_DIR / FORCE=1（丢弃本地改动）/ NO_RESTART=1
# =============================================================================
set -uo pipefail

MARKER="${INSTALL_MARKER:-/etc/gcp-php-web.path}"
APP_DIR="${APP_DIR:-}"
CHECK=0
FORCE="${FORCE:-0}"
NO_RESTART="${NO_RESTART:-0}"
REPO_URL="${REPO_URL:-https://github.com/2016xyz/GCP-Manager-Web.git}"
SVC="${SVC:-gcp-php-web}"

B="\033[1m"; G="\033[32m"; Y="\033[33m"; R="\033[31m"; N="\033[0m"
ok()   { printf "  ${G}✓${N} %s\n" "$*"; }
warn() { printf "  ${Y}!${N} %s\n" "$*"; }
err()  { printf "  ${R}✗${N} %s\n" "$*" >&2; }

for a in "$@"; do
  case "$a" in
    --check) CHECK=1 ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) err "未知参数：$a"; exit 2 ;;
  esac
done

printf "\n${B}══ GCP Manager Web · PHP 版 升级 ══${N}\n"

# ── 定位部署目录 ────────────────────────────────────────────────────────────
is_deploy() { [ -f "$1/public/index.php" ] && [ -f "$1/src/Version.php" ]; }

locate_app_dir() {
  local c
  # 1) 安装时写的标记文件
  if [ -f "$MARKER" ]; then
    c="$(head -n1 "$MARKER" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "$c" ] && is_deploy "$c"; then echo "$c"; return 0; fi
  fi
  # 2) 常见位置
  for c in /opt/gcp-manager-php /srv/gcp-manager-php /usr/local/gcp-manager-php \
           "${HOME:-/root}/gcp-manager-php" /root/gcp-manager-php \
           /www/wwwroot/*/php; do
    [ -d "$c" ] || continue
    if is_deploy "$c"; then echo "$c"; return 0; fi
  done
  # 3) 浅层搜索（排除备份/临时目录，且必须同时有 public/index.php 与 src/Version.php）
  local hit
  hit="$(find /opt /srv /root /home /usr/local /www/wwwroot -maxdepth 4 \
           -name index.php -path '*/public/*' 2>/dev/null \
         | grep -Ev '/(\.|~)|\.bak|\.old|\.orig|\.save|-bak|-old|/backup|/tmp' \
         | while read -r f; do d="$(dirname "$(dirname "$f")")"; is_deploy "$d" && echo "$d"; done \
         | head -n1 || true)"
  if [ -n "$hit" ]; then echo "$hit"; return 0; fi
  return 1
}

if [ -z "$APP_DIR" ]; then
  APP_DIR="$(cd "$PWD" 2>/dev/null && pwd)"
fi
if ! is_deploy "$APP_DIR"; then
  warn "在 $APP_DIR 找不到 public/index.php，尝试自动定位部署目录…"
  if located="$(locate_app_dir)"; then
    APP_DIR="$located"
    ok "已定位到部署目录 $APP_DIR"
  else
    printf "\n  ${R}✘ 找不到任何已部署的实例。${N}\n\n"
    printf "  常见原因：${B}这台机器还没装过${N} —— update-php.sh 只是「已安装之后的升级通道」，\n"
    printf "  不能代替首次安装（很多人先试更新才发现还没装）。\n\n"
    printf "  首次安装：\n"
    printf "      curl -fsSL https://raw.githubusercontent.com/2016xyz/GCP-Manager-Web/main/php/install-php.sh | bash\n\n"
    printf "  宝塔面板用户：\n"
    printf "      bash <源码>/bt/bt-install.sh --site /www/wwwroot/你的域名/php\n\n"
    printf "  确认装过、只是路径不同，就显式指定：\n"
    printf "      APP_DIR=/你的/部署路径 bash update-php.sh\n\n"
    printf "  想知道它装在哪个目录：\n"
    printf "      cat %s 2>/dev/null \\\\\n" "$MARKER"
    printf "        || find / -maxdepth 4 -name index.php -path '*public*' 2>/dev/null | head\n\n"
    exit 1
  fi
fi

printf "  ▸ 部署目录 %s\n" "$APP_DIR"
cd "$APP_DIR" || { err "无法进入 $APP_DIR"; exit 1; }

# ── 比对版本 ────────────────────────────────────────────────────────────────
local_ver="$(php -r 'require "src/Version.php"; echo Version::VERSION;' 2>/dev/null || echo '?')"
printf "  ▸ 本地版本 %s\n" "$local_ver"

remote_ver=""
if command -v curl >/dev/null 2>&1; then
  printf "  ▸ 查询远端…\n"
  remote_ver="$(curl -fsSL --max-time 20 \
    "${REPO_URL%.git}/raw/main/core/version.py" 2>/dev/null \
    | sed -n 's/^VERSION *= *"\([^"]*\)".*/\1/p' | head -n1 || true)"
fi
if [ -z "$remote_ver" ]; then
  warn "拿不到远端版本（网络不通？）"
  if [ "$CHECK" = "1" ]; then
    exit 0
  fi
  if [ "$FORCE" != "1" ]; then
    err "无法确认远端版本，已中止。确认要更新可加 FORCE=1 直接拉取"
    exit 1
  fi
  warn "FORCE=1 → 继续按 tarball 拉取最新代码"
fi

if [ -n "$remote_ver" ]; then
  printf "  ▸ 远端版本 %s\n" "$remote_ver"
  if [ "$local_ver" = "$remote_ver" ]; then
    ok "已是最新（$local_ver）"
    [ "$CHECK" = "1" ] && exit 0
    exit 0
  fi
  ok "有新版本：$local_ver → $remote_ver"
fi
[ "$CHECK" = "1" ] && exit 0

# ── data/ 指纹（升级前后必须一致）────────────────────────────────────────────
data_fingerprint() {
  [ -d "$APP_DIR/data" ] || { echo "none"; return; }
  find "$APP_DIR/data" -type f -exec sha256sum {} \; 2>/dev/null | sort | sha256sum | cut -d' ' -f1
}
BEFORE="$(data_fingerprint)"

# ── 拉取新代码 ──────────────────────────────────────────────────────────────
is_git=0
if [ -d "$APP_DIR/.git" ] || [ -d "$(dirname "$APP_DIR")/.git" ]; then
  is_git=1
fi

if [ "$is_git" = "1" ]; then
  printf "  ▸ git fetch origin main\n"
  if ! git -C "$APP_DIR" fetch --depth 1 origin main >/dev/null 2>&1; then
    warn "git fetch 失败（网络？）—— 改用 tarball 覆盖"
    is_git=0
  else
    if [ -n "$(git -C "$APP_DIR" status --porcelain 2>/dev/null)" ] && [ "$FORCE" != "1" ]; then
      err "本地有未提交改动，已中止（想强制更新加 FORCE=1，改动会被丢弃）"
      exit 1
    fi
    git -C "$APP_DIR" reset --hard origin/main >/dev/null 2>&1 || true
    ok "代码已更新"
  fi
fi

if [ "$is_git" != "1" ]; then
  printf "  ▸ 用 tarball 覆盖源码（只覆盖同名文件，不动 data/）\n"
  TMP="$(mktemp -d)"
  if curl -fsSL "${REPO_URL%.git}/archive/refs/heads/main.tar.gz" 2>/dev/null \
       | tar xz -C "$TMP" --strip-components=1 2>/dev/null; then
    PSrc="$TMP/php"
    [ -d "$PSrc" ] || PSrc="$TMP"
    for item in src bin public bt install-php.sh update-php.sh README-PHP.md INTERFACES.md; do
      [ -e "$PSrc/$item" ] || continue
      # public/static 里的前端资源也要跟着更新
      cp -a "$PSrc/$item" "$APP_DIR/" 2>/dev/null || true
    done
    ok "源码已覆盖"
  else
    rm -rf "$TMP"
    err "下载新版本失败"
    exit 1
  fi
  rm -rf "$TMP"
fi

# ── data/ 未被改动 ──────────────────────────────────────────────────────────
AFTER="$(data_fingerprint)"
if [ "$BEFORE" = "$AFTER" ]; then
  ok "data/ 目录未被改动（账号与密钥完好）"
else
  warn "data/ 指纹发生变化（通常是会话/日志正常写入，非升级导致）"
fi

# ── 依赖与缓存 ──────────────────────────────────────────────────────────────
php -r 'echo PHP_VERSION;' >/dev/null 2>&1 && ok "PHP 可用（$(php -r 'echo PHP_VERSION;')）" || err "PHP 不可用"
[ -d "$APP_DIR/data" ] && chmod 700 "$APP_DIR/data" 2>/dev/null || true

# ── 重启 ────────────────────────────────────────────────────────────────────
if [ "$NO_RESTART" = "1" ]; then
  warn "按要求不重启服务（NO_RESTART=1）"
else
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q "^$SVC.service"; then
    systemctl restart "$SVC" >/dev/null 2>&1 && ok "服务已重启（$SVC）" || warn "重启失败：systemctl status $SVC"
  else
    warn "未发现 systemd 服务 $SVC"
    printf "      若用宝塔/Nginx+php-fpm，PHP 代码是即时生效的，无需重启；\n"
    printf "      只重启任务 worker 即可： php %s/bin/task-runner.php --once\n" "$APP_DIR"
  fi
fi

NEW_VER="$(php -r 'require "src/Version.php"; echo Version::VERSION;' 2>/dev/null || echo '?')"
printf "\n  ${G}✓ 升级完成${N} —— 当前版本 v%s\n" "$NEW_VER"
printf "      安装目录 %s\n\n" "$APP_DIR"
