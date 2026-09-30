"""题目生成(generator)解析与规范化单元测试

覆盖(不依赖数据库,直接喂 LLM 原始输出字符串):
- 合法 JSON 数组解析 / 单对象包裹 / markdown 围栏容错(json_repair)
- 字段校验:qtype 白名单、选项数、answer_idx 越界、全同选项
- true_false 强制选项 ["正确","错误"]
- CWE 元信息优先于 LLM 输出的 knowledge_key(含纯数字补前缀)
- source_file/source_lines 源码定位字段解析(含超长截断)
- 单条 finding 最多 3 题截断
- dedup_hash 稳定性
- 主题提示词切换(build_system_prompt)与工具说明段注入
- 提示词强制读码要求(禁止常识题 / 必须给源码定位)
- 泛化出题与改编题(origin/languages)指引及字段解析
- 知识点语言标签并集累积
- 质量关卡过滤(工作区可用时无 code_snippet 的题被丢弃 + 反馈重试)
- 致命错误快速失败(401/403 额度类错误立即中止并冒泡友好原因)
- 迷你工具循环(_call_llm / _execute_practice_tool)
- 出题前工作区保障(_ensure_workspace 重新 clone 恢复)
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import openai
import pytest

import app.models.task_artifact  # noqa: F401  让 Task mapper 能解析 TaskArtifact 关联
import app.services.practice.generator as gen
from app.services.practice.generator import (
    _MAX_TOOL_RESULT_CHARS,
    _apply_thinking_mode,
    _call_llm,
    _ensure_workspace,
    _execute_practice_tool,
    _fatal_llm_reason,
    _normalize_raw_question,
    _parse_llm_questions,
    build_system_prompt,
    compute_dedup_hash,
)

VALID_Q = {
    "qtype": "single_choice",
    "stem": "该代码存在哪种漏洞?",
    "code_snippet": "cursor.execute(sql)",
    "options": ["SQL 注入", "XSS", "CSRF", "无漏洞"],
    "answer_idx": 0,
    "explanation": "拼接 SQL 导致注入",
    "difficulty": 3,
    "knowledge_key": "CWE-89",
    "knowledge_name": "SQL 注入",
}


def _raw(**overrides):
    q = dict(VALID_Q)
    q.update(overrides)
    return q


# ============================================================
# 解析(json_repair 容错)
# ============================================================


def test_parse_json_array():
    import json

    content = json.dumps([_raw()], ensure_ascii=False)
    qs = _parse_llm_questions(content, {})
    assert len(qs) == 1
    assert qs[0]["qtype"] == "single_choice"
    assert qs[0]["answer_idx"] == 0


def test_parse_single_object_wrapped():
    import json

    content = json.dumps(_raw(), ensure_ascii=False)
    qs = _parse_llm_questions(content, {})
    assert len(qs) == 1


def test_parse_markdown_fence_tolerated():
    import json

    content = "```json\n" + json.dumps([_raw()], ensure_ascii=False) + "\n```"
    qs = _parse_llm_questions(content, {})
    assert len(qs) == 1


def test_parse_empty_or_garbage():
    assert _parse_llm_questions("", {}) == []
    assert _parse_llm_questions("完全不是 JSON 的散文", {}) == []


def test_parse_max_3_questions():
    import json

    content = json.dumps([_raw(stem=f"题干{i}") for i in range(5)], ensure_ascii=False)
    qs = _parse_llm_questions(content, {})
    assert len(qs) == 3


# ============================================================
# 字段校验
# ============================================================


def test_invalid_qtype_rejected():
    assert _normalize_raw_question(_raw(qtype="essay"), {}) is None
    assert _normalize_raw_question(_raw(qtype=""), {}) is None


def test_empty_stem_rejected():
    assert _normalize_raw_question(_raw(stem="  "), {}) is None


def test_answer_idx_out_of_range():
    assert _normalize_raw_question(_raw(answer_idx=9), {}) is None
    assert _normalize_raw_question(_raw(answer_idx=-1), {}) is None
    # answer_idx 非整数
    assert _normalize_raw_question(_raw(answer_idx="x"), {}) is None


def test_too_few_or_many_options():
    assert _normalize_raw_question(_raw(options=["A"]), {}) is None
    assert _normalize_raw_question(_raw(options=[str(i) for i in range(9)]), {}) is None


def test_duplicate_options_rejected():
    # 全同选项无效;含重复但非全同时保留(去重不是解析层职责)
    assert _normalize_raw_question(_raw(options=["A", "A"]), {}) is None
    assert _normalize_raw_question(_raw(options=["A", "A", "B"]), {}) is not None


def test_true_false_options_forced():
    q = _normalize_raw_question(_raw(
        qtype="true_false",
        stem="参数化查询可防御 SQL 注入",
        options=["对", "错", "不确定"],
        answer_idx=1,
    ), {})
    assert q is not None
    assert q["options"] == ["正确", "错误"]
    assert q["answer_idx"] == 1  # 索引保留(1 仍在新选项范围内)


def test_difficulty_clamped_and_default():
    assert _normalize_raw_question(_raw(difficulty=9), {})["difficulty"] == 5.0
    assert _normalize_raw_question(_raw(difficulty=0), {})["difficulty"] == 1.0
    assert _normalize_raw_question(_raw(difficulty="abc"), {})["difficulty"] == 3.0


# ============================================================
# knowledge_key:CWE 元信息优先
# ============================================================


def test_cwe_from_finding_meta_wins():
    q = _normalize_raw_question(_raw(knowledge_key="injection"), {"cwe": "CWE-79"})
    assert q["knowledge_key"] == "CWE-79"


def test_numeric_cwe_prefixed():
    q = _normalize_raw_question(_raw(), {"cwe": "89"})
    assert q["knowledge_key"] == "CWE-89"


def test_fallback_to_llm_key_then_general():
    assert _normalize_raw_question(_raw(), {})["knowledge_key"] == "CWE-89"  # LLM 输出
    q = _normalize_raw_question(_raw(knowledge_key="  "), {})
    assert q["knowledge_key"] == "general"


# ============================================================
# source_file / source_lines 源码定位字段
# ============================================================


def test_source_fields_parsed():
    q = _normalize_raw_question(
        _raw(source_file="src/db.py", source_lines="120-150"), {},
    )
    assert q["source_file"] == "src/db.py"
    assert q["source_lines"] == "120-150"


def test_source_fields_missing_default_none():
    q = _normalize_raw_question(_raw(), {})
    assert q["source_file"] is None
    assert q["source_lines"] is None


def test_source_fields_truncated():
    q = _normalize_raw_question(
        _raw(source_file="x" * 600, source_lines="y" * 60), {},
    )
    assert len(q["source_file"]) == 512
    assert len(q["source_lines"]) == 32


# ============================================================
# origin(真实代码题/改编题)与 languages(语言标签)解析
# ============================================================


def test_origin_parsed_and_fallback():
    assert _normalize_raw_question(_raw(origin="synthetic"), {})["origin"] == "synthetic"
    assert _normalize_raw_question(_raw(origin="repo"), {})["origin"] == "repo"
    # 非法/缺失回退 repo
    assert _normalize_raw_question(_raw(origin="weird"), {})["origin"] == "repo"
    assert _normalize_raw_question(_raw(), {})["origin"] == "repo"


def test_languages_normalized():
    q = _normalize_raw_question(
        _raw(languages=[" Python ", "SQL", "python", ""]), {},
    )
    assert q["languages"] == ["python", "sql"]
    # 最多 5 个,单项截断 24 字符
    q = _normalize_raw_question(_raw(languages=[f"lang{i}" for i in range(8)]), {})
    assert len(q["languages"]) == 5
    q = _normalize_raw_question(_raw(languages=["x" * 40]), {})
    assert q["languages"] == ["x" * 24]


def test_languages_inferred_from_source_file_ext():
    # LLM 未给时从 source_file 扩展名推断
    q = _normalize_raw_question(_raw(source_file="src/db.py"), {})
    assert q["languages"] == ["python"]
    # LLM 已给时不覆盖
    q = _normalize_raw_question(_raw(source_file="src/db.py", languages=["java"]), {})
    assert q["languages"] == ["java"]
    # 无扩展名/未知扩展名 → 空列表
    assert _normalize_raw_question(_raw(source_file="README"), {})["languages"] == []
    assert _normalize_raw_question(_raw(source_file="a.unknownext"), {})["languages"] == []


# ============================================================
# 知识点语言标签并集累积
# ============================================================


def test_kp_languages_union_merge_on_existing():
    existing = SimpleNamespace(languages=["python"])
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = existing
    kp = gen._get_or_create_knowledge_point(
        db, "u1", "CWE-89", "SQL 注入", languages=["python", "sql"],
    )
    assert kp is existing
    assert kp.languages == ["python", "sql"]
    # 无新语言时不重复追加
    gen._get_or_create_knowledge_point(
        db, "u1", "CWE-89", "SQL 注入", languages=["python"],
    )
    assert existing.languages == ["python", "sql"]


def test_origin_and_kp_languages_persisted_in_pipeline(monkeypatch):
    """完整管线:改编题 origin 与知识点语言标签落库"""
    import json

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    synthetic = _raw(stem="改编题", origin="synthetic", languages=["java"])
    monkeypatch.setattr(
        gen, "_call_llm",
        lambda *a, **k: json.dumps([synthetic], ensure_ascii=False),
    )
    db = _gen_db()
    created, skipped = gen.generate_questions_for_task(
        db, _gen_task(), "u1", client=MagicMock(),
    )
    assert len(created) == 1 and skipped == 0
    assert created[0].origin == "synthetic"
    kps_added = [
        c.args[0] for c in db.add.call_args_list
        if isinstance(c.args[0], gen.KnowledgePoint)
    ]
    assert kps_added and kps_added[0].languages == ["java"]


# ============================================================
# 质量关卡:工作区可用时无 code_snippet 的题被过滤,
# 全部不合格时带质量反馈重试一次
# ============================================================


def _gen_db(finding_count=1):
    """构造 mock db:按查询目标返回不同链(可指定 finding 条数)"""
    db = MagicMock()
    findings = [
        SimpleNamespace(
            id=f"r{i}",
            title="SQL 注入风险",
            content="cursor.execute(sql) 直接拼接用户输入构造查询",
            metadata_={"cwe": "CWE-89"},
        )
        for i in range(finding_count)
    ]

    def _query(model):
        q = MagicMock()
        if model is gen.PracticeSettings:
            q.filter.return_value.first.return_value = None
        elif model is gen.Result:
            q.filter.return_value.order_by.return_value.all.return_value = findings
        elif model is gen.KnowledgePoint:
            q.filter.return_value.first.return_value = None
        else:  # Question.dedup_hash 列查询:无已入库题目
            q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = _query
    return db


def _gen_task():
    return SimpleNamespace(
        id="t1", params={"repo_url": "https://example.com/r.git"},
    )


def test_quality_gate_filters_snippetless_questions(monkeypatch):
    """repo_path 非空:带 snippet 的题保留,无 snippet 的丢弃"""
    import json

    monkeypatch.setattr(
        gen.sandbox_tools, "get_workspace_info",
        lambda tid: {"repo_path": "/repo"},
    )
    with_snippet = _raw(stem="有代码的题")
    no_snippet = _raw(stem="常识题", code_snippet=None)
    client = _FakeClient([
        [_chunk(content=json.dumps([with_snippet, no_snippet], ensure_ascii=False))],
    ])
    monkeypatch.setattr(gen, "_call_llm", lambda *a, **k: client.rounds[0][0].content_delta)
    created, skipped = gen.generate_questions_for_task(
        _gen_db(), _gen_task(), "u1", client=client,
    )
    assert len(created) == 1
    assert created[0].stem == "有代码的题"


def test_quality_gate_feedback_retry_then_drop(monkeypatch):
    """全部无 snippet → 追加质量反馈重试一次;仍不合格则整条 finding 跳过"""
    import json

    monkeypatch.setattr(
        gen.sandbox_tools, "get_workspace_info",
        lambda tid: {"repo_path": "/repo"},
    )
    no_snippet = json.dumps([_raw(code_snippet=None)], ensure_ascii=False)
    prompts_seen = []

    def fake_call_llm(client, system_prompt, finding_text, task_id, repo_path, on_event=None):
        prompts_seen.append(finding_text)
        return no_snippet

    monkeypatch.setattr(gen, "_call_llm", fake_call_llm)
    created, skipped = gen.generate_questions_for_task(
        _gen_db(), _gen_task(), "u1", client=MagicMock(),
    )
    assert not created
    assert skipped == 1
    # 第一次是原始 prompt,第二次追加了质量反馈
    assert len(prompts_seen) == 2
    assert "质量反馈" not in prompts_seen[0]
    assert "质量反馈" in prompts_seen[1]


def test_quality_gate_skipped_without_workspace(monkeypatch):
    """工作区不可用(repo_path 空):无 snippet 的题也照常保留(纯 prompt 模式)"""
    import json

    no_snippet = json.dumps([_raw(code_snippet=None)], ensure_ascii=False)
    monkeypatch.setattr(gen, "_call_llm", lambda *a, **k: no_snippet)
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    created, skipped = gen.generate_questions_for_task(
        _gen_db(), _gen_task(), "u1", client=MagicMock(),
    )
    assert len(created) == 1
    assert skipped == 0


# ============================================================
# 致命错误快速失败(401/403 额度类:立即中止并冒泡友好原因)
# ============================================================


def _http_response(status=403):
    return httpx.Response(status, request=httpx.Request("POST", "http://test"))


def test_fatal_llm_reason_detection():
    quota_403 = openai.PermissionDeniedError(
        "Error code: 403 - {'type': 'AllocationQuota.FreeTierOnly'}",
        response=_http_response(), body=None,
    )
    assert "免费额度" in _fatal_llm_reason(quota_403)
    perm_403 = openai.PermissionDeniedError(
        "Error code: 403 - access denied", response=_http_response(), body=None,
    )
    assert "403" in _fatal_llm_reason(perm_403)
    auth_401 = openai.AuthenticationError(
        "Error code: 401", response=_http_response(401), body=None,
    )
    assert "API Key" in _fatal_llm_reason(auth_401)
    # 非致命错误(超时/运行时异常)仍走重试丢弃路径
    assert _fatal_llm_reason(RuntimeError("timeout")) is None


def test_fatal_llm_error_aborts_without_retry(monkeypatch):
    """首次调用即额度耗尽:不重试、不处理后续 finding,冒泡友好错误"""
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise openai.PermissionDeniedError(
            "Error code: 403 - {'type': 'AllocationQuota.FreeTierOnly'}",
            response=_http_response(), body=None,
        )

    monkeypatch.setattr(gen, "_call_llm", boom)
    client = MagicMock()
    client.model = "MiniMax-M2.5"
    # 3 条 finding:只在第 1 条第 1 次尝试就中止(共 1 次调用,
    # 而非 3 条 × 2 次尝试 = 6 次空转)
    with pytest.raises(gen.PracticeGenerateError) as ei:
        gen.generate_questions_for_task(
            _gen_db(finding_count=3), _gen_task(), "u1", client=client,
        )
    assert len(calls) == 1
    msg = str(ei.value)
    assert "MiniMax-M2.5" in msg
    assert "免费额度" in msg


# ============================================================
# 去重哈希
# ============================================================


def test_dedup_hash_stable_and_trim_insensitive():
    h1 = compute_dedup_hash("题干", "code")
    h2 = compute_dedup_hash("  题干  ", "  code  ")
    assert h1 == h2
    assert h1 != compute_dedup_hash("另一题干", "code")


def test_dedup_hash_none_snippet():
    assert len(compute_dedup_hash("题干", None)) == 64


# ============================================================
# 主题提示词
# ============================================================


def test_build_system_prompt_topic_switch():
    assert "网络安全培训出题专家" in build_system_prompt("security", False)
    assert "架构培训出题专家" in build_system_prompt("architecture", False)
    assert "通用编码能力培训出题专家" in build_system_prompt("coding", False)


def test_build_system_prompt_unknown_topic_falls_back_security():
    assert "网络安全培训出题专家" in build_system_prompt("not_a_topic", False)


def test_build_system_prompt_tool_section_only_with_workspace():
    assert "源码查阅工具" in build_system_prompt("security", True)
    assert "源码查阅工具" not in build_system_prompt("security", False)


def test_build_system_prompt_common_rules_always_present():
    for topic in ("security", "architecture", "coding"):
        prompt = build_system_prompt(topic, True)
        assert "只输出 JSON 数组" in prompt
        assert "verified: false" in prompt  # 误报发现不出题提示


def test_build_system_prompt_requires_code_reading_questions():
    """通用规则强制题目必须阅读代码才能作答(禁常识题)"""
    for topic in ("security", "architecture", "coding"):
        prompt = build_system_prompt(topic, False)
        assert "必须阅读代码才能作答" in prompt
        assert "常识题" in prompt


def test_build_system_prompt_generalization_and_synthetic():
    """通用规则含泛化要求与改编题指引,输出结构含 origin/languages"""
    for topic in ("security", "architecture", "coding"):
        prompt = build_system_prompt(topic, True)
        assert "自包含" in prompt            # code_snippet 泛化要求
        assert "改编题" in prompt            # synthetic 出题形式
        assert '"origin": "repo|synthetic"' in prompt
        assert '"languages"' in prompt


def test_build_system_prompt_workspace_requires_source_location():
    """工作区可用时:强制先读源码 + 输出要求 source_file/source_lines"""
    prompt = build_system_prompt("security", True)
    assert "必须使用" in prompt  # 工具段从可选变强制
    assert "source_file" in prompt
    assert "source_lines" in prompt


# ============================================================
# 迷你工具循环
# ============================================================


def _chunk(content=None, tool_deltas=None):
    """伪造 StreamChunk(只含 _stream_one_round 用到的字段)"""
    return SimpleNamespace(
        content_delta=content,
        tool_call_deltas=tool_deltas or [],
        finish_reason="stop",
    )


def _delta(index=0, id=None, name=None, args=""):
    return SimpleNamespace(
        index=index, id=id, name=name, arguments_fragment=args,
    )


class _FakeClient:
    """按轮次依次返回预设 chunk 序列,并记录每次调用"""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = []

    def chat_stream(self, messages, **kw):
        self.calls.append({"messages": messages, "kw": kw})
        return iter(self.rounds.pop(0))


def test_call_llm_no_workspace_single_call_without_tools():
    client = _FakeClient([[_chunk(content="[]")]])
    out = _call_llm(client, "sys", "finding 文本", "t1", "")
    assert out == "[]"
    assert len(client.calls) == 1
    assert client.calls[0]["kw"].get("tools") is None


def test_call_llm_tool_loop_executes_tool_then_finalizes(monkeypatch):
    """第一轮 LLM 要求 read_file,执行后第二轮直出题目"""
    read_calls = []

    def fake_read_file(repo_path, file_path, **kw):
        read_calls.append((repo_path, file_path))
        return {"path": file_path, "content": "def foo(): pass"}

    monkeypatch.setattr(gen.sandbox_tools, "read_file", fake_read_file)
    client = _FakeClient([
        [_chunk(tool_deltas=[
            _delta(id="call_1", name="read_file"),
            _delta(args='{"file_path": "src/a.py"}'),
        ])],
        [_chunk(content="[{\"qtype\": \"single_choice\"}]")],
    ])
    out = _call_llm(client, "sys", "finding 文本", "t1", "/repo")
    assert "single_choice" in out
    assert read_calls == [("/repo", "src/a.py")]
    # 第一轮带工具,第二轮收到 tool 结果消息
    assert client.calls[0]["kw"].get("tools") is not None
    msgs = client.calls[1]["messages"]
    assert any(m["role"] == "tool" and "src/a.py" in m["content"] for m in msgs)


def test_execute_practice_tool_unknown_and_truncate(monkeypatch):
    assert "未知工具" in _execute_practice_tool("t1", "/repo", "hack", {})
    monkeypatch.setattr(
        gen.sandbox_tools, "read_file",
        lambda *a, **k: {"content": "x" * 100000},
    )
    out = _execute_practice_tool("t1", "/repo", "read_file", {"file_path": "a.py"})
    assert len(out) == _MAX_TOOL_RESULT_CHARS


def test_execute_practice_tool_failure_returns_text(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("session gone")

    monkeypatch.setattr(gen.sandbox_tools, "search_code", _raise)
    out = _execute_practice_tool("t1", "/repo", "search_code", {"pattern": "x"})
    assert out.startswith("工具执行失败")


# ============================================================
# 出题前工作区保障(_ensure_workspace)
# ============================================================


def _task_with_repo():
    task = MagicMock()
    task.id = "t1"
    task.params = {"repo_url": "https://example.com/r.git", "branch": "dev"}
    return task


def test_ensure_workspace_alive_reuses_no_clone(monkeypatch):
    monkeypatch.setattr(
        gen.sandbox_tools, "get_workspace_info",
        lambda tid: {"repo_path": "/repo", "mode": "sandbox"},
    )
    clone_calls = []
    monkeypatch.setattr(
        gen.sandbox_tools, "clone_repo_with_fallback",
        lambda *a, **k: clone_calls.append(1),
    )
    info = _ensure_workspace(MagicMock(), _task_with_repo(), None)
    assert info["repo_path"] == "/repo"
    assert not clone_calls


def test_ensure_workspace_setting_off_no_clone(monkeypatch):
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    clone_calls = []
    monkeypatch.setattr(
        gen.sandbox_tools, "clone_repo_with_fallback",
        lambda *a, **k: clone_calls.append(1),
    )
    pref = MagicMock()
    pref.restore_workspace_for_practice = False
    assert _ensure_workspace(MagicMock(), _task_with_repo(), pref) is None
    assert not clone_calls


def test_ensure_workspace_restores_when_enabled(monkeypatch):
    """session 已清理 + 开关开启 → 重新 clone 并纳入 TTL 清理序列"""
    state = {"info": None}
    monkeypatch.setattr(
        gen.sandbox_tools, "get_workspace_info", lambda tid: state["info"],
    )
    clone_calls = []

    def fake_clone(repo_url, branch=None, task_id="", git_tokens=None, **kw):
        clone_calls.append((repo_url, branch, task_id))
        state["info"] = {"repo_path": "/repo-restored"}

    monkeypatch.setattr(gen.sandbox_tools, "clone_repo_with_fallback", fake_clone)
    marked = []
    monkeypatch.setattr(
        gen.sandbox_tools, "mark_task_completed", lambda tid: marked.append(tid),
    )
    monkeypatch.setattr(gen, "_load_git_tokens", lambda db, uid: {"github": "tk"})
    pref = MagicMock()
    pref.restore_workspace_for_practice = True
    info = _ensure_workspace(MagicMock(), _task_with_repo(), pref)
    assert info == {"repo_path": "/repo-restored"}
    assert clone_calls == [("https://example.com/r.git", "dev", "t1")]
    assert marked == ["t1"]


def test_ensure_workspace_clone_failure_degrades(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("network error")

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(gen.sandbox_tools, "clone_repo_with_fallback", _raise)
    monkeypatch.setattr(gen, "_load_git_tokens", lambda db, uid: {})
    pref = MagicMock()
    pref.restore_workspace_for_practice = True
    # 失败静默降级返回 None(出题走无工具路径),不抛异常
    assert _ensure_workspace(MagicMock(), _task_with_repo(), pref) is None


def test_ensure_workspace_emits_restore_events(monkeypatch):
    """恢复成功:依次推 start → progress(克隆进度回调透传)→ done 事件"""
    state = {"info": None}
    monkeypatch.setattr(
        gen.sandbox_tools, "get_workspace_info", lambda tid: state["info"],
    )

    def fake_clone(repo_url, branch=None, task_id="", git_tokens=None, **kw):
        # 模拟克隆过程中回调进度(与真实节流点位一致)
        cb = kw.get("progress_callback")
        assert cb is not None, "应透传 progress_callback"
        cb(30, "Receiving objects: 30%")
        cb(80, "Receiving objects: 80%")
        state["info"] = {"repo_path": "/repo-restored"}

    monkeypatch.setattr(gen.sandbox_tools, "clone_repo_with_fallback", fake_clone)
    monkeypatch.setattr(gen.sandbox_tools, "mark_task_completed", lambda tid: None)
    monkeypatch.setattr(gen, "_load_git_tokens", lambda db, uid: {})
    events = []
    pref = MagicMock()
    pref.restore_workspace_for_practice = True
    info = _ensure_workspace(
        MagicMock(), _task_with_repo(), pref,
        event_callback=lambda etype, data: events.append((etype, data)),
    )
    assert info == {"repo_path": "/repo-restored"}
    assert [e[0] for e in events] == ["restore"] * 4
    phases = [e[1]["phase"] for e in events]
    assert phases == ["start", "progress", "progress", "done"]
    assert events[1][1]["percent"] == 30
    assert events[2][1]["message"] == "Receiving objects: 80%"


def test_ensure_workspace_failure_emits_failed_event(monkeypatch):
    """恢复失败:推 start → failed(带截断原因),且不抛异常"""

    def _raise(*a, **k):
        raise RuntimeError("network error")

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(gen.sandbox_tools, "clone_repo_with_fallback", _raise)
    monkeypatch.setattr(gen, "_load_git_tokens", lambda db, uid: {})
    events = []
    pref = MagicMock()
    pref.restore_workspace_for_practice = True
    info = _ensure_workspace(
        MagicMock(), _task_with_repo(), pref,
        event_callback=lambda etype, data: events.append((etype, data)),
    )
    assert info is None
    assert [e[1]["phase"] for e in events] == ["start", "failed"]
    assert "network error" in events[1][1]["message"]


def test_ensure_workspace_no_repo_url(monkeypatch):
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    clone_calls = []
    monkeypatch.setattr(
        gen.sandbox_tools, "clone_repo_with_fallback",
        lambda *a, **k: clone_calls.append(1),
    )
    task = MagicMock()
    task.id = "t1"
    task.params = {}
    pref = MagicMock()
    pref.restore_workspace_for_practice = True
    assert _ensure_workspace(MagicMock(), task, pref) is None
    assert not clone_calls


# ============================================================
# resolve_llm_client 三级解析(task 级 > 用户级默认 > env 默认)
# ============================================================


def _resolve_db(pref_default=None, configs=None, force_default=False):
    """构造 mock db:按查询的模型返回不同行

    force_default=True 且 pref_default=None 时仍返回 settings 行
    (代表用户开了开关但没设默认模型,应回退 env 默认)
    """
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        if model is gen.PracticeSettings:
            if pref_default is not None or force_default:
                pref = MagicMock()
                pref.default_llm_config_id = pref_default
                pref.force_default_llm = force_default
                q.filter.return_value.first.return_value = pref
            else:
                q.filter.return_value.first.return_value = None
        else:  # UserLLMConfig
            q.filter.return_value.first.return_value = SimpleNamespace(
                llm_configs=configs or [],
            )
        return q

    db.query.side_effect = _query
    return db


def test_resolve_task_level_wins(monkeypatch):
    """task 级与用户级默认都命中时,优先用 task 级"""
    llm_cls = MagicMock()
    monkeypatch.setattr(gen, "LLMClient", llm_cls)
    cfg_task = {"id": "cfg-task", "provider": "dashscope", "model": "qwen-max"}
    cfg_user = {"id": "cfg-user", "provider": "dashscope", "model": "qwen-flash"}
    db = _resolve_db(pref_default="cfg-user", configs=[cfg_task, cfg_user])
    task = SimpleNamespace(llm_config_id="cfg-task", user_id="u1")
    gen.resolve_llm_client(db, task)
    llm_cls.from_config_dict.assert_called_once_with(cfg_task)


def test_resolve_task_missing_falls_back_to_user_default(monkeypatch):
    """task 级配置失效(已被删)时回退用户级默认"""
    llm_cls = MagicMock()
    monkeypatch.setattr(gen, "LLMClient", llm_cls)
    cfg_user = {"id": "cfg-user", "provider": "dashscope", "model": "qwen-flash"}
    db = _resolve_db(pref_default="cfg-user", configs=[cfg_user])
    task = SimpleNamespace(llm_config_id="cfg-gone", user_id="u1")
    gen.resolve_llm_client(db, task)
    llm_cls.from_config_dict.assert_called_once_with(cfg_user)


def test_resolve_user_default_only(monkeypatch):
    """task 未指定模型时用用户级默认出题模型"""
    llm_cls = MagicMock()
    monkeypatch.setattr(gen, "LLMClient", llm_cls)
    cfg_user = {"id": "cfg-user", "provider": "dashscope", "model": "qwen-flash"}
    db = _resolve_db(pref_default="cfg-user", configs=[cfg_user])
    task = SimpleNamespace(llm_config_id=None, user_id="u1")
    gen.resolve_llm_client(db, task)
    llm_cls.from_config_dict.assert_called_once_with(cfg_user)


def test_resolve_all_missing_uses_env_default(monkeypatch):
    """task 级与用户级都未命中时回退 env 默认(无参构造)"""
    llm_cls = MagicMock()
    monkeypatch.setattr(gen, "LLMClient", llm_cls)
    db = _resolve_db(pref_default=None, configs=[])
    task = SimpleNamespace(llm_config_id="cfg-gone", user_id="u1")
    gen.resolve_llm_client(db, task)
    llm_cls.from_config_dict.assert_not_called()
    llm_cls.assert_called_once_with()


def test_resolve_force_default_skips_task_level(monkeypatch):
    """「始终用默认出题模型」开启时忽略任务级配置,直接用用户默认"""
    llm_cls = MagicMock()
    monkeypatch.setattr(gen, "LLMClient", llm_cls)
    cfg_task = {"id": "cfg-task", "provider": "dashscope", "model": "qwen-max"}
    cfg_user = {"id": "cfg-user", "provider": "dashscope", "model": "qwen-flash"}
    db = _resolve_db(
        pref_default="cfg-user", configs=[cfg_task, cfg_user], force_default=True
    )
    task = SimpleNamespace(llm_config_id="cfg-task", user_id="u1")
    gen.resolve_llm_client(db, task)
    llm_cls.from_config_dict.assert_called_once_with(cfg_user)


def test_resolve_force_default_without_default_falls_back_env(monkeypatch):
    """开关开启但未设默认模型时回退 env 默认(不误用任务级)"""
    llm_cls = MagicMock()
    monkeypatch.setattr(gen, "LLMClient", llm_cls)
    db = _resolve_db(pref_default=None, configs=[], force_default=True)
    task = SimpleNamespace(llm_config_id="cfg-task", user_id="u1")
    gen.resolve_llm_client(db, task)
    llm_cls.from_config_dict.assert_not_called()
    llm_cls.assert_called_once_with()


def test_resolve_generate_model_info_sources(monkeypatch):
    """模型信息来源判定:task=任务配置 / default=练习默认 / env=环境默认"""
    llm_cls = MagicMock()
    llm_cls.from_config_dict.return_value.model = "qwen-max"  # 配置命中路径
    llm_cls.return_value.model = "qwen-max"  # env 默认回退路径
    monkeypatch.setattr(gen, "LLMClient", llm_cls)
    # 任务级命中
    db = _resolve_db(pref_default="cfg-user", configs=[{"id": "cfg-task"}])
    task = SimpleNamespace(llm_config_id="cfg-task", user_id="u1")
    assert gen.resolve_generate_model_info(db, task) == {"model": "qwen-max", "source": "task"}
    # 默认设置命中(任务未指定)
    db = _resolve_db(pref_default="cfg-user", configs=[{"id": "cfg-user"}])
    task = SimpleNamespace(llm_config_id=None, user_id="u1")
    assert gen.resolve_generate_model_info(db, task) == {"model": "qwen-max", "source": "default"}
    # 开关开启时任务级让位给默认
    db = _resolve_db(
        pref_default="cfg-user", configs=[{"id": "cfg-task"}, {"id": "cfg-user"}],
        force_default=True,
    )
    task = SimpleNamespace(llm_config_id="cfg-task", user_id="u1")
    assert gen.resolve_generate_model_info(db, task) == {"model": "qwen-max", "source": "default"}
    # 全部未命中 → env
    db = _resolve_db(pref_default=None, configs=[])
    task = SimpleNamespace(llm_config_id=None, user_id="u1")
    assert gen.resolve_generate_model_info(db, task) == {"model": "qwen-max", "source": "env"}


# ============================================================
# 出题思考模式覆盖(_apply_thinking_mode)
# ============================================================


def _thinking_client(meta=None, enable_thinking=True):
    """伪造 LLMClient(只含 _apply_thinking_mode 用到的属性)"""
    return SimpleNamespace(
        enable_thinking=enable_thinking,
        model_meta=meta,
        model="kimi-k2.5",
    )


def _pref_mode(mode):
    return SimpleNamespace(thinking_mode_for_practice=mode)


def test_thinking_mode_follow_keeps_config():
    """follow(默认):不动模型配置自身的思考开关"""
    client = _thinking_client(meta={"thinking": "hybrid"}, enable_thinking=True)
    _apply_thinking_mode(client, _pref_mode("follow"))
    assert client.enable_thinking is True
    client2 = _thinking_client(meta={"thinking": "hybrid"}, enable_thinking=False)
    _apply_thinking_mode(client2, _pref_mode("follow"))
    assert client2.enable_thinking is False


def test_thinking_mode_on_forces_enable():
    client = _thinking_client(meta={"thinking": "hybrid"}, enable_thinking=False)
    _apply_thinking_mode(client, _pref_mode("on"))
    assert client.enable_thinking is True


def test_thinking_mode_off_forces_disable():
    client = _thinking_client(meta={"thinking": "hybrid"}, enable_thinking=True)
    _apply_thinking_mode(client, _pref_mode("off"))
    assert client.enable_thinking is False


def test_thinking_mode_off_ignored_for_thinking_only_model():
    """仅支持思考模式的模型(thinking=only):强制关无效,保持原样"""
    client = _thinking_client(meta={"thinking": "only"}, enable_thinking=True)
    _apply_thinking_mode(client, _pref_mode("off"))
    assert client.enable_thinking is True


def test_thinking_mode_no_settings_or_unknown_value():
    """无设置行 / 未知值(如 MagicMock 属性)→ 保持模型配置原样"""
    client = _thinking_client(meta={"thinking": "hybrid"}, enable_thinking=True)
    _apply_thinking_mode(client, None)
    assert client.enable_thinking is True
    _apply_thinking_mode(client, MagicMock())
    assert client.enable_thinking is True
    # model_meta 缺失(自定义模型)时强制开/关仍生效
    client2 = _thinking_client(meta=None, enable_thinking=True)
    _apply_thinking_mode(client2, _pref_mode("off"))
    assert client2.enable_thinking is False
