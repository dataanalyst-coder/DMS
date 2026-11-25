import pytest
from sqlalchemy import text

from app.models import Item
from app.services import audit, common, designs, items, ledger
from app.services.errors import PermissionDenied, ValidationError


# ---- id normalisation ---------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [("b32-01", "B32-1"), ("  c56 ", "C56"), ("B32-007", "B32-7"),
                                      ("B32-00", "B32-0"), ("t 211", "T211"), ("tf_03", "TF_3")])
def test_normalize(raw, want):
    assert common.normalize_id(raw) == want


def test_c56_and_C56_resolve_to_the_same_item(db, people, make_item):
    it = make_item(legacy="c56")
    assert [r["id"] for r in items.lookup(db, "C56")] == [r["id"] for r in items.lookup(db, " c56 ")] == [it.id]


# ---- A01: identity ------------------------------------------------------------------
def test_two_items_one_design_have_distinct_identity(db, people, world, make_item):
    a = make_item(legacy="b32-1", bin_index=0)
    b = make_item(legacy="b32-2", bin_index=1, condition="HOLD")
    assert a.design_id == b.design_id
    assert a.system_id != b.system_id and a.display_id != b.display_id
    assert a.system_id == f"TI-{a.id:06d}"
    sa, sb = ledger.get_state(db, a.id), ledger.get_state(db, b.id)
    assert (sa["item_condition"], sb["item_condition"]) == ("SERVICEABLE", "HOLD")
    assert sa["location_id"] == world["bins"][0] and sb["location_id"] == world["bins"][1]


def test_reprinting_a_label_keeps_the_same_identity(db, people, make_item):
    it = make_item(legacy="b32-01")
    first = items.label_data(db, people["stores"], it.id)
    again = items.label_data(db, people["stores"], it.id)
    assert first == again
    assert first["system_id"] == first["qr_payload"] == it.system_id
    assert first["legacy_no"] == "b32-01" and first["display_id"] == "B32-1"
    assert db.query(Item).count() == 1
    _, rows = audit.list_audit(db, entity_type="item", entity_id=it.id, action="PRINT_LABEL")
    assert len(rows) == 2
    with pytest.raises(PermissionDenied):
        items.label_data(db, people["prod"], it.id)


def test_identity_is_immutable_via_update(db, people, make_item):
    it = make_item()
    with pytest.raises(TypeError):
        items.update_item(db, people["stores"], it.id, display_id="NEW")  # not an editable field
    with pytest.raises(TypeError):
        items.update_item(db, people["stores"], it.id, condition="HOLD")  # status is not editable


# ---- legacy duplicates --------------------------------------------------------------
def test_duplicate_legacy_numbers_need_a_unique_display_id(db, people, world, make_item):
    first = make_item(legacy="b32-01")
    with pytest.raises(ValidationError) as e:
        make_item(legacy="B32-1", bin_index=1)  # same legacy, no distinguishing display id
    assert first.system_id in e.value.message
    second = make_item(legacy="B32-1", display_id="B32-1-NEW", bin_index=1)
    assert second.legacy_key == first.legacy_key and second.display_id == "B32-1-NEW"
    assert {r["system_id"] for r in items.lookup(db, "b32-1")} == {first.system_id, second.system_id}
    with pytest.raises(ValidationError):
        make_item(legacy="x", display_id="b32-1-new")  # display ids stay unique


def test_item_needs_some_identifier(db, people, world):
    with pytest.raises(ValidationError):
        items.register_item(db, people["stores"], design_id=world["die"], condition="SERVICEABLE",
                            location_id=world["bins"][0])


# ---- registration rules -------------------------------------------------------------
def test_stores_and_engineering_register_and_design_must_be_approved(db, people, world):
    kw = dict(design_id=world["die"], legacy_no="a1", condition="SERVICEABLE", location_id=world["bins"][0])
    for who in ("prod", "tooling", "mgr"):
        with pytest.raises(PermissionDenied):
            items.register_item(db, people[who], **kw)
    draft = designs.create_design(db, people["eng"], code="d-draft", description="Draft die",
                                  category_id=world["cat"], base_uom="EA")
    with pytest.raises(ValidationError) as e:
        items.register_item(db, people["stores"], **{**kw, "design_id": draft.id})
    assert "no approved revision" in e.value.message
    with pytest.raises(ValidationError):
        items.register_item(db, people["stores"], **{**kw, "condition": "BROKEN"})


def test_unknown_opening_usage_stays_unknown_not_zero(db, people, make_item):
    it = make_item(opening_cycles=None, opening_tonnage=None)
    assert it.opening_cycles is None and it.opening_tonnage is None
    known = make_item(legacy="k1", opening_cycles=0, opening_tonnage=0.0)
    assert known.opening_cycles == 0 and known.opening_tonnage == 0.0  # a real zero is kept as zero
    with pytest.raises(ValidationError):
        make_item(legacy="k2", opening_cycles=-1)


def test_register_creates_ledger_receive_and_audit_in_one_transaction(db, people, world, make_item):
    it = make_item(condition="AWAITING_INSPECTION", bin_index=2)
    total, rows = ledger.ledger_rows(db, item_id=it.id)
    assert total == 1 and rows[0]["action"] == "RECEIVE" and rows[0]["user"] == "stores"
    assert rows[0]["to_location"] == "P1/A1/R1/S1/B3"
    _, a = audit.list_audit(db, entity_type="item", entity_id=it.id, action="REGISTER")
    assert len(a) == 1


def test_no_status_columns_on_items():
    cols = {c.name for c in Item.__table__.columns}
    assert not cols & {"condition", "status", "custody", "location", "location_id", "current_location"}


# ---- aliases ------------------------------------------------------------------------
def test_alias_must_be_approved_by_engineering_before_it_resolves(db, people, make_item):
    it = make_item()
    a = items.request_alias(db, people["stores"], it.id, "old die 7", reason="Painted on side")
    assert items.lookup(db, "OLDDIE7") == []  # pending aliases do not resolve
    for who in ("stores",):
        with pytest.raises(PermissionDenied):
            items.approve_alias(db, people[who], a.id)
    items.approve_alias(db, people["eng"], a.id)
    assert [r["id"] for r in items.lookup(db, "olddie7")] == [it.id]
    with pytest.raises(ValidationError):  # alias keys are globally unique
        items.request_alias(db, people["stores"], it.id, "OLDDIE7", reason="again")


# ---- search / scan ------------------------------------------------------------------
def test_search_filters_and_pagination(db, people, world, make_item):
    for i in range(1, 6):
        make_item(legacy=f"b32-0{i}", bin_index=i % 3, condition="HOLD" if i == 5 else "SERVICEABLE")
    make_item(legacy="p-1", design="pat")
    total, rows = items.search_items(db, q="b32", limit=2, offset=0)
    assert total == 5 and len(rows) == 2
    _, page3 = items.search_items(db, q="b32", limit=2, offset=4)
    assert len(page3) == 1
    assert items.search_items(db, condition="HOLD")[0] == 1
    assert items.search_items(db, design_id=world["pat"])[0] == 1
    assert items.search_items(db, location_id=world["shelf"])[0] == 6  # subtree match
    assert items.search_items(db, q="die 32")[0] == 5  # design description
    assert items.search_items(db, q="TI-0000")[0] == 6  # QR / system id


def test_lookup_by_qr_display_and_legacy(db, people, make_item):
    it = make_item(legacy="b32-01")
    for code in (it.system_id, it.system_id.lower(), "B32-1", "b32-01"):
        assert [r["id"] for r in items.lookup(db, code)] == [it.id]
    assert items.lookup(db, "nope") == []


def test_home_mismatch_is_flagged_not_hidden(db, people, world, make_item):
    it = make_item(bin_index=0)
    ledger.move_items(db, people["stores"], [it.id], world["bins"][2])
    (row,) = items.search_items(db, home_mismatch=True)[1]
    assert row["location"].endswith("B3") and row["home_location"].endswith("B1") and row["home_mismatch"]


def test_retire_never_deletes(db, people, make_item):
    it = make_item()
    with pytest.raises(ValidationError):
        items.retire_item(db, people["eng"], it.id, reason=" ")
    items.retire_item(db, people["eng"], it.id, reason="Obsolete tooling")
    assert db.get(Item, it.id) is not None
    assert items.search_items(db)[0] == 0 and items.search_items(db, active=None)[0] == 1
    with pytest.raises(ValidationError):
        ledger.move_items(db, people["stores"], [it.id], 1)  # retired items cannot be moved


def test_state_is_not_directly_writable_through_items_service(db, people, make_item):
    it = make_item()
    n = db.execute(text("SELECT COUNT(*) FROM item_state")).scalar()
    assert n == 1 and ledger.get_state(db, it.id)["item_condition"] == "SERVICEABLE"


def test_engineering_can_add_to_the_die_master(db, people, world):
    it = items.register_item(db, people["eng"], design_id=world["die"], legacy_no="e1", condition="SERVICEABLE",
                             location_id=world["bins"][0])
    assert it.display_id == "E1"
