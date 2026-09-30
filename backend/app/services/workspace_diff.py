"""任务完成时捕获工作区变更(diff/patch),持久化到 task_artifacts。

由 orchestrator 在任务成功完成段调用(容器仍存活:mark_task_completed 只打时间戳,
真正销毁是 workspace 路由的 cleanup_expired_sessions 惰性触发,TTL 1h)。

捕获范围:工作区所有修改
- 已跟踪文件(已暂存 + 未暂存):git diff HEAD
- 未跟踪文件:git ls-files --others --exclude-standard -z 列出,逐个读内容拼成 new file patch
  - -z null 分隔,正确处理含空格/特殊字符的路径
  - 直接列出每个文件(递归展开目录,不会丢目录内文件)
  - 逐文件 try/except,单文件失败(二进制解码等)不影响其余

失败兜底:任何异常都 catch + log,不影响任务完成状态(与 memory_summarize 同范式)。
"""
import logging
import re

from sqlalchemy.orm import Session

from app.models.task_artifact import TaskArtifact
from app.tools import sandbox_tools

logger = logging.getLogger(__name__)

# 单条 artifact 上限(避免超大 diff 撑爆 DB);截断后 patch 不可 git apply,仅用于查看
_MAX_PATCH_CHARS = 1_000_000

# 仓库树快照条目上限(超大仓库截断,仅用于不可用时兜底展示文件清单)
_MAX_TREE_ENTRIES = 5000


def capture_workspace_diff(task_id: str) -> dict | None:
    """在任务完成的容器里捕获工作区变更,返回 patch 文本 + 元信息。

    返回 None 的情形:session 不存在、未 clone 仓库、git 命令失败、无任何修改。
    任何异常都 catch + log warning,返回 None(调用方跳过写入,不影响任务完成)。
    """
    ctx = sandbox_tools._sessions.get(task_id)
    if ctx is None:
        return None
    session = ctx["session"]
    repo_path = ctx.get("repo_path", "")
    if not repo_path:
        return None

    parts: list[str] = []
    files_changed = 0

    # 1. 已跟踪文件的修改(已暂存 + 未暂存):git diff HEAD
    try:
        diff = session.run_command(
            f"cd {repo_path} && git diff HEAD --no-color", timeout=30
        )
        if diff and diff.strip():
            parts.append(diff)
            stat = session.run_command(
                f"cd {repo_path} && git diff HEAD --stat", timeout=15
            )
            files_changed += _parse_files_changed(stat)
    except Exception as e:
        logger.warning(f"[task={task_id}] git diff HEAD 失败: {e}")

    # 2. 未跟踪文件:用 git ls-files --others --exclude-standard -z 列出
    #    - 直接列出每个文件(递归展开目录,不会出现「?? dir/」导致目录内文件整体丢失)
    #    - -z null 分隔,路径不转义不引号,正确处理含空格/特殊字符的路径
    #    - 逐文件 try/except,单文件失败(二进制解码等)不影响其余文件捕获
    try:
        raw = session.run_command(
            f"cd {repo_path} && git ls-files --others --exclude-standard -z",
            timeout=15,
        )
        # -z 用 \0 分隔,末尾会有一个空段,过滤掉
        untracked = [p for p in raw.split("\0") if p]
        for path in untracked:
            try:
                content = session.read_file(f"{repo_path}/{path}")
                parts.append(_format_new_file_patch(path, content))
                files_changed += 1
            except Exception as file_err:
                # 二进制文件解码失败、单文件读取异常:跳过该文件,继续处理其余
                logger.warning(
                    f"[task={task_id}] 跳过未跟踪文件 {path}: {file_err}"
                )
    except Exception as e:
        logger.warning(f"[task={task_id}] 列举未跟踪文件失败: {e}")

    if not parts:
        return None

    patch = "\n".join(parts)
    truncated = False
    if len(patch) > _MAX_PATCH_CHARS:
        patch = patch[:_MAX_PATCH_CHARS] + "\n[...diff 已截断...]"
        truncated = True

    return {
        "content": patch,
        "metadata": {
            "files_changed": files_changed,
            "truncated": truncated,
            "char_count": len(patch),
        },
    }


def save_workspace_diff_artifact(task, db: Session, task_id_str: str) -> None:
    """捕获工作区 diff 并写入 task_artifacts(kind="git_diff")。

    kind="git_diff" 在 task 维度唯一:重启审计再完成时先删旧记录,避免多条残留。
    失败兜底:任何异常都 catch + log,不影响任务完成。
    由 orchestrator 在任务成功完成段调用(调用方再用 try/except 包裹 inline import,
    与 summarize_and_save_memory 同范式)。
    """
    diff_result = capture_workspace_diff(task_id_str)
    if not diff_result:
        return
    # task 维度唯一:先删旧 git_diff 记录,再写新的(覆盖重启前的快照)
    db.query(TaskArtifact).filter(
        TaskArtifact.task_id == task.id,
        TaskArtifact.kind == "git_diff",
    ).delete(synchronize_session=False)
    db.add(
        TaskArtifact(
            task_id=task.id,
            kind="git_diff",
            content=diff_result["content"],
            metadata_=diff_result["metadata"],
        )
    )
    db.commit()
    logger.info(f"[task={task.id}] 已捕获工作区 diff")


def capture_repo_tree(task_id: str) -> dict | None:
    """捕获仓库文件清单(已跟踪 + 未跟踪且非忽略),返回逐行路径文本 + 元信息。

    用于工作区不可用(会话过期/任务失败/零改动)时侧栏兜底展示文件清单:
    在沙箱健康时(clone 后/任务结束时)持久化到 DB,事后不依赖沙箱。

    返回 None 的情形:session 不存在、未 clone 仓库、空仓库、git 命令失败。
    任何异常都 catch + log warning,返回 None(调用方跳过写入)。
    """
    ctx = sandbox_tools._sessions.get(task_id)
    if ctx is None:
        return None
    session = ctx["session"]
    repo_path = ctx.get("repo_path", "")
    if not repo_path:
        return None

    try:
        # -c(已跟踪) + -o(未跟踪) + --exclude-standard(排除忽略项),
        # git 自身保证去重排序,一条命令拿全清单
        raw = session.run_command(
            f"cd {repo_path} && git ls-files -co --exclude-standard",
            timeout=30,
        )
    except Exception as e:
        logger.warning(f"[task={task_id}] git ls-files 失败,跳过树快照: {e}")
        return None

    paths = [p for p in (raw or "").splitlines() if p.strip()]
    if not paths:
        return None

    truncated = len(paths) > _MAX_TREE_ENTRIES
    if truncated:
        paths = paths[:_MAX_TREE_ENTRIES]

    return {
        "content": "\n".join(paths),
        "metadata": {
            "file_count": len(paths),
            "truncated": truncated,
        },
    }


def save_repo_tree_artifact(task, db: Session, task_id_str: str) -> None:
    """捕获仓库文件清单并写入 task_artifacts(kind="repo_tree")。

    写入时机:clone 成功后(保底快照,对抗后续 git 异常/任务失败)+
    任务结束时(更新为最终态,含新建文件)。

    捕获为 None 时静默返回且不删已有记录——保住 clone 时的保底快照
    (结束段捕获失败不应让侧栏连兜底清单都丢失)。
    kind="repo_tree" 在 task 维度唯一:有新结果时先删旧再写。
    """
    tree_result = capture_repo_tree(task_id_str)
    if not tree_result:
        return
    db.query(TaskArtifact).filter(
        TaskArtifact.task_id == task.id,
        TaskArtifact.kind == "repo_tree",
    ).delete(synchronize_session=False)
    db.add(
        TaskArtifact(
            task_id=task.id,
            kind="repo_tree",
            content=tree_result["content"],
            metadata_=tree_result["metadata"],
        )
    )
    db.commit()
    logger.info(
        f"[task={task.id}] 已捕获仓库树快照 "
        f"({tree_result['metadata']['file_count']} 个文件)"
    )


# ============================================================
# 辅助函数
# ============================================================


def _format_new_file_patch(path: str, content: str) -> str:
    """把未跟踪文件内容格式化成 git apply 可还原的 new file patch。

    格式:
        diff --git a/{path} b/{path}
        new file mode 100644
        --- /dev/null
        +++ b/{path}
        @@ -0,0 +1,N @@
        +行1
        +行2
    """
    lines = content.splitlines()
    body = "\n".join(f"+{line}" for line in lines)
    # 末行无换行时补标记,保证 git apply 行为一致
    if content and not content.endswith("\n"):
        body += "\n\\ No newline at end of file"
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}"
    )


def _parse_files_changed(stat: str) -> int:
    """从 git diff --stat 末行 'N files changed' 解析文件数;失败返回 0。

    元信息只是参考,不强制准确。
    """
    if not stat:
        return 0
    for line in reversed(stat.splitlines()):
        m = re.search(r"(\d+)\s+files?\s+changed", line)
        if m:
            return int(m.group(1))
    return 0
