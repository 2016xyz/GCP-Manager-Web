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
        if ($headOnly) {
            // 只取响应头（test_proxy 用：拿到状态码即算通）
            curl_setopt($ch, CURLOPT_NOBODY, true);
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
            return ['status' => 0, 'body' => '', 'error' => 'curl(' . $errno . '): ' . $err];
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
            $s = substr($s, (int) strrpos($s, '/') + 1);
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
        $mt = $mtUrl !== '' ? substr($mtUrl, (int) strrpos($mtUrl, '/') + 1) : '';
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

        // 引导盘：类型 / 容量 / 来源镜像
        $diskType = '';
        $diskSize = 0;
        $imageSrc = '';
        $disks = $inst['disks'] ?? [];
        if (is_array($disks) && isset($disks[0]) && is_array($disks[0])) {
            $boot = $disks[0];
            $diskSize = (int) ($boot['diskSizeGb'] ?? 0);
            $params = $boot['initializeParams'] ?? null;
            if (is_array($params)) {
                $dt = (string) ($params['diskType'] ?? '');
                $diskType = $dt !== '' ? substr($dt, (int) strrpos($dt, '/') + 1) : '';
                $imageSrc = (string) ($params['sourceImage'] ?? '');
                if (!$diskSize) {
                    $diskSize = (int) ($params['diskSizeGb'] ?? 0);
                }
            }
            // 已运行实例的 initializeParams 常为空，退而从 type / source 取
            if ($diskType === '') {
                $t = (string) ($boot['type'] ?? '');
                $diskType = $t !== '' ? substr($t, (int) strrpos($t, '/') + 1) : '';
            }
            if ($imageSrc === '') {
                $imageSrc = (string) ($boot['source'] ?? '');
            }
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
            'image' => $imageSrc !== '' ? substr($imageSrc, (int) strrpos($imageSrc, '/') + 1) : '',
            'image_source' => $imageSrc,
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
            throw new RuntimeException('操作失败：' . $msg);
        }
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

    // ---- 静态快捷方法（供 index.php 直接使用）----
    public static function maskProxy(?string $p): string { return self::mask_proxy($p); }
    public static function parseProxyInput(?string $p, string $t = 'HTTPS'): array { return self::parse_proxy_input($p, $t); }
    public static function testProxy(?string $p, string $t = 'HTTPS', int $to = 12, string $url = self::PROXY_TEST_URL): array { return self::test_proxy($p, $t, $to, $url); }
    public static function buildInstanceSpec(?array $s): array { return self::build_instance_spec($s); }
}
