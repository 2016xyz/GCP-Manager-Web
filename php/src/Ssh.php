<?php
/**
 * Ssh —— 系统 ssh / ssh-keygen 调用封装
 *
 * 设计背景
 *   Python 版用 paramiko 做 SSH；PHP 侧**零 composer 依赖**，因此直接调用系统
 *   二进制（ssh / ssh-keygen / sshpass）。这样部署面最小，但必须格外注意
 *   命令注入与子进程回收。
 *
 * ★ 命令注入防护（红线 S6）
 *   · 一律用 proc_open 的**数组参数形式**：argv 各元素原样传给 execve，
 *     不经过本地 shell，用户输入永远不会被本地 shell 解析。
 *   · 需要把多行脚本送到远端时用 runScript()：先把脚本写进本地临时文件（0600），
 *     再把该文件作为 ssh 的 stdin，远端执行 `bash -s`。
 *     —— 脚本正文绝不拼接进任何命令行字符串（即「先落临时文件再执行」）。
 *   · 密码通过环境变量 SSHPASS 交给 sshpass（-e），不进 argv（ps 里看不到）。
 *
 * ★ TOFU 主机密钥
 *   Python 用自建策略「首连记录指纹、之后不一致即中断」。
 *   ssh CLI 的等价物是 StrictHostKeyChecking=accept-new + 单独的 known_hosts
 *   文件：首连自动记录，之后指纹变化即拒连 —— 比 `no`（静默信任，等同
 *   AutoAddPolicy）安全，也避免了首次连接交互式确认卡死。
 *
 * ★ 超时与僵尸进程
 *   procRun() 内部用非阻塞读 + stream_select 循环，带总超时；超时后先 SIGTERM
 *   再 SIGKILL，最后一定 proc_close() —— 由它回收子进程，杜绝僵尸。
 */

declare(strict_types=1);

final class Ssh
{
    /** 公钥可接受的前缀（与 Python _PUBKEY_PREFIXES 对齐） */
    private const PUBKEY_PREFIXES = [
        'ssh-rsa', 'ssh-ed25519', 'ssh-dss', 'ecdsa-sha2-',
        'sk-ecdsa-sha2-', 'sk-ssh-ed25519',
        'ssh-rsa-cert', 'ssh-ed25519-cert', 'ecdsa-sha2-nistp256-cert',
    ];

    /** 明显是私钥/证书库的文件名后缀（与 Python _PRIVATE_KEY_HINTS 对齐） */
    private const PRIVATE_KEY_HINTS = ['.pem', '.key', '.ppk', '.pfx', '.p12', '.jks', '.keystore'];

    /** 公钥最大字节数（与 Python _MAX_PUBKEY_BYTES 对齐） */
    public const MAX_PUBKEY_BYTES = 8192;

    /**
     * SSH 执行能力探测 —— 对应 Python 版的 ssh_mod.check_paramiko()。
     *
     * Python 靠 paramiko 这个 Python 包；PHP 零 composer 依赖，改用系统 ssh。
     * 因此这里如实报告「系统 ssh 是否可用」，字段名在 /api/status 里保持一致
     * （前端只是拿它显示「SSH 可用 / 不可用」）。
     *
     * @return array{ok:bool, tool:string, reason:string}
     */
    public static function available(): array
    {
        static $cached = null;
        if ($cached !== null) {
            return $cached;
        }
        $ssh = self::which('ssh');
        if ($ssh === '') {
            return $cached = ['ok' => false, 'tool' => 'ssh', 'reason' => '系统未安装 ssh 客户端'];
        }
        $sshpass = self::which('sshpass');
        if ($sshpass === '') {
            return $cached = ['ok' => false, 'tool' => 'sshpass',
                              'reason' => '未安装 sshpass（密码登录需要它；请 apt install sshpass 或 yum install sshpass）'];
        }
        return $cached = ['ok' => true, 'tool' => 'ssh', 'reason' => ''];
    }

    /** 在 PATH 里找可执行文件（不经过 shell） */
    private static function which(string $bin): string
    {
        $path = getenv('PATH') ?: '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin';
        foreach (explode(PATH_SEPARATOR, $path) as $dir) {
            $dir = rtrim($dir, '/');
            if ($dir === '') {
                continue;
            }
            $full = $dir . '/' . $bin;
            if (is_file($full) && is_executable($full)) {
                return $full;
            }
        }
        return '';
    }

    // ==================================================================
    // 公钥读取（POST /api/sshkey/read 的核心校验）
    // ==================================================================
    /**
     * 判断给定路径能否作为「公钥」返回。
     * @return array{ok:bool,value:string} ok=true 时 value 是公钥内容，
     *                                     ok=false 时 value 是拒绝原因（文案与 Python 一致）
     */
    public static function readPubKey(string $path): array
    {
        if (trim($path) === '') {
            return ['ok' => false, 'value' => '路径为空'];
        }
        $raw = trim($path);

        // 展开 ~（realpath 不认 ~）
        if ($raw === '~' || str_starts_with($raw, '~/')) {
            $home = getenv('HOME') ?: ($_SERVER['HOME'] ?? '');
            if ($home !== '') {
                $raw = rtrim($home, '/') . substr($raw, 1);
            }
        }

        // realpath 解析符号链接与 .. 穿越，之后再比对前缀
        $real = realpath($raw);
        if ($real === false || !is_file($real)) {
            return ['ok' => false, 'value' => '文件不存在，或不是普通文件'];
        }

        $name = basename($real);
        $low  = strtolower($name);

        // 1) 拒绝私钥文件名：id_* 且不以 .pub 结尾
        if (str_starts_with($low, 'id_') && !str_ends_with($low, '.pub')) {
            return ['ok' => false, 'value' => '这看起来是私钥，不允许读取'];
        }
        // 2) 拒绝明显是私钥/证书库的后缀
        foreach (self::PRIVATE_KEY_HINTS as $hint) {
            if (str_ends_with($low, $hint)) {
                return ['ok' => false, 'value' => '这看起来是私钥或证书库文件，不允许读取'];
            }
        }

        // 3) data/ 目录整体保护，只放行其中我们生成的 .pub
        $dataRoot = realpath(Config::dataDir());
        if ($dataRoot !== false
            && ($real === $dataRoot || str_starts_with($real, $dataRoot . DIRECTORY_SEPARATOR))) {
            $allowed = [];
            foreach ([Config::dataDir() . '/ssh_keys', Config::keysDir()] as $d) {
                $r = realpath($d);
                if ($r !== false) {
                    $allowed[] = $r;
                }
            }
            $inside = false;
            foreach ($allowed as $a) {
                if ($real === $a || str_starts_with($real, $a . DIRECTORY_SEPARATOR)) {
                    $inside = true;
                    break;
                }
            }
            if (!$inside) {
                return ['ok' => false, 'value' => '拒绝读取 data/ 目录下的文件（该目录含管理员密码、'
                    . '数据库与服务账号私钥）；只允许读取其中的公钥'];
            }
            if (!str_ends_with($low, '.pub')) {
                return ['ok' => false, 'value' => '只允许读取 data/ 目录下的 .pub 公钥文件'];
            }
        }

        // 4) 大小限制
        $size = @filesize($real);
        if ($size === false) {
            return ['ok' => false, 'value' => '无法获取文件大小'];
        }
        if ($size === 0) {
            return ['ok' => false, 'value' => '文件为空'];
        }
        if ($size > self::MAX_PUBKEY_BYTES) {
            return ['ok' => false, 'value' => sprintf('文件过大（%d 字节），不像是公钥', $size)];
        }

        // 5) 读取 + 文本校验
        $content = @file_get_contents($real);
        if ($content === false) {
            return ['ok' => false, 'value' => '读取失败'];
        }
        if (!mb_check_encoding($content, 'UTF-8')) {
            return ['ok' => false, 'value' => '文件不是文本格式，无法作为公钥读取'];
        }
        $content = trim($content);

        // 6) 内容必须以公钥前缀开头 —— 这一条挡住 /etc/passwd、数据库、源码等一切非公钥内容
        $ok = false;
        foreach (self::PUBKEY_PREFIXES as $p) {
            if (str_starts_with($content, $p)) {
                $ok = true;
                break;
            }
        }
        if (!$ok) {
            return ['ok' => false, 'value' => '文件内容不是 SSH 公钥（应以 ssh-rsa / ssh-ed25519 / '
                . 'ecdsa-sha2- 等开头）'];
        }
        return ['ok' => true, 'value' => $content];
    }

    // ==================================================================
    // 密钥生成（POST /api/sshkey/generate）
    // ==================================================================
    /**
     * 生成 RSA 2048 密钥对。
     * @return array{ok:bool,public_key?:string,private_key_path?:string,error?:string}
     */
    public static function generateKey(?string $name = null, ?string $comment = null, bool $save = true): array
    {
        $comment = $comment !== null && trim($comment) !== '' ? trim($comment) : 'gcp-manager-web';
        $tmpDir  = null;

        if ($save) {
            $dir = Config::dataDir() . '/ssh_keys';
            if (!is_dir($dir) && !@mkdir($dir, 0700, true)) {
                return ['ok' => false, 'error' => '无法创建密钥目录'];
            }
            @chmod($dir, 0700);
            // basename 掐掉任何路径成分，杜绝穿越（S5）
            $safe = basename($name !== null && trim($name) !== '' ? trim($name) : ('gcp_key_' . time()));
            $safe = preg_replace('/[^A-Za-z0-9._\-]/', '_', $safe) ?: ('gcp_key_' . time());
            if (str_ends_with($safe, '.pub')) {
                $safe = substr($safe, 0, -4);
            }
            $keyPath = $dir . '/' . $safe;
        } else {
            // 不落盘场景：生成到临时目录，读完公钥即删
            $tmpDir = sys_get_temp_dir() . '/gcpkey_' . bin2hex(random_bytes(6));
            if (!@mkdir($tmpDir, 0700, true)) {
                return ['ok' => false, 'error' => '无法创建临时目录'];
            }
            $keyPath = $tmpDir . '/key';
        }

        $args = ['ssh-keygen', '-q', '-t', 'rsa', '-b', '2048', '-N', '', '-C', $comment, '-f', $keyPath];
        $r = self::procRun($args, [], null, 30);
        if ($r['code'] !== 0) {
            if ($tmpDir !== null) {
                @unlink($tmpDir . '/key');
                @unlink($tmpDir . '/key.pub');
                @rmdir($tmpDir);
            }
            return ['ok' => false, 'error' => 'ssh-keygen 执行失败：' . trim($r['err'] ?: $r['out'])];
        }

        $pub = @file_get_contents($keyPath . '.pub');
        if ($pub === false) {
            return ['ok' => false, 'error' => '公钥文件生成失败'];
        }
        $pub = trim($pub);

        if ($save) {
            @chmod($keyPath, 0600);
            @chmod($keyPath . '.pub', 0600);
            return ['ok' => true, 'public_key' => $pub, 'private_key_path' => $keyPath];
        }
        @unlink($keyPath);
        @unlink($keyPath . '.pub');
        @rmdir($tmpDir);
        return ['ok' => true, 'public_key' => $pub];
    }

    // ==================================================================
    // 远端命令执行
    // ==================================================================
    /**
     * 在远端执行一条命令。
     *
     * @param array $opts 支持 key_path / port / connect_timeout / total_timeout
     *                    / idle_timeout / known_hosts / log_callback(callable) / stop(?callable)
     * @return array{ok:bool,output:string}
     */
    public static function run(string $ip, string $user, string $password, string $command, array $opts = []): array
    {
        $port   = (int) ($opts['port'] ?? 22);
        $connT  = (int) ($opts['connect_timeout'] ?? 15);
        $totalT = (int) ($opts['total_timeout'] ?? 1800);
        $keyPath = $opts['key_path'] ?? null;
        $logCb  = $opts['log_callback'] ?? null;
        $stopCb = $opts['stop'] ?? null;

        $argv = self::buildSshArgv($ip, $user, $port, $connT, $keyPath, $opts);
        $argv[] = '--';
        $argv[] = $command;   // 作为**单个 argv 元素**，本地 shell 不会解析它

        return self::execSsh($argv, $password, null, $totalT, $logCb, $stopCb, $opts);
    }

    /**
     * 把一段脚本（可能多行，带任何引号/特殊字符）送到远端执行。
     * 实现方式：脚本先写入本地临时文件（0600），用该文件作为 ssh 的 stdin，
     * 远端 `bash -s` 从标准输入读取执行 —— 脚本正文从不进入命令行字符串。
     *
     * @return array{ok:bool,output:string}
     */
    public static function runScript(string $ip, string $user, string $password, string $script, array $opts = []): array
    {
        if (trim($script) === '') {
            return ['ok' => false, 'output' => '脚本内容为空'];
        }
        // 落临时文件：随机名 + 0600，避免被同机其它用户读取
        $tmp = tempnam(sys_get_temp_dir(), 'gcprs_');
        if ($tmp === false) {
            return ['ok' => false, 'output' => '无法创建临时脚本文件'];
        }
        @chmod($tmp, 0600);
        if (@file_put_contents($tmp, $script) === false) {
            @unlink($tmp);
            return ['ok' => false, 'output' => '写入临时脚本失败'];
        }

        try {
            $port   = (int) ($opts['port'] ?? 22);
            $connT  = (int) ($opts['connect_timeout'] ?? 15);
            $totalT = (int) ($opts['total_timeout'] ?? 1800);
            $argv = self::buildSshArgv($ip, $user, $port, $connT, $opts['key_path'] ?? null, $opts);
            $argv[] = '--';
            $argv[] = 'bash -s';
            return self::execSsh($argv, $password, $tmp, $totalT,
                $opts['log_callback'] ?? null, $opts['stop'] ?? null, $opts);
        } finally {
            @unlink($tmp);   // 无论成败都清理
        }
    }

    /** 轮询端口直至 SSH 可认证（移植 wait_ssh_ready 的语义） */
    public static function waitReady(string $ip, string $user, string $password, array $opts = []): array
    {
        $deadline = microtime(true) + max(30, (int) ($opts['timeout'] ?? 300));
        $last = '';
        while (microtime(true) < $deadline) {
            if (!empty($opts['stop']) && is_callable($opts['stop']) && ($opts['stop'])()) {
                return ['ok' => false, 'output' => '已停止'];
            }
            $sock = @fsockopen($ip, (int) ($opts['port'] ?? 22), $en, $es, 5);
            if ($sock === false) {
                $last = '端口未开放：' . $es;
                sleep(5);
                continue;
            }
            fclose($sock);
            $probe = $opts;
            $probe['connect_timeout'] = 8;
            $probe['total_timeout'] = 20;
            $r = self::run($ip, $user, $password, 'echo __SSH_READY__', $probe);
            if ($r['ok'] && strpos($r['output'], '__SSH_READY__') !== false) {
                return ['ok' => true, 'output' => 'ready'];
            }
            $last = $r['output'] ?: 'ssh 认证失败';
            sleep(5);
        }
        return ['ok' => false, 'output' => $last];
    }

    /** root 密码模式的开机脚本（逐字移植 core/ssh.py 的模板） */
    public static function buildRootStartupScript(string $rootPassword): string
    {
        $tpl = <<<'BASH'
#!/bin/bash
set -euxo pipefail
mkdir -p /root
LOG_FILE=/root/gcp_root_mode.log
exec > >(tee -a "$LOG_FILE") 2>&1
echo "[INFO] starting root password mode setup"
export DEBIAN_FRONTEND=noninteractive
echo 'root:__PWD__' | chpasswd
passwd -u root || true
if [ -f /etc/ssh/sshd_config ]; then
  sed -i 's/^\s*#\?\s*PermitRootLogin.*/PermitRootLogin yes/g' /etc/ssh/sshd_config || true
  sed -i 's/^\s*#\?\s*PasswordAuthentication.*/PasswordAuthentication yes/g' /etc/ssh/sshd_config || true
  sed -i 's/^\s*#\?\s*KbdInteractiveAuthentication.*/KbdInteractiveAuthentication yes/g' /etc/ssh/sshd_config || true
  sed -i 's/^\s*#\?\s*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication yes/g' /etc/ssh/sshd_config || true
  grep -q '^PermitRootLogin yes$' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config
  grep -q '^PasswordAuthentication yes$' /etc/ssh/sshd_config || echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config
  grep -q '^KbdInteractiveAuthentication yes$' /etc/ssh/sshd_config || echo 'KbdInteractiveAuthentication yes' >> /etc/ssh/sshd_config
  grep -q '^ChallengeResponseAuthentication yes$' /etc/ssh/sshd_config || echo 'ChallengeResponseAuthentication yes' >> /etc/ssh/sshd_config
fi
rm -rf /etc/ssh/sshd_config.d/* /etc/ssh/ssh_config.d/* || true
mkdir -p /etc/ssh/sshd_config.d /etc/ssh/ssh_config.d
cat >/etc/ssh/sshd_config.d/99-root-password.conf <<'EOF'
PermitRootLogin yes
PasswordAuthentication yes
KbdInteractiveAuthentication yes
ChallengeResponseAuthentication yes
UsePAM yes
EOF
sshd -t || sshd -T || true
systemctl restart ssh || systemctl restart sshd || service ssh restart || service sshd restart || true
sleep 2
echo "[INFO] root password mode setup finished"
touch /root/.gcp_root_mode_ok
BASH;
        // 单引号转义，等价于 Python 版的 replace("'", "'\"'\"'")
        $safe = str_replace("'", "'\"'\"'", $rootPassword);
        return str_replace('__PWD__', $safe, $tpl);
    }

    /** 生成一段随机 root 密码（字母数字 + 少量符号，与 Python _rand_password 同构） */
    public static function randPassword(int $length = 16): string
    {
        $alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#%^*-_';
        $max = strlen($alphabet) - 1;
        $out = '';
        for ($i = 0; $i < $length; $i++) {
            $out .= $alphabet[random_int(0, $max)];   // S8：用 random_int
        }
        return $out;
    }

    // ==================================================================
    // 内部：ssh argv 组装 与 子进程执行
    // ==================================================================
    private static function buildSshArgv(string $ip, string $user, int $port, int $connT,
                                         ?string $keyPath, array $opts): array
    {
        $knownHosts = $opts['known_hosts'] ?? (Config::keysDir() . '/known_hosts');
        if (!is_dir(dirname($knownHosts))) {
            @mkdir(dirname($knownHosts), 0700, true);
        }
        $base = ['ssh',
            '-o', 'StrictHostKeyChecking=accept-new',   // TOFU：首连记录，变更拒连
            '-o', 'UserKnownHostsFile=' . $knownHosts,
            '-o', 'ConnectTimeout=' . max(1, $connT),
            '-o', 'ServerAliveInterval=30',
            '-o', 'LogLevel=ERROR',
            '-o', 'GlobalKnownHostsFile=/dev/null',
            '-p', (string) $port,
        ];
        if ($keyPath !== null && $keyPath !== '') {
            $base[] = '-o';
            $base[] = 'BatchMode=yes';
            $base[] = '-i';
            $base[] = $keyPath;
            $base[] = '-o';
            $base[] = 'IdentitiesOnly=yes';
        }
        $base[] = $user . '@' . $ip;
        return $base;
    }

    /**
     * 执行一条 ssh 命（argv 已含 ssh 及其参数）。密码走 sshpass -e（环境变量）。
     */
    private static function execSsh(array $sshArgv, string $password, ?string $stdinFile,
                                    int $totalTimeout, $logCb, $stopCb, array $opts): array
    {
        $env = [];
        $argv = $sshArgv;
        if ($password !== '') {
            // sshpass 从 SSHPASS 环境变量读密码，密码不进 argv
            if (!self::hasBinary('sshpass')) {
                return ['ok' => false, 'output' => '需要密码认证但未安装 sshpass（apt/yum install sshpass）'];
            }
            array_unshift($argv, 'sshpass', '-e');
            $env['SSHPASS'] = $password;
        }
        $r = self::procRun($argv, $env, $stdinFile, $totalTimeout, $opts['idle_timeout'] ?? null, $logCb, $stopCb);
        $out = $r['out'];
        if ($r['err'] !== '') {
            $out .= ($out !== '' ? "\n" : '') . $r['err'];
        }
        if ($r['timedOut']) {
            $out .= "\n[总超时 {$totalTimeout}s，终止]";
        }
        if ($r['reapedSignal']) {
            $out .= "\n[已手动停止]";
        }
        return ['ok' => !$r['timedOut'] && !$r['reapedSignal'] && $r['code'] === 0, 'output' => $out];
    }

    /**
     * 执行外部命令（数组参数形式，无本地 shell）。
     *
     * @param array<string,string> $extraEnv 追加到继承环境之上的变量
     * @param ?callable $logCb 输出回调（实时回显）
     * @param ?callable $stopCb 返回 true 表示请求停止
     * @return array{code:int,out:string,err:string,timedOut:bool,reapedSignal:bool}
     */
    private static function procRun(array $argv, array $extraEnv = [], ?string $stdinFile = null,
                                    int $timeoutSec = 60, ?int $idleTimeoutSec = null,
                                    $logCb = null, $stopCb = null): array
    {
        $descriptors = [
            0 => $stdinFile !== null ? ['file', $stdinFile, 'r'] : ['file', '/dev/null', 'r'],
            1 => ['pipe', 'w'],
            2 => ['pipe', 'w'],
        ];
        // 继承当前环境（含 PATH），叠加额外变量；env=null 会丢失 PATH，故显式传
        $env = getenv();
        if (!is_array($env)) {
            $env = [];
        }
        foreach ($extraEnv as $k => $v) {
            $env[$k] = $v;
        }

        $proc = @proc_open($argv, $descriptors, $pipes, null, $env);
        if (!is_resource($proc)) {
            return ['code' => -1, 'out' => '', 'err' => '无法启动子进程', 'timedOut' => false, 'reapedSignal' => false];
        }
        foreach ([1, 2] as $fd) {
            stream_set_blocking($pipes[$fd], false);
        }

        $out = '';
        $err = '';
        $start = microtime(true);
        $lastData = $start;
        $timedOut = false;
        $stopped = false;
        $exitCode = null;   // ★ proc_get_status 只在进程结束后第一次调用返回有效 exitcode

        while (true) {
            $read = [];
            if (is_resource($pipes[1])) {
                $read[] = $pipes[1];
            }
            if (is_resource($pipes[2])) {
                $read[] = $pipes[2];
            }
            if ($read !== []) {
                $w = null;
                $e = null;
                @stream_select($read, $w, $e, 0, 200000);   // 最多等 0.2s
                foreach ($read as $stream) {
                    $chunk = @fread($stream, 65536);
                    if (!is_string($chunk) || $chunk === '') {
                        continue;
                    }
                    if ($stream === $pipes[1]) {
                        $out .= $chunk;
                    } else {
                        $err .= $chunk;
                    }
                    $lastData = microtime(true);
                    if (is_callable($logCb)) {
                        try {
                            $logCb($chunk);
                        } catch (Throwable $ignored) {
                        }
                    }
                }
            } else {
                usleep(50000);
            }

            $status = proc_get_status($proc);
            $running = (bool) ($status['running'] ?? false);
            if (!$running) {
                // 进程已退出：这一次的 exitcode 才是有效值，务必当场取走
                $exitCode = (int) ($status['exitcode'] ?? 0);
                break;
            }
            $now = microtime(true);
            if (is_callable($stopCb)) {
                try {
                    if ($stopCb()) {
                        $stopped = true;
                        break;
                    }
                } catch (Throwable $ignored) {
                }
            }
            if ($now - $lastData > ($idleTimeoutSec ?? $timeoutSec)) {
                $timedOut = true;
                break;
            }
            if ($now - $start > $timeoutSec) {
                $timedOut = true;
                break;
            }
        }

        // 收尾：把残余输出读干净，然后终止并回收
        foreach ([1, 2] as $fd) {
            if (is_resource($pipes[$fd])) {
                $rest = @stream_get_contents($pipes[$fd]);
                if (is_string($rest) && $rest !== '') {
                    if ($fd === 1) {
                        $out .= $rest;
                    } else {
                        $err .= $rest;
                    }
                }
                @fclose($pipes[$fd]);
            }
        }

        $code = 0;
        if ($timedOut || $stopped) {
            @proc_terminate($proc, 15);   // SIGTERM
            usleep(200000);
            $status = proc_get_status($proc);
            if (($status['running'] ?? false)) {
                @proc_terminate($proc, 9);   // SIGKILL
            }
        }
        // proc_close 会等待并回收子进程（杜绝僵尸），其返回值就是退出码
        $closeCode = @proc_close($proc);
        if ($exitCode !== null) {
            $code = $exitCode;
        } elseif ($closeCode !== -1) {
            $code = $closeCode;
        } else {
            $code = ($timedOut || $stopped) ? 143 : 0;
        }

        return [
            'code' => $code,
            'out' => $out,
            'err' => $err,
            'timedOut' => $timedOut,
            'reapedSignal' => $stopped,
        ];
    }

    private static function hasBinary(string $name): bool
    {
        $paths = explode(PATH_SEPARATOR, (string) getenv('PATH'));
        foreach ($paths as $p) {
            if ($p !== '' && is_executable($p . '/' . $name)) {
                return true;
            }
        }
        return is_executable('/usr/bin/' . $name) || is_executable('/usr/local/bin/' . $name);
    }
}
