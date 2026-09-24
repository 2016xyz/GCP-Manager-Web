"""
版本与仓库信息（单一事实来源）

改版本号只改这里一处，后端 /api/status、控制台侧栏、登录页、
安装与升级脚本提示都从这里取，避免各处写死后互相不一致。

版本规则：语义化版本 MAJOR.MINOR.PATCH
  · MAJOR 不兼容改动
  · MINOR 向后兼容的功能新增
  · PATCH 向后兼容的问题修复
"""

VERSION = "1.0.1"

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