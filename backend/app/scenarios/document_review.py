"""文书审核场景模板(降级后:仅提供预设提示词 + 推荐 skill)

覆盖合同/协议类文书的审查:权责对等、付款与违约、知识产权归属、霸王条款。
当前仅支持 txt/md 等纯文本格式(docx/pdf 无解析),description 已引导用户。
无推荐 skill(前端语义为全部可用)。
"""
from app.scenarios.base import register_scenario


class DocumentReviewTemplate:
    """文书审核场景模板(合同/协议类文本)"""

    id = "document_review"
    name = "文书审核"
    description = (
        "审核合同/协议类文书:权责对等、付款与违约、知识产权归属、霸王条款"
        "(请上传 txt/md 纯文本格式)"
    )

    @property
    def preset_prompt(self) -> str:
        return (
            "请审核这份合同/协议文书,关注以下维度:"
            "权责对等(单方面义务、责任不对等)、"
            "付款与违约(付款条件模糊、违约金畸高或缺失)、"
            "知识产权归属(成果归属、署名权、竞业限制)、"
            "常见霸王条款(任意解除权、单方变更、免责条款),"
            "逐条定位到具体条款,说明风险后果并给出修改建议。"
        )

    @property
    def recommended_skills(self) -> list[str]:
        return []  # 无推荐 → 前端全选(等同于不限制)


register_scenario(DocumentReviewTemplate())
