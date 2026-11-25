"""The item ledger: the only place where condition, custody and location change.

Rules enforced here:
  * append-only (also by DB triggers); corrections are REVERSAL rows
  * condition / custody / location are derived from the latest effective row (item_state view)
  * one event_id per API call; reversal reverses every row of that event, or nothing
  * a location change never alters condition (a HOLD survives a move)
"""
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import CONDITIONS, Item, Ledger, User
from . import audit, batches, config, permissions, recheck
from .common import new_event, require_reason, transaction
from .errors import NotFoundError, ValidationError

# (from condition, to condition) -> permission needed. Anything not listed is not allowed.
TRANSITIONS: dict[tuple[str, str], str] = {
    ("SERVICEABLE", "HOLD"): "condition.hold",
    ("AWAITING_INSPECTION", "HOLD"): "condition.hold",
    ("UNDER_REPAIR", "HOLD"): "condition.hold",
    ("SERVICEABLE", "AWAITING_INSPECTION"): "condition.reinspect",
    ("UNDER_REPAIR", "AWAITING_INSPECTION"): "condition.reinspect",
    ("HOLD", "UNDER_REPAIR"): "condition.repair",
    ("AWAITING_INSPECTION", "SERVICEABLE"): "condition.release",
    ("HOLD", "AWAITING_INSPECTION"): "condition.reinspect",  # further inspection (a hold never jumps to serviceable)
    ("HOLD", "SCRAPPED"): "condition.scrap",  # item stays held until the scrap approver acts
}
MOVABLE_CUSTODIES = ("STORES", "REPAIR")


# ---------------------------------------------------------------- state
def get_state(db: Session, item_id: int) -> dict | None:
    row = db.execute(text("SELECT * FROM item_state WHERE item_id = :i"), {"i": item_id}).mappings().first()
    return dict(row) if row else None


def active_build_id(db: Session, item_id: int) -> int | None:
    return db.execute(text("SELECT build_id FROM build_components WHERE item_id = :i AND removed_at IS NULL"),
                      {"i": item_id}).scalar()


def _require_item(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise NotFoundError(f"Item {item_id} not found")
    return item


def _label(item: Item) -> str:
    return item.display_id


def _append(db: Session, item_id: int, action: str, *, event_id: str, prev: dict | None, user: User,
            to_condition: str, to_custody: str, to_location_id, remarks=None, ref_txn_id=None,
            build_id=None, issue_id=None, source=None) -> Ledger:
    row = Ledger(
        item_id=item_id, action=action, event_id=event_id, user_id=user.id, remarks=remarks,
        ref_txn_id=ref_txn_id, build_id=build_id, issue_id=issue_id, source=source,
        # sequence is derived from the state the caller validated against; a competing writer on
        # the same stale state hits the (item_id, item_seq) unique constraint -> ConflictError
        item_seq=(prev["last_seq"] if prev else 0) + 1,
        from_condition=prev["item_condition"] if prev else None, to_condition=to_condition,
        from_custody=prev["custody"] if prev else None, to_custody=to_custody,
        from_location_id=prev["location_id"] if prev else None, to_location_id=to_location_id)
    db.add(row)
    return row


def receive(db: Session, item: Item, actor: User, *, condition: str, location_id, event_id: str,
            remarks="Initial registration") -> Ledger:
    """First ledger row of an item (caller owns the transaction and has checked permission)."""
    if condition not in CONDITIONS:
        raise ValidationError(f"condition must be one of {', '.join(CONDITIONS)}")
    return _append(db, item.id, "RECEIVE", event_id=event_id, prev=None, user=actor,
                   to_condition=condition, to_custody="STORES", to_location_id=location_id,
                   remarks=remarks)


# ---------------------------------------------------------------- moves
def _resolve_items(db: Session, item_ids) -> list[Item]:
    ids = list(dict.fromkeys(item_ids or []))
    if not ids:
        raise ValidationError("No items given")
    items = [_require_item(db, i) for i in ids]
    for it in items:
        if not it.active:
            raise ValidationError(f"Item {_label(it)} is retired")
    return items


def move_items(db: Session, actor: User, item_ids, to_location_id: int, *, remarks=None) -> dict:
    """Record a physical move. Condition and custody are carried over unchanged."""
    permissions.require(actor, "item.move")
    loc = config.get_location(db, to_location_id)
    if not loc.active:
        raise ValidationError("Destination location is inactive")
    items = _resolve_items(db, item_ids)
    event_id = new_event()
    with transaction(db):
        for it in items:
            s = get_state(db, it.id)
            if s["custody"] not in MOVABLE_CUSTODIES:
                b = active_build_id(db, it.id)
                why = (f"it is fitted in build BLD-{b:06d}; move the whole build, or remove the item first"
                       if b and s["custody"] == "ASSEMBLY_AREA" else f"it is in {s['custody']} custody")
                raise ValidationError(f"Item {_label(it)} cannot be relocated by a stores move: {why}")
            _append(db, it.id, "MOVE", event_id=event_id, prev=s, user=actor,
                    to_condition=s["item_condition"], to_custody=s["custody"],
                    to_location_id=loc.id, remarks=remarks)
    return {"event_id": event_id, "item_ids": [i.id for i in items]}


# ---------------------------------------------------------------- condition
def apply_condition(db: Session, actor: User, items: list[Item], to_condition: str, reason: str,
                    event_id: str, source=None) -> None:
    """Write the condition change rows. The caller owns the transaction, so workflows (inspection, repair,
    scrap) can change the condition and their own records atomically."""
    for it in items:
        s = get_state(db, it.id)
        perm = TRANSITIONS.get((s["item_condition"], to_condition))
        if perm is None:
            raise ValidationError(
                f"Item {_label(it)}: {s['item_condition']} -> {to_condition} is not allowed")
        permissions.require(actor, perm)
        if s["custody"] == "MISSING":
            raise ValidationError(f"Item {_label(it)} is marked missing; it must be recovered through a stock count "
                                  f"before its condition can change")
        if to_condition == "SERVICEABLE":
            from . import inspections  # local import: inspections writes through this module
            inspections.require_passing_inspection(db, it, s)
        b = active_build_id(db, it.id)
        if b and to_condition in ("UNDER_REPAIR", "SCRAPPED"):
            raise ValidationError(
                f"Item {_label(it)} is fitted in build BLD-{b:06d}; remove it from the build before "
                f"changing its condition to {to_condition} (hold, inspection and release can be done "
                f"in place)")
        custody = "REPAIR" if to_condition == "UNDER_REPAIR" else (
            "STORES" if s["custody"] == "REPAIR" else s["custody"])
        _append(db, it.id, "SET_CONDITION", event_id=event_id, prev=s, user=actor,
                to_condition=to_condition, to_custody=custody,
                to_location_id=s["location_id"], remarks=reason, source=source)
        audit.record(db, actor, "SET_CONDITION", "item", it.id,
                     before={"condition": s["item_condition"]}, after={"condition": to_condition},
                     reason=reason)
    if to_condition != "SERVICEABLE":
        recheck.flag(db, actor, f"Item condition changed to {to_condition}: {reason}",
                     item_ids=[i.id for i in items])


def set_condition(db: Session, actor: User, item_ids, to_condition: str, *, reason, source=None) -> dict:
    """Move items along the allowed condition transitions; each needs its own role. Becoming SERVICEABLE
    needs a passing inspection (Engineering then approves the technical release). `source` marks rows
    written by a workflow (inspection, repair...), which are corrected through that workflow, not reversed."""
    if to_condition not in CONDITIONS:
        raise ValidationError(f"condition must be one of {', '.join(CONDITIONS)}")
    reason = require_reason(reason, "a condition change")
    items = _resolve_items(db, item_ids)
    event_id = new_event()
    with transaction(db):
        apply_condition(db, actor, items, to_condition, reason, event_id, source)
    return {"event_id": event_id, "item_ids": [i.id for i in items]}


# ---------------------------------------------------------------- reversal
def _perm_for_row(row: Ledger) -> str:
    if row.source:
        raise ValidationError(
            f"Entry {row.txn_id} was written by the {row.source.lower()} workflow and cannot be reversed by hand; "
            f"correct it through that workflow")
    if row.action == "ISSUE":
        return "issue.create"
    if row.action == "ACKNOWLEDGE":
        return "issue.acknowledge"
    if row.action == "RETURN":
        return "return.receive"
    if row.action in ("FIT", "REMOVE"):
        raise ValidationError(
            f"Entry {row.txn_id} is a build {row.action.lower()} and cannot be reversed: build membership "
            f"has its own history. Correct it by removing/fitting the item explicitly")
    if row.action == "RECEIVE":
        return "item.register"
    if row.action == "MOVE":
        return "item.move"
    return TRANSITIONS[(row.from_condition, row.to_condition)]


def reverse(db: Session, actor: User, txn_id: int, *, reason) -> list[Ledger]:
    """Append REVERSAL rows for txn_id's whole event, or nothing.

    Every not-yet-reversed row sharing the target's event_id is reversed together under one new
    event. Each item returns to exactly its previous condition/custody/location, so only an item's
    current effective entry can be reversed. The actor needs the permission of the original action.
    """
    reason = require_reason(reason, "a reversal")
    target = db.get(Ledger, txn_id)
    if target is None:
        raise NotFoundError(f"Ledger entry {txn_id} not found")
    if target.action == "REVERSAL":
        raise ValidationError("A REVERSAL cannot itself be reversed; make a new entry instead")

    with transaction(db):
        reversed_ids = db.query(Ledger.ref_txn_id).filter(
            Ledger.action == "REVERSAL", Ledger.ref_txn_id.isnot(None))
        targets = db.query(Ledger).filter(
            Ledger.event_id == target.event_id, Ledger.action != "REVERSAL",
            ~Ledger.txn_id.in_(reversed_ids)).order_by(Ledger.txn_id).all()
        if target.txn_id not in {t.txn_id for t in targets}:
            raise ValidationError(f"Entry {txn_id} is already reversed")

        scope = f"event {target.event_id[:8]}: "
        states = {}
        for t in targets:
            permissions.require(actor, _perm_for_row(t))
            cur = get_state(db, t.item_id)
            if cur is None or cur["txn_id"] != t.txn_id:
                raise ValidationError(
                    f"{scope}entry {t.txn_id} is not the latest entry for item {t.item_id} "
                    f"(already reversed or superseded); nothing was reversed")
            has_previous = db.execute(text(
                "SELECT 1 FROM ledger l WHERE l.item_id = :i AND l.txn_id < :t"
                " AND l.action <> 'REVERSAL' AND NOT EXISTS"
                " (SELECT 1 FROM ledger r WHERE r.action = 'REVERSAL' AND r.ref_txn_id = l.txn_id)"
                " LIMIT 1"), {"i": t.item_id, "t": t.txn_id}).first()
            if not has_previous:
                raise ValidationError(
                    f"{scope}entry {t.txn_id} is the item's first entry and cannot be reversed; "
                    f"nothing was reversed")
            states[t.txn_id] = cur

        event_id = new_event()
        out = []
        brows = batches.pending_reversal_rows(db, target.event_id)  # consumables moved in the same event
        for t in targets:
            cur = states[t.txn_id]
            # the REVERSAL row itself records the swapped from/to for readability; item state
            # comes from the earlier effective row, not from this one
            row = Ledger(
                item_id=t.item_id, action="REVERSAL", event_id=event_id, user_id=actor.id,
                remarks=reason, ref_txn_id=t.txn_id, group_id=t.group_id,
                item_seq=cur["last_seq"] + 1,
                from_condition=t.to_condition, to_condition=t.from_condition or t.to_condition,
                from_custody=t.to_custody, to_custody=t.from_custody or t.to_custody,
                from_location_id=t.to_location_id, to_location_id=t.from_location_id)
            db.add(row)
            out.append(row)
            audit.record(db, actor, "REVERSE", "ledger", t.txn_id, reason=reason,
                         after={"event_id": event_id, "item_id": t.item_id})
        if brows:
            batches.reverse_rows(db, actor, brows, new_event_id=event_id, reason=reason)
    return out


# ---------------------------------------------------------------- queries
def ledger_rows(db: Session, *, item_id=None, event_id=None, user=None, action=None,
                date_from: datetime | None = None, date_to: datetime | None = None,
                limit=500, offset=0) -> tuple[int, list[dict]]:
    q = db.query(Ledger, User.username).join(User, User.id == Ledger.user_id)
    if item_id:
        q = q.filter(Ledger.item_id == item_id)
    if event_id:
        q = q.filter(Ledger.event_id == event_id)
    if user:
        q = q.filter(User.username == user.strip().lower())
    if action:
        q = q.filter(Ledger.action == action.strip().upper())
    if date_from:
        q = q.filter(Ledger.txn_time >= date_from)
    if date_to:
        q = q.filter(Ledger.txn_time <= date_to)
    total = q.count()
    rows = q.order_by(Ledger.txn_id.desc()).offset(offset).limit(min(limit, 5000)).all()
    locs = {l["id"]: l["path"] for l in config.list_locations(db)}
    items = {i.id: i.display_id for i in db.query(Item.id, Item.display_id).filter(
        Item.id.in_({r.Ledger.item_id for r in rows}))} if rows else {}
    out = []
    for r in rows:
        l = r.Ledger
        out.append({
            "txn_id": l.txn_id, "txn_time": l.txn_time, "event_id": l.event_id,
            "group_id": l.group_id, "build_id": l.build_id, "issue_id": l.issue_id, "item_id": l.item_id, "display_id": items.get(l.item_id),
            "action": l.action, "from_condition": l.from_condition, "to_condition": l.to_condition,
            "from_custody": l.from_custody, "to_custody": l.to_custody,
            "from_location": locs.get(l.from_location_id), "to_location": locs.get(l.to_location_id),
            "user": r.username, "remarks": l.remarks, "ref_txn_id": l.ref_txn_id})
    return total, out


def dashboard_counts(db: Session) -> dict:
    cond = {c: 0 for c in CONDITIONS}
    for c, n in db.execute(text(
            "SELECT s.item_condition, COUNT(*) FROM item_state s JOIN items i ON i.id = s.item_id"
            " WHERE i.active = 1 GROUP BY s.item_condition")):
        cond[c] = n
    cust = {}
    for c, n in db.execute(text(
            "SELECT s.custody, COUNT(*) FROM item_state s JOIN items i ON i.id = s.item_id"
            " WHERE i.active = 1 GROUP BY s.custody")):
        cust[c] = n
    pending = db.execute(text("SELECT COUNT(*) FROM import_review WHERE status = 'PENDING'")).scalar()
    return {"condition": cond, "custody": cust, "holds": cond["HOLD"],
            "pending_import_review": pending}
