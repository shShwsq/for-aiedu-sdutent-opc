"""综合选题策略:薄弱点 + 难度匹配 + 记忆状态加权组卷

按需即时练习:用户点「开始练习」时一次组卷。每道候选题打分:

    score = 3.0 * overdue_urgency      # 到期复习优先级最高
          + 2.0 * weakness             # 知识点错误率强化
          + 1.5 * difficulty_match     # 与用户能力匹配
          + 0.5 * novelty              # 新题引入
          + random(0, 0.3)             # 抖动防固定套路

约束:
- 同一知识点最多占 60%
- 存在到期复习题时,复习题占比不低于 50%(不够则全选)
- 冷启动(无任何记忆状态):只取 difficulty ≤ 2 的新题

实现为纯函数(输入 CandidateInfo 列表),与 ORM 解耦便于单测。
"""
import math
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# 打分权重
W_OVERDUE = 3.0
W_WEAKNESS = 2.0
W_DIFFICULTY = 1.5
W_NOVELTY = 0.5
JITTER_MAX = 0.3

# 薄弱点判定阈值:错误率 > 40% 且样本 >= 3
WEAKNESS_ERROR_RATE = 0.4
WEAKNESS_MIN_ATTEMPTS = 3
# 过期紧迫度封顶天数
OVERDUE_CAP_DAYS = 4.0
# 约束比例
MAX_SAME_KP_RATIO = 0.6
REVIEW_MIN_RATIO = 0.5
# 冷启动:无记忆状态时只出低难度新题
COLD_START_MAX_DIFFICULTY = 2.0


@dataclass
class CandidateInfo:
    """一道候选题及其知识点记忆上下文"""

    question: Any  # ORM Question(策略层不感知其字段,仅原样回传)
    question_id: Any
    kp_id: Any
    difficulty: float
    # 该题历史作答次数(0 = 新题)
    question_attempts: int
    # 知识点 SM-2 状态(从未作答过该知识点时为 None)
    kp_due_at: datetime | None
    kp_attempts: int
    kp_correct_count: int


def _error_rate(c: CandidateInfo) -> float:
    if c.kp_attempts <= 0:
        return 0.0
    return 1.0 - c.kp_correct_count / c.kp_attempts


def _overdue_urgency(c: CandidateInfo, now: datetime) -> float:
    """到期紧迫度 0-1:过期越久越急,封顶 OVERDUE_CAP_DAYS 天"""
    if c.kp_due_at is None or c.kp_due_at > now:
        return 0.0
    days = overdue_days_raw(c.kp_due_at, now)
    return min(days + 1, OVERDUE_CAP_DAYS + 1) / (OVERDUE_CAP_DAYS + 1)


def overdue_days_raw(due_at: datetime, now: datetime) -> float:
    return max(0.0, (now - due_at).total_seconds() / 86400.0)


def _weakness(c: CandidateInfo) -> float:
    if c.kp_attempts < WEAKNESS_MIN_ATTEMPTS:
        return 0.0
    rate = _error_rate(c)
    return rate if rate > WEAKNESS_ERROR_RATE else 0.0


def _difficulty_match(c: CandidateInfo, ability: float) -> float:
    return max(0.0, 1.0 - abs(c.difficulty - ability) / 2.0)


def is_review_due(c: CandidateInfo, now: datetime) -> bool:
    return c.kp_due_at is not None and c.kp_due_at <= now


def score_candidate(
    c: CandidateInfo, ability: float, now: datetime, jitter: float | None = None
) -> float:
    """单题综合得分(jitter 可注入固定值,便于测试)"""
    if jitter is None:
        jitter = random.uniform(0.0, JITTER_MAX)
    return (
        W_OVERDUE * _overdue_urgency(c, now)
        + W_WEAKNESS * _weakness(c)
        + W_DIFFICULTY * _difficulty_match(c, ability)
        + W_NOVELTY * (1.0 if c.question_attempts == 0 else 0.0)
        + jitter
    )


def select_questions(
    candidates: list[CandidateInfo],
    ability: float,
    count: int,
    now: datetime,
) -> list[CandidateInfo]:
    """组卷:打分 + 知识点多样性约束 + 复习占比约束

    返回按出题顺序排列的候选题(复习题在前)。
    """
    if not candidates or count <= 0:
        return []

    # 冷启动:完全没有记忆状态 → 只出低难度新题
    has_memory = any(c.kp_due_at is not None for c in candidates)
    if not has_memory:
        pool = [c for c in candidates if c.difficulty <= COLD_START_MAX_DIFFICULTY]
        pool.sort(key=lambda c: c.difficulty)
        return pool[:count]

    # 打分(含随机抖动,防固定套路)
    scored = [(score_candidate(c, ability, now), c) for c in candidates]
    scored.sort(key=lambda t: t[0], reverse=True)

    max_per_kp = max(1, math.floor(count * MAX_SAME_KP_RATIO))
    review_candidates = [c for c in candidates if is_review_due(c, now)]
    review_required = (
        math.ceil(count * REVIEW_MIN_RATIO) if review_candidates else 0
    )
    # 复习题不够时全选
    review_required = min(review_required, len(review_candidates))

    picked: list[CandidateInfo] = []
    picked_ids: set = set()
    kp_counter: dict = {}

    def _try_pick(c: CandidateInfo) -> bool:
        if c.question_id in picked_ids:
            return False
        if kp_counter.get(c.kp_id, 0) >= max_per_kp:
            return False
        picked.append(c)
        picked_ids.add(c.question_id)
        kp_counter[c.kp_id] = kp_counter.get(c.kp_id, 0) + 1
        return True

    # 第一轮:复习题优先,保证占比
    if review_required:
        due_scored = [
            (s, c) for s, c in scored if is_review_due(c, now)
        ]
        for _, c in due_scored:
            if len([x for x in picked if is_review_due(x, now)]) >= review_required:
                break
            _try_pick(c)

    # 第二轮:按总分补足
    for _, c in scored:
        if len(picked) >= count:
            break
        _try_pick(c)

    # 知识点多样性不足等极端情况下允许少选
    return picked[:count]
