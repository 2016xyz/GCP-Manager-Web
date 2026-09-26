<?php
/**
 * Store —— 通用本地存储层（accounts / vm_passwords / tasks / logs / settings）
 *
 * 与 Python 版 core/store.py 的关系
 *   本类是 core/store.py 中 `class Store` 的逐方法移植，**表结构与列名逐列一致**
 *   （见 php/INTERFACES.md 第 4 节），因此 PHP 版与 Python 版可以共用同一个
 *   SQLite 文件而互不破坏对方的数据。
 *
 * 关键取舍
 *   · 并发：Python 是单进程多线程（threading.RLock 串行化）。PHP-FPM 是多进程，
 *     进程间没有共享内存锁，所以这里不模拟线程锁，统一交给 SQLite：
 *       WAL + busy_timeout=5000（Db.php 已设置）+ 写操作走 BEGIN IMMEDIATE 事务。
 *     单个写事务必须短小（毫秒级），禁止在事务里做网络/磁盘等待。
 *   · 表结构：本类**只**建 accounts / vm_passwords / tasks / logs / settings
 *     五张表（契约要求）。users/sessions/audit 由 Users.php 负责，
 *     captcha / login_guard 由 Auth.php 负责，各建各的互不越界。
 *   · 增量迁移：用 ALTER TABLE 逐列补齐（幂等），老库升级不丢数据 ——
 *     vm_passwords 里存着 root 密码，搬表一旦中断风险太高，故绝不重建表。
 *   · 所有 SQL 一律 PDO 预处理；可写列名只来自硬编码白名单，绝不把用户输入
 *     当成标识符（契约红线 S2）。
 */

declare(strict_types=1);

final class Store
{
    /** 增量迁移清单：(表名, 列名, 列定义)。全部为硬编码字面量，避免注入。 */
    private const MIGRATIONS = [
        // 实例备注：创建时可填，建完可改
        ['vm_passwords', 'note', "TEXT DEFAULT ''"],
        // 实例真实创建时间（取自 GCP creation_timestamp），用于算「已用费用」
        ['vm_passwords', 'created_at', 'REAL'],
        // 会话建立后的首次记录时间，作为 created_at 取不到时的兜底
        ['vm_passwords', 'first_seen', 'REAL'],
        // 创建后自动执行的安装项（逗号分隔的预设 key）
        ['vm_passwords', 'installs', "TEXT DEFAULT ''"],
        // 账号备注（accounts 表里 label 字段早已存在，这里只为老库兜底）
        ['accounts', 'label', "TEXT DEFAULT ''"],
    ];

    // ------------------------------------------------------------------
    // 建表 / 迁移
    // ------------------------------------------------------------------
    /** 幂等的初始化入口（可被外部显式调用；各方法内部也会惰性触发一次） */
    public static function ensure(): void
    {
        self::initSchema();
    }

    private static function initSchema(): void
    {
        Db::exec("CREATE TABLE IF NOT EXISTS accounts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT, project_id TEXT, key_path TEXT,
            proxy TEXT DEFAULT '', proxy_type TEXT DEFAULT 'HTTPS',
            label TEXT DEFAULT '', created_at REAL)");

        Db::exec("CREATE TABLE IF NOT EXISTS vm_passwords(
            name TEXT PRIMARY KEY, ip TEXT, password TEXT, account_id TEXT,
            zone TEXT, machine_type TEXT, image_key TEXT, disk_type TEXT,
            disk_size_gb INTEGER, updated_at REAL)");

        Db::exec("CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY, kind TEXT, status TEXT, payload TEXT,
            result TEXT, message TEXT, created_at REAL, updated_at REAL)");

        Db::exec("CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id TEXT, level TEXT, message TEXT)");

        Db::exec('CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)');

        self::migrate();
    }

    /** 惰性初始化：任何公开方法调用时保证表已存在（同请求内只跑一次） */
    private static function boot(): void
    {
        static $done = false;
        if ($done) {
            return;
        }
        $done = true;
        self::initSchema();
    }

    /** 补齐后加字段；已存在则跳过（幂等）。并发启动时另一进程可能刚好加过列 → 忽略异常。 */
    private static function migrate(): void
    {
        foreach (self::MIGRATIONS as [$table, $column, $decl]) {
            try {
                $cols = Db::all('PRAGMA table_info(' . $table . ')');
                if ($cols === []) {          // 表不存在（理论上不会）
                    continue;
                }
                $have = array_map(static fn(array $r): string => (string) $r['name'], $cols);
                if (!in_array($column, $have, true)) {
                    // 表名/列名/定义都来自上面的硬编码常量，不存在注入面
                    Db::exec('ALTER TABLE ' . $table . ' ADD COLUMN ' . $column . ' ' . $decl);
                }
            } catch (PDOException $e) {
                // 并发启动时重复加列 → duplicate column name，忽略即可
            }
        }
    }

    // ------------------------------------------------------------------
    // accounts
    // ------------------------------------------------------------------
    public static function addAccount(string $email, string $projectId, string $keyPath,
                                      string $proxy = '', string $proxyType = 'HTTPS',
                                      string $label = ''): int
    {
        self::boot();
        Db::exec(
            'INSERT INTO accounts(email,project_id,key_path,proxy,proxy_type,label,created_at)'
            . ' VALUES(?,?,?,?,?,?,?)',
            [$email, $projectId, $keyPath, $proxy, $proxyType, $label, microtime(true)]
        );
        return (int) Db::conn()->lastInsertId();
    }

    /** 按 (key_path, project_id) 判重插入；返回 [id, created(bool)] */
    public static function upsertAccountByKey(string $email, string $projectId, string $keyPath,
                                              string $proxy = '', string $proxyType = 'HTTPS',
                                              string $label = ''): array
    {
        self::boot();
        return Db::tx(static function (PDO $pdo) use ($email, $projectId, $keyPath, $proxy, $proxyType, $label): array {
            $st = $pdo->prepare('SELECT id FROM accounts WHERE key_path=? AND project_id=?');
            $st->execute([$keyPath, $projectId]);
            $row = $st->fetch();
            if ($row !== false) {
                return [(int) $row['id'], false];
            }
            $ins = $pdo->prepare(
                'INSERT INTO accounts(email,project_id,key_path,proxy,proxy_type,label,created_at)'
                . ' VALUES(?,?,?,?,?,?,?)'
            );
            $ins->execute([$email, $projectId, $keyPath, $proxy, $proxyType, $label, microtime(true)]);
            return [(int) $pdo->lastInsertId(), true];
        });
    }

    public static function deleteAccount(int $accId): void
    {
        self::boot();
        Db::exec('DELETE FROM accounts WHERE id=?', [$accId]);
    }

    /** 只更新传入的字段（列名白名单硬编码） */
    public static function updateAccount(int $accId, array $kw): void
    {
        self::boot();
        $allowed = ['email', 'project_id', 'key_path', 'proxy', 'proxy_type', 'label'];
        $fields = array_values(array_filter($allowed, static fn(string $k): bool => array_key_exists($k, $kw)));
        if ($fields === []) {
            return;
        }
        $sets = implode(',', array_map(static fn(string $f): string => $f . '=?', $fields));
        $args = array_map(static fn(string $f) => $kw[$f], $fields);
        $args[] = $accId;
        Db::exec('UPDATE accounts SET ' . $sets . ' WHERE id=?', $args);
    }

    public static function getAccounts(): array
    {
        self::boot();
        return Db::all('SELECT * FROM accounts ORDER BY id');
    }

    public static function getAccount(int $accId): ?array
    {
        self::boot();
        return Db::one('SELECT * FROM accounts WHERE id=?', [$accId]);
    }

    // ------------------------------------------------------------------
    // vm_passwords
    // ------------------------------------------------------------------
    /**
     * 写入 / 更新实例记录。
     *
     * note / installs / created_at 为 null 时**不覆盖已有值** ——
     * 创建流程里 saveVm 会被调用多次（创建后、命令执行后各一次），
     * 若每次都写 NULL，会把用户填的备注和 GCP 返回的创建时间冲掉。
     */
    public static function saveVm(string $name, ?string $ip, ?string $password,
                                  $accountId = null, ?string $zone = null, ?string $machineType = null,
                                  ?string $imageKey = null, ?string $diskType = null,
                                  ?int $diskSizeGb = null, ?string $note = null,
                                  $createdAt = null, ?string $installs = null): void
    {
        self::boot();
        Db::tx(static function (PDO $pdo) use (
            $name, $ip, $password, $accountId, $zone, $machineType, $imageKey, $diskType,
            $diskSizeGb, $note, $createdAt, $installs
        ): void {
            $now = microtime(true);
            $st = $pdo->prepare('SELECT note, installs, created_at, first_seen FROM vm_passwords WHERE name=?');
            $st->execute([$name]);
            $old = $st->fetch();
            $old = $old === false ? [] : $old;

            // created_at：优先用调用方给的（GCP 真实创建时间）；没给就保留原值；
            // 从没记过才用当前时间兜底。
            if ($createdAt === null) {
                $createdAt = (!empty($old['created_at'])) ? $old['created_at'] : $now;
            }
            $oldNote     = array_key_exists('note', $old) ? $old['note'] : '';
            $oldInstalls = array_key_exists('installs', $old) ? $old['installs'] : '';
            $firstSeen   = !empty($old['first_seen']) ? $old['first_seen'] : $now;

            $up = $pdo->prepare(
                "INSERT INTO vm_passwords(name,ip,password,account_id,zone,machine_type,
                   image_key,disk_type,disk_size_gb,note,installs,created_at,first_seen,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET ip=excluded.ip, password=excluded.password,
                   account_id=excluded.account_id, zone=excluded.zone,
                   machine_type=excluded.machine_type, image_key=excluded.image_key,
                   disk_type=excluded.disk_type, disk_size_gb=excluded.disk_size_gb,
                   note=excluded.note, installs=excluded.installs,
                   created_at=excluded.created_at, updated_at=excluded.updated_at"
            );
            $up->execute([
                $name, $ip, $password,
                $accountId === null ? null : (string) $accountId,
                $zone, $machineType, $imageKey, $diskType, $diskSizeGb,
                $note === null ? $oldNote : $note,
                $installs === null ? $oldInstalls : $installs,
                $createdAt, $firstSeen, $now,
            ]);
        });
    }

    /** 单独改备注；返回是否命中记录（不能用累计的 total_changes） */
    public static function updateVmNote(string $name, ?string $note): bool
    {
        self::boot();
        return Db::exec(
            'UPDATE vm_passwords SET note=?, updated_at=? WHERE name=?',
            [$note ?? '', microtime(true), $name]
        ) > 0;
    }

    public static function updateVmInstalls(string $name, ?string $installs): void
    {
        self::boot();
        Db::exec(
            'UPDATE vm_passwords SET installs=?, updated_at=? WHERE name=?',
            [$installs ?? '', microtime(true), $name]
        );
    }

    public static function updateVmPassword(string $name, ?string $password): void
    {
        self::boot();
        Db::exec(
            'UPDATE vm_passwords SET password=?, updated_at=? WHERE name=?',
            [$password ?? '', microtime(true), $name]
        );
    }

    public static function forgetVm(string $name): void
    {
        self::boot();
        Db::exec('DELETE FROM vm_passwords WHERE name=?', [$name]);
    }

    public static function getVm(string $name): ?array
    {
        self::boot();
        return Db::one('SELECT * FROM vm_passwords WHERE name=?', [$name]);
    }

    public static function getAllVms(): array
    {
        self::boot();
        return Db::all('SELECT * FROM vm_passwords');
    }

    // ------------------------------------------------------------------
    // tasks
    // ------------------------------------------------------------------
    public static function createTask(string $taskId, string $kind, $payload = null): void
    {
        self::boot();
        $now = microtime(true);
        $json = json_encode($payload === null ? [] : $payload,
            JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        if ($json === false) {
            $json = '{}';
        }
        Db::exec(
            'INSERT INTO tasks(id,kind,status,payload,result,message,created_at,updated_at)'
            . ' VALUES(?,?,?,?,?,?,?,?)',
            [$taskId, $kind, 'queued', $json, '', '', $now, $now]
        );
    }

    /** 只更新传入（非 null）的字段；result 非字符串时自动 JSON 编码 */
    public static function updateTask(string $taskId, array $kw): void
    {
        self::boot();
        $allowed = ['status', 'result', 'message'];
        $sets = [];
        $args = [];
        foreach ($allowed as $f) {
            if (!array_key_exists($f, $kw)) {
                continue;
            }
            $v = $kw[$f];
            if ($f === 'result' && $v !== null && !is_string($v)) {
                $v = json_encode($v, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
            }
            $sets[] = $f . '=?';
            $args[] = $v;
        }
        if ($sets === []) {
            return;
        }
        $sets[] = 'updated_at=?';
        $args[] = microtime(true);
        $args[] = $taskId;
        Db::exec('UPDATE tasks SET ' . implode(',', $sets) . ' WHERE id=?', $args);
    }

    public static function getTask(string $taskId): ?array
    {
        self::boot();
        $r = Db::one('SELECT * FROM tasks WHERE id=?', [$taskId]);
        return $r === null ? null : self::decodeTaskRow($r);
    }

    public static function getTasks(int $limit = 200, ?string $kind = null): array
    {
        self::boot();
        $limit = max(0, $limit);
        if ($kind !== null && $kind !== '') {
            $rows = Db::all(
                'SELECT * FROM tasks WHERE kind=? ORDER BY created_at DESC LIMIT ?',
                [$kind, $limit]
            );
        } else {
            $rows = Db::all('SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?', [$limit]);
        }
        return array_map([self::class, 'decodeTaskRow'], $rows);
    }

    /** 把 DB 行转成与 Python _task_row 一致的形态（payload→对象，result→任意/null） */
    private static function decodeTaskRow(array $r): array
    {
        foreach (['payload', 'result'] as $f) {
            $raw = $r[$f] ?? null;
            if ($raw === null || $raw === '') {
                $r[$f] = ($f === 'payload') ? [] : null;
                continue;
            }
            try {
                $r[$f] = json_decode((string) $raw, true, 64, JSON_THROW_ON_ERROR);
            } catch (JsonException $e) {
                // 解码失败时保留原字符串，别让整行不可用（与 Python 的 except: pass 同义）
            }
        }
        return $r;
    }

    // ------------------------------------------------------------------
    // logs
    // ------------------------------------------------------------------
    public static function addLog(string $message, ?string $taskId = null, string $level = 'info'): void
    {
        self::boot();
        Db::exec(
            'INSERT INTO logs(ts,task_id,level,message) VALUES(?,?,?,?)',
            [microtime(true), $taskId, $level, $message]
        );
    }

    public static function getLogs(int $sinceId = 0, int $limit = 500, ?string $taskId = null): array
    {
        self::boot();
        $limit = max(0, $limit);
        if ($taskId !== null && $taskId !== '') {
            return Db::all(
                'SELECT * FROM logs WHERE id>? AND task_id=? ORDER BY id LIMIT ?',
                [$sinceId, $taskId, $limit]
            );
        }
        return Db::all('SELECT * FROM logs WHERE id>? ORDER BY id LIMIT ?', [$sinceId, $limit]);
    }

    public static function clearLogs(): void
    {
        self::boot();
        Db::exec('DELETE FROM logs');
    }

    // ------------------------------------------------------------------
    // settings（值统一 JSON 编码存储，与 Python set_setting 一致）
    // ------------------------------------------------------------------
    public static function setSetting(string $k, $v): void
    {
        self::boot();
        $json = json_encode($v, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        if ($json === false) {
            $json = 'null';
        }
        Db::exec(
            'INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
            [$k, $json]
        );
    }

    /** 取设置；不存在或值不是合法 JSON 时返回 $default（与 Python get_setting 一致） */
    public static function getSetting(string $k, $default = null)
    {
        self::boot();
        $v = Db::scalar('SELECT v FROM settings WHERE k=?', [$k]);
        if ($v === false || $v === null) {
            return $default;
        }
        try {
            return json_decode((string) $v, true, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException $e) {
            return $default;
        }
    }

    /** 取全部设置；解码失败的行回退为原始字符串（与 Python get_all_settings 一致） */
    public static function getAllSettings(): array
    {
        self::boot();
        $out = [];
        foreach (Db::all('SELECT k,v FROM settings') as $r) {
            try {
                $out[(string) $r['k']] = json_decode((string) $r['v'], true, 512, JSON_THROW_ON_ERROR);
            } catch (JsonException $e) {
                $out[(string) $r['k']] = $r['v'];
            }
        }
        return $out;
    }
}
