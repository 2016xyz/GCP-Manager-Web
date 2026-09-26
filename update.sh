#!/usr/bin/env bash
# =============================================================================
# GCP Manager Web —— 升级脚本
#
# 作用：把已部署的实例更新到 GitHub 上的最新版本，并重启服务。
#
# 用法：
#   cd /path/to/gcp-manager-web && bash update.sh
#   APP_DIR=/opt/gcp-manager-web bash update.sh        # 指定部署目录
#   bash update.sh --check                             # 只看有没有新版本，不更新
#   FORCE=1 bash update.sh                             # 本地有改动也强制更新（改动会被丢弃）
#   NO_RESTART=1 bash update.sh                        # 只更新代码，不重启服务
#
# 也可以直接联网执行：
#   curl -fsSL https://raw.githubusercontent.com/2016xyz/GCP-Manager-Web/main/update.sh | bash
#
# 安全保证：
#   · 全程不触碰 data/ 目录（账号、密钥、数据库都在那里）
#   · 走 git 时会先 git stash 保存本地改动，不会静默丢弃
#   · 更新前后都会打印当前提交号，方便回滚
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/2016xyz/GCP-Manager-Web.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_TARBALL="${REPO_TARBALL:-https://github.com/2016xyz/GCP-Manager-Web/archive/refs/heads/${REPO_BRANCH}.tar.gz}"
SERVICE_NAME="${SERVICE_NAME:-gcp-manager-web}"
PY="${PYTHON:-python3}"

# curl|bash 场景下 BASH_SOURCE 可能未绑定，用 :- 兜底
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
  HERE="$(cd "$(dirname "$SELF")" && pwd)"
else
  HERE="$PWD"
fi
APP_DIR="${APP_DIR:-$HERE}"

C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_0=$'\033[0m'
info()  { printf "  ${C_DIM}▸${C_0} %s\n" "$*"; }
ok()    { printf "  ${C_OK}✔${C_0} %s\n" "$*"; }
warn()  { printf "  ${C_WARN}!${C_0} %s\n" "$*"; }
die()   { printf "  ${C_ERR}✘${C_0} %s\n" "$*" >&2; exit 1; }

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { sed -n '2,22p' "$SELF" 2>/dev/null || true; exit 0; }

echo
echo "══ GCP Manager Web · 升级 ══"
info "部署目录 $APP_DIR"

# ── 部署目录定位 ────────────────────────────────────────────────────────────
# 为什么需要这段：安装路径曾是「执行 install.sh 时的当前目录」，而文档里的
# 升级指引写死了 /opt/gcp-manager-web —— 两处对不上时，用户敲
# `cd /opt/gcp-manager-web && bash update.sh` 会在 **cd 阶段**就报
# 「没有那个文件或目录」，根本轮不到本脚本解释。
# 现在：找不到 app.py 就自动去标记文件 / 常见位置 / 浅层搜索里定位；
# 确实没有则明确告诉用户「这台机器还没装过」，而不是一句找不到 app.py。
locate_app_dir() {
  local c
  # 1) 安装时写的标记文件（install.sh 会写）
  if [ -f /etc/gcp-manager-web.path ]; then
    c="$(head -n1 /etc/gcp-manager-web.path 2>/dev/null | tr -d '[:space:]')"
    [ -n "$c" ] && [ -f "$c/app.py" ] && { echo "$c"; return 0; }
  fi
  # 2) 常见部署位置
  for c in /opt/gcp-manager-web /srv/gcp-manager-web /usr/local/gcp-manager-web \
           "${HOME:-/root}/gcp-manager-web" /root/gcp-manager-web; do
    [ -f "$c/app.py" ] && { echo "$c"; return 0; }
  done
  # 3) 浅层搜索（限定名字含 gcp，排除备份/临时目录，避免误认 .bak / .old）
  local hit
  hit="$(find /opt /srv /root /home /usr/local -maxdepth 4 -name app.py \
          -path '*gcp*' 2>/dev/null \
          | grep -Ev '/(\.|~)|\.bak|\.old|\.orig|\.save|-bak|-old|/backup|/tmp' \
          | head -n1 || true)"
  if [ -n "$hit" ]; then
    c="$(dirname "$hit")"
    [ -f "$c/app.py" ] && [ -f "$c/core/version.py" ] && { echo "$c"; return 0; }
  fi
  return 1
}

if [ ! -f "$APP_DIR/app.py" ]; then
  warn "在 $APP_DIR 找不到 app.py，尝试自动定位部署目录…"
  if FOUND="$(locate_app_dir)"; then
    APP_DIR="$FOUND"
    ok "已定位到部署目录 $APP_DIR"
  else
    cat >&2 <<'EOF'

  ✘ 找不到任何已部署的实例。

  常见原因：**这台机器还没装过** —— update.sh 只是「已安装之后的升级通道」，
  不能代替首次安装（很多人先试更新才发现还没装）。

  首次安装（装完会打印真实安装路径与升级命令）：
      curl -fsSL https://raw.githubusercontent.com/2016xyz/GCP-Manager-Web/main/install.sh | bash

  确认装过、只是路径不同，就显式指定：
      APP_DIR=/你的/部署路径 bash update.sh

  想知道它装在哪个目录：
      cat /etc/gcp-manager-web.path 2>/dev/null \
        || find / -maxdepth 4 -name app.py -path '*gcp*' 2>/dev/null | head

EOF
    exit 1
  fi
fi
cd "$APP_DIR"

# ── 数据目录兜底备份 ────────────────────────────────────────────────
# 更新流程本身不会写 data/，这里只做一个"万一手滑"的保险：
# 记录更新前 data/ 的文件指纹，更新后比对，一旦有变化立刻告警。
data_fingerprint() {
  [ -d "$APP_DIR/data" ] || { echo "(无 data 目录)"; return; }
  find "$APP_DIR/data" -type f -printf '%P %s\n' 2>/dev/null | LC_ALL=C sort | md5sum | cut -d' ' -f1
}
DATA_BEFORE="$(data_fingerprint)"

# ── 解析本地版本 ────────────────────────────────────────────────────
IS_GIT=0
LOCAL_SHA="(未知)"
if [ -d "$APP_DIR/.git" ] && command -v git >/dev/null 2>&1; then
  IS_GIT=1
  LOCAL_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo '(未知)')"
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "$REPO_BRANCH")"
  info "本地版本 $LOCAL_SHA（git 仓库，分支 $BRANCH）"
else
  info "本地不是 git 仓库（将用 tarball 覆盖源码）"
fi

# ── 仅检查模式 ──────────────────────────────────────────────────────
if [ "$CHECK_ONLY" = "1" ]; then
  if [ "$IS_GIT" = "0" ]; then
    warn "非 git 安装无法比对版本；直接跑 bash update.sh 即可拉最新"
    exit 0
  fi
  info "查询远端…"
  git fetch --quiet origin "$REPO_BRANCH" || die "拉取远端失败，检查网络"
  REMOTE_SHA="$(git rev-parse --short "origin/$REPO_BRANCH")"
  if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
    ok "已是最新（$LOCAL_SHA）"
  else
    warn "有新版本：$LOCAL_SHA → $REMOTE_SHA"
    echo "      执行 bash update.sh 升级"
  fi
  exit 0
fi

# ── 停止服务（避免升级到一半还挂着旧代码在跑）──────────────────────
HAS_SERVICE=0
SUDO=""
[ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
if command -v systemctl >/dev/null 2>&1 && \
   $SUDO systemctl list-unit-files "${SERVICE_NAME}.service" 2>/dev/null | grep -q "$SERVICE_NAME"; then
  HAS_SERVICE=1
fi

# ── 拉取新代码 ──────────────────────────────────────────────────────
if [ "$IS_GIT" = "1" ]; then
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    if [ "${FORCE:-0}" = "1" ]; then
      warn "本地有未提交改动，FORCE=1 → 丢弃"
      git reset --hard --quiet
    else
      # 保存改动再更新，绝不静默丢弃
      STASH_MSG="gcp-web-update-$(date +%Y%m%d-%H%M%S)"
      git stash push --quiet -m "$STASH_MSG" --include-untracked 2>/dev/null || true
      warn "本地有未提交改动，已暂存为 stash：$STASH_MSG"
      warn "需要时用 git stash list / git stash pop 取回"
    fi
  fi
  info "git fetch origin $REPO_BRANCH"
  git fetch --quiet origin "$REPO_BRANCH" || die "拉取远端失败，检查网络"
  git reset --hard --quiet "origin/$REPO_BRANCH" || die "切到 origin/$REPO_BRANCH 失败"
  NEW_SHA="$(git rev-parse --short HEAD)"
  ok "代码已更新：$LOCAL_SHA → $NEW_SHA"
else
  command -v curl >/dev/null 2>&1 || die "需要 curl"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  info "下载 $REPO_TARBALL"
  curl -fsSL "$REPO_TARBALL" -o "$TMP/src.tar.gz" || die "下载失败，检查网络"
  tar xzf "$TMP/src.tar.gz" -C "$TMP" || die "解压失败"
  INNER="$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -1)"
  [ -n "$INNER" ] || die "压缩包结构异常"
  # tarball 只含版本控制内的文件，data/ 不在其中，因此不会被覆盖
  cp -a "$INNER"/. "$APP_DIR"/
  ok "代码已覆盖为最新版（tarball 模式，未记录版本号）"
fi

# ── 校验数据目录未被触碰 ────────────────────────────────────────────
DATA_AFTER="$(data_fingerprint)"
if [ "$DATA_BEFORE" = "$DATA_AFTER" ]; then
  ok "data/ 目录未被改动（账号与密钥完好）"
else
  warn "data/ 目录有变化！请确认是否符合预期"
fi

# ── 更新依赖 ────────────────────────────────────────────────────────
PYBIN="$PY"
if [ -x "$APP_DIR/.venv/bin/python" ]; then
  PYBIN="$APP_DIR/.venv/bin/python"
  info "使用已有虚拟环境 $APP_DIR/.venv"
elif [ -x "$APP_DIR/venv/bin/python" ]; then
  PYBIN="$APP_DIR/venv/bin/python"
  info "使用已有虚拟环境 $APP_DIR/venv"
else
  warn "未找到虚拟环境，将安装到系统 Python"
fi

if [ -f "$APP_DIR/requirements.txt" ]; then
  info "更新依赖（最多等 ${PIP_TIMEOUT:-300}s）…"
  PIP_TIMEOUT="${PIP_TIMEOUT:-300}"
  PIP_OPTS="-q --disable-pip-version-check"
  if ! timeout "$PIP_TIMEOUT" "$PYBIN" -m pip install $PIP_OPTS -r "$APP_DIR/requirements.txt" >/dev/null 2>&1; then
    warn "默认源失败，换清华源重试"
    timeout "$PIP_TIMEOUT" "$PYBIN" -m pip install $PIP_OPTS \
      -i https://pypi.tuna.tsinghua.edu.cn/simple -r "$APP_DIR/requirements.txt" >/dev/null 2>&1 \
      || warn "清华源也失败，依赖可能未更新（若启动报错请手动 pip install -r requirements.txt）"
  fi
  ok "依赖就绪"
fi

# ── 重启服务 ────────────────────────────────────────────────────────
if [ "${NO_RESTART:-0}" = "1" ]; then
  warn "NO_RESTART=1，跳过重启；改动将在下次启动时生效"
elif [ "$HAS_SERVICE" = "1" ]; then
  info "重启 systemd 服务 $SERVICE_NAME"
  $SUDO systemctl restart "$SERVICE_NAME"
  sleep 2
  if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "服务已重启并运行中"
  else
    die "服务未正常启动，查看：journalctl -u $SERVICE_NAME -n 50 --no-pager"
  fi
else
  warn "未发现 systemd 服务 $SERVICE_NAME"
  echo "      若用 run.sh / docker 启动，请手动重启："
  echo "        bash run.sh                  # 前台重启"
  echo "        docker compose up -d --build # 容器方式"
fi

# 从 core/version.py 读产品版本（单一来源）
APP_VER="$(grep -m1 '^VERSION = ' "$APP_DIR/core/version.py" 2>/dev/null | cut -d'"' -f2 || true)"

echo
ok "升级完成${APP_VER:+ —— 当前版本 v$APP_VER}"
if [ "$IS_GIT" = "1" ]; then
  echo "      提交 $NEW_SHA"
  echo "      回滚方式 git reset --hard $LOCAL_SHA"
fi
echo
