import pytest

from app.models import AuditLog, DesignRevision, Location
from app.services import config, designs
from app.services.errors import PermissionDenied, ValidationError


# ---- configuration -----------------------------------------------------------------
def test_only_admin_configures(db, people):
    for who in ("eng", "stores", "mgr"):
        with pytest.raises(PermissionDenied):
            config.create_category(db, people[who], "X", "X")
    config.create_category(db, people["admin"], " new cat ", "New category")
    assert config.list_categories(db)[0].code == "NEWCAT"


def test_new_category_needs_no_code_change(db, people, world):
    # spec A02: a brand-new tooling category is just data
    cat = config.create_category(db, people["admin"], "CHILL-PLATE", "Chill plate")
    d = designs.create_design(db, people["eng"], code="cp-1", description="Chill plate 1",
                              category_id=cat.id, base_uom="EA")
    assert d.category_id == cat.id
    with pytest.raises(ValidationError):
        config.create_category(db, people["admin"], "chill-plate", "dup")


def test_location_levels_follow_the_hierarchy(db, people, world):
    levels = {l["code"]: l["level"] for l in config.list_locations(db)}
    assert [levels[c] for c in ("P1", "A1", "R1", "S1", "B1")] == ["PLANT", "AREA", "RACK", "SHELF", "BIN"]
    with pytest.raises(ValidationError):  # a bin cannot contain locations
        config.create_location(db, people["admin"], code="X", parent_id=world["bins"][0])
    with pytest.raises(ValidationError):  # duplicate code under the same parent
        config.create_location(db, people["admin"], code="b1", parent_id=world["shelf"])
    with pytest.raises(ValidationError):  # duplicate top-level plant
        config.create_location(db, people["admin"], code="p1")


def test_location_paths_resolution_and_subtree(db, people, world):
    assert config.location_path(db, world["bins"][1]) == "P1/A1/R1/S1/B2"
    assert config.resolve_path(db, " p1/a1/r1/s1/b3 ").id == world["bins"][2]
    with pytest.raises(ValidationError):
        config.resolve_path(db, "P1/A1/R9")
    assert set(config.subtree_ids(db, world["shelf"])) == {world["shelf"], *world["bins"]}


def test_cannot_deactivate_location_with_items(db, people, world, make_item):
    make_item()
    with pytest.raises(ValidationError) as e:
        config.update_location(db, people["admin"], world["bins"][0], active=False)
    assert "1 item" in e.value.message
    config.update_location(db, people["admin"], world["bins"][1], active=False)  # empty bin is fine


# ---- designs & revisions -----------------------------------------------------------
def test_design_starts_as_draft_revision_a_and_needs_engineering(db, people, world):
    with pytest.raises(PermissionDenied):
        designs.create_design(db, people["stores"], code="d9", description="x",
                              category_id=world["cat"], base_uom="EA")
    d = designs.create_design(db, people["eng"], code="d9", description="Die 9",
                              category_id=world["cat"], base_uom="EA")
    (rev,) = designs.revisions(db, d.id)
    assert (rev.revision, rev.status) == ("A", "DRAFT")
    assert designs.current_approved(db, d.id) is None


def test_approval_needs_engineering_and_admin_cannot_bypass(db, people, world):
    d = designs.create_design(db, people["eng"], code="d9", description="Die 9",
                              category_id=world["cat"], base_uom="EA")
    rid = designs.revisions(db, d.id)[0].id
    for who in ("stores", "mgr"):
        with pytest.raises(PermissionDenied):
            designs.approve_revision(db, people[who], rid)
    designs.approve_revision(db, people["eng"], rid)
    assert designs.current_approved(db, d.id).revision == "A"
    with pytest.raises(ValidationError):  # already approved
        designs.approve_revision(db, people["eng"], rid)


def test_new_revision_supersedes_but_old_revision_is_retained(db, people, world):
    eng, d = people["eng"], world["die"]
    with pytest.raises(ValidationError):  # reason required
        designs.new_revision(db, eng, d, reason=" ")
    b = designs.new_revision(db, eng, d, reason="Bore enlarged", specifications="bore 270")
    assert b.revision == "B" and b.status == "DRAFT"
    assert designs.current_approved(db, d).revision == "A"  # new work still uses A until B approved
    with pytest.raises(ValidationError):  # one draft at a time
        designs.new_revision(db, eng, d, reason="again")
    designs.approve_revision(db, eng, b.id)
    status = {r.revision: r.status for r in designs.revisions(db, d)}
    assert status == {"A": "OBSOLETE", "B": "APPROVED"}
    assert designs.current_approved(db, d).revision == "B"


def test_released_revisions_are_immutable_but_drafts_are_editable(db, people, world):
    eng, d = people["eng"], world["die"]
    a = designs.revisions(db, d)[0]
    with pytest.raises(ValidationError):
        designs.update_draft_revision(db, eng, a.id, specifications="sneaky edit")
    b = designs.new_revision(db, eng, d, reason="r")
    designs.update_draft_revision(db, eng, b.id, specifications="v2 spec")
    assert db.get(DesignRevision, b.id).specifications == "v2 spec"


def test_design_changes_are_audited_with_before_and_after(db, people, world):
    designs.update_design(db, people["eng"], world["die"], description="Die 32 NB heavy")
    row = db.query(AuditLog).filter(AuditLog.entity_type == "design", AuditLog.action == "UPDATE").one()
    assert "Die 32" in row.before and "heavy" in row.after and row.username == "eng"
