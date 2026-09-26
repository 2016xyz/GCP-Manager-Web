<?php
/**
 * router.php —— PHP 内置服务器（php -S）的路由脚本
 *
 * 为什么需要它：
 *   `php -S 127.0.0.1:8000 -t public` 只会把「存在的文件」交给静态处理，
 *   像 /login、/api/status 这种没有对应文件的路径会直接 404，
 *   而本项目的路由全靠 index.php 解析。
 *   这个脚本的作用就是：真实存在的静态文件让内置服务器自己处理（返回 false），
 *   其余一律交给 index.php。
 *
 * 生产环境不要用内置服务器（它是单进程的，并发能力很弱）——
 *   裸机/VPS 用 nginx + php-fpm，宝塔用 bt/nginx-rewrite.conf。
 *   本文件只服务于「跑起来看看」和本地验证。
 *
 * 用法：php -S 127.0.0.1:8000 -t public public/router.php
 */

declare(strict_types=1);

$path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH);
$path = is_string($path) ? rawurldecode($path) : '/';
$path = preg_replace('#/+#', '/', $path);

// 静态资源：存在就交给内置服务器（它自己会处理 MIME 与 304）
if ($path !== '/' && strpos($path, "\0") === false) {
    $abs = realpath(__DIR__ . $path);
    // 必须落在 public 目录内，防止 ../../ 读到 src/、data/
    if ($abs !== false
        && strncmp($abs, __DIR__ . DIRECTORY_SEPARATOR, strlen(__DIR__) + 1) === 0
        && is_file($abs)
        && !preg_match('#\.(php|db|db-wal|db-shm|sqlite|pem|key|ini|sh|md|log)$#i', $abs)
    ) {
        return false;
    }
}

require __DIR__ . '/index.php';
