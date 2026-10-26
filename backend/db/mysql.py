"""MySQL 访问层：引擎/会话单例 + ORM 建表。

【零基础说明：三层概念】
1. Engine（引擎）：一个"数据库连接池"对象。创建成本高，所以全局只建一次
   （get_engine 里的单例模式），所有线程共用。pool_pre_ping=True 表示每次
   借连接前先 ping 一下，连接断了自动重建（防止 MySQL 8 小时超时断连）。
2. ORM Models（db/models.py）：用 Python 类来描述表结构。一个类=一张表，
   一个对象=一行数据。好处：IDE 有提示、类型安全、换数据库不用重写 SQL。
3. Session（会话）：一次数据库"工作单元"。add/修改的对象攒在 Session 里，
   commit() 时一次性写入。用完必须 close()，所以统一用 with 语法。

谁调用它：采集入库、API 查询、模型训练，全都从这里拿引擎/会话。
"""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from config import settings
from db.models import Base

_engine = None
_session_factory: sessionmaker[Session] | None = None


def get_engine():
    """返回全局唯一的 Engine（第一次调用时创建，之后直接复用）。"""
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.mysql_url,
            pool_pre_ping=True,   # 借用连接前先测活，坏连接自动换
            pool_recycle=3600,    # 连接用满 1 小时强制回收，避免被 MySQL 踢掉
            future=True,          # SQLAlchemy 2.0 风格 API
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """返回会话工厂。用法：with get_session_factory()() as session: ..."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
        # expire_on_commit=False：commit 之后对象属性仍可直接读，不用再查一次库
    return _session_factory


def init_schema() -> None:
    """按 ORM models 建全部表（幂等：已存在的表会跳过，不会删数据）。

    这就是"用 models 建表"：不需要手写 CREATE TABLE，
    SQLAlchemy 会把每个类的 __tablename__ + Column 翻译成建表语句。
    """
    Base.metadata.create_all(get_engine())


def ping() -> bool:
    """健康检查：能执行 SELECT 1 就说明数据库活着。"""
    with get_engine().connect() as conn:
        return conn.execute(text("SELECT 1")).scalar() == 1
