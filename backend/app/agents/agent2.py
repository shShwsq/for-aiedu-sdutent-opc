"""agent2:质检智能体(检查助手,SecondLook 双 agent 架构核心)

角色:扮演严谨的质量审查官,质检 agent1(执行智能体,"AI助手")的审查结果。
按决策文档,agent2 承担三项职责:
1. 质检审查结果:核实 agent1 的发现是否有真实源码依据、严重度是否合理、
   有无误报或遗漏关键维度(必要时用只读工具读源码核对)
2. 必要时追问补全:针对不足构造 followup_query 让 agent1 再跑一轮
3. 整理审查报告与结论:覆盖完整、质量合格后输出结构化结果(results +
   grouping)与「敢不敢上线」结论;任务完成后还负责生成练习题与知识点
   (实现位于 app/services/practice/,由 orchestrator 在任务完成时调用)

设计要点(继承自原项目 user_agent 的成熟机制):
- agent2 不直接执行审查,只做质检与追问;但可用**只读工具**
  (read_file / list_files / find_files / search_code)核对真实源码,
  以及可选的 verify 工具生成 PoC 动态验证安全问题(经 verifier_agent)
- 输出结构化 JSON:covered / missing / followup_query / done
  + done=true 时输出 grouping(结果分组声明)与 results(结构化结果)
- done=true 表示质检通过,agent2 认为任务可以结束
- 覆盖度清单(初始评估/用户确认)机制已移除:agent2 在每次评估时
  根据用户意图自行确定应覆盖的审查维度,在 covered/missing 中
  按维度 id 标注覆盖情况,跨轮记忆保证判断连续性

流程(任务开始时不再有 agent2 初始评估,agent1 直接按用户意图执行):
1. agent1 跑一轮,返回 summary
2. agent2 质检 agent1 的总结(可读源码核对):
   - 哪些维度质检通过(covered)/ 哪些维度不合格或缺失(missing)
   - 针对 missing 维度构造 followup_query 追问补全
3. 若 missing 为空或 done=true,任务结束(done 时输出 results + grouping)
4. 否则把 followup_query 发给 agent1 再跑一轮
5. 循环 1-4,最多 MAX_ROUNDS 轮
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from json_repair import repair_json
from sqlalchemy.orm import Session

from app.domain_events import VERIFIER_COMPLETED, emit
from app.event_bus import publish
from app.llm.client import LLMClient
from app.models.task import Conversation, Task

logger = logging.getLogger(__name__)


# 最大追问轮次(防止死循环)
MAX_ROUNDS = 4

# 跨轮记忆传递:agent2 之前各轮评估的单条最大字符数与总字符数上限
# 与 agent1 的对应常量保持一致,避免两边不一致
MAX_HISTORY_MSG_CHARS = 3000
MAX_HISTORY_TOTAL_CHARS = 12000

# 解析失败兜底时,展示在最终总结里的 agent2 输出原文截断上限
MAX_RAW_OUTPUT_CHARS = 3000

# agent2 评估调用的单次输出上限。done=true 时需输出 results+grouping
# 大 JSON,2048 容易被截断导致解析失败;16384 预留足够余量。
# 实际上限还会被 LLMClient.max_output_tokens 按模型输出能力钳制
UA_EVAL_MAX_TOKENS = 16384

# 单次评估中最多调用只读工具的次数(防止读文件循环失控)
MAX_READ_TOOL_CALLS = 12

# 单次评估中最多调用 verifier_agent 的次数(防止无限验证)
MAX_VERIFY_CALLS = 3

# ============================================================
# agent2 的只读核查工具定义(工作区可用时启用,repo_path 由后端注入)
# ============================================================

_READ_ONLY_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取任务工作区内文件内容,返回带行号的内容(cat -n 格式),支持分页。"
                "质检 agent1 的发现时,用真实源码核对其说法是否成立"
                "(如声称的漏洞代码、行号、上下文)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "工作区内相对路径",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "本次最多返回行数,默认 200",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "从第几行开始读(1-based),默认 1",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "列出工作区内某目录下的文件和子目录(单层,不递归)。"
                "用于核对 agent1 声称检查过的文件是否真实存在。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subdir": {
                        "type": "string",
                        "description": "工作区内相对路径,默认根目录",
                    },
                    "max_entries": {
                        "type": "integer",
                        "description": "最大返回条目数,默认 200",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "按文件名 pattern 递归查找工作区内的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "文件名 glob 模式(如 *.py)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大返回条数,默认 100",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "在工作区按内容正则搜索代码。核实 agent1 的发现"
                "(如声称某输入未参数化)时,用关键词定位真实代码。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "正则表达式",
                    },
                    "file_glob": {
                        "type": "string",
                        "description": "限定文件名 glob(如 *.py),可选",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "匹配行前后各显示 N 行(建议 3-5),默认 0",
                    },
                    "max_matches": {
                        "type": "integer",
                        "description": "最多返回匹配数,默认 50",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]

# verify 工具定义(agent2 可选调用,仅当 task.verifier_enabled 且 policy.allow_verify 时启用)
_VERIFY_TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "verify",
        "description": (
            "在已部署的测试环境动态验证 agent1 发现的安全问题是否真实可利用。"
            "传入需要验证的安全发现描述,系统会自动构造 PoC 发送到测试环境验证。"
            "验证完成后你会收到验证结果(已确认/未确认/误报 + 证据),据此调整质检结论。"
            "适用于:静态分析疑似但不确定的漏洞、需要实际触发确认的注入/认证绕过等。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verification_request": {
                    "type": "string",
                    "description": (
                        "需要验证的安全发现描述,应包含:漏洞类型、代码位置、攻击思路。"
                        "如'验证 src/api/users.py 第 42 行的 SQL 注入:用户输入 username "
                        "未参数化直接拼接到 SQL,尝试用 ' OR 1=1-- 验证'"
                    ),
                },
            },
            "required": ["verification_request"],
        },
    },
}


# ============================================================
# 通用 system prompt(质检官人设)
# ============================================================

AGENT2_SYSTEM_PROMPT = """你是 agent2(质检智能体),扮演一位严谨的质量审查官。

## 你的职责
你负责质检 agent1(执行智能体)的审查结果,面向"敢不敢用 AI 产出物"的验收目标:
1. **核实**:agent1 的发现是否有真实源码依据、严重度是否合理、有无误报或夸大;
   有只读工具时必须抽查关键发现对应的真实代码,不要凭 agent1 的说法臆断。
2. **查漏**:判断是否遗漏了任务应有的关键审查维度。
3. **补全**:发现不足时,构造 followup_query 让 agent1 再跑一轮补全。
4. **结论**:覆盖完整、质量合格后宣布结束,整理结构化审查报告(results),
   并给出「敢不敢上线/敢不敢用」的明确结论(放进 reasoning)。

## 质检基准维度
本任务没有预定义的覆盖度清单。你需要根据用户意图自行确定本任务
**应覆盖哪些审查维度**(3-8 个为宜,维度 id 用英文下划线命名,如
injection / readability / contract_terms),并在 reasoning 中简要说明
你采用的维度。后续各轮保持维度 id 稳定,保证覆盖判断可跨轮延续。

## 工作流程
1. **协作轮(每轮)**:对照你确定的审查维度质检 agent1 的总结,
   标记质检通过(covered)和不合格/缺失(missing)的维度,
   针对未通过维度构造 followup_query 追问补全。
2. **结束**:所有维度质检通过(done=true)或已达最大轮次时,
   整理结构化结果(results)并声明结果分组方式(grouping)。

## 输出格式(严格 JSON)

### 协作轮输出:
```json
{
  "covered": ["dim_id1"],
  "missing": ["dim_id2"],
  "reasoning": "质检结论:为什么这些维度通过,那些未通过(含核实依据/误报判断)",
  "followup_query": "针对 missing 维度的追问补全指令(空字符串若 done)",
  "done": false
}
```

### done=true 时的输出(附加 results + grouping):
```json
{
  "covered": ["所有维度id"],
  "missing": [],
  "reasoning": "最终质检结论(含「敢不敢上线/敢不敢用」的明确判断)",
  "followup_query": "",
  "done": true,
  "results": [
    {"title": "结果标题", "content": "结果详细内容", "metadata": {"自定义字段": "值"}}
  ],
  "grouping": {
    "field": "metadata中的分组字段名",
    "type": "ordered",
    "values": [{"value": "值", "label": "显示名", "color": "颜色key", "order": 1}],
    "default_label": "其他",
    "default_color": "unknown"
  }
}
```
grouping 可为 null(不分组,平铺展示)。非 null 时各字段说明:

- **field**:必填。从 result.metadata 取该字段的值作为分组 key。
- **type**:必填,`ordered` 或 `dynamic`。
  - `ordered`:**固定枚举 + 顺序**。适合分类维度可预知的场景
    (如安全审计的 severity: high/medium/low/info)。
    按 `values` 数组中 `order` 字段排序展示,未匹配枚举的结果归入 default 组。
    **values 必填且需列全所有可能的枚举值**。
  - `dynamic`:**按 metadata 实际值动态分组**。适合分类值不可预知的场景
    (如按 file_path 分组,文件名无数种可能)。
    按 results 实际出现的值现分,无固定顺序。**values 可省略**(填了也不读)。
- **values**:ordered 必填。每项含 `value`(原始值)、`label`(展示名)、
  `color`(颜色 key,可选 high/medium/low/info/critical/unknown)、`order`(展示顺序,数字)。
- **default_label**:必填。metadata 缺失该字段时的分组展示名(如"其他")。
- **default_color**:必填。default 组的颜色 key,可选 high/medium/low/info/critical/unknown。

**选 type 的判断准则**:
- 1-8 个固定分类(严重程度、优先级、类型)→ 用 `ordered`
- 文件名/模块名/标签等开放集合 → 用 `dynamic`
- 不确定是否固定 → 用 `dynamic`(更安全)

## 审查维度确定原则
- 根据用户意图自适应:代码审查任务覆盖安全漏洞/隐性成本(失控 API 调用、
  死循环烧钱逻辑)/可维护性/能否交付上线等维度,合同任务覆盖权责对等/
  付款违约/知识产权/霸王条款等维度,其他任务按语义生成。
- 维度应覆盖该任务类型的主要风险点,不遗漏重要类别。

## 质检原则(协作轮)
- 基于 agent1 的总结 + 你自己读到的源码证据做判断,不要臆测未提及的维度已覆盖。
- 对关键发现(高危漏洞、上线阻塞项)优先用只读工具核实真实性;
  明显合理的低风险结论可以采信,不要为读而读。
- missing 列表为空是 done 的必要条件,但非充分条件——还需结果质量足够
  (无严重误报、结论有依据、严重度标注合理)。
- followup_query 应具体可执行,指明 agent1 需要补充哪些维度的分析、
  修正哪些误报或重新核实哪些结论。
- 保持质检判断连续性:之前已标 covered 的维度,本轮若 agent1 未推翻,继续保持。
- 「本轮执行中的检查点观察」是你在执行过程中做出的实时判断,质检应与之保持一致性。
- 若检查点已发出纠正指令且 agent1 总结显示已响应,followup_query 不要重复该指令。
- 「最后一次检查点之后的工具调用明细」是最近的原始证据,可用于校验总结的真实性;
  更早的工具细节未传入,以检查点结论和各轮总结为准。

## 结果整理原则(done=true 时)
- results 从 agent1 各轮总结中提取结构化发现,并融入你质检核实的结论
  (误报剔除、严重度校准、大白话解释)。
- 每条 result 含 title(简短标题)、content(详细内容)、metadata(自定义字段)。
  面向创业者的报告,metadata 建议包含:
  - severity(严重程度:high/medium/low/info,便于风险排序)
  - explanation(大白话解释:为什么这是坑、有什么后果)
  - suggestion(修复建议)
  - file_path / line(源码定位,便于跳转)
- reasoning 必须给出「敢不敢上线/敢不敢用」的明确结论
  (如"修复 2 个高危问题前不建议上线")。
- grouping 声明前端如何分组展示:field 指定 metadata 中的分组字段,
  type 决定分组模式(ordered 固定枚举+顺序 / dynamic 按实际值动态分)。
  无明确分组维度时 grouping=null。
- **典型场景参考**:
  - 安全审计 → `ordered` 按 severity(high/medium/low/info)分组,高危在前
  - 代码审查 → `ordered` 按 category(可读性/正确性/性能/安全)分组
  - 文件级分析 → `dynamic` 按 file_path 分组(文件名集合开放)

## 只读核查工具(工作区可用时提供)
如果提供了只读工具(read_file / list_files / find_files / search_code),
质检时可用它们核对真实源码:声称的漏洞代码是否属实、行号与上下文是否对得上、
关键入口是否真的没有防护。工具参数里的路径都是工作区内相对路径。

## 动态验证(可选,有 verify 工具时)
如果任务配置了测试环境,你可以调用 `verify` 工具动态验证 agent1 发现的安全问题:
- 对静态分析疑似但不确定的漏洞,调 verify 发送 PoC 到测试环境确认
- 验证结果会作为 tool_result 返回给你,据此在 results 中标注"已确认可利用"或"误报"
- 不要对每个发现都验证,只验证关键的、不确定的;已明确的问题不需要验证
- 验证结果应反映在 results 的 metadata 中(如加 verified: true/false 字段)
"""


# ============================================================
# 工具调用窗口构造(完整评估注入)
# ============================================================


def build_tool_window_section(
    db: Session,
    task_id,
    round_idx: int,
    boundary: datetime | None,
    *,
    title: str,
    max_calls: int = 30,
    max_chars: int = 6000,
    result_limit: int = 300,
) -> str:
    """构造指定时间窗口内的工具调用明细段落

    查询本轮 boundary 之后(含;None=整轮)的 react_agent tool_call/tool_result
    记录,格式化为"意图行 + 结果摘要":
    - tool_call 只取 content 首行(工具意图),丢弃参数 JSON 详情
    - tool_result 紧随其后截断至 result_limit 字符
    - 超 max_calls 条 tool_call 或总长超 max_chars 时从最早丢弃(尾部最新最有价值)

    builtin 与 CLI(acp_base)执行器落库格式一致(role=agent1、首行意图),
    两条执行路径均可用。无记录返回空串。
    """
    q = db.query(Conversation).filter(
        Conversation.task_id == task_id,
        Conversation.round_idx == round_idx,
        Conversation.role == "agent1",
        Conversation.type.in_(["tool_call", "tool_result"]),
    )
    if boundary is not None:
        q = q.filter(Conversation.created_at >= boundary)
    convs = q.order_by(Conversation.created_at).all()
    if not convs:
        return ""

    # 逐条格式化:tool_call 取首行意图,tool_result 截断作结果摘要
    items: list[tuple[bool, str]] = []  # (is_tool_call, formatted_line)
    for c in convs:
        content = (c.content or "").strip()
        if not content:
            continue
        if c.type == "tool_call":
            items.append((True, f"- {content.splitlines()[0]}"))
        else:
            summary = content[:result_limit]
            if len(content) > result_limit:
                summary += "[...truncated...]"
            items.append((False, f"  结果摘要: {summary}"))

    # 兜底裁剪:tool_call 条数超限 / 总长超限,均从最早丢弃
    while sum(1 for is_call, _ in items if is_call) > max_calls and items:
        items.pop(0)
    while items and sum(len(t) for _, t in items) > max_chars:
        items.pop(0)
    # 裁剪后若开头残留孤立的 tool_result(其 tool_call 已被丢),一并丢弃
    while items and not items[0][0]:
        items.pop(0)
    if not items:
        return ""

    return title + "\n" + "\n".join(text for _, text in items)


# ============================================================
# agent2 执行入口
# ============================================================


def run_agent2(
    user_intent: str,
    agent1_summaries: list[dict[str, Any]],
    task_id: UUID | str,
    db: Session | None = None,
    round_idx: int = 1,
    scenario_id: str = "general",
    client: LLMClient | None = None,
    user_id: UUID | None = None,
    repo_url: str | None = None,
    task: Task | None = None,
    agent_policy: dict[str, Any] | None = None,
    repo_path: str | None = None,
) -> dict[str, Any]:
    """执行一次 agent2 质检评估

    参数:
        user_intent: 用户原始意图(如"审查这个项目: ...")
        client: 可选的 LLMClient(从用户配置构造),None 时回退到 env 默认
        agent1_summaries: agent1 之前几轮的执行结果列表
            每个元素:{"round": 1, "summary": "..."}
        task_id: 任务 ID(必填,用于推送 thinking_delta 事件)
        db: 数据库会话(可选)。传入时用于加载 agent2 自己之前各轮的评估记录,
            让 agent2 跨轮记住 covered/missing 判断,避免反复摇摆。
        round_idx: 当前协作轮次(从 1 起,agent1 执行后的评估轮)
        scenario_id: 场景标识(仅作模板标识,不再驱动 prompt)
        task: 任务对象(可选)。传入时用于读取 verifier 配置(test_env_url / verifier_enabled)。
        agent_policy: agent 策略(可选)。含 allow_verify 开关,控制是否启用 verify 工具。
        repo_path: 任务工作区路径(可选)。传入时启用只读核查工具,
            agent2 可读真实源码核对 agent1 的发现。

    返回:agent2 的结构化输出
        {
            "covered": [...],
            "missing": [...],
            "reasoning": str,
            "followup_query": str,
            "done": bool,
            "results": [...],           # 仅 done=true 时输出
            "grouping": {...} | null,    # 仅 done=true 时输出
        }
    """
    system_prompt = AGENT2_SYSTEM_PROMPT

    # 长期记忆注入:User Profile + 全局记忆 + 项目记忆精简版
    # (仅当有内容时,追加到 system prompt 末尾)
    # user_id 为 None(匿名任务)或无配置时 build_*_section 返回空串,不影响原 prompt
    if db is not None and user_id is not None:
        from app.services.memory_injection import build_agent2_memory_section
        _memory_section = build_agent2_memory_section(db, user_id, repo_url)
        if _memory_section:
            system_prompt = system_prompt + "\n\n" + _memory_section

    # 构造 user 消息:包含用户意图 + agent1 之前的所有摘要
    if not agent1_summaries:
        # 兜底:agent1 尚无总结(异常/降级路径)。任务开始时不再有初始评估,
        # agent1 直接按用户意图执行;正常协作轮不会走到这里。
        user_msg = (
            f"用户原始意图:{user_intent}\n\n"
            f"这是任务开始,agent1 尚未执行。"
            f"请输出 followup_query 给 agent1 的执行指令。done=false。"
        )
    else:
        # 协作轮:把 agent1 的自然语言总结给 agent2 质检
        # 注意:agent1 只输出自然语言 summary,不再有结构化 results 字段
        rounds_text = []
        for i, r in enumerate(agent1_summaries, 1):
            summary = r.get("summary", "(无 summary)")
            rounds_text.append(
                f"### 第 {i} 轮 agent1 自然语言总结\n{summary}"
            )

        # 跨轮记忆注入:agent2 看到自己之前各轮的评估记录,
        # 避免在 covered/missing 之间反复摇摆(第 2 轮起注入)
        history_prefix = ""
        if db is not None and round_idx >= 2:
            history_prefix = _build_agent2_history(db, task_id, round_idx)

        user_msg_parts = [
            f"用户原始意图:{user_intent}\n",
        ]
        if history_prefix:
            user_msg_parts.append(history_prefix)
        user_msg_parts.append(
            f"\n以下是 agent1 已执行的 {len(agent1_summaries)} 轮自然语言总结:\n\n"
            + "\n\n".join(rounds_text)
        )

        # 本轮工具调用明细(截尾窗口注入):给原始证据,
        # 供校验总结真实性、避免重复追问
        if db is not None:
            try:
                tool_section = build_tool_window_section(
                    db, task_id, round_idx, None,
                    title="[本轮全部工具调用明细(截尾)]",
                )
                if tool_section:
                    user_msg_parts.append("\n" + tool_section)
            except Exception as e:
                logger.warning(
                    f"[task={task_id}] 加载工具窗口注入失败(跳过): {e}"
                )

        user_msg_parts.append(
            "\n\n请质检覆盖情况与结论质量,决定是否追问补全或结束。"
        )
        if history_prefix:
            user_msg_parts.append(
                "\n[记忆提示] 上面已附上你之前各轮的评估记录,请保持质检判断的连续性:"
                "之前已标 covered 的类别,本轮若 agent1 未推翻结论,继续保持 covered,"
                "不要无意义反复追问。"
            )
        user_msg = "\n".join(user_msg_parts)

    # 调 LLM(流式)
    client = client or LLMClient()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    # 判断是否启用 verify 工具
    # 条件:task 配了 verifier_enabled + agent_policy.allow_verify + 有 test_env_url
    verify_enabled = (
        task is not None
        and task.verifier_enabled
        and bool(task.test_env_url)
        and (agent_policy or {}).get("allow_verify", False)
    )
    tools = []
    if repo_path:
        tools.extend(_READ_ONLY_TOOL_DEFINITIONS)
    if verify_enabled:
        tools.append(_VERIFY_TOOL_DEFINITION)
    tools = tools or None

    # LLM 调用循环:处理只读核查/verify 工具调用(结果回灌后再调 LLM 输出 JSON 评估)
    # 流式调用失败降级:首次失败重试一次;重试仍失败返回降级结果
    # (orchestrator 据此直接把用户输入内容交给 agent1 执行,不杀死任务)
    content = ""
    read_tool_count = 0
    verify_count = 0
    reasoning_parts: list[str] = []  # 各次流式调用的真实思考链(含工具循环)
    degraded_error: Exception | None = None
    while True:
        try:
            content, tool_calls, reasoning_chunk = _stream_agent2_llm(
                client, messages, task_id=task_id, round_idx=round_idx, tools=tools
            )
        except Exception as e:
            logger.warning(
                f"[task={task_id}] agent2 流式调用失败(第 1 次): {e},将重试一次"
            )
            try:
                content, tool_calls, reasoning_chunk = _stream_agent2_llm(
                    client, messages, task_id=task_id, round_idx=round_idx, tools=tools
                )
            except Exception as e2:
                degraded_error = e2
                logger.exception(
                    f"[task={task_id}] agent2 流式调用重试仍失败,降级直连 agent1"
                )
                break
        if reasoning_chunk:
            reasoning_parts.append(reasoning_chunk)

        # 无工具调用 → content 是 JSON 评估结果,跳出循环
        if not tool_calls:
            break

        # 有工具调用:把 assistant 消息(含 tool_calls)加回 messages
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
        assistant_msg["tool_calls"] = [
            {
                "id": tc["id"] or f"call_{tc['index']}",
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments_str"]},
            }
            for tc in tool_calls
        ]
        messages.append(assistant_msg)

        for tc in tool_calls:
            fn_name = tc["name"]
            try:
                args = json.loads(tc["arguments_str"]) if tc["arguments_str"] else {}
            except json.JSONDecodeError:
                args = {}

            # ---- 只读核查工具 ----
            if fn_name in ("read_file", "list_files", "find_files", "search_code"):
                if not repo_path:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"] or f"call_{tc['index']}",
                        "content": "[工具不可用: 当前任务没有可访问的工作区]",
                    })
                    continue
                if read_tool_count >= MAX_READ_TOOL_CALLS:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"] or f"call_{tc['index']}",
                        "content": (
                            f"已达只读工具调用上限({MAX_READ_TOOL_CALLS}),"
                            "跳过本次核查,请基于已有证据给出评估。"
                        ),
                    })
                    continue
                read_tool_count += 1
                tool_result_str = _execute_read_tool(
                    fn_name, args, repo_path, str(task_id),
                    db=db, task=task, round_idx=round_idx,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"] or f"call_{tc['index']}",
                    "content": tool_result_str,
                })
                continue

            # ---- verify 工具(PoC 动态验证) ----
            if fn_name != "verify":
                # 未知工具调用:返回错误让 LLM 知道
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"] or f"call_{tc['index']}",
                    "content": f"[不支持的工具: {fn_name}]",
                })
                continue

            if verify_count >= MAX_VERIFY_CALLS:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"] or f"call_{tc['index']}",
                    "content": f"已达验证次数上限({MAX_VERIFY_CALLS}),跳过本次验证。",
                })
                continue

            verify_count += 1
            verification_request = args.get("verification_request", "")

            # 调用 verifier_agent 执行动态验证
            logger.info(
                f"[task={task_id}] agent2 调用 verify(第 {verify_count} 次),"
                f"目标: {verification_request[:200]}"
            )
            try:
                from app.agents.verifier_agent import run_verifier_agent
                verify_result = run_verifier_agent(
                    task, db, verification_request, client, round_idx
                )
                # 领域事件:一次动态验证完成(成功路径)
                emit(
                    VERIFIER_COMPLETED, task_id,
                    round_idx=round_idx, attempt=verify_count, success=True,
                    request=verification_request[:200],
                )
            except Exception as e:
                logger.exception(f"[task={task_id}] verifier_agent 执行失败")
                verify_result = f"[验证失败: {e}]"
                # 领域事件:一次动态验证失败(异常路径)
                emit(
                    VERIFIER_COMPLETED, task_id,
                    round_idx=round_idx, attempt=verify_count, success=False,
                    request=verification_request[:200], error=str(e)[:300],
                )

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"] or f"call_{tc['index']}",
                "content": verify_result,
            })

        # 循环回去:LLM 看到工具结果后,要么再调工具,要么输出 JSON 评估

    # 流式调用降级:重试仍失败时返回降级结果(不抛异常杀死任务)。
    # orchestrator 检测到 degraded=true 后,resume 首次分析直接把用户输入
    # 内容交给 agent1 执行;协作轮评估降级则直接以当前进度收尾结束。
    if degraded_error is not None:
        degrade_reason = str(degraded_error) or type(degraded_error).__name__
        logger.info(
            f"[task={task_id}] agent2 流式调用失败已降级"
            f"(round_idx={round_idx}): {degrade_reason}"
        )
        return {
            "covered": [],
            "missing": [],
            "reasoning": (
                f"agent2 流式调用失败(重试仍失败),已降级:\n{degrade_reason}\n\n"
                f"[降级说明] 本轮跳过 agent2 质检,直接把用户输入内容交给 "
                f"agent1 执行。"
            ),
            "followup_query": user_intent,
            "done": False,
            "degraded": True,
            "degrade_reason": degrade_reason,
        }

    # 解析 JSON(LLM 可能输出带 ```json ``` 包裹的)
    try:
        result = _parse_json_response(content)
    except Exception as e:
        logger.error(f"agent2 输出解析失败: {e},raw: {content[:500]}")
        # 兜底:直接宣布完成,避免无意义重跑把所有类别再来一遍(浪费 token)
        # results 留空,orchestrator 落库 0 个结果。
        # reasoning 展示 agent2 输出原文(截断),供用户在最终总结里回查
        # 实际产出——比只留一句"解析失败"更可追溯。
        raw_output = (content or "").strip()
        if len(raw_output) > MAX_RAW_OUTPUT_CHARS:
            raw_output = (
                raw_output[:MAX_RAW_OUTPUT_CHARS]
                + f"\n...(原文过长已截断,共 {len(content)} 字符)"
            )
        result = {
            "covered": [],
            "missing": [],
            "reasoning": (
                f"agent2 输出解析失败({e}),直接结束避免无意义重跑。"
                f"\n\n[agent2 输出原文]\n{raw_output or '(空输出)'}"
            ),
            "followup_query": "",
            "done": True,
        }

    # 落库真实思考链(供前端刷新后还原思考卡片,与 agent1 thinking 同机制)。
    # 不推 SSE:流式期间已通过 thinking_delta 在流式卡片展示,推送会重复。
    # 结构化评估记录仍由 orchestrator._record_agent2 落库(跨轮记忆依赖)。
    reasoning_full = "\n\n".join(p for p in reasoning_parts if p.strip())
    if db is not None and task is not None and reasoning_full:
        try:
            db.add(Conversation(
                task_id=task.id,
                round_idx=round_idx,
                role="agent2",
                type="thinking",
                content="",
                reasoning=reasoning_full,
            ))
            db.commit()
        except Exception as e:
            logger.warning(f"[task={task_id}] 落库 agent2 思考链失败(忽略): {e}")

    return result


# ============================================================
# 只读核查工具执行(经 sandbox_tools,repo_path 后端注入)
# ============================================================


def _execute_read_tool(
    fn_name: str,
    args: dict[str, Any],
    repo_path: str,
    task_id: str,
    db: Session | None = None,
    task: Task | None = None,
    round_idx: int = 0,
) -> str:
    """执行只读核查工具,结果 JSON 序列化返回给 LLM

    同时落库 tool_call / tool_result 对话记录(role="agent2"),
    供前端在对话流看到质检读源码的过程。
    """
    from app.tools import sandbox_tools

    # 工具意图(前端卡片首行)
    intent_map = {
        "read_file": f"核对文件 {args.get('file_path', '?')}",
        "list_files": f"查看目录结构: {args.get('subdir') or '根目录'}",
        "find_files": f"查找文件: {args.get('pattern', '?')}",
        "search_code": f"搜索代码: {args.get('pattern', '?')}",
    }
    intent = f"[agent2 质检] {intent_map.get(fn_name, fn_name)} [{fn_name}]"

    call_conv = None
    if db is not None and task is not None:
        try:
            call_conv = Conversation(
                task_id=task.id,
                round_idx=round_idx,
                role="agent2",
                type="tool_call",
                content=f"{intent}\n{json.dumps(args, ensure_ascii=False, indent=2)}",
            )
            db.add(call_conv)
            db.commit()
            db.refresh(call_conv)
        except Exception as e:
            logger.warning(f"[task={task_id}] 落库 agent2 工具调用失败(忽略): {e}")

    try:
        if fn_name == "read_file":
            result = sandbox_tools.read_file(
                repo_path=repo_path,
                file_path=args.get("file_path", ""),
                max_lines=int(args.get("max_lines", 200)),
                offset=int(args.get("offset", 1)),
                task_id=task_id,
            )
        elif fn_name == "list_files":
            result = sandbox_tools.list_files(
                repo_path=repo_path,
                subdir=args.get("subdir", ""),
                max_entries=int(args.get("max_entries", 200)),
                task_id=task_id,
            )
        elif fn_name == "find_files":
            result = sandbox_tools.find_files(
                repo_path=repo_path,
                pattern=args.get("pattern", ""),
                max_results=int(args.get("max_results", 100)),
                task_id=task_id,
            )
        else:  # search_code
            result = sandbox_tools.search_code(
                repo_path=repo_path,
                pattern=args.get("pattern", ""),
                file_glob=args.get("file_glob"),
                case_sensitive=bool(args.get("case_sensitive", False)),
                max_matches=int(args.get("max_matches", 50)),
                context_lines=int(args.get("context_lines", 0)),
                output_mode=args.get("output_mode", "content"),
                offset=int(args.get("offset", 0)),
                task_id=task_id,
            )
        result_str = json.dumps(result, ensure_ascii=False, default=str)
        err: Exception | None = None
    except Exception as e:
        logger.error(f"[task={task_id}] agent2 只读工具执行失败: {e}")
        result_str = f"工具执行失败: {e}"
        err = e

    if db is not None and task is not None and call_conv is not None:
        try:
            db.add(Conversation(
                task_id=task.id,
                round_idx=round_idx,
                role="agent2",
                type="tool_result",
                content=result_str,
                tool_call_id=str(call_conv.id),
            ))
            db.commit()
        except Exception as e:
            logger.warning(f"[task={task_id}] 落库 agent2 工具结果失败(忽略): {e}")

    # 截断超长结果防上下文爆炸(工具自身已限流,这里是硬兜底)
    if len(result_str) > _MAX_TOOL_RESULT_CHARS:
        result_str = (
            result_str[:_MAX_TOOL_RESULT_CHARS]
            + f"\n...(工具结果过长已截断,共 {len(result_str)} 字符)"
        )
    return result_str


# 单次工具结果回传 LLM 的截断阈值(防上下文爆炸)
_MAX_TOOL_RESULT_CHARS = 3000


# ============================================================
# 流式 LLM 调用:agent2 思考过程实时推送
# ============================================================


def _stream_agent2_llm(
    client: LLMClient,
    messages: list[dict[str, Any]],
    *,
    task_id: UUID | str,
    round_idx: int = 0,
    tools: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]], str]:
    """流式调用 agent2 的 LLM,实时推送 thinking_delta 事件

    支持工具调用(只读核查 / verify):tools 非空时传入 LLM,
    返回的 tool_calls 供调用方处理。
    无 tools 时行为与原来一致(只产出 reasoning + content)。

    返回 (content_full, tool_calls_full, reasoning_full)
        - content_full: 完整回答内容(JSON 格式的结构化评估结果)
        - tool_calls_full: 工具调用列表 [{"id", "name", "arguments_str", "index"}]
        - reasoning_full: 完整思考链(调用方落库,供前端刷新后还原思考卡片)
    """
    conv_id = str(uuid.uuid4())
    reasoning_full = ""
    content_full = ""
    tool_calls_acc: dict[int, dict[str, Any]] = {}

    # 推送流开始事件
    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "agent2",
        "phase": "start",
        "delta": "",
    })

    try:
        for chunk in client.chat_stream(messages, tools=tools, tool_choice="auto", max_tokens=UA_EVAL_MAX_TOKENS):
            # 思考链增量(推给前端流式卡片显示)
            if chunk.reasoning_delta:
                reasoning_full += chunk.reasoning_delta
                publish(task_id, "thinking_delta", {
                    "conv_id": conv_id,
                    "round_idx": round_idx,
                    "role": "agent2",
                    "phase": "reasoning",
                    "delta": chunk.reasoning_delta,
                })

            # 正式回答增量(JSON 结构化评估结果)
            if chunk.content_delta:
                content_full += chunk.content_delta

            # 工具调用增量(跨 chunk 累积)
            if chunk.tool_call_deltas:
                for tc_delta in chunk.tool_call_deltas:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.id or "",
                            "name": tc_delta.name or "",
                            "arguments_str": "",
                            "index": idx,
                        }
                    else:
                        if tc_delta.id and not tool_calls_acc[idx]["id"]:
                            tool_calls_acc[idx]["id"] = tc_delta.id
                        if tc_delta.name and not tool_calls_acc[idx]["name"]:
                            tool_calls_acc[idx]["name"] = tc_delta.name
                    if tc_delta.arguments_fragment:
                        tool_calls_acc[idx]["arguments_str"] += tc_delta.arguments_fragment

            if chunk.finish_reason:
                logger.debug(
                    f"[task={task_id}] agent2 流式结束,finish={chunk.finish_reason}, "
                    f"reasoning={len(reasoning_full)}字符, content={len(content_full)}字符, "
                    f"tool_calls={len(tool_calls_acc)}"
                )
    except Exception as e:
        logger.exception(f"[task={task_id}] agent2 流式调用失败")
        publish(task_id, "thinking_delta", {
            "conv_id": conv_id,
            "round_idx": round_idx,
            "role": "agent2",
            "phase": "error",
            "delta": f"[流式调用失败: {e}]",
        })
        raise

    # 推送流结束事件
    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "agent2",
        "phase": "end",
        "delta": "",
    })

    tool_calls_full = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
    return content_full, tool_calls_full, reasoning_full


# ============================================================
# 辅助函数
# ============================================================


def _build_agent2_history(
    db: Session, task_id, current_round_idx: int,
) -> str:
    """加载 agent2 自己之前各轮的评估记录,作为前缀注入 user_msg

    同一任务内,agent2 每次调用都是无状态的(messages 只含 system + 当前 user)。
    若不做记忆传递,agent2 看不到自己之前几轮的 covered/missing 判断,
    可能在 covered/missing 之间反复摇摆,或忘记之前已认定的覆盖情况。

    本函数从 Conversation 表加载 round_idx < current_round_idx 的
    agent2 type=evaluation 记录,提取其 reasoning(完整评估含 covered/
    missing/判断/追问),拼接成文本。单条截断到 MAX_HISTORY_MSG_CHARS,
    整体超 MAX_HISTORY_TOTAL_CHARS 时按"重要性"保留:
      - 优先保留 missing 非空的轮次(还有未覆盖项,对决策更有参考价值)
      - 其次保留 done=false 的轮次
      - 同优先级内 FIFO 丢最早轮次

    返回字符串(可能为空)。current_round_idx < 2 时返回空(第 1 轮
    之前没有历史评估记录可注入)。
    """
    if current_round_idx < 2:
        return ""

    convs = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.round_idx < current_round_idx,
            Conversation.role == "agent2",
            Conversation.type == "evaluation",
        )
        .order_by(Conversation.round_idx, Conversation.created_at)
        .all()
    )
    if not convs:
        return ""

    # 逐轮构造记忆段
    segments: list[str] = []
    # 同时记录每段的"重要性"(用于超限时裁剪):missing 非空 > done=false > 其他
    priorities: list[int] = []
    for c in convs:
        # reasoning 是 _record_agent2 写入的 full_eval(含 covered/missing/判断/追问)
        text = c.reasoning or c.content or ""
        if not text:
            continue
        text = text[:MAX_HISTORY_MSG_CHARS]

        # 解析重要性(从 reasoning 文本粗判)
        # full_eval 格式:"已覆盖: [...]\n未覆盖: [...]\n判断: ..."
        priority = 0
        if "未覆盖: []" not in text and "未覆盖: []" not in text.replace(" ", ""):
            # missing 列表非空 → 最高优先级
            if "未覆盖:" in text:
                missing_part = text.split("未覆盖:")[1].split("\n")[0]
                if missing_part.strip() and missing_part.strip() != "[]":
                    priority = 2
        if priority == 0 and "→ 宣布完成" not in text:
            # done=false → 中等优先级
            priority = 1

        segments.append(f"=== 第 {c.round_idx} 轮 agent2 评估 ===\n{text}")
        priorities.append(priority)

    if not segments:
        return ""

    # 整体超限时按优先级裁剪:优先级低的先丢;同优先级 FIFO 丢最早
    total = sum(len(s) for s in segments)
    while total > MAX_HISTORY_TOTAL_CHARS and len(segments) > 1:
        # 找最低优先级中最早的一条
        min_priority = min(priorities)
        drop_idx = priorities.index(min_priority)
        dropped = segments.pop(drop_idx)
        priorities.pop(drop_idx)
        total -= len(dropped)

    return "[你之前各轮的评估记录(保持质检判断连续性)]\n" + "\n\n".join(segments)


def _parse_json_response(content: str) -> dict[str, Any]:
    """解析 LLM 输出的 JSON,容忍 markdown 包裹/前后散文/输出截断

    依赖 json_repair:它能修复截断(补未闭合引号/括号)、剥离 markdown
    围栏与前后散文、还原缺引号等常见问题。max_tokens 打满时输出被
    拦腰截断的场景下,通常能保住已生成的完整字段,避免整体解析失败。

    注意:文本中出现多个 JSON 片段(如散文中夹带示例对象)时,json_repair
    返回 list,此时需从中挑选真正的评估结果。
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("LLM 输出为空")

    result = repair_json(text, return_objects=True)
    if isinstance(result, list):
        result = _pick_eval_dict(result)
    if not isinstance(result, dict) or not result:
        raise ValueError("无法从输出中提取有效 JSON 对象")
    return result


def _pick_eval_dict(items: list[Any]) -> dict[str, Any] | None:
    """从 json_repair 返回的多个候选对象中挑选真正的评估结果

    优先取含评估字段(covered/missing/done 等)且排在最后的对象
    (散文示例通常出现在真正结果之前)。
    """
    dicts = [x for x in items if isinstance(x, dict) and x]
    if not dicts:
        return None
    markers = ("covered", "missing", "done", "followup_query", "results", "grouping")
    marked = [d for d in dicts if any(k in d for k in markers)]
    return marked[-1] if marked else dicts[-1]
