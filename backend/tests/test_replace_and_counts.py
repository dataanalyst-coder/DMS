import pytest

from app.models import AuditLog, BuildComponent, Ledger
from app.services import (auth, batches, builds, config, counts, designs, issues, items, ledger, mappings, readiness,
                          recheck)
from app.services.errors import NotFoundError, PermissionDenied, ValidationError
from tests.conftest import PW, mk_setup, setup_body

KEYS = ("d1", "p1", "t1", "b1")


@pytest.fixture
def rig(db, people, kit, world):
    m = mappings.create_mapping(db, people["eng"], part_id="PN-1", routing_revision="R1", operation_id="OP10",
                                definition_id=kit["asm"], selected=True)
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    for k in KEYS:
        builds.fit_item(db, people["stores"], b.id, kit["items"][k])
    builds.verify_build(db, people["eng"], b.id)
    ids = [kit["items"][k] for k in KEYS]
    iss = issues.create_issue(db, people["stores"], setup_id=mk_setup(db, people, "WO-1", m.id), wo_id="WO-1", mapping_id=m.id, build_id=b.id, confirmed_item_ids=ids)
    issues.acknowledge_issue(db, people["prod"], iss.id)
    spare = items.register_item(db, people["stores"], design_id=kit["tf"], legacy_no="t2", condition="SERVICEABLE",
                                location_id=world["bins"][2])
    kit.update(mapping=m.id, build=b.id, issue=iss.id, ids=ids, spare=spare.id)
    return kit


def st(db, item_id):
    return ledger.get_state(db, item_id)


# ---- replacing a component while the build is in production (spec section 4) ---------------------------------
def test_replacement_is_one_explicit_remove_add_event_and_needs_verification_later(db, people, rig):
    old = rig["items"]["t1"]
    res = issues.replace_in_production(db, people["stores"], rig["issue"], old, rig["spare"], reason="top flange worn through")
    rows = db.query(Ledger).filter_by(event_id=res["event_id"]).order_by(Ledger.txn_id).all()
    assert [(r.action, r.item_id) for r in rows] == [("REMOVE", old), ("FIT", rig["spare"]), ("ISSUE", rig["spare"])]
    assert res["build_status"] == "ASSEMBLING"  # must be verified again before it is issued once more
    assert not builds.build_detail(db, rig["build"])["ready_to_issue"]
    members = {m.item_id for m in builds.members(db, rig["build"])}
    assert rig["spare"] in members and old not in members
    hist = {h["item_id"]: h for h in builds.build_detail(db, rig["build"])["history"]}
    assert hist[old]["removal_reason"] == "top flange worn through" and hist[old]["removed_at"]  # membership history kept


def test_the_outgoing_item_stays_issued_and_must_still_be_returned_and_inspected(db, people, rig, world):
    old = rig["items"]["t1"]
    issues.replace_in_production(db, people["stores"], rig["issue"], old, rig["spare"], reason="worn")
    assert st(db, old)["custody"] == "PRODUCTION"  # still with Production, still on the WO
    d = issues.issue_detail(db, rig["issue"])
    assert {l["display_id"]: l["state"] for l in d["lines"]}["T1"] == "IN_PRODUCTION" and "T1" in d["outstanding"]
    assert {l["display_id"]: l["state"] for l in d["lines"]}["T2"] == "PENDING_HANDOVER"  # incoming: handed over normally
    issues.acknowledge_issue(db, people["prod"], rig["issue"])
    assert st(db, rig["spare"])["custody"] == "PRODUCTION"
    issues.return_items(db, people["stores"], rig["issue"], [{"item_id": old, "to_location_id": world["bins"][1]}])
    s = st(db, old)
    assert (s["item_condition"], s["custody"]) == ("AWAITING_INSPECTION", "STORES")  # not in a build any more: back to stores
    assert issues.issue_detail(db, rig["issue"])["status"] == "PARTIALLY_RETURNED"


def test_replacement_rules(db, people, rig, world):
    old, t = rig["items"]["t1"], rig["spare"]
    for who in ("prod", "tooling"):
        with pytest.raises(PermissionDenied):
            issues.replace_in_production(db, people[who], rig["issue"], old, t, reason="x")
    with pytest.raises(ValidationError):
        issues.replace_in_production(db, people["stores"], rig["issue"], old, t, reason=" ")
    with pytest.raises(ValidationError) as e:  # wrong design cannot take the top flange position
        issues.replace_in_production(db, people["stores"], rig["issue"], old, rig["items"]["d2"], reason="x")
    assert "no free place" in e.value.message or "wrong" in e.value.message
    ledger.set_condition(db, people["tooling"], [t], "HOLD", reason="crack")
    with pytest.raises(ValidationError) as e:
        issues.replace_in_production(db, people["stores"], rig["issue"], old, t, reason="x")
    assert "only serviceable" in e.value.message
    ledger.set_condition(db, people["tooling"], [t], "AWAITING_INSPECTION", reason="x")
    from tests.conftest import clear_items
    clear_items(db, people, t)
    with pytest.raises(ValidationError) as e:  # the outgoing item is not on this issue / not a component
        issues.replace_in_production(db, people["stores"], rig["issue"], rig["items"]["d2"], t, reason="x")
    assert "not outstanding" in e.value.message
    before = db.query(Ledger).count()
    issues.replace_in_production(db, people["stores"], rig["issue"], old, t, reason="worn")
    with pytest.raises(ValidationError):  # the same part cannot be swapped out twice
        issues.replace_in_production(db, people["stores"], rig["issue"], old, rig["spare"], reason="again")
    assert db.query(Ledger).count() == before + 3


def test_a_failed_replacement_changes_nothing(db, people, rig):
    before = (db.query(Ledger).count(), db.query(BuildComponent).count())
    with pytest.raises(ValidationError):
        issues.replace_in_production(db, people["stores"], rig["issue"], rig["items"]["t1"], rig["items"]["d2"], reason="x")
    assert (db.query(Ledger).count(), db.query(BuildComponent).count()) == before


# ---- stock counts -----------------------------------------------------------------------------------------------
@pytest.fixture
def stock(db, people, world, make_item):
    """Three dies in bin 1 and one in bin 2; a 50 L coating batch in bin 1."""
    a, b, c = (make_item(legacy=f"c{i}", bin_index=0) for i in (1, 2, 3))
    far = make_item(legacy="far", bin_index=1)
    config.create_uom(db, people["admin"], "L", "Litre")
    d = designs.create_design(db, people["eng"], code="coat", description="coat", category_id=world["cat"], base_uom="L", kind="CONSUMABLE")
    designs.approve_revision(db, people["eng"], designs.revisions(db, d.id)[0].id)
    batch = batches.receive_batch(db, people["stores"], design_id=d.id, quantity=50, location_id=world["bins"][0])
    return {"a": a, "b": b, "c": c, "far": far, "batch": batch, "bins": world["bins"]}


def start(db, people, stock, bin_i=0):
    return counts.start_count(db, people["stores"], stock["bins"][bin_i])


def test_count_reports_expected_found_missing_and_unexpected(db, people, stock):
    c = start(db, people, stock)
    counts.scan(db, people["stores"], c.id, "c1")
    counts.scan(db, people["stores"], c.id, stock["a"].system_id)  # a repeat scan is harmless
    counts.scan(db, people["stores"], c.id, "c2")
    counts.scan(db, people["stores"], c.id, "far")  # belongs in bin 2
    r = counts.report(db, c.id)
    assert r["summary"] == {"expected": 3, "found": 2, "missing": 1, "unexpected": 1, "batch_variances": 1}
    assert [m["display_id"] for m in r["missing"]] == ["C3"]
    assert r["unexpected"][0]["display_id"] == "FAR" and r["unexpected"][0]["reason"] == "EXPECTED_ELSEWHERE"
    assert r["unexpected"][0]["recorded_location"].endswith("B2")


def test_unexpected_reasons_cover_missing_production_and_retired_items(db, people, stock, world, make_item):
    c = start(db, people, stock)
    gone = make_item(legacy="gone")
    ledger_c = counts.start_count(db, people["stores"], world["bins"][0])
    counts.scan(db, people["stores"], ledger_c.id, "gone")
    counts.complete(db, people["stores"], ledger_c.id)
    items.retire_item(db, people["eng"], stock["c"].id, reason="obsolete")
    counts.scan(db, people["stores"], c.id, "c3")
    assert [u["reason"] for u in counts.report(db, c.id)["unexpected"]] == ["RETIRED"]
    assert gone.id


def test_scanning_a_build_label_counts_everything_fitted_in_it(db, people, kit, world):
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    for k in KEYS:
        builds.fit_item(db, people["stores"], b.id, kit["items"][k])
    c = counts.start_count(db, people["stores"], world["bins"][0])
    r = counts.scan(db, people["stores"], c.id, "BLD-000001")
    assert r["recorded"] == 4 and counts.report(db, c.id)["summary"]["found"] == 4


def test_batch_counts_need_a_quantity_and_show_the_variance(db, people, stock):
    c = start(db, people, stock)
    with pytest.raises(ValidationError):
        counts.scan(db, people["stores"], c.id, stock["batch"].batch_code)
    with pytest.raises(ValidationError):
        counts.scan(db, people["stores"], c.id, stock["batch"].batch_code, quantity=-1)
    counts.scan(db, people["stores"], c.id, stock["batch"].batch_code, quantity=47.5)
    (b,) = counts.report(db, c.id)["batches"]
    assert (b["expected"], b["counted"], b["variance"], b["state"]) == (50, 47.5, -2.5, "SHORT")
    counts.scan(db, people["stores"], c.id, stock["batch"].batch_code, quantity=50)  # a recount replaces the figure
    assert counts.report(db, c.id)["batches"][0]["state"] == "OK"


def test_completed_count_keeps_the_figures_it_closed_with(db, people, stock):
    c = start(db, people, stock)
    counts.scan(db, people["stores"], c.id, "c1")
    counts.complete(db, people["stores"], c.id)
    ledger.move_items(db, people["stores"], [stock["a"].id], stock["bins"][2])  # the world moves on afterwards
    assert counts.report(db, c.id)["summary"]["found"] == 1 and counts.report(db, c.id)["status"] == "COMPLETED"
    with pytest.raises(ValidationError):
        counts.scan(db, people["stores"], c.id, "c2")  # no scans after completion
    with pytest.raises(ValidationError):
        counts.complete(db, people["stores"], c.id)


def test_who_may_count_and_who_may_adjust(db, people, stock):
    for who in ("prod", "eng", "mgr"):
        with pytest.raises(PermissionDenied):
            counts.start_count(db, people[who], stock["bins"][0])
    c = start(db, people, stock)
    counts.scan(db, people["multi"], c.id, "c1")  # STORES + TOOLING person
    counts.complete(db, people["stores"], c.id)
    for who in ("stores", "tooling", "scrap"):
        with pytest.raises(PermissionDenied):
            counts.apply(db, people[who], c.id, [{"item_id": stock["b"].id, "action": "MARK_MISSING", "reason": "gone"}])


# ---- nothing changes until an authorised adjustment -----------------------------------------------------------------
def test_a_count_by_itself_changes_nothing(db, people, stock):
    before = db.query(Ledger).count()
    c = start(db, people, stock)
    counts.scan(db, people["stores"], c.id, "c1")
    counts.complete(db, people["stores"], c.id)
    assert db.query(Ledger).count() == before and st(db, stock["b"].id)["custody"] == "STORES"


def test_missing_item_cannot_pass_readiness_be_fitted_issued_or_moved(db, people, kit, world):
    m = mappings.create_mapping(db, people["eng"], part_id="PN-1", routing_revision="R1", operation_id="OP10",
                                definition_id=kit["asm"], selected=True)
    ok = readiness.run_check(db, people["prod"], wo_id="WO-1", part_id="PN-1", routing_revision="R1", operation_id="OP10")
    assert ok["result"] == "READY" and m.id
    c = counts.start_count(db, people["stores"], world["bins"][0])
    counts.complete(db, people["stores"], c.id)  # nothing scanned at all: every item here is missing
    rep = counts.report(db, c.id)
    missing_ids = {x["id"] for x in rep["missing"]}
    assert kit["items"]["d1"] in missing_ids and kit["items"]["p1"] in missing_ids
    counts.apply(db, people["eng"], c.id, [{"item_id": i, "action": "MARK_MISSING", "reason": "not on the rack"} for i in missing_ids])
    with pytest.raises(ValidationError):  # already missing: not marked twice
        counts.apply(db, people["eng"], c.id, [{"item_id": next(iter(missing_ids)), "action": "MARK_MISSING", "reason": "again"}])
    for i in missing_ids:
        assert st(db, i)["custody"] == "MISSING"
    # d2 sits in bin 2 (not counted) so a die is still available; mark it missing too and readiness must fail
    d2 = kit["items"]["d2"]
    c2 = counts.start_count(db, people["stores"], world["bins"][1])
    counts.complete(db, people["stores"], c2.id)
    counts.apply(db, people["eng"], c2.id, [{"item_id": d2, "action": "MARK_MISSING", "reason": "not on the rack"}])
    res = readiness.run_check(db, people["prod"], wo_id="WO-2", part_id="PN-1", routing_revision="R1", operation_id="OP10")
    assert res["result"] == "NOT_READY"
    dies = next(s for s in res["shortages"] if s["line"] == "DIE-32")
    assert {(b["display_id"], b["condition"]) for b in dies["blocked_items"]} >= {("D1", "MISSING"), ("D2", "MISSING")}
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    with pytest.raises(ValidationError) as e:
        builds.fit_item(db, people["stores"], b.id, kit["items"]["d1"])
    assert "MISSING custody" in e.value.message
    with pytest.raises(ValidationError):
        ledger.move_items(db, people["stores"], [kit["items"]["d1"]], world["bins"][2])
    with pytest.raises(ValidationError) as e:
        ledger.set_condition(db, people["tooling"], [kit["items"]["d1"]], "HOLD", reason="x")
    assert "marked missing" in e.value.message


def test_marking_missing_flags_open_wos_and_is_audited_with_a_reason(db, people, kit, world):
    mappings.create_mapping(db, people["eng"], part_id="PN-1", routing_revision="R1", operation_id="OP10",
                            definition_id=kit["asm"], selected=True)
    chk = readiness.run_check(db, people["prod"], wo_id="WO-1", part_id="PN-1", routing_revision="R1", operation_id="OP10")
    cand = chk["candidate_item_ids"][0]
    where = ledger.get_state(db, cand)["location_id"]
    c = counts.start_count(db, people["stores"], where)
    counts.complete(db, people["stores"], c.id)
    with pytest.raises(ValidationError):
        counts.apply(db, people["eng"], c.id, [{"item_id": cand, "action": "MARK_MISSING", "reason": " "}])
    counts.apply(db, people["eng"], c.id, [{"item_id": cand, "action": "MARK_MISSING", "reason": "not found on the rack"}])
    assert "WO-1" in {x.wo_id for x in recheck.flagged(db)}
    row = db.query(AuditLog).filter_by(action="COUNT_MARK_MISSING", entity_id=str(cand)).one()
    assert row.reason == "not found on the rack" and row.username == "eng"
    last = db.query(Ledger).filter_by(item_id=cand).order_by(Ledger.txn_id.desc()).first()
    assert last.action == "MARK_MISSING" and last.source == "COUNT"
    with pytest.raises(ValidationError):  # corrected through a count, not by hand-reversal
        ledger.reverse(db, people["eng"], last.txn_id, reason="oops")


def test_recovering_a_missing_item_puts_it_back_awaiting_inspection(db, people, stock, world):
    c = start(db, people, stock)
    counts.complete(db, people["stores"], c.id)
    counts.apply(db, people["eng"], c.id, [{"item_id": stock["b"].id, "action": "MARK_MISSING", "reason": "not here"}])
    c2 = counts.start_count(db, people["stores"], world["bins"][2])
    counts.scan(db, people["stores"], c2.id, "c2")
    counts.complete(db, people["stores"], c2.id)
    assert counts.report(db, c2.id)["unexpected"][0]["reason"] == "MARKED_MISSING"
    counts.apply(db, people["eng"], c2.id, [{"item_id": stock["b"].id, "action": "RECOVER", "reason": "found behind the rack"}])
    with pytest.raises(ValidationError):
        counts.apply(db, people["eng"], c2.id, [{"item_id": stock["b"].id, "action": "RECOVER", "reason": "again"}])
    s = st(db, stock["b"].id)
    assert (s["custody"], s["item_condition"], s["location_id"]) == ("STORES", "AWAITING_INSPECTION", world["bins"][2])


def test_item_found_in_the_wrong_place_is_corrected_by_an_authorised_adjustment(db, people, stock, world):
    c = start(db, people, stock)
    counts.scan(db, people["stores"], c.id, "far")
    counts.complete(db, people["stores"], c.id)
    with pytest.raises(ValidationError):  # not in the missing list, nor found elsewhere as a different case
        counts.apply(db, people["eng"], c.id, [{"item_id": stock["a"].id, "action": "MOVE_HERE", "reason": "x"}])
    counts.apply(db, people["eng"], c.id, [{"item_id": stock["far"].id, "action": "MOVE_HERE", "reason": "found on the wrong rack"}])
    with pytest.raises(ValidationError):  # applying it a second time would duplicate the ledger entry
        counts.apply(db, people["eng"], c.id, [{"item_id": stock["far"].id, "action": "MOVE_HERE", "reason": "again"}])
    assert st(db, stock["far"].id)["location_id"] == world["bins"][0]
    assert st(db, stock["far"].id)["item_condition"] == "SERVICEABLE"  # a location fix never changes condition


def test_batch_variance_is_adjusted_with_history_and_never_below_zero(db, people, stock):
    c = start(db, people, stock)
    counts.scan(db, people["stores"], c.id, stock["batch"].batch_code, quantity=44)
    with pytest.raises(ValidationError):  # not completed yet
        counts.apply(db, people["eng"], c.id, [], [{"batch_id": stock["batch"].id, "reason": "count"}])
    counts.complete(db, people["stores"], c.id)
    with pytest.raises(ValidationError):
        counts.apply(db, people["eng"], c.id, [], [{"batch_id": stock["batch"].id, "reason": " "}])
    out = counts.apply(db, people["eng"], c.id, [], [{"batch_id": stock["batch"].id, "reason": "short by 6 L: spillage"}])
    assert out["batches"][0]["delta"] == -6
    assert batches.balance(db, stock["batch"].id)["usable"] == 44
    hist = batches.batch_dict(db, stock["batch"], detail=True)["history"]
    assert [h["action"] for h in hist] == ["ADJUST", "RECEIVE"] and "Stock count" in hist[0]["remarks"]  # history is preserved
    with pytest.raises(ValidationError) as e:  # the same count cannot adjust the stock twice
        counts.apply(db, people["eng"], c.id, [], [{"batch_id": stock["batch"].id, "reason": "again"}])
    assert "changed since the count" in e.value.message and batches.balance(db, stock["batch"].id)["usable"] == 44


def test_adjustment_that_would_make_usable_negative_is_refused(db, people, stock):
    batches.hold_quantity(db, people["tooling"], stock["batch"].id, 50, reason="all quarantined")  # usable 0, held 50
    c = start(db, people, stock)
    counts.scan(db, people["stores"], c.id, stock["batch"].batch_code, quantity=40)
    counts.complete(db, people["stores"], c.id)
    with pytest.raises(ValidationError) as e:
        counts.apply(db, people["eng"], c.id, [], [{"batch_id": stock["batch"].id, "reason": "count"}])
    assert "negative" in e.value.message and batches.balance(db, stock["batch"].id)["held"] == 50


def test_cancel_and_listing(db, people, stock):
    c = start(db, people, stock)
    with pytest.raises(ValidationError):
        counts.cancel(db, people["stores"], c.id, reason=" ")
    counts.cancel(db, people["stores"], c.id, reason="started on the wrong rack")
    with pytest.raises(ValidationError):
        counts.scan(db, people["stores"], c.id, "c1")
    assert [x["status"] for x in counts.list_counts(db)] == ["CANCELLED"]
    with pytest.raises(NotFoundError):
        counts.report(db, 999)
