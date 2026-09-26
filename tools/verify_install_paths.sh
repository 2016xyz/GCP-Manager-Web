#!/usr/bin/env bash
# =============================================================================
# 验证「安装路径与文档一致性」—— 真跑，不是静态检查
#
# 背景（用户实测踩到的坑）：
#   README 推荐 `curl … | bash` 安装，但 install.sh 管道模式把源码装到
#   `$PWD/gcp-manager-web`（即「你在哪个目录执行就装到哪」）；而 README 的
#   升级指引写死 `cd /opt/gcp-manager-web && bash update.sh`。
#   两处对不上时，`cd` 在 update.sh 运行前就失败 —— 用户只看到
#   `cd: 没有那个文件或目录`，脚本连解释的机会都没有。
#
# 本脚本真跑 4 个场景，全部通过才算修复成立：
#   ① 管道模式（模拟 curl|bash）默认装到 /opt/gcp-manager-web
#   ② 从任意目录执行 update.sh → 靠 /etc 标记文件自动定位
#   ③ 标记文件不存在 → 靠「常见位置」兜底定位
#   ④ 确实没装过 → 明确提示「先装再用」且退出码 1
#
# ⚠ 会临时创建 /opt/gcp-manager-web 与 /etc/gcp-manager-web.path，结束后清理。
#    请勿在生产机上执行。
#
# 用法：bash tools/verify_install_paths.sh
# =============================================================================
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INST="$BASE/install.sh"
UPD="$BASE/update.sh"
TARGET="/opt/gcp-manager-web"
MARKER="/etc/gcp-manager-web.path"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); printf "  \033[32m✅\033[0m %s\n" "$*"; }
bad()  { FAIL=$((FAIL+1)); printf "  \033[31m❌\033[0m %s\n" "$*"; }
head_() { printf "\n%s\n%s\n%s\n" "$(printf '═%.0s' {1..72})" "$*" "$(printf '═%.0s' {1..72})"; }

cleanup() {
  rm -rf "$TARGET" "$TARGET.bak" "$HOME/gcp-manager-web" 2>/dev/null
  rm -f "$MARKER" 2>/dev/null
}
trap cleanup EXIT
cleanup

# 安装/升级脚本的输出带 ANSI 颜色码（\033[32m 之类），直接 grep 中文+路径会
# 匹配不到 —— 必须先去色。所有断言都 grep 去色后的文件。
strip_ansi() { sed 's/\x1b\[[0-9;]*m//g' "$1" > "$1.clean" 2>/dev/null || cp "$1" "$1.clean"; }
# 用 bash -s -- 传参：`bash < file --check` 里的 --check 会被 bash 自己吃掉
# 注意：函数里后面还跑了 strip_ansi，必须显式把脚本的退出码透出去，
# 否则调用方的 $? 拿到的是 strip_ansi 的状态（恒为 0）。
run_piped() {   # run_piped <起始目录> <日志> [脚本参数…]
  local dir="$1" log="$2"; shift 2
  local rc=0
  ( cd "$dir" && bash -s -- "$@" < "$UPD" ) > "$log" 2>&1 || rc=$?
  strip_ansi "$log"
  return "$rc"
}

# ── ① 管道模式默认路径 ──────────────────────────────────────────────────────
head_ "① 管道模式（模拟 curl | bash，当前目录 \$HOME）默认安装路径"
( cd "$HOME" && NO_SERVICE=1 bash < "$INST" ) > /tmp/vip1.log 2>&1
strip_ansi /tmp/vip1.log
if [ -f "$TARGET/app.py" ]; then
  ok "装到 $TARGET（与 README 升级指引一致）"
  ok "版本 $(grep -m1 'VERSION = ' "$TARGET/core/version.py" | cut -d'"' -f2)"
else
  bad "未装到 $TARGET"
fi
[ -e "$HOME/gcp-manager-web" ] && bad "仍装到了当前目录 \$HOME/gcp-manager-web" \
                               || ok "不再依赖执行时的当前目录"
[ "$(cat "$MARKER" 2>/dev/null)" = "$TARGET" ] \
  && ok "安装路径已写入 $MARKER" || bad "标记文件未写或内容不对"
grep -q "安装目录 $TARGET" /tmp/vip1.log.clean \
  && ok "收尾信息打印了真实安装目录" || bad "收尾信息未打印安装目录"

# ── ② 从错误目录执行 update.sh，靠标记文件定位 ──────────────────────────────
head_ "② 在 \$HOME 执行 update.sh（那里没有 app.py）→ 应自动定位"
run_piped "$HOME" /tmp/vip2.log --check
grep -q "已定位到部署目录 $TARGET" /tmp/vip2.log.clean \
  && ok "靠 $MARKER 自动定位成功" || bad "未自动定位"

# ── ③ 标记文件缺失，靠常见位置兜底 ─────────────────────────────────────────
head_ "③ 删掉标记文件 → 应靠「常见位置」兜底定位"
rm -f "$MARKER"
run_piped "$HOME" /tmp/vip3.log --check
grep -q "已定位到部署目录 $TARGET" /tmp/vip3.log.clean \
  && ok "靠常见位置兜底定位成功" || bad "兜底定位失败"

# ── ④ 确实没装过 → 明确指引 + 退出码非 0 ───────────────────────────────────
head_ "④ 整台机器确实没装过 → 应明确提示「先装再用」"
mv "$TARGET" "$TARGET.bak"
run_piped "$HOME" /tmp/vip4.log --check
code=$?
[ "$code" = "1" ] && ok "退出码 1（不是静默成功）" || bad "退出码 $code，应为 1"
grep -q "找不到任何已部署的实例" /tmp/vip4.log.clean \
  && ok "明确告知「没装过」" || bad "未给出明确结论"
grep -q "不能代替首次安装" /tmp/vip4.log.clean \
  && ok "解释了 update.sh 是升级通道而非安装器" || bad "未解释定位差异"
grep -q "install.sh | bash" /tmp/vip4.log.clean \
  && ok "给出了首次安装命令" || bad "未给安装命令"
grep -q "gcp-manager-web.path" /tmp/vip4.log.clean \
  && ok "给出了「如何找到装在哪」的命令" || bad "未给查找命令"
grep -q "已定位到部署目录 $TARGET.bak" /tmp/vip4.log.clean \
  && bad "把 .bak 备份误认成部署目录" || ok "未把 .bak 误认成部署"
mv "$TARGET.bak" "$TARGET"

head_ "结果"
printf "  通过 %d 项，失败 %d 项\n\n" "$PASS" "$FAIL"
[ "$FAIL" = "0" ] || exit 1
