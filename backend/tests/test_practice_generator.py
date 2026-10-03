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
from app.models.practice import LearningTopic
from app.prompts.practice import build_system_prompt
from app.services.practice.generator import (
    _MAX_TOOL_RESULT_CHARS,
    _apply_thinking_mode,
    _call_llm,
    _ensure_workspace,
    _execute_practice_tool,
    _fatal_llm_reason,
    _normalize_raw_question,
    _parse_llm_questions,
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


def _gen_db(finding_count=1, findings=None, topics=None):
    """构造 mock db:按查询目标返回不同链(可指定 finding 列表/主题词表)"""
    db = MagicMock()
    if findings is None:
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
        elif model is LearningTopic:
            # ensure_user_topics 的词表查询(需含全部内置 key 才不触发播种)
            q.filter.return_value.all.return_value = topics if topics is not None else []
        else:  # Question.dedup_hash 列查询:无已入库题目
            q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = _query
    return db


def _gen_task(scenario=None):
    return SimpleNamespace(
        id="t1", params={"repo_url": "https://example.com/r.git"},
        scenario=scenario,
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
# 质量关卡 1:退化题检测(叙述式判断题 / 答案泄露措辞,始终启用)
# ============================================================

# 真实退化案例:仅凭叙述句式即可答「错误」,零知识送分
REAL_BAD_STEM = (
    "某同学在仓库中看到文件「vibe_coding_contract_final.md」,大小 5 KB,"
    "首行为「# 外包开发合同(AI 生成版)」。他仅凭文件名里有 \"contract\" "
    "就认定这是一份 Word 合同文档。该判断是否正确?"
)


def test_is_degenerate_narrative_question():
    """「某同学 + 该判断是否正确」的叙述式判断题:单选/判断均为退化"""
    assert gen._is_degenerate_question(_raw(
        stem=REAL_BAD_STEM, qtype="true_false", options=["正确", "错误"]))
    assert gen._is_degenerate_question(_raw(stem=REAL_BAD_STEM))


def test_is_degenerate_answer_leak_wording():
    """判断题含「仅凭」等泄露词:措辞即答案(此类叙述必然是反例)"""
    for word in ("仅凭", "就想当然", "便断定", "就认定"):
        assert gen._is_degenerate_question(_raw(
            stem=f"他{word}配置无误,可以安全上线",
            qtype="true_false", options=["正确", "错误"],
        ))


def test_is_degenerate_normal_question_passes():
    """正常专业题不误伤:题干直接对材料技术事实作判断"""
    assert not gen._is_degenerate_question(_raw())
    # 判断题但题干是专业事实判断,无人物叙述、无泄露词
    assert not gen._is_degenerate_question(_raw(
        stem="该查询直接拼接用户输入,存在 SQL 注入风险",
        qtype="true_false", options=["正确", "错误"],
    ))
    # 单选题含「仅凭」不拦截:有干扰项,泄露程度低
    assert not gen._is_degenerate_question(
        _raw(stem="仅凭文件名后缀判断上传文件格式,以下哪种做法更安全?")
    )
    # 无人物只有「是否正确」不拦截:可能是正常的事实判断
    assert not gen._is_degenerate_question(_raw(
        stem="该函数对空输入的处理是否正确?",
        qtype="true_false", options=["正确", "错误"],
    ))


def test_quality_gate_filters_degenerate_questions(monkeypatch):
    """混合输出:正常题保留,叙述式判断题被丢弃(工作区不可用也拦截)"""
    import json

    good = _raw()
    bad = _raw(stem=REAL_BAD_STEM, qtype="true_false", options=["正确", "错误"])
    output = json.dumps([good, bad], ensure_ascii=False)
    monkeypatch.setattr(gen, "_call_llm", lambda *a, **k: output)
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    created, skipped = gen.generate_questions_for_task(
        _gen_db(), _gen_task(), "u1", client=MagicMock(),
    )
    assert len(created) == 1
    assert created[0].stem == good["stem"]
    assert skipped == 0


def test_quality_gate_degenerate_retry_feedback(monkeypatch):
    """全部退化 → 带退化题质量反馈重试一次;仍退化则整条 finding 跳过"""
    import json

    bad = json.dumps([_raw(
        stem=REAL_BAD_STEM, qtype="true_false", options=["正确", "错误"],
    )], ensure_ascii=False)
    prompts_seen = []

    def fake_call_llm(client, system_prompt, finding_text, task_id, repo_path, on_event=None):
        prompts_seen.append(finding_text)
        return bad

    monkeypatch.setattr(gen, "_call_llm", fake_call_llm)
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    created, skipped = gen.generate_questions_for_task(
        _gen_db(), _gen_task(), "u1", client=MagicMock(),
    )
    assert not created
    assert skipped == 1
    # 第一次原始 prompt,第二次追加了退化题质量反馈
    assert len(prompts_seen) == 2
    assert "出题套路" not in prompts_seen[0]
    assert "出题套路" in prompts_seen[1]


def test_common_rules_forbid_narrative_and_leak():
    """通用规则含退化题禁止条款(叙述式判断/答案泄露/常识题/空数组许可)"""
    rules = build_system_prompt("security", workspace_available=False)
    # 禁止叙述式判断题与答案泄露措辞
    assert "某同学" in rules
    assert "仅凭" in rules
    # 知识密度要求(禁常识题)
    assert "专业深度" in rules
    # 空数组许可扩大到「发现内容单薄」
    assert "单薄" in rules


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
    assert "合同文书培训出题专家" in build_system_prompt("contract", False)


def test_build_system_prompt_unknown_topic_falls_back_security():
    assert "网络安全培训出题专家" in build_system_prompt("not_a_topic", False)


def test_build_system_prompt_tool_section_only_with_workspace():
    assert "材料查阅工具" in build_system_prompt("security", True)
    assert "材料查阅工具" not in build_system_prompt("security", False)


def test_build_system_prompt_common_rules_always_present():
    for topic in ("security", "architecture", "coding", "contract"):
        prompt = build_system_prompt(topic, True)
        assert "只输出 JSON 数组" in prompt
        assert "verified: false" in prompt  # 误报发现不出题提示


def test_build_system_prompt_requires_material_reading_questions():
    """通用规则强制题目必须阅读材料(代码或条款原文)才能作答(禁常识题)"""
    for topic in ("security", "architecture", "coding", "contract"):
        prompt = build_system_prompt(topic, False)
        assert "必须阅读材料才能作答" in prompt
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
    """工作区可用时:强制先读材料 + 输出要求 source_file/source_lines"""
    prompt = build_system_prompt("security", True)
    assert "必须使用" in prompt  # 工具段从可选变强制
    assert "source_file" in prompt
    assert "source_lines" in prompt


# ============================================================
# 主题自动匹配(规则先行 + LLM 批量兜底;不再是用户级设置)
# ============================================================


def _plain_finding(fid, title="发现", meta=None):
    """构造一条无 CWE 的 finding(规则判不定,交 LLM 分类)"""
    return SimpleNamespace(
        id=fid, title=title, content="若干描述", metadata_=meta or {},
    )


def _builtin_topic_defs():
    """内置主题词表(模拟 ensure_user_topics 返回启用主题的 dict 形态)"""
    return [
        {"key": d["key"], "name": d["name"], "description": d["description"]}
        for d in gen.BUILTIN_TOPIC_DEFS
    ]


def _topic_rows(enabled_keys=None):
    """构造 LearningTopic 行(SimpleNamespace,模拟库表返回)"""
    enabled_keys = enabled_keys or {d["key"] for d in gen.BUILTIN_TOPIC_DEFS}
    return [
        SimpleNamespace(
            key=d["key"], name=d["name"], description=d["description"],
            is_builtin=True, enabled=d["key"] in enabled_keys,
            sort_order=d["sort_order"],
            created_at="2026-01-01",
        )
        for d in gen.BUILTIN_TOPIC_DEFS
    ]


def test_match_topic_by_rule_document_review_scenario():
    """文书审核场景 → 全部 contract(场景强信号,优先于 CWE)"""
    task = _gen_task(scenario="document_review")
    assert gen._match_topic_by_rule(task, {"cwe": "CWE-89"}) == "contract"
    assert gen._match_topic_by_rule(task, {}) == "contract"


def test_match_topic_by_rule_cwe_key_or_value():
    """metadata 键名含 cwe 或值匹配 CWE-\\d → security"""
    task = _gen_task(scenario="code_review")
    assert gen._match_topic_by_rule(task, {"cwe": "89"}) == "security"
    assert gen._match_topic_by_rule(task, {"ref": "见 CWE-79 说明"}) == "security"


def test_match_topic_by_rule_undetermined_returns_none():
    """无场景强信号且无 CWE → None(交 LLM 分类)"""
    task = _gen_task(scenario="code_review")
    assert gen._match_topic_by_rule(task, {"file_path": "a.py"}) is None
    assert gen._match_topic_by_rule(task, {}) is None


def test_match_finding_topics_rule_hits_skip_llm(monkeypatch):
    """规则全部命中时不触发 LLM 分类调用"""
    calls = []
    monkeypatch.setattr(
        gen, "_classify_topics_with_llm",
        lambda *a, **k: calls.append(1) or {},
    )
    findings = [_mk_finding("a"), _mk_finding("b")]  # 均带 cwe → security
    out = gen._match_finding_topics(
        _gen_task(), findings, MagicMock(), _builtin_topic_defs(),
    )
    assert out == {"a": "security", "b": "security"}
    assert not calls


def test_match_finding_topics_llm_classifies_pending(monkeypatch):
    """规则未定的 finding 批量送 LLM,按序号回填主题"""
    findings = [
        _plain_finding("p0", title="付款条件模糊"),
        _plain_finding("p1", title="SQL 注入风险"),
    ]

    def fake_classify(client, pending, task_id, topic_defs):
        return {
            idx: ("contract" if "付款" in f.title else "security")
            for idx, f in pending
        }

    monkeypatch.setattr(gen, "_classify_topics_with_llm", fake_classify)
    out = gen._match_finding_topics(
        _gen_task(scenario="code_review"), findings, MagicMock(),
        _builtin_topic_defs(),
    )
    assert out == {"p0": "contract", "p1": "security"}


def test_match_finding_topics_llm_failure_degrades_first_enabled(monkeypatch):
    """LLM 分类抛异常 → 降级到排序第一的启用主题(不再硬编码 security)"""
    def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(gen, "_classify_topics_with_llm", boom)
    findings = [_plain_finding("x")]
    # security 停用,排序第一的启用主题为 architecture
    defs = [d for d in _builtin_topic_defs() if d["key"] != "security"]
    out = gen._match_finding_topics(_gen_task(), findings, MagicMock(), defs)
    assert out == {"x": "architecture"}


def test_match_finding_topics_custom_key_accepted(monkeypatch):
    """LLM 分类返回自定义主题 key 被接受(词表含自定义)"""
    defs = _builtin_topic_defs() + [
        {"key": "custom_abcd1234", "name": "算法", "description": "复杂度分析"},
    ]
    monkeypatch.setattr(
        gen, "_classify_topics_with_llm",
        lambda *a, **k: {0: "custom_abcd1234"},
    )
    findings = [_plain_finding("x")]
    out = gen._match_finding_topics(
        _gen_task(scenario="code_review"), findings, MagicMock(), defs,
    )
    assert out == {"x": "custom_abcd1234"}


def test_question_learning_topic_records_matched(monkeypatch):
    """文书审核场景出题:Question.learning_topic 落库为实际匹配主题 contract"""
    import json

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(
        gen, "_call_llm", lambda *a, **k: json.dumps([_raw()], ensure_ascii=False),
    )
    created, _ = gen.generate_questions_for_task(
        _gen_db(), _gen_task(scenario="document_review"), "u1", client=MagicMock(),
    )
    assert created and created[0].learning_topic == "contract"


# ============================================================
# 学习主题词表动态化(分类器 / 提示词 / 出题过滤)
# ============================================================


def test_classify_topics_custom_key_accepted(monkeypatch):
    """LLM 分类返回词表内的自定义 key → 被接受"""
    monkeypatch.setattr(
        gen, "_stream_one_round",
        lambda client, messages, tools: (
            '[{"id": 0, "topic": "custom_abcd1234", "reason": "算法相关"}]', None,
        ),
    )
    defs = _builtin_topic_defs() + [
        {"key": "custom_abcd1234", "name": "算法", "description": "复杂度分析"},
    ]
    out = gen._classify_topics_with_llm(
        MagicMock(), [(0, _plain_finding("x"))], "t1", defs,
    )
    assert out == {0: "custom_abcd1234"}


def test_classify_topics_unknown_key_dropped(monkeypatch):
    """LLM 分类返回词表外的 key → 条目被丢弃(不落入 topics)"""
    monkeypatch.setattr(
        gen, "_stream_one_round",
        lambda client, messages, tools: (
            '[{"id": 0, "topic": "not_in_vocab", "reason": "x"}]', None,
        ),
    )
    out = gen._classify_topics_with_llm(
        MagicMock(), [(0, _plain_finding("x"))], "t1", _builtin_topic_defs(),
    )
    assert out == {}


def test_classify_prompt_includes_custom_topics():
    """分类提示词按词表动态构建:自定义主题以 key:name——描述 列出"""
    from app.prompts.practice import build_topic_classify_prompt

    defs = _builtin_topic_defs() + [
        {"key": "custom_abcd1234", "name": "算法", "description": "复杂度分析"},
    ]
    prompt = build_topic_classify_prompt(defs)
    # 内置四类描述保留
    for key in ("security", "architecture", "coding", "contract"):
        assert f"- {key}:" in prompt
    # 自定义主题出现且格式正确
    assert "- custom_abcd1234:算法——复杂度分析" in prompt
    # 输出约束:key 必须来自列表
    assert "必须是上述列表中给定的 key 之一" in prompt


def test_classify_prompt_builtin_only_matches_legacy_text():
    """仅内置词表时,分类提示词与历史四行文案一致(无自定义条目)"""
    from app.prompts.practice import build_topic_classify_prompt

    prompt = build_topic_classify_prompt(_builtin_topic_defs())
    assert "- security:安全漏洞" in prompt
    assert "custom_" not in prompt


def test_build_custom_topic_head_renders_user_description():
    """自定义主题出题视角:通用模板渲染用户描述"""
    from app.prompts.practice import build_custom_topic_head

    head = build_custom_topic_head({
        "key": "custom_abcd1234", "name": "算法",
        "description": "考察复杂度分析、边界条件、正确性证明",
    })
    assert "「算法」培训出题专家" in head
    assert "考察复杂度分析、边界条件、正确性证明" in head


def test_build_system_prompt_custom_topic(monkeypatch):
    """自定义主题 key 命中 custom_topics → 用通用模板渲染(不回落 security)"""
    from app.prompts.practice import build_system_prompt

    custom = [{"key": "custom_abcd1234", "name": "算法", "description": "复杂度"}]
    prompt = build_system_prompt(
        "custom_abcd1234", False, custom_topics=custom,
    )
    assert "「算法」培训出题专家" in prompt
    # 通用规则段仍包含
    assert "必须阅读材料才能作答" in prompt


def test_build_system_prompt_unknown_custom_without_defs_falls_back():
    """自定义 key 不在 custom_topics / 无词表 → 回落 security(防御)"""
    from app.prompts.practice import build_system_prompt

    prompt = build_system_prompt("custom_xxxx", False, custom_topics=None)
    assert "网络安全培训出题专家" in prompt


def test_kp_learning_topic_first_wins():
    """新建 KP 写入出题主题;已存在 KP 保持原主题(first-wins)"""
    # 新建:query 返回 None → learning_topic 写入
    db = _gen_db()
    kp = gen._get_or_create_knowledge_point(
        db, "u1", "CWE-89", "SQL 注入", learning_topic="contract",
    )
    assert kp.learning_topic == "contract"

    # 已存在:query 返回既有 KP(learning_topic=security)→ 不改
    existing = SimpleNamespace(
        user_id="u1", key="CWE-89", name="SQL 注入",
        languages=["python"], learning_topic="security",
    )
    db2 = MagicMock()
    q = MagicMock()
    q.filter.return_value.first.return_value = existing
    db2.query.return_value = q
    kp2 = gen._get_or_create_knowledge_point(
        db2, "u1", "CWE-89", "SQL 注入", learning_topic="contract",
    )
    assert kp2 is existing
    assert kp2.learning_topic == "security"


def test_generator_filters_disabled_topic_findings(monkeypatch):
    """停用主题的 finding 在出题前被过滤,进度分母重算"""
    import json

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(
        gen, "_call_llm", lambda *a, **k: json.dumps([_raw()], ensure_ascii=False),
    )
    # security 停用:带 CWE 的 finding(规则捷径命中 security)应被跳过;
    # 无 CWE 的 finding 走 LLM 分类失败降级 architecture(排序第一启用主题)
    def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(gen, "_classify_topics_with_llm", boom)
    findings = [
        _mk_finding("a"),                              # cwe → security(停用)
        _plain_finding("b", title="模块耦合过紧"),       # LLM 分类 → 降级 architecture
    ]
    topics = _topic_rows(enabled_keys={"architecture", "coding", "contract"})
    events = []
    created, _ = gen.generate_questions_for_task(
        _gen_db(findings=findings, topics=topics), _gen_task(), "u1",
        client=MagicMock(), event_callback=lambda t, d: events.append((t, d)),
    )
    # 只有 finding b 出题(topic=architecture)
    assert len(created) == 1
    assert created[0].learning_topic == "architecture"
    # 进度事件的 total 反映过滤后的分母(1 而非 2)
    finding_events = [d for t, d in events if t == "finding"]
    assert finding_events and finding_events[-1]["total"] == 1


def test_generator_all_enabled_keeps_legacy_behavior(monkeypatch):
    """全启用时行为与现状等价:带 CWE 的 finding 照常出题(security)"""
    import json

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(
        gen, "_call_llm", lambda *a, **k: json.dumps([_raw()], ensure_ascii=False),
    )
    created, _ = gen.generate_questions_for_task(
        _gen_db(topics=_topic_rows()), _gen_task(), "u1", client=MagicMock(),
    )
    assert created and created[0].learning_topic == "security"


def test_generator_no_enabled_topics_falls_back_builtin(monkeypatch):
    """词表为空(防御)→ 按内置词表出题,不阻断"""
    import json

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(
        gen, "_call_llm", lambda *a, **k: json.dumps([_raw()], ensure_ascii=False),
    )
    created, _ = gen.generate_questions_for_task(
        _gen_db(topics=[]), _gen_task(), "u1", client=MagicMock(),
    )
    assert created and created[0].learning_topic == "security"


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


# ============================================================
# 选题:agent2 学习点标记(practice_worthy)优先
# ============================================================


def _mk_finding(fid, marked=False, note=None):
    """构造一条 finding;marked=True 时带 practice_worthy + learning_note"""
    meta = {"cwe": "CWE-89"}
    if marked:
        meta["practice_worthy"] = True
        if note is not None:
            meta["learning_note"] = note
    return SimpleNamespace(
        id=fid,
        title=f"发现 {fid}",
        content="cursor.execute(sql) 直接拼接用户输入构造查询",
        metadata_=meta,
    )


def test_select_findings_marked_first_preserving_order():
    """标记的优先且保持 agent2 给出的原顺序,未标记的排在后面。"""
    a, b, c, d = (
        _mk_finding("a"), _mk_finding("b", marked=True),
        _mk_finding("c", marked=True), _mk_finding("d"),
    )
    out = gen._select_findings(_gen_db(findings=[a, b, c, d]), _gen_task(), 10)
    assert [f.id for f in out] == ["b", "c", "a", "d"]


def test_select_findings_backfills_unmarked_when_insufficient():
    """标记的不足 max_findings → 按原顺序补未标记的。"""
    a, b, c = (
        _mk_finding("a", marked=True), _mk_finding("b"), _mk_finding("c"),
    )
    out = gen._select_findings(_gen_db(findings=[a, b, c]), _gen_task(), 3)
    assert [f.id for f in out] == ["a", "b", "c"]


def test_select_findings_no_marking_keeps_legacy_behavior():
    """无任何标记(单 agent 模式 / 老任务)→ 与按 created_at 取前 N 条一致。"""
    findings = [_mk_finding(f"r{i}") for i in range(5)]
    out = gen._select_findings(_gen_db(findings=findings), _gen_task(), 3)
    assert [f.id for f in out] == ["r0", "r1", "r2"]


def test_select_findings_marked_exceeding_max_truncated():
    """标记数超过 max_findings → 截断(标记内部保持原顺序)。"""
    findings = [_mk_finding(f"m{i}", marked=True) for i in range(4)]
    out = gen._select_findings(_gen_db(findings=findings), _gen_task(), 2)
    assert [f.id for f in out] == ["m0", "m1"]


# ============================================================
# learning_note 注入出题提示
# ============================================================


def test_learning_note_injected_into_finding_prompt(monkeypatch):
    """practice_worthy + learning_note → 出题 prompt 注入学习价值提示。

    同批未标记的 finding 不注入;标记但缺 learning_note 也不注入。
    """
    import json

    monkeypatch.setattr(
        gen.sandbox_tools, "get_workspace_info", lambda tid: None,
    )
    prompts = []

    def fake_call_llm(client, system_prompt, finding_text, task_id, repo_path, on_event=None):
        prompts.append(finding_text)
        return json.dumps([_raw()], ensure_ascii=False)

    monkeypatch.setattr(gen, "_call_llm", fake_call_llm)

    findings = [
        _mk_finding("marked", marked=True, note="考察 SQL 注入的参数化修复方式"),
        _mk_finding("marked-no-note", marked=True),
        _mk_finding("plain"),
    ]
    created, _ = gen.generate_questions_for_task(
        _gen_db(findings=findings), _gen_task(), "u1", client=MagicMock(),
    )
    assert created  # 正常出题
    assert len(prompts) == 3

    marked_prompt = prompts[0]
    assert "【学习价值提示】" in marked_prompt
    assert "考察 SQL 注入的参数化修复方式" in marked_prompt
    # 标记但缺 note / 未标记:均不注入
    assert "【学习价值提示】" not in prompts[1]
    assert "【学习价值提示】" not in prompts[2]
