import pytest
from sqlalchemy.exc import IntegrityError

from app.models import AuditLog, Build, BuildComponent, Ledger
from app.services import assemblies, builds, designs, items, ledger
from app.services.common import transaction
from app.services.errors import ConflictError, PermissionDenied, ValidationError
from tests.conftest import clear_items


def new_build(db, people, kit, world, who="stores", bin_i=0):
    return builds.create_build(db, people[who], definition_id=kit["asm"], location_id=world["bins"][bin_i])


def fit(db, people, b, kit, *keys):
    for k in keys:
        builds.fit_item(db, people["stores"], b.id, kit["items"][k])


def full_build(db, people, kit, world):
    b = new_build(db, people, kit, world)
    fit(db, people, b, kit, "d1", "p1", "t1", "b1")
    return b


def st(db, kit, key):
    return ledger.get_state(db, kit["items"][key])


def detail(db, b):
    return builds.build_detail(db, b.id)


# ---- create ---------------------------------------------------------------------------
def test_only_stores_or_tooling_build_and_revision_must_be_approved(db, people, kit, world):
    for who in ("prod", "mgr", "eng"):
        with pytest.raises(PermissionDenied):
            new_build(db, people, kit, world, who=who)
    assert new_build(db, people, kit, world, who="tooling").assembler_id == people["tooling"].id
    d = assemblies.create_assembly(db, people["eng"], code="dr", description="d", category_id=kit["asm_cat"])
    with pytest.raises(ValidationError) as e:
        builds.create_build(db, people["stores"], definition_id=d.id, location_id=world["bins"][0])
    assert "no approved revision" in e.value.message


# ---- fitting and rejection rules ------------------------------------------------------
def test_full_build_takes_location_and_assembly_area_custody(db, people, kit, world):
    b = new_build(db, people, kit, world, bin_i=1)
    fit(db, people, b, kit, "d1", "p1", "t1", "b1")
    for k in ("d1", "p1", "t1", "b1"):
        s = st(db, kit, k)
        assert (s["custody"], s["item_condition"], s["location_id"]) == ("ASSEMBLY_AREA", "SERVICEABLE", world["bins"][1])
    # fitted items are no longer individually available in stores
    total, rows = items.search_items(db, custody="STORES")
    assert {r["display_id"] for r in rows} == {"D2", "P2", "P3"}
    assert items.item_detail(db, kit["items"]["d1"])["build"]["code"] == f"BLD-{b.id:06d}"
    assert items.item_detail(db, kit["items"]["d2"])["build"] is None


def test_wrong_design_wrong_revision_and_no_free_place_are_rejected(db, people, kit, world):
    b = new_build(db, people, kit, world)
    fit(db, people, b, kit, "d1", "p1")
    with pytest.raises(ValidationError) as e:  # second die: the only die position is taken
        builds.fit_item(db, people["stores"], b.id, kit["items"]["d2"])
    assert "no free place" in e.value.message and "D2" in e.value.message
    with pytest.raises(ValidationError) as e:  # pattern slot taken too (substitute pattern is no exception)
        builds.fit_item(db, people["stores"], b.id, kit["items"]["p2"])
    assert "no free place" in e.value.message
    other = designs.create_design(db, people["eng"], code="stray", description="stray", category_id=kit["asm_cat"], base_uom="EA")
    designs.approve_revision(db, people["eng"], designs.revisions(db, other.id)[0].id)
    stray = items.register_item(db, people["stores"], design_id=other.id, legacy_no="s1", condition="SERVICEABLE",
                                location_id=world["bins"][0])
    with pytest.raises(ValidationError) as e:
        builds.fit_item(db, people["stores"], b.id, stray.id)
    assert "wrong item" in e.value.message
    # wrong revision: item built to rev A of the die; Engineering approves rev B; default rule = current only
    designs.new_revision(db, people["eng"], kit["die"], reason="bore changed")
    designs.approve_revision(db, people["eng"], designs.revisions(db, kit["die"])[-1].id)
    b2 = new_build(db, people, kit, world)
    with pytest.raises(ValidationError) as e:
        builds.fit_item(db, people["stores"], b2.id, kit["items"]["d2"])
    assert "wrong revision" in e.value.message and "revision A" in e.value.message


def test_line_can_allow_an_older_revision_explicitly(db, people, kit, world):
    eng = people["eng"]
    designs.new_revision(db, eng, kit["die"], reason="bore changed")
    designs.approve_revision(db, eng, designs.revisions(db, kit["die"])[-1].id)
    r = assemblies.new_revision(db, eng, kit["asm"], reason="rev A dies are still fine")
    assemblies.update_line(db, eng, [l for l in assemblies.revision_dict(db, r)["lines"] if l["design_code"] == "DIE-32"][0]["id"],
                           allowed_revisions="A,B")
    assemblies.approve_revision(db, eng, r.id)
    b = new_build(db, people, kit, world)
    builds.fit_item(db, people["stores"], b.id, kit["items"]["d1"])  # d1 is rev A: allowed now


@pytest.mark.parametrize("condition", ["HOLD", "AWAITING_INSPECTION"])
def test_only_serviceable_items_can_be_fitted(db, people, kit, world, condition):
    ledger.set_condition(db, people["tooling"], [kit["items"]["d1"]], "HOLD", reason="crack")
    if condition == "AWAITING_INSPECTION":
        ledger.set_condition(db, people["tooling"], [kit["items"]["d1"]], "UNDER_REPAIR", reason="weld")
        ledger.set_condition(db, people["tooling"], [kit["items"]["d1"]], "AWAITING_INSPECTION", reason="done")
    b = new_build(db, people, kit, world)
    with pytest.raises(ValidationError) as e:
        builds.fit_item(db, people["stores"], b.id, kit["items"]["d1"])
    assert condition in e.value.message and "only serviceable" in e.value.message
    assert not builds.members(db, b.id)


def test_items_out_of_stores_or_retired_cannot_be_fitted(db, people, kit, world):
    b = new_build(db, people, kit, world)
    d1 = kit["items"]["d1"]
    s = ledger.get_state(db, d1)
    ledger._append(db, d1, "MOVE", event_id="e", prev=s, user=people["stores"], to_condition="SERVICEABLE",
                   to_custody="PRODUCTION", to_location_id=s["location_id"])  # simulate an issued item
    db.commit()
    with pytest.raises(ValidationError) as e:
        builds.fit_item(db, people["stores"], b.id, d1)
    assert "PRODUCTION custody" in e.value.message
    items.retire_item(db, people["eng"], kit["items"]["d2"], reason="obsolete")
    with pytest.raises(ValidationError) as e:
        builds.fit_item(db, people["stores"], b.id, kit["items"]["d2"])
    assert "retired" in e.value.message


def test_substitute_design_fits_the_substitutable_line(db, people, kit, world):
    b = new_build(db, people, kit, world)
    fit(db, people, b, kit, "p2")  # PAT-2 is an approved substitute for PAT-1
    d = detail(db, b)
    assert d["components"][0]["design_code"] == "PAT-2"
    pat_line = next(l for l in d["lines"] if l["design_code"] == "PAT-1")
    assert pat_line["filled"] == 1 and d["components"][0]["fills_line_id"] == pat_line["line_id"]


def test_item_in_another_build_must_be_removed_there_first(db, people, kit, world):
    b1, b2 = full_build(db, people, kit, world), new_build(db, people, kit, world, bin_i=1)
    with pytest.raises(ValidationError) as e:
        builds.fit_item(db, people["stores"], b2.id, kit["items"]["d1"])
    assert f"BLD-{b1.id:06d}" in e.value.message and "Remove it from there first" in e.value.message
    builds.remove_item(db, people["stores"], b1.id, kit["items"]["d1"], world["bins"][2], reason="needed elsewhere")
    d1 = detail(db, b1)
    assert not d1["complete"] and not d1["ready_to_issue"] and "missing 1 x DIE-32" in d1["problems"][0]
    builds.fit_item(db, people["stores"], b2.id, kit["items"]["d1"])
    assert items.item_detail(db, kit["items"]["d1"])["build"]["id"] == b2.id


def test_database_enforces_one_active_membership(db, people, kit, world):
    b1, b2 = new_build(db, people, kit, world), new_build(db, people, kit, world)
    fit(db, people, b1, kit, "d1")
    db.add(BuildComponent(build_id=b2.id, item_id=kit["items"]["d1"], fitted_by=people["stores"].id, fit_event_id="x"))
    with pytest.raises(ConflictError):
        with transaction(db):
            db.flush()
    assert db.query(BuildComponent).filter_by(item_id=kit["items"]["d1"], removed_at=None).count() == 1


# ---- verification ---------------------------------------------------------------------
def test_verify_needs_a_complete_build_and_records_the_verifier(db, people, kit, world):
    b = new_build(db, people, kit, world)
    fit(db, people, b, kit, "d1", "p1", "t1")
    with pytest.raises(ValidationError) as e:
        builds.verify_build(db, people["stores"], b.id)
    assert "missing 1 x BF-A" in e.value.message
    fit(db, people, b, kit, "b1")
    with pytest.raises(PermissionDenied):
        builds.verify_build(db, people["prod"], b.id)
    builds.verify_build(db, people["eng"], b.id)
    d = detail(db, b)
    assert d["status"] == "VERIFIED" and d["assembler"] == "stores" and d["verified_by"] == "eng"
    assert d["ready_to_issue"] and d["complete"] and d["problems"] == []
    with pytest.raises(ValidationError):
        builds.verify_build(db, people["eng"], b.id)  # already verified


def test_any_component_change_requires_verifying_again(db, people, kit, world):
    b = full_build(db, people, kit, world)
    builds.verify_build(db, people["eng"], b.id)
    res = builds.replace_component(db, people["stores"], b.id, kit["items"]["p1"], kit["items"]["p3"],
                                   world["bins"][2], reason="p1 worn")
    d = detail(db, b)
    assert d["status"] == "ASSEMBLING" and d["verified_by"] is None and not d["ready_to_issue"]
    rows = db.query(Ledger).filter(Ledger.event_id == res["event_id"]).order_by(Ledger.txn_id).all()
    assert [r.action for r in rows] == ["REMOVE", "FIT"]  # explicit remove/add in one event
    assert st(db, kit, "p1")["custody"] == "STORES" and st(db, kit, "p1")["location_id"] == world["bins"][2]
    assert st(db, kit, "p3")["custody"] == "ASSEMBLY_AREA"
    builds.verify_build(db, people["eng"], b.id)
    assert detail(db, b)["status"] == "VERIFIED"


def test_failed_replacement_changes_nothing(db, people, kit, world):
    b = full_build(db, people, kit, world)
    before = db.query(Ledger).count()
    with pytest.raises(ValidationError):  # d2 is a die, but the pattern slot is what p1 leaves
        builds.replace_component(db, people["stores"], b.id, kit["items"]["p1"], kit["items"]["d2"],
                                 world["bins"][2], reason="oops")
    assert db.query(Ledger).count() == before
    assert st(db, kit, "p1")["custody"] == "ASSEMBLY_AREA" and len(builds.members(db, b.id)) == 4


def test_build_with_a_held_component_is_not_ready_and_cannot_be_verified(db, people, kit, world):
    b = full_build(db, people, kit, world)
    builds.verify_build(db, people["eng"], b.id)
    ledger.set_condition(db, people["tooling"], [kit["items"]["t1"]], "HOLD", reason="crack")
    d = detail(db, b)
    assert d["status"] == "VERIFIED" and not d["ready_to_issue"] and "T1 is HOLD" in d["problems"]
    builds.remove_item(db, people["stores"], b.id, kit["items"]["t1"], world["bins"][2], reason="held part out")
    with pytest.raises(ValidationError):  # the held item cannot go back in
        builds.fit_item(db, people["stores"], b.id, kit["items"]["t1"])


def test_fitted_items_can_be_held_and_inspected_in_place_but_not_repaired_or_scrapped(db, people, kit, world):
    b = full_build(db, people, kit, world)
    d1 = kit["items"]["d1"]
    ledger.set_condition(db, people["tooling"], [d1], "AWAITING_INSPECTION", reason="routine check")
    ledger.set_condition(db, people["tooling"], [d1], "HOLD", reason="suspect crack")
    with pytest.raises(ValidationError) as e:  # repair needs the part out of the build first
        ledger.set_condition(db, people["tooling"], [d1], "UNDER_REPAIR", reason="weld")
    assert f"BLD-{b.id:06d}" in e.value.message
    with pytest.raises(ValidationError) as e:
        ledger.set_condition(db, people["scrap"], [d1], "SCRAPPED", reason="beyond repair")
    assert f"BLD-{b.id:06d}" in e.value.message
    clear_items(db, people, d1)  # further inspection, inspect and release: all in place
    assert st(db, kit, "d1")["item_condition"] == "SERVICEABLE" and st(db, kit, "d1")["custody"] == "ASSEMBLY_AREA"


# ---- moving an intact build ----------------------------------------------------------
def test_intact_build_moves_together_and_parts_cannot_be_moved_alone(db, people, kit, world):
    b = full_build(db, people, kit, world)
    with pytest.raises(ValidationError) as e:
        ledger.move_items(db, people["stores"], [kit["items"]["d1"]], world["bins"][2])
    assert f"BLD-{b.id:06d}" in e.value.message and "move the whole build" in e.value.message
    res = builds.move_build(db, people["stores"], b.id, world["bins"][2])
    assert res["items"] == 4
    assert {st(db, kit, k)["location_id"] for k in ("d1", "p1", "t1", "b1")} == {world["bins"][2]}
    assert detail(db, b)["location"].endswith("B3")
    with pytest.raises(PermissionDenied):
        builds.move_build(db, people["prod"], b.id, world["bins"][0])
    # the whole move is one event and reverses as one
    ledger.reverse(db, people["stores"], db.query(Ledger).filter_by(event_id=res["event_id"]).first().txn_id,
                   reason="wrong rack")
    assert {st(db, kit, k)["location_id"] for k in ("d1", "p1", "t1", "b1")} == {world["bins"][0]}


def test_fit_and_remove_ledger_rows_cannot_be_reversed(db, people, kit, world):
    b = new_build(db, people, kit, world)
    fit(db, people, b, kit, "d1")
    row = db.query(Ledger).filter_by(action="FIT").one()
    with pytest.raises(ValidationError) as e:
        ledger.reverse(db, people["stores"], row.txn_id, reason="undo")
    assert "cannot be reversed" in e.value.message and row.build_id == b.id
    assert st(db, kit, "d1")["custody"] == "ASSEMBLY_AREA"


# ---- dismantling (spec A09, build half) ---------------------------------------------------
def test_dismantle_needs_a_destination_for_every_component_and_keeps_history(db, people, kit, world):
    b = full_build(db, people, kit, world)
    keys = ("d1", "p1", "t1", "b1")
    dest = {kit["items"][k]: world["bins"][i % 3] for i, k in enumerate(keys)}
    short = {k: v for k, v in dest.items() if k != kit["items"]["b1"]}
    with pytest.raises(ValidationError) as e:
        builds.dismantle_build(db, people["stores"], b.id, short, reason="stand-down")
    assert "missing: B1" in e.value.message and detail(db, b)["status"] != "DISMANTLED"
    with pytest.raises(ValidationError):
        builds.dismantle_build(db, people["stores"], b.id, dest, reason=" ")
    with pytest.raises(PermissionDenied):
        builds.dismantle_build(db, people["prod"], b.id, dest, reason="x")
    builds.dismantle_build(db, people["stores"], b.id, dest, reason="design change")
    d = detail(db, b)
    assert d["status"] == "DISMANTLED" and d["components"] == [] and len(d["history"]) == 4
    assert all(h["removal_reason"] == "design change" and h["removed_at"] for h in d["history"])
    for k in keys:
        s = st(db, kit, k)
        assert (s["custody"], s["location_id"]) == ("STORES", dest[kit["items"][k]])
        assert items.item_detail(db, kit["items"][k])["build"] is None
    # each part is individually selectable again and no part is double counted
    assert {r["display_id"] for r in items.search_items(db, custody="STORES")[1]} == {"D1", "D2", "P1", "P2", "P3", "T1", "B1"}
    assert db.query(BuildComponent).filter_by(removed_at=None).count() == 0
    with pytest.raises(ValidationError):
        builds.fit_item(db, people["stores"], b.id, kit["items"]["d1"])  # dismantled builds are closed
    b2 = new_build(db, people, kit, world)
    fit(db, people, b2, kit, "d1", "p1", "t1", "b1")  # components reused in a new build
    assert len(builds.members(db, b2.id)) == 4


def test_dismantled_event_is_one_event_with_a_row_per_component(db, people, kit, world):
    b = full_build(db, people, kit, world)
    dest = {kit["items"][k]: world["bins"][1] for k in ("d1", "p1", "t1", "b1")}
    res = builds.dismantle_build(db, people["tooling"], b.id, dest, reason="stand-down")
    rows = db.query(Ledger).filter_by(event_id=res["event_id"]).all()
    assert len(rows) == 4 and {r.action for r in rows} == {"REMOVE"} and {r.build_id for r in rows} == {b.id}


# ---- audit & permissions ----------------------------------------------------------------
def test_build_actions_are_audited_with_user(db, people, kit, world):
    b = full_build(db, people, kit, world)
    builds.verify_build(db, people["eng"], b.id)
    builds.remove_item(db, people["tooling"], b.id, kit["items"]["d1"], world["bins"][1], reason="swap")
    rows = db.query(AuditLog).filter_by(entity_type="build", entity_id=str(b.id)).all()
    by = {(r.action, r.username) for r in rows}
    assert {("CREATE", "stores"), ("FIT", "stores"), ("VERIFY", "eng"), ("REMOVE", "tooling")} <= by
    assert [r.reason for r in rows if r.action == "REMOVE"] == ["swap"]


def test_remove_needs_reason_and_destination(db, people, kit, world):
    b = full_build(db, people, kit, world)
    with pytest.raises(ValidationError):
        builds.remove_item(db, people["stores"], b.id, kit["items"]["d1"], world["bins"][1], reason=" ")
    with pytest.raises(ValidationError):  # not a member of this build
        builds.remove_item(db, people["stores"], b.id, kit["items"]["d2"], world["bins"][1], reason="x")
    assert db.query(Build).count() == 1


def test_list_builds_filters_by_item(db, people, kit, world):
    b = full_build(db, people, kit, world)
    new_build(db, people, kit, world)
    total, rows = builds.list_builds(db, item_id=kit["items"]["d1"])
    assert total == 1 and rows[0]["id"] == b.id and rows[0]["components"] == 4
    assert builds.list_builds(db, status="ASSEMBLING")[0] == 2
