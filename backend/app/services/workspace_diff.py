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
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.task_artifact import TaskArtifact
from app.services.repo_cache import force_rmtree
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

    return _finalize_patch(parts, files_changed)


def _finalize_patch(parts: list[str], files_changed: int) -> dict:
    """拼装 patch 文本 + 截断 + 元信息(会话内捕获 / 宿主机孤儿恢复两路共用)"""
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


# ============================================================
# B1:TTL 清理前兜底保存(close_session 路径,无请求上下文)
# ============================================================


def save_diff_best_effort_on_close(task_id: str) -> None:
    """close_session 兜底:任务已完成但尚无 git_diff artifact 时捕获保存一次

    调用时机:cleanup_expired_sessions(_bg)销毁会话前(会话仍存活)。
    任务完成段正常路径已保存过(orchestrator 各完成/失败点);本函数只兜
    "完成时捕获曾失败"的边角。已有 artifact 则跳过(不重复覆盖正常快照)。
    close 路径无请求上下文 → 自建 SessionLocal;任何异常 catch + log。
    """
    from app.database import SessionLocal
    from app.models.task import Task

    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        return
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_uuid).first()
        if task is None:
            return
        existing = (
            db.query(TaskArtifact)
            .filter(
                TaskArtifact.task_id == task.id,
                TaskArtifact.kind == "git_diff",
            )
            .first()
        )
        if existing is not None:
            return  # 已有正常快照,不覆盖
        diff_result = capture_workspace_diff(task_id)
        if not diff_result:
            return
        db.add(
            TaskArtifact(
                task_id=task.id,
                kind="git_diff",
                content=diff_result["content"],
                metadata_=diff_result["metadata"],
            )
        )
        db.commit()
        logger.info(f"[task={task_id}] 清理前兜底保存了工作区 diff")
    finally:
        db.close()


# ============================================================
# B2:进程重启后的孤儿临时目录恢复(local 模式)
# ============================================================


def _run_host_git(argv: list[str], timeout: int = 60) -> str:
    """宿主机执行 git(argv 列表不经 shell),非零退出抛 RuntimeError"""
    proc = subprocess.run(
        ["git"] + argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-300:]
        raise RuntimeError(f"git 命令失败({argv[:3]}): {err.strip()}")
    return proc.stdout or ""


def capture_local_diff_from_dir(repo_dir: Path) -> dict | None:
    """宿主机直接从目录捕获 diff(进程重启后的孤儿目录恢复用,无会话依赖)

    与 capture_workspace_diff 同样的捕获范围(已跟踪 diff + 未跟踪文件拼
    new-file patch),但用宿主机 git + 磁盘读文件(孤儿目录没有沙箱会话)。
    复用 _format_new_file_patch/_parse_files_changed/_finalize_patch。
    """
    repo_dir = Path(repo_dir)
    if not (repo_dir / ".git").exists():
        return None
    parts: list[str] = []
    files_changed = 0

    try:
        diff = _run_host_git(["-C", str(repo_dir), "diff", "HEAD", "--no-color"])
        if diff.strip():
            parts.append(diff)
            stat = _run_host_git(["-C", str(repo_dir), "diff", "HEAD", "--stat"], timeout=30)
            files_changed += _parse_files_changed(stat)
    except Exception as e:
        logger.warning(f"[orphan] git diff HEAD 失败({repo_dir.name}): {e}")

    try:
        raw = _run_host_git(
            ["-C", str(repo_dir), "ls-files", "-o", "--exclude-standard", "-z"],
            timeout=30,
        )
        untracked = [p for p in raw.split("\0") if p]
        for path in untracked:
            try:
                content = (repo_dir / path).read_text(
                    encoding="utf-8", errors="replace"
                )
                parts.append(_format_new_file_patch(path, content))
                files_changed += 1
            except Exception as file_err:
                logger.warning(f"[orphan] 跳过未跟踪文件 {path}: {file_err}")
    except Exception as e:
        logger.warning(f"[orphan] 列举未跟踪文件失败({repo_dir.name}): {e}")

    if not parts:
        return None
    return _finalize_patch(parts, files_changed)


def _find_repo_dir(orphan_dir: Path) -> Path | None:
    """孤儿临时目录内定位仓库子目录(local 布局:{local_dir}/{repo_name}/.git)"""
    try:
        for child in orphan_dir.iterdir():
            if child.is_dir() and (child / ".git").exists():
                return child
    except OSError:
        return None
    return None


def recover_orphan_local_workspaces(max_age_no_meta: float = 3600.0) -> int:
    """进程启动时扫描 local 模式孤儿临时目录(sandbox_local_*):抢救 diff 后清理

    进程重启后 _sessions 内存丢失,mkdtemp 临时目录残留磁盘(旧版会永久泄漏):
    - 有 meta(.secondlook_meta.json,含 task_id):查任务;任务存在且目录内
      含仓库 → 宿主机捕获 diff 保存(kind="git_diff",metadata 标 recovered=true,
      先删旧再写——孤儿是重启前的最新工作区状态,覆盖旧快照符合"最新态唯一"
      语义);捕获为 None(无修改)则不动已有快照。无论保存与否 rmtree 孤儿
      (防泄漏)。
    - 无 meta:mkdtemp 与写 meta 之间的窗口残留,超过 max_age_no_meta 才删
      (防误删刚创建、meta 尚未写的活跃目录)

    幂等(首轮清完即空);单目录失败跳过。返回清理的目录数。
    sandbox 模式孤儿在 Server 侧随容器销毁,不适用本函数。
    """
    import json as _json

    from app.database import SessionLocal
    from app.models.task import Task

    tmp_root = Path(tempfile.gettempdir())
    meta_name = sandbox_tools._LOCAL_SESSION_META_FILE
    handled = 0
    try:
        orphans = list(tmp_root.glob("sandbox_local_*"))
    except OSError:
        return 0
    for orphan in orphans:
        try:
            if not orphan.is_dir():
                continue
            meta_path = orphan / meta_name
            if not meta_path.exists():
                # 无 meta:可能是刚创建、meta 尚未写的活跃目录,超龄才删
                if time.time() - orphan.stat().st_mtime <= max_age_no_meta:
                    continue
            else:
                task_id_str = ""
                try:
                    meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                    task_id_str = str(meta.get("task_id") or "")
                except Exception:
                    task_id_str = ""
                if task_id_str:
                    _salvage_orphan_diff(orphan, task_id_str)
            # 删除(git pack 只读 → force_rmtree);删不净也计入失败告警
            if not force_rmtree(orphan):
                logger.warning(f"[orphan] 孤儿目录未能删净({orphan.name})")
            handled += 1
        except Exception as e:
            logger.warning(f"[orphan] 处理孤儿目录失败({orphan.name},跳过): {e}")
    if handled:
        logger.info(f"[orphan] 孤儿临时目录恢复扫描完成,清理 {handled} 个")
    return handled


def _salvage_orphan_diff(orphan: Path, task_id_str: str) -> None:
    """把孤儿目录里的仓库修改保存为 git_diff artifact(先删旧再写,自建 db)"""
    from app.database import SessionLocal
    from app.models.task import Task

    try:
        task_uuid = uuid.UUID(task_id_str)
    except ValueError:
        return
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_uuid).first()
        if task is None:
            return
        repo_dir = _find_repo_dir(orphan)
        if repo_dir is None:
            return
        diff_result = capture_local_diff_from_dir(repo_dir)
        if not diff_result:
            return  # 无修改:不动已有快照
        diff_result["metadata"]["recovered"] = True
        # 孤儿是重启前的最新工作区状态:先删旧快照再写(与完成段"最新态唯一"一致)
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
        logger.info(f"[orphan] [task={task_id_str}] 已从孤儿目录抢救工作区 diff")
    finally:
        db.close()


# ============================================================
# B3:resume 恢复工作区后补回已保存的 diff(重启前后工作成果不丢)
# ============================================================


def reapply_workspace_diff(task, db: Session, task_id_str: str) -> None:
    """resume 重新 clone 恢复工作区后,把已保存的 git_diff artifact 补回新仓库

    重启后旧沙箱内容丢失,重新 clone 出来是干净的;本函数把重启前 agent 的
    修改(git apply)补回。截断过的 patch 不可 apply,跳过。
    任何失败仅 warning(补回是尽力而为,不影响 resume 主流程)。
    """
    artifact = (
        db.query(TaskArtifact)
        .filter(
            TaskArtifact.task_id == task.id,
            TaskArtifact.kind == "git_diff",
        )
        .first()
    )
    if not artifact or not artifact.content:
        return
    meta = artifact.metadata_ or {}
    if meta.get("truncated"):
        logger.info(f"[task={task_id_str}] 已保存 diff 曾截断,跳过恢复补回")
        return
    ctx = sandbox_tools._sessions.get(task_id_str)
    if ctx is None or not ctx.get("repo_path"):
        return
    session = ctx["session"]
    repo_path = ctx["repo_path"]
    try:
        if ctx.get("mode") == "local":
            patch_path = Path(ctx["local_dir"]) / ".restore.patch"
            patch_path.write_text(artifact.content, encoding="utf-8")
            _run_host_git(
                ["-C", repo_path, "apply", "--whitespace=nowarn", str(patch_path)],
                timeout=60,
            )
        else:
            session.write_file("/home/user/.restore.patch", artifact.content)
            out = session.run_command(
                f"cd {repo_path} && "
                f"(git apply --whitespace=nowarn /home/user/.restore.patch "
                f"|| echo SECONDLOOK_APPLY_FAILED)",
                timeout=60,
            )
            if "SECONDLOOK_APPLY_FAILED" in (out or ""):
                raise RuntimeError("容器内 git apply 失败")
        logger.info(f"[task={task_id_str}] 已把保存的 diff 补回恢复后的工作区")
    except Exception as e:
        logger.warning(f"[task={task_id_str}] 恢复补回 diff 失败(忽略): {e}")
