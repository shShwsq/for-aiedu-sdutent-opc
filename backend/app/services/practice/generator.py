"""题目生成:把审查任务的真实发现(Result)改编为客观练习题

流程:
1. 取任务 Results(上限 max_findings 条,防 LLM 成本失控)
2. 逐条 finding 调 LLM 生成 1~3 题:
   - system prompt 按发现内容自动匹配的主题切换:
     主题词表来自用户设置(learning_topics 表,内置 4 个 + 自定义,
     仅启用主题参与;规则先行 + LLM 批量兜底),内置主题用专有
     出题视角,自定义主题用通用模板 + 用户描述
   - 提示词强制题目必须阅读真实材料(代码或文书原文)才能作答(禁止常识题);
     工作区可用时挂只读迷你工具循环(read_file / search_code / find_files),
     要求出题前先读材料并记录 source_file/source_lines;
     沙箱已清理且用户开启「出题前恢复工作区」时先重新 clone 恢复
3. json_repair 容错解析 + 字段校验,失败重试 1 次,仍失败丢弃该 finding;
   两级质量关卡拦截不合格题(全部被拦时带质量反馈重试 1 次后丢弃):
   - 关卡 1(始终启用):退化题 — 叙述式判断题(某同学做了某判断是否
     正确)或判断题措辞泄露答案(题干含「仅凭」等)
   - 关卡 2(工作区可用时):无 code_snippet 的题,不看材料也能作答
4. 致命错误快速失败:模型 401/403(额度耗尽/Key 失效)等不可重试错误
   立即中止剩余 finding,抛 PracticeGenerateError 由 job 层展示原因
5. 知识点 get_or_create(优先 CWE 编号)+ 同用户 sha256 去重
6. 落库为 draft(记录出题时实际匹配的主题与源码定位),前端预览确认后转 active

出题模型解析:task.llm_config_id > 用户级默认出题模型
(practice_settings.default_llm_config_id) > env 默认,逐级回退;
用户开启「始终用默认出题模型」(force_default_llm)时跳过任务级配置,
直接按 用户级默认 > env 默认 解析。
"""
import hashlib
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from json_repair import repair_json
from sqlalchemy.orm import Session

import openai
from app.llm.client import LLMClient
from app.models.practice import (
    BUILTIN_TOPIC_DEFS,
    DEFAULT_LEARNING_TOPIC,
    LEARNING_TOPIC_ARCHITECTURE,
    LEARNING_TOPIC_CODING,
    LEARNING_TOPIC_CONTRACT,
    LEARNING_TOPIC_SECURITY,
    THINKING_MODE_OFF,
    THINKING_MODE_ON,
    KnowledgePoint,
    PracticeSettings,
    Question,
    QuestionStatus,
    QuestionType,
    ensure_user_topics,
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
# 提示词资产:集中管理于 app/prompts/practice.py,此处仅导入
# ============================================================
from app.prompts.practice import (
    _DEGEN_FEEDBACK,
    _FINDING_TEMPLATE,
    _LEARNING_NOTE_TEMPLATE,
    _NO_CODE_FEEDBACK,
    _PRACTICE_TOOL_DEFINITIONS,
    build_system_prompt,
    build_topic_classify_prompt,
)

# ============================================================
# 主题自动匹配:规则先行 + LLM 批量兜底(主题词表来自用户设置)
# ============================================================

# metadata 值里的 CWE 编号模式(如 "CWE-89")
_CWE_PATTERN = re.compile(r"CWE-\d+", re.IGNORECASE)


def _match_topic_by_rule(task: Task, meta: dict | None) -> str | None:
    """规则匹配单条发现的主题;判不定返回 None(交 LLM 批量分类)

    只保留两条强规则,避免误伤:
    1. 文书审核场景 → 全部 contract(场景级强信号)
    2. metadata 带 CWE 编号(键名含 cwe 或值匹配 CWE-\\d)→ security
    其余(如"知识点提炼"类发现常无 file_path,无法凭元信息区分代码/文书)
    统一交 LLM 按内容判断。
    """
    # 场景强信号:文书审核任务全部按合同文书出题
    if (task.scenario or "") == "document_review":
        return LEARNING_TOPIC_CONTRACT
    for k, v in (meta or {}).items():
        if "cwe" in str(k).lower():
            return LEARNING_TOPIC_SECURITY
        if v and _CWE_PATTERN.search(str(v)):
            return LEARNING_TOPIC_SECURITY
    return None


def _classify_topics_with_llm(
    client: LLMClient, pending: list[tuple[int, Result]], task_id: str,
    topic_defs: list[dict],
) -> dict[int, str]:
    """把规则未定的 findings 一次批量送 LLM 分类,返回 {序号: topic}

    topic_defs 为用户启用主题词表(内置 + 自定义,{key,name,description}),
    分类提示词按词表动态构建,输出必须命中词表内的 key。
    任何异常由调用方捕获降级;输出解析容错,个别条目非法只跳过该条。
    """
    items = []
    for idx, f in pending:
        items.append({
            "id": idx,
            "title": (f.title or "")[:200],
            "content": (f.content or "")[:400],
        })
    user_prompt = (
        "以下是待分类的发现列表(JSON 数组):\n"
        + json.dumps(items, ensure_ascii=False)
        + "\n请对每条判断出题主题,按系统要求的格式输出。"
    )
    messages: list[dict] = [
        {"role": "system", "content": build_topic_classify_prompt(topic_defs)},
        {"role": "user", "content": user_prompt},
    ]
    content, _ = _stream_one_round(client, messages, None)
    parsed = json.loads(repair_json(content))
    valid_keys = {d["key"] for d in topic_defs}
    result: dict[int, str] = {}
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                i = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            t = str(item.get("topic") or "").strip()
            if t in valid_keys:
                result[i] = t
    return result


def _match_finding_topics(
    task: Task, findings: list[Result], client: LLMClient,
    topic_defs: list[dict],
) -> dict:
    """逐 finding 匹配出题主题:规则先行,判不定的批量送 LLM 分类

    topic_defs 为用户启用主题词表(按 sort_order 排序)。
    返回 {finding.id: topic}。LLM 分类失败/条目缺失时降级到
    排序第一的启用主题(防该主题被停用后降级落空),保证出题不中断。
    异常静默降级,不阻断出题主流程。
    """
    fallback_topic = topic_defs[0]["key"] if topic_defs else DEFAULT_LEARNING_TOPIC
    topics: dict = {}
    pending: list[tuple[int, Result]] = []
    for idx, f in enumerate(findings):
        t = _match_topic_by_rule(task, f.metadata_ or {})
        if t:
            topics[f.id] = t
        else:
            pending.append((idx, f))

    if pending:
        try:
            classified = _classify_topics_with_llm(
                client, pending, str(task.id), topic_defs,
            )
        except Exception as e:
            logger.warning(
                "[practice] task=%s 主题 LLM 分类失败,全部降级 %s: %s",
                task.id, fallback_topic, e,
            )
            classified = {}
        for idx, f in pending:
            topics[f.id] = classified.get(idx) or fallback_topic

    # 兜底:理论上不会缺,防御性补齐
    for f in findings:
        topics.setdefault(f.id, fallback_topic)
    return topics


def _topic_stats(topic_map: dict) -> str:
    """主题分布统计(日志用),如 "security:3,contract:2" """
    counts: dict[str, int] = {}
    for t in topic_map.values():
        counts[t] = counts.get(t, 0) + 1
    return ",".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "none"


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
    learning_topic: str = DEFAULT_LEARNING_TOPIC,
) -> KnowledgePoint:
    key = (key or "").strip() or "general"
    kp = db.query(KnowledgePoint).filter(
        KnowledgePoint.user_id == user_id,
        KnowledgePoint.key == key,
    ).first()
    if kp:
        # 已有知识点:语言标签并集累积(保持原顺序追加新语言);
        # learning_topic first-wins(首个出题主题归属保持稳定)
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
        learning_topic=learning_topic,
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


def _is_practice_worthy(finding: Result) -> bool:
    """finding 是否被 agent2 标记为有学习价值(metadata.practice_worthy)"""
    meta = finding.metadata_
    return isinstance(meta, dict) and meta.get("practice_worthy") is True


# ============================================================
# 质量关卡 1:退化题检测(叙述式判断题 / 答案泄露措辞)
# ============================================================

# 虚构人物叙述模式:某(位/个)同学/工程师/...、小明/小王等
_NARRATOR_PATTERN = re.compile(
    r"某(?:位|个)?(?:同学|工程师|开发(?:者|人员)?|程序员|测试(?:人员|工程师)?|"
    r"用户|新人|审计(?:员|人员)?|分析(?:师|人员)?)|小[明华红强伟芳]"
)
# 叙述式判断题的题干问法(与人物模式同时出现即为退化)
_NARRATIVE_JUDGE_PATTERN = re.compile(
    r"是否正确|正确吗|对不对|对吗|判断是否|判断正确"
)
# 判断题措辞即答案的泄露词(「仅凭 X 就 Y」的叙述必然是反例)
_ANSWER_LEAK_WORDS = ("仅凭", "就想当然", "便断定", "就直接认定", "就认定")


def _is_degenerate_question(q: dict) -> bool:
    """检测退化题:不看材料也能答对、或题型套路泄露答案的题

    两类模式:
    1. 叙述式判断题:「某同学做了某判断,该判断是否正确」— 出题惯性里
       此类叙述必然是反例,看到「某同学 + 仅凭」即可答"错误",零知识送分
    2. 判断题措辞泄露:题干含「仅凭」等泄露词时,答案恒为"错误"

    纯规则检测(零 LLM 成本),误伤可控:正常专业题不会引入虚构人物
    叙述,也不会用「仅凭」开头描述材料事实。
    """
    stem = q.get("stem") or ""
    if not stem:
        return False
    if _NARRATOR_PATTERN.search(stem) and _NARRATIVE_JUDGE_PATTERN.search(stem):
        return True
    if q.get("qtype") == "true_false":
        return any(w in stem for w in _ANSWER_LEAK_WORDS)
    return False


def _select_findings(
    db: Session, task: Task, max_findings: int,
) -> list[Result]:
    """选取出题素材:practice_worthy 标记的优先,不足再补未标记的

    标记由 agent2 在 done=true 整理 results 时写入 metadata
    (practice_worthy=true + learning_note=考察点说明)。
    无标记(单 agent 模式 / 老任务 / agent2 未标)时,
    行为与按 created_at 顺序取前 N 条完全一致,向后兼容。
    两组内部均保持 created_at 顺序(agent2 的标记顺序即结果顺序)。
    """
    all_findings = (
        db.query(Result)
        .filter(Result.task_id == task.id)
        .order_by(Result.created_at)
        .all()
    )
    marked = [f for f in all_findings if _is_practice_worthy(f)]
    if not marked:
        return all_findings[:max_findings]
    rest = [f for f in all_findings if not _is_practice_worthy(f)]
    return (marked + rest)[:max_findings]


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

    # 用户练习设置:是否允许出题前恢复工作区;思考模式覆盖出题模型的思考开关
    settings_row = db.query(PracticeSettings).filter(
        PracticeSettings.user_id == user_id
    ).first()
    _apply_thinking_mode(client, settings_row)

    # 用户学习主题词表(仅启用主题参与分类与出题):
    # 停用主题的 finding 出题前被过滤,只停新增、不动存量。
    # CRUD 已保证启用数不归零;此处防御兜底为内置词表
    all_topics = ensure_user_topics(db, user_id)
    enabled_topics = [t for t in all_topics if t.enabled]
    if enabled_topics:
        topic_defs = [
            {"key": t.key, "name": t.name, "description": t.description}
            for t in enabled_topics
        ]
    else:
        logger.warning(
            "[practice] task=%s user=%s 无启用学习主题,按内置词表出题",
            task.id, user_id,
        )
        topic_defs = [
            {"key": d["key"], "name": d["name"], "description": d["description"]}
            for d in BUILTIN_TOPIC_DEFS
        ]
    custom_defs = [d for d in topic_defs if d["key"] not in
                   {b["key"] for b in BUILTIN_TOPIC_DEFS}]

    # 工作区:存活 → 挂工具循环;已清理 → 按设置尝试重新 clone
    # (restore 事件经 event_callback 推出题进度侧栏展示克隆进度)
    ws_info = _ensure_workspace(db, task, settings_row, event_callback=event_callback)
    repo_path = (ws_info or {}).get("repo_path") or ""
    task_id_str = str(task.id)

    # 选题:agent2 标记的学习点(practice_worthy)优先,不足补未标记的;
    # 无标记时与按 created_at 取前 N 条等价(向后兼容)
    findings = _select_findings(db, task, max_findings)
    total_findings = len(findings)

    # 主题自动匹配:规则先行 + LLM 批量兜底(每任务至多一次分类调用)
    topic_map = _match_finding_topics(task, findings, client, topic_defs)
    # 主题开关过滤:规则捷径可能命中停用主题(如停用安全后 metadata 带 CWE 的
    # 发现),分类完成后统一拦截,并重算进度分母
    enabled_keys = {d["key"] for d in topic_defs}
    disabled_hits = [f.id for f in findings if topic_map[f.id] not in enabled_keys]
    if disabled_hits:
        logger.info(
            "[practice] task=%s 主题开关跳过 %d 条停用主题的 finding",
            task.id, len(disabled_hits),
        )
        findings = [f for f in findings if topic_map[f.id] in enabled_keys]
        topic_map = {f.id: topic_map[f.id] for f in findings}
        total_findings = len(findings)
    # 预构建各主题的 system prompt(工作区可用性统一判定;
    # 内置主题用专有视角,自定义主题用通用模板 + 用户描述)
    system_prompts = {
        t: build_system_prompt(
            t, workspace_available=bool(repo_path), custom_topics=custom_defs,
        )
        for t in set(topic_map.values())
    }

    # 出题起始快照:模型/主题分布/工作区/发现数(排查无题产出时的第一手上下文)
    logger.info(
        "[practice] 开始出题 task=%s user=%s model=%s topics=%s workspace=%s findings=%d",
        task.id, user_id, getattr(client, "model", "?"),
        _topic_stats(topic_map), bool(repo_path), total_findings,
    )

    # 已有 dedup_hash(同用户),避免重复入库
    existing_hashes = {
        row[0] for row in db.query(Question.dedup_hash).filter(
            Question.user_id == user_id
        ).all()
    }

    created: list[Question] = []
    skipped = 0
    fatal_reason = ""  # 非空表示遇到不可重试致命错误,需中止原因冒泡
    for idx, finding in enumerate(findings):
        meta = finding.metadata_ or {}
        topic = topic_map[finding.id]
        system_prompt = system_prompts[topic]
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
        # agent2 标记的学习点:注入考察方向,引导出题聚焦值得学的点
        learning_note = (
            meta.get("learning_note") if isinstance(meta, dict) else None
        )
        if _is_practice_worthy(finding) and learning_note:
            prompt += _LEARNING_NOTE_TEMPLATE.format(note=str(learning_note)[:500])

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
            # 质量关卡 1(始终启用):退化题拦截(叙述式判断题/答案泄露措辞)
            degen_dropped = 0
            if questions:
                sane = [q for q in questions if not _is_degenerate_question(q)]
                degen_dropped = len(questions) - len(sane)
                if degen_dropped:
                    logger.info(
                        "[practice] finding=%s 质量关卡: %d/%d 题为退化题"
                        "(叙述式判断/答案泄露)被丢弃",
                        finding.id, degen_dropped, len(questions),
                    )
                questions = sane
            # 质量关卡 2:工作区可用时无 code_snippet 的题不合格(常识题拦截)
            no_snippet_dropped = 0
            if repo_path and questions:
                qualified = [q for q in questions if q["code_snippet"]]
                no_snippet_dropped = len(questions) - len(qualified)
                if no_snippet_dropped:
                    logger.info(
                        "[practice] finding=%s 质量关卡: %d/%d 题缺 code_snippet 被丢弃",
                        finding.id, no_snippet_dropped, len(questions),
                    )
                questions = qualified
            # 全部被关卡拦截:带对应质量反馈重试一次(退化问题优先反馈)
            if not questions and attempt < PARSE_RETRY and (degen_dropped or no_snippet_dropped):
                feedback = _DEGEN_FEEDBACK if degen_dropped else _NO_CODE_FEEDBACK
                logger.info(
                    "[practice] finding=%s 全部题目被质量关卡拦截,带反馈重试(退化 %d/缺材料 %d)",
                    finding.id, degen_dropped, no_snippet_dropped,
                )
                user_prompt = prompt + feedback
                continue
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
                learning_topic=topic,
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
                learning_topic=topic,  # 该题实际匹配的出题主题
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
