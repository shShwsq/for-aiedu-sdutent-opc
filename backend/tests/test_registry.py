"""agents/registry 单元测试:agent 类型注册表查询与完整性校验。

registry 是外部 CLI agent 的元数据中心,后端 API/executor/前端表单均据此动态生成。
注册表结构损坏(缺字段/字段类型错)会导致 API 500、executor 找不到实现、前端表单渲染失败。

覆盖:
- AGENT_REGISTRY 完整性:每个 agent 必含必需字段
- get_registered_types / is_registered / get_agent_meta
- get_executor_location:返回 (module, func)
- get_credential_fields:字段结构合法
- get_sandbox_config:含 bin/install_cmd/acp_args/credential_env
- 凭证字段类型常量(secret/text/select)合法
"""
import importlib

import pytest

from app.agents.registry import (
    AGENT_REGISTRY,
    CREDENTIAL_FIELD_SECRET,
    CREDENTIAL_FIELD_SELECT,
    CREDENTIAL_FIELD_TEXT,
    get_agent_meta,
    get_credential_fields,
    get_executor_location,
    get_registered_types,
    get_sandbox_config,
    is_registered,
)


# ============================================================
# AGENT_REGISTRY 结构完整性
# ============================================================

# 每个注册项必须包含的顶层字段
_REQUIRED_TOP_KEYS = {
    "display_name",
    "description",
    "credential_fields",
    "sandbox",
    "executor_module",
    "executor_func",
}

# sandbox 子字典必须包含的字段
_REQUIRED_SANDBOX_KEYS = {
    "bin_config_key",
    "bin_default",
    "install_cmd_config_key",
    "install_cmd_default",
    "acp_args",
    "credential_env",
}

# 凭证字段定义必须包含的子字段
_REQUIRED_CRED_FIELD_KEYS = {"key", "label", "type", "required"}

_VALID_CRED_TYPES = {CREDENTIAL_FIELD_SECRET, CREDENTIAL_FIELD_TEXT, CREDENTIAL_FIELD_SELECT}


def test_registry_is_non_empty():
    """注册表不应为空(至少有一个 agent)。"""
    assert len(AGENT_REGISTRY) >= 1


def test_registry_keys_are_strings():
    """所有 agent_type key 应为字符串。"""
    for key in AGENT_REGISTRY:
        assert isinstance(key, str), f"agent_type 不是字符串: {key}"
        assert key, f"agent_type 是空字符串"


def test_every_agent_has_required_top_level_fields():
    """每个 agent 元数据包含所有必需顶层字段。"""
    for agent_type, meta in AGENT_REGISTRY.items():
        missing = _REQUIRED_TOP_KEYS - set(meta.keys())
        assert not missing, f"{agent_type} 缺少顶层字段: {missing}"


def test_every_agent_display_name_non_empty():
    """display_name 非空字符串。"""
    for agent_type, meta in AGENT_REGISTRY.items():
        assert meta["display_name"], f"{agent_type} display_name 为空"


def test_every_agent_executor_module_importable():
    """executor_module 应是可 import 的模块路径(语法层面)。

    不实际 import(避免触发重依赖),只验证字符串格式合法。
    """
    for agent_type, meta in AGENT_REGISTRY.items():
        mod = meta["executor_module"]
        assert isinstance(mod, str) and mod, f"{agent_type} executor_module 非法"
        # 应是 dotted path(至少含一个 .)
        assert "." in mod, f"{agent_type} executor_module 不是 dotted path: {mod}"


def test_every_agent_executor_func_non_empty():
    """executor_func 非空字符串。"""
    for agent_type, meta in AGENT_REGISTRY.items():
        func = meta["executor_func"]
        assert isinstance(func, str) and func, f"{agent_type} executor_func 为空"


def test_every_agent_has_at_least_one_credential_field():
    """每个 agent 至少有一个凭证字段(否则无法认证)。"""
    for agent_type, meta in AGENT_REGISTRY.items():
        fields = meta["credential_fields"]
        assert isinstance(fields, list) and len(fields) >= 1, (
            f"{agent_type} 无凭证字段"
        )


def test_every_credential_field_has_required_keys():
    """每个凭证字段定义包含 key/label/type/required。"""
    for agent_type, meta in AGENT_REGISTRY.items():
        for i, field in enumerate(meta["credential_fields"]):
            missing = _REQUIRED_CRED_FIELD_KEYS - set(field.keys())
            assert not missing, f"{agent_type} 凭证字段[{i}] 缺少: {missing}"


def test_every_credential_field_type_is_valid():
    """每个凭证字段 type 必须是 secret/text/select 三者之一。"""
    for agent_type, meta in AGENT_REGISTRY.items():
        for i, field in enumerate(meta["credential_fields"]):
            assert field["type"] in _VALID_CRED_TYPES, (
                f"{agent_type} 凭证字段[{i}] type 非法: {field['type']}"
            )


def test_credential_field_keys_unique_per_agent():
    """同一 agent 的凭证字段 key 不应重复(避免覆盖)。"""
    for agent_type, meta in AGENT_REGISTRY.items():
        keys = [f["key"] for f in meta["credential_fields"]]
        assert len(keys) == len(set(keys)), f"{agent_type} 凭证字段 key 重复: {keys}"


def test_select_fields_have_options():
    """type=select 的字段必须提供 options 列表。"""
    for agent_type, meta in AGENT_REGISTRY.items():
        for field in meta["credential_fields"]:
            if field["type"] == CREDENTIAL_FIELD_SELECT:
                opts = field.get("options")
                assert isinstance(opts, list) and len(opts) >= 1, (
                    f"{agent_type} select 字段 {field['key']} 无 options"
                )
                for opt in opts:
                    assert "value" in opt and "label" in opt, (
                        f"{agent_type} select 字段 {field['key']} option 缺 value/label"
                    )


def test_every_agent_sandbox_has_required_keys():
    """每个 sandbox 配置包含必需字段。"""
    for agent_type, meta in AGENT_REGISTRY.items():
        sb = meta["sandbox"]
        assert isinstance(sb, dict), f"{agent_type} sandbox 不是 dict"
        missing = _REQUIRED_SANDBOX_KEYS - set(sb.keys())
        assert not missing, f"{agent_type} sandbox 缺少: {missing}"


def test_every_sandbox_acp_args_is_list():
    """acp_args 必须是 list(传给 subprocess 的参数列表)。"""
    for agent_type, meta in AGENT_REGISTRY.items():
        args = meta["sandbox"]["acp_args"]
        assert isinstance(args, list), f"{agent_type} acp_args 不是 list"
        # 每个元素应是字符串
        for a in args:
            assert isinstance(a, str), f"{agent_type} acp_args 元素非字符串: {a}"


def test_every_sandbox_credential_env_is_dict():
    """credential_env 必须是 dict(凭证字段 → 环境变量名映射)。"""
    for agent_type, meta in AGENT_REGISTRY.items():
        env = meta["sandbox"]["credential_env"]
        assert isinstance(env, dict), f"{agent_type} credential_env 不是 dict"


def test_credential_env_keys_match_credential_fields():
    """credential_env 的 key 应是 credential_fields 的子集。

    每个 env 映射应对应一个凭证字段的 key(否则映射无意义)。
    反向不要求:某些 agent 动态构建 env,credential_env 可为空 dict 或子集。
    """
    for agent_type, meta in AGENT_REGISTRY.items():
        cred_keys = {f["key"] for f in meta["credential_fields"]}
        env_keys = set(meta["sandbox"]["credential_env"].keys())
        # env_keys 应是 cred_keys 的子集
        extra = env_keys - cred_keys
        assert not extra, (
            f"{agent_type} credential_env 引用了不存在的凭证字段: {extra}"
        )


# ============================================================
# 查询辅助函数
# ============================================================

def test_get_registered_types_returns_all_keys():
    """get_registered_types 返回所有 agent_type。"""
    types = get_registered_types()
    assert set(types) == set(AGENT_REGISTRY.keys())


def test_is_registered_true_for_existing():
    """已注册的 agent_type → True。"""
    for agent_type in AGENT_REGISTRY:
        assert is_registered(agent_type) is True


def test_is_registered_false_for_unknown():
    """未注册的 → False。"""
    assert is_registered("nonexistent_agent") is False
    assert is_registered("") is False


def test_get_agent_meta_returns_meta_for_existing():
    """已注册 → 返回元数据 dict。"""
    for agent_type in AGENT_REGISTRY:
        meta = get_agent_meta(agent_type)
        assert meta is not None
        assert meta is AGENT_REGISTRY[agent_type]  # 同一对象


def test_get_agent_meta_returns_none_for_unknown():
    """未注册 → None。"""
    assert get_agent_meta("nonexistent") is None


def test_get_executor_location_returns_tuple():
    """已注册 → (module, func) 元组。"""
    for agent_type in AGENT_REGISTRY:
        loc = get_executor_location(agent_type)
        assert loc is not None
        assert isinstance(loc, tuple)
        assert len(loc) == 2
        module, func = loc
        assert module and func


def test_get_executor_location_none_for_unknown():
    """未注册 → None。"""
    assert get_executor_location("nonexistent") is None


def test_get_credential_fields_returns_list():
    """已注册 → 返回凭证字段列表。"""
    for agent_type in AGENT_REGISTRY:
        fields = get_credential_fields(agent_type)
        assert isinstance(fields, list)
        assert len(fields) >= 1


def test_get_credential_fields_empty_for_unknown():
    """未注册 → 空列表(不抛异常)。"""
    assert get_credential_fields("nonexistent") == []


def test_get_sandbox_config_returns_dict():
    """已注册 → 返回 sandbox 配置 dict。"""
    for agent_type in AGENT_REGISTRY:
        sb = get_sandbox_config(agent_type)
        assert sb is not None
        assert isinstance(sb, dict)


def test_get_sandbox_config_none_for_unknown():
    """未注册 → None。"""
    assert get_sandbox_config("nonexistent") is None


# ============================================================
# 已知 agent 类型存在性(防误删注册项)
# ============================================================

@pytest.mark.parametrize("agent_type", [
    "qoder_cli",
    "deepseek_cli",
    "codex_cli",
])
def test_known_agent_types_registered(agent_type):
    """关键 agent 类型必须注册(防误删或重命名导致功能消失)。"""
    assert is_registered(agent_type), f"关键 agent 类型 {agent_type} 未注册"


def test_retired_agent_types_not_registered():
    """已下线的 agent 类型必须不再注册(防止残留引用复活)。"""
    for agent_type in ("kimi_cli", "qoder_cli_cn", "hermes_cli"):
        assert not is_registered(agent_type), f"已下线 agent 类型 {agent_type} 仍在注册表"


# ============================================================
# executor_module 可 import 性(轻量验证)
# ============================================================

def test_all_executor_modules_importable():
    """所有 executor_module 应可被 Python import(模块存在)。

    不调用 executor_func(避免触发 LLM/沙箱依赖),只验证模块路径有效。
    """
    for agent_type, meta in AGENT_REGISTRY.items():
        mod_path = meta["executor_module"]
        try:
            importlib.import_module(mod_path)
        except ImportError as e:
            pytest.fail(f"{agent_type} executor_module {mod_path} 不可 import: {e}")
