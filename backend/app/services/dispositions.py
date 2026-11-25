"""Held items: repair, further inspection or proposed scrap; repair records; revision rework; scrap approval.

  * The tooling person decides for a HELD item (findings and the responsible person are mandatory).
  * REPAIR sends it to repair (custody REPAIR) with a repair job; completing the repair puts it back to AWAITING
    INSPECTION. It never becomes serviceable by itself: inspection passes, then Engineering approves release.
  * PROPOSE_SCRAP keeps the item on HOLD until the designated scrap approver decides.
  * Reworking an item to a new design revision keeps its identity and history, is approved by Engineering, and
    flags every open WO that counted on the item for recheck.
  * Every condition change made here is written in the same transaction as the record it belongs to."""
from sqlalchemy.orm import Session

from ..models import (Disposition, DesignRevision, ElementDesign, Inspection, Item, ItemRevisionChange, RepairJob,
                      ScrapProposal, User, _now)
from . import assemblies, audit, designs, ledger, permissions, recheck
from .common import clean, new_event, require_reason, transaction
from .errors import NotFoundError, ValidationError
from .matching import Candidate, accepts


def _item(db: Session, item_id: int) -> Item:
    it = db.get(Item, item_id)
    if it is None:
        raise NotFoundError(f"Item {item_id} not found")
    return it


def decide(db: Session, actor: User, item_id: int, decision: str, *, findings, responsible, inspection_id=None) -> Disposition:
    permissions.require(actor, "disposition.decide")
    if decision == "PROPOSE_SCRAP":
        permissions.require(actor, "scrap.propose")
    if decision not in ("REPAIR", "FURTHER_INSPECTION", "PROPOSE_SCRAP"):
        raise ValidationError("decision must be REPAIR, FURTHER_INSPECTION or PROPOSE_SCRAP")
    findings, responsible = clean(findings), clean(responsible)
    if not findings:
        raise ValidationError("Record the findings")
    if not responsible:
        raise ValidationError("Assign responsibility (who owns this item's next step)")
    item = _item(db, item_id)
    s = ledger.get_state(db, item.id)
    if s["item_condition"] != "HOLD":
        raise ValidationError(f"{item.display_id} is {s['item_condition']}; only held items get a disposition")
    if inspection_id is not None:
        insp = db.get(Inspection, inspection_id)
        if insp is None or insp.item_id != item.id:
            raise ValidationError("That inspection does not belong to this item")
    event_id = new_event()
    with transaction(db):
        d = Disposition(item_id=item.id, inspection_id=inspection_id, decision=decision, findings=findings,
                        responsible=responsible, decided_by=actor.id)
        db.add(d)
        db.flush()
        if decision == "REPAIR":
            ledger.apply_condition(db, actor, [item], "UNDER_REPAIR", f"Repair: {findings}", event_id, source="DISPOSITION")
            db.add(RepairJob(item_id=item.id, disposition_id=d.id, opened_by=actor.id))
        elif decision == "FURTHER_INSPECTION":
            ledger.apply_condition(db, actor, [item], "AWAITING_INSPECTION", f"Further inspection: {findings}", event_id,
                                   source="DISPOSITION")
        else:
            db.add(ScrapProposal(item_id=item.id, disposition_id=d.id, findings=findings, proposed_by=actor.id))
        audit.record(db, actor, f"DISPOSITION_{decision}", "item", item.id,
                     after={"findings": findings, "responsible": responsible}, reason=findings)
    return d


def _open_repair(db: Session, repair_id: int) -> RepairJob:
    r = db.get(RepairJob, repair_id)
    if r is None:
        raise NotFoundError(f"Repair {repair_id} not found")
    if r.status != "OPEN":
        raise ValidationError("This repair is already completed")
    return r


def complete_repair(db: Session, actor: User, repair_id: int, *, work_done, measurements=None) -> RepairJob:
    """Record the work done; the item goes back to AWAITING INSPECTION (not to serviceable)."""
    permissions.require(actor, "repair.complete")
    r = _open_repair(db, repair_id)
    work = clean(work_done)
    if not work:
        raise ValidationError("Describe the repair work done")
    item = _item(db, r.item_id)
    s = ledger.get_state(db, item.id)
    if s["item_condition"] != "UNDER_REPAIR":
        raise ValidationError(f"{item.display_id} is {s['item_condition']}, not under repair")
    event_id = new_event()
    with transaction(db):
        ledger.apply_condition(db, actor, [item], "AWAITING_INSPECTION", f"Repair completed: {work}", event_id, source="REPAIR")
        r.status, r.work_done, r.measurements = "COMPLETED", work, clean(measurements)
        r.completed_by, r.completed_at = actor.id, _now()
        audit.record(db, actor, "COMPLETE_REPAIR", "item", item.id, after={"repair_id": r.id}, reason=work)
    return r


def accepting_assemblies(db: Session, design_id: int, revision_id: int) -> list[str]:
    """Approved assemblies that would accept an item of this design/revision: the compatibility recheck."""
    from ..models import AssemblyDefinition
    out = []
    for d in db.query(AssemblyDefinition).filter(AssemblyDefinition.active.is_(True)).order_by(AssemblyDefinition.code):
        rev = assemblies.current_approved(db, d.id)
        if rev is None:
            continue
        cand = Candidate(0, design_id, revision_id)
        if any(accepts(ln, cand) for ln in assemblies.matching_lines(db, rev.id)):
            out.append(d.code)
    return out


def rework_revision(db: Session, actor: User, item_id: int, new_revision_id: int, *, reason, repair_id=None) -> dict:
    """Move one item to a new APPROVED design revision during its repair. Identity and history are preserved;
    old/new revision, reason, date and approver are recorded; assembly compatibility is rechecked."""
    permissions.require(actor, "rework.revision")
    reason = require_reason(reason, "a revision change")
    item = _item(db, item_id)
    new = designs.get_revision(db, new_revision_id)
    if new.design_id != item.design_id:
        raise ValidationError("That revision belongs to a different design")
    if new.id == item.design_revision_id:
        raise ValidationError("The item is already at that revision")
    if new.status != "APPROVED":
        raise ValidationError(f"Revision {new.revision} is {new.status}; rework only to the current approved revision")
    s = ledger.get_state(db, item.id)
    if s["item_condition"] != "UNDER_REPAIR":
        raise ValidationError(f"{item.display_id} is {s['item_condition']}; a revision change is made while it is under repair")
    old = designs.get_revision(db, item.design_revision_id)
    with transaction(db):
        db.add(ItemRevisionChange(item_id=item.id, old_revision_id=old.id, new_revision_id=new.id, reason=reason,
                                  repair_id=repair_id, changed_by=actor.id))
        item.design_revision_id = new.id
        audit.record(db, actor, "REWORK_REVISION", "item", item.id, before={"revision": old.revision},
                     after={"revision": new.revision}, reason=reason)
        recheck.flag(db, actor, f"{item.display_id} reworked from revision {old.revision} to {new.revision}",
                     item_ids=[item.id])
    return {"item_id": item.id, "old_revision": old.revision, "new_revision": new.revision,
            "accepted_by_assemblies": accepting_assemblies(db, item.design_id, new.id)}


def decide_scrap(db: Session, actor: User, proposal_id: int, *, approve: bool, note=None) -> ScrapProposal:
    """The designated scrap approver authorises (or refuses) scrapping. Until then the item stays on HOLD."""
    permissions.require(actor, "condition.scrap")
    p = db.get(ScrapProposal, proposal_id)
    if p is None:
        raise NotFoundError(f"Scrap proposal {proposal_id} not found")
    if p.status != "PENDING":
        raise ValidationError(f"This proposal is already {p.status}")
    note = clean(note)
    if not approve and not note:
        raise ValidationError("Give the reason for refusing the scrap")
    item = _item(db, p.item_id)
    event_id = new_event()
    with transaction(db):
        p.status, p.decided_by, p.decided_at, p.decision_note = ("APPROVED" if approve else "REJECTED"), actor.id, _now(), note
        if approve:
            ledger.apply_condition(db, actor, [item], "SCRAPPED", f"Scrap approved: {note or p.findings}", event_id, source="SCRAP")
        audit.record(db, actor, "SCRAP_APPROVED" if approve else "SCRAP_REFUSED", "item", item.id,
                     after={"proposal": p.id}, reason=note or p.findings)
    return p


# ---------------------------------------------------------------- views
def _users(db: Session) -> dict:
    return {u.id: u.username for u in db.query(User.id, User.username)}


def held_queue(db: Session) -> dict:
    from sqlalchemy import text
    users = _users(db)
    holds = db.execute(text(
        "SELECT i.id, i.display_id, i.system_id, d.code AS design_code, s.since FROM items i "
        "JOIN item_state s ON s.item_id = i.id JOIN element_designs d ON d.id = i.design_id "
        "WHERE i.active = 1 AND s.item_condition = 'HOLD' ORDER BY s.since")).mappings().all()
    pending = {p.item_id: p for p in db.query(ScrapProposal).filter_by(status="PENDING")}
    last = {}
    for d in db.query(Disposition).order_by(Disposition.id):
        last[d.item_id] = d
    out_holds = []
    for h in holds:
        d = last.get(h["id"])
        out_holds.append({**dict(h), "pending_scrap_proposal_id": pending[h["id"]].id if h["id"] in pending else None,
                          "last_decision": d.decision if d else None, "last_findings": d.findings if d else None})
    repairs = []
    for r in db.query(RepairJob).filter_by(status="OPEN").order_by(RepairJob.id):
        it = _item(db, r.item_id)
        d = db.get(Disposition, r.disposition_id)
        repairs.append({"id": r.id, "item_id": it.id, "display_id": it.display_id, "findings": d.findings,
                        "responsible": d.responsible, "opened_by": users.get(r.opened_by), "opened_at": r.opened_at,
                        "design_id": it.design_id, "design_revision_id": it.design_revision_id})
    scrap = []
    for p in pending.values():
        it = _item(db, p.item_id)
        d = db.get(Disposition, p.disposition_id)
        scrap.append({"id": p.id, "item_id": it.id, "display_id": it.display_id, "findings": p.findings,
                      "responsible": d.responsible, "proposed_by": users.get(p.proposed_by), "proposed_at": p.proposed_at})
    return {"holds": out_holds, "open_repairs": repairs, "pending_scrap": scrap}


def quality_counts(db: Session) -> dict:
    return {"open_inspections": db.query(Inspection).filter_by(status="OPEN").count(),
            "open_repairs": db.query(RepairJob).filter_by(status="OPEN").count(),
            "pending_scrap_proposals": db.query(ScrapProposal).filter_by(status="PENDING").count()}


def item_history(db: Session, item_id: int) -> dict:
    users = _users(db)
    insp = [{"id": i.id, "status": i.status, "outcome": i.outcome, "started_by": users.get(i.started_by),
             "started_at": i.started_at, "completed_at": i.completed_at}
            for i in db.query(Inspection).filter_by(item_id=item_id).order_by(Inspection.id.desc())]
    disp = [{"id": d.id, "decision": d.decision, "findings": d.findings, "responsible": d.responsible,
             "by": users.get(d.decided_by), "at": d.decided_at}
            for d in db.query(Disposition).filter_by(item_id=item_id).order_by(Disposition.id.desc())]
    reps = [{"id": r.id, "status": r.status, "work_done": r.work_done, "measurements": r.measurements,
             "opened_at": r.opened_at, "completed_at": r.completed_at, "completed_by": users.get(r.completed_by)}
            for r in db.query(RepairJob).filter_by(item_id=item_id).order_by(RepairJob.id.desc())]
    revs = [{"id": c.id, "old": designs.get_revision(db, c.old_revision_id).revision,
             "new": designs.get_revision(db, c.new_revision_id).revision, "reason": c.reason,
             "approved_by": users.get(c.changed_by), "at": c.changed_at}
            for c in db.query(ItemRevisionChange).filter_by(item_id=item_id).order_by(ItemRevisionChange.id.desc())]
    scrap = [{"id": p.id, "status": p.status, "findings": p.findings, "note": p.decision_note}
             for p in db.query(ScrapProposal).filter_by(item_id=item_id).order_by(ScrapProposal.id.desc())]
    return {"inspections": insp, "dispositions": disp, "repairs": reps, "revision_changes": revs, "scrap_proposals": scrap}
