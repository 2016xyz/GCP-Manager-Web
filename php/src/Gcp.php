<?php
/**
 * Gcp —— GCP 客户端（服务账号 JWT → OAuth2 → Compute REST），零 composer 依赖
 *
 * 逐条对照 core/gcp.py 移植。本文件是「PHP 版能真正操作 GCP」的核心：
 *
 *   1. 鉴权：读服务账号 JSON → 用 openssl_sign 手工构造 RS256 JWT →
 *      POST https://oauth2.googleapis.com/token（jwt-bearer 流程）换 access_token。
 *      token 缓存到 data/tokens/（0600），过期前 60 秒自动刷新。
 *   2. HTTP：全部走 curl；强制 SSL 校验、设连接/总超时、禁止跨协议跟随跳转。
 *   3. 代理：parse_proxy_input 归一化各种写法（含 socks5h），
 *      mask_proxy 打码（覆盖 @ 形态与 host:port:user:pass 形态），
 *      test_proxy 用**硬编码** PROXY_TEST_URL 真实探测（SSRF 防护：绝不接受请求方传入 URL）。
 *   4. Compute：实例列表/创建/删除/启停/重置、防火墙、区域、可用区、网络、子网、
 *      项目元数据（SSH 公钥注入）。删除类操作必须检查 operation 结果。
 *
 * ★ 安全红线：
 *   · 所有外连 URL 来自硬编码常量（googleapis.com）或已校验的配置，绝不接受请求参
 *     数当 URL。
 *   · 实例列表白名单式丢弃密码字段，只回 has_password（对应 Python 第二轮修过的
 *     sync=false 泄漏 root 密码明文的 bug）。
 *   · 防火墙绑实例所在 VPC（Python 硬编码 global/networks/default 曾导致 404）。
 *   · zone/region/name 等进入 URL 路径的片段一律做字符白名单校验（防路径注入）。
 */

declare(strict_types=1);

final class Gcp
{
    // ------------------------------------------------------------------
    // 常量（硬编码，绝不接受请求方覆盖）
    // ------------------------------------------------------------------
    public const DEFAULT_NETWORK = 'global/networks/default';
    /** 子网 URL 模板：必须与实例所在 region 匹配 */
    public const DEFAULT_SUBNET_FMT = 'regions/%s/subnetworks/default';

    public const OAUTH_TOKEN_URL  = 'https://oauth2.googleapis.com/token';
    public const OAUTH_SCOPE      = 'https://www.googleapis.com/auth/cloud-platform';
    public const COMPUTE_API      = 'https://compute.googleapis.com/compute/v1';
    public const CRM_API          = 'https://cloudresourcemanager.googleapis.com/v1';
    public const SERVICEUSAGE_API = 'https://serviceusage.googleapis.com/v1';
    public const BILLING_API      = 'https://cloudbilling.googleapis.com/v1';
    public const IAM_API          = 'https://iam.googleapis.com/v1';

    /** 代理连通性探测目标：**硬编码**，绝不接受请求方传入（否则就是 SSRF） */
    public const PROXY_TEST_URL = 'https://www.googleapis.com/discovery/v1/apis';

    /** 支持的代理协议 → 归一化后的类型标签 */
    public const PROXY_SCHEMES = [
        'http' => 'HTTP', 'https' => 'HTTPS',
        'socks4' => 'SOCKS4', 'socks4a' => 'SOCKS4',
        'socks5' => 'SOCKS5', 'socks5h' => 'SOCKS5H', 'socks' => 'SOCKS5H',
    ];

    public const PROXY_TYPE_LABELS = [
        'HTTP' => 'HTTP 代理',
        'HTTPS' => 'HTTPS 代理（HTTP CONNECT）',
        'SOCKS4' => 'SOCKS4（仅 IPv4，无认证）',
        'SOCKS5' => 'SOCKS5（本地解析 DNS）',
        'SOCKS5H' => 'SOCKS5（代理端解析 DNS，推荐）',
    ];

    /** 服务账号 JSON 的必要特征（与 Python __init__.py 的 _SA_REQUIRED 一致） */
    public const SA_REQUIRED = ['type', 'private_key', 'client_email', 'project_id'];

    private string $keyPath;
    private string $projectId;
    private string $email;
    private string $proxyUrl = '';
    private string $proxyType = 'HTTPS';

    /** @var array<string,mixed>|null 惰性加载的服务账号 JSON */
    private ?array $saJson = null;

    // ==================================================================
    // 构造
    // ==================================================================
    public function __construct(string $keyPath, string $projectId, string $email, string $proxy = '', string $proxyType = 'HTTPS')
    {
        $this->keyPath = $keyPath;
        $this->projectId = $projectId;
        $this->email = $email;
        $parsed = self::parse_proxy_input($proxy, $proxyType);
        $this->proxyUrl = !empty($parsed['ok']) ? ($parsed['proxy_url'] ?? '') : '';
        $this->proxyType = (string) ($parsed['proxy_type'] ?? strtoupper($proxyType ?: 'HTTPS'));
    }

    public function projectId(): string { return $this->projectId; }
    public function email(): string { return $this->email; }
    public function keyPath(): string { return $this->keyPath; }

    // ==================================================================
    // 代理：解析 / 打码 / 探测（静态，模块级能力）
    // ==================================================================

    /**
     * 把各种代理写法统一成可用的 URL。返回 dict：
     * ok / empty / proxy_url / proxy_type / proxy_type_label / host / port / has_auth / (error)
     *
     * 支持的输入形式：
     *   1.2.3.4:8080
     *   1.2.3.4:8080:user:pass
     *   http://1.2.3.4:8080
     *   socks5://user:pass@1.2.3.4:1080
     *   socks5h://proxy.example.com:1080
     *   socks5   1.2.3.4 1080 user pass
     */
    public static function parse_proxy_input(?string $proxyText, string $fallbackProxyType = 'HTTPS'): array
    {
        $raw = trim((string) $proxyText);
        $fb = strtoupper($fallbackProxyType ?: 'HTTPS');
        if ($raw === '') {
            return [
                'ok' => true, 'empty' => true, 'proxy_url' => '', 'proxy_type' => $fb,
                'proxy_type_label' => self::PROXY_TYPE_LABELS[$fb] ?? $fb,
            ];
        }

        $ptype = strtoupper($fallbackProxyType ?: 'HTTPS');
        if ($ptype === 'SOCKS5') {
            $ptype = 'SOCKS5H';          // 老库里存的 SOCKS5 一律按推荐的 socks5h 处理
        }
        $user = null;
        $pw = null;
        $host = '';
        $port = '';

        $schemePos = strpos($raw, '://');
        if ($schemePos !== false) {
            $scheme = strtolower(trim(substr($raw, 0, $schemePos)));
            $rest = substr($raw, $schemePos + 3);
            // 协议名不认识时**必须报错**，不能静默回退默认值（否则到调 GCP 才失败）
            if (!isset(self::PROXY_SCHEMES[$scheme])) {
                return [
                    'ok' => false, 'proxy_url' => '', 'proxy_type' => $ptype,
                    'host' => '', 'port' => '',
                    'error' => "不认识的代理协议「{$scheme}」；支持 "
                        . implode(', ', self::uniqueSorted(array_keys(self::PROXY_SCHEMES))),
                ];
            }
            $ptype = self::PROXY_SCHEMES[$scheme];
            $at = strrpos($rest, '@');
            if ($at !== false) {
                $cred = substr($rest, 0, $at);
                $hostport = substr($rest, $at + 1);
                $cpos = strpos($cred, ':');
                if ($cpos !== false) {
                    $user = substr($cred, 0, $cpos);
                    $pw = substr($cred, $cpos + 1);
                } else {
                    $user = $cred;
                }
            } else {
                $hostport = $rest;
            }
            if (strncmp($hostport, '[', 1) === 0) {
                $bpos = strrpos($hostport, ']:');
                if ($bpos !== false) {
                    $host = ltrim(substr($hostport, 0, $bpos), '[');
                    $port = substr($hostport, $bpos + 2);
                } else {
                    $host = ltrim($hostport, '[');
                }
            } else {
                $cpos = strrpos($hostport, ':');
                if ($cpos !== false) {
                    $host = substr($hostport, 0, $cpos);
                    $port = substr($hostport, $cpos + 1);
                } else {
                    $host = $hostport;
                }
            }
            $parts = [];
        } else {
            $parts = preg_split('/\s+/', $raw) ?: [];
            $parts = array_values(array_filter($parts, static fn($x) => $x !== ''));
            if (count($parts) === 1) {
                $head = strtolower(trim($parts[0]));
                if (isset(self::PROXY_SCHEMES[$head])) {
                    $parts = explode(':', $parts[0]);
                    $ptype = self::PROXY_SCHEMES[$head];
                    $parts = array_slice($parts, 1);
                } elseif (strncmp($raw, '[', 1) === 0) {
                    // 括号包起来的 IPv6：[::1]:8080[:user:pass]
                    $bpos = strpos($raw, ']:');
                    if ($bpos !== false) {
                        $host = ltrim(substr($raw, 0, $bpos), '[');
                        $tail = explode(':', substr($raw, $bpos + 2));
                        $port = $tail[0] ?? '';
                        if (count($tail) >= 3) {
                            $user = $tail[1];
                            $pw = $tail[2];
                        }
                    } else {
                        $host = ltrim($raw, '[');
                    }
                    $parts = [];
                } else {
                    $parts = explode(':', $parts[0]);
                }
            } else {
                $head = strtolower(trim($parts[0]));
                if (isset(self::PROXY_SCHEMES[$head])) {
                    $ptype = self::PROXY_SCHEMES[$head];
                    $parts = array_slice($parts, 1);
                }
            }
        }

        if ($parts) {
            if (count($parts) >= 1 && $host === '') {
                $host = trim((string) ($parts[0] ?? ''));
            }
            if (count($parts) >= 2 && $port === '') {
                $port = trim((string) ($parts[1] ?? ''));
            }
            if (count($parts) >= 4 && $user === null) {
                $user = trim((string) $parts[2]);
                $pw = trim((string) $parts[3]);
            }
        }
        $host = trim($host);
        $port = trim($port);

        // 主机名与 IPv4/IPv6 都接受（老实现只认 IPv4 字面量，把 socks5h://host 拒了）
        if ($host === '') {
            return ['ok' => false, 'error' => '代理地址为空', 'proxy_url' => ''];
        }
        $isIpv4 = (bool) preg_match('/^\d{1,3}(\.\d{1,3}){3}$/', $host);
        $isHost = (bool) preg_match('/^[A-Za-z0-9][A-Za-z0-9._-]*\.[A-Za-z]{2,}$/', $host);
        $isIpv6 = strpos($host, ':') !== false;
        if (!($isIpv4 || $isHost || $isIpv6)) {
            return ['ok' => false, 'proxy_url' => '',
                    'error' => "代理地址格式不正确：{$host}（应为 IP 或域名）"];
        }
        if ($isIpv4) {
            foreach (explode('.', $host) as $seg) {
                if ((int) $seg > 255) {
                    return ['ok' => false, 'proxy_url' => '', 'error' => "IPv4 段超出 0-255：{$host}"];
                }
            }
        }
        if (!ctype_digit($port) || !((int) $port > 0 && (int) $port < 65536)) {
            return ['ok' => false, 'proxy_url' => '', 'error' => '代理端口不正确：' . ($port !== '' ? $port : '(空)')];
        }

        if ($ptype === 'SOCKS4') {
            if ($user !== null && $user !== '') {
                return ['ok' => false, 'proxy_url' => '',
                        'error' => 'SOCKS4 不支持用户名/密码认证，请改用 socks5'];
            }
            $scheme = 'socks4';
        } elseif ($ptype === 'SOCKS5' || $ptype === 'SOCKS5H') {
            // 统一用 socks5h：让代理解析域名，避免本地 DNS 污染
            $scheme = 'socks5h';
            $ptype = 'SOCKS5H';
        } else {
            $scheme = 'http';
        }

        if ($user !== null && $user !== '') {
            $cred = rawurlencode($user) . ':' . rawurlencode((string) ($pw ?? '')) . '@';
        } else {
            $cred = '';
        }
        // IPv6 地址在 URL 里必须用方括号包起来
        $hostpart = (strpos($host, ':') !== false && strncmp($host, '[', 1) !== 0) ? "[{$host}]" : $host;
        $proxyUrl = "{$scheme}://{$cred}{$hostpart}:{$port}";

        return [
            'ok' => true, 'empty' => false, 'proxy_url' => $proxyUrl, 'proxy_type' => $ptype,
            'proxy_type_label' => self::PROXY_TYPE_LABELS[$ptype] ?? $ptype,
            'host' => $host, 'port' => $port, 'has_auth' => ($user !== null && $user !== ''),
        ];
    }

    /**
     * 展示用：把代理里的密码打码，避免账号页面直接看到明文密码。
     *
     * ★ 必须覆盖两种写法：
     *   scheme://user:pass@host:port  → scheme://user:***@host:port
     *   host:port:user:pass           → host:port:user:***
     * 旧实现只处理带 `@` 的形态，host:port:user:pass 会被原样回显（泄漏）。
     */
    public static function mask_proxy(?string $proxyText): string
    {
        $raw = trim((string) $proxyText);
        if ($raw === '') {
            return $raw;
        }
        $at = strrpos($raw, '@');
        if ($at !== false) {
            $head = substr($raw, 0, $at);
            $tail = substr($raw, $at + 1);
            $sp = strpos($head, '://');
            if ($sp !== false) {
                $scheme = substr($head, 0, $sp);
                $cred = substr($head, $sp + 3);
                $cp = strpos($cred, ':');
                if ($cp !== false) {
                    $u = substr($cred, 0, $cp);
                    return "{$scheme}://{$u}:***@{$tail}";
                }
            }
            return $raw;
        }
        // `host:port:user:pass` 形态：4 段、第 2 段是端口 → 打码第 4 段
        $parts = explode(':', $raw);
        if (count($parts) === 4 && ctype_digit($parts[1]) && strpos($raw, '://') === false) {
            return "{$parts[0]}:{$parts[1]}:{$parts[2]}:***";
        }
        return $raw;
    }

    /**
     * 真实探测代理是否可用：**经该代理**发一个轻量 HTTPS 请求，返回延迟与结果。
     *
     * 注意 $url 参数仅供内部/测试覆盖使用；HTTP 层（index.php）绝不允许把请求参数
     * 透传成 URL。默认值即硬编码常量 PROXY_TEST_URL。
     */
    public static function test_proxy(?string $proxyText, string $proxyType = 'HTTPS', int $timeout = 12, string $url = self::PROXY_TEST_URL): array
    {
        $parsed = self::parse_proxy_input($proxyText, $proxyType);
        $base = [
            'latency_ms' => 0, 'status' => 0, 'url' => $url, 'error' => '',
            'proxy_display' => self::mask_proxy($proxyText), 'empty' => false,
            'blocked_by_proxy' => false,
        ];
        if (empty($parsed['ok'])) {
            return array_merge($base, ['ok' => false, 'via' => '', 'error' => ($parsed['error'] ?? '') ?: '代理配置不合法']);
        }

        $isEmpty = (bool) ($parsed['empty'] ?? false);
        $purl = (string) ($parsed['proxy_url'] ?? '');
        $via = (string) ($parsed['proxy_type_label'] ?? '');
        $base['empty'] = $isEmpty;
        $base['via'] = $via;
        $display = $purl !== '' ? self::mask_proxy($purl) : '直连';

        $t0 = microtime(true);
        $res = self::curl_request(
            'GET',
            $url,
            ['User-Agent: GCP-Manager-Web/proxy-test'],
            null,
            8,
            $timeout,
            $purl,
            (string) ($parsed['proxy_type'] ?? ''),
            true // 只取响应头（stream）
        );
        $ms = (int) round((microtime(true) - $t0) * 1000);

        if ($res['error'] !== '') {
            $msg = $res['error'];
            $blocked = (!$isEmpty) && self::anyContains($msg, ['407', 'Proxy Authentication', 'proxy auth', 'tunnel', 'SSL', 'SSL certificate']);
            return array_merge($base, [
                'ok' => false, 'latency_ms' => $ms,
                'error' => mb_substr($msg, 0, 220),
                'blocked_by_proxy' => (bool) $blocked,
                'display' => $display,
            ]);
        }
        $code = (int) $res['status'];
        $ok = $code < 400;
        return array_merge($base, [
            'ok' => $ok, 'latency_ms' => $ms, 'status' => $code,
            'error' => $ok ? '' : "目标返回 HTTP {$code}",
            'display' => $display,
        ]);
    }

    // ==================================================================
    // 服务账号 JSON 校验（防「按路径读任意 JSON 并回显字段」）
    // ==================================================================

    /** 校验文件路径处是否确实是一份 GCP 服务账号密钥。返回 [bool, reason]。 */
    public static function validate_service_account_file(string $path): array
    {
        if (!is_file($path)) {
            return [false, '文件不存在'];
        }
        if (filesize($path) > 256 * 1024) {
            return [false, '文件过大（服务账号密钥通常只有几 KB）'];
        }
        $raw = @file_get_contents($path);
        if ($raw === false) {
            return [false, '读取失败'];
        }
        try {
            $data = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
        } catch (JsonException $e) {
            return [false, '不是合法的 JSON：' . $e->getMessage()];
        }
        if (!is_array($data)) {
            return [false, 'JSON 顶层不是对象'];
        }
        return self::validate_service_account_array($data);
    }

    /** 校验已解析的服务账号 JSON 数组。返回 [bool, reason]。 */
    public static function validate_service_account_array(array $data): array
    {
        $missing = [];
        foreach (self::SA_REQUIRED as $k) {
            if (empty($data[$k])) {
                $missing[] = $k;
            }
        }
        if ($missing) {
            return [false, '不是 GCP 服务账号密钥（缺少字段：' . implode('、', $missing) . '）'];
        }
        if (($data['type'] ?? null) !== 'service_account') {
            $t = $data['type'] ?? null;
            return [false, 'type 必须是 service_account，实际是 ' . var_export($t, true)];
        }
        if (strpos((string) ($data['client_email'] ?? ''), '@') === false) {
            return [false, 'client_email 不像一个邮箱'];
        }
        return [true, ''];
    }

    /** 读服务账号 JSON（懒加载 + 校验）。 */
    private function service_account_json(): array
    {
        if ($this->saJson !== null) {
            return $this->saJson;
        }
        $raw = @file_get_contents($this->keyPath);
        if ($raw === false) {
            throw new RuntimeException("服务账号 JSON 不存在：{$this->keyPath}");
        }
        try {
            $data = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
        } catch (JsonException $e) {
            throw new RuntimeException('服务账号 JSON 解析失败：' . $e->getMessage());
        }
        if (!is_array($data)) {
            throw new RuntimeException('服务账号 JSON 顶层不是对象');
        }
        $this->saJson = $data;
        return $data;
    }

    // ==================================================================
    // 鉴权：JWT(RS256) → OAuth2 access_token
    // ==================================================================

    private static function b64url(string $bin): string
    {
        return rtrim(strtr(base64_encode($bin), '+/', '-_'), '=');
    }

    /**
     * 构造 RS256 服务账号 JWT。
     * header {alg:RS256,typ:JWT}；claim iss/client_email/aud/exp/iat/scope。
     */
    public function jwt(): string
    {
        $sa = $this->service_account_json();
        $email = (string) ($sa['client_email'] ?? $this->email);
        $privateKeyPem = (string) ($sa['private_key'] ?? '');
        if ($privateKeyPem === '') {
            throw new RuntimeException('服务账号 JSON 缺少 private_key');
        }

        $now = time();
        $header = ['alg' => 'RS256', 'typ' => 'JWT'];
        $claim = [
            'iss' => $email,
            'client_email' => $email,
            'aud' => self::OAUTH_TOKEN_URL,
            'exp' => $now + 3600,
            'iat' => $now,
            'scope' => self::OAUTH_SCOPE,
        ];
        $signingInput = self::b64url(json_encode($header, JSON_UNESCAPED_SLASHES))
            . '.' . self::b64url(json_encode($claim, JSON_UNESCAPED_SLASHES));

        $pkey = openssl_pkey_get_private($privateKeyPem);
        if ($pkey === false) {
            throw new RuntimeException('私钥不可用（JSON 可能被截断/损坏）');
        }
        $sig = '';
        if (!openssl_sign($signingInput, $sig, $pkey, OPENSSL_ALGO_SHA256)) {
            throw new RuntimeException('JWT 签名失败');
        }
        return $signingInput . '.' . self::b64url($sig);
    }

    /** token 缓存文件路径（data/tokens/，按 key/project/email 去重）。 */
    private function token_cache_path(): string
    {
        $dir = Config::dataDir() . '/tokens';
        if (!is_dir($dir)) {
            @mkdir($dir, 0700, true);
        }
        $id = md5($this->keyPath . '|' . $this->projectId . '|' . $this->email);
        return $dir . '/' . $id . '.json';
    }

    /**
     * 取 access_token（带缓存，过期前 60 秒刷新）。
     */
    public function token(): string
    {
        $cacheFile = $this->token_cache_path();
        if (is_file($cacheFile)) {
            $raw = @file_get_contents($cacheFile);
            if ($raw !== false) {
                $c = json_decode($raw, true);
                if (is_array($c) && !empty($c['access_token'])
                    && (float) ($c['expires_at'] ?? 0) - 60 > time()) {
                    return (string) $c['access_token'];
                }
            }
        }

        $jwt = $this->jwt();
        $body = http_build_query([
            'grant_type' => 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion'  => $jwt,
        ]);
        $res = self::curl_request(
            'POST',
            self::OAUTH_TOKEN_URL,
            ['Content-Type: application/x-www-form-urlencoded'],
            $body,
            15,
            30,
            $this->proxyUrl,
            $this->proxyType,
            false
        );
        if ($res['error'] !== '') {
            throw new RuntimeException('OAuth 请求失败：' . $res['error']);
        }
        $data = json_decode($res['body'], true);
        if ((int) $res['status'] >= 400 || !is_array($data) || empty($data['access_token'])) {
            $detail = is_array($data) ? (string) ($data['error_description'] ?? json_encode($data)) : (string) $res['body'];
            throw new RuntimeException('获取 access_token 失败：HTTP ' . $res['status'] . ' ' . mb_substr($detail, 0, 300));
        }
        $token = (string) $data['access_token'];
        $expiresIn = (int) ($data['expires_in'] ?? 3600);
        $payload = json_encode(['access_token' => $token, 'expires_at' => time() + $expiresIn], JSON_UNESCAPED_SLASHES);
        @file_put_contents($cacheFile, (string) $payload, LOCK_EX);
        @chmod($cacheFile, 0600);
        return $token;
    }

    // ==================================================================
    // HTTP / REST 基础设施（curl）
    // ==================================================================

    /**
     * 统一 curl 封装。
     *
     * 安全约束：SSL 校验强制开启；不允许跟随跳转（防止跳到 file:// 等协议）；
     * curl 只允许 HTTP/HTTPS 协议；连接与总超时都必须设置。
     *
     * @return array{status:int,body:string,error:string}
     */
    public static function curl_request(
        string $method,
        string $url,
        array $headers = [],
        ?string $body = null,
        int $connectTimeout = 15,
        int $timeout = 60,
        string $proxyUrl = '',
        string $proxyType = '',
        bool $headOnly = false
    ): array {
        $ch = curl_init();
        curl_setopt_array($ch, [
            CURLOPT_URL => $url,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => false,        // 禁止跟随跳转（防 file:// 等）
            CURLOPT_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,
            CURLOPT_REDIR_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS,
            CURLOPT_SSL_VERIFYPEER => true,
            CURLOPT_SSL_VERIFYHOST => 2,
            CURLOPT_CONNECTTIMEOUT => $connectTimeout,
            CURLOPT_TIMEOUT => $timeout,
            CURLOPT_HTTPHEADER => $headers,
            CURLOPT_USERAGENT => 'GCP-Manager-Web/php',
        ]);

        $m = strtoupper($method);
        if ($m === 'POST') {
            curl_setopt($ch, CURLOPT_POST, true);
            curl_setopt($ch, CURLOPT_POSTFIELDS, $body ?? '');
        } elseif ($m === 'GET') {
            curl_setopt($ch, CURLOPT_HTTPGET, true);
        } else {
            curl_setopt($ch, CURLOPT_CUSTOMREQUEST, $m);
            if ($body !== null) {
                curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
            }
        }
        $headStatus = 0;      // HEAD 模式下由 HEADERFUNCTION 抓到的状态码
        if ($headOnly) {
            // ★ 绝不能用 CURLOPT_NOBODY —— 那是发 **HEAD** 请求，而我们的探测目标
            //   https://www.googleapis.com/discovery/v1/apis 在 HEAD 下一律返回 404
            //   （实测 HEAD=404 / GET=200），而判定是 `$code < 400`，
            //   于是**代理完全正常也会被判成「不通」**，用户看到的就是「代理不可用」。
            //   正确做法：发 GET，拿到响应头后立刻中断传输 —— 状态码已到手，
            //   不必等下完 body（那个 discovery 列表有 380KB）。
            curl_setopt($ch, CURLOPT_HTTPGET, true);
            curl_setopt($ch, CURLOPT_HEADERFUNCTION, static function ($c, $line) use (&$headStatus) {
                if (preg_match('#^HTTP/\S+\s+(\d{3})#', $line, $mm)) {
                    $headStatus = (int) $mm[1];
                }
                return strlen($line);
            });
            curl_setopt($ch, CURLOPT_WRITEFUNCTION, static fn($c, $chunk) => 0);
        }

        // 代理：URL 里可能带 user:pass，curl 会自行解析凭据
        if ($proxyUrl !== '') {
            curl_setopt($ch, CURLOPT_PROXY, $proxyUrl);
            $type = self::curl_proxy_type($proxyType);
            if ($type !== null) {
                curl_setopt($ch, CURLOPT_PROXYTYPE, $type);
            }
        }

        $resp = curl_exec($ch);
        $errno = curl_errno($ch);
        $err = curl_error($ch);
        $status = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);

        if ($errno !== 0) {
            // headOnly 模式下我们用 WRITEFUNCTION 返回 0 主动中断传输，
            // curl 会把它报成 CURLE_WRITE_ERROR(23)。这不是故障：
            // 只要状态码已经拿到，就说明请求**已经通了**，应照常返回状态码。
            if ($headOnly && $errno === 23 && $headStatus > 0) {
                return ['status' => $headStatus, 'body' => '', 'error' => ''];
            }
            return ['status' => 0, 'body' => '', 'error' => 'curl(' . $errno . '): ' . $err];
        }
        if ($headOnly && $headStatus > 0) {
            $status = $headStatus;   // 以 HEADERFUNCTION 抓到的为准（重定向场景更准）
        }
        return ['status' => $status, 'body' => is_string($resp) ? $resp : '', 'error' => ''];
    }

    private static function curl_proxy_type(string $ptype): ?int
    {
        switch (strtoupper($ptype)) {
            case 'SOCKS4': return CURLPROXY_SOCKS4;
            case 'SOCKS5': return CURLPROXY_SOCKS5;
            case 'SOCKS5H': return CURLPROXY_SOCKS5_HOSTNAME;
            case 'HTTP': return CURLPROXY_HTTP;
            case 'HTTPS': return CURLPROXY_HTTP; // HTTPS 代理同样走 HTTP CONNECT
            default: return null;
        }
    }

    /** URL 路径片段白名单校验（防路径注入）。 */
    private static function seg(string $v): string
    {
        if (!preg_match('/^[A-Za-z0-9._-]+$/', $v)) {
            throw new InvalidArgumentException("非法的资源标识：{$v}");
        }
        return $v;
    }

    /**
     * 带鉴权的 REST 调用（自动补 Bearer token + 代理）。失败抛 RuntimeException。
     */
    public function rest(string $method, string $url, ?array $params = null, ?array $jsonBody = null, int $timeout = 60, int $connectTimeout = 15): array
    {
        $full = $url;
        if ($params) {
            $full .= (strpos($url, '?') === false ? '?' : '&') . http_build_query($params);
        }
        $headers = ['Authorization: Bearer ' . $this->token(), 'Accept: application/json'];
        $body = null;
        $m = strtoupper($method);
        if ($jsonBody !== null) {
            $body = json_encode($jsonBody, JSON_UNESCAPED_SLASHES);
            $headers[] = 'Content-Type: application/json';
        }
        $res = self::curl_request($m, $full, $headers, $body, $connectTimeout, $timeout, $this->proxyUrl, $this->proxyType);
        if ($res['error'] !== '') {
            throw new RuntimeException($res['error']);
        }
        $data = json_decode($res['body'], true);
        if ((int) $res['status'] >= 400) {
            $detail = '';
            if (is_array($data) && isset($data['error']['message'])) {
                $detail = (string) $data['error']['message'];
            } else {
                $detail = (string) $res['body'];
            }
            throw new RuntimeException('HTTP ' . $res['status'] . ' ' . mb_substr($detail, 0, 300));
        }
        return is_array($data) ? $data : [];
    }

    /** 供 Inspect 复用的只读 HTTP 读取（不解码即返回原始体，失败抛异常）。 */
    public function rest_get(string $url, array $params = [], int $timeout = 30): array
    {
        return $this->rest('GET', $url, $params, null, $timeout);
    }

    // ==================================================================
    // 规格规整 build_instance_spec
    // ==================================================================

    /**
     * 把前端传来的配置规整成出参齐全的 spec —— 「自定义选择服务器配置」的唯一入口。
     */
    public static function build_instance_spec(?array $userSpec): array
    {
        $userSpec = $userSpec ?? [];
        $spec = Catalog::DEFAULT_CONFIG;
        foreach ($userSpec as $k => $v) {
            if ($v !== null) {
                $spec[$k] = $v;
            }
        }

        if (empty($spec['region'])) {
            $zone = (string) ($spec['zone'] ?? '');
            if (strpos($zone, '-') !== false) {
                $spec['region'] = substr($zone, 0, (int) strrpos($zone, '-'));
            } else {
                $spec['region'] = 'us-central1';
            }
        }

        $spec['machine_type'] = self::norm($spec['machine_type'] ?? null, Catalog::DEFAULT_CONFIG['machine_type']);
        $spec['image_key']    = self::norm($spec['image_key'] ?? null, Catalog::DEFAULT_CONFIG['image_key']);
        $spec['disk_type']    = self::norm($spec['disk_type'] ?? null, Catalog::DEFAULT_CONFIG['disk_type']);
        $spec['disk_size_gb'] = (int) self::norm($spec['disk_size_gb'] ?? null, Catalog::DEFAULT_CONFIG['disk_size_gb']);

        $img = Catalog::IMAGES[$spec['image_key']] ?? [];
        $spec['image_source'] = self::norm($spec['image_source'] ?? null, Catalog::image_source($spec['image_key']));
        $spec['image_label']  = $img['label'] ?? $spec['image_key'];
        $spec['image_os']     = $img['os'] ?? 'linux';
        $spec['image_user']   = $img['default_user'] ?? 'ubuntu';

        // 自定义机型：允许直接填 n2-standard-4 这类字符串
        $spec['machine_type_source'] = isset(Catalog::MACHINE_TYPES[$spec['machine_type']]) ? 'catalog' : 'custom';

        $net = $spec['network'] ?? '';
        if ($net === 'default') {
            $spec['network_url'] = self::DEFAULT_NETWORK;
        } elseif (strncmp((string) $net, 'projects/', 9) === 0 || strncmp((string) $net, 'global/', 7) === 0) {
            $spec['network_url'] = (string) $net;
        } else {
            $spec['network_url'] = "global/networks/{$net}";
        }

        $subnet = (string) ($spec['subnet'] ?? '') !== '' ? (string) $spec['subnet'] : 'default';
        $region = trim((string) ($spec['region'] ?? ''));
        if ($region === '') {
            $region = 'us-central1';
        }
        if (substr_count($region, '-') >= 2) {   // 传的是 zone → 归一化为 region
            $region = substr($region, 0, (int) strrpos($region, '-'));
        }
        $spec['region'] = $region;

        if ($subnet === 'default') {
            $spec['subnet_url'] = sprintf(self::DEFAULT_SUBNET_FMT, $region);
        } elseif (strncmp($subnet, 'regions/', 8) === 0) {
            $spec['subnet_url'] = $subnet;
        } else {
            $spec['subnet_url'] = "regions/{$region}/subnetworks/{$subnet}";
        }

        $tags = $spec['tags'] ?? null;
        if (is_string($tags)) {
            $tags = array_values(array_filter(array_map('trim', explode(',', $tags)), static fn($t) => $t !== ''));
        }
        $spec['tags'] = $tags ?: [];

        $spec['preemptible'] = (bool) ($spec['preemptible'] ?? false);
        $spec['spot'] = (bool) ($spec['spot'] ?? false);
        $spec['assign_public_ip'] = (bool) ($spec['assign_public_ip'] ?? true);
        $spec['auto_open_firewall'] = (bool) ($spec['auto_open_firewall'] ?? false);
        $spec['disable_ops_agent'] = (bool) ($spec['disable_ops_agent'] ?? true);
        $spec['no_backup'] = (bool) ($spec['no_backup'] ?? true);
        $spec['no_snapshot_schedule'] = (bool) ($spec['no_snapshot_schedule'] ?? true);
        $spec['no_resource_policy'] = (bool) ($spec['no_resource_policy'] ?? true);
        $spec['deletion_protection'] = (bool) ($spec['deletion_protection'] ?? false);
        return $spec;
    }

    private static function norm($value, $default)
    {
        return ($value === null || $value === '') ? $default : $value;
    }

    /**
     * 取 URL / 路径的最后一段（GCP 的 xxxUrl 字段都是 selfLink）。
     *
     * ★★ 2026-09-27 血案：全仓曾用 `Gcp::short_name($s)` 表达这个意思。
     *    **`strrpos` 找不到时返回 `false`，`(int) false === 0`，于是变成 `substr($s, 1)`
     *    —— 静默吃掉第一个字符。** GCP 的 selfLink 大多含 '/'，所以平时看不出问题；
     *    一旦某个字段是裸值（实例名、`PERSISTENT`、账号 id 之类），就会被悄悄改坏：
     *      · `disks[0].type` = "PERSISTENT" → 界面显示 "ERSISTENT"
     *      · 磁盘 selfLink 的 basename 是磁盘名 → 被当成"镜像"显示
     *    这类 bug 不会报错、不会抛异常，只是界面上的字错了 —— 最难发现的一种。
     *    统一走这里，别再手写。
     */
    public static function short_name(?string $s): string
    {
        $s = trim((string) $s);
        if ($s === '') {
            return '';
        }
        $p = strrpos($s, '/');
        return $p === false ? $s : substr($s, $p + 1);
    }

    /**
     * 从各种 VPC 写法里取出短名。
     * 接受 'jxihegwg' / 'global/networks/jxihegwg' / 'projects/p/global/networks/...' 等。
     */
    public static function network_short_name(?string $network): string
    {
        $s = rtrim(trim((string) $network), '/');
        if ($s === '') {
            return '';
        }
        if (strpos($s, '/') !== false) {
            $s = Gcp::short_name($s);
        }
        return $s;
    }

    // ==================================================================
    // 实例解析（REST JSON → 前端扁平字典）
    // ==================================================================

    /**
     * 把 GCP Instance（REST JSON）解析成前端要用的扁平字典。
     */
    public static function parse_instance(array $inst, string $zone, string $projectId = '', string $accountEmail = ''): array
    {
        $ip = '';
        $privateIp = '';
        $ni = $inst['networkInterfaces'][0] ?? null;
        if (is_array($ni)) {
            $privateIp = (string) ($ni['networkIP'] ?? '');
            $ac = $ni['accessConfigs'][0] ?? null;
            if (is_array($ac)) {
                $ip = (string) ($ac['natIP'] ?? '');
            }
        }

        $mtUrl = (string) ($inst['machineType'] ?? '');
        $mt = Gcp::short_name($mtUrl);
        $zname = ltrim((string) $zone, '/');
        if (strncmp($zname, 'zones/', 6) === 0) {
            $zname = substr($zname, 6);
        }
        if ($zname === '' && isset($inst['zone'])) {
            $zname = ltrim((string) $inst['zone'], '/');
            if (strncmp($zname, 'zones/', 6) === 0) {
                $zname = substr($zname, 6);
            }
        }

        // 引导盘：类型 / 容量 / 来源镜像 / 模式
        //
        // ★★ 2026-09-27 实测修正（用真实 GCP 响应核对过）：
        //   运行中实例的 disks[0] 长这样（已跑过一段时间的机器 initializeParams 就没了）：
        //     {"type":"PERSISTENT","mode":"READ_WRITE",
        //      "source":".../zones/X/disks/vm-1-76864-1-4505",
        //      "boot":true,"licenses":[".../centos-cloud/global/licenses/centos-stream-9"],
        //      "diskSizeGb":"30"}
        //   于是原来那两处「兜底」全都取错了字段：
        //     · disks[0].type 是**磁盘模式**（PERSISTENT / SCRATCH），不是磁盘类型。
        //       拿它当 disk_type 显示，界面写的就是 "PERSISTENT"（还被 strrpos 吃掉首字母）——
        //       而真正的 pd-standard / pd-balanced 在 initializeParams.diskType 里，
        //       运行中实例拿不到。正确做法是留空，由本地库记录兜底，而不是拿模式冒充类型。
        //     · disks[0].source 是**源磁盘的 selfLink**，basename 就是磁盘名
        //       （≈ 实例名）。拿它当"镜像"显示，用户看到的是一串实例名。
        //       真正的镜像线索在 licenses[] 里（centos-stream-9 之类）。
        $diskType = '';
        $diskSize = 0;
        $imageSrc = '';
        $diskMode = '';
        $licenses = [];
        $disks = $inst['disks'] ?? [];
        if (is_array($disks) && isset($disks[0]) && is_array($disks[0])) {
            $boot = $disks[0];
            $diskSize = (int) ($boot['diskSizeGb'] ?? 0);
            $diskMode = strtoupper((string) ($boot['type'] ?? ''));   // PERSISTENT / SCRATCH
            $licenses = is_array($boot['licenses'] ?? null) ? $boot['licenses'] : [];
            $params = $boot['initializeParams'] ?? null;
            if (is_array($params)) {
                $diskType = Gcp::short_name((string) ($params['diskType'] ?? ''));
                $imageSrc = (string) ($params['sourceImage'] ?? '');
                if (!$diskSize) {
                    $diskSize = (int) ($params['diskSizeGb'] ?? 0);
                }
            }
            // 磁盘类型不再退回磁盘模式：拿不到就留空（上层用本地记录兜底）
        }

        // 镜像：sourceImage → licenses 推断 → 留空。
        // 绝不退回 disks[0].source（那是磁盘，不是镜像）。
        $imageFrom = '';
        if ($imageSrc !== '') {
            $imageFrom = 'sourceImage';
        } elseif ($licenses !== []) {
            // licenses 形如 .../projects/centos-cloud/global/licenses/centos-stream-9
            $imageSrc = (string) $licenses[0];
            $imageFrom = 'license';
        }

        // 抢占式 / Spot：GCP 用两个不同字段表达，两个都要看
        $sched = $inst['scheduling'] ?? [];
        $preemptible = is_array($sched) ? (bool) ($sched['preemptible'] ?? false) : false;
        $provisioning = is_array($sched) ? strtoupper((string) ($sched['provisioningModel'] ?? '')) : '';
        $spot = $provisioning === 'SPOT';

        // creationTimestamp 是 RFC3339
        $rawCreated = (string) ($inst['creationTimestamp'] ?? '');
        $createdTs = 0.0;
        if ($rawCreated !== '') {
            try {
                $dt = new DateTime($rawCreated);
                $createdTs = (float) $dt->getTimestamp();
            } catch (Throwable $e) {
                $createdTs = 0.0;
            }
        }

        $region = Catalog::region_of_zone($zname);
        return [
            'name' => (string) ($inst['name'] ?? ''),
            'ip' => $ip,
            'private_ip' => $privateIp,
            'zone' => $zname,
            'region' => $region,
            'location' => Catalog::region_label($region),
            'status' => (string) ($inst['status'] ?? ''),
            'machine_type' => $mt,
            'disk_type' => $diskType,
            'disk_size_gb' => $diskSize,
            // 磁盘模式（PERSISTENT / SCRATCH）—— 以前被误当成 disk_type 显示
            'disk_mode' => $diskMode,
            'image' => Gcp::short_name($imageSrc),
            'image_source' => $imageSrc,
            // 镜像这个值是怎么来的：sourceImage（准）/ license（从 licenses 推断）
            // / ''（拿不到）。界面据此决定要不要标「推断」。
            'image_from' => $imageFrom,
            'licenses' => array_map(static fn($l) => Gcp::short_name((string) $l), $licenses),
            'created' => $rawCreated,
            'created_ts' => $createdTs,
            'preemptible' => $preemptible,
            'spot' => $spot,
            'project_id' => $projectId,
            'account_email' => $accountEmail,
        ];
    }

    // ==================================================================
    // 实例列表 / 查询
    // ==================================================================

    /**
     * 列出项目下全部实例（aggregated）。
     *
     * ★ 白名单式丢弃密码字段，只回 has_password（Python 第二轮修过的泄漏点）。
     * 注意：本方法返回的是 GCP 实时数据，本身不含 root 密码（密码在本地库），
     * 但仍显式加 has_password 标记，供上层统一口径。
     */
    public function list_instances(): array
    {
        $url = self::COMPUTE_API . '/projects/' . self::seg($this->projectId) . '/aggregated/instances';
        $data = $this->rest('GET', $url, ['maxResults' => 500], null, 60);
        $out = [];
        $items = $data['items'] ?? [];
        if (is_array($items)) {
            foreach ($items as $zoneKey => $bucket) {
                $insts = is_array($bucket) ? ($bucket['instances'] ?? []) : [];
                if (!is_array($insts)) {
                    continue;
                }
                foreach ($insts as $i) {
                    if (is_array($i)) {
                        $row = self::parse_instance($i, (string) $zoneKey, $this->projectId, $this->email);
                        $out[] = self::sanitize_instance_row($row);
                    }
                }
            }
        }
        usort($out, static fn($a, $b) => [$a['zone'], $a['name']] <=> [$b['zone'], $b['name']]);
        return $out;
    }

    /** 白名单式丢弃敏感字段，只留 has_password。 */
    public static function sanitize_instance_row(array $row): array
    {
        $hasPw = !empty($row['password']);
        foreach (['password', 'root_password', 'private_key', 'ssh_private_key', 'secret', 'token'] as $k) {
            unset($row[$k]);
        }
        $row['has_password'] = $hasPw;
        return $row;
    }

    /** 取单台实例（REST）。 */
    public function get_instance(string $zone, string $name): array
    {
        $z = ltrim($zone, '/');
        if (strncmp($z, 'zones/', 6) === 0) {
            $z = substr($z, 6);
        }
        $url = self::COMPUTE_API . '/projects/' . self::seg($this->projectId)
            . '/zones/' . self::seg($z) . '/instances/' . self::seg($name);
        return $this->rest('GET', $url);
    }

    // ==================================================================
    // 区域 / 可用区 / 网络 / 子网
    // ==================================================================

    public function list_regions(): array
    {
        $url = self::COMPUTE_API . '/projects/' . self::seg($this->projectId) . '/regions';
        try {
            $data = $this->rest('GET', $url, ['maxResults' => 500], null, 45);
        } catch (Throwable $e) {
            return [];
        }
        $out = [];
        foreach (($data['items'] ?? []) as $r) {
            if (is_array($r)) {
                $out[] = (string) ($r['name'] ?? '');
            }
        }
        sort($out);
        return $out;
    }

    /** 区域内真实存在的 zone（region 为空则全部）。 */
    public function list_zones(string $region = ''): array
    {
        $region = trim($region);
        if (substr_count($region, '-') >= 2) {
            $region = substr($region, 0, (int) strrpos($region, '-'));
        }
        $url = self::COMPUTE_API . '/projects/' . self::seg($this->projectId) . '/zones';
        try {
            $data = $this->rest('GET', $url, ['maxResults' => 500], null, 45);
        } catch (Throwable $e) {
            return [];
        }
        $out = [];
        foreach (($data['items'] ?? []) as $z) {
            if (!is_array($z)) {
                continue;
            }
            $name = (string) ($z['name'] ?? '');
            if ($region !== '' && strpos($name, $region . '-') !== 0) {
                continue;
            }
            $out[] = $name;
        }
        sort($out);
        return $out;
    }

    public function list_networks(): array
    {
        $url = self::COMPUTE_API . '/projects/' . self::seg($this->projectId) . '/global/networks';
        try {
            $data = $this->rest('GET', $url, ['maxResults' => 500], null, 45);
        } catch (Throwable $e) {
            return [];
        }
        $out = [];
        foreach (($data['items'] ?? []) as $n) {
            if (is_array($n)) {
                $out[] = (string) ($n['name'] ?? '');
            }
        }
        sort($out);
        return $out;
    }

    public function list_subnetworks(string $region): array
    {
        $region = trim($region);
        if ($region === '') {
            $region = 'us-central1';
        }
        if (substr_count($region, '-') >= 2) {
            $region = substr($region, 0, (int) strrpos($region, '-'));
        }
        $url = self::COMPUTE_API . '/projects/' . self::seg($this->projectId)
            . '/regions/' . self::seg($region) . '/subnetworks';
        try {
            $data = $this->rest('GET', $url, ['maxResults' => 500], null, 45);
        } catch (Throwable $e) {
            return [];
        }
        $out = [];
        foreach (($data['items'] ?? []) as $s) {
            if (is_array($s)) {
                $out[] = (string) ($s['name'] ?? '');
            }
        }
        sort($out);
        return $out;
    }

    // ==================================================================
    // 防火墙
    // ==================================================================

    /** 列出项目防火墙规则（REST JSON 原样返回 items）。 */
    public function list_firewalls(): array
    {
        $url = self::COMPUTE_API . '/projects/' . self::seg($this->projectId) . '/global/firewalls';
        $data = $this->rest('GET', $url, ['maxResults' => 500], null, 45);
        $items = $data['items'] ?? [];
        return is_array($items) ? $items : [];
    }

    /**
     * 检查某 VPC 在 INGRESS / EGRESS 方向是否已被「全开放」规则覆盖。
     *
     * 返回 [缺失方向列表, 说明]。只要同 VPC 上存在一条 INGRESS（或 EGRESS）规则
     * 同时满足「优先级 ≤ 1000 / 源(或目标) 0.0.0.0/0 / 协议 all」，就认为该方向已覆盖。
     *
     * ★ 刻意返回「缺失方向列表」而不是布尔：调用方只补缺的一侧。
     * 旧写法会去 UPDATE 用户已收敛的规则，等于把 0.0.0.0/0 重新铺开。
     */
    public function firewall_coverage(string $network = '', string $networkUrl = ''): array
    {
        $netName = self::network_short_name($networkUrl);
        if ($netName === '') {
            $netName = self::network_short_name($network);
        }
        try {
            $rules = $this->list_firewalls();
        } catch (Throwable $e) {
            return [[], '列举规则失败：' . $e->getMessage()];
        }

        $missing = [];
        foreach (['INGRESS', 'EGRESS'] as $want) {
            $covered = false;
            foreach ($rules as $f) {
                if (!is_array($f)) {
                    continue;
                }
                if ($netName !== '' && self::network_short_name((string) ($f['network'] ?? '')) !== $netName) {
                    continue;
                }
                if (!empty($f['disabled'])) {
                    continue;
                }
                if ((int) ($f['priority'] ?? 65535) > 1000) {
                    continue;
                }
                $dir = strtoupper((string) ($f['direction'] ?? 'INGRESS'));
                if ($dir !== $want) {
                    continue;
                }
                // 协议必须是 all
                $protoAll = false;
                foreach (($f['allowed'] ?? []) as $a) {
                    if (is_array($a) && strtolower((string) ($a['IPProtocol'] ?? '')) === 'all') {
                        $protoAll = true;
                        break;
                    }
                }
                if (!$protoAll) {
                    continue;
                }
                $rangesRaw = $want === 'INGRESS' ? ($f['sourceRanges'] ?? []) : ($f['destinationRanges'] ?? []);
                $ranges = is_array($rangesRaw) ? array_map('strval', $rangesRaw) : [];
                if (in_array('0.0.0.0/0', $ranges, true)) {
                    $covered = true;
                    break;
                }
            }
            if (!$covered) {
                $missing[] = $want;
            }
        }
        return [$missing, ''];
    }

    /**
     * 为指定 VPC 补齐全开放入站/出站规则。返回 [ok, 文案]。
     *
     * ★ 必须绑定实例所在 VPC（Python 硬编码 global/networks/default 曾导致 404）。
     */
    public function create_open_firewall_rules(bool $ingress = true, bool $egress = true, int $priority = 1000, string $network = ''): array
    {
        $netName = self::network_short_name($network);
        if ($netName === '') {
            $netName = 'default';
        }
        $netUrl = "global/networks/{$netName}";
        $msgs = [];
        $directions = [];
        if ($ingress) {
            $directions[] = 'INGRESS';
        }
        if ($egress) {
            $directions[] = 'EGRESS';
        }
        foreach ($directions as $dir) {
            $baseName = 'gcp-manager-open-' . strtolower($dir);
            $name = $baseName;
            $exists = false;
            // 检查同名规则是否已存在，并确认它绑定的是目标 VPC
            $getUrl = self::COMPUTE_API . '/projects/' . self::seg($this->projectId) . '/global/firewalls/' . self::seg($baseName);
            try {
                $cur = $this->rest('GET', $getUrl, null, null, 30);
                $exists = true;
                $curNet = self::network_short_name((string) ($cur['network'] ?? ''));
                if ($curNet !== '' && $curNet !== $netName) {
                    // 同名规则绑在别的 VPC 上 → 换个名字，别覆盖
                    $name = $baseName . '-' . $netName;
                }
            } catch (Throwable $e) {
                $exists = false;
            }

            $body = [
                'name' => $name,
                'network' => $netUrl,
                'direction' => $dir,
                'priority' => $priority,
                'allowed' => [['IPProtocol' => 'all']],
            ];
            if ($dir === 'INGRESS') {
                $body['sourceRanges'] = ['0.0.0.0/0'];
            } else {
                $body['destinationRanges'] = ['0.0.0.0/0'];
            }

            try {
                if ($exists) {
                    $putUrl = self::COMPUTE_API . '/projects/' . self::seg($this->projectId) . '/global/firewalls/' . self::seg($name);
                    $this->rest('PUT', $putUrl, null, $body, 45);
                    $msgs[] = "已更新规则 {$name}（{$dir} 全开放）";
                } else {
                    $postUrl = self::COMPUTE_API . '/projects/' . self::seg($this->projectId) . '/global/firewalls';
                    $op = $this->rest('POST', $postUrl, null, $body, 45);
                    $this->wait_operation($op);
                    $msgs[] = "已创建规则 {$name}（{$dir} 全开放）";
                }
            } catch (Throwable $e) {
                $msg = $e->getMessage();
                if (stripos($msg, 'already exists') !== false) {
                    $msgs[] = "规则 {$name} 已存在（{$dir}）";
                } else {
                    return [false, "防火墙 {$dir} 处理失败：" . $msg];
                }
            }
        }
        return [true, implode('；', $msgs)];
    }

    // ==================================================================
    // 项目元数据（SSH 公钥注入）
    // ==================================================================

    /** 把 SSH 公钥追加到项目级 commonInstanceMetadata 的 ssh-keys 项。返回 [ok, 文案]。 */
    public function add_ssh_key(string $pubKey, string $username = 'root'): array
    {
        try {
            $url = self::COMPUTE_API . '/projects/' . self::seg($this->projectId);
            $proj = $this->rest('GET', $url, null, null, 30);
            $meta = $proj['commonInstanceMetadata'] ?? [];
            if (!is_array($meta)) {
                $meta = [];
            }
            $items = $meta['items'] ?? [];
            if (!is_array($items)) {
                $items = [];
            }
            $keyLine = $username . ':' . trim($pubKey);
            $found = false;
            foreach ($items as &$item) {
                if (is_array($item) && strtolower((string) ($item['key'] ?? '')) === 'ssh-keys') {
                    $found = true;
                    $val = (string) ($item['value'] ?? '');
                    if (strpos($val, $keyLine) === false) {
                        $item['value'] = $val === '' ? $keyLine : ($val . "\n" . $keyLine);
                    }
                    break;
                }
            }
            unset($item);
            if (!$found) {
                $items[] = ['key' => 'ssh-keys', 'value' => $keyLine];
            }
            $meta['items'] = $items;
            $setUrl = self::COMPUTE_API . '/projects/' . self::seg($this->projectId) . '/setCommonInstanceMetadata';
            $op = $this->rest('POST', $setUrl, null, $meta, 45);
            $this->wait_operation($op);
            return [true, '公钥注入成功'];
        } catch (Throwable $e) {
            return [false, $e->getMessage()];
        }
    }

    // ==================================================================
    // 实例创建 / 删除 / 启停
    // ==================================================================

    /**
     * 创建实例。$spec 为 null 时用默认配置。返回 [ok, payload|文案]。
     */
    public function create_instance(string $zone, string $name, string $startupScript = '', ?array $spec = null, ?array $sshKeys = null): array
    {
        $spec = self::build_instance_spec($spec);
        $z = ltrim($zone, '/');
        if (strncmp($z, 'zones/', 6) === 0) {
            $z = substr($z, 6);
        }
        $region = Catalog::region_of_zone($z);
        $spec['region'] = $region;

        // 引导盘
        $initParams = [
            'sourceImage' => $spec['image_source'],
            'diskSizeGb' => (int) $spec['disk_size_gb'],
            'diskType' => "zones/{$z}/diskTypes/{$spec['disk_type']}",
        ];
        if (!empty($spec['no_snapshot_schedule'])) {
            // 省钱：不挂快照时间表
            $initParams['resourcePolicies'] = [];
        }
        $disk = [
            'boot' => true,
            'autoDelete' => true,
            'initializeParams' => $initParams,
        ];

        // 网络接口
        $nic = ['network' => $spec['network_url']];
        if (!empty($spec['subnet_url'])) {
            $nic['subnetwork'] = $spec['subnet_url'];
        }
        if (!empty($spec['assign_public_ip'])) {
            $nic['accessConfigs'][] = [
                'name' => 'External NAT',
                'type' => 'ONE_TO_ONE_NAT',
                'networkTier' => $spec['network_tier'] ?? 'STANDARD',
            ];
        }

        $inst = [
            'name' => $name,
            'machineType' => "zones/{$z}/machineTypes/{$spec['machine_type']}",
            'disks' => [$disk],
            'networkInterfaces' => [$nic],
            'deletionProtection' => (bool) $spec['deletion_protection'],
        ];
        if (!empty($spec['tags'])) {
            $inst['tags'] = ['items' => array_values($spec['tags'])];
        }

        // metadata：省钱（禁用 ops agent）+ 启动脚本
        $metaItems = [];
        if (!empty($spec['disable_ops_agent'])) {
            $metaItems[] = ['key' => 'google-logging-enabled', 'value' => 'false'];
            $metaItems[] = ['key' => 'google-monitoring-enabled', 'value' => 'false'];
        }
        if ($startupScript !== '') {
            $metaItems[] = ['key' => 'startup-script', 'value' => $startupScript];
        }
        if ($metaItems) {
            $inst['metadata'] = ['items' => $metaItems];
        }
        if ($sshKeys !== null && $sshKeys !== []) {
            $inst['metadata'] = ['items' => array_merge(
                $inst['metadata']['items'] ?? [],
                [['key' => 'ssh-keys', 'value' => implode("\n", $sshKeys)]]
            )];
        }

        // 抢占式 / Spot
        if (!empty($spec['preemptible']) || !empty($spec['spot'])) {
            $sched = [
                'preemptible' => (bool) $spec['preemptible'],
                'automaticRestart' => false,
                'onHostMaintenance' => 'TERMINATE',
            ];
            if (!empty($spec['spot'])) {
                $sched['provisioningModel'] = 'SPOT';
                $sched['instanceTerminationAction'] = 'STOP';
            }
            $inst['scheduling'] = $sched;
        }

        $url = self::COMPUTE_API . '/projects/' . self::seg($this->projectId)
            . '/zones/' . self::seg($z) . '/instances';
        try {
            $op = $this->rest('POST', $url, null, $inst, 120, 20);
            $this->wait_operation($op);
        } catch (Throwable $e) {
            $msg = $e->getMessage();
            if (stripos($msg, 'resource_pool_exhausted') !== false || stripos($msg, 'ZONE_RESOURCE_POOL_EXHAUSTED') !== false) {
                return [false, "区域 {$z} 资源耗尽（ZONE_RESOURCE_POOL_EXHAUSTED），请换可用区或机型：" . $msg];
            }
            return [false, $msg];
        }

        // 取回实例详情（尽力而为；取不到不影响创建成功）
        $base = [];
        try {
            $got = $this->get_instance($z, $name);
            $base = self::parse_instance($got, $z, $this->projectId, $this->email);
        } catch (Throwable $e) {
            $base = ['name' => $name, 'zone' => $z, 'region' => $region];
        }

        // 防火墙（按需，绑定实例所在 VPC）
        if (!empty($spec['auto_open_firewall'])) {
            $netShort = self::network_short_name((string) ($base['network'] ?? '')) ?: 'default';
            [$missing, $why] = $this->firewall_coverage('', (string) ($spec['network_url'] ?? ''));
            if ($why !== '') {
                $base['firewall'] = ['ok' => false, 'message' => $why];
            } elseif ($missing) {
                [$ok, $fwMsg] = $this->create_open_firewall_rules(
                    in_array('INGRESS', $missing, true),
                    in_array('EGRESS', $missing, true),
                    1000,
                    $spec['network_url'] ?? ''
                );
                $base['firewall'] = ['ok' => $ok, 'message' => $fwMsg];
            } else {
                $base['firewall'] = ['ok' => true, 'message' => '入站/出站全开放规则已存在，未改动'];
            }
        }
        return [true, $base];
    }

    /** 删除实例（检查 operation 结果）。返回 [ok, 文案]。 */
    public function delete_instance(string $zone, string $name): array
    {
        return $this->operate('DELETE', $zone, $name, '已删除');
    }

    public function start_instance(string $zone, string $name): array
    {
        return $this->operate('POST', $zone, $name, '已启动', 'start');
    }

    public function stop_instance(string $zone, string $name): array
    {
        return $this->operate('POST', $zone, $name, '已停止', 'stop');
    }

    public function reset_instance(string $zone, string $name): array
    {
        return $this->operate('POST', $zone, $name, '已重启', 'reset');
    }

    /**
     * 通用实例动作。DELETE / start / stop / reset 都检查 operation 结果。
     */
    private function operate(string $method, string $zone, string $name, string $okMsg, string $verb = ''): array
    {
        $z = ltrim($zone, '/');
        if (strncmp($z, 'zones/', 6) === 0) {
            $z = substr($z, 6);
        }
        $base = self::COMPUTE_API . '/projects/' . self::seg($this->projectId)
            . '/zones/' . self::seg($z) . '/instances/' . self::seg($name);
        $url = $verb !== '' ? ($base . '/' . $verb) : $base;
        try {
            $op = $this->rest($method, $url, null, $method === 'POST' ? new stdClass() : null, 90, 20);
            if (is_array($op) && isset($op['name'])) {
                $this->wait_operation($op);
            }
            return [true, $okMsg];
        } catch (Throwable $e) {
            $msg = $e->getMessage();
            if (stripos($msg, 'not found') !== false || stripos($msg, 'HTTP 404') !== false) {
                // 删除时不存在视为成功（幂等）
                return [true, $verb === '' ? '实例不存在（视为已删除）' : $msg];
            }
            if (stripos($msg, 'already') !== false) {
                return [true, '实例已处于目标状态'];
            }
            return [false, $msg];
        }
    }

    /** 轮询 operation 直到 DONE；有 error 则抛出。 */
    public function wait_operation(array $op, int $maxWait = 300): array
    {
        if (!is_array($op) || empty($op['name'])) {
            return $op;
        }
        // 已有 status 且 DONE
        if (($op['status'] ?? '') === 'DONE') {
            $this->throw_if_op_error($op);
            return $op;
        }
        $proj = self::seg($this->projectId);
        $opName = (string) $op['name'];
        $scope = '';
        if (!empty($op['zone'])) {
            $scope = 'zones/' . self::seg(self::network_short_name((string) $op['zone']));
        } elseif (!empty($op['region'])) {
            $scope = 'regions/' . self::seg(self::network_short_name((string) $op['region']));
        }
        $url = self::COMPUTE_API . '/projects/' . $proj . ($scope !== '' ? '/' . $scope : '/global')
            . '/operations/' . self::seg($opName);

        $start = time();
        $cur = $op;
        while (($cur['status'] ?? '') !== 'DONE') {
            if (time() - $start > $maxWait) {
                throw new RuntimeException('操作超时未完成：' . $opName);
            }
            usleep(1000000);
            $cur = $this->rest('GET', $url, null, null, 30, 10);
        }
        $this->throw_if_op_error($cur);
        return $cur;
    }

    private function throw_if_op_error(array $op): void
    {
        $errors = $op['error']['errors'] ?? null;
        if (is_array($errors) && $errors) {
            $first = $errors[0];
            $msg = is_array($first) ? (string) ($first['message'] ?? json_encode($first)) : (string) $first;
            $hint = self::explain_quota_error($msg);
            throw new RuntimeException('操作失败：' . $msg . ($hint !== null ? "\n\n" . $hint : ''));
        }
    }

    /**
     * 把 GCP 的配额报错翻译成**可行动**的说明。
     *
     * 实测原文（用户真的撞到过）：
     *   Quota 'CPUS_ALL_REGIONS' exceeded. Limit: 12.0 globally.
     *   Quota 'CPUS' exceeded. Limit: 32.0 in region asia-northeast1.
     *
     * 这句话本身没说错，但对用户没有半点可操作性：看不出是自己的配额满了、
     * 还是 GCP 故障、还是本工具有 bug，也看不出下一步该干什么。
     * 于是这里补一句「是什么 + 为什么重试没用 + 可以怎么办」。
     *
     * ★ 为什么不做「创建前预检配额」：查过了，Compute 的 regions 接口返回 113 条
     *   配额指标，**里面没有 CPUS_ALL_REGIONS**（它只在 Cloud Quotas API 里，
     *   那要额外启用服务）。拿一个查不到的配额去做预检，只会给出虚假的
     *   「配额充足」—— 比不预检更糟。所以这里选择把错误说清楚，而不是假装能预判。
     *
     * @return string|null 命中配额错误时返回说明，否则 null
     */
    public static function explain_quota_error(string $msg): ?string
    {
        $scope = '';
        $quota = '';
        $limit = '';
        if (preg_match(
            "/Quota '([A-Za-z0-9_]+)' exceeded\.\s*Limit:\s*([0-9.]+)\s*(globally|in region\s+([a-z0-9-]+))?/i",
            $msg, $m
        )) {
            $quota = (string) $m[1];
            $limit = (string) $m[2];
            $where = strtolower((string) ($m[3] ?? ''));
            $region = (string) ($m[4] ?? '');
            $scope = $region !== '' ? "区域 {$region}" : ($where === 'globally' ? '全局' : '');
        } elseif (stripos($msg, 'quotaExceeded') === false && stripos($msg, 'QUOTA_EXCEEDED') === false) {
            return null;   // 不是配额问题，别乱加话
        }

        $isCpu = stripos($quota, 'CPU') !== false;
        $lines = [];
        $lines[] = '── 这是 GCP 的配额上限，不是本工具的问题 ──';
        $lines[] = sprintf('配额项：%s%s%s', $quota !== '' ? $quota : '（未标明）',
            $scope !== '' ? "（{$scope}）" : '', $limit !== '' ? "，上限 {$limit}" : '');
        $lines[] = '这个上限由 GCP 账号自身决定，**重试、换区都不会成功** —— 配额已满时';
        $lines[] = '再建只会立刻失败，还白等一轮超时。';
        $lines[] = '';
        $lines[] = '可以怎么办（任选其一）：';
        if ($isCpu) {
            $lines[] = '  1. 删掉几台不用的实例释放 CPU —— 最快的办法；';
            $lines[] = '  2. 用更小 CPU 的机型（例如 e2-micro 只占 0.25 vCPU），同样的钱能开更多台；';
            $lines[] = '  3. 到 GCP 控制台「IAM 与管理 → 配额」申请提高该配额（免费，通常几小时到一天）；';
            $lines[] = '  4. 换一个配额有余量的账号（本工具支持多账号，创建时勾选即可）。';
        } else {
            $lines[] = '  1. 到 GCP 控制台「IAM 与管理 → 配额」查看该配额与当前用量；';
            $lines[] = '  2. 释放已占用该配额的资源，或申请提高上限；';
            $lines[] = '  3. 换一个配额有余量的账号。';
        }
        return implode("\n", $lines);
    }

    // ==================================================================
    // 工具
    // ==================================================================

    private static function uniqueSorted(array $a): array
    {
        $a = array_values(array_unique($a));
        sort($a);
        return $a;
    }

    /** 子串命中检测（大小写不敏感，用于识别代理拦截类错误）。 */
    private static function anyContains(string $haystack, array $needles): bool
    {
        $h = strtolower($haystack);
        foreach ($needles as $n) {
            if ($n !== '' && strpos($h, strtolower($n)) !== false) {
                return true;
            }
        }
        return false;
    }

    /** 拉取公共镜像族清单（只读，供 Inspect / catalog 校正） */
    public function list_image_families(string $imageProject): array
    {
        $url = self::COMPUTE_API . '/projects/' . self::seg($imageProject) . '/global/images';
        $data = $this->rest('GET', $url, ['maxResults' => 200, 'filter' => 'deprecated.state != DEPRECATED'], null, 45);
        $out = [];
        foreach (($data['items'] ?? []) as $img) {
            if (!is_array($img)) {
                continue;
            }
            $out[] = [
                'family' => (string) ($img['family'] ?? $img['name'] ?? ''),
                'name' => (string) ($img['name'] ?? ''),
                'diskSizeGb' => $img['diskSizeGb'] ?? null,
                'status' => (string) ($img['status'] ?? ''),
                'creationTimestamp' => (string) ($img['creationTimestamp'] ?? ''),
            ];
        }
        return $out;
    }

    // ==================================================================
    // 任务编排层（供 bin/task-runner.php 调用）—— 逐条对照 core/tasks.py
    //
    // 为什么放在 Gcp.php 而不是 task-runner.php：
    //   task-runner 是「领取 / 调度 / 终态兜底」的壳；真正会变的业务语义
    //   （区域配额规划、命名规则、换区重试、创建后 SSH 阶段…）属于 GCP
    //   领域逻辑。放在这里可以与 core/tasks.py 逐条对照移植，也让任何入口
    //   （CLI / 未来的 HTTP 同步调用）复用同一套行为，而不是把规则散进脚本。
    //
    // 与 Python 的执行模型差异（明确写出来，避免被误判为漏写）：
    //   Python 是单进程 + ThreadPoolExecutor，create 的并发由
    //   concurrency / account_workers 控制；PHP 这里是「一个任务 = 一个
    //   task-runner 进程」，进程内顺序执行（宝塔环境不引入 pcntl/fork，
    //   保持零依赖）。因此 concurrency / account_workers 在 PHP 侧**不生效**：
    //   结果集（每账号台数、每区配额上限、单台失败不影响其余、换区重试）
    //   与 Python 完全一致，只是不并行。
    //
    // 跨进程状态：一律走 SQLite（Store / Tasks），不用文件锁、不用内存缓存。
    // ==================================================================

    /** 从 accounts 表行构造 GCP 客户端（与 ApiGcp::client 同构） */
    private static function account_client(array $acc): Gcp
    {
        return new Gcp(
            (string) ($acc['key_path'] ?? ''),
            (string) ($acc['project_id'] ?? ''),
            (string) ($acc['email'] ?? ''),
            (string) ($acc['proxy'] ?? ''),
            (string) ($acc['proxy_type'] ?? 'HTTPS')
        );
    }

    /**
     * 区域池解析 —— 对应 Python resolve_region_pool。
     * 返回 [区域列表, 每区上限, 是否限定单区]。
     *
     * region_mode：auto_free（默认，GCP 永久免费三区）/ auto_paid /
     *              custom（用 spec.regions 指定列表）/ single（spec.region 单区）
     */
    public static function resolve_region_pool(?array $spec): array
    {
        $spec = $spec ?? [];
        $mode = trim((string) ($spec['region_mode'] ?? ''));
        if ($mode === '') {
            $mode = 'auto_free';
        }
        // Python 是 int(spec.get("max_per_region") or 4)：null / 空 / 0 都回落到 4
        $rawMax = $spec['max_per_region'] ?? null;
        $maxPerRegion = ($rawMax === null || $rawMax === '' || (int) $rawMax === 0) ? 4 : (int) $rawMax;

        switch ($mode) {
            case 'auto_paid':
                return [array_keys(Catalog::PAID_REGIONS), $maxPerRegion, false];

            case 'custom':
                $pool = [];
                foreach ((array) ($spec['regions'] ?? []) as $r) {
                    $r = trim((string) $r);
                    if ($r !== '') {
                        $pool[] = $r;
                    }
                }
                // Python：只保留目录里认识的区域；一个都不认识时保留原样，
                // 交给 GCP 去报「区域不存在」，而不是静默换成免费区。
                $all = Catalog::all_regions();
                $known = array_values(array_filter($pool, static fn(string $r): bool => isset($all[$r])));
                if ($known !== []) {
                    $pool = $known;
                }
                if ($pool === []) {
                    $pool = array_keys(Catalog::FREE_REGIONS);
                }
                return [$pool, $maxPerRegion, false];

            case 'single':
                // region 可能被写成 zone（us-west1-b）：不归一化就会拼出
                // us-west1-b-b 这种不存在的 zone，被 GCP 报成「权限不足」。
                $region = trim((string) ($spec['region'] ?? ''));
                if ($region === '') {
                    $region = 'us-central1';
                }
                if (substr_count($region, '-') >= 2) {
                    $region = substr($region, 0, (int) strrpos($region, '-'));
                }
                return [[$region], 1000000, true];

            case 'auto_free':
            default:
                return [array_keys(Catalog::FREE_REGIONS), $maxPerRegion, false];
        }
    }

    /**
     * 解析某 region 下真实存在的 zone —— 对应 Python zones_for_region。
     *
     * 两个坑必须保留同样的处理（都是 Python 侧实测踩出来的）：
     *   · 入参可能是 zone；不归一化会拼出不存在的 zone。
     *   · a/b/c/d/f 后缀只是**离线兜底**（无网络/无权限时）：各 region 实际
     *     后缀不同（us-west1 只有 a/b/c），所以优先向 GCP 拉真实列表。
     * 只在拿到真实列表时进 static 缓存（一次任务内同区只查一次）；兜底结果
     * 不缓存，以便下一次能重试真实查询。
     */
    private static function zones_for_region(Gcp $gcp, string $region): array
    {
        $region = trim($region);
        if (substr_count($region, '-') >= 2) {
            $region = substr($region, 0, (int) strrpos($region, '-'));
        }
        static $cache = [];
        if (isset($cache[$region])) {
            return $cache[$region];
        }
        $real = [];
        try {
            $real = $gcp->list_zones($region);
        } catch (Throwable $e) {
            $real = [];   // 无权限/瞬时错误 → 走兜底
        }
        if ($real !== []) {
            return $cache[$region] = $real;
        }
        $fallback = [];
        foreach (['a', 'b', 'c', 'd', 'f'] as $s) {
            $fallback[] = $region . '-' . $s;
        }
        return $fallback;
    }

    /** 随机取一项（等价 Python random.choice；S8：只用 random_int） */
    private static function pick(array $items)
    {
        $n = count($items);
        return $n === 0 ? null : $items[random_int(0, $n - 1)];
    }

    /** 预留一个区域配额 —— 对应 Python 的 reserve()。返回 [选中区|null, 当时可用区列表] */
    private static function reserve_region(array $pool, int $maxPerRegion, bool $single,
                                           array $spec, array &$regionCount): array
    {
        $avail = [];
        foreach ($pool as $r) {
            if (($regionCount[$r] ?? 0) < $maxPerRegion) {
                $avail[] = $r;
            }
        }
        if ($avail === []) {
            return [null, []];      // 所有可用区域配额已满
        }
        if ($single) {
            $chosen = $pool[0];
        } elseif (!empty($spec['region']) && in_array((string) $spec['region'], $avail, true)) {
            // 指定区域仍在可用列表里就优先用它（与 Python 同：单区优先填满再换区）
            $chosen = (string) $spec['region'];
        } else {
            $chosen = self::pick($avail);
        }
        if (!array_key_exists($chosen, $regionCount)) {
            $regionCount[$chosen] = 0;
        }
        $regionCount[$chosen]++;
        return [$chosen, $avail];
    }

    /** 归还配额 —— 对应 Python 的 release() */
    private static function release_region(array &$regionCount, string $region): void
    {
        if ($region !== '' && array_key_exists($region, $regionCount)) {
            $regionCount[$region] = max(0, $regionCount[$region] - 1);
        }
    }

    /** 判断创建失败是否属于「可用区资源耗尽」（值得换区重试）。PHP 的
     *  create_instance 返回的是带前缀的长文案，所以用包含判断而不是等值 ——
     *  Python 那边是精确比对 "资源耗尽" 这个哨兵值，语义等价。 */
    private static function is_resource_exhausted(string $msg): bool
    {
        return stripos($msg, 'ZONE_RESOURCE_POOL_EXHAUSTED') !== false
            || stripos($msg, 'resource_pool_exhausted') !== false
            || strpos($msg, '资源耗尽') !== false;
    }

    /**
     * dry-run 预览 —— 对应 Python _plan_preview。
     * 只做**只读**清点（list_instances），不创建任何资源、不产生费用。
     *
     * ★ public 而不是 private（2026-09-27）：API 层的同步 dry-run 分支要用它。
     *   早前 API 层图省事自己拼了个 `build_instance_spec()` 的结果当 plan 返回 ——
     *   那是「机型/磁盘规格对象」，不是计划数组，前端 `plan.forEach` 直接炸
     *   （Vue runtime-5：plan.forEach is not a function），整块预检结果渲染不出来。
     *   现在 API 层与 worker 共用这一个实现，不可能再分叉。
     */
    public static function plan_preview(array $accounts, int $count, array $spec, array $rawSpec): array
    {
        $out = [];
        foreach ($accounts as $acc) {
            try {
                $gcp = self::account_client($acc);
                $instances = $gcp->list_instances();
            } catch (Throwable $e) {
                $out[] = ['account' => (string) ($acc['email'] ?? ''), 'error' => $e->getMessage()];
                continue;
            }
            [$pool, $maxPerRegion] = self::resolve_region_pool($rawSpec);
            $used = [];
            foreach ($instances as $inst) {
                $region = Catalog::region_of_zone((string) ($inst['zone'] ?? ''));
                $used[$region] = ($used[$region] ?? 0) + 1;
            }
            $avail = [];
            foreach ($pool as $r) {
                if (($used[$r] ?? 0) < $maxPerRegion) {
                    $avail[] = $r;
                }
            }
            $out[] = [
                'account'            => (string) ($acc['email'] ?? ''),
                'project_id'         => (string) ($acc['project_id'] ?? ''),
                'existing_instances' => count($instances),
                // 空 map 必须序列化成 {} 而不是 []，否则前端 typeof 判断会走偏
                'region_usage'       => $used === [] ? new stdClass() : $used,
                'available_regions'  => $avail,
                'planned_instances'  => $count,
                'can_create'         => $avail !== [],
                'machine_type'       => (string) $spec['machine_type'],
            ];
        }
        return $out;
    }

    /**
     * 【任务编排】批量创建实例
     *   = Python submit_create + _run_create_batch + _create_for_account + run_one。
     *
     * @param array    $payload /api/create 请求体（dry_run 已由 task-runner 拦截时可不传）
     * @param string   $taskId  任务 id（写日志 + 取消轮询）
     * @param callable $log     $log(string $msg, string $level='info', ?string $taskId=null)
     * @return array{ok:bool,message:string,result:array}
     */
    public static function runCreateTask(array $payload, string $taskId, callable $log): array
    {
        $spec    = self::build_instance_spec($payload['spec'] ?? null);
        $rawSpec = is_array($payload['spec'] ?? null) ? $payload['spec'] : [];

        // Python 是 int(... or N)：null / '' 都取默认值
        $intOr = static function ($v, int $d): int {
            $i = (int) ($v === null ? 0 : $v);
            return $i > 0 ? $i : $d;
        };
        $count    = max(1, $intOr($payload['count'] ?? null, 1));
        $retries  = max(0, min($intOr($payload['retry_count'] ?? null, 2), 5));

        // 账号筛选：account_ids 为空 = 全部账号
        $accounts = Store::getAccounts();
        $wanted = [];
        foreach ((array) ($payload['account_ids'] ?? []) as $w) {
            $wanted[] = (string) $w;
        }
        if ($wanted !== []) {
            $accounts = array_values(array_filter(
                $accounts,
                static fn(array $a): bool => in_array((string) $a['id'], $wanted, true)
            ));
        }
        if ($accounts === []) {
            // 与 Python 一致：这是业务性拒绝，返回 ok=false → task-runner 落 failed
            return ['ok' => false, 'message' => '没有匹配的账号，请先导入 GCP 服务账号 JSON', 'result' => []];
        }

        $log(sprintf('=== 任务 %s：%d 个账号 × %d 台 ===', $taskId, count($accounts), $count), 'info', $taskId);
        // 把网络也打进日志：排查「防火墙 404 networks/default not found」时，
        // 看不到用的哪个 VPC 就只能靠猜（Python 侧就是为此加的）。
        $log(sprintf(
            '[规格] 机型=%s 镜像=%s 磁盘=%s %sGB 网络=%s/%s 区域模式=%s%s',
            $spec['machine_type'], $spec['image_label'], $spec['disk_type'], $spec['disk_size_gb'],
            (string) (($spec['network'] ?? '') !== '' ? $spec['network'] : 'default'),
            (string) (($spec['subnet'] ?? '') !== '' ? $spec['subnet'] : 'default'),
            (string) ($rawSpec['region_mode'] ?? 'auto_free'),
            ((string) ($rawSpec['region_mode'] ?? '')) === 'single' ? (' 指定区域=' . (string) ($spec['region'] ?? '')) : ''
        ), 'info', $taskId);

        if (!empty($payload['dry_run'])) {
            $plan = self::plan_preview($accounts, $count, $spec, $rawSpec);
            $log('dry-run 预览完成（只读清点，未创建任何资源）', 'success', $taskId);
            return ['ok' => true, 'message' => 'dry-run 预览完成',
                    'result' => ['dry_run' => true, 'plan' => $plan]];
        }

        $loginMode     = trim((string) ($payload['login_mode'] ?? '')) ?: 'root_password';
        $sshPublicKey  = trim((string) ($payload['ssh_public_key'] ?? ''));
        $rootPasswordIn = trim((string) ($payload['root_password'] ?? ''));
        $postCommand   = trim((string) ($payload['post_command'] ?? ''));
        $verifyCommand = trim((string) ($payload['verify_command'] ?? ''));

        // 安装预设展开：Python 是在 app.py（API 层）做的，而 PHP 的 ApiGcp::create
        // 只做参数转发，所以这一步必须落在编排层，否则勾选的 installs 会静默失效。
        $installs = InstallPresets::normalize($payload['installs'] ?? []);
        if ($installs !== []) {
            $presetScript = InstallPresets::build_script($installs);
            if ($presetScript !== '') {
                // 用户自己写的 post_command 优先级更高 —— 预设只是省去手写
                $postCommand = $postCommand === '' ? $presetScript : ($postCommand . "\n\n" . $presetScript);
            }
            if ($verifyCommand === '') {
                $verifyCommand = InstallPresets::verify_command($installs);
            }
            if (!isset($payload['ssh_timeout'])) {
                $payload['ssh_timeout'] = 300;
            }
        }

        if (($spec['image_os'] ?? 'linux') === 'windows' && $loginMode === 'root_password') {
            // 文案沿用 Python 原文；但行为同样是「照旧生成 root 密码 + startup-script」
            // （Python 这里也只有告警，并未真的改走密钥模式）—— 保持两版一致。
            $log('[警告] 镜像为 Windows，startup-script 不会执行 bash，Root 密码模式无效，已按 SSH 密钥模式处理', 'warn', $taskId);
        }

        $started = microtime(true);
        $results = [];
        foreach ($accounts as $acc) {
            if (Tasks::cancelRequested($taskId)) {
                break;
            }
            $results[] = self::create_for_account(
                $taskId, $acc, $count, $spec, $rawSpec, $retries,
                $loginMode, $sshPublicKey, $rootPasswordIn, $postCommand, $verifyCommand,
                $installs, $payload, $log
            );
        }

        $totalOk = 0;
        $totalFail = 0;
        $firstErr = '';
        foreach ($results as $r) {
            $totalOk  += (int) ($r['created'] ?? 0);
            $totalFail += (int) ($r['failed'] ?? 0);
            if ($firstErr === '' && !empty($r['error'])) {
                $firstErr = (string) $r['error'];
            }
            // 账号级没有 error、但单台全失败（例如配额不足）时，把第一台的原因带出来
            if ($firstErr === '') {
                foreach ((array) ($r['instances'] ?? []) as $it) {
                    if (empty($it['ok']) && !empty($it['error'])) {
                        $firstErr = (string) $it['error'];
                        break;
                    }
                }
            }
        }
        $elapsed = round(microtime(true) - $started, 1);

        $summary = [
            'accounts'          => count($accounts),
            'per_account_count' => $count,
            'created'           => $totalOk,
            'failed'            => $totalFail,
            'elapsed_sec'       => $elapsed,
            'results'           => $results,
        ];

        // ★ 与 Python 的**有意差异**：Python 只在抛异常时判 failed，
        //   「所有账号都失败 / 一台都没建成」（例如密钥文件不存在）会得到
        //   status=done + 成功 0 台 —— 界面上像是「跑完了、没问题」。
        //   这里把「一台都没建成且确实有失败原因」收敛为 failed，让失败不会被
        //   当成成功（条目级结果仍然逐条保留，便于定位是哪个账号/哪台）。
        $allFailed = ($totalOk === 0 && ($totalFail > 0 || $firstErr !== ''));
        $message = sprintf('完成：成功 %d 台，失败 %d 台，耗时 %ss', $totalOk, $totalFail, $elapsed);
        if ($allFailed) {
            // totalFail 为 0 说明失败发生在「账号级」（客户端构造/实例清点），
            // 逐台循环根本没跑起来 —— 这时说「失败 0 台」看着自相矛盾，单独交代。
            $message = $totalFail > 0
                ? sprintf('全部失败：成功 0 台，失败 %d 台，耗时 %ss', $totalFail, $elapsed)
                : sprintf('全部失败：成功 0 台（%d 个账号在准备阶段就失败），耗时 %ss', count($results), $elapsed);
            if ($firstErr !== '') {
                $message .= '；原因：' . mb_substr($firstErr, 0, 300);
            }
        }
        $log(sprintf('=== 任务 %s 结束：成功 %d / 失败 %d ===', $taskId, $totalOk, $totalFail),
            $allFailed ? 'warn' : 'success', $taskId);

        return ['ok' => !$allFailed, 'message' => $message, 'result' => $summary];
    }

    /**
     * 单账号创建 —— 对应 Python _create_for_account。
     * 返回 ['account','account_id','project_id','created','failed','instances',(error)]。
     */
    private static function create_for_account(string $taskId, array $acc, int $count, array $spec,
        array $rawSpec, int $retries, string $loginMode, string $sshPublicKey, string $rootPasswordIn,
        string $postCommand, string $verifyCommand, array $installs, array $payload, callable $log): array
    {
        $email = trim((string) ($acc['email'] ?? ''));
        $label = $email !== '' ? $email : (string) ($acc['project_id'] ?? '');
        $result = [
            'account'    => $label,
            'account_id' => $acc['id'],
            'project_id' => (string) ($acc['project_id'] ?? ''),
            'created'    => 0,
            'failed'     => 0,
            'instances'  => [],
        ];

        try {
            $gcp = self::account_client($acc);
        } catch (Throwable $e) {
            $log(sprintf('[%s] 初始化 GCP 客户端失败：%s', $label, $e->getMessage()), 'error', $taskId);
            $result['error'] = $e->getMessage();
            return $result;
        }

        // SSH 密钥模式：公钥写进**项目级**元数据（与 Python 一致，不是实例级）
        if ($loginMode === 'ssh_key' && $sshPublicKey !== '') {
            [$ok, $msg] = $gcp->add_ssh_key($sshPublicKey, 'root');
            $log(sprintf('[%s] SSH 公钥注入%s', $label, $ok ? '成功' : ('失败：' . $msg)),
                $ok ? 'success' : 'warn', $taskId);
        }

        [$pool, $maxPerRegion, $single] = self::resolve_region_pool($rawSpec);
        $regionCount = array_fill_keys($pool, 0);
        try {
            $existing = $gcp->list_instances();
            foreach ($existing as $inst) {
                $region = Catalog::region_of_zone((string) ($inst['zone'] ?? ''));
                if (array_key_exists($region, $regionCount)) {
                    $regionCount[$region]++;
                }
            }
        } catch (Throwable $e) {
            // 清点失败就不能做配额规划 → 该账号整批放弃（与 Python 一致），
            // 但不影响其它账号，也不把任务留在 running。
            $log(sprintf('[%s] 实例清点失败：%s', $label, $e->getMessage()), 'error', $taskId);
            $result['error'] = $e->getMessage();
            return $result;
        }
        $log(sprintf('[%s] 现有实例区域分布：%s', $label, (string) json_encode($regionCount, JSON_UNESCAPED_UNICODE)),
            'info', $taskId);

        $createdCount = 0;
        for ($idx = 1; $idx <= $count; $idx++) {
            if (Tasks::cancelRequested($taskId)) {
                $log(sprintf('[%s] 检测到取消请求，停止后续创建', $label), 'warn', $taskId);
                break;
            }
            $item = self::create_one($taskId, $gcp, $acc, $idx, $spec, $payload, $pool, $maxPerRegion,
                $single, $regionCount, $retries, $loginMode, $rootPasswordIn, $postCommand,
                $verifyCommand, $installs, $label, $createdCount, $log);
            $result['instances'][] = $item;
            if (empty($item['ok'])) {
                $result['failed']++;
            }
        }
        $result['created'] = $createdCount;
        $log(sprintf('[%s] 小结：成功 %d 台 / 失败 %d 台', $label, $result['created'], $result['failed']),
            'info', $taskId);
        return $result;
    }

    /**
     * 创建单台（含换区重试与创建后 SSH 阶段）—— 对应 Python run_one。
     */
    private static function create_one(string $taskId, Gcp $gcp, array $acc, int $idx, array $spec,
        array $payload, array $pool, int $maxPerRegion, bool $single, array &$regionCount, int $retries,
        string $loginMode, string $rootPasswordIn, string $postCommand, string $verifyCommand,
        array $installs, string $label, int &$createdCount, callable $log): array
    {
        // 1) 预留区域配额
        [$reserved, $candidates] = self::reserve_region($pool, $maxPerRegion, $single, $spec, $regionCount);
        if ($reserved === null) {
            return ['ok' => false, 'error' => sprintf('所有可用区域配额已满（每区上限 %d）', $maxPerRegion)];
        }

        $name = sprintf('vm-%s-%d-%d-%d', (string) $acc['id'], time() % 100000, $idx, random_int(1000, 9999));

        $rootPassword = '';
        $startupScript = '';
        if ($loginMode === 'root_password') {
            // 用户填了就用用户的，否则随机生成（Python：root_password_in or _rand_password()）
            $rootPassword = $rootPasswordIn !== '' ? $rootPasswordIn : Ssh::randPassword(16);
            $startupScript = Ssh::buildRootStartupScript($rootPassword);
        }

        // 2) 首轮：预留区优先，其余可用区按池顺序兜底
        //    ★ Python 里写的是 random.shuffle(ordered_regions[1:])，但切片是副本，
        //      洗牌结果被丢弃 —— 真实顺序就是「预留区 + 池顺序」。这里保持一致。
        $tried = [];
        $ordered = array_merge([$reserved], array_values(array_filter(
            $candidates,
            static fn($r): bool => $r !== $reserved
        )));
        $lastErr = '';
        $res = null;
        foreach ($ordered as $region) {
            $zoneList = [];
            foreach (self::zones_for_region($gcp, (string) $region) as $z) {
                if (!isset($tried[$z])) {
                    $zoneList[] = $z;
                }
            }
            shuffle($zoneList);   // 同 Python：同一区内打乱，避免总撞同一个可用区
            foreach ($zoneList as $zone) {
                if (Tasks::cancelRequested($taskId)) {
                    self::release_region($regionCount, (string) $reserved);
                    return ['ok' => false, 'error' => '已取消'];
                }
                $attemptSpec = $spec;
                $attemptSpec['region'] = (string) $region;
                [$ok, $out] = $gcp->create_instance((string) $zone, $name, $startupScript, $attemptSpec);
                if ($ok) {
                    $res = $out;
                    break;
                }
                $lastErr = is_string($out) ? $out : (string) json_encode($out, JSON_UNESCAPED_UNICODE);
                $tried[$zone] = true;
                if (!self::is_resource_exhausted($lastErr)) {
                    break;   // 非「资源耗尽」类错误换区也没用（配额/权限/参数错）
                }
            }
            if ($res) {
                break;
            }
            if (!self::is_resource_exhausted($lastErr)) {
                break;
            }
        }

        // 3) 换区重试（Python retries 段）
        if ($res === null) {
            self::release_region($regionCount, (string) $reserved);
            for ($attempt = 0; $attempt < $retries; $attempt++) {
                if (Tasks::cancelRequested($taskId)) {
                    break;
                }
                [$r2] = self::reserve_region($pool, $maxPerRegion, $single, $spec, $regionCount);
                if ($r2 === null) {
                    break;
                }
                $z2 = self::pick(self::zones_for_region($gcp, (string) $r2));
                $aSpec = $spec;
                $aSpec['region'] = (string) $r2;
                $log(sprintf('[%s] %s 重试 %d/%d → %s：%s', $label, $name, $attempt + 1, $retries,
                    (string) $z2, $lastErr), 'warn', $taskId);
                if ($z2 === null) {
                    self::release_region($regionCount, (string) $r2);
                    break;
                }
                [$ok, $out] = $gcp->create_instance((string) $z2, $name, $startupScript, $aSpec);
                if ($ok) {
                    $res = $out;
                    break;
                }
                $lastErr = is_string($out) ? $out : (string) json_encode($out, JSON_UNESCAPED_UNICODE);
                self::release_region($regionCount, (string) $r2);
                sleep(3);
            }
        }

        if ($res === null) {
            return ['ok' => false, 'name' => $name, 'error' => $lastErr !== '' ? $lastErr : '创建失败'];
        }

        // 4) 落库 + 日志（创建成功即计数；后续 SSH 阶段失败不改这个计数，与 Python 一致）
        $ip    = (string) ($res['ip'] ?? '');
        $zone  = (string) ($res['zone'] ?? '');
        $actualRegion = Catalog::region_of_zone($zone);
        $item = [
            'ok'           => true,
            'name'         => $name,
            'ip'           => $ip,
            'private_ip'   => (string) ($res['private_ip'] ?? ''),
            'zone'         => $zone,
            'region'       => $actualRegion,
            'machine_type' => (string) $spec['machine_type'],
            'image'        => (string) $spec['image_label'],
            'disk'         => $spec['disk_type'] . ' ' . $spec['disk_size_gb'] . 'GB',
            'stage'        => 'created',
        ];

        $createdCount++;
        $note         = trim((string) ($payload['note'] ?? ''));
        $installsStr  = implode(',', $installs);
        $createdTs    = (float) ($res['created_ts'] ?? 0);
        Store::saveVm(
            $name, $ip,
            $loginMode === 'root_password' ? $rootPassword : '',
            $acc['id'], $zone, (string) $spec['machine_type'], (string) $spec['image_key'],
            (string) $spec['disk_type'], (int) $spec['disk_size_gb'],
            $note, $createdTs > 0 ? $createdTs : null, $installsStr
        );

        // ★★ 绝不能把 root 密码拼进日志。
        //    日志经 GET /api/logs 与 WS /ws/logs 对所有 view 权限用户开放，而 viewer
        //    就是只读角色 —— 一旦写进日志，任何能登录的人（哪怕只读账号、或一个被窃
        //    的会话）一次请求就能拿走全部机器的 root 密码，把
        //    POST /api/instances/password 那道「重新输入登录密码」的二次验证彻底绕过。
        //    密码本身已由上面的 Store::saveVm 落库，前端点「显示密码」走二次验证查看。
        //    （Python 侧 core/tasks.py 已同步改为不打印明文，两版一致。）
        $log(sprintf('[%s] ✅ %s 创建成功 | %s(%s) | IP %s | %s | %s%s',
            $label, $name, $actualRegion, $zone, $ip, $spec['machine_type'], $spec['image_label'],
            $rootPassword !== '' ? ' | Root密码已记录（在实例列表点「显示密码」查看，需二次验证）' : ''),
            'success', $taskId);

        // 5) 创建后 SSH 阶段 —— 对应 Python 的 post_command / verify_command 段
        if ($postCommand === '' && $verifyCommand === '') {
            return $item;
        }
        $user = $loginMode === 'root_password' ? 'root' : (string) ($spec['image_user'] ?? 'ubuntu');
        $pwd  = $rootPassword;
        $intOr = static function ($v, int $d): int {
            $i = (int) ($v === null ? 0 : $v);
            return $i > 0 ? $i : $d;
        };
        if ($ip === '') {
            $item['ok'] = false;
            $item['stage'] = 'ssh';
            $item['error'] = '实例无公网 IP，无法 SSH';
            return $item;
        }
        $avail = Ssh::available();
        if (!$avail['ok']) {
            // 本机缺 ssh/sshpass 时不要说成「SSH 不可用」—— 那是环境问题，不是实例问题
            $item['ok'] = false;
            $item['stage'] = 'ssh';
            $item['ssh_ok'] = false;
            $item['error'] = '本机 SSH 执行能力不可用：' . $avail['reason'];
            $log(sprintf('[%s] ❌ %s %s', $label, $name, $item['error']), 'error', $taskId);
            return $item;
        }

        $log(sprintf('[%s] %s 等待 SSH 就绪（%s@%s）…', $label, $name, $user, $ip), 'info', $taskId);
        $sshOpts = [
            'timeout'      => $intOr($payload['ssh_timeout'] ?? null, 300),
            'stop'         => static fn(): bool => Tasks::cancelRequested($taskId),
            'log_callback' => static function (string $chunk) use ($log, $taskId, $label, $name): void {
                $log(sprintf('[%s][%s] %s', $label, $name, rtrim($chunk)), 'info', $taskId);
            },
        ];
        $wr = Ssh::waitReady($ip, $user, $pwd, $sshOpts);
        $item['ssh_ok'] = $wr['ok'];
        if (!$wr['ok']) {
            $item['ok'] = false;
            $item['stage'] = 'ssh';
            $item['error'] = 'SSH 不可用：' . $wr['output'];
            $log(sprintf('[%s] ❌ %s SSH 不可用：%s', $label, $name, $wr['output']), 'error', $taskId);
            return $item;
        }
        $log(sprintf('[%s] %s SSH 就绪', $label, $name), 'success', $taskId);

        if ($postCommand !== '') {
            $r = Ssh::run($ip, $user, $pwd, $postCommand, [
                'connect_timeout' => 15,
                'idle_timeout'    => $intOr($payload['idle_timeout'] ?? null, 180),
                'total_timeout'   => $intOr($payload['command_timeout'] ?? null, 1800),
                'stop'            => static fn(): bool => Tasks::cancelRequested($taskId),
                'log_callback'    => static function (string $chunk) use ($log, $taskId, $label, $name): void {
                    $log(sprintf('[%s][%s] %s', $label, $name, rtrim($chunk)), 'info', $taskId);
                },
            ]);
            $item['post_command_ok']   = $r['ok'];
            $item['post_command_tail'] = mb_substr($r['output'], -4000);
            // 不传 note/created_at/installs —— Store::saveVm 会保留原值，
            // 否则第二次保存会把用户填的备注/安装项冲空（Python 注释里同款说明）。
            Store::saveVm($name, $ip, $loginMode === 'root_password' ? $rootPassword : '',
                $acc['id'], $zone, (string) $spec['machine_type'], (string) $spec['image_key'],
                (string) $spec['disk_type'], (int) $spec['disk_size_gb']);
            if (!$r['ok']) {
                $item['ok'] = false;
                $item['stage'] = 'command';
                $log(sprintf('[%s] ⚠️ %s 安装命令执行失败', $label, $name), 'error', $taskId);
            } else {
                $item['stage'] = 'installed';
                $log(sprintf('[%s] ✅ %s 安装命令执行完成', $label, $name), 'success', $taskId);
            }
        }

        if ($verifyCommand !== '') {
            $r = Ssh::run($ip, $user, $pwd, $verifyCommand, [
                'connect_timeout' => 15,
                'idle_timeout'    => 60,
                'total_timeout'   => $intOr($payload['verify_timeout'] ?? null, 180),
                'stop'            => static fn(): bool => Tasks::cancelRequested($taskId),
            ]);
            $item['verify_ok']     = $r['ok'];
            $item['verify_output'] = mb_substr($r['output'], -2000);
            $log(sprintf('[%s] 验证命令 %s：%s', $label, $r['ok'] ? '通过' : '未通过',
                mb_substr($r['output'], -300)), $r['ok'] ? 'success' : 'warn', $taskId);
            if (!$r['ok']) {
                $item['ok'] = false;
                $item['stage'] = 'verify';
            } else {
                $item['stage'] = 'done';
            }
        }
        return $item;
    }

    /**
     * 在项目里定位实例的真实 zone —— 对应 Python locate()。
     * 返回 [zone, 最近一次错误]；zone 为空串表示没找到。
     *
     * 这里与 Python 一样**吞掉**查询异常（凭证失效、瞬时网络错误不该被误报成
     * 「实例不存在」）；区别是 PHP 把原因回传给调用方写进日志，Python 直接丢弃，
     * 结果就是用户只看到一句「未找到」而不知道是密钥失效 —— 这里补上可读原因。
     */
    private static function locate_instance(Gcp $gcp, string $name, string $zoneHint): array
    {
        $zoneHint = trim($zoneHint);
        if ($zoneHint !== '') {
            return [$zoneHint, ''];
        }
        try {
            foreach ($gcp->list_instances() as $inst) {
                if ((string) ($inst['name'] ?? '') === $name) {
                    return [(string) ($inst['zone'] ?? ''), ''];
                }
            }
        } catch (Throwable $e) {
            return ['', $e->getMessage()];
        }
        return ['', ''];
    }

    /**
     * 【任务编排】实例批量操作（start / stop / reset / delete）
     *   = Python submit_instance_action + _run_action + _do_action + locate。
     *
     * ★ delete 的语义对齐 Python：只做「把本地 vm_passwords 记录也删掉」，
     *   **不加额外确认**（确认由前端弹框完成）；也**不漏** Python 的保护 ——
     *   逐个账号按名字定位、找不到就明确报错而不是静默跳过。
     *
     * @param string[] $targets 实例名列表（也兼容 [{name:...}] 形态）
     * @return array{ok:bool,message:string,result:array}
     */
    public static function runInstanceAction(string $action, array $targets, string $taskId, callable $log): array
    {
        if (!in_array($action, ['start', 'stop', 'reset', 'delete'], true)) {
            return ['ok' => false, 'message' => '不支持的操作：' . $action, 'result' => []];
        }
        $names = [];
        foreach ($targets as $t) {
            if (is_array($t)) {
                $t = $t['name'] ?? '';
            }
            $t = trim((string) $t);
            if ($t !== '') {
                $names[] = $t;
            }
        }
        if ($names === []) {
            return ['ok' => false, 'message' => '没有可操作的实例', 'result' => []];
        }

        $log(sprintf('%s %d 台实例', $action, count($names)), 'info', $taskId);

        $accountById = [];
        foreach (Store::getAccounts() as $a) {
            $accountById[(string) $a['id']] = $a;
        }
        $vmByName = [];
        foreach (Store::getAllVms() as $v) {
            $vmByName[(string) $v['name']] = $v;
        }

        $results = [];
        $byAccount = [];
        $unresolved = [];
        // 先按本地记录归组（有 account_id 且账号还在）
        foreach ($names as $nm) {
            $vm = $vmByName[$nm] ?? [];
            $accId = (string) ($vm['account_id'] ?? '');
            if ($accId !== '' && isset($accountById[$accId])) {
                $byAccount[$accId][] = ['name' => $nm, 'zone' => (string) ($vm['zone'] ?? '')];
            } else {
                $unresolved[] = ['name' => $nm, 'zone' => (string) ($vm['zone'] ?? '')];
            }
        }

        // 本地没有记录的实例（预存在的、或用原版桌面工具建的）不能直接判
        // 「未找到所属账号」—— 界面能列出它，就该能操作它（Python 侧修过的真实缺陷）。
        if ($unresolved !== [] && $accountById !== []) {
            foreach ($unresolved as $item) {
                $placed = false;
                $locErr = '';
                foreach ($accountById as $acc) {
                    try {
                        $gcp = self::account_client($acc);
                    } catch (Throwable $e) {
                        $locErr = $locErr !== '' ? $locErr : $e->getMessage();
                        continue;
                    }
                    [$zone, $err] = self::locate_instance($gcp, (string) $item['name'], (string) $item['zone']);
                    if ($err !== '' && $locErr === '') {
                        $locErr = $err;
                    }
                    if ($zone !== '') {
                        $byAccount[(string) $acc['id']][] = ['name' => $item['name'], 'zone' => $zone];
                        $placed = true;
                        break;
                    }
                }
                if (!$placed) {
                    $results[] = ['name' => $item['name'], 'ok' => false,
                        'error' => '未在任何已配置账号的项目中找到该实例（可能所属账号已删除或密钥已失效）'];
                    if ($locErr !== '') {
                        $log(sprintf('[%s] 定位失败（遍历各账号时的错误）：%s', $item['name'], $locErr), 'error', $taskId);
                    }
                }
            }
        } elseif ($unresolved !== []) {
            foreach ($unresolved as $item) {
                $results[] = ['name' => $item['name'], 'ok' => false, 'error' => '未配置任何账号'];
            }
        }

        foreach ($byAccount as $accId => $items) {
            $acc = $accountById[$accId] ?? null;
            if ($acc === null) {
                foreach ($items as $i) {
                    $results[] = ['name' => $i['name'], 'ok' => false, 'error' => '未找到所属账号'];
                }
                continue;
            }
            try {
                $gcp = self::account_client($acc);
            } catch (Throwable $e) {
                foreach ($items as $i) {
                    $results[] = ['name' => $i['name'], 'ok' => false, 'error' => $e->getMessage()];
                }
                continue;
            }
            $email = (string) ($acc['email'] ?? '');
            foreach ($items as $i) {
                // 取消轮询：Python 的 _do_action 没有取消检查（这里是**有意增强**），
                // 因为 delete 一旦发出请求就可能已经生效，中途取消至少能少删几台。
                if (Tasks::cancelRequested($taskId)) {
                    $log('检测到取消请求，停止后续实例操作', 'warn', $taskId);
                    break 2;
                }
                [$zone, $err] = self::locate_instance($gcp, (string) $i['name'], (string) $i['zone']);
                if ($zone === '') {
                    if ($err !== '') {
                        $log(sprintf('[%s] %s 定位失败：%s', $email, $i['name'], $err), 'error', $taskId);
                    }
                    $results[] = ['name' => $i['name'], 'ok' => false, 'error' => '未知 zone'];
                    continue;
                }
                try {
                    [$ok, $msg] = match ($action) {
                        'start'  => $gcp->start_instance($zone, (string) $i['name']),
                        'stop'   => $gcp->stop_instance($zone, (string) $i['name']),
                        'reset'  => $gcp->reset_instance($zone, (string) $i['name']),
                        default  => $gcp->delete_instance($zone, (string) $i['name']),
                    };
                } catch (Throwable $e) {
                    // 单台异常不影响其余（与 Python 的「逐台独立」原则一致）
                    $ok = false;
                    $msg = $e->getMessage();
                }
                $log(sprintf('[%s] %s %s：%s', $email, $action, $i['name'], $msg),
                    $ok ? 'success' : 'error', $taskId);
                $results[] = ['name' => $i['name'], 'ok' => $ok, 'message' => $msg];
                if ($ok && $action === 'delete') {
                    // 删除成功才清本地密码记录（Python 同：只删 vm_passwords 行）
                    Store::forgetVm((string) $i['name']);
                }
            }
        }

        $okCount = 0;
        $firstErr = '';
        foreach ($results as $r) {
            if (!empty($r['ok'])) {
                $okCount++;
            } elseif ($firstErr === '') {
                $firstErr = (string) ($r['error'] ?? $r['message'] ?? '');
            }
        }
        $summary = ['action' => $action, 'results' => $results];

        // ★ 有意差异：Python 哪怕 0 成功也判 done（界面看不出失败）。
        //   这里「一台都没成功」收敛为 failed，并带上首个可读原因。
        $allFailed = ($okCount === 0);
        $message = sprintf('%s：%d/%d 成功', $action, $okCount, count($results));
        if ($allFailed) {
            $message = '全部失败：' . $message;
            if ($firstErr !== '') {
                $message .= '；原因：' . mb_substr($firstErr, 0, 300);
            }
        }
        return ['ok' => !$allFailed, 'message' => $message, 'result' => $summary];
    }

    /**
     * 【任务编排】刷新各账号实例（并回写实例计数缓存）
     *   = Python submit_refresh + _refresh_inner。
     *
     * 与 Python 的差异：Python 的计数缓存在进程内存里，PHP 是多进程，所以
     * 计数缓存落 SQLite settings（键 inst_counts_cache，正是 ApiGcp::listAccounts
     * 读取的那个）—— 这样 POST /api/refresh 之后 GET /api/accounts 里的
     * inst_count_live 才会真的变新，而不是两个接口各说各话。
     *
     * @return array{ok:bool,message:string,result:array}
     */
    public static function runRefreshTask(array $payload, string $taskId, callable $log): array
    {
        $log('刷新中', 'info', $taskId);
        $accounts = Store::getAccounts();
        $wanted = [];
        foreach ((array) ($payload['account_ids'] ?? []) as $w) {
            $wanted[] = (string) $w;
        }
        if ($wanted !== []) {
            $accounts = array_values(array_filter(
                $accounts,
                static fn(array $a): bool => in_array((string) $a['id'], $wanted, true)
            ));
        }

        // 本地记录计数（无需网络），与 ApiGcp::accountInstanceCounts 口径一致
        $localCnt = [];
        foreach (Store::getAllVms() as $vm) {
            $k = (string) ($vm['account_id'] ?? '');
            if ($k !== '') {
                $localCnt[$k] = ($localCnt[$k] ?? 0) + 1;
            }
        }

        $all = [];
        $counts = [];
        $errors = [];
        foreach ($accounts as $acc) {
            if (Tasks::cancelRequested($taskId)) {
                $log('检测到取消请求，停止后续账号刷新', 'warn', $taskId);
                break;
            }
            $aid = (string) $acc['id'];
            $email = (string) ($acc['email'] ?? '');
            $row = [
                'account_id'       => (int) $acc['id'],
                'email'            => $email,
                'label'            => (string) ($acc['label'] ?? ''),
                'inst_count_live'  => null,
                'inst_count_local' => $localCnt[$aid] ?? 0,
            ];
            try {
                $gcp = self::account_client($acc);
                $insts = $gcp->list_instances();
                foreach ($insts as $inst) {
                    $inst['account_email'] = $email;
                    $inst['account_id']    = $acc['id'];
                    $all[] = $inst;
                }
                $row['inst_count_live'] = count($insts);
                $log(sprintf('刷新 %s：%d 台实例', $email, count($insts)), 'info', $taskId);
            } catch (Throwable $e) {
                // 单账号失败只记错误，不影响其它账号（凭证失效/网络不通很常见）
                $errors[] = ['account_id' => (int) $acc['id'], 'email' => $email, 'error' => $e->getMessage()];
                $log(sprintf('刷新失败 %s：%s', $email, $e->getMessage()), 'error', $taskId);
            }
            $counts[] = $row;
        }

        // 回写计数缓存（供 /api/accounts 复用同一份数字）
        try {
            Store::setSetting('inst_counts_cache', ['counts' => $counts, 'at' => microtime(true)]);
        } catch (Throwable $e) {
            $log('写入实例计数缓存失败：' . $e->getMessage(), 'warn', $taskId);
        }

        $message = sprintf('共 %d 台实例', count($all));
        if ($errors !== []) {
            $message .= sprintf('，%d 个账号刷新失败', count($errors));
        }
        // ★ 有意差异：全部账号都刷新失败时判 failed（Python 恒定 done），
        //   否则「密钥全失效」会显示成一次正常刷新。
        $allFailed = ($errors !== [] && $all === []);
        if ($allFailed) {
            $message = '刷新失败：' . $message . '；原因：' . mb_substr((string) $errors[0]['error'], 0, 300);
        }
        return [
            'ok'     => !$allFailed,
            'message' => $message,
            'result'  => ['instances' => $all, 'errors' => $errors, 'counts' => $counts],
        ];
    }

    // ---- 静态快捷方法（供 index.php 直接使用）----
    public static function maskProxy(?string $p): string { return self::mask_proxy($p); }
    public static function parseProxyInput(?string $p, string $t = 'HTTPS'): array { return self::parse_proxy_input($p, $t); }
    public static function testProxy(?string $p, string $t = 'HTTPS', int $to = 12, string $url = self::PROXY_TEST_URL): array { return self::test_proxy($p, $t, $to, $url); }
    public static function buildInstanceSpec(?array $s): array { return self::build_instance_spec($s); }
}
