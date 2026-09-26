<?php
/**
 * CaptchaImage —— 验证码图形渲染辅助
 *
 * 为什么单独成文件：Auth.php 负责的是**安全逻辑**（哈希 / 限速 / 会话 / 权限），
 * 图形渲染是纯展示层、与安全无关，拆出来让 Auth.php 保持聚焦。
 *
 * 依赖
 *   · Python 版用 Pillow(PIL) 画 PNG；PHP 侧 **不引入 composer**，用 gd 扩展画 PNG。
 *   · ★ gd 是**必需扩展**（不是可选）：没有 gd 就没有 PNG，而 SVG 降级会把验证码
 *     明文写在响应里（等于没有验证码），所以这里选择直接报错而不是降级。
 *     install-php.sh / bt-install.sh 的必需扩展清单已含 gd。
 *   · 干扰线/噪点用 random_int（契约红线 S8 禁止 rand/mt_rand）；验证码字符本身
 *     的随机性由 Auth::CaptchaStore 用 random_bytes 保证，渲染层只加视觉噪声。
 *   · 所有输出字符经 htmlspecialchars 转义（虽字符集固定为无特殊字符，仍做防护）。
 */

declare(strict_types=1);

final class CaptchaImage
{
    /** 画布尺寸，与 Python 版一致 */
    public const WIDTH  = 132;
    public const HEIGHT = 46;

    /**
     * 渲染为可直接塞进 <img src> 的 data URI。
     *
     * ★ 已移除 SVG 降级路径。原实现是「装了 GD 画 PNG，没装就回退到把字符写成
     *   `<text>` 的 SVG」，而 SVG 会 base64 后直接放进响应 JSON 的 `image` 字段 ——
     *   调用方解开 base64 就能读到验证码，**完全不需要 OCR**。
     *   实测（本机 PHP 8.0 无 GD）：GET /api/auth/captcha 解出的字符与库里的
     *   验证码逐字符相同。也就是说在无 GD 的部署上（宝塔默认 PHP 常常没装 gd），
     *   登录验证码这道防爆破环节等于不存在。
     *
     *   这类「界面看起来有、实际形同虚设」的防护比明摆着没有更危险：它会让
     *   「登录有验证码」这个判断长期为真，从而掩盖真实的爆破风险。
     *
     *   所以现在**明确失败**：要求部署方装上 gd 扩展（install-php.sh / bt-install.sh
     *   已把 gd 列入必需扩展清单），而不是给一个能被脚本直接读出的验证码。
     *
     * @throws RuntimeException 缺少 gd 扩展或绘图失败
     */
    public static function dataUri(string $code): string
    {
        if (!extension_loaded('gd') || !function_exists('imagecreatetruecolor')) {
            throw new RuntimeException(
                '验证码服务不可用：缺少 PHP gd 扩展。请安装（宝塔：软件商店 → PHP 设置 → '
                . '安装扩展 → gd；裸机：apt install php-gd / yum install php-gd），'
                . '然后重启 PHP-FPM。不能用 SVG 降级 —— 那会把验证码明文写在响应里。'
            );
        }
        $png = self::png($code);
        if ($png === null) {
            throw new RuntimeException(
                '验证码服务不可用：gd 扩展存在但绘图失败，请检查 gd 安装是否完整'
            );
        }
        return 'data:image/png;base64,' . base64_encode($png);
    }

    /** GD 可用时画 PNG；失败返回 null（交给 SVG 兜底） */
    private static function png(string $code): ?string
    {
        try {
            $img = imagecreatetruecolor(self::WIDTH, self::HEIGHT);
            if ($img === false) {
                return null;
            }
            $bg = imagecolorallocate($img, 248, 250, 252);
            imagefilledrectangle($img, 0, 0, self::WIDTH, self::HEIGHT, $bg);

            // 低对比背景干扰线 / 噪点，不压字符
            for ($i = 0; $i < 6; $i++) {
                $c = imagecolorallocate($img, random_int(186, 214), random_int(198, 226), random_int(208, 236));
                imageline($img,
                    random_int(0, self::WIDTH), random_int(0, self::HEIGHT),
                    random_int(0, self::WIDTH), random_int(0, self::HEIGHT), $c);
            }
            for ($i = 0; $i < 110; $i++) {
                $c = imagecolorallocate($img, random_int(175, 215), random_int(186, 222), random_int(198, 236));
                imagesetpixel($img, random_int(0, self::WIDTH - 1), random_int(0, self::HEIGHT - 1), $c);
            }

            $chars = mb_str_split($code, 1, 'UTF-8');
            $slot  = self::WIDTH / max(1, count($chars));
            foreach ($chars as $i => $ch) {
                $c = imagecolorallocate($img, random_int(18, 72), random_int(52, 108), random_int(118, 188));
                $x = (int) ($slot * $i + 6);
                $y = random_int(10, 20);
                imagestring($img, 5, $x, $y, $ch, $c);
            }
            // 前景细线压一下顶/底
            $line = imagecolorallocate($img, 158, 182, 212);
            imageline($img, 0, random_int(6, self::HEIGHT - 6), self::WIDTH, random_int(6, self::HEIGHT - 6), $line);

            ob_start();
            imagepng($img);
            $bytes = ob_get_clean();
            imagedestroy($img);
            return is_string($bytes) && $bytes !== '' ? $bytes : null;
        } catch (Throwable $e) {
            return null;
        }
    }

    /** 无 GD 时的降级方案：带干扰的 SVG 文本验证码（与 Python render_svg 同风格） */
    /**
     * ★ 已移除：SVG 降级会把验证码明文写在响应里，等于没有验证码。
     *
     * 原实现把字符渲染成 `<text>` 元素，整个 SVG base64 后放进响应 JSON 的
     * `image` 字段 —— 调用方解 base64 就能读出验证码，不需要 OCR。
     * 实测（无 gd 的 PHP 8.0）：读出的字符与库里验证码逐字符相同。
     *
     * 这类「看起来有、实际形同虚设」的防护比明摆着没有更危险：它让
     * 「登录有验证码」这个判断长期为真，掩盖了真实的爆破风险。
     * 因此整条路径删除，只留这个会报错的桩，防止以后被接回鉴权流程。
     */
    private static function svg(string $code): string
    {
        throw new RuntimeException(
            'SVG 验证码降级已移除（会把验证码明文写在响应里）。请安装 PHP gd 扩展。'
        );
    }}
