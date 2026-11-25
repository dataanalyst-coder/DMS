from datetime import date, timedelta

import pytest
from tests.conftest import mk_setup, setup_body

from app.models import AuditLog, BatchLedger, Issue, IssueBatch, Ledger
from app.services import assemblies, batches, builds, config, designs, issues, items, ledger, mappings
from app.services.errors import PermissionDenied, ValidationError

KEYS = ("d1", "p1", "t1", "b1")


@pytest.fixture
def crig(db, people, kit, world):
    """T211 rev B needs 2 L of coating (basis: ME spec). A verified build, a mapping and 100 L in stock."""
    eng = people["eng"]
    config.create_uom(db, people["admin"], "L", "Litre")
    coat = designs.create_design(db, eng, code="coat", description="Die coating", category_id=kit["asm_cat"],
                                 base_uom="L", kind="CONSUMABLE")
    designs.approve_revision(db, eng, designs.revisions(db, coat.id)[0].id)
    alt = designs.create_design(db, eng, code="coat-b", description="Alt coating", category_id=kit["asm_cat"],
                                base_uom="L", kind="CONSUMABLE")
    designs.approve_revision(db, eng, designs.revisions(db, alt.id)[0].id)
    r = assemblies.new_revision(db, eng, kit["asm"], reason="add coating")
    line = assemblies.add_line(db, eng, r.id, design_id=coat.id, quantity=2, quantity_basis="ME spec ME-17")
    assemblies.add_alternative(db, eng, line.id, design_id=alt.id)
    assemblies.approve_revision(db, eng, r.id)
    m = mappings.create_mapping(db, eng, part_id="PN-1", routing_revision="R1", operation_id="OP10",
                                definition_id=kit["asm"], selected=True)
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    for k in KEYS:
        builds.fit_item(db, people["stores"], b.id, kit["items"][k])
    builds.verify_build(db, people["eng"], b.id)
    batch = batches.receive_batch(db, people["stores"], design_id=coat.id, quantity=100, location_id=world["bins"][1])
    kit.update(mapping=m.id, build=b.id, ids=[kit["items"][k] for k in KEYS], coat=coat.id, coat_b=alt.id,
               line=line.id, batch=batch.id)
    return kit


def issue(db, people, rig, picks=None, who="stores", override=None, **kw):
    picks = [{"line_id": rig["line"], "batch_id": rig["batch"], "quantity": 2}] if picks is None else picks
    return issues.create_issue(db, people[who], setup_id=mk_setup(db, people, "WO-1", rig["mapping"]), wo_id="WO-1", mapping_id=rig["mapping"], build_id=rig["build"],
                               confirmed_item_ids=rig["ids"], consumables=picks, override_reason=override, **kw)


def bal(db, rig, batch=None):
    return batches.balance(db, batch or rig["batch"])


# ---- the requirement and its basis ---------------------------------------------------------------
def test_preview_shows_the_requirement_with_its_basis_and_usable_batches(db, people, crig, world):
    old = batches.receive_batch(db, people["stores"], design_id=crig["coat"], quantity=5, location_id=world["bins"][1],
                                expiry_date=date.today() + timedelta(days=3))
    batches.receive_batch(db, people["stores"], design_id=crig["coat"], quantity=5, location_id=world["bins"][1],
                          expiry_date=date.today() - timedelta(days=1))
    (c,) = issues.issue_preview(db, crig["build"])["consumables"]
    assert (c["design_code"], c["quantity"], c["uom"], c["basis"]) == ("COAT", 2, "L", "ME spec ME-17")
    assert [b["id"] for b in c["batches"]] == [old.id, crig["batch"]]  # earliest expiry first; expired one is not offered


def test_issue_needs_batch_picks_for_every_consumable_line(db, people, crig):
    with pytest.raises(ValidationError) as e:
        issue(db, people, crig, picks=[])
    assert "2 L of COAT" in e.value.message
    assert db.query(Issue).count() == 0 and db.query(BatchLedger).filter_by(action="ISSUE").count() == 0


def test_pick_must_match_the_required_quantity_unless_a_reason_is_documented(db, people, crig):
    with pytest.raises(ValidationError) as e:
        issue(db, people, crig, picks=[{"line_id": crig["line"], "batch_id": crig["batch"], "quantity": 3}])
    assert "needs 2 L (ME spec ME-17) but 3 L was picked" in e.value.message
    i = issue(db, people, crig, picks=[{"line_id": crig["line"], "batch_id": crig["batch"], "quantity": 3}],
              override="trial pour needs extra coating, approved by Engineering")
    row = db.query(IssueBatch).filter_by(issue_id=i.id).one()
    assert row.quantity == 3 and row.basis == "ME spec ME-17" and "trial pour" in row.override_reason
    assert bal(db, crig)["usable"] == 97  # the documented authorised entry is what was deducted, not a guess
    audit = db.query(AuditLog).filter_by(entity_type="issue", action="ISSUE").one()
    assert "trial pour" in audit.after


def test_wrong_material_duplicate_and_unknown_lines_are_rejected(db, people, crig, world):
    other = designs.create_design(db, people["eng"], code="oil", description="Oil", category_id=crig["asm_cat"],
                                  base_uom="L", kind="CONSUMABLE")
    designs.approve_revision(db, people["eng"], designs.revisions(db, other.id)[0].id)
    wrong = batches.receive_batch(db, people["stores"], design_id=other.id, quantity=10, location_id=world["bins"][1])
    with pytest.raises(ValidationError) as e:
        issue(db, people, crig, picks=[{"line_id": crig["line"], "batch_id": wrong.id, "quantity": 2}])
    assert "not an accepted material" in e.value.message
    with pytest.raises(ValidationError):
        issue(db, people, crig, picks=[{"line_id": 9999, "batch_id": crig["batch"], "quantity": 2}])
    with pytest.raises(ValidationError) as e:
        issue(db, people, crig, picks=[{"line_id": crig["line"], "batch_id": crig["batch"], "quantity": 1},
                                       {"line_id": crig["line"], "batch_id": crig["batch"], "quantity": 1}])
    assert "appears twice" in e.value.message


def test_substitute_material_and_split_across_batches(db, people, crig, world):
    alt = batches.receive_batch(db, people["stores"], design_id=crig["coat_b"], quantity=1, location_id=world["bins"][1])
    i = issue(db, people, crig, picks=[{"line_id": crig["line"], "batch_id": crig["batch"], "quantity": 1.5},
                                       {"line_id": crig["line"], "batch_id": alt.id, "quantity": 0.5}])
    assert bal(db, crig)["usable"] == 98.5 and bal(db, crig, alt.id)["usable"] == 0.5
    assert len(issues.issue_detail(db, i.id)["consumables"]) == 2


# ---- insufficient / blocked batches; atomic with the items --------------------------------------------
def test_insufficient_expired_or_held_batches_reject_the_whole_issue(db, people, crig, world):
    batches.adjust(db, people["eng"], crig["batch"], -99, reason="count")  # 1 L left
    with pytest.raises(ValidationError) as e:
        issue(db, people, crig)
    assert "insufficient balance" in e.value.message
    # nothing happened to the items or the stock: all or nothing
    assert db.query(Issue).count() == 0
    assert {ledger.get_state(db, i)["custody"] for i in crig["ids"]} == {"ASSEMBLY_AREA"}
    assert bal(db, crig)["usable"] == 1 and bal(db, crig)["outstanding"] == 0
    b = batches.get_batch(db, crig["batch"])
    batches.adjust(db, people["eng"], crig["batch"], 99, reason="recount")
    b.expiry_date = date.today() - timedelta(days=1)
    with pytest.raises(ValidationError) as e:
        issue(db, people, crig)
    assert "expired" in e.value.message
    b.expiry_date = None
    batches.hold_quantity(db, people["tooling"], crig["batch"], 100, reason="suspect")
    with pytest.raises(ValidationError) as e:
        issue(db, people, crig)
    assert "on hold" in e.value.message
    assert db.query(Ledger).filter_by(action="ISSUE").count() == 0


def test_issue_deducts_stock_and_custody_in_one_transaction(db, people, crig):
    i = issue(db, people, crig)
    assert bal(db, crig)["usable"] == 98 and bal(db, crig)["outstanding"] == 2
    assert {ledger.get_state(db, x)["custody"] for x in crig["ids"]} == {"PENDING_HANDOVER"}
    rows = db.query(Ledger).filter_by(issue_id=i.id).all()
    brow = db.query(BatchLedger).filter_by(issue_id=i.id).one()
    assert brow.event_id == rows[0].event_id  # one event across both ledgers
    d = issues.issue_detail(db, i.id)
    assert d["consumables"][0]["state"] == "ISSUED" and d["consumables"][0]["quantity"] == 2
    assert d["consumables"][0]["basis"] == "ME spec ME-17" and "BATCH-000001" in d["outstanding"]


def test_a_failure_while_booking_stock_rolls_the_items_back_too(db, people, crig, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("disk error")
    monkeypatch.setattr(batches, "issue_to_wo", boom)
    with pytest.raises(RuntimeError):
        issue(db, people, crig)
    assert db.query(Issue).count() == 0 and db.query(Ledger).filter_by(action="ISSUE").count() == 0
    assert {ledger.get_state(db, x)["custody"] for x in crig["ids"]} == {"ASSEMBLY_AREA"}


# ---- A11: return the verified unused quantity; consumed is recorded; no double return ---------------------
def test_return_unused_records_the_consumed_balance_and_cannot_be_repeated(db, people, crig, world):
    i = issue(db, people, crig)
    issues.acknowledge_issue(db, people["prod"], i.id)
    res = issues.return_items(db, people["stores"], i.id, [], [{"batch_id": crig["batch"], "unused_quantity": 0.5}])
    assert res["consumables_settled"] == 1
    b = bal(db, crig)
    assert (b["usable"], b["outstanding"], b["consumed"]) == (98.5, 0, 1.5)
    d = issues.issue_detail(db, i.id)["consumables"][0]
    assert (d["state"], d["unused"], d["consumed"]) == ("RETURNED", 0.5, 1.5)
    with pytest.raises(ValidationError) as e:  # consumed material cannot be returned twice
        issues.return_items(db, people["stores"], i.id, [], [{"batch_id": crig["batch"], "unused_quantity": 0.5}])
    assert "not outstanding" in e.value.message
    assert bal(db, crig) == b  # nothing moved


def test_unused_quantity_is_validated(db, people, crig):
    i = issue(db, people, crig)
    for bad in (-1, 2.5, "abc"):
        with pytest.raises(ValidationError):
            issues.return_items(db, people["stores"], i.id, [], [{"batch_id": crig["batch"], "unused_quantity": bad}])
    assert bal(db, crig)["outstanding"] == 2
    with pytest.raises(PermissionDenied):
        issues.return_items(db, people["prod"], i.id, [], [{"batch_id": crig["batch"], "unused_quantity": 1}])
    issues.return_items(db, people["stores"], i.id, [], [{"batch_id": crig["batch"], "unused_quantity": 0}])  # all consumed
    assert (bal(db, crig)["usable"], bal(db, crig)["consumed"]) == (98, 2)


def test_nothing_to_return_is_refused(db, people, crig):
    i = issue(db, people, crig)
    with pytest.raises(ValidationError):
        issues.return_items(db, people["stores"], i.id, [], [])


def test_items_and_consumables_return_in_separate_steps_and_the_issue_stays_open(db, people, crig, world):
    i = issue(db, people, crig)
    issues.acknowledge_issue(db, people["prod"], i.id)
    lines = [{"item_id": x, "to_location_id": world["bins"][2]} for x in crig["ids"]]
    issues.return_items(db, people["stores"], i.id, lines)
    d = issues.issue_detail(db, i.id)
    assert d["status"] == "PARTIALLY_RETURNED" and d["outstanding"] == ["BATCH-000001"]  # coating still out
    assert issues.issue_counts(db)["outstanding_returns"] == 1
    issues.return_items(db, people["stores"], i.id, [], [{"batch_id": crig["batch"], "unused_quantity": 2}])
    assert issues.issue_detail(db, i.id)["status"] == "CLOSED" and issues.issue_counts(db)["outstanding_returns"] == 0


def test_consumables_only_outstanding_issue_is_listed_as_open(db, people, crig, world):
    i = issue(db, people, crig)
    assert issues.list_issues(db, status="open")[0] == 1
    lines = [{"item_id": x, "to_location_id": world["bins"][2]} for x in crig["ids"]]
    issues.return_items(db, people["stores"], i.id, lines)
    assert issues.list_issues(db, status="open")[0] == 1  # still open: the coating is not settled
    issues.return_items(db, people["stores"], i.id, [], [{"batch_id": crig["batch"], "unused_quantity": 1}])
    assert issues.list_issues(db, status="open")[0] == 0 and issues.list_issues(db, status="closed")[0] == 1


# ---- reversal covers both ledgers --------------------------------------------------------------------------
def test_reversing_an_issue_restores_items_and_stock_together(db, people, crig):
    i = issue(db, people, crig)
    row = db.query(Ledger).filter_by(issue_id=i.id).first()
    ledger.reverse(db, people["stores"], row.txn_id, reason="wrong WO")
    assert (bal(db, crig)["usable"], bal(db, crig)["outstanding"]) == (100, 0)
    assert {ledger.get_state(db, x)["custody"] for x in crig["ids"]} == {"ASSEMBLY_AREA"}
    d = issues.issue_detail(db, i.id)
    assert d["status"] == "VOID" and d["consumables"][0]["state"] == "VOID"
    issue(db, people, crig)  # can be issued again


def test_an_issue_cannot_be_reversed_after_its_consumables_were_settled(db, people, crig):
    i = issue(db, people, crig)
    issues.return_items(db, people["stores"], i.id, [], [{"batch_id": crig["batch"], "unused_quantity": 1}])
    row = db.query(Ledger).filter_by(issue_id=i.id, action="ISSUE").first()
    before = (db.query(Ledger).count(), db.query(BatchLedger).count())
    with pytest.raises(ValidationError):  # items are fine to reverse but the coating was already settled
        ledger.reverse(db, people["stores"], row.txn_id, reason="wrong WO")
    assert (db.query(Ledger).count(), db.query(BatchLedger).count()) == before  # nothing written on either side


def test_reversing_a_return_event_puts_the_consumables_back_outstanding(db, people, crig, world):
    i = issue(db, people, crig)
    lines = [{"item_id": x, "to_location_id": world["bins"][2]} for x in crig["ids"]]
    res = issues.return_items(db, people["stores"], i.id, lines, [{"batch_id": crig["batch"], "unused_quantity": 0.5}])
    row = db.query(Ledger).filter_by(event_id=res["event_id"]).first()
    ledger.reverse(db, people["stores"], row.txn_id, reason="returned to the wrong issue")
    b = bal(db, crig)
    assert (b["usable"], b["outstanding"], b["consumed"]) == (98, 2, 0)
    assert issues.issue_detail(db, i.id)["consumables"][0]["state"] == "ISSUED"
    assert {ledger.get_state(db, x)["custody"] for x in crig["ids"]} == {"PENDING_HANDOVER"}


def test_batch_only_reversal_is_refused_when_the_event_also_moved_items(db, people, crig):
    i = issue(db, people, crig)
    brow = db.query(BatchLedger).filter_by(issue_id=i.id).one()
    with pytest.raises(ValidationError) as e:
        batches.reverse(db, people["stores"], brow.txn_id, reason="x")
    assert "also moved items" in e.value.message


def test_consumable_picks_are_refused_when_the_assembly_has_no_consumable_line(db, people, kit, world):
    m = mappings.create_mapping(db, people["eng"], part_id="PN-9", routing_revision="R1", operation_id="OP10",
                                definition_id=kit["asm"], selected=True)
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    for k in KEYS:
        builds.fit_item(db, people["stores"], b.id, kit["items"][k])
    builds.verify_build(db, people["eng"], b.id)
    ids = [kit["items"][k] for k in KEYS]
    with pytest.raises(ValidationError) as e:
        issues.create_issue(db, people["stores"], setup_id=mk_setup(db, people, "W", m.id), wo_id="W", mapping_id=m.id, build_id=b.id, confirmed_item_ids=ids,
                            consumables=[{"line_id": 1, "batch_id": 1, "quantity": 1}])
    assert "no consumable lines" in e.value.message
    assert issues.create_issue(db, people["stores"], setup_id=mk_setup(db, people, "W", m.id), wo_id="W", mapping_id=m.id, build_id=b.id, confirmed_item_ids=ids).id
