"""Issue of a verified build to a WO, hand-over acknowledgement, and returns (reusable items AND consumables).

  * Stores issues -> items go PENDING_HANDOVER (not free stock); Production acknowledges receipt under a named
    account -> PRODUCTION. Item custody changes and consumable deductions commit in ONE transaction.
  * Scanning a build (or any of its parts) expands the verified component list and the caller must confirm
    exactly that list; nothing is concealed.
  * Consumables: every consumable line of the assembly needs batch picks totalling the line's required quantity
    (ME requirement or authorised entry, with its basis). A different total needs a documented reason. Insufficient,
    expired or held batches are rejected. Never an invented consumption rate.
  * Competing issues of the same parts or batch stock: only one wins (per-item / per-batch sequencing).
  * Whether a line is pending, in production, returned or void is DERIVED from the ledger rows that carry the
    issue_id (so reversing an ISSUE/ACKNOWLEDGE/RETURN event just works); nothing is stored twice.
  * Returned items become AWAITING_INSPECTION (or HOLD if damaged) and stay unavailable until cleared. Unused
    consumable quantity goes back to its batch; the rest is recorded as consumed, once."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import (BuildComponent, ConsumableBatch, DesignRevision, Document, DocumentVersion, ElementDesign, Issue,
                      IssueBatch, IssueDocument, IssueItem, IssueLine, Item, MeMapping, SetupLine, User, _now)
from . import assemblies, audit, batches, builds, config, documents, ledger, mappings, permissions, setups
from .matching import Candidate, match
from .common import clean, new_event, require_reason, require_text, transaction
from .errors import NotFoundError, ValidationError

_STATE = {"ISSUE": "PENDING_HANDOVER", "ACKNOWLEDGE": "IN_PRODUCTION", "RETURN": "RETURNED", "HANDOVER": "HANDED_ON"}
OUTSTANDING = ("PENDING_HANDOVER", "IN_PRODUCTION")
EPS = 1e-6

_EFFECTIVE = """
WITH eff AS (
  SELECT l.item_id, l.issue_id, l.action, l.txn_id, l.txn_time, l.user_id FROM ledger l
  WHERE l.issue_id IS NOT NULL AND l.action IN ('ISSUE', 'ACKNOWLEDGE', 'RETURN', 'HANDOVER')
    AND NOT EXISTS (SELECT 1 FROM ledger r WHERE r.action = 'REVERSAL' AND r.ref_txn_id = l.txn_id)
), cur AS (
  SELECT e.* FROM eff e
  WHERE e.txn_id = (SELECT MAX(e2.txn_id) FROM eff e2 WHERE e2.item_id = e.item_id AND e2.issue_id = e.issue_id)
)
"""


def _get(db: Session, issue_id: int) -> Issue:
    i = db.get(Issue, issue_id)
    if i is None:
        raise NotFoundError(f"Issue {issue_id} not found")
    return i


def issue_code(issue_id: int) -> str:
    return f"ISS-{issue_id:06d}"


def line_states(db: Session, issue_id: int) -> dict[int, dict | None]:
    """item_id -> {state, since, by} derived from the ledger (state None = void)."""
    rows = db.execute(text(_EFFECTIVE + "SELECT item_id, action, txn_time, user_id FROM cur WHERE issue_id = :i"),
                      {"i": issue_id}).mappings().all()
    by_item = {r["item_id"]: {"state": _STATE[r["action"]], "since": r["txn_time"], "by": r["user_id"]} for r in rows}
    return {ii.item_id: by_item.get(ii.item_id) for ii in db.query(IssueItem).filter(IssueItem.issue_id == issue_id)}


def open_issue_for_item(db: Session, item_id: int) -> tuple[Issue, str] | None:
    row = db.execute(text(_EFFECTIVE + "SELECT issue_id, action FROM cur WHERE item_id = :i "
                                       "AND action IN ('ISSUE', 'ACKNOWLEDGE') ORDER BY txn_id DESC LIMIT 1"),
                     {"i": item_id}).first()
    return (db.get(Issue, row[0]), _STATE[row[1]]) if row else None


def outstanding_issue_ids(db: Session, pending_only=False) -> list[int]:
    acts = "('ISSUE')" if pending_only else "('ISSUE', 'ACKNOWLEDGE')"
    ids = {r[0] for r in db.execute(text(_EFFECTIVE + f"SELECT DISTINCT issue_id FROM cur WHERE action IN {acts}"))}
    if not pending_only:  # unsettled consumables keep an issue open too
        ids |= set(batches.outstanding_issue_ids(db))
    return sorted(ids)


def issue_counts(db: Session) -> dict:
    return {"outstanding_returns": len(outstanding_issue_ids(db)),
            "pending_handovers": len(outstanding_issue_ids(db, pending_only=True))}


# ---------------------------------------------------------------- preview (build scan expansion)
def _consumable_requirements(db: Session, revision_id: int) -> list[dict]:
    out = []
    for c in assemblies.consumable_lines(db, revision_id):
        out.append({**c, "batches": batches.available_for_designs(db, c["accepted_design_ids"])})
    return out


def issue_preview(db: Session, build_id: int) -> dict:
    """What a build scan expands to. The UI shows this list and requires confirmation of it."""
    d = builds.build_detail(db, build_id)
    b = builds._get(db, build_id)
    held = [c for c in d["components"] if c["custody"] != "ASSEMBLY_AREA"]
    out = {"build": {"id": d["id"], "code": d["code"], "status": d["status"], "assembly": d["assembly"]["code"],
                     "revision": d["revision"]["revision"], "location": d["location"]},
           "components": d["components"], "lines": d["lines"], "problems": d["problems"],
           "consumables": _consumable_requirements(db, b.revision_id),
           "can_issue": d["status"] == "VERIFIED" and d["ready_to_issue"] and not held}
    if held:
        o = open_issue_for_item(db, held[0]["item_id"])
        out["open_issue"] = {"id": o[0].id, "code": issue_code(o[0].id), "wo_id": wo_text(db, o[0]), "state": o[1]} if o else None
    return out


# ---------------------------------------------------------------- issue
def _plan_consumables(db: Session, revision_id: int, picks, override_reason) -> list[dict]:
    """Validate the batch picks against the assembly's consumable lines. Returns rows to book."""
    lines = {c["line_id"]: c for c in assemblies.consumable_lines(db, revision_id)}
    picks = list(picks or [])
    if not lines:
        if picks:
            raise ValidationError("This assembly has no consumable lines; remove the batch picks")
        return []
    if not picks:
        need = ", ".join(f"{c['quantity']:g} {c['uom']} of {c['design_code']}" for c in lines.values())
        raise ValidationError(f"Consumables are required for this issue: {need}. Pick the batches to issue")
    override = clean(override_reason)
    seen, totals, plan = set(), {lid: 0.0 for lid in lines}, []
    for p in picks:
        line = lines.get(int(p.get("line_id") or 0))
        if line is None:
            raise ValidationError("A batch pick refers to a consumable line that is not part of this assembly")
        batch = batches.get_batch(db, int(p["batch_id"]))
        if batch.design_id not in line["accepted_design_ids"]:
            raise ValidationError(f"Batch {batch.batch_code} is not an accepted material for {line['design_code']}")
        if batch.id in seen:
            raise ValidationError(f"Batch {batch.batch_code} appears twice; combine the quantity")
        seen.add(batch.id)
        q = batches._positive(p["quantity"], f"Quantity from {batch.batch_code}")
        totals[line["line_id"]] += q
        plan.append({"line": line, "batch": batch, "quantity": q})
    for lid, line in lines.items():
        got = round(totals[lid], 6)
        if got <= 0:
            raise ValidationError(f"No batch picked for {line['design_code']} (needs {line['quantity']:g} {line['uom']})")
        if abs(got - line["quantity"]) > EPS and not override:
            raise ValidationError(
                f"{line['design_code']} needs {line['quantity']:g} {line['uom']} ({line['basis'] or 'authorised requirement'}) "
                f"but {got:g} {line['uom']} was picked. To issue a different quantity, give the documented reason")
    for row in plan:
        row["override"] = override if abs(totals[row["line"]["line_id"]] - row["line"]["quantity"]) > EPS else None
    return plan


def wo_ids(db: Session, issue: Issue) -> list[str]:
    """The WOs an issue covers (one per setup line; older issues have just their own WO)."""
    got = [r[0] for r in db.query(SetupLine.wo_id).join(IssueLine, IssueLine.setup_line_id == SetupLine.id)
           .filter(IssueLine.issue_id == issue.id).order_by(IssueLine.id)]
    return list(dict.fromkeys(got)) or [issue.wo_id]


def wo_text(db: Session, issue: Issue) -> str:
    return ", ".join(wo_ids(db, issue))


def _resolve_mappings(db: Session, lines: list[SetupLine], b, mapping_id=None, line_mappings=None) -> list[tuple[SetupLine, MeMapping]]:
    """The ME mapping (operation) each line runs under: the one chosen for the line, else `mapping_id` when it is for that
    part, else the only / ME-selected selectable one for the part on this assembly."""
    chosen = {int(k): int(v) for k, v in (line_mappings or {}).items()}
    if mapping_id:
        one = db.get(MeMapping, int(mapping_id))
        if one is None:
            raise ValidationError("Choose the approved assembly option (ME mapping) for this operation")
        if lines and not any(ln.part_id == one.part_id for ln in lines):
            raise ValidationError(f"The operation chosen is for part {one.part_id}, but the setup is for part "
                                  f"{', '.join(dict.fromkeys(ln.part_id for ln in lines))}")
    out = []
    for ln in lines:
        m = None
        if ln.id in chosen:
            m = db.get(MeMapping, chosen[ln.id])
        elif mapping_id and (db.get(MeMapping, int(mapping_id)) or MeMapping()).part_id == ln.part_id:
            m = db.get(MeMapping, int(mapping_id))
        else:
            cands = [x for x in db.query(MeMapping).filter(MeMapping.part_id == ln.part_id, MeMapping.definition_id == b.definition_id,
                                                           MeMapping.active.is_(True)).order_by(MeMapping.id)
                     if mappings.mapping_dict(db, x)["selectable"]]
            m = cands[0] if len(cands) == 1 else next((x for x in cands if x.selected), None)
        if m is None:
            raise ValidationError(f"Choose the operation (ME mapping) for part {ln.part_id} (WO {ln.wo_id}): none is linked to this "
                                  f"assembly, or several are and none is the ME-selected one")
        if m.part_id != ln.part_id:
            raise ValidationError(f"The operation chosen is for part {m.part_id}, but WO {ln.wo_id} is for part {ln.part_id}")
        out.append((ln, m))
    return out


def _check_config(db: Session, m: MeMapping, b) -> None:
    """Wrong configuration: the current approved option, and this exact definition revision, on a verified build."""
    md = mappings.mapping_dict(db, m)
    if not md["selectable"]:
        raise ValidationError("This assembly option cannot be issued now: " + "; ".join(md["not_selectable_because"]))
    if b.definition_id != m.definition_id:
        raise ValidationError(f"Wrong configuration: build {builds.build_code(b.id)} is not an instance of "
                              f"assembly {md['assembly_code']}")
    if md["effective_revision_id"] != b.revision_id:
        raise ValidationError(f"Wrong configuration: the build is of an older/different revision than the approved "
                              f"revision {md['effective_revision']}; rebuild or verify against the current one")
    if b.status != "VERIFIED":
        raise ValidationError(f"Build {builds.build_code(b.id)} is {b.status}; only a verified build can be issued")


def create_issue(db: Session, actor: User, *, setup_id=None, wo_id=None, line_ids=None, line_mappings=None, mapping_id=None,
                 build_id: int, confirmed_item_ids, scanned_item_ids=None, destination_location_id=None, remarks=None,
                 consumables=None, override_reason=None) -> Issue:
    permissions.require(actor, "issue.create")
    b = builds._get(db, build_id)
    setup, lines = setups.require_issuable(db, setup_id, b, wo_id, line_ids)  # an issue is always FOR a planned setup
    pairs = _resolve_mappings(db, lines, b, mapping_id, line_mappings)
    for m in {m.id: m for _, m in pairs}.values():
        _check_config(db, m, b)
    m = pairs[0][1]  # the first line's operation is the issue's headline operation
    wo_id = ", ".join(dict.fromkeys(ln.wo_id for ln, _ in pairs))

    comps = builds.members(db, b.id)
    comp_ids = [c.item_id for c in comps]

    if scanned_item_ids:
        dup = sorted({i for i in scanned_item_ids if list(scanned_item_ids).count(i) > 1})
        if dup:
            raise ValidationError(f"Duplicate scan of item(s) {dup}; each item is scanned once")
        stray = [i for i in scanned_item_ids if i not in comp_ids]
        if stray:
            raise ValidationError(f"Scanned item(s) {stray} are not fitted in build {builds.build_code(b.id)}")
    confirmed = list(confirmed_item_ids or [])
    if len(set(confirmed)) != len(confirmed):
        raise ValidationError("Duplicate item in the confirmed list")
    if set(confirmed) != set(comp_ids):
        names = {i: db.get(Item, i).display_id for i in set(confirmed) | set(comp_ids)}
        missing = [names[i] for i in comp_ids if i not in confirmed]
        extra = [names[i] for i in confirmed if i not in comp_ids]
        raise ValidationError("The confirmed list does not match the build as it is now"
                              + (f" (not confirmed: {', '.join(missing)})" if missing else "")
                              + (f" (not in build: {', '.join(extra)})" if extra else "")
                              + ". Scan the build again and confirm the shown components")

    for c in comps:
        s = ledger.get_state(db, c.item_id)
        if s["custody"] != "ASSEMBLY_AREA":
            o = open_issue_for_item(db, c.item_id)
            it = db.get(Item, c.item_id)
            where = f" under WO {o[0].wo_id} ({issue_code(o[0].id)}, {o[1].replace('_', ' ').lower()})" if o else ""
            raise ValidationError(f"{it.display_id} is in {s['custody']} custody{where}; it cannot be issued again "
                                  f"until it is returned and cleared")
    problems = builds._problems(db, b)
    if problems:
        raise ValidationError("Build is not ready to issue: " + "; ".join(problems))
    plan = _plan_consumables(db, b.revision_id, consumables, override_reason)
    dest = None
    if destination_location_id:
        dest = config.get_location(db, destination_location_id)
        if not dest.active:
            raise ValidationError("Destination location is inactive")

    event_id = new_event()
    with transaction(db):  # items and consumables: all or nothing
        issue = Issue(wo_id=pairs[0][0].wo_id, setup_id=setup.id, part_id=m.part_id, routing_revision=m.routing_revision,
                      operation_id=m.operation_id, mapping_id=m.id, build_id=b.id, revision_id=b.revision_id,
                      destination_location_id=dest.id if dest else None, issued_by=actor.id, remarks=clean(remarks))
        db.add(issue)
        db.flush()
        for ln, lm in pairs:
            db.add(IssueLine(issue_id=issue.id, setup_line_id=ln.id, mapping_id=lm.id))
        for c in comps:
            s = ledger.get_state(db, c.item_id)
            db.add(IssueItem(issue_id=issue.id, item_id=c.item_id))
            ledger._append(db, c.item_id, "ISSUE", event_id=event_id, prev=s, user=actor, build_id=b.id,
                           issue_id=issue.id, to_condition=s["item_condition"], to_custody="PENDING_HANDOVER",
                           to_location_id=dest.id if dest else s["location_id"],
                           remarks=f"Issued to WO {wo_id}")
        for row in plan:
            db.add(IssueBatch(issue_id=issue.id, batch_id=row["batch"].id, line_id=row["line"]["line_id"],
                              quantity=row["quantity"], basis=row["line"]["basis"], override_reason=row["override"]))
            db.flush()
            batches.issue_to_wo(db, actor, row["batch"].id, row["quantity"], issue_id=issue.id, event_id=event_id,
                                remarks=f"Issued to WO {wo_id}")
        for v in documents.versions_in_force(db, b):
            db.add(IssueDocument(issue_id=issue.id, document_version_id=v.id))
        audit.record(db, actor, "ISSUE", "issue", issue.id,
                     after={"wo_id": wo_id, "setup": setups.setup_code(setup.id), "build": builds.build_code(b.id), "items": len(comps),
                            "operation": m.operation_id,
                            "consumables": [{"batch": r["batch"].batch_code, "quantity": r["quantity"],
                                             "override": r["override"]} for r in plan]})
    return issue


def hand_over(db: Session, actor: User, issue_id: int, *, setup_id, wo_id=None, line_ids=None, line_mappings=None, mapping_id=None,
              consumable_returns=None, consumables=None, override_reason=None, remarks=None) -> Issue:
    """The tooling in production moves on to the NEXT setup without going back to stores. The old issue closes with the
    set handed on, a new issue for the new setup opens (Production confirms receipt again), the old issue's
    consumables are settled and the new setup's are issued, all in one transaction."""
    permissions.require(actor, "issue.create")
    old = _get(db, issue_id)
    states = line_states(db, old.id)
    live = {i: st for i, st in states.items() if st}
    if not live or any(st["state"] != "IN_PRODUCTION" for st in live.values()):
        raise ValidationError(f"{issue_code(old.id)} can only be handed on while every part is in production and acknowledged; "
                              f"return or acknowledge the rest first")
    b = builds._get(db, old.build_id)
    if setup_id and old.setup_id and int(setup_id) == old.setup_id:
        raise ValidationError("That is the setup the tooling is already issued for")
    setup, lines = setups.require_issuable(db, setup_id, b, wo_id, line_ids)
    pairs = _resolve_mappings(db, lines, b, mapping_id, line_mappings)
    for mm in {mm.id: mm for _, mm in pairs}.values():
        _check_config(db, mm, b)
    m = pairs[0][1]
    wo_text_new = ", ".join(dict.fromkeys(ln.wo_id for ln, _ in pairs))
    comps = builds.members(db, b.id)
    if {c.item_id for c in comps} != set(live):
        raise ValidationError("The build no longer matches what was issued; return it and issue it again")
    # the parts are with Production by design, so only condition and completeness are checked here (not custody)
    bad = [f"{db.get(Item, i).display_id} is {ledger.get_state(db, i)['item_condition']}" for i in live
           if ledger.get_state(db, i)["item_condition"] != "SERVICEABLE"]
    if bad or not builds.build_detail(db, b.id)["complete"]:
        raise ValidationError("Build is not ready to hand on: " + ("; ".join(bad) or "it is not a complete assembly"))

    # the old issue's consumables must be settled (unused quantity back to stock, the rest consumed) at this moment
    out_batches = [ib for ib in db.query(IssueBatch).filter(IssueBatch.issue_id == old.id)
                   if (batches.line_state(db, old.id, ib.batch_id) or {}).get("state") == "ISSUED"]
    returns = {int(c["batch_id"]): c for c in (consumable_returns or [])}
    missing = [batches.batch_code(ib.batch_id) for ib in out_batches if ib.batch_id not in returns]
    if missing:
        raise ValidationError(f"Enter the unused quantity of {', '.join(missing)} (0 if none was left): the coating of the "
                              f"previous setup must be settled first")
    if set(returns) - {ib.batch_id for ib in out_batches}:
        raise ValidationError("A consumable batch listed is not outstanding on that issue")
    plan = _plan_consumables(db, b.revision_id, consumables, override_reason)

    event_id = new_event()
    with transaction(db):
        issue = Issue(wo_id=pairs[0][0].wo_id, setup_id=setup.id, part_id=m.part_id, routing_revision=m.routing_revision,
                      operation_id=m.operation_id, mapping_id=m.id, build_id=b.id, revision_id=b.revision_id,
                      issued_by=actor.id, remarks=clean(remarks) or f"Handed on from {issue_code(old.id)}")
        db.add(issue)
        db.flush()
        for ln, lm in pairs:
            db.add(IssueLine(issue_id=issue.id, setup_line_id=ln.id, mapping_id=lm.id))
        for item_id in live:
            s = ledger.get_state(db, item_id)
            ledger._append(db, item_id, "HANDOVER", event_id=event_id, prev=s, user=actor, build_id=b.id, issue_id=old.id,
                           to_condition=s["item_condition"], to_custody=s["custody"], to_location_id=s["location_id"],
                           source="HANDOVER", remarks=f"Handed on to WO {wo_text_new} ({setups.setup_code(setup.id)})")
            after = {**s, "last_seq": s["last_seq"] + 1}
            db.add(IssueItem(issue_id=issue.id, item_id=item_id))
            ledger._append(db, item_id, "ISSUE", event_id=event_id, prev=after, user=actor, build_id=b.id, issue_id=issue.id,
                           to_condition=s["item_condition"], to_custody="PENDING_HANDOVER", to_location_id=s["location_id"],
                           source="HANDOVER", remarks=f"Issued to WO {wo_text_new} by hand-over from {issue_code(old.id)}")
        for ib in out_batches:
            batches.settle_return(db, actor, ib.batch_id, returns[ib.batch_id].get("unused_quantity", 0),
                                  issue_id=old.id, event_id=event_id)
        for row in plan:
            db.add(IssueBatch(issue_id=issue.id, batch_id=row["batch"].id, line_id=row["line"]["line_id"],
                              quantity=row["quantity"], basis=row["line"]["basis"], override_reason=row["override"]))
            db.flush()
            batches.issue_to_wo(db, actor, row["batch"].id, row["quantity"], issue_id=issue.id, event_id=event_id,
                                remarks=f"Issued to WO {wo_text_new}")
        for v in documents.versions_in_force(db, b):
            db.add(IssueDocument(issue_id=issue.id, document_version_id=v.id))
        old.handed_over_at, old.handed_over_to = _now(), issue.id
        audit.record(db, actor, "HANDOVER", "issue", old.id,
                     before={"setup": setups.setup_code(old.setup_id) if old.setup_id else None, "wo_id": wo_text(db, old)},
                     after={"new_issue": issue_code(issue.id), "setup": setups.setup_code(setup.id), "wo_id": wo_text_new,
                            "items": len(live)})
    return issue


def acknowledge_issue(db: Session, actor: User, issue_id: int) -> dict:
    """Production takes receipt, under their own named account."""
    permissions.require(actor, "issue.acknowledge")
    issue = _get(db, issue_id)
    pending = [i for i, st in line_states(db, issue.id).items() if st and st["state"] == "PENDING_HANDOVER"]
    if not pending:
        raise ValidationError("Nothing to acknowledge: no items of this issue are waiting for hand-over")
    event_id = new_event()
    with transaction(db):
        for item_id in pending:
            s = ledger.get_state(db, item_id)
            ledger._append(db, item_id, "ACKNOWLEDGE", event_id=event_id, prev=s, user=actor,
                           build_id=issue.build_id, issue_id=issue.id, to_condition=s["item_condition"],
                           to_custody="PRODUCTION", to_location_id=s["location_id"],
                           remarks=f"Received for WO {wo_text(db, issue)}")
        audit.record(db, actor, "ACKNOWLEDGE", "issue", issue.id, after={"items": len(pending)})
    return {"event_id": event_id, "acknowledged": len(pending)}


def return_items(db: Session, actor: User, issue_id: int, lines: list[dict] | None = None,
                 consumables: list[dict] | None = None) -> dict:
    """Stores records what actually came back. Items not listed stay outstanding. Returned items are
    AWAITING_INSPECTION (HOLD when damaged) and remain unavailable until cleared. For each consumable, the
    verified UNUSED quantity returns to its batch and the rest is recorded as consumed (once only)."""
    permissions.require(actor, "return.receive")
    issue = _get(db, issue_id)
    lines, consumables = list(lines or []), list(consumables or [])
    if not lines and not consumables:
        raise ValidationError("List the items (or consumable batches) that were returned")
    ids = [int(l["item_id"]) for l in lines]
    if len(set(ids)) != len(ids):
        raise ValidationError("An item appears twice in the return")
    bids = [int(c["batch_id"]) for c in consumables]
    if len(set(bids)) != len(bids):
        raise ValidationError("A batch appears twice in the return")
    states = line_states(db, issue.id)
    plan = []
    for l in lines:
        item = db.get(Item, int(l["item_id"]))
        label = item.display_id if item else l["item_id"]
        st = states.get(int(l["item_id"]))
        if item is None or st is None or st["state"] not in OUTSTANDING:
            raise ValidationError(f"{label} is not outstanding on {issue_code(issue.id)}")
        where = l.get("to_location_id") or item.home_location_id  # blank = back to its home bin
        if not where:
            raise ValidationError(f"Give the location where {label} was put (it has no home bin)")
        loc = config.get_location(db, int(where))
        if not loc.active:
            raise ValidationError(f"Location {config.location_path(db, loc.id)} is inactive")
        damaged = bool(l.get("damaged"))
        if damaged and not clean(l.get("remarks")):
            raise ValidationError(f"Describe the damage for {label}")
        plan.append((item, loc, damaged, clean(l.get("remarks"))))
    event_id = new_event()
    with transaction(db):
        for item, loc, damaged, remarks in plan:
            s = ledger.get_state(db, item.id)
            ledger._append(db, item.id, "RETURN", event_id=event_id, prev=s, user=actor, build_id=issue.build_id,
                           issue_id=issue.id, to_condition="HOLD" if damaged else "AWAITING_INSPECTION",
                           to_custody="ASSEMBLY_AREA" if ledger.active_build_id(db, item.id) else "STORES",
                           to_location_id=loc.id,
                           remarks=("DAMAGED: " if damaged else "") + (remarks or f"Returned from WO {wo_text(db, issue)}"))
        settled = []
        for c in consumables:
            row = batches.settle_return(db, actor, int(c["batch_id"]), c.get("unused_quantity", 0),
                                        issue_id=issue.id, event_id=event_id)
            settled.append({"batch": batches.batch_code(row.batch_id), "unused": row.d_usable, "consumed": row.d_consumed})
        audit.record(db, actor, "RETURN", "issue", issue.id,
                     after={"items": [i.display_id for i, *_ in plan], "damaged": [i.display_id for i, _, d, _ in plan if d],
                            "consumables": settled})
    return {"event_id": event_id, "returned": len(plan), "consumables_settled": len(consumables),
            "outstanding": outstanding_names(db, issue.id)}


def outstanding_names(db: Session, issue_id: int) -> list[str]:
    out = [db.get(Item, i).display_id for i, st in line_states(db, issue_id).items() if st and st["state"] in OUTSTANDING]
    for ib in db.query(IssueBatch).filter(IssueBatch.issue_id == issue_id):
        st = batches.line_state(db, issue_id, ib.batch_id)
        if st and st["state"] == "ISSUED":
            out.append(batches.batch_code(ib.batch_id))
    return out


# ---------------------------------------------------------------- views
def _status(item_states: list[str | None], batch_states: list[str | None]) -> str:
    live = [s for s in item_states if s]
    if not live:
        return "VOID"
    out = [s for s in live if s in OUTSTANDING]
    cons_out = any(s == "ISSUED" for s in batch_states)
    if not out and not cons_out:
        return "CLOSED"
    if not out or any(s == "RETURNED" for s in live):
        return "PARTIALLY_RETURNED"
    return "PENDING_HANDOVER" if all(s == "PENDING_HANDOVER" for s in out) else "ACTIVE"


def issue_detail(db: Session, issue_id: int) -> dict:
    i = _get(db, issue_id)
    users = {u.id: u.username for u in db.query(User.id, User.username)}
    locs = {l["id"]: l["path"] for l in config.list_locations(db)}
    states = line_states(db, i.id)
    lines = []
    for item_id, st in states.items():
        it = db.get(Item, item_id)
        cur = ledger.get_state(db, item_id)
        lines.append({"item_id": it.id, "display_id": it.display_id, "system_id": it.system_id,
                      "design_code": db.get(ElementDesign, it.design_id).code,
                      "design_revision": db.get(DesignRevision, it.design_revision_id).revision,
                      "state": st["state"] if st else "VOID", "since": st["since"] if st else None,
                      "by": users.get(st["by"]) if st else None, "condition": cur["item_condition"],
                      "custody": cur["custody"], "location": locs.get(cur["location_id"])})
    blines = []
    for ib in db.query(IssueBatch).filter(IssueBatch.issue_id == i.id).order_by(IssueBatch.id):
        batch = db.get(ConsumableBatch, ib.batch_id)
        st = batches.line_state(db, i.id, ib.batch_id)
        blines.append({"batch_id": batch.id, "code": batch.batch_code, "design_code": db.get(ElementDesign, batch.design_id).code,
                       "uom": batch.uom, "quantity": ib.quantity, "basis": ib.basis, "override_reason": ib.override_reason,
                       "state": st["state"] if st else "VOID", "unused": st and st["unused"], "consumed": st and st["consumed"],
                       "since": st and st["since"], "by": users.get(st["by"]) if st else None})
    receivers = [l for l in lines if l["state"] in ("IN_PRODUCTION",)]
    b = builds._get(db, i.build_id)
    m = db.get(MeMapping, i.mapping_id)
    return {
        "id": i.id, "code": issue_code(i.id), "wo_id": wo_text(db, i), "part_id": i.part_id,
        "setup": _setup_ref(db, i.setup_id), "wo_ids": wo_ids(db, i), "setup_lines": _issue_lines(db, i.id),
        "handed_over_to": ({"id": i.handed_over_to, "code": issue_code(i.handed_over_to)} if i.handed_over_to else None),
        "handed_over_at": i.handed_over_at,
        "routing_revision": i.routing_revision, "operation_id": i.operation_id, "mapping_id": i.mapping_id,
        "build": {"id": b.id, "code": builds.build_code(b.id)}, "issued_by": users.get(i.issued_by),
        "issued_at": i.issued_at, "destination": locs.get(i.destination_location_id), "remarks": i.remarks,
        "status": _status([l["state"] if l["state"] != "VOID" else None for l in lines], [x["state"] for x in blines]),
        "outstanding": [l["display_id"] for l in lines if l["state"] in OUTSTANDING]
                       + [x["code"] for x in blines if x["state"] == "ISSUED"],
        "received_by": sorted({l["by"] for l in receivers}), "lines": lines, "consumables": blines,
        "documents": _documents_used(db, i.id),
        "pouring_weight_kg": m.pouring_weight_kg if m else None,
    }


def _issue_lines(db: Session, issue_id: int) -> list[dict]:
    out = []
    for il in db.query(IssueLine).filter(IssueLine.issue_id == issue_id).order_by(IssueLine.id):
        ln, m = db.get(SetupLine, il.setup_line_id), db.get(MeMapping, il.mapping_id)
        out.append({"line_id": ln.id, "wo_id": ln.wo_id, "part_id": ln.part_id, "tool_description": ln.tool_description,
                    "family": ln.family, "grade": ln.grade, "operation_id": m.operation_id, "routing_revision": m.routing_revision,
                    "mapping_id": m.id})
    return out


def _setup_ref(db: Session, setup_id) -> dict | None:
    s = db.get(setups.Setup, setup_id) if setup_id else None
    if s is None:
        return None
    first = next(iter(setups.lines_of(db, s.id)), None)
    return {"id": s.id, "code": setups.setup_code(s.id), "name": s.name,
            "tool_description": first.tool_description if first else None, "family": first.family if first else None,
            "grade": first.grade if first else None}


def _documents_used(db: Session, issue_id: int) -> list[dict]:
    """The document versions this issue was made with, next to what is approved NOW (for new work)."""
    out = []
    for row in db.query(IssueDocument).filter(IssueDocument.issue_id == issue_id).order_by(IssueDocument.id):
        v = db.get(DocumentVersion, row.document_version_id)
        d = db.get(Document, v.document_id)
        cur = documents.current_version(db, d.id)
        out.append({"doc_key": d.doc_key, "title": d.title, "revision_used": v.revision, "version_id": v.id,
                    "current_revision": cur.revision if cur else None, "superseded": bool(cur and cur.id != v.id)})
    return out


def replace_in_production(db: Session, actor: User, issue_id: int, outgoing_item_id: int, incoming_item_id: int,
                          *, reason) -> dict:
    """Swap one component of an issued build. One explicit remove/add event; compatibility is verified; the build
    needs verifying again when it is back. The outgoing item stays issued to the WO (it must still be returned and
    inspected). The incoming item is handed over like any other and Production acknowledges it. Production usage
    before and after the change is attributed by membership time."""
    permissions.require(actor, "issue.create")
    reason = require_reason(reason, "replacing a component in production")
    issue = _get(db, issue_id)
    b = builds._get(db, issue.build_id)
    st = line_states(db, issue.id).get(outgoing_item_id)
    if st is None or st["state"] not in OUTSTANDING:
        raise ValidationError("The outgoing item is not outstanding on this issue")
    comp = db.query(BuildComponent).filter_by(build_id=b.id, item_id=outgoing_item_id, removed_at=None).first()
    if comp is None:
        raise ValidationError("The outgoing item is not a current component of this build")
    incoming = db.get(Item, incoming_item_id)
    if incoming is None:
        raise NotFoundError(f"Item {incoming_item_id} not found")
    if not incoming.active:
        raise ValidationError(f"{incoming.display_id} is retired")
    other = ledger.active_build_id(db, incoming.id)
    if other:
        raise ValidationError(f"{incoming.display_id} is already fitted in build {builds.build_code(other)}")
    s_in = ledger.get_state(db, incoming.id)
    if s_in["custody"] != "STORES":
        raise ValidationError(f"{incoming.display_id} is in {s_in['custody']} custody (not in stores)")
    if s_in["item_condition"] != "SERVICEABLE":
        raise ValidationError(f"{incoming.display_id} is {s_in['item_condition']}; only serviceable items can be fitted")
    lines = assemblies.matching_lines(db, b.revision_id)
    others = [builds._cand(db, c.item_id) for c in builds.members(db, b.id) if c.item_id != outgoing_item_id]
    new = Candidate(incoming.id, incoming.design_id, incoming.design_revision_id)
    if match(lines, others + [new])[1]:
        raise ValidationError(builds._why_not_fittable(db, b, incoming, lines, others))

    event_id = new_event()
    with transaction(db):
        s_out = ledger.get_state(db, outgoing_item_id)
        comp.removed_at, comp.removed_by, comp.removal_reason, comp.remove_event_id = _now(), actor.id, reason, event_id
        ledger._append(db, outgoing_item_id, "REMOVE", event_id=event_id, prev=s_out, user=actor, build_id=b.id,
                       issue_id=issue.id, to_condition=s_out["item_condition"], to_custody=s_out["custody"],
                       to_location_id=s_out["location_id"], remarks=f"Replaced in production: {reason}")
        loc = s_out["location_id"]
        db.add(BuildComponent(build_id=b.id, item_id=incoming.id, fitted_by=actor.id, fit_event_id=event_id))
        ledger._append(db, incoming.id, "FIT", event_id=event_id, prev=s_in, user=actor, build_id=b.id,
                       to_condition=s_in["item_condition"], to_custody="ASSEMBLY_AREA", to_location_id=loc,
                       remarks=f"Fitted to {builds.build_code(b.id)} as a replacement")
        after_fit = {**s_in, "last_seq": s_in["last_seq"] + 1, "custody": "ASSEMBLY_AREA", "location_id": loc}
        db.add(IssueItem(issue_id=issue.id, item_id=incoming.id))
        ledger._append(db, incoming.id, "ISSUE", event_id=event_id, prev=after_fit, user=actor, build_id=b.id,
                       issue_id=issue.id, to_condition=s_in["item_condition"], to_custody="PENDING_HANDOVER",
                       to_location_id=loc, remarks=f"Issued to WO {wo_text(db, issue)} as a replacement")
        builds._touch(b)
        audit.record(db, actor, "REPLACE_IN_PRODUCTION", "issue", issue.id,
                     before={"item": db.get(Item, outgoing_item_id).display_id}, after={"item": incoming.display_id},
                     reason=reason)
    return {"event_id": event_id, "outgoing": db.get(Item, outgoing_item_id).display_id, "incoming": incoming.display_id,
            "build_status": b.status}


def list_issues(db: Session, *, wo_id=None, status=None, build_id=None, item_id=None, limit=100, offset=0):
    q = db.query(Issue)
    if wo_id:
        by_line = [r[0] for r in db.query(IssueLine.issue_id).join(SetupLine, SetupLine.id == IssueLine.setup_line_id)
                   .filter(SetupLine.wo_id == wo_id.strip())]
        q = q.filter((Issue.wo_id == wo_id.strip()) | Issue.id.in_(by_line or [0]))
    if build_id:
        q = q.filter(Issue.build_id == build_id)
    if item_id:
        q = q.filter(Issue.id.in_([ii.issue_id for ii in db.query(IssueItem).filter(IssueItem.item_id == item_id)]))
    if status == "open":
        q = q.filter(Issue.id.in_(outstanding_issue_ids(db) or [0]))
    rows = [issue_detail(db, i.id) for i in q.order_by(Issue.id.desc())]
    if status == "closed":
        rows = [r for r in rows if r["status"] in ("CLOSED", "VOID")]
    total = len(rows)
    page = rows[offset: offset + min(limit, 500)]
    return total, [{k: r[k] for k in ("id", "code", "wo_id", "part_id", "operation_id", "build", "issued_by", "setup",
                                      "issued_at", "status", "outstanding")} for r in page]
