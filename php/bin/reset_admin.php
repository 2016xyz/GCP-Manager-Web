<?php
/**
 * reset_admin.php —— 应急：重置某个用户的密码（CLI）
 *
 * 场景：管理员把密码忘了，人又进不去后台。此时只能在服务器上执行本脚本。
 *
 * 用法：
 *   php bin/reset_admin.php                    # 重置 admin，随机新密码
 *   php bin/reset_admin.php --user=alice       # 重置指定用户
 *   php bin/reset_admin.php --user=alice --new     # 用户不存在则新建为管理员
 *   php bin/reset_admin.php --password='xxx'   # 指定新密码（不推荐，会留在 shell 历史里）
 *   php bin/reset_admin.php --no-must-change   # 不强制下次登录改密
 *
 * ★ 安全约束：
 *   · 默认置 must_change_password=1，即临时密码只能用来改密
 *   · 重置会**吊销该用户的全部会话**（防止旧会话继续可用）
 *   · 写一条审计（action=cli_reset_password），事后可追溯
 *   · 不打印明文到日志文件，只在终端显示
 *
 * 这相当于一个本地提权入口 —— 能执行它的人本来就有服务器权限，
 * 所以不再额外做「身份校验」；但必须留下审计痕迹。
 */

declare(strict_types=1);

require __DIR__ . '/../src/bootstrap.php';

$opts = ['user' => 'admin', 'password' => null, 'new' => false, 'must-change' => true];
foreach (array_slice($argv ?? [], 1) as $a) {
    if (preg_match('/^--user=(.+)$/', $a, $m))        { $opts['user'] = $m[1]; }
    elseif (preg_match('/^--password=(.+)$/', $a, $m)) { $opts['password'] = $m[1]; }
    elseif ($a === '--new')                            { $opts['new'] = true; }
    elseif ($a === '--no-must-change')                 { $opts['must-change'] = false; }
    elseif ($a === '-h' || $a === '--help') {
        $lines = file(__FILE__);
        foreach (array_slice($lines, 1, 24) as $l) {
            echo preg_replace('/^\s*\*\s?/', '', $l);
        }
        exit(0);
    }
}

function out(string $m): void { fwrite(STDOUT, $m . "\n"); }

Store::ensure();
Users::ensure();

$user = Users::getUser(null, $opts['user']);

if ($user === null) {
    if (!$opts['new']) {
        fwrite(STDERR, "✗ 用户 {$opts['user']} 不存在。要新建管理员请加 --new\n");
        exit(1);
    }
    $pw = $opts['password'] ?? bin2hex(random_bytes(12));
    try {
        $id = Users::createUser($opts['user'], $pw, 'admin', 'CLI 新建管理员',
                                (bool) $opts['must-change'], 'cli');
    } catch (Throwable $e) {
        fwrite(STDERR, "✗ 建号失败：" . $e->getMessage() . "\n");
        exit(1);
    }
    out('');
    out('已新建管理员（#' . $id . '）');
    out('  用户名 ' . $opts['user']);
    out('  密码   ' . $pw);
    out('  ' . ($opts['must-change'] ? '下次登录必须改密' : '密码即为最终密码'));
    out('');
    exit(0);
}

$pw = $opts['password'] ?? bin2hex(random_bytes(12));

try {
    Users::setPassword((int) $user['id'], $pw, (bool) $opts['must-change']);
    // 吊销该用户全部会话：防止旧会话在改密后仍可用
    $killed = Users::revokeUserSessions((int) $user['id']);
    Users::addAudit(
        (string) $user['username'],
        'cli',
        'cli_reset_password',
        'user=' . $user['username'],
        '由服务器本地 CLI 重置密码' . ($killed > 0 ? "，吊销会话 $killed 条" : ''),
        true
    );
} catch (Throwable $e) {
    fwrite(STDERR, "✗ 重置失败：" . $e->getMessage() . "\n");
    exit(1);
}

out('');
out('已重置密码');
out('  用户名 ' . $user['username'] . '（#' . $user['id'] . '，角色 ' . $user['role'] . '）');
out('  新密码 ' . $pw);
if ($opts['must-change']) {
    out('  下次登录必须改密（服务端强制）');
}
if ($killed > 0) {
    out('  已吊销该用户 ' . $killed . ' 个会话');
}
out('  已写入审计：cli_reset_password');
out('');
