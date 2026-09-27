<?php
/**
 * Update —— 检查有没有新版本（走 GitHub API）
 *
 * 背景：控制台的「关于」卡片原本只显示**本地**的 changelog，
 * 没有任何东西去比对远端 —— 也就是说它永远显示"当前版本的一些说明"，
 * 用户根本无从知道有没有新版。这个类补上"去问一次 GitHub"。
 *
 * ★ 设计取舍
 *
 * 1) 两段式取数：先 `GET /repos/{o}/{r}/releases/latest`（正式的 Release，
 *    带发布说明）；404 说明这个仓库只有 tag 没建过 Release，就退回
 *    `GET /repos/{o}/{r}/tags` 取最高版本。两条路都走不通才算失败。
 *
 * 2) 一定要缓存。GitHub 未认证调用是 **每 IP 每小时 60 次**，而这个页面
 *    是用户随手会点的按钮。默认缓存 1 小时；`force` 才穿透。
 *
 * 3) 失败时**不抛异常、不返回空**：把原因说清楚，并且如果手上有旧缓存，
 *    把旧结果一起带上（顺便标注它是旧的）—— 比只说一句"失败"有用。
 *
 * 4) 只做「检查」，不做「自动升级」。升级要动部署目录里的文件，
 *    那不是点个按钮就该干的事；这里只告诉用户有没有新版 + 怎么升。
 */
final class Update
{
    /** 结果缓存多久（秒）。GitHub 未认证限流 60 次/小时，缓存是必须的 */
    public const TTL = 3600;

    private const CACHE_KEY = 'update_check';
    private const UA = 'GCP-Manager-Web-update-check';
    private const API = 'https://api.github.com';

    /**
     * 检查更新。
     *
     * @param bool $force true = 忽略缓存，真去问 GitHub
     * @return array<string,mixed> 见类注释里的字段说明
     */
    public static function check(bool $force = false): array
    {
        $now = time();
        $cached = self::loadCache();

        if (!$force && $cached !== null
            && ($now - (int) ($cached['checked_at'] ?? 0)) < self::TTL) {
            $cached['cached'] = true;
            $cached['age_sec'] = $now - (int) $cached['checked_at'];
            return $cached;
        }

        $res = self::fetch();

        if (!empty($res['ok'])) {
            // 顺便把「本地 changelog 里比当前版本新的条目」一起给出：
            // 用户想知道的不只是"有新版本"，还有"新在哪"
            $res['behind'] = self::behind();
            $res['behind_count'] = count($res['behind']);
            self::saveCache($res);
            return $res;
        }

        // 失败：有旧缓存就把旧结果一并带上（标明是旧的），别只丢一句"失败"
        if ($cached !== null) {
            $res['stale'] = $cached;
            $res['stale_age_sec'] = $now - (int) ($cached['checked_at'] ?? 0);
        }
        return $res;
    }

    /**
     * 真去问一次 GitHub。
     * @return array<string,mixed>
     */
    private static function fetch(): array
    {
        $repo = Version::REPO_NAME;          // owner/name
        $now = time();
        $base = [
            'current'    => Version::VERSION,
            'repo'       => $repo,
            'checked_at' => $now,
            'cached'     => false,
            'upgrade_hint' => 'bash update.sh',
        ];

        // ① 正式 Release（带发布说明）
        $r = self::http(self::API . '/repos/' . $repo . '/releases/latest');
        if ($r['code'] === 200) {
            $d = json_decode($r['body'], true);
            if (is_array($d) && !empty($d['tag_name'])) {
                $latest = self::norm((string) $d['tag_name']);
                $out = $base + [
                    'ok'           => true,
                    'latest'       => $latest,
                    'has_update'   => self::newer($latest, Version::VERSION),
                    'source'       => 'releases',
                    'release_name' => (string) ($d['name'] ?? $d['tag_name']),
                    'release_url'  => (string) ($d['html_url'] ?? self::releasesUrl()),
                    'published_at' => (string) ($d['published_at'] ?? ''),
                    'notes'        => trim((string) ($d['body'] ?? '')),
                    'release_url_all' => self::releasesUrl(),
                ];
                return $out;
            }
        }
        // 403 + 限流要单独说，不然用户以为是网络坏了
        if ($r['code'] === 403 && stripos($r['head'], 'x-ratelimit-remaining: 0') !== false) {
            return $base + [
                'ok'     => false,
                'reason' => 'GitHub API 调用次数已用完（未认证调用每 IP 每小时 60 次）',
                'detail' => 'GitHub API 调用次数已用完（未认证调用每 IP 每小时 60 次）',
                'hint'   => '等一小时再试，或直接打开仓库的 Releases 页看。',
                'release_url' => self::releasesUrl(),
            ];
        }

        // ② 退回 tag 列表（仓库只打 tag、没建 Release 时走这里）
        $r2 = self::http(self::API . '/repos/' . $repo . '/tags?per_page=100');
        if ($r2['code'] === 200) {
            $tags = json_decode($r2['body'], true);
            if (is_array($tags) && $tags) {
                $best = '';
                foreach ($tags as $t) {
                    $n = self::norm((string) ($t['name'] ?? ''));
                    if ($n !== '' && ($best === '' || self::newer($n, $best))) {
                        $best = $n;
                    }
                }
                if ($best !== '') {
                    return $base + [
                        'ok'         => true,
                        'latest'     => $best,
                        'has_update' => self::newer($best, Version::VERSION),
                        'source'     => 'tags',
                        'release_name' => 'v' . $best,
                        'release_url'  => Version::REPO_URL . '/releases/tag/v' . $best,
                        'published_at' => '',
                        // tags 接口没有发布说明 —— 如实留空，不编
                        'notes'      => '',
                        'notes_note' => '这个仓库没有建 GitHub Release，只能从 tag 读出最新版本号，因此拿不到发布说明。',
                        'release_url_all' => self::releasesUrl(),
                    ];
                }
            }
        }

        // ③ 都失败：把两条路各自的失败原因都带上，便于定位
        return $base + [
            'ok'     => false,
            'reason' => self::why($r),
            'detail' => self::why($r),
            'hint'   => '服务器可能无法直连 GitHub API。可以直接打开仓库的 Releases 页手动确认。',
            'tried'  => [
                'releases/latest' => 'HTTP ' . $r['code'] . ($r['err'] !== '' ? (' / ' . $r['err']) : ''),
                'tags'            => 'HTTP ' . $r2['code'] . ($r2['err'] !== '' ? (' / ' . $r2['err']) : ''),
            ],
            'release_url' => self::releasesUrl(),
        ];
    }

    private static function why(array $r): string
    {
        if ($r['err'] !== '') {
            return '无法连接 GitHub API：' . $r['err'];
        }
        if ($r['code'] === 404) {
            return 'GitHub 上找不到这个仓库（404）—— 仓库可能改名、转私有，或 REPO_NAME 配错了';
        }
        return 'GitHub API 返回意外状态：HTTP ' . $r['code'];
    }

    /** 发一个 GET，返回 [code, body, err, head] */
    private static function http(string $url): array
    {
        $head = '';                 // 收 header，不靠切字符串（跟随跳转时会有多段 header）
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CONNECTTIMEOUT => 6,
            CURLOPT_TIMEOUT        => 12,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_MAXREDIRS      => 3,
            CURLOPT_USERAGENT      => self::UA,  // GitHub 强制要求 UA，缺了直接 403
            CURLOPT_HTTPHEADER     => [
                'Accept: application/vnd.github+json',
                'X-GitHub-Api-Version: 2022-11-28',
            ],
            CURLOPT_HEADERFUNCTION => static function ($c, $line) use (&$head) {
                $head .= $line;     // 跟随跳转时会调用多轮，累积即可
                return strlen($line);
            },
        ]);
        $body = curl_exec($ch);
        $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $err = (string) curl_error($ch);
        curl_close($ch);
        if (!is_string($body)) {
            return ['code' => $code, 'body' => '', 'err' => $err !== '' ? $err : '无响应', 'head' => strtolower($head)];
        }
        return ['code' => $code, 'body' => $body, 'err' => $err, 'head' => strtolower($head)];
    }

    /** 去掉 tag 名里的 v 前缀、去空白 */
    public static function norm(string $s): string
    {
        $s = trim($s);
        if ($s !== '' && ($s[0] === 'v' || $s[0] === 'V')) {
            $s = substr($s, 1);
        }
        return trim($s);
    }

    /**
     * $a 是否比 $b 新。用 PHP 内建的 version_compare（1.4.10 > 1.4.9 这类
     * 用法都能正确处理，自己写 split('.') 比数字很容易在这里翻车）。
     *
     * ★ 必须先 norm()：version_compare('v1.5.0', '1.4.7') 会把带 v 的那个
     *   判成"更旧"，而 tag 名天然带 v —— 不移除就会漏报新版本。
     *   Python 侧的 newer() 也是先 norm 再比，两版必须一致。
     */
    public static function newer(string $a, string $b): bool
    {
        $a = self::norm($a);
        $b = self::norm($b);
        if ($a === '' || $b === '') {
            return false;
        }
        return version_compare($a, $b, '>');
    }

    /** 本地 changelog 里比当前版本新的条目（新→旧的顺序保持） */
    public static function behind(): array
    {
        $out = [];
        foreach (Version::changelog() as $e) {
            if (self::newer((string) ($e['version'] ?? ''), Version::VERSION)) {
                $out[] = $e;
            }
        }
        return $out;
    }

    private static function releasesUrl(): string
    {
        return Version::REPO_URL . '/releases';
    }

    private static function loadCache(): ?array
    {
        try {
            $v = Store::getSetting(self::CACHE_KEY, null);
        } catch (Throwable $e) {
            return null;
        }
        return is_array($v) && !empty($v['ok']) ? $v : null;
    }

    private static function saveCache(array $res): void
    {
        try {
            Store::setSetting(self::CACHE_KEY, $res);
        } catch (Throwable $ignored) {
            // 缓存写失败不该让"检查更新"失败
        }
    }
}
