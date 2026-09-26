<?php
/**
 * Auth —— 认证与授权核心（哈希 / 验证码 / 限速 / 会话 / 权限点）
 *
 * 与 Python 版 core/auth.py 的关系
 *   · hashPassword / verifyPassword / passwordStrength / randomPassword 逐行对拍，
 *     确保与 Python 版产出的密码哈希**逐位相同**（同一 SQLite 库可互操作）。
 *   · CaptchaStore / LoginGuard 是 Python 内存态实现的**跨进程等价物**。
 *
 * ★ 为什么验证码与限速必须落 SQLite（而不是内存数组 / APCu）
 *   PHP-FPM 是多进程模型：进程间不共享内存，请求之间也不保证落在同一进程。
 *   Python 版是单进程多线程，用 dict + 锁即可；PHP 若用内存态会导致
 *   「验证码刚生成、下一个请求就查不到」「爆破计数被进程分散」。
 *   因此把这两张表（captcha / login_guard）落进 SQLite，天然跨进程可见。
 *   这两张表是运行期状态表，不属于 INTERFACES 第 4 节的互操作数据模型。
 *
 * ★ 容量上限（契约红线 S12：未认证接口必须有容量上限）
 *   · captcha：MAX_ITEMS=20000。**每次生成都做容量检查**（不受 30s 过期清理节流影响），
 *     超限即从最旧的一条开始淘汰 —— 否则匿名刷验证码能把磁盘打满。
 *   · login_guard：MAX_TRACKED=20000，按 (scope,key) 计；剪枝**只清已到期记录**，
 *     until=0 表示「从未锁定」，绝不能被当成「已解锁」而误删（falsy 陷阱）。
 *
 * ★ 密码哈希（互操作关键，见 INTERFACES 第 4 节）
 *   salt = 32 位十六进制（=16 字节）
 *   hash = PBKDF2-HMAC-SHA256(password, raw_salt_bytes, 200000, 32 字节) 的 hex
 *   即 PHP: hash_pbkdf2('sha256', $password, hex2bin($salt), 200000, 64)
 *   第 4 参数 200000、第 5 参数 64（hex 字符数，等价 32 字节）二者缺一不可；
 *   盐必须先 hex2bin。比较一律用恒定时间 hash_equals。
 */

declare(strict_types=1);

final class Auth
{
    // ---- 常量（与 Python 版一致，见 INTERFACES 第 5 节） ----
    public const PBKDF2_ROUNDS = 200000;
    public const SESSION_TTL   = 43200;   // 12h
    public const CAPTCHA_TTL   = 300;
    public const CAPTCHA_LEN   = 4;
    public const MAX_FAIL_PER_ACCOUNT = 6;
    public const MAX_FAIL_PER_IP      = 20;
    public const LOCK_SECONDS         = 300;
    public const CAPTCHA_ALPHABET     = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';
    public const SESSION_TOUCH        = 300;
    public const CAPTCHA_MAX_ITEMS    = 20000;
    public const LOGIN_MAX_TRACKED    = 20000;

    /** 会话 Cookie 名（与 Python 版一致） */
    public const COOKIE_NAME = 'gcp_sid';

    /** 角色（顺序与 Python ROLES 一致） */
    public const ROLES = ['admin', 'operator', 'viewer'];
    public const ROLE_ADMIN    = 'admin';
    public const ROLE_OPERATOR = 'operator';
    public const ROLE_VIEWER   = 'viewer';

    public const ROLE_LABELS = [
        'admin'    => '管理员',
        'operator' => '运维',
        'viewer'   => '只读',
    ];

    /** 权限点 → 允许的角色（与 Python PERMISSIONS 一致） */
    public const PERMISSIONS = [
        'view'     => ['admin', 'operator', 'viewer'],
        'operate'  => ['admin', 'operator'],
        'account'  => ['admin', 'operator'],
        'settings' => ['admin', 'operator'],
        'user'     => ['admin'],
    ];

    /** 强制改密状态下仍放行的路径（与 Python auth_middleware 一致） */
    public const MUST_CHANGE_ALLOWED = [
        '/api/auth/change_password',
        '/api/auth/logout',
        '/api/auth/me',
        '/api/auth/password_policy',
    ];

    // ------------------------------------------------------------------
    // 建表（验证码 / 限速 运行期状态表）
    // ------------------------------------------------------------------
    public static function ensure(): void
    {
        self::initSchema();
    }

    private static function initSchema(): void
    {
        Db::exec("CREATE TABLE IF NOT EXISTS captcha(
            cid TEXT PRIMARY KEY, code TEXT NOT NULL,
            expire REAL NOT NULL, created REAL NOT NULL)");

        Db::exec("CREATE TABLE IF NOT EXISTS auth_runtime(
            k TEXT PRIMARY KEY, v REAL NOT NULL)");

        Db::exec("CREATE TABLE IF NOT EXISTS login_guard(
            scope TEXT NOT NULL, k TEXT NOT NULL,
            cnt INTEGER NOT NULL DEFAULT 0, until REAL NOT NULL DEFAULT 0,
            created REAL NOT NULL, PRIMARY KEY(scope,k))");
    }

    private static function boot(): void
    {
        static $done = false;
        if ($done) {
            return;
        }
        $done = true;
        self::initSchema();
    }

    // ------------------------------------------------------------------
    // 密码哈希（务必与 Python 逐位一致）
    // ------------------------------------------------------------------
    /**
     * 生成密码哈希。
     * @return array{0:string,1:string} [hash_hex(64), salt_hex(32)]
     */
    public static function hashPassword(string $password, ?string $salt = null): array
    {
        if ($salt === null || $salt === '') {
            // 32 位十六进制 = 16 字节随机盐（与 Python secrets.token_hex(16) 同形）
            $salt = bin2hex(random_bytes(16));
        }
        if (preg_match('/^[0-9a-fA-F]{32}$/', $salt) !== 1) {
            throw new InvalidArgumentException('密码盐格式不合法（应为 32 位十六进制）');
        }
        $raw = hex2bin($salt);
        // ★ 200000 轮、输出 64 个 hex 字符（=32 字节）；盐必须先 hex2bin 成原始字节
        $dk = hash_pbkdf2('sha256', $password, $raw, self::PBKDF2_ROUNDS, 64);
        return [$dk, $salt];
    }

    /** 恒定时间校验密码；哈希或盐为空一律 false（与 Python verify_password 一致） */
    public static function verifyPassword(string $password, ?string $passwordHash, ?string $salt): bool
    {
        if ($passwordHash === null || $passwordHash === '' || $salt === null || $salt === '') {
            return false;
        }
        [$calc, ] = self::hashPassword($password, $salt);
        return hash_equals($calc, $passwordHash);
    }

    /**
     * 密码强度评估。
     * @return array{0:int,1:string} [0..4, 中文标签]
     */
    public static function passwordStrength(string $pw): array
    {
        if ($pw === '') {
            return [0, '密码不能为空'];
        }
        $score = 0;
        $len = mb_strlen($pw, 'UTF-8');
        if ($len >= 8) {
            $score++;
        }
        if ($len >= 12) {
            $score++;
        }
        // 大小写各至少一个（Unicode 小写/大写类，对齐 Python islower/ isupper）
        if (preg_match('/\p{Ll}/u', $pw) === 1 && preg_match('/\p{Lu}/u', $pw) === 1) {
            $score++;
        }
        // 数字或符号至少一类
        $special = '!@#$%^&*()-_=+[]{};:,.<>?/|~`';
        $hasSpecial = false;
        foreach (mb_str_split($pw, 1, 'UTF-8') as $c) {
            if (strpos($special, $c) !== false) {
                $hasSpecial = true;
                break;
            }
        }
        if (preg_match('/\p{Nd}/u', $pw) === 1 || $hasSpecial) {
            $score++;
        }
        if ($len < 8) {
            return [min($score, 1), '至少 8 位'];
        }
        $labels = ['很弱', '弱', '一般', '较强', '强'];
        return [$score, $labels[min($score, 4)]];
    }

    /** 生成随机密码，循环直到强度分 >= 3（与 Python random_password 一致） */
    public static function randomPassword(int $length = 14): string
    {
        $alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#%^*-_';
        $n = strlen($alphabet);
        $length = max(1, $length);
        while (true) {
            $pw = '';
            for ($i = 0; $i < $length; $i++) {
                $pw .= $alphabet[random_int(0, $n - 1)];
            }
            [$score, ] = self::passwordStrength($pw);
            if ($score >= 3) {
                return $pw;
            }
        }
    }

    // ------------------------------------------------------------------
    // 角色 / 权限点
    // ------------------------------------------------------------------
    public static function roleLabel(string $role): string
    {
        return self::ROLE_LABELS[$role] ?? $role;
    }

    /** 某角色拥有的全部权限点（用于 /api/auth/me 的 permissions 字段） */
    public static function permsForRole(string $role): array
    {
        $out = [];
        foreach (self::PERMISSIONS as $perm => $roles) {
            if (in_array($role, $roles, true)) {
                $out[] = $perm;
            }
        }
        return $out;
    }

    public static function isMustChangeAllowed(string $path): bool
    {
        return in_array($path, self::MUST_CHANGE_ALLOWED, true);
    }

    // ------------------------------------------------------------------
    // 会话解析 / 鉴权
    // ------------------------------------------------------------------
    /** 取会话 token：优先 Cookie，其次 Authorization: Bearer（与 Python 中间件一致） */
    public static function parseSessionToken(): ?string
    {
        $t = $_COOKIE[self::COOKIE_NAME] ?? null;
        if (is_string($t) && $t !== '') {
            return $t;
        }
        $auth = $_SERVER['HTTP_AUTHORIZATION'] ?? ($_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '');
        if (is_string($auth) && stripos($auth, 'bearer ') === 0) {
            $t = trim(substr($auth, 7));
            if ($t !== '') {
                return $t;
            }
        }
        return null;
    }

    /** 当前会话（含 must_change / user_disabled 联动）；未登录/过期返回 null */
    public static function currentSession(): ?array
    {
        $token = self::parseSessionToken();
        if ($token === null || $token === '') {
            return null;
        }
        return Users::getSession($token);
    }

    /**
     * 校验权限点；不通过直接发响应并终止请求（永不返回）。
     * 与 Python require() 语义一致：未登录 401，角色不匹配 403。
     */
    public static function requirePerm(string $perm): array
    {
        $sess = self::currentSession();
        if ($sess === null) {
            Json::err('未登录', 401, 'unauthorized');
        }
        $allowed = self::PERMISSIONS[$perm] ?? [];
        if (!in_array((string) $sess['role'], $allowed, true)) {
            Json::err('当前角色（' . self::roleLabel((string) $sess['role']) . '）无权执行此操作', 403);
        }
        return $sess;
    }

    // ------------------------------------------------------------------
    // 单例入口
    // ------------------------------------------------------------------
    private static ?CaptchaStore $captcha = null;
    private static ?LoginGuard $guard = null;

    public static function captcha(): CaptchaStore
    {
        return self::$captcha ??= new CaptchaStore();
    }

    public static function loginGuard(): LoginGuard
    {
        return self::$guard ??= new LoginGuard();
    }

    /**
     * 直接产出 GET /api/auth/captcha 的响应体（含 ok），供 index.php 一句话返回。
     * @return array{captcha_id:string,image:string,expires_in:int,ok:bool}
     */
    public static function captchaNew(): array
    {
        [$cid, $code] = self::captcha()->create();
        return [
            'ok'         => true,
            'captcha_id' => $cid,
            'image'      => CaptchaImage::dataUri($code),
            'expires_in' => self::CAPTCHA_TTL,
        ];
    }
}

/**
 * CaptchaStore —— 一次性图形验证码的跨进程存储（落 SQLite 表 captcha）
 *
 * 语义与 Python core.auth.CaptchaStore 一致：
 *   · 生成 → 返回 (cid, code)；校验 → 无论对错都消费掉（防重放）。
 *   · 容量上限 MAX_ITEMS=20000，**每次生成都做容量检查**；
 *     过期记录的清理按 30 秒节流（避免每次匿名请求都做全表 DELETE）。
 */
final class CaptchaStore
{
    public const MAX_ITEMS = 20000;
    private const GC_INTERVAL = 30.0;

    /**
     * 容量 + 过期清理。
     * 注意：容量检查在节流之外 —— 即使刚 GC 过，超限也必须立刻淘汰最旧记录。
     */
    private function gc(): void
    {
        $n = (int) Db::scalar('SELECT COUNT(*) FROM captcha');
        // 从最旧的一条开始淘汰，直到容量降到上限之下（正常最多循环 1 次）
        while ($n >= self::MAX_ITEMS) {
            Db::exec(
                'DELETE FROM captcha WHERE cid='
                . '(SELECT cid FROM captcha ORDER BY created ASC, rowid ASC LIMIT 1)'
            );
            $n--;
        }

        $now = microtime(true);
        $last = (float) (Db::scalar("SELECT v FROM auth_runtime WHERE k='captcha_last_gc'") ?: 0.0);
        if ($now - $last < self::GC_INTERVAL) {
            return;   // 未到清理节流窗口
        }
        Db::exec('DELETE FROM captcha WHERE expire<?', [$now]);
        Db::exec(
            "INSERT INTO auth_runtime(k,v) VALUES('captcha_last_gc',?)"
            . ' ON CONFLICT(k) DO UPDATE SET v=excluded.v',
            [$now]
        );
    }

    /** 生成新验证码；返回 [cid, code] */
    public function create(): array
    {
        Auth::ensure();
        $this->gc();
        $alphabet = Auth::CAPTCHA_ALPHABET;
        $n = strlen($alphabet);
        $code = '';
        for ($i = 0; $i < Auth::CAPTCHA_LEN; $i++) {
            $code .= $alphabet[random_int(0, $n - 1)];
        }
        $cid = bin2hex(random_bytes(16));
        $now = microtime(true);
        Db::exec(
            'INSERT INTO captcha(cid,code,expire,created) VALUES(?,?,?,?)',
            [$cid, mb_strtoupper($code, 'UTF-8'), $now + Auth::CAPTCHA_TTL, $now]
        );
        return [$cid, $code];
    }

    /**
     * 校验验证码，**无论成败都已消费**（一个验证码只能用一次）。
     * @return array{0:bool,1:string} [是否通过, 原因]
     */
    public function verify(?string $cid, ?string $code): array
    {
        Auth::ensure();
        $cid  = (string) $cid;
        $code = (string) $code;
        if ($cid === '' || $code === '') {
            return [false, '缺少验证码'];
        }
        // 弹出式消费：SELECT + DELETE 放在同一 BEGIN IMMEDIATE 事务里，
        // 并发重放时只有一个请求能拿到记录。
        $item = Db::tx(static function (PDO $pdo) use ($cid): ?array {
            $st = $pdo->prepare('SELECT code,expire FROM captcha WHERE cid=?');
            $st->execute([$cid]);
            $row = $st->fetch();
            $pdo->prepare('DELETE FROM captcha WHERE cid=?')->execute([$cid]);
            return $row === false ? null : $row;
        });
        if ($item === null) {
            return [false, '验证码已失效，请重新获取'];
        }
        if ((float) $item['expire'] < microtime(true)) {
            return [false, '验证码已过期'];
        }
        if (mb_strtoupper(trim($code), 'UTF-8') !== (string) $item['code']) {
            return [false, '验证码错误'];
        }
        return [true, 'ok'];
    }
}

/**
 * LoginGuard —— 登录/敏感操作的失败限速（落 SQLite 表 login_guard）
 *
 * 语义与 Python core.auth.LoginGuard 一致：按「用户名」与「IP」双维度独立计数，
 * 任一维度触发阈值即锁定 LOCK_SECONDS 秒。
 *   · 账号维度：MAX_FAIL_PER_ACCOUNT = 6
 *   · IP 维度：  MAX_FAIL_PER_IP      = 20
 * 计数达阈值时锁定期开始并把计数清零（这样锁定期间若还在尝试，会重新累计）。
 *
 * ★ falsy 陷阱：until=0 表示「未锁定」，只判 `until > 0 && until <= now` 才算已到期
 *   可清理；若写成 `if ($until && $until <= $now)` 尚可，但绝不能用 `if (!$until)` 判定
 *   为「已解锁」而不分情况删除 —— 那样会在计数尚未达阈值时误删计数记录，等于绕过限速。
 */
final class LoginGuard
{
    public const MAX_TRACKED = 20000;

    /** @return array{0:bool,1:string} [是否放行, 原因] */
    public function check(?string $username, ?string $ip): array
    {
        Auth::ensure();
        $now = microtime(true);
        [$okUser, $waitUser] = $this->checkScope('user', $this->userKey($username), $now);
        [$okIp, $waitIp]     = $this->checkScope('ip', $this->ipKey($ip), $now);

        if (!$okUser) {
            return [false, '账号已被临时锁定，请 ' . $waitUser . ' 秒后重试'];
        }
        if (!$okIp) {
            return [false, '当前 IP 尝试过于频繁，请 ' . $waitIp . ' 秒后重试'];
        }
        return [true, 'ok'];
    }

    /** 记录一次失败（达阈值即锁定并清零计数） */
    public function fail(?string $username, ?string $ip): void
    {
        Auth::ensure();
        $now = microtime(true);
        $this->bumpScope('user', $this->userKey($username), Auth::MAX_FAIL_PER_ACCOUNT, $now);
        $this->bumpScope('ip', $this->ipKey($ip), Auth::MAX_FAIL_PER_IP, $now);
    }

    /** 登录成功/校验通过后清零双维度计数 */
    public function reset(?string $username, ?string $ip): void
    {
        Auth::ensure();
        Db::exec("DELETE FROM login_guard WHERE (scope='user' AND k=?) OR (scope='ip' AND k=?)",
            [$this->userKey($username), $this->ipKey($ip)]);
    }

    // ---- 内部 ----
    private function userKey(?string $username): string
    {
        return mb_strtolower((string) $username, 'UTF-8');   // 与 Python (username or "").lower() 一致
    }

    private function ipKey(?string $ip): string
    {
        return ($ip === null || $ip === '') ? '-' : $ip;      // 与 Python (ip or "-") 一致
    }

    /** @return array{0:bool,1:int} [放行?, 剩余等待秒数] */
    private function checkScope(string $scope, string $key, float $now): array
    {
        $rec = Db::one('SELECT cnt,until FROM login_guard WHERE scope=? AND k=?', [$scope, $key]);
        if ($rec === null) {
            return [true, 0];
        }
        $until = (float) $rec['until'];
        // 仍在锁定期
        if ($until > 0 && $until > $now) {
            return [false, (int) ($until - $now)];
        }
        // 锁定期已过 → 清理该条（仅此时才是真正的「已到期」）
        if ($until > 0 && $until <= $now) {
            Db::exec('DELETE FROM login_guard WHERE scope=? AND k=?', [$scope, $key]);
        }
        return [true, 0];
    }

    /** 计数 +1；达阈值则写 until 并把计数清零 */
    private function bumpScope(string $scope, string $key, int $limit, float $now): void
    {
        Db::tx(static function (PDO $pdo) use ($scope, $key, $limit, $now): void {
            LoginGuard::prune($pdo, $scope, $now);
            $st = $pdo->prepare('SELECT cnt FROM login_guard WHERE scope=? AND k=?');
            $st->execute([$scope, $key]);
            $row = $st->fetch();
            if ($row === false) {
                $pdo->prepare(
                    'INSERT INTO login_guard(scope,k,cnt,until,created) VALUES(?,?,?,?,?)'
                )->execute([$scope, $key, 1, 0.0, $now]);
                return;
            }
            $cnt = (int) $row['cnt'] + 1;
            if ($cnt >= $limit) {
                // 触发锁定：置 until，计数清零（锁定期间继续尝试会重新累计）
                $pdo->prepare('UPDATE login_guard SET cnt=0, until=? WHERE scope=? AND k=?')
                    ->execute([$now + Auth::LOCK_SECONDS, $scope, $key]);
            } else {
                $pdo->prepare('UPDATE login_guard SET cnt=? WHERE scope=? AND k=?')
                    ->execute([$cnt, $scope, $key]);
            }
        });
    }

    /**
     * 容量剪枝（在事务内调用）。
     * 仅当该 scope 的记录数达到上限时触发：
     *   ① 先清「已到期」的（until > 0 且 until <= now）—— 绝不能碰 until=0 的记录；
     *   ② 仍超限则从最旧的开始淘汰，保证插入当前 key 后不超过上限。
     */
    private static function prune(PDO $pdo, string $scope, float $now): void
    {
        $st = $pdo->prepare('SELECT COUNT(*) FROM login_guard WHERE scope=?');
        $st->execute([$scope]);
        $n = (int) $st->fetchColumn();
        if ($n < self::MAX_TRACKED) {
            return;
        }
        $pdo->prepare('DELETE FROM login_guard WHERE scope=? AND until>0 AND until<=?')
            ->execute([$scope, $now]);
        while (true) {
            $st = $pdo->prepare('SELECT COUNT(*) FROM login_guard WHERE scope=?');
            $st->execute([$scope]);
            if ((int) $st->fetchColumn() < self::MAX_TRACKED) {
                break;
            }
            $pdo->prepare(
                'DELETE FROM login_guard WHERE scope=? AND k='
                . '(SELECT k FROM login_guard WHERE scope=? ORDER BY created ASC, rowid ASC LIMIT 1)'
            )->execute([$scope, $scope]);
        }
    }
}
