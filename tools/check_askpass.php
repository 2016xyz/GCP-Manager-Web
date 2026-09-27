<?php
/**
 * 本地验证「没有 sshpass 时」的密码认证路径。
 *
 * 用两条 PATH 各跑一次：
 *   A. 真实 PATH           → 有 sshpass，走 sshpass 分支
 *   B. 只放 ssh 的临时目录  → 无 sshpass，必须自动退回 askpass 分支，且 available() 仍为 ok
 *
 * 用法：php tools/check_askpass.php
 */
require dirname(__DIR__) . '/php/src/Ssh.php';

echo "══ 当前 PATH ══\n  ", getenv('PATH'), "\n";
$has = false;
foreach (explode(PATH_SEPARATOR, (string) getenv('PATH')) as $d) {
    if ($d !== '' && @is_executable($d . '/sshpass')) { $has = true; break; }
}
echo "  有 sshpass: ", $has ? '是' : '否', "\n\n";

$a = Ssh::available();
echo "══ Ssh::available() ══\n  ", json_encode($a, JSON_UNESCAPED_UNICODE), "\n";
echo "  → 判定: ", $a['ok'] ? "可用（密码认证走 " . ($a['password_auth'] ?? '?') . "）" : "不可用：" . $a['reason'], "\n";
