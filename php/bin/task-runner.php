#!/usr/bin/env php
<?php
/**
 * task-runner —— 后台任务队列执行器（CLI）
 *
 * 用法：
 *   php bin/task-runner.php --once          跑一轮（领取≤1 个任务并执行完）后退出
 *   php bin/task-runner.php                 常驻模式：循环领取执行，空闲则 sleep
 *   php bin/task-runner.php --sleep=2       常驻模式下空闲轮询间隔（秒）
 *   php bin/task-runner.php --task=<id>     只执行指定任务（便于排查）
 *   php bin/task-runner.php --stale=1800    每轮开始时把超期 running 任务兜底为 failed
 *
 * 部署形态：
 *   · 宝塔「计划任务」每分钟拉起一次 --once；
 *   · 或由 PHP-FPM 侧 proc_open 后台拉起为常驻进程。
 *
 * 支持的任务类型：
 *   kind=execute          对已有实例批量执行命令（经系统 ssh 二进制）
 *   kind=create           批量创建实例（委托 Gcp 模块；dry_run 时只做预览）
 *   kind=instance_*       实例 start/stop/reset/delete（委托 Gcp 模块）
 *   kind=instance_action  同上，动作在 payload.action 里（ApiGcp 的统一入口）
 *   kind=refresh          刷新各账号实例计数缓存（委托 Gcp 模块）
 *
 * ★ 终态保证：每个任务都用 try/catch/finally 包裹，finally 里调用
 *   Tasks::ensureTerminal()，任何异常/提前 return 都不会把任务留在 running。
 */

declare(strict_types=1);

require __DIR__ . '/../src/bootstrap.php';

// ------------------------------------------------------------------
// ★ 关闭从父进程继承来的文件描述符
//
// 为什么必须做：worker 由 Web 侧用 proc_open 拉起（见 Tasks::spawnWorker），
// 而 fork+exec 出来的子进程默认继承父进程的全部 fd —— **包括 Web 服务器的
// 监听套接字**。后果实测过：测试期间反复创建任务后，主进程被 kill 掉，
// 端口却仍被 11 个 worker 子进程占着不放（ss 里十几个 pid 同时 LISTEN 同一个
// 端口），既看不出是谁在占，也没法正常重启服务。
//
// 这里在启动时主动关掉 3 以上的所有可访问 fd（0/1/2 保留）。Linux 专属做法，
// 而本项目的目标环境就是 Linux（宝塔/裸机/Docker）。
// 失败不影响功能（拿不到 /proc 时静默跳过）。
// ------------------------------------------------------------------
if (is_dir('/proc/self/fd') && ($dh = @opendir('/proc/self/fd')) !== false) {
    $keep = [0, 1, 2];
    // opendir 本身占用一个 fd，需一并跳过（否则把遍历句柄关掉会中断循环）
    $dirFd = null;
    $entries = [];
    while (($e = readdir($dh)) !== false) {
        if (ctype_digit($e)) {
            $entries[] = (int) $e;
        }
    }
    // /proc/self/fd 里的最大值通常就是 dirFd
    closedir($dh);
    $dirFd = $entries ? max($entries) : null;
    foreach ($entries as $fd) {
        if (in_array($fd, $keep, true) || $fd === $dirFd) {
            continue;
        }
        @fclose(@fopen('php://fd/' . $fd, 'r'));
    }
}

// ------------------------------------------------------------------
// 参数解析
// ------------------------------------------------------------------
$opts = ['once' => false, 'sleep' => 2, 'task' => null, 'stale' => Tasks::STALE_SECONDS];
foreach (array_slice($argv, 1) as $arg) {
    if ($arg === '--once') {
        $opts['once'] = true;
    } elseif (preg_match('/^--sleep=(\d+)$/', $arg, $m)) {
        $opts['sleep'] = max(1, (int) $m[1]);
    } elseif (preg_match('/^--stale=(\d+)$/', $arg, $m)) {
        $opts['stale'] = max(60, (int) $m[1]);
    } elseif (preg_match('/^--task=(.+)$/', $arg, $m)) {
        $opts['task'] = trim($m[1]);
    } elseif ($arg === '-h' || $arg === '--help') {
        fwrite(STDOUT, "用法: php bin/task-runner.php [--once] [--sleep=N] [--task=ID] [--stale=N]\n");
        exit(0);
    }
}

Config::ensureDataDirs();

$log = static function (string $msg, string $level = 'info', ?string $taskId = null): void {
    Tasks::addLog($taskId ?? '', $level, $msg);
    fwrite(STDOUT, sprintf("[%s] %s\n", date('H:i:s'), $msg));
};

$log(sprintf('task-runner 启动（%s），数据目录 %s', $opts['once'] ? 'once' : 'daemon', Config::dataDir()));

// ------------------------------------------------------------------
// 主循环
// ------------------------------------------------------------------
$emptyRounds = 0;
while (true) {
    // 1) 终态兜底：把进程中断／超时导致的「永久 running」收敛掉
    try {
        $n = Tasks::failStale($opts['stale']);
        if ($n > 0) {
            $log(sprintf('兜底收敛了 %d 个超期未结束的任务', $n), 'warn');
        }
    } catch (Throwable $e) {
        $log('兜底扫描失败：' . $e->getMessage(), 'error');
    }

    // 2) 领取任务
    $task = null;
    try {
        if ($opts['task'] !== null) {
            $task = Tasks::get($opts['task']);
            if ($task !== null && !in_array((string) $task['status'], Tasks::TERMINAL, true)
                && (string) $task['status'] !== 'running') {
                Tasks::update($opts['task'], 'running', '由 task-runner 定向执行');
                $task['status'] = 'running';
            } else {
                $task = null;   // 已终态或未知
            }
        } else {
            $task = Tasks::claimNext();
        }
    } catch (Throwable $e) {
        $log('领取任务失败：' . $e->getMessage(), 'error');
        $task = null;
    }

    if ($task === null) {
        if ($opts['once']) {
            break;
        }
        $emptyRounds++;
        sleep($opts['sleep']);
        continue;
    }
    $emptyRounds = 0;

    runTask($task, $log);

    if ($opts['once']) {
        break;
    }
}

$log('task-runner 退出');
exit(0);

// ==================================================================
// 单个任务的执行入口（保证终态）
// ==================================================================
function runTask(array $task, callable $log): void
{
    $id = (string) $task['id'];
    $kind = (string) $task['kind'];
    $payload = is_array($task['payload'] ?? null) ? $task['payload'] : [];
    $log(sprintf('领取任务 %s（kind=%s）', $id, $kind), 'info', $id);

    try {
        if (Tasks::cancelRequested($id)) {
            Tasks::update($id, 'cancelled', '领取时已收到取消请求');
            return;
        }
        switch ($kind) {
            case 'execute':
                executeCommandTask($id, $payload, $log);
                break;
            case 'create':
                executeCreateTask($id, $payload, $log);
                break;
            case 'refresh':
                executeRefreshTask($id, $payload, $log);
                break;
            default:
                if (str_starts_with($kind, 'instance_')) {
                    executeInstanceAction($id, $kind, $payload, $log);
                } else {
                    Tasks::update($id, 'failed', '不支持的任务类型：' . $kind);
                    $log('不支持的任务类型：' . $kind, 'error', $id);
                }
        }
    } catch (Throwable $e) {
        $log(sprintf('任务 %s 异常：%s', $id, $e->getMessage()), 'error', $id);
        Tasks::update($id, 'failed', '任务异常：' . $e->getMessage());
    } finally {
        // ★ 终态兜底：即便上面提前 return / 抛异常，也必须落到 done/failed/cancelled
        Tasks::ensureTerminal($id, '任务未正常结束（详见日志）');
    }
}

// ==================================================================
// kind=execute：对已有实例批量执行命令
// ==================================================================
function executeCommandTask(string $taskId, array $payload, callable $log): bool
{
    $command = trim((string) ($payload['command'] ?? ''));
    if ($command === '') {
        Tasks::update($taskId, 'failed', '命令为空');
        $log('命令为空，任务失败', 'error', $taskId);
        return true;
    }
    $timeout = max(1, (int) ($payload['command_timeout'] ?? 600));
    $idle = max(1, (int) ($payload['idle_timeout'] ?? 120));
    $targets = is_array($payload['targets'] ?? null) ? $payload['targets'] : [];
    // ★ all 的三态语义（与 Python core/tasks.py 的 submit_execute 保持一致）：
    //   缺省 / null = 调用方没指定 → 给了 targets 就只打 targets；
    //   true  = 显式要求扩散到（该作用域下）全部实例；
    //   false = 只打 targets。
    //   早前写的是 `array_key_exists('all') ? (bool)$payload['all'] : true`，
    //   把「没指定」当成 true —— 于是 `{"command":"x","targets":["a"]}`
    //   会把该账号下**所有**实例一并执行。前端显式传了 all 所以 UI 不会踩到，
    //   但 curl / 脚本 / 第三方调用会，等于误伤生产机。
    if (!array_key_exists('all', $payload) || $payload['all'] === null) {
        $all = null;
    } else {
        $all = (bool) $payload['all'];
    }
    $keyPath = isset($payload['key_path']) && is_string($payload['key_path']) ? $payload['key_path'] : null;
    $userOverride = isset($payload['user']) && is_string($payload['user']) ? $payload['user'] : null;

    // 目标机：优先显式 targets，否则取本地记录里的全部实例
    $vms = [];
    foreach (Db::all('SELECT name,ip,password,zone FROM vm_passwords') as $row) {
        $vms[(string) $row['name']] = $row;
    }
    $targetList = [];
    if ($targets !== []) {
        foreach ($targets as $name) {
            $name = (string) $name;
            $vm = $vms[$name] ?? [];
            $targetList[] = ['name' => $name, 'ip' => (string) ($vm['ip'] ?? ''),
                'password' => (string) ($vm['password'] ?? ''), 'zone' => (string) ($vm['zone'] ?? '')];
        }
    }
    // 扩散到全部实例的条件（见上面 all 的三态说明）：
    //   没有 targets → 缺省即全部（保持原来的便利语义）；
    //   有 targets   → 只有显式 all=true 才扩散，否则只打 targets
    $expandAll = ($targets === []) ? ($all !== false) : ($all === true);
    if ($expandAll) {
        if ($targets !== []) {
            $log('[警告] 已显式指定 all=true：除指定的 ' . count($targets)
                . ' 台外，其它实例也会执行同一条命令', 'warn', $taskId);
        }
        foreach ($vms as $name => $vm) {
            $exists = false;
            foreach ($targetList as $t) {
                if ($t['name'] === $name) {
                    $exists = true;
                    break;
                }
            }
            if (!$exists) {
                $targetList[] = ['name' => $name, 'ip' => (string) $vm['ip'],
                    'password' => (string) ($vm['password'] ?? ''), 'zone' => (string) ($vm['zone'] ?? '')];
            }
        }
    }

    if ($targetList === []) {
        Tasks::update($taskId, 'failed', '没有可执行的目标实例');
        $log('没有可执行的目标实例', 'error', $taskId);
        return true;
    }

    Tasks::update($taskId, 'running', sprintf('在 %d 台实例上执行命令', count($targetList)));
    $log(sprintf('任务 %s：在 %d 台实例上执行命令：%s', $taskId, count($targetList), mb_substr($command, 0, 200)), 'info', $taskId);

    $results = [];
    $okCount = 0;
    foreach ($targetList as $tgt) {
        if (Tasks::cancelRequested($taskId)) {
            $log('检测到取消请求，停止后续执行', 'warn', $taskId);
            Tasks::update($taskId, 'cancelled',
                sprintf('已取消：%d/%d 已完成', $okCount, count($results)), ['results' => $results]);
            return true;
        }
        $ip = (string) $tgt['ip'];
        if ($ip === '') {
            $results[] = ['name' => $tgt['name'], 'ok' => false, 'output' => '缺少 IP'];
            continue;
        }
        $pwd = (string) $tgt['password'];
        // 用户选择：显式覆盖 > key 模式默认 root > 密码模式 root > 镜像默认 ubuntu
        if ($userOverride !== null && $userOverride !== '') {
            $user = $userOverride;
        } elseif ($pwd !== '' || ($keyPath !== null && $keyPath !== '')) {
            $user = 'root';
        } else {
            $user = 'ubuntu';
        }
        $r = Ssh::run($ip, $user, $pwd, $command, [
            'key_path' => $keyPath,
            'connect_timeout' => 15,
            'idle_timeout' => $idle,
            'total_timeout' => $timeout,
            'keepalive' => 30,
            'log_callback' => static function (string $chunk) use ($log, $taskId, $tgt): void {
                $log(sprintf('[%s] %s', $tgt['name'], rtrim($chunk)), 'info', $taskId);
            },
            'stop' => static fn(): bool => Tasks::cancelRequested($taskId),
        ]);
        $item = ['name' => $tgt['name'], 'ip' => $ip, 'ok' => $r['ok'],
            'output' => mb_substr($r['output'], -4000)];
        $results[] = $item;
        if ($r['ok']) {
            $okCount++;
        }
        $log(sprintf('[%s] %s', $tgt['name'], $r['ok'] ? '✅ 成功' : '❌ 失败'), $r['ok'] ? 'success' : 'error', $taskId);
    }

    if (Tasks::cancelRequested($taskId)) {
        Tasks::update($taskId, 'cancelled', sprintf('已取消：%d/%d 成功', $okCount, count($results)),
            ['results' => $results]);
        return true;
    }
    Tasks::update($taskId, 'done', sprintf('执行完成：%d/%d 成功', $okCount, count($results)),
        ['results' => $results]);
    $log(sprintf('任务 %s 执行完成：%d/%d 成功', $taskId, $okCount, count($results)), 'success', $taskId);
    return true;
}

// ==================================================================
// kind=create：批量创建实例（委托 Gcp 模块）
// ==================================================================
/**
 * 集成约定：Gcp.php 提供静态钩子 runCreateTask(array $payload, string $taskId, callable $log): array
 * 钩子返回 ['ok'=>bool,'message'=>string,'result'=>array]。
 *
 * dry_run 的处理：**不碰云**。预览只需要把规整后的配置回给前端看，
 * 因此这里就地合成 plan，不调 Gcp（也就不会因为账号密钥不可用而失败）。
 * 实建任务才委托 Gcp；若钩子不存在（并行开发阶段），明确置 failed，绝不留在 running。
 */
function executeCreateTask(string $taskId, array $payload, callable $log): bool
{
    $dryRun = (bool) ($payload['dry_run'] ?? false);
    if ($dryRun) {
        $plan = [
            'dry_run' => true,
            'count' => (int) ($payload['count'] ?? 1),
            'account_ids' => $payload['account_ids'] ?? [],
            'spec' => $payload['spec'] ?? [],
        ];
        // Gcp 就绪时顺带把配置规整一遍（build_instance_spec 是纯函数，不碰网络），
        // 让预览里展示的机型/镜像/磁盘/区域与实建时真正用的一致。
        if (class_exists('Gcp') && method_exists('Gcp', 'build_instance_spec')) {
            try {
                $plan['resolved_spec'] = Gcp::build_instance_spec(
                    is_array($payload['spec'] ?? null) ? $payload['spec'] : []
                );
            } catch (Throwable $e) {
                $plan['resolved_spec_error'] = $e->getMessage();
            }
        }
        Tasks::update($taskId, 'done', 'dry-run 预览完成', ['plan' => $plan]);
        $log('dry-run 预览完成（未创建任何资源）', 'success', $taskId);
        return true;
    }

    if (class_exists('Gcp') && method_exists('Gcp', 'runCreateTask')) {
        Tasks::update($taskId, 'running', '开始创建实例');
        /** @var array $ret */
        $ret = Gcp::runCreateTask($payload, $taskId, $log);
        $ok = (bool) ($ret['ok'] ?? false);
        // 取消请求优先于 ok/failed —— 与 Python 一致（_run_create_batch 里
        // status 就是按 is_cancelled 决定的），否则「取消」会被覆盖成 failed。
        $final = Tasks::cancelRequested($taskId) ? 'cancelled' : ($ok ? 'done' : 'failed');
        Tasks::update(
            $taskId,
            $final,
            (string) ($ret['message'] ?? ($ok ? '创建完成' : '创建失败')),
            $ret['result'] ?? null
        );
        return true;
    }

    // Gcp 模块未就绪：明确失败，不留 running
    Tasks::update($taskId, 'failed',
        'Gcp 模块未就绪（缺少 Gcp::runCreateTask），无法执行建实例任务');
    $log('Gcp 模块未就绪，无法执行建实例任务', 'error', $taskId);
    return true;
}

// ==================================================================
// kind=refresh：刷新各账号实例计数缓存（委托 Gcp 模块）
// ==================================================================
function executeRefreshTask(string $taskId, array $payload, callable $log): bool
{
    if (class_exists('Gcp') && method_exists('Gcp', 'runRefreshTask')) {
        Tasks::update($taskId, 'running', '刷新中');
        /** @var array $ret */
        $ret = Gcp::runRefreshTask($payload, $taskId, $log);
        $ok = (bool) ($ret['ok'] ?? false);
        $final = Tasks::cancelRequested($taskId) ? 'cancelled' : ($ok ? 'done' : 'failed');
        Tasks::update($taskId, $final,
            (string) ($ret['message'] ?? ($ok ? '刷新完成' : '刷新失败')), $ret['result'] ?? null);
        return true;
    }
    Tasks::update($taskId, 'failed',
        'Gcp 模块未就绪（缺少 Gcp::runRefreshTask），无法执行刷新任务');
    $log('Gcp 模块未就绪，无法执行刷新任务', 'error', $taskId);
    return true;
}

// ==================================================================
// kind=instance_*：实例启停/重置/删除（委托 Gcp 模块）
// ==================================================================
/**
 * 从 kind / payload 解析实例动作。
 *
 * 为什么要两个来源：历史（Python 语义）的动作是**编码在 kind 里**的
 * （instance_delete / instance_start…），而 ApiGcp::instanceAction 统一落
 * kind=instance_action、把动作放在 payload.action。只认前者会让
 * POST /api/instance_action 生成的任务全部报「不支持的实例操作：action」——
 * 两个来源都支持，前端两条路径才都可用。
 */
function resolveInstanceAction(string $kind, array $payload): string
{
    $valid = ['start', 'stop', 'reset', 'delete'];
    $fromKind = substr($kind, strlen('instance_'));
    if (in_array($fromKind, $valid, true)) {
        return $fromKind;
    }
    $fromPayload = (string) ($payload['action'] ?? '');
    return in_array($fromPayload, $valid, true) ? $fromPayload : $fromKind;
}

function executeInstanceAction(string $taskId, string $kind, array $payload, callable $log): bool
{
    $action = resolveInstanceAction($kind, $payload);
    if (!in_array($action, ['start', 'stop', 'reset', 'delete'], true)) {
        Tasks::update($taskId, 'failed', '不支持的实例操作：' . $action);
        return true;
    }
    if (class_exists('Gcp') && method_exists('Gcp', 'runInstanceAction')) {
        Tasks::update($taskId, 'running', sprintf('%s 实例', $action));
        /** @var array $ret */
        $ret = Gcp::runInstanceAction($action, $payload['targets'] ?? [], $taskId, $log);
        $ok = (bool) ($ret['ok'] ?? false);
        $final = Tasks::cancelRequested($taskId) ? 'cancelled' : ($ok ? 'done' : 'failed');
        Tasks::update($taskId, $final,
            (string) ($ret['message'] ?? ($ok ? '完成' : '失败')), $ret['result'] ?? null);
        return true;
    }
    Tasks::update($taskId, 'failed',
        'Gcp 模块未就绪（缺少 Gcp::runInstanceAction），无法执行实例操作');
    $log('Gcp 模块未就绪，无法执行实例操作', 'error', $taskId);
    return true;
}
