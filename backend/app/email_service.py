"""邮件服务:验证邮箱 + 重置密码的 token 签发/校验 + 邮件发送抽象

设计要点:
- token 用 secrets.token_urlsafe(32) 生成,库里只存 sha256 哈希
- 开发阶段 send_email 用日志打印验证链接(不接 SMTP)
- 生产环境重写 send_email 实现即可,其他代码不变
"""
import hashlib
import logging
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models.email_token import EmailToken, EmailTokenType
from app.models.user import User

# 独立 logger(不依赖 uvicorn 默认配置),确保开发期看到邮件内容
logger = logging.getLogger("app.email_service")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# token 有效期
VERIFY_EMAIL_TTL = timedelta(hours=24)
RESET_PASSWORD_TTL = timedelta(minutes=30)


# ============================================================
# token 生成与哈希
# ============================================================


def _generate_token() -> str:
    """生成 urlsafe 随机 token(48 字符)"""
    return secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    """sha256 哈希(存数据库,防泄露后被重放)"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ============================================================
# 创建 token 记录
# ============================================================


def create_email_verification_token(db: Session, user_id: uuid.UUID) -> str:
    """生成邮箱验证 token,返回原文(发给用户)"""
    return _create_token_record(db, user_id, EmailTokenType.VERIFY_EMAIL, VERIFY_EMAIL_TTL)


def create_password_reset_token(db: Session, user_id: uuid.UUID) -> str:
    """生成重置密码 token,返回原文(发给用户)"""
    return _create_token_record(db, user_id, EmailTokenType.RESET_PASSWORD, RESET_PASSWORD_TTL)


def _create_token_record(
    db: Session,
    user_id: uuid.UUID,
    token_type: EmailTokenType,
    ttl: timedelta,
) -> str:
    token_plain = _generate_token()
    token_hash = _hash_token(token_plain)
    expires_at = datetime.now(timezone.utc) + ttl

    record = EmailToken(
        user_id=user_id,
        type=token_type,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db.add(record)
    db.flush()  # 拿到 id
    return token_plain


# ============================================================
# 校验 token
# ============================================================


def verify_token(
    db: Session,
    token_plain: str,
    expected_type: EmailTokenType,
) -> EmailToken | None:
    """校验 token,返回 EmailToken 记录(已校验有效且未使用)

    返回 None 表示 token 无效/过期/已使用
    """
    token_hash = _hash_token(token_plain)
    record = (
        db.query(EmailToken)
        .filter(
            EmailToken.token_hash == token_hash,
            EmailToken.type == expected_type,
            EmailToken.used_at.is_(None),
        )
        .first()
    )
    if not record:
        return None

    # 检查过期
    if datetime.now(timezone.utc) > record.expires_at:
        return None

    return record


def mark_token_used(db: Session, token_record: EmailToken) -> None:
    """标记 token 已使用"""
    token_record.used_at = datetime.now(timezone.utc)
    db.flush()


# ============================================================
# 邮件发送抽象
# ============================================================


def send_email(to: str, subject: str, body: str) -> None:
    """发送邮件

    开发阶段:仅打印日志,不实际发邮件
    生产环境:重写此函数对接 SMTP / 阿里云邮件 / SendGrid 等
    """
    logger.info(
        "[EMAIL] to=%s subject=%s\n%s",
        to,
        subject,
        body,
    )


def send_verification_email(user: User, token_plain: str) -> None:
    """发邮箱验证邮件"""
    verify_url = f"{settings.APP_BASE_URL}/auth/verify-email?token={token_plain}"
    send_email(
        to=user.email,
        subject="[AgentPair] 验证你的邮箱",
        body=f"请点击以下链接验证邮箱(24 小时内有效):\n\n{verify_url}\n\n如果不是你本人操作,请忽略此邮件。",
    )


def send_password_reset_email(user: User, token_plain: str) -> None:
    """发重置密码邮件"""
    reset_url = f"{settings.APP_BASE_URL}/auth/password/reset?token={token_plain}"
    send_email(
        to=user.email,
        subject="[AgentPair] 重置你的密码",
        body=f"请点击以下链接重置密码(30 分钟内有效):\n\n{reset_url}\n\n如果不是你本人操作,请忽略此邮件。",
    )
