"""模型输出上限(max_tokens 钳制)测试

覆盖:
- resolve_output_limit 优先级:用户显式 > 模型级 > provider 级 > 系统默认
- LLMClient 构造时按 catalog 解析上限,chat_stream 钳制超限请求
- 路由 merge/序列化透传用户自定义的 max_output_tokens
"""
from unittest.mock import MagicMock

from app.llm.client import DEFAULT_MAX_OUTPUT_TOKENS, LLMClient, resolve_output_limit
from app.routers.model_configs import _merge_llm_configs, _to_llm_out


# ============================================================
# resolve_output_limit 优先级
# ============================================================

def test_explicit_limit_wins():
    """用户显式设置优先于 catalog。"""
    assert resolve_output_limit(
        {"fallbackOutputLimit": 8192}, {"outputLimit": 4096}, 32768
    ) == 32768


def test_model_meta_limit():
    assert resolve_output_limit({}, {"outputLimit": 8192}) == 8192


def test_provider_fallback_limit():
    """模型不在 catalog(豆包 Endpoint ID)→ provider 级兜底。"""
    assert resolve_output_limit({"fallbackOutputLimit": 4096}, None) == 4096
    assert resolve_output_limit({"fallbackOutputLimit": 4096}, {"id": "ep-xxx"}) == 4096


def test_default_limit_is_16384():
    assert resolve_output_limit({}, None) == DEFAULT_MAX_OUTPUT_TOKENS
    assert DEFAULT_MAX_OUTPUT_TOKENS == 16384


def test_invalid_limits_ignored():
    """0/负数等非法值不参与解析,走下一级。"""
    assert resolve_output_limit({}, {"outputLimit": 0}) == DEFAULT_MAX_OUTPUT_TOKENS
    assert resolve_output_limit({}, None, -5) == DEFAULT_MAX_OUTPUT_TOKENS
    assert resolve_output_limit({}, None, 0) == DEFAULT_MAX_OUTPUT_TOKENS


# ============================================================
# LLMClient:构造解析 + chat_stream 钳制
# ============================================================

def _mk_client(model="deepseek-v4-pro", max_output_tokens=None):
    return LLMClient(
        provider_id="deepseek",
        api_key="sk-test",
        model=model,
        max_output_tokens=max_output_tokens,
    )


def test_client_resolves_catalog_limit():
    """catalog 中 deepseek 模型 outputLimit=8192 → 客户端按此解析。"""
    assert _mk_client().max_output_tokens == 8192


def test_client_unknown_model_uses_default():
    """模型不在 catalog → 系统默认 16384。"""
    assert _mk_client(model="some-custom-model").max_output_tokens == 16384


def test_client_explicit_limit_overrides_catalog():
    assert _mk_client(max_output_tokens=32768).max_output_tokens == 32768


def test_from_config_dict_passes_limit():
    client = LLMClient.from_config_dict({
        "provider": "deepseek",
        "api_key": "sk-test",
        "model": "deepseek-v4-pro",
        "max_output_tokens": 20000,
    })
    assert client.max_output_tokens == 20000


def test_from_config_dict_without_limit_uses_catalog():
    client = LLMClient.from_config_dict({
        "provider": "deepseek",
        "api_key": "sk-test",
        "model": "deepseek-v4-pro",
    })
    assert client.max_output_tokens == 8192


def test_chat_stream_clamps_excessive_max_tokens():
    """请求超过模型输出上限 → 钳制到上限,避免厂商 400。"""
    client = _mk_client()  # 上限 8192
    captured = {}

    fake_completions = MagicMock()

    def fake_create(**kwargs):
        captured.update(kwargs)
        return iter([])  # 空流,直接结束

    fake_completions.create = fake_create
    client.client.chat.completions = fake_completions

    list(client.chat_stream(
        [{"role": "user", "content": "hi"}], max_tokens=16384
    ))
    assert captured["max_tokens"] == 8192


def test_chat_stream_keeps_smaller_max_tokens():
    """请求未超上限 → 原样传递。"""
    client = _mk_client()
    captured = {}

    fake_completions = MagicMock()

    def fake_create(**kwargs):
        captured.update(kwargs)
        return iter([])

    fake_completions.create = fake_create
    client.client.chat.completions = fake_completions

    list(client.chat_stream(
        [{"role": "user", "content": "hi"}], max_tokens=1024
    ))
    assert captured["max_tokens"] == 1024


# ============================================================
# 路由:merge/序列化透传 max_output_tokens
# ============================================================

def test_merge_llm_configs_keeps_limit():
    items = [MagicMock(
        id="c1", name="测试", provider="deepseek", api_key="sk-x",
        model="deepseek-v4-pro", enable_thinking=True,
        base_url=None, max_output_tokens=20000,
    )]
    merged = _merge_llm_configs([], items)
    assert merged[0]["max_output_tokens"] == 20000


def test_to_llm_out_returns_limit():
    out = _to_llm_out({
        "id": "c1", "name": "测试", "provider": "deepseek",
        "model": "deepseek-v4-pro", "api_key": "sk-x",
        "max_output_tokens": 20000,
    })
    assert out.max_output_tokens == 20000

    out2 = _to_llm_out({"id": "c2", "provider": "deepseek", "model": "m"})
    assert out2.max_output_tokens is None
