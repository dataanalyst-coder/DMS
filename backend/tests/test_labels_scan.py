from xml.dom import minidom

import pytest

from app.models import Item
from app.services import builds, config, items, labels, scan
from app.services.errors import ValidationError


def test_item_label_is_well_formed_svg_with_the_required_text(db, people, make_item):
    it = make_item(legacy="b32-01")
    svg = labels.item_label_svg(db, it)
    doc = minidom.parseString(svg)  # raises if not well-formed
    assert doc.documentElement.tagName == "svg" and doc.getElementsByTagName("path")
    text = " ".join(t.firstChild.data for t in doc.getElementsByTagName("text") if t.firstChild)
    assert "b32-01" in text and it.system_id in text and "DIE-32" in text and "Die 32" in text


def test_reprint_is_byte_identical_and_identity_never_changes(db, people, make_item):
    a, b = make_item(legacy="b32-01"), make_item(legacy="b32-02", bin_index=1)
    first = labels.item_label_svg(db, a)
    again = labels.item_label_svg(db, a)
    assert first == again  # same QR, same text
    assert first != labels.item_label_svg(db, b)
    assert a.system_id == f"TI-{a.id:06d}" and db.query(Item).count() == 2  # no new identity from printing


def test_qr_payload_is_the_stable_id_and_matrices_differ_per_payload():
    m1, m2 = labels.qr_modules("TI-000001"), labels.qr_modules("TI-000002")
    assert len(m1) == len(m1[0]) >= 21 and m1 != m2
    assert labels.qr_modules("TI-000001") == m1


def test_label_text_is_escaped(db, people, world, make_item):
    from app.services import designs
    d = designs.create_design(db, people["eng"], code="xs", description="A <b> & \"q\"", category_id=world["cat"], base_uom="EA")
    designs.approve_revision(db, people["eng"], designs.revisions(db, d.id)[0].id)
    it = items.register_item(db, people["stores"], design_id=d.id, legacy_no="x1", condition="SERVICEABLE",
                             location_id=world["bins"][0])
    minidom.parseString(labels.item_label_svg(db, it))  # would raise on unescaped markup


def test_build_and_location_labels(db, people, kit, world):
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    from app.services import assemblies
    d, r = assemblies.get_definition(db, kit["asm"]), assemblies.get_revision(db, kit["rev"])
    svg = labels.build_label_svg(builds.build_code(b.id), d.code, d.description, r.revision)
    assert "BLD-000001" in svg and minidom.parseString(svg)
    loc = config.get_location(db, world["bins"][0])
    lsvg = labels.location_label_svg(db, loc)
    assert "LOC:P1/A1/R1/S1/B1" in lsvg and minidom.parseString(lsvg)


# ---- one resolver for scanner / camera / typed input --------------------------------------------------
def test_scan_resolves_item_codes_every_way(db, people, make_item):
    it = make_item(legacy="b32-01")
    items.request_alias(db, people["stores"], it.id, "old die 7", reason="painted on side")
    items.approve_alias(db, people["eng"], 1)
    for code in (it.system_id, it.system_id.lower(), " b32-01 ", "B32-1", "OLDDIE7"):
        r = scan.resolve(db, code)
        assert r["type"] == "ITEM" and [x["id"] for x in r["items"]] == [it.id], code
    assert scan.resolve(db, "nothing-like-this") == {"type": "NONE"}
    with pytest.raises(ValidationError):
        scan.resolve(db, "  ")


def test_scan_returns_all_matches_for_a_duplicated_legacy_number(db, people, make_item):
    a = make_item(legacy="b32-1")
    b = make_item(legacy="B32-1", display_id="B32-1-NEW", bin_index=1)
    r = scan.resolve(db, "b32-1")
    assert {x["id"] for x in r["items"]} == {a.id, b.id}
    assert [x["id"] for x in scan.resolve(db, "B32-1-NEW")["items"]] == [b.id]  # a display ID is unique


def test_scanning_a_fitted_part_points_at_its_build_and_codes_for_builds_and_locations(db, people, kit, world):
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    builds.fit_item(db, people["stores"], b.id, kit["items"]["d1"])
    r = scan.resolve(db, "d1")
    assert r["items"][0]["build"]["code"] == "BLD-000001"
    assert scan.resolve(db, "d2")["items"][0]["build"] is None
    assert scan.resolve(db, "bld-000001") == {"type": "BUILD", "build": {"id": b.id, "code": "BLD-000001", "status": "ASSEMBLING"}}
    assert scan.resolve(db, "BLD-999999") == {"type": "NONE"}
    loc = scan.resolve(db, "LOC:p1/a1/r1/s1/b2")
    assert loc["type"] == "LOCATION" and loc["location"]["path"] == "P1/A1/R1/S1/B2"
    with pytest.raises(ValidationError):
        scan.resolve(db, "LOC:P1/A9")
