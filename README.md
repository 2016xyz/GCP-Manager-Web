# GCP Manager Web — 使用说明

![版本](https://img.shields.io/badge/version-1.2.5-1a73e8)
![许可](https://img.shields.io/badge/license-MIT-10b981)
![仓库](https://img.shields.io/badge/github-2016xyz%2FGCP--Manager--Web-0f172a)

> 当前版本 **v1.1.1** · 仓库 <https://github.com/2016xyz/GCP-Manager-Web> ·
> 反馈 <https://github.com/2016xyz/GCP-Manager-Web/issues>

原版 `shenping1200/GCP-Manager-V3.4`（实为 v7.6 PyQt6 桌面版）的 **Web 化重构版**。

三项核心改造：

1. **自定义服务器配置** —— 网页上自由选择机型 / 镜像 / 磁盘 / 区域 / 网络 / 标签，
   不再像原版那样把 `e2-micro + Ubuntu Minimal 22.04 + pd-standard 30GB` 硬编码在源码常量里。
2. **Vue 3 响应式控制台** —— 手机 / 平板 / 电脑自适应，配色依据色彩心理学选型。
3. **登录鉴权** —— 账号 + 密码 + 图形验证码，多角色权限，后台可改密、可加用户。
4. **GCP 资源总览** —— 17 个分区只读勘察：项目 / 配额 / 区域 / 机型 / 镜像 /
   网络 / 子网 / 防火墙 / 磁盘 / 快照 / 静态 IP / API / IAM（见「七、GCP 资源总览」）。
   登录页视觉对齐 [2016xyz/sysuahb](https://github.com/2016xyz/sysuahb)（见「登录页设计」一节）。

对齐 v7.4+ 的**极致省钱**默认行为：

- **禁用 Ops Agent** —— 不产生日志存储与监控费用
- **数据保护 → 无备份** —— 不挂快照时间表与备份策略，不产生快照存储费
- **关闭删除保护** —— 实例可随时回收，避免忘记清理而持续计费

安全侧默认值：

- **全开放防火墙默认关闭** —— 不自动放开 `0.0.0.0/0`，需在界面上显式开启
  （点「🔥 放开全开防火墙」按钮会弹出规则明细与风险说明，二次确认后生效）

---

## 快速开始

### 方式一：一键脚本（推荐，裸机 / VPS）

```bash
curl -fsSL https://raw.githubusercontent.com/2016xyz/GCP-Manager-Web/main/install.sh | bash
```

一条命令完成：检查 Python 环境 → 建虚拟环境 → 装依赖（PyPI 不通自动切清华镜像）
→ 建数据目录 → 注册 systemd 服务并设开机自启 → 启动 → 打印访问地址与初始密码。

装完即可打开 `http://<你的IP>:8000/`。

指定端口 / 目录 / 不装服务：

```bash
# 换端口与安装目录
curl -fsSL https://raw.githubusercontent.com/2016xyz/GCP-Manager-Web/main/install.sh \
  | PORT=9000 APP_DIR=/opt/gcp-manager-web bash

# 只在当前目录装好环境，不注册 systemd（容器 / 手动启动场景）
curl -fsSL https://raw.githubusercontent.com/2016xyz/GCP-Manager-Web/main/install.sh \
  | NO_SERVICE=1 bash

# 只下载源码，不安装（想先看看或自己接管环境）
curl -fsSL https://raw.githubusercontent.com/2016xyz/GCP-Manager-Web/main/install.sh \
  | ONLY_FETCH=1 bash
```

管道模式下会把源码下载到 `./gcp-manager-web`（有 git 用 `git clone`，没有则下
`main.tar.gz`）；在已克隆的仓库里执行 `bash install.sh` 则就地安装，不会重复下载。

> 若你的 shell 已经为别的程序设了 `PORT`，会被脚本继承（安装时会明确标注
> 「来自环境变量 PORT」）。想固定端口就显式传 `PORT=8000`。

### 方式二：Docker Compose

```bash
git clone https://github.com/2016xyz/GCP-Manager-Web.git
cd GCP-Manager-Web
docker compose up -d

# 取初始管理员密码
docker compose exec gcp-manager-web cat /app/data/INITIAL_ADMIN.txt
```

默认只绑 `127.0.0.1:8000`，**不会**把控制台暴露到公网。
需要局域网访问时改 `docker-compose.yml` 里的端口映射为 `"8000:8000"`。
数据（服务账号 JSON、Root 密码库）在命名卷 `gcpweb-data` 里，换镜像不丢。

### 方式三：手动运行（源码）

```bash
git clone https://github.com/2016xyz/GCP-Manager-Web.git
cd GCP-Manager-Web
./run.sh                     # 自动装依赖并启动
# 或
pip install -r requirements.txt
python3 app.py               # 默认 0.0.0.0:8000
```

### 首次登录

首次启动会自动创建管理员并打印随机初始密码：

```
==================================================================
  [初始化] 已创建管理员账号（首次启动）
  用户名: admin
  密码:   <16 位随机强密码>
  已写入: data/INITIAL_ADMIN.txt
==================================================================
```

打开 `http://127.0.0.1:8000/` → 自动跳转登录页。登录时需填 **用户名 + 密码 + 图形验证码**，
首次登录会被强制要求修改初始密码。API 文档在 `/docs`。

> `data/INITIAL_ADMIN.txt` 权限为 600，仅在初始化时生成一次。请登录后立即改密并删除该文件。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8000` | 监听端口（非整数会直接报错退出） |
| `HOST` | `127.0.0.1` | 监听地址。**v1.2.4 起默认只绑回环**；需要对外时显式 `HOST=0.0.0.0`，启动时会打印警告 |
| `GCPWEB_DATA_DIR` | `<项目>/data` | 数据目录（账号密钥、密码库、会话）。容器/多实例部署时用它隔离 |
| `GCPWEB_TRUSTED_PROXIES` | 本机 + 私有网段 | 只有 TCP 直连来源落在这些网段内，才采信 `X-Forwarded-For` 作为限速用的客户端 IP。逗号分隔 CIDR / 单个 IP；设为 `-` 表示完全不信任任何代理头 |
| `GCPWEB_COOKIE_SECURE` | 按请求协议自动 | 会话 Cookie 的 `Secure` 标记。`1/true/yes` 强制开启，`0/false/no` 强制关闭；默认 HTTP 下关、HTTPS 下开 |
| `SERVICE_USER` | `gcpweb` | 安装脚本创建的服务账号（仅 `install.sh`）。设成 `root` 可回到旧的以 root 运行的行为，但**不推荐** |

### 安装后常用命令

```bash
systemctl status  gcp-manager-web      # 状态
journalctl -u gcp-manager-web -f       # 实时日志
systemctl restart gcp-manager-web      # 重启
systemctl disable gcp-manager-web      # 取消开机自启
```

---

## 版本号

当前版本 **v1.1.1**，采用语义化版本 `MAJOR.MINOR.PATCH`：

- `MAJOR` 不兼容改动
- `MINOR` 向后兼容的功能新增
- `PATCH` 向后兼容的问题修复

**单一事实来源**：`core/version.py`。改版本号只改这一处，
后端接口、控制台侧栏、登录页、安装/升级脚本提示全部自动跟随，
不会出现「页面写 1.0.1、接口报 2.0.0」这类不一致。

版本号出现的位置：

| 位置 | 形式 |
|---|---|
| 侧栏品牌 | `WEB CONSOLE · v1.0.1` |
| 侧栏页脚 | GitHub 链接 + `v1.0.1` 徽章 |
| 登录页卡片下方 | 仓库链接 + 版本号 |
| 个人设置 → 关于 | 版本徽章 + 仓库 + 反馈地址 + 本版更新内容 |
| `GET /api/version` | 匿名可读（登录页需要），只回版本与仓库，不含账号或环境信息 |
| `GET /api/status` | 登录后返回版本 + 完整更新日志 |
| `install.sh` / `update.sh` | 结束提示打印版本号 |

新增版本时的做法：改 `core/version.py` 的 `VERSION`，并在 `CHANGELOG`
列表头部追加一条（版本号、日期、改动点），页面「关于」卡片会自动展示最新一条。

---

## 升级到最新版

```bash
cd /opt/gcp-manager-web && bash update.sh
```

`update.sh` 会拉取最新代码、更新依赖、重启服务，**全程不碰 `data/` 目录**
（账号、密钥、数据库都在那里，升级不会丢）。

只想知道有没有新版本、不想动手：

```bash
bash update.sh --check
```

### 常用参数

| 命令 | 作用 |
|---|---|
| `bash update.sh` | 更新到最新版并重启服务 |
| `bash update.sh --check` | 只比对版本，不做任何改动 |
| `APP_DIR=/opt/gcp-manager-web bash update.sh` | 部署目录不在当前目录时指定 |
| `FORCE=1 bash update.sh` | 本地有未提交改动时也强制更新（**改动会被丢弃**） |
| `NO_RESTART=1 bash update.sh` | 只更新代码，不重启服务 |
| `PIP_TIMEOUT=600 bash update.sh` | 网络慢时放宽依赖安装超时 |

联网直接执行（不用先进目录）：

```bash
curl -fsSL https://raw.githubusercontent.com/2016xyz/GCP-Manager-Web/main/update.sh | bash
```

### 更新过程做了什么

1. 记录 `data/` 的文件指纹（只读，用于事后校验）
2. 停止前校验：本地有未提交改动时先 `git stash` 保存，**不静默丢弃**
3. 拉取最新代码
   - 是 git 仓库 → `git fetch` + `git reset --hard origin/main`（能精确回滚）
   - 不是 git 仓库（装机时无 git，走的是 tarball）→ 下载 tarball 覆盖源码
4. 比对 `data/` 指纹，确认未被改动，有变化会告警
5. 用已有虚拟环境更新 `requirements.txt` 依赖（默认源失败自动换清华源）
6. 重启 systemd 服务并检查是否 `active`

### 回滚

git 安装会打印更新前的提交号，照着回退即可：

```bash
cd /opt/gcp-manager-web
git reset --hard <更新前的提交号>
sudo systemctl restart gcp-manager-web
```

### tarball 模式的一个已知限制

非 git 部署用 `cp -a` 覆盖源码，只会**覆盖同名文件、不会删除**新版本里已移除的文件。
所以若某次升级删掉了某个源文件，它会在 tarball 部署里残留。
残留文件不影响运行（Python 只 import 用到的模块），但要彻底干净，
建议用 git 方式部署，或升级后删掉已知废弃文件。

### 为什么不能直接重跑安装命令

`install.sh` 里这一行是刻意设计的：

```bash
if [ ! -f "$APP_DIR/app.py" ]; then   # 已有代码就跳过下载
```

它的目标是**幂等**——重复执行不会覆盖既有环境、不会重置账号数据。
副作用就是：**它不会更新代码**。所以升级要用 `update.sh`。

---

## 一、登录与权限

### 登录流程

`用户名 + 密码 + 图形验证码` 三者全部正确才放行。

| 机制 | 实现 |
|---|---|
| 密码存储 | PBKDF2-HMAC-SHA256，200,000 轮，每用户独立 16 字节随机盐 |
| 密码比较 | 恒定时间比较（`hmac.compare_digest`），防时序侧信道 |
| 用户名枚举 | 用户不存在时仍执行一次哈希运算，消除响应时间差 |
| 验证码 | PIL 生成，4 位、5 分钟有效、**一次性消费**（无论对错都销毁，防重放） |
| 防爆破 | 双维度计数：单用户名 6 次失败锁 5 分钟；单 IP 20 次失败锁 5 分钟 |
| 会话 | 服务端 session token（32 字节 URL-safe），HttpOnly + SameSite=Lax，12 小时滑动续期 |
| 前端存储 | 不使用 localStorage 存 token，仅依赖 HttpOnly Cookie |

图形验证码渲染有多级兜底，确保**任何环境都能画出可读字符**：
字体路径探测 → `fc-match` 查询系统字体 → Pillow 内置字体 → SVG 文本降级。
`ink_ratio()` 自检函数可验证字符确实被绘制（防止渲染成空框），测试套件已固化该回归。

### 三种角色

| 角色 | 查看 | 运维操作（创建/启停/删除/执行命令） | 账号管理 | 保存配置 | 用户管理 |
|---|:---:|:---:|:---:|:---:|:---:|
| **管理员** admin | ✔ | ✔ | ✔ | ✔ | ✔ |
| **运维** operator | ✔ | ✔ | ✔ | ✔ | ✕ |
| **只读** viewer | ✔ | ✕ | ✕ | ✕ | ✕ |

权限在**后端**逐接口校验（`require(request, perm)`），前端隐藏入口只是体验优化，不构成安全边界。

### 用户管理（管理员专属）

- **添加用户**：指定用户名 / 角色 / 显示名；密码留空则自动生成 14 位强密码并一次性展示。
- **重置密码**：可自定义或自动生成；重置后该用户所有会话立即失效。
- **修改角色**：不能修改自己的角色（防止把自己锁在门外）。
- **禁用 / 启用**：禁用会立刻吊销该用户全部会话，且无法再登录。
- **删除用户**：不能删除自己；系统强制保留至少一个管理员。
- **在线会话**：查看所有活跃会话（用户 / 角色 / IP / 登录时间 / 最后活动），可强制下线他人。
- **操作审计**：登录、改密、建用户、重置密码、创建实例、执行命令、删除实例等全部落库（最近 200 条）。

### 修改 Web 页面密码

两种路径：

1. **用户自助** —— 侧栏「个人设置 → 修改密码」，需验证当前密码，实时密码强度条；
   修改成功后**其它设备的登录自动失效**（仅保留当前会话）。
2. **管理员重置** —— 「用户管理 → 重置密码」，适用于用户忘记密码。

密码策略：最少 8 位；强度按 长度 / 大小写混合 / 数字或符号 四维度评分。

---

## 二、登录页设计（对齐 sysuahb）

登录页的 DOM 结构、类名、CSS 与交互逐项对齐
[2016xyz/sysuahb](https://github.com/2016xyz/sysuahb) 的
`web/views/login/index.html` 与 `web/static/css/style.css`。

### 复刻范围

| 项 | 来源 | 本仓库位置 |
|---|---|---|
| 页面结构（`wow-login-*` 全套类名与层级） | 上游 `web/views/login/index.html` | `static/login.html` |
| 登录相关全部 CSS（含 5 组断点） | 上游 `web/static/css/style.css` 提取 | `static/login.css` |
| 字体图标 FontAwesome 5 Solid | 上游 `web/static/css/*` + `webfonts/` | `static/vendor/fontawesome/` |
| Bootstrap 4 基线 | 上游 `web/static/css/bootstrap.min.css` | `static/vendor/bootstrap.min.css` |
| jQuery 3.7.1 | 上游 `web/static/js/jquery-3.7.1.min.js` | `static/vendor/jquery-3.7.1.min.js` |
| `showMsg` 居中提示遮罩 | 上游 `web/static/js/language.js` | `static/login.html` 内联 |
| `togglePwd` 密码可见切换 | 上游同名函数 | `static/login.html` 内联 |

资源**全部本地托管**，无 CDN 依赖，内网可用。

### 保留的设计细节

- 背景：`#1b3468 → #2d529a → #466ab2 → #6f9bd1 → #93bde4` 多段渐变 + 两个浮动光斑
  （`wow-bg-shift` / `wow-float-a` / `wow-float-b` 三组动画）
- 卡片：400px 宽、14px 圆角、顶部 3px 渐变色条 `#3d53f5 → #6f9bd1 → #2ec6b4`
- 标题「统一协同平台」`letter-spacing: 12px`，副标题大写 + `letter-spacing: 4px`
- 输入框：左侧图标 + 栅格分隔线、`:focus-within` 变品牌蓝、16px 字号
- 按钮：`#3d53f5 → #4f6df5 → #5a7cf7` 渐变 + 字间距 2px
- 断点：`576-991.98` / `≤600` / `≤380` / `max-height:700` / `prefers-reduced-motion`
- 记住账号：沿用上游 `localStorage` 键名 `nps_login_username`
- 语言切换：按上游方式由 `li[lang]` 驱动，按钮显示语言全名

### 与上游的两处有意差异

**1. 修掉了上游登录按钮的常驻加载圈**

上游 `style.css` 里隐藏 spinner 的选择器写成了：

```css
.login-page .login-card .btn-login .btn-spinner { display: none; }
```

但登录页实际用的容器类是 `.wow-login-card`，并不匹配 `.login-card`
—— 这是它从旧版 `.login-card` 布局迁移到 `.wow-login-card` 时漏改的选择器。
结果是登录按钮右侧的 `circle-notch` 图标**常驻显示且一直在转**，
让按钮看起来永远停在"加载中"。

本仓库补上了 `.wow-login-card .btn-login .btn-spinner`，让 spinner 只在 `.loading` 时出现。
若要连这个现象一起复刻，删掉 `static/login.css` 里那两条规则即可。

**2. 验证码与提交协议沿用本产品自己的后端**

上游用 RSA + nonce + PoW 的登录协议，本产品用的是
`/api/auth/captcha` + `/api/auth/login`（PBKDF2 + 图形验证码 + 登录限速）。
视觉与交互一致，协议不外借。上游的 `pow-worker.js` 因此未移植 ——
本产品后端不校验 PoW，移植过来只会是无用的等待。

### 一处顺带修掉的既有缺陷

后端错误走 `HTTPException`，响应体是 `{"detail": "..."}`；
而登录页原来只读 `r.error`，**永远拿到 `undefined`**，
导致"验证码错误"/"用户名或密码错误"这些具体原因全部被吞成通用提示。
现已同时读 `error` 与 `detail`。

---

## 三、响应式与视觉设计

### 断点策略

| 断点 | 设备 | 布局变化 |
|---|---|---|
| ≥ 1280px | 桌面 | 完整侧栏（236px）+ 创建页左右双栏 |
| 1024–1279px | 小桌面 | 侧栏保留，双栏收为单栏 |
| 769–1023px | 平板横屏 | 侧栏收窄为 70px 图标栏（仅图标，hover 有 title 提示） |
| 481–768px | 平板竖屏 / 大手机 | 侧栏变成**顶部横向滚动导航**；**表格转为卡片流**（每个单元格带字段标签） |
| ≤ 480px | 手机 | 导航 + 单列表单 + 全宽按钮；弹层全屏化；集群间距收紧 |
| ≤ 360px | 小屏手机 | 进一步压缩导航与内边距 |

移动端专项处理：

- 输入框字号 ≥ 16px（防 iOS 聚焦自动缩放）
- 触控目标 ≥ 40px 高
- `env(safe-area-inset-bottom)` 适配 iPhone 刘海/底部横条
- 表格卡片化用 `td::before { content: attr(data-l) }` 显示字段名，无需 JS
- 弹层在手机上全屏展示，避免小屏裁切

### 配色心理学依据

写进了页面 CSS 注释，便于后续维护时不被随意改乱：

| 用色 | 取值 | 心理学理由 |
|---|---|---|
| 主色（品牌蓝） | `#1a73e8` | 冷色调降低唤醒与焦虑；蓝色普遍关联可靠、专业。用于「执行不可逆运维操作」的场景，让用户敢按确认键 |
| 侧栏（深靛） | `#0f172a` | 制造区域纵深，深色降低侧栏视觉权重，把视线留给工作区 |
| 成功绿 | `#10b981` | 完成即时正反馈（峰终定律，强化"成功时刻"的记忆） |
| 危险红 | `#e5484d` | 低明度高饱和，醒目但不刺激；仅用于破坏性操作，建立「红色=需停顿」的条件反射 |
| 背景冷灰 | `#f1f5f9` | 比纯白低约 8% 亮度负荷，缓解长时间盯屏疲劳 |
| 等宽字体 | 用于 IP/容量/时间 | 等宽字形数字宽度一致，便于纵向比对结构化数据，减少串行读错 |
| 间距 / 圆角 | 8px 栅格、10–14px 圆角 | 一致性与柔和边界降低认知摩擦 |

另外：登录页左侧品牌区在 ≤900px 自动隐藏（小屏优先保表单），
并支持 `prefers-reduced-motion` 减少动效，以及打印样式。

### 前端技术

- **Vue 3.5.13**（`static/vendor/vue.global.prod.js`，**本地托管**，内网/离线可用，不依赖 CDN）
- 单文件组件内联，无构建步骤，改完刷新即生效
- 实时日志走 **WebSocket**，按 `id` 去重（断线重连不会重复刷屏）
- 关键交互均带二次确认弹层，破坏性操作文案明确标注不可恢复

---

## 四、按钮体系与多端适配

### 按钮

统一的一套按钮系统，针对 **Windows 的渲染差异**做了专门处理：

| 处理 | 原因 |
|---|---|
| `appearance:none` | 抹掉 Windows 对 `<button>` 施加的系统立体边框与背景，否则在不同主题下会出现半像素描边、按下时的系统凹陷 |
| 边框统一 `1px`（原来是 1.5px） | Windows 在 125%/150% 缩放下会把 1.5px 舍入成 1px 或 2px，同一行按钮粗细不一致 |
| `line-height:1.2` | 微软雅黑的行高偏大，不锁定会把按钮撑高 |
| 字体栈含 `"Microsoft YaHei","Segoe UI"` | Windows 中文字体优先 |
| `@media (forced-colors:active)` | Windows 高对比度模式会抹掉渐变，必须有描边兜底 |

视觉层次：

- 主色/成功按钮用极浅垂直渐变 + 顶部内高光，按住时位移 1px 并压入内阴影
- 投影与配色绑定：主色蓝 `rgba(26,115,232,.22)`、成功绿、危险红各用各的投影色
- 危险按钮默认柔和描边（不血腥），悬停才加强，避免视觉噪音
- `:focus-visible` 焦点环 `0 0 0 3px rgba(26,115,232,.32)`，键盘可达

> **一个容易踩的坑**：焦点环与投影共用 `box-shadow` 变量组合时，
> 占位值必须写 `0 0 0 0 rgba(...,0)` 而**不能写 `none`** ——
> CSS 规范里 `none` 只能作为 `box-shadow` 的唯一值，
> 出现在逗号列表里会让整条声明非法被丢弃，结果连投影一起消失。
> 这个问题是靠读计算样式发现的（所有按钮 `box-shadow` 都是 `none`），肉眼很难察觉。

### 按钮分组与排序

原来按钮是平铺一条，破坏性操作紧贴常规操作（「重启」旁边就是「删除」，
「执行」旁边就是「清空结果」）。现在统一按 **主要 → 辅助 → 危险** 三段排列，
段间用细分隔线 `.br` 隔开，把「手滑点到隔壁」的概率降到最低：

| 页面 | 分段 |
|---|---|
| 创建实例（预设区） | ⚡ 极速部署预设 ‖ 🔥 放开全开防火墙（危险） |
| 创建实例（账号区） | ↻ 刷新账号 ‖ 全选 / 全不选 |
| 创建实例（提交区） | 🚀 开始创建 / 🔍 预检 ‖ 💾 保存为默认配置 / ↻ 载入 ‖ 清空日志 |
| 账号管理 | ⬆ 上传并导入 ‖ ＋ 按路径导入 / 📂 导入目录 |
| 账号列表 | ↻ 刷新 / 🔌 测试全部连通性 ‖ 🗑 删除勾选 |
| 实例列表 | ↻ 同步云端实例 ‖ 全选 / 全不选 ‖ ▶ 启动 / ⏹ 停止 / 🔄 重启 ‖ 🗑 删除 |
| 命令执行 | ▶ 执行 ‖ ↻ 刷新任务 ‖ 清空结果 |
| 任务日志 | ↻ 刷新 ‖ 清空后端日志 |

窄屏下分隔线自动转为横向（跟随按钮换行），不会出现半截竖线。

### 导航分组与排序

侧栏按 **运维 / 资源 / 系统** 分组，顺序按实际操作流程而不是开发顺序：

| 分组 | 顺序 | 理由 |
|---|---|---|
| 运维 | 创建实例 → 实例列表 → 命令执行 → 任务日志 | 日常主流程：建机器 → 管机器 → 批量执行 → 看任务 |
| 资源 | GCP 资源 → 账号管理 | GCP 侧的对象与凭据 |
| 系统 | 用户管理 → 个人设置 | 管理面，使用频率最低 |

「账号管理」从原来的第 2 位挪到「资源」组 —— 它是一次性配置，
不是日常动作；创建页在无账号时仍会提示「还没有账号，请到「账号管理」导入」，
不会让人找不到入口。

分组小标题在 ≤1023px（图标侧栏 / 顶部横滚导航）时自动隐藏，不挤占空间。

### 多端适配

整体断点策略见「三、响应式与视觉设计」。本轮在此之上补了两点：

- 触屏设备（`hover:none`）自动抬高最小点击高度：普通按钮 42px、小按钮 34px；
  桌面（有鼠标）保持 36px，不牺牲信息密度。
- 勘察页（GCP 资源）的专项适配：

- 工具栏控件在窄屏铺满整行（原本是写死的 `max-width`）
- 勘察表列数多（防火墙 8 列），窄屏给 `min-width` 后横向滚动，
  而不是把 CIDR / 协议端口挤成豆腐块
- 配额进度条在窄屏改成两行：指标名独占一行，进度条 + 数值一行
- 键值卡改上下排列，长邮箱/URL 不再被标签挤成竖排
- KPI 收成 3~4 个一行，小手机隐藏分区摘要

> **另一个关键坑**：单列栅格必须写 `minmax(0,1fr)` 而不是 `1fr`。
> `1fr` 等价于 `minmax(auto,1fr)`，其中 `auto` 的最小值是内容的 min-content；
> 本页有长文案和固定宽度的输入框，min-content 超过可用宽度时会把整条
> 栅格轨道顶宽 —— 实测 360px 宽手机上产生 **22px 横向溢出**（页面能左右拖动）。
> 已在所有单列覆盖里统一改为 `minmax(0,1fr)`。

### 用脚本验证，不靠肉眼

```bash
python3 tools/responsive_check.py /tmp/shots   # 5 个视口截图 + 布局指标
python3 tools/overflow_audit.py                # 4 视口 × 5 页面 的破版审计
```

`overflow_audit.py` 会逐元素检查三件事：文档级横向溢出、元素顶出视口、
内容被裁（只认 `overflow:hidden/clip`，`visible` 的 1~3px 舍入差不计）。
当前结果：**20 个「视口 × 页面」组合全部无破版**。

---

## 五、实例备注 · 费用 · 密码 · 代理

### 实例备注

创建时填一次，之后在「实例列表」的名称下方点击即可就地改；回车保存、Esc 取消。
备注只存在本机数据库（`vm_passwords.note`），**不写入 GCP 资源** —— 不污染云端元数据。

> 一个容易忽略的点：创建流程里 `save_vm()` 会被调用**多次**（创建成功一次、
> 安装命令执行后又一次）。如果第二次把字段写成 NULL，用户填的备注和 GCP 返回的
> 创建时间就被冲掉了。所以 `save_vm()` 对 `note/created_at/installs` 的语义是
> 「传 None 就保留原值」，并有一条测试专门锁住这个行为。

### 费用估算

实例列表的「费用」列给出每小时 / 每天 / 已用三个数字，外加是否落在免费额度的标记。

口径上有几个刻意的选择：

| 情形 | 处理 | 原因 |
|---|---|---|
| 停机（TERMINATED） | 只计磁盘费，不计算力费 | GCP 停机后不再收 CPU/内存费，一刀切会把数字算高 |
| 抢占式 / Spot | 算力单价 × 0.2 / × 0.35 | 折扣波动大，取常见区间 |
| 未收录的自定义机型 | 标记「无单价」，不显示 0 | 显示 0 会被误读成「免费」 |
| 没有创建时间的旧记录 | 已用费用显示 `—` | 不拿当前时间冒充，免得给出一个看起来很确定的假数字 |

**关于「免费机型」这个标记 —— 免费额度是按时间算的，不是按台数算的。**

GCP 官方原文：*"Your Free Tier e2-micro instance limit is by time, not by instance.
Each month, eligible use of all of your e2-micro instances is free until you have used
a number of hours equal to the total hours in the current month. Usage calculations are
combined across the supported regions."*

也就是说：同一账单账号下，三个免费区域里**所有** e2-micro 的运行小时数**合并计算**，
当月累计到「当月总小时数」（30 天 = 720h / 31 天 = 744h / 28 天 = 672h）为止免费。

- 一台 e2-micro 常驻 → 正好用掉全部额度，免费
- 两台 e2-micro 常驻 → 第二台有一半小时数要按量计费
- 三台各跑 1/3 个月 → 合并仍不超额，同样免费

所以界面上的「免费机型」只表示**规格落在额度内**，不代表这一定不花钱。
很多资料把它写成「每月 1 台免费」，是不准确的。

判定条件（四项同时满足）：`e2-micro` + 区域属于 `us-west1/us-central1/us-east1`
+ 非抢占式 + 磁盘是 `pd-standard` 且 ≤ 30GB。

单价来源：`MACHINE_TYPES` / `DISK_TYPES` 里的 us-central1 按需价 × `REGION_PRICE_INDEX`
区域系数。e2-micro 取 `$0.008376/h`（官方价格计算器实测值），pd-standard 取
`$0.000054795/GiB·h`（官方磁盘价格页）。**未计网络流量**，界面明确标注是参考价。

### Root 密码：默认看不见

原来 `/api/instances` 直接把所有实例的 root 密码明文返回 —— 而这个接口只需要
`view` 权限，也就是说任何一个只读账号登录后都能一次拿到全部机器的 root。

现在改成：

1. 列表接口只回 `has_password` 布尔值，密码显示为 `••••••••`
2. 点「显示」弹窗要求**重新输入自己的登录密码**
3. 校验走 `verify_login()`（含哈希比对与恒定耗时路径），并复用登录的
   `LoginGuard` 限速 —— 否则这个接口就成了拿别人的会话暴力猜密码的现成 oracle
4. 出示后 15 分钟自动收回；点「隐藏」立即收回
5. 把每次出示都写进审计日志（用户、IP、目标实例）

### 账号备注与代理

- **备注**：`accounts.label` 字段（老库里早就存在但从没被用过）。邮箱很长时列表以备注为主标识，点「加备注 / 改备注」就地编辑。
- **长邮箱**：默认省略号折叠，点「展开」换行显示全部。
- **代理**：显示哪些账号走了代理、走的是哪种协议；**代理密码在列表里已打码**，原始 `proxy` 字段不再外发。

支持的代理写法：

```
1.2.3.4:8080                        IP:端口
1.2.3.4:8080:user:pass              带认证
http://1.2.3.4:8080                 显式 HTTP
socks5h://user:pass@host:1080       域名 + SOCKS5
socks5  9.9.9.9  1080  u1  p1       空格分隔（常见面板导出格式）
[2001:db8::1]:1080                  IPv6
```

| 协议 | 说明 |
|---|---|
| HTTP / HTTPS | 走 HTTP CONNECT，经 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量 |
| SOCKS5H | SOCKS5 且 **DNS 在代理端解析**（默认走这个） |
| SOCKS4 | 仅 IPv4、无认证；带用户名密码会被拒并提示改用 socks5 |

> 为什么默认用 socks5h 而不是 socks5：socks5 会先在本地解析域名，
> 本地 DNS 被污染或解析不了 `compute.googleapis.com` 时，连代理请求都发不出去。
> 因此输入写 `socks5://` 也一律按 socks5h 处理，界面标签同步显示为
> 「代理端解析 DNS」——标签与实际行为必须一致，否则就是误导。
>
> 老实现只认 IPv4 字面量，`socks5h://proxy.example.com:1080` 这种完全合法的
> 写法会被拒；现在主机名、IPv4、IPv6 都接受。

### 代理可以在账号导入后随时改

「账号管理」列表的「代理」列提供了 **设代理 / 改代理 / 清空(改直连)** 三个动作，
不必删掉账号重新导入。编辑框里的代理地址与协议下拉就地校验，写错立刻拒绝并说明原因：

```
代理格式不正确：不认识的代理协议「socks9」；支持 http, https, socks, socks4, socks4a, socks5, socks5h
代理格式不正确：SOCKS4 不支持用户名/密码认证，请改用 socks5
代理格式不正确：代理端口不正确：(空)
```

改完会提示去点「测试」验证连通性 —— **配置改成功不等于代理通了**，这两件事要分开确认。
代理属于敏感配置，每次变更都写入审计日志（谁、什么时候、改了哪个账号）。

> **这里修掉了一个静默降级的缺陷。** 早先的写法是
> `ptype = PROXY_SCHEMES.get(scheme, ptype)`：协议名不认识时**回退到下拉框的值**，
> 于是把 `socks5` 拼成 `socks9`、或者写成 `ftp://`，都会被当成 HTTP/HTTPS 代理存下去。
> 配置界面上显示「成功」，真正的报错要等到创建实例、去调 GCP API 时才炸出来，
> 排查时离现场已经很远。现在未知协议直接拒绝并在报错里列出所有可用协议。
>
> 另一个坑：代理编辑框**不能**回填服务端返回的打码值 ——
> `/api/accounts` 只回 `socks5h://user:***@host:1080`，若把它当成初值填回输入框，
> 用户点一下保存就把 `***` 当密码存进库了。所以编辑框一律留空，要求重新输入。

### 创建页：目标账号只显示「备注 + 已有机器数」

创建实例页选账号时，真正影响判断的只有两件事：这个账号是谁、它名下已经有几台机器。
因此该表只有这两列（外加一个勾选框）：

| 列 | 内容 |
|---|---|
| 备注 | 优先显示账号备注；没填备注时回退显示邮箱（过长可展开）；密钥文件缺失时带红色徽章 |
| 已有机器 | 该账号在 GCP 里的实例数量，点「⟳ 查询机器数」实时查询 |

- 数量是**向 GCP 实时查询**得到的，不依赖本地记录，因此手工建的实例也算得进去。
- 逐账号串行查询会让页面像卡死，所以按账号并发（默认 8 并发），
  并在响应里返回 `elapsed_ms` 便于实测。
- 响应同时给出两个口径，**不合并**：`inst_count_live`（GCP 实时，查询失败为
  `null`）与 `inst_count_local`（本工具创建并记录的）。语义不同就不能混着显示，
  否则「本工具建了 0 台」会被误读成「这个账号没有机器」。
- 单个账号查询失败（凭证失效、网络不通）只记录在 `errors` 里，不影响其它账号显示。

> 同样修掉一个缺陷：这一列原来是 `{{ a.proxy }}`，而 `/api/accounts` 为防泄露
> 早已把 `proxy` 字段 `pop` 掉了 —— 所以那列**永远显示 `-`**，是个死列。
> 现在改为显示机器数，并有用例锁住「不得再引用被接口抹掉的字段」。
> 「密钥文件缺失」的警告被保留成徽章：带着缺失的密钥去创建必定失败，
> 这个提示不能因为精简列而被丢掉。

---

## 六、创建后自动安装

创建实例时勾选若干项，实例 **SSH 就绪后自动按顺序执行**安装脚本。可多选，
单项失败只记警告，不影响实例创建本身。

| 预设 | 版本 | 说明 |
|---|---|---|
| Docker CE + Compose | 装最新 | 官方 `get.docker.com` 便利脚本 |
| 3x-ui 面板 | v3.8.5 | Xray 面板。**原 v2-ui 已不可用**，见下 |
| NPS 内网穿透 | v0.34.7 | **2016xyz/sysuahb**（djylb/nps 改名重打包），非 2021 年停更的 ehang-io/nps |
| Hermes Agent | 滚动最新 | Nous Research 的 AI Agent 运行时 |
| Ekko Studio | 0.7.24 | 自托管 Web 控制台（原 Hermes Studio） |

### 关于 v2-ui：它已经装不了了

原项目 `github.com/sprov/v2-ui` 的仓库现在返回 **404**（被删除或改名），
官方一键脚本 `raw.githubusercontent.com/sprov/v2-ui/master/install.sh` 同样 **404**，
最后一次更新约在 2021 年。这是实测结果，不是推测：

```
$ curl -o /dev/null -w '%{http_code}' https://github.com/sprov/v2-ui
404
$ curl -o /dev/null -w '%{http_code}' https://raw.githubusercontent.com/sprov/v2-ui/master/install.sh
404
```

所以这里改用社区活跃替代 **3x-ui**（最新 v3.8.5），界面上的选项名保留
「3x-ui（替代已停更的 v2-ui）」以便对照。**没有**伪造一个还能用的 v2-ui 安装命令。

### nps 用的是 sysuahb，不是停更的 ehang-io/nps

原版 `ehang-io/nps` 最后一次发版是 **2021-04**（v0.26.10），已停更 4 年多。
现在改用 **[2016xyz/sysuahb](https://github.com/2016xyz/sysuahb)** ——
它基于社区持续维护的 [djylb/nps](https://github.com/djylb/nps) **v0.34.7** 重新打包
（2026-09-14 发版）。

```bash
curl -fsSL https://raw.githubusercontent.com/2016xyz/sysuahb/v0.34.7/install.sh \
  | sh -s nps v0.34.7          # 上游推荐的手动写法；本工具内部走 _run_remote
```

这个分支的特点是**每次安装生成随机进程名**（`sys` + 4 位小写字母），
服务名 / 二进制路径 / 配置目录 / 日志文件都跟着随机名走：

```
/etc/<name>/conf/sysuahb.conf      ← 固定标记文件，用来反查随机名
/usr/bin/<name>                    ← 二进制（本次实测 25 MB）
/var/log/<name>.log
```

因为名字是随机的，装完不能假设命令叫 `nps`。本工具用标记文件反查：

```bash
d=$(ls -d /etc/sys???? | head -1); n=$(basename "$d")
"$n" status
```

**容器实测结果**（Debian 12，`docker run --rm`）：

| 检查项 | 结果 |
|---|---|
| 安装是否成功 | ✅ 生成随机名 `sysalvy`，二进制 25 065 386 B |
| 版本 | ✅ 日志确认 `the version of server is 0.34.7` |
| 进程是否起来 | ✅ `/usr/bin/sysduce service` |
| 面板是否可访问 | ✅ `GET http://127.0.0.1:8081/` → **302 → /login/index** |
| 随机名是否真的随机 | ✅ 两次安装分别是 `sysalvy` / `sysduce` |
| 面板端口 | ⚠ **8081**（不是老版的 8080），账号 `admin/123` |

> 面板默认端口是 8081，默认口令 `admin/123` —— 公网部署必须立即改密，
> 并只放行必要来源。安装脚本**实测 0 处 `read` 调用**，本身就不会卡交互。

### 一个把三个预设都弄坏的坑：`curl | sh </dev/null`

上一版为了「防止上游新增未交互提示把任务挂死」，给所有远程脚本加了 `</dev/null`。
写法是错的：

```bash
# ❌ 错：sh 的 stdin 重定向会覆盖管道，脚本内容直接被丢掉
curl -fsSL https://get.docker.com | sh </dev/null
# → curl: (23) Failure writing output to destination

# ✅ 对：脚本走文件，stdin 走 /dev/null
_f=$(mktemp); curl -fsSL URL -o "$_f" && sh "$_f" args </dev/null
```

在 POSIX shell 里，`cmd1 | cmd2 < file` 的输入重定向**优先于管道**，`cmd2` 从
`file` 读而不是从管道读，于是 curl 写出去的数据没人收，报 (23) 退出。
**Docker、nps、Hermes 三个预设因此全部失效** ——
而 `bash -n` 只做语法检查，这种语义错误完全查不出来。

是**容器里真实跑一遍**才发现的：第一次实测输出就是
`curl: (23) Failure writing output to destination`，什么都没装上。

修复方式：生成脚本里带一个 `_run_remote` 助手，统一「下载到临时文件 →
`sh "$_f" "$@" </dev/null` → 删除临时文件」，并补了断言禁止流水线写法回流。

### 3x-ui 的非交互处理

3x-ui 的官方安装脚本本身是交互式的，但它内置了非交互模式：

```bash
# 脚本第 46 行：stdin 不是终端时自动进入非交互
if [[ "${XUI_NONINTERACTIVE:-0}" == "1" ]] || [[ ! -t 0 ]]; then
```

因此本工具显式设 `XUI_NONINTERACTIVE=1`，并用 `XUI_USERNAME` / `XUI_PASSWORD` /
`XUI_WEB_BASE_PATH` 固定凭据（面板路径随机化），再显式 `XUI_DB_TYPE=sqlite`
绕开 PostgreSQL 那条交互分支。装完从 `/etc/x-ui/install-result.env`
（官方脚本落盘的权威凭据文件）读回真实地址与账号密码打印到日志。

### 无人值守的两道保险

自动安装是在没人盯着的情况下跑的，任何一处 `read` 提示都会把创建任务挂死。因此：

1. 所有 `curl | bash` 与外部命令都接 `</dev/null` —— 万一上游日后新增了未守卫的
   `read`，会立刻读到 EOF 返回，而不是永久挂住。
2. 每个预设包在独立子 shell 里执行并记录退出码，单项失败不阻断后续项。

> 核实方法：把 3x-ui 安装脚本的 21 处 `read -rp` 逐条回溯，确认每一处都在
> `NONINTERACTIVE` 守卫分支内、或在非交互模式下不可达（例如 reloadcmd 分支
> 需要 `setReloadcmd=y`，而该值非交互时被强制为 `n`）。

### 自定义安装命令

预设只是省去手写。自己填在「创建后执行的安装命令」里的内容优先级更高 ——
勾选预设时，预设脚本会**追加**在你自己的命令之后，不会覆盖它。

---

## 七、可自定义的服务器配置项

**机型**（33 种，按区域自动过滤可用性）

- E2：`e2-micro`（免费机型）、`e2-small/medium`、`e2-standard-2/4/8/16/32`、`e2-highcpu-2/4/8`、`e2-highmem-2/4/8`
- N1：`n1-standard-1/2/4/8/16`
- N2：`n2-standard-2/4/8`、`n2-highmem-2`
- T2D / T2A（ARM）：`t2d-standard-1/2/4`、`t2a-standard-1/2`
- C2 / C3：`c2-standard-4`、`c3-standard-4/8`
- M1：`m1-megamem-96`
- GPU：`n1-standard-4-gpu-t4`

目录里没有的机型可直接填「自定义机型」输入框，不做白名单校验，交给 GCP 判定。

**镜像**（14 种）：Ubuntu 24.04 / 22.04 / Minimal 22.04 / 20.04、Debian 12 / 11、
Rocky 9 / 8、AlmaLinux 9、CentOS Stream 9、Container-Optimized OS、FreeBSD 14、Windows Server 2022 / 2019。

> Windows 镜像不会执行 bash `startup-script`，选择后 Root 密码模式无效（页面会明确提示）。

**磁盘**（5 种）：`pd-standard`、`pd-balanced`、`pd-ssd`、`pd-extreme`、`hyperdisk-balanced`，容量 10–65536GB。

**区域**：免费区 3 个（us-central1 / us-east1 / us-west1）+ 付费区 39 个。

**网络与启动**：网络/子网（**从项目真实 VPC 列表下拉选择**，见「八、实测记录」）、
STANDARD 或 PREMIUM 网络层级、是否分配公网 IP、
是否放开全开放防火墙、是否禁用 Ops Agent、抢占式 / Spot、自定义网络标签。

**区域与可用性**：区域列表、zone 列表、VPC 列表均来自 GCP 真实返回，
不再依赖本地硬编码猜测（各 region 的 zone 后缀并不统一）。

**登录方式**：Root 密码模式（随机或自定义，`startup-script` 自动改密并放开 Root SSH）
或 SSH 密钥模式（粘贴公钥、读服务器公钥文件、在线生成密钥对）。

---

## 八、与原版 v7.6 对照 + 需求功能落地

| 能力 | 原版 v7.6（PyQt6 桌面） | 本 Web 版 |
|---|---|---|
| 运行形态 | 只有 Windows exe / 源码跑 GUI | 浏览器访问，跨平台，可部署到服务器 |
| **多账号并行** | 支持导入多个 JSON 密钥 | ✅ 支持多账号批量导入（文件/路径/目录），账号级并发 1–10 |
| **默认全开防火墙** | 默认开启 | ⚙ 本版**默认关闭**：不自动放开 `0.0.0.0/0`，需显式开启（界面按钮 + 二次确认） |
| **智能区域管理** | 免费区/付费区二选一 | ✅ 4 种模式：免费区自动 / 付费区自动 / 自定义多区域 / 指定单区域，可设每区配额 |
| **Root 密码模式** | startup-script 自动改密 + 放开 Root SSH | ✅ 同 |
| **SSH 密钥模式** | 上传公钥注入项目 metadata | ✅ 同，另支持在线生成密钥对、读服务器公钥文件 |
| **创建后自动执行命令** | SSH 就绪后自动执行 | ✅ 同，输出实时推到网页日志（WebSocket） |
| **禁用 Ops Agent** | v7.4 默认禁用 | ✅ 默认禁用，写 `logging/monitoring/ops-agent-enabled=false` |
| **数据保护 → 无备份** | v7.4 默认无备份 | ✅ 默认无备份，`resource_policies` 置空不挂快照时间表 |
| 服务器规格 | **硬编码** `e2-micro` + `ubuntu-minimal-2204-lts` + `pd-standard 30GB` | **页面自由选择** 33 机型 / 14 镜像 / 5 磁盘类型 / 10–65536GB，还能手填任意机型名 |
| 登录鉴权 | 无（打开即用） | 账号 + 密码 + 图形验证码，三角色权限，用户管理，审计 |
| 界面 | PyQt6 固定窗口 | Vue 3 响应式，手机/平板/电脑自适应 |
| 并发 | 固定 `MAX_WORKERS=3` | 实例级并发（1–30）+ 账号级并发（1–10） |
| 成本预估 | 无 | 按机型单价 × 区域系数 × 磁盘估算月成本 |
| 默认配置作用域 | 全局 | **每用户独立**，互不覆盖 |
| 敏感信息 | JSON 密钥路径明文入库 | 接口不回传 `key_path` / 密钥内容，密钥收进 `data/keys/` |

### 省钱优化清单（界面实时展示，共 7 项）

每项都对应 `core/gcp.py::create_instance` 里的一处真实实现，不是文案：

| # | 省钱项 | 实现位置 | 默认 |
|---|---|---|---|
| 1 | 禁用 Ops / 监控 Agent | metadata `google-logging-enabled=false`、`google-monitoring-enabled=false`、`google-ops-agent-enabled=false` | ✅ |
| 2 | 数据保护 → 无备份 | 不创建快照时间表、不绑定备份策略 | ✅ |
| 3 | 无快照时间表 | 磁盘 `resource_policies=[]`，不指定 `source_snapshot` | ✅ |
| 4 | 关闭删除保护 | `instance.deletion_protection=False`，可随时回收避免僵尸实例计费 | ✅ |
| 5 | STANDARD 网络层级 | 出站 200GB/月内免费（PREMIUM 不免费） | ✅ |
| 6 | 标准盘 + 免费机型 | `e2-micro` + `pd-standard` ≤30GB 命中 GCP 永久免费额度 | ✅ |
| 7 | 抢占式 / Spot | 计算费约按需的 20% / 35% | ❌ 默认不抢占 |

出厂默认启用 6/7 项，唯一未启用的是「抢占式 / Spot」（默认不抢占，避免实例被意外回收）。
界面右侧「💸 省钱优化」卡片会实时显示每项状态与省下的费用项。

### 关于「全开放防火墙」的取舍

本版**默认关闭**：创建实例时不会改动项目的防火墙规则，不会自动放开 `0.0.0.0/0`。

- 保持关闭时：仅带 `http-server` / `https-server` 标签的实例按 GCP 默认规则开放 80 / 443，
  其余端口需自行放行。这是默认状态。
- 需要放开时：点「🔥 放开全开防火墙」按钮，会弹出规则明细与风险说明，二次确认后才开启；
  开启后将建立 `allow-all-ingress` 与 `allow-all-egress`（`0.0.0.0/0` 全协议），
  实例所有端口对所有来源开放。
- **危险开关不会被静默记住**：某次创建若勾选了全开防火墙，它**不会**写回默认配置，
  因此不会变成此后每次创建的默认行为。要让它成为默认，只能主动点「保存为默认配置」。
  `preemptible` / `spot` 同理（避免后续实例被意外抢占）。

---

## 九、GCP 资源总览

新增「🛰 GCP 资源」页，把服务账号能看到的项目信息尽量都摊开。
**全部只读**（`get` / `list` / `aggregatedList`），不创建也不修改任何资源。

### 17 个分区

| 分区 | 内容 | 权限要求 |
|---|---|---|
| 📊 计算资源汇总 | 实例数/运行/停止/vCPU 合计/按状态·区域·机型分布 + 实例明细 | compute |
| 🔑 服务账号 | 本地 JSON 的邮箱、项目、私钥是否可解析 | 无（本地读取） |
| 📁 项目信息 | 项目号、名称、状态、创建时间、label、上级组织 | Resource Manager API |
| 💳 计费状态 | 是否已关联计费账号、计费账号 ID | Cloud Billing API |
| 🧩 已启用的 API | 项目里启用的全部 API（含分页） | Service Usage API |
| 🧑‍💼 IAM 服务账号 | 项目下的服务账号列表（含停用状态） | IAM API |
| 🌐 区域与配额 | 43 个区域的可用区 + **每区域配额用量进度条** | compute |
| 📍 可用区 | 全部 zone、状态、所属区域、CPU 平台 | compute |
| ⚙️ 机器类型 | 指定可用区下的机型：vCPU / 内存 / 共享核 / 架构 / 最大磁盘数 | compute |
| 💿 公共镜像族 | ubuntu / debian / cos / rocky / windows 的最新镜像族 | compute |
| 🕸 VPC 网络 | 网络名、自动子网、MTU、路由模式、子网数 | compute |
| 🔗 子网 | 42 个子网：CIDR、网关、私有 Google 访问、用途 | compute |
| 🛡 防火墙规则 | 方向/动作/优先级/来源/协议端口/标签，**对公网开放的标红** | compute |
| 💾 磁盘 | 容量、类型、状态、挂载于谁，**未挂载的标为孤儿盘** | compute |
| 📸 快照 | 源盘、源盘容量、实际占用、创建时间 | compute |
| 🌍 静态 IP | 区域级 + 全局，是否已绑定 | compute |
| 📦 其他资源 | 路由器/VPN/转发规则/实例组/模板/健康检查/后端服务/保留/承诺 | compute |

### 设计要点

- **逐分区隔离**：每节独立 try/except。服务账号通常只有 compute 权限，
  没开通 Resource Manager / Service Usage / Cloud Billing / IAM 时，
  只有那一节显示「失败」并给出 GCP 的原始原因，不影响其余 15 节。
  失败原因会原样展示（例如 `HTTP 403 Cloud Resource Manager API has not been used in project …`）。
- **快速节 + 深节**：进入页面先拉 10 个快速节（纯 compute 只读权限，约 11 秒），
  再自动补 7 个深节（约 22 秒）。也可以只用「仅快速节」或「全量刷新」。
- **服务端 45 秒缓存**：重复刷新不打 GCP，实测二次请求 91ms。
  `fresh=1` 可强制绕过。
- **并发 + 单次代理**：各节并发执行（默认 5 线程），整套从 88 秒降到 33 秒。
  代理只在批量外层设置一次 —— `ProxyEnvContext` 改的是进程级环境变量，
  每节各设各的会在并发时互相清掉。
- **零数据也如实显示**：磁盘/快照/静态 IP 为 0 时给中性徽章，
  不做「看起来有内容」的误导。

### 端点上

```http
GET /api/inspect/sections
    → {default:[…10 个快速节], deep:[…7 个深节], all:[…全部]}

GET /api/inspect?account_id=&sections=&region=&zone=&fresh=1&quick=1
    → {ok, project_id, account_email, region, zone, cached, elapsed_ms,
       failed:[失败节名], sections:{节名:{ok, ms, data|error}}}
```

`sections` 留空取快速节 + 深节；`quick=1` 只取快速节。
`region` 传 zone（如 `us-central1-a`）会自动归一到 `us-central1`。

---

## 十、REST API

本轮新增（详见「五、实例备注 · 费用 · 密码 · 代理」）：

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/install_presets` | view | 可选安装预设清单（含版本与依据） |
| `PATCH` | `/api/instances/note` | operate | 改实例备注（上限 200 字） |
| `POST` | `/api/instances/password` | view + **重新验登录密码** | 二次验证后返回 root 密码；错误计入登录限速 |

变更：

- `GET /api/instances` **不再返回 root 密码明文**，改为 `has_password` 布尔值；
  同时新增 `note` / `installs` / `location` / `region` / `image` / `disk_type` / `disk_size_gb` / `cost` / `account_label`
- `GET /api/accounts` **不再外发原始 `proxy` 字段**（可能含明文密码），改为
  `proxy_set` / `proxy_ok` / `proxy_type` / `proxy_type_label` / `proxy_display`（打码）
- `GET /favicon.ico` 新增路由（此前在白名单里但从未注册，每个页面一个 404）


基础地址 `http://<host>:<port>`，交互式文档 `/docs`。
除公开接口外，全部需要登录（Cookie `gcp_sid`，也支持 `Authorization: Bearer <token>`）。

### 公开接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/login` | 登录页 |
| GET | `/api/auth/captcha` | 获取图形验证码（返回 `captcha_id` + 图片 data URI） |
| POST | `/api/auth/login` | 登录（需 `username` / `password` / `captcha_id` / `captcha_code`） |
| GET | `/api/auth/me` | 当前登录身份与权限集 |
| POST | `/api/auth/logout` | 登出 |

### 需登录接口

| 方法 | 路径 | 所需权限 | 说明 |
|---|---|---|---|
| POST | `/api/auth/change_password` | 登录 | 改自己的密码 |
| GET | `/api/status` | view | 版本、账号数、能力探测、用户数 |
| GET | `/api/catalog?region=` | view | 机型/镜像/磁盘/区域目录（含区域可用性过滤） |
| GET | `/api/config` | view | 读取**本人**的默认创建配置 |
| POST | `/api/config` | settings | 保存本人默认配置 |
| POST | `/api/cost/estimate` | view | 月成本估算 |
| GET | `/api/accounts` | view | 账号列表（不回传密钥路径） |
| POST | `/api/accounts` | account | 按服务器路径导入 |
| POST | `/api/accounts/upload` | account | 上传 JSON 密钥导入 |
| POST | `/api/accounts/import_dir` | account | 按目录批量导入 |
| POST | `/api/accounts/{id}/test` | view | 测试连通性并统计实例数 |
| PATCH | `/api/accounts/{id}` | account | 改账号：备注 / 代理 / 代理协议（代理就地校验，变更写审计） |
| GET | `/api/accounts/instance_counts` | view | 每个账号的实例数（GCP 实时、并发；缓存 5 分钟） |
| GET | `/api/instances?sync=true` | view | 同步拉取全部实例 |
| POST | `/api/refresh` | view | 刷新实例 |
| POST | `/api/create` | operate | 提交创建任务（支持 `dry_run` 预检） |
| POST | `/api/execute` | operate | 批量执行 SSH 命令 |
| POST | `/api/instance_action` | operate | `start`/`stop`/`reset`/`delete` |
| GET | `/api/tasks`、`/api/tasks/{id}` | view | 任务列表 / 详情 |
| POST | `/api/tasks/{id}/cancel` | operate | 取消任务 |
| GET/DELETE | `/api/logs` | view / operate | 读取 / 清空日志 |
| POST | `/api/sshkey/generate`、`/api/sshkey/read` | operate | 生成密钥对 / 读取公钥文件 |
| GET | `/api/users` | user | 用户列表 |
| POST | `/api/users` | user | 创建用户 |
| PATCH | `/api/users/{id}` | user | 改角色 / 显示名 / 启停 |
| POST | `/api/users/{id}/password` | user | 重置密码 |
| DELETE | `/api/users/{id}` | user | 删除用户 |
| GET | `/api/sessions` | user | 在线会话 |
| DELETE | `/api/sessions/{ref}` | user | 强制下线 |
| GET | `/api/audit` | user | 操作审计 |
| WS | `/ws/logs` | 登录 | 实时日志 + 任务状态推送（未登录会被 4401 关闭） |

### 创建任务示例

```bash
# 1) 登录拿 Cookie
curl -c /tmp/cj -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<你的密码>","captcha_id":"<id>","captcha_code":"<图上字符>"}'

# 2) 提交创建任务
curl -b /tmp/cj -X POST http://127.0.0.1:8000/api/create \
  -H 'Content-Type: application/json' \
  -d '{
    "account_ids": [1,2],
    "count": 2,
    "spec": {
      "machine_type": "e2-micro",
      "image_key": "ubuntu-2404-lts",
      "disk_type": "pd-standard",
      "disk_size_gb": 30,
      "region_mode": "auto_free",
      "max_per_region": 4,
      "tags": ["http-server","https-server"],
      "assign_public_ip": true,
      "auto_open_firewall": false,
      "preemptible": false
    },
    "login_mode": "root_password",
    "root_password": "",
    "post_command": "curl -fsSL https://example.com/install.sh | bash",
    "verify_command": "docker ps",
    "concurrency": 3,
    "account_workers": 2,
    "retry_count": 2
  }'
```

`region_mode` 取值：`auto_free` / `auto_paid` / `custom`（配 `regions: [...]`）/ `single`（配 `region`）。

---

## 十一、目录结构

```
gcp-manager-web/
├── app.py                 FastAPI 入口 + 全部路由 + 鉴权中间件
├── run.sh                 一键启动
├── requirements.txt
├── core/
│   ├── version.py         版本号与仓库地址（单一事实来源）
│   ├── install_presets.py 创建后自动安装的预设（含依据来源与风险说明）
│   ├── inspect.py         GCP 资源只读勘察（17 个分区）
│   ├── auth.py            密码哈希 / 会话 / 图形验证码 / 登录限速 / 权限矩阵
│   ├── users.py           用户 / 会话 / 审计 存储层
│   ├── catalog.py         机型/镜像/磁盘/区域/价格 目录（自定义配置数据源）
│   ├── gcp.py             GCP 客户端封装（create_instance 接收自定义 spec）
│   ├── ssh.py             SSH 执行引擎 + Root startup-script 模板
│   ├── store.py           SQLite 存储（账号/实例/任务/日志/设置）
│   └── tasks.py           任务引擎（创建/执行/运维/刷新）
├── static/
│   ├── login.html         登录页（含强制改密弹层）
│   ├── login.css          登录页样式（自 sysuahb 提取）
│   ├── console.html       Vue 3 响应式控制台
│   └── vendor/            全部本地托管，无 CDN（内网可用）
│       ├── vue.global.prod.js          Vue 3.5.13
│       ├── jquery-3.7.1.min.js         登录页交互
│       ├── bootstrap.min.css           登录页基线样式
│       └── fontawesome/                登录页图标字体（css + woff2/woff/ttf）
├── update.sh              升级脚本（更新代码 + 重启，不动 data/）
├── install.sh             一键安装部署（venv + systemd + 自动换源重试）
├── run.sh                 手动启动脚本（优先复用 .venv）
├── Dockerfile             容器镜像（非 root 运行 + 健康检查）
├── docker-compose.yml     compose 部署（默认只绑本机 + 命名卷持久化）
├── .dockerignore          镜像构建忽略清单
├── requirements.txt       Python 依赖
├── tools/
│   ├── ui_login_probe.py  登录页端到端验证夹具（仅测试，强制只绑本机）
│   ├── responsive_check.py 多视口截图 + 布局指标
│   ├── ui_verify.py        版本号/导航/按钮分组的实测校验
│   ├── fe_verify.py        备注/费用/密码/代理/安装预设的浏览器实测
│   ├── _fixtures.py        测试夹具共享（管理员密码、展示用假数据）
│   ├── overflow_audit.py   窄屏破版审计（逐元素查溢出/裁切）
│   ├── smoke_proxy_counts.py 接口冒烟：账号改代理（含非法值拒绝）与机器数统计
│   ├── verify_proxy_ui.py  浏览器实测：创建页账号列精简 + 账号页改代理
│   ├── audit_authz.py      路由 × 权限巡检（找漏鉴权 / 写操作权限过低）
│   ├── audit_authz_matrix.py 权限矩阵实测（低权角色逐个打写接口）
│   ├── poc_sshk_read.py    sshkey 任意文件读取的 PoC 与修复回归验证
│   ├── detect_overlap.py   逐文字块的窄屏重叠检测（含滚动容器裁剪校正）
│   ├── probe_overlap.py    重叠问题的几何量测（表格/单元格/元素坐标）
│   └── diag_overlap.py     重叠根因定点诊断（容器盒高 vs 内容高、溢出方向）
│   └── socks5_probe.py     本地 SOCKS5 服务端（验证代理链路真的通）
├── tests_e2e.py           端到端验证（605 项，无需真实 GCP 账号）
└── data/                  运行时数据（db / 上传的密钥 / 初始密码文件）
```

---

## 十二、验证

```bash
python3 tests_e2e.py
```

共 605 项断言。用假密钥 + mock 掉 Google 客户端，实测：

- **A. 认证**（22 项）：初始管理员生成、未登录 401/302、验证码正确/错误/一次性/过期、
  密码错误不泄露用户存在性、HttpOnly Cookie、强制改密、连续失败锁定
- **B. 用户管理**（13 项）：创建/重复用户名/自动生成密码/非法角色/过短密码、
  不回传哈希与盐、管理员自我保护、重置密码、审计覆盖、会话不回传完整 token
- **C. 权限矩阵**（19 项）：operator / viewer 对各敏感接口的 403 拦截、被禁用用户无法登录
- **D. 自定义配置穿透**（16 项）：机型/镜像/磁盘类型/容量/标签/抢占式/网络层级
  是否完整落到 `compute.instances.insert` 请求体
- **E. 区域/成本/任务**（20 项）：4 种区域模式、成本估算与折扣、dry-run、
  实例动作、命令执行、任务与日志
- **F. 页面与前端**（36 项）：Vue 本地托管、响应式断点、`mounted` 调用 boot、
  日志去重、验证码真实渲染（`ink_ratio` 回归）、极速预设入口、省钱优化面板

- **F2. 实例操作**（6 项）：非本工具创建的实例（预存在的、原版桌面工具建的）
  也能被 start/stop/reset/delete；本地无记录时遍历账号按名字定位真实 zone；
  找不到时给出明确原因而非静默跳过；找不到时不执行任何动作

- **H. 登录页对齐 sysuahb**（22 项）：`wow-login-*` 结构齐全、标题与副标题、
  FontAwesome 图标、`togglePwd` / `showMsg` / `nps_login_username` 与上游同名、
  语言切换按 `li[lang]` 驱动、资源全本地（无 CDN）、无残留模板变量、
  `static/login.css` 含品牌色与三组动画、上游五组断点、减少动效偏好、
  16px 字号、spinner 选择器修正、读取后端 `detail`、本地资源可达性
- **N. 备注 / 费用 / root 密码 / 代理 / 安装预设 / 远程脚本执行**（131 项）：预设齐备与顺序稳定、
  非法 key 过滤去重、全量脚本过 `bash -n`、每项带 stdin 兜底、单项失败不阻断、
  v2-ui 已死且有替代依据、费用三项计算与停机只算磁盘、未知机型不冒充 0、
  免费额度按时间计（含官方原文断言）、四类免费判定条件、代理解析 9 种合法写法 +
  4 种非法拒绝、SOCKS5 走代理端 DNS、代理密码打码、老库迁移幂等、
  备注二次保存不被冲掉、root 密码须二次验证且拒绝时不泄露、
  账号接口不外发明文代理、前端各列与交互存在性、nps 已换源 sysuahb 且不再引用 ehang-io、版本号固定、随机名反查逻辑、面板端口为实测值；禁止 `curl|sh </dev/null` 反例写法、`_run_remote` 助手存在且被全部预设使用；
  favicon 路由、`CreateRequest` 必须声明 `note`/`installs`（pydantic 会静默丢弃
  未声明字段）、dry-run 端到端确认安装脚本真的拼进 `post_command`

- **O. 账号代理可改 / 账号列精简 / 每账号机器数**（41 项）：
  未知代理协议被拒（附可选协议列表）、协议名大小写不敏感、
  `PATCH /api/accounts/{id}` 可改代理/清空为直连、非法值被拒且不破坏原配置、
  SOCKS4 带认证被拒、账号接口仍不外发明文代理密码、备注改动不被代理逻辑带坏、
  备注超长与空 PATCH 被拒、改不存在的账号 404、代理变更写审计、
  `count_instances_per_account` 并发且单账号失败隔离、live/local 两口径不合并、
  查询失败为 `null` 而非冒充 0、`/api/accounts` 也带机器数、
  创建页账号表只剩「备注/已有机器」两列、不再引用被接口抹掉的 `a.proxy`、
  保留密钥缺失警告、代理编辑框不回填打码值（否则把 `***` 当密码存进去）、
  提供「清空(改直连)」、改完提示去测连通性、机器数查询不挂在 loadAccounts 上

- **R. 后台任务终态兜底**（8 项）：
  实例动作/命令执行/刷新三类任务都有异常兜底、兜底里保证写终态、
  创建任务本有兜底不退化、用 `store.conn.commit()`；并真跑一次
  「底层抛异常」路径，断言任务变成 failed 而非停在 running、异常不逃逸

- **Q. 防火墙绑定 VPC / 自定义配置穿透**（26 项）：
  防火墙不再硬编码 `DEFAULT_NETWORK`、函数接收 network、URL 由所选 VPC 拼出、
  在实例创建成功之后才建规则、失败只警告、建前先探测既有覆盖、
  只补缺失方向、同名规则属他网则改名；网络短名解析 5 种写法；
  自定义机型/指定单区/network_url/subnet_url 五项穿透；
  任务日志含网络与指定区域；极速部署不静默打开全开放防火墙

- **P. 窄屏重叠 / sshkey 任意文件读取修复**（19 项）：
  账号表限高改用 CSS 类（内联 style 会压制媒体查询）、`.tw-acc` 定义与窄屏放开、
  `_is_readable_pubkey` 存在、只放行公钥内容、按文件名拒私钥、
  `data/` 目录整体保护、`realpath` 防穿越、8KB 上限、
  二进制不再抛 500、拒绝写审计、端到端拒读 4 类敏感文件、
  正常读 `.pub` 功能保留、私钥伪装成公钥仍被拒、穿越写法被拒

- **M. 版本号 / 导航分组 / 按钮排序**（42 项）：版本号符合语义化格式且 ≥1.0.1、
  FastAPI 元数据与 version 模块一致、`/api/version` 匿名可读且不泄露账号信息、
  `/api/status` 与之一致、更新日志首条与当前版本匹配、导航分组字段与顺序
  （创建→实例→执行→任务→资源→账号→用户→设置）、侧栏品牌/页脚/关于卡片的
  版本与仓库展示、外链 rel=noopener、按钮分隔线存在性（按标签页切片避免误匹配）、
  安装与升级脚本打印版本

- **L. 按钮与响应式不变量**（26 项）：appearance:none、边框 1px、行高锁定、
  焦点环占位不用 none、各语义按钮的投影色、触屏最小点击高度、
  prefers-reduced-motion、Windows 高对比度兜底、单列栅格一律 minmax(0,1fr)、
  断点齐备、勘察页窄屏适配（工具栏铺满/表格滚动/配额条换行/键值卡堆叠）、
  既有能力不被改回去

- **K. GCP 资源勘察**（29 项）：只读承诺（源码内无任何写操作调用）、17 个分区齐全、
  快速/深节划分、未知节名不炸、逐节异常隔离、zones 用 `available_cpu_platforms`、
  防火墙 action 由 allowed/denied 反推、孤儿盘识别、聚合响应字段名动态探测、
  并发下代理只设一次、未登录 401、缓存命中与 `fresh=1` 绕过、
  zone→region 归一、前端分区元信息/列定义经 computed 暴露（防白屏）、
  全局渲染错误兜底

- **J. 升级通道**（10 项）：`update.sh` 可执行、绝不触碰 `data/`、保留本地改动（stash）、git 与 tarball 双路径、`--check` 只读模式、重启并校验服务存活、失败换国内源、管道模式安全、`install.sh` 与 README 均有升级引导

- **I. 测试夹具隔离**（3 项）：产品 `app.py` 无任何 `__probe` 路由、
  运行中的 app 无 `__probe` 路由、夹具强制只监听 127.0.0.1

- **G. 安装与部署产物**（28 项）：`install.sh` / `run.sh` 语法检查、
  依赖失败自动换源（PyPI→清华→阿里云）、耗时兜底而非仅探测连通性、
  systemd 注册与开机自启、数据目录权限 700、Dockerfile 非 root + 健康检查、
  compose 默认只绑本机 + 命名卷持久化、**管道模式自举**（模拟 `curl | bash`
  真正没有 `BASH_SOURCE` 的场景，断言能自举出源码且 stderr 无「未绑定」）

另有 **省钱与默认值专项**（19 项）：全开防火墙默认关闭、Ops Agent 默认禁用、
无备份默认开启、删除保护默认关闭、省钱清单 7 项计数、`/api/savings` 实时计算、
危险开关（防火墙/抢占式/Spot）不被回写进默认配置，
以及**逐字段核对省钱项是否真实落到 `compute.instances.insert` 请求体**：
`google-logging-enabled=false`、`google-monitoring-enabled=false`、
`google-ops-agent-enabled=false`、`resource_policies=[]`、无 `source_snapshot`、
`deletion_protection=False`；并对照验证：防火墙关闭时**完全不触碰**项目防火墙规则
（`FW_CALLS == 0`），显式开启时才建立 `allow-all-ingress`/`allow-all-egress`
两条 `0.0.0.0/0` + `all` 协议规则。

---

## 十三、实测记录（真实 GCP 项目）

用真实服务账号 `80717428802-compute@developer.gserviceaccount.com`
（项目 `sincere-axon-354618`）端到端跑通，非 mock。

### 创建结果

| 项 | 实例 1 | 实例 2 |
|---|---|---|
| 名称 | `vm-hermes-probe-1` | `vm-1-48904-1-7286` |
| zone | `us-west1-b` | `us-west1-c` |
| 公网 IP | 35.212.163.193 | 35.212.222.149 |
| 内网 IP | 10.138.0.2 | — |
| 机型 / 盘 | e2-micro / pd-standard 30GB | 同 |
| 创建耗时 | 17.4 s | 17.8 s |
| Root 密码 SSH | ✅ `whoami → root` | ✅ `whoami → root` |
| OS | Ubuntu 22.04.5 LTS | Ubuntu 22.04.5 LTS |
| 外网出口 | — | 35.212.222.149 |

### 省钱项在真实实例上逐条核对（8/8 通过）

```
✅ google-logging-enabled    = false      ← Ops Agent 日志关闭
✅ google-monitoring-enabled = false      ← Ops Agent 监控关闭
✅ google-ops-agent-enabled  = false      ← Ops Agent 本体禁用
✅ deletion_protection       = False      ← 可随时回收
✅ disk.resource_policies    = []         ← 未挂快照时间表/备份策略
✅ disk.source_snapshot      = (空)       ← 非快照来源
✅ network_tier              = STANDARD   ← 出站 200GB/月免费
✅ startup-script 已执行     exit status 0 → /root/.gcp_root_mode_ok
```

### 实测中发现并修复的三个真实缺陷

**1. zone 不存在被误报成「权限不足」**（严重，会误导排查方向）

`zones_for_region()` 把已是 zone 的 `us-west1-b` 当作 region 再拼后缀，
生成 `us-west1-b-b` / `us-west1-b-a` 这类不存在的 zone；且后缀硬编码为
`a/b/c/d/f`，而 `us-west1` 实际只有 `a/b/c`。GCP 对不存在的 zone 返回
`Permission denied on 'locations/us-west1-b-b'`，看起来像服务账号缺权限，
实为 zone 名错误。修复：优先向 GCP 拉真实 zone（`GCPService.list_zones`），
并对 region/zone 入参做归一化。

**2. 硬编码 `network="default"`**（严重，直接导致创建失败）

程序假设项目一定有 `default` VPC。实测项目用的是自定义 VPC `jxihegwg`，
`default` 已被删除，创建报
`The referenced network resource cannot be found`。
修复：新增 `/api/project_networks`，界面下拉选择真实 VPC/子网，
并在检测到无 `default` 网络时给出醒目告警并自动切换。

**3. 子网与实例区域不匹配**

`build_instance_spec()` 未对缺失的 `region` 兜底，导致
`regions//subnetworks/default` 这类非法子网 URL，报
`Scope of the specified subnetwork doesn't match the scope of the instance`。
修复：region 缺省兜底 + zone→region 归一化。

**4. 测试脚本误删生产数据库**（严重，会伪装成「数据凭空消失」）

`tests_e2e.py` 原本直接 `os.remove(data/gcp_web.db)`。若服务正在运行，
进程仍持有已删除 inode 的文件句柄，客户端看到的是
「会话全部失效 / 账号库忽然空了」，排查方向被严重误导。
修复：测试改用独立临时库（`tempfile.mkdtemp`），
并通过 `GCPWEB_DATA_DIR` 环境变量告知 `app.py`，
另加断言「测试库路径不得等于生产库路径」。已验证测试前后生产库
inode 与时间戳完全不变。

### 新增接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/project_networks?account_id=&region=` | 项目真实 VPC 与子网 |
| GET | `/api/project_zones?account_id=&region=` | 区域内真实可用 zone |

---

## 十四、安全说明

- **服务账号 JSON 与 Root 密码是高敏感数据**：`data/` 目录不要提交到公开仓库
  （`.gitignore` 已排除）。
- **本服务自带登录鉴权，但设计前提仍是内网或本机使用**。
  若要暴露公网，请在其前面再加一层反向代理 + TLS，并考虑 IP 白名单：
  ```nginx
  location / {
      allow 10.0.0.0/8;
      deny all;
      proxy_pass http://127.0.0.1:8000;
      proxy_set_header X-Forwarded-For $remote_addr;   # 让登录限速取到真实 IP
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;          # WebSocket 支持
      proxy_set_header Connection "upgrade";
  }
  ```
- 「全开放防火墙」放开入站/出站 `0.0.0.0/0` 全协议，**默认关闭**，请按需谨慎开启。
- Root 密码模式会开启 Root 的 SSH 密码登录，仅在可控环境使用。
- 首次启动生成的 `data/INITIAL_ADMIN.txt` 请在改密后删除。
- 会话有效期 12 小时（滑动续期）；改密会吊销该用户全部其它会话。
- **登录限速取客户端 IP 的口径（v1.2.4 起有变化）**：只有当 TCP 直连来源落在
  可信代理网段时才采信 `X-Forwarded-For`，否则一律用直连 IP。
  默认可信网段为本机 + 私有网段（`127.0.0.0/8, ::1/128, 10/8, 172.16/12,
  192.168/16, fc00::/7`），可用 `GCPWEB_TRUSTED_PROXIES` 覆盖（设为 `-`
  表示完全不信任任何代理头）。放在同机 nginx 后面时，直连来源是 `127.0.0.1`，
  属可信网段，上面的 nginx 写法依然有效。
  之所以要改：原实现无条件采信该头，而该头是客户端可任意伪造的 ——
  实测「伪造 XFF + 轮换用户名」可连续爆破 60 次不被限速（不伪造时第 21 次即锁定）。
- **本服务默认只监听 `127.0.0.1`**（v1.2.4 起）。需要对外提供时必须显式
  `HOST=0.0.0.0`，启动日志会打印醒目警告。本控制台持有 GCP 服务账号凭据与
  实例 Root 密码，默认绑全网卡等于把管理台直接送上网。
- **安装脚本创建的服务以专用账号 `gcpweb` 运行**（v1.2.4 起），不再是 root；
  容器镜像同样是非 root 的 `appuser`。服务账号无登录 shell，并启用了
  `ProtectHome` / `ProtectKernelTunables` / `ProtectControlGroups` /
  `RestrictSUIDSGID`。若建号失败会回退到 root 并打印告警（此时请手动加固）。
- **首次登录强制改密是服务端强制的**（v1.2.4 起）：新建账号与被重置密码的账号，
  在改密前除改密/登出/查自己之外的接口一律 403（`code=must_change_password`）。
  原实现只在页面提示，直接调 API 可绕过。

### 安全审计（v1.2.4）：17 项修复，每项都先 PoC 实测

这一版做了一次逐函数、逐分支的审计，工具基线：`bandit -ll`、`pip-audit`、`ruff`。
所有问题都遵循同一个纪律：**先在真实运行的服务上验证可利用，再改代码，最后补回归断言**。
605 项测试中的 **S 段 34 项**专门锁定这些修复。

| 级别 | 问题 | 利用方式（实测） | 修复 |
|---|---|---|---|
| 高危 | 登录限速可被绕过 | 伪造 `X-Forwarded-For` + 轮换用户名 → 连续 60 次爆破零限速 | 仅直连来源可信时采信 XFF |
| 高危 | 强制改密形同虚设 | 未改密即可 `200` 调 `/api/status`、`/api/accounts`、`/api/create` | 中间件强制，其余 403 |
| 高危 | 服务以 root 运行 | 安装脚本无 `User=`，进程可读 `data/INITIAL_ADMIN.txt` 与私钥 | 降权到 `gcpweb` + 加固指令 |
| 中危 | SSH 静默信任主机密钥 | `AutoAddPolicy` 接受任意主机密钥，可中间人 | TOFU：首记指纹，变了就断 |
| 中危 | 用户名可含 HTML | `username=<img src=x onerror=...>` 建号成功并回显 | 服务端字符集校验 + 截断 |
| 中危 | 实例数量无上限 | `count=999999` 被受理 → 费用灾难 | `Field(ge=1, le=200)` |
| 中危 | 限速表无界增长 | 海量随机用户名可把内存打满 | 20000 条上限 + 只清已到期锁定 |
| 中危 | 验证码清理数据竞争 | 并发下 `RuntimeError: dictionary changed size` | 全程持锁 |
| 中危 | WebSocket 未受强制改密约束 | HTTP 侧被 403 拦住，`/ws/logs` 却仍能连上（PoC 实测） | 握手时单独再判，未改密关 4403 |
| 中危 | 验证码用弱随机源 | `random`（Mersenne Twister）可预测 | `secrets.choice` |
| 低危 | 默认监听 `0.0.0.0` | 管理台直接暴露公网 | 默认 `127.0.0.1` |
| 低危 | 会话令牌回传响应体 | token 出现在登录/改密/会话列表 JSON | 不再回传 + Cookie `Secure` |
| 低危 | 无安全响应头 | 可被 iframe 嵌套等 | CSP / XFO / nosniff / Referrer-Policy |
| 低危 | `limit` 无上限 | 一次请求拉全表 | `Query(le=...)` 封顶 |
| 低危 | 勘察缓存无锁 | 共享可变字典 + 并发击穿（N 倍 API 调用） | 加锁 + 防击穿 |
| 低危 | 5 处 `commit()` 无锁 | 跨线程 sqlite 提交交错 | 统一持 `store.lock` |
| 依赖 | `requirements.txt` 未锁上限 | 上游次版本破坏性变更即无法启动 | 全部改为 `>=x,<y` 区间 |

**一个值得单独说的坑**：限速表加容量上限时，最初写的清理条件是
「删掉未锁定的条目」——而刚建出来的计数记录是 `{"count": 1, "until": 0}`，
`until=0` 是 falsy，于是**正在累计的活跃计数器被全部清掉**，
等于送给攻击者一个「换个用户名就重置计数」的后门。
实测灌 30000 个键后表里只剩 9999 条、且限速记忆被削弱。
现改为只清「确实锁定过且已到期」的条目，并补了「小容量下限速仍生效」的断言。
同一个函数还有一处 off-by-one（先判断再插入，插完恰好超出 1 条 → 实测 20001），
也一并修掉。

**依赖漏洞的可达性判断**：`pip-audit` 报 Pillow 12.2.0 有 4 个 CVE
（均修于 12.3.0）。逐一核对代码后确认**当前不可达**：全仓唯一的 `Image.open()`
在 `core/auth.py` 的 `ink_ratio()`，打开的是服务端自己刚生成的验证码 PNG，
而该函数只被测试调用；其余 Pillow 用法都是 `Image.new()` + 绘图 + 硬编码字体路径。
也就是说这些 CVE 需要「打开攻击者提供的图片」才能触发，本产品没有这条路径。
仍建议升级（无成本），但不应把它报成可利用漏洞。

#### 尚无校验、已知未修的两项（供应链）

- `install.sh` / `update.sh` 从 GitHub 拉取源码后**没有做哈希校验**。
  当前依赖 HTTPS + 仓库可信，若要更严可自行指定 `git clone` 并核对 commit。
- 无 CSRF token，靠 `SameSite=lax` 缓解（跨站表单 POST 不会带上 Cookie）。
  如需更强可加同步令牌。

### 已修的安全缺陷：`/api/sshkey/read` 任意文件读取

这个接口的用途是「从服务器读一个已有的 `.pub` 填进创建表单」，
原实现却是：

```python
with open(req.pubkey_path, "r", encoding="utf-8") as f:   # 路径完全由请求方指定
    return {"ok": True, "public_key": f.read().strip()}
```

也就是把用户给的路径直接打开返回，没有任何目录限制或格式校验。
后果：拥有 `operate` 权限的角色（`operator`，**不是**管理员）可以读取
服务进程有权限的任意文件。实测读出了：

```
✗ /etc/passwd                                    200  1889 字节
✗ data/INITIAL_ADMIN.txt                         200   157 字节
     | 用户名: admin
     | 密码:   <已打码 — 见下方说明>
✗ app.py                                         200 50261 字节
```

因为 `data/INITIAL_ADMIN.txt` 里是**管理员密码明文**，
这条路等于 `operator → admin` 的提权链；对容器化部署还能读环境变量与
service account token。另外传二进制路径会抛 `UnicodeDecodeError`，
以未捕获异常返回 500。

修复后的边界（`_is_readable_pubkey`）：

| 约束 | 目的 |
|---|---|
| 内容必须以 `ssh-rsa`/`ssh-ed25519`/`ecdsa-sha2-` 等开头 | 一条就挡住 `/etc/passwd`、数据库、源码等一切非公钥内容 |
| 文件名以 `id_` 开头且非 `.pub`，或 `*.pem`/`*.key`/`*.ppk`… | 私钥绝不经此接口外发 |
| `data/` 目录整体禁止（仅放行其中的 `ssh_keys/`、`keys/` 下的 `.pub`） | 保护管理员密码、数据库、服务账号私钥 |
| `os.path.realpath` 解析后再比对 | 防符号链接与 `../` 穿越 |
| 单文件 ≤ 8KB、必须是文本 | 公钥都很短；同时避免二进制触发解码异常 |
| 拒绝时写 `read_sshkey_denied` 审计 | 有人在探测这个接口，本身值得留痕 |

为什么**没有**简单地把可读范围锁死在 `data/` 内：前端默认值就是
`/root/.ssh/id_rsa.pub`（复用服务器上已有公钥是正常用法），那样会把功能改坏。
所以采取「路径不限、内容必须像公钥」的策略。

验证脚本 `tools/poc_sshk_read.py` 覆盖 20 项：正常读 `.pub` 仍可用、
8 类敏感路径（含穿越写法）全被拒、伪装成公钥的私钥被拒、
`data/` 根部文件被拒、viewer 被 403、拒绝行为留审计。

### 已修的功能缺陷：全开放防火墙在自定义 VPC 项目里必定失败

现象（真实日志）：

```
[规格] 机型=e2-micro 镜像=CentOS Stream 9 磁盘=pd-standard 30GB 区域模式=auto_free
vm-1-8725-1-2777 重试 1/2 → us-central1-c：防火墙前置失败：
  allow-all-ingress 失败：404 POST .../global/firewalls:
  The resource 'projects/x/global/networks/default' was not found
vm-1-8725-1-2777 重试 2/2 → us-central1-f：防火墙前置失败：（同上）
小结：成功 0 台 / 失败 1 台
```

三个叠加的问题：

1. **硬编码 `global/networks/default`**。防火墙函数收不到用户选的 `network`，
   永远按 `default` 建规则；而用自定义 VPC（本例 `jxihegwg`）的项目里
   压根没有 `default`，GCP 直接 404。
2. **防火墙被当作实例创建的前置条件**。它不仅与实例无关，还是**全局资源、
   与 zone 完全无关** —— 却被放在 `create_instance` 最前面，失败即返回，
   于是外层「换个区域重试」两次全是无用功（换区不会改变全局资源的结果）。
3. **日志不打印实际用的 VPC**，只有一个 `区域模式=auto_free`，
   排查时看不出网络是哪个，只能靠猜。

修复：

- 防火墙绑定实例所在的 VPC（接受短名 / `global/networks/x` / 完整 URL）。
- 移到**实例创建成功之后**再补规则；失败只写 `warning`，不影响实例本身。
- 建规则前先探测该 VPC 是否已有覆盖 `0.0.0.0/0` 的 `all` 协议规则，
  **只补缺失的方向**。实测某项目自带 `aqq` 已覆盖全协议入站，
  旧写法会去 `UPDATE` 它，等于把用户已经收敛的规则重新铺开成 `0.0.0.0/0`。
- 同名规则若属于**别的** VPC，改用 `allow-all-<vpc>` 这样的名字，
  不把它改绑过来（改绑可能让原来依赖该规则的业务失联）。
- 日志补打 `网络=jxihegwg/jxihegwg` 与 `指定区域=asia-south2`。

真实端到端验证（`tools/live_test_create.py`，30 项断言全过）：

```
【0. 环境勘察】VPC 列表：['jxihegwg']   ← 确认没有 default
【3. 真实创建】livetest-1790310000 @ asia-south2-a
    实例创建成功 14.0s  | 公网 IP 34.0.13.157
    防火墙 已存在覆盖 0.0.0.0/0 的规则，未重复创建
【4. 云端核对】机型 e2-micro ✓  网络 jxihegwg ✓  子网 jxihegwg ✓
    磁盘 30GB pd-standard ✓  标签 ✓  Ops Agent 关闭 ✓  无资源策略 ✓
【5. 防火墙核对】入站 allow-all-ingress + aqq（绑定 jxihegwg）✓
                 出站 allow-all-egress ✓   没有跑到 default 上 ✓
【6. 清理】实例已删除 ✓  无孤儿磁盘 ✓
```

### 已修的健壮性缺陷：后台任务会静默卡在「运行中」

所有耗时任务（创建/实例动作/命令执行/刷新）都跑在**裸 daemon 线程**里。
这种线程里抛出的异常不会传播到任何地方 —— 只会打一行 traceback 然后线程消失，
任务状态永远停在 `running`。危害在于**操作其实已经生效**：

```
实测：提交删除实例后，云端实例确实被删掉了，
      但任务状态 120 秒后仍是「运行中」（message: delete 1 台实例）
      用户会以为没执行，于是重复点删除 / 手工去控制台处理
```

修复：三类任务的执行体都套上兜底，保证**任何路径都写入终态**：

```python
def run():
    completed = False
    try:
        completed = _run_action()          # 正常路径自己写 done
    except Exception as exc:
        self.log(f"[{task_id}] 任务异常终止：{exc}", task_id, "error")
        self.update_task(task_id, "failed", f"任务异常：{exc}")
    finally:
        if not completed:
            with self.api_lock:
                cur = (self.api_tasks.get(task_id) or {}).get("status")
            if cur == "running":            # 兜底：绝不留 running
                self.update_task(task_id, "failed", "任务未正常结束（详见日志）")
        self.store.conn.commit()
        self.log.flush()
```

验证方式是**真跑异常路径**（不是读代码）：把底层 GCP 客户端换成必抛异常的假实现，
断言任务变成 `failed`、且异常没有逃逸出线程 —— 见测试 R 段。

顺带把「本来就已经是目标状态」视为成功：启动一个已在运行的实例、
删除一个不存在的实例，不再报失败。

> 关于耗时的一点澄清：asia-south2 上删除实例实测要 **约 134 秒**
> （实例从 `get()` 消失于 133.7 s，`operation.done()` 于 134.1 s）——
> 两者几乎同时，说明这是 GCP 的真实速度，`.result()` 没有多余等待。
> 期间曾误判成「后台记账慢、可见性早就没了」并改过一版，
> 后用实测数据确认推断错误并回退。

### 自查工具

仓库自带三个检查脚本，改代码后建议都跑一遍：

```bash
python3 tests_e2e.py                        # 端到端断言（含鉴权与安全用例）
python3 tools/audit_authz.py                # 路由 × 权限 巡检：找漏鉴权的接口
python3 tools/audit_authz_matrix.py         # 权限矩阵实测：低权角色逐个打写接口
python3 tools/poc_sshk_read.py              # sshkey 任意文件读取的回归验证
python3 tools/detect_overlap.py             # 逐文字块的窄屏重叠检测
```

`audit_authz.py` 会列出所有路由及其要求的权限，并对「未鉴权且不在白名单」
与「写操作却只要 view 权限」两类打标。它顺带纠正过一个误报：
`/ws/logs` 走的是 Cookie 会话校验而不是 `require()`，需要单独识别。
