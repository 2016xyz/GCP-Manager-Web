<?php
/**
 * ApiTask —— 任务队列 / 日志 / SSH 密钥 的 HTTP handler
 *
 * 与前端的契约（对着 Python 版 app.py 逐字段对齐）：
 *   GET  /api/tasks           → {ok, tasks[], db_tasks[]}      两个口径都给出
 *   GET  /api/tasks/{id}      → {ok, task}  或 404 {detail}
 *   POST /api/tasks/{id}/cancel → {ok, task_id, cancel}
 *   GET  /api/logs?since_id=&limit=&task_id= → {ok, logs[]}
 *   DELETE /api/logs          → {ok}
 *   POST /api/sshkey/generate → {ok, public_key, private_key_path?}
 *   POST /api/sshkey/read     → {ok, public_key}；被拒时 400 且**写审计**
 */

declare(strict_types=1);

final class ApiTask
{
    private static function uname(): string
    {
        $s = Auth::currentSession();
        return (string) ($s['username'] ?? '');
    }

    /**
     * GET /api/tasks?limit=
     * → {ok, tasks[], db_tasks[]}
     * Python 版同时给两个口径：tasks 是给界面看的快照（含进度文案），
     * db_tasks 是数据库原始行。两者语义不同，都要给，不合并。
     */
    public static function listTasks(array $p): void
    {
        $limit = Http::queryInt('limit', 100) ?? 100;
        if ($limit < 1) {
            $limit = 1;
        }
        if ($limit > 1000) {
            $limit = 1000;
        }
        // 先做终态兜底：把卡在 running 超过阈值（默认 30 分钟）的任务标失败，
        // 避免界面上永远显示「运行中」（Python 版曾出现后台任务静默卡住）。
        try {
            Tasks::failStale();
        } catch (Throwable $e) {
            error_log('[gcpweb] failStale: ' . $e->getMessage());
        }
        $db = Tasks::list($limit);
        Json::ok([
            'tasks'    => self::snapshot($db),
            'db_tasks' => $db,
        ]);
    }

    /** 给界面用的任务快照：补上中文状态标签与进度文案 */
    private static function snapshot(array $dbTasks): array
    {
        $labels = [
            'queued'    => '排队中',
            'running'   => '执行中',
            'done'      => '已完成',
            'failed'    => '失败',
            'cancelled' => '已取消',
        ];
        $out = [];
        foreach ($dbTasks as $t) {
            $status = (string) ($t['status'] ?? '');
            $out[] = [
                'id'         => $t['id'],
                'kind'       => $t['kind'],
                'status'     => $status,
                'status_cn'  => $labels[$status] ?? $status,
                'message'    => (string) ($t['message'] ?? ''),
                'payload'    => $t['payload'] ?? null,
                'result'     => $t['result'] ?? null,
                'created_at' => $t['created_at'] ?? null,
                'updated_at' => $t['updated_at'] ?? null,
                'finished'   => in_array($status, Tasks::TERMINAL, true),
            ];
        }
        return $out;
    }

    /** GET /api/tasks/{id} → {ok, task} */
    public static function getTask(array $p): void
    {
        $id = (string) $p[0];
        $t = Tasks::get($id);
        if ($t === null) {
            Json::err('任务不存在', 404);
        }
        // 单查也做一次终态兜底（用户可能就点进这一条在看）
        if (!in_array((string) $t['status'], Tasks::TERMINAL, true)
            && (microtime(true) - (float) $t['updated_at']) > Tasks::STALE_SECONDS) {
            try {
                Tasks::failStale();
                $t = Tasks::get($id) ?? $t;
            } catch (Throwable $e) {
                error_log('[gcpweb] failStale: ' . $e->getMessage());
            }
        }
        Json::ok(['task' => $t, 'status_cn' => self::snapshot([$t])[0]['status_cn'] ?? '']);
    }

    /** POST /api/tasks/{id}/cancel → {ok, task_id, cancel} */
    public static function cancelTask(array $p): void
    {
        $id = (string) $p[0];
        $t = Tasks::get($id);
        if ($t === null) {
            Json::err('任务不存在', 404);
        }
        $cancel = Tasks::cancel($id);
        Users::addAudit(self::uname(), Http::clientIp(), 'cancel_task', $id,
            (string) $t['kind'], (bool) $cancel);
        Json::ok(['task_id' => $id, 'cancel' => (bool) $cancel]);
    }

    /**
     * GET /api/logs?since_id=&limit=&task_id=
     * → {ok, logs[]}
     * 前端轮询与 WebSocket 都用它（WS 是推送版本，轮询是降级版本）。
     */
    public static function logs(array $p): void
    {
        $since = Http::queryInt('since_id', 0) ?? 0;
        $limit = Http::queryInt('limit', 500) ?? 500;
        if ($limit < 1) {
            $limit = 1;
        }
        if ($limit > 5000) {
            $limit = 5000;
        }
        $taskId = trim((string) (Http::query('task_id') ?: ''));
        $rows = Tasks::listLogs($since, $limit, $taskId !== '' ? $taskId : null);
        Json::ok(['logs' => $rows]);
    }

    /** DELETE /api/logs → {ok} */
    public static function clearLogs(array $p): void
    {
        Tasks::clearLogs();
        Users::addAudit(self::uname(), Http::clientIp(), 'clear_logs', '', '', true);
        Json::ok(['message' => '日志已清空']);
    }

    /**
     * POST /api/sshkey/generate  body: {name?, comment?, save?}
     * → {ok, public_key, private_key_path?}
     * 生成的是 RSA-2048 密钥对；save=true 时私钥落到 data/ssh_keys/（600）。
     */
    public static function generateSshKey(array $p): void
    {
        $b = Http::jsonBody();
        $avail = Ssh::available();
        if (empty($avail['ok'])) {
            Json::err('本机没有可用的 ssh-keygen：' . (string) ($avail['reason'] ?? ''), 500);
        }
        $comment = (string) ($b['comment'] ?? '') ?: 'gcp-manager-web';
        $save = !empty($b['save']);
        // name 会被 Ssh 内部 basename + 字符白名单处理，这里不额外信任它
        $name = isset($b['name']) ? (string) $b['name'] : null;

        $res = Ssh::generateKey($name, $comment, $save);
        if (empty($res['ok'])) {
            Json::err((string) ($res['error'] ?? '生成密钥失败'), 500);
        }
        Users::addAudit(self::uname(), Http::clientIp(), 'generate_sshkey',
            (string) ($res['private_key_path'] ?? '(未保存)'), 'comment=' . $comment, true);

        $out = ['public_key' => (string) ($res['public_key'] ?? '')];
        if (!empty($res['private_key_path'])) {
            $out['private_key_path'] = (string) $res['private_key_path'];
        }
        Json::ok($out);
    }

    /**
     * POST /api/sshkey/read  body: {pubkey_path}
     * → {ok, public_key}；被拒时 400 并写审计（有人拿非公钥路径探测这个接口，
     *   本身值得记录 —— Python 版第一轮审计发现过它曾能读出 /etc/passwd 与管理员密码）。
     */
    public static function readSshKey(array $p): void
    {
        $b = Http::jsonBody();
        $path = (string) ($b['pubkey_path'] ?? '');
        $res = Ssh::readPubKey($path);
        if (empty($res['ok'])) {
            Users::addAudit(self::uname(), Http::clientIp(), 'read_sshkey_denied',
                mb_substr($path, 0, 200), (string) ($res['value'] ?? ''), false);
            Json::err((string) ($res['value'] ?? '不允许读取该路径'), 400);
        }
        Users::addAudit(self::uname(), Http::clientIp(), 'read_sshkey',
            mb_substr($path, 0, 200), '', true);
        Json::ok(['public_key' => (string) $res['value']]);
    }
}
