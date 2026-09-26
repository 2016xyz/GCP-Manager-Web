#!/usr/bin/env php
<?php
/**
 * ws-server —— 纯 PHP WebSocket 服务（/ws/logs 实时日志）
 *
 * 为什么不用 composer 库（如 Ratchet / Workerman）：
 *   契约要求「零 composer 依赖」。这里用 stream_socket_server + 手写
 *   RFC6455 握手与帧编解码，只依赖 PHP 内置 stream/openssl 函数。
 *
 * 监听地址：GCPWEB_HOST（默认 127.0.0.1）/ GCPWEB_PORT（默认 9001）
 * 反向代理把 /ws/logs 转发到本服务。
 *
 * ★ 鉴权（与 Python 版一致）
 *   从握手请求的 Cookie 里取会话 token（gcp_sid），去 SQLite 的 sessions 表校验：
 *     · 无 token / token 无效 / 已过期 / 用户被禁用 → 完成握手后以 close code 4401 关闭；
 *     · 会话 must_change_password 为真 → 以 close code 4403 关闭
 *       （WebSocket 是独立握手路径，不经过 HTTP 中间件，必须单独再判一次）。
 *
 * ★ 增量推送
 *   客户端可传 ?since=<lastLogId>，或在首条 JSON 消息里带 {"since_id": N}；
 *   服务端只推 id > since 的日志（hello 帧回显生效的 since_id）。
 *
 * ★ 多客户端并发
 *   单线程 stream_select：任何单个客户端断开都只清理它自己，不影响其它客户端。
 */

declare(strict_types=1);

require __DIR__ . '/../src/bootstrap.php';

const WS_COOKIE_NAME = 'gcp_sid';
const WS_GUID        = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const WS_PUSH_EVERY  = 1.0;    // 日志/任务推送间隔（秒），与 Python ws_logs 一致
const WS_HELLO_GRACE = 0.5;    // 握手后等首条 init 消息的最长时间

// ------------------------------------------------------------------
// 启动参数
// ------------------------------------------------------------------
$host = Config::env('GCPWEB_HOST', '127.0.0.1');
$port = Config::envInt('GCPWEB_PORT', 9001);

Config::ensureDataDirs();

// 忽略 SIGPIPE（客户端断开时写 socket 不应杀死整个服务）
if (function_exists('pcntl_signal')) {
    pcntl_signal(SIGPIPE, SIG_IGN);
}

$server = @stream_socket_server("tcp://{$host}:{$port}", $errno, $errstr);
if ($server === false) {
    fwrite(STDERR, "无法监听 {$host}:{$port}：{$errstr} ({$errno})\n");
    exit(1);
}
stream_set_blocking($server, false);
fwrite(STDOUT, sprintf("[%s] ws-server 监听 ws://%s:%d/ws/logs\n", date('H:i:s'), $host, $port));

/**
 * 客户端表：id => [
 *   sock, hs(bool 已握手), buf(string 收包缓冲), since(int),
 *   helloAt(float), helloSent(bool), user(string),
 *   fragOp(int), fragBuf(string), alive(bool)
 * ]
 * @var array<int,array>
 */
$clients = [];
$nextId = 1;
$lastPush = 0.0;

while (true) {
    // ---- 1) 构造 select 集合 ----
    $read = [$server];
    foreach ($clients as $cid => $c) {
        if (!is_resource($c['sock'])) {
            unset($clients[$cid]);
            continue;
        }
        $read[] = $c['sock'];
    }
    $write = null;
    $except = null;
    $n = @stream_select($read, $write, $except, 0, 200000);   // 最多等 0.2s
    if ($n === false) {
        // 被信号打断等情况：继续下一轮
        usleep(50000);
        continue;
    }

    // ---- 2) 处理可读事件 ----
    foreach ($read as $sock) {
        if ($sock === $server) {
            $conn = @stream_socket_accept($server, 0);
            if ($conn !== false) {
                stream_set_blocking($conn, false);
                $clients[$nextId] = [
                    'sock' => $conn, 'hs' => false, 'buf' => '', 'since' => 0,
                    'helloAt' => microtime(true) + WS_HELLO_GRACE, 'helloSent' => false,
                    'user' => '', 'fragOp' => 0, 'fragBuf' => '', 'alive' => true,
                ];
                $nextId++;
            }
            continue;
        }

        // 找到对应客户端
        $cid = null;
        foreach ($clients as $k => $c) {
            if ($c['sock'] === $sock) {
                $cid = $k;
                break;
            }
        }
        if ($cid === null) {
            continue;
        }

        $data = @fread($sock, 65536);
        if ($data === false || $data === '') {
            // feof 判定：真正断开才移除（避免误删）
            if (feof($sock)) {
                dropClient($clients, $cid);
            }
            continue;
        }
        $clients[$cid]['buf'] .= $data;

        try {
            // 未握手 → 尝试解析 HTTP 握手
            if (!$clients[$cid]['hs']) {
                if (!tryHandshake($clients, $cid)) {
                    continue;
                }
            }
            // 已握手 → 解析 WebSocket 帧
            if ($clients[$cid]['hs']) {
                handleFrames($clients, $cid);
            }
        } catch (Throwable $e) {
            fwrite(STDOUT, sprintf("[%s] 客户端 %d 处理异常：%s\n", date('H:i:s'), $cid, $e->getMessage()));
            dropClient($clients, $cid);
        }
    }

    // ---- 3) hello 兜底发送（客户端迟迟不来 init 消息时） ----
    $now = microtime(true);
    foreach ($clients as $cid => $c) {
        if ($c['hs'] && !$c['helloSent'] && $now >= $c['helloAt']) {
            sendHello($clients, $cid);
        }
    }

    // ---- 4) 周期性推送日志 + 任务 ----
    if ($now - $lastPush >= WS_PUSH_EVERY) {
        $lastPush = $now;
        pushToAll($clients);
    }
}

// ==================================================================
// 握手
// ==================================================================
/** @return bool 是否完成握手（false = 数据不足，等待更多） */
function tryHandshake(array &$clients, int $cid): bool
{
    $buf = $clients[$cid]['buf'];
    $pos = strpos($buf, "\r\n\r\n");
    if ($pos === false) {
        // 防御：握手头过大直接断开（1.5 万个头字段的 DoS 尝试）
        if (strlen($buf) > 16384) {
            closeWith($clients[$cid]['sock'], 4400, '请求头过大');
            dropClient($clients, $cid);
        }
        return false;
    }

    $headerBlock = substr($buf, 0, $pos);
    $clients[$cid]['buf'] = substr($buf, $pos + 4);
    $lines = explode("\r\n", $headerBlock);
    $requestLine = array_shift($lines) ?? '';
    $headers = [];
    foreach ($lines as $line) {
        $c = strpos($line, ':');
        if ($c !== false) {
            $headers[strtolower(trim(substr($line, 0, $c)))] = trim(substr($line, $c + 1));
        }
    }

    // 只接受对 /ws/logs 的 GET
    if (!preg_match('#^GET\s+(\S+)#', $requestLine, $m)) {
        writeRaw($clients[$cid]['sock'], "HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
        dropClient($clients, $cid);
        return false;
    }
    $target = $m[1];
    $path = parse_url($target, PHP_URL_PATH) ?: '/';
    if ($path !== '/ws/logs') {
        writeRaw($clients[$cid]['sock'], "HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n");
        dropClient($clients, $cid);
        return false;
    }

    $key = $headers['sec-websocket-key'] ?? '';
    $upgrade = strtolower($headers['upgrade'] ?? '');
    if ($key === '' || $upgrade !== 'websocket') {
        writeRaw($clients[$cid]['sock'], "HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
        dropClient($clients, $cid);
        return false;
    }

    // 回 101
    $accept = base64_encode(sha1($key . WS_GUID, true));
    $resp = "HTTP/1.1 101 Switching Protocols\r\n"
        . "Upgrade: websocket\r\n"
        . "Connection: Upgrade\r\n"
        . "Sec-WebSocket-Accept: {$accept}\r\n\r\n";
    if (!writeRaw($clients[$cid]['sock'], $resp)) {
        dropClient($clients, $cid);
        return false;
    }
    $clients[$cid]['hs'] = true;

    // 解析 ?since=
    $q = [];
    $query = parse_url($target, PHP_URL_QUERY);
    if (is_string($query)) {
        parse_str($query, $q);
    }
    if (isset($q['since']) && preg_match('/^\d+$/', (string) $q['since'])) {
        $clients[$cid]['since'] = (int) $q['since'];
    }

    // ---- 鉴权 ----
    $token = cookieValue($headers['cookie'] ?? '', WS_COOKIE_NAME);
    $sess = validateSession($token);
    if ($sess === null) {
        // 未认证：完成握手后以 4401 关闭（前端会退化为轮询）
        closeWith($clients[$cid]['sock'], 4401, '未认证');
        dropClient($clients, $cid);
        return false;
    }
    if (!empty($sess['must_change'])) {
        // 强制改密：以 4403 关闭（与 Python ws_logs 一致）
        closeWith($clients[$cid]['sock'], 4403, 'must_change_password');
        dropClient($clients, $cid);
        return false;
    }
    $clients[$cid]['user'] = (string) ($sess['username'] ?? '');
    return true;
}

/** 从 Cookie 头里取指定 cookie 的值 */
function cookieValue(string $cookieHeader, string $name): string
{
    if ($cookieHeader === '') {
        return '';
    }
    foreach (explode(';', $cookieHeader) as $pair) {
        $pair = trim($pair);
        $eq = strpos($pair, '=');
        if ($eq === false) {
            continue;
        }
        if (substr($pair, 0, $eq) === $name) {
            return rawurldecode(substr($pair, $eq + 1));
        }
    }
    return '';
}

/**
 * 校验会话 token（与 core/users.py get_session 同语义）。
 * sessions 表尚未建好 / 查询异常时按「未认证」处理，绝不放过。
 */
function validateSession(string $token): ?array
{
    if ($token === '') {
        return null;
    }
    try {
        $row = Db::one(
            'SELECT s.*, u.disabled AS user_disabled, u.must_change_password AS must_change '
            . 'FROM sessions s LEFT JOIN users u ON u.id=s.user_id WHERE s.token=?',
            [$token]
        );
    } catch (Throwable $e) {
        return null;
    }
    if ($row === null) {
        return null;
    }
    if ((float) ($row['expires_at'] ?? 0) < microtime(true)) {
        return null;
    }
    if (!empty($row['user_disabled'])) {
        return null;
    }
    return $row;
}

// ==================================================================
// 帧处理
// ==================================================================
function handleFrames(array &$clients, int $cid): void
{
    while (true) {
        $frames = wsDecodeFrames($clients[$cid]['buf']);
        if ($frames === null || $frames === []) {
            return;   // 数据不足（没有完整帧可消费），等下一轮
        }
        foreach ($frames as $f) {
            [$fin, $op, $payload] = $f;
            if ($op === 0x8) {          // close
                closeWith($clients[$cid]['sock'], 1000, '');
                dropClient($clients, $cid);
                return;
            }
            if ($op === 0x9) {          // ping → pong
                @fwrite($clients[$cid]['sock'], wsEncode($payload, 0xA));
                continue;
            }
            if ($op === 0xA) {          // pong：忽略
                continue;
            }
            if ($op === 0x1 || $op === 0x2 || $op === 0x0) {
                // 分片重组
                if ($op !== 0x0) {
                    $clients[$cid]['fragOp'] = $op;
                    $clients[$cid]['fragBuf'] = $payload;
                } else {
                    $clients[$cid]['fragBuf'] .= $payload;
                }
                if ($fin) {
                    $msg = $clients[$cid]['fragBuf'];
                    $clients[$cid]['fragBuf'] = '';
                    onMessage($clients, $cid, $msg);
                }
            }
        }
    }
}

/** 处理客户端消息：兼容前端在 onopen 里发的 {"since_id": N} */
function onMessage(array &$clients, int $cid, string $msg): void
{
    $msg = trim($msg);
    if ($msg === '') {
        return;
    }
    $d = json_decode($msg, true);
    if (is_array($d) && isset($d['since_id']) && is_numeric($d['since_id'])) {
        $clients[$cid]['since'] = max(0, (int) $d['since_id']);
        sendHello($clients, $cid);
    }
}

/** 发送 hello（回显生效的 since_id / 用户名） */
function sendHello(array &$clients, int $cid): void
{
    if (!isset($clients[$cid]) || !$clients[$cid]['hs']) {
        return;
    }
    $clients[$cid]['helloSent'] = true;
    $payload = json_encode([
        'type' => 'hello',
        'since_id' => $clients[$cid]['since'],
        'user' => $clients[$cid]['user'],
    ], JSON_UNESCAPED_UNICODE);
    @fwrite($clients[$cid]['sock'], wsEncode((string) $payload, 0x1));
}

/** 向所有已握手客户端推送增量日志与任务快照 */
function pushToAll(array &$clients): void
{
    if ($clients === []) {
        return;
    }
    foreach ($clients as $cid => $c) {
        if (!$c['hs'] || !$c['helloSent']) {
            continue;
        }
        try {
            $logs = Tasks::listLogs((int) $c['since'], 200, null);
            if ($logs !== []) {
                $clients[$cid]['since'] = (int) $logs[count($logs) - 1]['id'];
                $payload = json_encode(['type' => 'logs', 'items' => $logs], JSON_UNESCAPED_UNICODE);
                if (!@fwrite($c['sock'], wsEncode((string) $payload, 0x1))) {
                    dropClient($clients, $cid);
                    continue;
                }
            }
            $tasks = Tasks::list(20);
            $items = array_map(static fn(array $t): array => [
                'id' => $t['id'], 'kind' => $t['kind'], 'status' => $t['status'], 'message' => $t['message'],
            ], $tasks);
            $tp = json_encode(['type' => 'tasks', 'items' => $items], JSON_UNESCAPED_UNICODE);
            if (!@fwrite($c['sock'], wsEncode((string) $tp, 0x1))) {
                dropClient($clients, $cid);
            }
        } catch (Throwable $e) {
            dropClient($clients, $cid);
        }
    }
}

// ==================================================================
// 帧编解码（RFC6455）
// ==================================================================
/**
 * 从缓冲区解析出完整的帧，并**就地消费**已解析的字节。
 * @return array<int,array{0:int,1:int,2:string}>|null null 表示数据不足以构成一帧
 */
function wsDecodeFrames(string &$buf): ?array
{
    $frames = [];
    $offset = 0;
    $len = strlen($buf);

    while ($offset < $len) {
        if ($len - $offset < 2) {
            break;
        }
        $b0 = ord($buf[$offset]);
        $b1 = ord($buf[$offset + 1]);
        $fin = ($b0 & 0x80) !== 0;
        $op = $b0 & 0x0F;
        $masked = ($b1 & 0x80) !== 0;
        $plen = $b1 & 0x7F;
        $p = $offset + 2;

        if ($plen === 126) {
            if ($len - $p < 2) {
                break;
            }
            $plen = unpack('n', substr($buf, $p, 2))[1];
            $p += 2;
        } elseif ($plen === 127) {
            if ($len - $p < 8) {
                break;
            }
            $plen = unpack('J', substr($buf, $p, 8))[1];
            $p += 8;
        }
        // 防御：超大帧直接拒绝（正常消息都很小）
        if ($plen > 1048576) {
            $buf = '';
            return [];
        }
        $mask = '';
        if ($masked) {
            if ($len - $p < 4) {
                break;
            }
            $mask = substr($buf, $p, 4);
            $p += 4;
        }
        if ($len - $p < $plen) {
            break;   // 载荷不完整，等更多数据
        }
        $payload = substr($buf, $p, $plen);
        if ($masked) {
            $unmasked = '';
            for ($i = 0; $i < $plen; $i++) {
                $unmasked .= $payload[$i] ^ $mask[$i % 4];
            }
            $payload = $unmasked;
        }
        $offset = $p + $plen;
        $frames[] = [$fin, $op, $payload];
    }

    $buf = substr($buf, $offset);
    return $frames;
}

/** 编码一个服务端帧（服务端发出的帧**不加掩码**） */
function wsEncode(string $payload, int $opcode = 0x1): string
{
    $len = strlen($payload);
    $head = chr(0x80 | $opcode);
    if ($len < 126) {
        $head .= chr($len);
    } elseif ($len < 65536) {
        $head .= chr(126) . pack('n', $len);
    } else {
        $head .= chr(127) . pack('J', $len);
    }
    return $head . $payload;
}

/** 发送 close 帧（不带掩码，服务端） */
function closeWith($sock, int $code, string $reason = ''): void
{
    if (!is_resource($sock)) {
        return;
    }
    $payload = pack('n', $code) . $reason;
    @fwrite($sock, wsEncode($payload, 0x8));
}

/** 移除并关闭一个客户端（只影响它自己） */
function dropClient(array &$clients, int $cid): void
{
    if (isset($clients[$cid])) {
        if (is_resource($clients[$cid]['sock'])) {
            @fclose($clients[$cid]['sock']);
        }
        unset($clients[$cid]);
    }
}

/** 尽力写原始字节，返回是否成功 */
function writeRaw($sock, string $data): bool
{
    if (!is_resource($sock)) {
        return false;
    }
    $n = @fwrite($sock, $data);
    return $n !== false && $n === strlen($data);
}
