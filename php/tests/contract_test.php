<?php
/**
 * contract_test.php —— PHP 版与 Python 版的 API 契约一致性验证
 *
 * 目的：证明「前端可以原样复用」这个前提成立 —— 两版对同一请求返回的
 *      状态码、JSON 顶层字段集合必须一致。
 *
 * 做法（真发 HTTP，不 mock）：
 *   1. 分别登录 Python 版（默认 :8000）与 PHP 版（默认 :8080）
 *      · Python 版的验证码明文由测试夹具 tools/ui_login_probe.py 提供（仅本机）
 *      · PHP 版直接从 SQLite 里读验证码答案（本就是落库存储的）
 *   2. 对每条路由发同样的请求，比对：状态码 + JSON 顶层 key 集合 + ok/detail 形态
 *   3. 输出逐条结果与汇总
 *
 * 用法：
 *   php tests/contract_test.php \
 *       --py http://127.0.0.1:8000 --php http://127.0.0.1:8080 \
 *       --probe http://127.0.0.1:8001 --user admin --pass 'xxx' \
 *       [--php-data /path/to/php/data]
 *
 * 退出码：0 = 全部一致；1 = 存在差异或无法完成比对。
 */

declare(strict_types=1);

require __DIR__ . '/../src/bootstrap.php';

// ── 参数 ────────────────────────────────────────────────────────────────────
$opt = [
    'py'       => 'http://127.0.0.1:8000',
    'php'      => 'http://127.0.0.1:8080',
    'probe'    => 'http://127.0.0.1:8001',
    'user'     => getenv('GCPWEB_ADMIN_USER') ?: 'admin',
    'pass'     => getenv('GCPWEB_ADMIN_PW') ?: '',
    'php-data' => '',
    'timeout'  => '20',
];
for ($i = 1; $i < $argc; $i++) {
    $a = $argv[$i];
    if (preg_match('/^--([a-z\-]+)=(.*)$/', $a, $m)) {
        $opt[$m[1]] = $m[2];
    } elseif (preg_match('/^--([a-z\-]+)$/', $a, $m) && isset($argv[$i + 1])) {
        $opt[$m[1]] = $argv[++$i];
    }
}
if ($opt['pass'] === '') {
    // 支持从文件读（与 Python 版测试夹具同一约定）
    foreach (['/opt/gcp-manager-web/data/INITIAL_ADMIN.txt', __DIR__ . '/../data/INITIAL_ADMIN.txt'] as $f) {
        if (is_file($f)) {
            $opt['pass'] = trim((string) file_get_contents($f));
            break;
        }
    }
}
if ($opt['pass'] === '') {
    fwrite(STDERR, "缺少管理员密码：用 --pass= 传入，或让 data/INITIAL_ADMIN.txt 存在\n");
    exit(1);
}

$PASS = 0;
$FAIL = 0;
$SKIP = 0;
$failures = [];

function ok(string $m): void   { global $PASS; $PASS++; printf("  \033[32m✅\033[0m %s\n", $m); }
function bad(string $m): void  { global $FAIL, $failures; $FAIL++; $failures[] = $m; printf("  \033[31m❌\033[0m %s\n", $m); }
function skip(string $m): void { global $SKIP; $SKIP++; printf("  \033[33m⏭\033[0m  %s\n", $m); }
function sec(string $m): void  { printf("\n\033[1m── %s\033[0m\n", $m); }

/**
 * 发请求。返回 [status, headers(小写名=>值), body, json|null]
 * 用 curl 手动处理 Cookie，这样才能把两版各自的会话分别带上。
 */
function req(string $url, string $method = 'GET', array $body = null, string $cookie = '',
             array $extraHeaders = [], int $timeout = 20): array
{
    $ch = curl_init($url);
    $hdrs = ['Accept: application/json'];
    foreach ($extraHeaders as $h) { $hdrs[] = $h; }
    if ($cookie !== '') { $hdrs[] = 'Cookie: ' . $cookie; }
    $opts = [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HEADER         => true,
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_FOLLOWLOCATION => false,
        CURLOPT_TIMEOUT        => $timeout,
        CURLOPT_CONNECTTIMEOUT => 5,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_HTTPHEADER     => $hdrs,
    ];
    if ($body !== null) {
        $opts[CURLOPT_POSTFIELDS] = json_encode($body, JSON_UNESCAPED_UNICODE);
        $hdrs[] = 'Content-Type: application/json';
        $opts[CURLOPT_HTTPHEADER] = $hdrs;
    }
    curl_setopt_array($ch, $opts);
    $raw  = curl_exec($ch);
    $err  = curl_error($ch);
    $code = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    $hlen = (int) curl_getinfo($ch, CURLINFO_HEADER_SIZE);
    curl_close($ch);
    if ($raw === false) {
        return [0, [], 'curl error: ' . $err, null];
    }
    $rawHeaders = substr($raw, 0, $hlen);
    $b          = substr($raw, $hlen);
    $headers    = [];
    foreach (explode("\r\n", $rawHeaders) as $line) {
        $p = strpos($line, ':');
        if ($p !== false) {
            $headers[strtolower(substr($line, 0, $p))] = trim(substr($line, $p + 1));
        }
    }
    $j = json_decode($b, true);
    return [$code, $headers, $b, is_array($j) ? $j : null];
}

/** 从响应头里抽 Set-Cookie 的 name=value（只需值部分） */
function cookieFrom(array $headers): string
{
    $sc = $headers['set-cookie'] ?? '';
    if ($sc === '') { return ''; }
    $out = [];
    foreach (explode("\n", str_replace("\r\n", "\n", $sc)) as $one) {
        $kv = trim(explode(';', $one)[0]);
        if ($kv !== '' && strpos($kv, '=') !== false) { $out[] = $kv; }
    }
    return implode('; ', $out);
}

function keysOf(?array $j): array
{
    if (!is_array($j)) { return []; }
    $k = array_keys($j);
    sort($k);
    return $k;
}

// ── 登录 ────────────────────────────────────────────────────────────────────
sec('0. 登录两版（验证码走各自的通路：Python 用探针，PHP 读 SQLite）');

function loginPython(array $opt): array
{
    [, , $b] = req($opt['py'] . '/api/auth/captcha');
    $c = json_decode($b, true);
    $cid = $c['captcha_id'] ?? $c['id'] ?? null;
    $code = null;
    if ($cid !== null) {
        [, , $pb] = req($opt['probe'] . '/__probe/captcha/' . rawurlencode((string) $cid));
        $pj = json_decode($pb, true);
        $code = $pj['code'] ?? $pj['text'] ?? null;
    }
    [$st, $h, ] = req($opt['py'] . '/api/auth/login', 'POST',
        ['username' => $opt['user'], 'password' => $opt['pass'],
         'captcha_id' => $cid, 'captcha_code' => $code]);
    return [$st, cookieFrom($h)];
}

function loginPhp(array $opt): array
{
    [$st0, $h0, $b0] = req($opt['php'] . '/api/auth/captcha');
    $c = json_decode($b0, true);
    $cid = $c['captcha_id'] ?? $c['id'] ?? null;
    $code = null;

    // PHP 版把验证码落 SQLite 表 captcha(cid, code, expire, created)
    // （PHP-FPM 是多进程，内存不共享，所以必须落库 —— 见 Auth.php 的说明）
    $dataDir = $opt['php-data'] !== '' ? $opt['php-data'] : __DIR__ . '/../data';
    $dbFile  = rtrim($dataDir, '/') . '/gcp_php.db';
    if ($cid !== null && is_file($dbFile)) {
        try {
            $pdo = new PDO('sqlite:' . $dbFile, null, null, [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
            $st = $pdo->prepare('SELECT code FROM captcha WHERE cid = ? LIMIT 1');
            $st->execute([(string) $cid]);
            $code = $st->fetchColumn() ?: null;
        } catch (Throwable $e) {
            // 读不到就让登录失败，由下面的断言报出来（不静默跳过）
        }
    }
    [$st, $h, ] = req($opt['php'] . '/api/auth/login', 'POST',
        ['username' => $opt['user'], 'password' => $opt['pass'],
         'captcha_id' => $cid, 'captcha_code' => $code]);
    return [$st, cookieFrom($h)];
}

[$pySt, $pyCookie] = loginPython($opt);
[$phSt, $phCookie] = loginPhp($opt);

if ($pySt === 200) { ok("Python 版登录成功（HTTP 200）"); }
else { bad("Python 版登录失败（HTTP $pySt）—— 检查 :8000 是否在跑、探针 :8001 是否起着"); }

if ($phSt === 200) { ok("PHP 版登录成功（HTTP 200）"); }
else { bad("PHP 版登录失败（HTTP $phSt）—— 检查 :8080 是否在跑、data/gcp_php.db 是否存在"); }

if ($pyCookie === '' || $phCookie === '') {
    bad('未能取到会话 Cookie，后续鉴权比对无法进行');
}

// ── 路由清单（与 INTERFACES.md 第 3.5 节一致）────────────────────────────────
sec('1. 公开接口（无需登录）契约比对');

$publicRoutes = [
    ['GET',  '/api/version',   ['version']],
    ['GET',  '/api/auth/me',   []],            // 未登录应为 401
    ['GET',  '/api/status',    []],            // 未登录应为 401
    ['GET',  '/favicon.ico',   null],          // 二进制，只比状态码
];

foreach ($publicRoutes as [$m, $p, $mustKeys]) {
    [$s1, $h1, $b1, $j1] = req($opt['py'] . $p, $m);
    [$s2, $h2, $b2, $j2] = req($opt['php'] . $p, $m);
    $label = "$m $p";
    if ($s1 !== $s2) {
        bad("$label 状态码不一致：Python $s1 vs PHP $s2");
        continue;
    }
    if ($mustKeys === null) {
        ok("$label 状态码一致（$s1）");
        continue;
    }
    $k1 = keysOf($j1);
    $k2 = keysOf($j2);
    if ($k1 !== $k2) {
        bad("$label 顶层字段不一致：Python [" . implode(',', $k1) . '] vs PHP [' . implode(',', $k2) . ']');
    } else {
        ok("$label 一致（HTTP $s1，字段 " . implode(',', $k1) . '）');
    }
    foreach ($mustKeys as $mk) {
        if (!in_array($mk, $k2, true)) {
            bad("$label PHP 响应缺少必需字段 $mk");
        }
    }
}

sec('2. 安全响应头比对（两版都必须有）');
foreach ([['Python', $opt['py']], ['PHP', $opt['php']]] as [$name, $base]) {
    [, $h, ] = req($base . '/api/version');
    foreach (['x-content-type-options', 'x-frame-options', 'referrer-policy',
              'content-security-policy'] as $hn) {
        if (isset($h[$hn])) { ok("$name 有 $hn: " . mb_substr($h[$hn], 0, 60)); }
        else { bad("$name 缺少安全头 $hn"); }
    }
}

sec('3. 需登录接口契约比对（带上各自的会话 Cookie）');

$authRoutes = [
    ['GET',    '/api/auth/password_policy', null],
    ['GET',    '/api/config',               null],
    ['GET',    '/api/accounts',             null],
    ['GET',    '/api/instances?sync=false',  null],
    ['GET',    '/api/tasks',                null],
    ['GET',    '/api/logs?limit=5',          null],
    ['GET',    '/api/audit?page=1&page_size=5', null],
    ['GET',    '/api/install_presets',      null],
    ['GET',    '/api/catalog',              null],
    ['GET',    '/api/project_zones',        null],
    ['GET',    '/api/project_networks',     null],
    ['GET',    '/api/users',                null],
    ['GET',    '/api/sessions',             null],
    ['GET',    '/api/inspect/sections',     null],
    ['GET',    '/api/accounts/instance_counts', null],
];

foreach ($authRoutes as [$m, $p, $_]) {
    [$s1, , , $j1] = req($opt['py'] . $p, $m, null, $pyCookie);
    [$s2, , , $j2] = req($opt['php'] . $p, $m, null, $phCookie);
    $label = "$m $p";
    if ($s1 !== $s2) {
        bad("$label 状态码不一致：Python $s1 vs PHP $s2");
        continue;
    }
    $k1 = keysOf($j1);
    $k2 = keysOf($j2);
    if ($k1 !== $k2) {
        bad("$label 字段不一致：Python [" . implode(',', $k1) . '] vs PHP [' . implode(',', $k2) . ']');
    } else {
        ok("$label 一致（HTTP $s1，字段 " . (implode(',', $k1) ?: '(无)') . '）');
    }
}

sec('4. 错误响应形态（前端读的是 detail）');
foreach ([['Python', $opt['py'], $pyCookie], ['PHP', $opt['php'], $phCookie]] as [$name, $base, $ck]) {
    [$st, , , $j] = req($base . '/api/users/999999', 'DELETE', null, $ck);
    if ($j !== null) {
        if (array_key_exists('detail', $j) || array_key_exists('ok', $j)) {
            ok("$name 错误响应含 detail/ok（HTTP $st）");
        } else {
            bad("$name 错误响应既无 detail 也无 ok：" . json_encode(array_keys($j), JSON_UNESCAPED_UNICODE));
        }
    } else {
        bad("$name 错误响应不是 JSON（HTTP $st）");
    }
}

// ── 汇总 ────────────────────────────────────────────────────────────────────
printf("\n%s\n", str_repeat('=', 68));
printf("通过 %d 项，失败 %d 项，跳过 %d 项\n", $PASS, $FAIL, $SKIP);
if ($failures) {
    printf("\n失败明细：\n");
    foreach ($failures as $f) { printf("  - %s\n", $f); }
}
printf("%s\n\n", str_repeat('=', 68));
exit($FAIL === 0 ? 0 : 1);
