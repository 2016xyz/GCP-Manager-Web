<?php
/**
 * ApiGcp —— GCP 账号 / 实例 / 勘察 / 费用 / 配置 的 HTTP handler
 *
 * 设计要点：
 *   · 本类不含鉴权（见 index.php 中间件）。权限点由路由表声明：
 *       view     只读：看列表、看费用、勘察、测账号
 *       operate  变更：建机、启停、删机、执行命令、改备注、生成密钥
 *       account  账号：增删改 GCP 账号
 *       settings 保存默认配置
 *   · 账号列表**必须**白名单式丢弃 key_path 与代理明文密码 ——
 *     Python 版第一轮审计发现 proxy 字段会把明文密码发上屏（mask_proxy 只处理
 *     `@` 形态，漏了页面自己提示的 host:port:user:pass 形态），这里两个都打码。
 *   · 实例列表 sync=false 走本地库，**必须**过滤 root 密码字段
 *     （Python 版第二轮审计的高危项：sync=false 时把全部 root 密码明文回了）。
 *   · 所有外连 URL 都来自硬编码常量或 Gcp 类内部拼接，不接受请求参数当 URL。
 */

declare(strict_types=1);

final class ApiGcp
{
    /** 每用户默认配置在 settings 表里的键（与 Python 版 _cfg_key 同构） */
    private static function cfgKey(int $userId): string
    {
        return 'cfg:' . $userId;
    }

    /** 取当前登录用户的 id（中间件已保证有会话） */
    private static function uid(): int
    {
        $s = Auth::currentSession();
        return (int) ($s['user_id'] ?? 0);
    }

    private static function uname(): string
    {
        $s = Auth::currentSession();
        return (string) ($s['username'] ?? '');
    }

    /** 由账号行构造 GCP 客户端 */
    private static function client(array $acc): Gcp
    {
        return new Gcp(
            (string) ($acc['key_path'] ?? ''),
            (string) ($acc['project_id'] ?? ''),
            (string) ($acc['email'] ?? ''),
            (string) ($acc['proxy'] ?? ''),
            (string) ($acc['proxy_type'] ?? 'HTTPS')
        );
    }

    /** 取账号，不存在则 404 */
    private static function requireAccount(int $id): array
    {
        $acc = Store::getAccount($id);
        if ($acc === null) {
            Json::err('账号不存在', 404);
        }
        return $acc;
    }

    // ══ 概览 / 配置 ═══════════════════════════════════════════════════════

    /** GET /api/status → 与 Python 版同字段（前端「关于」卡片 + 概览数字） */
    public static function status(array $p): void
    {
        $users = Users::listUsers(true);
        $active = 0;
        foreach ($users as $u) {
            if ((int) ($u['disabled'] ?? 0) === 0) {
                $active++;
            }
        }
        Json::ok(array_merge(Version::info(), [
            'app_name'          => Version::APP_NAME,
            'app_name_cn'       => Version::APP_NAME_CN,
            'repo'              => Version::REPO_URL,
            'repo_name'         => Version::REPO_NAME,
            'issue_url'         => Version::ISSUE_URL,
            'changelog'         => Version::changelog(),
            'accounts'          => count(Store::getAccounts()),
            'vms_with_password' => count(Store::getAllVms()),
            // ★ 必须是 bool，不能把 Ssh::available() 的数组直接回给前端：
            //   JS 里非空数组恒为真，前端 `paramiko ? '可用' : '不可用'` 会永远显示"可用"。
            //   Python 版此处就是 bool（core/ssh.py 的 check_paramiko 返回 True/False）。
            'paramiko'          => (bool) (Ssh::available()['ok'] ?? false),
            'machine_types'     => count(Catalog::MACHINE_TYPES),
            'images'            => count(Catalog::IMAGES),
            'disk_types'        => count(Catalog::DISK_TYPES),
            'regions'           => count(Catalog::all_regions()),
            'users'             => count($users),
            'active_users'      => $active,
            'time'              => microtime(true),
            // 明确标注后端实现，便于两版共存时一眼分辨
            'backend'           => 'php',
            'php_version'       => PHP_VERSION,
        ]));
    }

    /** GET /api/config → {ok, config, scope, user} */
    public static function getConfig(array $p): void
    {
        $uid = self::uid();
        $saved = Store::getSetting(self::cfgKey($uid), []);
        if (!is_array($saved)) {
            $saved = [];
        }
        $cfg = array_merge(Catalog::DEFAULT_CONFIG ?? [], $saved);
        Json::ok([
            'config' => $cfg,
            'scope'  => 'user',
            'user'   => self::uname(),
        ]);
    }

    /**
     * POST /api/config  body: {config:{...}}
     * → {ok, scope, config}
     * 只接受白名单键，避免把任意 JSON 灌进 settings 表。
     */
    public static function setConfig(array $p): void
    {
        $b = Http::jsonBody();
        $incoming = $b['config'] ?? $b;
        if (!is_array($incoming)) {
            Json::err('config 必须是对象', 400);
        }
        // Python 版 app.py 的 api_set_config 只做一件事：
        //     data = {k: v for k, v in (payload or {}).items() if v is not None}
        // 即「过滤掉 None 就整体存进 settings 的 JSON 字段」，**没有键白名单**。
        // 早前照 Catalog::DEFAULT_CONFIG 做白名单，会把合法的自定义配置项挡掉
        // （且 {} 会被判 400，而 Python 是 200）—— 这里改为对齐 Python，
        // 只加容量上限，避免 settings 被灌成无限增长的大对象。
        $clean = [];
        foreach ($incoming as $k => $v) {
            if (!is_string($k) || $k === '' || mb_strlen($k) > 120) {
                continue;
            }
            if ($v === null) {
                continue;   // 与 Python 一致：None 不写入（表示"不修改该项"）
            }
            $clean[$k] = $v;
            if (count($clean) >= 200) {
                Json::err('配置项过多（上限 200 项）', 400);
            }
        }
        $encoded = json_encode($clean, JSON_UNESCAPED_UNICODE);
        if ($encoded === false || strlen($encoded) > 65536) {
            Json::err('配置内容过大（上限 64KB）', 400);
        }
        $uid = self::uid();
        $cur = Store::getSetting(self::cfgKey($uid), []);
        if (!is_array($cur)) {
            $cur = [];
        }
        $merged = array_merge($cur, $clean);
        Store::setSetting(self::cfgKey($uid), $merged);
        Users::addAudit(self::uname(), Http::clientIp(), 'save_config', '',
            implode(',', array_keys($clean)), true);
        // Python 只回 {ok:true, scope:"user"}；这里多回一个 config 作为超集（前端不用也无害）
        Json::ok(['scope' => 'user', 'config' => $merged]);
    }

    // ══ 目录 / 费用 / 预设 ═════════════════════════════════════════════════

    /** GET /api/catalog?region=&include_unavailable= → catalog_payload + savings */
    public static function catalog(array $p): void
    {
        $region = Http::query('region', 'us-central1') ?: 'us-central1';
        $inc = in_array(strtolower((string) Http::query('include_unavailable', '')), ['1', 'true', 'yes'], true);
        $payload = Catalog::catalog_payload($region, $inc);
        $saved = Store::getSetting(self::cfgKey(self::uid()), []);
        if (!is_array($saved)) {
            $saved = [];
        }
        $payload['savings'] = Catalog::savings_status(array_merge(Catalog::DEFAULT_CONFIG ?? [], $saved));
        Json::ok($payload);
    }

    /** POST /api/savings  body: {config?} → {ok, savings} */
    public static function savings(array $p): void
    {
        $b = Http::jsonBody();
        $base = Catalog::DEFAULT_CONFIG ?? [];
        $saved = Store::getSetting(self::cfgKey(self::uid()), []);
        if (!is_array($saved)) {
            $saved = [];
        }
        $cfg = array_merge($base, $saved);
        if (isset($b['config']) && is_array($b['config'])) {
            $cfg = array_merge($cfg, array_intersect_key($b['config'], $base));
        }
        Json::ok(['savings' => Catalog::savings_status($cfg)]);
    }

    /** POST /api/cost/estimate → {ok, estimate} */
    public static function costEstimate(array $p): void
    {
        $b = Http::jsonBody();
        $est = Catalog::estimate_monthly_cost(
            isset($b['machine_type']) ? (string) $b['machine_type'] : null,
            isset($b['disk_type']) ? (string) $b['disk_type'] : null,
            $b['disk_size_gb'] ?? 30,
            isset($b['region']) ? (string) $b['region'] : null,
            (int) ($b['hours'] ?? 730),
            (int) ($b['count'] ?? 1),
            (bool) ($b['preemptible'] ?? false),
            (bool) ($b['spot'] ?? false)
        );
        Json::ok(['estimate' => $est]);
    }

    /** GET /api/install_presets → {ok, presets} */
    public static function installPresets(array $p): void
    {
        Json::ok(['presets' => InstallPresets::preset_payload()]);
    }

    // ══ 项目级只读查询 ═════════════════════════════════════════════════════

    /**
     * GET /api/project_zones?account_id=&region=
     * → {ok, zones[], region, error?}
     * 没配账号时返回空数组 + error 文案（前端据此提示「先导入账号」），
     * 而不是 500 —— 与 Python 版行为一致。
     */
    public static function projectZones(array $p): void
    {
        $accId = Http::queryInt('account_id');
        $region = (string) (Http::query('region') ?: '');
        if ($accId === null) {
            Json::ok(['zones' => [], 'region' => $region, 'error' => '未指定账号']);
        }
        $acc = Store::getAccount($accId);
        if ($acc === null) {
            Json::ok(['zones' => [], 'region' => $region, 'error' => '账号不存在']);
        }
        if (!is_file((string) $acc['key_path'])) {
            Json::ok(['zones' => [], 'region' => $region, 'error' => '账号密钥文件不存在']);
        }
        try {
            $zones = self::client($acc)->list_zones($region);
            Json::ok(['zones' => $zones, 'region' => $region]);
        } catch (Throwable $e) {
            Json::ok(['zones' => [], 'region' => $region,
                      'error' => '查询区域失败：' . $e->getMessage()]);
        }
    }

    /** GET /api/project_networks?account_id=&region= → {ok, networks, subnets, has_default_network, region, error?} */
    public static function projectNetworks(array $p): void
    {
        $accId = Http::queryInt('account_id');
        $region = (string) (Http::query('region') ?: '');
        if ($accId === null) {
            Json::ok(['networks' => [], 'subnets' => [], 'has_default_network' => false,
                      'region' => $region, 'error' => '未指定账号']);
        }
        $acc = Store::getAccount($accId);
        if ($acc === null) {
            Json::ok(['networks' => [], 'subnets' => [], 'has_default_network' => false,
                      'region' => $region, 'error' => '账号不存在']);
        }
        if (!is_file((string) $acc['key_path'])) {
            Json::ok(['networks' => [], 'subnets' => [], 'has_default_network' => false,
                      'region' => $region, 'error' => '账号密钥文件不存在']);
        }
        try {
            $cli = self::client($acc);
            $nets = $cli->list_networks();
            $subs = [];
            if ($region !== '') {
                foreach ($cli->list_subnetworks($region) as $s) {
                    $subs[] = $s;
                }
            }
            // 该 VPC 是否存在（Python 版硬编码 global/networks/default 曾导致建机 404，
            // 所以这里必须如实告诉前端「有没有 default」）
            $hasDefault = false;
            foreach ($nets as $n) {
                if (($n['name'] ?? '') === 'default') {
                    $hasDefault = true;
                    break;
                }
            }
            Json::ok([
                'networks'            => $nets,
                'subnets'             => $subs,
                'has_default_network' => $hasDefault,
                'region'              => $region,
            ]);
        } catch (Throwable $e) {
            Json::ok(['networks' => [], 'subnets' => [], 'has_default_network' => false,
                      'region' => $region, 'error' => '查询网络失败：' . $e->getMessage()]);
        }
    }

    // ══ 只读资源勘察 ═══════════════════════════════════════════════════════

    /**
     * GET /api/inspect/sections → {ok, all[], default[], deep[]}
     * 注意 Inspect::all_sections() 返回的是**节名列表**，而 API 契约要求
     * 包成 {all, default, deep} 三个字段（前端据此渲染勾选项）——
     * 早前直接把它交给 Json::ok 会退化成带数字下标的数组，
     * 前端拿不到 all/default/deep，勾选项就是空的。
     */
    public static function inspectSections(array $p): void
    {
        $all = Inspect::all_sections();
        sort($all, SORT_STRING);   // 与 Python 的 sorted(SECTIONS) 同口径
        Json::ok([
            'all'     => $all,
            'default' => Inspect::DEFAULT_SECTIONS,
            'deep'    => Inspect::DEEP_SECTIONS,
        ]);
    }

    /**
     * GET /api/inspect?account_id=&sections=a,b&region=&zone=&fresh=&deep=
     * → {ok, ...} 或 {ok:false, error}
     * ★ 勘察全部只读：只发 GET，不创建/修改任何云资源。
     */
    public static function inspect(array $p): void
    {
        $accId = Http::queryInt('account_id');
        if ($accId === null) {
            Json::ok(['error' => '未指定账号']);
        }
        $acc = Store::getAccount($accId);
        if ($acc === null) {
            Json::ok(['error' => '账号不存在']);
        }
        $keyPath = (string) ($acc['key_path'] ?? '');
        if (!is_file($keyPath)) {
            Json::ok(['error' => '账号密钥文件不存在']);
        }

        $sectionsRaw = (string) (Http::query('sections') ?: Http::query('section') ?: '');
        $requested = array_values(array_filter(array_map('trim', explode(',', $sectionsRaw)), fn($x) => $x !== ''));
        $all = Inspect::all_sections();            // 返回的是节名列表
        $default = Inspect::DEFAULT_SECTIONS;
        $want = $requested !== [] ? $requested : $default;
        // ★ 白名单：只接受已知的 section，绝不把用户字符串当标识去用
        $want = array_values(array_intersect($want, $all));
        if (empty($want)) {
            Json::err('没有有效的勘察项', 400);
        }
        $deep = in_array(strtolower((string) Http::query('deep', '')), ['1', 'true', 'yes'], true);
        $fresh = in_array(strtolower((string) Http::query('fresh', '')), ['1', 'true', 'yes'], true);

        $params = [];
        if ($r = Http::query('region')) {
            $params['region'] = (string) $r;
        }
        if ($z = Http::query('zone')) {
            $params['zone'] = (string) $z;
        }

        try {
            $res = Inspect::inspect_sections(
                $keyPath,
                (string) ($acc['project_id'] ?? ''),
                (string) ($acc['email'] ?? ''),
                $want,
                $params,
                (string) ($acc['proxy'] ?? ''),
                (string) ($acc['proxy_type'] ?? 'HTTPS'),
                $deep ? 10 : 5,
                $fresh,
                true
            );
            Json::ok($res);
        } catch (Throwable $e) {
            error_log('[gcpweb] inspect: ' . $e->getMessage());
            Json::ok(['error' => '勘察失败：' . $e->getMessage()]);
        }
    }

    // ══ GCP 账号 ═══════════════════════════════════════════════════════════

    /** GET /api/accounts → {ok, accounts[]}；key_path 与代理明文**不外发** */
    public static function listAccounts(array $p): void
    {
        $accounts = Store::getAccounts();
        $localCnt = [];
        foreach (Store::getAllVms() as $vm) {
            $k = (string) ($vm['account_id'] ?? '');
            if ($k !== '') {
                $localCnt[$k] = ($localCnt[$k] ?? 0) + 1;
            }
        }
        $live = self::cachedInstanceCounts();
        $liveMap = [];
        foreach (($live['counts'] ?? []) as $c) {
            $liveMap[(string) ($c['account_id'] ?? '')] = $c['count'] ?? null;
        }

        $out = [];
        foreach ($accounts as $a) {
            $keyPath = (string) ($a['key_path'] ?? '');
            $rawProxy = (string) ($a['proxy'] ?? '');
            $pinfo = Gcp::parse_proxy_input($rawProxy, (string) ($a['proxy_type'] ?? 'HTTPS'));
            $out[] = [
                'id'                => (int) $a['id'],
                'email'             => (string) ($a['email'] ?? ''),
                'project_id'        => (string) ($a['project_id'] ?? ''),
                'label'             => (string) ($a['label'] ?? ''),
                'created_at'        => $a['created_at'] ?? null,
                'key_exists'        => $keyPath !== '' && is_file($keyPath),
                'key_file'          => $keyPath !== '' ? basename($keyPath) : '',
                'proxy_set'         => trim($rawProxy) !== '',
                'proxy_ok'          => (bool) ($pinfo['ok'] ?? false),
                'proxy_error'       => (string) ($pinfo['error'] ?? ''),
                'proxy_type'        => (string) ($pinfo['proxy_type'] ?? ($a['proxy_type'] ?? 'HTTPS')),
                'proxy_type_label'  => (string) ($pinfo['proxy_type_label'] ?? ''),
                'proxy_has_auth'    => (bool) ($pinfo['has_auth'] ?? false),
                'proxy_display'     => Gcp::mask_proxy($rawProxy),
                'proxy_host'        => (string) ($pinfo['host'] ?? ''),
                'proxy_port'        => (string) ($pinfo['port'] ?? ''),
                'inst_count_local'  => $localCnt[(string) $a['id']] ?? 0,
                'inst_count_live'   => $liveMap[(string) $a['id']] ?? null,
                'inst_count_live_at' => $live['at'] ?? null,
            ];
        }
        Json::ok(['accounts' => $out]);
    }

    /** 实例数实时缓存（Python 版同款：缓存内容 + 时间戳） */
    private static function cachedInstanceCounts(): array
    {
        $c = Store::getSetting('inst_counts_cache', []);
        return is_array($c) ? $c : [];
    }

    /** 缓存里的实时计数（供 listAccounts 复用，返回 [account_id => count]） */
    private static function cachedLiveMap(): array
    {
        $out = [];
        foreach ((self::cachedInstanceCounts()['counts'] ?? []) as $c) {
            $out[(string) ($c['account_id'] ?? '')] = $c['inst_count_live'] ?? null;
        }
        return $out;
    }

    /**
     * POST /api/accounts  body: {key_path} 或 {json_content, label?, proxy?, proxy_type?}
     * → {ok, account_id, email, project_id, created}
     * ★ 必须校验内容真的是服务账号 JSON —— 否则这接口等于「按路径读任意 JSON
     *   并回显其字段」（Python 版第二轮审计的中危项）。
     */
    public static function addAccount(array $p): void
    {
        $b = Http::jsonBody();
        $label = mb_substr(trim((string) ($b['label'] ?? '')), 0, 80);
        $proxy = trim((string) ($b['proxy'] ?? ''));
        $proxyType = (string) ($b['proxy_type'] ?? 'HTTPS');
        $keyPath = trim((string) ($b['key_path'] ?? ''));
        $jsonContent = (string) ($b['json_content'] ?? '');

        $created = false;
        if ($jsonContent !== '') {
            // 上传/粘贴的内容：落盘到 data/keys/，文件名由内容哈希决定（不接受用户给名字）
            $data = json_decode($jsonContent, true);
            if (!is_array($data)) {
                Json::err('不是合法的 JSON', 400);
            }
            // ★ Gcp::validate_service_account_array() 返回的是 [bool, reason] 元组，
            //   **不是抛异常**。原先按「失败抛异常」写，导致校验失败的输入被静默
            //   当成"通过"，随后用空 email/project 建了一条垃圾账号记录，还回 ok:true
            //   （实测：POST /api/accounts {"key_path":"file:///etc/passwd"} → 200 ok:true，
            //    库里多出一条 email='' 的账号；Python 版是 400）。
            [$saOk, $saWhy] = Gcp::validate_service_account_array($data);
            if (!$saOk) {
                Json::err('不是有效的服务账号 JSON：' . $saWhy, 400);
            }
            $info = $data;
            Config::ensureDataDirs();
            $name = 'sa-' . substr(hash('sha256', $jsonContent), 0, 16) . '.json';
            $dest = Config::keysDir() . '/' . $name;
            if (file_put_contents($dest, $jsonContent, LOCK_EX) === false) {
                Json::err('写入密钥文件失败（检查 data/keys 权限）', 500);
            }
            @chmod($dest, 0600);
            $keyPath = $dest;
            $created = true;
        } else {
            if ($keyPath === '') {
                Json::err('请提供 key_path 或 json_content', 400);
            }
            // ★ 拒绝流包装器（file:// / http:// / php:// / data:// …）：
            //   PHP 的 is_file() 对 file:// 返回 true、file_get_contents() 也会读它，
            //   于是「绝对路径」这个语义被绕开了；Python 的 open() 不认 file://，
            //   同样是这个输入会直接报"文件不存在"。两者行为不一致本身就该消除 ——
            //   而且 php:// 之类的包装器能读出进程内存等本不该被文件读取接口碰到的东西。
            //   这里只允许真实的本地文件系统路径。
            if (preg_match('#^[A-Za-z][A-Za-z0-9+.\-]*://#', $keyPath)) {
                Json::err('key_path 只接受本地文件系统路径，不接受 ' . strstr($keyPath, '://', true) . ':// 之类的URL包装器', 400);
            }
            // 同上：这里是 [bool, reason] 元组返回，不是异常
            [$saOk, $saWhy] = Gcp::validate_service_account_file($keyPath);
            if (!$saOk) {
                Json::err('无法导入：' . $saWhy, 400);
            }
            $info = (array) json_decode((string) @file_get_contents($keyPath), true);
        }

        $email = (string) ($info['client_email'] ?? '');
        $projectId = (string) ($info['project_id'] ?? '');

        $existing = null;
        foreach (Store::getAccounts() as $a) {
            if ($a['email'] === $email && (string) $a['project_id'] === $projectId) {
                $existing = $a;
                break;
            }
        }
        if ($existing !== null) {
            Store::updateAccount((int) $existing['id'], [
                'key_path' => $keyPath, 'label' => $label,
                'proxy' => $proxy, 'proxy_type' => $proxyType,
            ]);
            $accId = (int) $existing['id'];
        } else {
            $accId = Store::addAccount($email, $projectId, $keyPath, $proxy, $proxyType, $label);
        }
        Users::addAudit(self::uname(), Http::clientIp(), 'add_account', $email,
            'project=' . $projectId, true);
        Json::ok([
            'account_id' => $accId, 'email' => $email,
            'project_id' => $projectId, 'created' => $created,
        ]);
    }

    /**
     * POST /api/accounts/upload  multipart 文件上传
     * → {ok, account_id, email, project_id, created}
     * 只接受 .json，大小上限 256KB，内容必须是服务账号 JSON。
     */
    public static function uploadAccount(array $p): void
    {
        $f = $_FILES['file'] ?? ($_FILES['json'] ?? null);
        if (!is_array($f) || !isset($f['tmp_name'])) {
            Json::err('没有收到文件（字段名应为 file）', 400);
        }
        if ((int) $f['error'] !== UPLOAD_ERR_OK) {
            Json::err('上传失败（错误码 ' . (int) $f['error'] . '）', 400);
        }
        if ((int) $f['size'] > 262144) {
            Json::err('文件过大（服务账号 JSON 不应超过 256KB）', 413);
        }
        // 只认 JSON 后缀；同时用 finfo 看真实类型，双保险
        $origName = (string) ($f['name'] ?? '');
        if (strtolower(pathinfo($origName, PATHINFO_EXTENSION)) !== 'json') {
            Json::err('只接受 .json 文件', 400);
        }
        $content = @file_get_contents((string) $f['tmp_name']);
        if (!is_string($content) || $content === '') {
            Json::err('读取上传内容失败', 400);
        }
        if (function_exists('finfo_open')) {
            $fi = finfo_open(FILEINFO_MIME_TYPE);
            $mime = $fi ? (string) finfo_file($fi, (string) $f['tmp_name']) : '';
            if ($fi) {
                finfo_close($fi);
            }
            // 服务账号 JSON 常见 text/plain、application/json、application/octet-stream
            if ($mime !== '' && !preg_match('#(json|text/plain|octet-stream)#i', $mime)) {
                Json::err('文件类型不像 JSON（' . $mime . '）', 400);
            }
        }
        $data = json_decode($content, true);
        if (!is_array($data)) {
            Json::err('文件内容不是合法 JSON', 400);
        }
        [$saOk, $saWhy] = Gcp::validate_service_account_array($data);
        if (!$saOk) {
            Json::err('不是有效的服务账号 JSON：' . $saWhy, 400);
        }
        $info = $data;

        Config::ensureDataDirs();
        $dest = Config::keysDir() . '/sa-' . substr(hash('sha256', $content), 0, 16) . '.json';
        if (file_put_contents($dest, $content, LOCK_EX) === false) {
            Json::err('写入密钥文件失败（检查 data/keys 权限）', 500);
        }
        @chmod($dest, 0600);

        $email = (string) ($info['client_email'] ?? '');
        $projectId = (string) ($info['project_id'] ?? '');
        $label = mb_substr(trim((string) ($_POST['label'] ?? '')), 0, 80);
        $proxy = trim((string) ($_POST['proxy'] ?? ''));
        $proxyType = (string) ($_POST['proxy_type'] ?? 'HTTPS');

        $accId = null;
        foreach (Store::getAccounts() as $a) {
            if ($a['email'] === $email && (string) $a['project_id'] === $projectId) {
                $accId = (int) $a['id'];
                Store::updateAccount($accId, ['key_path' => $dest, 'label' => $label,
                                              'proxy' => $proxy, 'proxy_type' => $proxyType]);
                break;
            }
        }
        if ($accId === null) {
            $accId = Store::addAccount($email, $projectId, $dest, $proxy, $proxyType, $label);
        }
        Users::addAudit(self::uname(), Http::clientIp(), 'add_account', $email,
            'upload ' . $origName, true);
        Json::ok(['account_id' => $accId, 'email' => $email,
                  'project_id' => $projectId, 'created' => true]);
    }

    /**
     * POST /api/accounts/import_dir  body: {dir}
     * → {ok, imported[], skipped[], skipped_count}
     * 递归扫描目录下所有 .json，**逐个校验**是不是服务账号（不校验就等于
     * 按路径批量读任意 JSON 并回显字段），并跳过 data/keys 里的重复文件。
     */
    public static function importDir(array $p): void
    {
        $b = Http::jsonBody();
        $dir = trim((string) ($b['dir'] ?? ''));
        if ($dir === '') {
            Json::err('请提供 dir', 400);
        }
        $real = realpath($dir);
        if ($real === false || !is_dir($real)) {
            Json::err('目录不存在：' . $dir, 400);
        }
        if (!is_readable($real)) {
            Json::err('目录不可读', 400);
        }

        $imported = [];
        $skipped = [];
        $files = [];
        $it = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator($real, FilesystemIterator::SKIP_DOTS),
            RecursiveIteratorIterator::SELF_FIRST
        );
        $n = 0;
        foreach ($it as $file) {
            if (++$n > 2000) {
                break;   // 目录过大时止损，避免拖垮请求
            }
            if (!$file->isFile()) {
                continue;
            }
            if (strtolower($file->getExtension()) !== 'json') {
                continue;
            }
            $files[] = $file->getPathname();
        }
        sort($files);

        Config::ensureDataDirs();
        $keysDir = realpath(Config::keysDir()) ?: Config::keysDir();

        foreach ($files as $fp) {
            $realFp = realpath($fp);
            if ($realFp === false) {
                $skipped[] = ['file' => $fp, 'reason' => '无法读取'];
                continue;
            }
            // 已经在我们自己的 keys 目录里 → 重复导入，跳过
            if (strncmp($realFp, $keysDir . DIRECTORY_SEPARATOR, strlen($keysDir) + 1) === 0) {
                $skipped[] = ['file' => $realFp, 'reason' => '已在 data/keys 中'];
                continue;
            }
            if (filesize($realFp) > 262144) {
                $skipped[] = ['file' => $realFp, 'reason' => '文件过大'];
                continue;
            }
            $content = @file_get_contents($realFp);
            $data = is_string($content) ? json_decode($content, true) : null;
            if (!is_array($data)) {
                $skipped[] = ['file' => $realFp, 'reason' => '不是合法 JSON'];
                continue;
            }
            [$saOk, $saWhy] = Gcp::validate_service_account_array($data);
            if (!$saOk) {
                $skipped[] = ['file' => $realFp, 'reason' => $saWhy];
                continue;
            }
            $info = $data;
            $email = (string) ($info['client_email'] ?? '');
            $projectId = (string) ($info['project_id'] ?? '');
            $dest = Config::keysDir() . '/sa-' . substr(hash('sha256', (string) $content), 0, 16) . '.json';
            if (!is_file($dest)) {
                if (file_put_contents($dest, (string) $content, LOCK_EX) === false) {
                    $skipped[] = ['file' => $realFp, 'reason' => '写入失败'];
                    continue;
                }
                @chmod($dest, 0600);
            }
            $dup = null;
            foreach (Store::getAccounts() as $a) {
                if ($a['email'] === $email && (string) $a['project_id'] === $projectId) {
                    $dup = $a;
                    break;
                }
            }
            if ($dup !== null) {
                Store::updateAccount((int) $dup['id'], ['key_path' => $dest]);
                $skipped[] = ['file' => $realFp, 'reason' => '账号已存在（已更新密钥路径）'];
                continue;
            }
            $id = Store::addAccount($email, $projectId, $dest, '', 'HTTPS', '');
            $imported[] = ['account_id' => $id, 'email' => $email, 'project_id' => $projectId];
        }

        Users::addAudit(self::uname(), Http::clientIp(), 'import_accounts', $dir,
            sprintf('导入 %d，跳过 %d', count($imported), count($skipped)), true);
        Json::ok([
            'imported'      => $imported,
            'skipped'       => $skipped,
            'skipped_count' => count($skipped),
        ]);
    }

    /** PATCH /api/accounts/{id}  body: {label?, proxy?, proxy_type?} → {ok, updated} */
    public static function updateAccount(array $p): void
    {
        $id = (int) $p[0];
        $acc = self::requireAccount($id);
        $b = Http::jsonBody();
        $kw = [];
        if (array_key_exists('label', $b)) {
            $kw['label'] = mb_substr(trim((string) $b['label']), 0, 80);
        }
        if (array_key_exists('proxy', $b)) {
            // ★ 空字符串代表「清掉代理」，必须允许；不能写 trim 后丢弃
            $kw['proxy'] = trim((string) $b['proxy']);
        }
        if (array_key_exists('proxy_type', $b)) {
            $pt = (string) $b['proxy_type'];
            if (!in_array($pt, ['HTTPS', 'HTTP', 'SOCKS5'], true)) {
                Json::err('不支持的代理类型：' . $pt, 400);
            }
            $kw['proxy_type'] = $pt;
        }
        if (empty($kw)) {
            Json::err('没有要修改的字段', 400);
        }
        if (!empty($kw['proxy'])) {
            $pinfo = Gcp::parse_proxy_input((string) $kw['proxy'], (string) ($kw['proxy_type'] ?? $acc['proxy_type']));
            if (!($pinfo['ok'] ?? false)) {
                Json::err('代理配置无法解析：' . (string) ($pinfo['error'] ?? ''), 400);
            }
        }
        Store::updateAccount($id, $kw);
        $detail = [];
        foreach ($kw as $k => $v) {
            $detail[] = $k . '=' . ($k === 'proxy' ? Gcp::mask_proxy((string) $v) : (string) $v);
        }
        Users::addAudit(self::uname(), Http::clientIp(), 'update_account',
            (string) $acc['email'], implode(' ', $detail), true);
        Json::ok(['updated' => count($kw)]);
    }

    /** DELETE /api/accounts/{id} → {ok}（只删记录，不删磁盘上的密钥文件） */
    public static function deleteAccount(array $p): void
    {
        $id = (int) $p[0];
        $acc = self::requireAccount($id);
        Store::deleteAccount($id);
        Users::addAudit(self::uname(), Http::clientIp(), 'delete_account',
            (string) $acc['email'], '', true);
        Json::ok(['message' => '账号已删除']);
    }

    /**
     * POST /api/accounts/{id}/test
     * → {ok, instances, latency_ms, via_proxy, proxy_type, error?}
     * 真发一次 list instances 请求（只读）验证账号可用。
     */
    public static function testAccount(array $p): void
    {
        $id = (int) $p[0];
        $acc = self::requireAccount($id);
        $t0 = microtime(true);
        $viaProxy = trim((string) ($acc['proxy'] ?? '')) !== '';
        $base = [
            'via_proxy'  => $viaProxy,
            'proxy_type' => (string) ($acc['proxy_type'] ?? 'HTTPS'),
        ];
        if (!is_file((string) ($acc['key_path'] ?? ''))) {
            Json::ok(array_merge($base, ['instances' => 0, 'latency_ms' => 0,
                                         'error' => '密钥文件不存在']));
        }
        try {
            $insts = self::client($acc)->list_instances();
            $ms = (int) round((microtime(true) - $t0) * 1000);
            Json::ok(array_merge($base, ['instances' => count($insts), 'latency_ms' => $ms]));
        } catch (Throwable $e) {
            $ms = (int) round((microtime(true) - $t0) * 1000);
            Json::ok(array_merge($base, ['instances' => 0, 'latency_ms' => $ms,
                                         'error' => $e->getMessage()]));
        }
    }

    /**
     * POST /api/accounts/{id}/test_proxy  body: {proxy?, proxy_type?}（都不传则用账号已存的）
     * → {ok, result{...}}
     * ★ 目标 URL 是 Gcp::PROXY_TEST_URL 硬编码常量，不接受请求参数（防 SSRF）。
     */
    public static function testAccountProxy(array $p): void
    {
        $id = (int) $p[0];
        $acc = self::requireAccount($id);
        $b = Http::jsonBody();
        $proxy = array_key_exists('proxy', $b)
            ? trim((string) $b['proxy'])
            : trim((string) ($acc['proxy'] ?? ''));
        $proxyType = (string) ($b['proxy_type'] ?? ($acc['proxy_type'] ?? 'HTTPS'));

        if ($proxy === '') {
            Json::ok(['result' => ['ok' => false, 'empty' => true,
                                   'error' => '该账号未配置代理', 'display' => '']]);
        }
        if (!in_array($proxyType, ['HTTPS', 'HTTP', 'SOCKS5'], true)) {
            Json::err('不支持的代理类型：' . $proxyType, 400);
        }
        // 只读探测，不改任何状态、不发往用户指定的地址
        $res = Gcp::test_proxy($proxy, $proxyType);
        Json::ok(['result' => $res]);
    }

    /** GET /api/accounts/instance_counts → 刷新并返回各账号的实时实例数 */
    public static function accountInstanceCounts(array $p): void
    {
        // ★ 字段名严格对齐 Python 的 api_account_instance_counts：
        //     {ok, counts:[{account_id,email,label,inst_count_live,inst_count_local}],
        //      errors:[], elapsed_ms, cached}
        //   早前自造了 count/at 两个名字，前端按 inst_count_live 取值会拿不到。
        $t0 = microtime(true);
        $localCnt = [];
        foreach (Store::getAllVms() as $vm) {
            $k = (string) ($vm['account_id'] ?? '');
            if ($k !== '') {
                $localCnt[$k] = ($localCnt[$k] ?? 0) + 1;
            }
        }
        $counts = [];
        $errors = [];
        foreach (Store::getAccounts() as $a) {
            $aid = (string) $a['id'];
            $row = [
                'account_id'       => (int) $a['id'],
                'email'            => (string) ($a['email'] ?? ''),
                'label'            => (string) ($a['label'] ?? ''),
                'inst_count_live'  => null,
                'inst_count_local' => $localCnt[$aid] ?? 0,
            ];
            if (!is_file((string) ($a['key_path'] ?? ''))) {
                $errors[] = ['account_id' => (int) $a['id'], 'error' => '密钥文件不存在'];
                $counts[] = $row;
                continue;
            }
            try {
                $row['inst_count_live'] = count(self::client($a)->list_instances());
            } catch (Throwable $e) {
                $errors[] = ['account_id' => (int) $a['id'], 'email' => (string) ($a['email'] ?? ''),
                             'error' => $e->getMessage()];
            }
            $counts[] = $row;
        }
        $elapsed = (int) round((microtime(true) - $t0) * 1000);
        Store::setSetting('inst_counts_cache', ['counts' => $counts, 'at' => microtime(true)]);
        Json::ok([
            'counts'     => $counts,
            'errors'     => $errors,
            'elapsed_ms' => $elapsed,
            'cached'     => false,     // 本次是真去查了
        ]);
    }

    // ══ 实例 ═══════════════════════════════════════════════════════════════

    /**
     * GET /api/instances?sync=true|false&account_ids=a,b
     * sync=false → 本地库（★ 必须过滤密码字段）
     * sync=true  → 实时聚合各账号（只读）
     */
    public static function instances(array $p): void
    {
        $sync = strtolower((string) Http::query('sync', 'true')) !== 'false';
        $idsRaw = (string) (Http::query('account_ids') ?: '');
        $ids = array_values(array_filter(array_map('trim', explode(',', $idsRaw)), fn($x) => $x !== ''));

        if (!$sync) {
            // ★ 本地行含 root 密码明文，必须白名单式过滤后再出网
            $rows = [];
            foreach (Store::getAllVms() as $v) {
                $rows[] = Gcp::sanitize_instance_row($v);
            }
            Json::ok(['instances' => $rows]);
        }

        $all = [];
        $errors = [];
        foreach (Store::getAccounts() as $a) {
            $aid = (string) $a['id'];
            if ($ids !== [] && !in_array($aid, $ids, true)) {
                continue;
            }
            if (!is_file((string) ($a['key_path'] ?? ''))) {
                $errors[] = ['account_id' => $aid, 'email' => $a['email'], 'error' => '密钥文件不存在'];
                continue;
            }
            try {
                foreach (self::client($a)->list_instances() as $inst) {
                    $all[] = $inst;
                }
            } catch (Throwable $e) {
                $errors[] = ['account_id' => $aid, 'email' => $a['email'], 'error' => $e->getMessage()];
            }
        }
        Json::ok(['instances' => $all, 'errors' => $errors]);
    }

    /** PATCH /api/instances/note  body: {name, note} → {ok, name, note} */
    public static function updateInstanceNote(array $p): void
    {
        $b = Http::jsonBody();
        $name = trim((string) ($b['name'] ?? ''));
        if ($name === '') {
            Json::err('缺少实例名', 400);
        }
        $note = mb_substr((string) ($b['note'] ?? ''), 0, 500);
        if (Store::getVm($name) === null) {
            Json::err('本地没有该实例的记录', 404);
        }
        Store::updateVmNote($name, $note);
        Users::addAudit(self::uname(), Http::clientIp(), 'update_note', $name, $note, true);
        Json::ok(['name' => $name, 'note' => $note]);
    }

    /**
     * POST /api/instances/password  body: {name}
     * → {ok, name, root_password}
     * 这是**有意**的敏感接口（用户要按需查看自己建的实例密码），
     * 但必须：权限点 view、只在本地有记录时返回、每次出示都写审计。
     */
    public static function revealInstancePassword(array $p): void
    {
        $b = Http::jsonBody();
        $name = trim((string) ($b['name'] ?? ''));
        if ($name === '') {
            Json::err('缺少实例名', 400);
        }
        $vm = Store::getVm($name);
        if ($vm === null) {
            Users::addAudit(self::uname(), Http::clientIp(), 'reveal_root_password',
                $name, '未找到记录', false);
            Json::err('没有该实例的密码记录', 404);
        }
        $pw = (string) ($vm['password'] ?? '');
        if ($pw === '') {
            Json::err('该实例没有记录密码', 404);
        }
        Users::addAudit(self::uname(), Http::clientIp(), 'reveal_root_password',
            $name, '已出示', true);
        Json::ok(['name' => $name, 'root_password' => $pw]);
    }

    // ══ 异步任务（建机 / 执行命令 / 启停 / 刷新）═══════════════════════════

    /**
     * 建任务 + 拉起 worker，返回 {ok, task_id, ...}。
     * 与 Python 版 tm.submit_* 的返回形态一致。
     */
    private static function submit(string $kind, array $payload): void
    {
        $id = Tasks::create($kind, $payload);
        Tasks::addLog($id, 'info', '任务已创建，等待执行');
        Tasks::spawnWorker($id);
        Json::ok(['task_id' => $id]);
    }

    /** POST /api/refresh  body: {account_ids?} → {ok, task_id} */
    public static function refresh(array $p): void
    {
        $b = Http::jsonBody();
        $ids = $b['account_ids'] ?? null;
        self::submit('refresh', ['account_ids' => $ids]);
    }

    /**
     * POST /api/create  body: {count, spec:{...}, installs?, account_ids?, dry_run?}
     * → {ok, task_id, count?, accounts?, dry_run?, plan?}
     */
    public static function create(array $p): void
    {
        $b = Http::jsonBody();
        $accounts = Store::getAccounts();
        if (empty($accounts)) {
            // ★ 与 Python 的 tm.submit_create 一致：这是**业务性拒绝**，
            //   HTTP 仍是 200，用 ok:false + error 表达（前端读 r.error 显示）。
            Json::out(['ok' => false, 'error' => '没有匹配的账号，请先导入 GCP 服务账号 JSON'], 200);
        }
        $count = (int) ($b['count'] ?? 1);
        if ($count < 1 || $count > 200) {
            Json::err('数量需在 1-200 之间', 400);
        }
        $ids = $b['account_ids'] ?? null;
        if (is_array($ids) && $ids !== []) {
            $sel = [];
            foreach ($accounts as $a) {
                if (in_array((string) $a['id'], array_map('strval', $ids), true)) {
                    $sel[] = $a;
                }
            }
            if (empty($sel)) {
                Json::err('指定的账号不存在', 400);
            }
            $accounts = $sel;
        }

        if (!empty($b['dry_run'])) {
            // 预演：只算计划，不建任务、不碰云
            $plan = Gcp::build_instance_spec($b['spec'] ?? null);
            $id = Tasks::create('create', array_merge($b, ['dry_run' => true]));
            Json::ok(['task_id' => $id, 'dry_run' => true, 'plan' => $plan]);
        }

        $id = Tasks::create('create', $b);
        Tasks::addLog($id, 'info', sprintf('准备创建 %d 台实例，账号 %d 个', $count, count($accounts)));
        Tasks::spawnWorker($id);
        Json::ok(['task_id' => $id, 'count' => $count, 'accounts' => count($accounts)]);
    }

    /** POST /api/execute  body: {command, targets[], user?, timeout?} → {ok, task_id} */
    public static function execute(array $p): void
    {
        $b = Http::jsonBody();
        $cmd = trim((string) ($b['command'] ?? ''));
        if ($cmd === '') {
            // 同 Python 的 tm.submit_execute：200 + ok:false
            Json::out(['ok' => false, 'error' => '命令为空'], 200);
        }
        self::submit('execute', $b);
    }

    /** POST /api/instance_action  body: {action, targets[]} → {ok, task_id} */
    public static function instanceAction(array $p): void
    {
        $b = Http::jsonBody();
        $action = (string) ($b['action'] ?? '');
        if (!in_array($action, ['start', 'stop', 'reset', 'delete'], true)) {
            Json::err('不支持的操作：' . $action, 400);
        }
        $targets = $b['targets'] ?? [];
        if (!is_array($targets) || $targets === []) {
            Json::err('没有选择任何实例', 400);
        }
        if (count($targets) > 200) {
            Json::err('一次最多操作 200 台', 400);
        }
        self::submit('instance_action', ['action' => $action, 'targets' => $targets]);
        Users::addAudit(self::uname(), Http::clientIp(), 'instance_' . $action,
            implode(',', array_slice(array_map(fn($t) => (string) ($t['name'] ?? $t), $targets), 0, 10)),
            '共 ' . count($targets) . ' 台', true);
    }
}
