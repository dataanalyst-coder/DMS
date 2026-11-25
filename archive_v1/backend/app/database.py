from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .models import Base, create_views_and_triggers

DB_PATH = Path(__file__).resolve().parent.parent / "die_mgmt.db"


def make_engine(url: str | None = None):
    engine = create_engine(url or f"sqlite:///{DB_PATH}", future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def init_db(engine) -> None:
    Base.metadata.create_all(engine)
    create_views_and_triggers(engine)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
