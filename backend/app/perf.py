"""性能打点日志:关键阶段耗时统一追加写入 logs/perf.log

每行一条记录(key=value 格式,便于 grep / 粘贴分析):

    [perf] ts=2026-08-13 11:22:33.456 task=<task_id> stage=<阶段名> cost=1.234s k1=v1 k2=v2

用法:
- perf_log(task_id, stage, cost_s, **fields)
    手动记录一条(带或不带耗时均可,不带 cost 时仅作时间线锚点)。
- with perf_timer(task_id, stage, **fields) as extra:
    自动计量代码块耗时;块内可 extra["k"] = v 追加动态字段。

原则:打点绝不抛异常、绝不影响主流程(所有写文件操作吞异常)。
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

# 日志文件:backend/logs/perf.log(与 logs/acp 同级)
_PERF_FILE = Path(__file__).resolve().parent.parent / "logs" / "perf.log"


def perf_log(
    task_id: Any,
    stage: str,
    cost_s: float | None = None,
    **fields: Any,
) -> None:
    """追加一条打点记录。cost_s 为 None 时只记时间戳(事件锚点)。"""
    try:
        _PERF_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        parts = [f"ts={ts}", f"task={task_id}", f"stage={stage}"]
        if cost_s is not None:
            parts.append(f"cost={cost_s:.3f}s")
        for k, v in fields.items():
            parts.append(f"{k}={v}")
        with open(_PERF_FILE, "a", encoding="utf-8") as f:
            f.write("[perf] " + " ".join(parts) + "\n")
    except Exception:
        pass  # 打点失败不影响主流程


@contextmanager
def perf_timer(task_id: Any, stage: str, **fields: Any) -> Iterator[dict]:
    """代码块耗时计时器:退出(含异常)时自动写一条 cost 记录"""
    extra: dict = dict(fields)
    start = time.perf_counter()
    try:
        yield extra
    finally:
        perf_log(task_id, stage, time.perf_counter() - start, **extra)
