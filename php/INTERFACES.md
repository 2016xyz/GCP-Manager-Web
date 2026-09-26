# PHP 版架构与冻结接口（v1.0）

> 本文件是 **契约**。所有模块实现必须严格遵守，不得自行改名/改签名/改响应字段。
> 前端 `public/static/console.html` 与 `login.html` **原样复用 Python 版**，
> 因此 PHP 后端必须提供**逐字段一致**的 JSON 响应。

## 0. 为什么可以原样复用前端

前端是纯静态 SPA（Vue 3 全局构建，本地托管 `vendor/vue.global.prod.js`），
所有数据都通过 `fetch('/api/…')` 与 `WebSocket /ws/logs` 获取。
只要后端契约一致，前端一行都不用改 —— 这也保证了「PHP 版与 Python 版界面完全相同」。

---

## 1. 目录结构

```
php/
├── public/                 ← Web 根目录（宝塔里把「运行目录」设成 /public）
│   ├── index.php           ← 唯一入口：前端控制器 + 路由表 + 中间件
│   ├── .user.ini           ← 防跨站目录攻击（宝塔需要）
│   └── static/
│       ├── console.html    ← 复用
│       ├── login.html      ← 复用
│       ├── login.css       ← 复用
│       ├── favicon.ico     ← 复用
│       └── vendor/vue.global.prod.js  ← 复用
├── src/
│   ├── Config.php      ★ 环境变量 + 路径 + 常量（已冻结，勿改）
│   ├── Db.php          ★ SQLite 连接（已冻结，勿改）
│   ├── Json.php        ★ JSON 响应辅助（已冻结，勿改）
│   ├── Http.php        ★ 请求辅助 / 客户端 IP / 安全响应头（已冻结，勿改）
│   ├── Version.php     ★ 版本与更新日志（由 core/version.py 生成，勿手改）
│   ├── Store.php       ← 通用存储：accounts / vm_passwords / tasks / logs / settings
│   ├── Users.php       ← 用户 / 会话 / 审计
│   ├── Auth.php        ← 登录 / 验证码 / 限速 / 会话鉴权 / 权限点
│   ├── Gcp.php         ← 服务账号 JWT(RS256) → OAuth → Compute API；防火墙；代理
│   ├── Cost.php        ← 费用估算
│   ├── Catalog.php     ← 机型 / 区域 / 镜像目录
│   ├── Inspect.php     ← 只读资源勘察
│   ├── InstallPresets.php ← 初始化脚本预设
│   ├── Tasks.php       ← 后台任务（落库 + 交 worker 执行）
│   └── Ssh.php         ← ssh / ssh-keygen 调用
├── bin/
│   ├── task-runner.php ← CLI：执行任务队列（宝塔可用「计划任务」每分钟拉起）
│   └── ws-server.php   ← CLI：纯 PHP WebSocket 服务（/ws/logs），无 composer 依赖
├── data/               ← SQLite + keys（权限 700，Web 不可读）
├── install-php.sh      ← 通用 PHP 安装脚本（裸机 / VPS）
├── bt/                 ← 宝塔面板专用
│   ├── nginx-rewrite.conf
│   ├── bt-install.sh
│   └── GUIDE.md
└── README-PHP.md
```

---

## 2. 运行时要求

- PHP **≥ 8.0**（宝塔面板可选 8.0 / 8.1 / 8.2 / 8.3）
- 扩展：`pdo_sqlite`、`openssl`、`curl`、`mbstring`、`json`、`zlib`
- 可选：`posix`（宝塔里用于降权判断）、`sockets`（`bin/ws-server.php` 实时日志，
  不可用时前端会自动退化为轮询 —— 前端本来就有轮询分支）
- **零 composer 依赖**：JWT 用 `openssl_sign`，HTTP 用 `curl`，SSH 走系统 `ssh`/`ssh-keygen`

---

## 3. 冻结接口（基础层）

### 3.1 `Config`

```php
final class Config {
    public static function dataDir(): string;      // GCPWEB_DATA_DIR 或 <phpRoot>/data
    public static function keysDir(): string;      // dataDir()/keys
    public static function dbPath(): string;       // dataDir()/gcp_php.db
    public static function appRoot(): string;      // php/ 绝对路径
    public static function publicDir(): string;    // php/public
    public static function trustedProxies(): array;// GCPWEB_TRUSTED_PROXIES 解析后的 CIDR 数组；"-" → []
    public static function env(string $k, ?string $d = null): ?string;
    public static function envInt(string $k, int $d): int;
    public static function secureCookie(): ?bool;  // GCPWEB_COOKIE_SECURE: 1/0/null(自动)
    public static function version(): string;      // 来自 Version.php
}
```

### 3.2 `Db`（SQLite）

```php
final class Db {
    public static function conn(): PDO;   // 单例，WAL，busy_timeout=5000，ATTR_ERRMODE=EXCEPTION
    public static function tx(callable $fn); // 事务包裹（BEGIN IMMEDIATE / COMMIT / ROLLBACK）
}
```

> 并发说明：PHP-FPM 是多进程，Python 版用 `threading.RLock`。
> PHP 侧**不要**用文件锁模拟线程锁 —— 统一靠 SQLite 的 WAL + `busy_timeout`
> 与 `BEGIN IMMEDIATE` 事务；长事务必须短、必须能重试。

### 3.3 `Json`

```php
final class Json {
    public static function out(array $data, int $status = 200): never; // 发响应并结束
    public static function ok(array $extra = []): never;               // {"ok":true, ...}
    public static function err(string $detail, int $status = 400,
                               ?string $code = null): never;           // {"detail":...}
}
```

> 前端失败提示读的是 **`r.detail`**（不是 `message`），必须一致。

### 3.4 `Http`

```php
final class Http {
    public static function method(): string;          // 已归一化大写
    public static function path(): string;            // 不含 query string
    public static function query(string $k, $d = null);
    public static function jsonBody(): array;         // 解析 JSON body；空体→[]
    public static function form(): array;             // $_POST + $_FILES
    public static function clientIp(): string;        // ★ 只信任可信代理来的 XFF
    public static function isHttps(): bool;
    public static function securityHeaders(): void;   // 统一安全响应头
    public static function cookieSecure(): bool;
}
```

### 3.5 路由表（`public/index.php`）
路由与 Python 版 **逐条对应**，包含这 51 条（方法 + 路径 + 权限点）：

```
GET    /api/auth/captcha                public
GET    /api/auth/me                     user
POST   /api/auth/login                  public
POST   /api/auth/logout                 public
POST   /api/auth/change_password        user
GET    /api/auth/password_policy        public
GET    /api/users                       user
POST   /api/users                       user
PATCH  /api/users/{user_id}             user
POST   /api/users/{user_id}/password    user
DELETE /api/users/{user_id}             user
GET    /api/sessions                    user
DELETE /api/sessions/{token_ref}        user
GET    /api/audit                       user
GET    /login                           page
GET    /                                page
GET    /api/catalog                     view（★ 已核实：Python 版 app.py:727 有 require(request,"view")，
                                              早前文档误写为 public，此处更正）
GET    /api/version                     public
GET    /api/status                      view
GET    /api/config                      view
POST   /api/config                      settings
GET    /api/accounts                    view
POST   /api/accounts                    account
POST   /api/accounts/upload             account
POST   /api/accounts/import_dir         account
GET    /api/accounts/instance_counts    view
PATCH  /api/accounts/{acc_id}           account
DELETE /api/accounts/{acc_id}           account
POST   /api/accounts/{acc_id}/test      view
POST   /api/accounts/{acc_id}/test_proxy view
GET    /api/instances                   view
PATCH  /api/instances/note              operate
POST   /api/instances/password          view
POST   /api/refresh                     view
POST   /api/create                      operate
POST   /api/execute                     operate
POST   /api/instance_action             operate
GET    /api/tasks                       view
GET    /api/tasks/{task_id}             view
POST   /api/tasks/{task_id}/cancel      operate
DELETE /api/logs                        operate
GET    /api/logs                        view
GET    /api/savings                     view
POST   /api/savings                     view
GET    /api/project_networks            view
GET    /api/project_zones               view
GET    /api/inspect/sections            view
GET    /api/inspect                     view
POST   /api/cost/estimate               view
GET    /api/install_presets             view
POST   /api/sshkey/generate             operate
POST   /api/sshkey/read                 operate
GET    /ws/logs                         WS（bin/ws-server.php）
```

### 3.6 中间件顺序（必须在路由分发之前）
1. `Http::securityHeaders()`
2. 会话解析（`Auth::currentSession()`）
3. **强制改密拦截**：会话 `must_change` 为真时，除
   `change_password` / `logout` / `me` / `password_policy` 外一律
   `403 {"detail":"…","code":"must_change_password"}`
4. 权限点校验（见 `Auth::PERMISSIONS`）

---

## 4. 数据模型（与 Python 版逐列一致，**同一个 DB 文件可互操作**）

```sql
accounts(id INTEGER PK AUTOINCREMENT, email TEXT, project_id TEXT, key_path TEXT,
         proxy TEXT DEFAULT '', proxy_type TEXT DEFAULT 'HTTPS',
         label TEXT DEFAULT '', created_at REAL)

vm_passwords(name TEXT PK, ip TEXT, password TEXT, account_id TEXT, zone TEXT,
             machine_type TEXT, image_key TEXT, disk_type TEXT,
             disk_size_gb INTEGER, updated_at REAL,
             note TEXT DEFAULT '', created_at REAL, first_seen REAL,
             installs TEXT DEFAULT '')

tasks(id TEXT PK, kind TEXT, status TEXT, payload TEXT, result TEXT,
      message TEXT, created_at REAL, updated_at REAL)

logs(id INTEGER PK AUTOINCREMENT, ts REAL, task_id TEXT, level TEXT, message TEXT)

settings(k TEXT PK, v TEXT)

users(id INTEGER PK AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL, salt TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'operator', display_name TEXT DEFAULT '',
      disabled INTEGER DEFAULT 0, must_change_password INTEGER DEFAULT 0,
      last_login REAL, last_ip TEXT DEFAULT '', login_count INTEGER DEFAULT 0,
      failed_count INTEGER DEFAULT 0, created_at REAL, updated_at REAL,
      created_by TEXT DEFAULT 'system')

sessions(token TEXT PK, user_id INTEGER NOT NULL, username TEXT NOT NULL,
         role TEXT NOT NULL, ip TEXT DEFAULT '', user_agent TEXT DEFAULT '',
         created_at REAL, last_seen REAL, expires_at REAL)

audit(id INTEGER PK AUTOINCREMENT, ts REAL, username TEXT, ip TEXT, action TEXT,
      target TEXT DEFAULT '', detail TEXT DEFAULT '', ok INTEGER DEFAULT 1)
```

### ★ 密码哈希必须与 Python 版**逐位相同**（关键互操作点）
```
salt  = 32 位十六进制字符串（= 16 字节）
hash  = PBKDF2-HMAC-SHA256(password, raw_salt_bytes, iterations=200000, dklen=32) 的 hex
比较  = 恒定时间比较（hash_equals）
```
PHP 实现：
```php
$salt = $salt ?: bin2hex(random_bytes(16));
$dk   = hash_pbkdf2('sha256', $password, hex2bin($salt), 200000, 64); // 64 hex 字符 = 32 字节
return [$dk, $salt];
```
> 这行 `64` 是**必须**的（目标是 32 字节），且 `hex2bin($salt)` 不能写成 `$salt`。
> 有专门的互操作测试项 `tests/php_interop.php` 校验。

---

## 5. 常量（与 Python 版一致）

```php
const PBKDF2_ROUNDS = 200000;
const SESSION_TTL   = 43200;   // 12h
const CAPTCHA_TTL   = 300;
const CAPTCHA_LEN   = 4;
const MAX_FAIL_PER_ACCOUNT = 6;
const MAX_FAIL_PER_IP      = 20;
const LOCK_SECONDS         = 300;
const CAPTCHA_ALPHABET     = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';
const SESSION_TOUCH        = 300;
const CAPTCHA_MAX_ITEMS    = 20000;
const LOGIN_MAX_TRACKED    = 20000;
```

权限点 → 角色：
```php
PERMISSIONS = [
  'view'     => ['admin','operator','viewer'],
  'operate'  => ['admin','operator'],
  'account'  => ['admin','operator'],
  'settings' => ['admin','operator'],
  'user'     => ['admin'],
];
```

---

## 6. 编写纪律（所有模块）

1. **逐条对照 Python 实现对拍**：字段名、状态码、`detail` 文案、默认值一视同仁。
2. 所有 SQL **必须** 预处理参数（`PDO::prepare` + 绑定），禁止字符串拼接值。
3. 所有输出到 HTML 的字符串由前端负责转义；后端**不得**回显未过滤的用户输入。
4. 读路径必须 `realpath` 校验前缀；写路径必须落在 `data/` 或 `public/static`。
5. 危险操作（删除实例、清日志）必须写审计。
6. 不得引入 composer / 外部网络下载依赖。
7. 每个文件顶部写中文注释说明职责与关键取舍。
8. **不要**实现「绕过第三方商业授权」之类的功能。

---

## 7. 安全红线（审计会逐条查）

| # | 红线 |
|---|---|
| S1 | 不使用 `$_REQUEST`；只用 `$_GET/$_POST/$_FILES` + `php://input` |
| S2 | 不拼接 SQL；不把用户输入当标识符（表名/列名/ORDER BY） |
| S3 | 不 `eval` / `assert(string)` / `create_function` / `preg_replace /e` |
| S4 | 不 `include`/`require` 用户可控路径；模板路径必须白名单正则 |
| S5 | 文件操作路径必须 `realpath` + 前缀白名单，拒绝 `..`、绝对路径、符号链接逃逸 |
| S6 | 命令执行禁止拼接用户输入；必须用数组参数（`proc_open` 数组形式） |
| S7 | 反序列化只允许 `json_decode(..., true, 深度, JSON_THROW_ON_ERROR)`，禁止 `unserialize` |
| S8 | 随机数用 `random_bytes`/`random_int`，禁止 `rand`/`mt_rand` |
| S9 | 口令用 `hash_pbkdf2`（上述参数），禁止 `md5`/`sha1`/无盐 |
| S10 | Cookie 必须 `HttpOnly` + `SameSite=Lax`，HTTPS 时 `Secure` |
| S11 | 错误不得回显堆栈/路径给未认证用户 |
| S12 | 未认证接口必须有容量上限（验证码池、限速表） |
| S13 | 路径参数必须校验白名单（如 `user_id` 必须是整数） |
| S14 | 响应头必须含 `X-Content-Type-Options`/`X-Frame-Options`/`Referrer-Policy`/CSP |
| S15 | SSRF：外连 URL 必须来自硬编码常量或已校验配置，不接受请求参数 |
