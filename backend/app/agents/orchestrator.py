"""双智能体协作编排器(阶段 4)

驱动 agent2 + react_agent 多轮协作:
1. agent2 初始评估,动态生成覆盖度清单(checklist)+ 给出第一轮指令
2. 用户编辑确认 checklist(orchestrator 阻塞等待)
3. react_agent 执行第一轮(含 clone),输出自然语言总结
4. agent2 对照 checklist 评估 react_agent 结果
5. 若未覆盖完整,agent2 构造追问
6. react_agent 执行追问(不重新 clone)
7. 循环 4-6 直到 done 或达到 MAX_ROUNDS
8. agent2 done=true 时,输出结构化结果(results + grouping),orchestrator 落库

场景降级后的变更:
- checklist 不再从场景读取,由 agent2 第 0 轮动态生成 + 用户编辑确认
- 结果提取不再调 scenario.extract_results,改为直接取 ua_result["results"]
- 结果分组不再从场景声明,改为从 ua_result["grouping"] 读取
- allowed_skills 传给 react_agent(set_current_task),按用户选择过滤 skill

阶段 8(用户澄清):第 0 轮初始评估时,agent2 可输出 ask_user=true
触发用户澄清弹窗。orchestrator 推送 question 事件,后台线程阻塞等待
用户提交答案;答案拼回 user_intent 重新调 agent2。最多 MAX_ASKS 轮提问。
"""
import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agents.executor_agent import get_executor
from app.agents.agent2 import (
    MAX_ASKS,
    MAX_ROUNDS,
    SUPPLEMENT_QUESTION,
    run_agent2,
)
from app.clone_skip import clear_skip_state
from app.config import settings
from app.domain_events import (
    AGENT1_ROUND_COMPLETED,
    CHECKLIST_CONFIRMED,
    QUESTION_RAISED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_STARTED,
    emit,
)
from app.event_bus import finish_task, publish
from app.llm.client import LLMClient
from app.models.task import Conversation, Result, Task, TaskStatus
from app.models.user import User
from app.models.user_llm_config import UserLLMConfig
from app.pause_controller import clear_pause_state, wait_if_paused
from app.perf import perf_log
from app.security import decrypt_secret
from app.tools import sandbox_tools
from app.tools.schema import set_current_git_tokens, set_current_task
from app.user_interaction import (
    clear_pending_checklist,
    clear_pending_question,
    set_pending_checklist,
    set_pending_question,
    wait_for_answers,
    wait_for_checklist_confirmation,
)
from app.agent_checkpoint import resolve_agent_policy
from app.agent_interrupt import clear_interrupt_count, clear_interrupts
from app.user_messages import clear_user_messages
from app.user_interaction import clear_pending_command_confirm, clear_pending_verify_action

logger = logging.getLogger(__name__)

# 完成后重启允许的最大轮次(避免用户反复追加导致无限审计)
MAX_RESUME_ROUNDS = 3


def run_dual_agent_audit(task: Task, db: Session) -> None:
    """执行双智能体协作审计"""
    task_id_str = str(task.id)

    # 先解析 agent 策略(检查点评估频率、打断权限等):
    # 启动阶段文案必须在推送前由 agent2 启停决定,
    # 否则单 agent 模式会先闪现"双智能体协作启动"误导前端
    # 合并用户级默认(agent_policies 表)+ 任务级覆盖(task.params["_agent_policy"])
    agent_policy = resolve_agent_policy(task, db)
    logger.info(
        f"[task={task.id}] agent_policy: K={agent_policy.get('checkpoint_interval')}, "
        f"allow_interrupt={agent_policy.get('allow_interrupt')}, "
        f"max_interrupts={agent_policy.get('max_interrupts_per_round')}"
    )

    # agent2 启停 + 协作总轮次(替代 agent2.py 硬编码 MAX_ROUNDS)
    ua_enabled = bool(agent_policy.get("agent2_enabled", True))
    max_rounds = int(agent_policy.get("max_rounds", MAX_ROUNDS))
    logger.info(
        f"[task={task.id}] agent2_enabled={ua_enabled}, max_rounds={max_rounds}"
    )
    # [perf] 任务启动锚点(含 ua 启停 + 执行器类型,供四次对照实验分组)
    perf_log(
        task.id, "task_start",
        ua_enabled=ua_enabled, executor=(task.executor or "builtin"),
        max_rounds=max_rounds,
    )

    task.status = TaskStatus.RUNNING
    task.current_stage = (
        "双智能体协作启动" if ua_enabled else "单 agent 模式启动(agent2 已禁用)"
    )
    db.commit()
    _publish_status(task)
    # 领域事件:任务启动(订阅者:审计日志等,见 app/domain_events.py)
    emit(
        TASK_STARTED, task.id,
        ua_enabled=ua_enabled, executor=task.executor or "builtin",
        scenario=task.scenario, max_rounds=max_rounds,
    )

    scenario_id = task.scenario

    # 阶段 6:加载 LLM 配置
    # - llm_client:agent2 评估用(来自 task.llm_config_id)
    # - react_client:内置 react_agent 用(来自 task.react_llm_config_id,空时回退到 llm_config_id)
    #   外部 CLI 执行器忽略 react_client(模型由 CLI 自管)
    llm_client = _build_llm_client(db, task.user_id, task.llm_config_id)
    react_client, react_client_source = _build_react_llm_client(db, task)
    logger.info(
        f"[task={task.id}] LLM 配置:agent2=task.llm_config_id,"
        f"react_agent 来源={react_client_source}, executor={task.executor}"
    )

    # 加载用户的各 git provider access_token(解密),供 clone_repo 访问私有仓库
    # 空 dict 表示未绑定,clone_repo_with_fallback 会回退到 SSH/匿名 HTTPS
    git_tokens = _load_git_tokens(db, task.user_id)
    set_current_git_tokens(git_tokens)

    # 场景降级后:设置 task 上下文(含 allowed_skills,供 skill 工具按用户选择过滤)
    # allowed_skills 为 None/空 表示全部 skill 可用(默认)
    allowed_skills = task.allowed_skills
    set_current_task(task_id_str, scenario_id, allowed_skills)

    # 执行器选择:按 task.executor 拿到对应的 ExecutorAgent provider
    # (builtin → 内置 react_agent;registry 中的 agent_type → 外部 CLI via ACP)
    executor = get_executor(task)

    # agent_policy / ua_enabled / max_rounds 已在函数开头解析
    # (启动阶段文案需在推送前由 ua_enabled 决定)

    # 用户原始意图
    user_intent = task.user_input
    params = task.params or {}
    if params.get("repo_url"):
        user_intent += f"\n仓库地址: {params['repo_url']}"
    if params.get("branch"):
        user_intent += f"\n分支: {params['branch']}"
    if params.get("upload_id"):
        user_intent += "\n交付物来源:用户上传的文件(已就绪在任务工作区,无需 clone)"

    # 阶段 8:用户澄清后的意图会拼到这个变量,作为 agent2 后续评估的输入
    effective_intent = user_intent

    # react_agent 历轮结果摘要(给 agent2 评估用)
    react_summaries: list[dict] = []
    all_results_count = 0
    # 修复 4:跨轮 plan 状态(react_agent 之间传递,避免重新规划已完成项)
    current_plan: list[dict] = []
    # 本轮是否正常完成(内存标志,不受外部状态修改影响):
    # finally 兜底 error 推送必须用它判定,不能用 task.status ——
    # 任务完成后用户发追问会把状态改回 RUNNING,若按状态判定,
    # 已正常完成的任务会被误判为"未完成"而推送 error 并重新标记总线结束
    normal_completed = False

    try:
        # ---------- 预处理:若用户选了仓库,主动 clone + list_files ----------
        # 把仓库结构和 repo_path 提前准备好:
        #   - 注入 agent2 第 0 轮:看到结构后能给更精准的初始指令/提问
        #   - 注入 react_agent 第 1 轮:跳过自主 clone,直接开始审计
        # 修复 9:repo_context 不再拼到 effective_intent(避免膨胀所有轮次的
        #   agent2 输入),改为单独传参给 round 0 的 agent2 调用
        # clone 失败不再让整个任务 failed:降级返回 (None, ""),回到
        #   react_agent 自主 clone 路径(有 LLM 重试/自适应,成功率更高)
        _t0 = time.perf_counter()
        repo_path, repo_context = _prepare_repo_context(task, db, task_id_str, git_tokens)
        perf_log(
            task.id, "prepare_repo_context", time.perf_counter() - _t0,
            has_repo=bool((task.params or {}).get("repo_url")),
            cloned=bool(repo_path),
        )

        # ===== 单 agent 模式:agent2 已禁用,跳过评估/打断/验证 =====
        # react_agent 只跑 1 轮,用 summary 作为唯一结构化结果(无 covered/missing 提取)
        if not ua_enabled:
            logger.info(f"[task={task.id}] agent2 已禁用,单 agent 模式")
            task.current_stage = "react_agent 执行(单 agent 模式)"
            db.commit()
            _publish_status(task)

            _t0 = time.perf_counter()
            _results, summary, _plan = executor.run(
                task, db,
                round_idx=1,
                followup_query=None,
                client=react_client,
                repo_context=repo_context,
                previous_plan=None,
                agent_policy=agent_policy,
            )
            perf_log(task.id, "executor_run", time.perf_counter() - _t0, round_idx=1, executor=executor.name)
            emit(
                AGENT1_ROUND_COMPLETED, task.id,
                round_idx=1, executor=executor.name,
                results_count=len(_results),
            )
            react_summaries.append({"round": 1, "summary": summary})

            # 用 summary 作为唯一结构化结果(agent2 已禁用,不做结构化提取)
            structured_results = [{"title": "执行结果", "content": summary}]
            for r in structured_results:
                result = Result(
                    task_id=task.id,
                    round_idx=1,
                    title=r["title"],
                    content=r["content"],
                )
                db.add(result)
            db.commit()
            all_results_count = len(structured_results)

            # 标记完成
            task.status = TaskStatus.COMPLETED
            task.current_stage = (
                f"单 agent 执行完成,共 {len(react_summaries)} 轮,"
                f"共 {all_results_count} 个结果"
            )
            task.completed_at = datetime.now(timezone.utc)
            db.commit()
            _publish_status(task)

            # 单 agent 模式不写 agent2 总结对话(无评估可展示,避免误导)

            # 领域事件:任务完成(单 agent 路径)
            emit(
                TASK_COMPLETED, task.id,
                mode="single_agent", rounds=len(react_summaries),
                results_count=all_results_count,
            )

            # 提前推送 done 事件:results 已落库,让前端立即拉取展示
            publish(task.id, "done", {"status": "completed"})
            # 立即标记总线结束(不等 finally):任务已完成,后续 publish 静默丢弃。
            # 提前标记可消除竞态 —— 若等 finally 再标记,用户在此期间发追问会触发
            # reset_task_bus(清 _finished),随后 finally 的 finish_task 又把它重新置 True,
            # resume 线程后续 publish 全被丢弃(checklist_review 弹窗丢失、任务卡死)。
            finish_task(task.id)
            normal_completed = True

            # 记忆归纳(失败兜底,不影响任务完成)
            try:
                from app.services.memory_summarize import summarize_and_save_memory
                summarize_and_save_memory(task, db, llm_client)
            except Exception as mem_err:
                logger.warning(f"[task={task.id}] 归纳写入记忆失败(忽略): {mem_err}")

            # 自动生成练习题 draft(失败兜底,不影响任务完成;产出仍需用户确认)
            if settings.PRACTICE_ENABLED:
                try:
                    from app.services.practice.auto_generate import auto_generate_practice_for_task
                    auto_generate_practice_for_task(task, db)
                except Exception as practice_err:
                    logger.warning(f"[task={task.id}] 自动生成练习题失败(忽略): {practice_err}")

            # 捕获工作区 diff(失败兜底,不影响任务完成)
            try:
                from app.services.workspace_diff import (
                    save_repo_tree_artifact,
                    save_workspace_diff_artifact,
                )
                save_workspace_diff_artifact(task, db, task_id_str)
                # 树快照:更新为最终态(含新建文件),供不可用时兜底展示
                save_repo_tree_artifact(task, db, task_id_str)
            except Exception as diff_err:
                logger.warning(f"[task={task.id}] 捕获工作区 diff 失败(忽略): {diff_err}")

            return  # 单 agent 模式结束,finally 块仍会执行清理

        # ---------- 第 0 轮:agent2 初始评估(含用户澄清循环) ----------
        task.current_stage = "agent2 初始评估"
        db.commit()
        _publish_status(task)

        ua_result_0: dict | None = None
        ask_round = 0
        while True:
            _t0 = time.perf_counter()
            ua_result_0 = run_agent2(
                effective_intent, [],
                task_id=task.id, db=db, round_idx=0,
                scenario_id=scenario_id,
                client=llm_client,
                ask_round=ask_round,
                repo_context=repo_context,  # 修复 9:仅 round 0 注入,不膨胀 effective_intent
                user_id=task.user_id,
                repo_url=(task.params or {}).get("repo_url"),
                task=task,
                agent_policy=agent_policy,
                repo_path=repo_path,  # round 0 用预克隆结果;agent2 据此启用只读核查工具
            )
            perf_log(task.id, "ua_eval", time.perf_counter() - _t0, round_idx=0)

            # agent2 没请求提问 → 提问循环结束,进入协作阶段
            if not ua_result_0.get("ask_user"):
                _record_agent2(db, task, 0, ua_result_0, ask_round=ask_round)
                break

            # ask_user=true:若已达提问上限(agent2.py 已强制 ask_user=false,
            # 这里是兜底),强制关闭并记录
            if ask_round >= MAX_ASKS:
                logger.warning(
                    f"[task={task.id}] agent2 在 ask_round={ask_round} 仍试图提问,"
                    f"已达上限,强制关闭"
                )
                ua_result_0["ask_user"] = False
                if not ua_result_0.get("followup_query"):
                    ua_result_0["followup_query"] = effective_intent
                _record_agent2(db, task, 0, ua_result_0, ask_round=ask_round)
                break

            # 推送提问并阻塞等待用户答案
            questions = list(ua_result_0.get("questions") or [])
            # 追加固定的"是否有其他补充"问题(最后一题)
            questions.append(dict(SUPPLEMENT_QUESTION))

            _handle_ask_user(db, task, ask_round, questions, ua_result_0)

            # 阻塞后台线程,直到用户提交答案(无限等待)
            answers = wait_for_answers(task.id)
            if not answers:
                # 答案为空(任务被取消或清理),结束提问循环,用最后一次结果兜底
                logger.warning(f"[task={task.id}] 用户答案为空,结束提问循环")
                if not ua_result_0.get("followup_query"):
                    ua_result_0["followup_query"] = effective_intent
                # 不记录 ask_user=true 的评估,直接退出
                break

            # 把答案格式化为文本,拼到 effective_intent,让 agent2 重新评估
            answer_text = _format_user_answers(questions, answers)
            if answer_text:
                effective_intent = user_intent + answer_text
            # 注意:answer 落库已移到 API 端点(submit_task_answer → _record_answer)
            # 同步执行,确保刷新时数据库已有记录。此处不再重复落库。
            ask_round += 1
            # 继续循环:再调 agent2 评估,可能再次 ask_user 或给出 followup_query

        # 兜底:若因异常路径 ua_result_0 为 None,用用户意图作 followup
        if ua_result_0 is None:
            ua_result_0 = {
                "covered": [],
                "missing": [],
                "reasoning": "agent2 未返回有效结果,兜底使用用户原始意图",
                "followup_query": effective_intent,
                "done": False,
                "ask_user": False,
            }

        # ---------- 场景降级后:agent2 第 0 轮动态生成 checklist,用户编辑确认 ----------
        # agent2 在 round 0 输出 checklist 字段(动态生成的覆盖度维度)。
        # orchestrator 推送给前端,阻塞等待用户编辑或"直接采用"。
        # 确认后的 checklist 落库到 task.checklist,后续协作轮 agent2 按此评估。
        generated_checklist = ua_result_0.get("checklist")
        task_checklist: list[dict] | None = None
        if generated_checklist and isinstance(generated_checklist, list):
            task.current_stage = "等待用户确认覆盖度清单"
            db.commit()
            _publish_status(task)

            # 推送 checklist 给前端,并阻塞等待用户确认
            set_pending_checklist(task.id, generated_checklist)
            publish(task.id, "checklist_review", {
                "checklist": generated_checklist,
                "reasoning": ua_result_0.get("reasoning", ""),
            })

            # 阻塞后台线程,直到用户提交编辑/直接采用(无限等待)
            task_checklist = wait_for_checklist_confirmation(task.id)

            # 落库到 task.checklist
            task.checklist = task_checklist
            db.commit()
            logger.info(
                f"[task={task.id}] 覆盖度清单已确认,{len(task_checklist)} 个维度"
            )
            # 领域事件:意图对齐完成(用户确认 checklist 并落库)
            emit(
                CHECKLIST_CONFIRMED, task.id,
                dimensions=len(task_checklist),
                edited=(task_checklist != generated_checklist),
            )

        followup = ua_result_0.get("followup_query", effective_intent)

        # ---------- 协作循环 ----------
        for round_idx in range(1, max_rounds + 1):
            # 暂停检查点:每轮开始前(粗粒度,react_agent 内部还有细粒度检查点)
            wait_if_paused(task.id)

            task.current_stage = f"第 {round_idx} 轮:react_agent 执行"
            db.commit()
            _publish_status(task)

            # react_agent 跑一轮
            # 第 1 轮 followup_query=None(用初始指令);若已主动 clone,传 repo_context
            #   让 react_agent 跳过自主 clone,直接基于已 clone 的仓库开始审计
            # 后续轮 followup_query=追问(不 clone,不传 repo_context)
            # 修复 4:传入上轮 plan(previous_plan),让本轮从已有进度续接;
            #   返回本轮结束时的 plan 供下一轮使用
            # 执行器抽象:按 task.executor 选择 builtin / 外部 CLI provider
            # client=react_client:builtin 用它执行;CLI 忽略
            is_first = round_idx == 1
            _t0 = time.perf_counter()
            _results, summary, current_plan = executor.run(
                task, db,
                round_idx=round_idx,
                followup_query=None if is_first else followup,
                client=react_client,
                repo_context=repo_context if is_first else None,
                previous_plan=current_plan if not is_first else None,
                agent_policy=agent_policy,
            )
            perf_log(task.id, "executor_run", time.perf_counter() - _t0, round_idx=round_idx, executor=executor.name)
            emit(
                AGENT1_ROUND_COMPLETED, task.id,
                round_idx=round_idx, executor=executor.name,
                results_count=len(_results),
            )

            react_summaries.append({
                "round": round_idx,
                "summary": summary,
            })

            # agent2 评估
            # 暂停检查点:react_agent 跑完后、agent2 评估前
            wait_if_paused(task.id)

            task.current_stage = f"第 {round_idx} 轮:agent2 评估"
            db.commit()
            _publish_status(task)

            # agent2 只读核查用的工作区路径:预克隆失败时 agent1 可能已自主
            # clone,故逐轮从会话信息刷新(无会话/未 clone 时为 None,禁用只读工具)
            _ws_info = sandbox_tools.get_workspace_info(task_id_str)
            cur_repo_path = (_ws_info or {}).get("repo_path") or None

            _t0 = time.perf_counter()
            ua_result = run_agent2(
                effective_intent, react_summaries,
                task_id=task.id, db=db, round_idx=round_idx,
                scenario_id=scenario_id,
                client=llm_client,
                ask_round=MAX_ASKS,  # 协作循环阶段不允许再提问
                task_checklist=task_checklist,  # 场景降级后:传已确认的 checklist
                user_id=task.user_id,
                repo_url=(task.params or {}).get("repo_url"),
                task=task,
                agent_policy=agent_policy,
                repo_path=cur_repo_path,
            )
            perf_log(task.id, "ua_eval", time.perf_counter() - _t0, round_idx=round_idx)
            _record_agent2(db, task, round_idx, ua_result)

            if ua_result.get("done"):
                logger.info(f"[task={task.id}] agent2 在第 {round_idx} 轮宣布完成")
                # 场景降级后:结果提取通用化,直接取 ua_result["results"]
                # agent2 done=true 时输出 results + grouping
                structured_results = ua_result.get("results") or []
                grouping = ua_result.get("grouping")
                for r in structured_results:
                    result = Result(
                        task_id=task.id,
                        round_idx=round_idx,
                        title=r.get("title", "(无标题)"),
                        content=r.get("content", ""),
                        metadata_=r.get("metadata"),
                    )
                    db.add(result)
                db.commit()
                all_results_count += len(structured_results)
                # 把 grouping 存到 task.params 供前端读取(结果分组声明)
                if grouping and task.params is not None:
                    task.params = {**(task.params or {}), "_grouping": grouping}
                    db.commit()
                elif grouping and task.params is None:
                    task.params = {"_grouping": grouping}
                    db.commit()
                logger.info(
                    f"[task={task.id}] agent2 整理 {len(structured_results)} 个结构化结果"
                )
                break

            # 评估降级(流式调用失败重试仍失败):不再追问,以当前进度收尾结束,
            # 避免每轮都直连 react_agent 造成重复执行
            if ua_result.get("degraded"):
                logger.warning(
                    f"[task={task.id}] 第 {round_idx} 轮 agent2 评估降级,结束协作循环"
                )
                break

            followup = ua_result.get("followup_query", "")
        else:
            logger.warning(f"[task={task.id}] 达到最大轮次 {max_rounds}")

        # ---------- 标记完成 ----------
        task.status = TaskStatus.COMPLETED
        task.current_stage = (
            f"双智能体协作完成,{len(react_summaries)} 轮,"
            f"共 {all_results_count} 个结果"
        )
        task.completed_at = datetime.now(timezone.utc)
        db.commit()
        _publish_status(task)

        # 领域事件:任务完成(双 agent 协作路径)
        emit(
            TASK_COMPLETED, task.id,
            mode="dual_agent", rounds=len(react_summaries),
            results_count=all_results_count,
        )

        # agent2 最终总结:只展示最终评估本身
        # (轮次/结果数等元信息在任务概览与结果清单已可见,不在对话流重复)
        _add_conversation(
            db, task, round_idx=len(react_summaries),
            role="agent2", type="summary",
            content=ua_result.get("reasoning") or "(未给出最终评估)",
        )

        # 提前推送 done 事件:results 已落库,让前端立即拉取展示
        # (归纳记忆和 git diff 是后台兜底任务,不阻塞前端结果清单展示)
        publish(task.id, "done", {"status": "completed"})
        # 立即标记总线结束(不等 finally):与单 agent 分支同理,消除"用户追问触发的
        # reset_task_bus 被 finally 的 finish_task 重新覆盖"竞态(详见 finally 注释)。
        finish_task(task.id)
        normal_completed = True

        # 任务成功完成:自动归纳写入长期记忆(失败兜底,不影响任务完成)
        try:
            from app.services.memory_summarize import summarize_and_save_memory
            summarize_and_save_memory(task, db, llm_client)
        except Exception as mem_err:
            logger.warning(f"[task={task.id}] 归纳写入记忆失败(忽略): {mem_err}")

        # 自动生成练习题 draft(失败兜底,不影响任务完成;产出仍需用户确认)
        if settings.PRACTICE_ENABLED:
            try:
                from app.services.practice.auto_generate import auto_generate_practice_for_task
                auto_generate_practice_for_task(task, db)
            except Exception as practice_err:
                logger.warning(f"[task={task.id}] 自动生成练习题失败(忽略): {practice_err}")

        # 捕获工作区 diff(失败兜底,不影响任务完成;容器仍存活)
        try:
            from app.services.workspace_diff import (
                save_repo_tree_artifact,
                save_workspace_diff_artifact,
            )
            save_workspace_diff_artifact(task, db, task_id_str)
            # 树快照:更新为最终态(含新建文件),供不可用时兜底展示
            save_repo_tree_artifact(task, db, task_id_str)
        except Exception as diff_err:
            logger.warning(f"[task={task.id}] 捕获工作区 diff 失败(忽略): {diff_err}")

    except Exception as e:
        logger.exception(f"[task={task.id}] 双智能体协作失败")
        # 错误详情增强:消息为空时补异常类型名,避免 UI 显示"未知错误"
        err_detail = _err_detail(e)
        task.status = TaskStatus.FAILED
        task.error_message = err_detail[:1000]
        task.current_stage = "执行失败"
        db.commit()
        _publish_status(task)
        # 领域事件:任务失败(主流程 except 路径)
        emit(TASK_FAILED, task.id, error=err_detail[:500], stage="run")
        _add_conversation(
            db, task, round_idx=0,
            role="agent2", type="error",
            content=f"执行失败: {err_detail}",
        )
        # 失败也尽量捕获:工作区 diff + 仓库树快照(失败兜底;沙箱通常仍存活,
        # 会话已死则自然返回 None,不影响失败处理)
        try:
            from app.services.workspace_diff import (
                save_repo_tree_artifact,
                save_workspace_diff_artifact,
            )
            save_workspace_diff_artifact(task, db, task_id_str)
            save_repo_tree_artifact(task, db, task_id_str)
        except Exception as diff_err:
            logger.warning(f"[task={task.id}] 失败时捕获工作区产物失败(忽略): {diff_err}")
    finally:
        # 阶段 8:清理可能残留的待回答问题状态
        try:
            clear_pending_question(task.id)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 清理待回答问题失败: {cleanup_err}")
        # 场景降级后:清理可能残留的待确认 checklist 状态
        try:
            clear_pending_checklist(task.id)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 清理待确认清单失败: {cleanup_err}")
        # 清理暂停状态(防止任务结束时仍有 in-memory 残留)
        try:
            clear_pause_state(task.id)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 清理暂停状态失败: {cleanup_err}")
        # 清理跳过预克隆标志(未消费时兜底清理,防残留影响后续任务)
        try:
            clear_skip_state(task.id)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 清理跳过标志失败: {cleanup_err}")
        # 清理用户补充消息队列(防止任务结束时仍有 in-memory 残留)
        try:
            clear_user_messages(task.id)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 清理用户消息队列失败: {cleanup_err}")
        # 清理 verifier 待授权动作(防止任务结束时仍有阻塞的验证动作)
        try:
            clear_pending_verify_action(task.id)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 清理验证待授权状态失败: {cleanup_err}")
        # 清理危险命令待确认状态(防止任务结束时仍有阻塞的命令确认)
        try:
            clear_pending_command_confirm(task.id)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 清理命令待确认状态失败: {cleanup_err}")
        # 清理 agent2 中断队列 + 打断计数(防止任务结束时仍有 in-memory 残留)
        try:
            clear_interrupts(task.id)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 清理中断队列失败: {cleanup_err}")
        try:
            clear_interrupt_count(task.id)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 清理打断计数失败: {cleanup_err}")
        # 延迟关闭沙箱:标记任务完成,保留 session 供前端浏览工作区文件
        # 实际清理由 workspace 路由的 cleanup_expired_sessions() 惰性触发(TTL 1 小时)
        try:
            sandbox_tools.mark_task_completed(task_id_str)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 标记任务完成失败: {cleanup_err}")
        # 通知事件总线:任务结束
        # done 事件已在 try 块中提前推送(在归纳记忆/git diff 之前),正常路径
        # 也已同步调用 finish_task(提前标记总线结束,消除与 reset_task_bus 的竞态)。
        # 此处仅兜底:异常路径(本轮未正常完成)推送 error 事件 + 标记总线结束。
        # 注意:判定必须用本轮执行的内存标志 normal_completed,不能用 task.status ——
        # 任务完成后用户发追问会把状态改回 RUNNING(API 端点同步落库),若按状态判定,
        # 已正常完成的任务会被误判为"未完成"而推送 error(前端显示"未知错误"失败横幅),
        # 并重新标记总线结束(resume 线程后续事件全被静默丢弃,任务卡死)。
        if not normal_completed:
            # [诊断] error 事件推送日志:前端 onError 的唯一事件源,全量记录
            logger.warning(
                f"[task={task.id}] finally 兜底推送 error 事件 "
                f"(status={task.status.value}, error_message={task.error_message!r})"
            )
            publish(task.id, "error", {
                "status": "failed",
                "error_message": task.error_message or "未知错误(无异常详情,请查看服务日志)",
            })
            finish_task(task.id)


# ============================================================
# 阶段 8:用户澄清处理
# ============================================================


def _handle_ask_user(
    db: Session,
    task: Task,
    ask_round: int,
    questions: list[dict],
    ua_result: dict,
) -> bool:
    """处理 agent2 的 ask_user 请求

    1. 落库 agent2 的提问(Conversation: role=agent2, type=question)
    2. 设置 pending question(供 API 端点 / 前端恢复查询)
    3. 推送 question 事件给前端 SSE
    4. 更新任务状态为"等待用户回答"
    5. 阻塞等待用户答案(本函数不阻塞,只是设置 pending;实际阻塞在调用方的
       wait_for_answers)

    返回 True 表示已设置 pending,调用方应继续 wait_for_answers;
    返回 False 表示不应等待(异常情况)。
    """
    reasoning = ua_result.get("reasoning", "")

    # 1. 落库提问(agent2 → 用户)
    question_payload = {
        "ask_round": ask_round,
        "questions": questions,
        "reasoning": reasoning,
    }
    # content 用简短文本(便于对话流显示),完整 questions 放 reasoning 字段(JSON)
    short_content = (
        f"[第 {ask_round + 1} 次澄清提问] "
        f"问了 {len(questions) - 1} 个问题 + 1 个补充问题"
    )
    conv = Conversation(
        task_id=task.id,
        round_idx=0,
        role="agent2",
        type="question",
        content=short_content,
        reasoning=json.dumps(question_payload, ensure_ascii=False),
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    # 推送 conversation 事件(让前端在对话流里看到提问记录)
    publish(task.id, "conversation", {
        "id": str(conv.id),
        "round_idx": conv.round_idx,
        "role": conv.role,
        "type": conv.type,
        "content": conv.content,
        "reasoning": conv.reasoning,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
    })

    # 2. 设置 pending question(API 端点和前端恢复弹窗都从这里读)
    set_pending_question(task.id, question_payload)

    # 3. 推送 question 事件(前端收到后弹出 QuestionDialog)
    publish(task.id, "question", {
        "ask_round": ask_round,
        "questions": questions,
        "reasoning": reasoning,
        "conversation_id": str(conv.id),
    })
    # 领域事件:agent2 发起澄清提问
    emit(
        QUESTION_RAISED, task.id,
        ask_round=ask_round, question_count=len(questions),
        conversation_id=str(conv.id),
    )

    # 4. 更新任务状态
    task.current_stage = f"等待用户回答澄清问题(第 {ask_round + 1} 次)"
    db.commit()
    _publish_status(task)

    return True


def _format_user_answers(
    questions: list[dict],
    answers: list[dict],
) -> str:
    """把用户答案格式化为文本,拼到 user_intent 后面

    格式:
        [用户澄清]
        Q1: <问题文本>
        A1: <答案文本>

        Q2: <问题文本>
        A2: <答案文本>
        ...
    """
    # 按 question_id 索引答案
    answer_map: dict[str, dict] = {}
    for a in answers:
        qid = a.get("question_id")
        if qid:
            answer_map[qid] = a

    lines = ["\n\n[用户澄清]"]
    for i, q in enumerate(questions, 1):
        qid = q.get("id", f"q_{i}")
        q_text = q.get("question", f"问题 {i}")
        a = answer_map.get(qid)
        if a is None:
            continue
        value = a.get("value")
        if value is None or value == "":
            continue
        # 多选答案(value 是 list)拼接为逗号分隔
        if isinstance(value, list):
            value_text = ", ".join(str(v) for v in value)
        else:
            value_text = str(value)
        # 跳过空补充
        if qid == "_supplement" and not value_text.strip():
            continue
        lines.append(f"Q{i}: {q_text}")
        lines.append(f"A{i}: {value_text}")
        lines.append("")

    # 若没有任何有效答案,返回空字符串(不拼)
    if len(lines) <= 2:
        return ""

    return "\n".join(lines)


# 注意:用户答案落库(_record_user_answer)已迁移到 API 端点
# submit_task_answer → _record_answer,同步执行,确保刷新时数据库已有记录。


# ============================================================
# 辅助:记录 agent2 的对话
# ============================================================


def _record_agent2(
    db: Session, task: Task, round_idx: int, ua_result: dict,
    ask_round: int | None = None,
) -> None:
    """把 agent2 的输出记录到 Conversation 表

    - content:精简显示,只放追问内容(前端默认展示)
    - reasoning:完整评估(覆盖情况/判断/追问/done),用于刷新页面后回看
    react_agent 接收追问是通过函数参数传递的,不依赖 Conversation 表。

    ask_round:第 0 轮提问循环的轮次(阶段 8)。None 表示非提问循环。
    """
    # 标记已记录(供 orchestrator 兜底逻辑判断)
    ua_result["_recorded"] = True

    covered = ua_result.get("covered", [])
    missing = ua_result.get("missing", [])
    reasoning_text = ua_result.get("reasoning", "")
    followup = ua_result.get("followup_query", "")
    done = ua_result.get("done", False)
    ask_user = ua_result.get("ask_user", False)

    # 精简 content:只显示追问
    if done:
        content = "评估完成,无需追问"
    elif ask_user:
        questions = ua_result.get("questions") or []
        content = f"请求用户澄清({len(questions)} 个问题)"
    elif followup:
        content = followup
    else:
        content = "(未给出追问)"

    # 完整评估 reasoning(可折叠回看)
    full_eval = (
        f"[agent2 第 {round_idx} 轮评估"
        + (f", ask_round={ask_round}" if ask_round is not None else "")
        + "]\n"
        f"已覆盖: {covered}\n"
        f"未覆盖: {missing}\n"
        f"判断: {reasoning_text}\n"
    )
    if ask_user:
        full_eval += f"→ 请求用户澄清({len(ua_result.get('questions') or [])} 个问题)\n"
    if followup:
        full_eval += f"追问: {followup}\n"
    # 追问清单更新(round_idx>0 时 agent2 附带更新后 checklist):落库标记便于追溯
    # (第 0 轮 checklist 是首次生成,另有确认机制,不在此标记)
    if round_idx > 0 and isinstance(ua_result.get("checklist"), list) and ua_result["checklist"]:
        full_eval += f"→ 更新覆盖度清单({len(ua_result['checklist'])} 个维度)\n"
    if done:
        full_eval += "→ 宣布完成\n"

    _add_conversation(
        db, task, round_idx=round_idx,
        role="agent2", type="evaluation",
        content=content,
        reasoning=full_eval,
    )


def _add_conversation(
    db: Session, task: Task, *, round_idx: int, role: str, type: str, content: str,
    reasoning: str | None = None,
) -> None:
    """落库一条对话,同时推送事件给前端 SSE

    reasoning:可选,完整评估/思考链(如 agent2 evaluation 的覆盖情况+判断),
        前端默认折叠,点击展开回看。None 时不落库该字段。
    """
    conv = Conversation(
        task_id=task.id,
        round_idx=round_idx,
        role=role,
        type=type,
        content=content,
        reasoning=reasoning,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    # 推送给事件总线(前端 SSE 实时接收)
    publish(task.id, "conversation", {
        "id": str(conv.id),
        "round_idx": conv.round_idx,
        "role": conv.role,
        "type": conv.type,
        "content": conv.content,
        "reasoning": conv.reasoning,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
    })


def _publish_status(task: Task) -> None:
    """推送任务状态变更事件"""
    publish(task.id, "status", {
        "status": task.status.value if hasattr(task.status, 'value') else str(task.status),
        "current_stage": task.current_stage,
    })


def _build_llm_client(
    db: Session,
    user_id,
    llm_config_id: str | None = None,
    *,
    label: str = "llm_config_id",
) -> LLMClient | None:
    """按 user_id + llm_config_id 加载用户保存的 LLM 配置

    - user_id 为空(匿名任务)或 llm_config_id 为空 → 返回 None,agent 回退到 env 默认
    - 找到指定配置 → 返回 LLMClient.from_config_dict(...)
    - 找不到配置 id 或构造失败 → 记日志并回退到 None

    label 仅用于日志区分(agent2 / react_agent)。
    """
    if user_id is None or not llm_config_id:
        return None
    try:
        cfg = db.query(UserLLMConfig).filter(UserLLMConfig.user_id == user_id).first()
        if cfg is None:
            return None
        # 从配置列表中按 id 查找
        target = None
        for c in cfg.llm_configs:
            if c.get("id") == llm_config_id:
                target = c
                break
        if target is None:
            logger.warning(
                f"[user={user_id}] 未找到 {label}={llm_config_id},回退到 env 默认"
            )
            return None
        return LLMClient.from_config_dict(target)
    except Exception as e:
        logger.warning(f"[user={user_id}] 加载用户 LLM 配置失败({label}),回退到 env 默认: {e}")
        return None


def _build_react_llm_client(
    db: Session,
    task: Task,
) -> tuple[LLMClient | None, str]:
    """构造内置 react_agent 使用的 LLMClient

    仅 executor=builtin 时调用方有意义(外部 CLI 忽略 client)。
    优先级:task.react_llm_config_id → task.llm_config_id(回退) → None(env 默认)

    返回 (client, source_label):
        source_label 取值 "react_llm_config_id" / "llm_config_id" / "env_default",
        仅供日志记录用。
    """
    react_id = task.react_llm_config_id
    if react_id:
        client = _build_llm_client(
            db, task.user_id, react_id, label="react_llm_config_id"
        )
        # 即使 client 为 None(未找到/加载失败)也认为用的是 react_llm_config_id 槽位
        return client, "react_llm_config_id"
    # 回退到 agent2 的配置
    client = _build_llm_client(
        db, task.user_id, task.llm_config_id, label="llm_config_id(fallback)"
    )
    return client, "llm_config_id"


def _load_git_tokens(db: Session, user_id) -> dict[str, str]:
    """加载用户所有 git provider 的 access_token(解密明文)

    返回 {provider: token},只含有 access_token 的 provider(已显式绑定仓库)。
    空 dict 表示未绑定任何平台或解密失败,clone_repo 会回退到 SSH/匿名 HTTPS。
    """
    if user_id is None:
        return {}
    try:
        from app.models.user_git_binding import UserGitBinding

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
                logger.warning(f"[user={user_id}] 解密 {b.provider} token 失败: {e}")
        return tokens
    except Exception as e:
        logger.warning(f"[user={user_id}] 加载 git tokens 失败: {e}")
        return {}


# ============================================================
# 预处理:主动 clone + list_files(把仓库结构提前准备好)
# ============================================================


def _prepare_repo_context(
    task: Task, db: Session, task_id_str: str, git_tokens: dict | None = None,
) -> tuple[str | None, str]:
    """若用户选了仓库,主动 clone + list_files,返回 (repo_path, repo_context 文本)

    - 无 repo_url:返回 (None, ""),走原流程(react_agent 自主 clone)
    - 有 repo_url:clone(HTTPS+token → SSH → HTTPS 匿名,含分支回退);
      失败不抛异常,降级返回 (None, "") → 回到 react_agent 自主 clone 路径
      (工具失败可被 LLM 重试/自适应,成功率高于预 clone 一次定生死);
      成功且仓库非空时 list_files 根目录,格式化成 repo_context 文本;
      仓库为空(或 list_files 失败无法确认非空)则降级返回 (repo_path, ""),
      不注入"已预先 clone"提示,避免误导 agent 在空仓库上直接开始审计

    git_tokens 用于访问私有仓库({provider: token},按 repo_url 主机匹配;
    无匹配则只试 SSH + 匿名 HTTPS)。

    repo_context 会注入:
      - agent2 第 0 轮(拼到 effective_intent):看到结构给更准初始指令
      - react_agent 第 1 轮(传 repo_context 参数):跳过自主 clone 直接审计

    主动 clone 复用 sandbox_tools 的 session 管理,完成后 react_agent / workspace
    路由可通过 task_id 直接复用同一会话(前端工作区侧栏也立即可用)。
    """
    params = task.params or {}
    repo_url = params.get("repo_url")

    # 记忆文件提前写入(不依赖是否选仓库,供执行侧 read_file 查全量):
    # - 全局记忆文件:无条件写(无记忆则写空串清残留)
    # - 项目记忆文件:选了仓库且能匹配到 Project 时写,否则写空串清残留
    _write_memory_files_for_task(task, db, task_id_str, repo_url)

    # 上传交付物分支(优先于 clone;创建时已保证与 repo_url 互斥):
    # 把上传内容传输进沙箱工作区。上传文件 agent 无法自行重新获取,
    # 服务端文件缺失/传输失败都直接失败,不降级
    if params.get("upload_id"):
        return _prepare_upload_context(task, db, task_id_str)

    if not repo_url:
        return None, ""

    branch = params.get("branch")

    # 主动 clone(协议回退:HTTPS+token → SSH → HTTPS 匿名,含分支回退)
    task.current_stage = "正在克隆仓库(HTTPS+token → SSH → 匿名)..."
    db.commit()
    _publish_status(task)

    try:
        clone_result = sandbox_tools.clone_repo_with_fallback(
            repo_url, branch=branch, task_id=task_id_str, git_tokens=git_tokens or {},
            cancellable=True,
        )
    except sandbox_tools.CloneSkippedError:
        # 用户主动跳过预克隆:与失败降级同路径,改由 react_agent 自主克隆
        # (LLM 看报错重试/自适应,历史观察成功率更高)
        logger.info(
            f"[task={task.id}] 用户跳过预克隆,降级为 react_agent 自主克隆"
        )
        task.current_stage = "已跳过预克隆,改由执行阶段自主克隆..."
        db.commit()
        _publish_status(task)
        _add_conversation(
            db, task, round_idx=0,
            role="system", type="warning",
            content="用户已跳过预克隆,改由执行阶段自主克隆(可能多耗时几十秒)",
        )
        return None, ""
    except Exception as e:
        # 降级而非失败:预 clone 一次定生死太脆(网络抖动/分支不符/私有仓库
        # token 问题都会直接挂任务),改为回到 react_agent 自主 clone 路径,
        # 由 LLM 看报错重试/自适应(历史观察该路径成功率明显更高)
        logger.warning(
            f"[task={task.id}] 主动 clone 失败,降级为 react_agent 自主 clone: {e}"
        )
        task.current_stage = "预克隆失败,改由 react_agent 自主克隆..."
        db.commit()
        _publish_status(task)
        _add_conversation(
            db, task, round_idx=0,
            role="system", type="warning",
            content=(
                f"预先克隆仓库失败,已降级为执行阶段自主克隆:\n{str(e)[:500]}"
            ),
        )
        return None, ""
    repo_path = clone_result["path"]
    logger.info(
        f"[task={task.id}] 主动 clone 成功,path={repo_path},"
        f"files_count={clone_result.get('files_count')}"
    )

    # (记忆文件已在任务启动时写入,见 _write_memory_files_for_task)

    # 主动 list_files(根目录),把结构拼进上下文
    task.current_stage = "正在读取仓库根目录结构..."
    db.commit()
    _publish_status(task)

    try:
        files_result = sandbox_tools.list_files(repo_path, task_id=task_id_str)
    except Exception as e:
        # list_files 失败不应让整个任务失败(clone 已成功),降级为只给 repo_path
        logger.warning(f"[task={task.id}] list_files 失败,降级为仅 repo_path: {e}")
        files_result = {"entries": [], "total": 0, "truncated": False}

    # 仓库树快照保底:clone 成功、沙箱健康时立即捕获一份落库,
    # 后续任务失败/零改动/git 异常导致 diff 缺失时,侧栏仍能兜底展示文件清单
    # (任务结束段会再更新为最终态;此处失败不影响主流程)
    try:
        from app.services.workspace_diff import save_repo_tree_artifact
        save_repo_tree_artifact(task, db, task_id_str)
    except Exception as tree_err:
        logger.warning(f"[task={task.id}] 捕获仓库树快照失败(忽略): {tree_err}")

    # 仅当仓库非空才注入上下文:根目录无条目(空仓库/list_files 降级)时,
    # 告诉 agent "已 clone、直接开始审计"会误导,降级为仅 repo_path
    if not files_result.get("entries"):
        logger.info(
            f"[task={task.id}] clone 成功但根目录为空,"
            f"不注入 repo_context(降级为仅 repo_path={repo_path})"
        )
        return repo_path, ""

    repo_context = _format_repo_context(repo_url, repo_path, files_result)
    return repo_path, repo_context


def _prepare_upload_context(
    task: Task, db: Session, task_id_str: str,
) -> tuple[str, str]:
    """上传交付物:把上传内容传输进任务沙箱工作区

    与 clone 分支的差异:
    - 上传文件 agent 无法自主重新获取(clone 失败可降级为 react_agent 自主
      clone 重试,上传没有对应手段),故服务端文件缺失 / 传输失败都直接
      抛异常让任务失败,不降级——降级只会让 agent 对着不存在的仓库盲跑
    - repo_context 的 header 明确"文件已就绪",提示 agent1 跳过 clone
      直接开始处理
    """
    from app.services.uploads import (
        UploadError,
        get_upload_files_dir,
        load_upload_meta,
    )

    upload_id = (task.params or {}).get("upload_id")
    try:
        meta = load_upload_meta(upload_id)
        files_dir = get_upload_files_dir(upload_id)
    except UploadError as e:
        raise RuntimeError(f"上传内容不可用: {e}") from e

    task.current_stage = "正在把上传文件传输进任务工作区..."
    db.commit()
    _publish_status(task)

    repo_path = sandbox_tools.transfer_upload_to_workspace(
        task_id_str, str(files_dir)
    )
    logger.info(f"[task={task.id}] 上传交付物传输完成,path={repo_path}")

    # 根目录列表 → repo_context(同 clone 分支;list_files 复用同一会话)
    try:
        files_result = sandbox_tools.list_files(repo_path, task_id=task_id_str)
    except Exception as e:
        logger.warning(f"[task={task.id}] list_files 失败,降级为仅 repo_path: {e}")
        files_result = {"entries": [], "total": 0, "truncated": False}

    # 仓库树快照保底(同 clone 分支,任务失败/零改动时侧栏兜底展示)
    try:
        from app.services.workspace_diff import save_repo_tree_artifact
        save_repo_tree_artifact(task, db, task_id_str)
    except Exception as tree_err:
        logger.warning(f"[task={task.id}] 捕获仓库树快照失败(忽略): {tree_err}")

    # 空目录不注入 repo_context(同 clone 分支,避免误导 agent)
    if not files_result.get("entries"):
        logger.info(
            f"[task={task.id}] 上传内容根目录为空,不注入 repo_context"
            f"(降级为仅 repo_path={repo_path})"
        )
        return repo_path, ""

    filename = meta.get("filename") or "上传文件"
    file_count = meta.get("file_count") or "?"
    header = (
        f"用户上传的交付物({filename},共 {file_count} 个文件)"
        f"已就绪在 {repo_path},可直接开始处理,无需 clone"
    )
    repo_context = _format_repo_context("", repo_path, files_result, header=header)
    return repo_path, repo_context


def _write_memory_files_for_task(
    task: Task, db: Session, task_id_str: str, repo_url: str | None,
) -> None:
    """把记忆文件写入沙箱固定路径(供 react_agent / CLI 随时 read_file 查阅)

    - 全局记忆文件:写入 UserMemory.content(无则写空串清残留),无条件写
    - 项目记忆文件:按 user_id + repo_url 归一化查 Project,取 memory_content 写入;
      无 Project/无记忆则写空串(清空上一个项目残留,避免看到无关记忆)

    任何异常都 catch + log,不阻塞任务启动。
    """
    try:
        from app.models.user_memory import UserMemory

        global_content = ""
        if task.user_id is not None:
            mem = (
                db.query(UserMemory)
                .filter(UserMemory.user_id == task.user_id)
                .first()
            )
            if mem and mem.content:
                global_content = mem.content.strip()
        sandbox_tools.write_global_memory_file(task_id_str, global_content)
        logger.info(
            f"[task={task.id}] 已写入全局记忆文件 "
            f"(/home/user/.agent_memory/global_memory.md, {len(global_content)} 字符)"
        )
    except Exception as e:
        logger.warning(f"[task={task.id}] 写入全局记忆文件失败(忽略): {e}")

    try:
        from app.models.project import Project
        from app.services.repo_url import normalize_repo_url

        memory_content = ""
        norm = normalize_repo_url(repo_url) if repo_url else ""
        if norm and task.user_id is not None:
            proj = (
                db.query(Project)
                .filter(
                    Project.user_id == task.user_id,
                    Project.repo_url_normalized == norm,
                )
                .first()
            )
            if proj:
                memory_content = proj.memory_content or ""
        sandbox_tools.write_project_memory_file(task_id_str, memory_content)
        logger.info(
            f"[task={task.id}] 已写入项目记忆文件 "
            f"(/home/user/.agent_memory/project_memory.md, {len(memory_content)} 字符)"
        )
    except Exception as e:
        logger.warning(f"[task={task.id}] 写入项目记忆文件失败(忽略): {e}")


def _format_repo_context(
    repo_url: str, repo_path: str, files_result: dict, header: str | None = None,
) -> str:
    """把 clone / 上传结果 + list_files 结果格式化成给 agent 看的上下文文本

    格式:
        仓库 <url> 已克隆到 <path>   (或 header 自定义首行,如上传交付物)
        根目录结构(共 N 项):
          [目录] src
          [文件] README.md (1234 B)
          ...
    """
    entries = files_result.get("entries", [])
    total = files_result.get("total", 0)
    truncated = files_result.get("truncated", False)

    lines = [header or f"仓库 {repo_url} 已克隆到 {repo_path}"]
    trunc_hint = ", 已截断(仅显示部分)" if truncated else ""
    lines.append(f"根目录结构(共 {total} 项{trunc_hint}):")
    for e in entries:
        etype = e.get("type", "file")
        name = e.get("name", "?")
        if etype == "dir":
            lines.append(f"  [目录] {name}")
        else:
            size = e.get("size", 0)
            size_str = f" ({size} B)" if size else ""
            lines.append(f"  [文件] {name}{size_str}")

    return "\n".join(lines)


def _restore_workspace_if_needed(
    task: Task, db: Session, task_id_str: str, git_tokens: dict | None = None,
) -> bool:
    """沙箱会话已被回收且任务配了仓库 → 重新克隆恢复工作区

    判断与 retry_failed_task 一致:无 repo_url 或会话存活时跳过。
    用户追问(resume)与失败重试共用本助手,消除两条链路在
    工作区恢复上的不对称——否则追问轮只能新建空沙箱,
    react_agent 拿不到 repo_path 提示,行为不确定。

    _prepare_repo_context 内部已写记忆文件,且 clone 失败时降级
    不抛异常(续跑时 react_agent 可自主克隆),故返回 True 后
    调用方无需再写记忆文件。返回是否执行了恢复动作。

    上传任务(upload_id)同样适用:沙箱回收后重新传输上传内容
    (agent 无法自行重新获取,必须由服务端恢复)。
    """
    params = task.params or {}
    repo_url = params.get("repo_url")
    upload_id = params.get("upload_id")
    if (
        not (repo_url or upload_id)
        or sandbox_tools.get_workspace_info(task_id_str) is not None
    ):
        return False
    is_upload_task = bool(upload_id)
    logger.info(
        f"[task={task.id}] 沙箱会话已回收,"
        + ("重新传输上传文件恢复工作区" if is_upload_task else "重新克隆仓库恢复工作区")
    )
    task.current_stage = (
        "工作区已被回收,重新传输上传文件恢复..."
        if is_upload_task
        else "工作区已被回收,正在重新克隆恢复..."
    )
    db.commit()
    _publish_status(task)
    _prepare_repo_context(task, db, task_id_str, git_tokens)
    return True


# ============================================================
# 完成后重启:用户在 task 完成后追加消息,触发新一轮协作
# ============================================================


def _checklist_changed(
    old: list[dict] | None, new: list[dict],
) -> bool:
    """判断 agent2 输出的 checklist 与现有清单是否有实质差异

    逐维度比较 id + name + description + 子项,任一不同即视为变更。
    用于追问清单更新时避免 agent2 输出与原清单相同的 checklist
    触发无意义的确认弹窗。
    """
    def _norm(cl: list[dict] | None) -> list[tuple]:
        return [
            (
                str(d.get("id", "")),
                str(d.get("name", "")),
                str(d.get("description", "")),
                [str(x) for x in (d.get("checklist") or [])],
            )
            for d in (cl or [])
            if isinstance(d, dict)
        ]
    return _norm(old) != _norm(new)


def resume_audit_with_message(
    task: Task, db: Session, user_message: str, retry: bool = False,
) -> None:
    """用户在任务完成后追加消息,重启协作循环

    retry=True 时表示失败任务重试(断点续跑),消息措辞与阶段文案
    改为重试语境,其余流程一致。

    流程:
    1. task.status: COMPLETED/FAILED → RUNNING
    2. 加载历史上下文(react_summaries / task_checklist / LLM 配置)
    3. 起始 round_idx:用户追加消息时复用消息所在轮(消息与首轮 react 执行
       同轮,不隔轮);失败重试时从 max+1 续接新轮
    4. 先调 agent2 分析用户消息(对照已有 checklist,输出 followup_query)
    5. 启动协作循环(react_agent + agent2 评估),最多 MAX_RESUME_ROUNDS 轮;
       分析评估与首轮 react 执行共享起始 round_idx,不单独占轮
    6. done 或达到上限时结束,task.status → COMPLETED

    用户消息本身已由 API 端点落库为 Conversation(role=user, type=message),
    本函数不重复落库。

    注意:本函数由 API 端点在独立后台线程中调用(类似 _run_task_in_background),
    与原 run_dual_agent_audit 互斥(任务从 completed 改回 running 时,
    原后台线程已结束)。
    """
    task_id_str = str(task.id)

    task.status = TaskStatus.RUNNING
    task.current_stage = (
        "重试失败任务,恢复执行" if retry else "用户追加消息,重启执行"
    )
    task.error_message = None  # 清除之前的错误信息(若有)
    db.commit()
    _publish_status(task)

    # 加载上下文(与 run_dual_agent_audit 一致)
    # llm_client:agent2 评估;react_client:内置 react_agent(空时回退到 llm_config_id)
    llm_client = _build_llm_client(db, task.user_id, task.llm_config_id)
    react_client, _ = _build_react_llm_client(db, task)
    git_tokens = _load_git_tokens(db, task.user_id)
    set_current_git_tokens(git_tokens)
    allowed_skills = task.allowed_skills
    set_current_task(task_id_str, task.scenario, allowed_skills)

    # 执行器选择:按 task.executor 拿到对应的 ExecutorAgent provider
    executor = get_executor(task)

    # 加载 agent 策略(检查点评估频率、打断权限等)
    # 合并用户级默认(agent_policies 表)+ 任务级覆盖(task.params["_agent_policy"])
    agent_policy = resolve_agent_policy(task, db)
    logger.info(
        f"[task={task.id}] resume agent_policy: K={agent_policy.get('checkpoint_interval')}, "
        f"allow_interrupt={agent_policy.get('allow_interrupt')}, "
        f"max_interrupts={agent_policy.get('max_interrupts_per_round')}"
    )

    # agent2 启停(重启场景)
    ua_enabled = bool(agent_policy.get("agent2_enabled", True))
    logger.info(
        f"[task={task.id}] resume agent2_enabled={ua_enabled}"
    )

    task_checklist = task.checklist
    react_summaries = _load_react_summaries(db, task.id)
    # 重启时不复用旧 plan(让 LLM 根据新消息重新规划)
    current_plan: list[dict] = []

    # 起始轮:用户追加消息时复用消息所在轮(消息已由 API 端点落库为最新轮,
    # 分析评估与首轮 react 执行与该消息同轮——用户消息 → 分析 → 执行 → 产出评估
    # 构成一轮完整协作闭环,不隔轮);失败重试时无新消息,从 max+1 续接新轮。
    # 循环最多跑 MAX_RESUME_ROUNDS 轮 react 执行。
    start_round_idx = _get_next_round_idx(db, task.id, retry=retry)
    max_rounds = start_round_idx + MAX_RESUME_ROUNDS - 1
    # [perf] resume 锚点(用户追加消息后重启;与 user_message 锚点配对算总延迟)
    perf_log(
        task.id, "resume_start",
        ua_enabled=ua_enabled, executor=executor.name,
        start_round=start_round_idx,
    )

    # 会话已被回收且配了仓库 → 与重试链路一致,重新克隆恢复工作区
    # (_prepare_repo_context 内部已写记忆文件);否则幂等补写记忆文件
    # (会话存活时覆盖同内容,保证执行侧 read_file 能查到全量记忆)
    # 重试链路进入本函数前已自行恢复过工作区,此处会话存活会自然跳过,不会双重 clone
    _t0 = time.perf_counter()
    repo_url = (task.params or {}).get("repo_url")
    restored = _restore_workspace_if_needed(task, db, task_id_str, git_tokens)
    if not restored:
        _write_memory_files_for_task(task, db, task_id_str, repo_url)
    perf_log(
        task.id,
        "restore_workspace" if restored else "write_memory_files",
        time.perf_counter() - _t0,
    )

    # 把用户消息拼到 user_intent 后面,让 agent2 把它视为新的检查方向
    # 重试场景用专门标记,避免 agent2 把续跑当成用户新增需求
    msg_label = "[重试续跑]" if retry else "[用户追加消息]"
    effective_intent = task.user_input + f"\n\n{msg_label}\n{user_message}"

    # 本轮是否正常完成(内存标志):与 run_dual_agent_audit 同理,
    # finally 兑底 error 推送必须用它判定,不能用 task.status ——
    # 本轮完成后用户可能又发新追问(状态被改回 RUNNING),旧线程 finally
    # 按状态判定会误推 error 并重新标记总线结束,导致新 resume 线程事件全丢。
    normal_completed = False

    try:
        # ===== 单 agent 模式:agent2 已禁用,跳过评估,直接跑 react_agent =====
        if not ua_enabled:
            logger.info(f"[task={task.id}] resume 单 agent 模式(agent2 已禁用)")
            task.current_stage = f"第 {start_round_idx} 轮:react_agent 执行(单 agent)"
            db.commit()
            _publish_status(task)

            _t0 = time.perf_counter()
            _results, summary, current_plan = executor.run(
                task, db,
                round_idx=start_round_idx,
                followup_query=user_message,
                client=react_client,
                repo_context=None,
                previous_plan=None,
                agent_policy=agent_policy,
            )
            perf_log(task.id, "executor_run", time.perf_counter() - _t0, round_idx=start_round_idx, executor=executor.name)
            react_summaries.append({"round": start_round_idx, "summary": summary})

            # 用 summary 作为唯一结构化结果
            structured_results = [{"title": "执行结果", "content": summary}]
            for r in structured_results:
                result = Result(
                    task_id=task.id,
                    round_idx=start_round_idx,
                    title=r["title"],
                    content=r["content"],
                )
                db.add(result)
            db.commit()

            ua_result = None  # 单 agent 模式无 agent2 评估,_finish_resume 据此写简洁总结
            _finish_resume(task, db, react_summaries, ua_result)
            normal_completed = True  # 正常完成:finally 不再兑底推 error(见 finally 注释)
            return  # finally 块仍会执行清理

        # 先调 agent2 分析用户消息(round_idx = start_round_idx)
        task.current_stage = f"第 {start_round_idx} 轮:agent2 分析用户消息"
        db.commit()
        _publish_status(task)

        # agent2 只读核查用的工作区路径(resume 时会话可能存活/刚恢复/不存在)
        _ws_info = sandbox_tools.get_workspace_info(task_id_str)
        cur_repo_path = (_ws_info or {}).get("repo_path") or None

        _t0 = time.perf_counter()
        ua_result = run_agent2(
            effective_intent, react_summaries,
            task_id=task.id, db=db, round_idx=start_round_idx,
            scenario_id=task.scenario, client=llm_client,
            ask_round=MAX_ASKS,  # 重启不允许提问
            task_checklist=task_checklist,
            user_id=task.user_id,
            repo_url=(task.params or {}).get("repo_url"),
            task=task,
            agent_policy=agent_policy,
            # 追问清单更新模式:仅用户追问启用(重试续跑不是新需求,不更新清单)
            checklist_update_mode=not retry,
            repo_path=cur_repo_path,
        )
        perf_log(task.id, "ua_eval", time.perf_counter() - _t0, round_idx=start_round_idx, phase="analyze_message")

        # 流式调用降级标记(agent2 重试仍失败时返回 degraded=true):
        # 跳过清单确认环节,直接把用户输入内容交给 react_agent 执行
        degraded = bool(ua_result.get("degraded"))

        # ---------- 追问清单更新:agent2 输出更新后 checklist,再次向用户确认 ----------
        # 复用第 0 轮的确认机制(set_pending_checklist → checklist_review 事件 →
        # 阻塞等待)。确认后的清单覆写 task.checklist,后续 resume 循环评估按新清单。
        # 在 _record_agent2 之前处理,确保落库的评估反映最终 done 状态。
        if not degraded and not retry:
            updated_checklist = ua_result.get("checklist")
            if isinstance(updated_checklist, list):
                # 过滤畸形条目(至少需有 id)
                updated_checklist = [
                    d for d in updated_checklist
                    if isinstance(d, dict) and d.get("id")
                ]
            else:
                updated_checklist = []
            if updated_checklist and _checklist_changed(task.checklist, updated_checklist):
                task.current_stage = "等待用户确认覆盖度清单更新"
                db.commit()
                _publish_status(task)

                set_pending_checklist(task.id, updated_checklist)
                publish(task.id, "checklist_review", {
                    "checklist": updated_checklist,
                    "reasoning": ua_result.get("reasoning", ""),
                })

                # 阻塞后台线程,直到用户提交编辑/直接采用(无限等待)
                confirmed = wait_for_checklist_confirmation(task.id)
                if confirmed:
                    # 落库到 task.checklist,后续循环评估用新清单
                    task.checklist = confirmed
                    task_checklist = confirmed
                    db.commit()
                    logger.info(
                        f"[task={task.id}] 追问清单更新已确认,{len(confirmed)} 个维度"
                    )
                    # 用户确认了新清单,react_agent 必须跑一轮执行追问需求,
                    # 不允许直接结束(兜底:LLM 可能同时输出 done=true)
                    ua_result["done"] = False

        _record_agent2(db, task, start_round_idx, ua_result)

        # agent2 认为用户消息无需新检查,直接结束
        if ua_result.get("done"):
            _persist_structured_results(db, task, start_round_idx, ua_result)
            _finish_resume(task, db, react_summaries, ua_result)
            normal_completed = True  # 正常完成
            return

        # 启动协作循环:react_agent 执行 + agent2 评估
        # 降级时直接把用户输入内容交给 react_agent(不经过 agent2 生成的指令,
        # 它已不可用;react_agent 有历史上下文与 previous_plan 可续接)
        # 首轮(round_idx == start_round_idx)与前面的分析评估共享轮号:
        # 用户消息 → 分析评估 → react 执行 → 产出评估,一轮完整协作闭环
        followup = (
            user_message if degraded
            else ua_result.get("followup_query", user_message)
        )
        for round_idx in range(start_round_idx, max_rounds + 1):
            # 暂停检查点:每轮开始前
            wait_if_paused(task.id)

            task.current_stage = f"第 {round_idx} 轮:react_agent 执行"
            db.commit()
            _publish_status(task)

            _t0 = time.perf_counter()
            _results, summary, current_plan = executor.run(
                task, db,
                round_idx=round_idx,
                followup_query=followup,
                client=react_client,
                repo_context=None,  # 重启不传 repo_context(仓库已 clone,react_agent 自行从 sandbox 取)
                previous_plan=current_plan if round_idx > start_round_idx else None,
                agent_policy=agent_policy,
            )
            perf_log(task.id, "executor_run", time.perf_counter() - _t0, round_idx=round_idx, executor=executor.name)
            react_summaries.append({"round": round_idx, "summary": summary})

            # 暂停检查点:react_agent 跑完后、agent2 评估前
            wait_if_paused(task.id)

            task.current_stage = f"第 {round_idx} 轮:agent2 评估"
            db.commit()
            _publish_status(task)

            # agent2 只读核查用的工作区路径:agent1 本轮可能刚自主 clone,逐轮刷新
            _ws_info = sandbox_tools.get_workspace_info(task_id_str)
            cur_repo_path = (_ws_info or {}).get("repo_path") or None

            _t0 = time.perf_counter()
            ua_result = run_agent2(
                effective_intent, react_summaries,
                task_id=task.id, db=db, round_idx=round_idx,
                scenario_id=task.scenario, client=llm_client,
                ask_round=MAX_ASKS,
                task_checklist=task_checklist,
                user_id=task.user_id,
                repo_url=(task.params or {}).get("repo_url"),
                task=task,
                agent_policy=agent_policy,
                repo_path=cur_repo_path,
            )
            perf_log(task.id, "ua_eval", time.perf_counter() - _t0, round_idx=round_idx)
            _record_agent2(db, task, round_idx, ua_result)

            if ua_result.get("done"):
                _persist_structured_results(db, task, round_idx, ua_result)
                break

            # 评估降级(流式调用失败重试仍失败):不再追问,以当前进度收尾结束,
            # 避免每轮都直连 react_agent 造成重复执行
            if ua_result.get("degraded"):
                logger.warning(
                    f"[task={task.id}] 第 {round_idx} 轮 agent2 评估降级,结束协作循环"
                )
                break

            followup = ua_result.get("followup_query", "")
        else:
            logger.warning(
                f"[task={task.id}] 重启审计达到最大轮次 {max_rounds}"
            )

        _finish_resume(task, db, react_summaries, ua_result)
        normal_completed = True  # 正常完成(循环结束或 done 分支跳出)

    except Exception as e:
        err_stage = "重试执行失败" if retry else "重启执行失败"
        logger.exception(f"[task={task.id}] {err_stage}")
        # 错误详情增强:消息为空时补异常类型名,避免 UI 显示"未知错误"
        err_detail = _err_detail(e)
        task.status = TaskStatus.FAILED
        task.error_message = err_detail[:1000]
        task.current_stage = err_stage
        db.commit()
        _publish_status(task)
        # 领域事件:任务失败(resume/重试续跑 except 路径)
        emit(TASK_FAILED, task.id, error=err_detail[:500], stage=err_stage)
        _add_conversation(
            db, task, round_idx=0,
            role="agent2", type="error",
            content=f"{err_stage}: {err_detail}",
        )
        # 失败也尽量捕获:工作区 diff + 仓库树快照(失败兜底;沙箱通常仍存活,
        # 会话已死则自然返回 None,不影响失败处理)
        try:
            from app.services.workspace_diff import (
                save_repo_tree_artifact,
                save_workspace_diff_artifact,
            )
            save_workspace_diff_artifact(task, db, task_id_str)
            save_repo_tree_artifact(task, db, task_id_str)
        except Exception as diff_err:
            logger.warning(f"[task={task.id}] 失败时捕获工作区产物失败(忽略): {diff_err}")
    finally:
        # 清理资源(与 run_dual_agent_audit 对齐)
        for cleanup_fn, name in [
            (clear_pending_question, "待回答问题"),
            (clear_pending_checklist, "待确认清单"),
            (clear_pause_state, "暂停状态"),
            (clear_skip_state, "跳过预克隆标志"),
            (clear_user_messages, "用户消息队列"),
            (clear_pending_verify_action, "验证待授权状态"),
            (clear_interrupts, "中断队列"),
            (clear_interrupt_count, "打断计数"),
        ]:
            try:
                cleanup_fn(task.id)
            except Exception as cleanup_err:
                logger.warning(f"[task={task.id}] 清理{name}失败: {cleanup_err}")
        try:
            sandbox_tools.mark_task_completed(task_id_str)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 标记任务完成失败: {cleanup_err}")
        # 推送终止事件
        # done 事件已在 _finish_resume 中提前推送(在归纳记忆/git diff 之前),正常路径
        # 也已同步调用 finish_task(提前标记总线结束,消除与 reset_task_bus 的竞态)。
        # 此处仅兑底:异常路径(本轮未正常完成)推送 error 事件 + 标记总线结束。
        # 判定必须用内存标志 normal_completed,不能用 task.status ——
        # 本轮完成后用户又发新追问会把状态改回 RUNNING,按状态判定会误推 error
        # 并重新标记总线结束,导致新 resume 线程事件全被静默丢弃。
        if not normal_completed:
            # [诊断] error 事件推送日志:前端 onError 的唯一事件源,全量记录
            logger.warning(
                f"[task={task.id}] resume finally 兑底推送 error 事件 "
                f"(status={task.status.value}, error_message={task.error_message!r})"
            )
            publish(task.id, "error", {
                "status": "failed",
                "error_message": task.error_message or "未知错误(无异常详情,请查看服务日志)",
            })
            finish_task(task.id)


# ============================================================
# 失败任务重试(断点续跑优先,无进度时从头重跑)
# ============================================================


def retry_failed_task(task: Task, db: Session) -> None:
    """失败任务重试入口:断点续跑优先,无可续进度时从头重跑

    判定依据:是否已有 agent2 / react_agent 的对话落库
    - 无(预克隆/沙箱/LLM 配置等早期失败,执行未真正开始):
      没有可续内容,直接重跑 run_dual_agent_audit
    - 有(执行中途失败):复用 resume_audit_with_message 断点续跑,
      以重试续跑消息驱动 agent2 分析现状、接续未完成工作
      (round_idx 由 _get_next_round_idx 自动续接,不产生重复轮次)

    续跑前的沙箱探测:失败 finally 已 mark_task_completed,session
    超 1 小时 TTL 被回收(或后端重启)后已 clone 的仓库丢失;若 session
    不在且任务配了 repo_url,重新 clone 恢复工作区,避免续跑时执行器
    找不到仓库。

    注意:本函数由 API 端点在独立后台线程中调用(与 resume 一致),
    重试时原后台线程已因异常退出,互斥无竞态。
    """
    task_id_str = str(task.id)
    # 先捕获失败原因(后续会清 error_message),拼进续跑消息供 agent2 参考
    # (error_message 已由失败路径增强为非空,这里仅兜底旧数据)
    last_error = task.error_message or "未知错误(无异常详情)"
    perf_log(task.id, "retry_start", last_error_chars=len(last_error))

    has_progress = (
        db.query(Conversation.id)
        .filter(
            Conversation.task_id == task.id,
            Conversation.role.in_(("agent1", "agent2")),
        )
        .first()
        is not None
    )

    if not has_progress:
        # 早期失败:无可续内容,从头重跑(状态流转/清理由其内部处理)
        logger.info(f"[task={task.id}] 重试:无可续进度,从头重跑")
        task.error_message = None
        db.commit()
        run_dual_agent_audit(task, db)
        return

    # 执行中途失败:沙箱会话已被回收时,重新 clone 恢复工作区
    repo_url = (task.params or {}).get("repo_url")
    if repo_url and sandbox_tools.get_workspace_info(task_id_str) is None:
        logger.info(
            f"[task={task.id}] 重试:沙箱会话已回收,重新克隆仓库恢复工作区"
        )
        task.status = TaskStatus.RUNNING
        task.current_stage = "重试失败任务,正在恢复工作区..."
        task.error_message = None
        db.commit()
        _publish_status(task)
        git_tokens = _load_git_tokens(db, task.user_id)
        set_current_git_tokens(git_tokens)
        # clone 失败时 _prepare_repo_context 已降级不抛异常
        # (续跑时 react_agent 可自主克隆,不阻塞重试)
        _prepare_repo_context(task, db, task_id_str, git_tokens)

    # 以重试续跑消息恢复执行(状态流转/记忆文件重建/轮次续接由 resume 链路处理)
    retry_message = (
        f"该任务上一次执行因错误中断: {last_error}\n"
        "这是一次失败重试(断点续跑),不是用户的新需求: "
        "请基于已有进度继续完成原任务,不要重做已完成的部分。"
    )
    resume_audit_with_message(task, db, retry_message, retry=True)


def _err_detail(e: Exception) -> str:
    """提取人类可读的错误详情:异常消息为空时补异常类型名,杜绝"未知错误"字面"""
    text = str(e)
    return text if text else f"{type(e).__name__}(无错误详情)"


def _finish_resume(
    task: Task, db: Session, react_summaries: list[dict], ua_result: dict | None,
) -> None:
    """重启执行完成:标记 task 状态 + 写最终总结对话

    ua_result=None 表示单 agent 模式(agent2 已禁用):
    无 agent2 评估可展示,不写总结对话。
    """
    task.status = TaskStatus.COMPLETED
    task.current_stage = f"重启执行完成,共 {len(react_summaries)} 轮"
    task.completed_at = datetime.now(timezone.utc)
    db.commit()
    _publish_status(task)
    # 领域事件:任务完成(resume/重试续跑路径)
    emit(TASK_COMPLETED, task.id, mode="resume", rounds=len(react_summaries))
    if ua_result is not None:
        # 与主流程一致:总结只展示最终评估本身
        _add_conversation(
            db, task, round_idx=len(react_summaries),
            role="agent2", type="summary",
            content=ua_result.get("reasoning") or "(未给出最终评估)",
        )

    # 提前推送 done 事件:results 已落库,让前端立即拉取展示
    # (归纳记忆和 git diff 是后台兑底任务,不阻塞前端结果清单展示)
    publish(task.id, "done", {"status": "completed"})
    # 立即标记总线结束(不等 resume 线程 finally):与 run_dual_agent_audit 同理,
    # 消除"用户再发新追问触发的 reset_task_bus 被旧线程 finally 的 finish_task 重新覆盖"
    # 竞态 —— 否则新 resume 线程 publish 全被丢弃(checklist_review 弹窗丢失、任务卡死)。
    finish_task(task.id)
    
    # 重启完成:自动归纳写入长期记忆(失败兑底,不影响;client 用默认,归纳是简单任务)
    try:
        from app.services.memory_summarize import summarize_and_save_memory
        summarize_and_save_memory(task, db, None)
    except Exception as mem_err:
        logger.warning(f"[task={task.id}] 归纳写入记忆失败(忽略): {mem_err}")

    # 捕获工作区 diff(失败兜底,不影响任务完成;容器仍存活)
    try:
        from app.services.workspace_diff import (
            save_repo_tree_artifact,
            save_workspace_diff_artifact,
        )
        save_workspace_diff_artifact(task, db, str(task.id))
        # 树快照:更新为最终态(含新建文件),供不可用时兜底展示
        save_repo_tree_artifact(task, db, str(task.id))
    except Exception as diff_err:
        logger.warning(f"[task={task.id}] 捕获工作区 diff 失败(忽略): {diff_err}")


def _load_react_summaries(db: Session, task_id) -> list[dict]:
    """从 Conversation 表加载历史 react_agent 总结(供 agent2 评估)

    按 round_idx 升序,取每个 round 的最后一条 thinking content 作为该轮 summary。
    (react_agent 内部把每轮最终思考落库为 type=thinking,content 即为 summary)
    """
    convs = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.role == "agent1",
            Conversation.type == "thinking",
        )
        .order_by(Conversation.round_idx.asc(), Conversation.created_at.asc())
        .all()
    )
    summaries_by_round: dict[int, str] = {}
    for c in convs:
        if c.content:
            summaries_by_round[c.round_idx] = c.content
    return [
        {"round": r, "summary": s}
        for r, s in sorted(summaries_by_round.items())
    ]


def _get_next_round_idx(db: Session, task_id, retry: bool = False) -> int:
    """resume 起始轮计算:

    - 用户追加消息(retry=False):消息已由 API 端点落库到最新轮
      (round = max(Conversation.round_idx)),此处复用该轮号——
      分析评估与首轮 react 执行与用户消息同轮,消息位于轮首,不隔轮
    - 失败重试(retry=True):无新用户消息落库,从 max+1 续接新轮

    无对话记录时返回 1(理论上不会发生,因为已完成的任务一定有对话)。
    """
    latest = (
        db.query(Conversation)
        .filter(Conversation.task_id == task_id)
        .order_by(Conversation.round_idx.desc())
        .first()
    )
    if not latest:
        return 1
    return latest.round_idx if not retry else latest.round_idx + 1


def _persist_structured_results(
    db: Session, task: Task, round_idx: int, ua_result: dict,
) -> None:
    """落库结构化结果(从 ua_result 提取 results + grouping)

    与 run_dual_agent_audit 协作循环里 done=true 的落库逻辑一致,
    抽出复用避免代码重复。
    """
    structured_results = ua_result.get("results") or []
    grouping = ua_result.get("grouping")
    for r in structured_results:
        result = Result(
            task_id=task.id,
            round_idx=round_idx,
            title=r.get("title", "(无标题)"),
            content=r.get("content", ""),
            metadata_=r.get("metadata"),
        )
        db.add(result)
    db.commit()
    # 把 grouping 存到 task.params 供前端读取(结果分组声明)
    if grouping:
        if task.params is not None:
            task.params = {**(task.params or {}), "_grouping": grouping}
        else:
            task.params = {"_grouping": grouping}
        db.commit()
    logger.info(
        f"[task={task.id}] agent2 整理 {len(structured_results)} 个结构化结果"
    )
