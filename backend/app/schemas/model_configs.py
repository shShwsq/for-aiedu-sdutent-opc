"""模型配置相关的 Pydantic 模型(列表式)

用户可配置多个 LLM / Embedding 模型,每个配置有唯一 id 和自定义名称。
任务提交时选择一个 llm_config_id,orchestrator 按此 id 加载对应配置。

安全约定:
- 响应(GET)绝不回传 api_key 原文,只返回 has_api_key 布尔,避免凭据经 HTTP 泄露
- 请求(PUT)中 api_key 为空字符串表示"保留已存的 key",非空表示"更新为新 key"
- 测试端点(POST /test)按 config_id 使用已保存的配置,不接收前端传来的明文 key
"""
from pydantic import BaseModel, Field


# ============================================================
# 配置写入(请求体)——单个配置项
# ============================================================


class LLMConfigItem(BaseModel):
    """LLM 配置项(列表中的一个元素)

    api_key 约定:
    - 首次保存:必填,传完整 key
    - 后续更新:传空串 "" 表示保留已存的 key;传非空串表示更新为新 key
    """

    # 前端生成的唯一 id(uuid),用于任务提交时引用
    id: str
    # 用户自定义别名,如 "DeepSeek-V3 日常"、"Qwen-QwQ 深度"
    name: str = ""
    provider: str
    api_key: str = ""
    model: str
    enable_thinking: bool = True
    # 可选:自定义 baseUrl 覆盖 catalog 中的预设(留空则用 catalog 的 baseUrl)
    base_url: str | None = None
    # 可选:单次输出上限(max_tokens 钳制值)。留空则按 catalog
    # 模型级 outputLimit > provider 级 fallback > 系统默认 16384 解析
    max_output_tokens: int | None = Field(default=None, ge=1, le=1000000)


class EmbeddingConfigItem(BaseModel):
    """Embedding 配置项(api_key 约定同 LLMConfigItem)"""

    id: str
    name: str = ""
    provider: str
    api_key: str = ""
    model: str
    base_url: str | None = None
    dimension: int = 1024


class SaveModelsRequest(BaseModel):
    """保存模型配置请求(整体替换列表)

    - 传 llm_configs: 整体替换 LLM 配置列表
    - 传 embedding_configs: 整体替换 Embedding 配置列表
    - 未传的一侧保持不变
    """

    llm_configs: list[LLMConfigItem] | None = None
    embedding_configs: list[EmbeddingConfigItem] | None = None


# ============================================================
# 配置读取(响应体)——单个配置项(不含 api_key 原文)
# ============================================================


class LLMConfigItemOut(BaseModel):
    """LLM 配置项读取(不含 api_key 原文)"""

    id: str
    name: str = ""
    provider: str
    model: str
    enable_thinking: bool = True
    base_url: str | None = None
    # 单次输出上限(None = 未显式设置,按 catalog/系统默认解析)
    max_output_tokens: int | None = None
    has_api_key: bool = False


class EmbeddingConfigItemOut(BaseModel):
    """Embedding 配置项读取(不含 api_key 原文)"""

    id: str
    name: str = ""
    provider: str
    model: str
    base_url: str | None = None
    dimension: int = 1024
    has_api_key: bool = False


class UserModelsResponse(BaseModel):
    """用户已保存的模型配置列表"""

    llm_configs: list[LLMConfigItemOut] = Field(default_factory=list)
    embedding_configs: list[EmbeddingConfigItemOut] = Field(default_factory=list)


# ============================================================
# 测试连通性请求与响应
# ============================================================


class TestRequest(BaseModel):
    """测试连通性请求(指定要测试的配置 id)"""

    config_id: str


class TestResponse(BaseModel):
    """测试 LLM / Embedding 连通性的响应"""

    success: bool
    message: str
    # 耗时(毫秒)
    latency_ms: int | None = None
    # 仅 embedding 测试返回:实际向量维度
    dimension: int | None = None
    # 仅 LLM 测试返回:模型的实际回复文本(供前端展示)
    reply: str | None = None
