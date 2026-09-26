<?php
/**
 * Json —— 统一 JSON 响应
 *
 * 契约要点（前端依赖，改动会导致界面报错）：
 *   · 成功：{"ok":true, ...其余字段}
 *   · 失败：{"detail":"人类可读的失败原因"}（+ 可选 "code"）
 *   · 前端只读 `detail` 显示错误 —— 不是 message/msg/error。
 *
 * 安全要点：
 *   · 绝不把 PHP 异常堆栈/文件路径/行号回给未认证用户（见 Json::fail）。
 *   · 输出前清掉已有输出缓冲，避免前面误 echo 的内容污染 JSON。
 */

declare(strict_types=1);

final class Json
{
    /** 发响应并结束请求（永不返回） */
    public static function out(array $data, int $status = 200): void
    {
        while (ob_get_level() > 0) {
            ob_end_clean();
        }
        if (!headers_sent()) {
            http_response_code($status);
            header('Content-Type: application/json; charset=utf-8');
            header('Cache-Control: no-store, no-cache, must-revalidate');
            header('X-Content-Type-Options: nosniff');
        }
        $flags = JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES;
        $s = json_encode($data, $flags);
        if ($s === false) {
            // 极端情况（非 UTF-8、循环引用）：退化成最小可解析响应，别吐半个 JSON
            $s = '{"ok":false,"detail":"响应序列化失败"}';
            if (!headers_sent()) {
                http_response_code(500);
            }
        }
        echo $s;
        exit;
    }

    public static function ok(array $extra = []): void
    {
        self::out(array_merge(['ok' => true], $extra), 200);
    }

    /**
     * 业务/参数错误。
     *
     * ★ 同时给 `error` 与 `detail` 两个键，值相同。原因：
     *   Python 版有**两种**错误形态 ——
     *     · 中间件/鉴权层: {"ok":false,"error":"...","code":"..."}
     *     · handler 层 HTTPException: {"detail":"..."}
     *   前端两种都读（console.html 里是 `r.error || r.detail`）。
     *   PHP 版统一都带，无论前端走哪条分支都能拿到文案，也不用去猜
     *   「这个接口的错误在哪个键里」。
     *
     * $code 供前端做程序化分支（如 must_change_password / unauthenticated）。
     */
    public static function err(string $detail, int $status = 400, ?string $code = null): void
    {
        $body = [
            'ok'     => false,
            'error'  => $detail,
            'detail' => $detail,
        ];
        if ($code !== null) {
            $body['code'] = $code;
        }
        self::out($body, $status);
    }

    /**
     * 把未预期异常转成 500，**不回显内部细节**（只在服务端日志留痕）。
     * 认证前的错误尤其不能泄漏路径/类名。
     */
    public static function fail(Throwable $e, string $publicDetail = '服务器内部错误'): void
    {
        error_log('[gcpweb] unhandled: ' . get_class($e) . ': ' . $e->getMessage()
            . ' @ ' . $e->getFile() . ':' . $e->getLine());
        self::err($publicDetail, 500);
    }
}
