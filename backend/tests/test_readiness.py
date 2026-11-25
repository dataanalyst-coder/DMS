import pytest

from app.models import Ledger, ReadinessCheck
from app.services import assemblies, builds, designs, issues, items, ledger, mappings, readiness, recheck
from app.services.errors import PermissionDenied, ValidationError
from tests.conftest import clear_items, mk_setup, setup_body

WO = dict(wo_id="WO-1", part_id="PN-1", routing_revision="R1", operation_id="OP10")


@pytest.fixture
def routed(db, people, kit):
    """PN-1 / R1 / OP10 is mapped to T211 (selected)."""
    m = mappings.create_mapping(db, people["eng"], part_id="PN-1", routing_revision="R1", operation_id="OP10",
                                definition_id=kit["asm"], selected=True)
    kit["mapping"] = m.id
    return kit


def check(db, people, who="prod", **kw):
    return readiness.run_check(db, people[who], **{**WO, **kw})


def hold(db, people, kit, *keys):
    for k in keys:
        ledger.set_condition(db, people["tooling"], [kit["items"][k]], "HOLD", reason="test hold")


def codes(res):
    return {r["code"] for r in res["reasons"]}


# ---- A04: ready when parts exist, even unassembled or issued; nothing reserved ---------------
def test_ready_when_serviceable_parts_exist_unassembled_and_nothing_is_reserved(db, people, routed):
    before = db.query(Ledger).count()
    res = check(db, people)
    assert res["result"] == "READY" and res["can_release"] and res["shortages"] == [] and res["reasons"] == []
    assert res["custody_warnings"] == []  # all candidates are sitting in stores
    assert len(res["candidate_item_ids"]) == 4
    assert db.query(Ledger).count() == before  # no reservation, no custody change
    assert all(ledger.get_state(db, i)["custody"] == "STORES" for i in res["candidate_item_ids"])
    assert "separate check" in res["note"]
    again = check(db, people, wo_id="WO-2")  # the same parts can clear another WO: no reservation
    assert again["result"] == "READY" and set(again["candidate_item_ids"]) == set(res["candidate_item_ids"])


def test_ready_even_if_all_serviceable_parts_are_issued_or_fitted_with_custody_warnings(db, people, routed, world):
    k = routed
    b = builds.create_build(db, people["stores"], definition_id=k["asm"], location_id=world["bins"][0])
    for key in ("d1", "p1", "t1", "b1"):
        builds.fit_item(db, people["stores"], b.id, k["items"][key])
    builds.verify_build(db, people["eng"], b.id)
    hold(db, people, k, "d2", "p2", "p3")  # the spare parts are unavailable, so the fitted ones are all there is
    res = check(db, people, wo_id="WO-5")
    assert res["result"] == "READY"  # fitted in a stored build still counts
    assert {w["display_id"] for w in res["custody_warnings"]} == {"D1", "P1", "T1", "B1"}
    assert all(w["build"] == builds.build_code(b.id) for w in res["custody_warnings"])
    # now issue it to WO-1: still READY for WO-2, now with production custody + the WO named
    mappings_id = k["mapping"]
    issues.create_issue(db, people["stores"], setup_id=mk_setup(db, people, "WO-1", mappings_id), wo_id="WO-1", mapping_id=mappings_id, build_id=b.id,
                        confirmed_item_ids=[c.item_id for c in builds.members(db, b.id)])
    res = check(db, people, wo_id="WO-2")
    assert res["result"] == "READY" and res["can_release"]
    assert {w["custody"] for w in res["custody_warnings"]} == {"PENDING_HANDOVER"}
    assert all(w["wo_id"] == "WO-1" and "not reserved" in w["message"] for w in res["custody_warnings"])


# ---- A05: shortage with exact detail; approved substitute restores readiness --------------------
def test_sole_eligible_item_held_fails_with_the_exact_shortage(db, people, routed):
    hold(db, people, routed, "d1", "d2")
    res = check(db, people)
    assert res["result"] == "NOT_READY" and not res["can_release"] and codes(res) == {"SHORTAGE"}
    (s,) = res["shortages"]
    assert (s["line"], s["needed"], s["missing"]) == ("DIE-32", 1, 1)
    assert {(b["display_id"], b["condition"]) for b in s["blocked_items"]} == {("D1", "HOLD"), ("D2", "HOLD")}
    assert "Short by 1 x DIE-32" in res["reasons"][0]["message"]


def test_missing_item_with_nothing_registered_is_a_shortage_too(db, people, routed):
    items.retire_item(db, people["eng"], routed["items"]["b1"], reason="lost")
    res = check(db, people)
    (s,) = res["shortages"]
    assert s["line"] == "BF-A" and s["blocked_items"] == [] and "no registered item" in s["message"]


def test_approved_serviceable_substitute_restores_readiness(db, people, routed):
    hold(db, people, routed, "p1", "p3")  # both PAT-1 patterns held
    res = check(db, people)
    assert res["result"] == "READY"  # PAT-2 (p2) is an approved substitute on that line
    assert routed["items"]["p2"] in res["candidate_item_ids"]
    hold(db, people, routed, "p2")
    assert check(db, people)["result"] == "NOT_READY"


@pytest.mark.parametrize("cond", ["AWAITING_INSPECTION", "UNDER_REPAIR", "SCRAPPED"])
def test_every_unserviceable_condition_counts_as_missing(db, people, routed, cond):
    t1 = routed["items"]["t1"]
    hold(db, people, routed, "t1")
    if cond in ("UNDER_REPAIR", "AWAITING_INSPECTION"):
        ledger.set_condition(db, people["tooling"], [t1], "UNDER_REPAIR", reason="weld")
    if cond == "AWAITING_INSPECTION":
        ledger.set_condition(db, people["tooling"], [t1], "AWAITING_INSPECTION", reason="done")
    if cond == "SCRAPPED":
        ledger.set_condition(db, people["scrap"], [t1], "SCRAPPED", reason="scrap")
    res = check(db, people)
    assert res["result"] == "NOT_READY" and res["shortages"][0]["blocked_items"][0]["condition"] == cond


# ---- A06: overlapping alternatives, one item never counted twice ----------------------------------
def test_overlapping_alternative_sets_never_count_one_item_twice(db, people, kit):
    eng = people["eng"]
    a = assemblies.create_assembly(db, eng, code="x-ov", description="overlap", category_id=kit["asm_cat"])
    rid = assemblies.revisions(db, a.id)[0].id
    l1 = assemblies.add_line(db, eng, rid, design_id=kit["pat"])
    l2 = assemblies.add_line(db, eng, rid, design_id=kit["pat2"])
    assemblies.add_alternative(db, eng, l1.id, design_id=kit["pat2"])  # either line can use either design
    assemblies.add_alternative(db, eng, l2.id, design_id=kit["pat"])
    assemblies.approve_revision(db, eng, rid)
    mappings.create_mapping(db, eng, part_id="PN-9", routing_revision="R1", operation_id="OP10",
                            definition_id=a.id, selected=True)
    hold(db, people, kit, "p3", "p2")  # only P1 left: one pattern cannot fill two lines
    res = readiness.run_check(db, people["prod"], wo_id="WO-9", part_id="PN-9", routing_revision="R1", operation_id="OP10")
    assert res["result"] == "NOT_READY" and sum(s["missing"] for s in res["shortages"]) == 1
    clear_items(db, people, kit["items"]["p2"])
    res = readiness.run_check(db, people["prod"], wo_id="WO-9", part_id="PN-9", routing_revision="R1", operation_id="OP10")
    assert res["result"] == "READY" and len(set(res["candidate_item_ids"])) == 2


# ---- not ready for configuration reasons ---------------------------------------------------------
def test_draft_obsolete_or_unapproved_assembly_is_not_ready(db, people, routed):
    eng = people["eng"]
    draft = assemblies.create_assembly(db, eng, code="x-draft", description="d", category_id=routed["asm_cat"])
    assemblies.add_line(db, eng, assemblies.revisions(db, draft.id)[0].id, design_id=routed["die"])
    mappings.create_mapping(db, eng, part_id="PN-2", routing_revision="R1", operation_id="OP10", definition_id=draft.id, selected=True)
    res = readiness.run_check(db, people["prod"], wo_id="WO-3", part_id="PN-2", routing_revision="R1", operation_id="OP10")
    assert res["result"] == "NOT_READY" and codes(res) == {"ASSEMBLY_NOT_APPROVED"}
    assemblies.obsolete_revision(db, eng, routed["rev"], reason="withdrawn")
    res = check(db, people, wo_id="WO-4")
    assert res["result"] == "NOT_READY" and "ASSEMBLY_NOT_APPROVED" in codes(res)


def test_tooling_marked_to_be_developed_is_not_ready(db, people, routed):
    mappings.update_mapping(db, people["eng"], routed["mapping"], fields_set={"tooling_status"},
                            tooling_status="TO_BE_DEVELOPED")
    res = check(db, people)
    assert res["result"] == "NOT_READY" and codes(res) == {"TOOLING_TO_BE_DEVELOPED"}
    mappings.update_mapping(db, people["eng"], routed["mapping"], fields_set={"tooling_status"}, tooling_status="READY")
    assert check(db, people)["result"] == "READY"  # ready once tooling exists, is approved and passes


def test_failed_selected_option_shows_a_passing_alternative_without_switching(db, people, routed):
    eng = people["eng"]
    alt = assemblies.create_assembly(db, eng, code="x-alt", description="patterns only", category_id=routed["asm_cat"])
    r = assemblies.revisions(db, alt.id)[0].id
    assemblies.add_line(db, eng, r, design_id=routed["pat"])
    assemblies.approve_revision(db, eng, r)
    m2 = mappings.create_mapping(db, eng, part_id="PN-1", routing_revision="R1", operation_id="OP10", definition_id=alt.id)
    hold(db, people, routed, "d1", "d2")  # the selected option (T211) now fails
    res = check(db, people)
    assert res["result"] == "NOT_READY" and res["mapping_id"] == routed["mapping"]  # still evaluated as selected
    assert [a["mapping_id"] for a in res["alternatives"]] == [m2.id] and res["alternatives"][0]["assembly_code"] == "X-ALT"
    rerun = check(db, people, mapping_id=m2.id)  # the OMS user chooses it and reruns
    assert rerun["result"] == "READY" and rerun["mapping_id"] == m2.id and rerun["alternatives"] == []


# ---- unable to verify: never ready, no bypass --------------------------------------------------------
def test_unmapped_operation_or_unselected_option_is_unable_to_verify(db, people, routed):
    res = check(db, people, operation_id="OP99")
    assert res["result"] == "UNABLE_TO_VERIFY" and not res["can_release"] and codes(res) == {"MAPPING_NOT_FOUND"}
    mappings.update_mapping(db, people["eng"], routed["mapping"], fields_set={"selected"}, selected=False)
    res = check(db, people)
    assert res["result"] == "UNABLE_TO_VERIFY" and codes(res) == {"NO_SELECTED_OPTION"}
    assert res["mapping_id"] is None and res["candidate_item_ids"] == []


def test_option_from_another_operation_is_rejected_not_bypassed(db, people, routed):
    with pytest.raises(ValidationError):
        check(db, people, operation_id="OP99", mapping_id=routed["mapping"])


def test_consumable_stock_only_ever_warns_at_release(db, people, routed, world):
    from app.services import batches
    eng = people["eng"]
    cons = designs.create_design(db, eng, code="coat", description="coat", category_id=routed["asm_cat"], base_uom="EA", kind="CONSUMABLE")
    designs.approve_revision(db, eng, designs.revisions(db, cons.id)[0].id)
    r = assemblies.new_revision(db, eng, routed["asm"], reason="add coating")
    assemblies.add_line(db, eng, r.id, design_id=cons.id, quantity=2, quantity_basis="ME spec")
    assemblies.approve_revision(db, eng, r.id)
    res = check(db, people)  # no stock at all: a warning, release is NOT blocked
    assert res["result"] == "READY" and [w["code"] for w in res["warnings"]] == ["CONSUMABLE_ABSENT"]
    batches.receive_batch(db, people["stores"], design_id=cons.id, quantity=1, location_id=world["bins"][0])
    res = check(db, people)  # 1 of the 2 needed per issue: low-stock warning
    assert res["result"] == "READY" and [w["code"] for w in res["warnings"]] == ["CONSUMABLE_LOW"]
    assert "Only 1 EA" in res["warnings"][0]["message"]
    batches.receive_batch(db, people["stores"], design_id=cons.id, quantity=5, location_id=world["bins"][0])
    assert check(db, people)["warnings"] == []  # enough usable stock: no warning (nothing is reserved either)


# ---- evidence, roles, freshness ---------------------------------------------------------------------
def test_every_check_is_stored_with_time_revisions_and_reason_codes(db, people, routed):
    ok = check(db, people)
    hold(db, people, routed, "d1", "d2")
    bad = check(db, people)
    total, rows = readiness.list_checks(db, wo_id="WO-1")
    assert total == 2 and [r["result"] for r in rows] == ["NOT_READY", "READY"]  # newest first; each run is fresh
    assert bad["definition_revision_id"] == routed["rev"] and bad["checked_at"] and bad["checked_by"] == "prod"
    assert bad["reasons"][0]["code"] == "SHORTAGE" and ok["reasons"] == []
    assert readiness.get_check(db, ok["id"])["candidate_item_ids"] == ok["candidate_item_ids"]
    assert db.query(ReadinessCheck).count() == 2


def test_who_may_run_checks(db, people, routed):
    for who in ("prod", "stores", "eng", "tooling", "mgr", "admin"):  # admin has full access
        assert check(db, people, who=who)["result"] == "READY"
    with pytest.raises(PermissionDenied):
        check(db, people, who="plan")


# ---- recheck flags ---------------------------------------------------------------------------------
def flagged(db):
    return {c.wo_id: c.recheck_reason for c in recheck.flagged(db)}


def test_losing_a_candidate_part_flags_the_open_wo_but_cancels_nothing(db, people, routed):
    res = check(db, people)
    cand = res["candidate_item_ids"][0]
    spare = [i for i in routed["items"].values() if i not in res["candidate_item_ids"]][0]
    ledger.set_condition(db, people["tooling"], [spare], "HOLD", reason="unrelated spare")
    assert flagged(db) == {}  # not a candidate: no flag
    ledger.set_condition(db, people["tooling"], [cand], "HOLD", reason="crack found")
    assert list(flagged(db)) == ["WO-1"] and "crack found" in flagged(db)["WO-1"]
    stored = readiness.get_check(db, res["id"])
    assert stored["result"] == "READY" and stored["needs_recheck"]  # history kept, nothing cancelled
    fresh = check(db, people)  # OMS reruns: new latest check, no longer flagged
    assert fresh["needs_recheck"] is False and flagged(db) == {}


def test_closed_wos_are_not_flagged(db, people, routed):
    res = check(db, people)
    readiness.close_wo(db, people["prod"], "WO-1")
    ledger.set_condition(db, people["tooling"], [res["candidate_item_ids"][0]], "HOLD", reason="crack")
    assert flagged(db) == {}


def test_retiring_a_candidate_flags(db, people, routed):
    res = check(db, people)
    items.retire_item(db, people["eng"], res["candidate_item_ids"][0], reason="obsolete tooling")
    assert "WO-1" in flagged(db)


def test_new_assembly_design_or_mapping_revisions_flag_affected_wos(db, people, routed):
    eng = people["eng"]
    check(db, people)
    new = assemblies.new_revision(db, eng, routed["asm"], reason="composition change")
    assert flagged(db) == {}  # a draft changes nothing yet
    assemblies.approve_revision(db, eng, new.id)
    assert "WO-1" in flagged(db) and "Assembly revision B approved" in flagged(db)["WO-1"]
    check(db, people)  # rerun clears
    assert flagged(db) == {}
    designs.new_revision(db, eng, routed["die"], reason="bore change")
    designs.approve_revision(db, eng, designs.revisions(db, routed["die"])[-1].id)
    assert "WO-1" in flagged(db) and "no longer accepted" in flagged(db)["WO-1"]
    check(db, people)  # old dies are now rev A (not accepted by default): shortage
    mappings.update_mapping(db, eng, routed["mapping"], fields_set={"tooling_status"}, tooling_status="TO_BE_DEVELOPED")
    assert flagged(db) == {}  # the latest check was NOT_READY: nothing to invalidate


def test_mapping_change_flags_a_ready_wo(db, people, routed):
    check(db, people)
    mappings.update_mapping(db, people["eng"], routed["mapping"], fields_set={"tooling_status"}, tooling_status="TO_BE_DEVELOPED")
    assert "ME mapping changed" in flagged(db)["WO-1"]
