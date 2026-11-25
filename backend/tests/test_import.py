from io import BytesIO

import pandas as pd
import pytest

from app.models import ImportReview, Item, Ledger
from app.services import excel_io, imports, items, ledger
from app.services.errors import PermissionDenied, ValidationError

BIN1, BIN2 = "P1/A1/R1/S1/B1", "P1/A1/R1/S1/B2"


def run(db, people, df, commit=False, who="stores"):
    buf = BytesIO()
    df.to_excel(buf, index=False)
    cols, rows = excel_io.read_rows(buf.getvalue())
    return imports.import_items(db, people[who], cols, rows, commit=commit, source_file="t.xlsx")


def frame(*rows):
    return pd.DataFrame(list(rows))


def row(legacy, design="die-32", cond="SERVICEABLE", home=BIN1, **kw):
    return {"legacy_no": legacy, "design_code": design, "condition": cond, "home_location": home, **kw}


def test_template_has_the_documented_columns_and_no_data_rows(db, people, world, tmp_path):
    (path,) = excel_io.write_templates(tmp_path)
    with pd.ExcelFile(path) as xl:
        assert "instructions" in xl.sheet_names
        df = xl.parse("data")
    assert list(df.columns) == imports.ITEM_COLUMNS and len(df) == 0
    res = run(db, people, df)
    assert not res["valid"] and "no data rows" in res["errors"][0]["message"]


def test_preview_writes_nothing_and_commit_creates_items_with_receive(db, people, world):
    df = frame(row("b32-01", provenance="bought 2019", acquisition_date="2019-03-04", opening_cycles="1200"),
               row("b32-02", home=BIN2, current_location=BIN1, cond="HOLD"))
    res = run(db, people, df)
    assert res["valid"] and not res["committed"] and res["counts"]["OK"] == 2
    assert db.query(Item).count() == 0
    res = run(db, people, df, commit=True)
    assert res["committed"] and res["created"] == 2
    assert db.query(Item).count() == 2 and db.query(Ledger).count() == 2
    a = db.query(Item).filter(Item.display_id == "B32-1").one()
    assert a.opening_cycles == 1200 and str(a.acquisition_date) == "2019-03-04"
    assert a.opening_tonnage is None  # not supplied -> unknown, not zero
    b = db.query(Item).filter(Item.display_id == "B32-2").one()
    s = ledger.get_state(db, b.id)
    assert s["item_condition"] == "HOLD" and s["location_id"] != b.home_location_id  # current != home
    events = {r.event_id for r in db.query(Ledger)}
    assert len(events) == 1  # whole import is one event


def test_row_level_errors_block_the_whole_commit(db, people, world):
    df = frame(row("ok-1"),
               row("e-1", design="nope"),
               row("e-2", cond="BROKEN"),
               row("e-3", home="P1/A9"),
               row("e-4", opening_cycles="-5"),
               row("e-5", opening_tonnage="lots"),
               row("e-6", acquisition_date="03/04/2019"),
               {"legacy_no": None, "display_id": None, "design_code": "die-32", "condition": "HOLD",
                "home_location": BIN1})
    res = run(db, people, df, commit=True)
    assert not res["valid"] and not res["committed"] and res["counts"]["ERROR"] == 7
    msgs = {e["row"]: e["message"] for e in res["errors"]}
    assert "Unknown design" in msgs[3] and "condition must be" in msgs[4] and "Unknown location" in msgs[5]
    assert ">= 0" in msgs[6] and "must be a number" in msgs[7] and "YYYY-MM-DD" in msgs[8]
    assert "legacy_no or display_id" in msgs[9]
    assert db.query(Item).count() == 0  # the valid row ok-1 was not written either


def test_design_without_approved_revision_is_an_error(db, people, world):
    from app.services import designs
    designs.create_design(db, people["eng"], code="d-draft", description="Draft", category_id=world["cat"],
                          base_uom="EA")
    res = run(db, people, frame(row("z1", design="d-draft")), commit=True)
    assert not res["committed"] and "no approved revision" in res["errors"][0]["message"]
    assert db.query(Item).count() == 0


def test_duplicate_legacy_numbers_go_to_review_queue_not_overwritten(db, people, world, make_item):
    existing = make_item(legacy="b32-1")
    df = frame(row("B32-01"),                       # clashes with the existing item
               row("n-1"), row("n-1", home=BIN2),  # second n-1 clashes within the file
               row("n-2", display_id="N-2-A"),     # explicit unique display id: fine
               row("N-2"))                          # legacy N-2 repeats with no display id -> review
    res = run(db, people, df)
    assert res["valid"] and res["counts"] == {"OK": 2, "ERROR": 0, "DUPLICATE": 3}
    res = run(db, people, df, commit=True)
    assert res["committed"] and res["created"] == 2 and res["queued_for_review"] == 3
    assert db.query(Item).count() == 3
    assert db.get(Item, existing.id).display_id == "B32-1"  # untouched
    q = imports.list_review(db)
    assert len(q) == 3 and all(r["status"] == "PENDING" for r in q)
    assert "already exists" in q[0]["reason"] and q[0]["data"]["legacy_no"] == "B32-01"


def test_explicit_display_id_clash_is_an_error_not_a_duplicate(db, people, world, make_item):
    make_item(legacy="a1")
    res = run(db, people, frame(row("zz", display_id="a1")), commit=True)
    assert not res["committed"] and "already used" in res["errors"][0]["message"]


def test_resolve_review_with_unique_display_id_creates_distinct_item(db, people, world, make_item):
    make_item(legacy="b32-1")
    run(db, people, frame(row("B32-1", home=BIN2)), commit=True)
    (rv,) = imports.list_review(db)
    with pytest.raises(ValidationError):
        imports.resolve_review(db, people["stores"], rv["id"], display_id="b32-1")  # still clashes
    with pytest.raises(PermissionDenied):
        imports.resolve_review(db, people["prod"], rv["id"], display_id="B32-1-NEW")
    it = imports.resolve_review(db, people["stores"], rv["id"], display_id="b32-1-new", note="second die")
    assert it.display_id == "B32-1-NEW" and it.legacy_key == "B32-1"
    assert imports.list_review(db) == []
    assert len(items.lookup(db, "B32-1")) == 2  # both resolve by legacy number; user picks
    with pytest.raises(ValidationError):
        imports.resolve_review(db, people["stores"], rv["id"], display_id="other")  # already resolved


def test_reject_review_needs_reason(db, people, world, make_item):
    make_item(legacy="b32-1")
    run(db, people, frame(row("b32-1")), commit=True)
    (rv,) = imports.list_review(db)
    with pytest.raises(ValidationError):
        imports.reject_review(db, people["stores"], rv["id"], note=" ")
    imports.reject_review(db, people["stores"], rv["id"], note="same tool listed twice")
    assert db.get(ImportReview, rv["id"]).status == "REJECTED"
    assert db.query(Item).count() == 1


def test_stores_and_engineering_import_and_missing_columns_reported(db, people, world):
    with pytest.raises(PermissionDenied):
        run(db, people, frame(row("a1")), who="prod")
    res = run(db, people, frame({"legacy_no": "a1", "design_code": "die-32"}))
    assert not res["valid"] and {"Missing column: condition", "Missing column: home_location"} <= {
        e["message"] for e in res["errors"]}


def test_blank_rows_keep_excel_row_numbers(db, people, world):
    df = frame(row("a1"), {"legacy_no": None, "design_code": None, "condition": None, "home_location": None},
               row("a2", design="nope"))
    res = run(db, people, df)
    assert [e["row"] for e in res["errors"]] == [4]
