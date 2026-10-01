"""Codex CLI 执行器:OpenAI Codex CLI 集成(通过 codex exec --json + ACP 翻译 bridge)

Codex CLI 不原生支持 ACP 协议,但提供:
- `codex exec --json`:非交互模式,输出 JSONL 流式事件
- `codex exec resume <thread_id>`:恢复之前的会话(多轮对话)
- `~/.codex/config.toml`:模型/provider/审批策略配置

集成方式:
1. registry 注册 codex_cli,指定 bridge_script="codex_bridge"(非默认的 acp_bridge)
2. codex_bridge.py 在沙箱内运行,将 ACP JSON-RPC 翻译为 codex exec 调用
3. pre_bridge_hook 向沙箱写入 ~/.codex/config.toml(模型/provider/base_url 配置)
4. 凭证经环境变量 CODEX_API_KEY 注入(config.toml 的 env_key 指向它)

凭证字段:
- api_key(secret):API Key(注入到 CODEX_API_KEY 环境变量)
- base_url(text,可选):自定义 API 端点(留空用 OpenAI 官方)
- model(text,可选):模型名(留空用 gpt-5)
- wire_api(select):通信协议(固定 Responses API,chat 已被 codex 移除)
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.agents.acp_base import (
    run_acp_agent,
    test_credential_streaming as _base_test_streaming,
)
from app.models.task import Task

logger = logging.getLogger(__name__)


# ============================================================
# Codex config.toml 配置
# ============================================================

# 默认模型(留空时使用)
_DEFAULT_MODEL = "gpt-5"

# wire_api 固定为 responses:Codex 已彻底移除 chat 支持
# (WireApi enum 仅 Responses 一个 variant,"chat" 会导致 config.toml 加载失败)
# 用户端点必须支持 /v1/responses(OpenAI Responses API)


def _codex_pre_bridge_hook(
    session, credentials: dict[str, str], agent_type: str, task: Task | None = None
) -> dict[str, str] | None:
    """bridge 启动前:写入 codex 的 config.toml

    sandbox 模式:写沙箱内 ~/.codex/config.toml(容器独立 home)。
    local 模式:写 local_dir/.codex/config.toml,并返回 CODEX_HOME 环境变量
    指向该目录(acp_base 合并注入 bridge 进程)——codex 支持 CODEX_HOME 重定向,
    确保 CLI 完全不碰宿主机真实 ~/.codex(读不到任务配置/不会覆盖用户配置)。

    Codex 从 config.toml 读取模型/provider 配置,
    环境变量 CODEX_API_KEY 作为 API Key(config.toml 的 env_key 指向它)。

    config.toml 关键字段:
    - model:模型名(如 gpt-5)
    - model_provider:使用的 provider 名(默认 "secondlook")
    - approval_policy:审批策略("never" = 从不审批,非交互模式必须)
    - sandbox_mode:沙箱模式("danger-full-access" = 关闭 Codex 内部沙箱,我们用 OpenSandbox)
    - [model_providers.secondlook]:自定义 provider 配置
      - base_url:API 端点(必须支持 /v1/responses)
      - wire_api:通信协议(固定 "responses",chat 已被 codex 移除)
      - env_key:读取哪个环境变量的 API Key

    命令确认模式(task.params._executor_command_confirm):
    - always_approve(默认 / task=None 测试场景):approval_policy="never"
    - per_command:codex exec --json 是非交互模式,无法暂停等待用户审批,
      强制降级为 approval_policy="never" 并警告(codex exec 不支持 request_permission 透传)

    返回:需额外注入 bridge 进程的环境变量(local 模式 CODEX_HOME),sandbox 返回 None。
    """
    api_key = credentials.get("api_key", "")
    base_url = (credentials.get("base_url") or "").strip()
    model = (credentials.get("model") or "").strip() or _DEFAULT_MODEL
    # wire_api 固定 responses(不读用户配置):codex 已移除 chat 支持(WireApi enum
    # 仅 Responses 一个 variant),用户凭证里若存了旧的 "chat" 会导致 config.toml
    # 加载失败。端点必须支持 /v1/responses(OpenAI Responses API)。
    wire_api = "responses"

    # 读命令确认模式:task=None(测试连接)时默认 always_approve
    approval_mode = "always_approve"
    if task and task.params:
        approval_mode = task.params.get("_executor_command_confirm", "always_approve")

    # codex exec --json 是非交互模式,approval_policy 必须为 "never"
    # (on-request/on-failure/untrusted/granular 需要交互式审批通道,codex exec 不支持)
    # per_command 模式下强制降级为 always_approve 并警告
    if approval_mode == "per_command":
        logger.warning(
            "[codex_cli] per_command 模式不被 codex exec --json 支持(非交互模式),"
            "已降级为 always_approve。如需命令确认,请改用 qoder_cli/deepseek_cli。"
        )
        approval_mode = "always_approve"

    # 构建 config.toml
    # 注意:base_url 留空时不写 model_provider,让 Codex 用默认 OpenAI provider
    if base_url:
        # 自定义端点:写入 [model_providers.secondlook] 表
        config_toml = f"""# Codex CLI 配置(由 SecondLook 自动生成)
# 模型配置
model = "{model}"
model_provider = "secondlook"

# 审批策略:never = 从不审批(非交互模式必须)
# 合法值:untrusted / on-failure / on-request / granular / never
approval_policy = "never"

# 沙箱模式:关闭 Codex 内部沙箱(我们用 OpenSandbox 隔离)
sandbox_mode = "danger-full-access"

# 自定义 model provider
[model_providers.secondlook]
name = "SecondLook Custom Provider"
base_url = "{base_url}"
wire_api = "{wire_api}"
env_key = "CODEX_API_KEY"
"""
    else:
        # 使用 OpenAI 官方端点(不需要自定义 provider)
        config_toml = f"""# Codex CLI 配置(由 SecondLook 自动生成)
# 模型配置
model = "{model}"

# 审批策略:never = 从不审批(非交互模式必须)
# 合法值:untrusted / on-failure / on-request / granular / never
approval_policy = "never"

# 沙箱模式:关闭 Codex 内部沙箱(我们用 OpenSandbox 隔离)
sandbox_mode = "danger-full-access"
"""

    # 写入配置
    if getattr(session, "mode", "") == "local":
        # local 模式:写 local_dir/.codex/(write_file 自动建父目录),
        # CODEX_HOME 重定向后 codex 不读/不写宿主机真实 ~/.codex
        session.write_file(".codex/config.toml", config_toml)
        codex_home = str(session.local_dir / ".codex")
        logger.info(
            f"[codex_cli] config.toml 已写入(local: CODEX_HOME={codex_home}, "
            f"model={model}, base_url={'自定义' if base_url else 'OpenAI默认'}, "
            f"wire_api={wire_api}, approval_mode={approval_mode})"
        )
        return {"CODEX_HOME": codex_home}

    # sandbox 模式:写沙箱内 ~/.codex(容器独立 home)
    # 先创建 ~/.codex 目录
    session.run_command("mkdir -p ~/.codex", timeout=5)
    session.write_file("~/.codex/config.toml", config_toml)
    logger.info(
        f"[codex_cli] config.toml 已写入(model={model}, base_url={'自定义' if base_url else 'OpenAI默认'}, wire_api={wire_api}, approval_mode={approval_mode})"
    )
    return None


# ============================================================
# 执行器入口
# ============================================================


def run_codex_cli_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    repo_context: str | None = None,
    previous_plan: list[dict[str, Any]] | None = None,
    agent_type: str = "codex_cli",
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """跑一轮 Codex CLI 执行器

    通过 codex_bridge.py(ACP 翻译层)与 codex exec --json 交互:
    1. pre_bridge_hook 写入 ~/.codex/config.toml(模型/provider/审批策略)
    2. codex_bridge.py 启动,监听 HTTP 端口
    3. ACPClient 发送 initialize → session/new → session/prompt
    4. codex_bridge.py 收到 session/prompt 后运行 codex exec --json
    5. JSONL 事件翻译为 ACP 通知,经 SSE 流式返回
    6. 首次调用提取 thread_id,后续用 codex exec resume 恢复会话
    """
    return run_acp_agent(
        task=task,
        db=db,
        round_idx=round_idx,
        followup_query=followup_query,
        repo_context=repo_context,
        previous_plan=previous_plan,
        agent_type=agent_type,
        pre_bridge_hook=_codex_pre_bridge_hook,
        # Codex 不需要 credential_env_builder:
        # API Key 经 config.toml 的 env_key="CODEX_API_KEY" 指定,
        # registry 的 credential_env 映射 api_key → CODEX_API_KEY 即可
    )


def test_credential_streaming(db: Session, user_id, agent_type: str):
    """流式测试 Codex CLI 凭证连通性

    流程:
    1. 创建临时沙箱
    2. 写入 ~/.codex/config.toml(含模型/provider 配置)
    3. 启动 codex_bridge.py
    4. 发送测试 prompt("你好,请简短回复确认连接正常")
    5. 流式返回 stage/thinking/content/done 事件
    6. 销毁临时沙箱
    """
    return _base_test_streaming(
        db=db,
        user_id=user_id,
        agent_type=agent_type,
        pre_bridge_hook=_codex_pre_bridge_hook,
    )
