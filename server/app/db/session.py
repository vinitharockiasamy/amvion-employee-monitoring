from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, scoped_session
from server.app.core.config import settings
from server.app.db.models import Base

db_url = settings.DATABASE_URL.replace("\\", "/") if settings.DATABASE_URL.startswith("sqlite") else settings.DATABASE_URL
IS_SQLITE = db_url.startswith("sqlite")

if IS_SQLITE:
    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 15},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA busy_timeout=5000;")
        finally:
            cursor.close()
else:
    engine = create_engine(
        db_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=300,
    )

SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
