"""通用场景模板(降级后:仅提供预设提示词 + 推荐 skill)

通用场景 = 不选择特定模板:不预填特定提示词,用户自由描述需求;
无推荐 skill(前端语义为全部可用)。
"""
from app.scenarios.base import register_scenario


class GeneralTemplate:
    """通用场景模板(默认选项)"""

    id = "general"
    name = "通用"
    description = "不限定场景:自由描述任务需求,智能体按需规划执行"

    @property
    def preset_prompt(self) -> str:
        return ""  # 通用场景不预填提示词,用户自由输入

    @property
    def recommended_skills(self) -> list[str]:
        return []  # 无推荐 → 前端全选(等同于不限制)


register_scenario(GeneralTemplate())
