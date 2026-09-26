<?php
/**
 * 验证 bin/task-runner.php 会关闭继承的文件描述符
 *
 * 做法：父进程先建一个监听套接字（模拟 Web 服务器），再用数组参数 proc_open
 *   ① 拉起修复前的行为（一个什么都不做的 PHP 子进程）
 *   ② 拉起 task-runner.php
 * 然后检查子进程 /proc/<pid>/fd 里是否还能看到那个套接字。
 */
declare(strict_types=1);

$socket = stream_socket_server('tcp://127.0.0.1:18099', $errno, $errstr);
if (!$socket) {
    fwrite(STDERR, "建监听套接字失败: $errstr\n");
    exit(1);
}
printf("父进程监听 127.0.0.1:18099（fd=%d）\n", (int) $socket);

/** 列出某进程 fd 表里指向 socket 的条目数 */
function socketFds(int $pid): array
{
    $out = [];
    $dir = "/proc/$pid/fd";
    if (!is_dir($dir)) {
        return $out;
    }
    foreach (scandir($dir) ?: [] as $fd) {
        if ($fd === '.' || $fd === '..') {
            continue;
        }
        $target = @readlink("$dir/$fd");
        if (is_string($target) && strpos($target, 'socket:') === 0) {
            $out[] = $fd;
        }
    }
    return $out;
}

function spawn(bool $useRunner): array
{
    $php = PHP_BINARY;
    $script = $useRunner
        ? __DIR__ . '/../bin/task-runner.php'
        : __DIR__ . '/_noop_child.php';
    $args = $useRunner ? [$php, $script, '--once'] : [$php, $script];
    $desc = [0 => ['file', '/dev/null', 'r'], 1 => ['file', '/dev/null', 'w'], 2 => ['file', '/dev/null', 'w']];
    $p = proc_open($args, $desc, $pipes, dirname(__DIR__));
    if (!is_resource($p)) {
        return [null, 0];
    }
    $st = proc_get_status($p);
    return [$p, (int) $st['pid']];
}

$noop = [];
$runner = [];

echo "\n═══ ① 对照组：普通子进程（不做任何 fd 处理）═══\n";
[$h1, $pid1] = spawn(false);
if ($pid1) {
    usleep(300000);
    $noop = socketFds($pid1);
    printf("  子进程 pid=%d，fd 表里指向 socket 的条目：%s\n",
        $pid1, $noop ? ('❌ 有 ' . count($noop) . ' 个（' . implode(',', $noop) . '）—— 继承了监听套接字') : '✅ 无');
    @proc_terminate($h1, 9);
} else {
    echo "  拉起失败\n";
}

echo "\n═══ ② 被测：task-runner.php（启动时关闭继承的 fd）═══\n";
[$h2, $pid2] = spawn(true);
if ($pid2) {
    usleep(600000);
    $runner = socketFds($pid2);
    printf("  子进程 pid=%d，fd 表里指向 socket 的条目：%s\n",
        $pid2, $runner ? ('❌ 有 ' . count($runner) . ' 个（' . implode(',', $runner) . '）') : '✅ 无 —— 已关闭继承的监听套接字');
    @proc_terminate($h2, 9);
} else {
    echo "  拉起失败\n";
}

echo "\n═══ 结论 ═══\n";
if ($noop && !$runner) {
    echo "  ✅ 修复有效：对照组继承了套接字，task-runner 没有\n";
} elseif (!$noop) {
    echo "  ⚠ 对照组也没继承（可能 proc_open 已自动处理）—— 结论需保留\n";
} else {
    echo "  ❌ 修复无效：task-runner 仍持有监听套接字\n";
}

fclose($socket);
