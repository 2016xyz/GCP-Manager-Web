<?php
/**
 * ApiAuth —— 认证 / 用户 / 会话 / 审计 的 HTTP handler
 *
 * ★ 本类中**没有任何鉴权判断**：登录态、强制改密、权限点全部由
 *   public/index.php 的中间件统一完成（见该文件顶部说明）。
 *   这样做的好处是：新增路由时不可能忘记鉴权 —— 路由表里没写权限点就进不来。
 *
 * 响应字段名严格对齐 Python 版 app.py 的对应 handler，
 * 保证前端 console.html / login.html 一行都不用改。
 */

declare(strict_types=1);

final class ApiAuth
{
    // ── 认证（公开）────────────────────────────────────────────────────────

    /** GET /api/auth/captcha → {ok, captcha_id, image, expires_in} */
    public static function captcha(array $p): void
    {
        Json::out(Auth::captchaNew());
    }

    /**
     * POST /api/auth/login
     * body: {username, password, captcha_id, captcha_code}
     * → {ok, user{...}, permissions[]}
     *
     * 校验顺序与 Python 版一致，且每一步都要写审计：
     *   ① 限速（按用户名 + IP 双维度）—— 先于验证码，省得被刷验证码图
     *   ② 验证码（一次性消费，无论成败，防重放）
     *   ③ 口令校验
     * 会话只走 HttpOnly Cookie，**不回传 token**（回传等于多一份可被
     * 前端 JS / 插件 / 反代日志窃取的凭据，而前端本来也不用它）。
     */
    public static function login(array $p): void
    {
        $body = Http::jsonBody();
        $username = trim((string) ($body['username'] ?? ''));
        $password = (string) ($body['password'] ?? '');
        $capId    = (string) ($body['captcha_id'] ?? '');
        $capCode  = (string) ($body['captcha_code'] ?? '');
        $ip       = Http::clientIp();

        // ① 限速
        [$ok, $why] = Auth::loginGuard()->check($username, $ip);
        if (!$ok) {
            Users::addAudit($username, $ip, 'login', '', $why, false);
            Json::err($why, 429);
        }

        // ② 验证码（先消费再判断，防重放）
        [$capOk, $capWhy] = Auth::captcha()->verify($capId, $capCode);
        if (!$capOk) {
            Auth::loginGuard()->fail($username, $ip);
            Users::addAudit($username, $ip, 'login', '', '验证码失败：' . $capWhy, false);
            Json::err($capWhy, 400);
        }

        // ③ 口令
        [$user, $reason] = Users::verifyLogin($username, $password);
        if ($user === null) {
            Auth::loginGuard()->fail($username, $ip);
            Users::addAudit($username, $ip, 'login', '', $reason, false);
            Json::err($reason, 401);
        }

        Auth::loginGuard()->reset($username, $ip);
        Users::markLogin((int) $user['id'], $ip);

        [$token, $_expires] = Users::createSession($user, $ip, Http::userAgent());
        Users::addAudit((string) $user['username'], $ip, 'login', '', '登录成功', true);

        // Cookie 必须先于 Json::out（后者会 exit）
        Http::setSessionCookie(Auth::COOKIE_NAME, $token, Auth::SESSION_TTL);

        Json::ok([
            'user' => [
                'id'                   => (int) $user['id'],
                'username'             => $user['username'],
                'role'                 => $user['role'],
                'role_label'           => Auth::roleLabel((string) $user['role']),
                'display_name'         => (string) ($user['display_name'] ?? ''),
                'must_change_password' => (bool) ($user['must_change_password'] ?? false),
            ],
            'permissions' => Auth::permsForRole((string) $user['role']),
        ]);
    }

    /** POST /api/auth/logout → {ok, message} */
    public static function logout(array $p): void
    {
        $token = Auth::parseSessionToken();
        $sess  = Auth::currentSession();
        if ($token !== null) {
            Users::revokeSession($token);
            if ($sess !== null) {
                Users::addAudit((string) $sess['username'], Http::clientIp(), 'logout', '', '主动登出', true);
            }
        }
        Http::clearCookie(Auth::COOKIE_NAME);
        Json::ok(['message' => '已登出']);
    }

    /** GET /api/auth/password_policy → {ok, min_length, recommend_length, hint} */
    public static function passwordPolicy(array $p): void
    {
        Json::ok([
            'min_length'       => 8,
            'recommend_length' => 12,
            'hint'             => '至少 8 位，建议 12 位以上；混合大小写、数字与符号更安全',
        ]);
    }

    /** GET /api/version → {ok, ...Version::info()} */
    public static function version(array $p): void
    {
        Json::ok(Version::info());
    }

    // ── 认证（需登录）──────────────────────────────────────────────────────

    /**
     * GET /api/auth/me
     * ★ 与 Python 版逐字段对齐（app.py 的 api_me）：
     *   无会话 → 200 {ok:true, authenticated:false}
     *   有会话 → 200 {ok:true, authenticated:true, user:{...}, permissions:[], roles:[...]}
     *   注意 user 是**嵌套对象**（不是平铺），roles 是**角色名数组**（不是 {value,label} 列表）。
     *   前端在页面加载时对所有角色都调它，所以路由权限点是 public。
     */
    public static function me(array $p): void
    {
        $sess = Auth::currentSession();
        if ($sess === null) {
            Json::ok(['authenticated' => false]);
        }
        $u = Users::getUser((int) $sess['user_id']);
        if ($u === null) {
            // 用户在别处被删除，但浏览器还拿着旧 Cookie → 当作未登录
            Http::clearCookie(Auth::COOKIE_NAME);
            Json::ok(['authenticated' => false]);
        }
        Json::ok([
            'authenticated' => true,
            'user' => [
                'id'                   => (int) $sess['user_id'],
                'username'             => (string) $sess['username'],
                'role'                 => (string) $sess['role'],
                'role_label'           => Auth::roleLabel((string) $sess['role']),
                'display_name'         => (string) ($u['display_name'] ?? ''),
                'last_login'           => $u['last_login'] ?? null,
                'must_change_password' => (bool) ($sess['must_change'] ?? false),
            ],
            'permissions' => Auth::permsForRole((string) $sess['role']),
            'roles'       => array_values(Auth::ROLES),
        ]);
    }

    /**
     * POST /api/auth/change_password
     * body: {old_password, new_password}
     * → {ok, message}；改密后**吊销其它会话**并重签当前会话 Cookie。
     *
     * 吊销其它会话是安全要求：密码泄漏场景下，改密必须让攻击者的旧会话立即失效。
     */
    public static function changePassword(array $p): void
    {
        $body    = Http::jsonBody();
        $old     = (string) ($body['old_password'] ?? '');
        $new     = (string) ($body['new_password'] ?? '');
        $sess    = Auth::currentSession();
        $ip      = Http::clientIp();

        $u = $sess === null ? null : Users::getUser((int) $sess['user_id']);
        if ($u === null) {
            Json::err('会话无效', 401);
        }
        if (!Auth::verifyPassword($old, (string) $u['password_hash'], (string) $u['salt'])) {
            Users::addAudit((string) $u['username'], $ip, 'change_password', '', '原密码错误', false);
            Json::err('原密码错误', 400);
        }
        if ($old === $new) {
            Json::err('新密码不能与原密码相同', 400);
        }
        if (mb_strlen($new) < 8) {
            Json::err('新密码至少 8 位', 400);
        }
        [$score, $label] = Auth::passwordStrength($new);

        Users::setPassword((int) $u['id'], $new, false);
        Users::revokeUserSessions((int) $u['id']);      // 其它设备的登录立即失效
        [$token] = Users::createSession(
            array_merge($u, ['must_change_password' => 0]),
            $ip,
            Http::userAgent()
        );
        Users::addAudit((string) $u['username'], $ip, 'change_password', '', '强度=' . $label, true);

        Http::setSessionCookie(Auth::COOKIE_NAME, $token, Auth::SESSION_TTL);
        Json::ok(['message' => '密码已修改，其它设备的登录已失效']);
    }

    // ── 用户管理（权限点 user = 仅 admin）──────────────────────────────────

    /** GET /api/users → {ok, users[], roles[{value,label}], label, value} */
    public static function listUsers(array $p): void
    {
        $users = Users::listUsers(true);
        // 绝不外发 password_hash / salt
        foreach ($users as &$u) {
            unset($u['password_hash'], $u['salt']);
        }
        unset($u);
        // ★ 与 Python 的 api_users 一致：只回 {ok, users, roles}，
        //   roles 是 [{value,label}] 列表。早前多回 label/value 两个顶层键，
        //   那是从 return 语句里误抓的嵌套键，Python 并没有。
        Json::ok([
            'users' => $users,
            'roles' => [
                ['value' => 'admin',    'label' => Auth::roleLabel('admin')],
                ['value' => 'operator', 'label' => Auth::roleLabel('operator')],
                ['value' => 'viewer',   'label' => Auth::roleLabel('viewer')],
            ],
        ]);
    }

    /**
     * POST /api/users
     * body: {username, password?, role, display_name?, must_change?}
     * → {ok, user_id, username, role, generated, password?}
     * 未传密码时自动生成一个并**仅在本响应里**返回一次（前端要求用户立即记录）。
     */
    public static function createUser(array $p): void
    {
        $b = Http::jsonBody();
        $admin = Auth::currentSession();
        $role  = (string) ($b['role'] ?? '');
        if (!in_array($role, Auth::ROLES, true)) {
            Json::err('非法角色：' . $role, 400);
        }
        $generated = false;
        $pw = trim((string) ($b['password'] ?? ''));
        if ($pw === '') {
            $pw = Auth::randomPassword(14);
            $generated = true;
        } elseif (mb_strlen($pw) < 8) {
            Json::err('密码至少 8 位', 400);
        }
        try {
            $uid = Users::createUser(
                (string) ($b['username'] ?? ''),
                $pw,
                $role,
                (string) ($b['display_name'] ?? ''),
                (bool) ($b['must_change'] ?? false) || $generated,
                (string) ($admin['username'] ?? 'system')
            );
        } catch (ValueError | InvalidArgumentException $e) {
            Json::err($e->getMessage(), 400);
        } catch (Throwable $e) {
            // 唯一约束等：给用户看得懂的话，但**不回显** SQL 细节
            error_log('[gcpweb] createUser: ' . $e->getMessage());
            Json::err('创建用户失败（用户名可能已存在）', 400);
        }
        Users::addAudit(
            (string) ($admin['username'] ?? ''), Http::clientIp(), 'create_user',
            (string) ($b['username'] ?? ''), 'role=' . $role, true
        );
        $out = [
            'user_id'   => $uid,
            'username'  => (string) ($b['username'] ?? ''),
            'role'      => $role,
            'generated' => $generated,
        ];
        if ($generated) {
            $out['password'] = $pw;   // 只在此处返回一次
        }
        Json::ok($out);
    }

    /**
     * PATCH /api/users/{id}   body: {role?, display_name?, disabled?, must_change?}
     * → {ok, updated}
     *
     * 防呆：不允许把最后一个可用的 admin 降级或禁用，否则系统会失去管理入口。
     */
    public static function updateUser(array $p): void
    {
        $uid = (int) $p[0];
        $b   = Http::jsonBody();
        $me  = Auth::currentSession();
        $target = Users::getUser($uid);
        if ($target === null) {
            Json::err('用户不存在', 404);
        }
        if (isset($b['role']) && !in_array((string) $b['role'], Auth::ROLES, true)) {
            Json::err('非法角色：' . $b['role'], 400);
        }

        // 「最后一个管理员」保护：降级/禁用都要先确认还有别的可用 admin
        $losingAdmin = ((isset($b['role']) && $b['role'] !== 'admin' && $target['role'] === 'admin')
                        || (!empty($b['disabled']) && $target['role'] === 'admin'));
        if ($losingAdmin) {
            $others = 0;
            foreach (Users::listUsers(false) as $u) {
                if ((int) $u['id'] !== $uid && $u['role'] === 'admin' && (int) $u['disabled'] === 0) {
                    $others++;
                }
            }
            if ($others === 0) {
                Json::err('不能降级或禁用最后一个可用的管理员', 400);
            }
        }

        $kw = [];
        foreach (['role', 'display_name', 'disabled', 'must_change'] as $k) {
            if (array_key_exists($k, $b)) {
                $kw[$k] = $b[$k];
            }
        }
        $n = Users::updateUser($uid, $kw);
        Users::addAudit(
            (string) ($me['username'] ?? ''), Http::clientIp(), 'update_user',
            (string) $target['username'], json_encode($kw, JSON_UNESCAPED_UNICODE) ?: '', true
        );
        Json::ok(['updated' => $n]);
    }

    /**
     * POST /api/users/{id}/password  body: {password?}
     * → {ok, generated, password?}
     * 重置他人密码后吊销其全部会话。
     */
    public static function resetUserPassword(array $p): void
    {
        $uid = (int) $p[0];
        $b   = Http::jsonBody();
        $me  = Auth::currentSession();
        $target = Users::getUser($uid);
        if ($target === null) {
            Json::err('用户不存在', 404);
        }
        $generated = false;
        $pw = trim((string) ($b['password'] ?? ''));
        if ($pw === '') {
            $pw = Auth::randomPassword(14);
            $generated = true;
        } elseif (mb_strlen($pw) < 8) {
            Json::err('密码至少 8 位', 400);
        }
        try {
            Users::setPassword($uid, $pw, true);       // 强制其下次登录改密
            Users::revokeUserSessions($uid);
        } catch (Throwable $e) {
            error_log('[gcpweb] resetUserPassword: ' . $e->getMessage());
            Json::err('重置密码失败', 400);
        }
        Users::addAudit(
            (string) ($me['username'] ?? ''), Http::clientIp(), 'reset_password',
            (string) $target['username'], $generated ? '随机生成' : '管理员指定', true
        );
        $out = ['generated' => $generated, 'username' => $target['username']];
        if ($generated) {
            $out['password'] = $pw;
        }
        Json::ok($out);
    }

    /** DELETE /api/users/{id} → {ok, deleted}；不允许删自己、不允许删最后一个管理员 */
    public static function deleteUser(array $p): void
    {
        $uid = (int) $p[0];
        $me  = Auth::currentSession();
        $target = Users::getUser($uid);
        if ($target === null) {
            Json::err('用户不存在', 404);
        }
        if ($uid === (int) ($me['user_id'] ?? 0)) {
            Json::err('不能删除当前登录的账号', 400);
        }
        if ($target['role'] === 'admin') {
            $others = 0;
            foreach (Users::listUsers(true) as $u) {
                if ((int) $u['id'] !== $uid && $u['role'] === 'admin') {
                    $others++;
                }
            }
            if ($others === 0) {
                Json::err('不能删除最后一个管理员', 400);
            }
        }
        $n = Users::deleteUser($uid);
        Users::addAudit(
            (string) ($me['username'] ?? ''), Http::clientIp(), 'delete_user',
            (string) $target['username'], '', true
        );
        Json::ok(['deleted' => $n]);
    }

    // ── 会话 ───────────────────────────────────────────────────────────────

    /** GET /api/sessions → {ok, sessions[]}；★ 绝不回传 token 原文 */
    public static function listSessions(array $p): void
    {
        $rows = Users::listSessions();
        $cur  = Auth::currentSession();
        $curTokenRef = '';
        $token = Auth::parseSessionToken();
        if ($token !== null) {
            $curTokenRef = substr(hash('sha256', $token), 0, 16);
        }
        $out = [];
        foreach ($rows as $r) {
            $ref = substr(hash('sha256', (string) $r['token']), 0, 16);
            $out[] = [
                'ref'        => $ref,
                'username'   => $r['username'],
                'role'       => $r['role'],
                'role_label' => Auth::roleLabel((string) $r['role']),
                'ip'         => (string) ($r['ip'] ?? ''),
                'user_agent' => (string) ($r['user_agent'] ?? ''),
                'created_at' => $r['created_at'],
                'last_seen'  => $r['last_seen'],
                'expires_at' => $r['expires_at'],
                'current'    => ($curTokenRef !== '' && $ref === $curTokenRef),
            ];
        }
        Json::ok(['sessions' => $out]);
    }

    /**
     * DELETE /api/sessions/{ref} → {ok}
     * ref 是 token 的 sha256 前 16 位（不回传明文 token，也不接受明文 token 当参数）。
     */
    public static function killSession(array $p): void
    {
        $ref = (string) $p[0];
        if (!preg_match('/^[0-9a-f]{16}$/', $ref)) {
            Json::err('会话标识格式不正确', 400);
        }
        $me = Auth::currentSession();
        $hit = null;
        foreach (Users::listSessions() as $s) {
            if (substr(hash('sha256', (string) $s['token']), 0, 16) === $ref) {
                $hit = $s;
                break;
            }
        }
        if ($hit === null) {
            Json::err('会话不存在或已失效', 404);
        }
        Users::revokeSession((string) $hit['token']);
        Users::addAudit(
            (string) ($me['username'] ?? ''), Http::clientIp(), 'kill_session',
            (string) $hit['username'], 'ip=' . $hit['ip'], true
        );
        Json::ok(['message' => '会话已终止', 'username' => $hit['username']]);
    }

    // ── 审计 ───────────────────────────────────────────────────────────────

    /**
     * GET /api/audit?page=1&page_size=5[&limit=]
     * → {ok, audit[], total, page, page_size, pages}
     * 服务端分页（Python 版 v1.2.5 才改成服务端分页，之前是把 200 条全捞回来）。
     */
    public static function audit(array $p): void
    {
        $page     = Http::queryInt('page', 1) ?? 1;
        $pageSize = Http::queryInt('page_size', 5) ?? 5;
        $limit    = Http::queryInt('limit');

        if ($page < 1) {
            $page = 1;
        }
        // 上限 200：不给「一次拉全库」留口子
        if ($pageSize < 1) {
            $pageSize = 5;
        }
        if ($pageSize > 200) {
            $pageSize = 200;
        }

        $total = Users::countAudit();

        // 兼容旧的 limit 语义：limit 视为「只取前 N 条」，保持 page=1
        $isLegacy = ($limit !== null && !isset($_GET['page']) && !isset($_GET['page_size']));
        if ($isLegacy) {
            $n = max(1, min((int) $limit, 1000));
            $rows    = Users::getAudit($n, 0);
            $pageSize = $n;
            $totalForPage = $total;
        } else {
            $pages = max(1, (int) ceil($total / $pageSize));
            if ($page > $pages) {
                $page = $pages;
            }
            $rows = Users::getAudit($pageSize, ($page - 1) * $pageSize);
        }

        $pages = max(1, (int) ceil($total / $pageSize));
        Json::ok([
            'audit'     => $rows,
            'total'     => $total,
            'page'      => $page,
            'page_size' => $pageSize,
            'pages'     => $pages,
        ]);
    }
}
