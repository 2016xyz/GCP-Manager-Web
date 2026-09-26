<?php
/**
 * CaptchaImage —— 验证码图形渲染辅助
 *
 * 为什么单独成文件：Auth.php 负责的是**安全逻辑**（哈希 / 限速 / 会话 / 权限），
 * 图形渲染是纯展示层、与安全无关，拆出来让 Auth.php 保持聚焦。
 *
 * 取舍
 *   · Python 版用 Pillow(PIL) 画 PNG。PHP 侧 **不引入 composer**，若环境装了 GD
 *     就画 PNG，否则回退到 SVG（前端 <img> 对 data:image/svg+xml 一样能显示）。
 *     当前部署环境（PHP 8.0 CLI，无 GD）走 SVG 分支。
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

    /** 渲染为可直接塞进 <img src> 的 data URI（优先 PNG，回退 SVG） */
    public static function dataUri(string $code): string
    {
        if (extension_loaded('gd') && function_exists('imagecreatetruecolor')) {
            $png = self::png($code);
            if ($png !== null) {
                return 'data:image/png;base64,' . base64_encode($png);
            }
        }
        return self::svg($code);
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
    private static function svg(string $code): string
    {
        $parts = '';
        foreach (mb_str_split($code, 1, 'UTF-8') as $i => $ch) {
            $x   = 18 + $i * 28;
            $y   = 32 + random_int(-4, 4);
            $rot = random_int(-18, 18);
            $color = sprintf('rgb(%d,%d,%d)', random_int(20, 75), random_int(55, 110), random_int(120, 190));
            $parts .= '<text x="' . $x . '" y="' . $y . '" font-size="26" font-weight="700" fill="' . $color . '" '
                . 'transform="rotate(' . $rot . ' ' . $x . ' ' . $y . ')" font-family="monospace">'
                . htmlspecialchars($ch, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8') . '</text>';
        }
        $lines = '';
        for ($i = 0; $i < 5; $i++) {
            $lines .= sprintf(
                '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="rgba(148,163,184,.5)" stroke-width="1"/>',
                random_int(0, self::WIDTH), random_int(0, self::HEIGHT),
                random_int(0, self::WIDTH), random_int(0, self::HEIGHT)
            );
        }
        $svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' . self::WIDTH . '" height="' . self::HEIGHT
            . '" viewBox="0 0 ' . self::WIDTH . ' ' . self::HEIGHT . '">'
            . '<rect width="' . self::WIDTH . '" height="' . self::HEIGHT . '" fill="#f8fafc"/>'
            . $lines . $parts . '</svg>';
        return 'data:image/svg+xml;base64,' . base64_encode($svg);
    }
}
