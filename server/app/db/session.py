from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from server.app.core.config import settings
from server.app.db.models import Base

# Normalize SQLite path on Windows
db_url = settings.DATABASE_URL.replace("\\", "/")

engine = create_engine(
    db_url,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True
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
