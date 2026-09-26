<?php
/**
 * Http —— 请求读取、客户端 IP 判定、安全响应头
 *
 * ★ 客户端 IP 的判定是安全关键（登录限速按 IP 计数）：
 *   只有当**直连来源**落在可信代理网段内时才采信 X-Forwarded-For /
 *   X-Real-IP。否则任何人都能伪造 XFF 绕过限速。
 *   （Python 版第一轮审计实测：不校验时连续 60 次登录零限速。）
 *
 * 安全要点：
 *   · 不用 $_REQUEST（受 variables_order/php.ini 影响，且混淆来源）。
 *   · JSON body 有大小上限，防止超大 body 打满内存。
 *   · CIDR 比对分别处理 IPv4/IPv6，且用 inet_pton 二进制比较（不靠字符串前缀，
 *     避免 10.0.0.1 被 10.0.0.0/8 之外的 "10.0.0" 前缀误判）。
 */

declare(strict_types=1);

final class Http
{
    /** JSON body 上限 1 MiB（服务账号 JSON 最大 256KB，留足余量） */
    private const MAX_BODY = 1048576;

    // ------------------------------------------------------------------
    // 请求
    // ------------------------------------------------------------------
    public static function method(): string
    {
        return strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET');
    }

    /** 请求路径（不含 query），已做一次 URL 解码与多斜杠归并 */
    public static function path(): string
    {
        $u = $_SERVER['REQUEST_URI'] ?? '/';
        $p = parse_url($u, PHP_URL_PATH);
        if (!is_string($p) || $p === '') {
            $p = '/';
        }
        $decoded = rawurldecode($p);
        // 拒绝解码后含 NUL 的路径（PHP 层面已很难触发，但显式挡掉）
        if (strpos($decoded, "\0") !== false) {
            return '/';
        }
        // 多斜杠归并 + 去掉结尾多余斜杠（/ 与 /api/ 都保留根）
        $norm = preg_replace('#/+#', '/', $decoded);
        if ($norm !== '/' && substr($norm, -1) === '/') {
            $norm = rtrim($norm, '/');
        }
        return $norm === '' ? '/' : $norm;
    }

    public static function query(string $key, $default = null)
    {
        $v = $_GET[$key] ?? null;
        return is_string($v) ? $v : $default;
    }

    public static function queryInt(string $key, ?int $default = null): ?int
    {
        $v = self::query($key);
        if ($v === null || !preg_match('/^-?\d+$/', trim($v))) {
            return $default;
        }
        return (int) trim($v);
    }

    /** 解析 JSON body；空体/非法体返回 []（由调用方决定是否报错） */
    public static function jsonBody(): array
    {
        static $cache = null;
        if (is_array($cache)) {
            return $cache;
        }
        $len = (int) ($_SERVER['CONTENT_LENGTH'] ?? 0);
        if ($len > self::MAX_BODY) {
            Json::err('请求体过大', 413);
        }
        $raw = file_get_contents('php://input', false, null, 0, self::MAX_BODY + 1);
        if (!is_string($raw) || $raw === '') {
            return $cache = [];
        }
        if (strlen($raw) > self::MAX_BODY) {
            Json::err('请求体过大', 413);
        }
        // 明确禁止 unserialize：只认 JSON
        try {
            $d = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
        } catch (JsonException $e) {
            Json::err('请求体不是合法 JSON', 400);
        }
        return $cache = (is_array($d) ? $d : []);
    }

    /** 表单（multipart / urlencoded）：$_POST + $_FILES */
    public static function form(): array
    {
        return ['post' => $_POST, 'files' => $_FILES];
    }

    public static function header(string $name): ?string
    {
        $k = 'HTTP_' . strtoupper(str_replace('-', '_', $name));
        $v = $_SERVER[$k] ?? null;
        return is_string($v) ? $v : null;
    }

    public static function userAgent(): string
    {
        return mb_substr((string) ($_SERVER['HTTP_USER_AGENT'] ?? ''), 0, 255);
    }

    // ------------------------------------------------------------------
    // 客户端 IP
    // ------------------------------------------------------------------
    public static function clientIp(): string
    {
        $remote = (string) ($_SERVER['REMOTE_ADDR'] ?? '');
        $trusted = Config::trustedProxies();
        if ($trusted === [] || !self::ipInAny($remote, $trusted)) {
            // 直连来源不可信 → 忽略所有代理头
            return $remote;
        }
        // 从右往左找第一个不在可信网段里的地址：那才是真实客户端
        $xff = self::header('X-Forwarded-For');
        if ($xff !== null && $xff !== '') {
            $parts = array_map('trim', explode(',', $xff));
            for ($i = count($parts) - 1; $i >= 0; $i--) {
                $ip = $parts[$i];
                if ($ip === '' || !filter_var($ip, FILTER_VALIDATE_IP)) {
                    continue;
                }
                if (!self::ipInAny($ip, $trusted)) {
                    return $ip;
                }
            }
            // 全都在可信网段内 → 取最左（最靠近客户端）的那个
            if (!empty($parts) && filter_var($parts[0], FILTER_VALIDATE_IP)) {
                return $parts[0];
            }
        }
        $real = self::header('X-Real-IP');
        if ($real !== null && filter_var($real, FILTER_VALIDATE_IP)) {
            return $real;
        }
        return $remote;
    }

    /** IP 是否落在任一 CIDR 内（inet_pton 二进制前缀比较） */
    public static function ipInAny(string $ip, array $cidrs): bool
    {
        if (!filter_var($ip, FILTER_VALIDATE_IP)) {
            return false;
        }
        $bin = inet_pton($ip);
        if ($bin === false) {
            return false;
        }
        foreach ($cidrs as $cidr) {
            $slash = strrpos($cidr, '/');
            if ($slash === false) {
                continue;
            }
            $net = substr($cidr, 0, $slash);
            $bits = (int) substr($cidr, $slash + 1);
            $netBin = inet_pton($net);
            if ($netBin === false || strlen($netBin) !== strlen($bin)) {
                continue; // 协议族不同（v4 比 v6）直接跳过
            }
            $maxBits = strlen($bin) * 8;
            if ($bits < 0 || $bits > $maxBits) {
                continue;
            }
            $fullBytes = intdiv($bits, 8);
            $remBits   = $bits % 8;
            if ($fullBytes > 0 && substr($bin, 0, $fullBytes) !== substr($netBin, 0, $fullBytes)) {
                continue;
            }
            if ($remBits > 0) {
                $mask = 0xFF << (8 - $remBits) & 0xFF;
                if ((ord($bin[$fullBytes]) & $mask) !== (ord($netBin[$fullBytes]) & $mask)) {
                    continue;
                }
            }
            return true;
        }
        return false;
    }

    // ------------------------------------------------------------------
    // Cookie / TLS
    // ------------------------------------------------------------------
    public static function isHttps(): bool
    {
        if (!empty($_SERVER['HTTPS']) && strtolower((string) $_SERVER['HTTPS']) !== 'off') {
            return true;
        }
        if ((string) ($_SERVER['SERVER_PORT'] ?? '') === '443') {
            return true;
        }
        // 仅在可信代理场景下采信 X-Forwarded-Proto
        $remote = (string) ($_SERVER['REMOTE_ADDR'] ?? '');
        if (self::ipInAny($remote, Config::trustedProxies())) {
            $p = self::header('X-Forwarded-Proto');
            if ($p !== null && strtolower(trim(explode(',', $p)[0])) === 'https') {
                return true;
            }
        }
        return false;
    }

    public static function cookieSecure(): bool
    {
        $forced = Config::secureCookie();
        if ($forced !== null) {
            return $forced;
        }
        return self::isHttps();
    }

    /** 统一安全响应头（每个响应都要有，含错误响应） */
    public static function securityHeaders(): void
    {
        if (headers_sent()) {
            return;
        }
        header('X-Content-Type-Options: nosniff');
        header('X-Frame-Options: DENY');
        header('Referrer-Policy: strict-origin-when-cross-origin');
        header('Permissions-Policy: geolocation=(), microphone=(), camera=()');
        // ★ CSP 必须与 Python 版 app.py 逐字符一致（实测踩过坑）：
        //   · 前端用的是 Vue **全局构建**（vue.global.prod.js），它在运行期用
        //     `new Function()` 编译 DOM 里的模板，因此 script-src 必须有
        //     'unsafe-eval'。少了它浏览器直接抛
        //     「Evaluating a string as JavaScript violates CSP」，
        //     Vue 挂不上 → 页面白屏（v-cloak 把内容全藏着），而且**没有其它报错**，
        //     极难排查。这是浏览器实测才发现的，静态比对响应头发现不了。
        //   · font-src 要带 data: —— FontAwesome 的 woff2 以 data URI 内联。
        //   · base-uri 用 'self'（Python 版即如此），保持一致。
        //   'unsafe-eval' 确实削弱了 XSS 防护，但这是 Vue 运行期编译的硬性要求；
        //   前端所有模板都是同源静态文件、不接受用户输入当模板，风险可控。
        header("Content-Security-Policy: default-src 'self'; "
            . "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            . "style-src 'self' 'unsafe-inline'; "
            . "img-src 'self' data:; "
            . "font-src 'self' data:; "
            . "connect-src 'self' ws: wss:; "
            . "object-src 'none'; "
            . "base-uri 'self'; "
            . "form-action 'self'; "
            . "frame-ancestors 'none'");
        // 允许 HTTPS 场景下的 HSTS 只在真 HTTPS 时下发
        if (self::isHttps()) {
            header('Strict-Transport-Security: max-age=31536000; includeSubDomains');
        }
    }

    /** 发送 Session Cookie（HttpOnly + SameSite=Lax + 条件 Secure） */
    public static function setSessionCookie(string $name, string $value, int $ttl): void
    {
        $opts = [
            'expires'  => time() + $ttl,
            'path'     => '/',
            'httponly' => true,
            'samesite' => 'Lax',
        ];
        if (self::cookieSecure()) {
            $opts['secure'] = true;
        }
        // PHP 7.3+ 支持数组形式，是最可靠的方式（自动处理 SameSite）
        setcookie($name, $value, $opts);
    }

    public static function clearCookie(string $name): void
    {
        $opts = ['expires' => time() - 3600, 'path' => '/', 'httponly' => true, 'samesite' => 'Lax'];
        if (self::cookieSecure()) {
            $opts['secure'] = true;
        }
        setcookie($name, '', $opts);
    }

    /** 由 Request 推导出本站 base URL（用于 ws-server 提示，不含 Host 注入风险） */
    public static function host(): string
    {
        $h = (string) ($_SERVER['HTTP_HOST'] ?? 'localhost');
        // 只允许合法主机名/端口字面量，挡掉 Host 头注入（换行、@、路径）
        if (!preg_match('/^[A-Za-z0-9._\-]+(:\d{1,5})?$/', $h)) {
            return 'localhost';
        }
        return $h;
    }
}
