# GCP Manager Web — 使用说明

原版 `shenping1200/GCP-Manager-V3.4`（实为 v7.6 PyQt6 桌面版）的 **Web 化重构版**。

三项核心改造：

1. **自定义服务器配置** —— 网页上自由选择机型 / 镜像 / 磁盘 / 区域 / 网络 / 标签，
   不再像原版那样把 `e2-micro + Ubuntu Minimal 22.04 + pd-standard 30GB` 硬编码在源码常量里。
2. **Vue 3 响应式控制台** —— 手机 / 平板 / 电脑自适应，配色依据色彩心理学选型。
3. **登录鉴权** —— 账号 + 密码 + 图形验证码，多角色权限，后台可改密、可加用户。

对齐 v7.4+ 的**极速部署 + 极致省钱**默认行为：

- **默认全开防火墙** —— 创建实例时自动建立 `allow-all-ingress` / `allow-all-egress`，部署后立即可访问
- **禁用 Ops Agent** —— 不产生日志存储与监控费用
- **数据保护 → 无备份** —— 不挂快照时间表与备份策略，不产生快照存储费
- **关闭删除保护** —— 实例可随时回收，避免忘记清理而持续计费

---

## 快速开始

```bash
cd gcp-manager-web
./run.sh                     # 自动装依赖并启动
# 或
pip install -r requirements.txt
python3 app.py               # 默认 0.0.0.0:8000
```

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

环境变量：`PORT`（默认 8000）、`HOST`（默认 0.0.0.0）。

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

## 二、响应式与视觉设计

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

## 三、与原版 v7.6 对照 + 需求功能落地

| 能力 | 原版 v7.6（PyQt6 桌面） | 本 Web 版 |
|---|---|---|
| 运行形态 | 只有 Windows exe / 源码跑 GUI | 浏览器访问，跨平台，可部署到服务器 |
| **多账号并行** | 支持导入多个 JSON 密钥 | ✅ 支持多账号批量导入（文件/路径/目录），账号级并发 1–10 |
| **默认全开防火墙** | 默认开启 | ✅ 默认开启，自动建 `allow-all-ingress`/`allow-all-egress`（入站+出站 `0.0.0.0/0`） |
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

### 关于「默认全开防火墙」的取舍

本版按需求默认开启。**必须明确知道它的代价**：

- 它为项目建立 `allow-all-ingress` 与 `allow-all-egress`（`0.0.0.0/0` 全协议），
  意味着实例的所有端口对所有来源开放，公网暴露面最大。
- 界面上会有黄色警告条 + 创建前弹层二次确认，提示这是默认行为。
- 若需要收敛，点「🛡 保守预设」一键关闭，改为仅放开 `http-server` / `https-server` 标签端口。

---

## 四、可自定义的服务器配置项

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

**网络与启动**：网络/子网、STANDARD 或 PREMIUM 网络层级、是否分配公网 IP、
是否放开全开放防火墙、是否禁用 Ops Agent、抢占式 / Spot、自定义网络标签。

**登录方式**：Root 密码模式（随机或自定义，`startup-script` 自动改密并放开 Root SSH）
或 SSH 密钥模式（粘贴公钥、读服务器公钥文件、在线生成密钥对）。

---

## 五、REST API

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

## 六、目录结构

```
gcp-manager-web/
├── app.py                 FastAPI 入口 + 全部路由 + 鉴权中间件
├── run.sh                 一键启动
├── requirements.txt
├── core/
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
├── tests_e2e.py           端到端验证（155 项，无需真实 GCP 账号）
└── data/                  运行时数据（db / 上传的密钥 / 初始密码文件）
```

---

## 七、验证

```bash
python3 tests_e2e.py
```

共 155 项断言。用假密钥 + mock 掉 Google 客户端，实测：

- **A. 认证**（22 项）：初始管理员生成、未登录 401/302、验证码正确/错误/一次性/过期、
  密码错误不泄露用户存在性、HttpOnly Cookie、强制改密、连续失败锁定
- **B. 用户管理**（13 项）：创建/重复用户名/自动生成密码/非法角色/过短密码、
  不回传哈希与盐、管理员自我保护、重置密码、审计覆盖、会话不回传完整 token
- **C. 权限矩阵**（19 项）：operator / viewer 对各敏感接口的 403 拦截、被禁用用户无法登录
- **D. 自定义配置穿透**（16 项）：机型/镜像/磁盘类型/容量/标签/抢占式/网络层级
  是否完整落到 `compute.instances.insert` 请求体
- **E. 区域/成本/任务**（20 项）：4 种区域模式、成本估算与折扣、dry-run、
  实例动作、命令执行、任务与日志
- **F. 页面与前端**（30 项）：Vue 本地托管、响应式断点、`mounted` 调用 boot、
  日志去重、验证码真实渲染（`ink_ratio` 回归）、极速预设入口、省钱优化面板

另有 **省钱与默认值专项**（12 项）：全开防火墙默认开启、Ops Agent 默认禁用、
无备份默认开启、删除保护默认关闭、省钱清单 7 项计数、`/api/savings` 实时计算，
以及**逐字段核对省钱项是否真实落到 `compute.instances.insert` 请求体**：
`google-logging-enabled=false`、`google-monitoring-enabled=false`、
`google-ops-agent-enabled=false`、`resource_policies=[]`、无 `source_snapshot`、
`deletion_protection=False`，以及 `allow-all-ingress`/`allow-all-egress`
两条防火墙规则的 `0.0.0.0/0` 与 `all` 协议。

---

## 八、安全说明

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
