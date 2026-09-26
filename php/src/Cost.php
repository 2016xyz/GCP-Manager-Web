<?php
/**
 * Cost —— 费用估算（单台实例费用 + Always Free 免费额度口径）
 *
 * 逐条对照 core/catalog.py 的 instance_cost / free_tier_reason /
 * free_tier_hours_in_month / estimate_monthly_cost 移植。
 *
 * 关键口径（与 Python 版逐位一致，会被测试逐字段比对）：
 *   · 停机（TERMINATED/STOPPED/STOPPING/SUSPENDED）**只算磁盘费**，不收算力费
 *     —— 这是 GCP 的真实规则，不是估算偷懒。
 *   · 抢占式 ×0.2、Spot ×0.35。
 *   · 月计费小时数固定 730（estimate_monthly_cost），日 = ×24。
 *   · 舍入：hourly/compute/disk 6 位；daily 4 位；monthly 2 位；used_usd 4 位；
 *     used_hours 2 位。
 *   · 没有 created_at 时 used_usd / used_hours 返回 null（不拿当前时间冒充 0）。
 *   · 免费额度是「按时间合并计算」：当月总小时数（30天=720/31天=744/28天=672）
 *     即额度上限，不是「前 1 台免费」。
 *
 * 单价来自 Catalog（us-central1 按需参考价 × 区域系数），真实计费以账单为准。
 */

declare(strict_types=1);

final class Cost
{
    /**
     * 当月总小时数（30 天=720 / 31 天=744 / 28 天=672），即免费额度上限。
     * $now 传 null 时取当前时间（UTC）。
     */
    public static function free_tier_hours_in_month($now = null): int
    {
        $ts = $now === null ? time() : (int) $now;
        $days = (int) date('t', $ts); // 当月天数
        return $days * 24;
    }

    /**
     * 判断**规格**是否落在 Always Free 额度内，返回 [是否落在额度内, 说明文字]。
     *
     * 判定条件（任一不满足即按量计费）：
     *   1. 机型为 e2-micro
     *   2. 区域属于 us-west1 / us-central1 / us-east1
     *   3. 不是抢占式 / Spot
     *   4. 磁盘为 pd-standard 且 ≤ 30GB
     */
    public static function free_tier_reason(
        ?string $machineType,
        ?string $region,
        bool $preemptible = false,
        ?string $diskType = null,
        $diskSizeGb = null
    ): array {
        $mt = trim((string) $machineType);
        $rg = Catalog::region_of_zone($region);
        $dt = trim((string) $diskType);
        $size = (float) ($diskSizeGb ?? 0);

        $problems = [];
        if ($mt !== 'e2-micro') {
            $problems[] = '机型 ' . ($mt !== '' ? $mt : '未知') . ' 不是 e2-micro';
        }
        if (!in_array($rg, Catalog::FREE_TIER_REGIONS, true)) {
            $problems[] = '区域 ' . ($rg !== '' ? $rg : '未知') . ' 不在 ' . implode('/', Catalog::FREE_TIER_REGIONS);
        }
        if ($preemptible) {
            $problems[] = '抢占式/Spot 实例不适用免费额度';
        }
        if ($dt !== '' && $dt !== 'pd-standard') {
            $problems[] = "磁盘类型 {$dt} 不是 pd-standard";
        }
        if ($size > 0 && $size > 30) {
            $problems[] = '磁盘 ' . self::g($size) . 'GB 超过免费额度 30GB';
        }

        if ($problems) {
            return [false, '不免费：' . implode('；', $problems)];
        }
        return [true, '规格落在 Always Free 额度内：e2-micro + 免费区域 + ≤30GB 标准盘。'
            . '额度按时间合并计算（当月总小时数），多台同时运行会超额度'];
    }

    /**
     * 计算单台实例的费用（美元）。返回字段与 Python 版逐一对齐。
     *
     * $createdAt / $now 为 Unix 时间戳（float 或 null）。
     */
    public static function instance_cost(
        ?string $machineType,
        ?string $diskType,
        $diskSizeGb,
        ?string $region,
        $createdAt = null,
        $now = null,
        bool $preemptible = false,
        bool $spot = false,
        ?string $status = 'RUNNING'
    ): array {
        $mt = Catalog::MACHINE_TYPES[(string) $machineType] ?? [];
        $dt = Catalog::DISK_TYPES[(string) $diskType] ?? [];
        $rg = Catalog::region_of_zone($region);
        $idx = Catalog::REGION_PRICE_INDEX[$rg] ?? 1.0;

        $priced = ($mt !== []) && ($dt !== []);

        $computeHourly = (float) ($mt['hourly_usd'] ?? 0.0) * $idx;
        if ($preemptible) {
            $computeHourly *= 0.2;
        } elseif ($spot) {
            $computeHourly *= 0.35;
        }

        $diskHourly = (float) ($dt['hourly_usd_per_gb'] ?? 0.0) * (float) ($diskSizeGb ?? 0) * $idx;

        // 停机只计磁盘
        $stopped = in_array(strtoupper((string) $status), ['TERMINATED', 'STOPPED', 'STOPPING', 'SUSPENDED'], true);
        $hourly = $stopped ? $diskHourly : ($computeHourly + $diskHourly);

        [$free, $reason] = self::free_tier_reason(
            $machineType,
            $rg,
            $preemptible || $spot,
            $diskType,
            $diskSizeGb
        );

        // 已用费用：没有 created_at（老记录）时给 null，而不是拿当前时间冒充 0
        $usedUsd = null;
        $usedHours = null;
        if ($createdAt !== null && is_numeric($createdAt)) {
            $nowTs = $now !== null ? (float) $now : (float) time();
            $usedHours = max(0.0, ($nowTs - (float) $createdAt) / 3600.0);
            $usedUsd = $usedHours * $hourly;
        }

        return [
            'currency' => 'USD',
            'hourly_usd' => round($hourly, 6),
            'compute_hourly_usd' => round($computeHourly, 6),
            'disk_hourly_usd' => round($diskHourly, 6),
            'daily_usd' => round($hourly * 24, 4),
            'monthly_usd' => round($hourly * 730, 2),
            'used_usd' => $usedUsd === null ? null : round($usedUsd, 4),
            'used_hours' => $usedHours === null ? null : round($usedHours, 2),
            'region_price_index' => $idx,
            'stopped' => $stopped,
            'free_tier' => $free,
            'free_tier_reason' => $reason,
            'free_tier_hours_cap' => $free ? self::free_tier_hours_in_month($now) : null,
            'free_tier_doc' => Catalog::FREE_TIER_DOC,
            'priced' => $priced,
            'disclaimer' => '参考价：us-central1 按需单价 × 区域系数，未计网络流量，实际以 GCP 账单为准',
        ];
    }

    /** 月成本估算（转发 Catalog，保持与 Python catalog.estimate_monthly_cost 同源） */
    public static function estimate_monthly_cost(
        ?string $machineType,
        ?string $diskType,
        $diskSizeGb,
        ?string $region,
        int $hours = 730,
        int $count = 1,
        bool $preemptible = false,
        bool $spot = false
    ): array {
        return Catalog::estimate_monthly_cost($machineType, $diskType, $diskSizeGb, $region, $hours, $count, $preemptible, $spot);
    }

    /** 类似 Python 的 `{size:g}`：去掉多余的小数 0（30.0 → "30"） */
    private static function g(float $v): string
    {
        $s = rtrim(rtrim(sprintf('%.6f', $v), '0'), '.');
        return $s === '' ? '0' : $s;
    }

    // ---- camelCase 别名 ----
    public static function freeTierHoursInMonth($now = null): int { return self::free_tier_hours_in_month($now); }
    public static function freeTierReason(?string $m, ?string $r, bool $p = false, ?string $d = null, $s = null): array
    {
        return self::free_tier_reason($m, $r, $p, $d, $s);
    }
    public static function instanceCost(?string $m, ?string $d, $s, ?string $r, $createdAt = null, $now = null, bool $p = false, bool $sp = false, ?string $st = 'RUNNING'): array
    {
        return self::instance_cost($m, $d, $s, $r, $createdAt, $now, $p, $sp, $st);
    }
}
