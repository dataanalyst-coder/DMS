from io import BytesIO

import pandas as pd
import pytest

from app.models import Setup, SetupLine
from app.services import assemblies, builds, issues, mappings, setups
from app.services.errors import PermissionDenied, ValidationError
from tests.conftest import mk_setup, setup_body
from tests.test_api import api, login  # noqa: F401  (fixtures)

KEYS = ("d1", "p1", "t1", "b1")
COLS = ["SETUP", "SL NO", "WO NO", "PART ID", "TOOL DESCRIPTION", "FAMILY", "FG QTY", "RCD QTY", "WEIGHT", "PLAN", "EACH PART PRICE"]


def sheet(rows, header_row=1, sheet_name="NOVEMBER 1"):
    """A sheet laid out like Planning's: blank first row, header on row 2, formula error cells in extra columns."""
    body = [[""] * len(COLS)] * header_row + [COLS]
    body += [[(r.get(c, "") if c != "EACH PART PRICE" else "#REF!") for c in COLS] for r in rows]
    buf = BytesIO()
    pd.DataFrame(body).to_excel(buf, index=False, header=False, sheet_name=sheet_name)
    return buf.getvalue()


def line(sl, wo, part, desc, family="LTC", fg=10, rcd=5, weight=100, setup=""):
    return {"SETUP": setup, "SL NO": sl, "WO NO": wo, "PART ID": part, "TOOL DESCRIPTION": desc, "FAMILY": family,
            "FG QTY": fg, "RCD QTY": rcd, "WEIGHT": weight}


@pytest.fixture
def rig(db, people, kit, world):
    m = mappings.create_mapping(db, people["eng"], part_id="LTC9", routing_revision="R1", operation_id="OP10",
                                definition_id=kit["asm"], selected=True)
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    for k in KEYS:
        builds.fit_item(db, people["stores"], b.id, kit["items"][k])
    builds.verify_build(db, people["eng"], b.id)
    kit.update(mapping=m.id, build=b.id, ids=[kit["items"][k] for k in KEYS])
    return kit


def other_assembly(db, people, kit, code):
    a = assemblies.create_assembly(db, people["eng"], code=code, description="Other", category_id=kit["asm_cat"])
    rev = assemblies.revisions(db, a.id)[0]
    assemblies.add_line(db, people["eng"], rev.id, design_id=kit["die"])
    assemblies.approve_revision(db, people["eng"], rev.id)
    return a


# ---- the sheet -----------------------------------------------------------------------------------------------
def test_planning_sheet_with_stray_rows_and_error_cells_imports(db, people):
    data = sheet([line(1, "WO-17835", "BLV3", "BLV 003", "ANNELEAD PART"), line(2, "WO-17840", "MIL14", "215 X 165 X 500", "BLOCK"),
                  line(3, "WO-17840", "ASB29", "215 X 215 X 630", "BLOCK"), {}])
    pre = setups.import_sheet(db, people["plan"], data, commit=False)
    assert pre["valid"] and pre["sheet"] == "NOVEMBER 1" and pre["counts"] == {"OK": 3, "ERROR": 0, "DUPLICATE": 0}
    assert db.query(Setup).count() == 0 and db.query(SetupLine).count() == 0  # a preview writes nothing
    done = setups.import_sheet(db, people["plan"], data, commit=True, source_file="nov.xlsx")
    assert done["committed"] and done["created"] == 3
    a = db.query(SetupLine).filter_by(part_id="BLV3").one()
    su = db.get(Setup, a.setup_id)
    assert (a.wo_id, a.tool_description, a.family, a.fg_qty, a.rcd_qty, a.weight) == \
        ("WO-17835", "BLV 003", "ANNELEAD PART", 10, 5, 100.0)
    assert (su.source, su.plan_ref) == ("EXCEL", "NOVEMBER 1")
    assert db.query(SetupLine).filter_by(wo_id="WO-17840").count() == 2  # one WO, two parts: two lines
    assert db.query(Setup).count() == 3  # no SETUP column filled: every row is a setup of its own


def test_a_line_already_planned_is_skipped_never_overwritten(db, people):
    setups.import_sheet(db, people["plan"], sheet([line(1, "WO-1", "A1", "ONE", weight=50)]), commit=True)
    data = sheet([line(1, "WO-1", "A1", "CHANGED", weight=99), line(2, "WO-2", "B1", "TWO"), line(3, "WO-2", "B1", "TWO AGAIN")])
    res = setups.import_sheet(db, people["plan"], data, commit=True)
    assert res["counts"] == {"OK": 1, "ERROR": 0, "DUPLICATE": 2} and res["created"] == 1
    assert db.query(SetupLine).filter_by(wo_id="WO-1").one().tool_description == "ONE"


def test_a_bad_number_blocks_the_whole_commit(db, people):
    data = sheet([line(1, "WO-1", "A1", "ONE"), line(2, "WO-2", "B1", "TWO", fg="many")])
    res = setups.import_sheet(db, people["plan"], data, commit=True)
    assert not res["valid"] and not res["committed"] and res["errors"][0]["row"] == 4 and "FG QTY" in res["errors"][0]["message"]
    assert db.query(Setup).count() == 0
    with pytest.raises(ValidationError):
        setups.import_sheet(db, people["plan"], b"not an excel file", commit=False)
    no_header = BytesIO()
    pd.DataFrame([["a", "b"], [1, 2]]).to_excel(no_header, index=False, header=False)
    with pytest.raises(ValidationError) as e:
        setups.import_sheet(db, people["plan"], no_header.getvalue(), commit=False)
    assert "header row" in e.value.message


def test_manual_entry_and_roles(db, people):
    s = setups.create_setup(db, people["plan"], wo_id="WO-5", part_id=" ltc 9 ", tool_description="LTC 009", family="LTC",
                            grade="G2", fg_qty=10)
    (ln,) = setups.lines_of(db, s.id)
    assert ln.part_id == "LTC9" and ln.grade == "G2" and s.source == "MANUAL"
    with pytest.raises(ValidationError):
        setups.create_setup(db, people["plan"], wo_id="WO-5", part_id="LTC9")  # the same WO + part
    for who in ("stores", "prod", "eng", "mgr"):
        with pytest.raises(PermissionDenied):
            setups.create_setup(db, people[who], wo_id="WO-6", part_id="X1")
        with pytest.raises(PermissionDenied):
            setups.import_sheet(db, people[who], sheet([line(1, "WO-6", "X1", "X")]), commit=True)
    with pytest.raises(ValidationError):
        setups.create_setup(db, people["plan"], wo_id="", part_id="X1")
    setups.cancel_setup(db, people["plan"], s.id, reason="plan changed")
    assert setups.setup_dict(db, s)["state"] == "CANCELLED"
    with pytest.raises(ValidationError):
        setups.cancel_setup(db, people["plan"], s.id, reason=" ")


# ---- matching to tooling -----------------------------------------------------------------------------------------------
def test_unmatched_until_engineering_maps_it_then_it_matches_at_once(db, people, kit):
    s = setups.create_setup(db, people["plan"], wo_id="WO-1", part_id="LTC9", tool_description="LTC 009", family="LTC")
    assert setups.setup_dict(db, s)["match"] == "UNMATCHED"
    for who in ("plan", "stores"):
        with pytest.raises(PermissionDenied):
            setups.add_map(db, people[who], definition_id=kit["asm"], description="LTC 009")
    with pytest.raises(ValidationError):
        setups.add_map(db, people["eng"], definition_id=kit["asm"])  # a rule needs something to match on
    setups.add_map(db, people["eng"], definition_id=kit["asm"], description="ltc009")  # spaces and case do not matter
    d = setups.setup_dict(db, s)
    assert d["match"] == "MATCHED" and d["assembly_code"] == "T211"
    assert setups.counts(db) == {"setups_planned": 1, "setups_unmatched": 0}


def test_most_specific_rule_wins_and_a_tie_is_reported_not_guessed(db, people, kit):
    other = other_assembly(db, people, kit, "t212")
    s = setups.create_setup(db, people["plan"], wo_id="WO-1", part_id="LTC9", tool_description="LTC 009", family="LTC")
    setups.add_map(db, people["eng"], definition_id=kit["asm"], family="LTC")
    assert setups.setup_dict(db, s)["assembly_code"] == "T211"
    setups.add_map(db, people["eng"], definition_id=other.id, part_id="LTC9", family="LTC")  # more specific
    assert setups.setup_dict(db, s)["assembly_code"] == "T212"
    setups.add_map(db, people["eng"], definition_id=kit["asm"], part_id="LTC9", description="LTC 009")  # equally specific, other assembly
    d = setups.setup_dict(db, s)
    assert d["match"] == "AMBIGUOUS" and d["definition_id"] is None and "T211" in d["match_reason"]
    rid = next(m["id"] for m in setups.list_maps(db) if m["part_id"] == "LTC9" and m["description"] == "LTC 009")
    with pytest.raises(ValidationError):
        setups.deactivate_map(db, people["eng"], rid, reason="")
    setups.deactivate_map(db, people["eng"], rid, reason="wrong rule")
    assert setups.setup_dict(db, s)["assembly_code"] == "T212"


def test_a_rule_to_an_assembly_without_an_approved_revision_does_not_match(db, people, kit):
    draft = assemblies.create_assembly(db, people["eng"], code="t999", description="Draft", category_id=kit["asm_cat"])
    s = setups.create_setup(db, people["plan"], wo_id="WO-1", part_id="Z1")
    setups.add_map(db, people["eng"], definition_id=draft.id, part_id="Z1")
    d = setups.setup_dict(db, s)
    assert d["match"] == "UNMATCHED" and "no approved revision" in d["match_reason"]


def test_matched_setup_shows_die_pattern_and_bin_of_the_actual_tooling(db, people, rig):
    s = setups.create_setup(db, people["plan"], wo_id="WO-1", part_id="LTC9", tool_description="LTC 009")
    setups.add_map(db, people["eng"], definition_id=rig["asm"], part_id="LTC9")
    t = setups.setup_dict(db, s, detail=True)["tooling"]
    assert t["assembly"]["code"] == "T211"
    (b,) = t["builds"]
    by = {c["design_code"]: c for c in b["components"]}
    assert {"DIE-32", "PAT-1", "TF-A", "BF-A"} == set(by) and by["DIE-32"]["location"].endswith("B1")
    die_line = next(ln for ln in t["lines"] if ln["label"] == "DIE-32")
    assert {c["display_id"] for c in die_line["candidates"]} == {"D1", "D2"}
    d2 = next(c for c in die_line["candidates"] if c["display_id"] == "D2")
    assert d2["location"].endswith("B2")  # the spare die's bin


# ---- issuing is for a setup -----------------------------------------------------------------------------------------------
def issue(db, people, rig, setup_id, **kw):
    return issues.create_issue(db, people["stores"], setup_id=setup_id, mapping_id=rig["mapping"], build_id=rig["build"],
                               confirmed_item_ids=rig["ids"], **kw)


def test_issue_without_a_setup_is_refused(db, people, rig):
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig, None)
    assert "Choose the setup" in e.value.message
    with pytest.raises(ValidationError):
        issue(db, people, rig, 9999)


def test_issue_records_the_setup_and_takes_the_wo_from_it(db, people, rig):
    sid = mk_setup(db, people, "WO-77", rig["mapping"])
    i = issue(db, people, rig, sid)
    assert i.wo_id == "WO-77" and i.setup_id == sid
    d = issues.issue_detail(db, i.id)
    assert d["setup"]["code"] == f"SET-{sid:06d}" and d["wo_id"] == "WO-77"
    s = setups.setup_dict(db, db.get(Setup, sid))
    assert s["state"] == "ISSUED" and s["issues"][0]["open"] and issues.list_issues(db)[1][0]["setup"]["id"] == sid
    with pytest.raises(ValidationError) as e:  # cannot be cancelled while its tooling is out
        setups.cancel_setup(db, people["plan"], sid, reason="oops")
    assert "open issue" in e.value.message


def test_setup_must_match_the_build_wo_and_not_be_cancelled_or_busy(db, people, rig, kit):
    s = setups.create_setup(db, people["plan"], wo_id="WO-1", part_id="NOPE", tool_description="unknown")
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig, s.id)
    assert "no tooling assigned" in e.value.message and "Engineering must add a tool mapping" in e.value.message
    other = other_assembly(db, people, kit, "t300")
    setups.add_map(db, people["eng"], definition_id=other.id, part_id="NOPE")
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig, s.id)
    assert "needs assembly T300" in e.value.message and "is T211" in e.value.message
    ok = mk_setup(db, people, "WO-2", rig["mapping"])
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig, ok, wo_id="WO-OTHER")
    assert "has no line for WO WO-OTHER" in e.value.message
    setups.cancel_setup(db, people["plan"], ok, reason="plan changed")
    with pytest.raises(ValidationError) as e:
        issue(db, people, rig, ok)
    assert "cancelled" in e.value.message
    issue(db, people, rig, mk_setup(db, people, "WO-3", rig["mapping"]))
    with pytest.raises(ValidationError):  # the build is already out
        issue(db, people, rig, mk_setup(db, people, "WO-4", rig["mapping"]))


def test_a_setup_is_open_on_one_issue_at_a_time_and_done_after_return(db, people, rig, world):
    sid = mk_setup(db, people, "WO-8", rig["mapping"])
    i = issue(db, people, rig, sid)
    assert setups.for_build(db, rig["build"]) == []  # the only setup is out
    issues.acknowledge_issue(db, people["prod"], i.id)
    issues.return_items(db, people["stores"], i.id, [{"item_id": x, "to_location_id": world["bins"][1]} for x in rig["ids"]])
    assert setups.setup_dict(db, db.get(Setup, sid))["state"] == "DONE"


def test_setups_offered_for_a_build_are_only_those_matched_to_its_assembly(db, people, rig):
    a = mk_setup(db, people, "WO-1", rig["mapping"])
    setups.create_setup(db, people["plan"], wo_id="WO-2", part_id="UNMAPPED")
    cancelled = mk_setup(db, people, "WO-3", rig["mapping"])
    setups.cancel_setup(db, people["plan"], cancelled, reason="x")
    assert [s["id"] for s in setups.for_build(db, rig["build"])] == [a]


# ---- over HTTP -----------------------------------------------------------------------------------------------------------
def test_setup_routes_need_a_token_and_the_right_role(api, login, kit):
    for method, path in [("get", "/setups"), ("post", "/setups"), ("post", "/setups/import"), ("get", "/setups/1"),
                         ("get", "/setups/for-build/1"), ("post", "/setups/1/cancel"), ("get", "/templates/setups"),
                         ("get", "/setup-maps"), ("post", "/setup-maps"), ("post", "/setup-maps/1/remove")]:
        assert getattr(api, method)(path).status_code == 401, (method, path)
    body = {"wo_id": "WO-1", "part_id": "LTC9", "tool_description": "LTC 009", "family": "LTC"}
    assert api.post("/setups", json=body, headers=login("stores")).status_code == 403
    r = api.post("/setups", json=body, headers=login("plan"))
    assert r.status_code == 201 and r.json()["match"] == "UNMATCHED" and r.json()["code"].startswith("SET-")
    assert api.post("/setups", json=body, headers=login("plan")).status_code == 422  # already planned
    rule = {"definition_id": kit["asm"], "description": "ltc 009"}
    assert api.post("/setup-maps", json=rule, headers=login("plan")).status_code == 403
    assert api.post("/setup-maps", json=rule, headers=login("eng")).status_code == 201
    got = api.get(f"/setups/{r.json()['id']}", headers=login("prod")).json()
    assert got["match"] == "MATCHED" and got["tooling"]["assembly"]["code"] == "T211"
    assert api.get("/dashboard", headers=login("mgr")).json()["setups_planned"] == 1


def test_sheet_upload_and_template_over_http(api, login):
    h = login("plan")
    files = {"file": ("nov.xlsx", sheet([line(1, "WO-1", "A1", "ONE"), line(2, "WO-2", "B1", "TWO")]))}
    pre = api.post("/setups/import", files=files, headers=h).json()
    assert pre["valid"] and not pre["committed"] and api.get("/setups", headers=h).json()["total"] == 0
    done = api.post("/setups/import", params={"commit": True}, files=files, headers=h).json()
    assert done["created"] == 2 and api.get("/setups", params={"q": "two"}, headers=h).json()["total"] == 1
    t = api.get("/templates/setups", headers=h)
    assert t.status_code == 200 and list(pd.read_excel(BytesIO(t.content)).columns)[:4] == ["SETUP", "INTERNAL ID", "WO NO", "PART ID"]
    assert api.post("/setups/import", files=files, headers=login("stores")).status_code == 403


def test_issue_over_http_needs_a_setup(api, login, rig):
    body = {"mapping_id": rig["mapping"], "build_id": rig["build"], "confirmed_item_ids": rig["ids"]}
    r = api.post("/issues", json={**body, "wo_id": "WO-1"}, headers=login("stores"))
    assert r.status_code == 422 and "setup_id" in str(r.json())
    ok = api.post("/issues", json={**body, **setup_body(api, login, "WO-1", rig["mapping"])}, headers=login("stores"))
    assert ok.status_code == 201 and ok.json()["setup"]["code"].startswith("SET-")
