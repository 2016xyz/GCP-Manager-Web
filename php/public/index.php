<?php
/**
 * index.php —— 唯一入口：中间件 + 路由分发
 *
 * 请求处理顺序（顺序本身就是安全设计的一部分，不可调换）：
 *   1. bootstrap：自动加载、关掉错误回显、下发安全响应头
 *   2. 静态资源与页面路由（/login、/、/static/*、/favicon.ico）
 *   3. 会话解析（Auth::currentSession，从 Cookie 取 token 查 SQLite）
 *   4. ★ 强制改密拦截：must_change 为真时，除白名单外一律 403
 *      —— 必须在路由分发**之前**，否则新增路由会忘掉这个检查（Python 版第一轮
 *         审计就踩过：只有提示没有拦截，未改密照样能调 /api/create）
 *   5. 路由查表 → 权限点校验（Auth::requirePerm）→ 调 handler
 *
 * 路由表是「单一事实来源」：路径、方法、权限点都写在一处，便于审计时逐条核对
 * 是否漏了鉴权。任何 handler 都不能绕过 requirePerm。
 *
 * WebSocket（/ws/logs）由 bin/ws-server.php 单独提供，不在此文件里 ——
 * PHP-FPM 无法承载长连接。未启动该服务时前端自动退化为轮询。
 */

declare(strict_types=1);

require __DIR__ . '/../src/bootstrap.php';

// ── 静态资源与页面 ──────────────────────────────────────────────────────────
$path = Http::path();

// 页面路由（不在 /api 下）
if ($path === '/' || $path === '/index.html' || $path === '/console') {
    serveStatic('console.html');
}
if ($path === '/login' || $path === '/login.html') {
    serveStatic('login.html');
}
if ($path === '/favicon.ico') {
    serveStatic('favicon.ico');
}
if (strpos($path, '/static/') === 0) {
    // 交给 Web 服务器处理更高效；走到这里说明用 PHP 内置服务器且路由没放行
    serveStatic(substr($path, strlen('/static/')));
}

/**
 * 输出 public/static 下的静态文件。
 * 路径必须规范化后仍落在 static 目录内 —— 否则 /static/../../data/gcp_php.db
 * 就能把数据库读走。realpath + 前缀比较是这里唯一可靠的防线。
 */
function serveStatic(string $rel): void
{
    $base = Config::publicDir() . '/static';
    $rel  = str_replace("\0", '', $rel);
    $abs  = realpath($base . '/' . ltrim($rel, '/'));
    if ($abs === false || strncmp($abs, $base . DIRECTORY_SEPARATOR, strlen($base) + 1) !== 0
        || !is_file($abs)) {
        http_response_code(404);
        header('Content-Type: text/plain; charset=utf-8');
        echo '404 Not Found';
        exit;
    }
    // 绝不把可执行/敏感文件当静态资源吐出去
    if (preg_match('/\.(php|db|db-wal|db-shm|sqlite|sqlite3|pem|key|ini|sh|md|log|env)$/i', $abs)) {
        http_response_code(404);
        exit;
    }

    $mime = static function (string $f): string {
        static $map = [
            'html' => 'text/html; charset=utf-8',
            'css'  => 'text/css; charset=utf-8',
            'js'   => 'application/javascript; charset=utf-8',
            'json' => 'application/json; charset=utf-8',
            'ico'  => 'image/x-icon',
            'png'  => 'image/png',
            'svg'  => 'image/svg+xml',
            'woff' => 'font/woff',
            'woff2'=> 'font/woff2',
            'ttf'  => 'font/ttf',
            'map'  => 'application/json; charset=utf-8',
        ];
        $e = strtolower(pathinfo($f, PATHINFO_EXTENSION));
        return $map[$e] ?? 'application/octet-stream';
    };

    $etag = '"' . substr(sha1_file($abs) ?: (string) filesize($abs), 0, 20) . '"';
    header('Content-Type: ' . $mime($abs));
    header('ETag: ' . $etag);
    header('Cache-Control: public, max-age=' . (preg_match('/\.(ico|woff2?|ttf)$/', $abs) ? 604800 : 3600));
    if (($_SERVER['HTTP_IF_NONE_MATCH'] ?? '') === $etag) {
        http_response_code(304);
        exit;
    }
    readfile($abs);
    exit;
}

// ── /api/* 之外的一切都是 404（不泄漏任何信息）───────────────────────────────
if (strpos($path, '/api/') !== 0) {
    http_response_code(404);
    header('Content-Type: text/plain; charset=utf-8');
    echo '404 Not Found';
    exit;
}

// ── 路由表：方法 + 路径正则 + 权限点 + handler ───────────────────────────────
// perm：'public' = 无需登录；'user'/'view'/… = Auth::PERMISSIONS 里的权限点
// ws:   true = 该路径由独立进程提供（这里只做提示）
$ROUTES = [
    // ---- 认证（公开）----
    // ★ 与 Python 版 app.py 的 PUBLIC_PATHS 严格对齐：
    //   /login /api/auth/login /api/auth/captcha /api/auth/logout
    //   /api/auth/me /favicon.ico /api/version (+ /static/ 前缀)
    //   实测发现 /api/auth/password_policy **不在**白名单里（需要登录），
    //   早前误标为 public，会让 PHP 版比 Python 版宽松 —— 已改为 view。
    ['GET',    '#^/api/auth/captcha$#',          'public', [ApiAuth::class, 'captcha']],
    ['POST',   '#^/api/auth/login$#',            'public', [ApiAuth::class, 'login']],
    ['POST',   '#^/api/auth/logout$#',           'public', [ApiAuth::class, 'logout']],
    ['GET',    '#^/api/auth/password_policy$#',  'view',   [ApiAuth::class, 'passwordPolicy']],
    ['GET',    '#^/api/version$#',               'public', [ApiAuth::class, 'version']],
    // ---- 认证（需登录）----
    ['GET',    '#^/api/auth/me$#',               'public', [ApiAuth::class, 'me']],
    ['POST',   '#^/api/auth/change_password$#',  'user',   [ApiAuth::class, 'changePassword']],
    // ---- 用户 / 会话 / 审计（admin）----
    ['GET',    '#^/api/users$#',                 'user',   [ApiAuth::class, 'listUsers']],
    ['POST',   '#^/api/users$#',                 'user',   [ApiAuth::class, 'createUser']],
    ['PATCH',  '#^/api/users/(\d+)$#',           'user',   [ApiAuth::class, 'updateUser']],
    ['POST',   '#^/api/users/(\d+)/password$#',  'user',   [ApiAuth::class, 'resetUserPassword']],
    ['DELETE', '#^/api/users/(\d+)$#',           'user',   [ApiAuth::class, 'deleteUser']],
    ['GET',    '#^/api/sessions$#',              'user',   [ApiAuth::class, 'listSessions']],
    ['DELETE', '#^/api/sessions/([^/]+)$#',      'user',   [ApiAuth::class, 'killSession']],
    ['GET',    '#^/api/audit$#',                 'user',   [ApiAuth::class, 'audit']],
    // ---- 概览 / 配置（只读 view）----
    ['GET',    '#^/api/status$#',                'view',   [ApiGcp::class, 'status']],
    ['GET',    '#^/api/config$#',                'view',   [ApiGcp::class, 'getConfig']],
    ['POST',   '#^/api/config$#',                'settings', [ApiGcp::class, 'setConfig']],
    ['POST',   '#^/api/savings$#',               'view',   [ApiGcp::class, 'savings']],
    ['POST',   '#^/api/cost/estimate$#',         'view',   [ApiGcp::class, 'costEstimate']],
    ['GET',    '#^/api/catalog$#',               'view',   [ApiGcp::class, 'catalog']],
    ['GET',    '#^/api/install_presets$#',       'view',   [ApiGcp::class, 'installPresets']],
    ['GET',    '#^/api/project_networks$#',      'view',   [ApiGcp::class, 'projectNetworks']],
    ['GET',    '#^/api/project_zones$#',         'view',   [ApiGcp::class, 'projectZones']],
    ['GET',    '#^/api/inspect/sections$#',      'view',   [ApiGcp::class, 'inspectSections']],
    ['GET',    '#^/api/inspect$#',               'view',   [ApiGcp::class, 'inspect']],
    // ---- GCP 账号（account 权限）----
    ['GET',    '#^/api/accounts$#',              'view',   [ApiGcp::class, 'listAccounts']],
    ['POST',   '#^/api/accounts$#',              'account',[ApiGcp::class, 'addAccount']],
    ['POST',   '#^/api/accounts/upload$#',       'account',[ApiGcp::class, 'uploadAccount']],
    ['POST',   '#^/api/accounts/import_dir$#',   'account',[ApiGcp::class, 'importDir']],
    ['GET',    '#^/api/accounts/instance_counts$#', 'view', [ApiGcp::class, 'accountInstanceCounts']],
    ['POST',   '#^/api/accounts/(\d+)/test$#',   'view',   [ApiGcp::class, 'testAccount']],
    ['POST',   '#^/api/accounts/(\d+)/test_proxy$#', 'view', [ApiGcp::class, 'testAccountProxy']],
    ['PATCH',  '#^/api/accounts/(\d+)$#',        'account',[ApiGcp::class, 'updateAccount']],
    ['DELETE', '#^/api/accounts/(\d+)$#',        'account',[ApiGcp::class, 'deleteAccount']],
    // ---- 实例 ----
    ['GET',    '#^/api/instances$#',             'view',   [ApiGcp::class, 'instances']],
    ['PATCH',  '#^/api/instances/note$#',        'operate',[ApiGcp::class, 'updateInstanceNote']],
    ['POST',   '#^/api/instances/password$#',    'view',   [ApiGcp::class, 'revealInstancePassword']],
    ['POST',   '#^/api/refresh$#',               'view',   [ApiGcp::class, 'refresh']],
    ['POST',   '#^/api/create$#',                'operate',[ApiGcp::class, 'create']],
    ['POST',   '#^/api/execute$#',               'operate',[ApiGcp::class, 'execute']],
    ['POST',   '#^/api/instance_action$#',       'operate',[ApiGcp::class, 'instanceAction']],
    // ---- 任务 / 日志 / 密钥 ----
    ['GET',    '#^/api/tasks$#',                 'view',   [ApiTask::class, 'listTasks']],
    ['GET',    '#^/api/tasks/([A-Za-z0-9_\-]+)$#', 'view', [ApiTask::class, 'getTask']],
    ['POST',   '#^/api/tasks/([A-Za-z0-9_\-]+)/cancel$#', 'operate', [ApiTask::class, 'cancelTask']],
    ['GET',    '#^/api/logs$#',                  'view',   [ApiTask::class, 'logs']],
    ['DELETE', '#^/api/logs$#',                  'operate',[ApiTask::class, 'clearLogs']],
    ['POST',   '#^/api/sshkey/generate$#',       'operate',[ApiTask::class, 'generateSshKey']],
    ['POST',   '#^/api/sshkey/read$#',           'operate',[ApiTask::class, 'readSshKey']],
];

$method = Http::method();
if ($method === 'HEAD') {
    $method = 'GET';           // HEAD 按 GET 处理（PHP 会自动丢弃 body）
}

$matched = null;
$params  = [];
$pathFound = false;

foreach ($ROUTES as $r) {
    if ($r[1] !== null && preg_match($r[1], $path, $m)) {
        $pathFound = true;
        if ($r[0] === $method) {
            $matched = $r;
            $params  = array_slice($m, 1);
            break;
        }
    }
}

if ($matched === null) {
    if (strpos($path, '/api/') !== 0) {
        http_response_code(404);
        echo '404 Not Found';
        exit;
    }
    // 路径存在但方法不对 → 405；路径不存在 → 404。不泄漏哪个是真的存在。
    if ($pathFound) {
        header('Allow: ' . implode(', ', array_map(
            static fn($x) => $x[0],
            array_filter($ROUTES, static fn($x) => preg_match($x[1], $path))
        )));
        Json::err('方法不被允许', 405);
    }
    Json::err('接口不存在', 404, 'not_found');
}

// ── 中间件：会话 + 强制改密 + 权限 ──────────────────────────────────────────
$perm = $matched[2];

if ($perm !== 'public') {
    $sess = Auth::currentSession();
    if ($sess === null) {
        Json::err('未登录或会话已过期', 401, 'unauthenticated');
    }

    // ★ 强制改密：白名单之外一律拦截。
    //   白名单是「改密本身 + 登出 + 查自己 + 查密码策略」，
    //   否则用户连改密页面都打不开，会死锁。
    $mustChange = !empty($sess['must_change']);
    if ($mustChange && !Auth::isMustChangeAllowed($path)) {
        Json::err('必须先修改初始密码才能使用其他功能', 403, 'must_change_password');
    }

    Auth::requirePerm($perm);   // 权限不足时内部直接 Json::err(403) 并 exit
}

// ── 分发 ────────────────────────────────────────────────────────────────────
try {
    $matched[3]($params);
} catch (Throwable $e) {
    // 不把内部细节回给客户端；细节进 error_log
    Json::fail($e);
}
