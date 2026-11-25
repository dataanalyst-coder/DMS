import pytest
from sqlalchemy.orm import sessionmaker

from app.models import AuditLog, Issue, Item, Ledger
from app.services import assemblies, builds, designs, issues, items, ledger, mappings
from app.services.errors import ConflictError, PermissionDenied, ValidationError
from tests.conftest import clear_items, mk_setup, setup_body

KEYS = ("d1", "p1", "t1", "b1")


@pytest.fixture
def rig(db, people, kit, world):
    """A verified build of T211 and a selected ME option for PN-1/OP10."""
    m = mappings.create_mapping(db, people["eng"], part_id="PN-1", routing_revision="R1", operation_id="OP10",
                                definition_id=kit["asm"], selected=True)
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    for k in KEYS:
        builds.fit_item(db, people["stores"], b.id, kit["items"][k])
    builds.verify_build(db, people["eng"], b.id)
    kit.update(mapping=m.id, build=b.id, ids=[kit["items"][k] for k in KEYS])
    return kit


def issue(db, people, rig, who="stores", wo="WO-1", **kw):
    args = dict(setup_id=mk_setup(db, people, wo, rig["mapping"]), wo_id=wo, mapping_id=rig["mapping"], build_id=rig["build"], confirmed_item_ids=rig["ids"])
    return issues.create_issue(db, people[who], **{**args, **kw})


def state(db, rig, key):
    return ledger.get_state(db, rig["items"][key])


def ret(db, people, rig, keys, world, damaged=(), issue_id=1, bin_i=1, who="stores"):
    lines = [{"item_id": rig["items"][k], "to_location_id": world["bins"][bin_i], "damaged": k in damaged,
              "remarks": "dented" if k in damaged else None} for k in keys]
    return issues.return_items(db, people[who], issue_id, lines)


# ---- preview: a build scan expands the verified component list -------------------------------------
def test_build_scan_expands_components_and_flags_what_is_not_issuable(db, people, rig):
    p = issues.issue_preview(db, rig["build"])
    assert p["can_issue"] and [c["display_id"] for c in p["components"]] == ["D1", "P1", "T1", "B1"]
    assert {l["design_code"]: (l["required"], l["filled"]) for l in p["lines"]} == {
        "DIE-32": (1, 1), "PAT-1": (1, 1), "TF-A": (1, 1), "BF-A": (1, 1)}
    ledger.set_condition(db, people["tooling"], [rig["items"]["t1"]], "HOLD", reason="crack")
    p = issues.issue_preview(db, rig["build"])
    assert not p["can_issue"] and "T1 is HOLD" in p["problems"]  # a held part is shown, not concealed


# ---- issue ---------------------------------------------------------------------------------------------
def test_issue_moves_all_parts_to_pending_handover_in_one_event(db, people, rig):
    i = issue(db, people, rig)
    d = issues.issue_detail(db, i.id)
    assert d["status"] == "PENDING_HANDOVER" and d["issued_by"] == "stores" and d["wo_id"] == "WO-1"
    assert d["part_id"] == "PN-1" and d["operation_id"] == "OP10" and len(d["lines"]) == 4
    assert {state(db, rig, k)["custody"] for k in KEYS} == {"PENDING_HANDOVER"}
    rows = db.query(Ledger).filter_by(issue_id=i.id).all()
    assert len(rows) == 4 and len({r.event_id for r in rows}) == 1 and {r.action for r in rows} == {"ISSUE"}
    # a pending hand-over is not free stock
    assert items.search_items(db, custody="STORES")[1] and not {r["display_id"] for r in items.search_items(db, custody="STORES")[1]} & {"D1", "P1", "T1", "B1"}
    assert not builds.build_detail(db, rig["build"])["ready_to_issue"]


def test_only_stores_issues_and_only_production_acknowledges(db, people, rig):
    for who in ("prod", "eng", "tooling"):
        with pytest.raises(PermissionDenied):
            issue(db, people, rig, who=who)
    i = issue(db, people, rig)
    for who in ("stores", "tooling"):
        with pytest.raises(PermissionDenied):
            issues.acknowledge_issue(db, people[who], i.id)
    issues.acknowledge_issue(db, people["prod"], i.id)
    d = issues.issue_detail(db, i.id)
    assert d["status"] == "ACTIVE" and d["received_by"] == ["prod"]
    assert {state(db, rig, k)["custody"] for k in KEYS} == {"PRODUCTION"}
    assert {l["state"] for l in d["lines"]} == {"IN_PRODUCTION"} and all(l["since"] for l in d["lines"])
    with pytest.raises(ValidationError):  # nothing left to acknowledge
        issues.acknowledge_issue(db, people["prod"], i.id)


def test_wo_is_required_and_issue_is_audited(db, people, rig):
    with pytest.raises(ValidationError):
        issue(db, people, rig, wo=" ")
    i = issue(db, people, rig)
    row = db.query(AuditLog).filter_by(entity_type="issue", action="ISSUE").one()
    assert row.entity_id == str(i.id) and row.username == "stores" and "WO-1" in row.after


# ---- the confirmed list must match what the scan expanded --------------------------------------------
def test_confirmation_must_match_the_expanded_build(db, people, rig):
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig, confirmed_item_ids=rig["ids"][:3])
    assert "not confirmed: B1" in e.value.message
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig, confirmed_item_ids=rig["ids"] + [rig["items"]["d2"]])
    assert "not in build: D2" in e.value.message
    with pytest.raises(ValidationError):
        issue(db, people, rig, confirmed_item_ids=rig["ids"] + [rig["ids"][0]])
    assert db.query(Issue).count() == 0 and state(db, rig, "d1")["custody"] == "ASSEMBLY_AREA"


def test_duplicate_scans_and_parts_from_another_build_are_rejected(db, people, rig):
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig, scanned_item_ids=[rig["items"]["d1"], rig["items"]["d1"]])
    assert "Duplicate scan" in e.value.message
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig, scanned_item_ids=[rig["items"]["d2"]])
    assert "not fitted in build" in e.value.message
    issue(db, people, rig, scanned_item_ids=[rig["items"]["d1"], rig["items"]["t1"]])  # scanning some parts is fine


# ---- wrong configuration / unverified / unready --------------------------------------------------------
def test_wrong_assembly_option_or_old_revision_is_rejected(db, people, rig):
    eng = people["eng"]
    other = assemblies.create_assembly(db, eng, code="x-o", description="o", category_id=rig["asm_cat"])
    r = assemblies.revisions(db, other.id)[0].id
    assemblies.add_line(db, eng, r, design_id=rig["die"])
    assemblies.approve_revision(db, eng, r)
    wrong = mappings.create_mapping(db, eng, part_id="PN-1", routing_revision="R1", operation_id="OP10", definition_id=other.id)
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig, mapping_id=wrong.id)
    assert "Wrong configuration" in e.value.message
    new = assemblies.new_revision(db, eng, rig["asm"], reason="change")  # T211 moves to rev B; the build is rev A
    assemblies.approve_revision(db, eng, new.id)
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig)
    assert "Wrong configuration" in e.value.message and "revision B" in e.value.message


def test_option_that_is_not_selectable_cannot_be_issued(db, people, rig):
    mappings.update_mapping(db, people["eng"], rig["mapping"], fields_set={"tooling_status"}, tooling_status="TO_BE_DEVELOPED")
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig)
    assert "cannot be issued now" in e.value.message and "to be developed" in e.value.message


def test_unverified_or_unserviceable_builds_are_rejected(db, people, rig, world):
    builds.remove_item(db, people["stores"], rig["build"], rig["items"]["b1"], world["bins"][1], reason="swap")
    with pytest.raises(ValidationError) as e:  # fell back to ASSEMBLING
        issue(db, people, rig)
    assert "ASSEMBLING" in e.value.message or "does not match" in e.value.message
    builds.fit_item(db, people["stores"], rig["build"], rig["items"]["b1"])
    builds.verify_build(db, people["eng"], rig["build"])
    ledger.set_condition(db, people["tooling"], [rig["items"]["d1"]], "HOLD", reason="crack")  # condition re-checked now
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig)
    assert "D1 is HOLD" in e.value.message


# ---- A07: two issues of the same parts: exactly one succeeds ------------------------------------------------
def test_second_issue_of_the_same_build_is_rejected_naming_the_first(db, people, rig):
    issue(db, people, rig, wo="WO-1")
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig, wo="WO-2")
    assert "WO WO-1" in e.value.message and "cannot be issued again" in e.value.message
    assert db.query(Issue).count() == 1 and db.query(Ledger).filter_by(action="ISSUE").count() == 4


def test_competing_issue_on_stale_state_collides_instead_of_double_issuing(db, engine, people, rig, monkeypatch):
    stale = {i: ledger.get_state(db, i) for i in rig["ids"]}  # what a slow second request already read
    issue(db, people, rig, wo="WO-1")
    s2 = sessionmaker(bind=engine, autoflush=False, future=True)()
    from app.models import User
    from app.services import auth
    stores2 = auth.load_roles(s2, s2.query(User).filter_by(username="stores").one())
    monkeypatch.setattr(ledger, "get_state", lambda d, i: stale[i])
    with pytest.raises(ConflictError):
        issues.create_issue(s2, stores2, setup_id=mk_setup(db, people, "WO-2", rig["mapping"]), wo_id="WO-2", mapping_id=rig["mapping"], build_id=rig["build"],
                            confirmed_item_ids=rig["ids"])
    s2.close()
    monkeypatch.undo()
    assert db.query(Issue).count() == 1 and db.query(Ledger).filter_by(action="ISSUE").count() == 4


def test_no_component_changes_while_issued(db, people, rig, world):
    issue(db, people, rig)
    with pytest.raises(ValidationError):
        builds.remove_item(db, people["stores"], rig["build"], rig["items"]["d1"], world["bins"][1], reason="x")
    with pytest.raises(ValidationError):
        builds.move_build(db, people["stores"], rig["build"], world["bins"][1])
    with pytest.raises(ValidationError):
        ledger.move_items(db, people["stores"], [rig["items"]["d1"]], world["bins"][1])
    assert items.item_detail(db, rig["items"]["d1"])["issue"]["wo_id"] == "WO-1"


# ---- A08: partial return -------------------------------------------------------------------------------------
def test_return_three_of_four_leaves_the_fourth_outstanding_and_inspection_required(db, people, rig, world):
    i = issue(db, people, rig)
    issues.acknowledge_issue(db, people["prod"], i.id)
    res = ret(db, people, rig, ("d1", "p1", "t1"), world, damaged=("t1",))
    assert res["returned"] == 3 and res["outstanding"] == ["B1"]
    d = issues.issue_detail(db, i.id)
    assert d["status"] == "PARTIALLY_RETURNED" and d["outstanding"] == ["B1"]
    assert (state(db, rig, "d1")["item_condition"], state(db, rig, "d1")["custody"]) == ("AWAITING_INSPECTION", "ASSEMBLY_AREA")
    assert state(db, rig, "t1")["item_condition"] == "HOLD"  # the damaged one is held at once
    assert state(db, rig, "b1")["custody"] == "PRODUCTION"  # not returned: stays with production
    assert state(db, rig, "d1")["location_id"] == world["bins"][1]
    # returned items stay unavailable until cleared: the build cannot be issued again
    assert issues.issue_counts(db)["outstanding_returns"] == 1
    with pytest.raises(ValidationError):
        issue(db, people, rig, wo="WO-2")


def test_failed_item_stays_held_while_the_rest_are_cleared_and_returns_complete_later(db, people, rig, world):
    i = issue(db, people, rig)
    issues.acknowledge_issue(db, people["prod"], i.id)
    ret(db, people, rig, ("d1", "p1", "t1"), world, damaged=("t1",))
    clear_items(db, people, rig["items"]["d1"], rig["items"]["p1"])  # inspected and released one by one
    assert state(db, rig, "t1")["item_condition"] == "HOLD"  # one failed item does not clear or condemn the others
    ret(db, people, rig, ("b1",), world)  # the fourth comes back later
    d = issues.issue_detail(db, i.id)
    assert d["status"] == "CLOSED" and d["outstanding"] == [] and issues.issue_counts(db)["outstanding_returns"] == 0


def test_return_validation(db, people, rig, world):
    i = issue(db, people, rig)
    with pytest.raises(PermissionDenied):
        ret(db, people, rig, ("d1",), world, who="prod")
    with pytest.raises(ValidationError):
        issues.return_items(db, people["stores"], i.id, [])
    with pytest.raises(ValidationError):  # not part of this issue
        issues.return_items(db, people["stores"], i.id, [{"item_id": rig["items"]["d2"], "to_location_id": world["bins"][1]}])
    with pytest.raises(ValidationError) as e:  # damage must be described
        issues.return_items(db, people["stores"], i.id, [{"item_id": rig["ids"][0], "to_location_id": world["bins"][1], "damaged": True}])
    assert "Describe the damage" in e.value.message
    ret(db, people, rig, ("d1",), world)
    with pytest.raises(ValidationError):  # already returned
        ret(db, people, rig, ("d1",), world)


# ---- A09: return an intact assembly, then dismantle it -----------------------------------------------------
def test_return_intact_then_dismantle_keeps_history_with_no_duplicate_availability(db, people, rig, world):
    i = issue(db, people, rig)
    issues.acknowledge_issue(db, people["prod"], i.id)
    ret(db, people, rig, KEYS, world)
    assert issues.issue_detail(db, i.id)["status"] == "CLOSED"
    assert builds.build_detail(db, rig["build"])["status"] == "VERIFIED"  # the intact build returned without dismantling
    dest = {rig["items"][k]: world["bins"][2] for k in KEYS}
    builds.dismantle_build(db, people["stores"], rig["build"], dest, reason="design change")
    for k in KEYS:
        s = state(db, rig, k)
        assert (s["custody"], s["location_id"], s["item_condition"]) == ("STORES", world["bins"][2], "AWAITING_INSPECTION")
    hist = builds.build_detail(db, rig["build"])["history"]
    assert len(hist) == 4 and all(h["removed_at"] for h in hist)
    stores_ids = [r["display_id"] for r in items.search_items(db, custody="STORES", limit=100)[1]]
    assert len(stores_ids) == len(set(stores_ids)) == 7  # every item exactly once; none in two places
    from app.models import BuildComponent
    assert db.query(BuildComponent).filter_by(removed_at=None).count() == 0


# ---- reversals work because status is derived from the ledger -------------------------------------------------
def test_reversing_an_unacknowledged_issue_voids_it(db, people, rig):
    i = issue(db, people, rig)
    row = db.query(Ledger).filter_by(issue_id=i.id).first()
    with pytest.raises(PermissionDenied):
        ledger.reverse(db, people["prod"], row.txn_id, reason="wrong WO")
    ledger.reverse(db, people["stores"], row.txn_id, reason="wrong WO")
    assert issues.issue_detail(db, i.id)["status"] == "VOID"
    assert {state(db, rig, k)["custody"] for k in KEYS} == {"ASSEMBLY_AREA"}
    issue(db, people, rig, wo="WO-1B")  # can be issued again


def test_partial_returns_reverse_independently(db, people, rig, world):
    i = issue(db, people, rig)
    issues.acknowledge_issue(db, people["prod"], i.id)
    first = ret(db, people, rig, ("d1", "p1"), world)
    ret(db, people, rig, ("t1",), world)
    first_row = db.query(Ledger).filter_by(event_id=first["event_id"]).first()
    ledger.reverse(db, people["stores"], first_row.txn_id, reason="wrong shelf")
    assert state(db, rig, "d1")["custody"] == "PRODUCTION" and state(db, rig, "p1")["custody"] == "PRODUCTION"
    assert state(db, rig, "t1")["custody"] == "ASSEMBLY_AREA"  # the second return stands
    assert sorted(issues.issue_detail(db, i.id)["outstanding"]) == ["B1", "D1", "P1"]


def test_acknowledgement_can_be_reversed_by_production(db, people, rig):
    i = issue(db, people, rig)
    issues.acknowledge_issue(db, people["prod"], i.id)
    row = db.query(Ledger).filter_by(issue_id=i.id, action="ACKNOWLEDGE").first()
    with pytest.raises(PermissionDenied):
        ledger.reverse(db, people["stores"], row.txn_id, reason="x")
    ledger.reverse(db, people["prod"], row.txn_id, reason="signed by mistake")
    assert issues.issue_detail(db, i.id)["status"] == "PENDING_HANDOVER"


def test_open_issue_lookups_and_listing(db, people, rig, world):
    i = issue(db, people, rig)
    assert issues.open_issue_for_item(db, rig["items"]["d1"])[1] == "PENDING_HANDOVER"
    assert issues.open_issue_for_item(db, rig["items"]["d2"]) is None
    assert issues.list_issues(db, status="open")[0] == 1 and issues.list_issues(db, wo_id="WO-1")[0] == 1
    assert issues.list_issues(db, status="closed")[0] == 0
    assert issues.list_issues(db, item_id=rig["items"]["t1"])[1][0]["id"] == i.id


def test_a_return_without_a_location_goes_back_to_each_parts_home_bin(db, people, rig):
    i = issue(db, people, rig)
    issues.acknowledge_issue(db, people["prod"], i.id)
    items_ = [db.get(Item, x) for x in rig["ids"]]
    res = issues.return_items(db, people["stores"], i.id, [{"item_id": it.id} for it in items_])
    assert res["returned"] == 4 and res["outstanding"] == []
    for it in items_:
        assert ledger.get_state(db, it.id)["location_id"] == it.home_location_id
    with pytest.raises(ValidationError):  # nothing left to return
        issues.return_items(db, people["stores"], i.id, [{"item_id": items_[0].id}])
