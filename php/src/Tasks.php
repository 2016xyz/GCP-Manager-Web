<?php
/**
 * Tasks —— 后台任务执行层（落库 + 领取 + 执行 + 终态兜底）
 *
 * 与 Python 版的关系
 *   Python 是「单进程 + 内存态 api_tasks 字典 + 后台线程」：任务同时存在于
 *   内存（api_tasks）和 SQLite（tasks 表）。PHP-FPM 是**多进程**，没有共享内存，
 *   所以这里把 SQLite 的 tasks 表当作**唯一真源**，不再维护内存态字典
 *   （api_tasks_snapshot 与 db_tasks 在 PHP 侧合并为同一份数据，
 *    天然不会出现「内存里有、DB 里没有」的不一致）。
 *
 * 执行模型（关键取舍）
 *   HTTP 层只负责 Tasks::create() 把任务写成 status='queued'；
 *   真正的执行由 bin/task-runner.php（可被宝塔「计划任务」每分钟 / 常驻）领取执行。
 *   这样避免了 PHP-FPM 请求生命周期结束后无法后台跑任务的限制。
 *
 * ★ 终态兜底（Python 版曾出现「任务静默卡在 running」的真实故障）
 *   任何执行路径都必须把任务落到 done / failed / cancelled 之一：
 *     · task-runner 用 try/catch/finally 包住执行体，并在 finally 里强制收敛状态；
 *     · 额外提供 failStale()：把 updated_at 超期仍停在 running 的任务判为 failed
 *       （覆盖「runner 进程被杀 / 机器重启」导致的中断）。
 *
 * 安全 / 正确性
 *   · 所有 SQL 一律 PDO 预处理，绝不把值拼进 SQL 字符串；
 *   · 可写列名只来自硬编码白名单；
 *   · 日志表有总量上限，防止无限增长拖垮磁盘与 /api/logs 查询。
 */

declare(strict_types=1);

final class Tasks
{
    /** 日志表保留的最大行数（超过即从最旧的开始裁剪） */
    public const LOG_MAX = 50000;

    /** running 超过该秒数仍未更新，视为中断并兜底为 failed（默认 30 分钟） */
    public const STALE_SECONDS = 1800;

    /** 允许写入的列名白名单（防止列名被污染） */
    private const UPDATABLE = ['status', 'message', 'result'];

    /** 处于终态的状态集合 —— 落到这里就表示任务已经「有结论」 */
    public const TERMINAL = ['done', 'failed', 'cancelled'];

    // ------------------------------------------------------------------
    // 任务
    // ------------------------------------------------------------------
    /**
     * 建任务（只落库，状态 queued，由 task-runner 领取执行）。
     * @return string 任务 id
     */
    public static function create(string $kind, array $payload = []): string
    {
        $kind = trim($kind) !== '' ? trim($kind) : 'task';
        $id = self::newTaskId($kind);
        $now = microtime(true);
        $json = json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        if ($json === false) {
            $json = '{}';
        }
        Db::exec(
            'INSERT INTO tasks(id,kind,status,payload,result,message,created_at,updated_at)'
            . ' VALUES(?,?,?,?,?,?,?,?)',
            [$id, $kind, 'queued', $json, '', '', $now, $now]
        );
        return $id;
    }

    /**
     * 后台拉起 worker 执行指定任务（不阻塞当前 HTTP 请求）。
     *
     * 为什么用 proc_open 数组形式而不是 exec("... &")：
     *   · 数组形式不经过 shell，参数里的任何字符都不会被解释成命令分隔符 ——
     *     即使任务 id 里混进 `;` 也只会被当成一个普通参数（双重保险，id 本身
     *     也已在 newTaskId 里限定为字母数字）。
     *   · shell 里拼 `&` 还得自己处理重定向与文件描述符，出错时容易留下僵尸；
     *     这里显式把三个标准流都重定向到 /dev/null 并立即释放句柄。
     *
     * 失败时**不抛异常**：worker 拉不起来不应该让 HTTP 请求 500 ——
     * 任务已经落库为 queued，宝塔的计划任务（每分钟 --once）会兜底领取执行。
     * 因此这里只记日志。
     *
     * @return bool 是否成功拉起（false 表示交给计划任务兜底）
     */
    public static function spawnWorker(string $taskId = ''): bool
    {
        if (!function_exists('proc_open')) {
            // 宝塔默认禁用 proc_open；此时靠计划任务兜底，功能仍可用
            error_log('[gcpweb] proc_open 不可用，任务将由计划任务兜底执行');
            return false;
        }
        $php = PHP_BINARY;
        $script = dirname(__DIR__) . '/bin/task-runner.php';
        if (!is_file($script)) {
            error_log('[gcpweb] 找不到 task-runner.php：' . $script);
            return false;
        }

        $cmd = $taskId !== ''
            ? [$php, $script, '--task=' . $taskId]
            : [$php, $script, '--once'];

        $devNull = DIRECTORY_SEPARATOR === '\\' ? 'NUL' : '/dev/null';
        $descriptors = [
            0 => ['file', $devNull, 'r'],
            1 => ['file', $devNull, 'w'],
            2 => ['file', $devNull, 'w'],
        ];
        try {
            $proc = @proc_open($cmd, $descriptors, $pipes, dirname(__DIR__));
            if (!is_resource($proc)) {
                error_log('[gcpweb] proc_open 拉起 worker 失败');
                return false;
            }
            // 不等待：立即关闭并让子进程自己跑完。
            // 不调 proc_close（那会阻塞到子进程结束，等于把异步变同步），
            // 只 close 管道句柄，回收交给系统。
            foreach ($pipes as $pipe) {
                if (is_resource($pipe)) {
                    fclose($pipe);
                }
            }
            return true;
        } catch (Throwable $e) {
            error_log('[gcpweb] 拉起 worker 异常：' . $e->getMessage());
            return false;
        }
    }

    /** 取单个任务（payload/result 已解码，字段与 Python get_task 一致） */
    public static function get(string $id): ?array
    {
        $row = Db::one('SELECT * FROM tasks WHERE id=?', [$id]);
        return $row === null ? null : self::decodeRow($row);
    }

    /** 任务列表（按创建时间倒序，limit 夹在 1..1000） */
    public static function list(int $limit = 100, ?string $kind = null): array
    {
        $limit = max(1, min($limit, 1000));
        if ($kind !== null && $kind !== '') {
            $rows = Db::all(
                'SELECT * FROM tasks WHERE kind=? ORDER BY created_at DESC LIMIT ?',
                [$kind, $limit]
            );
        } else {
            $rows = Db::all('SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?', [$limit]);
        }
        return array_map([self::class, 'decodeRow'], $rows);
    }

    /**
     * 原子领取下一个排队任务：置为 running 并返回该行；无任务返回 null。
     *
     * 用 BEGIN IMMEDIATE 立刻取写锁，配合「先查后改」在同事务内完成，
     * 多进程并发领取时不会重复拿到同一个任务（rowCount 校验兜底）。
     */
    public static function claimNext(): ?array
    {
        $id = Db::tx(static function (PDO $pdo): ?string {
            $st = $pdo->prepare("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at ASC, rowid ASC LIMIT 1");
            $st->execute();
            $row = $st->fetch();
            if ($row === false) {
                return null;
            }
            $candidate = (string) $row['id'];
            $up = $pdo->prepare("UPDATE tasks SET status='running', updated_at=? WHERE id=? AND status='queued'");
            $up->execute([microtime(true), $candidate]);
            return $up->rowCount() === 1 ? $candidate : null;
        });
        return $id === null ? null : self::get($id);
    }

    /**
     * 更新任务。只更新传入（非 null）的字段，与 Python update_task(**kw) 同义。
     * $result 为数组时自动 JSON 编码，与 Python 行为一致。
     */
    public static function update(string $id, ?string $status = null, ?string $message = null, $result = null): void
    {
        $sets = [];
        $args = [];
        if ($status !== null) {
            $sets[] = 'status=?';
            $args[] = $status;
        }
        if ($message !== null) {
            $sets[] = 'message=?';
            $args[] = $message;
        }
        if ($result !== null) {
            $sets[] = 'result=?';
            $args[] = is_string($result)
                ? $result
                : (string) json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        }
        if ($sets === []) {
            return;
        }
        $sets[] = 'updated_at=?';
        $args[] = microtime(true);
        $args[] = $id;
        Db::exec('UPDATE tasks SET ' . implode(',', $sets) . ' WHERE id=?', $args);
    }

    /**
     * 请求取消任务。
     *   · 尚未被领取（queued）→ 直接落终态 cancelled；
     *   · 正在执行（running）→ 写取消标志，由 task-runner 在执行循环里检测并停止；
     *   · 已终态 → 幂等返回 true。
     * 跨进程取消标志用 settings 表存（key = task_cancel:<id>），因为 PHP 没有
     * 可共享的内存标志位。
     */
    public static function cancel(string $id): bool
    {
        $task = self::get($id);
        if ($task === null) {
            return false;
        }
        self::setFlag($id, true);
        if (!in_array((string) $task['status'], self::TERMINAL, true)) {
            if ($task['status'] === 'queued') {
                self::update($id, 'cancelled', '已请求取消');
            } else {
                self::update($id, null, '已请求取消，正在停止…');
            }
        }
        self::addLog($id, 'warn', '[' . $id . '] 已请求取消');
        return true;
    }

    /** 执行侧查询：是否已请求取消 */
    public static function cancelRequested(string $id): bool
    {
        return self::getFlag($id);
    }

    /**
     * 终态兜底：把长时间停在 running 的任务判为 failed。
     * 用于我们无法感知 runner 被强杀的场景。
     * @return int 被兜底的任务数
     */
    public static function failStale(int $maxAgeSeconds = self::STALE_SECONDS): int
    {
        $cutoff = microtime(true) - max(60, $maxAgeSeconds);
        return Db::exec(
            "UPDATE tasks SET status='failed', message=?, updated_at=? "
            . "WHERE status='running' AND updated_at < ?",
            ['任务未正常结束（进程中断或超时，已自动兜底）', microtime(true), $cutoff]
        );
    }

    /**
     * 确保指定任务最终处于终态：若仍停在 queued/running，则按 needFailed
     * 收敛为 failed 或 cancelled。供 task-runner 的 finally 段落调用。
     */
    public static function ensureTerminal(string $id, string $reason = '任务未正常结束'): void
    {
        $t = self::get($id);
        if ($t === null) {
            return;
        }
        $st = (string) $t['status'];
        if (in_array($st, self::TERMINAL, true)) {
            return;
        }
        $final = self::cancelRequested($id) ? 'cancelled' : 'failed';
        self::update($id, $final, $reason);
    }

    // ------------------------------------------------------------------
    // 日志
    // ------------------------------------------------------------------
    /**
     * 写一条日志。task_id 可为空串（全局日志）。
     * 插入后按 id 周期性裁剪，保证日志表总量不超过 LOG_MAX。
     */
    public static function addLog(string $taskId, string $level, string $message): void
    {
        $message = rtrim($message, "\r\n");
        if ($message === '') {
            return;
        }
        $level = in_array($level, ['info', 'warn', 'error', 'success'], true) ? $level : 'info';
        $st = Db::conn()->prepare('INSERT INTO logs(ts,task_id,level,message) VALUES(?,?,?,?)');
        $st->execute([microtime(true), $taskId === '' ? null : $taskId, $level, mb_substr($message, 0, 8000)]);
        $lastId = (int) Db::conn()->lastInsertId();
        // 每 32 条裁剪一次即可（用主键做条件，走 PK 索引，代价极低）
        if ($lastId % 32 === 0) {
            self::pruneLogs();
        }
    }

    /** 裁剪最旧日志，只保留最新 LOG_MAX 条 */
    public static function pruneLogs(int $keep = self::LOG_MAX): int
    {
        $keep = max(1000, $keep);
        return Db::exec(
            'DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?',
            [$keep]
        );
    }

    /**
     * 增量取日志：id > $sinceId，可按 task_id 过滤。
     * 字段与 Python get_logs 一致（id/ts/task_id/level/message）。
     */
    public static function listLogs(int $sinceId = 0, int $limit = 500, ?string $taskId = null): array
    {
        $limit = max(1, min($limit, 5000));
        if ($taskId !== null && $taskId !== '') {
            return Db::all(
                'SELECT * FROM logs WHERE id>? AND task_id=? ORDER BY id LIMIT ?',
                [$sinceId, $taskId, $limit]
            );
        }
        return Db::all('SELECT * FROM logs WHERE id>? ORDER BY id LIMIT ?', [$sinceId, $limit]);
    }

    /** 清空日志（危险操作，调用方需写审计） */
    public static function clearLogs(): void
    {
        Db::exec('DELETE FROM logs');
    }

    // ------------------------------------------------------------------
    // 内部工具
    // ------------------------------------------------------------------
    private static function newTaskId(string $kind): string
    {
        $prefix = match (true) {
            $kind === 'create'                 => 'create',
            $kind === 'execute'                => 'exec',
            $kind === 'refresh'                => 'refresh',
            str_starts_with($kind, 'instance_') => 'act',
            default                            => 't',
        };
        // 时间戳 + 6 字节随机，跨进程唯一；random_bytes 满足 S8（禁用 rand）
        return sprintf('%s-%d-%s', $prefix, time(), bin2hex(random_bytes(6)));
    }

    /** 把 DB 行转成与 Python _task_row 一致的形态 */
    private static function decodeRow(array $row): array
    {
        foreach (['payload', 'result'] as $f) {
            $raw = $row[$f] ?? null;
            if ($raw === null || $raw === '') {
                $row[$f] = $f === 'payload' ? [] : null;
                continue;
            }
            try {
                $row[$f] = json_decode((string) $raw, true, 64, JSON_THROW_ON_ERROR);
            } catch (JsonException $e) {
                // 解码失败时保留原字符串，别让整行不可用
                $row[$f] = $raw;
            }
        }
        return $row;
    }

    // ---- 取消标志（存 settings 表，跨进程可见） ----
    private static function flagKey(string $id): string
    {
        return 'task_cancel:' . $id;
    }

    private static function setFlag(string $id, bool $on): void
    {
        Db::exec(
            'INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
            [self::flagKey($id), $on ? '1' : '0']
        );
    }

    private static function getFlag(string $id): bool
    {
        $v = Db::scalar('SELECT v FROM settings WHERE k=?', [self::flagKey($id)]);
        return $v === '1';
    }
}
