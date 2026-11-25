import re
import uuid
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .errors import ConflictError, ValidationError

_SEP_PADDING = re.compile(r"([-_/.])0+(\d)")


def normalize_id(raw) -> str:
    """trim, uppercase, strip zero-padding in suffixes: ' b32-01 ' -> 'B32-1'. Whitespace inside
    is removed. Used for legacy numbers, display ids, aliases and codes."""
    if raw is None or not str(raw).strip():
        raise ValidationError("ID must not be empty")
    s = re.sub(r"\s+", "", str(raw)).upper()
    return _SEP_PADDING.sub(r"\1\2", s)


def norm_opt(raw):
    return normalize_id(raw) if raw is not None and str(raw).strip() else None


def clean(v):
    if isinstance(v, str):
        return v.strip() or None
    return v


def require_text(**fields):
    missing = [k for k, v in fields.items() if not (v and str(v).strip())]
    if missing:
        raise ValidationError("Required: " + ", ".join(missing))


def require_reason(reason, what="this action"):
    if not (reason and str(reason).strip()):
        raise ValidationError(f"A reason is required for {what}")
    return str(reason).strip()


def new_event() -> str:
    """One event id per API call; every ledger row written by that call shares it."""
    return uuid.uuid4().hex


@contextmanager
def transaction(db: Session):
    """One DB transaction: commit on success, roll back everything on any error. A database
    integrity error here means a competing writer got there first (or a rule was raced)."""
    try:
        yield
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise ConflictError("The record was changed by someone else or already exists; "
                            "refresh and try again") from e
    except OperationalError as e:
        db.rollback()
        if "locked" in str(e).lower() or "busy" in str(e).lower():  # another writer got there first
            raise ConflictError("The system was busy with another change; nothing was saved, please try again") from e
        raise
    except Exception:
        db.rollback()
        raise
