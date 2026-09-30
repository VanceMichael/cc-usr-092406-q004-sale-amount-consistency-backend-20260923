"""测试支持：为每个用例装配独立的临时 SQLite 库。"""

import atexit
import os
import sys
import tempfile
from pathlib import Path

# 将 backend 目录加入导入路径，使测试可直接 import app.*
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# 必须在 import app 之前指定，避免在仓库目录生成 aquaculture.db
_TMP_DB = tempfile.mktemp(prefix="aq_test_", suffix=".db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DB}")

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import app.database as database
from app.models import Base
from app.migrations import ensure_schema


def attach_sqlite_pragmas(eng):
    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


_LAST = {"engine": None, "path": None}


def configure_temp_db():
    """新建临时库并把全局 engine/SessionLocal 指向它，返回 (engine, db路径)。

    同时释放并删除上一个用例的临时库（含 WAL/SHM），避免用例间串扰和文件堆积。
    """
    if _LAST["engine"] is not None:
        try:
            _LAST["engine"].dispose()
        except Exception:
            pass
    if _LAST["path"]:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(_LAST["path"] + suffix)
            except OSError:
                pass

    path = tempfile.mktemp(prefix="aq_case_", suffix=".db")
    eng = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    attach_sqlite_pragmas(eng)
    Base.metadata.create_all(bind=eng)
    ensure_schema(eng)

    database.engine = eng
    database.SessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=eng
    )
    _LAST["engine"] = eng
    _LAST["path"] = path
    return eng, path


@atexit.register
def _cleanup_last():
    if _LAST["engine"] is not None:
        try:
            _LAST["engine"].dispose()
        except Exception:
            pass
    if _LAST["path"]:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(_LAST["path"] + suffix)
            except OSError:
                pass


def make_client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)
