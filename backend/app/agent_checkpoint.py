"""agent2 检查点评估:迭代边界的轻量方向纠正

场景:react_agent(含内置 react_agent 和外部 CLI agent)执行过程中,
每 K 个迭代边界,agent2 做一次轻量评估,判断方向是否明显跑偏。
若跑偏,生成追问指令入中断队列,react_agent 下一迭代注入(软中断)。

与 agent2.py 的完整评估区别:
- 完整评估(round 边界):判断 covered/missing,决定是否追问或结束,输出结构化结果
- 检查点评估(迭代边界):只判断"方向是否明显跑偏",不做 covered/missing,
  只在明显跑偏时打断,避免频繁打断影响 react_agent 工作

配置解析:resolve_agent_policy 合并用户级默认 + 任务级覆盖,
get_effective_interval 按执行器类型解析实际生效的 K 值。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.event_bus import publish
from app.llm.client import LLMClient
from app.models.task import Conversation, Task

logger = logging.getLogger(__name__)


# ============================================================
# 默认策略 + 配置解析
# ============================================================

# 协作总轮次上限(可通过环境变量 AGENTPAIR_MAX_ROUNDS_LIMIT 调整,默认 10)
# 前端展示的"最大 10"与此对齐;改环境变量后前端需同步(或未来通过 API 下发)
MAX_MAX_ROUNDS = int(os.environ.get("AGENTPAIR_MAX_ROUNDS_LIMIT", "10"))

DEFAULT_AGENT_POLICY: dict[str, Any] = {
    "agent2_enabled": True,  # 是否启用 agent2(关闭=单 agent 模式,跳过评估/打断/验证)
    "max_rounds": 4,  # agent2 协作总轮次(替代 agent2.py 硬编码 MAX_ROUNDS)
    "checkpoint_interval": 10,  # 统一 K 值,每 K 个迭代评估一次
    "checkpoint_interval_builtin": None,  # 高级:内置专用(null=用统一值)
    "checkpoint_interval_cli": None,  # 高级:CLI 专用(null=用统一值)
    "allow_interrupt": True,  # agent2 是否能打断 react_agent
    "max_interrupts_per_round": 2,  # 每轮最多打断次数(防死锁)
    "allow_verify": False,  # agent2 是否能调用 verifier_agent(需任务配了 test_env_url)
    "verifier_auth_mode_default": "per_action",  # 验证授权默认模式(任务级可覆盖)
    "executor_command_confirm_default": "always_approve",  # 执行智能体命令确认默认模式(任务级 _executor_command_confirm 可覆盖)
}


def resolve_agent_policy(task: Task, db: Session) -> dict[str, Any]:
    """合并用户级默认 + 任务级覆盖,返回最终生效的策略

    优先级:任务级覆盖(task.params["_agent_policy"]) > 用户级默认
    (agent_policies 表) > DEFAULT_AGENT_POLICY

    任务的 user_id 可能为 None(匿名任务),此时只用默认值 + 任务级覆盖。
    """
    defaults = dict(DEFAULT_AGENT_POLICY)

    # 加载用户级默认(若用户已登录且保存过协作策略)
    if task.user_id is not None:
        try:
            from app.models.agent_policy import AgentPolicy
            policy_row = (
                db.query(AgentPolicy)
                .filter(AgentPolicy.user_id == task.user_id)
                .first()
            )
            if policy_row is not None:
                defaults.update(policy_row.to_dict())
        except Exception as e:
            logger.warning(
                f"[task={task.id}] 加载用户级 agent_policy 失败(用默认): {e}"
            )

    # 合并任务级覆盖
    overrides = (task.params or {}).get("_agent_policy") or {}
    merged = {**defaults, **overrides}
    # 钳制 max_rounds 到 [1, MAX_MAX_ROUNDS](防御:前端/老数据可能送超界值)
    try:
        mr = int(merged.get("max_rounds", 4))
        merged["max_rounds"] = max(1, min(mr, MAX_MAX_ROUNDS))
    except (TypeError, ValueError):
        merged["max_rounds"] = 4

    # 把 executor_command_confirm_default 映射到 task.params._executor_command_confirm
    # (若任务级未显式设置 _executor_command_confirm),让 4 个 CLI agent wrapper 能读到
    # 优先级:task.params._executor_command_confirm > executor_command_confirm_default > "always_approve"
    if task.params is not None and not task.params.get("_executor_command_confirm"):
        default_mode = merged.get("executor_command_confirm_default", "always_approve")
        if default_mode in ("always_approve", "per_command"):
            task.params["_executor_command_confirm"] = default_mode

    return merged


def get_effective_interval(policy: dict[str, Any], executor: str) -> int:
    """根据执行器类型解析实际生效的 K 值

    - executor == "builtin":优先 checkpoint_interval_builtin,为 None 时用 checkpoint_interval
    - 其他(CLI agent):优先 checkpoint_interval_cli,为 None 时用 checkpoint_interval
    """
    base_k = int(policy.get("checkpoint_interval", 10))
    if executor == "builtin":
        specific = policy.get("checkpoint_interval_builtin")
        return int(specific) if specific else base_k
    else:
        specific = policy.get("checkpoint_interval_cli")
        return int(specific) if specific else base_k


# ============================================================
# 检查点评估 System Prompt
# ============================================================

CHECKPOINT_SYSTEM_PROMPT = """你是 agent2(质检智能体,即检查助手),正在实时观察 agent1(执行智能体)的执行过程。

## 你的职责
作为质检官,判断 agent1 当前方向是否明显跑偏,是否需要打断纠正。

## 重要约束
- 这是**轻量评估**,不需要做 covered/missing 判断(那个在 round 边界做)
- 只在方向**明显跑偏**时打断,避免频繁打断影响 agent1 工作
- 打断要有充分理由,如:
  - 偏离用户意图(在分析无关的代码/功能)
  - 重复无效操作(反复搜索相同关键词)
  - 遗漏关键维度(明显应该检查但未涉及的维度)
  - 钻牛角尖(在某个细节上耗费过多迭代)
- 不确定是否该打断时,选择不打断(让 agent1 继续)
- **仅观察模式**(用户消息中注明时):照常判断方向,但 interrupt 必须为 false;
  若观察到跑偏,在 reason 中写清偏离了什么、当前在做什么(只记录不干预)
- 若提供了「已确认的覆盖度清单(checklist)」:
  - 用它理解用户意图的完整边界,识别 agent1 长时间只围绕个别维度、
    明显遗漏清单中其他关键维度的情况
  - 检查点不做 covered/missing 覆盖度评估(那是 round 边界完整评估的职责);
    不要因为某维度暂时还没触及就打断,只有方向性明显偏离清单范围时才考虑
- 若提供了「之前的评估记录」:
  - 避免重复发出相同/相近的打断指令(历史已打断过的方向不再重复)
  - 核查上次打断指令是否已被执行(从当前快照判断),未执行时可换更明确的表述再次提醒
  - 历史记录仅供参考,以当前快照为主要判断依据
- 若提供了「上一次检查点以来的工具调用」窗口:
  - 用它核查该区间 agent1 的实际动作:识别重复无效操作
    (相同/相近意图反复出现)、核对上次打断指令是否被执行
  - 判断以当前快照 + 窗口为主;窗口内细节不要逐条复述到 reason 中

## 输出格式(严格 JSON)
```json
{
  "interrupt": false,
  "reason": "当前方向正确,继续",
  "query": null,
  "summary": "agent1 正在分析认证模块,方向符合预期"
}
```

或打断时:
```json
{
  "interrupt": true,
  "reason": "agent1 在深挖 SQL 注入,但用户意图是检查认证授权,且已发现 3 个同类问题,应转向检查认证绕过",
  "query": "已经发现 3 个 SQL 注入问题,现在转向检查认证与授权相关问题",
  "summary": "已发现 3 个 SQL 注入,打断并要求转向认证授权检查"
}
```

字段说明:
- interrupt: 是否打断(true 时 query 必填)
- reason: 判断理由(展示给用户看)
- query: 打断时的追问指令(注入 agent1 作为 user 消息),null 表示不打断。
  必须以用户下达指令的口吻表述,不得提及"agent2""评估者"等身份
  (agent1 不知道评估者的存在),也不要复述评估过程,直接给出指令
- summary: 本次评估的一句话摘要(≤50字,供下次评估参考;打断时写明已发出的指令)
"""


# ============================================================
# 覆盖度清单注入(帮助检查点评估理解用户意图的完整边界)
# ============================================================

# 检查点评估注入 checklist 的总字符数上限(轻量评估,收紧于历史记录的 1500)
MAX_CHECKLIST_CHARS = 1500


def _build_checklist_section(checklist: list[dict[str, Any]] | None) -> str:
    """把已确认的 checklist 格式化成检查点评估的注入段落;无清单返回空串

    与 agent2.py 完整评估的 _format_checklist_for_prompt 风格对齐,
    但去掉 description(检查点只需维度边界,不需细节),控制注入体积。
    """
    if not checklist:
        return ""
    lines = ["已确认的覆盖度清单(checklist,用户已确认;对照它理解用户意图的完整边界):"]
    for cat in checklist:
        name = cat.get("name") or cat.get("id") or "?"
        items = cat.get("checklist") or []
        if items:
            lines.append(f"- {name}:{'; '.join(str(i) for i in items)}")
        else:
            lines.append(f"- {name}")
    while lines and sum(len(x) for x in lines) > MAX_CHECKLIST_CHARS:
        lines.pop(-1)
    if len(lines) <= 1:
        return ""
    return "\n".join(lines) + "\n\n"


# ============================================================
# 历史评估记录注入(K 调大后评估变稀疏,靠历史摘要保持连续性)
# ============================================================

# 检查点评估落库的 content 前缀(用于与 round 边界完整评估区分,两者同为 evaluation type)
_CHECKPOINT_CONTENT_PREFIX = "[检查点评估"

# 用户取消待生效打断后,追加到对应检查点评估记录 content 末尾的标记。
# 作用:1) 下次检查点评估注入历史记录时 agent2 能看到指令被否决;
# 2) 前端侧栏据此把徽标从"已打断"改为"已取消"。
INTERRUPT_CANCEL_MARKER = "[用户已取消该打断,追问指令未下发]"

# 注入的历史评估记录条数上限(取最近 N 条)
MAX_HISTORY_RECORDS = 5

# 注入的历史评估记录总字符数上限(与 react_agent/agent2 截断风格对齐)
MAX_HISTORY_CHARS = 1500


def _load_checkpoint_history(db: Session, task_id, round_idx: int) -> list[dict[str, Any]]:
    """加载本轮(同一 round_idx)之前的检查点评估记录

    与 round 边界完整评估同为 role=agent2/type=evaluation,
    用 content 前缀区分。reasoning 解析失败的兜底记录直接跳过。
    返回记录列表(时间序,含 iteration/interrupt/reason/query/summary)。
    """
    convs = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.round_idx == round_idx,
            Conversation.role == "agent2",
            Conversation.type == "evaluation",
            Conversation.content.like(f"{_CHECKPOINT_CONTENT_PREFIX}%"),
        )
        .order_by(Conversation.created_at)
        .all()
    )
    records: list[dict[str, Any]] = []
    for c in convs:
        record = _extract_checkpoint_record(c)
        if record is not None:
            records.append(record)
    return records


def _extract_checkpoint_record(conv: Conversation) -> dict[str, Any] | None:
    """从检查点评估 Conversation 提取摘要记录;reasoning 解析失败返回 None

    iteration 从 content 前缀 `[检查点评估 · 第N轮迭代M]` 的 M 解析,失败时为 None。
    """
    try:
        parsed = _parse_checkpoint_json(conv.reasoning or "")
    except Exception:
        return None
    iteration = None
    try:
        iteration = int(str(conv.content).split("迭代")[1].split("]")[0])
    except (IndexError, ValueError):
        pass
    return {
        "iteration": iteration,
        "interrupt": parsed["interrupt"],
        "reason": parsed["reason"],
        "query": parsed["query"],
        "summary": parsed["summary"],
        "cancelled": INTERRUPT_CANCEL_MARKER in str(conv.content),
    }


def _build_history_section(records: list[dict[str, Any]]) -> str:
    """把历史评估记录格式化成 prompt 段落;无记录时返回空串

    只取最近 MAX_HISTORY_RECORDS 条,总长超 MAX_HISTORY_CHARS 时从最早的丢弃。
    """
    if not records:
        return ""
    lines: list[str] = []
    for r in records[-MAX_HISTORY_RECORDS:]:
        loc = f"迭代{r['iteration']}" if r.get("iteration") else "早期迭代"
        if r.get("interrupt"):
            # 用户取消过的打断明确标注,避免 agent2 误以为指令已下发
            # 而只核查执行情况,或朝同一方向重复打断
            action = "打断(用户已取消,指令未下发)" if r.get("cancelled") else "打断"
            line = f"- [{loc}] {action} | 摘要:{r.get('summary') or r.get('reason', '')} | 指令:{r.get('query', '')}"
        else:
            line = f"- [{loc}] 继续 | 摘要:{r.get('summary') or r.get('reason', '')}"
        lines.append(line)
    while lines and sum(len(x) for x in lines) > MAX_HISTORY_CHARS:
        lines.pop(0)
    if not lines:
        return ""
    return "你之前的评估记录:\n" + "\n".join(lines) + "\n\n"


# ============================================================
# 工具调用窗口构造(检查点评估 / 完整评估共用)
#
# 分层信息策略:上一次观察点(检查点)之前的区间由检查点结论覆盖,
# 观察点之后的工具调用作为明细窗口注入。窗口量由检查点间隔 K 自然钳制,
# 无检查点时靠条数/字符上限兜底。
# ============================================================

# 完整评估注入本轮检查点观察的上限(比检查点间互参的 5 条/1500 字符放宽)
ROUND_CHECKPOINT_MAX_RECORDS = 10
ROUND_CHECKPOINT_MAX_CHARS = 2400


def build_round_checkpoint_section(db: Session, task_id, round_idx: int) -> str:
    """把本轮全部检查点评估记录格式化成完整评估的注入段落

    供 agent2.py 完整评估调用:让 round 边界评估看到自己在执行过程中
    做出的实时判断(含打断指令),保持评估连续性、避免重复追问。

    与 _build_history_section(检查点间互参,5 条/1500 字符)独立实现,
    上限放宽到 10 条/2400 字符,超限从最早丢弃。
    无记录返回空串。
    """
    records = _load_checkpoint_history(db, task_id, round_idx)
    if not records:
        return ""
    lines: list[str] = []
    for r in records[-ROUND_CHECKPOINT_MAX_RECORDS:]:
        loc = f"迭代{r['iteration']}" if r.get("iteration") else "早期迭代"
        if r.get("interrupt"):
            line = f"- [{loc}] 打断 | 摘要:{r.get('summary') or r.get('reason', '')} | 指令:{r.get('query', '')}"
        else:
            line = f"- [{loc}] 继续 | 摘要:{r.get('summary') or r.get('reason', '')}"
        lines.append(line)
    while lines and sum(len(x) for x in lines) > ROUND_CHECKPOINT_MAX_CHARS:
        lines.pop(0)
    if not lines:
        return ""
    return (
        "[本轮执行中的检查点观察(你自己在执行过程中做出的判断)]\n"
        + "\n".join(lines)
    )


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
    """构造指定时间窗口内的工具调用明细段落(通用构造器,两处复用)

    查询本轮 boundary 之后(含;None=整轮)的 react_agent tool_call/tool_result
    记录,格式化为"意图行 + 结果摘要":
    - tool_call 只取 content 首行(工具意图),丢弃参数 JSON 详情
    - tool_result 紧随其后截断至 result_limit 字符
    - 超 max_calls 条 tool_call 或总长超 max_chars 时从最早丢弃(尾部最新最有价值)

    builtin 与 CLI(acp_base)执行器落库格式一致(role=react_agent、首行意图),
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


def build_tool_tail_section(db: Session, task_id, round_idx: int) -> str:
    """完整评估专用:最后一次检查点之后的工具调用明细(无检查点=整轮截尾)

    boundary 取本轮最后一条检查点记录的 created_at(检查点在迭代边界落库,
    严格早于后续工具调用,时间切分可靠)。
    """
    last_ckpt = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.round_idx == round_idx,
            Conversation.role == "agent2",
            Conversation.type == "evaluation",
            Conversation.content.like(f"{_CHECKPOINT_CONTENT_PREFIX}%"),
        )
        .order_by(Conversation.created_at.desc())
        .first()
    )
    if last_ckpt is not None:
        return build_tool_window_section(
            db, task_id, round_idx, last_ckpt.created_at,
            title="[最后一次检查点之后的工具调用明细]",
        )
    return build_tool_window_section(
        db, task_id, round_idx, None,
        title="[本轮全部工具调用明细(截尾)]",
    )


def _build_checkpoint_tool_window(db: Session, task_id, round_idx: int) -> str:
    """检查点评估专用:上一次检查点至今的工具调用窗口(无上一条=轮起点至今)

    本条检查点尚未落库,本轮"最后一条"检查点记录即"上一条"。
    检查点定位为轻量评估,上限比完整评估收紧(15 条/3500 字符/结果 200 字符)。
    """
    prev_ckpt = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.round_idx == round_idx,
            Conversation.role == "agent2",
            Conversation.type == "evaluation",
            Conversation.content.like(f"{_CHECKPOINT_CONTENT_PREFIX}%"),
        )
        .order_by(Conversation.created_at.desc())
        .first()
    )
    if prev_ckpt is not None:
        return build_tool_window_section(
            db, task_id, round_idx, prev_ckpt.created_at,
            title="[上一次检查点以来的工具调用]",
            max_calls=15, max_chars=3500, result_limit=200,
        )
    return build_tool_window_section(
        db, task_id, round_idx, None,
        title="[本轮开始以来的工具调用(截尾)]",
        max_calls=15, max_chars=3500, result_limit=200,
    )


# ============================================================
# 检查点评估主函数
# ============================================================

def run_agent2_checkpoint(
    task: Task,
    db: Session,
    round_idx: int,
    iteration: int,
    react_snapshot: dict[str, Any],
    client: LLMClient | None,
    *,
    allow_interrupt: bool = True,
) -> dict[str, Any]:
    """轻量评估:判断 react_agent 当前方向是否明显跑偏

    参数:
        task: 任务对象
        db: 数据库会话
        round_idx: 当前协作轮次
        iteration: 当前迭代序号
        react_snapshot: react_agent 当前迭代的快照,含:
            - thinking_summary: str  当前迭代的思考摘要(~500字符)
            - tool_intent: str  最近工具调用的意图(一句话)
            - tool_result_summary: str  最近工具结果摘要(~500字符)
            - plan_status: list[dict]  当前 plan 状态
        client: LLMClient(为 None 时用默认)
        allow_interrupt: 是否允许打断。False=仅观察模式:评估照常做,
            prompt 告知模型只记录不干预;兜底强制 interrupt=False

    返回:
        {"interrupt": bool, "reason": str, "query": str | None, "summary": str}
    """
    client = client or LLMClient()

    # 构造 user 消息:把 react_agent 的快照 + 用户意图放进去
    thinking = (react_snapshot.get("thinking_summary") or "")[:500]
    tool_intent = react_snapshot.get("tool_intent") or "(无工具调用)"
    tool_result = (react_snapshot.get("tool_result_summary") or "")[:500]
    plan_status = react_snapshot.get("plan_status") or []

    plan_text = ""
    if plan_status:
        status_sym = {"pending": "○", "in_progress": "◌", "done": "✓"}
        plan_lines = []
        for s in plan_status:
            sym = status_sym.get(s.get("status", ""), "○")
            plan_lines.append(f"{sym} {s.get('text', '')}")
        plan_text = "\n".join(plan_lines)
    else:
        plan_text = "(无 plan)"

    # 覆盖度清单:让检查点评估理解用户意图的完整边界(识别维度级方向遗漏);
    # 无清单(如第 0 轮尚未确认)时该段为空,不影响评估
    try:
        checklist_section = _build_checklist_section(getattr(task, "checklist", None))
    except Exception as e:
        logger.warning(f"[task={task.id}] 构造检查点 checklist 段落失败(跳过注入): {e}")
        checklist_section = ""

    # 本轮历史评估记录:注入上下文,避免重复打断、核查上次指令是否执行
    try:
        history_section = _build_history_section(
            _load_checkpoint_history(db, task.id, round_idx)
        )
    except Exception as e:
        logger.warning(f"[task={task.id}] 加载检查点历史失败(跳过注入): {e}")
        history_section = ""

    # 仅观察模式提示:引导模型 interrupt 固定输出 false,把观察到的跑偏写进 reason
    observe_note = (
        "【当前为仅观察模式】即使判断方向跑偏,interrupt 也必须为 false,"
        "请在 reason 中写清观察到的偏离(只记录不干预)。\n\n"
        if not allow_interrupt else ""
    )

    # 上一次检查点至今的工具调用窗口:让"重复无效操作"判据有真实数据可查
    # (原来只给最近一条 tool_intent,无法判断重复);查库失败降级为空不阻断评估
    try:
        tool_window_section = _build_checkpoint_tool_window(db, task.id, round_idx)
        if tool_window_section:
            tool_window_section += "\n\n"
    except Exception as e:
        logger.warning(f"[task={task.id}] 加载检查点工具窗口失败(跳过注入): {e}")
        tool_window_section = ""

    user_msg = (
        f"用户原始意图:{task.user_input[:500]}\n\n"
        f"{checklist_section}"
        f"当前协作轮次:第 {round_idx} 轮,第 {iteration} 次迭代\n\n"
        f"{observe_note}"
        f"{history_section}"
        f"{tool_window_section}"
        f"react_agent 当前思考:\n{thinking}\n\n"
        f"最近工具调用:{tool_intent}\n\n"
        f"最近工具结果:\n{tool_result}\n\n"
        f"当前 plan 状态:\n{plan_text}\n\n"
        f"请判断 react_agent 当前方向是否明显跑偏,是否需要打断纠正。"
    )

    messages = [
        {"role": "system", "content": CHECKPOINT_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    # 流式调用(推送 thinking_delta,source=checkpoint 供前端路由到右侧栏展示)
    conv_id = str(uuid.uuid4())
    content_full, reasoning_full = _stream_checkpoint_llm(
        client, messages, task_id=task.id, round_idx=round_idx, iteration=iteration,
        conv_id=conv_id,
    )

    # 解析 JSON
    try:
        result = _parse_checkpoint_json(content_full)
    except Exception as e:
        logger.warning(
            f"[task={task.id}] 检查点评估 JSON 解析失败(iteration={iteration}): {e}"
        )
        result = {
            "interrupt": False,
            "reason": f"检查点评估解析失败,默认不打断({e})",
            "query": None,
            "summary": "",
        }

    # 校验:interrupt=true 时 query 必填
    if result.get("interrupt") and not result.get("query"):
        logger.warning(
            f"[task={task.id}] 检查点评估 interrupt=true 但 query 为空,降级为不打断"
        )
        result["interrupt"] = False
        result["reason"] = result.get("reason", "") + "(query 为空,降级为不打断)"

    # 仅观察模式兜底:模型未遵守提示仍输出 interrupt=true 时强制降级(只记录不干预)
    if result.get("interrupt") and not allow_interrupt:
        logger.warning(
            f"[task={task.id}] 检查点评估 interrupt=true 但处于仅观察模式,降级为不打断"
        )
        result["interrupt"] = False
        result["reason"] = result.get("reason", "") + "(仅观察模式:已记录偏离,未干预)"

    # 落库真实思考链(role=agent2, type=thinking),供刷新后右侧栏还原;
    # content 带检查点前缀,前端据此与完整评估的思考链区分并路由到侧栏。
    # 不推 SSE:流式期间已通过 thinking_delta 在右侧栏展示,推送会重复。
    if reasoning_full.strip():
        try:
            db.add(Conversation(
                task_id=task.id,
                round_idx=round_idx,
                role="agent2",
                type="thinking",
                content=f"[检查点评估 · 第{round_idx}轮迭代{iteration}]",
                reasoning=reasoning_full,
            ))
            db.commit()
        except Exception as e:
            logger.warning(f"[task={task.id}] 落库检查点思考链失败(忽略): {e}")

    # 落库 + 推送 agent_checkpoint 事件;返回评估记录 id,
    # 供调用方 push_interrupt 时携带(用户取消待生效打断时定位该记录追加标记)
    eval_conv_id = _record_checkpoint(
        db, task, round_idx, iteration, result, reasoning=content_full
    )

    result["eval_conv_id"] = str(eval_conv_id) if eval_conv_id else None
    return result


# ============================================================
# 辅助函数
# ============================================================

def _stream_checkpoint_llm(
    client: LLMClient,
    messages: list[dict[str, str]],
    *,
    task_id,
    round_idx: int,
    iteration: int,
    conv_id: str,
) -> tuple[str, str]:
    """流式调用检查点评估 LLM,推送 thinking_delta 事件

    事件带 source=checkpoint 标记,前端据此把思考链路由到任务详情右侧栏
    (检查点评估聚合区),不进主对话流。

    返回 (content_full, reasoning_full):正式输出(JSON)与完整思考链。
    """
    content_full = ""
    reasoning_full = ""

    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "agent2",
        "phase": "start",
        "delta": "",
        "iteration": iteration,
        "source": "checkpoint",
    })

    try:
        for chunk in client.chat_stream(messages, max_tokens=1024):
            if chunk.reasoning_delta:
                reasoning_full += chunk.reasoning_delta
                publish(task_id, "thinking_delta", {
                    "conv_id": conv_id,
                    "round_idx": round_idx,
                    "role": "agent2",
                    "phase": "reasoning",
                    "delta": chunk.reasoning_delta,
                    "iteration": iteration,
                    "source": "checkpoint",
                })
            if chunk.content_delta:
                content_full += chunk.content_delta
            if chunk.finish_reason:
                logger.debug(
                    f"[task={task_id}] 检查点评估流式结束(iteration={iteration}),"
                    f"content={len(content_full)}字符"
                )
    except Exception as e:
        logger.exception(f"[task={task_id}] 检查点评估流式调用失败")
        publish(task_id, "thinking_delta", {
            "conv_id": conv_id,
            "round_idx": round_idx,
            "role": "agent2",
            "phase": "error",
            "delta": f"[检查点评估失败: {e}]",
            "iteration": iteration,
            "source": "checkpoint",
        })
        raise

    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "agent2",
        "phase": "end",
        "delta": "",
        "iteration": iteration,
        "source": "checkpoint",
    })

    return content_full, reasoning_full


def _parse_checkpoint_json(content: str) -> dict[str, Any]:
    """解析检查点评估的 JSON 输出

    LLM 可能输出带 ```json ``` 包裹的,需要容错处理。
    """
    # 去除 markdown 代码块包裹
    text = content.strip()
    if text.startswith("```"):
        # 去掉首行 ```json 或 ```
        lines = text.split("\n")
        lines = lines[1:]  # 去首行
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]  # 去尾行
        text = "\n".join(lines).strip()

    result = json.loads(text)

    # 字段校验 + 默认值
    return {
        "interrupt": bool(result.get("interrupt", False)),
        "reason": str(result.get("reason", "")),
        "query": result.get("query") if result.get("query") else None,
        # 摘要缺失时回退到 reason(兼容旧格式输出)
        "summary": str(result.get("summary") or result.get("reason", ""))[:100],
    }


def _record_checkpoint(
    db: Session,
    task: Task,
    round_idx: int,
    iteration: int,
    result: dict[str, Any],
    *,
    reasoning: str = "",
) -> Any:
    """落库检查点评估结果 + 推送 agent_checkpoint 事件

    落库为 Conversation(role=agent2, type=evaluation),content 记录
    评估摘要,reasoning 记录完整 JSON 输出供回查。返回落库记录 id。
    """
    interrupt = result.get("interrupt", False)
    reason = result.get("reason", "")
    query = result.get("query")

    if interrupt:
        content = (
            f"[检查点评估 · 第{round_idx}轮迭代{iteration}] 打断\n"
            f"理由:{reason}\n"
            f"追问指令:{query}"
        )
    else:
        content = (
            f"[检查点评估 · 第{round_idx}轮迭代{iteration}] 继续\n"
            f"理由:{reason}"
        )

    conv = Conversation(
        task_id=task.id,
        round_idx=round_idx,
        role="agent2",
        type="evaluation",
        content=content,
        reasoning=reasoning,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)

    # 推送 agent_checkpoint 事件(前端展示检查点评估卡片)
    publish(task.id, "agent_checkpoint", {
        "round_idx": round_idx,
        "iteration": iteration,
        "interrupt": interrupt,
        "reason": reason,
        "query": query,
    })

    # 同时推 conversation 事件(让前端对话流也能展示)
    publish(task.id, "conversation", {
        "id": str(conv.id),
        "task_id": str(task.id),
        "round_idx": round_idx,
        "role": "agent2",
        "type": "evaluation",
        "content": content,
        "reasoning": reasoning,
    })

    return conv.id
