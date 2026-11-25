import json
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import AuditLog


def _dump(v):
    return None if v is None else json.dumps(v, default=str, sort_keys=True)


def record(db: Session, actor, action: str, entity_type: str, entity_id, *, before=None,
           after=None, reason=None) -> None:
    """Stage an audit row in the caller's transaction (so it commits or rolls back with it)."""
    db.add(AuditLog(
        user_id=getattr(actor, "id", None), username=getattr(actor, "username", None),
        action=action, entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        before=_dump(before), after=_dump(after), reason=reason))


def snapshot(obj, *fields) -> dict:
    return {f: getattr(obj, f) for f in fields}


def list_audit(db: Session, *, entity_type=None, entity_id=None, user=None, action=None,
               date_from: datetime | None = None, date_to: datetime | None = None,
               limit=200, offset=0):
    q = db.query(AuditLog)
    if entity_type:
        q = q.filter(AuditLog.entity_type == entity_type)
    if entity_id:
        q = q.filter(AuditLog.entity_id == str(entity_id))
    if user:
        q = q.filter(AuditLog.username == user.strip().lower())
    if action:
        q = q.filter(AuditLog.action == action)
    if date_from:
        q = q.filter(AuditLog.at >= date_from)
    if date_to:
        q = q.filter(AuditLog.at <= date_to)
    total = q.count()
    rows = q.order_by(AuditLog.id.desc()).offset(offset).limit(min(limit, 1000)).all()
    return total, rows
