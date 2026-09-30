"""题目难度评估与用户能力估计

- 题目难度 1-5:LLM 生成时初评;作答后按「能力-难度-结果」三角微调
- 用户能力值:冷启动 2.5;之后为最近 10 次答对题目难度的加权均值
  (越近的作答权重越高,反映当前水平)
"""

DIFFICULTY_MIN = 1.0
DIFFICULTY_MAX = 5.0
COLD_START_ABILITY = 2.5
# 能力估计的答对样本窗口
ABILITY_WINDOW = 10


def clamp_difficulty(value: float) -> float:
    return max(DIFFICULTY_MIN, min(DIFFICULTY_MAX, float(value)))


def adjust_question_difficulty(
    difficulty: float, ability: float, is_correct: bool
) -> float:
    """作答后微调题目难度

    - 能力不低于该题难度却答错 → 题目标高了不算,反而可能说明题目有迷惑性,
      视为题目比预期难:+0.5
    - 能力远低于难度却答对 → 题目比预期简单:-0.5
    - 其余情况(结果与预期一致)不调整
    """
    d = clamp_difficulty(difficulty)
    if not is_correct and ability >= d:
        return clamp_difficulty(d + 0.5)
    if is_correct and ability <= d - 1.5:
        return clamp_difficulty(d - 0.5)
    return d


def estimate_ability(correct_difficulties: list[float]) -> float:
    """由最近答对题目的难度序列估计能力值

    入参按作答时间升序(旧→新);只取最后 ABILITY_WINDOW 条。
    权重线性递增:第 i 条(0 起)权重 i+1,越近越重要。
    无答对记录 → 冷启动 2.5。
    """
    recent = correct_difficulties[-ABILITY_WINDOW:]
    if not recent:
        return COLD_START_ABILITY
    total_w = 0.0
    total = 0.0
    for i, d in enumerate(recent):
        w = i + 1
        total += clamp_difficulty(d) * w
        total_w += w
    return total / total_w if total_w else COLD_START_ABILITY
