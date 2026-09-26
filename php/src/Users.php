<?php
/**
 * Users —— 用户 / 会话 / 审计 存储层
 *
 * 与 Python 版 core/users.py 的关系
 *   本类是 `class UserStore` 的逐方法移植，表结构与列名逐列一致（INTERFACES 第 4 节），
 *   因此 PHP 版与 Python 版可共用同一个 SQLite 文件：Python 建的账号 PHP 能登录，
 *   反之亦然（哈希算法逐位相同，见 Auth::hashPassword）。
 *
 * 关键取舍
 *   · 并发：不模拟 Python 的 threading.RLock，统一靠 SQLite WAL + BEGIN IMMEDIATE
 *     （Db.php 已配 busy_timeout=5000）。写事务保持短小。
 *   · 用户名校验必须在服务端做：前端正则可被绕过，实测用 <img src=x onerror=...>
 *     当用户名能建号并原样回显（潜在存储型 XSS）。口径与前端一致：
 *     字母/数字/下划线/点/横线/@，长度 2-40（按字符数计，非字节数）。
 *   · display_name 截断 80（按字符），并拒绝控制字符。
 *   · 所有 SQL 一律 PDO 预处理；可写列名只来自硬编码白名单（红线 S2）。
 */

declare(strict_types=1);

final class Users
{
    /** updateUser 允许写入的列（硬编码白名单，防止列名污染） */
    private const UPDATABLE = ['role', 'display_name', 'disabled', 'must_change_password'];

    // ------------------------------------------------------------------
    // 建表
    // ------------------------------------------------------------------
    public static function ensure(): void
    {
        self::initSchema();
    }

    private static function initSchema(): void
    {
        Db::exec("CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'operator',
            display_name TEXT DEFAULT '',
            disabled INTEGER DEFAULT 0,
            must_change_password INTEGER DEFAULT 0,
            last_login REAL,
            last_ip TEXT DEFAULT '',
            login_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            created_at REAL,
            updated_at REAL,
            created_by TEXT DEFAULT 'system')");

        Db::exec("CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            ip TEXT DEFAULT '',
            user_agent TEXT DEFAULT '',
            created_at REAL,
            last_seen REAL,
            expires_at REAL)");

        Db::exec("CREATE TABLE IF NOT EXISTS audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, username TEXT, ip TEXT, action TEXT,
            target TEXT DEFAULT '', detail TEXT DEFAULT '', ok INTEGER DEFAULT 1)");

        Db::exec('CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)');
        Db::exec('CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts)');
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
    // 用户
    // ------------------------------------------------------------------
    public static function countUsers(): int
    {
        self::boot();
        return (int) Db::scalar('SELECT COUNT(*) FROM users');
    }

    /**
     * 建号。校验失败抛 InvalidArgumentException（调用方转 400），
     * 用户名重复同样抛 InvalidArgumentException（与 Python 的 ValueError 语义一致）。
     *
     * @return int 新用户 id
     */
    public static function createUser(string $username, string $password,
                                      string $role = 'operator', string $displayName = '',
                                      bool $mustChange = false, string $createdBy = 'system'): int
    {
        self::boot();
        $username = trim($username);
        if ($username === '') {
            throw new InvalidArgumentException('用户名不能为空');
        }
        // 长度按「字符」计（与 Python len() 一致，不用 strlen 的字节数）
        $ulen = mb_strlen($username, 'UTF-8');
        if ($ulen < 2 || $ulen > 40) {
            throw new InvalidArgumentException('用户名长度需在 2-40 之间');
        }
        foreach (mb_str_split($username, 1, 'UTF-8') as $ch) {
            $asciiAlnum = preg_match('/^[A-Za-z0-9]$/', $ch) === 1;
            if (!$asciiAlnum && strpos('_.@-', $ch) === false) {
                throw new InvalidArgumentException('用户名只能包含字母、数字、下划线、点、横线或 @');
            }
        }
        // display_name：截断 80 字符后拒绝控制字符
        $displayName = mb_substr(trim($displayName), 0, 80, 'UTF-8');
        if (preg_match('/[\x00-\x1F]/', $displayName) === 1) {
            throw new InvalidArgumentException('显示名不能包含控制字符');
        }
        if (!in_array($role, Auth::ROLES, true)) {
            throw new InvalidArgumentException('非法角色：' . $role);
        }

        [$pwHash, $salt] = Auth::hashPassword($password);
        $now = microtime(true);
        try {
            Db::exec(
                'INSERT INTO users(username,password_hash,salt,role,display_name,'
                . 'disabled,must_change_password,created_at,updated_at,created_by)'
                . ' VALUES(?,?,?,?,?,0,?,?,?,?)',
                [$username, $pwHash, $salt, $role, $displayName,
                 $mustChange ? 1 : 0, $now, $now, $createdBy]
            );
        } catch (PDOException $e) {
            if (self::isUniqueViolation($e)) {
                throw new InvalidArgumentException('用户名已存在：' . $username);
            }
            throw $e;
        }
        return (int) Db::conn()->lastInsertId();
    }

    /** 按 id 或 username 取用户（返回含 password_hash/salt 的完整行） */
    public static function getUser(?int $userId = null, ?string $username = null): ?array
    {
        self::boot();
        if ($userId !== null) {
            return Db::one('SELECT * FROM users WHERE id=?', [$userId]);
        }
        return Db::one('SELECT * FROM users WHERE username=?', [$username]);
    }

    /** 用户列表（默认含被禁用者）；**始终剥离 password_hash / salt** */
    public static function listUsers(bool $includeDisabled = true): array
    {
        self::boot();
        $sql = 'SELECT * FROM users' . ($includeDisabled ? '' : ' WHERE disabled=0');
        $rows = Db::all($sql . ' ORDER BY id');
        $out = [];
        foreach ($rows as $r) {
            unset($r['password_hash'], $r['salt']);
            $out[] = $r;
        }
        return $out;
    }

    /** 更新 role / display_name / disabled / must_change_password；返回受影响行数 */
    public static function updateUser(int $userId, array $kw): int
    {
        self::boot();
        $fields = [];
        foreach (self::UPDATABLE as $k) {
            if (array_key_exists($k, $kw)) {
                $fields[] = $k;
            }
        }
        if ($fields === []) {
            return 0;
        }
        if (array_key_exists('role', $kw) && !in_array($kw['role'], Auth::ROLES, true)) {
            throw new InvalidArgumentException('非法角色：' . $kw['role']);
        }
        $sets = implode(',', array_map(static fn(string $f): string => $f . '=?', $fields)) . ',updated_at=?';
        $args = array_map(static fn(string $f) => $kw[$f], $fields);
        $args[] = microtime(true);
        $args[] = $userId;
        return Db::exec('UPDATE users SET ' . $sets . ' WHERE id=?', $args);
    }

    /** 设置密码（统一走 Auth::hashPassword，绝不出现第二套哈希实现） */
    public static function setPassword(int $userId, string $newPassword, bool $mustChange = false): void
    {
        self::boot();
        [$pwHash, $salt] = Auth::hashPassword($newPassword);
        Db::exec(
            'UPDATE users SET password_hash=?,salt=?,must_change_password=?,updated_at=? WHERE id=?',
            [$pwHash, $salt, $mustChange ? 1 : 0, microtime(true), $userId]
        );
    }

    /** 删号（连同其会话）；返回删除的用户行数 */
    public static function deleteUser(int $userId): int
    {
        self::boot();
        return Db::tx(static function (PDO $pdo) use ($userId): int {
            $pdo->prepare('DELETE FROM sessions WHERE user_id=?')->execute([$userId]);
            $st = $pdo->prepare('DELETE FROM users WHERE id=?');
            $st->execute([$userId]);
            return $st->rowCount();
        });
    }

    /**
     * 校验登录凭据。返回 [user_dict|null, 原因]。
     * 用户不存在时也做一次哈希，抹平「用户是否存在」的时间差，防用户名枚举。
     */
    public static function verifyLogin(string $username, string $password): array
    {
        self::boot();
        $u = self::getUser(null, $username);
        if (!$u) {
            // 恒定耗时路径：用户名不存在也做一次同等代价的哈希
            Auth::hashPassword($password !== '' ? $password : 'x');
            return [null, '用户名或密码错误'];
        }
        if ((int) $u['disabled'] !== 0) {
            return [null, '该账号已被禁用'];
        }
        if (!Auth::verifyPassword($password, (string) $u['password_hash'], (string) $u['salt'])) {
            Db::exec('UPDATE users SET failed_count=failed_count+1 WHERE id=?', [(int) $u['id']]);
            return [null, '用户名或密码错误'];
        }
        return [$u, 'ok'];
    }

    /** 记录一次成功登录：last_login / last_ip / login_count+1 / failed_count 清零 */
    public static function markLogin(int $userId, string $ip): void
    {
        self::boot();
        $now = microtime(true);
        Db::exec(
            'UPDATE users SET last_login=?,last_ip=?,login_count=login_count+1,'
            . 'failed_count=0,updated_at=? WHERE id=?',
            [$now, $ip, $now, $userId]
        );
    }

    // ------------------------------------------------------------------
    // 会话
    // ------------------------------------------------------------------
    /**
     * 建会话。
     *
     * ★ 会话固定（session fixation）防护：token 用密码学随机源生成
     *   （bin2hex(random_bytes(32))），并**先删掉该用户的旧会话**再插入新会话。
     *   这样即便攻击者事先种下 / 猜到某个 token 也无法维系，登录后只会存在
     *   一条由本次登录签发的新 token。
     *
     * @return array{0:string,1:float} [token, expires_at]
     */
    public static function createSession(array $user, string $ip, string $userAgent, int $ttl = Auth::SESSION_TTL): array
    {
        self::boot();
        $token = bin2hex(random_bytes(32));
        $now = microtime(true);
        $expires = $now + $ttl;
        Db::tx(static function (PDO $pdo) use ($token, $user, $ip, $userAgent, $now, $expires): void {
            // 防会话固定：清掉该用户此前所有会话
            $pdo->prepare('DELETE FROM sessions WHERE user_id=?')->execute([(int) $user['id']]);
            $pdo->prepare(
                'INSERT INTO sessions(token,user_id,username,role,ip,user_agent,'
                . 'created_at,last_seen,expires_at) VALUES(?,?,?,?,?,?,?,?,?)'
            )->execute([
                $token, (int) $user['id'], (string) $user['username'], (string) $user['role'],
                $ip, mb_substr($userAgent, 0, 300, 'UTF-8'), $now, $now, $expires,
            ]);
        });
        return [$token, $expires];
    }

    /**
     * 取会话（含联动用户状态）。过期 / 用户被禁用 → 回收并返回 null；
     * 命中则按 SESSION_TOUCH 节流做滑动续期。
     */
    public static function getSession(string $token): ?array
    {
        self::boot();
        if ($token === '') {
            return null;
        }
        $sess = Db::one(
            'SELECT s.*, u.disabled AS user_disabled, u.must_change_password AS must_change '
            . 'FROM sessions s LEFT JOIN users u ON u.id=s.user_id WHERE s.token=?',
            [$token]
        );
        if ($sess === null) {
            return null;
        }
        $now = microtime(true);
        if ((float) $sess['expires_at'] < $now) {
            self::revokeSession($token);
            return null;
        }
        if (!empty($sess['user_disabled'])) {
            self::revokeSession($token);
            return null;
        }
        // 滑动续期（限频写库，5 分钟内不重复写）
        if ($now - (float) ($sess['last_seen'] ?? 0) > Auth::SESSION_TOUCH) {
            Db::exec('UPDATE sessions SET last_seen=? WHERE token=?', [$now, $token]);
        }
        return $sess;
    }

    public static function revokeSession(string $token): void
    {
        self::boot();
        Db::exec('DELETE FROM sessions WHERE token=?', [$token]);
    }

    /** 吊销某用户的全部会话；返回被删行数 */
    public static function revokeUserSessions(int $userId): int
    {
        self::boot();
        return Db::exec('DELETE FROM sessions WHERE user_id=?', [$userId]);
    }

    /** 未过期会话列表（按 last_seen 倒序）；含 token 字段，由调用方决定是否脱敏 */
    public static function listSessions(): array
    {
        self::boot();
        return Db::all(
            'SELECT token,user_id,username,role,ip,user_agent,created_at,last_seen,'
            . 'expires_at FROM sessions WHERE expires_at>? ORDER BY last_seen DESC',
            [microtime(true)]
        );
    }

    /** 清理过期会话；返回清理行数 */
    public static function purgeExpiredSessions(): int
    {
        self::boot();
        return Db::exec('DELETE FROM sessions WHERE expires_at<?', [microtime(true)]);
    }

    // ------------------------------------------------------------------
    // 审计
    // ------------------------------------------------------------------
    public static function addAudit(string $username, string $ip, string $action,
                                    string $target = '', string $detail = '', bool $ok = true): void
    {
        self::boot();
        Db::exec(
            'INSERT INTO audit(ts,username,ip,action,target,detail,ok) VALUES(?,?,?,?,?,?,?)',
            [microtime(true), $username !== '' ? $username : '-', $ip !== '' ? $ip : '-',
             $action, $target, $detail, $ok ? 1 : 0]
        );
    }

    /** 审计总数（分页用） */
    public static function countAudit(): int
    {
        self::boot();
        return (int) Db::scalar('SELECT COUNT(*) FROM audit');
    }

    /** 按 ts 倒序取一页审计记录 */
    public static function getAudit(int $limit = 200, int $offset = 0): array
    {
        self::boot();
        return Db::all(
            'SELECT * FROM audit ORDER BY ts DESC LIMIT ? OFFSET ?',
            [max(0, $limit), max(0, $offset)]
        );
    }

    // ------------------------------------------------------------------
    // 内部工具
    // ------------------------------------------------------------------
    /** 判断是否为 SQLite UNIQUE 约束冲突（用户名重复） */
    private static function isUniqueViolation(PDOException $e): bool
    {
        $sqlState = (string) ($e->errorInfo[0] ?? $e->getCode());
        return $sqlState === '23000' || stripos($e->getMessage(), 'UNIQUE') !== false;
    }
}
