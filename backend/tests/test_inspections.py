import pytest

from app.models import AuditLog, Inspection, InspectionResult, Ledger
from app.services import builds, designs, inspections, issues, items, ledger, mappings, storage
from app.services.errors import PermissionDenied, ValidationError
from tests.conftest import clear_items, mk_setup, setup_body

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


def awaiting(db, people, item):
    """Put an item into AWAITING_INSPECTION via the legal route."""
    ledger.set_condition(db, people["tooling"], [item.id], "AWAITING_INSPECTION", reason="routine check")
    return item


def check_by_code(db, item, code):
    return next(c for c in inspections.effective_checklist(db, item)["checks"] if c["code"] == code)


def pass_all(db, people, insp_id, item, overrides=None):
    overrides = overrides or {}
    for c in inspections.effective_checklist(db, item)["checks"]:
        if c["id"] in overrides:
            inspections.record_result(db, people["tooling"], insp_id, c["id"], **overrides[c["id"]])
        else:
            inspections.record_result(db, people["tooling"], insp_id, c["id"], result="PASS")


# ---- checklist configuration (data, versioned, approved) ------------------------------------------
def test_starter_checklist_has_the_agreed_starting_content(db, people, world):
    cat = world["cat"]
    from app.services import config
    new_cat = config.create_category(db, people["admin"], "CORE", "Core").id
    v = inspections.apply_starter(db, people["eng"], "CATEGORY", new_cat)
    codes = [c["code"] for c in inspections.template_detail(db, v.template_id)["versions"][0]["checks"]]
    assert codes == ["COMPLETE_ID", "CLEAN", "CRACKS", "WEAR", "DAMAGE", "CRIT_DIMS", "FASTENERS", "COATING", "STORAGE_READY"]
    assert v.status == "DRAFT"  # Engineering tailors and approves it: starting content, not universal limits
    assert cat != new_cat


def test_only_engineering_edits_and_approves_checklists(db, people, world):
    from app.services import config
    cat = config.create_category(db, people["admin"], "CORE", "Core").id
    for who in ("stores", "tooling"):
        with pytest.raises(PermissionDenied):
            inspections.create_template(db, people[who], "CATEGORY", cat)
    v = inspections.create_template(db, people["eng"], "CATEGORY", cat)
    inspections.add_check(db, people["eng"], v.id, code="a b", title="A")
    for who in ("tooling", "stores"):
        with pytest.raises(PermissionDenied):
            inspections.approve_version(db, people[who], v.id)
    inspections.approve_version(db, people["eng"], v.id)
    with pytest.raises(ValidationError):  # already approved
        inspections.approve_version(db, people["eng"], v.id)


def test_check_validation(db, people, world):
    from app.services import config
    cat = config.create_category(db, people["admin"], "CORE", "Core").id
    eng, v = people["eng"], inspections.create_template(db, people["eng"], "CATEGORY", cat)
    with pytest.raises(ValidationError):
        inspections.approve_version(db, eng, v.id)  # empty checklist
    for kw in (dict(check_type="MEASUREMENT"), dict(check_type="MEASUREMENT", min_value=1),  # no unit / no limits
               dict(check_type="MEASUREMENT", unit="mm", min_value=5, max_value=1), dict(check_type="BOGUS"),
               dict(evidence="SOMETIMES")):
        with pytest.raises(ValidationError):
            inspections.add_check(db, eng, v.id, code="X", title="X", **kw)
    inspections.add_check(db, eng, v.id, code="BORE", title="Bore", check_type="MEASUREMENT", unit="mm", min_value=269.5, max_value=270.5)
    with pytest.raises(ValidationError):
        inspections.add_check(db, eng, v.id, code="bore", title="dup")


def test_approved_versions_are_immutable_and_new_versions_copy_checks(db, people, world):
    eng = people["eng"]
    tpl = inspections.list_templates(db)[0]
    v1 = inspections.current_version(db, tpl["id"])
    first = inspections.template_detail(db, tpl["id"])["versions"][0]["checks"][0]
    with pytest.raises(ValidationError):
        inspections.add_check(db, eng, v1.id, code="NEW", title="New")
    with pytest.raises(ValidationError):
        inspections.update_check(db, eng, first["id"], title="sneaky")
    with pytest.raises(ValidationError):
        inspections.remove_check(db, eng, first["id"])
    with pytest.raises(ValidationError):
        inspections.new_version(db, eng, tpl["id"], reason=" ")
    v2 = inspections.new_version(db, eng, tpl["id"], reason="add wear check")
    assert v2.version == "2" and len(inspections.template_detail(db, tpl["id"])["versions"][1]["checks"]) == 2
    inspections.add_check(db, eng, v2.id, code="WEAR", title="Wear")
    assert inspections.current_version(db, tpl["id"]).id == v1.id  # v1 stays the live one until v2 is approved
    inspections.approve_version(db, eng, v2.id)
    assert {v["version"]: v["status"] for v in inspections.template_detail(db, tpl["id"])["versions"]} == {"1": "OBSOLETE", "2": "APPROVED"}


def test_effective_checklist_merges_category_and_design_additions_and_ignores_drafts(db, people, make_item, world):
    it = make_item()
    base = [c["code"] for c in inspections.effective_checklist(db, it)["checks"]]
    assert base == ["COMPLETE_ID", "CRACKS"]
    v = inspections.create_template(db, people["eng"], "DESIGN", world["die"])
    inspections.add_check(db, people["eng"], v.id, code="BORE", title="Bore", check_type="MEASUREMENT", unit="mm", min_value=269, max_value=271)
    assert [c["code"] for c in inspections.effective_checklist(db, it)["checks"]] == base  # a draft is not used
    inspections.approve_version(db, people["eng"], v.id)
    got = inspections.effective_checklist(db, it)["checks"]
    assert [c["code"] for c in got] == base + ["BORE"] and got[-1]["scope"] == "design"


# ---- starting an inspection -----------------------------------------------------------------------------------
def test_only_awaiting_items_are_inspected_by_tooling_with_an_approved_checklist(db, people, make_item, world):
    it = make_item()
    with pytest.raises(ValidationError) as e:
        inspections.start_inspection(db, people["tooling"], it.id)  # serviceable
    assert "only items awaiting inspection" in e.value.message
    awaiting(db, people, it)
    for who in ("stores", "prod"):
        with pytest.raises(PermissionDenied):
            inspections.start_inspection(db, people[who], it.id)
    i = inspections.start_inspection(db, people["tooling"], it.id)
    assert i.status == "OPEN" and i.category_version_id
    with pytest.raises(ValidationError) as e:
        inspections.start_inspection(db, people["tooling"], it.id)
    assert "already has an open inspection" in e.value.message


def test_no_checklist_means_no_inspection(db, people, world, make_item):
    from app.services import config
    cat = config.create_category(db, people["admin"], "CORE", "Core").id  # a category with no approved checklist
    d = designs.create_design(db, people["eng"], code="core-1", description="Core", category_id=cat, base_uom="EA")
    designs.approve_revision(db, people["eng"], designs.revisions(db, d.id)[0].id)
    it = items.register_item(db, people["stores"], design_id=d.id, legacy_no="c1", condition="AWAITING_INSPECTION",
                             location_id=world["bins"][0])
    with pytest.raises(ValidationError) as e:
        inspections.start_inspection(db, people["tooling"], it.id)
    assert "No approved checklist exists for category CORE" in e.value.message


# ---- recording results ------------------------------------------------------------------------------------------
@pytest.fixture
def measured(db, people, make_item, world):
    """A die with a bore measurement (269.5..270.5 mm) added by a design-specific checklist."""
    v = inspections.create_template(db, people["eng"], "DESIGN", world["die"])
    inspections.add_check(db, people["eng"], v.id, code="BORE", title="Bore", check_type="MEASUREMENT", unit="mm",
                          min_value=269.5, max_value=270.5, evidence="ON_FAIL")
    inspections.add_check(db, people["eng"], v.id, code="NOTE", title="Inspector note", check_type="TEXT", required=False)
    inspections.add_check(db, people["eng"], v.id, code="FIXINGS", title="Fixings", na_allowed=True, required=False)
    inspections.approve_version(db, people["eng"], v.id)
    it = awaiting(db, people, make_item())
    i = inspections.start_inspection(db, people["tooling"], it.id)
    return it, i


def test_pass_fail_na_rules(db, people, measured):
    it, i = measured
    crack, fix = check_by_code(db, it, "CRACKS"), check_by_code(db, it, "FIXINGS")
    t = people["tooling"]
    with pytest.raises(ValidationError):
        inspections.record_result(db, t, i.id, crack["id"], result="MAYBE")
    with pytest.raises(ValidationError) as e:
        inspections.record_result(db, t, i.id, crack["id"], result="FAIL")
    assert "describe the failure" in e.value.message
    with pytest.raises(ValidationError):  # n/a not allowed for this check
        inspections.record_result(db, t, i.id, crack["id"], result="NA", reason="n/a")
    with pytest.raises(ValidationError):  # n/a needs a reason
        inspections.record_result(db, t, i.id, fix["id"], result="NA")
    assert inspections.record_result(db, t, i.id, fix["id"], result="NA", reason="no fasteners on this die").result == "NA"
    with pytest.raises(PermissionDenied):
        inspections.record_result(db, people["stores"], i.id, crack["id"], result="PASS")


def test_measurements_are_judged_by_the_limits_not_by_the_inspector(db, people, measured):
    it, i = measured
    bore = check_by_code(db, it, "BORE")
    t = people["tooling"]
    r = inspections.record_result(db, t, i.id, bore["id"], value=270.2, result="FAIL")  # claim is ignored
    assert (r.result, r.value) == ("PASS", 270.2)
    r = inspections.record_result(db, t, i.id, bore["id"], value=271.4, result="PASS")
    assert r.result == "FAIL" and "outside the limits [269.5, 270.5]" in r.reason
    r = inspections.record_result(db, t, i.id, bore["id"], value=269.5)  # limits are inclusive
    assert r.result == "PASS"
    with pytest.raises(ValidationError):
        inspections.record_result(db, t, i.id, bore["id"], value="abc")
    assert db.query(InspectionResult).filter_by(inspection_id=i.id).count() == 1  # re-recording replaces, not duplicates


def test_text_checks_and_foreign_checks(db, people, measured, make_item):
    it, i = measured
    note = check_by_code(db, it, "NOTE")
    with pytest.raises(ValidationError):
        inspections.record_result(db, people["tooling"], i.id, note["id"], text=" ")
    assert inspections.record_result(db, people["tooling"], i.id, note["id"], text="light scoring on seat").text == "light scoring on seat"
    with pytest.raises(ValidationError):  # a check from some other checklist
        inspections.record_result(db, people["tooling"], i.id, 99999, result="PASS")


# ---- completing: evidence, holds, passes --------------------------------------------------------------------------
def test_completion_needs_every_check_answered(db, people, measured):
    it, i = measured
    with pytest.raises(ValidationError) as e:
        inspections.complete_inspection(db, people["tooling"], i.id)
    assert "Not every check has a result" in e.value.message


def test_mandatory_photo_evidence_is_enforced(db, people, measured):
    it, i = measured
    bore = check_by_code(db, it, "BORE")
    pass_all(db, people, i.id, it, {bore["id"]: dict(value=272.0), check_by_code(db, it, "NOTE")["id"]: dict(text="n"),
                                      check_by_code(db, it, "FIXINGS")["id"]: dict(result="NA", reason="none")})
    with pytest.raises(ValidationError) as e:  # a failed measurement with evidence ON_FAIL has no photo yet
        inspections.complete_inspection(db, people["tooling"], i.id)
    assert "Photo evidence is mandatory for: Bore" in e.value.message
    with pytest.raises(ValidationError):
        inspections.add_photo(db, people["tooling"], i.id, bore["id"], "p.exe", b"MZ")
    with pytest.raises(ValidationError):
        inspections.add_photo(db, people["tooling"], i.id, 99999, "p.png", PNG)
    inspections.add_photo(db, people["tooling"], i.id, bore["id"], "bore.png", PNG)
    assert inspections.complete_inspection(db, people["tooling"], i.id).outcome == "HOLD"


def test_failed_required_check_forces_a_hold_in_the_same_transaction(db, people, measured):
    it, i = measured
    crack = check_by_code(db, it, "CRACKS")
    pass_all(db, people, i.id, it, {crack["id"]: dict(result="FAIL", reason="hairline crack at seat"),
                                      check_by_code(db, it, "BORE")["id"]: dict(value=270.0),
                                      check_by_code(db, it, "NOTE")["id"]: dict(text="n"),
                                      check_by_code(db, it, "FIXINGS")["id"]: dict(result="NA", reason="none")})
    inspections.add_photo(db, people["tooling"], i.id, crack["id"], "crack.png", PNG)
    done = inspections.complete_inspection(db, people["tooling"], i.id, summary="cracked")
    assert done.outcome == "HOLD" and done.status == "COMPLETED"
    assert ledger.get_state(db, it.id)["item_condition"] == "HOLD"
    row = db.query(Ledger).filter_by(item_id=it.id, action="SET_CONDITION").order_by(Ledger.txn_id.desc()).first()
    assert row.source == "INSPECTION" and "CRACKS" not in row.remarks and "Cracks" in row.remarks
    with pytest.raises(ValidationError):  # written by the workflow: not reversible by hand
        ledger.reverse(db, people["tooling"], row.txn_id, reason="oops")
    with pytest.raises(ValidationError):  # a completed inspection cannot be edited
        inspections.record_result(db, people["tooling"], i.id, crack["id"], result="PASS")


def test_failed_optional_check_is_a_finding_not_a_hold(db, people, measured):
    it, i = measured
    fix = check_by_code(db, it, "FIXINGS")
    pass_all(db, people, i.id, it, {fix["id"]: dict(result="FAIL", reason="one bolt stripped"),
                                      check_by_code(db, it, "BORE")["id"]: dict(value=270.0),
                                      check_by_code(db, it, "NOTE")["id"]: dict(text="n")})
    done = inspections.complete_inspection(db, people["tooling"], i.id)
    assert done.outcome == "PASS" and ledger.get_state(db, it.id)["item_condition"] == "AWAITING_INSPECTION"
    d = inspections.inspection_detail(db, i.id)
    assert next(c for c in d["checks"] if c["code"] == "FIXINGS")["result"] == "FAIL"  # recorded for the record


# ---- release only on a passing inspection, by Engineering ------------------------------------------------------------
def test_a_pass_does_not_release_and_only_engineering_can(db, people, make_item):
    it = awaiting(db, people, make_item())
    with pytest.raises(ValidationError) as e:  # no inspection yet
        ledger.set_condition(db, people["eng"], [it.id], "SERVICEABLE", reason="looks fine")
    assert "no passing inspection" in e.value.message
    i = inspections.start_inspection(db, people["tooling"], it.id)
    pass_all(db, people, i.id, it)
    assert inspections.complete_inspection(db, people["tooling"], i.id).outcome == "PASS"
    assert ledger.get_state(db, it.id)["item_condition"] == "AWAITING_INSPECTION"  # PASS alone releases nothing
    for who in ("tooling", "stores", "scrap"):
        with pytest.raises(PermissionDenied):
            ledger.set_condition(db, people[who], [it.id], "SERVICEABLE", reason="pass")
    ledger.set_condition(db, people["eng"], [it.id], "SERVICEABLE", reason="technical release")
    assert ledger.get_state(db, it.id)["item_condition"] == "SERVICEABLE"


def test_an_old_passing_inspection_cannot_release_a_later_return(db, people, make_item):
    it = make_item()
    awaiting(db, people, it)
    clear_items(db, people, it.id)  # inspected and released once
    awaiting(db, people, it)  # back for inspection (e.g. returned again)
    with pytest.raises(ValidationError) as e:
        ledger.set_condition(db, people["eng"], [it.id], "SERVICEABLE", reason="reuse the old pass")
    assert "no passing inspection" in e.value.message or "older than" in e.value.message
    clear_items(db, people, it.id)  # a fresh inspection works
    assert ledger.get_state(db, it.id)["item_condition"] == "SERVICEABLE"


def test_failed_inspection_is_not_a_basis_for_release(db, people, make_item):
    it = awaiting(db, people, make_item())
    i = inspections.start_inspection(db, people["tooling"], it.id)
    crack = check_by_code(db, it, "CRACKS")
    pass_all(db, people, i.id, it, {crack["id"]: dict(result="FAIL", reason="crack")})
    inspections.add_photo(db, people["tooling"], i.id, crack["id"], "c.png", PNG)
    inspections.complete_inspection(db, people["tooling"], i.id)  # -> HOLD
    ledger.set_condition(db, people["tooling"], [it.id], "AWAITING_INSPECTION", reason="further inspection")
    with pytest.raises(ValidationError):
        ledger.set_condition(db, people["eng"], [it.id], "SERVICEABLE", reason="release despite failure")


# ---- A08: component-level results after a partial return --------------------------------------------------------------
def test_one_failed_element_does_not_clear_or_condemn_the_others(db, people, kit, world):
    m = mappings.create_mapping(db, people["eng"], part_id="PN-1", routing_revision="R1", operation_id="OP10",
                                definition_id=kit["asm"], selected=True)
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    keys = ("d1", "p1", "t1", "b1")
    for k in keys:
        builds.fit_item(db, people["stores"], b.id, kit["items"][k])
    builds.verify_build(db, people["eng"], b.id)
    ids = [kit["items"][k] for k in keys]
    i = issues.create_issue(db, people["stores"], setup_id=mk_setup(db, people, "WO-1", m.id), wo_id="WO-1", mapping_id=m.id, build_id=b.id, confirmed_item_ids=ids)
    issues.acknowledge_issue(db, people["prod"], i.id)
    issues.return_items(db, people["stores"], i.id, [{"item_id": x, "to_location_id": world["bins"][1]} for x in ids[:3]])
    started = inspections.start_for_issue(db, people["tooling"], i.id)
    assert len(started) == 3  # the fourth is still in production: not inspected yet
    by_item = {s.item_id: s for s in started}
    for item_id, insp in by_item.items():
        it = db.get(items.Item, item_id)
        crack = check_by_code(db, it, "CRACKS")
        if item_id == kit["items"]["t1"]:
            pass_all(db, people, insp.id, it, {crack["id"]: dict(result="FAIL", reason="cracked flange")})
            inspections.add_photo(db, people["tooling"], insp.id, crack["id"], "c.png", PNG)
        else:
            pass_all(db, people, insp.id, it)
        inspections.complete_inspection(db, people["tooling"], insp.id)
    assert ledger.get_state(db, kit["items"]["t1"])["item_condition"] == "HOLD"  # only the failed one is held
    for k in ("d1", "p1"):
        assert ledger.get_state(db, kit["items"][k])["item_condition"] == "AWAITING_INSPECTION"  # not condemned
        ledger.set_condition(db, people["eng"], [kit["items"][k]], "SERVICEABLE", reason="inspection passed")  # and releasable
    assert ledger.get_state(db, kit["items"]["t1"])["item_condition"] == "HOLD"  # not cleared by its neighbours
    assert ledger.get_state(db, kit["items"]["b1"])["custody"] == "PRODUCTION"  # outstanding one untouched


# ---- A14: new checklist revision applies to new work; history keeps its version ---------------------------------------------
def test_checklist_change_applies_to_new_inspections_and_history_keeps_its_version(db, people, make_item):
    it = awaiting(db, people, make_item())
    old = inspections.start_inspection(db, people["tooling"], it.id)
    pass_all(db, people, old.id, it)
    inspections.complete_inspection(db, people["tooling"], old.id)
    old_detail = inspections.inspection_detail(db, old.id)
    assert old_detail["checklist_versions"]["category"]["version"] == "1" and len(old_detail["checks"]) == 2
    tpl = inspections.list_templates(db)[0]
    v2 = inspections.new_version(db, people["eng"], tpl["id"], reason="add wear check")
    inspections.add_check(db, people["eng"], v2.id, code="WEAR", title="Wear")
    inspections.approve_version(db, people["eng"], v2.id)
    # the completed inspection is unchanged
    again = inspections.inspection_detail(db, old.id)
    assert again["checklist_versions"]["category"]["version"] == "1" and len(again["checks"]) == 2
    assert [c["result"] for c in again["checks"]] == ["PASS", "PASS"]
    # but new work uses version 2
    clear_items(db, people, it.id)  # (item is awaiting; a fresh inspection is needed for the new rules)
    new = db.query(Inspection).filter_by(item_id=it.id).order_by(Inspection.id.desc()).first()
    nd = inspections.inspection_detail(db, new.id)
    assert nd["checklist_versions"]["category"]["version"] == "2" and len(nd["checks"]) == 3


# ---- audit & queue ---------------------------------------------------------------------------------------------------
def test_inspection_actions_are_audited_and_queue_lists_waiting_items(db, people, make_item):
    a, b = awaiting(db, people, make_item(legacy="a1")), awaiting(db, people, make_item(legacy="b1", bin_index=1))
    i = inspections.start_inspection(db, people["tooling"], a.id)
    q = {r["display_id"]: r["open_inspection_id"] for r in inspections.awaiting_queue(db)}
    assert q == {"A1": i.id, "B1": None}
    pass_all(db, people, i.id, a)
    inspections.complete_inspection(db, people["tooling"], i.id)
    actions = {r.action for r in db.query(AuditLog).filter_by(entity_type="inspection", entity_id=str(i.id))}
    assert {"START_INSPECTION", "RECORD_RESULT", "COMPLETE_INSPECTION"} <= actions
    assert inspections.list_inspections(db, status="COMPLETED")[0] == 1 and inspections.list_inspections(db, item_id=b.id)[0] == 0
    assert storage.PHOTO_EXTENSIONS == {"png", "jpg", "jpeg"}
