# GCP Manager Web — 使用说明

原版 `shenping1200/GCP-Manager-V3.4`（实为 v7.6 PyQt6 桌面版）的 **Web 化重构版**。

三项核心改造：

1. **自定义服务器配置** —— 网页上自由选择机型 / 镜像 / 磁盘 / 区域 / 网络 / 标签，
   不再像原版那样把 `e2-micro + Ubuntu Minimal 22.04 + pd-standard 30GB` 硬编码在源码常量里。
2. **Vue 3 响应式控制台** —— 手机 / 平板 / 电脑自适应，配色依据色彩心理学选型。
3. **登录鉴权** —— 账号 + 密码 + 图形验证码，多角色权限，后台可改密、可加用户。
4. **GCP 资源总览** —— 17 个分区只读勘察：项目 / 配额 / 区域 / 机型 / 镜像 /
   网络 / 子网 / 防火墙 / 磁盘 / 快照 / 静态 IP / API / IAM（见「六、GCP 资源总览」）。
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
| `PORT` | `8000` | 监听端口 |
| `HOST` | `0.0.0.0` | 监听地址（建议公网场景改 `127.0.0.1` 并走反代） |
| `GCPWEB_DATA_DIR` | `<项目>/data` | 数据目录（账号密钥、密码库、会话）。容器/多实例部署时用它隔离 |

### 安装后常用命令

```bash
systemctl status  gcp-manager-web      # 状态
journalctl -u gcp-manager-web -f       # 实时日志
systemctl restart gcp-manager-web      # 重启
systemctl disable gcp-manager-web      # 取消开机自启
```

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

## 四、与原版 v7.6 对照 + 需求功能落地

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

## 五、可自定义的服务器配置项

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

## 六、GCP 资源总览

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

## 七、REST API

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

## 八、目录结构

```
gcp-manager-web/
├── app.py                 FastAPI 入口 + 全部路由 + 鉴权中间件
├── run.sh                 一键启动
├── requirements.txt
├── core/
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
│   ├── console.html       Vue 3 响应式控制台
│   └── vendor/
│       └── vue.global.prod.js   Vue 3.5.13（本地托管）
├── update.sh              升级脚本（更新代码 + 重启，不动 data/）
├── install.sh             一键安装部署（venv + systemd + 自动换源重试）
├── run.sh                 手动启动脚本（优先复用 .venv）
├── Dockerfile             容器镜像（非 root 运行 + 健康检查）
├── docker-compose.yml     compose 部署（默认只绑本机 + 命名卷持久化）
├── .dockerignore          镜像构建忽略清单
├── requirements.txt       Python 依赖
├── tools/
│   └── ui_login_probe.py  登录页端到端验证夹具（仅测试，强制只绑本机）
├── tests_e2e.py           端到端验证（275 项，无需真实 GCP 账号）
└── data/                  运行时数据（db / 上传的密钥 / 初始密码文件）
```

---

## 九、验证

```bash
python3 tests_e2e.py
```

共 275 项断言。用假密钥 + mock 掉 Google 客户端，实测：

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

## 十、实测记录（真实 GCP 项目）

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

## 十一、安全说明

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
- 登录限速按 `X-Forwarded-For` 取客户端 IP，反向代理务必**覆盖**该头而不是透传用户输入。
- 「全开放防火墙」放开入站/出站 `0.0.0.0/0` 全协议，**默认关闭**，请按需谨慎开启。
- Root 密码模式会开启 Root 的 SSH 密码登录，仅在可控环境使用。
- 首次启动生成的 `data/INITIAL_ADMIN.txt` 请在改密后删除。
- 会话有效期 12 小时（滑动续期）；改密会吊销该用户全部其它会话。
