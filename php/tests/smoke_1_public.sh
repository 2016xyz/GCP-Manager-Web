#!/usr/bin/env bash
# =============================================================================
# PHP 版冒烟测试 ①：公开接口 / 静态资源 / 路径穿越 / 验证码 / 登录 / 强制改密
#
# 用法：
#   bash php/tests/smoke_1_public.sh
#
# 环境变量：
#   PHP_BASE   被测服务地址（默认 http://127.0.0.1:8099）
#   PHP_DATA   数据目录（默认 /tmp/apitest）—— 用于从库里读验证码答案
#   TEST_PW    管理员密码；不设则读 $PHP_DATA/../apitest_pw.txt 或 INITIAL_ADMIN.txt
#
# 前提：数据目录已用 `php bin/init.php` 初始化，且管理员 must_change_password=0。
# =============================================================================
set -u
B="${PHP_BASE:-http://127.0.0.1:8099}"
DATA="${PHP_DATA:-/tmp/apitest}"
CJ="${PHP_COOKIE:-/tmp/php_cookie.txt}"
DB="$DATA/gcp_php.db"

# 密码来源：环境变量 > 每次运行随机生成并写进测试库
#
# 为什么要随机生成而不是硬编码：硬编码一个测试密码等于把凭据写进仓库
# （本项目历史上有过这样的教训——管理员密码曾随 tools/ 下的脚本进入公开仓库）。
# 这里每次运行现场生成、写进**测试库**、用完即弃，仓库里不留任何明文。
if [ -z "${TEST_PW:-}" ]; then
  TEST_PW="$(python3 -c "
import secrets,string
a=string.ascii_letters+string.digits
print('Smoke#'+''.join(secrets.choice(a) for _ in range(16)))")"
  export TEST_PW
fi
PHP_BIN="${PHP_BIN:-php}"
# 把测试库里的 admin 密码设为本次的测试密码（只影响 $DATA 指向的库）
GCPWEB_DATA_DIR="$DATA" "$PHP_BIN" -r '
require "php/src/bootstrap.php";
Users::ensure();
$u = Users::getUser(null, "admin");
if (!$u) { fwrite(STDERR, "测试库里没有 admin 用户，请先 php bin/init.php\n"); exit(1); }
Users::setPassword((int)$u["id"], getenv("TEST_PW"), false);
Db::exec("DELETE FROM login_guard");
Db::exec("UPDATE users SET disabled=0, must_change_password=0 WHERE id=?", [(int)$u["id"]]);
echo "  测试库 admin 密码已设为本次随机值（不落仓库）\n";
' || { echo "测试前置失败" >&2; exit 2; }

# 把本次测试密码持久化给 smoke_2 用（只写到 /tmp，不进仓库）
printf 'TEST_PW=%q\n' "$TEST_PW" > /tmp/apitest_pw.env
chmod 600 /tmp/apitest_pw.env

rm -f "$CJ"
P=0; F=0
ok(){ P=$((P+1)); printf "  \033[32m✅\033[0m %s\n" "$*"; }
bad(){ F=$((F+1)); printf "  \033[31m❌\033[0m %s\n" "$*"; }
code(){ curl -s -o /tmp/s1.json -w "%{http_code}" -b "$CJ" -c "$CJ" "$@"; }
cap_code(){ python3 -c "
import sqlite3,sys
try: print(sqlite3.connect('$DB').execute('SELECT code FROM captcha WHERE cid=?',(sys.argv[1],)).fetchone()[0])
except Exception: print('')
" "$1"; }

echo "═══ 1. 公开路由（对齐 Python 版 PUBLIC_PATHS）═══"
c=$(code "$B/api/version"); [ "$c" = 200 ] && ok "GET /api/version → 200" || bad "→ $c"
grep -q '"version"' /tmp/s1.json && ok "响应含 version 字段" || bad "缺 version"
c=$(code "$B/api/auth/me"); [ "$c" = 200 ] && ok "GET /api/auth/me → 200（未登录也 200，回 authenticated:false）" || bad "→ $c"
python3 -c "import json;d=json.load(open('/tmp/s1.json'));exit(0 if d.get('authenticated') is False else 1)" \
  && ok "未登录时 authenticated=false" || bad "authenticated 不是 false"
# ★ password_policy 在 Python 版**不在**白名单里（需要登录），这里必须也是 401
c=$(code "$B/api/auth/password_policy"); [ "$c" = 401 ] && ok "GET /api/auth/password_policy → 401（与 Python 一致，需登录）" || bad "→ $c（应 401）"
for r in /api/status /api/users /api/accounts /api/tasks /api/logs /api/audit /api/sessions /api/catalog; do
  c=$(code "$B$r"); [ "$c" = 401 ] && ok "未登录 $r → 401" || bad "未登录 $r → $c（应 401）"
done
c=$(code -X DELETE "$B/api/version"); [ "$c" = 405 ] && ok "方法不匹配 → 405（不是 404）" || bad "DELETE /api/version → $c"
c=$(code "$B/api/no_such_endpoint"); [ "$c" = 404 ] && ok "不存在的接口 → 404" || bad "→ $c"

echo
echo "═══ 2. 页面与静态资源 ═══"
c=$(code "$B/login"); [ "$c" = 200 ] && ok "GET /login → 200" || bad "→ $c"
c=$(code "$B/"); [ "$c" = 200 ] && ok "GET / → 200" || bad "→ $c"
for f in static/vendor/vue.global.prod.js static/login.css static/login.html static/console.html static/favicon.ico; do
  c=$(code "$B/$f"); [ "$c" = 200 ] && ok "$f → 200（$(stat -c%s /tmp/s1.json 2>/dev/null || echo ?) 字节）" || bad "$f → $c"
done

echo
echo "═══ 3. 路径穿越与敏感路径（全部必须 403/404）═══"
for u in "/static/../../data/gcp_php.db" "/static/../../src/Config.php" \
         "/static/%2e%2e%2f%2e%2e%2fdata%2fgcp_php.db" "/data/gcp_php.db" \
         "/data/INITIAL_ADMIN.txt" "/src/Config.php" "/src/Auth.php" "/.git/config" \
         "/bt/bt-install.sh" "/static/../data/INITIAL_ADMIN.txt" "/index.php.bak" "/php/INTERFACES.md"; do
  c=$(curl -s -o /dev/null -w "%{http_code}" "$B$u")
  if [ "$c" = 404 ] || [ "$c" = 403 ]; then ok "$u → $c"; else bad "$u → $c（应 404/403）"; fi
done

echo
echo "═══ 4. 验证码 ═══"
c=$(code "$B/api/auth/captcha"); [ "$c" = 200 ] && ok "GET /api/auth/captcha → 200" || bad "→ $c"
CID=$(python3 -c "import json;print(json.load(open('/tmp/s1.json')).get('captcha_id',''))" 2>/dev/null)
IMGLEN=$(python3 -c "import json;print(len(json.load(open('/tmp/s1.json')).get('image','')))" 2>/dev/null)
[ -n "$CID" ] && ok "captcha_id = ${CID:0:16}…" || bad "无 captcha_id"
[ "${IMGLEN:-0}" -gt 200 ] && ok "图形验证码 base64 长度 $IMGLEN（已真实渲染）" || bad "验证码图异常"

echo
echo "═══ 5. 登录流程 ═══"
guard_reset(){ python3 -c "
import sqlite3;c=sqlite3.connect('$DB');c.execute('DELETE FROM login_guard');c.commit()"; }
neg_login(){  # neg_login <captcha_id> <captcha_code> → 输出 HTTP 码
  curl -s -o /tmp/s1.json -w "%{http_code}" -b "$CJ" -c "$CJ" -H 'Content-Type: application/json' \
    -d "{\"username\":\"admin\",\"password\":\"$TEST_PW\",\"captcha_id\":\"$1\",\"captcha_code\":\"$2\"}" \
    "$B/api/auth/login"
}
guard_reset
c=$(code "$B/api/auth/captcha")
CID=$(python3 -c "import json;print(json.load(open('/tmp/s1.json')).get('captcha_id',''))")
c=$(neg_login "$CID" "ZZZZ")
[ "$c" = 400 ] && ok "错误验证码 → 400" || bad "错误验证码 → $c（应 400）"

c=$(code "$B/api/auth/captcha")
CID=$(python3 -c "import json;print(json.load(open('/tmp/s1.json')).get('captcha_id',''))")
CODE=$(cap_code "$CID")
[ -n "$CODE" ] && ok "从库中读到验证码答案：$CODE" || bad "库里读不到答案"
# 第一次消费掉它（不管成败）
neg_login "$CID" "$CODE" >/dev/null
c=$(neg_login "$CID" "$CODE")
[ "$c" = 400 ] && ok "验证码一次性消费（重放被拒 400）" || bad "验证码可重放 → $c"

# ★ 限速是产品行为，这里显式断言：同一账号连续失败到阈值后必须被锁定。
#   （不能在内层重置 login_guard —— 那样永远累计不到阈值，等于没测）
guard_reset
locked=0
for k in 1 2 3 4 5 6 7 8 9; do
  c=$(code "$B/api/auth/captcha")
  CID=$(python3 -c "import json;print(json.load(open('/tmp/s1.json')).get('captcha_id',''))")
  r=$(neg_login "$CID" "ZZZZ")
  if [ "$r" = 429 ]; then locked=1; break; fi
done
[ "$locked" = "1" ] && ok "连续 $k 次失败后触发限速锁定（429）" || bad "限速未生效（连续失败仍不限）"
guard_reset

# 正式登录（先重置限速表，避免被上面的负面用例污染）
c=$(code "$B/api/auth/captcha")
CID=$(python3 -c "import json;print(json.load(open('/tmp/s1.json')).get('captcha_id',''))")
CODE=$(cap_code "$CID")
c=$(curl -s -o /tmp/s1.json -w "%{http_code}" -b "$CJ" -c "$CJ" -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$TEST_PW\",\"captcha_id\":\"$CID\",\"captcha_code\":\"$CODE\"}" "$B/api/auth/login")
[ "$c" = 200 ] && ok "正确凭据登录 → 200" || bad "登录 → $c：$(head -c 160 /tmp/s1.json)"
python3 -c "
import json;d=json.load(open('/tmp/s1.json'))
assert 'ok' in d and 'user' in d and 'permissions' in d, sorted(d.keys())
assert 'token' not in d, '响应里不应出现 token 字段'
u=d['user']
for k in ['id','username','role','role_label','display_name','must_change_password']: assert k in u, k
print('    字段:', sorted(d.keys()), '/ user:', sorted(u.keys()))
print('    权限:', d['permissions'])
" && ok "登录响应字段与 Python 版一致，且**不回传 token**" || bad "登录响应字段不符"
grep -q 'gcp_sid' "$CJ" && ok "会话 Cookie gcp_sid 已下发" || bad "未收到 Cookie"

echo
echo "═══ 6. 强制改密（服务端中间件拦截）═══"
python3 -c "
import sqlite3;c=sqlite3.connect('$DB');c.execute('UPDATE users SET must_change_password=1');c.commit()"
c=$(code -H 'Content-Type: application/json' -d '{"count":1}' "$B/api/create")
[ "$c" = 403 ] && ok "未改密 POST /api/create → 403" || bad "→ $c（应 403）"
grep -q 'must_change_password' /tmp/s1.json && ok "错误码 code=must_change_password" || bad "缺 code"
c=$(code "$B/api/status"); [ "$c" = 403 ] && ok "未改密 GET /api/status → 403" || bad "→ $c（应 403）"
c=$(code "$B/api/auth/me"); [ "$c" = 200 ] && ok "白名单 /api/auth/me 放行 → 200" || bad "→ $c"
c=$(code "$B/api/auth/password_policy"); [ "$c" = 200 ] && ok "白名单 /api/auth/password_policy 放行 → 200" || bad "→ $c"
python3 -c "
import sqlite3;c=sqlite3.connect('$DB');c.execute('UPDATE users SET must_change_password=0');c.commit()"

echo
echo "═══ 7. 安全响应头 ═══"
curl -s -D /tmp/s1h.txt -o /dev/null "$B/api/version"
for h in "X-Content-Type-Options: nosniff" "X-Frame-Options: DENY" "Content-Security-Policy" "Referrer-Policy"; do
  grep -qi "^$h" /tmp/s1h.txt && ok "$h" || bad "缺 $h"
done
# CSP 必须含 unsafe-eval（Vue 运行期编译需要，缺了会白屏）
grep -i "^Content-Security-Policy" /tmp/s1h.txt | grep -q "unsafe-eval" \
  && ok "CSP 含 unsafe-eval（Vue 运行期编译要求）" || bad "CSP 缺 unsafe-eval → 控制台会白屏"
grep -i "^Content-Security-Policy" /tmp/s1h.txt | grep -q "font-src 'self' data:" \
  && ok "CSP font-src 含 data:（FontAwesome 内联字体）" || bad "CSP font-src 缺 data:"

echo
printf "\n═══ 汇总：通过 %d 项，失败 %d 项 ═══\n" "$P" "$F"
exit $F
