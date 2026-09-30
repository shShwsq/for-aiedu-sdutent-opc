"""练习模块的 Pydantic 模型(请求与响应)"""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ============================================================
# 题目生成 / 确认
# ============================================================


class GenerateRequest(BaseModel):
    """从审计任务的真实发现生成练习题(POST /practice/generate)

    异步执行:立即返回 job_id,前端轮询 GET /practice/generate/{job_id}。
    """

    task_id: uuid.UUID
    # 参与生成的 finding 数上限(防 LLM 成本失控)
    max_findings: int = Field(default=10, ge=1, le=20)


class GenerateJobResponse(BaseModel):
    """异步生成任务句柄(POST /practice/generate 立即返回)"""

    job_id: str


class GenerateModelResponse(BaseModel):
    """任务出题模型解析结果(GET /practice/tasks/{task_id}/generate-model)

    与真实出题的 resolve_llm_client 同一优先级解析,供任务详情页在
    出题入口附近展示「本次出题将使用:xxx」,避免用户困惑为什么
    没用练习设置里的默认模型。
    source: task=任务自带配置 / default=练习默认出题模型 / env=环境默认
    """

    model: str
    source: Literal["task", "default", "env"]


class GenerateJobStatusResponse(BaseModel):
    """生成进度与结果(GET /practice/generate/{job_id})

    status: pending(排队) / running(出题中) / done(完成) / error(失败)
    done/total: 已处理/总 finding 数;done 时 questions 为新 draft 列表。
    """

    status: str
    done: int = 0
    total: int = 0
    error: str = ""
    questions: list["DraftQuestionResponse"] = []
    skipped_findings: int = 0


class GenerateJobSummary(BaseModel):
    """出题 job 摘要(GET /practice/generate/jobs)

    练习页侧栏发现运行中 job 用;含 SSE snapshot 同构字段,
    已完成 job 的 recent_text 保留最后一批输出尾部文本。
    """

    job_id: str
    status: str
    done: int = 0
    total: int = 0
    error: str = ""
    # 出题来源:manual(任务详情页手动) / auto(任务完成自动生成)
    source: str = "manual"
    task_id: str | None = None
    task_title: str = ""
    current_finding: str = ""
    recent_text: str = ""
    skipped_findings: int = 0
    created_count: int = 0
    started_at: str | None = None


class GenerateJobsResponse(BaseModel):
    """当前用户的出题 job 列表(运行中优先,限最近 10 条)"""

    jobs: list[GenerateJobSummary] = []


class DraftQuestionResponse(BaseModel):
    """生成的候选题(draft 状态,待用户预览确认)

    预览阶段即下发 answer_idx 与 explanation,供用户校对题目质量。
    """

    id: uuid.UUID
    qtype: str
    stem: str
    code_snippet: str | None = None
    options: list[str]
    answer_idx: int
    explanation: str
    difficulty: float
    knowledge_key: str | None = None
    knowledge_name: str | None = None
    # 出题形式:repo=基于真实源码,synthetic=改编题(虚构代码)
    origin: str = "repo"
    # 知识点编程语言标签(如 ["python", "sql"];来自知识点累积)
    languages: list[str] = []
    # 题目引用的源码定位(预览时校对出处用;老题为 None)
    source_file: str | None = None
    source_lines: str | None = None

    model_config = {"from_attributes": True}


class GenerateResponse(BaseModel):
    """题目生成结果"""

    questions: list[DraftQuestionResponse] = []
    # 因解析失败/重复被丢弃的 finding 数(前端提示用)
    skipped_findings: int = 0


class ConfirmQuestionsRequest(BaseModel):
    """确认 draft 题目入库(POST /practice/questions/confirm)

    只传用户勾选保留的 id;同一来源任务的其余 draft 一并删除。
    """

    task_id: uuid.UUID
    question_ids: list[uuid.UUID] = []


class ConfirmQuestionsResponse(BaseModel):
    confirmed: int
    discarded: int = 0


class ActivateQuestionsRequest(BaseModel):
    """转正指定 draft 题目(POST /practice/questions/activate)

    与 confirm 不同:只把传入 id 转 active,不影响其余 draft(题库管理页逐条操作用)。
    """

    question_ids: list[uuid.UUID] = []


class ActivateQuestionsResponse(BaseModel):
    activated: int = 0


# ============================================================
# 练习会话(按需即时组卷)
# ============================================================


class StartSessionRequest(BaseModel):
    """开始练习(POST /practice/sessions)"""

    count: int = Field(default=8, ge=1, le=30)
    # 限定知识点 key(如只看 "CWE-89"),为空表示全部
    topic_filter: str | None = None
    # 限定题目白名单(如错题重练):非空时只从这些 active 题中组卷
    question_ids: list[uuid.UUID] | None = None


class SessionQuestionResponse(BaseModel):
    """组卷下发的题面(不含 answer_idx / explanation,防作弊)

    source_task_id/source_file/source_lines 供做题页右侧代码栏
    打开题目来源工作区并自动定位(不含答案,无作弊风险)。
    """

    id: uuid.UUID
    qtype: str
    stem: str
    code_snippet: str | None = None
    options: list[str]
    difficulty: float
    knowledge_name: str | None = None
    # 知识点编程语言标签(做题页标签展示用)
    languages: list[str] = []
    # 出题形式:repo=真实代码题,synthetic=改编题
    origin: str = "repo"
    source_task_id: uuid.UUID | None = None
    source_file: str | None = None
    source_lines: str | None = None

    model_config = {"from_attributes": True}


class StartSessionResponse(BaseModel):
    session_id: uuid.UUID
    questions: list[SessionQuestionResponse] = []
    # 选题池不足时的提示(如题库为空)
    message: str = ""


class SubmitAnswerRequest(BaseModel):
    """提交单题答案(POST /practice/sessions/{id}/answers)"""

    question_id: uuid.UUID
    chosen_idx: int = Field(ge=0)


class KnowledgeStateResponse(BaseModel):
    """知识点记忆状态(答题反馈与统计展示共用)"""

    knowledge_key: str
    knowledge_name: str
    # 知识点编程语言标签
    languages: list[str] = []
    ease_factor: float
    interval_days: float
    repetitions: int
    due_at: datetime | None = None
    attempts: int
    correct_count: int
    accuracy: float | None = None

    model_config = {"from_attributes": True}


class SubmitAnswerResponse(BaseModel):
    is_correct: bool
    correct_idx: int
    explanation: str
    # 该题知识点的最新记忆状态
    state: KnowledgeStateResponse | None = None
    # 本会话进度(answered / total)
    answered_count: int
    total_count: int


# ============================================================
# 统计 / 题库管理
# ============================================================


class WeakPointItem(BaseModel):
    """薄弱点条目(按错误率排序)"""

    knowledge_key: str
    knowledge_name: str
    # 知识点编程语言标签
    languages: list[str] = []
    attempts: int
    correct_count: int
    accuracy: float
    ease_factor: float
    due_at: datetime | None = None


class StatsResponse(BaseModel):
    """练习首页统计(GET /practice/stats)"""

    # 用户能力估计值(难度匹配基准)
    ability: float
    # 到期待复习的知识点数
    due_count: int
    total_attempts: int
    total_correct: int
    accuracy: float | None = None
    weak_points: list[WeakPointItem] = []
    # 题库规模
    active_question_count: int = 0
    draft_question_count: int = 0


class QuestionListItem(BaseModel):
    """题库列表项(GET /practice/questions)"""

    id: uuid.UUID
    qtype: str
    stem: str
    difficulty: float
    status: str
    knowledge_name: str | None = None
    # 知识点编程语言标签
    languages: list[str] = []
    # 出题形式:repo=真实代码题,synthetic=改编题(老题为 None)
    origin: str | None = None
    attempts: int = 0
    accuracy: float | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ============================================================
# 导航徽章 / 历史会话 / 趋势 / 错题
# ============================================================


class PracticeSummaryResponse(BaseModel):
    """轻量汇总(GET /practice/summary,导航徽章用)"""

    due_count: int = 0
    draft_count: int = 0


class SessionListItem(BaseModel):
    """历史练习会话列表项(GET /practice/sessions)"""

    id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None = None
    question_count: int
    answered_count: int = 0
    correct_count: int = 0
    accuracy: float | None = None


class SessionAttemptItem(BaseModel):
    """会话明细中的单次作答(GET /practice/sessions/{id})"""

    question_id: uuid.UUID
    stem: str
    qtype: str
    knowledge_name: str | None = None
    chosen_idx: int
    correct_idx: int
    is_correct: bool
    answered_at: datetime


class SessionDetailResponse(BaseModel):
    """历史会话明细"""

    id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None = None
    question_count: int
    attempts: list[SessionAttemptItem] = []


class TrendPoint(BaseModel):
    """按周聚合的学习趋势点(GET /practice/trend)"""

    week_start: datetime
    attempts: int = 0
    correct: int = 0


class TrendResponse(BaseModel):
    """最近 N 周作答趋势(旧到新)"""

    weeks: list[TrendPoint] = []


class ClearRecordsResponse(BaseModel):
    """清空练习记录的删除计数(DELETE /practice/records)"""

    deleted_sessions: int = 0
    deleted_attempts: int = 0
    deleted_questions: int = 0
