"""学习主题路由:用户可管理的学习主题词表(CRUD + 启停)

主题是 finding 分类与出题视角的定义源:
- 内置 4 个(安全/架构/编码/合同,is_builtin=true,懒播种,不可删改,仅可停用)
- 用户自定义(name + 视角描述,上限 MAX_CUSTOM_TOPICS 个,可增删改)
enabled=false 的主题不再出新题(只停新增,存量题目/知识点不受影响);
启用数不可归零(最后一个启用的主题不能停/删,防止分类与出题无词表可用)。
"""
import secrets
import string
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models.practice import (
    CUSTOM_TOPIC_KEY_PREFIX,
    MAX_CUSTOM_TOPICS,
    KnowledgePoint,
    LearningTopic,
    ensure_user_topics,
)
from app.models.user import User
from app.schemas.practice import (
    TopicCreateRequest,
    TopicOut,
    TopicUpdateRequest,
)

router = APIRouter(prefix="/practice", tags=["practice"])

# 自定义主题 key 随机段字符集(小写字母数字,避免易混淆字符)
_KEY_ALPHABET = string.ascii_lowercase + string.digits


def _generate_custom_key(db: Session, user_id) -> str:
    """生成不与该用户现有主题冲突的 custom_<8位随机> key"""
    for _ in range(20):
        key = CUSTOM_TOPIC_KEY_PREFIX + "".join(
            secrets.choice(_KEY_ALPHABET) for _ in range(8)
        )
        exists = db.query(LearningTopic.id).filter(
            LearningTopic.user_id == user_id,
            LearningTopic.key == key,
        ).first()
        if not exists:
            return key
    # 理论上不可达(8 位随机 36^8 空间);防御性兜底
    raise HTTPException(status_code=500, detail="主题 key 生成失败,请重试")


def _kp_counts(db: Session, user_id) -> dict[str, int]:
    """按 learning_topic 统计该用户知识点数(分组展示与删除保护用)"""
    rows = (
        db.query(KnowledgePoint.learning_topic, sa_func.count(KnowledgePoint.id))
        .filter(KnowledgePoint.user_id == user_id)
        .group_by(KnowledgePoint.learning_topic)
        .all()
    )
    return dict(rows)


def _get_user_topic(
    db: Session, user_id, topic_id: UUID
) -> LearningTopic:
    topic = db.query(LearningTopic).filter(
        LearningTopic.id == topic_id,
        LearningTopic.user_id == user_id,
    ).first()
    if not topic:
        raise HTTPException(status_code=404, detail="学习主题不存在")
    return topic


def _assert_not_last_enabled(db: Session, user_id, topic: LearningTopic) -> None:
    """停用/删除前的保护:启用数不可归零"""
    if not topic.enabled:
        return  # 已停用的主题不影响启用计数
    others = db.query(sa_func.count(LearningTopic.id)).filter(
        LearningTopic.user_id == user_id,
        LearningTopic.id != topic.id,
        LearningTopic.enabled.is_(True),
    ).scalar()
    if not others:
        raise HTTPException(
            status_code=400,
            detail="至少需保留一个启用的学习主题,不能停用/删除最后一个",
        )


def _check_name_unique(db: Session, user_id, name: str, exclude_id: UUID | None = None) -> None:
    q = db.query(LearningTopic.id).filter(
        LearningTopic.user_id == user_id,
        LearningTopic.name == name,
    )
    if exclude_id is not None:
        q = q.filter(LearningTopic.id != exclude_id)
    if q.first():
        raise HTTPException(status_code=400, detail=f"学习主题名称「{name}」已存在")


@router.get("/topics", response_model=list[TopicOut])
def list_topics(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TopicOut]:
    """学习主题列表(懒播种内置主题),按 sort_order 排序,附每主题知识点数"""
    topics = ensure_user_topics(db, current_user.id)
    db.commit()  # 首次播种落库(幂等:已存在则无写入)
    counts = _kp_counts(db, current_user.id)
    return [
        TopicOut(
            id=t.id,
            key=t.key,
            name=t.name,
            description=t.description or "",
            is_builtin=t.is_builtin,
            enabled=t.enabled,
            sort_order=t.sort_order,
            kp_count=counts.get(t.key, 0),
        )
        for t in topics
    ]


@router.post("/topics", response_model=TopicOut, status_code=201)
def create_topic(
    req: TopicCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TopicOut:
    """新增自定义主题(key 服务端生成,用户不接触)"""
    ensure_user_topics(db, current_user.id)
    custom_count = db.query(sa_func.count(LearningTopic.id)).filter(
        LearningTopic.user_id == current_user.id,
        LearningTopic.is_builtin.is_(False),
    ).scalar()
    if custom_count >= MAX_CUSTOM_TOPICS:
        raise HTTPException(
            status_code=400,
            detail=f"自定义学习主题已达上限({MAX_CUSTOM_TOPICS} 个)",
        )
    _check_name_unique(db, current_user.id, req.name)
    # 排序:自定义接在现有最大 sort_order 之后(首个自定义 >= 50)
    max_order = db.query(sa_func.max(LearningTopic.sort_order)).filter(
        LearningTopic.user_id == current_user.id,
    ).scalar()
    next_order = max(50, (max_order or 0) + 10)
    topic = LearningTopic(
        user_id=current_user.id,
        key=_generate_custom_key(db, current_user.id),
        name=req.name,
        description=req.description or "",
        is_builtin=False,
        enabled=True,
        sort_order=next_order,
    )
    db.add(topic)
    db.commit()
    db.refresh(topic)
    return TopicOut(
        id=topic.id,
        key=topic.key,
        name=topic.name,
        description=topic.description or "",
        is_builtin=topic.is_builtin,
        enabled=topic.enabled,
        sort_order=topic.sort_order,
        kp_count=0,
    )


@router.patch("/topics/{topic_id}", response_model=TopicOut)
def update_topic(
    topic_id: UUID,
    req: TopicUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TopicOut:
    """修改主题:内置行仅接受 enabled;自定义行可改 name/description/enabled"""
    topic = _get_user_topic(db, current_user.id, topic_id)
    if topic.is_builtin:
        if req.name is not None or req.description is not None:
            raise HTTPException(
                status_code=400,
                detail="内置主题不可修改名称/描述,仅可启用/停用",
            )
    else:
        if req.name is not None:
            _check_name_unique(db, current_user.id, req.name, exclude_id=topic.id)
            topic.name = req.name
        if req.description is not None:
            topic.description = req.description
    if req.enabled is not None:
        if not req.enabled:
            _assert_not_last_enabled(db, current_user.id, topic)
        topic.enabled = req.enabled
    db.commit()
    db.refresh(topic)
    counts = _kp_counts(db, current_user.id)
    return TopicOut(
        id=topic.id,
        key=topic.key,
        name=topic.name,
        description=topic.description or "",
        is_builtin=topic.is_builtin,
        enabled=topic.enabled,
        sort_order=topic.sort_order,
        kp_count=counts.get(topic.key, 0),
    )


@router.delete("/topics/{topic_id}", status_code=204)
def delete_topic(
    topic_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """删除自定义主题(内置不可删);有关联知识点时拒绝(防悬挂分组)"""
    topic = _get_user_topic(db, current_user.id, topic_id)
    if topic.is_builtin:
        raise HTTPException(status_code=400, detail="内置主题不可删除,仅可停用")
    _assert_not_last_enabled(db, current_user.id, topic)
    kp_count = db.query(sa_func.count(KnowledgePoint.id)).filter(
        KnowledgePoint.user_id == current_user.id,
        KnowledgePoint.learning_topic == topic.key,
    ).scalar()
    if kp_count:
        raise HTTPException(
            status_code=400,
            detail=(
                f"该主题下还有 {kp_count} 个知识点,请先停用该主题"
                "(不再出新题)或清空其知识点后再删除"
            ),
        )
    db.delete(topic)
    db.commit()
