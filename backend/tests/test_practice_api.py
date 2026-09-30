"""练习模块路由集成测试(独立 PostgreSQL schema + 确定性 fake 生成器)

覆盖完整链路:generate(异步 job) → drafts → confirm → sessions → answers → stats,
外加 activate / summary / trend / 历史会话 / 错题过滤 / 越权隔离 / 鉴权。

在配置的数据库内建独立 schema pytest_practice(会话级 drop/create),
不污染开发数据;无建 schema 权限时跳过。
"""
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.deps import get_current_user

# 确保相关模型全部注册到 Base.metadata(create_all 才能建全表;
# User.git_bindings 为 selectin 关系,refresh 时会触发,必须一并导入)
import app.models.user  # noqa: F401
import app.models.user_git_binding  # noqa: F401
import app.models.user_preference  # noqa: F401
import app.models.task  # noqa: F401
import app.models.task_artifact  # noqa: F401
import app.models.practice  # noqa: F401

from app.models.practice import (
    KnowledgePoint,
    Question,
    QuestionStatus,
    QuestionType,
    UserKnowledgeState,
)
from app.models.task import Result, Task
from app.models.user import User
from app.routers import practice as practice_router

TEST_SCHEMA = "pytest_practice"
_TABLES = (
    "practice_attempts", "practice_sessions", "practice_questions",
    "user_knowledge_states", "knowledge_points",
    "results", "conversations", "task_artifacts", "tasks", "user_preferences", "user_git_bindings", "users",
)


@pytest.fixture(scope="session")
def test_engine():
    try:
        maint = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {TEST_SCHEMA}"))
        maint.dispose()
        engine = create_engine(
            settings.DATABASE_URL,
            connect_args={"options": f"-c search_path={TEST_SCHEMA}"},
        )
        Base.metadata.create_all(engine)
    except Exception as e:
        pytest.skip(f"无法创建测试 schema(数据库不可用或权限不足): {e}")
    yield engine
    engine.dispose()
    try:
        cleanup = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
        with cleanup.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
        cleanup.dispose()
    except Exception:
        pass  # 清理失败不影响测试结果


@pytest.fixture(autouse=True)
def _clean_tables(test_engine):
    # 重试几次:防上一用例的后台出题线程尚未退出时 TRUNCATE 死锁
    for i in range(5):
        try:
            with test_engine.begin() as conn:
                for t in _TABLES:
                    conn.execute(text(f"TRUNCATE TABLE {t} CASCADE"))
            break
        except Exception:
            if i == 4:
                raise
            time.sleep(0.3)
    yield


# 确定性 fake 生成器:每条 Result 出 1 题(难度 1.5 保证冷启动可选题)
def _fake_generate(db, task, user_id, max_findings=10, client=None,
                   progress_callback=None, event_callback=None):
    findings = db.query(Result).filter(Result.task_id == task.id).all()[:max_findings]
    created = []
    for i, r in enumerate(findings):
        if event_callback:
            event_callback("finding", {"index": i + 1, "total": len(findings), "title": r.title})
            event_callback("token", {"delta": f"fake-output-{i}"})
        kp = KnowledgePoint(
            user_id=user_id, key=f"CWE-{89 + i}", name=f"知识点{i}", category="cwe"
        )
        db.add(kp)
        db.flush()
        q = Question(
            user_id=user_id,
            source_task_id=task.id,
            source_result_id=r.id,
            knowledge_point_id=kp.id,
            qtype=QuestionType.SINGLE_CHOICE,
            stem=f"题干{i}",
            options=["甲", "乙", "丙", "丁"],
            answer_idx=0,
            explanation="解析",
            difficulty=1.5,
            status=QuestionStatus.DRAFT,
            dedup_hash=uuid.uuid4().hex,
        )
        db.add(q)
        created.append(q)
        if progress_callback:
            progress_callback(i + 1, len(findings))
    db.commit()
    for q in created:
        db.refresh(q)
    return created, 0


def _wait_job(client, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/practice/generate/{job_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.05)
    raise AssertionError("生成 job 超时未完成")


class Ctx:
    """一个用户的测试上下文:app/client + 库内 user/task/results"""

    def __init__(self, session_factory: sessionmaker, email: str, n_results: int):
        self.session_factory = session_factory
        self.app = FastAPI()
        self.app.include_router(practice_router.router)
        db = session_factory()
        self.user = User(email=email, password_hash="x")
        db.add(self.user)
        db.flush()
        self.task = Task(user_id=self.user.id, user_input="audit")
        db.add(self.task)
        db.flush()
        for i in range(n_results):
            db.add(Result(
                task_id=self.task.id, title=f"发现{i}", content="c",
                metadata_={"cwe": f"CWE-{89 + i}", "severity": "high"},
            ))
        db.commit()
        db.refresh(self.user)
        db.refresh(self.task)
        db.close()

        def override_get_db():
            s = session_factory()
            try:
                yield s
            finally:
                s.close()

        self.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(self.app)

    def login(self):
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        return self

    def logout(self):
        self.app.dependency_overrides.pop(get_current_user, None)
        return self


@pytest.fixture()
def ctx(test_engine, monkeypatch):
    factory = sessionmaker(bind=test_engine, autoflush=False)
    # 后台出题线程与路由层都替换为 fake 生成器 + 测试库会话
    monkeypatch.setattr(practice_router, "SessionLocal", factory)
    monkeypatch.setattr(
        practice_router, "generate_questions_for_task", _fake_generate
    )
    return Ctx(factory, "alice@test.local", n_results=3)


@pytest.fixture()
def ctx_b(test_engine):
    factory = sessionmaker(bind=test_engine, autoflush=False)
    return Ctx(factory, "bob@test.local", n_results=1).login()


def _generate_and_confirm(ctx) -> list[dict]:
    """辅助:走完 generate → confirm,返回入库题目"""
    r = ctx.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    assert r.status_code == 200
    job = _wait_job(ctx.client, r.json()["job_id"])
    assert job["status"] == "done" and len(job["questions"]) == 3
    ids = [q["id"] for q in job["questions"]]
    r = ctx.client.post("/practice/questions/confirm", json={
        "task_id": str(ctx.task.id), "question_ids": ids,
    })
    assert r.status_code == 200 and r.json()["confirmed"] == 3
    return job["questions"]


# ============================================================
# 鉴权
# ============================================================


def test_unauthenticated_returns_401(ctx):
    ctx.logout()
    assert ctx.client.get("/practice/stats").status_code == 401
    assert ctx.client.get("/practice/summary").status_code == 401
    assert ctx.client.delete("/practice/records").status_code == 401


# ============================================================
# 出题模型解析(任务详情页「本次出题将使用」展示)
# ============================================================


def test_generate_model_info_env_default(ctx):
    """任务无自带配置、无练习设置时,返回 env 默认模型(source=env)"""
    ctx.login()
    r = ctx.client.get(f"/practice/tasks/{ctx.task.id}/generate-model")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "env"
    assert body["model"] == settings.LLM_MODEL


def test_generate_model_info_requires_login_and_ownership(ctx, ctx_b):
    """未登录 401;他人任务 404(与出题接口同一归属校验)"""
    ctx.logout()
    assert (
        ctx.client.get(f"/practice/tasks/{ctx.task.id}/generate-model").status_code
        == 401
    )
    ctx.login()
    assert (
        ctx.client.get(f"/practice/tasks/{ctx_b.task.id}/generate-model").status_code
        == 404
    )


# ============================================================
# 生成 → 确认 → 组卷 → 答题 → 统计 完整链路
# ============================================================


def test_generate_requires_results(ctx_b):
    r = ctx_b.client.post("/practice/generate", json={
        "task_id": str(uuid.uuid4()),
    })
    assert r.status_code == 404  # 任务不存在


def test_full_chain(ctx):
    ctx.login()
    questions = _generate_and_confirm(ctx)

    # drafts 已清空
    assert ctx.client.get("/practice/drafts").json() == []

    # 组卷(冷启动:全为低难度新题)
    r = ctx.client.post("/practice/sessions", json={"count": 3})
    assert r.status_code == 200
    body = r.json()
    session_id = body["session_id"]
    assert len(body["questions"]) == 3
    assert all("answer_idx" not in q for q in body["questions"])

    # 逐题作答:第 1 题答对,其余答错
    for i, q in enumerate(body["questions"]):
        r = ctx.client.post(f"/practice/sessions/{session_id}/answers", json={
            "question_id": q["id"], "chosen_idx": 0 if i == 0 else 1,
        })
        assert r.status_code == 200
        assert r.json()["is_correct"] == (i == 0)
        assert r.json()["answered_count"] == i + 1

    # 重复作答 409
    r = ctx.client.post(f"/practice/sessions/{session_id}/answers", json={
        "question_id": body["questions"][0]["id"], "chosen_idx": 0,
    })
    assert r.status_code == 409

    # 统计:1/3 正确率(SM-2 最小间隔 1 天,刚答完均未到期)
    stats = ctx.client.get("/practice/stats").json()
    assert stats["total_attempts"] == 3
    assert stats["total_correct"] == 1
    assert stats["active_question_count"] == 3
    assert stats["due_count"] == 0
    assert len(stats["weak_points"]) == 3

    # 回拨两个知识点的 due_at 到过去,验证到期计数与 summary
    db = ctx.session_factory()
    states = db.query(UserKnowledgeState).filter(
        UserKnowledgeState.user_id == ctx.user.id
    ).all()
    for s in states[:2]:
        s.due_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()
    db.close()
    assert ctx.client.get("/practice/stats").json()["due_count"] == 2
    assert ctx.client.get("/practice/summary").json()["due_count"] == 2

    # 历史会话列表/明细
    sessions = ctx.client.get("/practice/sessions").json()
    assert len(sessions) == 1
    assert sessions[0]["answered_count"] == 3
    assert sessions[0]["correct_count"] == 1
    detail = ctx.client.get(f"/practice/sessions/{session_id}").json()
    assert len(detail["attempts"]) == 3
    assert sum(1 for a in detail["attempts"] if a["is_correct"]) == 1

    # 错题过滤 + 错题重练
    mistakes = ctx.client.get("/practice/questions?mistake=true").json()
    assert len(mistakes) == 2
    r = ctx.client.post("/practice/sessions", json={
        "count": 5, "question_ids": [m["id"] for m in mistakes],
    })
    assert r.status_code == 200
    assert len(r.json()["questions"]) == 2

    # 趋势:本周有 3 次作答
    trend = ctx.client.get("/practice/trend").json()
    assert len(trend["weeks"]) == 8
    assert trend["weeks"][-1]["attempts"] == 3
    assert trend["weeks"][-1]["correct"] == 1


# ============================================================
# 草稿消费 / 转正 / 汇总
# ============================================================


def test_drafts_filtered_by_task_and_activate(ctx, ctx_b):
    ctx.login()
    r = ctx.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    job = _wait_job(ctx.client, r.json()["job_id"])
    ids = [q["id"] for q in job["questions"]]

    # 按任务过滤 draft
    drafts = ctx.client.get(f"/practice/drafts?task_id={ctx.task.id}").json()
    assert len(drafts) == 3

    # 只转正前 1 题,其余 draft 保留
    r = ctx.client.post("/practice/questions/activate", json={"question_ids": ids[:1]})
    assert r.json()["activated"] == 1
    assert len(ctx.client.get("/practice/drafts").json()) == 2

    # summary:draft 2 条,无到期复习
    summary = ctx.client.get("/practice/summary").json()
    assert summary == {"due_count": 0, "draft_count": 2}

    # confirm 清场语义:确认剩余 2 条中的 1 条,另 1 条被丢弃
    r = ctx.client.post("/practice/questions/confirm", json={
        "task_id": str(ctx.task.id), "question_ids": ids[1:2],
    })
    assert r.json() == {"confirmed": 1, "discarded": 1}
    assert ctx.client.get("/practice/drafts").json() == []

    # 越权:Bob 看不到 Alice 的 draft,也不能转正 Alice 的题
    assert ctx_b.client.get("/practice/drafts").json() == []
    r = ctx_b.client.post("/practice/questions/activate", json={"question_ids": ids[:1]})
    assert r.json()["activated"] == 0
    # Bob 用 Alice 的 task_id 生成 → 404
    r = ctx_b.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    assert r.status_code == 404


def test_responses_carry_origin_and_languages(ctx):
    """job/draft/组卷/题库响应携带 origin 与 languages 字段"""
    ctx.login()
    questions = _generate_and_confirm(ctx)
    for q in questions:
        assert q["origin"] == "repo"
        assert q["languages"] == []

    # 组卷响应同样携带两字段
    r = ctx.client.post("/practice/sessions", json={"count": 3})
    assert r.status_code == 200
    for q in r.json()["questions"]:
        assert q["origin"] == "repo"
        assert q["languages"] == []

    # 知识点回写语言后,题库列表透传语言标签
    db = ctx.session_factory()
    kp = db.query(KnowledgePoint).first()
    kp.languages = ["python"]
    db.commit()
    db.close()
    items = ctx.client.get("/practice/questions").json()
    assert items and any(i["languages"] == ["python"] for i in items)
    assert all(i["origin"] in ("repo", None) for i in items)


def test_other_user_session_detail_404(ctx, ctx_b):
    ctx.login()
    _generate_and_confirm(ctx)
    session_id = ctx.client.post("/practice/sessions", json={"count": 1}).json()["session_id"]
    assert ctx_b.client.get(f"/practice/sessions/{session_id}").status_code == 404
    assert ctx_b.client.get("/practice/sessions").json() == []


def test_generate_job_of_other_user_404(ctx, ctx_b):
    ctx.login()
    r = ctx.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    job_id = r.json()["job_id"]
    assert ctx_b.client.get(f"/practice/generate/{job_id}").status_code == 404
    # 等后台线程跑完,避免与下个用例的 TRUNCATE 冲突
    _wait_job(ctx.client, job_id)


# ============================================================
# 清空练习记录
# ============================================================


def _play_one_round(ctx, n: int):
    """辅助:出题→确认→组卷并全部答错,产生流水/记忆状态/难度调整"""
    _generate_and_confirm(ctx)
    r = ctx.client.post("/practice/sessions", json={"count": n})
    assert r.status_code == 200
    body = r.json()
    for q in body["questions"]:
        resp = ctx.client.post(f"/practice/sessions/{body['session_id']}/answers", json={
            "question_id": q["id"], "chosen_idx": 1,  # 正确答案为 0,故全部答错
        })
        assert resp.status_code == 200
    return body["session_id"]


def test_clear_records_keeps_questions(ctx):
    """基础档:流水/会话/记忆状态清空,题库保留且难度重置回 3.0"""
    ctx.login()
    _play_one_round(ctx, 3)
    stats = ctx.client.get("/practice/stats").json()
    assert stats["total_attempts"] == 3
    assert len(stats["weak_points"]) == 3

    r = ctx.client.delete("/practice/records")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_sessions"] == 1
    assert body["deleted_attempts"] == 3
    assert body["deleted_questions"] == 0

    # 统计归零,题库仍在
    stats = ctx.client.get("/practice/stats").json()
    assert stats["total_attempts"] == 0
    assert stats["weak_points"] == []
    assert stats["active_question_count"] == 3
    assert stats["due_count"] == 0

    # 历史/错题/趋势均清空
    assert ctx.client.get("/practice/sessions").json() == []
    assert ctx.client.get("/practice/questions?mistake=true").json() == []
    assert all(w["attempts"] == 0 for w in ctx.client.get("/practice/trend").json()["weeks"])

    # 难度重置回初值 3.0(fake 出题为 1.5,答错后已被调高)
    questions = ctx.client.get("/practice/questions").json()
    assert len(questions) == 3
    assert all(q["difficulty"] == 3.0 for q in questions)
    assert all(q["attempts"] == 0 for q in questions)

    # SM-2 记忆状态已删
    db = ctx.session_factory()
    states = db.query(UserKnowledgeState).filter(
        UserKnowledgeState.user_id == ctx.user.id
    ).count()
    db.close()
    assert states == 0


def test_clear_records_include_questions(ctx):
    """全清档:连题目与知识点词典一并删除"""
    ctx.login()
    _play_one_round(ctx, 3)

    r = ctx.client.delete("/practice/records", params={"include_questions": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_questions"] == 3
    assert body["deleted_attempts"] == 3

    # 题库与知识点全部清空
    assert ctx.client.get("/practice/questions").json() == []
    assert ctx.client.get("/practice/stats").json()["active_question_count"] == 0
    db = ctx.session_factory()
    kps = db.query(KnowledgePoint).filter(
        KnowledgePoint.user_id == ctx.user.id
    ).count()
    qs = db.query(Question).filter(Question.user_id == ctx.user.id).count()
    db.close()
    assert kps == 0 and qs == 0


def test_clear_records_user_isolation(ctx, ctx_b):
    """多用户隔离:Alice 清空不影响 Bob 的记录"""
    ctx.login()
    _play_one_round(ctx, 3)

    # Bob 只有 1 条 finding,单走一轮出题→确认→作答
    r = ctx_b.client.post("/practice/generate", json={"task_id": str(ctx_b.task.id)})
    job = _wait_job(ctx_b.client, r.json()["job_id"])
    assert job["status"] == "done" and len(job["questions"]) == 1
    qid = job["questions"][0]["id"]
    r = ctx_b.client.post("/practice/questions/confirm", json={
        "task_id": str(ctx_b.task.id), "question_ids": [qid],
    })
    assert r.json()["confirmed"] == 1
    session_id = ctx_b.client.post(
        "/practice/sessions", json={"count": 1}
    ).json()["session_id"]
    resp = ctx_b.client.post(f"/practice/sessions/{session_id}/answers", json={
        "question_id": qid, "chosen_idx": 1,
    })
    assert resp.status_code == 200

    r = ctx.client.delete("/practice/records")
    assert r.status_code == 200

    # Bob 的会话/统计/题库均不受影响
    assert len(ctx_b.client.get("/practice/sessions").json()) == 1
    assert ctx_b.client.get("/practice/stats").json()["total_attempts"] == 1
    assert len(ctx_b.client.get("/practice/questions").json()) == 1
    # Alice 已清空
    assert ctx.client.get("/practice/sessions").json() == []
