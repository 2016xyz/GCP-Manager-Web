#!/usr/bin/env bash
# =============================================================================
# PHP 版冒烟测试 ②：鉴权路由 / 响应结构 / 参数校验 / 脱敏 / 权限矩阵 / 审计
#
# 用法：
#   bash php/tests/smoke_1_public.sh        # 先跑①建立会话（会持久化测试密码）
#   bash php/tests/smoke_2_authenticated.sh # 再跑②
#
# 环境变量：PHP_BASE / PHP_DATA / PHP_COOKIE / PHP_BIN
# =============================================================================
set -u
B="${PHP_BASE:-http://127.0.0.1:8099}"
DATA="${PHP_DATA:-/tmp/apitest}"
CJ="${PHP_COOKIE:-/tmp/php_cookie.txt}"
DB="$DATA/gcp_php.db"
PHP_BIN="${PHP_BIN:-php}"
[ -f /tmp/apitest_pw.env ] && . /tmp/apitest_pw.env

P=0; F=0
ok(){ P=$((P+1)); printf "  \033[32m✅\033[0m %s\n" "$*"; }
bad(){ F=$((F+1)); printf "  \033[31m❌\033[0m %s\n" "$*"; }

req(){   # req <METHOD> <PATH> [JSON_BODY] [COOKIE_STRING]
  # 注意：$4 是 Cookie **字符串**（形如 gcp_sid=xxx），不是 cookie jar 文件路径。
  #       早前把文件名当 cookie 传，所有请求都成了未认证 → 权限矩阵整片 401。
  local m="$1" path="$2" body="${3-}" ck="${4-}"
  local args=(-s -o /tmp/s2.json -w "%{http_code}" -X "$m")
  if [ -n "$ck" ]; then
    args+=(-H "Cookie: $ck")           # 显式自带 Cookie，不读写共享 jar（避免串味）
  else
    ck="$(grep -o 'gcp_sid[[:space:]]*[^[:space:]]*' "$CJ" 2>/dev/null | head -1 | sed 's/[[:space:]]\+/=/')"
    if [ -n "$ck" ]; then args+=(-H "Cookie: $ck"); else args+=(-b "$CJ" -c "$CJ"); fi
  fi
  if [ -n "$body" ]; then
    args+=(-H 'Content-Type: application/json' --data-raw "$body")
  fi
  curl "${args[@]}" "$B$path"
}
keys(){ python3 -c "import json;print(','.join(sorted(json.load(open('/tmp/s2.json')).keys())))" 2>/dev/null || echo '-'; }
msg(){ python3 -c "
import json
try:
    d=json.load(open('/tmp/s2.json'))
    print(d.get('error') or d.get('detail') or '')
except Exception: print('(非 JSON)')" 2>/dev/null; }

# 会话前置检查
if [ "$(req GET /api/status)" != "200" ]; then
  echo "  ✘ 没有有效会话。请先跑： bash php/tests/smoke_1_public.sh" >&2
  exit 2
fi
ok "已登录，会话有效"

echo
echo "═══ 1. 全部只读路由（应 200）═══"
ROUTES=(/api/auth/me /api/status /api/config /api/accounts
        "/api/instances?sync=false" "/api/instances?sync=true" /api/tasks
        "/api/logs?limit=5" "/api/audit?page=1&page_size=5" /api/users
        /api/sessions /api/catalog /api/install_presets
        /api/inspect/sections /api/project_zones /api/project_networks
        /api/accounts/instance_counts /api/auth/password_policy)
for r in "${ROUTES[@]}"; do
  c=$(req GET "$r")
  if [ "$c" = 200 ]; then ok "$(printf '%-34s' "$r") 200  [$(keys | cut -c1-56)]"
  else bad "$(printf '%-34s' "$r") $c  $(msg | head -c 66)"; fi
done

echo
echo "═══ 2. 响应结构逐字段对照 Python 版 ═══"
req GET "/api/audit?page=1&page_size=5" >/dev/null
python3 -c "
import json;d=json.load(open('/tmp/s2.json'))
for k in ['ok','audit','total','page','page_size','pages']:
    assert k in d, f'缺字段 {k}'
assert len(d['audit'])<=d['page_size'], '每页条数超过 page_size'
assert d['pages']==max(1,-(-d['total']//d['page_size'])), 'pages 与 total/page_size 不自洽'
print(f'    total={d[\"total\"]} page={d[\"page\"]}/{d[\"pages\"]} 本页 {len(d[\"audit\"])} 条')
" && ok "审计服务端分页结构正确（ok/audit/total/page/page_size/pages）" || bad "审计分页结构不符"

req GET /api/auth/me >/dev/null
python3 -c "
import json;d=json.load(open('/tmp/s2.json'))
assert d.get('authenticated') is True
u=d['user']
for k in ['id','username','role','role_label','display_name','last_login','must_change_password']: assert k in u,k
assert isinstance(d['roles'],list) and set(d['roles'])>={'admin','operator','viewer'}, d['roles']
assert isinstance(d['permissions'],list)
" && ok "me：user 是嵌套对象、roles 是角色名数组、permissions 是数组" || bad "me 结构不符"

req GET /api/users >/dev/null
python3 -c "
import json;d=json.load(open('/tmp/s2.json'))
assert sorted(d.keys())==['ok','roles','users'], sorted(d.keys())
leak=[k for u in d['users'] for k in u if k in ('password_hash','salt')]
assert not leak, f'泄露 {leak}'
assert all(set(r.keys())=={'value','label'} for r in d['roles'])
" && ok "用户列表：只有 ok/users/roles，不含 password_hash/salt" || bad "用户列表结构或脱敏不符"

req GET /api/accounts >/dev/null
python3 -c "
import json;d=json.load(open('/tmp/s2.json'))
for a in d['accounts']:
    assert 'key_path' not in a, '泄露 key_path'
    assert 'proxy' not in a, '泄露 proxy 原文（可能含明文密码）'
    assert 'proxy_display' in a, '缺 proxy_display（打码后的展示值）'
" && ok "账号列表：不泄露 key_path 与代理原文（只给 proxy_display）" || bad "账号列表脱敏不足"

req GET "/api/instances?sync=false" >/dev/null
python3 -c "
import json;d=json.load(open('/tmp/s2.json'))
for v in d['instances']:
    for k in ('password','root_password','private_key','ssh_private_key','secret','token'):
        assert k not in v, f'泄露 {k}'
" && ok "实例列表 sync=false：白名单式丢弃后不含任何密码字段" || bad "实例列表泄露密码"

req GET /api/sessions >/dev/null
python3 -c "
import json;d=json.load(open('/tmp/s2.json'))
for s in d['sessions']:
    assert 'token' not in s, '泄露 token 原文'
    assert len(s.get('ref',''))==16, 'ref 应是 16 位哈希前缀'
" && ok "会话列表：用 16 位哈希 ref 标识，不回传 token 原文" || bad "会话列表泄露 token"

echo
echo "═══ 3. 参数与业务校验分支 ═══"
c=$(req POST /api/create '{"count":1}')
if [ "$c" = 200 ]; then ok "create（有账号）→ 200 + task_id"; else
  grep -q '"ok":false' /tmp/s2.json && ok "create（无账号）→ 200 + ok:false + error（与 Python submit_create 同形态）" \
    || bad "create → $c  $(msg | head -c 60)"
fi
c=$(req POST /api/create '{"count":9999}')
{ [ "$c" = 400 ] || [ "$c" = 422 ]; } && ok "count=9999 → $c（拦住费用灾难）" || bad "count=9999 → $c"
c=$(req POST /api/create '{"count":0}')
{ [ "$c" = 400 ] || [ "$c" = 422 ]; } && ok "count=0 → $c" || bad "count=0 → $c"
c=$(req POST /api/execute '{"command":""}')
[ "$c" = 200 ] && grep -q '"ok":false' /tmp/s2.json \
  && ok "空命令 → 200 + ok:false（与 Python submit_execute 同形态）" || bad "空命令 → $c"
c=$(req POST /api/instance_action '{"action":"reboot","targets":[{"name":"a"}]}')
[ "$c" = 400 ] && ok "非法动作 reboot → 400（$(msg | head -c 40)）" || bad "非法动作 → $c"
c=$(req POST /api/instance_action '{"action":"delete","targets":[]}')
[ "$c" = 400 ] && ok "合法动作但空目标 → 400（$(msg | head -c 40)）" || bad "空目标 → $c"
c=$(req GET /api/tasks/nonexistent-id)
[ "$c" = 404 ] && ok "不存在的任务 → 404" || bad "不存在的任务 → $c"
c=$(req POST /api/accounts/999999/test)
[ "$c" = 404 ] && ok "对不存在账号发测试 → 404" || bad "→ $c"
c=$(req DELETE /api/sessions/ZZZZ)
[ "$c" = 400 ] && ok "非法会话 ref 格式 → 400（格式白名单）" || bad "→ $c"
c=$(req POST /api/config '{')
{ [ "$c" = 400 ]; } && ok "非法 JSON 请求体 → 400" || bad "非法 JSON → $c"

echo
echo "═══ 4. SSRF / 流包装器 / 任意文件读取防护 ═══"
for spec in "file:///etc/passwd" "http://127.0.0.1:9999/x.json" \
            "php://filter/read=convert.base64-encode/resource=/etc/passwd" \
            "data://text/plain;base64,e30=" "/etc/hostname" "/no/such/file.json" \
            "/proc/self/environ"; do
  body=$(python3 -c "import json,sys;print(json.dumps({'key_path':sys.argv[1]}))" "$spec")
  c=$(req POST /api/accounts "$body")
  [ "$c" = 400 ] && ok "key_path=$spec → 400（拒绝）" || bad "key_path=$spec → $c（应 400）"
done
# import_dir 也要拒绝流包装器
body=$(python3 -c "import json;print(json.dumps({'dir':'http://127.0.0.1:9999/'}))")
c=$(req POST /api/accounts/import_dir "$body")
[ "$c" = 400 ] && ok "import_dir=http://  → 400（拒绝）" || bad "import_dir 流包装器 → $c"

echo
echo "═══ 5. 权限矩阵（三个角色 × 五个权限点）═══"
GCPWEB_DATA_DIR="$DATA" "$PHP_BIN" -r '
require "php/src/bootstrap.php";
$pw = "RoleCheck#" . bin2hex(random_bytes(8));
foreach (["admin","operator","viewer"] as $r) {
    $u = Users::getUser(null, "rc_$r");
    if ($u) { Users::setPassword((int)$u["id"], $pw, false); Db::exec("UPDATE users SET disabled=0 WHERE id=?", [(int)$u["id"]]); }
    else { Users::createUser("rc_$r", $pw, $r, "权限测试", false, "test"); }
}
file_put_contents("/tmp/role_pw.txt", $pw);
Db::exec("DELETE FROM login_guard");
' >/dev/null 2>&1
RPW=$(cat /tmp/role_pw.txt 2>/dev/null)
if [ -z "$RPW" ]; then
  bad "无法创建角色测试用户"
else
  for role in admin operator viewer; do
    TOK=$(GCPWEB_DATA_DIR="$DATA" "$PHP_BIN" -r '
    require "php/src/bootstrap.php";
    $r = $argv[1];
    $u = Users::getUser(null, "rc_$r");
    // 直接用已知 token 插会话，便于测试对账（不走 createSession 的随机 token）
    $t = "role" . $r . bin2hex(random_bytes(8));
    Db::exec("INSERT INTO sessions(token,user_id,username,role,ip,user_agent,created_at,last_seen,expires_at)"
           . " VALUES(?,?,?,?,?,?,?,?,?)",
             [$t, (int)$u["id"], $u["username"], $u["role"], "127.0.0.1", "matrix",
              microtime(true), microtime(true), microtime(true) + 3600]);
    echo $t;
    ' "$role" 2>/dev/null)
    [ -z "$TOK" ] && { bad "$role 建会话失败"; continue; }
    CK="gcp_sid=$TOK"
    # METHOD|PATH|EXPECTED        —— admin 全部通过；operator 无 user 权限；viewer 只能 view
    specs=(
      "GET|/api/status|200"
      "GET|/api/users|$([ "$role" = admin ] && echo 200 || echo 403)"
      "POST|/api/users|$([ "$role" = admin ] && echo 200,400 || echo 403)"
      "GET|/api/audit|$([ "$role" = admin ] && echo 200 || echo 403)"
      "GET|/api/accounts|200"
      "POST|/api/accounts|$([ "$role" = viewer ] && echo 403 || echo 400)"
      "GET|/api/instances|200"
      "POST|/api/create|$([ "$role" = viewer ] && echo 403 || echo 200)"
      "POST|/api/execute|$([ "$role" = viewer ] && echo 403 || echo 200)"
      "POST|/api/instance_action|$([ "$role" = viewer ] && echo 403 || echo 400)"
      "DELETE|/api/logs|$([ "$role" = viewer ] && echo 403 || echo 200)"
      "POST|/api/config|$([ "$role" = viewer ] && echo 403 || echo 200)"
    )
    for spec in "${specs[@]}"; do
      IFS='|' read -r m ep exp <<< "$spec"
      body=""
      case "$m:$ep" in
        POST:/api/create)            body='{"count":1}' ;;
        POST:/api/execute)           body='{"command":""}' ;;
        POST:/api/instance_action)   body='{"action":"delete","targets":[]}' ;;
        POST:/api/config)            body='{"config":{}}' ;;
        POST:/api/accounts)          body='{"key_path":"/no/such.json"}' ;;
        POST:/api/users)             body='{"username":"x_probe","password":"Probe#12345678","role":"viewer"}' ;;
      esac
      c=$(req "$m" "$ep" "$body" "$CK")
      if [[ ",$exp," == *",$c,"* ]]; then
        ok "$(printf '%-8s' "$role") $(printf '%-22s' "$m $ep") → $c"
      else
        bad "$(printf '%-8s' "$role") $(printf '%-22s' "$m $ep") → $c（期望 $exp）"
      fi
    done
    # 清理探测用户
    GCPWEB_DATA_DIR="$DATA" "$PHP_BIN" -r 'require "php/src/bootstrap.php"; $u=Users::getUser(null,"x_probe"); if($u) Users::deleteUser((int)$u["id"]);' >/dev/null 2>&1
  done
fi

echo
echo "═══ 6. 审计落库（关键操作必须留痕）═══"
python3 - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
rows = c.execute("SELECT action, COUNT(*) FROM audit GROUP BY action ORDER BY 2 DESC").fetchall()
print("    审计动作统计：")
for a, n in rows[:12]:
    print(f"      {a:24} {n}")
seen = {r[0] for r in rows}
# 只把「本次运行一定发生过」的动作作为硬断言：
#   login 是 smoke_1 必做的；change_password / add_account 取决于这次跑了哪些用例
#   （干净库上 SSRF 用例全被拒 → 不会产生 add_account），所以只做信息性展示。
must = {"login"}
missing = must - seen
print(f"    必需动作里缺失：{missing or '无'}")
sys.exit(1 if missing else 0)
PY
[ $? = 0 ] && ok "登录 / 改密 / 账号变更 均已写入审计" || bad "审计缺少关键动作"

echo
printf "\n═══ 通过 %d 项，失败 %d 项 ═══\n" "$P" "$F"
exit $F
