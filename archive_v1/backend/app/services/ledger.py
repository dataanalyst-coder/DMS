"""All business logic for the DIE management ledger.

Routers and the frontend must stay thin and call into this module. Status and location are never
stored on items: they come from the `item_status` view (latest effective ledger entry).
"""
import re
import uuid
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import ITEM_TYPES, Assembly, Item, Ledger


# ---------------------------------------------------------------- errors
class LedgerError(Exception):
    """Base class; `message` is safe to show to users."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFoundError(LedgerError):
    pass


class ValidationError(LedgerError):
    pass


class BlockedError(LedgerError):
    """An item cannot be issued/moved. Names the blocking item, its WO and batch."""

    def __init__(self, blockers: list[dict]):
        self.blockers = blockers
        b = blockers[0]
        self.item_id, self.status = b["item_id"], b["status"]
        self.wo_id, self.batch_no = b.get("wo_id"), b.get("batch_no")
        super().__init__(
            f"Item {b['item_id']} is {b['status']}"
            f" (WO {b.get('wo_id') or '-'}, batch {b.get('batch_no') or '-'})"
            f" at {b.get('current_location') or '-'}; nothing was written."
        )


# ---------------------------------------------------------------- normalisation
_SEP_PADDING = re.compile(r"([-_/.])0+(\d)")


def normalize_id(raw) -> str:
    """trim, uppercase, strip zero-padding in suffixes: ' b32-01 ' -> 'B32-1'."""
    if raw is None or not str(raw).strip():
        raise ValidationError("ID must not be empty")
    s = re.sub(r"\s+", "", str(raw)).upper()
    return _SEP_PADDING.sub(r"\1\2", s)


def _norm_opt(raw):
    return normalize_id(raw) if raw is not None and str(raw).strip() else None


def _clean(v):
    if isinstance(v, str):
        return v.strip() or None
    return v


@contextmanager
def _transaction(db: Session):
    """One DB transaction: commit on success, roll back everything on any error."""
    try:
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------- reads
def get_status(db: Session, item_id) -> dict | None:
    row = db.execute(
        text("SELECT * FROM item_status WHERE item_id = :i"), {"i": normalize_id(item_id)}
    ).mappings().first()
    return dict(row) if row else None


def _require_item(db: Session, item_id) -> Item:
    item = db.get(Item, normalize_id(item_id))
    if item is None:
        raise NotFoundError(f"Item {normalize_id(item_id)} not found")
    return item


def assembly_item_ids(a: Assembly) -> list[str]:
    """Die, pattern, then whichever flanges exist (flanges are optional)."""
    return [i for i in (a.die_id, a.pattern_id, a.top_flange_id, a.bottom_flange_id) if i]


def get_assembly_view(db: Session, assembly_id) -> dict:
    a = db.get(Assembly, normalize_id(assembly_id))
    if a is None:
        raise NotFoundError(f"Assembly {normalize_id(assembly_id)} not found")
    items = [get_status(db, i) for i in assembly_item_ids(a)]
    return {
        "assembly_id": a.assembly_id,
        "items": items,
        "ready_to_issue": bool(a.active) and all(s and s["status"] == "AVAILABLE" for s in items),
    }


def open_issues(db: Session) -> list[dict]:
    """Issues not yet returned, one row per group (for the Return form)."""
    rows = db.execute(text(
        "SELECT group_id, assembly_id, wo_id, batch_no, issued_to, MIN(since) AS since,"
        " GROUP_CONCAT(item_id) AS item_ids FROM item_status"
        " WHERE status = 'IN_USE' GROUP BY group_id, assembly_id, wo_id, batch_no, issued_to"
        " ORDER BY since"
    )).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- masters
def create_item(db: Session, item_id, item_type, *, home_location=None, received_by=None,
                **fields) -> Item:
    """Create an item and its RECEIVE ledger entry at home_location (so it has a status)."""
    with _transaction(db):
        return _add_item(db, item_id, item_type, event_id=_new_event(),
                         home_location=home_location, received_by=received_by, **fields)


def _add_item(db: Session, item_id, item_type, *, event_id, home_location=None, received_by=None,
              remarks="Initial receipt", **fields) -> Item:
    """Validate and stage an item + its RECEIVE entry; the caller owns the transaction."""
    iid = normalize_id(item_id)
    item_type = (item_type or "").strip().upper()
    if item_type not in ITEM_TYPES:
        raise ValidationError(f"Invalid item_type {item_type!r}; expected one of {ITEM_TYPES}")
    if db.get(Item, iid) is not None:
        raise ValidationError(f"Item {iid} already exists")
    home = _clean(home_location)
    item = Item(item_id=iid, item_type=item_type, home_location=home, **fields)
    db.add(item)
    db.flush()
    _append(db, iid, "RECEIVE", event_id=event_id, to_location=home, issued_by=received_by, remarks=remarks)
    return item


_ASSEMBLY_SLOTS = (
    ("die_id", "DIE", True),
    ("pattern_id", "PATTERN", True),
    ("top_flange_id", "TOP_FLANGE", False),
    ("bottom_flange_id", "BOTTOM_FLANGE", False),
)


def _check_ref(db: Session, slot: str, raw, want: str, required: bool):
    """-> (normalised_ref_or_None, error_message_or_None) for one assembly slot."""
    ref = _norm_opt(raw)
    if ref is None:
        return None, (f"{slot} is required" if required else None)
    item = db.get(Item, ref)
    if item is None:
        return ref, f"{slot}: unknown item {ref}"
    if item.item_type != want:
        return ref, f"{slot}: {ref} is a {item.item_type}, expected {want}"
    return ref, None


def _validate_assembly_refs(db: Session, refs: dict) -> dict:
    out = {}
    for slot, want, required in _ASSEMBLY_SLOTS:
        ref, err = _check_ref(db, slot, refs.get(slot), want, required)
        if err:
            raise ValidationError(err)
        out[slot] = ref
    return out


def create_assembly(db: Session, assembly_id, **fields) -> Assembly:
    with _transaction(db):
        return _add_assembly(db, assembly_id, **fields)


def _add_assembly(db: Session, assembly_id, **fields) -> Assembly:
    aid = normalize_id(assembly_id)
    if db.get(Assembly, aid) is not None:
        raise ValidationError(f"Assembly {aid} already exists")
    refs = _validate_assembly_refs(db, fields)
    rest = {k: v for k, v in fields.items() if k not in refs}
    a = Assembly(assembly_id=aid, **refs, **rest)
    db.add(a)
    return a


# ---------------------------------------------------------------- ledger writes
def _new_event() -> str:
    """One event id per API call; every ledger row written by that call shares it."""
    return uuid.uuid4().hex


def _append(db: Session, item_id: str, action: str, *, event_id: str, **kw) -> Ledger:
    row = Ledger(item_id=item_id, action=action, event_id=event_id, **kw)
    db.add(row)
    return row


def _require_text(**fields):
    missing = [k for k, v in fields.items() if not (v and str(v).strip())]
    if missing:
        raise ValidationError("Required: " + ", ".join(missing))


def _resolve_items(db: Session, item_ids) -> list[str]:
    ids = list(dict.fromkeys(normalize_id(i) for i in (item_ids or [])))
    if not ids:
        raise ValidationError("No items given")
    for i in ids:
        _require_item(db, i)
    return ids


def _check_all(db: Session, ids: list[str], allowed: set[str]) -> dict[str, dict]:
    """Return statuses; raise BlockedError (listing every offender) unless all are in `allowed`."""
    statuses = {i: get_status(db, i) for i in ids}
    blockers = [
        s if s else {"item_id": i, "status": "NO_LEDGER_ENTRY"}
        for i, s in statuses.items()
        if not s or s["status"] not in allowed
    ]
    if blockers:
        raise BlockedError(blockers)
    return statuses


def issue(db: Session, *, wo_id, batch_no, issued_by, issued_to, assembly_id=None,
          item_ids=None, machine=None, remarks=None) -> dict:
    """Atomic issue: every item is issued together or none are."""
    _require_text(wo_id=wo_id, batch_no=batch_no, issued_by=issued_by, issued_to=issued_to)
    if bool(assembly_id) == bool(item_ids):
        raise ValidationError("Give either assembly_id or item_ids[], not both/neither")
    aid = None
    if assembly_id:
        a = db.get(Assembly, normalize_id(assembly_id))
        if a is None:
            raise NotFoundError(f"Assembly {normalize_id(assembly_id)} not found")
        if not a.active:
            raise ValidationError(f"Assembly {a.assembly_id} is inactive")
        aid, ids = a.assembly_id, assembly_item_ids(a)
    else:
        ids = _resolve_items(db, item_ids)

    group_id, event_id = uuid.uuid4().hex, _new_event()
    with _transaction(db):
        statuses = _check_all(db, ids, {"AVAILABLE"})
        for i in ids:
            _append(db, i, "ISSUE", event_id=event_id, assembly_id=aid, group_id=group_id,
                    from_location=statuses[i]["current_location"],
                    to_location=_clean(machine) or issued_to.strip(),
                    wo_id=wo_id.strip(), batch_no=batch_no.strip(),
                    issued_by=issued_by.strip(), issued_to=issued_to.strip(),
                    machine=_clean(machine), remarks=remarks)
    return {"group_id": group_id, "event_id": event_id, "assembly_id": aid, "item_ids": ids}


def return_items(db: Session, *, to_location, group_id=None, item_ids=None,
                 condition_remarks=None, returned_by=None) -> dict:
    """Return issued items; they become AVAILABLE at to_location."""
    _require_text(to_location=to_location)
    if bool(group_id) == bool(item_ids):
        raise ValidationError("Give either group_id or item_ids[], not both/neither")
    if group_id:
        ids = [r[0] for r in db.execute(
            text("SELECT item_id FROM item_status WHERE group_id = :g AND status = 'IN_USE'"
                 " ORDER BY item_id"), {"g": group_id})]
        if not ids:
            raise NotFoundError(f"No items currently issued under group {group_id}")
    else:
        ids = _resolve_items(db, item_ids)

    event_id = _new_event()
    with _transaction(db):
        statuses = _check_all(db, ids, {"IN_USE"})
        for i in ids:
            s = statuses[i]
            _append(db, i, "RETURN", event_id=event_id, assembly_id=s["assembly_id"], group_id=s["group_id"],
                    from_location=s["current_location"], to_location=to_location.strip(),
                    wo_id=s["wo_id"], batch_no=s["batch_no"], issued_by=returned_by,
                    issued_to=s["issued_to"], remarks=condition_remarks)
    return {"item_ids": ids, "to_location": to_location.strip(), "event_id": event_id}


def _simple_move(db, item_ids, action, allowed, to_location, remarks, by):
    ids = _resolve_items(db, item_ids)
    event_id = _new_event()
    with _transaction(db):
        statuses = _check_all(db, ids, allowed)
        for i in ids:
            _append(db, i, action, event_id=event_id, from_location=statuses[i]["current_location"],
                    to_location=to_location, issued_by=by, remarks=remarks)
    return {"item_ids": ids, "event_id": event_id}


def transfer(db: Session, *, item_ids, to_location, by=None, remarks=None) -> dict:
    _require_text(to_location=to_location)
    return _simple_move(db, item_ids, "TRANSFER", {"AVAILABLE"}, to_location.strip(), remarks, by)


def send_repair(db: Session, *, item_ids, to_location="REPAIR", by=None, remarks=None) -> dict:
    return _simple_move(db, item_ids, "SEND_REPAIR", {"AVAILABLE"}, to_location, remarks, by)


def back_from_repair(db: Session, *, item_ids, to_location, by=None, remarks=None) -> dict:
    _require_text(to_location=to_location)
    return _simple_move(db, item_ids, "BACK_FROM_REPAIR", {"UNDER_REPAIR"},
                        to_location.strip(), remarks, by)


def scrap(db: Session, *, item_ids, by=None, remarks=None) -> dict:
    return _simple_move(db, item_ids, "SCRAP", {"AVAILABLE", "UNDER_REPAIR"}, "SCRAPPED",
                        remarks, by)


def reverse(db: Session, txn_id: int, *, by=None, remarks=None) -> list[Ledger]:
    """Append REVERSAL entries for txn_id's event and return them.

    An event is everything written by one API call (an assembly issue, one return, one transfer).
    Every not-yet-reversed row sharing the target's event_id is reversed together, in one
    transaction, under a new event_id of its own. Each item returns to exactly its previous
    status/location, so only an item's current effective entry can be reversed. If any row of
    the event cannot be reversed, nothing is written. group_id (the link back to the original
    issue) is carried onto the reversal rows but plays no part in what gets reversed.
    """
    target = db.get(Ledger, txn_id)
    if target is None:
        raise NotFoundError(f"Ledger entry {txn_id} not found")
    if target.action == "REVERSAL":
        raise ValidationError("A REVERSAL cannot itself be reversed; make a new entry instead")

    with _transaction(db):
        reversed_ids = db.query(Ledger.ref_txn_id).filter(
            Ledger.action == "REVERSAL", Ledger.ref_txn_id.isnot(None))
        targets = db.query(Ledger).filter(
            Ledger.event_id == target.event_id, Ledger.action != "REVERSAL",
            ~Ledger.txn_id.in_(reversed_ids),
        ).order_by(Ledger.txn_id).all()
        if target.txn_id not in {t.txn_id for t in targets}:
            raise ValidationError(f"Entry {txn_id} is already reversed")

        scope = f"event {target.event_id[:8]}: "
        for t in targets:
            cur = get_status(db, t.item_id)
            if cur is None or cur["txn_id"] != t.txn_id:
                raise ValidationError(
                    f"{scope}entry {t.txn_id} is not the latest entry for {t.item_id} "
                    f"(already reversed or superseded); nothing was reversed")
            has_previous = db.execute(text(
                "SELECT 1 FROM ledger l WHERE l.item_id = :i AND l.txn_id < :t"
                " AND l.action <> 'REVERSAL' AND NOT EXISTS"
                " (SELECT 1 FROM ledger r WHERE r.action = 'REVERSAL' AND r.ref_txn_id = l.txn_id)"
                " LIMIT 1"), {"i": t.item_id, "t": t.txn_id}).first()
            if not has_previous:
                raise ValidationError(
                    f"{scope}entry {t.txn_id} is {t.item_id}'s first entry and cannot be "
                    f"reversed; nothing was reversed")

        event_id = _new_event()
        return [
            _append(db, t.item_id, "REVERSAL", event_id=event_id, ref_txn_id=t.txn_id,
                    assembly_id=t.assembly_id, group_id=t.group_id, from_location=t.to_location,
                    to_location=t.from_location, wo_id=t.wo_id, batch_no=t.batch_no,
                    issued_by=by, remarks=remarks or f"Reversal of txn {t.txn_id}")
            for t in targets
        ]


# ---------------------------------------------------------------- master updates / lists
_ITEM_EDITABLE = {"description", "od", "id_dim", "length", "home_location", "remarks", "active"}
_ASSEMBLY_EDITABLE = {"description", "od", "length", "pouring_wt", "gating_wt", "rcd_wt", "active"}


def update_item(db: Session, item_id, **fields) -> Item:
    """Edit master data only. item_type is fixed; status/location can never be edited."""
    item = _require_item(db, item_id)
    bad = set(fields) - _ITEM_EDITABLE
    if bad:
        raise ValidationError(f"Cannot edit: {', '.join(sorted(bad))}")
    with _transaction(db):
        for k, v in fields.items():
            setattr(item, k, _clean(v))
    return item


def update_assembly(db: Session, assembly_id, **fields) -> Assembly:
    """Edit master data and the item mapping (refs are re-validated; flanges may be cleared)."""
    a = db.get(Assembly, normalize_id(assembly_id))
    if a is None:
        raise NotFoundError(f"Assembly {normalize_id(assembly_id)} not found")
    slots = {s for s, _, _ in _ASSEMBLY_SLOTS}
    bad = set(fields) - _ASSEMBLY_EDITABLE - slots
    if bad:
        raise ValidationError(f"Cannot edit: {', '.join(sorted(bad))}")
    refs = _validate_assembly_refs(db, {s: fields.get(s, getattr(a, s)) for s in slots})
    with _transaction(db):
        for k, v in fields.items():
            if k not in slots:
                setattr(a, k, _clean(v))
        for k, v in refs.items():
            setattr(a, k, v)
    return a


def list_items(db: Session, item_type: str | None = None, active: bool | None = None) -> list[dict]:
    sql = ("SELECT i.*, s.status, s.current_location, s.wo_id, s.batch_no, s.issued_to, s.since"
           " FROM items i LEFT JOIN item_status s ON s.item_id = i.item_id WHERE 1=1")
    p: dict = {}
    if item_type:
        sql += " AND i.item_type = :t"
        p["t"] = item_type.strip().upper()
    if active is not None:
        sql += " AND i.active = :a"
        p["a"] = int(active)
    return [dict(r) for r in db.execute(text(sql + " ORDER BY i.item_id"), p).mappings()]


def assemblies_containing(db: Session, item_id) -> list[Assembly]:
    iid = normalize_id(item_id)
    return db.query(Assembly).filter(
        (Assembly.die_id == iid) | (Assembly.pattern_id == iid)
        | (Assembly.top_flange_id == iid) | (Assembly.bottom_flange_id == iid)
    ).order_by(Assembly.assembly_id).all()


def get_item_detail(db: Session, item_id) -> dict:
    item = _require_item(db, item_id)
    return {
        "item": item,
        "status": get_status(db, item.item_id),
        "assemblies": assemblies_containing(db, item.item_id),
        "history": query_ledger(db, item=item.item_id, limit=1000),
    }


# ---------------------------------------------------------------- ledger query / search
def query_ledger(db: Session, *, item=None, assembly=None, wo=None, batch=None, person=None,
                 action=None, date_from=None, date_to=None, limit=500, offset=0) -> list[Ledger]:
    q = db.query(Ledger)
    if item:
        q = q.filter(Ledger.item_id == normalize_id(item))
    if assembly:
        q = q.filter(Ledger.assembly_id == normalize_id(assembly))
    if wo:
        q = q.filter(Ledger.wo_id == wo.strip())
    if batch:
        q = q.filter(Ledger.batch_no == batch.strip())
    if person:
        like = f"%{person.strip()}%"
        q = q.filter(Ledger.issued_by.ilike(like) | Ledger.issued_to.ilike(like))
    if action:
        q = q.filter(Ledger.action == action.strip().upper())
    if date_from:
        q = q.filter(Ledger.txn_time >= date_from)
    if date_to:
        q = q.filter(Ledger.txn_time <= date_to)
    return q.order_by(Ledger.txn_id.desc()).offset(offset).limit(limit).all()


def search(db: Session, q: str) -> dict:
    """Match items, assemblies, WO or batch. Items include live status, location and holder."""
    term = (q or "").strip()
    if not term:
        return {"items": [], "assemblies": [], "issues": []}
    like = f"%{term.upper()}%"
    items = [dict(r) for r in db.execute(text(
        "SELECT i.item_id, i.item_type, i.description, s.status, s.current_location,"
        " s.wo_id, s.batch_no, s.issued_to, s.since FROM items i"
        " LEFT JOIN item_status s ON s.item_id = i.item_id"
        " WHERE UPPER(i.item_id) LIKE :l OR UPPER(COALESCE(i.description,'')) LIKE :l"
        " OR (s.wo_id IS NOT NULL AND UPPER(s.wo_id) LIKE :l)"
        " OR (s.batch_no IS NOT NULL AND UPPER(s.batch_no) LIKE :l)"
        " ORDER BY i.item_id LIMIT 100"), {"l": like}).mappings()]
    assemblies = [get_assembly_view(db, a.assembly_id) | {"description": a.description}
                  for a in db.query(Assembly).filter(
                      Assembly.assembly_id.ilike(f"%{term}%")).order_by(Assembly.assembly_id).limit(50)]
    issues = [dict(r) for r in db.execute(text(
        "SELECT group_id, assembly_id, wo_id, batch_no, issued_to, MIN(txn_time) AS issued_at,"
        " GROUP_CONCAT(item_id) AS item_ids FROM ledger WHERE action = 'ISSUE'"
        " AND (UPPER(wo_id) LIKE :l OR UPPER(batch_no) LIKE :l)"
        " GROUP BY group_id, assembly_id, wo_id, batch_no, issued_to"
        " ORDER BY issued_at DESC LIMIT 50"), {"l": like}).mappings()]
    return {"items": items, "assemblies": assemblies, "issues": issues}


def dashboard_counts(db: Session) -> dict:
    counts = {"AVAILABLE": 0, "IN_USE": 0, "UNDER_REPAIR": 0, "SCRAPPED": 0}
    for status, n in db.execute(text("SELECT status, COUNT(*) FROM item_status GROUP BY status")):
        counts[status] = n
    return counts


# ---------------------------------------------------------------- bulk import (Excel rows)
ITEM_COLUMNS = ["item_id", "item_type", "description", "od", "id_dim", "length", "home_location"]
ASSEMBLY_COLUMNS = ["assembly_id", "description", "od", "length", "die_id", "pattern_id",
                    "top_flange_id", "bottom_flange_id", "pouring_wt", "gating_wt", "rcd_wt"]
_ITEM_REQUIRED = ["item_id", "item_type"]
_ASSEMBLY_REQUIRED = ["assembly_id", "die_id", "pattern_id"]
_ITEM_NUMERIC = ["od", "id_dim", "length"]
_ASSEMBLY_NUMERIC = ["od", "length", "pouring_wt", "gating_wt", "rcd_wt"]


def _num(raw):
    """-> (float|None, error_flag)"""
    if raw is None or str(raw).strip() == "":
        return None, False
    try:
        return float(str(raw).strip()), False
    except ValueError:
        return None, True


def _missing_columns(columns, required):
    have = {c for c in columns}
    return [{"row": 1, "field": c, "message": f"Missing column: {c}"} for c in required if c not in have]


def validate_import(db: Session, kind: str, columns: list[str], rows: list[dict]) -> dict:
    """Validate parsed Excel rows without touching the DB.

    `rows` are dicts keyed by lower-case header; data starts at Excel row 2.
    Returns {"errors": [...], "records": [clean dicts]} (records only meaningful if no errors).
    """
    if kind not in ("items", "assemblies"):
        raise ValidationError("Import kind must be 'items' or 'assemblies'")
    is_items = kind == "items"
    id_col = "item_id" if is_items else "assembly_id"
    required = _ITEM_REQUIRED if is_items else _ASSEMBLY_REQUIRED
    numeric = _ITEM_NUMERIC if is_items else _ASSEMBLY_NUMERIC

    errors = _missing_columns(columns, [id_col] + required)
    if errors:
        return {"errors": errors, "records": []}

    errors, records, seen = [], [], {}
    for n, row in enumerate(rows, start=2):
        if all(v is None or str(v).strip() == "" for v in row.values()):
            continue  # blank spreadsheet row; keep numbering so error rows match Excel

        def err(field, msg, n=n):
            errors.append({"row": n, "field": field, "message": msg})

        raw_id = row.get(id_col)
        try:
            rid = normalize_id(raw_id)
        except ValidationError:
            err(id_col, f"{id_col} is required")
            continue
        if rid in seen:
            err(id_col, f"Duplicate {id_col} {rid} (also on row {seen[rid]})")
        else:
            seen[rid] = n
        rec = {id_col: rid}
        for col in numeric:
            val, bad = _num(row.get(col))
            if bad:
                err(col, f"{col} must be a number, got {row.get(col)!r}")
            rec[col] = val
        rec["description"] = _clean(row.get("description"))

        if is_items:
            if db.get(Item, rid) is not None:
                err(id_col, f"Item {rid} already exists")
            itype = (row.get("item_type") or "").strip().upper()
            if itype not in ITEM_TYPES:
                err("item_type", f"Invalid item_type {row.get('item_type')!r}; expected one of "
                    + ", ".join(ITEM_TYPES))
            rec["item_type"] = itype
            rec["home_location"] = _clean(row.get("home_location"))
        else:
            if db.get(Assembly, rid) is not None:
                err(id_col, f"Assembly {rid} already exists")
            for slot, want, req in _ASSEMBLY_SLOTS:
                ref, msg = _check_ref(db, slot, row.get(slot), want, req)
                if msg:
                    err(slot, msg)
                rec[slot] = ref
        records.append(rec)
    return {"errors": errors, "records": records}


def import_rows(db: Session, kind: str, columns: list[str], rows: list[dict], commit: bool) -> dict:
    """Validate, and (only if commit and there are zero errors) write everything atomically."""
    result = validate_import(db, kind, columns, rows)
    errors, records = result["errors"], result["records"]
    out = {"kind": kind, "rows": len(records), "errors": errors,
           "valid": not errors and bool(records), "committed": False, "created": 0}
    if not records and not errors:
        out["errors"] = [{"row": 2, "field": "", "message": "File has no data rows"}]
        out["valid"] = False
    if commit and out["valid"]:
        event_id = _new_event()  # the whole import is one event
        with _transaction(db):
            for rec in records:
                if kind == "items":
                    rid, itype = rec.pop("item_id"), rec.pop("item_type")
                    home = rec.pop("home_location")
                    _add_item(db, rid, itype, event_id=event_id, home_location=home, received_by="IMPORT",
                              remarks="Imported from Excel", **rec)
                else:
                    _add_assembly(db, rec.pop("assembly_id"), **rec)
        out["committed"], out["created"] = True, len(records)
    return out


def status_report(db: Session) -> list[dict]:
    """Every item with its derived status/location (for the status export)."""
    return list_items(db)
