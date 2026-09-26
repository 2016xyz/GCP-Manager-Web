<?php
/**
 * init.php —— 初始化数据库与首个管理员（CLI）
 *
 * 做什么：
 *   1. 建库建表（Store::ensure + Users::ensure，幂等，重复执行安全）
 *   2. 库里没有任何用户时，创建一个管理员，密码随机生成
 *   3. 把账号密码写到 data/INITIAL_ADMIN.txt（600），并打印到终端
 *   4. 设置 must_change_password=1 —— 首次登录必须改密（服务端强制）
 *
 * 幂等性：**绝不在已有用户的情况下新建/覆盖管理员**。
 *   重复执行只会打印「已存在 N 个用户，跳过」，不会重置任何人的密码
 *   —— 否则每次升级后重跑都会把管理员密码打回初始值，等于开后门。
 *
 * 用法：
 *   php bin/init.php              # 正常初始化
 *   php bin/init.php --force-new  # 已有用户时仍新建一个临时管理员（应急用）
 *   GCPWEB_INITIAL_PASSWORD=xxx php bin/init.php   # 指定初始密码
 */

declare(strict_types=1);

require __DIR__ . '/../src/bootstrap.php';

$forceNew = in_array('--force-new', $argv ?? [], true);

function say(string $m): void { fwrite(STDOUT, $m . "\n"); }

say('');
say('════════════════════════════════════════════');
say('  GCP Manager Web · PHP 版 初始化');
say('════════════════════════════════════════════');
say('');
say('  数据目录 ' . Config::dataDir());
say('  数据库   ' . Config::dbPath());
say('  PHP      ' . PHP_VERSION . ' (' . PHP_SAPI . ')');
say('');

// ── 1. 建表 ─────────────────────────────────────────────────────────────────
try {
    Store::ensure();
} catch (Throwable $e) {
    fwrite(STDERR, "  ✗ 建 Store 表失败：" . $e->getMessage() . "\n");
    exit(1);
}
try {
    Users::ensure();
} catch (Throwable $e) {
    fwrite(STDERR, "  ✗ 建 Users 表失败：" . $e->getMessage() . "\n");
    exit(1);
}
try {
    // Auth 自己维护三张表：captcha（图形验证码）、login_guard（登录限速）、
    // auth_runtime（跨进程运行时计数）。不建的话首次登录会报 no such table。
    Auth::ensure();
} catch (Throwable $e) {
    fwrite(STDERR, "  ✗ 建 Auth 表失败：" . $e->getMessage() . "\n");
    exit(1);
}
say('  ✓ 数据表就绪（accounts / vm_passwords / tasks / logs / settings / users / sessions / audit / captcha / login_guard / auth_runtime）');

// ── 2. 判断是否已有用户 ─────────────────────────────────────────────────────
$count = Users::countUsers();
if ($count > 0 && !$forceNew) {
    say("  · 已存在 $count 个用户，跳过管理员创建（不会覆盖任何密码）");
    say('');
    say('  如果你忘了管理员密码，在服务器上执行：');
    say('      php bin/reset_admin.php           # 生成一个新的临时管理员');
    say('  或直接删掉 data/INITIAL_ADMIN.txt 后重跑本脚本（用户表不会被清空）');
    say('');
    exit(0);
}

// ── 3. 建管理员 ─────────────────────────────────────────────────────────────
// 密码来源优先级：GCPWEB_INITIAL_PASSWORD > 随机生成
// 随机生成保证满足强度要求（含大小写/数字/符号，长度 16）
function gen_password(int $len = 16): string
{
    // 去掉易混淆字符（0/O/1/l/I），且保证四类字符各至少一个
    $lower = 'abcdefghijkmnopqrstuvwxyz';
    $upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ';
    $digit = '23456789';
    $sym   = '!@#%^*-_=+';
    $all   = $lower . $upper . $digit . $sym;
    $pw = $lower[random_int(0, strlen($lower) - 1)]
        . $upper[random_int(0, strlen($upper) - 1)]
        . $digit[random_int(0, strlen($digit) - 1)]
        . $sym[random_int(0, strlen($sym) - 1)];
    for ($i = strlen($pw); $i < $len; $i++) {
        $pw .= $all[random_int(0, strlen($all) - 1)];
    }
    // 打乱（Fisher-Yates，用 random_int 保证不可预测）
    $a = str_split($pw);
    for ($i = count($a) - 1; $i > 0; $i--) {
        $j = random_int(0, $i);
        [$a[$i], $a[$j]] = [$a[$j], $a[$i]];
    }
    return implode('', $a);
}

$username = Config::env('GCPWEB_ADMIN_USER', 'admin');
$password = Config::env('GCPWEB_INITIAL_PASSWORD');
$generated = false;
if ($password === null || $password === '') {
    $password = gen_password(16);
    $generated = true;
}

// 用户名冲突时加后缀（--force-new 场景下可能已存在 admin）
$finalUser = $username;
$n = 1;
while (Users::getUser(null, $finalUser) !== null) {
    $n++;
    $finalUser = $username . $n;
    if ($n > 50) {
        fwrite(STDERR, "  ✗ 无法找到可用的用户名\n");
        exit(1);
    }
}

try {
    $uid = Users::createUser(
        $finalUser,
        $password,
        'admin',
        '初始管理员',
        true,          // mustChange=true：服务端强制首次登录改密
        'installer'    // createdBy
    );
    say("  ✓ 已创建管理员：#$uid  用户名 $finalUser");
} catch (Throwable $e) {
    fwrite(STDERR, "  ✗ 创建管理员失败：" . $e->getMessage() . "\n");
    exit(1);
}

// ── 4. 写密码文件 ───────────────────────────────────────────────────────────
$pwFile = Config::dataDir() . '/INITIAL_ADMIN.txt';
$content =
    "GCP Manager Web（PHP 版）初始管理员\n"
    . "=====================================\n"
    . "用户名：" . $finalUser . "\n"
    . "密码：  " . $password . "\n"
    . "\n"
    . "★ 首次登录会强制要求修改密码（服务端强制，不是前端提示）。\n"
    . "★ 改完密码后请删除本文件，或至少确认权限是 600：\n"
    . "    rm -f " . $pwFile . "\n"
    . "\n"
    . "生成时间：" . gmdate('Y-m-d H:i:s') . " UTC\n";

try {
    file_put_contents($pwFile, $content, LOCK_EX);
    @chmod($pwFile, 0600);
} catch (Throwable $e) {
    fwrite(STDERR, "  ! 写入密码文件失败：" . $e->getMessage() . "\n");
}

say('');
say('────────────────────────────────────────────');
say('  初始管理员');
say('    用户名  ' . $finalUser);
if ($generated) {
    say('    密码    ' . $password . '   ← 随机生成，请立即记录');
} else {
    say('    密码    （来自 GCPWEB_INITIAL_PASSWORD，见你的环境变量）');
}
say('    密码文件 ' . $pwFile . '（600）');
say('────────────────────────────────────────────');
say('');
say('  首次登录会强制改密。改完可删除密码文件：');
say('      rm -f ' . $pwFile);
say('');

// ── 5. 顺手清理过期会话 ─────────────────────────────────────────────────────
try {
    $n = Users::purgeExpiredSessions();
    if ($n > 0) {
        say("  · 清理过期会话 $n 条");
    }
} catch (Throwable $e) {
    // 不影响初始化结果
}
exit(0);
