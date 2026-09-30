"""数据库连接与会话管理"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# 连接池配置:阿里云 PostgreSQL 推荐配置
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,  # 连接前检查是否有效,避免阿里云 RDS 主动断连后报错
    pool_size=5,  # 连接池大小
    max_overflow=10,  # 超出 pool_size 后还能创建的连接数
    pool_recycle=3600,  # 连接回收时间(秒),阿里云 RDS 默认 idle 超时较长,1 小时回收足够
    echo=settings.APP_DEBUG,  # 开发环境打印 SQL
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类"""

    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入:每个请求获取独立数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
