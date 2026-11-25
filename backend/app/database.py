from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .models import Base, create_views_and_triggers

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "die_mgmt.db"
STORAGE_DIR = BASE_DIR / "storage"  # uploaded documents and photos (not served statically)


def make_engine(url: str | None = None, *, fast: bool = False):
    """`fast` (tests/bulk loads only) skips fsync; never use it for the real database."""
    engine = create_engine(url or f"sqlite:///{DB_PATH}", future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        if not engine.url.database or engine.url.database == ":memory:":
            cur.close()
            return
        cur.execute("PRAGMA journal_mode=WAL")  # readers do not block the writer; far fewer lock conflicts
        cur.execute("PRAGMA synchronous=OFF" if fast else "PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


def init_db(engine) -> None:
    Base.metadata.create_all(engine)
    create_views_and_triggers(engine)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
