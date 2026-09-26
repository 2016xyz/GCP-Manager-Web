#!/usr/bin/env bash
# =============================================================================
# 安全回归：把本轮审计发现的每一处都跑一遍（可重跑断言）
#
# 设计原则：**每条都必须真跑**，脚本自己起服务/造数据/清理临时目录，
# 不依赖事先手工准备的现场，也不碰真实 GCP。
#
# 用法： bash tools/security/run_all.sh
#        bash tools/security/run_all.sh reveal_pw      # 只跑某一个
# =============================================================================
set -u
cd "$(dirname "$0")/../.." || exit 1
ROOT=$(pwd)
ONLY="${1:-}"

PASS=0
FAIL=0
run() {  # run <名字> <命令...>
    local name="$1"; shift
    if [ -n "$ONLY" ] && [ "$name" != "$ONLY" ]; then return 0; fi
    printf '\n\033[1m═══ %s ═══\033[0m\n' "$name"
    if "$@"; then
        printf '\033[32m✅ %s 通过\033[0m\n' "$name"; PASS=$((PASS+1))
    else
        printf '\033[31m❌ %s 失败\033[0m\n' "$name"; FAIL=$((FAIL+1))
    fi
}

echo "════════════════════════════════════════════════════════════"
echo "  GCP Manager Web · 安全回归（本轮审计发现项）"
echo "════════════════════════════════════════════════════════════"

# ── PHP 侧（脚本自带起/停服务与临时数据目录）──────────────────────────────
run "reveal_pw（root 密码二次验证 + 限速）"  python3 tools/security/poc_reveal_pw.py
run "task_payload_leak（任务下发脱敏）"      python3 tools/security/poc_task_payload_leak.py php
run "ws_revoke（WS 会话吊销生效）"           python3 tools/security/poc_ws_revoke.py php

# ── 纯逻辑 / 桩测试（不需要服务）────────────────────────────────────────
run "execute_all（execute 的 all 三态语义）" python3 tools/security/poc_execute_all.py
run "proxy_race（代理环境变量并发隔离）"     python3 tools/security/poc_proxy_race.py
run "log_password_leak（日志凭据擦除）"      python3 tools/security/poc_log_password_leak.py
run "low_hardening（枚举/会话吊销/脚本 trace）" python3 tools/security/poc_low_hardening.py
run "singleflight（勘察缓存单飞）"           php php/tests/singleflight_check.php
run "fd_inherit（worker 不继承监听套接字）"  php php/tests/fd_inherit_check.php

echo
echo "════════════════════════════════════════════════════════════"
printf '  结果：通过 %d 项，失败 %d 项\n' "$PASS" "$FAIL"
echo "════════════════════════════════════════════════════════════"
[ "$FAIL" = 0 ] || exit 1

# 提示：poc_input_validation / poc_ws_revoke_py / poc_task_payload_leak(py) 需要
# 外部已起好对应服务（并传入 GCPWEB_DATA_DIR），故不放进默认批次：
#   rm -rf /tmp/x && mkdir -p /tmp/x
#   GCPWEB_DATA_DIR=/tmp/x python3 -m uvicorn app:app --host 127.0.0.1 --port 8082
#   PY_DATA=/tmp/x PY_PORT=8082 python3 tools/security/poc_input_validation.py
