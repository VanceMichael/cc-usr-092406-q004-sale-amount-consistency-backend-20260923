from contextlib import contextmanager
from pathlib import Path
import os

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker


class WriteConflictError(Exception):
    """写事务在获取锁/提交时与并发事务冲突（如 SQLite database is locked）。

    语义为可重试：调用方凭同一幂等键重试即可，由唯一约束保证不重复生效。
    """

SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./aquaculture.db"
)

IS_SQLITE = SQLALCHEMY_DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    _raw_path = SQLALCHEMY_DATABASE_URL.replace("sqlite:///", "", 1)
    if _raw_path:
        db_dir = Path(_raw_path).parent
        if str(db_dir) not in ("", "."):
            db_dir.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        """WAL + 忙等 + 外键，保证并发写入时锁等待可预期、事务边界明确。"""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def transactional_session(db: Session = None):
    """提供显式事务边界的会话上下文。

    - 传入 FastAPI 请求会话时，开启 SAVEPOINT 嵌套事务，业务代码可以在
      子事务内提前失败回滚而不污染外层会话；异常向上抛出由端点决定响应码。
    - 未传入会话（批处理/脚本场景）时，新建连接并在 SQLite 上以
      ``BEGIN IMMEDIATE`` 立即取写锁，使"读-判重-写"整段串行化，
      避免并发提交产生重复更正或重复结算。
    """
    if db is not None:
        nested = db.begin_nested()
        try:
            yield db
            nested.commit()
        except BaseException:
            nested.rollback()
            raise
        return

    connection = engine.connect()
    try:
        if IS_SQLITE:
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            except OperationalError as exc:
                raise WriteConflictError("数据库写锁繁忙，请稍后重试") from exc
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
            session.commit()
            connection.commit()
        except OperationalError as exc:
            session.rollback()
            connection.rollback()
            if _is_lock_error(exc):
                raise WriteConflictError("数据库写锁繁忙，请稍后重试") from exc
            raise
        except BaseException:
            session.rollback()
            connection.rollback()
            raise
        finally:
            session.close()
    finally:
        connection.close()


def _is_lock_error(exc: OperationalError) -> bool:
    orig = getattr(exc, "orig", None)
    text_ = str(orig).lower() if orig is not None else str(exc).lower()
    return "locked" in text_ or "database is locked" in text_
