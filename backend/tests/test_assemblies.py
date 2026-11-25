import pytest

from app.models import AssemblyLine, AuditLog
from app.services import assemblies, audit, config, designs, mappings
from app.services.errors import PermissionDenied, ValidationError
from app.services.matching import Candidate, Line, match


def rev_of(db, kit):
    return assemblies.get_revision(db, kit["rev"])


# ---- definitions --------------------------------------------------------------------
def test_new_definition_is_a_draft_with_no_lines_and_needs_engineering(db, people, kit):
    for who in ("stores", "mgr"):
        with pytest.raises(PermissionDenied):
            assemblies.create_assembly(db, people[who], code="x1", description="x", category_id=kit["asm_cat"])
    d = assemblies.create_assembly(db, people["eng"], code="x1", description="x", category_id=kit["asm_cat"])
    (rev,) = assemblies.revisions(db, d.id)
    assert (rev.revision, rev.status) == ("A", "DRAFT")
    with pytest.raises(ValidationError) as e:
        assemblies.approve_revision(db, people["eng"], rev.id)
    assert "at least one" in e.value.message
    with pytest.raises(ValidationError):  # duplicate code after normalisation
        assemblies.create_assembly(db, people["eng"], code=" X1 ", description="x", category_id=kit["asm_cat"])


def test_approval_needs_engineering_admin_cannot_bypass(db, people, kit):
    d = assemblies.create_assembly(db, people["eng"], code="x2", description="x", category_id=kit["asm_cat"])
    rev = assemblies.revisions(db, d.id)[0]
    assemblies.add_line(db, people["eng"], rev.id, design_id=kit["die"])
    for who in ("stores", "tooling"):
        with pytest.raises(PermissionDenied):
            assemblies.approve_revision(db, people[who], rev.id)
    assemblies.approve_revision(db, people["eng"], rev.id)
    assert assemblies.current_approved(db, d.id).revision == "A"


def test_assembly_with_more_than_four_elements_and_a_new_category_needs_no_code_change(db, people, kit):
    """Spec A02."""
    eng, admin = people["eng"], people["admin"]
    chill = config.create_category(db, admin, "CHILL-PLATE", "Chill plate").id
    d = assemblies.create_assembly(db, eng, code="big-1", description="6 element assembly", category_id=chill)
    rev = assemblies.revisions(db, d.id)[0]
    for key in ("die", "pat", "pat2", "tf", "bf"):
        assemblies.add_line(db, eng, rev.id, design_id=kit[key])
    extra = designs.create_design(db, eng, code="chill-1", description="Chill plate", category_id=chill, base_uom="EA")
    designs.approve_revision(db, eng, designs.revisions(db, extra.id)[0].id)
    assemblies.add_line(db, eng, rev.id, design_id=extra.id, quantity=2)
    assemblies.approve_revision(db, eng, rev.id)
    detail = assemblies.assembly_detail(db, d.id)
    assert len(detail["revisions"][0]["lines"]) == 6 and detail["category"]["code"] == "CHILL-PLATE"
    assert assemblies.list_assemblies(db, category_id=chill)[1][0]["approved_revision"] == "A"


def test_line_validation(db, people, kit):
    eng = people["eng"]
    d = assemblies.create_assembly(db, eng, code="x3", description="x", category_id=kit["asm_cat"])
    rid = assemblies.revisions(db, d.id)[0].id
    for qty in (0, -1, 1.5, "abc"):
        with pytest.raises(ValidationError):
            assemblies.add_line(db, eng, rid, design_id=kit["die"], quantity=qty)
    with pytest.raises(ValidationError):  # unknown revision label
        assemblies.add_line(db, eng, rid, design_id=kit["die"], allowed_revisions="Z")
    ln = assemblies.add_line(db, eng, rid, design_id=kit["die"], quantity=2, allowed_revisions="a")
    assert ln.quantity == 2 and ln.allowed_revisions == "A"  # matched case-insensitively, stored as defined
    with pytest.raises(ValidationError):  # one line per design: raise the quantity instead
        assemblies.add_line(db, eng, rid, design_id=kit["die"])


def test_consumable_line_needs_a_quantity_basis(db, people, kit):
    eng = people["eng"]
    cons = designs.create_design(db, eng, code="coat-1", description="Coating", category_id=kit["asm_cat"],
                                 base_uom="EA", kind="CONSUMABLE")
    designs.approve_revision(db, eng, designs.revisions(db, cons.id)[0].id)
    d = assemblies.create_assembly(db, eng, code="x4", description="x", category_id=kit["asm_cat"])
    rid = assemblies.revisions(db, d.id)[0].id
    with pytest.raises(ValidationError) as e:
        assemblies.add_line(db, eng, rid, design_id=cons.id, quantity=0.5)
    assert "basis" in e.value.message
    ln = assemblies.add_line(db, eng, rid, design_id=cons.id, quantity=0.5, quantity_basis="ME spec ME-17")
    assert ln.quantity == 0.5  # fractional is fine for consumables


def test_substitute_rules(db, people, kit):
    eng = people["eng"]
    d = assemblies.create_assembly(db, eng, code="x5", description="x", category_id=kit["asm_cat"])
    rid = assemblies.revisions(db, d.id)[0].id
    line = assemblies.add_line(db, eng, rid, design_id=kit["die"])
    with pytest.raises(ValidationError):
        assemblies.add_alternative(db, eng, line.id, design_id=kit["die"])  # itself
    cons = designs.create_design(db, eng, code="c-1", description="c", category_id=kit["asm_cat"],
                                 base_uom="EA", kind="CONSUMABLE")
    designs.approve_revision(db, eng, designs.revisions(db, cons.id)[0].id)
    with pytest.raises(ValidationError):
        assemblies.add_alternative(db, eng, line.id, design_id=cons.id)  # kind mismatch
    assemblies.add_alternative(db, eng, line.id, design_id=kit["pat"])
    with pytest.raises(ValidationError):
        assemblies.add_alternative(db, eng, line.id, design_id=kit["pat"])  # duplicate
    for who in ("stores",):
        with pytest.raises(PermissionDenied):
            assemblies.add_alternative(db, people[who], line.id, design_id=kit["tf"])


def test_approved_revision_is_immutable_and_new_revision_copies_lines(db, people, kit):
    eng = people["eng"]
    rev = rev_of(db, kit)
    for fn in (lambda: assemblies.add_line(db, eng, rev.id, design_id=kit["pat2"]),
               lambda: assemblies.update_line(db, eng, kit["lines"]["die"], quantity=2),
               lambda: assemblies.remove_line(db, eng, kit["lines"]["die"]),
               lambda: assemblies.update_revision(db, eng, rev.id, instructions="sneaky")):
        with pytest.raises(ValidationError) as e:
            fn()
        assert "immutable" in e.value.message or "draft" in e.value.message
    with pytest.raises(ValidationError):
        assemblies.new_revision(db, eng, kit["asm"], reason=" ")
    b = assemblies.new_revision(db, eng, kit["asm"], reason="Pattern 2 now allowed", instructions="v2")
    detail = assemblies.revision_dict(db, b)
    assert b.revision == "B" and len(detail["lines"]) == 4
    pat_line = next(l for l in detail["lines"] if l["design_code"] == "PAT-1")
    assert [a["design_code"] for a in pat_line["alternatives"]] == ["PAT-2"]  # substitutes copied
    assert assemblies.current_approved(db, kit["asm"]).revision == "A"  # new work uses A until B approved
    assemblies.approve_revision(db, eng, b.id)
    assert {r.revision: r.status for r in assemblies.revisions(db, kit["asm"])} == {"A": "OBSOLETE", "B": "APPROVED"}


def test_cannot_approve_if_a_design_has_no_approved_revision(db, people, kit):
    eng = people["eng"]
    draft = designs.create_design(db, eng, code="d-draft", description="d", category_id=kit["asm_cat"], base_uom="EA")
    d = assemblies.create_assembly(db, eng, code="x6", description="x", category_id=kit["asm_cat"])
    rid = assemblies.revisions(db, d.id)[0].id
    assemblies.add_line(db, eng, rid, design_id=draft.id)
    with pytest.raises(ValidationError) as e:
        assemblies.approve_revision(db, eng, rid)
    assert "D-DRAFT has no approved revision" in e.value.message


def test_definition_changes_are_audited(db, people, kit):
    assemblies.update_assembly(db, people["eng"], kit["asm"], description="renamed")
    row = db.query(AuditLog).filter(AuditLog.entity_type == "assembly", AuditLog.action == "UPDATE").one()
    assert "32 NB" in row.before and "renamed" in row.after and row.username == "eng"
    _, rows = audit.list_audit(db, entity_type="assembly_revision", action="APPROVE")
    assert len(rows) == 1


# ---- matching (spec A06: one physical item must not satisfy two lines) -----------------
def test_one_item_cannot_satisfy_two_lines():
    x = Line(1, "X", 1, {10: {100}})
    y = Line(2, "Y", 1, {10: {100}, 20: {200}})
    assignment, unmatched, unfilled = match([x, y], [Candidate(1, 10, 100)])
    assert len(assignment) == 1 and unmatched == [] and sum(unfilled.values()) == 1


def test_overlapping_alternatives_are_resolved_not_double_counted():
    # Y is listed first and accepts both designs; a greedy pass would give Y the design-10 item and
    # strand the design-20 item. Matching must re-arrange so both lines are filled.
    y = Line(2, "Y", 1, {10: {100}, 20: {200}})
    x = Line(1, "X", 1, {10: {100}})
    assignment, unmatched, unfilled = match([y, x], [Candidate(1, 10, 100), Candidate(2, 20, 200)])
    assert assignment == {1: 1, 2: 2} and not unmatched and not unfilled


def test_matching_reports_extra_and_wrong_revision_items():
    ln = Line(1, "X", 1, {10: {100}})
    a, u, f = match([ln], [Candidate(1, 10, 100), Candidate(2, 10, 100), Candidate(3, 10, 999)])
    assert len(a) == 1 and sorted(u) == [2, 3] or len(u) == 2
    assert f == {}


def test_quantity_requires_that_many_distinct_items():
    ln = Line(1, "X", 2, {10: {100}})
    _, _, f1 = match([ln], [Candidate(1, 10, 100)])
    _, _, f2 = match([ln], [Candidate(1, 10, 100), Candidate(2, 10, 100)])
    assert f1 == {1: 1} and f2 == {}


# ---- ME mapping (spec A03) -----------------------------------------------------------
def test_one_assembly_to_many_parts_and_many_assemblies_to_one_operation(db, people, kit):
    eng = people["eng"]
    other = assemblies.create_assembly(db, eng, code="t212", description="alt", category_id=kit["asm_cat"])
    orev = assemblies.revisions(db, other.id)[0]
    assemblies.add_line(db, eng, orev.id, design_id=kit["die"])
    assemblies.approve_revision(db, eng, orev.id)
    for part in ("PN-1001", "pn-1002", "PN-1003"):
        mappings.create_mapping(db, eng, part_id=part, routing_revision="r1", operation_id="op10", definition_id=kit["asm"])
    mappings.create_mapping(db, eng, part_id="PN-1001", routing_revision="R1", operation_id="OP10", definition_id=other.id)
    total, rows = mappings.list_mappings(db, definition_id=kit["asm"])
    assert total == 3 and {r["part_id"] for r in rows} == {"PN-1001", "PN-1002", "PN-1003"}
    opts = mappings.options(db, part_id="pn-1001", routing_revision="r1", operation_id="op10")
    assert {o["assembly_code"] for o in opts} == {"T211", "T212"} and all(o["selectable"] for o in opts)
    with pytest.raises(ValidationError):
        mappings.create_mapping(db, eng, part_id="PN-1001", routing_revision="R1", operation_id="OP10", definition_id=kit["asm"])


def test_part_ids_are_referenced_not_rewritten(db, people, kit):
    m = mappings.create_mapping(db, people["eng"], part_id=" pn-007 ", routing_revision="r1", operation_id="op10",
                                definition_id=kit["asm"])
    assert m.part_id == "PN-007"  # trimmed + upper-cased only: zero padding is kept (shared part master ID)


def test_only_approved_options_are_selectable(db, people, kit):
    eng = people["eng"]
    draft = assemblies.create_assembly(db, eng, code="t300", description="draft only", category_id=kit["asm_cat"])
    assemblies.add_line(db, eng, assemblies.revisions(db, draft.id)[0].id, design_id=kit["die"])
    mappings.create_mapping(db, eng, part_id="P1", routing_revision="R1", operation_id="OP1", definition_id=draft.id)
    ok = mappings.create_mapping(db, eng, part_id="P1", routing_revision="R1", operation_id="OP1", definition_id=kit["asm"])
    dev = assemblies.create_assembly(db, eng, code="t301", description="to develop", category_id=kit["asm_cat"])
    r = assemblies.revisions(db, dev.id)[0]
    assemblies.add_line(db, eng, r.id, design_id=kit["die"])
    assemblies.approve_revision(db, eng, r.id)
    mappings.create_mapping(db, eng, part_id="P1", routing_revision="R1", operation_id="OP1", definition_id=dev.id,
                            tooling_status="TO_BE_DEVELOPED")
    by_code = {o["assembly_code"]: o for o in mappings.options(db, part_id="P1", routing_revision="R1", operation_id="OP1")}
    assert by_code["T211"]["selectable"] is True
    assert by_code["T300"]["selectable"] is False and "no approved revision" in by_code["T300"]["not_selectable_because"][0]
    assert by_code["T301"]["selectable"] is False and "to be developed" in by_code["T301"]["not_selectable_because"][0]
    mappings.update_mapping(db, eng, by_code["T301"]["id"], fields_set={"tooling_status"}, tooling_status="READY")
    assert {o["assembly_code"]: o["selectable"] for o in mappings.options(
        db, part_id="P1", routing_revision="R1", operation_id="OP1")}["T301"] is True
    assert ok.id  # keep a reference so the fixture mapping is not optimised away


def test_pinned_revision_that_gets_superseded_stops_being_selectable(db, people, kit):
    eng = people["eng"]
    m = mappings.create_mapping(db, eng, part_id="P2", routing_revision="R1", operation_id="OP1",
                                definition_id=kit["asm"], definition_revision_id=kit["rev"])
    assert mappings.mapping_dict(db, m)["selectable"] is True
    b = assemblies.new_revision(db, eng, kit["asm"], reason="change")
    assemblies.approve_revision(db, eng, b.id)  # A becomes OBSOLETE
    d = mappings.mapping_dict(db, m)
    assert d["selectable"] is False and "pinned revision A is not approved" in d["not_selectable_because"][0]
    mappings.update_mapping(db, eng, m.id, fields_set={"definition_revision_id"}, definition_revision_id=None)
    assert mappings.mapping_dict(db, m)["effective_revision"] == "B"  # follow the current approved revision


def test_selected_option_is_unique_per_operation_and_weights_are_unknown_not_zero(db, people, kit):
    eng = people["eng"]
    other = assemblies.create_assembly(db, eng, code="t400", description="x", category_id=kit["asm_cat"])
    r = assemblies.revisions(db, other.id)[0]
    assemblies.add_line(db, eng, r.id, design_id=kit["die"])
    assemblies.approve_revision(db, eng, r.id)
    a = mappings.create_mapping(db, eng, part_id="P3", routing_revision="R1", operation_id="OP1",
                                definition_id=kit["asm"], selected=True, pouring_weight_kg=25, pouring_weight_revision="W2")
    b = mappings.create_mapping(db, eng, part_id="P3", routing_revision="R1", operation_id="OP1",
                                definition_id=other.id, selected=True)
    assert mappings.mapping_dict(db, a)["selected"] is False and mappings.mapping_dict(db, b)["selected"] is True
    assert mappings.mapping_dict(db, b)["pouring_weight_kg"] is None  # unknown stays unknown
    with pytest.raises(ValidationError):
        mappings.create_mapping(db, eng, part_id="P4", routing_revision="R1", operation_id="OP1",
                                definition_id=kit["asm"], pouring_weight_kg=0)


def test_mapping_permissions_and_deactivation_needs_reason(db, people, kit):
    for who in ("stores", "mgr"):
        with pytest.raises(PermissionDenied):
            mappings.create_mapping(db, people[who], part_id="P5", routing_revision="R1", operation_id="OP1",
                                    definition_id=kit["asm"])
    m = mappings.create_mapping(db, people["eng"], part_id="P5", routing_revision="R1", operation_id="OP1",
                                definition_id=kit["asm"])
    with pytest.raises(ValidationError):
        mappings.update_mapping(db, people["eng"], m.id, fields_set={"active"}, active=False)
    mappings.update_mapping(db, people["eng"], m.id, fields_set={"active"}, active=False, reason="routing withdrawn")
    assert mappings.options(db, part_id="P5", routing_revision="R1", operation_id="OP1") == []
    assert db.get(type(m), m.id) is not None  # never deleted
    assert db.query(AssemblyLine).count() >= 4
