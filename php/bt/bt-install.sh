#!/usr/bin/env bash
# =============================================================================
# 宝塔面板（BT Panel / aaPanel）环境下的 GCP Manager PHP 版安装助手
#
# 它做四件事（全部幂等、可重复执行）：
#   1. 体检：PHP 版本 / 必需扩展 / 被禁用的关键函数 / open_basedir —— 缺什么报什么，
#      并给出宝塔界面里的**确切点击路径**
#   2. 目录：创建 data/ 与 keys/，收紧到 700，属主交给 www（宝塔 PHP-FPM 运行用户）
#   3. 初始化：建库、建管理员、写出初始密码文件（600）
#   4. 计划任务：注册「任务队列 worker」的宝塔计划任务（每分钟），
#      以及可选的 WebSocket 常驻服务
#
# 用法（宝塔是 root 环境，直接跑）：
#   bash bt-install.sh --site /www/wwwroot/gcp.example.com/php
#   bash bt-install.sh                    # 自动探测站点下的 php 目录
#
# ⚠ 不会改动你的 Nginx 站点配置 —— 伪静态规则需要你在宝塔界面粘贴
#   （见 bt/nginx-rewrite.conf，脚本会把内容打印出来）。
# =============================================================================
set -uo pipefail

SITE_ROOT=""
PHP_BIN=""
MARKER="/etc/gcp-php-web.path"
CRON_DIR="/www/server/cron"
PHP_VER_REQUIRED=8.0

c_ok()   { printf "  \033[32m✅\033[0m %s\n" "$*"; }
c_bad()  { printf "  \033[31m❌\033[0m %s\n" "$*"; }
c_warn() { printf "  \033[33m⚠\033[0m  %s\n" "$*"; }
c_info() { printf "  · %s\n" "$*"; }
head_()  { printf "\n%s\n%s\n%s\n" "════════════════════════════════════════════════════════════" "$*" "════════════════════════════════════════════════════════════"; }

# ── 参数 ────────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --site)   SITE_ROOT="${2:-}"; shift 2 ;;
    --php)    PHP_BIN="${2:-}";   shift 2 ;;
    --site=*) SITE_ROOT="${1#*=}"; shift ;;
    --php=*)  PHP_BIN="${1#*=}";   shift ;;
    -h|--help)
      sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
  esac
done

head_ "0. 环境识别"
if [ -d /www/server/panel ]; then
  if [ -f /www/server/panel/data/default.pl ] && grep -qi aapanel /www/server/panel/class/*.py 2>/dev/null; then
    c_ok "检测到 aaPanel（宝塔国际版）"
  else
    c_ok "检测到宝塔面板（/www/server/panel）"
  fi
else
  c_warn "没找到 /www/server/panel —— 这看起来不是宝塔环境"
  c_info "裸机 / VPS 请改用 php/install-php.sh"
  c_info "仍要继续（比如手工装的宝塔在别处）可用 --site 指定目录"
fi

# ── 站点目录 ────────────────────────────────────────────────────────────────
head_ "1. 定位站点目录"
if [ -z "$SITE_ROOT" ]; then
  c_info "未指定 --site，尝试自动探测含 app 入口的 php 目录…"
  SITE_ROOT="$(find /www/wwwroot -maxdepth 4 -type d -name php \
                 -exec test -f '{}/public/index.php' \; -print 2>/dev/null | head -n1)"
fi
if [ -z "$SITE_ROOT" ]; then
  c_bad "找不到站点目录。请显式指定：bash bt-install.sh --site /www/wwwroot/你的域名/php"
  exit 1
fi
SITE_ROOT="$(cd "$SITE_ROOT" && pwd)"
if [ ! -f "$SITE_ROOT/public/index.php" ]; then
  c_bad "$SITE_ROOT/public/index.php 不存在 —— 这不像本项目的 php 目录"
  exit 1
fi
c_ok "站点 PHP 目录：$SITE_ROOT"
printf '%s\n' "$SITE_ROOT" > "$MARKER" && chmod 644 "$MARKER"
c_info "路径已记录到 $MARKER（update 脚本靠它定位）"

# 反查宝塔站点名与 Web 根，供用户核对「运行目录」是否设对
WEB_ROOT="$(dirname "$SITE_ROOT")"
c_info "对应的宝塔网站根目录应为：$WEB_ROOT"
c_info "请在 宝塔 → 网站 → 该站点 → 设置 → 网站目录 里把「运行目录」设为 /public"

# ── PHP 体检 ────────────────────────────────────────────────────────────────
head_ "2. PHP 环境体检"

# 宝塔的 PHP 装在 /www/server/php/<版本>/bin/php
if [ -z "$PHP_BIN" ]; then
  for cand in /www/server/php/*/bin/php; do
    [ -x "$cand" ] || continue
    v="$("$cand" -r 'echo PHP_VERSION;' 2>/dev/null || echo 0)"
    # 只接受 >= 8.0 的第一个
    if [ "$(printf '%s\n%s\n' "$PHP_VER_REQUIRED" "$v" | sort -V | head -n1)" = "$PHP_VER_REQUIRED" ]; then
      PHP_BIN="$cand"
      break
    fi
  done
fi
if [ -z "$PHP_BIN" ] || [ ! -x "$PHP_BIN" ]; then
  PHP_BIN="$(command -v php || true)"
fi
if [ -z "$PHP_BIN" ]; then
  c_bad "找不到 PHP CLI。请在宝塔 → 软件商店 安装 PHP 8.0+（或 --php 指定绝对路径）"
  exit 1
fi
PHP_VER="$("$PHP_BIN" -r 'echo PHP_VERSION;' 2>/dev/null || echo unknown)"
c_info "PHP：$PHP_BIN（$PHP_VER）"
if [ "$(printf '%s\n%s\n' "$PHP_VER_REQUIRED" "$PHP_VER" | sort -V | head -n1)" = "$PHP_VER_REQUIRED" ]; then
  c_ok "版本满足 ≥ $PHP_VER_REQUIRED"
else
  c_bad "版本过低（需要 ≥ $PHP_VER_REQUIRED）。宝塔 → 软件商店 → PHP 8.x → 安装"
fi

# 扩展
c_info "检查扩展…"
ext_out="$("$PHP_BIN" -m 2>/dev/null || true)"
missing=()
for e in pdo_sqlite sqlite3 openssl curl mbstring json zlib; do
  if printf '%s\n' "$ext_out" | grep -qix "$e"; then :; else missing+=("$e"); fi
done
if [ "${#missing[@]}" -eq 0 ]; then
  c_ok "必需扩展齐全（pdo_sqlite/sqlite3/openssl/curl/mbstring/json/zlib）"
else
  c_bad "缺少扩展：${missing[*]}"
  c_info "宝塔安装路径：软件商店 → PHP $PHP_VER → 设置 → 安装扩展 → 勾选上面这些"
fi
if printf '%s\n' "$ext_out" | grep -qix sockets; then
  c_ok "sockets 已装 → 可使用 WebSocket 实时日志"
else
  c_warn "缺 sockets 扩展 → WebSocket 实时日志不可用（前端会自动退化为轮询，功能不受影响）"
  c_info "需要实时日志就装：软件商店 → PHP $PHP_VER → 设置 → 安装扩展 → sockets"
fi

# 被禁用的关键函数（宝塔默认禁一堆）
c_info "检查禁用函数…"
# ★ 直接用 ini_get 读，不要去解析 `php -i` 的文本：
#   php -i 的输出是 `指令 => 局部值 => 全局值`，用 cut/awk 拆很容易把
#   "no value => no value" 整段当成值（实测踩过）。ini_get 返回的是真值，
#   未设置时为空字符串，语义干净。
dis="$( "$PHP_BIN" -r 'echo ini_get("disable_functions");' 2>/dev/null | tr -d ' \t' )"
blocked=()
for fn in proc_open proc_get_status exec shell_exec escapeshellarg putenv; do
  if printf '%s' ",$dis," | grep -q ",$fn,"; then blocked+=("$fn"); fi
done
if [ "${#blocked[@]}" -eq 0 ]; then
  c_ok "关键函数未被禁用（后台任务与 SSH 执行可用）"
else
  c_bad "被禁用的关键函数：${blocked[*]}"
  c_info "宝塔放行路径：软件商店 → PHP $PHP_VER → 设置 → 禁用函数 → 删掉上面这些 → 保存"
  c_info "（不删也能跑，但后台建机任务与「执行命令」会失败）"
fi

# open_basedir —— 宝塔默认会把 open_basedir 限制在站点目录，
# 而 PHP 版会在自身 php/ 目录内读写，通常没问题；但如果 data 目录被设在站点之外就会报错。
obd="$( "$PHP_BIN" -r 'echo ini_get("open_basedir");' 2>/dev/null | tr -d ' \t' )"
# ini_get 未设置时返回空字符串（不是 "no value"）—— 空即未限制
if [ -n "$obd" ] && [ "$obd" != "no value" ]; then
  c_warn "open_basedir 已开启：$obd"
  c_info "若要自定义 GCPWEB_DATA_DIR 到站点之外，需把该路径加进 open_basedir"
else
  c_ok "open_basedir 未限制（data/ 可正常读写）"
fi

# ── 目录与权限 ──────────────────────────────────────────────────────────────
head_ "3. 数据目录与权限"
WEB_USER="www"
id "$WEB_USER" >/dev/null 2>&1 || WEB_USER="$(stat -c '%U' /www/server/panel 2>/dev/null || echo root)"
c_info "PHP-FPM 运行用户：$WEB_USER"

for d in "$SITE_ROOT/data" "$SITE_ROOT/data/keys"; do
  mkdir -p "$d"
  chmod 700 "$d"
done
chown -R "$WEB_USER":"$WEB_USER" "$SITE_ROOT/data" 2>/dev/null || c_warn "chown 失败（可忽略，稍后手工处理）"
c_ok "data/ 与 data/keys/ 已创建，权限 700，属主 $WEB_USER"

# 源码目录只读（防止被 Web 改写）
find "$SITE_ROOT/src" "$SITE_ROOT/bin" -type f -name '*.php' -exec chmod 644 {} \; 2>/dev/null || true
c_ok "src/ 与 bin/ 源码设为只读 644"

# ── 初始化 ──────────────────────────────────────────────────────────────────
head_ "4. 初始化数据库与管理员"
if [ -f "$SITE_ROOT/data/gcp_php.db" ]; then
  c_info "已存在 $SITE_ROOT/data/gcp_php.db —— 跳过初始化（不会覆盖你的账号数据）"
else
  if [ -f "$SITE_ROOT/bin/init.php" ]; then
    sudo -u "$WEB_USER" "$PHP_BIN" "$SITE_ROOT/bin/init.php" && c_ok "初始化完成"
  else
    c_warn "未找到 bin/init.php，改为在首次访问时自动建库"
  fi
fi
if [ -f "$SITE_ROOT/data/INITIAL_ADMIN.txt" ]; then
  chmod 600 "$SITE_ROOT/data/INITIAL_ADMIN.txt"
  chown "$WEB_USER":"$WEB_USER" "$SITE_ROOT/data/INITIAL_ADMIN.txt" 2>/dev/null || true
  c_ok "初始管理员密码文件：$SITE_ROOT/data/INITIAL_ADMIN.txt（600）"
  c_info "查看：cat $SITE_ROOT/data/INITIAL_ADMIN.txt"
fi

# ── 宝塔计划任务 ────────────────────────────────────────────────────────────
head_ "5. 宝塔计划任务（任务队列 worker）"
WORKER_CMD="$PHP_BIN $SITE_ROOT/bin/task-runner.php --once"
c_info "建议注册的计划任务命令："
printf '\n      %s\n\n' "$WORKER_CMD"
if [ -d "$CRON_DIR" ]; then
  c_info "宝塔的计划任务请用界面添加（脚本会写入 $CRON_DIR，界面添加最稳妥）："
  c_info "  宝塔 → 计划任务 → 添加任务 → 任务类型：Shell脚本"
  c_info "  任务名称：GCP Manager 任务队列"
  c_info "  执行周期：N 分钟 → 1 分钟"
  c_info "  脚本内容：$WORKER_CMD"
  c_info "（不注册也能用：发起建机/执行时会即时拉起 worker，只是失败重试少一层保障）"
else
  c_warn "没找到 $CRON_DIR —— 非宝塔环境，请改用系统 crontab："
  c_info "  * * * * * $WORKER_CMD >/dev/null 2>&1"
fi

head_ "6. 还必须手工做的一步：Nginx 伪静态"
c_info "宝塔 → 网站 → 你的站点 → 设置 → 伪静态 → 粘贴 $SITE_ROOT/bt/nginx-rewrite.conf 的内容"
c_info "同时确认「网站目录 → 运行目录 = /public」"
printf '\n'
c_ok "宝塔侧准备完成"
printf '\n  访问：https://你的域名/login\n  忘记装在哪：cat %s\n\n' "$MARKER"
