<?php
/**
 * bootstrap —— 类自动加载 + 运行环境固化
 *
 * 设计取舍：
 *   · 不引入 composer，用 spl_autoload_register 按「类名 = 文件名」加载 src/ 下的类。
 *     类名必须与文件名完全一致（Config.php → Config），便于审计时一一对应。
 *   · 这里统一关掉「把错误显示给客户端」：生产环境绝不能把路径/堆栈吐给未认证用户。
 *     错误只进 error_log；HTTP 错误由 Json::fail 统一转成 500。
 *   · 时区固定为 UTC —— Python 版所有时间戳都是 time.time()（UTC epoch），
 *     展示层的本地化交给前端 fmtTime，后端不参与。
 */

declare(strict_types=1);

if (defined('GCPWEB_BOOTSTRAPPED')) {
    return;
}
define('GCPWEB_BOOTSTRAPPED', true);

// 生产口径：不显示、只记录
ini_set('display_errors', '0');
ini_set('log_errors', '1');
ini_set('expose_php', '0');
error_reporting(E_ALL);
date_default_timezone_set('UTC');

spl_autoload_register(static function (string $class): void {
    // 只处理本项目类：字母数字下划线，杜绝路径穿越
    if (!preg_match('/^[A-Za-z_][A-Za-z0-9_]*$/', $class)) {
        return;
    }
    $f = __DIR__ . '/' . $class . '.php';
    if (is_file($f)) {
        require_once $f;
    }
});

// CLI（task-runner / ws-server）没有 HTTP 上下文，不能调 http_response_code
if (PHP_SAPI !== 'cli') {
    Http::securityHeaders();
}
