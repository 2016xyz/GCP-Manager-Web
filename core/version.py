"""
版本与仓库信息（单一事实来源）

改版本号只改这里一处，后端 /api/status、控制台侧栏、登录页、
安装与升级脚本提示都从这里取，避免各处写死后互相不一致。

版本规则：语义化版本 MAJOR.MINOR.PATCH
  · MAJOR 不兼容改动
  · MINOR 向后兼容的功能新增
  · PATCH 向后兼容的问题修复
"""

VERSION = "1.2.0"

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