<?php
/**
 * Inspect —— GCP 资源勘察（只读）
 *
 * 逐条对照 core/inspect.py 移植。
 *
 * ★ 只读：本模块只调用 get / list / aggregatedList，绝不 insert / update /
 *   delete / setIamPolicy。用于「把项目里能看到的都看到」。
 *
 * 设计要点：
 *   · 逐节隔离：每一节独立 try/catch，某一节没权限（服务账号常只有 compute
 *     权限、没有 resourcemanager / serviceusage）不会拖垮整页，只让那一节显示
 *     具体原因。
 *   · 走代理：与 Gcp 共用代理设置。
 *   · 不抛异常：对外方法一律返回 {ok, ms, data|error}。
 *   · 并发缓存加锁：PHP-FPM 是多进程，缓存用 flock 文件锁串行化读改写，
 *     避免并发请求同时打 GCP 造成缓存击穿（Python 版用 threading.Lock）。
 *
 * 注：Python 版用 ThreadPoolExecutor 并发跑各节。PHP 无轻量线程，这里改为
 * 串行执行 —— 结果字段完全一致，只是耗时更长。并发安全靠上面的文件锁保证。
 */

declare(strict_types=1);

final class Inspect
{
    // 通用 API 基址（硬编码，绝不来自请求参数）
    private const COMPUTE_API      = Gcp::COMPUTE_API;
    private const CRM_API          = Gcp::CRM_API;
    private const SERVICEUSAGE_API = Gcp::SERVICEUSAGE_API;
    private const BILLING_API      = Gcp::BILLING_API;
    private const IAM_API          = Gcp::IAM_API;

    private const IMAGE_PROJECTS = ['ubuntu-os-cloud', 'debian-cloud', 'cos-cloud', 'rocky-linux-cloud', 'windows-cloud'];

    private string $keyPath;
    private string $projectId;
    private string $email;
    private string $proxyUrl = '';
    private Gcp $gcp;

    public function __construct(string $keyPath, string $projectId, string $email, string $proxy = '', string $proxyType = 'HTTPS')
    {
        $this->keyPath = $keyPath;
        $this->projectId = $projectId;
        $this->email = $email;
        $parsed = Gcp::parse_proxy_input($proxy, $proxyType);
        $this->proxyUrl = !empty($parsed['ok']) ? (string) ($parsed['proxy_url'] ?? '') : '';
        $this->gcp = new Gcp($keyPath, $projectId, $email, $proxy, $proxyType);
    }

    // ------------------------------------------------------------------
    // 基础：统一执行壳
    // ------------------------------------------------------------------

    private static function brief(?string $msg, int $limit = 300): string
    {
        $s = trim(str_replace(["\n", "\r"], ' ', (string) $msg));
        $s = implode(' ', preg_split('/\s+/', $s) ?: []);
        return mb_substr($s, 0, $limit) . (mb_strlen($s) > $limit ? '…' : '');
    }

    private static function ms(float $t0): int
    {
        return (int) round((microtime(true) - $t0) * 1000);
    }

    /** 统一执行壳：异常转文案（只读，不抛给调用方）。 */
    public function run(callable $fn): array
    {
        $t0 = microtime(true);
        try {
            $data = $fn();
            return ['ok' => true, 'ms' => self::ms($t0), 'data' => $data];
        } catch (Throwable $e) {
            return ['ok' => false, 'ms' => self::ms($t0), 'error' => self::brief($e->getMessage())];
        }
    }

    private function get(string $url, array $params = [], int $timeout = 30): array
    {
        return $this->gcp->rest_get($url, $params, $timeout);
    }

    private function proj(): string
    {
        // projectId 来自已校验的账号记录，进入 URL 前再做一次白名单校验
        return preg_replace('/[^A-Za-z0-9._-]/', '', $this->projectId) ?: $this->projectId;
    }

    // ------------------------------------------------------------------
    // 1. 服务账号本身（本地可判定，不依赖网络）
    // ------------------------------------------------------------------
    public function account(): array
    {
        return $this->run(function (): array {
            $raw = @file_get_contents($this->keyPath);
            if ($raw === false) {
                throw new RuntimeException("服务账号 JSON 不存在：{$this->keyPath}");
            }
            $sa = json_decode($raw, true);
            if (!is_array($sa)) {
                throw new RuntimeException('服务账号 JSON 解析失败');
            }
            $keys = [];
            foreach (['project_id', 'private_key_id', 'client_id', 'client_email', 'type'] as $k) {
                if (!empty($sa[$k])) {
                    $keys[] = $k;
                }
            }
            // 私钥是否可解出（判断 JSON 是否被截断/损坏）
            $keyOk = false;
            $pkey = @openssl_pkey_get_private((string) ($sa['private_key'] ?? ''));
            if ($pkey !== false) {
                $keyOk = true;
            }
            $pkid = (string) ($sa['private_key_id'] ?? '');
            return [
                'client_email' => $sa['client_email'] ?? $this->email,
                'project_id' => $sa['project_id'] ?? $this->projectId,
                'type' => (string) ($sa['type'] ?? ''),
                'private_key_id' => $pkid !== '' ? (mb_substr($pkid, 0, 16) . '…') : '',
                'client_id' => (string) ($sa['client_id'] ?? ''),
                'has_fields' => $keys,
                'private_key_parsable' => $keyOk,
            ];
        });
    }

    // ------------------------------------------------------------------
    // 2. 项目元信息（Cloud Resource Manager）
    // ------------------------------------------------------------------
    public function project(): array
    {
        return $this->run(function (): array {
            $d = $this->get(self::CRM_API . '/projects/' . $this->proj());
            $parent = $d['parent'] ?? [];
            return [
                'projectId' => $d['projectId'] ?? null,
                'projectNumber' => $d['projectNumber'] ?? null,
                'name' => $d['name'] ?? null,
                'state' => $d['state'] ?? null,
                'createTime' => $d['createTime'] ?? null,
                'labels' => $d['labels'] ?? [],
                'parent' => $parent ? ['type' => $parent['type'] ?? null, 'id' => $parent['id'] ?? null] : null,
            ];
        });
    }

    // ------------------------------------------------------------------
    // 3. 已启用的 API（Service Usage）
    // ------------------------------------------------------------------
    public function services(): array
    {
        return $this->run(function (): array {
            $out = [];
            $token = null;
            while (true) {
                $params = ['filter' => 'state:ENABLED', 'pageSize' => 200];
                if ($token) {
                    $params['pageToken'] = $token;
                }
                $d = $this->get(self::SERVICEUSAGE_API . '/projects/' . $this->proj() . '/services', $params);
                foreach (($d['services'] ?? []) as $s) {
                    $cfg = $s['config'] ?? [];
                    $name = (string) ($s['name'] ?? '');
                    $out[] = [
                        'name' => $name !== '' ? substr($name, (int) strrpos($name, '/') + 1) : '',
                        'title' => (string) ($cfg['title'] ?? ''),
                        'state' => (string) ($s['state'] ?? ''),
                    ];
                }
                $token = $d['nextPageToken'] ?? null;
                if (!$token || count($out) > 500) {
                    break;
                }
            }
            usort($out, static fn($a, $b) => strcmp($a['name'], $b['name']));
            return ['count' => count($out), 'items' => $out];
        });
    }

    // ------------------------------------------------------------------
    // 4. 计费（Cloud Billing）
    // ------------------------------------------------------------------
    public function billing(): array
    {
        return $this->run(function (): array {
            $d = $this->get(self::BILLING_API . '/projects/' . $this->proj() . '/billingInfo');
            $acct = (string) ($d['billingAccountName'] ?? '');
            return [
                'billingEnabled' => (bool) ($d['billingEnabled'] ?? false),
                'billingAccountName' => $acct,
                'billingAccountId' => $acct !== '' ? substr($acct, (int) strrpos($acct, '/') + 1) : '',
                'name' => (string) ($d['name'] ?? ''),
            ];
        });
    }

    // ------------------------------------------------------------------
    // 5. 区域（含各区域配额）
    // ------------------------------------------------------------------
    public function regions(bool $withQuotas = true): array
    {
        return $this->run(function () use ($withQuotas): array {
            $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/regions', ['maxResults' => 500], 45);
            $out = [];
            foreach (($d['items'] ?? []) as $r) {
                $zones = is_array($r['zones'] ?? null) ? $r['zones'] : [];
                $item = [
                    'name' => (string) ($r['name'] ?? ''),
                    'status' => (string) ($r['status'] ?? ''),
                    'zones' => array_map('strval', $zones),
                    'zoneCount' => count($zones),
                ];
                if ($withQuotas) {
                    $qs = [];
                    foreach (($r['quotas'] ?? []) as $q) {
                        if (!is_array($q)) {
                            continue;
                        }
                        $limit = $q['limit'] ?? null;
                        $usage = $q['usage'] ?? null;
                        $qs[] = [
                            'metric' => (string) ($q['metric'] ?? ''),
                            'limit' => $limit,
                            'usage' => $usage,
                            'owner' => (string) ($q['owner'] ?? ''),
                            'pct' => ($limit ? round((float) $usage / (float) $limit * 100, 1) : null),
                        ];
                    }
                    usort($qs, static fn($a, $b) => strcmp($a['metric'], $b['metric']));
                    $item['quotas'] = $qs;
                }
                $out[] = $item;
            }
            usort($out, static fn($a, $b) => strcmp($a['name'], $b['name']));
            return $out;
        });
    }

    // ------------------------------------------------------------------
    // 6. 可用区
    // ------------------------------------------------------------------
    public function zones(string $region = ''): array
    {
        return $this->run(function () use ($region): array {
            $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/zones', ['maxResults' => 500], 45);
            $out = [];
            foreach (($d['items'] ?? []) as $z) {
                if (!is_array($z)) {
                    continue;
                }
                $name = (string) ($z['name'] ?? '');
                if ($region !== '' && strpos($name, $region . '-') !== 0) {
                    continue;
                }
                $rurl = (string) ($z['region'] ?? '');
                $out[] = [
                    'name' => $name,
                    'status' => (string) ($z['status'] ?? ''),
                    'region' => $rurl !== '' ? substr($rurl, (int) strrpos($rurl, '/') + 1) : '',
                    // Zone 资源没有 availableMachineTypes 字段，只有 availableCpuPlatforms
                    'cpuPlatforms' => array_map('strval', is_array($z['availableCpuPlatforms'] ?? null) ? $z['availableCpuPlatforms'] : []),
                ];
            }
            usort($out, static fn($a, $b) => strcmp($a['name'], $b['name']));
            return $out;
        });
    }

    // ------------------------------------------------------------------
    // 7. 机器类型（某个 zone 下可用的全部机型）
    // ------------------------------------------------------------------
    public function machine_types(string $zone, int $limit = 400): array
    {
        return $this->run(function () use ($zone, $limit): array {
            $z = ltrim($zone, '/');
            if (strncmp($z, 'zones/', 6) === 0) {
                $z = substr($z, 6);
            }
            $z = preg_replace('/[^A-Za-z0-9._-]/', '', $z) ?: 'us-central1-a';
            $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/zones/' . $z . '/machineTypes', ['maxResults' => 500], 45);
            $out = [];
            foreach (($d['items'] ?? []) as $m) {
                if (!is_array($m)) {
                    continue;
                }
                $acc = [];
                foreach (($m['accelerators'] ?? []) as $a) {
                    $gtype = (string) ($a['guestAcceleratorType'] ?? '');
                    $acc[] = [
                        'type' => $gtype !== '' ? substr($gtype, (int) strrpos($gtype, '/') + 1) : '',
                        'count' => $a['guestAcceleratorCount'] ?? null,
                    ];
                }
                $memMb = (int) ($m['memoryMb'] ?? 0);
                $out[] = [
                    'name' => (string) ($m['name'] ?? ''),
                    'guestCpus' => $m['guestCpus'] ?? null,
                    'memoryMb' => $m['memoryMb'] ?? null,
                    'memoryGb' => round($memMb / 1024, 2),
                    'isSharedCpu' => (bool) ($m['isSharedCpu'] ?? false),
                    'architecture' => (string) ($m['architecture'] ?? ''),
                    'accelerators' => $acc,
                    'maxPds' => $m['maximumPersistentDisks'] ?? null,
                ];
                if (count($out) >= $limit) {
                    break;
                }
            }
            usort($out, static fn($a, $b) => [(int) ($a['guestCpus'] ?? 0), $a['name']] <=> [(int) ($b['guestCpus'] ?? 0), $b['name']]);
            return $out;
        });
    }

    // ------------------------------------------------------------------
    // 8. 公共镜像 / 镜像族
    // ------------------------------------------------------------------
    public function images(?array $projects = null): array
    {
        $projects = $projects ?: self::IMAGE_PROJECTS;
        return $this->run(function () use ($projects): array {
            $out = [];
            foreach ($projects as $p) {
                try {
                    $d = $this->get(self::COMPUTE_API . '/projects/' . $p . '/global/images', [
                        'maxResults' => 200,
                        'filter' => 'deprecated.state != DEPRECATED',
                    ], 45);
                    $fams = [];
                    foreach (($d['items'] ?? []) as $img) {
                        if (!is_array($img)) {
                            continue;
                        }
                        $fams[] = [
                            'family' => (string) ($img['family'] ?? $img['name'] ?? ''),
                            'name' => (string) ($img['name'] ?? ''),
                            'diskSizeGb' => $img['diskSizeGb'] ?? null,
                            'status' => (string) ($img['status'] ?? ''),
                            'creationTimestamp' => (string) ($img['creationTimestamp'] ?? ''),
                        ];
                    }
                    // 同一 family 只留最新一个
                    $latest = [];
                    foreach ($fams as $f) {
                        $cur = $latest[$f['family']] ?? null;
                        if (!$cur || (($f['creationTimestamp'] ?? '') > ($cur['creationTimestamp'] ?? ''))) {
                            $latest[$f['family']] = $f;
                        }
                    }
                    $vals = array_values($latest);
                    usort($vals, static fn($a, $b) => strcmp($a['family'] ?: $a['name'], $b['family'] ?: $b['name']));
                    $out[$p] = $vals;
                } catch (Throwable $e) {
                    $out[$p] = ['error' => self::brief($e->getMessage())];
                }
            }
            return $out;
        });
    }

    // ------------------------------------------------------------------
    // 9. VPC 网络
    // ------------------------------------------------------------------
    public function networks(): array
    {
        return $this->run(function (): array {
            $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/global/networks', ['maxResults' => 500], 45);
            $out = [];
            foreach (($d['items'] ?? []) as $n) {
                if (!is_array($n)) {
                    continue;
                }
                $rc = $n['routingConfig'] ?? [];
                $out[] = [
                    'name' => (string) ($n['name'] ?? ''),
                    'id' => (string) ($n['id'] ?? ''),
                    'autoCreateSubnetworks' => (bool) ($n['autoCreateSubnetworks'] ?? false),
                    'mtu' => $n['mtu'] ?? null,
                    'routingMode' => (string) (($rc['routingMode'] ?? '') ?: ''),
                    'subnetworkCount' => is_array($n['subnetworks'] ?? null) ? count($n['subnetworks']) : 0,
                    'creationTimestamp' => (string) ($n['creationTimestamp'] ?? ''),
                    'description' => (string) ($n['description'] ?? ''),
                ];
            }
            usort($out, static fn($a, $b) => strcmp($a['name'], $b['name']));
            return $out;
        });
    }

    // ------------------------------------------------------------------
    // 10. 子网（aggregated，一次请求拿全部区域）
    // ------------------------------------------------------------------
    public function subnetworks(string $region = ''): array
    {
        return $this->run(function () use ($region): array {
            $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/aggregated/subnetworks', ['maxResults' => 500], 60);
            $out = [];
            foreach (self::agg_iter($d) as [$key, $items]) {
                $rg = $key !== '' ? substr($key, (int) strrpos($key, '/') + 1) : '';
                if ($region !== '' && $rg !== $region) {
                    continue;
                }
                foreach ($items as $s) {
                    if (!is_array($s)) {
                        continue;
                    }
                    $net = (string) ($s['network'] ?? '');
                    $out[] = [
                        'name' => (string) ($s['name'] ?? ''),
                        'region' => $rg,
                        'network' => $net !== '' ? substr($net, (int) strrpos($net, '/') + 1) : '',
                        'ipCidrRange' => (string) ($s['ipCidrRange'] ?? ''),
                        'gatewayAddress' => (string) ($s['gatewayAddress'] ?? ''),
                        'privateIpGoogleAccess' => (bool) ($s['privateIpGoogleAccess'] ?? false),
                        'purpose' => (string) (($s['purpose'] ?? '') ?: 'PRIVATE'),
                        'stackType' => (string) ($s['stackType'] ?? ''),
                        'creationTimestamp' => (string) ($s['creationTimestamp'] ?? ''),
                    ];
                }
            }
            usort($out, static fn($a, $b) => [$a['region'], $a['name']] <=> [$b['region'], $b['name']]);
            return $out;
        });
    }

    // ------------------------------------------------------------------
    // 11. 防火墙规则（含明细）
    // ------------------------------------------------------------------
    public function firewalls(): array
    {
        return $this->run(function (): array {
            $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/global/firewalls', ['maxResults' => 500], 45);
            $out = [];
            foreach (($d['items'] ?? []) as $f) {
                if (!is_array($f)) {
                    continue;
                }
                $net = (string) ($f['network'] ?? '');
                $allowed = [];
                foreach (($f['allowed'] ?? []) as $a) {
                    $allowed[] = ['protocol' => (string) ($a['IPProtocol'] ?? ''), 'ports' => array_map('strval', is_array($a['ports'] ?? null) ? $a['ports'] : [])];
                }
                $denied = [];
                foreach (($f['denied'] ?? []) as $a) {
                    $denied[] = ['protocol' => (string) ($a['IPProtocol'] ?? ''), 'ports' => array_map('strval', is_array($a['ports'] ?? null) ? $a['ports'] : [])];
                }
                $logCfg = $f['logConfig'] ?? null;
                $out[] = [
                    'name' => (string) ($f['name'] ?? ''),
                    'network' => $net !== '' ? substr($net, (int) strrpos($net, '/') + 1) : '',
                    'direction' => (string) (($f['direction'] ?? '') ?: 'INGRESS'),
                    'priority' => $f['priority'] ?? null,
                    'disabled' => (bool) ($f['disabled'] ?? false),
                    'sourceRanges' => array_map('strval', is_array($f['sourceRanges'] ?? null) ? $f['sourceRanges'] : []),
                    'destinationRanges' => array_map('strval', is_array($f['destinationRanges'] ?? null) ? $f['destinationRanges'] : []),
                    'targetTags' => array_map('strval', is_array($f['targetTags'] ?? null) ? $f['targetTags'] : []),
                    'sourceTags' => array_map('strval', is_array($f['sourceTags'] ?? null) ? $f['sourceTags'] : []),
                    'sourceServiceAccounts' => array_map('strval', is_array($f['sourceServiceAccounts'] ?? null) ? $f['sourceServiceAccounts'] : []),
                    'targetServiceAccounts' => array_map('strval', is_array($f['targetServiceAccounts'] ?? null) ? $f['targetServiceAccounts'] : []),
                    'allowed' => $allowed,
                    'denied' => $denied,
                    'logConfigEnabled' => (bool) (is_array($logCfg) ? ($logCfg['enable'] ?? false) : false),
                    'creationTimestamp' => (string) ($f['creationTimestamp'] ?? ''),
                ];
            }
            foreach ($out as &$r) {
                $r['action'] = $r['denied'] ? 'DENY' : 'ALLOW';
            }
            unset($r);
            foreach ($out as &$r) {
                $r['openToWorld'] = self::anyIn($r['sourceRanges'], ['0.0.0.0/0', '::/0'])
                    || self::anyIn($r['destinationRanges'], ['0.0.0.0/0', '::/0']);
            }
            unset($r);
            usort($out, static fn($a, $b) => [(int) ($a['priority'] ?? 0), $a['name']] <=> [(int) ($b['priority'] ?? 0), $b['name']]);
            return $out;
        });
    }

    // ------------------------------------------------------------------
    // 12. 磁盘
    // ------------------------------------------------------------------
    public function disks(): array
    {
        return $this->run(function (): array {
            $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/aggregated/disks', ['maxResults' => 500], 60);
            $out = [];
            foreach (self::agg_iter($d) as [$key, $items]) {
                $zone = $key !== '' ? substr($key, (int) strrpos($key, '/') + 1) : '';
                foreach ($items as $disk) {
                    if (!is_array($disk)) {
                        continue;
                    }
                    $type = (string) ($disk['type'] ?? '');
                    $src = (string) ($disk['sourceImage'] ?? '');
                    $users = [];
                    foreach (($disk['users'] ?? []) as $u) {
                        $users[] = substr((string) $u, (int) strrpos((string) $u, '/') + 1);
                    }
                    $out[] = [
                        'name' => (string) ($disk['name'] ?? ''),
                        'zone' => $zone,
                        'sizeGb' => $disk['sizeGb'] ?? null,
                        'type' => $type !== '' ? substr($type, (int) strrpos($type, '/') + 1) : '',
                        'status' => (string) ($disk['status'] ?? ''),
                        'users' => $users,
                        'sourceImage' => $src !== '' ? substr($src, (int) strrpos($src, '/') + 1) : '',
                        'creationTimestamp' => (string) ($disk['creationTimestamp'] ?? ''),
                        'physicalBlockSizeBytes' => $disk['physicalBlockSizeBytes'] ?? null,
                    ];
                }
            }
            foreach ($out as &$disk) {
                $disk['orphan'] = empty($disk['users']);   // 没挂到实例 = 空转计费
            }
            unset($disk);
            usort($out, static fn($a, $b) => [$a['zone'], $a['name']] <=> [$b['zone'], $b['name']]);
            return $out;
        });
    }

    // ------------------------------------------------------------------
    // 13. 快照
    // ------------------------------------------------------------------
    public function snapshots(): array
    {
        return $this->run(function (): array {
            $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/global/snapshots', ['maxResults' => 500], 45);
            $out = [];
            foreach (($d['items'] ?? []) as $s) {
                if (!is_array($s)) {
                    continue;
                }
                $bytes = $s['storageBytes'] ?? null;
                $src = (string) ($s['sourceDisk'] ?? '');
                $out[] = [
                    'name' => (string) ($s['name'] ?? ''),
                    'status' => (string) ($s['status'] ?? ''),
                    'diskSizeGb' => $s['diskSizeGb'] ?? null,
                    'storageBytes' => $bytes,
                    'storageBytesGb' => round(((float) ($bytes ?? 0)) / (1024 ** 3), 3),
                    'sourceDisk' => $src !== '' ? substr($src, (int) strrpos($src, '/') + 1) : '',
                    'sourceDiskId' => $s['sourceDiskId'] ?? null,
                    'creationTimestamp' => (string) ($s['creationTimestamp'] ?? ''),
                ];
            }
            usort($out, static fn($a, $b) => strcmp($a['name'], $b['name']));
            return $out;
        });
    }

    // ------------------------------------------------------------------
    // 14. 静态 IP（区域级 + 全局）
    // ------------------------------------------------------------------
    public function addresses(): array
    {
        return $this->run(function (): array {
            $out = [];
            try {
                $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/aggregated/addresses', ['maxResults' => 500], 60);
                foreach (self::agg_iter($d) as [$key, $items]) {
                    $region = $key !== '' ? substr($key, (int) strrpos($key, '/') + 1) : '';
                    foreach ($items as $a) {
                        if (!is_array($a)) {
                            continue;
                        }
                        $out[] = [
                            'name' => (string) ($a['name'] ?? ''),
                            'address' => (string) ($a['address'] ?? ''),
                            'region' => $region,
                            'scope' => 'regional',
                            'type' => (string) ($a['addressType'] ?? ($a['type'] ?? '')),
                            'status' => (string) ($a['status'] ?? ''),
                            'users' => array_map(static fn($u) => substr((string) $u, (int) strrpos((string) $u, '/') + 1), is_array($a['users'] ?? null) ? $a['users'] : []),
                        ];
                    }
                }
            } catch (Throwable $e) {
                $out[] = ['error' => self::brief($e->getMessage())];
            }
            try {
                $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/global/addresses', ['maxResults' => 500], 45);
                foreach (($d['items'] ?? []) as $a) {
                    if (!is_array($a)) {
                        continue;
                    }
                    $out[] = [
                        'name' => (string) ($a['name'] ?? ''),
                        'address' => (string) ($a['address'] ?? ''),
                        'region' => 'global',
                        'scope' => 'global',
                        'type' => (string) ($a['addressType'] ?? ($a['type'] ?? '')),
                        'status' => (string) ($a['status'] ?? ''),
                        'users' => array_map(static fn($u) => substr((string) $u, (int) strrpos((string) $u, '/') + 1), is_array($a['users'] ?? null) ? $a['users'] : []),
                    ];
                }
            } catch (Throwable $e) {
                // 全局地址权限不足时静默跳过（与 Python 版一致）
            }
            foreach ($out as &$a) {
                if (isset($a['users'])) {
                    $a['inUse'] = !empty($a['users']);
                }
            }
            unset($a);
            return $out;
        });
    }

    // ------------------------------------------------------------------
    // 15. 服务账号（IAM）
    // ------------------------------------------------------------------
    public function service_accounts(): array
    {
        return $this->run(function (): array {
            $d = $this->get(self::IAM_API . '/projects/' . $this->proj() . '/serviceAccounts', ['pageSize' => 100], 45);
            $out = [];
            foreach (($d['accounts'] ?? []) as $a) {
                $out[] = [
                    'email' => (string) ($a['email'] ?? ''),
                    'displayName' => (string) ($a['displayName'] ?? ''),
                    'disabled' => (bool) ($a['disabled'] ?? false),
                    'uniqueId' => (string) ($a['uniqueId'] ?? ''),
                    'oauth2ClientId' => (string) ($a['oauth2ClientId'] ?? ''),
                    'description' => (string) ($a['description'] ?? ''),
                ];
            }
            return $out;
        });
    }

    // ------------------------------------------------------------------
    // 16. 计算资源汇总
    // ------------------------------------------------------------------
    public function summary(): array
    {
        return $this->run(function (): array {
            $insts = [];
            $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/aggregated/instances', ['maxResults' => 500], 60);
            foreach (self::agg_iter($d) as [$key, $items]) {
                $zone = $key !== '' ? substr($key, (int) strrpos($key, '/') + 1) : '';
                foreach ($items as $i) {
                    if (!is_array($i)) {
                        continue;
                    }
                    $ip = '';
                    $pip = '';
                    foreach (($i['networkInterfaces'] ?? []) as $ni) {
                        $pip = $pip !== '' ? $pip : (string) ($ni['networkIP'] ?? '');
                        foreach (($ni['accessConfigs'] ?? []) as $ac) {
                            $ip = $ip !== '' ? $ip : (string) ($ac['natIP'] ?? '');
                        }
                    }
                    $mt = (string) ($i['machineType'] ?? '');
                    $sched = $i['scheduling'] ?? [];
                    $insts[] = [
                        'name' => (string) ($i['name'] ?? ''),
                        'zone' => $zone,
                        'status' => (string) ($i['status'] ?? ''),
                        'machineType' => $mt !== '' ? substr($mt, (int) strrpos($mt, '/') + 1) : '',
                        'ip' => $ip, 'privateIp' => $pip,
                        'cpuPlatform' => (string) ($i['cpuPlatform'] ?? ''),
                        'creationTimestamp' => (string) ($i['creationTimestamp'] ?? ''),
                        'preemptible' => is_array($sched) ? (bool) ($sched['preemptible'] ?? false) : false,
                        'spot' => is_array($sched) ? (strtoupper((string) ($sched['provisioningModel'] ?? '')) === 'SPOT') : false,
                        'disks' => is_array($i['disks'] ?? null) ? count($i['disks']) : 0,
                        'tags' => array_map('strval', is_array($i['tags']['items'] ?? null) ? $i['tags']['items'] : []),
                    ];
                }
            }
            $byStatus = [];
            $byZone = [];
            $byType = [];
            foreach ($insts as $i) {
                $byStatus[$i['status']] = ($byStatus[$i['status']] ?? 0) + 1;
                $byZone[$i['zone']] = ($byZone[$i['zone']] ?? 0) + 1;
                $byType[$i['machineType']] = ($byType[$i['machineType']] ?? 0) + 1;
            }
            // CPU 合计（共享核机型按 GCP 口径计 2 vCPU）
            $totalCpu = 0;
            try {
                $families = [];
                $md = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/aggregated/machineTypes', ['maxResults' => 500], 60);
                foreach (self::agg_iter($md) as [$key, $items]) {
                    foreach ($items as $m) {
                        if (is_array($m) && isset($m['name']) && !isset($families[$m['name']])) {
                            $families[$m['name']] = $m['guestCpus'] ?? 0;
                        }
                    }
                }
                foreach ($insts as $i) {
                    $totalCpu += (int) ($families[$i['machineType']] ?? 0);
                }
            } catch (Throwable $e) {
                // 权限不足时 CPU 合计为 0（与 Python 版一致）
            }
            return [
                'instanceCount' => count($insts),
                'running' => $byStatus['RUNNING'] ?? 0,
                'stopped' => ($byStatus['TERMINATED'] ?? 0) + ($byStatus['STOPPED'] ?? 0),
                'byStatus' => $byStatus,
                'byZone' => $byZone,
                'byMachineType' => $byType,
                'totalVCpu' => $totalCpu,
                'preemptible' => count(array_filter($insts, static fn($i) => $i['preemptible'])),
                'spot' => count(array_filter($insts, static fn($i) => $i['spot'])),
                'instances' => $insts,
            ];
        });
    }

    // ------------------------------------------------------------------
    // 17. 其他网络 / 负载均衡类资源
    // ------------------------------------------------------------------
    public function extras(): array
    {
        $jobs = [
            ['routers', 'routers', 'region'],
            ['vpnTunnels', 'vpnTunnels', 'region'],
            ['forwardingRules', 'forwardingRules', 'region'],
            ['globalForwardingRules', 'global/forwardingRules', null],
            ['instanceGroups', 'instanceGroups', 'zone'],
            ['instanceTemplates', 'global/instanceTemplates', null],
            ['healthChecks', 'global/healthChecks', null],
            ['backendServices', 'global/backendServices', null],
            ['reservations', 'reservations', 'zone'],
            ['commitments', 'commitments', 'region'],
        ];
        return $this->run(function () use ($jobs): array {
            $out = [];
            foreach ($jobs as [$label, $path, $scope]) {
                try {
                    $names = [];
                    if ($scope !== null) {
                        $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/aggregated/' . $path, ['maxResults' => 500], 60);
                        foreach (self::agg_iter($d) as [$key, $items]) {
                            $prefix = $key !== '' ? substr($key, (int) strrpos($key, '/') + 1) . '/' : '';
                            foreach ($items as $it) {
                                if (is_array($it) && isset($it['name'])) {
                                    $names[] = $prefix . $it['name'];
                                }
                            }
                        }
                    } else {
                        $d = $this->get(self::COMPUTE_API . '/projects/' . $this->proj() . '/' . $path, ['maxResults' => 500], 60);
                        foreach (($d['items'] ?? []) as $it) {
                            if (is_array($it) && isset($it['name'])) {
                                $names[] = (string) $it['name'];
                            }
                        }
                    }
                    sort($names);
                    $out[$label] = ['count' => count($names), 'items' => $names];
                } catch (Throwable $e) {
                    $out[$label] = ['count' => 0, 'items' => [], 'error' => self::brief($e->getMessage())];
                }
            }
            return $out;
        });
    }

    // ------------------------------------------------------------------
    // aggregated 响应解析：扫每个分区里的「第一个含 name 的列表」
    //   aggregated 的响应字段名与资源名不同构（commitments 而非
    //   regionCommitments），硬拼会静默拿到空列表（最危险的假阴性）。
    // ------------------------------------------------------------------
    private static function agg_iter(array $resp): array
    {
        $items = $resp['items'] ?? [];
        if (!is_array($items)) {
            return [];
        }
        $out = [];
        foreach ($items as $key => $bucket) {
            if (!is_array($bucket)) {
                continue;
            }
            foreach ($bucket as $field => $val) {
                if ($field === 'warning' || $field === 'warnings') {
                    continue;
                }
                if (is_array($val) && isset($val[0]) && is_array($val[0]) && isset($val[0]['name'])) {
                    $out[] = [(string) $key, $val];
                    break;
                }
            }
        }
        return $out;
    }

    private static function anyIn(array $haystack, array $needles): bool
    {
        foreach ($haystack as $h) {
            if (in_array($h, $needles, true)) {
                return true;
            }
        }
        return false;
    }

    // ==================================================================
    // 分节常量
    // ==================================================================
    public static function all_sections(): array
    {
        return [
            'account', 'project', 'services', 'billing', 'serviceAccounts',
            'regions', 'zones', 'machineTypes', 'images', 'networks',
            'subnetworks', 'firewalls', 'disks', 'snapshots', 'addresses',
            'summary', 'extras',
        ];
    }

    /** 默认「快速」节（都是必有的 compute 只读权限，适合首屏） */
    public const DEFAULT_SECTIONS = ['account', 'summary', 'regions', 'zones', 'networks',
        'subnetworks', 'firewalls', 'disks', 'snapshots', 'addresses'];

    /** 「深度」节（常需额外权限或较慢） */
    public const DEEP_SECTIONS = ['project', 'services', 'billing', 'serviceAccounts',
        'machineTypes', 'images', 'extras'];

    /** 跑单节 */
    public function section(string $name, array $params = []): array
    {
        $region = (string) ($params['region'] ?? '');
        $zone = (string) ($params['zone'] ?? '');
        if ($zone === '' && $region !== '') {
            $zone = $region . '-a';
        }
        switch ($name) {
            case 'account':         return $this->account();
            case 'project':         return $this->project();
            case 'services':        return $this->services();
            case 'billing':         return $this->billing();
            case 'serviceAccounts': return $this->service_accounts();
            case 'regions':         return $this->regions();
            case 'zones':           return $this->zones($region);
            case 'machineTypes':    return $this->machine_types($zone !== '' ? $zone : 'us-central1-a');
            case 'images':          return $this->images();
            case 'networks':        return $this->networks();
            case 'subnetworks':     return $this->subnetworks($region);
            case 'firewalls':       return $this->firewalls();
            case 'disks':           return $this->disks();
            case 'snapshots':       return $this->snapshots();
            case 'addresses':       return $this->addresses();
            case 'summary':         return $this->summary();
            case 'extras':          return $this->extras();
        }
        return ['ok' => false, 'ms' => 0, 'error' => "未知的勘察节：{$name}"];
    }

    // ==================================================================
    // 批量入口（含并发缓存加锁）
    // ==================================================================
    public const INSPECT_TTL = 45;

    private static function cache_path(): string
    {
        $dir = Config::dataDir();
        if (!is_dir($dir)) {
            @mkdir($dir, 0700, true);
        }
        return $dir . '/inspect_cache.json';
    }

    private static function cache_key(string $keyPath, string $projectId, array $sections, string $region, string $zone): string
    {
        return md5($keyPath . '|' . $projectId . '|' . implode(',', $sections) . '|' . $region . '|' . $zone);
    }

    /**
     * 按名称逐节勘察。返回 {section: {ok, ms, data|error}}，顺序与输入一致。
     *
     * 缓存：写入 data/inspect_cache.json，用 flock(LOCK_EX) 串行化。
     * 时间戳取「完成时刻」而非开始时刻（整套可能跑很久，若记开始时刻条目
     * 一存进去就过期，缓存永远不命中）。
     */
    public static function inspect_sections(
        string $keyPath,
        string $projectId,
        string $email,
        array $sections,
        array $params = [],
        string $proxy = '',
        string $proxyType = 'HTTPS',
        int $workers = 5,
        bool $fresh = false,
        bool $useCache = true
    ): array {
        $region = (string) ($params['region'] ?? '');
        $zone = (string) ($params['zone'] ?? '');
        $ck = self::cache_key($keyPath, $projectId, $sections, $region, $zone);
        $cacheFile = self::cache_path();

        if ($useCache && !$fresh) {
            $hit = self::cache_read($cacheFile, $ck);
            if ($hit !== null) {
                $hit['cached'] = true;
                $hit['age'] = (int) (time() - (float) ($hit['_ts'] ?? time()));
                return $hit;
            }
        }

        $ins = new self($keyPath, $projectId, $email, $proxy, $proxyType);
        $out = [];
        foreach ($sections as $n) {
            // 串行执行（PHP 无轻量线程）；结果字段与 Python 并发版一致
            $out[$n] = $ins->section((string) $n, ['region' => $region, 'zone' => $zone]);
        }
        // 保持调用方传入的顺序
        $ordered = [];
        foreach ($sections as $n) {
            $ordered[(string) $n] = $out[$n] ?? ['ok' => false, 'ms' => 0, 'error' => '未执行'];
        }
        $ordered['_ts'] = time();

        if ($useCache) {
            self::cache_write($cacheFile, $ck, $ordered);
        }
        unset($ordered['_ts']);
        return $ordered;
    }

    private static function cache_read(string $file, string $ck): ?array
    {
        if (!is_file($file)) {
            return null;
        }
        $fh = @fopen($file, 'r');
        if ($fh === false) {
            return null;
        }
        try {
            if (!flock($fh, LOCK_SH)) {
                return null;
            }
            $raw = stream_get_contents($fh);
            flock($fh, LOCK_UN);
        } finally {
            fclose($fh);
        }
        $all = json_decode((string) $raw, true);
        if (!is_array($all) || !isset($all[$ck])) {
            return null;
        }
        $entry = $all[$ck];
        $ts = (float) ($entry['_ts'] ?? 0);
        if (time() - $ts >= self::INSPECT_TTL) {
            return null;
        }
        return is_array($entry) ? $entry : null;
    }

    private static function cache_write(string $file, string $ck, array $entry): void
    {
        $fh = @fopen($file, 'c+');
        if ($fh === false) {
            return;
        }
        try {
            if (!flock($fh, LOCK_EX)) {
                return;
            }
            $raw = stream_get_contents($fh);
            $all = json_decode((string) $raw, true);
            if (!is_array($all)) {
                $all = [];
            }
            $all[$ck] = $entry;
            // 缓存别无限涨：超过 32 条按时间淘汰最老的 8 条
            if (count($all) > 32) {
                uasort($all, static fn($a, $b) => ((float) ($a['_ts'] ?? 0)) <=> ((float) ($b['_ts'] ?? 0)));
                $all = array_slice($all, 8, null, true);
            }
            ftruncate($fh, 0);
            rewind($fh);
            fwrite($fh, (string) json_encode($all, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
            fflush($fh);
            flock($fh, LOCK_UN);
        } finally {
            fclose($fh);
        }
        @chmod($file, 0600);
    }

    // ---- camelCase 别名 ----
    public static function inspectSections(
        string $keyPath, string $projectId, string $email, array $sections,
        array $params = [], string $proxy = '', string $proxyType = 'HTTPS',
        int $workers = 5, bool $fresh = false, bool $useCache = true
    ): array {
        return self::inspect_sections($keyPath, $projectId, $email, $sections, $params, $proxy, $proxyType, $workers, $fresh, $useCache);
    }
    public static function allSections(): array { return self::all_sections(); }
}
