"""依赖注入:get_current_user + 可选登录

从 Authorization header 解 access token,返回当前 User。
- Bearer <token> 格式
- access token 类型(不含 refresh)

SSE 端点特殊:EventSource 不能自定义 header,额外支持 query 参数 ?token=XXX
"""
import uuid

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.security import (
    TokenExpiredError,
    TokenInvalidError,
    extract_user_id_from_token,
)


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """从 Authorization: Bearer <token> 解析当前用户

    - 缺失 header / 格式错 / token 过期 / token 无效 → 401
    - 用户不存在 → 401
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 解析 Bearer token
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header 格式错误,应为 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = parts[1].strip()

    # 解码
    try:
        user_id = extract_user_id_from_token(token, expected_type="access")
    except TokenExpiredError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 已过期,请刷新或重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except TokenInvalidError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"token 无效: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 查用户
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def get_optional_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User | None:
    """可选登录:有 token 且有效 → User,无 token → None

    用于「公开但可识别用户」的端点。

    注意:token 过期/无效时(有 Authorization header 但解析失败)仍抛 401,
    而非吞成 None。这样前端 401 拦截器能触发 refresh;若吞成 None,
    权限校验会返回 403,前端不拦截 403,导致无法自动刷新 token。
    无 Authorization header(匿名访问)才返回 None。
    """
    if not authorization:
        return None
    # 有 header 但解析失败 → 抛 401(让前端拦截器 refresh),不吞成 None
    return get_current_user(authorization=authorization, db=db)


def get_optional_user_sse(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> User | None:
    """SSE 专用可选登录

    EventSource 不能自定义 header,所以额外支持 ?token=XXX 查询参数。
    优先级:Authorization header > query token > 匿名
    """
    # header 优先
    if authorization:
        try:
            return get_current_user(authorization=authorization, db=db)
        except HTTPException:
            pass
    # query token 备选
    if token:
        try:
            user_id = extract_user_id_from_token(token, expected_type="access")
            user = db.query(User).filter(User.id == user_id).first()
            return user
        except (TokenExpiredError, TokenInvalidError):
            pass
    return None


def get_current_user_sse(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> User:
    """SSE 专用必须登录(匿名 → 401)

    用于数据 per-user 隔离的 SSE 端点(如出题进度流);
    鉴权解析与 get_optional_user_sse 一致,仅匿名时行为不同。
    """
    user = get_optional_user_sse(authorization=authorization, token=token, db=db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
