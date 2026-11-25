import pytest

from app.models import AuditLog, Ledger, RepairJob, ScrapProposal
from app.services import (assemblies, builds, designs, dispositions, inspections, items, ledger, mappings, readiness)
from app.services.errors import ConflictError, PermissionDenied, ValidationError
from tests.conftest import clear_items


def held(db, people, make_item, **kw):
    it = make_item(**kw)
    ledger.set_condition(db, people["tooling"], [it.id], "HOLD", reason="crack suspected")
    return it


def decide(db, people, it, decision, who="tooling", findings="cracked at seat", responsible="Tooling shop", **kw):
    return dispositions.decide(db, people[who], it.id, decision, findings=findings, responsible=responsible, **kw)


def state(db, it):
    return ledger.get_state(db, it.id)


# ---- decisions ------------------------------------------------------------------------------------------
def test_only_held_items_get_a_decision_with_findings_and_a_responsible_person(db, people, make_item):
    ok = make_item(legacy="s1")
    with pytest.raises(ValidationError) as e:
        decide(db, people, ok, "REPAIR")
    assert "only held items" in e.value.message
    it = held(db, people, make_item, legacy="h1", bin_index=1)
    with pytest.raises(ValidationError):
        decide(db, people, it, "REPAIR", findings=" ")
    with pytest.raises(ValidationError):
        decide(db, people, it, "REPAIR", responsible=" ")
    with pytest.raises(ValidationError):
        decide(db, people, it, "MELT_DOWN")
    for who in ("stores", "eng", "scrap"):
        with pytest.raises(PermissionDenied):
            decide(db, people, it, "REPAIR", who=who)
    assert state(db, it)["item_condition"] == "HOLD" and db.query(RepairJob).count() == 0


def test_repair_decision_sends_the_item_to_repair_with_a_job(db, people, make_item):
    it = held(db, people, make_item)
    d = decide(db, people, it, "REPAIR", responsible="Mr Rao, tool room")
    s = state(db, it)
    assert (s["item_condition"], s["custody"]) == ("UNDER_REPAIR", "REPAIR")
    (job,) = db.query(RepairJob).all()
    assert job.status == "OPEN" and job.disposition_id == d.id and d.responsible == "Mr Rao, tool room"
    row = db.query(Ledger).filter_by(item_id=it.id).order_by(Ledger.txn_id.desc()).first()
    assert row.source == "DISPOSITION"
    with pytest.raises(ValidationError):  # written by the workflow
        ledger.reverse(db, people["tooling"], row.txn_id, reason="oops")


def test_a_fitted_item_must_be_taken_out_of_its_build_before_repair(db, people, kit, world):
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    builds.fit_item(db, people["stores"], b.id, kit["items"]["d1"])
    ledger.set_condition(db, people["tooling"], [kit["items"]["d1"]], "HOLD", reason="crack")
    it = db.get(items.Item, kit["items"]["d1"])
    with pytest.raises(ValidationError) as e:
        dispositions.decide(db, people["tooling"], it.id, "REPAIR", findings="crack", responsible="tool room")
    assert f"BLD-{b.id:06d}" in e.value.message
    assert db.query(RepairJob).count() == 0 and db.query(AuditLog).filter_by(action="DISPOSITION_REPAIR").count() == 0
    builds.remove_item(db, people["stores"], b.id, it.id, world["bins"][1], reason="for repair")
    dispositions.decide(db, people["tooling"], it.id, "REPAIR", findings="crack", responsible="tool room")
    assert state(db, it)["item_condition"] == "UNDER_REPAIR"


def test_further_inspection_decision_returns_the_item_to_the_inspection_queue(db, people, make_item):
    it = held(db, people, make_item)
    decide(db, people, it, "FURTHER_INSPECTION")
    assert state(db, it)["item_condition"] == "AWAITING_INSPECTION"
    assert inspections.awaiting_queue(db)[0]["id"] == it.id


# ---- repair ----------------------------------------------------------------------------------------------
def test_completing_a_repair_leads_to_inspection_never_straight_to_serviceable(db, people, make_item):
    it = held(db, people, make_item)
    decide(db, people, it, "REPAIR")
    job = db.query(RepairJob).one()
    for who in ("stores", "eng"):
        with pytest.raises(PermissionDenied):
            dispositions.complete_repair(db, people[who], job.id, work_done="re-bored")
    with pytest.raises(ValidationError):
        dispositions.complete_repair(db, people["tooling"], job.id, work_done=" ")
    dispositions.complete_repair(db, people["tooling"], job.id, work_done="re-bored to 270.0", measurements="bore 270.02 mm")
    assert state(db, it)["item_condition"] == "AWAITING_INSPECTION" and state(db, it)["custody"] == "STORES"
    job = db.get(RepairJob, job.id)
    assert (job.status, job.work_done, job.measurements) == ("COMPLETED", "re-bored to 270.0", "bore 270.02 mm")
    with pytest.raises(ValidationError):  # once only
        dispositions.complete_repair(db, people["tooling"], job.id, work_done="again")
    with pytest.raises(ValidationError):  # and Engineering cannot skip the inspection
        ledger.set_condition(db, people["eng"], [it.id], "SERVICEABLE", reason="repair looks fine")


# ---- scrap -----------------------------------------------------------------------------------------------------
def test_proposed_scrap_stays_held_until_the_designated_approver_acts(db, people, make_item):
    it = held(db, people, make_item)
    decide(db, people, it, "PROPOSE_SCRAP", findings="beyond repair: seat cracked through")
    (p,) = db.query(ScrapProposal).all()
    assert p.status == "PENDING" and state(db, it)["item_condition"] == "HOLD"  # remains held pending approval
    with pytest.raises(Exception):  # only one pending proposal per item (database-enforced)
        decide(db, people, it, "PROPOSE_SCRAP")
    db.rollback()
    for who in ("tooling", "eng", "stores"):
        with pytest.raises(PermissionDenied):
            dispositions.decide_scrap(db, people[who], p.id, approve=True)
    assert state(db, it)["item_condition"] == "HOLD"
    dispositions.decide_scrap(db, people["scrap"], p.id, approve=True, note="approved by works manager")
    assert state(db, it)["item_condition"] == "SCRAPPED"
    assert db.get(ScrapProposal, p.id).status == "APPROVED"
    row = db.query(Ledger).filter_by(item_id=it.id).order_by(Ledger.txn_id.desc()).first()
    assert row.source == "SCRAP" and row.user_id == people["scrap"].id
    with pytest.raises(ValidationError):
        dispositions.decide_scrap(db, people["scrap"], p.id, approve=True)  # already decided


def test_refused_scrap_keeps_the_item_on_hold_and_needs_a_reason(db, people, make_item):
    it = held(db, people, make_item)
    decide(db, people, it, "PROPOSE_SCRAP")
    p = db.query(ScrapProposal).one()
    with pytest.raises(ValidationError):
        dispositions.decide_scrap(db, people["scrap"], p.id, approve=False, note=" ")
    dispositions.decide_scrap(db, people["scrap"], p.id, approve=False, note="repairable; weld and re-machine")
    assert state(db, it)["item_condition"] == "HOLD" and db.get(ScrapProposal, p.id).status == "REJECTED"
    decide(db, people, it, "REPAIR")  # the tooling person can now choose repair instead
    assert state(db, it)["item_condition"] == "UNDER_REPAIR"


# ---- A10: repair an item and change its revision -------------------------------------------------------------------
def test_repair_with_revision_change_keeps_identity_and_history_and_needs_engineering_release(db, people, kit, world):
    eng, d1 = people["eng"], kit["items"]["d1"]
    it = db.get(items.Item, d1)
    system_id, display_id = it.system_id, it.display_id
    # a WO was cleared on this item, and the assembly only accepts the CURRENT approved die revision
    m = mappings.create_mapping(db, eng, part_id="PN-1", routing_revision="R1", operation_id="OP10", definition_id=kit["asm"], selected=True)
    cleared = readiness.run_check(db, people["prod"], wo_id="WO-7", part_id="PN-1", routing_revision="R1", operation_id="OP10")
    assert cleared["result"] == "READY" and m.id
    # Engineering revises the die design: rev B is approved, rev A items no longer fit by default
    designs.new_revision(db, eng, kit["die"], reason="bore enlarged")
    rev_b = designs.revisions(db, kit["die"])[-1]
    designs.approve_revision(db, eng, rev_b.id)
    rev_a_id = it.design_revision_id
    assert "T211" not in dispositions.accepting_assemblies(db, kit["die"], rev_a_id)
    # hold -> repair -> rework to B -> complete repair -> inspect -> release
    ledger.set_condition(db, people["tooling"], [d1], "HOLD", reason="to be reworked to rev B")
    dispositions.decide(db, people["tooling"], d1, "REPAIR", findings="re-bore to rev B", responsible="tool room")
    repair = db.query(RepairJob).one()
    for who in ("tooling", "stores"):
        with pytest.raises(PermissionDenied):
            dispositions.rework_revision(db, people[who], d1, rev_b.id, reason="rev B")
    with pytest.raises(ValidationError):
        dispositions.rework_revision(db, eng, d1, rev_b.id, reason=" ")
    res = dispositions.rework_revision(db, eng, d1, rev_b.id, reason="re-bored to rev B drawing", repair_id=repair.id)
    assert (res["old_revision"], res["new_revision"]) == ("A", "B") and "T211" in res["accepted_by_assemblies"]
    it = db.get(items.Item, d1)
    assert it.system_id == system_id and it.display_id == display_id and it.design_revision_id == rev_b.id  # identity kept
    from app.services import recheck
    assert "WO-7" in {c.wo_id for c in recheck.flagged(db)}  # the WO cleared on this item is flagged for recheck
    dispositions.complete_repair(db, people["tooling"], repair.id, work_done="re-bored", measurements="270.01 mm")
    with pytest.raises(ValidationError):  # Engineering cannot release without an inspection
        ledger.set_condition(db, eng, [d1], "SERVICEABLE", reason="reworked")
    clear_items(db, people, d1)
    assert state(db, it)["item_condition"] == "SERVICEABLE"
    # history is preserved: revision change, repair, inspection and the full ledger
    h = dispositions.item_history(db, d1)
    assert h["revision_changes"][0]["old"] == "A" and h["revision_changes"][0]["new"] == "B"
    assert h["revision_changes"][0]["approved_by"] == "eng" and h["repairs"][0]["work_done"] == "re-bored"
    assert h["inspections"][0]["outcome"] == "PASS" and h["dispositions"][0]["decision"] == "REPAIR"
    # and the reworked item fits the current assembly again
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    builds.fit_item(db, people["stores"], b.id, d1)


def test_revision_rework_rules(db, people, kit):
    eng, d1 = people["eng"], kit["items"]["d1"]
    it = db.get(items.Item, d1)
    designs.new_revision(db, eng, kit["die"], reason="r")
    rev_b = designs.revisions(db, kit["die"])[-1]
    designs.approve_revision(db, eng, rev_b.id)
    with pytest.raises(ValidationError) as e:  # not under repair
        dispositions.rework_revision(db, eng, d1, rev_b.id, reason="x")
    assert "under repair" in e.value.message
    designs.new_revision(db, eng, kit["die"], reason="next")
    draft = designs.revisions(db, kit["die"])[-1]
    ledger.set_condition(db, people["tooling"], [d1], "HOLD", reason="x")
    dispositions.decide(db, people["tooling"], d1, "REPAIR", findings="f", responsible="r")
    with pytest.raises(ValidationError) as e:  # a draft revision is not for new work
        dispositions.rework_revision(db, eng, d1, draft.id, reason="x")
    assert "DRAFT" in e.value.message
    with pytest.raises(ValidationError):
        dispositions.rework_revision(db, eng, d1, it.design_revision_id, reason="x")  # same revision
    with pytest.raises(ValidationError):  # a revision of another design
        dispositions.rework_revision(db, eng, d1, designs.revisions(db, kit["pat"])[0].id, reason="x")


# ---- queue & audit --------------------------------------------------------------------------------------------------------
def test_held_queue_lists_holds_repairs_and_pending_scrap(db, people, make_item):
    a = held(db, people, make_item, legacy="a1")
    b = held(db, people, make_item, legacy="b1", bin_index=1)
    c = held(db, people, make_item, legacy="c1", bin_index=2)
    decide(db, people, b, "REPAIR")
    decide(db, people, c, "PROPOSE_SCRAP", findings="seat cracked through")
    q = dispositions.held_queue(db)
    assert {h["display_id"] for h in q["holds"]} == {"A1", "C1"}  # b is under repair now
    assert [r["display_id"] for r in q["open_repairs"]] == ["B1"] and q["open_repairs"][0]["responsible"] == "Tooling shop"
    assert [s["display_id"] for s in q["pending_scrap"]] == ["C1"]
    assert dispositions.quality_counts(db) == {"open_inspections": 0, "open_repairs": 1, "pending_scrap_proposals": 1}
    assert a.id


def test_workflow_actions_are_audited_with_reason(db, people, make_item):
    it = held(db, people, make_item)
    decide(db, people, it, "REPAIR", findings="seat cracked")
    job = db.query(RepairJob).one()
    dispositions.complete_repair(db, people["tooling"], job.id, work_done="welded and re-machined")
    acts = {(r.action, r.username): r.reason for r in db.query(AuditLog).filter_by(entity_type="item", entity_id=str(it.id))}
    assert acts[("DISPOSITION_REPAIR", "tooling")] == "seat cracked"
    assert acts[("COMPLETE_REPAIR", "tooling")] == "welded and re-machined"
    assert ("SET_CONDITION", "tooling") in acts
    assert ConflictError and assemblies  # (imports kept for readability of the suite)
