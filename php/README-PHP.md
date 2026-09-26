# GCP Manager Web · PHP 版

与 Python 版**同一套界面、同一套 API、同一个数据库结构**，纯 PHP 实现，零 composer 依赖。
为**宝塔面板**用户与「只有 PHP 环境、没有 Python/venv」的服务器准备。

> 界面与 Python 版逐字节相同：`public/static/` 下的 `console.html`、`login.html`、
> `login.css`、`vendor/*` 就是从 Python 版复制过来的，**一行未改**。
> 因为前端是纯静态 SPA + JSON API，后端换语言不影响它。
> （可用 `sha256sum` 与 `../static/` 下同名文件比对验证。）

---

## 快速开始

### 方式一：宝塔面板（推荐）

见 **[bt/GUIDE.md](bt/GUIDE.md)** —— 逐步截图级说明，含「运行目录 /public」、
伪静态规则、PHP 扩展与禁用函数、计划任务。

一键助手（宝塔环境自查）：

```bash
cd /www/wwwroot/你的域名/php
bash bt/bt-install.sh --site /www/wwwroot/你的域名/php
```

### 方式二：裸机 / VPS 一键脚本

```bash
curl -fsSL https://raw.githubusercontent.com/2016xyz/GCP-Manager-Web/main/php/install-php.sh | bash
```

装完打开 `http://<你的IP>:8080/login`。

**安装目录**（升级时要用）：

| 执行方式 | 装到哪里 |
|---|---|
| `curl … \| bash`（root） | **`/opt/gcp-manager-php`** |
| `curl … \| bash`（非 root） | `$HOME/gcp-manager-php` |
| 在克隆的仓库里 `bash php/install-php.sh` | 就地安装（仓库所在目录） |
| `APP_DIR=…` | 显式指定 |

安装结束会打印**真实安装目录**与升级命令，路径同时写入 `/etc/gcp-php-web.path`
（忘了装在哪就 `cat` 它）。

### 方式三：只跑起来看看（不需要装任何东西）

```bash
cd php
php -S 127.0.0.1:8080 -t public public/router.php
# 打开 http://127.0.0.1:8080/login
```

> PHP 内置服务器是**单进程**的，只适合验证。生产请用 nginx + php-fpm（或宝塔）。

---

## 运行要求

| 项 | 要求 |
|---|---|
| PHP | **≥ 8.0**（宝塔可选 8.0 / 8.1 / 8.2 / 8.3） |
| 必需扩展 | `pdo_sqlite` `sqlite3` `openssl` `curl` `mbstring` `json` `zlib` |
| 可选扩展 | `sockets`（WebSocket 实时日志；不装则前端自动退化为轮询） |
| 需要放行的函数 | `proc_open` `proc_get_status` `exec` `shell_exec` `escapeshellarg` `putenv` |
| 依赖 | **无**（不用 composer，不用外部库） |
| 数据库 | **SQLite**（自带的，不需要 MySQL） |

---

## 与 Python 版的关系

### 可以共用同一个数据目录

两版的**数据库表结构逐列一致**，**密码哈希算法逐位一致**
（PBKDF2-HMAC-SHA256 / 200,000 轮 / 每用户 16 字节随机盐，`hex` 存储）。
所以：

```bash
# 让 PHP 版直接用 Python 版的数据目录（账号、实例密码、用户都能互通）
GCPWEB_DATA_DIR=/opt/gcp-manager-web/data php -S 127.0.0.1:8080 -t public public/router.php
```

> ⚠ 已建的**用户会话**不通用：Cookie 名与 token 表虽然相同，但两版同时运行时
> 会各自写 `sessions` 表，能互认。真正要注意的是**不要同时**让两版都跑——
> 任务队列由各自的 worker 领取，可能重复执行。**迁移请停掉 Python 版再切 PHP 版。**
> 文件名不同（`gcp_web.db` vs `gcp_php.db`），默认互不干扰。

### 功能对照

| 能力 | Python 版 | PHP 版 |
|---|---|---|
| 登录 / 图形验证码 / 限速锁定 | ✅ | ✅ 同参数（6 次/账号、20 次/IP、锁 5 分钟） |
| 角色与权限（admin/operator/viewer） | ✅ | ✅ 同权限矩阵 |
| 用户管理 / 强制改密 / 会话管理 / 操作审计 | ✅ | ✅ |
| GCP 账号管理（含代理、导入、上传） | ✅ | ✅ |
| 实例创建 / 启停 / 删除 / 备注 / root 密码出示 | ✅ | ✅ |
| 批量执行命令 | ✅ | ✅ |
| 防火墙（绑实例所在 VPC） | ✅ | ✅ |
| 只读资源勘察 | ✅ | ✅ 全部只读 |
| 费用估算 / 省钱建议 | ✅ | ✅ |
| 初始化脚本预设 | ✅ | ✅ |
| 实时日志 | WebSocket（内置） | WebSocket（`bin/ws-server.php`，可选，缺失时轮询） |
| 后台异步任务 | 线程池 | CLI worker（`bin/task-runner.php`） |
| 运行方式 | uvicorn / systemd | nginx + php-fpm / 宝塔 |

### 实现差异（诚实说明）

| 差异 | 说明 |
|---|---|
| 并发模型 | Python 单进程多线程 → PHP-FPM 多进程。写操作靠 SQLite WAL + `busy_timeout=5000` + `BEGIN IMMEDIATE`，**不用**文件锁模拟线程锁 |
| 后台任务 | Python 用线程；PHP-FPM 没有常驻线程，改为落库 + CLI worker 领取（宝塔可用计划任务兜底） |
| 实时日志 | Python 在应用内挂 WebSocket；PHP 用独立 CLI 进程（`bin/ws-server.php`） |
| 图形验证码 | Python 用 Pillow 画图；PHP 用 GD 画图（**要求 `gd` 扩展**；若没有则退化为 SVG 输出，见 `Auth.php` 说明） |

---

## 数据目录与环境变量

```
GCPWEB_DATA_DIR         数据目录（默认 php/data）
GCPWEB_TRUSTED_PROXIES  可信代理网段（默认内网段；"-" 表示不信任何代理头）
GCPWEB_COOKIE_SECURE    1/0 强制 Cookie Secure；不设则按请求协议自动判断
GCPWEB_HOST / GCPWEB_PORT   仅 bin/ws-server.php 使用
```

> PHP-FPM 下环境变量默认**不透传**（宝塔的 pool 配置里 `clear_env` 默认为 on）。
> 需要在 宝塔 → 软件商店 → PHP → 设置 → 配置文件 里，在 `[www]` 段加：
>
> ```ini
> clear_env = no
> env[GCPWEB_DATA_DIR] = /www/wwwroot/你的域名/php/data
> env[GCPWEB_COOKIE_SECURE] = 1
> ```
>
> 不设也能跑 —— 默认数据目录就是 `php/data`，Cookie Secure 会按协议自动判断。

---

## 安全设计（与 Python 版同一套纪律）

| # | 措施 |
|---|---|
| 1 | **运行目录必须是 `/public`** —— `src/`（GCP 客户端逻辑）与 `data/`（**含实例 root 密码**）在 Web 根之外 |
| 2 | 伪静态里对 `src/ bin/ data/ bt/` 与 `.db/.key/.pem` 后缀 `deny all`（第二层防御） |
| 3 | 口令 PBKDF2-SHA256 200k 轮 + 每用户随机盐，恒定时间比较（`hash_equals`） |
| 4 | 会话 Cookie `HttpOnly` + `SameSite=Lax`，HTTPS 时自动 `Secure` |
| 5 | 登录限速按**用户名 + IP** 双维度，且**只信任可信代理来的 X-Forwarded-For**（防伪造 XFF 绕限速） |
| 6 | 强制改密在**中间件**里拦（不只是前端提示），只放行改密/登出/查自己 |
| 7 | 全部 SQL 走 PDO 预处理；表名/列名不来自用户输入 |
| 8 | 不接受请求参数当外连 URL（防 SSRF）；代理探测 URL 是硬编码常量 |
| 9 | 文件读取必须 `realpath` + 前缀白名单；公钥内容必须符合公钥格式 |
| 10 | 命令执行用数组参数（不经 shell 字符串拼接） |
| 11 | 实例列表**白名单式**丢弃密码字段，只回 `has_password` |
| 12 | 未认证接口（验证码池、限速表）有容量上限，防内存耗尽 |
| 13 | 响应头含 CSP / X-Frame-Options / X-Content-Type-Options / Referrer-Policy |
| 14 | 错误不回显堆栈与路径；`display_errors=0`，只进 error_log |
| 15 | 服务不以 root 运行（安装脚本建 `gcpweb` nologin 账号；宝塔用 `www`） |

---

## 目录结构

```
php/
├── public/                ← Web 根（宝塔「运行目录」设成 /public）
│   ├── index.php          唯一入口：路由 + 鉴权中间件
│   ├── router.php         PHP 内置服务器用
│   └── static/            前端（与 Python 版逐字节相同）
├── src/
│   ├── bootstrap.php      自动加载 + 错误处理
│   ├── Config.php         环境变量与路径
│   ├── Db.php             SQLite（WAL + busy_timeout + BEGIN IMMEDIATE）
│   ├── Http.php           请求 / 客户端 IP / 安全响应头
│   ├── Json.php           统一 JSON 响应
│   ├── Version.php        版本与更新日志（由 core/version.py 生成）
│   ├── Store.php          accounts / vm_passwords / tasks / logs / settings
│   ├── Users.php          用户 / 会话 / 审计
│   ├── Auth.php           登录 / 验证码 / 限速 / 权限点
│   ├── Gcp.php            JWT(RS256) → OAuth → Compute API
│   ├── Cost.php           费用估算
│   ├── Catalog.php        机型 / 区域 / 镜像
│   ├── Inspect.php        只读勘察
│   ├── InstallPresets.php 初始化脚本预设
│   ├── Tasks.php          任务队列
│   └── Ssh.php            ssh / ssh-keygen
├── bin/
│   ├── init.php           初始化数据库与管理员
│   ├── task-runner.php    任务 worker（CLI）
│   └── ws-server.php      实时日志 WebSocket（CLI）
├── bt/                    宝塔面板：nginx-rewrite.conf / bt-install.sh / GUIDE.md
├── data/                  SQLite + 密钥（700）
├── install-php.sh         裸机一键安装
├── update-php.sh          升级
└── INTERFACES.md          架构与冻结接口（开发文档）
```

---

## 升级

```bash
cat /etc/gcp-php-web.path                  # 先确认装在哪
cd /opt/gcp-manager-php && bash update-php.sh
```

`update-php.sh` 会拉最新代码、**全程不碰 `data/`**（升级前后做指纹比对）、
必要时重启服务。目录不对会自己找；确实没装过会明确提示「先装再用」。

常用参数与 Python 版一致：`--check` / `FORCE=1` / `NO_RESTART=1` / `APP_DIR=…`

---

## 常见问题

见 [bt/GUIDE.md 的常见问题](bt/GUIDE.md#常见问题)（白屏 / 404 / 502 / 500 /
创建实例失败 / 忘记装在哪），那里的排查对裸机部署同样适用。

---

## 开发

- 架构与冻结接口：[INTERFACES.md](INTERFACES.md)
- 版本号单一事实来源：仓库根的 `core/version.py`；
  改完执行 `python3 tools/gen_php_version.py` 重新生成 `src/Version.php`
- 契约一致性测试：`php tests/contract_test.php`（逐条比对 51 个路由的响应形态）
