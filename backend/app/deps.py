from fastapi import Depends, Header, Request
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .database import SessionLocal
from .services import audit, auth
from .services.common import transaction
from .services.errors import AuthError, ConflictError


def begin_write(db: Session, request: Request | None) -> None:
    """Every changing request takes the database's write lock BEFORE it reads anything, so the checks it makes
    (is the item still in the assembly area? is the build still unissued?) cannot go stale before it writes.
    Two simultaneous requests are therefore strictly one after the other: the second sees the first's result
    and is refused by the normal rules. Reads (GET) never take the lock."""
    if request is None or request.method in ("GET", "HEAD", "OPTIONS"):
        return
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    except OperationalError as e:
        db.rollback()
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            raise ConflictError("The system was busy with another change; nothing was saved, please try again") from e
        raise


def get_db(request: Request = None):
    db = SessionLocal()
    try:
        begin_write(db, request)
        yield db
    finally:
        db.close()


def current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    """Every route except login requires a valid bearer token for an active user."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("Sign in required")
    return auth.user_from_token(db, authorization[7:].strip())


def audit_denied(request, error) -> None:
    """A refused action is itself a record: who tried what, and why it was refused (spec A15)."""
    gen = request.app.dependency_overrides.get(get_db, get_db)(None)  # a separate session; no write lock needed up front
    db = next(gen)
    try:
        header = request.headers.get("authorization", "")
        try:
            user = auth.user_from_token(db, header[7:].strip()) if header.lower().startswith("bearer ") else None
        except AuthError:
            user = None
        with transaction(db):
            audit.record(db, user, "DENIED", "request", f"{request.method} {request.url.path}", reason=error.message)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
