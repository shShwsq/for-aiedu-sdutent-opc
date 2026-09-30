"""verifier_agent:动态验证智能体(对用户透明,不暴露此名称)

角色:agent2 调用此 agent 在已部署的测试环境动态验证 react_agent 发现的安全问题。
例如:react_agent 静态分析发现 SQL 注入疑似点,verifier_agent 发送 PoC HTTP 请求
验证是否真的能注入。

工具:
- http_request:向 test_env_url 发送 HTTP 请求(GET/POST/PUT/DELETE + headers + body)
  在沙箱里执行(用 urllib 标准库),后端服务器 IP 不暴露给测试环境
- run_python_code:在沙箱执行 Python(复用 react_agent 沙箱,可 read_file 仓库代码辅助构造 PoC)

授权:
- auth_mode="direct":所有动作直接执行
- auth_mode="per_action":每个 http_request / run_python_code 调用前弹窗让用户确认
  (通过 user_interaction.request_verify_authorization 阻塞等待)

与 react_agent 的关系:
- 独立 ReAct 循环(自己的 messages 列表 + 迭代)
- 复用 react_agent 的沙箱会话(run_python_code 在同一沙箱执行,可访问已 clone 的仓库)
- 独立 LLM 调用(用 agent2 的 LLMClient,因为 verifier 是 agent2 的工具)

调用方:agent2(通过 tool_call 机制)
返回:验证结果文本(供 agent2 下一轮 LLM 调用作为 tool_result 注入)
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.event_bus import publish
from app.llm.client import LLMClient
from app.models.task import Task
from app.tools.verifier_tools import VERIFIER_TOOL_DEFINITIONS, http_request

logger = logging.getLogger(__name__)

# verifier_agent ReAct 循环最大迭代次数
MAX_VERIFIER_ITERATIONS = 10

VERIFIER_SYSTEM_PROMPT = """你是验证智能体,负责在已部署的测试环境动态验证安全发现是否真实可利用。

## 你的职责
react_agent 通过静态分析发现了潜在的安全问题,你需要通过实际发送 HTTP 请求 / 运行 PoC 脚本,
验证这些问题是否真实存在(而非误报)。

## 可用工具
1. **http_request**:向测试环境发送 HTTP 请求(在沙箱里执行,不经后端服务器)
   - method: GET/POST/PUT/DELETE 等
   - path: 相对路径(如 /api/users、/login?next=/)
   - headers: 自定义请求头(如 Content-Type;注意:认证头由 auth_profile 自动注入,不要手动填)
   - body: 请求体(POST/PUT 的 payload)
   - auth_profile: 选择登录身份(可选,对应任务配置的凭证 label);工具自动注入对应认证头
   URL base 固定为任务配置的 test_env_url,你只能指定相对路径。

2. **run_python_code**:在沙箱执行 Python 代码
   - 用于构造复杂 PoC(生成签名、编码 payload、解析响应等)
   - 沙箱与 react_agent 共享,可先 read_file 仓库代码了解接口细节
   - 网络访问受限,HTTP 探测用 http_request 而非 Python(两者都在沙箱里,
     但 http_request 有 URL base 锁定 + 授权拦截,更安全)

## 工作流程
1. 分析 agent2 传入的验证目标(哪些安全问题需要验证)
2. 构造合适的 PoC:先想清楚验证思路,再调工具
3. 观察工具返回结果(状态码、响应体),判断漏洞是否真实
4. 每个发现验证后给出明确结论:已确认 / 未确认 / 误报
5. 所有目标验证完成后,输出总结(不要调工具,直接输出文本)

## 输出格式
验证完成后直接输出自然语言总结,包含:
- 每个验证目标的结论(已确认可利用 / 无法确认 / 确认为误报)
- 关键证据(HTTP 状态码、响应内容片段)
- 建议(是否需要在结果中提升/降低严重级别)

## 注意
- 不要对生产环境造成破坏性影响(避免 DELETE 大量数据等危险操作)
- 优先用最小化的 PoC(如 ' OR 1=1-- 比 DROP TABLE 更合适)
- 如果测试环境不可达或返回异常,如实报告,不要臆测
"""


def run_verifier_agent(
    task: Task,
    db: Session,
    verification_request: str,
    client: LLMClient | None = None,
    round_idx: int = 1,
) -> str:
    """执行一轮动态验证

    参数:
        task: 任务对象(含 test_env_url / verifier_auth_mode 配置)
        db: 数据库会话(落库 conversation)
        verification_request: agent2 传入的验证目标描述
            (如"验证 src/api/users.py 第 42 行的 SQL 注入是否可利用")
        client: LLMClient(用 agent2 的 client)
        round_idx: 当前协作轮次(用于落库 + 事件标注)

    返回:验证结果文本(供 agent2 作为 tool_result 注入下一轮 LLM 调用)
    """
    task_id = task.id
    task_id_str = str(task_id)
    test_env_url = task.test_env_url or ""
    auth_mode = task.verifier_auth_mode or "per_action"
    auth_tokens = task.verifier_auth_tokens or []

    # 推送验证开始事件(前端显示"正在验证...")
    publish(task_id, "thinking_delta", {
        "conv_id": str(uuid.uuid4()),
        "round_idx": round_idx,
        "role": "agent2",  # 对用户透明:归到 agent2 名下
        "phase": "start",
        "delta": "",
        "verify": True,  # 前端据此显示"正在验证"而非"正在评估"
    })

    client = client or LLMClient()

    # 系统提示:动态注入可用登录身份(LLM 只看到 label,看不到 token 明文)
    system_prompt = VERIFIER_SYSTEM_PROMPT
    if auth_tokens:
        labels = ", ".join(t.get("label", "") for t in auth_tokens)
        system_prompt += (
            f"\n\n## 可用登录身份(通过 http_request 的 auth_profile 参数选择)\n"
            f"任务配置了以下身份,你只需指定 label,工具自动注入对应认证头(你看不到 token 明文):\n"
            f"- {labels}\n\n"
            f"越权测试建议:同一受保护端点用不同身份访问,对比状态码/响应体差异。"
            f"留空 auth_profile=匿名访问。"
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"请验证以下安全发现:\n\n{verification_request}\n\n"
            f"测试环境地址: {test_env_url}\n"
            f"请构造 PoC 验证每个问题是否真实可利用。"
        )},
    ]

    for iteration in range(1, MAX_VERIFIER_ITERATIONS + 1):
        # 流式调 LLM(复用 react_agent 的流式模式,但 role 标为 agent2 + verify)
        reasoning_full, content_full, tool_calls_full, finish_reason = _stream_verifier_llm(
            client, messages, task_id=task_id, round_idx=round_idx, iteration=iteration
        )

        # 无工具调用 → 验证完成,content 是总结
        if not tool_calls_full:
            logger.info(
                f"[task={task_id}] verifier_agent 在第 {iteration} 轮完成验证,"
                f"content={len(content_full)}字符"
            )
            _record_verifier_thinking(db, task, round_idx, content_full, reasoning_full)
            _publish_verify_end(task_id, round_idx)
            return content_full or "(验证完成,无总结输出)"

        # 把 assistant 消息(含 tool_calls)加回 messages
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content_full}
        assistant_msg["tool_calls"] = [
            {
                "id": tc["id"] or f"call_{tc['index']}",
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc["arguments_str"],
                },
            }
            for tc in tool_calls_full
        ]
        messages.append(assistant_msg)

        # 执行每个工具调用
        for tc in tool_calls_full:
            tool_name = tc["name"]
            try:
                args = json.loads(tc["arguments_str"]) if tc["arguments_str"] else {}
            except json.JSONDecodeError:
                args = {}

            # per_action 授权:每个动作弹窗确认
            if auth_mode == "per_action":
                approved = _request_action_authorization(
                    task_id_str, tool_name, args, test_env_url, auth_tokens
                )
                if not approved:
                    tool_result = {"status_code": 0, "body": "[用户拒绝执行此动作]"}
                else:
                    tool_result = _execute_verifier_tool(
                        tool_name, args, task_id_str, test_env_url, auth_tokens
                    )
            else:
                tool_result = _execute_verifier_tool(
                    tool_name, args, task_id_str, test_env_url, auth_tokens
                )

            # 落库工具调用 + 结果(对用户透明,role=agent2)
            _record_verifier_tool_call(
                db, task, round_idx, tool_name, args, tool_result
            )

            # tool_result 加回 messages
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"] or f"call_{tc['index']}",
                "content": json.dumps(tool_result, ensure_ascii=False, default=str),
            })

    # 达到最大迭代仍未完成
    logger.warning(f"[task={task_id}] verifier_agent 达到最大迭代 {MAX_VERIFIER_ITERATIONS}")
    _record_verifier_thinking(
        db, task, round_idx,
        f"验证达到最大迭代次数({MAX_VERIFIER_ITERATIONS}),强制结束。已有结果见上方工具调用。",
        "",
    )
    _publish_verify_end(task_id, round_idx)
    return f"验证达到最大迭代次数({MAX_VERIFIER_ITERATIONS}),强制结束。"


# ============================================================
# 流式 LLM 调用
# ============================================================


def _stream_verifier_llm(
    client: LLMClient,
    messages: list[dict[str, Any]],
    *,
    task_id: Any,
    round_idx: int,
    iteration: int,
) -> tuple[str, str, list[dict[str, Any]], str | None]:
    """流式调用 verifier_agent 的 LLM,实时推送 thinking_delta

    返回 (reasoning_full, content_full, tool_calls_full, finish_reason)
    """
    conv_id = str(uuid.uuid4())
    reasoning_full = ""
    content_full = ""
    tool_calls_acc: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None

    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "agent2",
        "phase": "reasoning",
        "delta": "",
        "iteration": iteration,
        "verify": True,
    })

    try:
        for chunk in client.chat_stream(
            messages, tools=VERIFIER_TOOL_DEFINITIONS, tool_choice="auto", max_tokens=4096
        ):
            if chunk.reasoning_delta:
                reasoning_full += chunk.reasoning_delta
                publish(task_id, "thinking_delta", {
                    "conv_id": conv_id,
                    "round_idx": round_idx,
                    "role": "agent2",
                    "phase": "reasoning",
                    "delta": chunk.reasoning_delta,
                    "iteration": iteration,
                    "verify": True,
                })

            if chunk.content_delta:
                content_full += chunk.content_delta
                publish(task_id, "thinking_delta", {
                    "conv_id": conv_id,
                    "round_idx": round_idx,
                    "role": "agent2",
                    "phase": "content",
                    "delta": chunk.content_delta,
                    "iteration": iteration,
                    "verify": True,
                })

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
                finish_reason = chunk.finish_reason
    except Exception as e:
        logger.exception(f"[task={task_id}] verifier_agent 流式调用失败")
        publish(task_id, "thinking_delta", {
            "conv_id": conv_id,
            "round_idx": round_idx,
            "role": "agent2",
            "phase": "error",
            "delta": f"[验证流式调用失败: {e}]",
            "iteration": iteration,
            "verify": True,
        })
        raise

    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "agent2",
        "phase": "end",
        "delta": "",
        "iteration": iteration,
        "verify": True,
    })

    tool_calls_full = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
    return reasoning_full, content_full, tool_calls_full, finish_reason


# ============================================================
# 工具执行 + 授权拦截
# ============================================================


def _request_action_authorization(
    task_id_str: str,
    tool_name: str,
    args: dict[str, Any],
    test_env_url: str,
    auth_tokens: list[dict] | None = None,
) -> bool:
    """per_action 模式:弹窗让用户确认是否执行此动作

    返回 True=用户同意,False=用户拒绝
    """
    from app.user_interaction import request_verify_authorization, wait_for_authorization

    action_id = str(uuid.uuid4())

    # 构造动作描述(前端弹窗展示)
    if tool_name == "http_request":
        method = args.get("method", "GET")
        path = args.get("path", "/")
        base = (test_env_url or "").rstrip("/")
        full_url = base + (path if path.startswith("/") else "/" + path)
        action_desc = {
            "action_id": action_id,
            "type": "http_request",
            "method": method.upper(),
            "url": full_url,
            "headers": args.get("headers", {}),
            "body": args.get("body", ""),
            # 显示登录身份(若有),便于用户判断该动作是否合理
            "auth_profile": args.get("auth_profile") or "",
        }
    elif tool_name == "run_python_code":
        code = args.get("code", "")
        action_desc = {
            "action_id": action_id,
            "type": "run_python_code",
            "code": code[:2000],  # 截断防前端弹窗过长
            "code_truncated": len(code) > 2000,
        }
    else:
        action_desc = {
            "action_id": action_id,
            "type": tool_name,
            "args": args,
        }

    request_verify_authorization(task_id_str, action_desc)
    return wait_for_authorization(task_id_str, action_id)


def _execute_verifier_tool(
    tool_name: str,
    args: dict[str, Any],
    task_id_str: str,
    test_env_url: str,
    auth_tokens: list[dict] | None = None,
) -> dict[str, Any]:
    """执行 verifier_agent 的工具调用(授权已通过,直接执行)"""
    if tool_name == "http_request":
        return http_request(
            method=args.get("method", "GET"),
            path=args.get("path", "/"),
            headers=args.get("headers"),
            body=args.get("body"),
            auth_profile=args.get("auth_profile"),
            task_id=task_id_str,
            test_env_url=test_env_url,
            auth_tokens=auth_tokens,
        )
    elif tool_name == "run_python_code":
        # 复用 react_agent 的沙箱(set_current_task 已由 orchestrator 设置)
        from app.tools.sandbox_tools import run_python_code
        return run_python_code(
            code=args.get("code", ""),
            timeout=args.get("timeout", 60),
            task_id=task_id_str,
        )
    else:
        return {"error": f"verifier_agent 不支持的工具: {tool_name}"}


# ============================================================
# 落库 + 事件
# ============================================================


def _record_verifier_thinking(
    db: Session,
    task: Task,
    round_idx: int,
    content: str,
    reasoning: str,
) -> None:
    """落库 verifier_agent 的思考/总结(对用户透明,role=agent2, type=thinking)"""
    from app.models.task import Conversation
    conv = Conversation(
        task_id=task.id,
        round_idx=round_idx,
        role="agent2",
        type="thinking",
        content=f"[验证结果] {content}" if not content.startswith("[验证") else content,
        reasoning=reasoning or None,
    )
    db.add(conv)
    db.commit()


def _record_verifier_tool_call(
    db: Session,
    task: Task,
    round_idx: int,
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """落库 verifier_agent 的工具调用 + 结果(对用户透明,role=agent2)"""
    from app.models.task import Conversation

    # 工具调用记录
    intent = _build_tool_intent(tool_name, args)
    db.add(Conversation(
        task_id=task.id,
        round_idx=round_idx,
        role="agent2",
        type="tool_call",
        content=intent,
    ))

    # 工具结果记录(截断防过长)
    result_str = json.dumps(result, ensure_ascii=False, default=str)
    if len(result_str) > 5000:
        result_str = result_str[:5000] + "...(已截断)"
    db.add(Conversation(
        task_id=task.id,
        round_idx=round_idx,
        role="agent2",
        type="tool_result",
        content=result_str,
    ))
    db.commit()

    # 推送 conversation 事件(前端展示工具调用)
    publish(task.id, "conversation", {
        "round_idx": round_idx,
        "role": "agent2",
        "type": "tool_call",
        "content": intent,
        "verify": True,
    })
    publish(task.id, "conversation", {
        "round_idx": round_idx,
        "role": "agent2",
        "type": "tool_result",
        "content": result_str,
        "verify": True,
    })


def _build_tool_intent(tool_name: str, args: dict[str, Any]) -> str:
    """生成人类可读的工具调用描述"""
    if tool_name == "http_request":
        method = args.get("method", "GET")
        path = args.get("path", "/")
        return f"验证请求: {method} {path} [http_request]"
    elif tool_name == "run_python_code":
        code = args.get("code", "")
        first_line = code.split("\n")[0][:100] if code else ""
        return f"运行 PoC 脚本: {first_line} [run_python_code]"
    return f"{tool_name}({args})"


def _publish_verify_end(task_id: Any, round_idx: int) -> None:
    """推送验证结束事件"""
    publish(task_id, "thinking_delta", {
        "conv_id": str(uuid.uuid4()),
        "round_idx": round_idx,
        "role": "agent2",
        "phase": "end",
        "delta": "",
        "verify": True,
    })
