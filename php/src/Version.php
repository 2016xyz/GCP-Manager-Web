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
    public const VERSION = '1.5.5';
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
                'version' => '1.5.5',
                'date'    => '2026-09-29',
                'notes'   => [
                    '【修复】自定义 VPC 网络创建失败：用户传入网络名（如 jxihegwg）时代码直接拼接为 global/networks/jxihegwg，但 GCP 验证该网络不存在时返回 HTTP 400 Invalid value for field \'resource.networkInterfaces[0].network\'。修复：改进网络 URL 处理逻辑，支持空值/null 自动使用 default 网络，支持 https:// 完整 URL 形式，并添加注释说明 GCP 会在创建实例时验证网络存在性',
                    '【修复】PHP 版实例操作（start/stop/delete/reset）全部失败：报错 Gcp::rest(): Argument #4 ($jsonBody) must be of type ?array, stdClass given。根因：php/src/Gcp.php:1482 调用 rest() 方法时传递了 new stdClass() 而不是 []（空数组），不符合类型声明 ?array。修复：将 new stdClass() 改为 []',
                ],
            ],
            [
                'version' => '1.5.4',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【新增】命令执行页加了「快捷命令」，把 ekko-studio-web 的启动 / 重启 / 停止 / 状态做成可点的按钮，点一下即填入命令框。以前这些要手打，最容易漏掉 BIND_HOST —— ekko 的 CLI **没有 --host 参数**，绑哪个地址只能靠这个环境变量，漏了就可能只绑回环、外部访问不到',
                    '  实测（真实实例，逐条跑过）：默认启动就是 0.0.0.0:8648，公网 IP 访问 HTTP 200；显式 BIND_HOST=0.0.0.0 更稳。用控制变量证明了这个变量真的生效：设 127.0.0.1 时监听变成 127.0.0.1:8648，设 0.0.0.0 时是 0.0.0.0:8648',
                    '【关键修正】「重启」没有用 ekko-studio-web restart —— 实测它在服务**没在跑**时只会报 not running 而**不会把服务拉起来**，用户点了会以为重启了、其实服务仍然停着。改成 stop + start 组合，两种起点都能拉起来（运行中 → 新 PID；已停止 → 直接起来），均已实测',
                    '【改动细节】4 条命令，全部是真跑过的原字符串：启动 BIND_HOST=0.0.0.0 ekko-studio-web start --port 8648 --no-open；重启 stop + sleep 2 + start；停止 ekko-studio-web stop；状态 ekko-studio-web status 再列 8648 监听。点按钮只**填入**不自动执行 —— 停止/重启是对所有勾选实例生效的，误触代价太大',
                    '【测试】断言 824 → 837。其中一条是这次最该守的：**quickCmds 必须经 computed 暴露**。模块级 const 在模板里是 undefined，会在渲染期抛错并把整个 #app 清空成白屏 —— 这个坑本文件末尾有专门的兜底注释，是已经栽过一次的。另有「没有任何一条实际命令使用裸 restart」等断言，锁的都是实测结论而非文档写法',
                    '【提示文案】页面上写明了 ekko 监听 8648，且云厂商**安全组 / 防火墙需放行 8648**，否则本机通了、外部仍然访问不到 —— 这是外网访问不了时最容易被忽略的一环',
                ],
            ],
            [
                'version' => '1.5.3',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【修复】ekko 预装的第二层失败：修好 Node 源之后才暴露出来 —— ekko-studio 依赖 node-pty（原生模块），npm 装它时会走 node-gyp「现场编译」，需要 make / g++ / python3，而精简镜像里这三个都没有。真实报错藏在几百行 npm 输出里：`gyp ERR! stack Error: not found: make`',
                    '  修法：新增 _install_build_tools() 助手（deb 用 build-essential python3，rpm 用 gcc-c++ make python3），ekko 在 npm 之前先装工具链。实测目标实例：工具链装好后 npm 安装成功，/usr/bin/ekko-studio-web 在位，npm ls -g 里能看到 ekko-studio@0.7.24',
                    '【修复·根因】顺手修掉一个「哑失败」：ekko 原来把 npm 的输出整段丢进 /dev/null，失败时只回一句「npm 返回非 0」—— 排查线索全被吞掉，用户根本无从下手。现在失败时打出 npm 输出的最后 15 行。这次能定位到「缺 make」正是靠这个改动：不改的话，下一层失败还是只看到一个没有信息量的「返回非 0」',
                    '【测试】断言 819 → 824。除了源码特征，还加了顺序断言（_install_build_tools 必须出现在 npm 之前）和「不得再出现 npm ... >/dev/null」',
                    '【这条链的教训】这个预设有三层问题，一层挡着一层：① 写死 apt-get（CentOS 上第一步就挂）→ ② 缺编译工具链（Node 装好了但 npm 编译失败）→ ③ 把错误输出吞掉（前两层都难定位）。第③点是放大器：它让前两层都变成「看不出为什么的失败」。所以修完第一层之后，务必**再跑一遍**——否则只会在下一个平台上再收到一份同样模糊的报错',
                ],
            ],
            [
                'version' => '1.5.2',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【修复】预装 ekko 在 CentOS 实例上必定失败：「NodeSource 源配置失败，无法安装 Node.js」。根因是脚本里写死了 Debian 那一套（deb.nodesource.com + apt-get），而目标镜像是 CentOS Stream 9 —— 它根本没有 apt-get。Debian 的源脚本能下载下来（HTTP 200），但在 CentOS 上会拒绝执行，于是报「源配置失败」，完全看不出真正原因是「发行版不对」',
                    '  修法：新增 _os_family() / _pkg_install() 两个共用助手，脚本按发行版选源与包管理器。不是只修 ekko 这一处 —— 别的预设将来要装包也用得上，免得各自再写一遍 apt。识别顺序：先看 /etc/os-release 的 ID/ID_LIKE，认不出来再按「哪个包管理器在」判，而不是直接放弃。ekko 现在还会打印识别到的族系，失败时一眼能看出是不是选错了',
                    '  实测（真实实例 + 两个容器）：centos 9 目标机 → rpm 族系 → NodeSource(RHEL) 配置成功 → dnf install nodejs 成功 → node=v24.21.0 npm=11.19.0；debian:12 容器 → deb；rockylinux:9 容器 → rpm',
                    '【测试】断言 811 → 819。含「生成脚本逐字节一致（8 种勾选组合）」「_os_family 在真实 debian/rocky 容器里分别返回 deb/rpm」，以及一条通用规则「任何预设都不得无条件调用 apt-get」—— 把这类问题从「修一个」变成「挡住一类」',
                    '【顺带】修掉两条自己写的断言：① 原来写死数 `_run_remote https` 的出现次数，ekko 改成传变量后计数掉了、断言反而误报 —— 改成数「真正的调用行」；② 又出现「断言被自己的注释误命中」，按既有纪律先剥注释行再查',
                ],
            ],
            [
                'version' => '1.5.1',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【修复·自省】v1.5.0 引入的回归：/api/status 返回 500。上一版给密码登录加 askpass 兜底时，新写的「探测本机 ssh 版本」会去起子进程，而宝塔默认把 proc_open 写进 disable_functions —— PHP 调用被禁用的函数抛的是 **Error 而不是 Exception**，Ssh::available() 又是 /api/status 的调用点，于是一探测就把状态接口打崩。日志原文：`unhandled: Error: Call to undefined function proc_open() @ Ssh.php:604`',
                    '  修法两层：① procRun() 在调 proc_open **之前**先判 function_exists，被禁用时降级成一条普通错误结果，而不是抛 Error；② 探测 ssh 版本前先判能不能起进程，不能起就不探测。顺带修正探测失败时的取值方向：原来「认不出版本」当成不支持，现在改成**假定支持** —— 两种猜法的代价不对称：猜错成「不支持」会让功能静默不可用，且报错会把矛头指向错误的方向（说你缺 sshpass）；猜错成「支持」只是认证失败并给出可理解的提示。宁可失败在明处',
                    '【修复】CSS 类名撞车：新版徽章用了 .badge.n，而审计日志「操作」列早就用了 class 为 badge n 的写法（样式表里另有 .badge.n{...} 规则）—— 复用同名把审计那列一起染成暖黄。已改名为 .badge.upd。这条是浏览器实测徽章「凭空出现」才挖出来的',
                    '【测试】断言 803 → 811。关键两条是**真造出禁用环境**来跑：`php -d disable_functions=proc_open` 下调 execSsh 必须返回结构化失败、Ssh::available() 必须仍能正常返回 —— 只查源码文本证明不了不会崩',
                ],
            ],
            [
                'version' => '1.5.0',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【新功能】检查更新：控制台「个人设置 → 关于」新增「检查更新」按钮，去 GitHub 问一次有没有新版本。以前那张卡片只显示**本地**的更新日志，没有任何东西去比对远端 —— 它永远只说明当前版本有什么，用户根本无从知道有没有新版',
                    '  有新版时：版本号旁出现徽章，并列出「本地更新日志里比当前新的条目」（用户想知道的不只是有没有新版，还有新在哪）、发布说明链接、发布时间，以及升级命令。只做**检查**不做自动升级 —— 升级要动部署目录里的文件，不是一个按钮该干的事',
                    '  取数分两段：先 `/releases/latest`（正式 Release，带发布说明）；返回 404 说明仓库只打了 tag 没建 Release，就退回 `/tags` 取最高版本号。两条路都走不通才算失败，失败时把**两条路各自的 HTTP 状态与错误**都列出来，便于定位',
                    '  三处刻意的取舍：① 检查结果**要求登录**，不塞进匿名可读的 /api/version —— 否则等于给匿名用户一个刷外网的接口；② 结果缓存 1 小时，因为 GitHub 未认证调用每 IP 每小时只有 60 次，而这是个随手会点的按钮；③ 检查失败时仍返回 HTTP 200，把原因放在 reason/detail 里让前端照常渲染 —— 用 4xx 会让前端走通用错误分支，反而看不到细节',
                    '【仓库】补建了 26 个 GitHub Release（原来只有 tag）。顺带发现：批量补建时GitHub 按 created_at 排「最新」，最后建的那个（v1.0.1）会变成 latest —— 已用 make_latest 把最新的版本钉回去。这个坑不复核就发现不了，而且后果很坏：检查更新会拿一个很旧的版本号当「最新」，然后显示「已是最新」，看着正常但是全错',
                    '【顺带】登录页已不再展示版本号，/api/version 的注释同步改准；该接口保留为匿名可读，供未登录时的探活与兜底查询',
                    '【测试】断言 784 → 803。最有价值的一条是「两版的版本比较结果逐项一致」—— 开发中它真抓到过分歧：PHP 侧漏了对输入做 norm()，version_compare(\'v1.5.0\', \'1.4.7\') 会把带 v 的判成更旧，于是漏报新版本（tag 名天然带 v，这不是边角情况）',
                ],
            ],
            [
                'version' => '1.4.7',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【修复】预装/命令执行全部失败：「需要密码认证但未安装 sshpass」。起因是 PHP 版的密码登录**硬前置**了 sshpass 这个系统包 —— 服务器上没装，于是 9 个目标一个都跑不成，整个功能不可用',
                    '  改为两条路：有 sshpass 就用 sshpass（成熟、行为可预期）；没有则退回 OpenSSH 自带的 SSH_ASKPASS 通道（≥ 8.4），**零系统依赖**。两条路都在真实实例上实测登录成功过，不是照文档推断的',
                    '  细节：用 SSH_ASKPASS_REQUIRE=force —— 它的含义是「即使有终端也走 askpass」，所以不需要 setsid 去摘控制终端；不用 setsid 还顺带解决一个隐患：进程树只剩 ssh 一层，超时终止时打的就是 ssh 本身，不会留下孤儿进程。密码仍只走环境变量，既不进 argv（ps 看不到），也不写进临时脚本本体，脚本用完在 finally 里删掉',
                    '  同时把「不可用」时的提示改准确：原来只说「装 sshpass」，现在会说明有两条路、以及本机 ssh 版本是否够新',
                    '【测试】断言 773 → 784。关键几条是**实跑**：造一个只有 ssh、没有 sshpass 的 PATH，断言 available() 仍然 ok 且自动走 askpass；再断言连 ssh 都没有时如实报错、不假装可用',
                ],
            ],
            [
                'version' => '1.4.6',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【改进】GCP 配额报错现在会说人话。原文实测长这样：`Quota \'CPUS_ALL_REGIONS\' exceeded. Limit: 12.0 globally.` —— 这句话没说错，但对用户没有半点可操作性：看不出是自己的配额满了、还是 GCP 故障、还是工具有 bug，也不知道下一步能干什么，更不知道「重试有没有用」',
                    '  现在这条错误后面会跟一段可行动说明：这是什么、为什么重试/换区都不会成功、以及四个可选出路（删实例释放 CPU / 用更小机型 / 控制台申请提额 / 换账号）。两版对同一错误说**逐字节相同**的话，有断言锁死',
                    '  顺带明确一件容易误解的事：配额已满时再建只会立刻失败，还会白等一轮超时 —— 工具侧不做「先重试试试」是对的行为，只是以前没把原因讲清楚',
                    '【调研结论·不做预检】查过能不能在创建前预判配额：Compute 的 regions 接口返回 113 条配额指标，**里面没有 CPUS_ALL_REGIONS**（它只在 Cloud Quotas API 里，需额外启用服务）。拿一个查不到的配额做预检，只会给出虚假的「配额充足」——比不预检更糟。所以选择把错误讲清楚，而不是假装能预判。这个取舍写进了代码注释，免得以后有人再「补」一个假预检',
                    '【测试】断言 763 → 773。含「两版译文逐字节一致」「非配额错误不得误报（返回 None）」「区域配额说区域、全局配额说全局」',
                ],
            ],
            [
                'version' => '1.4.5',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【调整】登录页不再展示「仓库地址 + 版本号」页脚。那两行是发给**所有未登录访客**的，把源码出处和版本号挂在门外没收益，反而等于免费告知「这个部署跑的是哪个版本、有没有对应的已知漏洞」。版本/仓库/更新说明保留在登录后的「个人设置 → 关于」卡片里',
                    '【顺带】登录页 HTML 顶部那段说明性注释里原本写着仓库地址，注释同样会随 HTML 发给所有访客 —— 技术说明留下，出处信息去掉。console.html 早就没有这类引用',
                    '【清理】页脚相关的 HTML / CSS / JS 一并删净，不留死样式与死引用：login.html 的 footer 块、login.css 的 5 条规则与 2 个 media query、login.js 的 loadVersion() 及其调用。删完复核了标签配平、JS 语法、CSS 花括号配平，并确认「关于」卡片仍能显示版本',
                    '【测试】断言 761 → 763。把三条**已过时**的旧断言（原要求页脚存在）反转成「必须不存在」，并补一条「注释里不得出现仓库地址」—— 这类断言不反转的话，改动之后测试还会因为错误的原因继续通过',
                ],
            ],
            [
                'version' => '1.4.4',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【修复·PHP 版】实例列表三列全空 —— 镜像 / Root 密码 / 费用。三个根因各自独立，凑在一起看起来像「前端没渲染」，实际全在后端：',
                    '  · **镜像列显示的是一串实例名**：GCP 运行中实例的 disks[0] 里没有 initializeParams，    原代码退回取 disks[0].source —— 那是**源磁盘的 selfLink**，basename 就是磁盘名（≈ 实例名）。真正的镜像线索在 licenses[]（centos-cloud/.../licenses/centos-stream-9）。    现在改为 sourceImage → licenses 推断 → 留空，并加 image_from 标注来源，前端对推断值打「推断」角标，不当成精确值',
                    '  · **磁盘类型显示 ERSISTENT**：两处错叠加。其一 disks[0].type 是**磁盘模式**（PERSISTENT / SCRATCH），不是磁盘类型（pd-standard 之类在 initializeParams.diskType，运行中实例拿不到）；其二全仓 21 处用 `substr($s, (int) strrpos($s,\'/\') + 1)` 取 URL 末段 —— **`strrpos` 找不到时返回 `false`，`(int)false+1 = 1`，于是变成 `substr($s,1)` 静默吃掉首字母**。两版一致的写法是 `rsplit(\'/\',1)[-1]`（Python）或加守卫；PHP 现在统一走新的 `Gcp::short_name()`，磁盘类型改为「本地记录优先、拿不到就留空」，模式单列 disk_mode',
                    '  · **Root 密码整列「无记录」、费用整列空白**：PHP 的 instances() 把 GCP 原始行直接返回，**本地库一个字都没用上**。而 has_password / note / installs 只存在本地，disk_type / image_key 运行中实例也从 GCP 拿不到 —— 费用又依赖机型与磁盘类型，于是连锁全空。现在按 Python 的约定合并（本地创建时记下的规格优先、GCP 实时数据兜底），费用走早就移植好却一直没接上的 Cost::instance_cost',
                    '【说明】修的是「拿错字段」和「没合并本地库」，不是「GCP 没返回」。GCP 侧的 disks[0] 本来就长这样（已用真实响应逐字段核对）：{"type":"PERSISTENT","source":".../disks/vm-xxx","licenses":[".../centos-stream-9"],"diskSizeGb":"30"}',
                    '【测试】断言 739 → 761。含一条用**真实 GCP 响应片段**喂进两版解析器、要求输出逐字节一致的契约断言；以及直接调 Gcp::short_name(\'PERSISTENT\') 断言不被吃首字母（源码文本断言会随重构失效，行为断言不会）',
                ],
            ],
            [
                'version' => '1.4.3',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【加固·前端】命令执行页的「预览将执行的脚本」也加了类型判断：r.script 不是字符串时不再让 .split 抛 TypeError 把整块预览搞没，而是直接把原始响应打出来。与 v1.4.2 修的 dry-run plan 是同一类问题 —— 后端契约一漂移，前端只报一句「渲染错误」，看不出是接口问题',
                    '【测试】断言增至 739 条',
                ],
            ],
            [
                'version' => '1.4.2',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【修复·PHP 版】点「预检（Dry-run）」后弹出「渲染错误 · Vue runtime-5] plan.forEach is not a function」，预检结果区整块渲染不出来（页面其余部分可用）。根因：ApiGcp::create() 的同步 dry-run 分支自己拼了 build_instance_spec() 的结果当 plan —— 那是「机型/磁盘规格对象」{machine_type, disk_type, disk_size_gb, ...}，不是「每个账号一条」的计划数组，前端拿到对象后 .forEach 直接抛异常。而 worker 里那条 dry-run 路径（Gcp::runCreateTask）用的才是真的 plan_preview —— 同一个功能两条实现，API 层那条调错了函数。修复：plan_preview 改为 public，API 层与 worker 共用同一实现，不可能再分叉',
                    '【修复·PHP 版】dry-run 任务现在也会写日志并把 plan 存进 result、当场判 done，与 Python 的 update_task(task_id, \'done\', \'dry-run 预览完成\', {\'plan\': plan}) 对齐；原先 PHP 只建了任务就返回，任务列表里那条 dry-run 永远悬着',
                    '【加固·前端】新增 asArr(v, what) 数组兜底助手：接口返回值不是数组时不再炸整页，而是返回 [] 并 console.warn 打出实收类型与原始值 —— 不静默吞掉异常值。r.plan 另加了显式 Array.isArray 判断，违约时直接弹出「预检结果异常」并附原始响应 JSON，一眼能看出是接口违约而不是前端坏了',
                    '【加固·前端】同类隐患一次性收口：另外 6 处对接口返回值的无保护 .forEach/.map/.filter/.length（accounts 实时实例数 counts、任务详情 results/instances、WS 轮询 items ×2）全部过 asArr。这些今天能跑只是因为后端恰好返回数组，契约一漂移就是整页崩',
                    '【测试】新增 9 条断言（738 通过 / 0 失败）：PHP dry-run 必须走 Gcp::plan_preview、不得再用 build_instance_spec 冒充 plan、plan_preview 必须 public、两版都指向同一实现、前端必须 Array.isArray 判 plan、asArr 助手存在且会告警、全仓无对接口返回值的无保护数组方法、PHP plan 字段与 Python 齐平、以及用假账号实打一次 plan_preview 断言「返回的必须是数组」（原 bug 的照妖镜）',
                    '★ 这类 bug 的特征：后端一处契约漂移，前端只报一句「渲染错误」，用户看不出是接口问题；而单测如果只断言「接口 200 / ok true」就完全抓不住。线上复现证据：POST /api/create {dry_run:true} → plan 类型 = object',
                ],
            ],
            [
                'version' => '1.4.1',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【修复·真 bug】预装脚本用 `sh` 执行下载来的官方安装脚本 —— Debian/Ubuntu 的 /bin/sh 是 dash，而官方脚本普遍用 bash 专有语法。真实 SSH 实测：Hermes 官方 install.sh 在 dash 下当场报 `Syntax error: Bad for loop variable` 并中止，日志里只留这一行，用户只会以为脚本本身坏了。`_run_remote` 改为优先用 bash',
                    '【修复·真 bug】每项的「退出码」其实只是**块内最后一条命令**的退出码。实测 docker 那一项 apt 已报 `not enough free space` 装失败了，但末尾是 `systemctl ... || true`，状态照样 0，日志打「退出码 0」——这种假绿比报错更坑，用户以为装好了。修法：每项 subshell 内加 set -e，按成功/失败分开打印，结尾汇总未成功的项并 exit 1，让任务真的判失败',
                    '【修复】4 处 `_run_remote ... || true` 吞掉了安装失败（3x-ui / nps / hermes 及 ekko 的 Node 安装步骤）。现在把状态存下来，跑完诊断回显再交出去 —— 回显不代表成功，返回码才算',
                    '【修复】ekko 预设 `npm install` 失败时写的是 `exit 0` —— 装失败却回报成功。改 exit 1。',
                    '【修复】`_run_remote` 里清临时文件用的是字面量 `_f` 而不是 `$_f`，临时文件永远删不掉（每次预装都在 /tmp 留一份下载的安装脚本）',
                    '【测试】新增 8 条断言（729 通过 / 0 失败），含一条**两版脚本逐字节一致**的契约断言：前端只发 installs，脚本由各自后端生成，若两版生成结果不同，界面看不出区别 —— 最难发现的一类分裂。这条断言在 8 个 key 组合上逐一比对 Python 与 PHP 的输出',
                    '★ 这轮两个 bug 都是「单测查不出」的：`bash -n` 只查语法，既查不出用了 sh 还是 bash，也查不出退出码语义。是在 docker 容器里起真实 SSH 目标跑完整脚本才暴露的',
                ],
            ],
            [
                'version' => '1.4.0',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【功能】预装脚本从「创建实例」页搬到「命令执行」页 —— 对**任意已存在的实例**随时可装。原先只能在建机时一次性勾选，机器建好之后就再也装不了；而且某个安装项卡住会拖住整个创建流程',
                    '【关键】SSH 密码**由服务端自动获取**：worker 从 vm_passwords 表取 root 密码，与 /api/execute 完全同一条路径。用户不再需要先到「实例列表」逐台出示密码、再手动 SSH 登录去装东西。密码不下发前端，因此本接口不需要二次验证登录密码 ——那是 /api/instances/password（把明文密码显示给用户看）才需要的控制',
                    '【新增接口】POST /api/execute/install {installs, targets?, all?, concurrency?, command_timeout?, idle_timeout?, verify?} → {ok, task_id}。两版共用同一契约：Python 用 InstallRequest 模型，PHP 手工校验类型对齐',
                    '【新增】任务归类 kind=install / id 前缀 inst-，与手输命令（execute / exec-）区分；任务列表里一眼能看出哪些是预装任务',
                    '【新增】GET /api/install_presets?keys=docker,3x-ui 返回拼好的脚本供前端预览。预览与真正执行走**同一个 build_script**，不会出现「预览一套、执行另一套」',
                    '【修复·易踩坑】无密码记录的实例（创建时用「SSH 密钥模式」）现在会被**明确跳过并写明原因**，而不是拿空密码去连、最后抛个「认证失败」让用户猜。本工具不保存实例登录私钥（accounts.key_path 是 GCP 服务账号的，不是登录用的），所以 ssh_key 模式的实例无法自动化 —— 这一限制现在会直接讲清楚',
                    '【修复·串状态】创建流程显式发 installs: []。预装选择状态现在是「命令执行」页的，若沿用 this.installPicked，用户先在那页勾了 docker 再回来建机，新机器会被动装上 —— 典型的跨页串状态 bug',
                    '【交互】预装卡片沿用 label 包 checkbox 的写法，点卡片任意位置即可切换（checkbox 挪到 label 外会失效，且丢失键盘可达性）；「一键预装」带二次确认，弹窗写明目标范围与将执行的项',
                    '【测试】新增 25 条断言（719 通过 / 0 失败）：接口契约、kind 归类、脚本预览与执行一致性、入参校验四态、PHP 侧路由与 worker 分支、无密码实例的明确提示、以及「预装确实搬走了而不是两处各留一份」的结构断言',
                ],
            ],
            [
                'version' => '1.3.6',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【修复】每个页面都会产生一条 /favicon.ico 的 404：浏览器找不到 <link rel=icon> 就会默认去站点根取 /favicon.ico，而图标实际在 /static/favicon.ico。已在 console.html 与 login.html 显式声明 link rel=icon / apple-touch-icon',
                    '【缓存分层】应用外壳 HTML 改用 no-cache + ETag 回源校验：console.html / login.html 是外壳，长缓存会让前端修复发布了用户还在跑旧代码（实测踩过：整个 /static/ 给 expires 7d）。vendor/ 里的第三方库内容不可变，仍走 30d immutable。Python 版本来就是 no-cache，本次让 PHP 版与之对齐',
                    '【运维记录】经 CDN 访问时 /ws/logs 返回 400、本地直连 101 —— 腾讯 EdgeOne 未透传 Upgrade 头（WebSocket 需在控制台开启，或等 HTTPS 修好后走 wss）。不影响功能：前端会自动降级为轮询拉日志',
                ],
            ],
            [
                'version' => '1.3.5',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【安全修复·凭据泄漏】上传账号时前端把 proxy 参数拼进 URL query：uploadAccounts() 原写成 `const url = `/api/accounts/upload?proxy=${...}&proxy_type=${...}``。代理 URL 形如 socks5h://user:password@host:port —— 拼进 query 后，明文密码会被 **nginx 的 access_log、CDN（EdgeOne）日志、浏览器历史**完整记下。线上日志实测抓到过该条记录（URL 编码但可轻易还原）。已改为放进 FormData（请求体）',
                    '【同处第二个 bug·静默丢弃】后端读的是 $_POST[\'proxy\']，而 $_POST **只装请求体**，query 参数根本进不去 —— 于是通过「上传 JSON 文件」导入账号时，代理被静默丢弃、账号存成「无代理 / HTTPS」。用户以为代理设上了其实没有，比直接报错更难排查。前端改走 FormData 后两边对上；后端同时加了护栏：URL 里出现 proxy 直接 400 并提示强制刷新，避免老缓存前端继续静默丢失配置',
                    '影响面：Python 版前端是同一份 console.html，走同一个 upload 契约（FastAPI 侧用 Form(...) 取参，同样读不到 query），因此**两版的这个 bug 一并修好**',
                    '★ 顺带修掉一处两版前端漂移：本次只改了 static/console.html，php/public/static/console.html 没同步 —— 已同步并确认 md5 一致（tests_e2e.py 早有「两版前端逐字节一致」断言，会拦住这类漂移）',
                    '新增 3 条断言锁死：前端不得把 proxy/password/secret/token 等拼进 URL query；upload 的 proxy 必须走 FormData；upload 后端必须对 URL 里的 proxy 明确报错',
                    '★ 提醒：此前已进入 nginx 与 CDN 日志的代理凭据应视为已泄漏，建议轮换代理密码；并清理 /www/wwwlogs 下的历史记录',
                ],
            ],
            [
                'version' => '1.3.4',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【修复】PHP 版设不了 SOCKS5H/SOCKS4 代理：前端两个下拉提供的是 HTTPS / HTTP / SOCKS5H / SOCKS4，而 ApiGcp 里两处硬编码白名单写的是 in_array($pt, [\'HTTPS\',\'HTTP\',\'SOCKS5\']) —— **SOCKS5H 与 SOCKS4 这两个 UI 选项全被 400**「不支持的代理类型：SOCKS5H」，用户既存不了代理也测不了代理',
                    '根因是「白名单有两份」：权威集合本来是 Gcp::PROXY_TYPE_LABELS（HTTP/HTTPS/SOCKS4/SOCKS5/SOCKS5H），ApiGcp 却又手写了一份且已与前端脱节。现已改为统一查 Gcp::PROXY_TYPE_LABELS，并顺手把传入的类型 toUpper 归一化（前端/接口大小写不一致时也稳）',
                    '★ 为什么测试没拦住：用例发的是前端根本不产生的 \'SOCKS5\'，等于一直在测一个不存在的契约。已新增 4 条**前后端契约断言**：把 console.html 里所有 <option value> 取出来逐个要求后端白名单认；反向再锁一遍 ApiGcp 不得再硬编码白名单；并校验 curl_proxy_type 覆盖白名单全部类型。该类断言一律先剥注释行再匹配（说明文字里要原样引用旧写法，不剥会自伤）',
                    '【修复·更严重】PHP 版代理探测用 HEAD，导致**能用的代理一律被判「不通」**：test_proxy 经 curl_request(headOnly:true) 落到 CURLOPT_NOBODY，即发 HEAD 请求；而探测目标 PROXY_TEST_URL = https://www.googleapis.com/discovery/v1/apis 在 HEAD 下一律返回 404（实测 HEAD=404 / GET=200），判定又是 `$code < 400` —— 于是代理完全正常也显示「代理不可用」，用户只会去怀疑代理和网络，不会怀疑探测实现。已改为发 GET + HEADERFUNCTION 抓状态码 + WRITEFUNCTION 立刻中断传输（discovery 列表 380KB，不能真下完）；中断被 curl 报成 CURLE_WRITE_ERROR(23)，只要状态码已拿到就当成功。Python 版用的是 requests.get，不受影响',
                    '实测（真实 SOCKS5 代理，4 个用例全部 ok=true / HTTP 200 / ~1.1s）：socks5h:// + SOCKS5H、socks5h:// + HTTPS（前端默认值，靠 scheme 纠正）、socks5:// + SOCKS5（归一化为 SOCKS5H）、大小写混写 Socks5H:// —— 全部通过；curl -v 确认走的是 SOCKS5 远端解析（remotely resolved）且出口 IP 变为代理 IP',
                    '新增 3 条断言锁死 HEAD 陷阱：curl_request 不得出现 CURLOPT_NOBODY、test_proxy 必须走 GET、PROXY_TEST_URL 必须仍是硬编码常量（防 SSRF）',
                    'Python 版不受影响：它本来就没有这层白名单，直接把 proxy_type 交给 parse_proxy_input 校验',
                ],
            ],
            [
                'version' => '1.3.3',
                'date'    => '2026-09-27',
                'notes'   => [
                    '【宝塔部署致命缺陷·线上实测复现】bt/nginx-rewrite.conf 里写了 `location = /index.php { client_max_body_size 8m; fastcgi_read_timeout 300s; }`，想「只补超时」。但 nginx 的 location 优先级是「精确 = > ^~ 前缀 > 正则（按书写顺序）> 最长前缀」，这个精确匹配**盖掉了宝塔 enable-php-XX.conf 里那份带 fastcgi_pass 的正则 location**，而它自己没有 fastcgi_pass —— 于是 /index.php 退化成静态文件被直接下载：GET /login → 200 application/octet-stream 12053 字节（正是 public/index.php 的源码）。后果：所有路由全废 + 入口源码泄漏。已在真实宝塔（CentOS Stream 9 + 宝塔 9.0.0 + PHP 8.2）上复现并修复',
                    '修法：伪静态只保留「路由到入口 + 拒绝敏感路径」，PHP 解析一律交给宝塔；需要调超时/上传上限改用**服务器级**指令，放进宝塔站点扩展目录 `/www/server/panel/vhost/nginx/extension/<域名>/tuning.conf`（在 location 之外，会被各 location 继承）',
                    '敏感目录由普通正则改为 `^~` 前缀匹配（location ^~ /src/ 等）：`^~` 优先级高于正则，能稳定压过宝塔那份 PHP 正则，避免 /src/*.php 被送进 PHP-FPM 执行',
                    'tests_e2e.py 新增 3 条断言锁死该缺陷：伪静态里不得有生效的 `location = /index.php`、不得有生效的 fastcgi_pass、敏感目录必须用 ^~（断言只看非注释行，避免把说明文字算进去）',
                    'php/bt/GUIDE.md 新增「血泪坑」小节：完整原理、事故现象、修法与一行自检命令',
                ],
            ],
            [
                'version' => '1.3.2',
                'date'    => '2026-09-27',
                'notes'   => [
                    '★ 补修三项低危（第二轮审计中标注但未即时修的残留项，均两版同步）',
                    '【防用户名枚举】禁用账号的登录分支原先直接返回「该账号已被禁用」：既不哈希、文案又与密码错不同 —— 实测耗时 0.00ms vs 密码错 ~100ms，100ms 的差量在网络上也肉眼可辨，足以枚举出「存在且被禁用」的用户名。现在三条分支（不存在 / 被禁用 / 密码错）统一走完整验密并回同一句「用户名或密码错误」（实测 101ms vs 100ms，差 1ms）',
                    '【权限回收即时生效】改角色原先不吊销该用户的会话：会话行里冻结了登录那一刻的 role，把某人从 operator 降成 viewer 后，他手里的旧会话**仍按 operator 放行**，权限回收被延迟到下次登录。现在改角色 / 禁用都会立刻吊销其全部会话（两版）',
                    '【开机脚本不再把密码写进日志】root 密码模式的开机脚本是 `set -euxo pipefail`，而 `set -x` 会把 `echo \'root:<明文密码>\' | chpasswd` 整条命令回显到 stderr —— 上面刚把 stderr 重定向进 /root/gcp_root_mode.log，于是明文密码被写进实例上的日志文件，该日志同时进 GCP 串口输出缓冲区。现在该行用 set +x / set -x 包住，并把日志文件权限收紧为 600（两版）',
                    '★ 新增第 9 个安全回归 tools/security/poc_low_hardening.py（枚举文案与耗时 / 角色变更吊销 / 脚本 trace），并在 tests_e2e.py 里把「禁用账号文案必须与密码错一致」锁成断言',
                ],
            ],
            [
                'version' => '1.3.1',
                'date'    => '2026-09-27',
                'notes'   => [
                    '★ 安全修复轮（第二轮逐函数逐分支审计）：修掉一条完整可达的「只读账号 → 拿走全部实例 root 密码」链路，以及一个会让登录验证码形同虚设的问题。共 12 项修复 + 8 个可重跑安全回归',
                    '【致命】任务 payload 里保存着 root_password 明文（worker 执行需要），而 /api/tasks、/api/tasks/{id} 的权限点是 view —— viewer 是只读角色。实测：只读账号一次请求就拿到全部实例 root 密码，把 POST /api/instances/password 的二次验证绕过。修复：在所有下发边界做字段投影（两版都修），并覆盖历史数据；日志出口同样擦拭（老库里已写入的明文也会被擦）',
                    '【高危】验证码降级成明文 SVG：缺 gd（PHP）/ Pillow（Python）时，验证码字符被写成 <text> 再 base64 放进响应的 image 字段 —— 解 base64 即可读出，不需要 OCR。实测在本机 PHP 8.0（无 gd）上读出的字符与库里验证码逐字符相同，也就是说宝塔默认环境下登录验证码等于不存在。修复：删除降级路径，缺依赖时明确报错（503 + 可操作提示），并把 gd 列入 install-php.sh / bt-install.sh 的必需扩展',
                    '【高危】PHP 版 POST /api/instances/password 漏了「登录密码复核 + 限速」：Python 版有，前端也因为 Python 版要求密码而照样弹框让用户输入，用户以为有防护，PHP 后端却直接忽略该字段。实测：不带密码 → 200 直出 root_password 明文；密码错误也 200。已逐条对齐 Python 的三道控制（含共用 LoginGuard 限速）',
                    '【高危】代理用进程级环境变量实现，并发时互相污染：一个线程退出会让仍在 with 里的线程代理消失（变直连），退出顺序颠倒又会把 A 的代理写回进程 —— 实测无代理线程在自己 with 内看到别的账号的代理，等于把该账号的 OAuth 凭据送到别的账号的第三方代理上。修复：进程级 RLock 串行化 + 无代理调用也进锁；完全没配代理的部署不加锁（保持原并发性能）',
                    '【中危】POST /api/execute 的 all 缺省为真，且判定写成 `if all_instances or not target_list` —— 给了 targets 但没写 all 的请求会把该账号下所有实例一并执行。前端显式传了 all 所以 UI 看不出，但 curl / 脚本 / 第三方调用的命令会打到远超预期的机器上。实测：修复前 targets=["t1"] 实际执行 3 台，修复后只执行 1 台。改为三态语义（None/True/False），两版都改',
                    '【中危】PATCH /api/accounts/{id} 把请求体任意键透传给 store，而 store 白名单含 key_path —— 可绕开 POST /api/accounts 的「内容必须是服务账号 JSON」校验，并借 key_exists/key_file 构成任意路径存在性 oracle；响应还把 key_path 从 updated 里滤掉，调用方看不出自己改了它。已加字段白名单',
                    '【中危】裸 int() 造成 500 + /api/config 配置投毒：/api/config 接受任意键值并持久化，用户把 disk_size_gb 存成 "abc" 后每次「创建实例」都 500。已加 safe_int()（非法即回落 + 范围夹取）与配置键白名单',
                    '【中危】勘察缓存的写与淘汰在锁外（Python），且两版的「防缓存击穿」其实都没实现 —— _INSPECT_INFLIGHT 是定义了从不使用的死代码，PHP 侧注释还谎称 flock 做了串行化。已实现真正的单飞（抢到计算锁的人去算，其余等结果），并修正假注释',
                    '【中危】WebSocket 只在握手时校验会话：管理员踢掉被窃会话后，长连接仍在持续收任务日志。两版都已加周期性复检（默认 30 秒，GCPWEB_WS_AUTH_EVERY 可调）—— 修复前 25 秒仍不断开，修复后 0.8s（PHP）/ 1.0s（Python）以 4401 断开',
                    '【低危】mask_proxy 漏「密码里含 : 或 @」的形态（原样返回明文），已按「宁可多打码也不能漏」重写；两版一致',
                    '【低危】其他：worker 子进程继承 Web 监听套接字（已关 fd）；ws-server.php 忽略 --host/--port（文档教用户这么用，已支持，命令行优先）；ssh.py 的 `if False else` 死代码；install-php.sh 补端口继承告警；bt-install.sh 用 ini_get 取代解析 php -i 文本',
                    '★ 新增 8 个可重跑安全回归：bash tools/security/run_all.sh（任务脱敏/密码二次验证/WS 吊销/execute 三态/代理并发隔离/日志擦除/勘察单飞/fd 继承），并把「验证码必须是 PNG 且响应不含明文」写进冒烟断言',
                ],
            ],
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
