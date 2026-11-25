"""Bulk item import: preview with row-level errors, duplicate checks, review queue, then commit."""
import json
import re
from datetime import date

from sqlalchemy.orm import Session

from ..models import CONDITIONS, ElementDesign, Item, ImportReview, DesignRevision, User, _now
from . import audit, config, designs, items as items_svc, permissions
from .common import clean, new_event, norm_opt, require_reason, transaction
from .errors import NotFoundError, ValidationError

ITEM_COLUMNS = ["legacy_no", "display_id", "design_code", "design_revision", "condition",
                "home_location", "current_location", "acquisition_date", "provenance",
                "opening_cycles", "opening_tonnage", "remarks"]
REQUIRED_COLUMNS = ["design_code", "condition", "home_location"]


def _num(raw, name, integer=False):
    if raw is None or str(raw).strip() == "":
        return None  # blank = unknown; never silently 0
    try:
        v = float(str(raw).strip())
    except ValueError:
        raise ValidationError(f"{name} must be a number, got {raw!r}")
    if v < 0:
        raise ValidationError(f"{name} must be >= 0")
    if integer:
        if v != int(v):
            raise ValidationError(f"{name} must be a whole number")
        return int(v)
    return v


def _date(raw):
    if raw is None or str(raw).strip() == "":
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(raw).strip())
    if not m:
        raise ValidationError(f"acquisition_date must be YYYY-MM-DD, got {raw!r}")
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        raise ValidationError(f"acquisition_date is not a real date: {raw!r}")


def _is_blank(row: dict) -> bool:
    return all(v is None or str(v).strip() == "" for v in row.values())


def _classify(db: Session, rows: list[dict]) -> list[dict]:
    """-> per-row {row, status: OK|ERROR|DUPLICATE, errors[], message, kwargs}. No DB writes."""
    results = []
    seen_display: dict[str, int] = {}  # display ids that this file will create -> first row
    seen_legacy: dict[str, int] = {}
    designs_cache: dict[str, ElementDesign | None] = {}
    for n, row in enumerate(rows, start=2):
        if _is_blank(row):
            continue
        res = {"row": n, "status": "OK", "errors": [], "message": None, "kwargs": None}
        results.append(res)

        def err(msg, res=res):
            res["errors"].append(msg)

        try:
            code = norm_opt(row.get("design_code"))
            if not code:
                raise ValidationError("design_code is required")
            if code not in designs_cache:
                designs_cache[code] = db.query(ElementDesign).filter(ElementDesign.code == code).first()
            design = designs_cache[code]
            if design is None:
                raise ValidationError(f"Unknown design {code}")
            if not design.active:
                raise ValidationError(f"Design {code} is inactive")
            if design.kind != "REUSABLE":
                raise ValidationError(f"Design {code} is a consumable, not a reusable item")
            rev_label = clean(row.get("design_revision"))
            if rev_label:
                rev = db.query(DesignRevision).filter_by(design_id=design.id, revision=rev_label).first()
                if rev is None:
                    raise ValidationError(f"Design {code} has no revision {rev_label}")
                if rev.status == "DRAFT":
                    raise ValidationError(f"Design {code} revision {rev_label} is not approved")
                rev_id = rev.id
            else:
                if designs.current_approved(db, design.id) is None:
                    raise ValidationError(f"Design {code} has no approved revision")
                rev_id = None
            cond = (row.get("condition") or "").strip().upper()
            if cond not in CONDITIONS:
                raise ValidationError(f"condition must be one of {', '.join(CONDITIONS)}")
            home = config.resolve_path(db, row.get("home_location"))
            cur = config.resolve_path(db, row.get("current_location")) if clean(
                row.get("current_location")) else None
            legacy_key = norm_opt(row.get("legacy_no"))
            display = norm_opt(row.get("display_id"))
            if not (legacy_key or display):
                raise ValidationError("legacy_no or display_id is required")
            kwargs = dict(design_id=design.id, design_revision_id=rev_id, legacy_no=clean(row.get("legacy_no")),
                          display_id=display, condition=cond, location_id=home.id,
                          current_location_id=cur.id if cur else None,
                          acquisition_date=_date(row.get("acquisition_date")),
                          provenance=clean(row.get("provenance")),
                          opening_cycles=_num(row.get("opening_cycles"), "opening_cycles", integer=True),
                          opening_tonnage=_num(row.get("opening_tonnage"), "opening_tonnage"),
                          remarks=clean(row.get("remarks")))
            res["kwargs"] = kwargs
        except ValidationError as e:
            err(e.message)
            continue

        effective = display or legacy_key
        in_db = db.query(Item).filter(Item.display_id == effective).first()
        taken_in_file = seen_display.get(effective)
        if display and (in_db or taken_in_file):
            where = f"item {in_db.system_id}" if in_db else f"row {taken_in_file}"
            err(f"display_id {display} is already used by {where}")
            continue
        if not display:
            legacy_db = db.query(Item).filter(Item.legacy_key == legacy_key).first()
            clash = in_db or legacy_db
            clash_row = taken_in_file or seen_legacy.get(legacy_key)
            if clash or clash_row:
                res["status"] = "DUPLICATE"
                res["message"] = (f"Legacy number {legacy_key} already exists "
                                  f"({'item ' + clash.system_id if clash else 'row ' + str(clash_row)}); "
                                  f"held for review: give this item a unique display ID or reject it")
                continue
        seen_display[effective] = n
        if legacy_key:
            seen_legacy.setdefault(legacy_key, n)
    for r in results:
        if r["errors"]:
            r["status"] = "ERROR"
    return results


def _summary(results, committed=False, created=0, queued=0):
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("OK", "ERROR", "DUPLICATE")}
    return {
        "rows": len(results), "counts": counts, "committed": committed, "created": created,
        "queued_for_review": queued, "valid": counts["ERROR"] == 0 and len(results) > 0,
        "errors": [{"row": r["row"], "message": m} for r in results for m in r["errors"]],
        "duplicates": [{"row": r["row"], "message": r["message"]} for r in results
                       if r["status"] == "DUPLICATE"]}


def import_items(db: Session, actor: User, columns: list[str], rows: list[dict], *, commit: bool,
                 source_file=None) -> dict:
    """Preview (commit=False) or commit. Any ERROR row blocks the whole commit; clashing legacy
    numbers are not overwritten and not guessed: they go to the review queue."""
    permissions.require(actor, "item.import")
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if "legacy_no" not in columns and "display_id" not in columns:
        missing.append("legacy_no or display_id")
    if missing:
        return {"rows": 0, "counts": {"OK": 0, "ERROR": 1, "DUPLICATE": 0}, "committed": False,
                "created": 0, "queued_for_review": 0, "valid": False, "duplicates": [],
                "errors": [{"row": 1, "message": f"Missing column: {m}"} for m in missing]}
    results = _classify(db, rows)
    out = _summary(results)
    if not results:
        out["errors"] = [{"row": 2, "message": "File has no data rows"}]
        return out
    if not commit or not out["valid"]:
        return out

    event_id, created, queued = new_event(), 0, 0
    with transaction(db):
        for r in results:
            if r["status"] == "OK":
                items_svc._stage_item(db, actor, event_id=event_id, **r["kwargs"])
                created += 1
            elif r["status"] == "DUPLICATE":
                data = {k: v for k, v in rows[r["row"] - 2].items()}
                db.add(ImportReview(created_by=actor.id, source_file=source_file, row_number=r["row"],
                                    data=json.dumps(data, default=str), reason=r["message"]))
                queued += 1
        audit.record(db, actor, "IMPORT", "item", None,
                     after={"file": source_file, "created": created, "queued": queued})
    return _summary(results, committed=True, created=created, queued=queued)


# ---------------------------------------------------------------- review queue
def list_review(db: Session, status="PENDING") -> list[dict]:
    q = db.query(ImportReview)
    if status:
        q = q.filter(ImportReview.status == status)
    return [{"id": r.id, "created_at": r.created_at, "source_file": r.source_file,
             "row_number": r.row_number, "data": json.loads(r.data), "reason": r.reason,
             "status": r.status, "resolution_note": r.resolution_note,
             "resolved_item_id": r.resolved_item_id} for r in q.order_by(ImportReview.id)]


def _get_review(db: Session, review_id: int) -> ImportReview:
    r = db.get(ImportReview, review_id)
    if r is None:
        raise NotFoundError(f"Review item {review_id} not found")
    if r.status != "PENDING":
        raise ValidationError(f"Review item is already {r.status}")
    return r


def resolve_review(db: Session, actor: User, review_id: int, *, display_id, note=None) -> Item:
    """Create the held item under a unique display ID (the legacy number stays as printed)."""
    permissions.require(actor, "review.resolve")
    r = _get_review(db, review_id)
    if not norm_opt(display_id):
        raise ValidationError("A unique display ID is required to resolve a duplicate")
    row = json.loads(r.data)
    row["display_id"] = display_id
    results = _classify(db, [row])
    res = results[0]
    if res["status"] != "OK":
        raise ValidationError("; ".join(res["errors"]) or res["message"])
    with transaction(db):
        item = items_svc._stage_item(db, actor, **res["kwargs"])
        r.status, r.resolved_by, r.resolved_at = "RESOLVED", actor.id, _now()
        r.resolution_note, r.resolved_item_id = clean(note), item.id
        audit.record(db, actor, "RESOLVE_DUPLICATE", "item", item.id,
                     after={"review_id": r.id, "display_id": item.display_id}, reason=note)
    return item


def reject_review(db: Session, actor: User, review_id: int, *, note) -> ImportReview:
    permissions.require(actor, "review.resolve")
    note = require_reason(note, "rejecting an imported row")
    r = _get_review(db, review_id)
    with transaction(db):
        r.status, r.resolved_by, r.resolved_at, r.resolution_note = "REJECTED", actor.id, _now(), note
        audit.record(db, actor, "REJECT_DUPLICATE", "import_review", r.id, reason=note)
    return r
