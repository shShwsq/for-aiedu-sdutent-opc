"""题目生成:把审计任务的真实发现(Result)改编为客观练习题

流程:
1. 取任务 Results(上限 max_findings 条,防 LLM 成本失控)
2. 逐条 finding 调 LLM 生成 1~3 题:
   - system prompt 按用户学习主题切换(security/architecture/coding)
   - 提示词强制题目必须阅读真实代码才能作答(禁止常识题);
     工作区可用时挂只读迷你工具循环(read_file / search_code / find_files),
     要求出题前先读源码并记录 source_file/source_lines;
     沙箱已清理且用户开启「出题前恢复工作区」时先重新 clone 恢复
3. json_repair 容错解析 + 字段校验,失败重试 1 次,仍失败丢弃该 finding;
   工作区可用时无 code_snippet 的题判为不合格,带质量反馈重试 1 次后丢弃
4. 致命错误快速失败:模型 401/403(额度耗尽/Key 失效)等不可重试错误
   立即中止剩余 finding,抛 PracticeGenerateError 由 job 层展示原因
5. 知识点 get_or_create(优先 CWE 编号)+ 同用户 sha256 去重
6. 落库为 draft(记录出题时主题与源码定位),前端预览确认后转 active

出题模型解析:task.llm_config_id > 用户级默认出题模型
(practice_settings.default_llm_config_id) > env 默认,逐级回退;
用户开启「始终用默认出题模型」(force_default_llm)时跳过任务级配置,
直接按 用户级默认 > env 默认 解析。
"""
import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

from json_repair import repair_json
from sqlalchemy.orm import Session

import openai
from app.llm.client import LLMClient
from app.models.practice import (
    DEFAULT_LEARNING_TOPIC,
    LEARNING_TOPIC_ARCHITECTURE,
    LEARNING_TOPIC_CODING,
    LEARNING_TOPIC_SECURITY,
    THINKING_MODE_OFF,
    THINKING_MODE_ON,
    KnowledgePoint,
    PracticeSettings,
    Question,
    QuestionStatus,
    QuestionType,
)
from app.models.task import Result, Task
from app.models.user_llm_config import UserLLMConfig
from app.services.practice.difficulty import clamp_difficulty
from app.tools import sandbox_tools

logger = logging.getLogger(__name__)

# 单条 finding 最多生成题数
MAX_QUESTIONS_PER_FINDING = 3
# 解析失败重试次数
PARSE_RETRY = 1
# 出题工具循环最大轮次(每轮可含多次并行工具调用;超限后强制无工具出题)
MAX_TOOL_ROUNDS = 6
# 单次工具结果回传 LLM 的截断阈值(防上下文爆炸)
_MAX_TOOL_RESULT_CHARS = 3000


class PracticeGenerateError(Exception):
    """出题过程遇到不可重试的致命错误(已生成的题目照常落库)

    异常消息为面向用户的友好原因,由 job 层捕获后置 error 终态,
    前端出题进度侧栏直接展示。
    """


def _fatal_llm_reason(e: Exception) -> str | None:
    """识别出题模型的不可重试致命错误(认证/额度),返回面向用户的原因

    额度耗尽/Key 失效类错误后续所有 finding 必然同样失败,
    应立即中止(快速失败)而非逐条重试空转;其他错误(超时/
    5xx/429 等)返回 None,走原有重试与丢弃路径。
    """
    if isinstance(e, openai.AuthenticationError):
        return "API Key 无效或已失效(401),请检查出题模型配置"
    if isinstance(e, openai.PermissionDeniedError):
        text = str(e).lower()
        if "quota" in text or "free tier" in text:
            return "免费额度已用尽(403),请充值或更换出题模型"
        return "无权访问该模型(403),请检查出题模型配置"
    return None

# ============================================================
# 提示词:按学习主题切换出题视角(通用规则段共享)
# ============================================================

_SECURITY_TOPIC_HEAD = """你是一名网络安全培训出题专家。基于给定的真实代码审计发现,改编出用于安全培训的客观题。

## 出题视角
- 漏洞识别:该代码片段存在哪种漏洞
- 成因判断:漏洞根因与触发条件
- 修复选择:正确的修复方式与安全编码实践
- knowledge_key 优先用 CWE 编号(如 "CWE-89");无对应 CWE 时用英文短标识(如 "hardcoded_secrets")"""

_ARCHITECTURE_TOPIC_HEAD = """你是一名软件架构培训出题专家。基于给定的真实代码分析发现,从架构设计视角改编出客观题。

## 出题视角
- 模块边界与职责划分、分层是否合理、依赖方向
- 设计模式的应用与误用、技术选型权衡(一致性/性能/可维护性)
- 耦合与内聚、扩展性、可测试性缺陷及其改进方案
- 即使发现本身是安全/质量问题,也应从架构成因或设计改进角度出题
- knowledge_key 用英文短标识(如 "layering"、"circular_dependency"、"observer_pattern")"""

_CODING_TOPIC_HEAD = """你是一名通用编码能力培训出题专家。基于给定的真实代码分析发现,改编出考察通用编码能力的客观题。

## 出题视角
- bug 识别与边界条件、异常与错误处理
- 代码坏味道与可读性、性能问题(如 N+1 查询、不必要的重复计算)
- 语言特性正确用法与工程最佳实践(测试、命名、API 设计)
- knowledge_key 用英文短标识(如 "null_safety"、"exception_handling"、"n_plus_one_query")"""

_TOPIC_PROMPT_HEADS = {
    LEARNING_TOPIC_SECURITY: _SECURITY_TOPIC_HEAD,
    LEARNING_TOPIC_ARCHITECTURE: _ARCHITECTURE_TOPIC_HEAD,
    LEARNING_TOPIC_CODING: _CODING_TOPIC_HEAD,
}

# 工作区可用时注入的工具说明段(工具实际可用与否与 sandbox 存活状态一致)
_TOOL_SECTION = """## 源码查阅工具(仓库已 clone,必须使用)
出题前必须先调工具查阅真实源码,禁止跳过直接出题:
- read_file(file_path, max_lines?, offset?):读仓库文件(带行号,分页)
- search_code(pattern, file_glob?, output_mode?):正则搜索代码;
  output_mode="files_with_matches" 可快速定位含关键词的文件
- find_files(pattern):按 glob 递归查文件路径(如 **/*.py)
要求:
1. 每道题出题前至少 read_file 一次相关源文件,确认代码真实存在
2. 真实代码题(origin=repo):题干与 code_snippet 必须引用读到的真实源码,
   不得虚构,并给出 source_file(仓库内相对路径)与
   source_lines(行区间如 "120-150" 或单行号 "42",取自 read_file 结果)
3. 改编题(origin=synthetic):先读原代码确认问题形态,再原创虚构代码,
   不给 source_file/source_lines
确实在仓库中找不到相关代码时,才退回基于发现描述出题(此时不给 source_file)。"""

# 三主题共享的输出规则段
_COMMON_RULES = """## 通用要求
1. 出 1~3 道题,题型限定:
   - single_choice(单选):如「该代码片段存在哪种问题」「正确的改进方式是」
   - true_false(判断):选项固定为 ["正确", "错误"],题干为一个可判定真伪的陈述
2. 题目必须阅读代码才能作答:题干要落到具体代码细节(函数名、调用关系、
   变量/字面量、分支逻辑等),禁止不看代码也能答对的通用概念题、常识题
3. 代码片段单独放 code_snippet,必须自包含:含必要的函数签名、导入与上下文,
   脱离原仓库也能读懂;题干不得依赖仓库特有路径、内部业务命名才可作答
4. 出题形式由你自主决定,鼓励真实代码题与改编题混合:
   - origin="repo"(真实代码题):基于发现中的真实代码/场景出题
   - origin="synthetic"(改编题):原创一段含同类漏洞/问题的完整虚构代码,
     业务场景与命名与原仓库完全不同,题干不得提及原仓库;考察用户把知识
     泛化应用到新代码的能力
5. 干扰项要有迷惑性但明确错误,正确答案唯一
6. explanation 讲清原理与改进要点,100 字以内
7. difficulty 评估难度(1-5 整数):1=概念识别,3=需理解代码逻辑,5=需深入细节
8. knowledge_name:知识点中文展示名(如 "SQL 注入")
9. languages:该题涉及的全部编程语言,小写短名数组(如 ["python", "sql"])
10. 元信息中若标注 verified: false 或判定为误报的发现,不要出题,直接返回空数组 []

只输出 JSON 数组,不要任何其他文字。每个元素结构:
{"qtype": "single_choice|true_false", "origin": "repo|synthetic",
 "stem": "...", "code_snippet": "...",
 "source_file": "仓库内相对路径"或null, "source_lines": "120-150"或null,
 "options": ["...", "..."], "answer_idx": 0, "explanation": "...",
 "difficulty": 3, "knowledge_key": "...", "knowledge_name": "...",
 "languages": ["python", "sql"]}"""


def build_system_prompt(topic: str, workspace_available: bool) -> str:
    """按学习主题拼出题 system prompt;工作区可用时附工具说明段"""
    head = _TOPIC_PROMPT_HEADS.get(topic) or _TOPIC_PROMPT_HEADS[LEARNING_TOPIC_SECURITY]
    sections = [head]
    if workspace_available:
        sections.append(_TOOL_SECTION)
    sections.append(_COMMON_RULES)
    return "\n\n".join(sections)


# ============================================================
# 出题工具循环(只读三工具;repo_path/task_id 由宿主注入,LLM 不感知)
# ============================================================

_PRACTICE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取仓库内文件内容(带行号,支持 offset 翻页)。发现描述缺少具体代码时,先读相关源文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "仓库内相对路径"},
                    "max_lines": {"type": "integer", "description": "本次最多返回行数,默认 100"},
                    "offset": {"type": "integer", "description": "从第几行开始读(1-based),默认 1"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "正则搜索仓库代码,定位相关函数、字符串或调用点",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "file_glob": {"type": "string", "description": "文件名过滤 glob(可选),如 *.py"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches"],
                        "description": "content=匹配行+行号;files_with_matches=仅文件路径(快速定位)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "按 glob 模式递归查找文件路径(不看内容),如 **/*.py",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式"},
                },
                "required": ["pattern"],
            },
        },
    },
]


def _execute_practice_tool(task_id: str, repo_path: str, name: str, args: dict) -> str:
    """执行出题工具循环的只读工具,返回截断后的 JSON 文本"""
    try:
        if name == "read_file":
            result = sandbox_tools.read_file(
                repo_path,
                str(args.get("file_path") or ""),
                max_lines=max(1, min(int(args.get("max_lines") or 100), 150)),
                offset=max(1, int(args.get("offset") or 1)),
                task_id=task_id,
            )
        elif name == "search_code":
            result = sandbox_tools.search_code(
                repo_path,
                str(args.get("pattern") or ""),
                file_glob=args.get("file_glob"),
                max_matches=max(1, min(int(args.get("max_matches") or 30), 50)),
                output_mode=str(args.get("output_mode") or "content"),
                task_id=task_id,
            )
        elif name == "find_files":
            result = sandbox_tools.find_files(
                repo_path, str(args.get("pattern") or ""), task_id=task_id,
            )
        else:
            return f"未知工具: {name}"
        return json.dumps(result, ensure_ascii=False, default=str)[:_MAX_TOOL_RESULT_CHARS]
    except Exception as e:
        return f"工具执行失败: {e}"


def _tool_event_summary(name: str, args: dict) -> str:
    """工具事件的简短描述(侧栏展示用,如 read_file: src/x.py)"""
    if name == "read_file":
        return f"read_file: {args.get('file_path') or '?'}"
    if name == "search_code":
        return f"search_code: {args.get('pattern') or '?'}"
    if name == "find_files":
        return f"find_files: {args.get('pattern') or '?'}"
    return name


def _stream_one_round(
    client: LLMClient, messages: list[dict], tools: list[dict] | None,
    on_event: Callable[[str, dict], None] | None = None,
) -> tuple[str, list[dict]]:
    """一轮 chat_stream:累积正式回复与工具调用增量(参数跨 chunk 拼接)

    on_event(可选):content 增量实时回调 token 事件(出题进度侧栏流式展示用);
    不含 reasoning_delta(思考链噪音大)。
    """
    content_parts: list[str] = []
    tool_calls_acc: dict[int, dict] = {}
    for chunk in client.chat_stream(messages, max_tokens=4096, tools=tools):
        if chunk.content_delta:
            content_parts.append(chunk.content_delta)
            if on_event:
                try:
                    on_event("token", {"delta": chunk.content_delta})
                except Exception:
                    pass  # 事件回调失败不影响出题主流程
        for d in chunk.tool_call_deltas or []:
            slot = tool_calls_acc.setdefault(
                d.index, {"id": "", "name": "", "arguments_str": ""},
            )
            if d.id and not slot["id"]:
                slot["id"] = d.id
            if d.name and not slot["name"]:
                slot["name"] = d.name
            if d.arguments_fragment:
                slot["arguments_str"] += d.arguments_fragment
    tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
    return "".join(content_parts), tool_calls


def _call_llm(
    client: LLMClient, system_prompt: str, finding_text: str,
    task_id: str, repo_path: str,
    on_event: Callable[[str, dict], None] | None = None,
) -> str:
    """出题 LLM 调用:工作区可用 → 有界工具循环;否则单次直出"""
    tools = _PRACTICE_TOOL_DEFINITIONS if repo_path else None
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": finding_text},
    ]
    for _ in range(MAX_TOOL_ROUNDS):
        content, tool_calls = _stream_one_round(client, messages, tools, on_event)
        if not tool_calls:
            return content
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
        assistant_msg["tool_calls"] = [
            {
                "id": tc["id"] or f"call_{i}",
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments_str"]},
            }
            for i, tc in enumerate(tool_calls)
        ]
        messages.append(assistant_msg)
        for i, tc in enumerate(tool_calls):
            try:
                args = json.loads(tc["arguments_str"]) if tc["arguments_str"] else {}
            except json.JSONDecodeError:
                args = {}
            if on_event:
                try:
                    on_event("tool", {
                        "name": tc["name"],
                        "summary": _tool_event_summary(tc["name"], args),
                    })
                except Exception:
                    pass
            result_str = _execute_practice_tool(task_id, repo_path, tc["name"], args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"] or f"call_{i}",
                "content": result_str,
            })
    # 超工具轮数:去掉工具强制收口出题
    content, _ = _stream_one_round(client, messages, None, on_event)
    return content


# ============================================================
# 工作区保障(沙箱已清理时按用户设置重新 clone)
# ============================================================


def _load_git_tokens(db: Session, user_id) -> dict[str, str]:
    """加载用户 git provider 的 access_token(与 orchestrator._load_git_tokens 同逻辑)"""
    if user_id is None:
        return {}
    try:
        from app.models.user_git_binding import UserGitBinding
        from app.security import decrypt_secret

        bindings = (
            db.query(UserGitBinding)
            .filter(
                UserGitBinding.user_id == user_id,
                UserGitBinding.access_token != "",
            )
            .all()
        )
        tokens: dict[str, str] = {}
        for b in bindings:
            try:
                tokens[b.provider] = decrypt_secret(b.access_token)
            except Exception as e:
                logger.warning("[practice] 解密 %s token 失败: %s", b.provider, e)
        return tokens
    except Exception as e:
        logger.warning("[practice] 加载 git token 失败: %s", e)
        return {}


def _ensure_workspace(
    db: Session, task: Task, settings_row: PracticeSettings | None,
    event_callback: Callable[[str, dict], None] | None = None,
) -> dict | None:
    """保障出题用的工作区,返回 workspace info(含 repo_path;不可用为 None)

    - 沙箱 session 存活 → 直接复用
    - 已清理 + 用户开启 restore_workspace_for_practice + 任务有 repo_url
      → 重新 clone 恢复(成功后标记 completed 纳入 1 小时 TTL 清理序列)
    - 恢复失败/条件不满足 → 静默降级(出题走无工具路径)

    event_callback 非空时推 restore 事件(start/progress/done/failed),
    供出题进度侧栏展示克隆进度;回调异常不影响恢复主流程。
    """

    def _emit(phase: str, **extra) -> None:
        if event_callback is None:
            return
        try:
            event_callback("restore", {"phase": phase, **extra})
        except Exception:
            logger.warning("[practice] restore 事件回调异常(忽略)", exc_info=True)

    task_id_str = str(task.id)
    info = sandbox_tools.get_workspace_info(task_id_str)
    if info and info.get("repo_path"):
        return info
    params = task.params or {}
    repo_url = params.get("repo_url")
    if not repo_url or settings_row is None or not settings_row.restore_workspace_for_practice:
        return info
    logger.info("[task=%s] 出题前沙箱已清理,重新 clone 恢复工作区", task.id)
    _emit("start")
    try:
        sandbox_tools.clone_repo_with_fallback(
            repo_url,
            branch=params.get("branch"),
            task_id=task_id_str,
            git_tokens=_load_git_tokens(db, task.user_id),
            progress_callback=lambda percent, message: _emit(
                "progress", percent=percent, message=message,
            ),
        )
        # 恢复的 session 属于已完成任务:纳入 TTL 清理序列,避免常驻泄漏
        sandbox_tools.mark_task_completed(task_id_str)
        _emit("done")
        return sandbox_tools.get_workspace_info(task_id_str)
    except Exception as e:
        logger.warning("[task=%s] 出题前恢复工作区失败(降级为无工具出题): %s", task.id, e)
        _emit("failed", message=str(e)[:200])
        return sandbox_tools.get_workspace_info(task_id_str)


# ============================================================
# 原有管线:模型解析 / 校验 / 去重 / 落库
# ============================================================


def resolve_llm_client(db: Session, task: Task) -> LLMClient:
    """解析出题模型:task.llm_config_id > 用户级默认出题模型 > env 默认

    任务级与用户级配置都存于 UserLLMConfig.llm_configs(一次查询,
    按优先级逐个匹配);任一级配置缺失/失效均回退下一级,全部失败回退 env 默认。
    用户开启「始终用默认出题模型」(practice_settings.force_default_llm)时
    跳过任务级配置,直接按 用户级默认 > env 默认 解析。
    手动出题与任务完成自动出题共用本解析。
    """
    config_ids: list[str] = []
    pref = None
    if task.user_id is not None:
        pref = db.query(PracticeSettings).filter(
            PracticeSettings.user_id == task.user_id
        ).first()
    # 「始终用默认出题模型」开启时忽略任务自带配置(用户级默认 > env 默认)
    force_default = pref is not None and pref.force_default_llm is True
    if not force_default and task.llm_config_id:
        config_ids.append(task.llm_config_id)
    if pref is not None and pref.default_llm_config_id:
        config_ids.append(pref.default_llm_config_id)
    if config_ids:
        try:
            cfg_row = db.query(UserLLMConfig).filter(
                UserLLMConfig.user_id == task.user_id
            ).first()
            configs = {
                c.get("id"): c for c in (cfg_row.llm_configs or [])
            } if cfg_row else {}
            for cid in config_ids:
                cfg = configs.get(cid)
                if cfg:
                    return LLMClient.from_config_dict(cfg)
            logger.warning(
                "[practice] 未找到出题模型配置 ids=%s,回退 env 默认", config_ids
            )
        except Exception as e:
            logger.warning("[practice] 加载出题模型配置失败,回退 env 默认: %s", e)
    return LLMClient()


def resolve_generate_model_info(db: Session, task: Task) -> dict[str, str]:
    """解析本次出题将使用的模型与来源(任务详情页展示用)

    与 resolve_llm_client 同一优先级(含「始终用默认出题模型」开关),
    返回 {model: 模型名, source: task=任务配置 / default=练习默认 / env=环境默认}。
    """
    client = resolve_llm_client(db, task)
    model = getattr(client, "model", None) or "?"
    source = "env"
    if task.user_id is not None:
        pref = db.query(PracticeSettings).filter(
            PracticeSettings.user_id == task.user_id
        ).first()
        if pref is not None and pref.force_default_llm is True:
            if pref.default_llm_config_id:
                source = "default"
        elif task.llm_config_id:
            source = "task"
        elif pref is not None and pref.default_llm_config_id:
            source = "default"
    elif task.llm_config_id:
        source = "task"
    return {"model": model, "source": source}


def _apply_thinking_mode(client: LLMClient, settings_row: PracticeSettings | None) -> None:
    """应用用户级出题思考模式覆盖(手动/自动出题共用)

    follow(默认)保持出题模型配置自身的思考开关不动;on/off 强制开/关:
    - 思考模式出题更慢但可能质量更高;部分模型思考模式下工具调用
      会写成文本而非结构化通道,导致出题工具循环失效,此时可强制关
    - 仅支持思考模式的模型(catalog thinking=only)强制关无效:
      build_thinking_extras 对该类模型始终强附思考参数,这里记日志后跳过
    """
    mode = getattr(settings_row, "thinking_mode_for_practice", None)
    if mode not in (THINKING_MODE_OFF, THINKING_MODE_ON):
        return  # follow / 未知值(如测试 mock)→ 保持模型配置原样
    meta = getattr(client, "model_meta", None) or {}
    if mode == THINKING_MODE_OFF and meta.get("thinking") == "only":
        logger.info(
            "[practice] 模型 %s 仅支持思考模式,忽略强制关闭设置",
            getattr(client, "model", "?"),
        )
        return
    client.enable_thinking = (mode == THINKING_MODE_ON)


def compute_dedup_hash(stem: str, code_snippet: str | None) -> str:
    return hashlib.sha256(
        f"{stem.strip()}\n{(code_snippet or '').strip()}".encode("utf-8")
    ).hexdigest()


# 文件扩展名 → 语言短名(LLM 未给 languages 时从 source_file 推断用)
_EXT_LANGUAGES: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".java": "java",
    ".go": "go", ".rs": "rust", ".c": "c", ".h": "c", ".cpp": "cpp",
    ".cc": "cpp", ".cs": "csharp", ".php": "php", ".rb": "ruby",
    ".sql": "sql", ".sh": "shell", ".html": "html", ".vue": "vue",
}

# 语言标签规范化上限(防 LLM 乱输出)
_MAX_LANGUAGES = 5
_MAX_LANGUAGE_LEN = 24


def _normalize_languages(raw: Any, source_file: str | None) -> list[str]:
    """规范化 LLM 输出的语言标签:小写/去重/截断;未给时从 source_file 扩展名推断"""
    langs: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            s = str(item or "").strip().lower()
            if s and s not in langs:
                langs.append(s[:_MAX_LANGUAGE_LEN])
    if not langs and source_file:
        ext = source_file[source_file.rfind("."):].lower() if "." in source_file else ""
        lang = _EXT_LANGUAGES.get(ext)
        if lang:
            langs.append(lang)
    return langs[:_MAX_LANGUAGES]


def _get_or_create_knowledge_point(
    db: Session, user_id, key: str, name: str, languages: list[str] | None = None,
) -> KnowledgePoint:
    key = (key or "").strip() or "general"
    kp = db.query(KnowledgePoint).filter(
        KnowledgePoint.user_id == user_id,
        KnowledgePoint.key == key,
    ).first()
    if kp:
        # 已有知识点:语言标签并集累积(保持原顺序追加新语言)
        merged = list(kp.languages or [])
        added = [l for l in (languages or []) if l not in merged]
        if added:
            kp.languages = merged + added
        return kp
    kp = KnowledgePoint(
        user_id=user_id,
        key=key,
        name=(name or "").strip() or key,
        category="cwe" if key.upper().startswith("CWE-") else "general",
        languages=list(languages or []),
    )
    db.add(kp)
    db.flush()
    return kp


def _normalize_raw_question(raw: Any, finding_meta: dict) -> dict | None:
    """校验并规范化 LLM 输出的单题结构,非法返回 None"""
    if not isinstance(raw, dict):
        return None

    qtype = str(raw.get("qtype") or "").strip()
    if qtype not in ("single_choice", "true_false"):
        return None

    stem = str(raw.get("stem") or "").strip()
    if not stem:
        return None

    options = raw.get("options")
    if qtype == "true_false":
        options = ["正确", "错误"]
    else:
        if not isinstance(options, list):
            return None
        options = [str(o).strip() for o in options if str(o).strip()]
        if len(options) < 2 or len(options) > 8:
            return None

    try:
        answer_idx = int(raw.get("answer_idx"))
    except (TypeError, ValueError):
        return None
    if not (0 <= answer_idx < len(options)):
        return None

    # 单选题全同选项无效
    if qtype == "single_choice" and len(set(options)) < 2:
        return None

    difficulty = raw.get("difficulty", 3)
    try:
        difficulty = clamp_difficulty(float(difficulty))
    except (TypeError, ValueError):
        difficulty = 3.0

    # knowledge_key 优先用 finding 元信息里的 CWE(比 LLM 输出可靠)
    cwe = str(finding_meta.get("cwe") or "").strip().upper()
    if cwe and not cwe.startswith("CWE-") and cwe.isdigit():
        cwe = f"CWE-{cwe}"
    knowledge_key = cwe or str(raw.get("knowledge_key") or "").strip() or "general"

    code_snippet = raw.get("code_snippet")
    code_snippet = str(code_snippet).strip() if code_snippet else None

    # 源码定位(工作区可用时 LLM 应给出;老输出无此字段时为 None)
    source_file = str(raw.get("source_file") or "").strip()[:512] or None
    source_lines = str(raw.get("source_lines") or "").strip()[:32] or None

    # 出题形式:repo=真实代码题,synthetic=改编题;白名单外回退 repo
    origin = str(raw.get("origin") or "").strip()
    if origin not in ("repo", "synthetic"):
        origin = "repo"

    return {
        "qtype": qtype,
        "stem": stem,
        "code_snippet": code_snippet,
        "options": options,
        "answer_idx": answer_idx,
        "explanation": str(raw.get("explanation") or "").strip(),
        "difficulty": difficulty,
        "knowledge_key": knowledge_key,
        "knowledge_name": str(raw.get("knowledge_name") or "").strip(),
        "source_file": source_file,
        "source_lines": source_lines,
        "origin": origin,
        "languages": _normalize_languages(raw.get("languages"), source_file),
    }


def _parse_llm_questions(content: str, finding_meta: dict, finding_id=None) -> list[dict]:
    """json_repair 容错解析 LLM 输出 → 规范题目列表

    解析/校验的每个丢弃分支都落日志(出题专用日志文件),
    便于排查“一道题也没生成”是模型输出问题还是校验过严。
    """
    text = (content or "").strip()
    if not text:
        logger.warning("[practice] finding=%s LLM 输出为空,无法出题", finding_id)
        return []
    try:
        result = repair_json(text, return_objects=True)
    except Exception as e:
        logger.warning(
            "[practice] finding=%s json_repair 解析失败: %s; 输出样例: %r",
            finding_id, e, text[:300],
        )
        return []
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        logger.warning(
            "[practice] finding=%s LLM 输出解析结果非数组(%s),样例: %r",
            finding_id, type(result).__name__, text[:300],
        )
        return []
    questions = []
    invalid: list[Any] = []
    for raw in result:
        q = _normalize_raw_question(raw, finding_meta)
        if q:
            questions.append(q)
        else:
            invalid.append(raw)
    if invalid:
        try:
            sample = json.dumps(invalid[0], ensure_ascii=False, default=str)[:300]
        except Exception:
            sample = str(invalid[0])[:300]
        logger.warning(
            "[practice] finding=%s 结构校验丢弃 %d/%d 题,首个无效元素样例: %s",
            finding_id, len(invalid), len(result), sample,
        )
    if not questions and not invalid:
        # 模型主动返回空数组(提示词约定:判定误报/不适合出题时返回 [])
        logger.info(
            "[practice] finding=%s LLM 返回空数组(可能判定为误报或不适合出题)",
            finding_id,
        )
    return questions[:MAX_QUESTIONS_PER_FINDING]


_FINDING_TEMPLATE = """以下是代码审计任务的一条真实发现:

【标题】{title}

【详细描述】
{content}

【元信息】{metadata}

请基于这条发现出题(1~3 道)。"""

# 工作区可用但生成的题目全部缺代码上下文时,追加到 user prompt 重试的质量反馈
_NO_CODE_FEEDBACK = (
    "\n\n【质量反馈】上一轮生成的题目缺少真实代码上下文,不看代码也能作答,不合格。"
    "请先用源码查阅工具(read_file 等)读取相关源文件,再基于实际源码重新出题:"
    "题干必须落到具体代码细节,每题必须带 code_snippet,并给出 source_file 与 source_lines。"
)


def generate_questions_for_task(
    db: Session,
    task: Task,
    user_id,
    max_findings: int = 10,
    client: LLMClient | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    event_callback: Callable[[str, dict], None] | None = None,
) -> tuple[list[Question], int]:
    """为任务的 Results 生成 draft 题目

    返回 (新建题目列表, 被跳过的 finding 数)。
    progress_callback(done, total):每处理完一条 finding 回调(异步生成进度展示用)。
    event_callback(type, data):流式事件回调(finding/token/tool,
    出题进度侧栏 SSE 展示用),异常不影响出题主流程。
    """
    if client is None:
        client = resolve_llm_client(db, task)

    # 用户练习设置:学习主题决定提示词;是否允许出题前恢复工作区;
    # 思考模式覆盖出题模型的思考开关
    settings_row = db.query(PracticeSettings).filter(
        PracticeSettings.user_id == user_id
    ).first()
    _apply_thinking_mode(client, settings_row)
    topic = (
        settings_row.learning_topic if settings_row else DEFAULT_LEARNING_TOPIC
    )

    # 工作区:存活 → 挂工具循环;已清理 → 按设置尝试重新 clone
    # (restore 事件经 event_callback 推出题进度侧栏展示克隆进度)
    ws_info = _ensure_workspace(db, task, settings_row, event_callback=event_callback)
    repo_path = (ws_info or {}).get("repo_path") or ""
    system_prompt = build_system_prompt(topic, workspace_available=bool(repo_path))
    task_id_str = str(task.id)

    findings = (
        db.query(Result).filter(Result.task_id == task.id).order_by(Result.created_at).all()
    )[:max_findings]
    total_findings = len(findings)

    # 出题起始快照:模型/主题/工作区/发现数(排查无题产出时的第一手上下文)
    logger.info(
        "[practice] 开始出题 task=%s user=%s model=%s topic=%s workspace=%s findings=%d",
        task.id, user_id, getattr(client, "model", "?"), topic,
        bool(repo_path), total_findings,
    )

    # 已有 dedup_hash(同用户),避免重复入库
    existing_hashes = {
        row[0] for row in db.query(Question.dedup_hash).filter(
            Question.user_id == user_id
        ).all()
    }

    created: list[Question] = []
    skipped = 0
    fatal_reason = ""  # 非空表示遇到不可重试致命错误,需中止并冒泡原因
    for idx, finding in enumerate(findings):
        meta = finding.metadata_ or {}
        if event_callback:
            try:
                event_callback("finding", {
                    "index": idx + 1,
                    "total": total_findings,
                    "title": finding.title,
                })
            except Exception:
                pass
        prompt = _FINDING_TEMPLATE.format(
            title=finding.title,
            content=(finding.content or "")[:4000],
            metadata=meta,
        )

        questions: list[dict] = []
        user_prompt = prompt
        content = ""
        for attempt in range(PARSE_RETRY + 1):
            try:
                content = _call_llm(
                    client, system_prompt, user_prompt, task_id_str, repo_path,
                    on_event=event_callback,
                )
            except Exception as e:
                fatal = _fatal_llm_reason(e)
                if fatal is not None:
                    # 额度/认证类错误:后续 finding 必然同样失败,立即中止
                    # 避免空转(原因经 PracticeGenerateError 推给前端展示)
                    logger.error(
                        "[practice] 出题模型致命错误,中止剩余 finding: %s", e,
                    )
                    fatal_reason = (
                        f"出题模型 {getattr(client, 'model', '?')} {fatal}"
                    )
                    break
                logger.warning(
                    "[practice] finding=%s LLM 调用失败(第 %d 次): %s",
                    finding.id, attempt + 1, e,
                )
                content = ""
            questions = _parse_llm_questions(content, meta, finding_id=finding.id)
            # 质量关卡:工作区可用时无 code_snippet 的题不合格(常识题拦截)
            if repo_path and questions:
                qualified = [q for q in questions if q["code_snippet"]]
                dropped = len(questions) - len(qualified)
                if dropped:
                    logger.info(
                        "[practice] finding=%s 质量关卡: %d/%d 题缺 code_snippet 被丢弃",
                        finding.id, dropped, len(questions),
                    )
                if not qualified and attempt < PARSE_RETRY:
                    # 全部缺代码上下文:带质量反馈重试一次
                    logger.info(
                        "[practice] finding=%s 全部题目缺代码上下文,带质量反馈重试",
                        finding.id,
                    )
                    user_prompt = prompt + _NO_CODE_FEEDBACK
                    continue
                questions = qualified
            if questions:
                break

        if fatal_reason:
            break

        if not questions:
            skipped += 1
            logger.warning(
                "[practice] finding=%s 未能产出任何题目(共 %d 次尝试);"
                "最后一次 LLM 输出样例: %r",
                finding.id, PARSE_RETRY + 1, (content or "")[:300],
            )
            if progress_callback:
                progress_callback(idx + 1, total_findings)
            continue

        dup_skipped = 0
        for q in questions:
            dedup_hash = compute_dedup_hash(q["stem"], q["code_snippet"])
            if dedup_hash in existing_hashes:
                dup_skipped += 1
                continue
            existing_hashes.add(dedup_hash)

            kp = _get_or_create_knowledge_point(
                db, user_id, q["knowledge_key"], q["knowledge_name"],
                languages=q["languages"],
            )
            question = Question(
                user_id=user_id,
                source_task_id=task.id,
                source_result_id=finding.id,
                knowledge_point_id=kp.id,
                qtype=QuestionType(q["qtype"]),
                stem=q["stem"],
                code_snippet=q["code_snippet"],
                options=q["options"],
                answer_idx=q["answer_idx"],
                explanation=q["explanation"],
                difficulty=q["difficulty"],
                status=QuestionStatus.DRAFT,
                dedup_hash=dedup_hash,
                learning_topic=topic,
                origin=q["origin"],
                source_file=q["source_file"],
                source_lines=q["source_lines"],
            )
            db.add(question)
            created.append(question)

        if dup_skipped:
            logger.info(
                "[practice] finding=%s 去重跳过 %d 题(同用户已有相同题目)",
                finding.id, dup_skipped,
            )

        if progress_callback:
            progress_callback(idx + 1, total_findings)

    db.commit()
    for q in created:
        db.refresh(q)
    logger.info(
        "[practice] 出题结束 task=%s: 生成 %d 题, %d/%d 条 finding 未出题%s",
        task.id, len(created), skipped, total_findings,
        f"(致命错误中止: {fatal_reason})" if fatal_reason else "",
    )
    if fatal_reason:
        # 已生成的 draft 照常保留;错误原因冒泡给 job 层展示
        suffix = f",中止前已生成 {len(created)} 题" if created else ""
        raise PracticeGenerateError(f"{fatal_reason}{suffix}")
    return created, skipped
