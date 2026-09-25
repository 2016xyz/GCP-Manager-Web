"""
版本与仓库信息（单一事实来源）

改版本号只改这里一处，后端 /api/status、控制台侧栏、登录页、
安装与升级脚本提示都从这里取，避免各处写死后互相不一致。

版本规则：语义化版本 MAJOR.MINOR.PATCH
  · MAJOR 不兼容改动
  · MINOR 向后兼容的功能新增
  · PATCH 向后兼容的问题修复
"""

VERSION = "1.2.4"

REPO_URL = "https://github.com/2016xyz/GCP-Manager-Web"
REPO_NAME = "2016xyz/GCP-Manager-Web"
ISSUE_URL = REPO_URL + "/issues"
README_URL = REPO_URL + "#readme"

# 产品名（侧栏 / 登录页 / 页面标题共用）
APP_NAME = "GCP Manager Web"
APP_NAME_CN = "GCP 批量管理控制台"

# 更新日志：新版本往上追加
CHANGELOG = [
    {
        "version": "1.2.4",
        "date": "2026-09-25",
        "notes": [
            "安全审计（逐函数逐分支）后的集中修复，共 17 项。每项都先写 PoC "
            "实测确认可达，再改代码，最后补回归断言锁定；605 项测试全通过",
            "★ 高危 登录限速可被绕过：限速按客户端 IP 计数，而 IP 取自 "
            "X-Forwarded-For 请求头。该头客户端可任意伪造，实测「伪造 XFF + "
            "轮换用户名」可连续爆破 60 次不被拦（不伪造时第 21 次即锁定）。"
            "现只在直连来源属于可信代理网段时才采信该头，可用 "
            "GCPWEB_TRUSTED_PROXIES 配置（默认仅本机与私有网段，设为 - 表示"
            "完全不信任任何代理头）",
            "★ 高危 首次登录强制改密形同虚设：must_change_password 只在前端提示，"
            "服务端不拦。实测未改密仍可调用 /api/status、/api/accounts、"
            "/api/create。现由中间件强制，未改密前仅放行改密/登出/查自己，"
            "其余返回 403 且 code=must_change_password",
            "★ 高危 安装脚本以 root 运行服务：install.sh 生成的 systemd 单元没有 "
            "User=，全程也没建服务账号，而容器版本早就用非 root 的 appuser。"
            "该进程能读 data/ 下的管理员密码、会话库与服务账号私钥，一旦出现"
            "任意文件读写缺陷，影响面就是整台机器。现默认降权到专用系统账号 "
            "gcpweb（无登录 shell），并加 ProtectHome / ProtectKernelTunables / "
            "ProtectControlGroups / RestrictSUIDSGID；建号失败时回退 root 并告警",
            "★ 中危 SSH 静默信任主机密钥：paramiko 的 AutoAddPolicy 会无条件接受"
            "任何主机密钥，中间人可借此截获实例 root 凭据。改用 TOFU（首次记录"
            "指纹、之后不一致即中断连接）",
            "★ 中危 用户接口接受含 HTML 的用户名：实测 "
            "username=<img src=x onerror=...> 能建号成功并原样回显。现服务端校验"
            "（2-40 位、仅字母数字与 _ . @ -、必须 ASCII），显示名截断 80 字符并"
            "拒绝控制字符",
            "★ 中危 实例数量无上限：count 传 999999 也被受理，一次误操作即可造成"
            "费用灾难。现限制 1-200",
            "★ 中危 登录限速表无界增长：每个失败用户名/IP 都永久留一条记录，"
            "攻击者可用海量随机用户名把内存打满。现设 20000 条上限并按需清理"
            "（只清锁定已到期的，不能清正在累计的计数器 —— 否则等于送攻击者"
            "一个「换用户名即重置计数」的后门）",
            "★ 中危 验证码清理存在数据竞争：遍历字典时未持锁，并发下会抛 "
            "RuntimeError: dictionary changed size during iteration。现全程持锁",
            "中危 验证码用 random 模块生成，属可预测的 Mersenne Twister。改 "
            "secrets.choice",
            "低危 默认监听 0.0.0.0：本控制台持有 GCP 凭据与实例 root 密码，默认"
            "绑全网卡等于把管理台直接送上网。现默认 127.0.0.1，需要对外时用 "
            "HOST=0.0.0.0 并打印醒目警告",
            "低危 会话令牌回传响应体：登录/改密/会话列表会把 token 放进 JSON，"
            "前端其实只靠 HttpOnly Cookie。现不再回传，并给 Cookie 补 Secure "
            "标记（GCPWEB_COOKIE_SECURE 可覆盖）",
            "低危 无任何安全响应头。现补 CSP（object-src 'none'、"
            "frame-ancestors 'none'）、X-Frame-Options: DENY、"
            "X-Content-Type-Options、Referrer-Policy、Permissions-Policy",
            "低危 审计/任务/日志接口的 limit 无上限，一次请求可拉全表。现封顶",
            "低危 并发访问 GCP 勘察缓存无锁（共享可变字典），且同一目标被并发"
            "请求时会各自发起一次全量勘察（每次几十个 API 调用）。现加锁并防"
            "缓存击穿",
            "低危 跨线程共享的 sqlite 连接有 5 处 commit 未持锁，会与内部持锁"
            "写操作交错。现统一持锁提交",
            "依赖 给 requirements.txt 加版本上限区间（原来只写 >=，上游一个"
            "破坏性变更就会让产品起不来），并明确 python-multipart>=0.0.18"
            "（更低版本有 CVE-2024-53981 畸形 multipart 边界 DoS，本项目有"
            "文件上传接口）",
            "★ 中危 WebSocket 未受强制改密约束：/ws/logs 走独立握手路径，不经过 "
            "HTTP 中间件。实测未改密账号在 HTTP 侧被 403 拦住，却仍能连上 "
            "日志流 —— 属「策略覆盖不全」，同一账号拿到日志流会泄露实例与"
            "运维信息。现握手时单独再判一次，未改密即以 4403 关闭",
            "新增 34 项安全回归断言（S 段），含「未改密真的被拦」「重置密码后"
            "也要先改密」「未改密连不上 /ws/logs」的端到端实测；测试总数 "
            "568 → 605",
        ],
    },
    {
        "version": "1.2.3",
        "date": "2026-09-25",
        "notes": [
            "修复：后台任务没有终态兜底 —— 任务跑在裸 daemon 线程里，任何未预期"
            "异常都会静默杀死线程，任务状态永远停在「运行中」。实测删除实例时"
            "实例真的被删掉了，界面却一直显示运行中，用户会以为没执行而重复操作。"
            "现给实例动作/命令执行/刷新三类任务都补上兜底，异常时必定写入 "
            "failed 终态并记录原因",
            "改进：实例操作把「本来就已经是目标状态」视为成功（启动一个运行中的"
            "实例、删除一个不存在的实例不再报失败）",
            "说明：asia-south2 上删除实例实测需约 134 秒（实例可见性 133.7s 才消失，"
            "operation.done() 在 134.1s），这是 GCP 的真实速度，不是代码空等；"
            "期间曾误判为「后台记账慢」并改过一版，已用实测数据回退",
        ],
    },
    {
        "version": "1.2.2",
        "date": "2026-09-25",
        "notes": [
            "重要修复：勾选「全开放防火墙」在自定义 VPC 项目里必定创建失败。"
            "根因是防火墙规则硬编码 global/networks/default，而项目只有自定义 "
            "VPC（如 jxihegwg），GCP 返回 404；且防火墙是全局资源、与 zone 无关，"
            "外层「换区重试」全是白试，最终把实例也判为创建失败",
            "修复：防火墙改为绑定实例所在的 VPC；改为实例创建成功之后再补规则，"
            "失败只警告不影响实例；建规则前先探测该 VPC 是否已有覆盖 0.0.0.0/0 的"
            "规则，只补缺失的方向；同名规则若属于别的 VPC，改用带网络后缀的名字，"
            "不越权改绑",
            "任务日志补打「网络=xxx/yyy」与实际指定区域：原来日志里看不到用的哪个"
            "VPC，排查这类 404 只能靠猜",
            "新增 tools/live_test_create.py：用真实服务账号端到端实测创建→云端核对"
            "→清理（含孤儿磁盘核对），30 项断言",
        ],
    },
    {
        "version": "1.2.1",
        "date": "2026-09-25",
        "notes": [
            "安全修复：/api/sshkey/read 原可读取任意文件（operator 权限即可读出"
            "/etc/passwd 与本工具生成的管理员初始密码文件，构成提权路径）；"
            "现改为只放行公钥内容，封禁 data/ 目录与私钥文件，越权尝试写审计",
            "修复手机版「目标账号」表格压住下方「机器备注」的重叠问题"
            "（内联 max-height 压制了窄屏媒体查询）",
            "新增 tools/audit_authz.py 与 audit_authz_matrix.py：路由鉴权与越权巡检",
            "新增 tools/detect_overlap.py：逐文字块的重叠检测（含滚动容器裁剪校正）",
        ],
    },
    {
        "version": "1.2.0",
        "date": "2026-09-25",
        "notes": [
            "账号管理：已导入的账号可事后修改代理，也可清空改为直连（原来只能删掉重导）",
            "改代理时就地校验格式，拼错协议名（如 socks9）会直接拒绝并列出可选协议",
            "修复：代理协议名不认识时被静默当成 HTTP 代理 —— 配置看着「成功」，"
            "直到创建实例调用 GCP 才失败，排查时离现场很远",
            "创建实例页的「目标账号」精简为 备注 + 已有机器数两列",
            "每个账号显示名下已有几台机器（向 GCP 实时查询，并发执行）",
            "修复：创建页账号表原有一列渲染已被接口抹掉的字段，永远显示「-」",
            "代理变更写入审计日志",
        ],
    },
    {
        "version": "1.1.1",
        "date": "2026-09-25",
        "notes": [
            "修复：`curl … | sh </dev/null` 的 stdin 重定向会覆盖管道，"
            "导致 Docker / nps / Hermes 的安装脚本内容被丢弃（curl 报 23）",
            "远程脚本统一改为「先下载到临时文件，再以 </dev/null 执行」",
            "nps 换源：ehang-io/nps（2021 起停更）→ 2016xyz/sysuahb（djylb/nps v0.34.7）",
            "nps 由容器实测验证：随机进程名、面板 302 → /login/index 均正常",
        ],
    },
    {
        "version": "1.1.0",
        "date": "2026-09-25",
        "notes": [
            "实例备注：创建时可填，实例列表里点击就地修改",
            "实例列表补全：IP 后显示所在地、镜像名称、磁盘大小与类型",
            "实例费用：每小时 / 每天 / 已用费用估算，并标注是否落在 Always Free 额度内",
            "Root 密码默认以圆点显示，点「显示」需重新输入登录密码，15 分钟自动隐藏",
            "账号备注：可加可改；邮箱过长自动折叠，列表以备注为主标识",
            "账号代理：显示哪个账号走了代理、走的是哪种协议；代理密码打码",
            "代理支持 HTTP / HTTPS / SOCKS5 / SOCKS4，允许域名，SOCKS5 统一走代理端解析 DNS",
            "创建后可自动安装：Docker / 3x-ui / nps / Hermes / Ekko，可多选，开机后自动执行",
        ],
    },
    {
        "version": "1.0.1",
        "date": "2026-09-25",
        "notes": [
            "版本号体系启用，统一从 core/version.py 取，页面与接口一处生效",
            "侧栏按「运维 / 资源 / 系统」分组，导航顺序按使用流程重排",
            "按钮按「主要 / 辅助 / 危险」分组并加分隔线，破坏性操作不再与常规操作相邻",
            "侧栏与登录页展示版本号与 GitHub 仓库地址",
        ],
    },
    {
        "version": "1.0.0",
        "date": "2026-09-25",
        "notes": [
            "按钮体系重做，针对 Windows 的边框舍入与字体行高差异做处理",
            "修复窄屏栅格轨道被内容撑破导致的横向溢出",
            "手机 / 平板 / 桌面多视口适配，触屏抬高最小点击高度",
            "GCP 资源总览：17 个分区只读勘察",
            "升级通道 update.sh，更新代码不丢账号数据",
            "登录页视觉对齐 2016xyz/sysuahb",
        ],
    },
]


def info():
    """给前端与脚本用的版本信息"""
    return {
        "version": VERSION,
        "name": APP_NAME,
        "name_cn": APP_NAME_CN,
        "repo": REPO_URL,
        "repo_name": REPO_NAME,
        "issue_url": ISSUE_URL,
        "readme_url": README_URL,
        "latest_notes": CHANGELOG[0]["notes"] if CHANGELOG else [],
        "released": CHANGELOG[0]["date"] if CHANGELOG else "",
    }