"""WO release readiness: do the tooling elements for the selected option EXIST in serviceable condition?

This is deliberately different from physical issue availability (spec section 3):
  * serviceable items that are issued to production, or fitted in another build, still count (shown as
    custody warnings); nothing is reserved and nothing is blocked for that reason
  * one physical item can satisfy only one slot (distinct-item matching, never a per-line stock count)
  * missing / held / awaiting inspection / under repair / scrapped items do not count, and the shortage
    names them
  * a draft, obsolete or unapproved assembly, or tooling still to be developed, is NOT READY
  * if the mapping cannot be verified the answer is UNABLE_TO_VERIFY: never "ready", and there is no
    manual bypass
  * if the selected option fails but another approved option passes, that option is shown, never
    selected silently. Candidate item IDs are evidence only, not allocation."""
import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import (AssemblyDefinition, Item, MeMapping, ReadinessCandidate, ReadinessCheck, User)
from . import assemblies, audit, batches, builds, issues, mappings, permissions
from .common import clean, require_text, transaction
from .errors import NotFoundError, ValidationError
from .matching import Candidate, match


def _reason(code, message, **extra):
    return {"code": code, "message": message, **extra}


def evaluate_mapping(db: Session, m: MeMapping) -> dict:
    """Pure evaluation of one assembly option (no writes). Returns result/reasons/shortages/warnings."""
    d = db.get(AssemblyDefinition, m.definition_id)
    out = {"mapping_id": m.id, "definition_id": d.id, "assembly_code": d.code, "definition_revision_id": None,
           "revision": None, "result": "NOT_READY", "reasons": [], "shortages": [], "custody_warnings": [],
           "warnings": [], "candidate_item_ids": []}
    if not m.active:
        out["reasons"].append(_reason("MAPPING_INACTIVE", "This option is no longer mapped to the operation"))
    if not d.active:
        out["reasons"].append(_reason("ASSEMBLY_INACTIVE", f"Assembly {d.code} is inactive"))
    if m.tooling_status != "READY":
        out["reasons"].append(_reason("TOOLING_TO_BE_DEVELOPED", "Tooling is marked to be developed"))
    rev = mappings._effective_revision(db, m)
    if rev is None or rev.status != "APPROVED":
        status = rev.status if rev else "none"
        out["reasons"].append(_reason("ASSEMBLY_NOT_APPROVED", f"Assembly {d.code} has no approved revision for this "
                                      f"operation (revision status: {status})"))
    if rev is not None:
        out["definition_revision_id"], out["revision"] = rev.id, rev.revision
    if out["reasons"]:
        return out
    return _core(db, out, rev)


def evaluate_definition(db: Session, definition_id: int) -> dict:
    """Readiness of an assembly on its own (no operation mapping): can its current approved revision be filled from
    serviceable stock right now? Used by the assembly-readiness report."""
    d = assemblies.get_definition(db, definition_id)
    rev = assemblies.current_approved(db, d.id)
    out = {"mapping_id": None, "definition_id": d.id, "assembly_code": d.code, "definition_revision_id": rev.id if rev else None,
           "revision": rev.revision if rev else None, "result": "NOT_READY", "reasons": [], "shortages": [],
           "custody_warnings": [], "warnings": [], "candidate_item_ids": []}
    if not d.active:
        out["reasons"].append(_reason("ASSEMBLY_INACTIVE", f"Assembly {d.code} is inactive"))
    if rev is None:
        out["reasons"].append(_reason("ASSEMBLY_NOT_APPROVED", f"Assembly {d.code} has no approved revision"))
    if out["reasons"]:
        return out
    return _core(db, out, rev)


def _core(db: Session, out: dict, rev) -> dict:
    lines = assemblies.matching_lines(db, rev.id)
    # consumables never block release; low or absent stock is a warning. Sufficient usable quantity is
    # enforced at issue. No future allocation is made.
    for c in assemblies.consumable_lines(db, rev.id):
        avail = batches.available_for_designs(db, c["accepted_design_ids"])
        total = round(sum(b["usable"] for b in avail), 6)
        if not avail:
            out["warnings"].append(_reason("CONSUMABLE_ABSENT", f"No usable stock of {c['design_code']} "
                                           f"({c['quantity']:g} {c['uom']} is needed per issue)"))
        elif total < c["quantity"]:
            out["warnings"].append(_reason("CONSUMABLE_LOW", f"Only {total:g} {c['uom']} of {c['design_code']} is usable "
                                           f"({c['quantity']:g} {c['uom']} is needed per issue)"))
    design_ids = sorted({d_id for ln in lines for d_id in ln.accepts})
    if not design_ids:
        out["result"] = "READY"
        return out
    marks = ",".join(str(x) for x in design_ids)
    pool = db.execute(text(
        f"SELECT i.id, i.design_id, i.design_revision_id, i.display_id, s.item_condition AS cond, s.custody AS custody "
        f"FROM items i JOIN item_state s ON s.item_id = i.id WHERE i.active = 1 AND i.design_id IN ({marks})"
    )).mappings().all()
    fitted = {r[0]: r[1] for r in db.execute(text(
        "SELECT item_id, build_id FROM build_components WHERE removed_at IS NULL"))}
    accepted_somewhere = {(d_id, rid) for ln in lines for d_id, revs in ln.accepts.items() for rid in revs}
    pool = [p for p in pool if (p["design_id"], p["design_revision_id"]) in accepted_somewhere]

    pool = [({**p, "cond": "MISSING"} if p["custody"] == "MISSING" else p) for p in pool]  # missing never counts
    serviceable = [p for p in pool if p["cond"] == "SERVICEABLE"]
    # evidence only: prefer parts sitting in stores, not fitted anywhere, so custody warnings are minimal
    serviceable.sort(key=lambda p: (p["custody"] != "STORES", p["id"] in fitted, p["display_id"]))
    cands = [Candidate(p["id"], p["design_id"], p["design_revision_id"]) for p in serviceable]
    assignment, _, unfilled = match(lines, cands)

    by_id = {p["id"]: p for p in serviceable}
    for item_id in assignment:
        p = by_id[item_id]
        out["candidate_item_ids"].append(item_id)
        if p["custody"] != "STORES" or item_id in fitted:
            w = {"item_id": item_id, "display_id": p["display_id"], "custody": p["custody"],
                 "build": builds.build_code(fitted[item_id]) if item_id in fitted else None, "wo_id": None}
            o = issues.open_issue_for_item(db, item_id)
            if o:
                w["wo_id"] = o[0].wo_id
            w["message"] = (f"{p['display_id']} is {p['custody'].replace('_', ' ').lower()}"
                            + (f" (WO {w['wo_id']})" if w["wo_id"] else "")
                            + (f", fitted in {w['build']}" if w["build"] else "")
                            + "; not reserved, check physical availability at issue")
            out["custody_warnings"].append(w)

    for ln in lines:
        if ln.line_id not in unfilled:
            continue
        blocked = [p for p in pool if p["cond"] != "SERVICEABLE"
                   and any(p["design_id"] == d_id and p["design_revision_id"] in revs for d_id, revs in ln.accepts.items())]
        avail = sum(1 for c in cands if any(c.design_id == d_id and c.revision_id in revs for d_id, revs in ln.accepts.items()))
        miss = unfilled[ln.line_id]
        out["shortages"].append({
            "line": ln.label, "needed": ln.quantity, "missing": miss, "serviceable_candidates": avail,
            "blocked_items": [{"display_id": b["display_id"], "condition": b["cond"]} for b in blocked],
            "message": f"Short by {miss} x {ln.label} (needed {ln.quantity}, {ln.quantity - miss} usable)"
                       + (": " + ", ".join(f"{b['display_id']} is {b['cond']}" for b in blocked) if blocked else
                          ": no registered item of an accepted design/revision exists")})
    if out["shortages"]:
        out["reasons"].append(_reason("SHORTAGE", "; ".join(s["message"] for s in out["shortages"])))
        return out
    out["result"] = "READY"
    return out


def _persist(db: Session, actor: User, base: dict, ev: dict | None, alternatives: list[dict]) -> ReadinessCheck:
    ev = ev or {}
    c = ReadinessCheck(
        requested_by=actor.id, wo_id=base["wo_id"], part_id=base["part_id"],
        routing_revision=base["routing_revision"], operation_id=base["operation_id"],
        mapping_id=ev.get("mapping_id"), definition_id=ev.get("definition_id"),
        definition_revision_id=ev.get("definition_revision_id"), result=ev.get("result", "UNABLE_TO_VERIFY"),
        reasons=json.dumps(ev.get("reasons", base.get("reasons", [])), default=str),
        shortages=json.dumps(ev.get("shortages", []), default=str),
        custody_warnings=json.dumps(ev.get("custody_warnings", []), default=str),
        warnings=json.dumps(ev.get("warnings", []), default=str),
        alternatives=json.dumps(alternatives, default=str))
    db.add(c)
    db.flush()
    for item_id in ev.get("candidate_item_ids", []):
        db.add(ReadinessCandidate(check_id=c.id, item_id=item_id))
    audit.record(db, actor, "READINESS_CHECK", "readiness_check", c.id,
                 after={"wo_id": c.wo_id, "result": c.result, "mapping_id": c.mapping_id})
    return c


def run_check(db: Session, actor: User, *, wo_id, part_id, routing_revision, operation_id,
              mapping_id=None) -> dict:
    """The WO release gate. Always evaluates NOW against current approvals and item conditions, and
    records the outcome (result, time, revisions, reason codes) as evidence."""
    permissions.require(actor, "readiness.check")
    require_text(wo_id=wo_id)
    base = {"wo_id": wo_id.strip(), "part_id": mappings._ref(part_id, "Part ID"),
            "routing_revision": mappings._ref(routing_revision, "Routing revision"),
            "operation_id": mappings._ref(operation_id, "Operation ID")}
    options = db.query(MeMapping).filter_by(part_id=base["part_id"], routing_revision=base["routing_revision"],
                                            operation_id=base["operation_id"], active=True).order_by(MeMapping.id).all()
    chosen, unable = None, None
    if mapping_id is not None:
        chosen = db.get(MeMapping, mapping_id)
        if chosen is None or (chosen.part_id, chosen.routing_revision, chosen.operation_id) != (
                base["part_id"], base["routing_revision"], base["operation_id"]):
            raise ValidationError("The selected option does not belong to this part/routing/operation")
    elif not options:
        unable = _reason("MAPPING_NOT_FOUND", "No assembly option is mapped to this part, routing revision and "
                         "operation; the mapping cannot be verified")
    else:
        selected = [o for o in options if o.selected]
        if len(selected) == 1:
            chosen = selected[0]
        else:
            unable = _reason("NO_SELECTED_OPTION", "ME has not selected exactly one assembly option for this "
                             "operation; the selection cannot be verified")
    if unable:
        base["reasons"] = [unable]
        ev, alts = None, []
    else:
        ev = evaluate_mapping(db, chosen)
        alts = []
        if ev["result"] != "READY":  # show, never silently switch
            for o in options:
                if o.id != chosen.id:
                    e = evaluate_mapping(db, o)
                    if e["result"] == "READY":
                        alts.append({"mapping_id": o.id, "assembly_code": e["assembly_code"], "revision": e["revision"],
                                     "custody_warnings": e["custody_warnings"]})
    with transaction(db):
        c = _persist(db, actor, base, ev, alts)
    return check_dict(db, c)


def check_dict(db: Session, c: ReadinessCheck) -> dict:
    users = {u.id: u.username for u in db.query(User.id, User.username)}
    d = db.get(AssemblyDefinition, c.definition_id) if c.definition_id else None
    cands = [r.item_id for r in db.query(ReadinessCandidate).filter(ReadinessCandidate.check_id == c.id)]
    return {
        "id": c.id, "checked_at": c.checked_at, "checked_by": users.get(c.requested_by), "wo_id": c.wo_id,
        "part_id": c.part_id, "routing_revision": c.routing_revision, "operation_id": c.operation_id,
        "mapping_id": c.mapping_id, "assembly": d.code if d else None, "definition_revision_id": c.definition_revision_id,
        "result": c.result, "can_release": c.result == "READY",
        "reasons": json.loads(c.reasons), "shortages": json.loads(c.shortages),
        "custody_warnings": json.loads(c.custody_warnings), "warnings": json.loads(c.warnings),
        "alternatives": json.loads(c.alternatives), "candidate_item_ids": cands,
        "needs_recheck": c.needs_recheck, "recheck_reason": c.recheck_reason, "wo_closed": c.wo_closed,
        "note": "Ready means the tooling exists in serviceable condition. Physical issue is a separate check; "
                "nothing is reserved." if c.result == "READY" else None}


def list_checks(db: Session, *, wo_id=None, limit=100, offset=0) -> tuple[int, list[dict]]:
    q = db.query(ReadinessCheck)
    if wo_id:
        q = q.filter(ReadinessCheck.wo_id == wo_id.strip())
    total = q.count()
    rows = q.order_by(ReadinessCheck.id.desc()).offset(offset).limit(min(limit, 500)).all()
    return total, [check_dict(db, c) for c in rows]


def get_check(db: Session, check_id: int) -> dict:
    c = db.get(ReadinessCheck, check_id)
    if c is None:
        raise NotFoundError(f"Readiness check {check_id} not found")
    return check_dict(db, c)


def close_wo(db: Session, actor: User, wo_id: str) -> int:
    """OMS tells us the WO is finished, so it stops being flagged for rechecks."""
    permissions.require(actor, "readiness.check")
    rows = db.query(ReadinessCheck).filter(ReadinessCheck.wo_id == (wo_id or "").strip(),
                                           ReadinessCheck.wo_closed.is_(False)).all()
    if not rows:
        raise NotFoundError(f"No open readiness checks for WO {wo_id}")
    with transaction(db):
        for c in rows:
            c.wo_closed = True
        audit.record(db, actor, "CLOSE_WO", "readiness_check", rows[-1].id, after={"wo_id": wo_id})
    return len(rows)
