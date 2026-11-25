"""One resolver for everything a scanner, phone camera or keyboard can type: item QR / display ID /
legacy number / approved alias, build code, or location label. Used by issue, fit and move screens."""
import re

from sqlalchemy.orm import Session

from ..models import Build, ConsumableBatch
from . import batches, builds, config, items
from .errors import ValidationError


def resolve(db: Session, code: str) -> dict:
    raw = (code or "").strip()
    if not raw:
        raise ValidationError("Nothing to look up")
    up = raw.upper()
    m = re.fullmatch(r"BLD-(\d+)", up)
    if m:
        b = db.get(Build, int(m.group(1)))
        return {"type": "BUILD", "build": {"id": b.id, "code": builds.build_code(b.id), "status": b.status}} if b else {
            "type": "NONE"}
    m = re.fullmatch(r"BATCH-(\d+)", up)
    if m:
        b = db.get(ConsumableBatch, int(m.group(1)))
        return {"type": "BATCH", "batch": batches.batch_dict(db, b)} if b else {"type": "NONE"}
    if up.startswith("LOC:"):
        loc = config.resolve_path(db, raw[4:], active_only=False)
        return {"type": "LOCATION", "location": {"id": loc.id, "path": config.location_path(db, loc.id)}}
    found = items.lookup(db, raw)
    if not found:
        return {"type": "NONE"}
    for f in found:  # a fitted item points at its build, which is what issue needs
        b = builds.active_build_for_item(db, f["id"])
        f["build"] = {"id": b.id, "code": builds.build_code(b.id), "status": b.status} if b else None
    return {"type": "ITEM", "items": found}
