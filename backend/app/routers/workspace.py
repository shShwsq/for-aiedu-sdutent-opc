"""工作区浏览路由

让前端在任务详情页浏览 react_agent clone 的工作区文件结构、查看文件内容。

端点:
- GET /tasks/{task_id}/workspace          工作区信息(是否可用、repo_path)
- GET /tasks/{task_id}/workspace/files    列出目录(懒加载树,单层)
- GET /tasks/{task_id}/workspace/tree     整树快照(首屏一次拉取,带短 TTL 缓存)
- GET /tasks/{task_id}/workspace/file     读取文件内容(原始文本 + 分页,前端自行渲染行号)
- POST /tasks/{task_id}/workspace/restore 过期工作区重新 clone(做题页代码栏一键恢复)

session 生命周期:
- 任务运行中:clone 完成后即可浏览
- 任务完成后:session 保留 1 小时(TTL),供用户回看
- 超时后惰性清理(下次访问任意 workspace 端点时触发)
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_optional_user
from app.models.task import Task
from app.models.user import User
from app.tools import sandbox_tools

logger = logging.getLogger(__name__)
router = APIRouter(tags=["workspace"])


def _check_task_access(
    task_id: uuid.UUID,
    db: Session,
    current_user: User | None,
) -> Task:
    """加载任务并校验访问权限,返回 task 对象"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 权限:任务归属用户或匿名任务可访问(与 get_task 一致)
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")
    return task


@router.get("/tasks/{task_id}/workspace")
def get_workspace(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """获取工作区信息

    返回:
    - available: 工作区是否可浏览(session 存在且已 clone)
    - repo_path: 工作区路径
    - completed: 任务是否已完成
    - mode: sandbox/local
    """
    _check_task_access(task_id, db, current_user)

    # 惰性清理过期 session
    sandbox_tools.cleanup_expired_sessions_bg()

    info = sandbox_tools.get_workspace_info(str(task_id))
    if info is None:
        return {"available": False, "reason": "工作区不可用(任务未 clone 仓库或会话已过期)"}

    repo_path = info.get("repo_path", "")
    return {
        "available": bool(repo_path),
        "reason": None if repo_path else "尚未 clone 仓库,请等待 react_agent 执行 clone_repo",
        "repo_path": repo_path,
        "completed": info.get("completed", False),
        "mode": info.get("mode", ""),
    }


@router.get("/tasks/{task_id}/workspace/files")
def list_workspace_files(
    task_id: uuid.UUID,
    subdir: str = Query(default="", description="仓库内相对路径,默认根目录"),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """列出工作区某目录下的文件和子目录(单层,懒加载树)

    返回结构与 list_files 工具一致:
    {
        "path": "src/",
        "entries": [{"name": "main.py", "type": "file", "size": 1024}, ...],
        "total": int,
        "truncated": bool,
    }
    """
    _check_task_access(task_id, db, current_user)
    sandbox_tools.cleanup_expired_sessions_bg()

    try:
        return sandbox_tools.browse_files(str(task_id), subdir)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception(f"[task={task_id}] 列出工作区文件失败: subdir={subdir}")
        raise HTTPException(status_code=500, detail=f"列出文件失败: {e}")


@router.get("/tasks/{task_id}/workspace/tree")
def get_workspace_tree(
    task_id: uuid.UUID,
    max_depth: int = Query(default=4, ge=1, le=8, description="快照最大深度"),
    max_entries: int = Query(default=3000, ge=100, le=10000, description="最大条目数"),
    refresh: bool = Query(default=False, description="绕过缓存强制重建快照"),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """一次性返回整树快照(前端文件树首屏渲染,替代逐级懒加载)

    返回结构:
    {
        "entries": [{"path": "src/main.py", "type": "file"|"dir"}, ...],
        "truncated": bool,   # 超上限截断,前端未覆盖目录退回 /workspace/files 懒加载
        "max_depth": int,    # 快照实际覆盖深度
    }
    """
    _check_task_access(task_id, db, current_user)
    sandbox_tools.cleanup_expired_sessions_bg()

    try:
        return sandbox_tools.browse_tree(str(task_id), max_depth, max_entries, refresh)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception(f"[task={task_id}] 获取工作区树快照失败")
        raise HTTPException(status_code=500, detail=f"获取文件树失败: {e}")


@router.get("/tasks/{task_id}/workspace/file")
def read_workspace_file(
    task_id: uuid.UUID,
    path: str = Query(..., description="仓库内文件相对路径"),
    offset: int = Query(default=1, ge=1, description="起始行号(1-based)"),
    max_lines: int = Query(default=500, ge=1, le=2000, description="最多返回行数"),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """读取工作区内文件内容(原始文本,支持分页)

    返回结构:
    {
        "path": str,
        "content": str,       # 原始文本(不带行号前缀,前端自行渲染行号列)
        "start_line": int,
        "end_line": int,
        "total_lines": int,
        "truncated": bool,
    }

    注意:与 LLM 工具 read_file 不同,此处 content 不带行号前缀。
    前端 WorkspaceSidebar 用 start_line + 行索引自行渲染行号,
    若后端再带行号会造成两列行号重复。
    """
    _check_task_access(task_id, db, current_user)
    sandbox_tools.cleanup_expired_sessions_bg()

    try:
        return sandbox_tools.browse_read_file(str(task_id), path, offset, max_lines)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception(f"[task={task_id}] 读取工作区文件失败: path={path}")
        raise HTTPException(status_code=500, detail=f"读取文件失败: {e}")


@router.post("/tasks/{task_id}/workspace/restore")
def restore_workspace(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """恢复已过期清理的工作区:重新 clone 任务仓库(用户显式操作)

    做题页右侧代码栏在沙箱 session 过期后展示「重新拉取代码」按钮调用。
    与出题时的自动恢复不同,此处为用户主动触发,不受
    restore_workspace_for_practice 开关限制。恢复的 session 属于已完成任务,
    标记 completed 纳入 TTL 清理序列避免常驻泄漏。

    - session 仍存活 → 直接返回当前工作区信息(幂等)
    - 任务无 repo_url → 400
    - clone 失败 → 500(前端提示并保持不可用态)
    """
    task = _check_task_access(task_id, db, current_user)

    info = sandbox_tools.get_workspace_info(str(task_id))
    if info and info.get("repo_path"):
        return {
            "available": True,
            "repo_path": info["repo_path"],
            "mode": info.get("mode", ""),
        }

    params = task.params or {}
    repo_url = params.get("repo_url")
    if not repo_url:
        raise HTTPException(status_code=400, detail="任务无仓库地址,无法恢复工作区")

    # 复用出题模块的 git token 解密逻辑(懒加载避免模块级循环引用)
    from app.services.practice.generator import _load_git_tokens

    logger.info("[task=%s] 用户请求恢复工作区,重新 clone", task_id)
    try:
        sandbox_tools.clone_repo_with_fallback(
            repo_url,
            branch=params.get("branch"),
            task_id=str(task_id),
            git_tokens=_load_git_tokens(db, task.user_id),
        )
    except Exception as e:
        logger.warning("[task=%s] 恢复工作区失败: %s", task_id, e)
        raise HTTPException(status_code=500, detail=f"恢复工作区失败: {e}")

    sandbox_tools.mark_task_completed(str(task_id))
    info = sandbox_tools.get_workspace_info(str(task_id)) or {}
    return {
        "available": bool(info.get("repo_path")),
        "repo_path": info.get("repo_path", ""),
        "mode": info.get("mode", ""),
    }
