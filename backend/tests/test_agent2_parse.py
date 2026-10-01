"""agent2 输出解析容错测试(_parse_json_response)

基于 json_repair 的解析器,覆盖真实失败场景:
- markdown ```json 包裹(含截断时无尾围栏)
- JSON 前后夹带散文 / 多个 JSON 片段(挑选真正评估结果)
- max_tokens 打满导致的 JSON 截断(字符串内/逗号后/悬空键/字面量碎片)
- 空输出与非对象输出
"""
import json

import pytest

from app.agents.agent2 import _parse_json_response


def _eval_dict(**overrides):
    result = {
        "covered": ["a"],
        "missing": [],
        "reasoning": "评估理由",
        "followup_query": "",
        "done": True,
    }
    result.update(overrides)
    return result


def _eval_json(**overrides):
    return json.dumps(_eval_dict(**overrides), ensure_ascii=False)


# ============================================================
# 基础:直接解析 / 围栏包裹
# ============================================================

def test_parse_plain_json():
    assert _parse_json_response(_eval_json()) == _eval_dict()


def test_parse_fenced_json():
    content = "```json\n" + _eval_json() + "\n```"
    assert _parse_json_response(content) == _eval_dict()


def test_parse_empty_raises():
    with pytest.raises(Exception):
        _parse_json_response("")
    with pytest.raises(Exception):
        _parse_json_response("   \n  ")


def test_parse_non_json_raises():
    """纯散文(无任何 JSON 对象)→ 抛出,由调用方兜底。"""
    with pytest.raises(Exception):
        _parse_json_response("这段不是 JSON:审计发现三个高危问题……")


def test_parse_json_array_raises():
    """输出是纯 JSON 数组(无评估对象)→ 抛出(下游依赖 dict 结构)。"""
    with pytest.raises(Exception):
        _parse_json_response('[1, 2, 3]')


# ============================================================
# 散文夹带 / 多 JSON 片段
# ============================================================

def test_parse_json_with_prose_around():
    content = "好的,以下是评估结果:\n" + _eval_json() + "\n以上是本轮结论。"
    assert _parse_json_response(content) == _eval_dict()


def test_parse_json_after_decoy_object():
    """散文中先出现的示例对象不应干扰真正评估结果的提取。"""
    content = '先给个例子 {"x": 1}\n真正结果:' + _eval_json()
    result = _parse_json_response(content)
    assert result["covered"] == ["a"]
    assert result["done"] is True


def test_parse_fenced_truncated_no_closing_fence():
    """```json 包裹但输出被截断(无尾围栏)→ 仍能提取+修复。"""
    full = _eval_json(results=[{"title": "t", "content": "内容较长"}])
    content = "```json\n" + full[: len(full) - 10]
    result = _parse_json_response(content)
    assert result["covered"] == ["a"]


# ============================================================
# 截断修复:max_tokens 打满导致 JSON 中途被切断
# ============================================================

def test_parse_truncated_inside_string():
    """截断在字符串值中间 → 补闭引号+括号,已生成字段保留。"""
    full = _eval_json(results=[
        {"title": "SSRF 漏洞", "content": "攻击者可以利用该漏洞访问内网"},
        {"title": "XSS", "content": "跨站脚本"},
    ])
    # 截断在第二条 content 中间
    cut = full.find("跨站") + 1
    result = _parse_json_response(full[:cut])
    assert result["covered"] == ["a"]
    assert result["done"] is True
    assert result["results"][0]["title"] == "SSRF 漏洞"


def test_parse_truncated_after_comma():
    result = _parse_json_response('{"covered": ["a"], "missing": [')
    assert result["covered"] == ["a"]
    assert result["missing"] == []


def test_parse_truncated_dangling_key():
    result = _parse_json_response('{"covered": ["a"], "miss')
    assert result["covered"] == ["a"]


def test_parse_truncated_dangling_colon():
    result = _parse_json_response('{"covered": ["a"], "missing":')
    assert result["covered"] == ["a"]


def test_parse_truncated_literal_fragment():
    """值只写了一半(tru 等字面量碎片)→ 已生成的关键字段仍保留。"""
    result = _parse_json_response('{"covered": ["a"], "done": tru')
    assert result["covered"] == ["a"]


def test_parse_truncated_nested_grouping():
    """嵌套结构(grouping)中途截断 → 外层与已完成的字段保留。"""
    text = '{"covered":["a"],"grouping":{"field":"sev","values":[{"value":"high"'
    result = _parse_json_response(text)
    assert result["covered"] == ["a"]
    assert result["grouping"]["field"] == "sev"


def test_parse_unrepairable_raises():
    """只有开头括号、无任何内容 → 修复后为空对象,抛出走兜底。"""
    with pytest.raises(Exception):
        _parse_json_response('{"')
