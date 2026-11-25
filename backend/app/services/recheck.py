"""Flag open WOs for recheck when tooling they were cleared on is no longer certain.

Nothing is cancelled or changed automatically: the latest READY check of each open WO is marked
needs_recheck with a reason, and OMS is expected to request a fresh check (spec section 3)."""
from sqlalchemy.orm import Session

from ..models import (AssemblyLine, AssemblyLineAlternative, AssemblyRevision, ReadinessCandidate,
                      ReadinessCheck)
from . import audit


def _latest_open_ready(db: Session):
    """Latest check per WO, only if that WO is still open, READY and not already flagged."""
    latest: dict[str, ReadinessCheck] = {}
    for c in db.query(ReadinessCheck).filter(ReadinessCheck.wo_closed.is_(False)).order_by(ReadinessCheck.id):
        latest[c.wo_id] = c
    return [c for c in latest.values() if c.result == "READY" and not c.needs_recheck]


def _definitions_using_design(db: Session, design_id: int) -> set[int]:
    revs = {r.id: r.definition_id for r in db.query(AssemblyRevision)}
    used = {revs[l.revision_id] for l in db.query(AssemblyLine).filter(AssemblyLine.design_id == design_id)}
    alt_lines = [a.line_id for a in db.query(AssemblyLineAlternative).filter(
        AssemblyLineAlternative.design_id == design_id)]
    if alt_lines:
        used |= {revs[l.revision_id] for l in db.query(AssemblyLine).filter(AssemblyLine.id.in_(alt_lines))}
    return used


def flag(db: Session, actor, reason: str, *, item_ids=None, definition_id=None, mapping_id=None,
         design_id=None, operation=None) -> int:
    """Stage recheck flags in the caller's transaction. Returns how many WOs were flagged."""
    open_ready = _latest_open_ready(db)
    if not open_ready:
        return 0
    item_ids = set(item_ids or ())
    candidate_checks = set()
    if item_ids:
        candidate_checks = {c.check_id for c in db.query(ReadinessCandidate).filter(
            ReadinessCandidate.item_id.in_(item_ids))}
    design_defs = _definitions_using_design(db, design_id) if design_id else set()
    n = 0
    for c in open_ready:
        hit = (c.id in candidate_checks
               or (definition_id is not None and c.definition_id == definition_id)
               or (mapping_id is not None and c.mapping_id == mapping_id)
               or (c.definition_id in design_defs)
               or (operation is not None and (c.part_id, c.routing_revision, c.operation_id) == tuple(operation)))
        if hit:
            c.needs_recheck, c.recheck_reason = True, reason
            audit.record(db, actor, "FLAG_RECHECK", "readiness_check", c.id,
                         after={"wo_id": c.wo_id}, reason=reason)
            n += 1
    return n


def flagged(db: Session) -> list[ReadinessCheck]:
    """Open WOs whose latest check needs a rerun."""
    latest: dict[str, ReadinessCheck] = {}
    for c in db.query(ReadinessCheck).filter(ReadinessCheck.wo_closed.is_(False)).order_by(ReadinessCheck.id):
        latest[c.wo_id] = c
    return [c for c in latest.values() if c.needs_recheck]
