"""react_agent 纯函数单元测试:工具调用解析、计划清单解析与合并。

不调 LLM、不连 DB、不起沙箱。覆盖文本 tool_call 兜底解析(GLM/Qwen
Hermes 风格)、plan 块提取/合并/状态推断、tool intent 模板生成。

这些纯函数是 react_agent 主循环的关键支路:解析失败会导致工具调用丢失、
plan 状态错乱、前端展示异常。
"""
import pytest

from app.agents.react_agent import (
    _build_tool_intent,
    _extract_plan,
    _extract_text_tool_calls,
    _format_plan_reminder,
    _infer_step_from_tool,
    _merge_plan,
    _strip_tool_call_blocks,
)


# ============================================================
# _extract_text_tool_calls:Hermes 风格 <tool_call> 兜底解析
# ============================================================

def test_extract_text_tool_calls_single_block():
    """单个 <tool_call> 块 → 解析为 1 个调用。"""
    content = '<tool_call>\n{"name": "read_file", "arguments": {"file_path": "a.py"}}\n</tool_call>'
    calls = _extract_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file"
    assert '"file_path": "a.py"' in calls[0]["arguments_str"]
    assert calls[0]["index"] == 0


def test_extract_text_tool_calls_multiple_blocks():
    """多个 <tool_call> 块 → 解析为多个调用,index 递增。"""
    content = (
        '<tool_call>{"name": "list_files", "arguments": {"subdir": "src"}}</tool_call>'
        '<tool_call>{"name": "read_file", "arguments": {"file_path": "b.py"}}</tool_call>'
    )
    calls = _extract_text_tool_calls(content)
    assert len(calls) == 2
    assert calls[0]["name"] == "list_files"
    assert calls[1]["name"] == "read_file"
    assert calls[0]["index"] == 0
    assert calls[1]["index"] == 1


def test_extract_text_tool_calls_parameters_field_alias():
    """部分模型用 parameters 而非 arguments,应同样解析。"""
    content = '<tool_call>{"name": "search_code", "parameters": {"pattern": "eval("}}</tool_call>'
    calls = _extract_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "search_code"
    assert "eval(" in calls[0]["arguments_str"]


def test_extract_text_tool_calls_no_blocks_returns_empty():
    """无 <tool_call> 块 → 空列表。"""
    assert _extract_text_tool_calls("正常思考内容,无工具调用") == []
    assert _extract_text_tool_calls("") == []


def test_extract_text_tool_calls_invalid_json_skipped():
    """JSON 不合法的块跳过(不抛异常)。"""
    content = (
        '<tool_call>{invalid json}</tool_call>'
        '<tool_call>{"name": "valid_tool", "arguments": {}}</tool_call>'
    )
    calls = _extract_text_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["name"] == "valid_tool"


def test_extract_text_tool_calls_missing_name_skipped():
    """缺 name 字段的块跳过。"""
    content = '<tool_call>{"arguments": {}}</tool_call>'
    assert _extract_text_tool_calls(content) == []


def test_extract_text_tool_calls_generates_unique_ids():
    """每次解析生成不同的 id(uuid 后缀)。"""
    content = '<tool_call>{"name": "x", "arguments": {}}</tool_call>'
    calls1 = _extract_text_tool_calls(content)
    calls2 = _extract_text_tool_calls(content)
    assert calls1[0]["id"] != calls2[0]["id"]


# ============================================================
# _strip_tool_call_blocks:清理 content 中的 <tool_call> 块
# ============================================================

def test_strip_tool_call_blocks_removes_all():
    """剥离所有 <tool_call>...</tool_call> 块。"""
    content = (
        "思考前\n"
        '<tool_call>{"name": "x"}</tool_call>\n'
        "中间文本\n"
        '<tool_call>{"name": "y"}</tool_call>\n'
        "思考后"
    )
    result = _strip_tool_call_blocks(content)
    assert "<tool_call>" not in result
    assert "思考前" in result
    assert "中间文本" in result
    assert "思考后" in result


def test_strip_tool_call_blocks_no_match_returns_trimmed():
    """无 <tool_call> 块时仅做 strip。"""
    assert _strip_tool_call_blocks("  plain text  ") == "plain text"
    assert _strip_tool_call_blocks("") == ""


# ============================================================
# _extract_plan:从 thinking content 提取 <plan>...</plan>
# ============================================================

def test_extract_plan_with_status_markers():
    """带状态标记的 plan 块正确解析。"""
    content = (
        "思考开始\n"
        "<plan>\n"
        "1. [done] 克隆仓库并查看结构\n"
        "2. [in_progress] 审计依赖漏洞\n"
        "3. [pending] 审计注入类漏洞\n"
        "</plan>\n"
        "思考结束"
    )
    plan = _extract_plan(content)
    assert plan is not None
    assert len(plan) == 3
    assert plan[0] == {"id": 1, "text": "克隆仓库并查看结构", "status": "done"}
    assert plan[1] == {"id": 2, "text": "审计依赖漏洞", "status": "in_progress"}
    assert plan[2] == {"id": 3, "text": "审计注入类漏洞", "status": "pending"}


def test_extract_plan_without_status_defaults_pending():
    """无状态标记 → 默认 pending。"""
    content = "<plan>\n1. 第一步\n2. 第二步\n</plan>"
    plan = _extract_plan(content)
    assert plan is not None
    assert all(s["status"] == "pending" for s in plan)
    assert plan[0]["text"] == "第一步"


def test_extract_plan_invalid_status_defaults_pending():
    """非法状态值(如 [xyz])→ 降级为 pending。"""
    content = "<plan>\n1. [xyz] 异常状态\n</plan>"
    plan = _extract_plan(content)
    assert plan is not None
    assert plan[0]["status"] == "pending"


def test_extract_plan_no_block_returns_none():
    """无 <plan> 块 → None。"""
    assert _extract_plan("纯思考,无计划") is None
    assert _extract_plan("") is None


def test_extract_plan_empty_block_returns_none():
    """<plan></plan> 空块 → None(无有效步骤)。"""
    assert _extract_plan("<plan>\n\n</plan>") is None


def test_extract_plan_skips_blank_lines():
    """空行不计入步骤,但 id 按原始行号(含中间空行)编号。

    实现:
    - 正则 `<plan>\\s*(.*?)\\s*</plan>` 的 \\s* 剥掉块首尾空白
    - block.split("\\n") 后 enumerate(..., 1) 按行号编号
    - 空行被 continue 跳过,但 enumerate 仍计数
    因此中间的空行会让 id 不连续(有间隔)。id 唯一即可,不强求连续。
    """
    content = "<plan>\n\n1. 第一步\n\n2. 第二步\n\n</plan>"
    # 正则剥首尾空白后 block = "1. 第一步\\n\\n2. 第二步"
    # split → ["1. 第一步", "", "2. 第二步"]
    # enumerate(..., 1) → 行 1 = "1. 第一步", 行 2 = ""(跳过), 行 3 = "2. 第二步"
    plan = _extract_plan(content)
    assert plan is not None
    assert len(plan) == 2
    assert plan[0]["text"] == "第一步"
    assert plan[1]["text"] == "第二步"
    # id 因中间空行而不连续
    assert plan[0]["id"] == 1
    assert plan[1]["id"] == 3
    assert plan[0]["id"] != plan[1]["id"]  # 唯一性


def test_extract_plan_handles_chinese_numbering():
    """中文编号(1、 2、)也能解析。"""
    content = "<plan>\n1、第一步\n2、第二步\n</plan>"
    plan = _extract_plan(content)
    assert plan is not None
    assert len(plan) == 2


def test_extract_plan_takes_first_block():
    """多个 <plan> 块时取第一个(search 而非 findall)。"""
    content = (
        "<plan>\n1. 第一版\n</plan>\n"
        "中间思考\n"
        "<plan>\n1. 第二版\n</plan>"
    )
    plan = _extract_plan(content)
    assert plan is not None
    assert plan[0]["text"] == "第一版"


# ---- JSON 数组格式(system prompt 示范格式,模型实际常输出)----

def test_extract_plan_json_array():
    """JSON 数组格式正确解析,text/status 从对象字段取(而非整行 JSON)。"""
    content = (
        "思考\n<plan>\n[\n"
        '{"id": 1, "text": "前端架构设计", "status": "in_progress"},\n'
        '{"id": 2, "text": "存储层架构", "status": "pending"},\n'
        '{"id": 3, "text": "安全架构设计", "status": "pending"}\n'
        "]\n</plan>"
    )
    plan = _extract_plan(content)
    assert plan is not None
    assert len(plan) == 3
    assert plan[0] == {"id": 1, "text": "前端架构设计", "status": "in_progress"}
    assert plan[1] == {"id": 2, "text": "存储层架构", "status": "pending"}
    assert plan[2] == {"id": 3, "text": "安全架构设计", "status": "pending"}


def test_extract_plan_json_single_line_array():
    """单行 JSON 数组(尾逗号也能容错)。"""
    content = (
        '<plan>[{"id": 1, "text": "步骤A", "status": "done"},'
        '{"id": 2, "text": "步骤B", "status": "pending"},]</plan>'
    )
    plan = _extract_plan(content)
    assert plan is not None
    assert len(plan) == 2
    assert plan[0]["status"] == "done"
    assert plan[1]["status"] == "pending"


def test_extract_plan_jsonl_objects_without_array():
    """无包裹数组的逐行对象(真实事故场景:第二次 plan 更新只有 3 行 JSON)。

    修复前:3 行 JSON 被逐行解析成 3 个文本为原始 JSON 的 pending 步骤,
    与首次 plan 的 text 不匹配 → _merge_plan 追加 → 清单重复膨胀到 8 条。
    修复后:按 JSON 解析出干净的 text,跨次合并可按 text 匹配。
    """
    content = (
        "<plan>\n"
        '{"id": 1, "text": "前端架构设计", "status": "done"},\n'
        '{"id": 2, "text": "存储层架构", "status": "done"},\n'
        '{"id": 3, "text": "安全架构设计", "status": "in_progress"}\n'
        "</plan>"
    )
    plan = _extract_plan(content)
    assert plan is not None
    assert len(plan) == 3
    assert plan[0] == {"id": 1, "text": "前端架构设计", "status": "done"}
    assert plan[2]["status"] == "in_progress"


def test_extract_plan_json_invalid_status_defaults_pending():
    """JSON 里非法 status 降级 pending;缺 text 的条目跳过。"""
    content = (
        '<plan>[{"id": 1, "text": "步骤A", "status": "finished"},'
        '{"id": 2, "status": "done"}]</plan>'
    )
    plan = _extract_plan(content)
    assert plan is not None
    assert len(plan) == 1
    assert plan[0]["status"] == "pending"


def test_extract_plan_json_content_key_supported():
    """兼容 content 字段(与 ACP plan entry 字段名对齐)。"""
    content = '<plan>[{"content": "步骤A", "status": "done"}]</plan>'
    plan = _extract_plan(content)
    assert plan is not None
    assert plan[0]["text"] == "步骤A"
    assert plan[0]["status"] == "done"


def test_extract_plan_line_mode_skips_symbol_lines():
    """逐行格式里混入纯符号行([ ])时不产生无意义步骤。"""
    content = (
        "<plan>\n[\n1. [done] 第一步\n2. 第二步\n]\n</plan>"
    )
    plan = _extract_plan(content)
    assert plan is not None
    assert len(plan) == 2
    assert plan[0]["text"] == "第一步"
    assert plan[1]["text"] == "第二步"


# ============================================================
# _infer_step_from_tool:根据工具名推断当前 plan step
# ============================================================

def test_infer_step_prefers_pending():
    """同关键词匹配时,优先 pending(即将开始)step。"""
    plan = [
        {"id": 1, "text": "克隆仓库", "status": "done"},
        {"id": 2, "text": "查看结构", "status": "pending"},
        {"id": 3, "text": "审计代码", "status": "in_progress"},
    ]
    # list_files 关键词含"结构",应匹配 id=2(pending)
    assert _infer_step_from_tool("list_files", plan) == 2


def test_infer_step_falls_back_to_in_progress():
    """无 pending 匹配时,回退到 in_progress。"""
    plan = [
        {"id": 1, "text": "克隆仓库", "status": "done"},
        {"id": 2, "text": "审计注入类", "status": "in_progress"},
    ]
    # search_code 关键词含"注入",应匹配 id=2(in_progress)
    assert _infer_step_from_tool("search_code", plan) == 2


def test_infer_step_no_match_returns_none():
    """工具名无关键词映射或无 step 匹配 → None。"""
    plan = [{"id": 1, "text": "克隆仓库", "status": "pending"}]
    assert _infer_step_from_tool("unknown_tool", plan) is None
    # list_files 关键词"结构/目录"与"克隆仓库"不匹配
    assert _infer_step_from_tool("list_files", plan) is None


def test_infer_step_empty_plan_returns_none():
    """空 plan → None。"""
    assert _infer_step_from_tool("clone_repo", []) is None


def test_infer_step_case_insensitive():
    """关键词匹配大小写不敏感(关键词与 step.text 都 lower)。"""
    plan = [{"id": 1, "text": "CLONE repository", "status": "pending"}]
    assert _infer_step_from_tool("clone_repo", plan) == 1


def test_infer_step_skips_done():
    """done 状态的 step 不被选中(已完成不再推进)。"""
    plan = [
        {"id": 1, "text": "克隆仓库", "status": "done"},
        {"id": 2, "text": "查找文件", "status": "done"},
    ]
    # 全部 done,即使关键词匹配也返回 None
    assert _infer_step_from_tool("clone_repo", plan) is None


# ============================================================
# _merge_plan:LLM 输出的 plan 合并到 current
# ============================================================

def test_merge_plan_empty_llm_update_returns_current_copy():
    """llm_update 为空 → 返回 current 的浅拷贝(不修改原 list)。"""
    current = [{"id": 1, "text": "步骤", "status": "pending"}]
    merged = _merge_plan(current, [])
    assert merged == current
    assert merged is not current  # 新 list


def test_merge_plan_empty_current_uses_llm_update():
    """current 为空 → 用 llm_update(浅拷贝,保留原 id,不重新编号)。

    注意:这是实现的早返回路径(`if not current: return [dict(s) for s in llm_update]`),
    与主路径(会重新编号)行为不一致。此处测试锁定当前行为,
    若后续统一了重编号逻辑,需同步更新本测试。
    """
    llm_update = [{"id": 99, "text": "新步骤", "status": "in_progress"}]
    merged = _merge_plan([], llm_update)
    assert len(merged) == 1
    assert merged[0]["text"] == "新步骤"
    # 早返回路径不重编号,保留原 id
    assert merged[0]["id"] == 99
    # 但应是新 dict(不修改入参)
    assert merged[0] is not llm_update[0]


def test_merge_plan_llm_marks_done():
    """LLM 标 done → current 对应 step 标 done(信任 LLM 的完成判断)。"""
    current = [
        {"id": 1, "text": "克隆仓库", "status": "in_progress"},
        {"id": 2, "text": "审计代码", "status": "pending"},
    ]
    llm_update = [{"id": 1, "text": "克隆仓库", "status": "done"}]
    merged = _merge_plan(current, llm_update)
    assert merged[0]["status"] == "done"
    assert merged[1]["status"] == "pending"  # 未提及,保持原状


def test_merge_plan_llm_untouched_step_keeps_current_status():
    """LLM 未提及的 step 保持 current 状态(代码推进的 in_progress 不丢)。"""
    current = [
        {"id": 1, "text": "A", "status": "in_progress"},
        {"id": 2, "text": "B", "status": "pending"},
    ]
    llm_update = [{"id": 1, "text": "A", "status": "done"}]
    merged = _merge_plan(current, llm_update)
    assert merged[1]["status"] == "pending"


def test_merge_plan_llm_new_step_appended():
    """LLM 新增的 step(文本不在 current)追加到末尾。"""
    current = [{"id": 1, "text": "原步骤", "status": "done"}]
    llm_update = [
        {"id": 1, "text": "原步骤", "status": "done"},
        {"id": 2, "text": "新步骤", "status": "pending"},
    ]
    merged = _merge_plan(current, llm_update)
    assert len(merged) == 2
    assert merged[1]["text"] == "新步骤"
    assert merged[1]["status"] == "pending"


def test_merge_plan_renumbers_ids_sequentially():
    """合并后 id 从 1 开始顺序重编(LLM 可能用任意 id)。"""
    current = [{"id": 7, "text": "A", "status": "pending"}]
    llm_update = [{"id": 99, "text": "A", "status": "done"}, {"id": 100, "text": "B", "status": "pending"}]
    merged = _merge_plan(current, llm_update)
    assert [s["id"] for s in merged] == [1, 2]


def test_merge_plan_text_match_case_insensitive():
    """text 匹配忽略大小写与首尾空白。"""
    current = [{"id": 1, "text": "Step A", "status": "pending"}]
    llm_update = [{"id": 1, "text": "  step a  ", "status": "done"}]
    merged = _merge_plan(current, llm_update)
    assert merged[0]["status"] == "done"


def test_merge_plan_does_not_mutate_input():
    """合并不修改入参 current / llm_update(返回新 list)。"""
    current = [{"id": 1, "text": "A", "status": "pending"}]
    llm_update = [{"id": 1, "text": "A", "status": "done"}]
    _merge_plan(current, llm_update)
    assert current[0]["status"] == "pending"  # 原入参未变
    assert llm_update[0]["status"] == "done"


# ============================================================
# _format_plan_reminder:格式化 plan 为 system 提醒
# ============================================================

def test_format_plan_reminder_empty_returns_empty():
    """空 plan → 空字符串(不注入无意义提醒)。"""
    assert _format_plan_reminder([]) == ""


def test_format_plan_reminder_includes_all_steps():
    """提醒包含所有步骤的符号 + 状态 + 文本。"""
    plan = [
        {"id": 1, "text": "已完成步", "status": "done"},
        {"id": 2, "text": "进行中步", "status": "in_progress"},
        {"id": 3, "text": "待办步", "status": "pending"},
    ]
    reminder = _format_plan_reminder(plan)
    assert "已完成步" in reminder
    assert "进行中步" in reminder
    assert "待办步" in reminder
    assert "[done]" in reminder
    assert "[in_progress]" in reminder
    assert "[pending]" in reminder
    # 末尾应有引导 LLM 标 done 的提示
    assert "done" in reminder


def test_format_plan_reminder_unknown_status_falls_back_to_pending_symbol():
    """未知 status 用 pending 符号(○)。"""
    plan = [{"id": 1, "text": "X", "status": "unknown"}]
    reminder = _format_plan_reminder(plan)
    assert "○" in reminder
    assert "[unknown]" in reminder  # 文本里仍原样输出 status


# ============================================================
# _build_tool_intent:工具名+参数 → 人类可读意图
# ============================================================

@pytest.mark.parametrize("tool_name,args,expected_substring", [
    ("clone_repo", {"repo_url": "https://github.com/x/y"}, "https://github.com/x/y"),
    ("list_files", {"subdir": "src"}, "src"),
    ("list_files", {}, "根目录"),
    ("find_files", {"pattern": "**/*.py"}, "**/*.py"),
    ("read_file", {"file_path": "a.py"}, "a.py"),
    ("search_code", {"pattern": "eval("}, "eval("),
    ("query_cve", {"package_name": "lodash", "version": "4.17.0"}, "lodash@4.17.0"),
    ("write_file", {"file_path": "out.txt", "mode": "write"}, "写入文件"),
    ("write_file", {"file_path": "log.txt", "mode": "append"}, "追加文件"),
    ("run_python_code", {}, "执行 Python 代码"),
    ("run_semgrep", {}, "Semgrep"),
    ("git_log", {}, "提交历史"),
    ("git_log", {"file_path": "a.py"}, "a.py"),
    ("git_blame", {"file_path": "a.py"}, "a.py"),
    ("run_command", {"command": "npm test"}, "npm test"),
    ("run_command", {}, "shell 命令"),
    ("str_replace_editor", {"command": "create", "file_path": "x.py"}, "创建文件"),
    ("str_replace_editor", {"command": "str_replace", "file_path": "x.py"}, "编辑文件"),
    ("str_replace_editor", {"command": "insert", "file_path": "x.py"}, "插入内容"),
    ("list_skills", {}, "技能"),
    ("skill", {"skill_name": "check_sql_injection"}, "check_sql_injection"),
])
def test_build_tool_intent_known_tools(tool_name, args, expected_substring):
    """已知工具的意图模板生成正确,且末尾带 [tool_name] 标签。"""
    intent = _build_tool_intent(tool_name, args)
    assert expected_substring in intent
    assert intent.endswith(f"[{tool_name}]")


def test_build_tool_intent_unknown_tool_falls_back():
    """未知工具回退到"调用 {tool_name} [tool_name]"。"""
    intent = _build_tool_intent("custom_tool", {"x": 1})
    assert "调用 custom_tool" in intent
    assert intent.endswith("[custom_tool]")


def test_build_tool_intent_truncates_long_command():
    """run_command 的 command 截断到 40 字符(防长命令撑爆卡片)。"""
    long_cmd = "echo " + "x" * 100
    intent = _build_tool_intent("run_command", {"command": long_cmd})
    # 应包含截断后的命令(前 40 字符),不包含完整命令
    assert long_cmd[:40] in intent
    assert long_cmd not in intent

