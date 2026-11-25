"""Physical builds: the exact item IDs fitted against one approved assembly definition revision.

Rules (spec sections 2 and 4):
  * an item belongs to at most one ACTIVE build (unique index + checks); history is retained
  * only serviceable items in stores custody can be fitted; wrong/held/issued/already-fitted are rejected
  * to use an item from another stored build you must explicitly remove it from that build first
  * a fitted item takes the build's location and ASSEMBLY_AREA custody, so it never shows as available in
    its home bin; moving an intact build moves all its components together
  * any component change sends a verified build back to ASSEMBLING: it must be verified again
  * dismantling closes the build and each component is scanned to a confirmed destination
"""
from sqlalchemy.orm import Session

from ..models import (Build, BuildComponent, DesignRevision, ElementDesign, Item, Ledger, User, _now)
from . import assemblies, audit, config, ledger, permissions
from .common import clean, new_event, require_reason, transaction
from .errors import NotFoundError, ValidationError
from .matching import Candidate, accepts, match


def build_code(build_id: int) -> str:
    return f"BLD-{build_id:06d}"


def _get(db: Session, build_id: int) -> Build:
    b = db.get(Build, build_id)
    if b is None:
        raise NotFoundError(f"Build {build_id} not found")
    return b


def members(db: Session, build_id: int) -> list[BuildComponent]:
    return db.query(BuildComponent).filter(BuildComponent.build_id == build_id,
                                           BuildComponent.removed_at.is_(None)).order_by(BuildComponent.id).all()


def active_build_for_item(db: Session, item_id: int) -> Build | None:
    bid = ledger.active_build_id(db, item_id)
    return db.get(Build, bid) if bid else None


def build_location_id(db: Session, b: Build) -> int:
    """Where the build physically is: where its fitted components are (they always move together),
    or the place it was started in while it is still empty."""
    for c in members(db, b.id):
        s = ledger.get_state(db, c.item_id)
        if s and s["location_id"]:
            return s["location_id"]
    return b.created_location_id


def _item(db: Session, item_id: int) -> Item:
    it = db.get(Item, item_id)
    if it is None:
        raise NotFoundError(f"Item {item_id} not found")
    return it


def _cand(db: Session, item_id: int) -> Candidate:
    it = _item(db, item_id)
    return Candidate(item_id=it.id, design_id=it.design_id, revision_id=it.design_revision_id)


def _open(db: Session, build_id: int) -> Build:
    b = _get(db, build_id)
    if b.status == "DISMANTLED":
        raise ValidationError(f"Build {build_code(b.id)} is dismantled and closed")
    return b


def _touch(b: Build):
    """A change to the component set invalidates verification."""
    if b.status == "VERIFIED":
        b.status, b.verified_by, b.verified_at = "ASSEMBLING", None, None


# ---------------------------------------------------------------- create
def create_build(db: Session, actor: User, *, definition_id: int, location_id: int, revision_id=None,
                 remarks=None) -> Build:
    permissions.require(actor, "build.assemble")
    d = assemblies.get_definition(db, definition_id)
    if not d.active:
        raise ValidationError(f"Assembly {d.code} is inactive")
    rev = assemblies.get_revision(db, revision_id) if revision_id else assemblies.current_approved(db, d.id)
    if rev is None or rev.definition_id != d.id:
        raise ValidationError(f"Assembly {d.code} has no approved revision to build against")
    if rev.status != "APPROVED":
        raise ValidationError(f"Revision {rev.revision} is {rev.status}; only an approved revision can be built")
    loc = config.get_location(db, location_id)
    if not loc.active:
        raise ValidationError("Build location is inactive")
    with transaction(db):
        b = Build(definition_id=d.id, revision_id=rev.id, created_location_id=loc.id, assembler_id=actor.id,
                  remarks=clean(remarks))
        db.add(b)
        db.flush()
        audit.record(db, actor, "CREATE", "build", b.id,
                     after={"code": build_code(b.id), "assembly": d.code, "revision": rev.revision})
    return b


# ---------------------------------------------------------------- fit / remove
def _why_not_fittable(db: Session, b: Build, item: Item, lines, current: list[Candidate]) -> str:
    cand = Candidate(item.id, item.design_id, item.design_revision_id)
    design = db.get(ElementDesign, item.design_id)
    rev = db.get(DesignRevision, item.design_revision_id)
    accepting_design = [ln for ln in lines if item.design_id in ln.accepts]
    if not accepting_design:
        return (f"{item.display_id} is design {design.code}, which is not part of this assembly "
                f"(wrong item)")
    if not any(accepts(ln, cand) for ln in accepting_design):
        return (f"{item.display_id} is {design.code} revision {rev.revision}, which this assembly does not "
                f"accept (wrong revision)")
    return (f"no free place for {item.display_id}: every {design.code} position (including substitutes) "
            f"is already filled")


def _fit(db: Session, actor: User, b: Build, item_id: int, event_id: str) -> BuildComponent:
    item = _item(db, item_id)
    label = item.display_id
    if not item.active:
        raise ValidationError(f"{label} is retired and cannot be fitted")
    other = ledger.active_build_id(db, item.id)
    if other:
        where = "this build" if other == b.id else f"build {build_code(other)}"
        raise ValidationError(f"{label} is already fitted in {where}. Remove it from there first")
    s = ledger.get_state(db, item.id)
    if s["custody"] != "STORES":
        raise ValidationError(f"{label} is in {s['custody']} custody (not in stores) and cannot be fitted")
    if s["item_condition"] != "SERVICEABLE":
        raise ValidationError(f"{label} is {s['item_condition']}; only serviceable items can be fitted")
    lines = assemblies.matching_lines(db, b.revision_id)
    current = [_cand(db, c.item_id) for c in members(db, b.id)]
    new = Candidate(item.id, item.design_id, item.design_revision_id)
    _, unmatched, _ = match(lines, current + [new])
    if unmatched:
        raise ValidationError(_why_not_fittable(db, b, item, lines, current))
    loc = build_location_id(db, b)
    comp = BuildComponent(build_id=b.id, item_id=item.id, fitted_by=actor.id, fit_event_id=event_id)
    db.add(comp)
    ledger._append(db, item.id, "FIT", event_id=event_id, prev=s, user=actor, build_id=b.id,
                   to_condition=s["item_condition"], to_custody="ASSEMBLY_AREA", to_location_id=loc,
                   remarks=f"Fitted to {build_code(b.id)}")
    db.flush()
    _touch(b)
    audit.record(db, actor, "FIT", "build", b.id, after={"item": label})
    return comp


def fit_item(db: Session, actor: User, build_id: int, item_id: int) -> dict:
    permissions.require(actor, "build.assemble")
    b = _open(db, build_id)
    event_id = new_event()
    with transaction(db):
        _fit(db, actor, b, item_id, event_id)
    return {"event_id": event_id, "build_id": b.id, "status": b.status}


def _remove(db: Session, actor: User, b: Build, item_id: int, to_location_id: int, reason: str,
            event_id: str):
    comp = db.query(BuildComponent).filter(BuildComponent.build_id == b.id, BuildComponent.item_id == item_id,
                                           BuildComponent.removed_at.is_(None)).first()
    if comp is None:
        raise ValidationError(f"Item {item_id} is not fitted in {build_code(b.id)}")
    item = _item(db, item_id)
    s = ledger.get_state(db, item.id)
    if s["custody"] != "ASSEMBLY_AREA":
        raise ValidationError(f"{item.display_id} is in {s['custody']} custody (the build is out of the "
                              f"assembly area); return it before changing the build")
    dest = config.get_location(db, to_location_id)
    if not dest.active:
        raise ValidationError("Destination location is inactive")
    ledger._append(db, item.id, "REMOVE", event_id=event_id, prev=s, user=actor, build_id=b.id,
                   to_condition=s["item_condition"], to_custody="STORES", to_location_id=dest.id,
                   remarks=reason)
    comp.removed_at, comp.removed_by, comp.removal_reason, comp.remove_event_id = _now(), actor.id, reason, event_id
    db.flush()
    _touch(b)
    audit.record(db, actor, "REMOVE", "build", b.id, before={"item": item.display_id}, reason=reason)


def remove_item(db: Session, actor: User, build_id: int, item_id: int, to_location_id: int, *, reason) -> dict:
    """Take one component out; the build is then incomplete and cannot be issued until fixed."""
    permissions.require(actor, "build.assemble")
    reason = require_reason(reason, "removing a component")
    b = _open(db, build_id)
    event_id = new_event()
    with transaction(db):
        _remove(db, actor, b, item_id, to_location_id, reason, event_id)
    return {"event_id": event_id, "build_id": b.id, "status": b.status}


def replace_component(db: Session, actor: User, build_id: int, remove_item_id: int, new_item_id: int,
                      to_location_id: int, *, reason) -> dict:
    """Explicit remove + add as one event; the build must be verified again afterwards."""
    permissions.require(actor, "build.assemble")
    reason = require_reason(reason, "replacing a component")
    b = _open(db, build_id)
    event_id = new_event()
    with transaction(db):
        _remove(db, actor, b, remove_item_id, to_location_id, reason, event_id)
        _fit(db, actor, b, new_item_id, event_id)
        audit.record(db, actor, "REPLACE", "build", b.id,
                     before={"item": remove_item_id}, after={"item": new_item_id}, reason=reason)
    return {"event_id": event_id, "build_id": b.id, "status": b.status}


# ---------------------------------------------------------------- verify
def _problems(db: Session, b: Build) -> list[str]:
    """Why this build cannot be relied on right now (empty list = fine)."""
    out = []
    rev = assemblies.get_revision(db, b.revision_id)
    if rev.status != "APPROVED":
        out.append(f"definition revision {rev.revision} is {rev.status}; build against the current approved revision")
    lines = assemblies.matching_lines(db, b.revision_id)
    comps = members(db, b.id)
    for c in comps:
        it = _item(db, c.item_id)
        s = ledger.get_state(db, it.id)
        if s["item_condition"] != "SERVICEABLE":
            out.append(f"{it.display_id} is {s['item_condition']}")
        if s["custody"] != "ASSEMBLY_AREA":
            out.append(f"{it.display_id} is in {s['custody']} custody")
    assignment, unmatched, unfilled = match(lines, [_cand(db, c.item_id) for c in comps])
    for ln in lines:
        if ln.line_id in unfilled:
            out.append(f"missing {unfilled[ln.line_id]} x {ln.label}")
    for item_id in unmatched:
        out.append(f"{_item(db, item_id).display_id} does not fit any position of the current revision")
    return out


def verify_build(db: Session, actor: User, build_id: int) -> Build:
    """Confirm the actual build matches the approved definition. Records the named verifier."""
    permissions.require(actor, "build.verify")
    b = _open(db, build_id)
    if b.status == "VERIFIED":
        raise ValidationError("Build is already verified")
    problems = _problems(db, b)
    if problems:
        raise ValidationError("Cannot verify " + build_code(b.id) + ": " + "; ".join(problems))
    with transaction(db):
        b.status, b.verified_by, b.verified_at = "VERIFIED", actor.id, _now()
        audit.record(db, actor, "VERIFY", "build", b.id, after={"assembler": b.assembler_id})
    return b


# ---------------------------------------------------------------- move / dismantle
def move_build(db: Session, actor: User, build_id: int, to_location_id: int, *, remarks=None) -> dict:
    """Move an intact build: every fitted component moves with it (one event)."""
    permissions.require(actor, "build.move")
    b = _open(db, build_id)
    comps = members(db, b.id)
    if not comps:
        raise ValidationError("The build has no components to move")
    dest = config.get_location(db, to_location_id)
    if not dest.active:
        raise ValidationError("Destination location is inactive")
    event_id = new_event()
    with transaction(db):
        for c in comps:
            s = ledger.get_state(db, c.item_id)
            if s["custody"] != "ASSEMBLY_AREA":
                raise ValidationError(f"{_item(db, c.item_id).display_id} is in {s['custody']} custody; "
                                      f"the build cannot be relocated as stored")
            ledger._append(db, c.item_id, "MOVE", event_id=event_id, prev=s, user=actor, build_id=b.id,
                           to_condition=s["item_condition"], to_custody=s["custody"],
                           to_location_id=dest.id, remarks=remarks)
        audit.record(db, actor, "MOVE", "build", b.id, after={"location_id": dest.id})
    return {"event_id": event_id, "build_id": b.id, "items": len(comps)}


def dismantle_build(db: Session, actor: User, build_id: int, destinations: dict[int, int], *, reason) -> dict:
    """Close the build. Every component needs a confirmed destination; afterwards each is an ordinary
    item again (selectable individually) and membership history stays."""
    permissions.require(actor, "build.dismantle")
    reason = require_reason(reason, "dismantling a build")
    b = _open(db, build_id)
    comps = members(db, b.id)
    destinations = {int(k): int(v) for k, v in destinations.items()}
    wanted = {c.item_id for c in comps}
    given = set(destinations)
    if wanted != given:
        missing = [_item(db, i).display_id for i in sorted(wanted - given)]
        extra = sorted(given - wanted)
        raise ValidationError("Give a destination for every component"
                              + (f" (missing: {', '.join(missing)})" if missing else "")
                              + (f" (not in this build: {extra})" if extra else ""))
    event_id = new_event()
    with transaction(db):
        for c in comps:
            _remove(db, actor, b, c.item_id, destinations[c.item_id], reason, event_id)
        b.status, b.closed_at, b.closed_by, b.close_reason = "DISMANTLED", _now(), actor.id, reason
        b.verified_by = b.verified_at = None
        audit.record(db, actor, "DISMANTLE", "build", b.id, reason=reason)
    return {"event_id": event_id, "build_id": b.id, "released_items": len(comps)}


# ---------------------------------------------------------------- views
def _users(db: Session) -> dict:
    return {u.id: u.username for u in db.query(User.id, User.username)}


def build_detail(db: Session, build_id: int) -> dict:
    b = _get(db, build_id)
    users = _users(db)
    d = assemblies.get_definition(db, b.definition_id)
    rev = assemblies.get_revision(db, b.revision_id)
    locs = {l["id"]: l["path"] for l in config.list_locations(db)}
    lines = assemblies.matching_lines(db, b.revision_id)
    comps = members(db, b.id)
    cands = [_cand(db, c.item_id) for c in comps]
    assignment, unmatched, unfilled = match(lines, cands) if b.status != "DISMANTLED" else ({}, [], {})
    comp_rows, history = [], []
    for c in db.query(BuildComponent).filter(BuildComponent.build_id == b.id).order_by(BuildComponent.id):
        it = _item(db, c.item_id)
        s = ledger.get_state(db, it.id)
        row = {"component_id": c.id, "item_id": it.id, "display_id": it.display_id, "system_id": it.system_id,
               "design_code": db.get(ElementDesign, it.design_id).code,
               "design_revision": db.get(DesignRevision, it.design_revision_id).revision,
               "fitted_by": users.get(c.fitted_by), "fitted_at": c.fitted_at, "removed_at": c.removed_at,
               "removed_by": users.get(c.removed_by), "removal_reason": c.removal_reason,
               "condition": s["item_condition"], "custody": s["custody"], "location": locs.get(s["location_id"]),
               "fills_line_id": assignment.get(it.id)}
        history.append(row)
        if c.removed_at is None:
            comp_rows.append(row)
    problems = _problems(db, b) if b.status != "DISMANTLED" else []
    return {
        "id": b.id, "code": build_code(b.id), "status": b.status,
        "assembly": {"id": d.id, "code": d.code, "description": d.description},
        "revision": {"id": rev.id, "revision": rev.revision, "status": rev.status},
        "location": locs.get(build_location_id(db, b)),
        "assembler": users.get(b.assembler_id), "created_at": b.created_at,
        "verified_by": users.get(b.verified_by), "verified_at": b.verified_at,
        "closed_at": b.closed_at, "close_reason": b.close_reason, "remarks": b.remarks,
        "complete": b.status != "DISMANTLED" and not unfilled and not unmatched,
        "ready_to_issue": b.status == "VERIFIED" and not problems,
        "problems": problems,
        "lines": [{"line_id": ln.line_id, "design_code": ln.label, "required": ln.quantity,
                   "filled": ln.quantity - unfilled.get(ln.line_id, 0)} for ln in lines],
        "components": comp_rows, "history": history,
    }


def list_builds(db: Session, *, status=None, definition_id=None, item_id=None, limit=100, offset=0):
    q = db.query(Build)
    if status:
        q = q.filter(Build.status == status)
    if definition_id:
        q = q.filter(Build.definition_id == definition_id)
    if item_id:
        ids = [c.build_id for c in db.query(BuildComponent).filter(BuildComponent.item_id == item_id)]
        q = q.filter(Build.id.in_(ids))
    total = q.count()
    rows = []
    for b in q.order_by(Build.id.desc()).offset(offset).limit(min(limit, 500)):
        d = assemblies.get_definition(db, b.definition_id)
        rev = assemblies.get_revision(db, b.revision_id)
        rows.append({"id": b.id, "code": build_code(b.id), "status": b.status, "assembly_code": d.code,
                     "revision": rev.revision, "components": len(members(db, b.id)),
                     "location": {l["id"]: l["path"] for l in config.list_locations(db)}.get(
                         build_location_id(db, b))})
    return total, rows
