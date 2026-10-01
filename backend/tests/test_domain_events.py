"""domain_events 单元测试:订阅/分发/退订/异常隔离(不连真实 DB)。

覆盖:
- emit:分发给指定类型订阅者,payload/task_id/occurred_at 正确
- subscribe_all:通配订阅者收到所有类型事件
- 退订:subscribe 返回的函数回滚本次订阅,不影响其他订阅者(可逆注册)
- 异常隔离:单个 handler 抛异常不影响其他 handler,emit 本身不抛
- 快照分发:handler 内退订不影响本轮已快照的分发列表
- 参数校验:未知事件类型拒绝订阅
- 审计订阅者:落库成功路径 + DB 异常时 rollback 不抛(mock SessionLocal)
- 埋点连通:orchestrator / agent2 / agent_checkpoint 模块可导入(埋点无语法错误)
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest

import app.domain_events as de
from app.domain_events import (
    ALL_EVENT_TYPES,
    CHECKPOINT_EVALUATED,
    TASK_COMPLETED,
    TASK_STARTED,
    emit,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """每个测试前后清空订阅,避免用例间串扰"""
    de.clear_all_subscribers()
    yield
    de.clear_all_subscribers()


# ============================================================
# 订阅与分发
# ============================================================

def test_emit_dispatches_to_typed_subscriber():
    received = []
    de.subscribe(TASK_STARTED, received.append)

    event = emit(TASK_STARTED, uuid.uuid4(), scenario="general")

    assert len(received) == 1
    got = received[0]
    assert got.type == TASK_STARTED
    assert got.payload == {"scenario": "general"}
    assert got.occurred_at  # 自动生成 ISO 时间
    assert event is got  # emit 返回构造的事件本身


def test_typed_subscriber_ignores_other_types():
    received = []
    de.subscribe(TASK_STARTED, received.append)

    emit(TASK_COMPLETED, uuid.uuid4(), mode="dual_agent")

    assert received == []


def test_wildcard_subscriber_receives_all_events():
    received = []
    de.subscribe_all(received.append)

    emit(TASK_STARTED, uuid.uuid4())
    emit(TASK_COMPLETED, uuid.uuid4())
    emit(CHECKPOINT_EVALUATED, uuid.uuid4())

    assert [e.type for e in received] == [
        TASK_STARTED, TASK_COMPLETED, CHECKPOINT_EVALUATED,
    ]


def test_task_id_normalized_to_str():
    received = []
    de.subscribe(TASK_STARTED, received.append)
    task_uuid = uuid.uuid4()

    emit(TASK_STARTED, task_uuid)

    assert received[0].task_id == str(task_uuid)


# ============================================================
# 可逆注册(退订)
# ============================================================

def test_unsubscribe_reverses_registration():
    received = []
    unsub = de.subscribe(TASK_STARTED, received.append)

    emit(TASK_STARTED, uuid.uuid4())
    assert len(received) == 1

    unsub()
    emit(TASK_STARTED, uuid.uuid4())
    assert len(received) == 1  # 退订后不再收到

    unsub()  # 幂等:重复退订不抛错


def test_unsubscribe_does_not_affect_other_subscribers():
    a, b = [], []
    de.subscribe(TASK_STARTED, a.append)
    unsub_b = de.subscribe(TASK_STARTED, b.append)

    unsub_b()
    emit(TASK_STARTED, uuid.uuid4())

    assert len(a) == 1  # b 的退订不影响 a


def test_wildcard_unsubscribe():
    received = []
    unsub = de.subscribe_all(received.append)

    unsub()
    emit(TASK_STARTED, uuid.uuid4())

    assert received == []


# ============================================================
# 异常隔离
# ============================================================

def test_handler_exception_isolated_from_others():
    good = []

    def bad_handler(event):
        raise RuntimeError("扩展挂了")

    de.subscribe(TASK_STARTED, bad_handler)
    de.subscribe(TASK_STARTED, good.append)

    # bad_handler 抛异常被隔离:emit 不抛,good 照常收到
    emit(TASK_STARTED, uuid.uuid4())

    assert len(good) == 1


def test_wildcard_exception_isolated():
    good = []

    def bad_handler(event):
        raise ValueError("通配订阅者挂了")

    de.subscribe_all(bad_handler)
    de.subscribe(TASK_COMPLETED, good.append)

    emit(TASK_COMPLETED, uuid.uuid4())

    assert len(good) == 1


# ============================================================
# 快照分发语义
# ============================================================

def test_unsubscribe_during_dispatch_does_not_break_current_round():
    """handler 内退订自己:本轮已快照,仍会完整分发(与 Cordis 语义对齐)"""
    received = []

    def self_removing(event):
        received.append(event)
        unsub()  # 在分发过程中退订自己

    unsub = de.subscribe(TASK_STARTED, self_removing)

    emit(TASK_STARTED, uuid.uuid4())

    assert len(received) == 1  # 本轮仍收到
    emit(TASK_STARTED, uuid.uuid4())
    assert len(received) == 1  # 下一轮不再收到


# ============================================================
# 参数校验
# ============================================================

def test_subscribe_rejects_unknown_event_type():
    with pytest.raises(ValueError, match="未知事件类型"):
        de.subscribe("not.a.real.event", lambda e: None)


def test_all_event_types_covers_documented_types():
    for t in [
        "task.started", "task.completed", "task.failed",
        "checklist.confirmed", "question.raised",
        "agent1.round_completed", "checkpoint.evaluated",
        "verifier.completed",
    ]:
        assert t in ALL_EVENT_TYPES


# ============================================================
# 审计订阅者(mock DB)
# ============================================================

class TestAuditSubscriber:
    def _fake_db(self):
        db = MagicMock()
        db.add = MagicMock()
        db.commit = MagicMock()
        db.rollback = MagicMock()
        db.close = MagicMock()
        return db

    def test_audit_writes_event_to_db(self):
        from app.services.domain_event_audit import _audit_handler

        db = self._fake_db()
        with patch("app.services.domain_event_audit.SessionLocal", return_value=db):
            task_uuid = uuid.uuid4()
            event = de.DomainEvent(
                type=TASK_COMPLETED, task_id=str(task_uuid),
                payload={"mode": "dual_agent", "rounds": 3},
            )
            _audit_handler(event)

        db.add.assert_called_once()
        log = db.add.call_args[0][0]
        assert log.event_type == TASK_COMPLETED
        assert log.task_id == task_uuid
        assert log.payload == {"mode": "dual_agent", "rounds": 3}
        db.commit.assert_called_once()
        db.rollback.assert_not_called()
        db.close.assert_called_once()

    def test_audit_rollback_on_db_error_without_raising(self):
        from app.services.domain_event_audit import _audit_handler

        db = self._fake_db()
        db.commit.side_effect = RuntimeError("db down")
        with patch("app.services.domain_event_audit.SessionLocal", return_value=db):
            # 落库失败不抛(emit 侧已隔离,handler 自身也要稳)
            _audit_handler(de.DomainEvent(
                type=TASK_STARTED, task_id=str(uuid.uuid4()), payload={},
            ))

        db.rollback.assert_called_once()
        db.close.assert_called_once()

    def test_audit_drops_invalid_task_id(self):
        from app.services.domain_event_audit import _audit_handler

        db = self._fake_db()
        with patch("app.services.domain_event_audit.SessionLocal", return_value=db):
            _audit_handler(de.DomainEvent(
                type=TASK_STARTED, task_id="not-a-uuid", payload={},
            ))

        db.add.assert_not_called()  # 非法 task_id 直接丢弃,不落库


# ============================================================
# 埋点连通性(import 即验证埋点语法正确)
# ============================================================

def test_instrumented_modules_importable():
    import app.agent_checkpoint  # noqa: F401
    import app.agents.agent2  # noqa: F401
    import app.agents.orchestrator  # noqa: F401

    # 埋点使用的常量在模块内可用(粗粒度:导入成功 = 常量存在)
    assert callable(emit)
