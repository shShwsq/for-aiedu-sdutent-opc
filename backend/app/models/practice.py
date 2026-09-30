"""练习模块数据模型(题库 / 知识点 / 遗忘曲线状态 / 答题流水 / 用户级设置)

设计:
- 题目来源于审计任务的真实发现(Result),由 LLM 改编为客观题(draft),
  用户预览确认后转 active 进入题库
- 知识点按 CWE 编号归类(无 CWE 时回退分类),per-user 隔离
- user_knowledge_states 承载 SM-2 遗忘曲线调度状态(ease_factor / interval / due_at)
- attempts 记录每次作答,供薄弱点统计与能力值估计
- practice_settings 存用户级练习设置(1:1,如自动生成练习题开关)

题库类表全新,随 Base.metadata.create_all 自动建表;
practice_settings 由 user_preferences.auto_generate_practice 拆出,
老数据由 migrate_practice_settings_table() 启动时迁移。
"""
import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class QuestionType(str, PyEnum):
    SINGLE_CHOICE = "single_choice"
    TRUE_FALSE = "true_false"


class QuestionStatus(str, PyEnum):
    # LLM 刚生成,待用户预览确认
    DRAFT = "draft"
    # 用户确认,进入选题池
    ACTIVE = "active"
    # 用户归档,不再参与选题
    ARCHIVED = "archived"


class KnowledgePoint(Base):
    """知识点(选题与遗忘曲线调度的最小单元)

    key 优先取 CWE 编号(如 "CWE-89"),来自 Result.metadata.cwe;
    无 CWE 时回退漏洞分类(如 "injection" / "auth" / "secrets")。
    per-user 唯一。
    """

    __tablename__ = "knowledge_points"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_knowledge_point_user_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    # 知识点唯一键:"CWE-89" / "injection" 等
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    # 展示名(如 "SQL 注入" / "CWE-89 SQL 注入")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 粗分类(前端分组展示用,如 injection / auth / crypto)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 编程语言标签(多值,如 ["python", "sql"]);出题时由 LLM 给出,
    # 同知识点多次出题做并集累积;老数据为空列表
    languages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Question(Base):
    """练习题(客观题:单选 / 判断)

    由审计任务的 Result 经 LLM 改编生成;dedup_hash = sha256(stem + code_snippet),
    用于同用户下防重复生成。
    """

    __tablename__ = "practice_questions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    # 来源追溯(可空:手工导入等未来扩展)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    source_result_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("results.id", ondelete="SET NULL"), nullable=True
    )
    knowledge_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_points.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    qtype: Mapped[QuestionType] = mapped_column(Enum(QuestionType), nullable=False)
    # 题干
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    # 相关代码片段(可空)
    code_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 选项列表(判断题固定为 ["正确", "错误"])
    options: Mapped[list] = mapped_column(JSONB, nullable=False)
    # 正确选项下标
    answer_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    # 答案解析
    explanation: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    # 难度 1-5(LLM 初评,作答后微调)
    difficulty: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)
    # 出题时使用的学习主题(security/architecture/coding;老数据为 NULL)
    learning_topic: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 题目来源形式:repo=基于真实源码出题,synthetic=改编题
    # (LLM 原创含同类问题的虚构代码,脱离原仓库);老题为 NULL 视为 repo
    origin: Mapped[str | None] = mapped_column(
        String(16), nullable=True, default="repo", server_default="repo"
    )
    # 题目引用的源码定位(仓库内相对路径 + 行区间,如 "120-150";老题为 NULL),
    # 做题页右侧代码栏据此自动打开并滚动定位
    source_file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_lines: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[QuestionStatus] = mapped_column(
        Enum(QuestionStatus), default=QuestionStatus.DRAFT, nullable=False, index=True
    )
    # sha256(stem + code_snippet),同用户去重
    dedup_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserKnowledgeState(Base):
    """用户对单个知识点的 SM-2 记忆状态(遗忘曲线核心)

    SM-2 参数:ease_factor(≥1.3) / interval_days / repetitions / due_at。
    首次作答该知识点时创建。
    """

    __tablename__ = "user_knowledge_states"
    __table_args__ = (
        UniqueConstraint("user_id", "knowledge_point_id", name="uq_knowledge_state_user_kp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    knowledge_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_points.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    ease_factor: Mapped[float] = mapped_column(Float, nullable=False, default=2.5)
    interval_days: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 下次到期复习时间(None = 尚未建立记忆轨迹)
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 作答统计(薄弱点分析用)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correct_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 最近一次作答质量(SM-2 quality:0-5)
    last_quality: Mapped[int | None] = mapped_column(Integer, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PracticeSession(Base):
    """一次练习会话(按需即时组卷)

    question_ids 保存本次组卷选中的题目 id 列表(JSON 字符串数组),
    全部作答完成后写 finished_at。
    """

    __tablename__ = "practice_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 组卷选中的题目 id(str 列表)
    question_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # 组卷时的策略快照(能力值 / 各知识点状态),供复盘
    stats: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Attempt(Base):
    """单次作答流水(薄弱点统计 / 能力值估计 / 错题回溯)"""

    __tablename__ = "practice_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("practice_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("practice_questions.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    chosen_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    is_correct: Mapped[bool] = mapped_column(nullable=False)

    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ============================================================
# 学习主题(出题提示词按主题切换;用户级默认存 practice_settings,
# 题目落库时记录出题当时的主题,便于后续按主题筛选/组卷)
# ============================================================
LEARNING_TOPIC_SECURITY = "security"          # 网络安全
LEARNING_TOPIC_ARCHITECTURE = "architecture"  # 架构设计
LEARNING_TOPIC_CODING = "coding"              # 通用代码能力
LEARNING_TOPICS = (
    LEARNING_TOPIC_SECURITY,
    LEARNING_TOPIC_ARCHITECTURE,
    LEARNING_TOPIC_CODING,
)
DEFAULT_LEARNING_TOPIC = LEARNING_TOPIC_SECURITY

# ============================================================
# 出题思考模式(用户级覆盖出题模型的思考开关)
# follow=跟随模型配置自身开关(默认);on/off=强制开/关。
# 仅支持思考模式的模型(catalog thinking=only)强制关会被忽略。
# ============================================================
THINKING_MODE_FOLLOW = "follow"
THINKING_MODE_ON = "on"
THINKING_MODE_OFF = "off"
THINKING_MODES = (THINKING_MODE_FOLLOW, THINKING_MODE_ON, THINKING_MODE_OFF)
DEFAULT_THINKING_MODE = THINKING_MODE_FOLLOW


class PracticeSettings(Base):
    """用户级练习设置 (per-user, 1:1)

    - auto_generate_practice:任务完成后是否自动生成练习题 draft
      (默认开启;产出仍需用户预览确认才转 active)
    - learning_topic:当前希望学习的主题(出题提示词按此切换)
    - restore_workspace_for_practice:出题前沙箱已清理时,
      是否重新 clone 仓库恢复工作区(供出题工具循环读源码)
    - default_llm_config_id:用户级默认出题模型(UserLLMConfig 中某条配置的 id),
      None=跟随任务级配置/env 默认
    - thinking_mode_for_practice:出题思考模式覆盖(follow/on/off),
      follow=跟随出题模型配置自身的思考开关

    迁移:老数据存于 user_preferences.auto_generate_practice 布尔列,
    migrate_practice_settings_table() 启动时把数据拷入本表后删除旧列(幂等);
    learning_topic / restore_workspace_for_practice / default_llm_config_id /
    thinking_mode_for_practice / force_default_llm 为后加列,由
    migrate_practice_learning_columns() 幂等补齐。
    """

    __tablename__ = "practice_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,  # 1:1
        nullable=False,
    )
    # 任务完成后是否自动生成练习题 draft(默认开启)
    auto_generate_practice: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    # 当前学习主题(security/architecture/coding,默认 security)
    learning_topic: Mapped[str] = mapped_column(
        String(32), nullable=False,
        server_default=DEFAULT_LEARNING_TOPIC, default=DEFAULT_LEARNING_TOPIC,
    )
    # 出题前沙箱已清理时是否重新 clone 恢复工作区(默认关,避免意外大仓库克隆)
    restore_workspace_for_practice: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # 用户级默认出题模型(UserLLMConfig.llm_configs 中某条配置的 id;None=不指定)
    # 解析优先级:task.llm_config_id > 本字段 > env 默认
    default_llm_config_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, default=None
    )
    # 始终用默认出题模型(忽略任务自带模型配置;默认关)
    force_default_llm: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # 出题思考模式覆盖:follow=跟随模型配置,on/off=强制开/关
    # (思考模式出题可能更慢但质量更高;部分模型思考模式下工具调用不稳定)
    thinking_mode_for_practice: Mapped[str] = mapped_column(
        String(16), nullable=False,
        server_default=DEFAULT_THINKING_MODE, default=DEFAULT_THINKING_MODE,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ============================================================
# 迁移:user_preferences.auto_generate_practice → 独立表
# ============================================================


def migrate_practice_settings_table() -> None:
    """幂等迁移:把 user_preferences.auto_generate_practice 拷入 practice_settings 表,
    完成后删除旧列。

    背景:项目用 Base.metadata.create_all(无 Alembic),新表随 create_all 建好,
    但老库的数据还在 user_preferences.auto_generate_practice 列里。

    - user_preferences 不存在 / 无该列(全新库或已迁过)→ 直接返回
    - 拷贝用 INSERT ... ON CONFLICT (user_id) DO UPDATE,中途失败重跑安全
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("user_preferences"):
            return  # 全新库,无老数据
        cols = {c["name"] for c in insp.get_columns("user_preferences")}
        if "auto_generate_practice" not in cols:
            return  # 全新库或已迁过
        conn.execute(
            text(
                """
                INSERT INTO practice_settings
                    (id, user_id, auto_generate_practice, created_at, updated_at)
                SELECT id, user_id, auto_generate_practice, now(), now()
                FROM user_preferences
                ON CONFLICT (user_id) DO UPDATE
                SET auto_generate_practice = EXCLUDED.auto_generate_practice,
                    updated_at = now()
                """
            )
        )
        conn.execute(
            text(
                "ALTER TABLE user_preferences "
                "DROP COLUMN auto_generate_practice"
            )
        )
        log.info(
            "practice_settings 迁移: user_preferences.auto_generate_practice "
            "→ practice_settings(旧列已删)"
        )
        conn.commit()


def migrate_practice_learning_columns() -> None:
    """幂等给 practice_settings / practice_questions 补新列

    背景:项目用 Base.metadata.create_all(无 Alembic),已存在的表不会自动加新列。
    - practice_settings 加 learning_topic / restore_workspace_for_practice /
      default_llm_config_id / thinking_mode_for_practice / force_default_llm
    - practice_questions 加 learning_topic(可空,老题不补)与
      source_file / source_lines(源码定位,可空)
    全新库(create_all 已建好新列)或已迁过 → 直接返回。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if insp.has_table("practice_settings"):
            cols = {c["name"] for c in insp.get_columns("practice_settings")}
            if "learning_topic" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN learning_topic "
                    "VARCHAR(32) NOT NULL DEFAULT 'security'"
                ))
                log.info("practice_settings.learning_topic 列迁移完成")
            if "restore_workspace_for_practice" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN "
                    "restore_workspace_for_practice BOOLEAN NOT NULL DEFAULT false"
                ))
                log.info("practice_settings.restore_workspace_for_practice 列迁移完成")
            if "default_llm_config_id" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN "
                    "default_llm_config_id VARCHAR(36)"
                ))
                log.info("practice_settings.default_llm_config_id 列迁移完成")
            if "thinking_mode_for_practice" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN "
                    "thinking_mode_for_practice VARCHAR(16) NOT NULL DEFAULT 'follow'"
                ))
                log.info("practice_settings.thinking_mode_for_practice 列迁移完成")
            if "force_default_llm" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN "
                    "force_default_llm BOOLEAN NOT NULL DEFAULT false"
                ))
                log.info("practice_settings.force_default_llm 列迁移完成")
        if insp.has_table("knowledge_points"):
            cols = {c["name"] for c in insp.get_columns("knowledge_points")}
            if "languages" not in cols:
                conn.execute(text(
                    "ALTER TABLE knowledge_points ADD COLUMN languages "
                    "JSONB NOT NULL DEFAULT '[]'"
                ))
                log.info("knowledge_points.languages 列迁移完成")
        if insp.has_table("practice_questions"):
            cols = {c["name"] for c in insp.get_columns("practice_questions")}
            if "learning_topic" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_questions ADD COLUMN "
                    "learning_topic VARCHAR(32)"
                ))
                log.info("practice_questions.learning_topic 列迁移完成")
            if "source_file" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_questions ADD COLUMN "
                    "source_file VARCHAR(512)"
                ))
                log.info("practice_questions.source_file 列迁移完成")
            if "source_lines" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_questions ADD COLUMN "
                    "source_lines VARCHAR(32)"
                ))
                log.info("practice_questions.source_lines 列迁移完成")
            if "origin" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_questions ADD COLUMN origin "
                    "VARCHAR(16) DEFAULT 'repo'"
                ))
                log.info("practice_questions.origin 列迁移完成")
        conn.commit()
