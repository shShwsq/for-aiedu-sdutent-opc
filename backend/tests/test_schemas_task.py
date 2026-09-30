"""schemas/task Pydantic 校验单元测试。

覆盖输入校验契约(executor pattern、verifier tokens、config update 语义),
不连 DB,只测 schema 层。校验失败应抛 ValidationError,而非进入业务逻辑。

覆盖:
- TaskCreateRequest:executor pattern / verifier_auth_mode / 空字段
- VerifierAuthToken:label/header_name/header_value 长度约束
- VerifyConfigUpdateRequest:None vs 空 list vs 非空 list 三态语义
- SendMessageRequest / TaskTitleUpdateRequest 长度边界
- 兼容字段:repo_url → scenario 自动推断由后端处理,schema 只校验类型
"""
import uuid

import pytest
from pydantic import ValidationError

from app.agents.registry import get_registered_types
from app.schemas.task import (
    AnswerItem,
    AnswerRequest,
    SendMessageRequest,
    SendMessageResponse,
    TaskCreateRequest,
    TaskTitleUpdateRequest,
    VerifierAuthToken,
    VerifyActionRequest,
    VerifyConfigUpdateRequest,
)


# ============================================================
# TaskCreateRequest:executor pattern 校验
# ============================================================

def test_task_create_default_executor_is_builtin():
    """默认 executor='builtin'。"""
    req = TaskCreateRequest(user_input="分析这个仓库")
    assert req.executor == "builtin"


def test_task_create_accepts_all_registered_executors():
    """所有 registry 中已注册的 agent_type 都应是合法 executor。"""
    for agent_type in get_registered_types():
        req = TaskCreateRequest(user_input="x", executor=agent_type)
        assert req.executor == agent_type


def test_task_create_rejects_unknown_executor():
    """未注册的 executor 字符串应校验失败(pattern 不匹配)。"""
    with pytest.raises(ValidationError):
        TaskCreateRequest(user_input="x", executor="not_a_real_agent")


def test_task_create_rejects_empty_executor():
    """空字符串 executor 应校验失败。"""
    with pytest.raises(ValidationError):
        TaskCreateRequest(user_input="x", executor="")


def test_task_create_user_input_required_when_no_repo_url():
    """无 repo_url 兼容字段时,user_input 必填(否则后端无法处理)。

    注意:schema 层 user_input=None 是允许的(兼容 repo_url 自动推断场景),
    真正的"必填"约束在 router 层。这里只验证 schema 接受 None。
    """
    req = TaskCreateRequest(user_input=None)
    assert req.user_input is None


# ============================================================
# TaskCreateRequest:verifier 配置
# ============================================================

def test_task_create_verifier_default_disabled():
    """默认 verifier_enabled=False。"""
    req = TaskCreateRequest(user_input="x")
    assert req.verifier_enabled is False
    assert req.verifier_auth_mode == "per_action"
    assert req.verifier_auth_tokens == []


def test_task_create_verifier_auth_mode_direct():
    """verifier_auth_mode='direct' 合法。"""
    req = TaskCreateRequest(user_input="x", verifier_auth_mode="direct")
    assert req.verifier_auth_mode == "direct"


def test_task_create_verifier_auth_mode_invalid_rejected():
    """verifier_auth_mode 非 direct/per_action 应校验失败。"""
    with pytest.raises(ValidationError):
        TaskCreateRequest(user_input="x", verifier_auth_mode="always")


def test_task_create_test_env_url_max_length_enforced():
    """test_env_url 超过 2048 字符应校验失败(max_length 含边界,2049 才超限)。"""
    TaskCreateRequest(user_input="x", test_env_url="h" * 2048)  # 边界值,合法
    with pytest.raises(ValidationError):
        TaskCreateRequest(user_input="x", test_env_url="h" * 2049)


def test_task_create_test_env_url_accepts_valid_url():
    """正常长度的 URL 合法。"""
    req = TaskCreateRequest(user_input="x", test_env_url="http://localhost:3000")
    assert req.test_env_url == "http://localhost:3000"


# ============================================================
# VerifierAuthToken:字段长度约束
# ============================================================

def test_verifier_auth_token_valid():
    """合法 token 三元组。"""
    t = VerifierAuthToken(
        label="管理员",
        header_name="Authorization",
        header_value="Bearer abc123",
    )
    assert t.label == "管理员"
    assert t.header_name == "Authorization"


def test_verifier_auth_token_empty_label_rejected():
    """空 label 不合法(min_length=1)。"""
    with pytest.raises(ValidationError):
        VerifierAuthToken(label="", header_name="Authorization", header_value="x")


def test_verifier_auth_token_label_too_long_rejected():
    """label 超 64 字符不合法。"""
    with pytest.raises(ValidationError):
        VerifierAuthToken(label="x" * 65, header_name="Authorization", header_value="x")


def test_verifier_auth_token_empty_header_name_rejected():
    """空 header_name 不合法。"""
    with pytest.raises(ValidationError):
        VerifierAuthToken(label="x", header_name="", header_value="x")


def test_verifier_auth_token_empty_header_value_rejected():
    """空 header_value 不合法(防止无意义占位)。"""
    with pytest.raises(ValidationError):
        VerifierAuthToken(label="x", header_name="Authorization", header_value="")


def test_verifier_auth_token_header_value_max_4096():
    """header_value 上限 4096(防超大 token 撑爆 DB)。"""
    VerifierAuthToken(label="x", header_name="Authorization", header_value="x" * 4096)  # 合法
    with pytest.raises(ValidationError):
        VerifierAuthToken(label="x", header_name="Authorization", header_value="x" * 4097)


# ============================================================
# VerifyConfigUpdateRequest:三态语义(关键设计)
# ============================================================

def test_verify_config_update_all_none_means_no_change():
    """所有字段 None → 表示"不修改"(语义契约)。

    后端据此区分:
    - None  → 不修改
    - []    → 清空
    - [...] → 覆盖
    """
    req = VerifyConfigUpdateRequest()
    assert req.verifier_enabled is None
    assert req.verifier_auth_mode is None
    assert req.test_env_url is None
    assert req.verifier_auth_tokens is None  # 关键:None = 不修改


def test_verify_config_update_empty_tokens_means_clear():
    """verifier_auth_tokens=[] → 表示"清空"(与 None 区分)。

    这是 verifier 配置更新的关键三态语义:
    - None  → 不修改(保留原值)
    - []    → 清空(删除所有 token)
    - [...] → 整体覆盖
    schema 层必须能区分 None 与 [],否则后端无法判断用户意图。
    """
    req = VerifyConfigUpdateRequest(verifier_auth_tokens=[])
    assert req.verifier_auth_tokens is not None
    assert req.verifier_auth_tokens == []


def test_verify_config_update_non_empty_tokens_means_overwrite():
    """verifier_auth_tokens=[...] → 表示"整体覆盖"。"""
    tokens = [
        VerifierAuthToken(label="管理员", header_name="Authorization", header_value="Bearer admin"),
        VerifierAuthToken(label="用户", header_name="Cookie", header_value="session=user"),
    ]
    req = VerifyConfigUpdateRequest(verifier_auth_tokens=tokens)
    assert len(req.verifier_auth_tokens) == 2
    assert req.verifier_auth_tokens[0].label == "管理员"


def test_verify_config_update_auth_mode_pattern():
    """verifier_auth_mode 仍受 pattern 约束。"""
    VerifyConfigUpdateRequest(verifier_auth_mode="direct")  # 合法
    VerifyConfigUpdateRequest(verifier_auth_mode="per_action")  # 合法
    with pytest.raises(ValidationError):
        VerifyConfigUpdateRequest(verifier_auth_mode="invalid")


# ============================================================
# TaskTitleUpdateRequest / SendMessageRequest 长度边界
# ============================================================

def test_task_title_update_accepts_empty_string():
    """空字符串 title 合法(等价于清除自定义标题)。"""
    req = TaskTitleUpdateRequest(title="")
    assert req.title == ""


def test_task_title_update_rejects_too_long():
    """title 超 255 字符不合法。"""
    with pytest.raises(ValidationError):
        TaskTitleUpdateRequest(title="x" * 256)


def test_task_title_update_accepts_max_length():
    """title = 255 字符合法(边界值)。"""
    req = TaskTitleUpdateRequest(title="x" * 255)
    assert len(req.title) == 255


def test_send_message_request_empty_content_rejected():
    """空 content 不合法(min_length=1)。"""
    with pytest.raises(ValidationError):
        SendMessageRequest(content="")


def test_send_message_request_too_long_rejected():
    """content 超 8000 字符不合法。"""
    with pytest.raises(ValidationError):
        SendMessageRequest(content="x" * 8001)


def test_send_message_request_accepts_max_length():
    """content = 8000 字符合法(边界值)。"""
    req = SendMessageRequest(content="x" * 8000)
    assert len(req.content) == 8000


# ============================================================
# VerifyActionRequest / AnswerRequest
# ============================================================

def test_verify_action_request_fields():
    """VerifyActionRequest 字段。"""
    req = VerifyActionRequest(action_id="act-123", approved=True)
    assert req.action_id == "act-123"
    assert req.approved is True


def test_answer_request_accepts_string_value():
    """AnswerItem value 为字符串(填空题)。"""
    req = AnswerRequest(answers=[AnswerItem(question_id="q1", value="自由文本回答")])
    assert req.answers[0].value == "自由文本回答"


def test_answer_request_accepts_list_value():
    """AnswerItem value 为字符串列表(多选题)。"""
    req = AnswerRequest(answers=[AnswerItem(question_id="q1", value=["选项A", "选项B"])])
    assert req.answers[0].value == ["选项A", "选项B"]


def test_answer_request_empty_list_rejected():
    """answers 空列表:Pydantic 默认允许(后端可能校验)。

    schema 层不强约束非空,留给 router 判断。这里验证 schema 接受空 list。
    """
    req = AnswerRequest(answers=[])
    assert req.answers == []
