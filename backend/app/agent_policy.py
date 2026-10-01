"""agent 协作策略:默认值定义 + 用户级/任务级合并解析

agent2(质检智能体)的协作策略入口:
- DEFAULT_AGENT_POLICY:全字段默认值(与 AgentPolicy 表列对齐)
- resolve_agent_policy:合并 用户级默认(agent_policies 表)+ 任务级覆盖
  (task.params["_agent_policy"]),返回最终生效的策略

策略项覆盖:agent2 开关、协作总轮次、验证权限(allow_verify /
verifier_auth_mode_default)、执行智能体命令确认默认模式等。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy.orm import Session

from app.models.task import Task

logger = logging.getLogger(__name__)


# ============================================================
# 默认策略 + 配置解析
# ============================================================

# 协作总轮次上限(可通过环境变量 SECONDLOOK_MAX_ROUNDS_LIMIT 调整,默认 10)
# 前端展示的"最大 10"与此对齐;改环境变量后前端需同步(或未来通过 API 下发)
MAX_MAX_ROUNDS = int(os.environ.get("SECONDLOOK_MAX_ROUNDS_LIMIT", "10"))

DEFAULT_AGENT_POLICY: dict[str, Any] = {
    "agent2_enabled": True,  # 是否启用 agent2(关闭=单 agent 模式,跳过评估/验证)
    "max_rounds": 4,  # agent2 协作总轮次
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
