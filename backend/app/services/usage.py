"""Usage from OMS: cycles, castings poured and tonnage, attributed to the assembly and to each reusable element
that was fitted in the build at the time.

Rules (spec section 6):
  * Usage comes ONLY from OMS. Cycles and castings are kept separate (one cycle may make several castings).
  * Poured tonnage = castings poured x pouring weight per casting (kg) / 1000, with the weight, UOM and ME revision
    snapshotted on each event, so later weight changes never rewrite history.
  * Element tonnage is exposure, not output: foundry output is the sum of ASSEMBLY events only.
  * Idempotent: replaying (source_event_id, version) changes nothing; a higher version supersedes the earlier one and
    takes effect exactly once (only POSTED events count); an older version arriving late is ignored (stale);
    a cancellation withdraws the effect.
  * Never guessed: a missing build association, castings, cycles or time queues the event for reconciliation; an event
    that cannot be tied to an issue of that build to that WO at that time is queued; an event spanning a change of
    component membership must be split by OMS. Unknown weight leaves tonnage unknown, never zero.
  * Failures are visible and auditable: queued and rejected events keep their reason, retry count and attempt times."""
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import (Build, BuildComponent, Issue, IssueLine, Item, MeMapping, SetupLine, UsageEvent, UsageExposure, User, _now)
from . import audit, builds, issues, permissions
from .common import clean, require_reason, transaction
from .errors import ConflictError, NotFoundError, ValidationError

REVIEW_CODES = ("MISSING_BUILD", "UNKNOWN_BUILD", "MISSING_CASTINGS", "MISSING_CYCLES", "MISSING_TIME",
                "NO_ISSUE", "SPLIT_REQUIRED")  # queue for reconciliation
REJECT_CODES = ("INVALID_NUMBER", "FUTURE_TIME", "INVALID_TIME")  # the sender must correct with a new version
FUTURE_TOLERANCE = timedelta(minutes=5)


class _Skip(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def _dt(v, what="time"):
    """ISO string -> naive UTC datetime (None stays None)."""
    if v in (None, ""):
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise _Skip("REJECTED", "INVALID_TIME", f"{what} {v!r} is not an ISO date-time")
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def _count(payload, key, missing_code):
    v = payload.get(key)
    if v in (None, ""):
        raise _Skip("PENDING", missing_code, f"{key} is missing; OMS must supply the actual {key} (it is never estimated)")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise _Skip("REJECTED", "INVALID_NUMBER", f"{key} must be a whole number, got {v!r}")
    if f != int(f) or f < 0:
        raise _Skip("REJECTED", "INVALID_NUMBER", f"{key} must be a whole number >= 0, got {v!r}")
    return int(f)


def _resolve_build(db: Session, payload) -> Build:
    ref = payload.get("build", payload.get("build_id"))
    if ref in (None, ""):
        raise _Skip("PENDING", "MISSING_BUILD", "No build association was supplied; it is never taken from the current "
                    "build or guessed from the WO")
    m = re.fullmatch(r"(?:BLD-)?0*(\d+)", str(ref).strip().upper())
    b = db.get(Build, int(m.group(1))) if m else None
    if b is None:
        raise _Skip("PENDING", "UNKNOWN_BUILD", f"Build {ref!r} does not exist")
    return b


def _effective_issue(db: Session, build: Build, wo: str, at: datetime) -> Issue | None:
    by_line = [r[0] for r in db.query(IssueLine.issue_id).join(SetupLine, SetupLine.id == IssueLine.setup_line_id)
               .filter(SetupLine.wo_id == wo)]
    rows = db.query(Issue).filter(Issue.build_id == build.id, (Issue.wo_id == wo) | Issue.id.in_(by_line or [0]),
                                  Issue.issued_at <= at).order_by(Issue.id.desc()).all()
    for i in rows:
        if i.handed_over_at is not None and at > i.handed_over_at:  # the tooling had moved on to the next setup
            continue
        if any(st is not None for st in issues.line_states(db, i.id).values()):  # not a void issue
            return i
    return None


def _mapping_for(db: Session, issue: Issue, wo: str, operation) -> MeMapping | None:
    """The ME mapping (and so the pouring weight) of the issue line this WO belongs to; an issue with several lines for
    that WO uses the line whose operation matches the event, else the issue's own mapping."""
    rows = [db.get(MeMapping, il.mapping_id) for il in db.query(IssueLine).join(SetupLine, SetupLine.id == IssueLine.setup_line_id)
            .filter(IssueLine.issue_id == issue.id, SetupLine.wo_id == wo).order_by(IssueLine.id)]
    if len(rows) > 1 and operation:
        same = [m for m in rows if (m.operation_id or "").upper() == str(operation).strip().upper()]
        rows = same or rows
    return rows[0] if rows else db.get(MeMapping, issue.mapping_id)


def members_at(db: Session, build_id: int, at: datetime) -> list[int]:
    """Item ids that were fitted in the build at that moment."""
    return [c.item_id for c in db.query(BuildComponent).filter(
        BuildComponent.build_id == build_id, BuildComponent.fitted_at <= at).order_by(BuildComponent.id)
        if c.removed_at is None or c.removed_at > at]


def _membership_changed_within(db: Session, build_id: int, start: datetime, end: datetime) -> bool:
    for c in db.query(BuildComponent).filter(BuildComponent.build_id == build_id):
        if start < c.fitted_at <= end or (c.removed_at is not None and start < c.removed_at <= end):
            return True
    return False


def _process(db: Session, ev: UsageEvent, payload: dict, now: datetime | None = None) -> None:
    """Try to post the event with current data. Sets status/reason on `ev`; never raises for data problems."""
    now = now or _now()
    ev.last_attempt_at = now
    try:
        wo = clean(payload.get("wo_id"))
        ev.wo_id, ev.operation_id = wo, clean(payload.get("operation_id"))
        end = _dt(payload.get("production_end", payload.get("production_time")), "production_end")
        start = _dt(payload.get("production_start"), "production_start") or end
        ev.production_start, ev.production_end = start, end
        if end is None:
            raise _Skip("PENDING", "MISSING_TIME", "production_end is missing")
        if end > now + FUTURE_TOLERANCE:
            raise _Skip("REJECTED", "FUTURE_TIME", "The production time is in the future")
        if start > end:
            raise _Skip("REJECTED", "INVALID_TIME", "production_start is after production_end")
        ev.cycles = _count(payload, "cycles", "MISSING_CYCLES")
        ev.castings = _count(payload, "castings", "MISSING_CASTINGS")
        if not wo:
            raise _Skip("PENDING", "NO_ISSUE", "wo_id is missing, so the issue cannot be identified")
        build = _resolve_build(db, payload)
        ev.build_id = build.id
        issue = _effective_issue(db, build, wo, end)
        if issue is None:
            raise _Skip("PENDING", "NO_ISSUE", f"{builds.build_code(build.id)} had not been issued to WO {wo} at that time "
                        f"(or the issue was voided)")
        ev.issue_id = issue.id
        if start < end and _membership_changed_within(db, build.id, start, end):
            raise _Skip("PENDING", "SPLIT_REQUIRED", "The build's component membership changed during this period; OMS must "
                        "split the event at the change (the cycles are not apportioned by guesswork)")

        # weight snapshot: what OMS says for this event, else ME's value for the option issued
        w, wrev, wsrc = None, None, None
        pw = payload.get("pouring_weight_kg")
        if pw not in (None, ""):
            try:
                w = float(pw)
            except (TypeError, ValueError):
                raise _Skip("REJECTED", "INVALID_NUMBER", f"pouring_weight_kg must be a number, got {pw!r}")
            if w <= 0:
                raise _Skip("REJECTED", "INVALID_NUMBER", "pouring_weight_kg must be greater than zero")
            wrev, wsrc = clean(payload.get("pouring_weight_revision")), "PAYLOAD"
        else:
            m = _mapping_for(db, issue, wo, ev.operation_id)
            if m is not None and m.pouring_weight_kg:
                w, wrev, wsrc = m.pouring_weight_kg, m.pouring_weight_revision, "ME_MAPPING"
        ev.weight_kg, ev.weight_uom, ev.weight_revision, ev.weight_source = w, "kg", wrev, wsrc
        ev.tonnage = round(ev.castings * w / 1000.0, 6) if w is not None else None

        db.query(UsageExposure).filter(UsageExposure.event_id == ev.id).delete()
        db.add(UsageExposure(event_id=ev.id, kind="ASSEMBLY", build_id=build.id, item_id=None, cycles=ev.cycles,
                             tonnage=ev.tonnage))
        for item_id in members_at(db, build.id, end):
            db.add(UsageExposure(event_id=ev.id, kind="ELEMENT", build_id=build.id, item_id=item_id, cycles=ev.cycles,
                                 tonnage=ev.tonnage))
        ev.status, ev.reason_code, ev.reason, ev.posted_at = "POSTED", None, None, now
    except _Skip as s:
        ev.status, ev.reason_code, ev.reason = s.status, s.code, s.message
        ev.posted_at = None


# ---------------------------------------------------------------- ingest
def ingest(db: Session, actor: User, payload: dict) -> dict:
    """Accept one usage event from OMS. Always leaves an auditable record, even for rejected input."""
    permissions.require(actor, "usage.ingest")
    sid = str(payload.get("source_event_id") or "").strip()
    err = None
    version = 1
    if not sid:
        err = "source_event_id is required"
    else:
        try:
            version = int(payload.get("version", 1))
            if version < 1:
                raise ValueError
        except (TypeError, ValueError):
            err = "version must be a whole number >= 1"
    if err:
        with transaction(db):
            audit.record(db, actor, "REJECT_USAGE", "usage_event", sid or None, after={"payload": payload}, reason=err)
        raise ValidationError(err)

    canon = json.dumps(payload, sort_keys=True, default=str)
    existing = db.query(UsageEvent).filter_by(source_event_id=sid).order_by(UsageEvent.version).all()
    same = next((e for e in existing if e.version == version), None)
    if same is not None:
        if same.payload == canon:
            return {"result": "DUPLICATE", "event": event_dict(db, same)}  # a replay changes nothing at all
        with transaction(db):
            audit.record(db, actor, "REJECT_USAGE", "usage_event", same.id, after={"payload": payload},
                         reason="same event id and version, different content")
        raise ConflictError(f"Event {sid} version {version} was already received with different content; send a higher "
                            f"version for a correction")
    latest = existing[-1].version if existing else 0
    with transaction(db):
        ev = UsageEvent(source_event_id=sid, version=version, status="PENDING", payload=canon, received_by=actor.id)
        db.add(ev)
        db.flush()
        if version < latest:  # out-of-order: a newer version is already in
            ev.status, ev.reason_code = "SUPERSEDED", "STALE"
            ev.reason = f"Version {version} arrived after version {latest}; ignored"
            result = "STALE"
        else:
            for old in existing:
                if old.status in ("POSTED", "PENDING", "CANCELLED", "REJECTED"):
                    old.status = "SUPERSEDED"  # its exposures stop counting; nothing is deleted
            if payload.get("cancelled"):
                ev.status, ev.reason_code, ev.reason = "CANCELLED", None, "Cancelled by OMS"
            else:
                _process(db, ev, payload)
            result = ev.status
        audit.record(db, actor, "USAGE_" + result, "usage_event", ev.id,
                     after={"source_event_id": sid, "version": version, "status": ev.status}, reason=ev.reason)
    return {"result": result, "event": event_dict(db, ev)}


def _merged(ev: UsageEvent) -> dict:
    return {**json.loads(ev.payload), **json.loads(ev.overrides or "{}")}


def retry(db: Session, actor: User, event_id: int) -> dict:
    """Re-run a queued event against current data (e.g. after the issue association was fixed)."""
    permissions.require(actor, "usage.reconcile")
    ev = _get(db, event_id)
    if ev.status not in ("PENDING", "REJECTED"):
        raise ValidationError(f"Only queued or rejected events can be retried (this one is {ev.status})")
    with transaction(db):
        ev.retry_count += 1
        _process(db, ev, _merged(ev))
        audit.record(db, actor, "USAGE_RETRY", "usage_event", ev.id, after={"status": ev.status}, reason=ev.reason)
    return event_dict(db, ev)


def resolve(db: Session, actor: User, event_id: int, *, reason, build=None, castings=None, cycles=None,
            weight_kg=None, weight_revision=None, wo_id=None) -> dict:
    """Authorised reconciliation: enter what OMS did not supply, with a documented reason. The original
    message is kept as received; the entered values are recorded separately and audited."""
    permissions.require(actor, "usage.reconcile")
    reason = require_reason(reason, "reconciling a usage event")
    ev = _get(db, event_id)
    if ev.status not in ("PENDING",):
        raise ValidationError(f"Only events queued for reconciliation can be resolved (this one is {ev.status})")
    ov = json.loads(ev.overrides or "{}")
    for k, v in (("build", build), ("castings", castings), ("cycles", cycles), ("pouring_weight_kg", weight_kg),
                 ("pouring_weight_revision", weight_revision), ("wo_id", wo_id)):
        if v not in (None, ""):
            ov[k] = v
    if not any(v not in (None, "") for v in (build, castings, cycles, weight_kg, wo_id)):
        raise ValidationError("Enter the missing value (build, castings, cycles, weight or WO)")
    with transaction(db):
        ev.overrides, ev.resolved_by, ev.resolution_note = json.dumps(ov, default=str), actor.id, reason
        ev.retry_count += 1
        _process(db, ev, _merged(ev))
        audit.record(db, actor, "USAGE_RESOLVE", "usage_event", ev.id, after={"entered": ov, "status": ev.status}, reason=reason)
    return event_dict(db, ev)


# ---------------------------------------------------------------- views
def _get(db: Session, event_id: int) -> UsageEvent:
    ev = db.get(UsageEvent, event_id)
    if ev is None:
        raise NotFoundError(f"Usage event {event_id} not found")
    return ev


def event_dict(db: Session, ev: UsageEvent) -> dict:
    return {"id": ev.id, "source_event_id": ev.source_event_id, "version": ev.version, "status": ev.status,
            "reason_code": ev.reason_code, "reason": ev.reason, "retry_count": ev.retry_count,
            "received_at": ev.received_at, "last_attempt_at": ev.last_attempt_at, "posted_at": ev.posted_at,
            "wo_id": ev.wo_id, "operation_id": ev.operation_id,
            "build": builds.build_code(ev.build_id) if ev.build_id else None, "build_id": ev.build_id,
            "issue_id": ev.issue_id, "production_start": ev.production_start, "production_end": ev.production_end,
            "cycles": ev.cycles, "castings": ev.castings, "weight_kg": ev.weight_kg, "weight_uom": ev.weight_uom,
            "weight_revision": ev.weight_revision, "weight_source": ev.weight_source, "tonnage": ev.tonnage,
            "resolution_note": ev.resolution_note, "overrides": json.loads(ev.overrides) if ev.overrides else None,
            "elements": [{"item_id": x.item_id, "display_id": db.get(Item, x.item_id).display_id}
                         for x in db.query(UsageExposure).filter_by(event_id=ev.id, kind="ELEMENT")]
            if ev.status == "POSTED" else []}


def list_events(db: Session, *, wo_id=None, build_id=None, status=None, source_event_id=None, limit=100, offset=0):
    q = db.query(UsageEvent)
    if wo_id:
        q = q.filter(UsageEvent.wo_id == wo_id.strip())
    if build_id:
        q = q.filter(UsageEvent.build_id == build_id)
    if status:
        q = q.filter(UsageEvent.status == status)
    if source_event_id:
        q = q.filter(UsageEvent.source_event_id == source_event_id.strip())
    total = q.count()
    return total, [event_dict(db, e) for e in q.order_by(UsageEvent.id.desc()).offset(offset).limit(min(limit, 500))]


def exception_count(db: Session) -> int:
    return db.query(UsageEvent).filter(UsageEvent.status.in_(("PENDING", "REJECTED"))).count()


def exceptions(db: Session) -> dict:
    """Integration exceptions: events that did not post, with reason, retry count and last attempt, plus the
    last successful sync."""
    rows = db.query(UsageEvent).filter(UsageEvent.status.in_(("PENDING", "REJECTED"))).order_by(UsageEvent.id).all()
    last = db.execute(text("SELECT MAX(posted_at) FROM usage_events WHERE status = 'POSTED'")).scalar()
    return {"events": [event_dict(db, e) for e in rows], "pending": sum(e.status == "PENDING" for e in rows),
            "rejected": sum(e.status == "REJECTED" for e in rows), "last_successful_sync": last}


def _sum(rows):
    cycles = castings = events = unknown = 0
    tonnage = 0.0
    for e in rows:
        events += 1
        cycles += e.cycles or 0
        castings += e.castings or 0
        if e.tonnage is None:
            unknown += 1
        else:
            tonnage += e.tonnage
    return {"events": events, "cycles": cycles, "castings": castings, "tonnage": round(tonnage, 6),
            "events_with_unknown_tonnage": unknown}


def assembly_usage(db: Session, *, build_id=None, wo_id=None, definition_id=None, date_from=None, date_to=None) -> dict:
    """Assembly-level usage (the only figure that adds up to foundry output)."""
    q = db.query(UsageEvent).filter(UsageEvent.status == "POSTED")
    if build_id:
        q = q.filter(UsageEvent.build_id == build_id)
    if wo_id:
        q = q.filter(UsageEvent.wo_id == wo_id.strip())
    if definition_id:
        q = q.filter(UsageEvent.build_id.in_([b.id for b in db.query(Build).filter(Build.definition_id == definition_id)]))
    if date_from:
        q = q.filter(UsageEvent.production_end >= date_from)
    if date_to:
        q = q.filter(UsageEvent.production_end <= date_to)
    return _sum(q.all())


def item_usage(db: Session, item_id: int) -> dict:
    """Cycles/tonnage recorded for one element (exposure). Unknown opening usage stays unknown."""
    item = db.get(Item, item_id)
    if item is None:
        raise NotFoundError(f"Item {item_id} not found")
    rows = db.query(UsageEvent, UsageExposure).join(UsageExposure, UsageExposure.event_id == UsageEvent.id).filter(
        UsageExposure.item_id == item_id, UsageEvent.status == "POSTED").order_by(UsageEvent.production_end).all()
    cycles = sum(x.cycles for _, x in rows)
    known = [x.tonnage for _, x in rows if x.tonnage is not None]
    out = {"item_id": item_id, "display_id": item.display_id, "recorded_cycles": cycles,
           "recorded_tonnage": round(sum(known), 6), "events_with_unknown_tonnage": len(rows) - len(known),
           "opening_cycles": item.opening_cycles, "opening_tonnage": item.opening_tonnage,
           "total_cycles": None if item.opening_cycles is None else cycles + item.opening_cycles,
           "total_tonnage": None if item.opening_tonnage is None or len(rows) != len(known)
           else round(sum(known) + item.opening_tonnage, 6),
           "events": [{"event_id": e.id, "wo_id": e.wo_id, "build": builds.build_code(e.build_id), "at": e.production_end,
                       "cycles": x.cycles, "tonnage": x.tonnage} for e, x in rows]}
    return out


def foundry_output(db: Session, *, date_from=None, date_to=None) -> dict:
    """Total poured output = assembly events only. Component exposure is never summed."""
    return assembly_usage(db, date_from=date_from, date_to=date_to)


def associations(db: Session, wo_id: str) -> list[dict]:
    """What OMS needs to attribute production: the WO/operation, build, and the actual constituent items with the
    periods they were fitted (effective association history)."""
    out = []
    by_line = [r[0] for r in db.query(IssueLine.issue_id).join(SetupLine, SetupLine.id == IssueLine.setup_line_id)
               .filter(SetupLine.wo_id == wo_id.strip())]
    for i in db.query(Issue).filter((Issue.wo_id == wo_id.strip()) | Issue.id.in_(by_line or [0])).order_by(Issue.id):
        d = issues.issue_detail(db, i.id)
        comps = [{"item_id": c.item_id, "display_id": db.get(Item, c.item_id).display_id, "fitted_at": c.fitted_at,
                  "removed_at": c.removed_at}
                 for c in db.query(BuildComponent).filter(BuildComponent.build_id == i.build_id).order_by(BuildComponent.id)]
        out.append({"issue": d["code"], "wo_id": i.wo_id, "operation_id": i.operation_id, "part_id": i.part_id,
                    "build": d["build"]["code"], "status": d["status"], "issued_at": i.issued_at,
                    "constituents": comps})
    return out
