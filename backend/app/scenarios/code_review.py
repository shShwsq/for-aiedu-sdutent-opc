"""代码审核场景模板(降级后:仅提供预设提示词 + 推荐 skill)

不再定义 checklist/prompt/工具白名单/结果 schema。
(原「代码安全审计 code_security_audit」已并入本场景,旧 id 经别名兼容)
"""
from app.scenarios.base import register_scenario


class CodeReviewTemplate:
    """代码审核场景模板(安全漏洞 + 隐性成本 + 可维护性 + 上线可行性)"""

    id = "code_review"
    name = "代码审核"
    description = "审查代码交付物:安全漏洞、隐性成本、可维护性、能否上线"

    @property
    def preset_prompt(self) -> str:
        return (
            "请审查本仓库的代码交付物，评估其是否达到上线标准。重点关注："
            "安全漏洞(注入类、硬编码凭证、认证授权、SSRF、配置泄露)、"
            "隐性成本(失控 API 调用、死循环烧钱逻辑、明显资源泄漏)、"
            "可维护性(可读性、边界条件、异常处理、并发问题)、"
            "以及阻塞发布的关键缺陷，"
            "给出具体文件位置、问题类型和修复建议。"
        )

    @property
    def recommended_skills(self) -> list[str]:
        # 对应 skills/code_review/ 目录下实际存在的 skill name(来自 SKILL.md frontmatter)
        return [
            "check_sql_injection",
            "check_hardcoded_secrets",
            "check_ssrf",
            "review_error_handling",
            "review_concurrency",
            "review_test_quality",
        ]


register_scenario(CodeReviewTemplate())
