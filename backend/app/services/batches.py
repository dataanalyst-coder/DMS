"""Consumable batches: stock that is counted, not individually tracked.

  * Every change is a row in the append-only batch ledger carrying signed deltas; usable / held /
    outstanding / consumed are their SUMS. Nothing is edited in place and nothing is deleted.
  * Balances can never go negative: checked here with a clear message AND by a database trigger.
  * Concurrent writers are sequenced per batch (batch_seq), so a writer acting on a stale balance
    collides instead of overdrawing the batch.
  * Corrections are REVERSAL rows with negated deltas; consumed material cannot be 'returned twice'.
  * A batch's location is the destination of its latest un-reversed RECEIVE/MOVE."""
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import BatchLedger, ConsumableBatch, ElementDesign, IssueBatch, Location, User
from . import audit, config, designs, permissions
from .common import clean, new_event, require_reason, transaction
from .errors import NotFoundError, ValidationError

EPS = 1e-6


def _q(x) -> float:
    return round(float(x), 6)


def batch_code(batch_id: int) -> str:
    return f"BATCH-{batch_id:06d}"


def get_batch(db: Session, batch_id: int) -> ConsumableBatch:
    b = db.get(ConsumableBatch, batch_id)
    if b is None:
        raise NotFoundError(f"Batch {batch_id} not found")
    return b


def balance(db: Session, batch_id: int) -> dict:
    r = db.execute(text(
        "SELECT COALESCE(SUM(d_usable),0), COALESCE(SUM(d_held),0), COALESCE(SUM(d_outstanding),0), "
        "COALESCE(SUM(d_consumed),0), COALESCE(MAX(batch_seq),0) FROM batch_ledger WHERE batch_id = :b"),
        {"b": batch_id}).one()
    return {"usable": _q(r[0]), "held": _q(r[1]), "outstanding": _q(r[2]), "consumed": _q(r[3]), "last_seq": r[4]}


def location_id(db: Session, batch_id: int) -> int | None:
    return db.execute(text(
        "SELECT l.to_location_id FROM batch_ledger l WHERE l.batch_id = :b AND l.action IN ('RECEIVE', 'MOVE') "
        "AND l.to_location_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM batch_ledger r WHERE r.action = 'REVERSAL' AND r.ref_txn_id = l.txn_id) "
        "ORDER BY l.txn_id DESC LIMIT 1"), {"b": batch_id}).scalar()


def status_of(b: ConsumableBatch, bal: dict, today: date | None = None) -> str:
    today = today or date.today()
    if not b.active:
        return "INACTIVE"
    if bal["usable"] + bal["held"] + bal["outstanding"] <= EPS:
        return "EXHAUSTED"
    if b.expiry_date and b.expiry_date < today:
        return "EXPIRED"
    if bal["usable"] <= EPS and bal["held"] > EPS:
        return "HELD"
    return "AVAILABLE" if bal["usable"] > EPS else "ISSUED_OUT"


def _append(db: Session, b: ConsumableBatch, action: str, *, event_id: str, bal: dict, user: User, seq_offset=0,
            d_usable=0.0, d_held=0.0, d_outstanding=0.0, d_consumed=0.0, to_location_id=None, issue_id=None,
            remarks=None, ref_txn_id=None) -> BatchLedger:
    row = BatchLedger(batch_id=b.id, action=action, event_id=event_id, user_id=user.id, remarks=remarks,
                      batch_seq=bal["last_seq"] + 1 + seq_offset, d_usable=_q(d_usable), d_held=_q(d_held),
                      d_outstanding=_q(d_outstanding), d_consumed=_q(d_consumed), to_location_id=to_location_id,
                      issue_id=issue_id, ref_txn_id=ref_txn_id)
    db.add(row)
    return row


def _positive(q, what="Quantity") -> float:
    try:
        v = _q(q)
    except (TypeError, ValueError):
        raise ValidationError(f"{what} must be a number")
    if v <= 0:
        raise ValidationError(f"{what} must be greater than zero")
    return v


# ---------------------------------------------------------------- receive and stock handling
def receive_batch(db: Session, actor: User, *, design_id, quantity, location_id, supplier_lot=None,
                  expiry_date=None, remarks=None) -> ConsumableBatch:
    permissions.require(actor, "batch.receive")
    design = designs.get_design(db, design_id)
    if design.kind != "CONSUMABLE":
        raise ValidationError(f"Design {design.code} is a reusable element; register it as an item instead")
    if not design.active:
        raise ValidationError(f"Design {design.code} is inactive")
    rev = designs.current_approved(db, design.id)
    if rev is None:
        raise ValidationError(f"Design {design.code} has no approved revision")
    qty = _positive(quantity, "Received quantity")
    loc = config.get_location(db, location_id)
    if not loc.active:
        raise ValidationError("Location is inactive")
    event_id = new_event()
    with transaction(db):
        b = ConsumableBatch(batch_code="PENDING", design_id=design.id, design_revision_id=rev.id,
                            supplier_lot=clean(supplier_lot), uom=design.base_uom, received_qty=qty,
                            expiry_date=expiry_date, remarks=clean(remarks), created_by=actor.id)
        db.add(b)
        db.flush()
        b.batch_code = batch_code(b.id)
        _append(db, b, "RECEIVE", event_id=event_id, bal=balance(db, b.id), user=actor, d_usable=qty,
                to_location_id=loc.id, remarks="Received")
        audit.record(db, actor, "RECEIVE", "batch", b.id,
                     after={"code": b.batch_code, "design": design.code, "quantity": qty, "lot": b.supplier_lot})
    return b


def _simple(db, actor, perm, batch_id, action, reason, make_deltas, to_location_id=None, need_reason=True):
    permissions.require(actor, perm)
    if need_reason:
        reason = require_reason(reason, f"{action.lower().replace('_', ' ')}")
    b = get_batch(db, batch_id)
    bal = balance(db, b.id)
    deltas = make_deltas(bal)
    after = {k: _q(bal[k] + v) for k, v in (("usable", deltas.get("d_usable", 0)), ("held", deltas.get("d_held", 0)))}
    if after["usable"] < -EPS:
        raise ValidationError(f"Batch {b.batch_code}: that would make the usable balance negative "
                              f"({bal['usable']} {b.uom} usable now)")
    if after["held"] < -EPS:
        raise ValidationError(f"Batch {b.batch_code}: only {bal['held']} {b.uom} is on hold")
    event_id = new_event()
    with transaction(db):
        _append(db, b, action, event_id=event_id, bal=bal, user=actor, remarks=reason,
                to_location_id=to_location_id, **deltas)
        audit.record(db, actor, action, "batch", b.id, before=bal, after=after, reason=reason)
    return b


def hold_quantity(db: Session, actor: User, batch_id: int, quantity, *, reason) -> ConsumableBatch:
    """Quarantine part (or all) of the usable balance: it can no longer be issued."""
    q = _positive(quantity)
    return _simple(db, actor, "batch.hold", batch_id, "HOLD", reason, lambda bal: {"d_usable": -q, "d_held": q})


def release_hold(db: Session, actor: User, batch_id: int, quantity, *, reason) -> ConsumableBatch:
    q = _positive(quantity)
    return _simple(db, actor, "batch.release", batch_id, "RELEASE_HOLD", reason,
                   lambda bal: {"d_usable": q, "d_held": -q})


def adjust(db: Session, actor: User, batch_id: int, delta, *, reason) -> ConsumableBatch:
    """Authorised stock adjustment (e.g. after a physical count). Never below zero; history is kept."""
    try:
        d = _q(delta)
    except (TypeError, ValueError):
        raise ValidationError("Adjustment must be a number")
    if abs(d) <= EPS:
        raise ValidationError("An adjustment of zero changes nothing")
    return _simple(db, actor, "batch.adjust", batch_id, "ADJUST", reason, lambda bal: {"d_usable": d})


def move_batch(db: Session, actor: User, batch_id: int, to_location_id: int, *, remarks=None) -> ConsumableBatch:
    loc = config.get_location(db, to_location_id)
    if not loc.active:
        raise ValidationError("Destination location is inactive")
    return _simple(db, actor, "batch.move", batch_id, "MOVE", remarks, lambda bal: {}, to_location_id=loc.id,
                   need_reason=False)


# ---------------------------------------------------------------- issue / return (called by services.issues)
def check_issuable(b: ConsumableBatch, bal: dict, qty: float, today: date | None = None) -> None:
    today = today or date.today()
    qty = _q(qty)
    if not b.active:
        raise ValidationError(f"Batch {b.batch_code} is inactive")
    if b.expiry_date and b.expiry_date < today:
        raise ValidationError(f"Batch {b.batch_code} expired on {b.expiry_date}; it cannot be issued")
    if bal["usable"] <= EPS and bal["held"] > EPS:
        raise ValidationError(f"Batch {b.batch_code} is on hold ({bal['held']} {b.uom}); it cannot be issued")
    if qty > bal["usable"] + EPS:
        raise ValidationError(f"Batch {b.batch_code}: insufficient balance, {bal['usable']} {b.uom} usable "
                              f"but {qty} {b.uom} needed")


def issue_to_wo(db: Session, actor: User, batch_id: int, quantity: float, *, issue_id: int, event_id: str,
                remarks=None) -> BatchLedger:
    """Deduct from usable and book as outstanding against the issue (caller owns the transaction)."""
    b = get_batch(db, batch_id)
    q = _positive(quantity)
    bal = balance(db, b.id)
    check_issuable(b, bal, q)
    return _append(db, b, "ISSUE", event_id=event_id, bal=bal, user=actor, d_usable=-q, d_outstanding=q,
                   issue_id=issue_id, remarks=remarks)


def line_state(db: Session, issue_id: int, batch_id: int) -> dict | None:
    """{state: ISSUED|RETURNED, quantity, unused, consumed, since, by} from the latest un-reversed row."""
    r = db.execute(text(
        "SELECT l.action, l.d_outstanding, l.d_usable, l.d_consumed, l.txn_time, l.user_id FROM batch_ledger l "
        "WHERE l.batch_id = :b AND l.issue_id = :i AND l.action IN ('ISSUE', 'RETURN') AND NOT EXISTS "
        "(SELECT 1 FROM batch_ledger r WHERE r.action = 'REVERSAL' AND r.ref_txn_id = l.txn_id) "
        "ORDER BY l.txn_id DESC LIMIT 1"), {"b": batch_id, "i": issue_id}).first()
    if r is None:
        return None
    if r[0] == "ISSUE":
        return {"state": "ISSUED", "quantity": _q(r[1]), "unused": None, "consumed": None, "since": r[4], "by": r[5]}
    ib = db.query(IssueBatch).filter_by(issue_id=issue_id, batch_id=batch_id).one()
    return {"state": "RETURNED", "quantity": ib.quantity, "unused": _q(r[2]), "consumed": _q(r[3]), "since": r[4], "by": r[5]}


def settle_return(db: Session, actor: User, batch_id: int, unused, *, issue_id: int, event_id: str) -> BatchLedger:
    """Record the verified unused quantity coming back; everything else is consumed. Once, per issue line."""
    b = get_batch(db, batch_id)
    ib = db.query(IssueBatch).filter_by(issue_id=issue_id, batch_id=batch_id).first()
    st = line_state(db, issue_id, batch_id) if ib else None
    if ib is None or st is None or st["state"] != "ISSUED":
        raise ValidationError(f"Batch {b.batch_code} is not outstanding on this issue (already settled, or never issued)")
    try:
        u = _q(unused)
    except (TypeError, ValueError):
        raise ValidationError("Unused quantity must be a number")
    if u < -EPS or u > ib.quantity + EPS:
        raise ValidationError(f"Batch {b.batch_code}: unused quantity must be between 0 and the {ib.quantity} {b.uom} issued")
    u = max(u, 0.0)
    return _append(db, b, "RETURN", event_id=event_id, bal=balance(db, b.id), user=actor, d_usable=u,
                   d_outstanding=-ib.quantity, d_consumed=_q(ib.quantity - u), issue_id=issue_id,
                   remarks=f"Returned {u} {b.uom} unused; {_q(ib.quantity - u)} {b.uom} consumed")


def outstanding_issue_ids(db: Session) -> list[int]:
    return [r[0] for r in db.execute(text(
        "SELECT DISTINCT l.issue_id FROM batch_ledger l WHERE l.issue_id IS NOT NULL AND l.action = 'ISSUE' "
        "AND NOT EXISTS (SELECT 1 FROM batch_ledger r WHERE r.action = 'REVERSAL' AND r.ref_txn_id = l.txn_id) "
        "AND NOT EXISTS (SELECT 1 FROM batch_ledger n WHERE n.batch_id = l.batch_id AND n.issue_id = l.issue_id "
        "AND n.action = 'RETURN' AND n.txn_id > l.txn_id AND NOT EXISTS "
        "(SELECT 1 FROM batch_ledger r2 WHERE r2.action = 'REVERSAL' AND r2.ref_txn_id = n.txn_id))")).all()]


# ---------------------------------------------------------------- availability (readiness warnings, issue picks)
def available_for_designs(db: Session, design_ids, today: date | None = None) -> list[dict]:
    """Issuable batches of the given designs, earliest expiry first (then oldest)."""
    today = today or date.today()
    out = []
    for b in db.query(ConsumableBatch).filter(ConsumableBatch.design_id.in_(list(design_ids) or [0]),
                                              ConsumableBatch.active.is_(True)).order_by(ConsumableBatch.id):
        bal = balance(db, b.id)
        if bal["usable"] > EPS and not (b.expiry_date and b.expiry_date < today):
            out.append({"id": b.id, "code": b.batch_code, "design_id": b.design_id, "usable": bal["usable"],
                        "uom": b.uom, "expiry_date": b.expiry_date, "supplier_lot": b.supplier_lot})
    return sorted(out, key=lambda x: (x["expiry_date"] or date.max, x["id"]))


# ---------------------------------------------------------------- reversal
def _perm_for_row(row: BatchLedger) -> str:
    return {"RECEIVE": "batch.receive", "MOVE": "batch.move", "HOLD": "batch.hold", "RELEASE_HOLD": "batch.release",
            "ADJUST": "batch.adjust", "ISSUE": "issue.create", "RETURN": "return.receive"}[row.action]


def pending_reversal_rows(db: Session, event_id: str) -> list[BatchLedger]:
    done = db.query(BatchLedger.ref_txn_id).filter(BatchLedger.action == "REVERSAL", BatchLedger.ref_txn_id.isnot(None))
    return db.query(BatchLedger).filter(BatchLedger.event_id == event_id, BatchLedger.action != "REVERSAL",
                                        ~BatchLedger.txn_id.in_(done)).order_by(BatchLedger.txn_id).all()


def reverse_rows(db: Session, actor: User, rows: list[BatchLedger], *, new_event_id: str, reason: str) -> list[BatchLedger]:
    """Validate then append REVERSAL rows (caller owns the transaction). Nothing is written if any row
    cannot be reversed without breaking a balance or an issue settlement."""
    out, seq_used, running = [], {}, {}
    for r in rows:
        permissions.require(actor, _perm_for_row(r))
        b = get_batch(db, r.batch_id)
        if r.action == "RECEIVE" and db.query(BatchLedger).filter(
                BatchLedger.batch_id == r.batch_id, BatchLedger.txn_id != r.txn_id,
                BatchLedger.action != "REVERSAL").count():
            raise ValidationError(f"Batch {b.batch_code} already has other activity; its receipt cannot be reversed")
        if r.action == "ISSUE":
            later = db.execute(text(
                "SELECT 1 FROM batch_ledger n WHERE n.batch_id = :b AND n.issue_id = :i AND n.action = 'RETURN' "
                "AND n.txn_id > :t AND NOT EXISTS (SELECT 1 FROM batch_ledger x WHERE x.action = 'REVERSAL' "
                "AND x.ref_txn_id = n.txn_id)"), {"b": r.batch_id, "i": r.issue_id, "t": r.txn_id}).first()
            if later:
                raise ValidationError(f"Batch {b.batch_code}: the issue was already settled by a return; reverse "
                                      f"the return first. Nothing was reversed")
        bal = running.get(r.batch_id) or balance(db, r.batch_id)
        new = {"usable": _q(bal["usable"] - r.d_usable), "held": _q(bal["held"] - r.d_held),
               "outstanding": _q(bal["outstanding"] - r.d_outstanding)}
        for k, v in new.items():
            if v < -EPS:
                raise ValidationError(f"Batch {b.batch_code}: reversing entry {r.txn_id} would make the {k} balance "
                                      f"negative (the material has since been used or moved on). Nothing was reversed")
        running[r.batch_id] = {**bal, **new, "last_seq": bal["last_seq"] + 1}
        off = seq_used.get(r.batch_id, 0)
        seq_used[r.batch_id] = off + 1
        row = BatchLedger(batch_id=r.batch_id, action="REVERSAL", event_id=new_event_id, user_id=actor.id,
                          remarks=reason, ref_txn_id=r.txn_id, issue_id=r.issue_id,
                          batch_seq=balance(db, r.batch_id)["last_seq"] + 1 + off,
                          d_usable=-r.d_usable, d_held=-r.d_held, d_outstanding=-r.d_outstanding,
                          d_consumed=-r.d_consumed, to_location_id=None)
        db.add(row)
        out.append(row)
        audit.record(db, actor, "REVERSE", "batch_ledger", r.txn_id, reason=reason,
                     after={"event_id": new_event_id, "batch": b.batch_code})
    return out


def reverse(db: Session, actor: User, txn_id: int, *, reason) -> list[BatchLedger]:
    """Reverse a batch-only event (hold, adjustment, move, receipt). Events that also moved items must be
    reversed from the item side so both ledgers undo together."""
    reason = require_reason(reason, "a reversal")
    target = db.get(BatchLedger, txn_id)
    if target is None:
        raise NotFoundError(f"Batch ledger entry {txn_id} not found")
    if target.action == "REVERSAL":
        raise ValidationError("A REVERSAL cannot itself be reversed; make a new entry instead")
    item_txn = db.execute(text("SELECT MIN(txn_id) FROM ledger WHERE event_id = :e"), {"e": target.event_id}).scalar()
    if item_txn:
        raise ValidationError(f"This event also moved items; reverse it from the item ledger entry {item_txn}")
    with transaction(db):
        rows = pending_reversal_rows(db, target.event_id)
        if txn_id not in {r.txn_id for r in rows}:
            raise ValidationError(f"Entry {txn_id} is already reversed")
        return reverse_rows(db, actor, rows, new_event_id=new_event(), reason=reason)


# ---------------------------------------------------------------- views
def batch_dict(db: Session, b: ConsumableBatch, *, detail=False) -> dict:
    bal = balance(db, b.id)
    design = db.get(ElementDesign, b.design_id)
    loc = location_id(db, b.id)
    locs = {l["id"]: l["path"] for l in config.list_locations(db)} if detail or loc else {}
    d = {"id": b.id, "code": b.batch_code, "design_id": design.id, "design_code": design.code,
         "description": design.description, "uom": b.uom, "supplier_lot": b.supplier_lot,
         "received_qty": b.received_qty, "expiry_date": b.expiry_date, "usable": bal["usable"], "held": bal["held"],
         "outstanding": bal["outstanding"], "consumed": bal["consumed"], "status": status_of(b, bal),
         "location": locs.get(loc), "location_id": loc, "active": b.active}
    if detail:
        users = {u.id: u.username for u in db.query(User.id, User.username)}
        d["history"] = [{"txn_id": r.txn_id, "txn_time": r.txn_time, "action": r.action, "d_usable": r.d_usable,
                         "d_held": r.d_held, "d_outstanding": r.d_outstanding, "d_consumed": r.d_consumed,
                         "location": locs.get(r.to_location_id), "issue_id": r.issue_id, "user": users.get(r.user_id),
                         "remarks": r.remarks, "ref_txn_id": r.ref_txn_id, "event_id": r.event_id}
                        for r in db.query(BatchLedger).filter(BatchLedger.batch_id == b.id).order_by(BatchLedger.txn_id.desc())]
    return d


def list_batches(db: Session, *, q=None, design_id=None, status=None, limit=100, offset=0) -> tuple[int, list[dict]]:
    qry = db.query(ConsumableBatch).join(ElementDesign, ElementDesign.id == ConsumableBatch.design_id)
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(ConsumableBatch.batch_code.ilike(like) | ConsumableBatch.supplier_lot.ilike(like)
                         | ElementDesign.code.ilike(like))
    if design_id:
        qry = qry.filter(ConsumableBatch.design_id == design_id)
    rows = [batch_dict(db, b) for b in qry.order_by(ConsumableBatch.id.desc())]
    if status:
        rows = [r for r in rows if r["status"] == status]
    total = len(rows)
    return total, rows[offset: offset + min(limit, 500)]
