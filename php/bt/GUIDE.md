# 宝塔面板部署引导（PHP 版）

> 适用于 **宝塔面板 Linux 版** 与 **aaPanel（宝塔国际版）**。
> 全流程约 5 分钟。每一步都写明「点哪里」，并说明**为什么**要这么做 ——
> 漏掉任何一步都会以「白屏 / 404 / 500 / 无法创建实例」的形式表现出来。

---

## 0. 前置条件

| 项 | 要求 | 宝塔里在哪看 |
|---|---|---|
| 面板版本 | 宝塔 Linux 7.x+ / aaPanel 6.x+ | 首页左上角 |
| PHP | **≥ 8.0**（推荐 8.1 / 8.2） | 软件商店 → 已安装 |
| Nginx | 任意 1.18+ | 软件商店 → 已安装 |
| 系统 | CentOS 7+ / Ubuntu 20+ / Debian 11+ | — |

需要的 PHP 扩展（缺一不可）：

```
pdo_sqlite  sqlite3  openssl  curl  mbstring  json  zlib
```

可选扩展：`sockets`（装了就支持 WebSocket 实时日志；不装也能用，前端会自动退化为轮询）

需要**放行**的 PHP 函数（宝塔默认会禁用，会导致后台建机与「执行命令」失败）：

```
proc_open  proc_get_status  exec  shell_exec  escapeshellarg  putenv
```

---

## 1. 建站（宝塔 → 网站 → 添加站点）

1. 宝塔 → **网站** → **添加站点**
2. 域名：填你的域名（没有域名可先用服务器 IP）
3. PHP 版本：选 **8.0 以上**
4. 数据库：**不创建**（本程序用自带的 SQLite，不需要 MySQL）
5. 提交

记下宝塔给你的根目录，形如：

```
/www/wwwroot/gcp.example.com
```

---

## 2. 上传代码

三种方式，任选其一。

### 方式 A：SSH 一键（推荐）

```bash
cd /www/wwwroot/gcp.example.com
git clone --depth 1 https://github.com/2016xyz/GCP-Manager-Web.git repo
mv repo/php ./php
rm -rf repo
```

### 方式 B：宝塔文件管理器上传

1. 宝塔 → **文件** → 进入 `/www/wwwroot/gcp.example.com`
2. 上传 `php/` 目录（或整包上传后解压，把里面的 `php` 目录留下）
3. 最终结构必须是 `/www/wwwroot/gcp.example.com/php/public/index.php` 存在

### 方式 C：用安装脚本自动下载

```bash
curl -fsSL https://raw.githubusercontent.com/2016xyz/GCP-Manager-Web/main/php/install-php.sh \
  | APP_DIR=/www/wwwroot/gcp.example.com/php ONLY_FETCH=1 bash
```

> ⚠ 上传时如果用的是 Windows 解压工具，注意 `public/static/vendor/` 里的大文件
> （`vue.global.prod.js` 等）不要漏传，否则页面会白屏（Vue 加载不到）。

---

## 3. ★ 设置运行目录为 `/public`（最关键的一步）

宝塔 → **网站** → 你的站点 → **设置** → **网站目录** → **运行目录** → 选 `/public` → 保存

**为什么必须做**：只把域名指向 `php/` 根目录，那么
`/src/*.php`（含 GCP 客户端与密钥处理逻辑）、`/data/*.db`（**含全部实例 root 密码与会话**）、
`/bt/*.sh` 全都可能被直接下载。设了运行目录后 Web 根就是 `public/`，
`src/` 与 `data/` 在 Web 之外，物理不可达。

> 即使忘了设，本项目的伪静态规则里也有 `deny all` 兜底 —— 但那是纵深防御的第二层，
> 不能替代正确设置。

---

## 4. 粘贴伪静态规则

宝塔 → **网站** → 你的站点 → **设置** → **伪静态** → 把
[`nginx-rewrite.conf`](./nginx-rewrite.conf) 的内容**整段**粘进去 → 保存。

它做四件事：
1. 拒绝 `src/ bin/ data/ bt/` 与 `.db/.key/.pem` 等敏感路径（第二层防御）
2. 把 `/ws/logs` 反代给实时日志服务（127.0.0.1:9001）
3. `/static/` 走静态缓存，不经过 PHP
4. 其余全部交给单一入口 `index.php`

> **不要**在这段规则里重复写 `location ~ \.php$` —— 宝塔的站点配置里已经有一份
> PHP-FPM 转发，重复定义会导致 502。

---

## 5. 装 PHP 扩展 + 放行函数

宝塔 → **软件商店** → **PHP 8.x** → **设置**：

- **安装扩展** 页：勾选并安装
  `pdo_sqlite`、`sqlite3`、`openssl`、`curl`、`mbstring`、`zlib`，可选 `sockets`
- **禁用函数** 页：把 `proc_open`、`proc_get_status`、`exec`、`shell_exec`、
  `escapeshellarg`、`putenv` 从列表里**删掉** → 保存

> 不删也能打开页面、看列表，但**创建实例**和**执行命令**会失败
> （它们要么拉起后台 worker，要么调用 ssh，都需要 `proc_open`）。

---

## 6. 跑安装助手

SSH 上执行（脚本会自查上面第 5 步有没有做对）：

```bash
cd /www/wwwroot/gcp.example.com/php
bash bt/bt-install.sh --site /www/wwwroot/gcp.example.com/php
```

它会：
- 识别宝塔环境、探测 PHP 路径与版本
- **逐项体检**：扩展是否齐、函数是否被禁、`open_basedir` 是否挡住 data 目录 ——
  缺什么直接告诉你「在宝塔哪个界面点哪里」
- 建 `data/` 与 `data/keys/`，权限 700，属主 `www`
- 初始化数据库、生成管理员账号，密码写到 `data/INITIAL_ADMIN.txt`（600）
- 把安装路径写入 `/etc/gcp-php-web.path`
- 打印需要你手工粘贴的伪静态规则位置

---

## 7. 注册任务队列（宝塔计划任务）

宝塔 → **计划任务** → **添加任务**：

| 字段 | 填什么 |
|---|---|
| 任务类型 | Shell 脚本 |
| 任务名称 | GCP Manager 任务队列 |
| 执行周期 | N 分钟 → **1 分钟** |
| 脚本内容 | `php /www/wwwroot/gcp.example.com/php/bin/task-runner.php --once` |

**为什么要它**：创建实例、批量执行命令这类操作是**异步任务**，由 worker 执行并把
状态写回数据库。发起操作时程序会即时拉起一次 worker，计划任务是**兜底** ——
防止进程被系统杀掉后任务永久停在「运行中」。

---

## 8.（可选）实时日志 WebSocket

前端「实时日志」用 WebSocket 推送。没起这个服务也能用，前端会自动退化为轮询。

想让它是实时的，用宝塔 **计划任务** 或 **Supervisor 管理器**常驻：

```bash
php /www/wwwroot/gcp.example.com/php/bin/ws-server.php --host 127.0.0.1 --port 9001
```

推荐用宝塔的 **Supervisor 管理器**（软件商店安装）：进程名 `gcp-ws`、
启动命令填上面那条、运行用户 `www`、自动重启打开。

---

## 9. 访问与验收

打开 `https://你的域名/login`，用 `data/INITIAL_ADMIN.txt` 里的账号密码登录。

逐项验收（对应上面每一步）：

| 检查 | 命令 / 操作 | 期望 |
|---|---|---|
| 页面能开 | 访问 `/login` | 出现登录页（有图形验证码） |
| 静态资源 | 浏览器控制台 Network | `vue.global.prod.js` 200（不是 404） |
| 未设运行目录 | 访问 `/data/gcp_php.db` | **404 或 403**（绝不能下载到文件） |
| 源码不可读 | 访问 `/src/Config.php` | 404 |
| API 正常 | 登录后看概览 | 版本号、账号列表正常显示 |
| 创建可用 | 建一台最小实例 | 任务从「运行中」走到「完成」 |
| 实时日志 | 打开「实时日志」页 | 有日志滚动（或轮询也能看到） |

### ★ 安全检查（务必做）

```bash
# 1. 数据库与密钥不可被 Web 读取
curl -s -o /dev/null -w "%{http_code}\n" https://你的域名/data/gcp_php.db
curl -s -o /dev/null -w "%{http_code}\n" https://你的域名/src/Config.php
curl -s -o /dev/null -w "%{http_code}\n" https://你的域名/bt/bt-install.sh
#   三条都必须是 404 / 403

# 2. 隐藏文件不可读
curl -s -o /dev/null -w "%{http_code}\n" https://你的域名/.git/config
```

---

## 常见问题

### 页面白屏 / Vue 未定义
`public/static/vendor/vue.global.prod.js` 没上传成功。宝塔 → 文件 → 检查该文件大小
应为约 158 KB。

### 打开是 404，除了首页都 404
伪静态没粘，或者没粘全。回到第 4 步。

### 502 Bad Gateway
伪静态里重复定义了 `location ~ \.php$`，与宝塔自带的 PHP-FPM 转发冲突。删掉重复段。

### 500 Internal Server Error
看日志：宝塔 → 网站 → 你的站点 → **日志**，或
`tail -n 50 /www/wwwlogs/你的域名.error.log`。
最常见原因：PHP 扩展缺失（第 5 步），或 `data/` 属主不是 `www`
（`chown -R www:www /www/wwwroot/你的域名/php/data`）。

### 能登录但创建实例失败
`proc_open` 被禁用（第 5 步）。或 `data/` 不可写。

### 提示「必须修改初始密码」且改完还是提示
会话 Cookie 没更新 —— 检查伪静态/反代是否把 `Set-Cookie` 吃掉了；
HTTPS 站点需要 Cookie `Secure`，本项目自动判断，反向代理场景请在
PHP 设置里加 `env[GCPWEB_COOKIE_SECURE]=1` 并把 `clear_env` 设为 `no`。

### 忘记装在哪
```bash
cat /etc/gcp-php-web.path
```
