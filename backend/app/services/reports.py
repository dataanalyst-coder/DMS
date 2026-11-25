"""Reports and exports (spec section 7). Every report returns {title, columns, rows, summary} so the same data
feeds the screen and the Excel export. All figures are derived from the ledgers; nothing here changes anything."""
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import AssemblyDefinition, ConsumableBatch, Disposition, Item, MeMapping, RepairJob, UsageExposure, User, _now
from . import assemblies, audit, batches, builds, config, issues, items, readiness, usage
from .errors import NotFoundError, ValidationError


def _all_items(db: Session, **filters) -> list[dict]:
    rows, offset = [], 0
    while True:
        total, page = items.search_items(db, limit=500, offset=offset, active=True, **filters)
        rows += page
        offset += 500
        if offset >= total:
            return rows


def inventory(db: Session, *, location_id=None, condition=None, custody=None) -> dict:
    """Every active item with its derived condition, custody and location; grouped counts for the summary."""
    rows = _all_items(db, location_id=location_id, condition=condition, custody=custody)
    by_cond, by_loc = {}, {}
    for r in rows:
        by_cond[r["condition"]] = by_cond.get(r["condition"], 0) + 1
        key = f"{r['location']} | {r['condition']}"
        by_loc[key] = by_loc.get(key, 0) + 1
    cols = ["display_id", "system_id", "design_code", "category", "revision", "condition", "custody", "location",
            "home_location", "home_mismatch"]
    return {"title": "Inventory by location and condition", "columns": cols, "rows": rows,
            "summary": {"items": len(rows), "by_condition": by_cond, "by_location_and_condition": by_loc}}


def assembly_readiness(db: Session) -> dict:
    """Can each approved assembly be filled from serviceable stock right now? (Not a reservation.)"""
    rows = []
    for d in db.query(AssemblyDefinition).filter(AssemblyDefinition.active.is_(True)).order_by(AssemblyDefinition.code):
        if assemblies.current_approved(db, d.id) is None:
            continue
        ev = readiness.evaluate_definition(db, d.id)
        rows.append({"assembly": d.code, "revision": ev["revision"], "result": ev["result"],
                     "shortages": "; ".join(s["message"] for s in ev["shortages"]),
                     "custody_warnings": len(ev["custody_warnings"]),
                     "mapped_parts": db.query(MeMapping).filter_by(definition_id=d.id, active=True).count()})
    return {"title": "Assembly readiness", "columns": ["assembly", "revision", "result", "shortages", "custody_warnings", "mapped_parts"],
            "rows": rows, "summary": {"assemblies": len(rows), "ready": sum(r["result"] == "READY" for r in rows),
                                      "not_ready": sum(r["result"] != "READY" for r in rows)}}


def custody_by_wo(db: Session) -> dict:
    """What is out with Production, per WO: items and consumables not yet returned, with how long."""
    now = _now()
    rows = []
    for i in issues.list_issues(db, status="open", limit=500)[1]:
        d = issues.issue_detail(db, i["id"])
        rows.append({"wo_id": d["wo_id"], "issue": d["code"], "part": ", ".join(dict.fromkeys(l["part_id"] for l in d["setup_lines"])) or d["part_id"], "operation": d["operation_id"],
                     "build": d["build"]["code"], "issued_by": d["issued_by"], "issued_at": d["issued_at"],
                     "days_out": (now - datetime.fromisoformat(str(d["issued_at"]))).days if d["issued_at"] else None,
                     "status": d["status"], "outstanding": d["outstanding"]})
    return {"title": "Custody by WO", "columns": ["wo_id", "issue", "part", "operation", "build", "issued_by", "issued_at",
                                                    "days_out", "status", "outstanding"],
            "rows": rows, "summary": {"open_issues": len(rows)}}


def missing_and_overdue(db: Session, *, days: int = 30) -> dict:
    """Items marked missing, and items out with Production for longer than `days` (for MANUAL review: the
    system does not decide an item is lost)."""
    if days < 0:
        raise ValidationError("days must be >= 0")
    rows = []
    for r in db.execute(text("SELECT i.id, i.display_id, s.since, s.location_id FROM items i JOIN item_state s ON s.item_id = i.id "
                             "WHERE i.active = 1 AND s.custody = 'MISSING' ORDER BY s.since")):
        rows.append({"kind": "MISSING", "item": r[1], "since": r[2], "detail": "marked missing in a stock count",
                     "wo_id": None})
    for i in issues.list_issues(db, status="open", limit=500)[1]:
        d = issues.issue_detail(db, i["id"])
        age = (_now() - datetime.fromisoformat(str(d["issued_at"]))).days
        if age >= days:
            for l in d["lines"]:
                if l["state"] in ("PENDING_HANDOVER", "IN_PRODUCTION"):
                    rows.append({"kind": "OVERDUE", "item": l["display_id"], "since": d["issued_at"],
                                 "detail": f"out {age} days on {d['code']} (review manually)", "wo_id": d["wo_id"]})
    return {"title": f"Missing and overdue items (out {days}+ days)", "columns": ["kind", "item", "wo_id", "since", "detail"],
            "rows": rows, "summary": {"missing": sum(r["kind"] == "MISSING" for r in rows),
                                      "overdue": sum(r["kind"] == "OVERDUE" for r in rows)}}


def repairs(db: Session) -> dict:
    users = {u.id: u.username for u in db.query(User.id, User.username)}
    rows = []
    for r in db.query(RepairJob).order_by(RepairJob.id.desc()):
        it, d = db.get(Item, r.item_id), db.get(Disposition, r.disposition_id)
        end = r.completed_at or _now()
        rows.append({"repair": r.id, "item": it.display_id, "status": r.status, "findings": d.findings,
                     "responsible": d.responsible, "opened_by": users.get(r.opened_by), "opened_at": r.opened_at,
                     "completed_at": r.completed_at, "days": (end - r.opened_at).days, "work_done": r.work_done,
                     "measurements": r.measurements})
    return {"title": "Repairs", "columns": ["repair", "item", "status", "findings", "responsible", "opened_by", "opened_at",
                                           "completed_at", "days", "work_done", "measurements"],
            "rows": rows, "summary": {"open": sum(r["status"] == "OPEN" for r in rows),
                                      "completed": sum(r["status"] == "COMPLETED" for r in rows)}}


def item_usage(db: Session) -> dict:
    """Cycles and poured tonnage per reusable element, plus the assembly-level figure that actually adds up to output.
    Unknown opening usage is shown as unknown, never as zero."""
    ids = [r[0] for r in db.execute(text("SELECT DISTINCT item_id FROM usage_exposures WHERE item_id IS NOT NULL"))]
    rows = []
    for iid in ids:
        u = usage.item_usage(db, iid)
        rows.append({"item": u["display_id"], "recorded_cycles": u["recorded_cycles"], "recorded_tonnage": u["recorded_tonnage"],
                     "opening_cycles": u["opening_cycles"] if u["opening_cycles"] is not None else "unknown",
                     "opening_tonnage": u["opening_tonnage"] if u["opening_tonnage"] is not None else "unknown",
                     "total_cycles": u["total_cycles"] if u["total_cycles"] is not None else "unknown",
                     "total_tonnage": u["total_tonnage"] if u["total_tonnage"] is not None else "unknown",
                     "events_with_unknown_tonnage": u["events_with_unknown_tonnage"]})
    out = usage.foundry_output(db)
    return {"title": "Item cycles and tonnage", "columns": ["item", "recorded_cycles", "recorded_tonnage", "opening_cycles",
                                                              "opening_tonnage", "total_cycles", "total_tonnage",
                                                              "events_with_unknown_tonnage"],
            "rows": sorted(rows, key=lambda r: r["item"]),
            "summary": {"foundry_output_tonnes": out["tonnage"], "assembly_events": out["events"],
                        "note": "Foundry output counts assembly events only; element tonnage is exposure and is never summed."}}


def mismatches_5s(db: Session) -> dict:
    """Items stored away from their home location, shown with where they are and the reason on record."""
    locs = {l["id"]: l["path"] for l in config.list_locations(db)}
    rows = []
    for r in db.execute(text(
            "SELECT i.display_id, i.home_location_id, s.location_id, s.custody, s.item_condition, s.since, l.remarks, u.username "
            "FROM items i JOIN item_state s ON s.item_id = i.id JOIN ledger l ON l.txn_id = s.txn_id "
            "JOIN users u ON u.id = l.user_id WHERE i.active = 1 AND s.custody IN ('STORES', 'ASSEMBLY_AREA', 'REPAIR') "
            "AND s.location_id IS NOT i.home_location_id ORDER BY s.since")):
        rows.append({"item": r[0], "home_location": locs.get(r[1]), "actual_location": locs.get(r[2]), "custody": r[3],
                     "condition": r[4], "since": r[5], "reason": r[6], "moved_by": r[7],
                     "reason_recorded": bool(r[6] and r[6].strip())})
    return {"title": "5S: items away from their home location", "columns": ["item", "home_location", "actual_location", "custody",
                                                                        "condition", "since", "moved_by", "reason", "reason_recorded"],
            "rows": rows, "summary": {"away_from_home": len(rows), "without_a_reason": sum(not r["reason_recorded"] for r in rows)}}


def batch_stock(db: Session) -> dict:
    total, rows = batches.list_batches(db, limit=500)
    return {"title": "Consumable stock", "columns": ["code", "design_code", "supplier_lot", "uom", "usable", "held", "outstanding",
                                                    "consumed", "expiry_date", "location", "status"],
            "rows": rows, "summary": {"batches": total}}


def audit_trail(db: Session, *, entity_type=None, user=None, action=None, limit=2000) -> dict:
    total, rows = audit.list_audit(db, entity_type=entity_type, user=user, action=action, limit=limit)
    data = [{"at": r.at, "user": r.username, "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id,
             "before": r.before, "after": r.after, "reason": r.reason} for r in rows]
    return {"title": "Audit trail", "columns": ["at", "user", "action", "entity_type", "entity_id", "before", "after", "reason"],
            "rows": data, "summary": {"total": total, "shown": len(data)}}


REPORTS = {
    "inventory": inventory, "assembly-readiness": assembly_readiness, "custody-by-wo": custody_by_wo,
    "missing-overdue": missing_and_overdue, "repairs": repairs, "item-usage": item_usage, "5s-mismatches": mismatches_5s,
    "batch-stock": batch_stock, "audit": audit_trail,
}


def run(db: Session, name: str, **params) -> dict:
    fn = REPORTS.get(name)
    if fn is None:
        raise NotFoundError(f"Unknown report {name!r}; available: {', '.join(sorted(REPORTS))}")
    return fn(db, **{k: v for k, v in params.items() if v not in (None, "")})
