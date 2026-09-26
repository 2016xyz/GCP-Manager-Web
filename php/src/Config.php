<?php
/**
 * Config —— 环境变量、路径与全局常量的唯一来源
 *
 * 与 Python 版对齐的环境变量：
 *   GCPWEB_DATA_DIR          数据目录（默认 php/data）
 *   GCPWEB_TRUSTED_PROXIES   可信代理网段（默认内网段；"-" 表示不信任任何代理头）
 *   GCPWEB_COOKIE_SECURE     1/0 强制 Cookie Secure；不设则按请求协议自动判断
 *   GCPWEB_PORT / GCPWEB_HOST 仅 bin/ws-server.php 使用
 *
 * 设计取舍：
 *   · 路径全部落成绝对路径（realpath 归一化），避免 PHP-FPM 的 cwd 与预期不符。
 *   · 环境变量读取集中在此，其他模块禁止直接读 getenv()/$_ENV，便于审计。
 *   · 不在这里做任何 I/O 副作用（不建目录）——建目录由 bootstrap 显式调用。
 */

declare(strict_types=1);

final class Config
{
    /** @var array<string,string>|null 惰性加载的环境变量快照 */
    private static ?array $env = null;

    /** 默认可信代理网段：与 Python 版一致（回环 + RFC1918 + IPv6 ULA） */
    private const DEFAULT_TRUSTED = [
        '127.0.0.0/8', '::1/128', '10.0.0.0/8', '172.16.0.0/12',
        '192.168.0.0/16', 'fc00::/7',
    ];

    // ------------------------------------------------------------------
    // 环境变量
    // ------------------------------------------------------------------
    private static function loadEnv(): array
    {
        if (self::$env !== null) {
            return self::$env;
        }
        $out = [];
        foreach ($_SERVER as $k => $v) {
            if (is_string($v) && preg_match('/^GCPWEB_/', $k)) {
                $out[$k] = $v;
            }
        }
        // CLI 场景（task-runner / ws-server）下 $_SERVER 不含环境变量
        if (function_exists('getenv')) {
            foreach (['GCPWEB_DATA_DIR', 'GCPWEB_TRUSTED_PROXIES', 'GCPWEB_COOKIE_SECURE',
                      'GCPWEB_HOST', 'GCPWEB_PORT', 'GCPWEB_INITIAL_PASSWORD'] as $k) {
                $v = getenv($k);
                if ($v !== false && !isset($out[$k])) {
                    $out[$k] = (string) $v;
                }
            }
        }
        return self::$env = $out;
    }

    public static function env(string $key, ?string $default = null): ?string
    {
        $e = self::loadEnv();
        $v = $e[$key] ?? null;
        if ($v === null || $v === '') {
            return $default;
        }
        return $v;
    }

    public static function envInt(string $key, int $default): int
    {
        $v = self::env($key);
        if ($v === null || !preg_match('/^-?\d+$/', trim($v))) {
            return $default;
        }
        return (int) trim($v);
    }

    // ------------------------------------------------------------------
    // 路径
    // ------------------------------------------------------------------
    /** php/ 根目录（本文件在 php/src/ 下，故上跳两级） */
    public static function appRoot(): string
    {
        return dirname(__DIR__);
    }

    public static function publicDir(): string
    {
        return self::appRoot() . '/public';
    }

    public static function dataDir(): string
    {
        $d = self::env('GCPWEB_DATA_DIR');
        if ($d === null) {
            return self::appRoot() . '/data';
        }
        // 相对路径按 appRoot 解析，避免受 cwd 影响
        if ($d[0] !== '/') {
            return self::appRoot() . '/' . ltrim($d, './');
        }
        return rtrim($d, '/');
    }

    public static function keysDir(): string
    {
        return self::dataDir() . '/keys';
    }

    public static function dbPath(): string
    {
        return self::dataDir() . '/gcp_php.db';
    }

    /** 确保数据目录存在且权限收紧（首次调用时创建） */
    public static function ensureDataDirs(): void
    {
        foreach ([self::dataDir(), self::keysDir()] as $d) {
            if (!is_dir($d)) {
                mkdir($d, 0700, true);
            }
            @chmod($d, 0700);
        }
    }

    // ------------------------------------------------------------------
    // 安全相关
    // ------------------------------------------------------------------
    /**
     * 可信代理网段列表。
     * 返回 [] 表示**完全不信任**任何代理头（XFF 一律忽略）。
     */
    public static function trustedProxies(): array
    {
        $raw = self::env('GCPWEB_TRUSTED_PROXIES');
        if ($raw === null) {
            return self::DEFAULT_TRUSTED;
        }
        if (trim($raw) === '-') {
            return [];
        }
        $out = [];
        foreach (explode(',', $raw) as $one) {
            $one = trim($one);
            if ($one !== '' && strpos($one, '/') !== false) {
                $out[] = $one;
            } elseif ($one !== '' && filter_var($one, FILTER_VALIDATE_IP)) {
                // 裸 IP 视为 /32 或 /128
                $out[] = $one . (strpos($one, ':') !== false ? '/128' : '/32');
            }
        }
        return $out;
    }

    /** null = 自动按请求协议判断 */
    public static function secureCookie(): ?bool
    {
        $v = self::env('GCPWEB_COOKIE_SECURE');
        if ($v === null) {
            return null;
        }
        $v = strtolower(trim($v));
        if (in_array($v, ['1', 'true', 'yes', 'on'], true)) {
            return true;
        }
        if (in_array($v, ['0', 'false', 'no', 'off'], true)) {
            return false;
        }
        return null;
    }

    public static function version(): string
    {
        return Version::VERSION;
    }
}
