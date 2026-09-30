"""SM-2 遗忘曲线调度(知识点记忆状态更新)

标准 SM-2 算法的简化落地:
- 作答质量映射:答对 quality=4,答错 quality=1(二值化,客观题无半对)
- quality<3 → 记忆重置:repetitions=0, interval=1 天(次日必须重学)
- quality>=3 → interval 序列 1 → 6 → 前值×EF
- EF' = EF + (0.1 - (5-q) * (0.08 + (5-q) * 0.02)),下限 1.3

状态直接体现在 UserKnowledgeState ORM 行上;纯函数 apply_sm2 接收
字段值返回新值,便于单测不依赖数据库。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

# SM-2 常量
DEFAULT_EASE_FACTOR = 2.5
MIN_EASE_FACTOR = 1.3
FIRST_INTERVAL_DAYS = 1.0
SECOND_INTERVAL_DAYS = 6.0

# 作答质量:答对 / 答错
QUALITY_CORRECT = 4
QUALITY_WRONG = 1


@dataclass
class SM2State:
    """SM-2 状态快照(与 UserKnowledgeState 字段对应,解耦 ORM)"""

    ease_factor: float = DEFAULT_EASE_FACTOR
    interval_days: float = 0.0
    repetitions: int = 0
    due_at: datetime | None = None
    attempts: int = 0
    correct_count: int = 0
    last_quality: int | None = None


def quality_from_correct(is_correct: bool) -> int:
    """客观题二值化质量映射"""
    return QUALITY_CORRECT if is_correct else QUALITY_WRONG


def apply_sm2(state: SM2State, quality: int, now: datetime) -> SM2State:
    """应用一次作答,返回更新后的状态(不修改入参)

    quality 取值 0-5;<3 视为失败,重置记忆轨迹。
    """
    quality = max(0, min(5, int(quality)))

    # EF 更新(无论成败都更新,连续失败会把 EF 压到下限)
    ef = state.ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    ef = max(MIN_EASE_FACTOR, ef)

    if quality < 3:
        repetitions = 0
        interval = FIRST_INTERVAL_DAYS
    else:
        repetitions = state.repetitions + 1
        if repetitions == 1:
            interval = FIRST_INTERVAL_DAYS
        elif repetitions == 2:
            interval = SECOND_INTERVAL_DAYS
        else:
            interval = max(1.0, round(state.interval_days * ef))

    return SM2State(
        ease_factor=ef,
        interval_days=interval,
        repetitions=repetitions,
        due_at=now + timedelta(days=interval),
        attempts=state.attempts + 1,
        correct_count=state.correct_count + (1 if quality >= 3 else 0),
        last_quality=quality,
    )


def is_overdue(state: SM2State, now: datetime) -> bool:
    """是否到期待复习(从未作答不算到期)"""
    return state.due_at is not None and state.due_at <= now


def overdue_days(state: SM2State, now: datetime) -> float:
    """过期天数(未到期/未作答返回 0)"""
    if state.due_at is None or state.due_at > now:
        return 0.0
    return (now - state.due_at).total_seconds() / 86400.0
