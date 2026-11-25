"""Location-based physical stock counts and the authorised adjustments they can lead to.

A count only OBSERVES: scan items (QR, typed lookup, or a build label for everything fitted in it) and enter the
quantity found for each consumable batch. The report lists what was expected, found, unexpected and missing. Nothing
changes in the ledger until an authorised person applies adjustments, each with a reason and an audit record:
  * MARK_MISSING: custody becomes MISSING (condition and last known location are kept). A missing reusable item can
    never satisfy readiness or be issued or fitted.
  * RECOVER: a missing item was found; it goes back to stores at the counted location, AWAITING INSPECTION.
  * MOVE_HERE: an item found away from where the ledger has it is corrected to the counted location.
  * batch adjustment: usable stock is corrected to the counted quantity (history kept, never negative)."""
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import ConsumableBatch, Item, StockCount, StockCountScan, User, _now
from . import audit, batches, builds, config, ledger, permissions, recheck, scan as scanner
from .common import clean, new_event, require_reason, transaction
from .errors import NotFoundError, ValidationError

PHYSICALLY_HERE = ("STORES", "ASSEMBLY_AREA", "REPAIR")  # custodies where an item is expected at its recorded location


def _get(db: Session, count_id: int) -> StockCount:
    c = db.get(StockCount, count_id)
    if c is None:
        raise NotFoundError(f"Stock count {count_id} not found")
    return c


def start_count(db: Session, actor: User, location_id: int, note=None) -> StockCount:
    permissions.require(actor, "count.perform")
    loc = config.get_location(db, location_id)
    if not loc.active:
        raise ValidationError("Location is inactive")
    with transaction(db):
        c = StockCount(location_id=loc.id, started_by=actor.id, note=clean(note))
        db.add(c)
        db.flush()
        audit.record(db, actor, "START_COUNT", "stock_count", c.id, after={"location": config.location_path(db, loc.id)})
    return c


def scan(db: Session, actor: User, count_id: int, code: str, quantity=None) -> dict:
    """Record one scan. Items: once each (a repeat scan is harmless). Batches: the quantity found is required."""
    permissions.require(actor, "count.perform")
    c = _get(db, count_id)
    if c.status != "OPEN":
        raise ValidationError(f"Count {count_id} is {c.status}; no more scans can be added")
    r = scanner.resolve(db, code)
    scanned: list[tuple[str, int]] = []
    if r["type"] == "ITEM":
        if len(r["items"]) != 1:
            raise ValidationError("Several items match that code; scan the QR label or use a unique display ID")
        scanned = [("ITEM", r["items"][0]["id"])]
    elif r["type"] == "BUILD":  # a build label counts everything fitted in that build
        scanned = [("ITEM", m.item_id) for m in builds.members(db, r["build"]["id"])]
        if not scanned:
            raise ValidationError("That build has no components to count")
    elif r["type"] == "BATCH":
        if quantity in (None, ""):
            raise ValidationError("Enter the quantity found for this batch (0 if none)")
        try:
            q = float(quantity)
        except (TypeError, ValueError):
            raise ValidationError("Quantity must be a number")
        if q < 0:
            raise ValidationError("Quantity cannot be negative")
        scanned = [("BATCH", r["batch"]["id"])]
    else:
        raise ValidationError(f"Nothing found for {code!r}, or it is not something that can be counted")
    added = 0
    with transaction(db):
        for kind, ident in scanned:
            if kind == "ITEM":
                if db.query(StockCountScan).filter_by(count_id=c.id, item_id=ident).first():
                    continue
                db.add(StockCountScan(count_id=c.id, item_id=ident, scanned_by=actor.id))
            else:
                row = db.query(StockCountScan).filter_by(count_id=c.id, batch_id=ident).first()
                if row:
                    row.quantity, row.scanned_by, row.scanned_at = q, actor.id, _now()  # a recount replaces the figure
                else:
                    db.add(StockCountScan(count_id=c.id, batch_id=ident, quantity=q, scanned_by=actor.id))
            added += 1
    return {"count_id": c.id, "recorded": added, "type": r["type"]}


def _live_report(db: Session, c: StockCount) -> dict:
    sub = config.subtree_ids(db, c.location_id)
    marks = ",".join(str(i) for i in sub)
    locs = {l["id"]: l["path"] for l in config.list_locations(db)}
    expected = {r["id"]: dict(r) for r in db.execute(text(
        f"SELECT i.id, i.display_id, i.system_id, d.code AS design_code, s.item_condition AS condition, "
        f"s.custody, s.location_id FROM items i JOIN item_state s ON s.item_id = i.id "
        f"JOIN element_designs d ON d.id = i.design_id WHERE i.active = 1 AND s.location_id IN ({marks}) "
        f"AND s.custody IN ('STORES', 'ASSEMBLY_AREA', 'REPAIR')")).mappings()}
    scans = db.query(StockCountScan).filter_by(count_id=c.id).all()
    scanned_items = {s.item_id for s in scans if s.item_id}
    found, unexpected = [], []
    for iid in sorted(scanned_items):
        if iid in expected:
            found.append(expected[iid])
            continue
        it = db.get(Item, iid)
        st = ledger.get_state(db, iid)
        if not it.active:
            why = "RETIRED"
        elif st["custody"] == "MISSING":
            why = "MARKED_MISSING"
        elif st["custody"] in ("PENDING_HANDOVER", "PRODUCTION"):
            why = "WITH_PRODUCTION"
        else:
            why = "EXPECTED_ELSEWHERE"
        unexpected.append({"id": iid, "display_id": it.display_id, "reason": why, "custody": st["custody"],
                           "recorded_location": locs.get(st["location_id"])})
    missing = [{**v, "location": locs.get(v["location_id"])} for k, v in expected.items() if k not in scanned_items]
    batch_rows = []
    counted = {s.batch_id: s.quantity for s in scans if s.batch_id}
    seen = set()
    for b in db.query(ConsumableBatch).filter(ConsumableBatch.active.is_(True)).order_by(ConsumableBatch.id):
        bal = batches.balance(db, b.id)
        loc = batches.location_id(db, b.id)
        here = loc in sub and (bal["usable"] + bal["held"]) > batches.EPS
        if not here and b.id not in counted:
            continue
        exp = round(bal["usable"] + bal["held"], 6)
        got = counted.get(b.id)
        seen.add(b.id)
        batch_rows.append({"batch_id": b.id, "code": b.batch_code, "uom": b.uom, "expected": exp, "counted": got,
                           "variance": None if got is None else round(got - exp, 6),
                           "recorded_location": locs.get(loc), "usable": bal["usable"], "held": bal["held"],
                           "state": ("NOT_COUNTED" if got is None else "OK" if abs(got - exp) <= batches.EPS
                                     else "SHORT" if got < exp else "OVER")})
    return {"count_id": c.id, "location": config.location_path(db, c.location_id), "status": c.status,
            "expected": len(expected), "found": found, "missing": missing, "unexpected": unexpected, "batches": batch_rows,
            "summary": {"expected": len(expected), "found": len(found), "missing": len(missing),
                        "unexpected": len(unexpected), "batch_variances": sum(1 for b in batch_rows if b["state"] in ("SHORT", "OVER", "NOT_COUNTED"))}}


def report(db: Session, count_id: int) -> dict:
    c = _get(db, count_id)
    if c.status == "COMPLETED" and c.result:
        return {**json.loads(c.result), "status": c.status}  # the figures as they stood when the count was closed
    return _live_report(db, c)


def complete(db: Session, actor: User, count_id: int, note=None) -> dict:
    permissions.require(actor, "count.perform")
    c = _get(db, count_id)
    if c.status != "OPEN":
        raise ValidationError(f"Count {count_id} is already {c.status}")
    snap = _live_report(db, c)
    with transaction(db):
        c.status, c.completed_by, c.completed_at = "COMPLETED", actor.id, _now()
        c.result = json.dumps({**snap, "status": "COMPLETED"}, default=str)
        if clean(note):
            c.note = clean(note)
        audit.record(db, actor, "COMPLETE_COUNT", "stock_count", c.id, after=snap["summary"])
    return report(db, count_id)


def cancel(db: Session, actor: User, count_id: int, *, reason) -> StockCount:
    permissions.require(actor, "count.perform")
    reason = require_reason(reason, "cancelling a count")
    c = _get(db, count_id)
    if c.status != "OPEN":
        raise ValidationError(f"Count {count_id} is already {c.status}")
    with transaction(db):
        c.status = "CANCELLED"
        audit.record(db, actor, "CANCEL_COUNT", "stock_count", c.id, reason=reason)
    return c


def apply(db: Session, actor: User, count_id: int, item_actions: list[dict] | None = None,
          batch_adjustments: list[dict] | None = None) -> dict:
    """Authorised adjustments after a COMPLETED count. Each needs a reason; everything is audited."""
    permissions.require(actor, "stock.adjust")
    c = _get(db, count_id)
    if c.status != "COMPLETED":
        raise ValidationError("Complete the count before adjusting anything")
    rep = report(db, count_id)
    sub = set(config.subtree_ids(db, c.location_id))
    missing = {m["id"] for m in rep["missing"]}
    unexp = {u["id"]: u for u in rep["unexpected"]}
    brows = {b["batch_id"]: b for b in rep["batches"]}
    item_actions, batch_adjustments = list(item_actions or []), list(batch_adjustments or [])
    if not item_actions and not batch_adjustments:
        raise ValidationError("Nothing to adjust")
    plan = []
    for a in item_actions:
        iid, act = int(a["item_id"]), (a.get("action") or "").upper()
        reason = require_reason(a.get("reason"), f"{act or 'an adjustment'} on item {iid}")
        item = db.get(Item, iid)
        if item is None:
            raise NotFoundError(f"Item {iid} not found")
        s = ledger.get_state(db, iid)
        loc_id = int(a.get("location_id") or c.location_id)
        if loc_id not in sub:
            raise ValidationError("The location must be inside the counted location")
        if act == "MARK_MISSING":
            if iid not in missing:
                raise ValidationError(f"{item.display_id} was found or is not part of this count's missing list")
            if s["custody"] not in PHYSICALLY_HERE:  # the picture changed since the count (or it was already adjusted)
                raise ValidationError(f"{item.display_id} is now in {s['custody']} custody; recount before adjusting")
        elif act == "RECOVER":
            if iid not in unexp or unexp[iid]["reason"] != "MARKED_MISSING":
                raise ValidationError(f"{item.display_id} was not found as a missing item in this count")
            if s["custody"] != "MISSING":
                raise ValidationError(f"{item.display_id} is no longer marked missing (already recovered?)")
        elif act == "MOVE_HERE":
            if iid not in unexp or unexp[iid]["reason"] != "EXPECTED_ELSEWHERE":
                raise ValidationError(f"{item.display_id} was not found away from its recorded location in this count")
            if s["location_id"] == loc_id:
                raise ValidationError(f"{item.display_id} is already recorded at that location")
            if s["custody"] != "STORES":
                b = ledger.active_build_id(db, iid)
                raise ValidationError(f"{item.display_id} is in {s['custody']} custody"
                                      + (f" as part of build BLD-{b:06d}; move the whole build" if b else ""))
        else:
            raise ValidationError("action must be MARK_MISSING, RECOVER or MOVE_HERE")
        plan.append((item, s, act, reason, loc_id))
    bplan = []
    for a in batch_adjustments:
        row = brows.get(int(a["batch_id"]))
        reason = require_reason(a.get("reason"), "a batch adjustment")
        if row is None or row["counted"] is None:
            raise ValidationError(f"Batch {a['batch_id']} was not counted here; enter its quantity in the count first")
        if abs(row["variance"]) <= batches.EPS:
            raise ValidationError(f"{row['code']} has no variance to adjust")
        now_bal = batches.balance(db, int(a["batch_id"]))
        if abs(now_bal["usable"] + now_bal["held"] - row["expected"]) > batches.EPS:
            raise ValidationError(f"{row['code']} has changed since the count (it was {row['expected']} {row['uom']}, it is now "
                                  f"{round(now_bal['usable'] + now_bal['held'], 6)}): count it again before adjusting")
        if row["usable"] + row["variance"] < -batches.EPS:
            raise ValidationError(f"{row['code']}: correcting to {row['counted']} would make usable stock negative "
                                  f"(release or issue-settle held/outstanding quantities first)")
        bplan.append((int(a["batch_id"]), row["variance"], reason))

    event_id = new_event()
    done = {"items": [], "batches": []}
    with transaction(db):
        for item, s, act, reason, loc_id in plan:
            if act == "MARK_MISSING":
                ledger._append(db, item.id, "MARK_MISSING", event_id=event_id, prev=s, user=actor, source="COUNT",
                               to_condition=s["item_condition"], to_custody="MISSING", to_location_id=s["location_id"],
                               remarks=f"Missing at count #{c.id}: {reason}")
                recheck.flag(db, actor, f"{item.display_id} was found missing in a stock count", item_ids=[item.id])
            elif act == "RECOVER":
                ledger._append(db, item.id, "RECOVER", event_id=event_id, prev=s, user=actor, source="COUNT",
                               to_condition="AWAITING_INSPECTION", to_custody="STORES", to_location_id=loc_id,
                               remarks=f"Recovered at count #{c.id}: {reason}")
            else:
                ledger._append(db, item.id, "MOVE", event_id=event_id, prev=s, user=actor, source="COUNT",
                               to_condition=s["item_condition"], to_custody=s["custody"], to_location_id=loc_id,
                               remarks=f"Location corrected by count #{c.id}: {reason}")
            audit.record(db, actor, f"COUNT_{act}", "item", item.id, after={"count": c.id}, reason=reason)
            done["items"].append({"item": item.display_id, "action": act})
    for batch_id, delta, reason in bplan:  # each is validated above; history is kept by the batch ledger
        batches.adjust(db, actor, batch_id, delta, reason=f"Stock count #{c.id}: {reason}")
        done["batches"].append({"batch": batches.batch_code(batch_id), "delta": delta})
    return done


def count_detail(db: Session, count_id: int) -> dict:
    c = _get(db, count_id)
    users = {u.id: u.username for u in db.query(User.id, User.username)}
    return {"id": c.id, "location": config.location_path(db, c.location_id), "location_id": c.location_id,
            "status": c.status, "started_by": users.get(c.started_by), "started_at": c.started_at,
            "completed_by": users.get(c.completed_by), "completed_at": c.completed_at, "note": c.note,
            "report": report(db, count_id)}


def open_count(db: Session) -> int:
    return db.query(StockCount).filter(StockCount.status == "OPEN").count()


def list_counts(db: Session, limit=50) -> list[dict]:
    users = {u.id: u.username for u in db.query(User.id, User.username)}
    return [{"id": c.id, "location": config.location_path(db, c.location_id), "status": c.status,
             "started_by": users.get(c.started_by), "started_at": c.started_at, "completed_at": c.completed_at}
            for c in db.query(StockCount).order_by(StockCount.id.desc()).limit(limit)]
