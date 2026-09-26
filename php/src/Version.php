<?php
/**
 * Version —— 版本号与更新日志
 *
 * ★ 本文件由 tools/gen_php_version.py 从 core/version.py 自动生成，请勿手改。
 *   单一事实来源仍是 core/version.py，避免两个版本号各说各话。
 *   重新生成：python3 tools/gen_php_version.py
 */

declare(strict_types=1);

final class Version
{
    public const VERSION = '1.3.0';
    public const APP_NAME = 'GCP Manager Web';
    public const APP_NAME_CN = 'GCP 批量管理控制台';
    public const REPO_URL = 'https://github.com/2016xyz/GCP-Manager-Web';
    public const REPO_NAME = '2016xyz/GCP-Manager-Web';
    public const ISSUE_URL = 'https://github.com/2016xyz/GCP-Manager-Web/issues';
    public const README_URL = 'https://github.com/2016xyz/GCP-Manager-Web#readme';

    /**
     * 与 Python 版 core/version.py 的 info() 逐字段对应。
     * 前端 /api/version 与 /api/status 都读这里的字段名。
     * 只包含 core/version.py 里确实存在的常量（缺的不编造）。
     * @return array<string,mixed>
     */
    public static function info(): array
    {
        return [
            'version'       => self::VERSION,
            'name'         => self::APP_NAME,
            'name_cn'      => self::APP_NAME_CN,
            'repo'         => self::REPO_URL,
            'repo_name'    => self::REPO_NAME,
            'issue_url'    => self::ISSUE_URL,
            'readme_url'   => self::README_URL,
            'latest_notes'  => self::changelog()[0]['notes'] ?? [],
            'released'      => self::changelog()[0]['date'] ?? '',
        ];
    }

    /** @return array<int,array{version:string,date:string,notes:string[]}> */
    public static function changelog(): array
    {
        return [
            [
                'version' => '1.3.0',
                'date'    => '2026-09-27',
                'notes'   => [
                    '新增 PHP 版（php/ 目录）：与 Python 版同一套界面、同一套 API、同一个数据库结构，零 composer 依赖，纯 PHP 8.0+ 实现。前端 console.html / login.html / vendor 从 Python 版原样复制、一行未改（sha256 可校验），因为后端复刻了逐字段一致的 JSON 契约',
                    '★ 宝塔面板支持：php/bt/GUIDE.md 逐步引导（运行目录设 /public、伪静态规则、PHP 扩展与禁用函数、计划任务）、php/bt/nginx-rewrite.conf、php/bt/bt-install.sh （环境自查 + 目录权限 + 初始化 + 计划任务注册）',
                    '★ PHP 版安装引导：php/install-php.sh（裸机一键，自动装 PHP 与扩展）+ php/update-php.sh（升级，绝不碰 data/）+ php/README-PHP.md',
                    '与 Python 版可共用数据：密码哈希逐位相同（PBKDF2-HMAC-SHA256/200000 轮/16 字节随机盐），实测双向交叉认证通过（Python 生成 → PHP 校验、PHP 生成 → Python 校验）',
                    '并发模型改为 SQLite WAL + busy_timeout=5000 + BEGIN IMMEDIATE（PHP-FPM 是多进程，不能用文件锁模拟线程锁）；后台任务改由 CLI worker（bin/task-runner.php）领取，宝塔可用计划任务兜底；实时日志由独立进程 bin/ws-server.php 提供（纯手写 RFC6455，无 composer 依赖），未启动时前端自动退化为轮询',
                    '安全：与 Python 版同一套纪律 —— 强制改密在中间件拦、登录限速只信任可信代理来的 XFF、全部 SQL 预处理、实例列表白名单式丢弃密码字段、账号列表不外发 key_path 与代理明文、命令用数组参数不经 shell、未认证 WS 客户端以 4401 关闭且不下发任何日志',
                    '★ 新增 135 项 PHP 冒烟断言 + 30 项浏览器端到端 + 4 项 WebSocket 安全验证 + 51 条路由的双版响应契约并排比对工具（php/tests/）',
                    '★ 本轮审计发现并修复（详见报告）：「账号导入校验返回值契约不一致导致静默建空账号」、「key_path 接受 file:// 等流包装器」、「CSP 缺 unsafe-eval 导致控制台白屏」',
                ],
            ],
            [
                'version' => '1.2.7',
                'date'    => '2026-09-26',
                'notes'   => [
                    '修复 Debian/Kali 上安装必失败：「venv 模块可用」是误判，导致跳过装包、随后建虚拟环境报 ensurepip is not available —— 在 debian:12 容器实测复现',
                    '★ 检查条件改为同时验证 `import venv` 与 `import ensurepip`：Debian 系 `import venv` 会成功，但真建环境依赖 ensurepip（由 python3-venv 提供）。只判 venv 就会把「缺 python3-venv」误判成「可用」，于是跳过安装',
                    '★ 建环境失败时新增兜底重试：按版本号装 python3.X-venv（如 python3.11-venv）或通用 python3-venv，再重建一次；仍失败才报错并回显 venv 的真实报错',
                    '★ 修复潜伏的 bash 语义 bug：`$SUDO DEBIAN_FRONTEND=noninteractive apt-get …` 在 root（SUDO 为空）下会把 DEBIAN_FRONTEND=noninteractive 当成命令名，报 command not found，安装静默失败。bash 只把「字面量出现在命令词位置」的 VAR=value 视作赋值前缀，而这里命令词位置是展开出来的 $SUDO。改用 env 传变量',
                    '★ 抽出统一的 pkg_install()，apt/dnf/yum 三分支共用，避免同类写法再次出现',
                    '验证：debian:12（无 python3-venv）容器内真跑 —— 自动装包、venv 建成、依赖全部可导入、安装路径标记正确',
                ],
            ],
            [
                'version' => '1.2.6',
                'date'    => '2026-09-26',
                'notes'   => [
                    '修复「按文档升级却报 cd: 没有那个文件或目录」——安装路径与文档不一致，属交付物缺陷，非使用问题。三处一起改，并真跑验证了 4 个场景',
                    '★ 根因：install.sh 的管道模式（README 推荐的一行命令 curl … | bash）默认装到 $PWD/gcp-manager-web，即「你在哪个目录执行就装到哪」；而 README 的升级指引写死 cd /opt/gcp-manager-web && bash update.sh。两处对不上时，cd 在 update.sh 运行之前就失败了，脚本连解释的机会都没有',
                    '★ install.sh：管道模式默认改为绝对路径 —— root 装 /opt/gcp-manager-web（与文档一致），非 root 装 $HOME/gcp-manager-web；在克隆仓库里执行仍是就地安装；APP_DIR 显式指定优先级最高。安装路径统一落成绝对路径',
                    '★ install.sh：装完把真实安装路径写入 /etc/gcp-manager-web.path，并在收尾信息中新增「安装目录」一行（升级命令本来就打印真实路径）',
                    '★ update.sh：新增部署目录自动定位 —— 在 /etc/gcp-manager-web.path、常见位置（/opt、/srv、$HOME 等）、浅层搜索中找一个真部署（排除 .bak/.old/临时目录，且必须同时有 app.py 与 core/version.py）。找到了自动切过去；确实没装过则明确提示「update.sh 是升级通道，不能代替首次安装」并给出安装命令与查找命令，退出码 1 —— 而不是丢一句「找不到 app.py」',
                    '★ README：新增「安装目录」对照表（哪种执行方式装到哪）；升级章节补「先确认装在哪」的查找命令，并说明目录不对也不会白跑；回滚章节的写死路径改为从标记文件读取',
                ],
            ],
            [
                'version' => '1.2.5',
                'date'    => '2026-09-26',
                'notes'   => [
                    '按使用反馈的 4 项改进 + 顺带查出的 2 个前端缺陷。全部在真实浏览器会话上验证过，测试从 606 项增至 620+ 项',
                    '★ 操作审计改服务端分页：原来是「一次拉最近 200 条」，库一大就是纯粹浪费，而且干脆看不到 200 条以前的历史。现默认每页 5 条（可选 5/10/20/50），支持上一页/下一页/跳页，由 SQL 层 LIMIT/OFFSET 分页，响应带 total/page/pages。旧的 ?limit=N 调用保持兼容',
                    '★ 账号管理页可单独测试代理是否有效：原来只有「测试连通性」，它验的是「密钥+网络」整体能不能列出实例，失败时分不清是密钥错还是代理挂。新增「测代理」按钮，只经该代理发一次轻量请求，回显可用状态、延迟、具体报错，并能区分「代理不可达」与「代理活着但被目标拦截」。探测目标地址是硬编码常量，不接受请求方传入（否则就是 SSRF）',
                    '★ 命令执行页显示机器列表：原来页面只有「全部实例 / 仅勾选实例」两个单选项，却**没有任何地方能勾选实例** —— 勾选状态只存在于实例列表页，进到这里看不到机器、也不知道会执行到哪几台。现补上完整目标列表（勾选状态两页互通），并提供「全选/全不选/只选运行中」与空态引导',
                    '★ 实例状态改中文显示：RUNNING / PROVISIONING / TERMINATED 这些是 GCP API 的枚举值，直接摆给用户看不直观。现映射为「运行中 / 创建中 / 已终止」等 9 种中文，原文保留在 title 里便于对照 API 与日志。实例列表页、命令执行页、GCP 资源勘察弹窗三处统一',
                    '★ 顺带修复（测试中新发现）代理密码明文泄漏：mask_proxy 只处理 scheme://user:pass@host 一种写法，只要字符串里没有 @ 就原样返回 —— 而本产品文档里明确支持的 host:port:user:pass 写法没有 @，录入后被原样存库、也原样回显到账号列表，等于把代理密码明文摆在页面上（而页面提示文案却写着「代理密码在列表中已打码」）。现补上该形态的打码，实测 1.2.3.4:8080:user:secretPw → 1.2.3.4:8080:user:***',
                    '★ 顺带修复（测试中新发现）审计表格时间列显示原始时间戳：options 对象里 fmtTime **定义了两次**，后者覆盖前者 —— 生效的那份按字符串处理（Date.parse），于是所有秒级数字时间戳（审计/任务/会话/日志/最后登录）Date.parse 得到 NaN 后直接原样返回，页面上显示成 1790410050.5641239。现合并为一个同时支持「秒级数字」与「ISO 8601 字符串」的函数，解析不了则原样回显（不伪造时间）',
                ],
            ],
            [
                'version' => '1.2.4',
                'date'    => '2026-09-25',
                'notes'   => [
                    '安全审计（逐函数逐分支）后的集中修复，共 17 项。每项都先写 PoC 实测确认可达，再改代码，最后补回归断言锁定；605 项测试全通过',
                    '★ 高危 登录限速可被绕过：限速按客户端 IP 计数，而 IP 取自 X-Forwarded-For 请求头。该头客户端可任意伪造，实测「伪造 XFF + 轮换用户名」可连续爆破 60 次不被拦（不伪造时第 21 次即锁定）。现只在直连来源属于可信代理网段时才采信该头，可用 GCPWEB_TRUSTED_PROXIES 配置（默认仅本机与私有网段，设为 - 表示完全不信任任何代理头）',
                    '★ 高危 首次登录强制改密形同虚设：must_change_password 只在前端提示，服务端不拦。实测未改密仍可调用 /api/status、/api/accounts、/api/create。现由中间件强制，未改密前仅放行改密/登出/查自己，其余返回 403 且 code=must_change_password',
                    '★ 高危 安装脚本以 root 运行服务：install.sh 生成的 systemd 单元没有 User=，全程也没建服务账号，而容器版本早就用非 root 的 appuser。该进程能读 data/ 下的管理员密码、会话库与服务账号私钥，一旦出现任意文件读写缺陷，影响面就是整台机器。现默认降权到专用系统账号 gcpweb（无登录 shell），并加 ProtectHome / ProtectKernelTunables / ProtectControlGroups / RestrictSUIDSGID；建号失败时回退 root 并告警',
                    '★ 中危 SSH 静默信任主机密钥：paramiko 的 AutoAddPolicy 会无条件接受任何主机密钥，中间人可借此截获实例 root 凭据。改用 TOFU（首次记录指纹、之后不一致即中断连接）',
                    '★ 中危 用户接口接受含 HTML 的用户名：实测 username=<img src=x onerror=...> 能建号成功并原样回显。现服务端校验（2-40 位、仅字母数字与 _ . @ -、必须 ASCII），显示名截断 80 字符并拒绝控制字符',
                    '★ 中危 实例数量无上限：count 传 999999 也被受理，一次误操作即可造成费用灾难。现限制 1-200',
                    '★ 中危 登录限速表无界增长：每个失败用户名/IP 都永久留一条记录，攻击者可用海量随机用户名把内存打满。现设 20000 条上限并按需清理（只清锁定已到期的，不能清正在累计的计数器 —— 否则等于送攻击者一个「换用户名即重置计数」的后门）',
                    '★ 中危 验证码清理存在数据竞争：遍历字典时未持锁，并发下会抛 RuntimeError: dictionary changed size during iteration。现全程持锁',
                    '中危 验证码用 random 模块生成，属可预测的 Mersenne Twister。改 secrets.choice',
                    '低危 默认监听 0.0.0.0：本控制台持有 GCP 凭据与实例 root 密码，默认绑全网卡等于把管理台直接送上网。现默认 127.0.0.1，需要对外时用 HOST=0.0.0.0 并打印醒目警告',
                    '低危 会话令牌回传响应体：登录/改密/会话列表会把 token 放进 JSON，前端其实只靠 HttpOnly Cookie。现不再回传，并给 Cookie 补 Secure 标记（GCPWEB_COOKIE_SECURE 可覆盖）',
                    '低危 无任何安全响应头。现补 CSP（object-src \'none\'、frame-ancestors \'none\'）、X-Frame-Options: DENY、X-Content-Type-Options、Referrer-Policy、Permissions-Policy',
                    '低危 审计/任务/日志接口的 limit 无上限，一次请求可拉全表。现封顶',
                    '低危 并发访问 GCP 勘察缓存无锁（共享可变字典），且同一目标被并发请求时会各自发起一次全量勘察（每次几十个 API 调用）。现加锁并防缓存击穿',
                    '低危 跨线程共享的 sqlite 连接有 5 处 commit 未持锁，会与内部持锁写操作交错。现统一持锁提交',
                    '依赖 给 requirements.txt 加版本上限区间（原来只写 >=，上游一个破坏性变更就会让产品起不来），并明确 python-multipart>=0.0.18（更低版本有 CVE-2024-53981 畸形 multipart 边界 DoS，本项目有文件上传接口）',
                    '★ 中危 WebSocket 未受强制改密约束：/ws/logs 走独立握手路径，不经过 HTTP 中间件。实测未改密账号在 HTTP 侧被 403 拦住，却仍能连上 日志流 —— 属「策略覆盖不全」，同一账号拿到日志流会泄露实例与运维信息。现握手时单独再判一次，未改密即以 4403 关闭',
                    '新增 34 项安全回归断言（S 段），含「未改密真的被拦」「重置密码后也要先改密」「未改密连不上 /ws/logs」的端到端实测；测试总数 568 → 605',
                ],
            ],
            [
                'version' => '1.2.3',
                'date'    => '2026-09-25',
                'notes'   => [
                    '修复：后台任务没有终态兜底 —— 任务跑在裸 daemon 线程里，任何未预期异常都会静默杀死线程，任务状态永远停在「运行中」。实测删除实例时实例真的被删掉了，界面却一直显示运行中，用户会以为没执行而重复操作。现给实例动作/命令执行/刷新三类任务都补上兜底，异常时必定写入 failed 终态并记录原因',
                    '改进：实例操作把「本来就已经是目标状态」视为成功（启动一个运行中的实例、删除一个不存在的实例不再报失败）',
                    '说明：asia-south2 上删除实例实测需约 134 秒（实例可见性 133.7s 才消失，operation.done() 在 134.1s），这是 GCP 的真实速度，不是代码空等；期间曾误判为「后台记账慢」并改过一版，已用实测数据回退',
                ],
            ],
            [
                'version' => '1.2.2',
                'date'    => '2026-09-25',
                'notes'   => [
                    '重要修复：勾选「全开放防火墙」在自定义 VPC 项目里必定创建失败。根因是防火墙规则硬编码 global/networks/default，而项目只有自定义 VPC（如 jxihegwg），GCP 返回 404；且防火墙是全局资源、与 zone 无关，外层「换区重试」全是白试，最终把实例也判为创建失败',
                    '修复：防火墙改为绑定实例所在的 VPC；改为实例创建成功之后再补规则，失败只警告不影响实例；建规则前先探测该 VPC 是否已有覆盖 0.0.0.0/0 的规则，只补缺失的方向；同名规则若属于别的 VPC，改用带网络后缀的名字，不越权改绑',
                    '任务日志补打「网络=xxx/yyy」与实际指定区域：原来日志里看不到用的哪个VPC，排查这类 404 只能靠猜',
                    '新增 tools/live_test_create.py：用真实服务账号端到端实测创建→云端核对→清理（含孤儿磁盘核对），30 项断言',
                ],
            ],
            [
                'version' => '1.2.1',
                'date'    => '2026-09-25',
                'notes'   => [
                    '安全修复：/api/sshkey/read 原可读取任意文件（operator 权限即可读出/etc/passwd 与本工具生成的管理员初始密码文件，构成提权路径）；现改为只放行公钥内容，封禁 data/ 目录与私钥文件，越权尝试写审计',
                    '修复手机版「目标账号」表格压住下方「机器备注」的重叠问题（内联 max-height 压制了窄屏媒体查询）',
                    '新增 tools/audit_authz.py 与 audit_authz_matrix.py：路由鉴权与越权巡检',
                    '新增 tools/detect_overlap.py：逐文字块的重叠检测（含滚动容器裁剪校正）',
                ],
            ],
            [
                'version' => '1.2.0',
                'date'    => '2026-09-25',
                'notes'   => [
                    '账号管理：已导入的账号可事后修改代理，也可清空改为直连（原来只能删掉重导）',
                    '改代理时就地校验格式，拼错协议名（如 socks9）会直接拒绝并列出可选协议',
                    '修复：代理协议名不认识时被静默当成 HTTP 代理 —— 配置看着「成功」，直到创建实例调用 GCP 才失败，排查时离现场很远',
                    '创建实例页的「目标账号」精简为 备注 + 已有机器数两列',
                    '每个账号显示名下已有几台机器（向 GCP 实时查询，并发执行）',
                    '修复：创建页账号表原有一列渲染已被接口抹掉的字段，永远显示「-」',
                    '代理变更写入审计日志',
                ],
            ],
            [
                'version' => '1.1.1',
                'date'    => '2026-09-25',
                'notes'   => [
                    '修复：`curl … | sh </dev/null` 的 stdin 重定向会覆盖管道，导致 Docker / nps / Hermes 的安装脚本内容被丢弃（curl 报 23）',
                    '远程脚本统一改为「先下载到临时文件，再以 </dev/null 执行」',
                    'nps 换源：ehang-io/nps（2021 起停更）→ 2016xyz/sysuahb（djylb/nps v0.34.7）',
                    'nps 由容器实测验证：随机进程名、面板 302 → /login/index 均正常',
                ],
            ],
            [
                'version' => '1.1.0',
                'date'    => '2026-09-25',
                'notes'   => [
                    '实例备注：创建时可填，实例列表里点击就地修改',
                    '实例列表补全：IP 后显示所在地、镜像名称、磁盘大小与类型',
                    '实例费用：每小时 / 每天 / 已用费用估算，并标注是否落在 Always Free 额度内',
                    'Root 密码默认以圆点显示，点「显示」需重新输入登录密码，15 分钟自动隐藏',
                    '账号备注：可加可改；邮箱过长自动折叠，列表以备注为主标识',
                    '账号代理：显示哪个账号走了代理、走的是哪种协议；代理密码打码',
                    '代理支持 HTTP / HTTPS / SOCKS5 / SOCKS4，允许域名，SOCKS5 统一走代理端解析 DNS',
                    '创建后可自动安装：Docker / 3x-ui / nps / Hermes / Ekko，可多选，开机后自动执行',
                ],
            ],
            [
                'version' => '1.0.1',
                'date'    => '2026-09-25',
                'notes'   => [
                    '版本号体系启用，统一从 core/version.py 取，页面与接口一处生效',
                    '侧栏按「运维 / 资源 / 系统」分组，导航顺序按使用流程重排',
                    '按钮按「主要 / 辅助 / 危险」分组并加分隔线，破坏性操作不再与常规操作相邻',
                    '侧栏与登录页展示版本号与 GitHub 仓库地址',
                ],
            ],
            [
                'version' => '1.0.0',
                'date'    => '2026-09-25',
                'notes'   => [
                    '按钮体系重做，针对 Windows 的边框舍入与字体行高差异做处理',
                    '修复窄屏栅格轨道被内容撑破导致的横向溢出',
                    '手机 / 平板 / 桌面多视口适配，触屏抬高最小点击高度',
                    'GCP 资源总览：17 个分区只读勘察',
                    '升级通道 update.sh，更新代码不丢账号数据',
                    '登录页视觉对齐 2016xyz/sysuahb',
                ],
            ],
        ];
    }
}
