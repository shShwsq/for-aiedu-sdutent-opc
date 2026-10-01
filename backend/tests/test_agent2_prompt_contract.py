"""agent2 system prompt 输出契约锚点测试

阶段重构(检查助手移居侧栏 + 结果清单改重点与知识点)后,AGENT2_SYSTEM_PROMPT
的关键契约:
- done=true 时 results 为重点与知识点形态(3-8 条精选,learning_note 必有)
- grouping 默认 null
- "敢不敢上线"不再是无条件硬性要求(仅用户意图涉及上线决策时)
- 核查优先、追问兜底原则保留

本测试锚定这些关键词,防止后续误改导致契约回归。
"""
import pytest

from app.agents.agent2 import AGENT2_SYSTEM_PROMPT


def test_prompt_contains_knowledge_results_contract():
    """results 契约:重点与知识点形态 + learning_note + practice_worthy。"""
    assert "重点与知识点" in AGENT2_SYSTEM_PROMPT
    assert "3-8" in AGENT2_SYSTEM_PROMPT
    assert "learning_note" in AGENT2_SYSTEM_PROMPT
    assert "practice_worthy" in AGENT2_SYSTEM_PROMPT
    # 不是全量发现清单的语义
    assert "不是全量发现清单" in AGENT2_SYSTEM_PROMPT


def test_prompt_grouping_defaults_to_null():
    """grouping 契约:默认输出 null(平铺)。"""
    assert "默认输出 null" in AGENT2_SYSTEM_PROMPT
    assert "默认 null" in AGENT2_SYSTEM_PROMPT


def test_prompt_ship_conclusion_is_conditional():
    """「敢不敢上线」结论:仅当用户意图涉及上线/采用决策时才要求,非硬性。"""
    assert "仅当用户意图" in AGENT2_SYSTEM_PROMPT
    # 旧的硬性要求表述已移除
    assert "必须给出「敢不敢上线" not in AGENT2_SYSTEM_PROMPT


def test_prompt_keeps_verify_first_followup_fallback():
    """核查优先、追问兜底原则保留。"""
    assert "核查优先、追问兜底" in AGENT2_SYSTEM_PROMPT
    assert "最后手段" in AGENT2_SYSTEM_PROMPT


def test_prompt_behind_scenes_positioning():
    """幕后质检定位:agent1 是台前回答者,agent2 过程经侧栏呈现。"""
    assert "幕后质检" in AGENT2_SYSTEM_PROMPT
    assert "台前回答者" in AGENT2_SYSTEM_PROMPT
    assert "侧栏" in AGENT2_SYSTEM_PROMPT


def test_prompt_reference_and_verify_sections_kept():
    """引用复核与动态验证章节保留(核查手段不变)。"""
    assert "引用复核" in AGENT2_SYSTEM_PROMPT
    assert "动态验证" in AGENT2_SYSTEM_PROMPT
    assert "check_reference" in AGENT2_SYSTEM_PROMPT
