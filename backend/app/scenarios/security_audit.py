"""安全审计场景模板(降级后:仅提供预设提示词 + 推荐 skill)

不再定义 checklist/prompt/工具白名单/结果 schema。
checklist 由 agent2 动态生成 + 用户编辑;
prompt 用通用 prompt;工具全部开放;结果结构通用化。
"""
from app.scenarios.base import register_scenario


class SecurityAuditTemplate:
    """代码安全审计场景模板"""

    id = "code_security_audit"
    name = "代码安全审计"
    description = "审计代码安全漏洞:注入、认证、反序列化、SSRF、配置泄露等"

    @property
    def preset_prompt(self) -> str:
        return (
            "请审计这个仓库的安全漏洞,关注注入类(SQL/命令/模板/代码注入)、"
            "认证与授权(硬编码凭证、弱哈希、JWT 验证、IDOR)、"
            "反序列化(pickle/yaml/marshal)、SSRF、配置泄露、"
            "XSS、路径穿越等类别,给出具体文件位置、漏洞类型和修复建议。"
        )

    @property
    def recommended_skills(self) -> list[str]:
        # 对应 skills/ 目录下实际存在的 skill name(来自 SKILL.md frontmatter)
        # 前端创建任务时默认勾选这些,用户可自行调整
        return [
            "check_sql_injection",
            "check_hardcoded_secrets",
            "check_ssrf",
        ]


register_scenario(SecurityAuditTemplate())
