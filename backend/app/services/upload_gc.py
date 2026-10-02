"""交付物保留 / GC(Stage 1 永久层清理)

背景:交付物比 skill 更重,若无保留策略会无限增长。本模块周期清理满足
以下任一条件的 upload:
- 被任务引用:该 upload 关联的**所有**任务都已进入终态(completed/failed),
  且最近终态时间(completed_at,缺失回退 created_at)早于保留窗口;
- 孤儿(无任何任务引用):meta.created_at 早于保留窗口。

未超期 / 仍被进行中任务引用的 upload 一律保留(交付物不可再生,宁可多留)。

调度:main.py lifespan 启动 gc_loop 后台协程(仅 UPLOAD_GC_ENABLED 时),
单 worker 部署无多进程重复执行风险;与现有惰性 cleanup_expired_sessions 互不影响。
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models.task import Task, TaskStatus
from app.services.upload_storage import UploadError, get_backend

logger = logging.getLogger(__name__)

# 终态:任务不再运行,交付物可进入保留倒计时
_TERMINAL_STATUSES = (TaskStatus.COMPLETED, TaskStatus.FAILED)


def _as_utc(dt: datetime | None) -> datetime | None:
    """把 datetime 规整为 aware UTC(naive 视为 UTC),None 原样返回"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    """解析 meta.created_at 的 ISO 字符串;失败返回 None"""
    if not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        return None


def _collect_task_upload_map(db) -> dict[str, tuple[bool, datetime | None]]:
    """构建 upload_id -> (是否全部引用任务已终态, 最近终态时间) 映射

    一个 upload 可能被多个任务引用(retry/resume 复用同一任务,通常一对一;
    保守起见取"全部终态"且"最近时间",避免误删仍被引用的交付物)。
    """
    rows = db.execute(
        # 只取必要列,避免加载整个 ORM 对象
        select(
            Task.params, Task.status, Task.completed_at, Task.created_at
        ).where(Task.params.isnot(None))
    ).all()

    result: dict[str, tuple[bool, datetime | None]] = {}
    for params, status, completed_at, created_at in rows:
        p = params or {}
        # 收集该任务引用的全部 upload_id 并集:legacy 单数 upload_id +
        # 创建多文件 upload_ids + 追问累积 followup_upload_ids。
        # 追问上传若不被收集会被当孤儿误删。
        uids: list[str] = []
        if p.get("upload_id"):
            uids.append(p["upload_id"])
        for x in (p.get("upload_ids") or []):
            if x:
                uids.append(x)
        for x in (p.get("followup_upload_ids") or []):
            if x:
                uids.append(x)
        if not uids:
            continue
        terminal = status in _TERMINAL_STATUSES or status in ("completed", "failed")
        t = _as_utc(completed_at or created_at)
        for uid in uids:
            if uid in result:
                prev_terminal, prev_t = result[uid]
                terminal_uid = prev_terminal and terminal
                t_uid = max(prev_t or datetime.min.replace(tzinfo=timezone.utc),
                            t or datetime.min.replace(tzinfo=timezone.utc))
            else:
                terminal_uid = terminal
                t_uid = t
            result[uid] = (terminal_uid, t_uid)
    return result


def run_gc_once(db=None) -> dict:
    """执行一轮 GC,返回统计 {scanned, deleted, kept, errors}

    db 为 None 时自建会话并在结束时关闭(后台协程用);传入则复用(测试用)。
    """
    close_db = db is None
    if close_db:
        db = SessionLocal()
    try:
        backend = get_backend()
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=settings.UPLOAD_RETENTION_DAYS
        )
        task_map = _collect_task_upload_map(db)

        stats = {"scanned": 0, "deleted": 0, "kept": 0, "errors": 0}
        for uid in backend.list_upload_ids():
            stats["scanned"] += 1
            try:
                if uid in task_map:
                    terminal, t = task_map[uid]
                    should_delete = terminal and t is not None and t < cutoff
                else:
                    # 孤儿:无任务引用,按 meta.created_at 判超期
                    try:
                        created = _parse_iso(backend.load_meta(uid).get("created_at"))
                    except UploadError:
                        created = None
                    should_delete = created is not None and created < cutoff

                if should_delete:
                    backend.delete(uid)
                    stats["deleted"] += 1
                else:
                    stats["kept"] += 1
            except Exception as e:  # 单条兜底,不中断整轮
                stats["errors"] += 1
                logger.warning(f"[upload_gc] 处理 upload_id={uid} 失败(跳过): {e}")
        return stats
    finally:
        if close_db:
            db.close()


async def gc_loop() -> None:
    """周期 GC 协程(lifespan 启动):每 UPLOAD_GC_INTERVAL_HOURS 跑一轮

    run_gc_once 为阻塞 IO(DB + 文件/对象存储),放到线程池避免阻塞事件循环。
    """
    interval = max(1, settings.UPLOAD_GC_INTERVAL_HOURS) * 3600
    while True:
        try:
            stats = await asyncio.to_thread(run_gc_once)
            logger.info(f"[upload_gc] 清理完成: {stats}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[upload_gc] 清理轮异常(忽略,等待下轮): {e}")
        await asyncio.sleep(interval)
