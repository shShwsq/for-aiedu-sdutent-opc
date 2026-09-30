"""健康检查路由"""
from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict:
    # features 供前端按需隐藏入口(如 PRACTICE_ENABLED=false 时隐藏练习导航/按钮)
    return {
        "status": "ok",
        "features": {"practice_enabled": settings.PRACTICE_ENABLED},
    }
