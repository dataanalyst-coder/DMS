from datetime import datetime, timedelta

import pytest

from app.models import BuildComponent, Issue, IssueLine, Setup, SetupLine
from app.services import auth, builds, issues, mappings, setups, usage
from app.services.errors import ValidationError
from tests.conftest import PW
from tests.test_api import api, login  # noqa: F401  (fixtures)
from tests.test_setups import line, sheet

KEYS = ("d1", "p1", "t1", "b1")


@pytest.fixture
def multi(db, people, kit, world):
    """A verified T211 build, three parts (LTC5 25 kg, LTC9 30 kg, LTC25 40 kg) all served by it, and ONE setup running three WOs."""
    for part, w in (("LTC5", 25), ("LTC9", 30), ("LTC25", 40)):
        mappings.create_mapping(db, people["eng"], part_id=part, routing_revision="R1", operation_id="OP10", definition_id=kit["asm"],
                                selected=True, pouring_weight_kg=w, pouring_weight_revision="W1")
    setups.add_map(db, people["eng"], definition_id=kit["asm"], family="LTC")
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    for k in KEYS:
        builds.fit_item(db, people["stores"], b.id, kit["items"][k])
    builds.verify_build(db, people["eng"], b.id)
    s = setups.create_setup(db, people["plan"], name="SETUP-A", lines=[
        {"wo_id": "WO-1", "part_id": "LTC5", "tool_description": "LTC 005", "family": "LTC", "fg_qty": 10},
        {"wo_id": "WO-2", "part_id": "LTC9", "tool_description": "LTC 009", "family": "LTC", "fg_qty": 15},
        {"wo_id": "WO-3", "part_id": "LTC25", "tool_description": "LTC 025", "family": "LTC", "fg_qty": 10}])
    kit.update(build=b.id, ids=[kit["items"][k] for k in KEYS], setup=s.id)
    return kit


def issue(db, people, rig, **kw):
    return issues.create_issue(db, people["stores"], setup_id=rig["setup"], build_id=rig["build"], confirmed_item_ids=rig["ids"], **kw)


# ---- the sheet groups rows into setups -------------------------------------------------------------------------------
def test_rows_with_the_same_setup_are_one_setup_and_a_blank_setup_stands_alone(db, people):
    data = sheet([line(1, "WO-1", "LTC5", "LTC 005", setup="Setup A"), line(2, "WO-2", "LTC9", "LTC 009", setup="setup a"),
                  line(3, "WO-3", "LTC25", "LTC 025", setup="SETUP  A"), line(4, "WO-4", "ADN2", "ADN 2", family="ADANI", setup="Setup B"),
                  line(5, "WO-5", "BLV3", "BLV 003")])
    pre = setups.import_sheet(db, people["plan"], data, commit=False)
    assert pre["counts"] == {"OK": 5, "ERROR": 0, "DUPLICATE": 0} and pre["setups"] == 3 and db.query(Setup).count() == 0
    setups.import_sheet(db, people["plan"], data, commit=True)
    assert db.query(Setup).count() == 3 and db.query(SetupLine).count() == 5
    a = next(d for d in (setups.setup_dict(db, s) for s in db.query(Setup)) if d["name"] == "Setup A")
    assert (a["wo_count"], a["part_count"], a["line_count"]) == (3, 3, 3) and a["wo_ids"] == ["WO-1", "WO-2", "WO-3"]
    assert a["fg_qty"] == 30


def test_new_rows_naming_an_existing_setup_are_added_to_it_and_old_lines_are_skipped(db, people):
    setups.import_sheet(db, people["plan"], sheet([line(1, "WO-1", "LTC5", "LTC 005", setup="A")]), commit=True)
    res = setups.import_sheet(db, people["plan"], sheet([line(1, "WO-1", "LTC5", "LTC 005", setup="A"), line(2, "WO-2", "LTC9", "LTC 009", setup="A")]),
                              commit=True)
    assert res["counts"] == {"OK": 1, "ERROR": 0, "DUPLICATE": 1}
    assert db.query(Setup).count() == 1 and db.query(SetupLine).count() == 2


def test_lines_can_be_added_by_hand_until_the_tooling_is_out(db, people, multi):
    ln = setups.add_line(db, people["plan"], multi["setup"], wo_id="WO-9", part_id="LTC5", tool_description="LTC 005", family="LTC")
    assert ln.setup_id == multi["setup"] and setups.setup_dict(db, db.get(Setup, multi["setup"]))["wo_count"] == 4
    with pytest.raises(ValidationError):
        setups.add_line(db, people["plan"], multi["setup"], wo_id="WO-9", part_id="LTC5")  # already planned
    issue(db, people, multi)
    with pytest.raises(ValidationError) as e:
        setups.add_line(db, people["plan"], multi["setup"], wo_id="WO-10", part_id="LTC5")
    assert "open issue" in e.value.message


# ---- matching a whole setup -----------------------------------------------------------------------------------------------
def test_a_setup_matches_only_when_every_line_points_to_the_same_assembly(db, people, kit):
    s = setups.create_setup(db, people["plan"], name="X", lines=[{"wo_id": "W1", "part_id": "A1", "family": "LTC"},
                                                                  {"wo_id": "W2", "part_id": "B1", "family": "OTHER"}])
    setups.add_map(db, people["eng"], definition_id=kit["asm"], family="LTC")
    d = setups.setup_dict(db, s)
    assert d["match"] == "UNMATCHED" and "W2 / B1" in d["match_reason"]  # names the line with no tooling
    assert [(ln["wo_id"], ln["match"]) for ln in d["lines"]] == [("W1", "MATCHED"), ("W2", "UNMATCHED")]
    from tests.test_setups import other_assembly
    other = other_assembly(db, people, kit, "t777")
    setups.add_map(db, people["eng"], definition_id=other.id, family="OTHER")
    d = setups.setup_dict(db, s)
    assert d["match"] == "AMBIGUOUS" and "different assemblies" in d["match_reason"]


# ---- issuing the whole setup ------------------------------------------------------------------------------------------------
def test_one_issue_covers_every_wo_and_part_of_the_setup(db, people, multi):
    i = issue(db, people, multi)
    assert db.query(IssueLine).filter_by(issue_id=i.id).count() == 3
    d = issues.issue_detail(db, i.id)
    assert d["wo_ids"] == ["WO-1", "WO-2", "WO-3"] and d["wo_id"] == "WO-1, WO-2, WO-3"
    assert [(l["wo_id"], l["part_id"], l["operation_id"]) for l in d["setup_lines"]] == [("WO-1", "LTC5", "OP10"), ("WO-2", "LTC9", "OP10"), ("WO-3", "LTC25", "OP10")]
    for wo in ("WO-1", "WO-2", "WO-3"):  # found by any of its WOs
        assert [r["id"] for r in issues.list_issues(db, wo_id=wo)[1]] == [i.id]
        assert usage.associations(db, wo)[0]["issue"] == d["code"]
    assert setups.setup_dict(db, db.get(Setup, multi["setup"]))["state"] == "ISSUED"


def test_a_subset_of_lines_can_be_issued_and_foreign_lines_are_refused(db, people, multi):
    lns = setups.lines_of(db, multi["setup"])
    other = setups.create_setup(db, people["plan"], wo_id="WO-OTHER", part_id="LTC5", tool_description="LTC 005", family="LTC")
    foreign = setups.lines_of(db, other.id)[0].id
    with pytest.raises(ValidationError) as e:
        issue(db, people, multi, line_ids=[lns[0].id, foreign])
    assert "does not belong" in e.value.message
    with pytest.raises(ValidationError):
        issue(db, people, multi, wo_id="WO-NOPE")
    i = issue(db, people, multi, line_ids=[lns[0].id, lns[2].id])
    assert issues.issue_detail(db, i.id)["wo_ids"] == ["WO-1", "WO-3"]
    assert db.get(Issue, i.id).wo_id == "WO-1"


def test_the_operation_of_each_part_is_chosen_when_it_is_not_clear(db, people, multi):
    mappings.create_mapping(db, people["eng"], part_id="LTC5", routing_revision="R2", operation_id="OP20", definition_id=multi["asm"], selected=False)
    lns = setups.lines_of(db, multi["setup"])
    # LTC5 now has two selectable operations; the ME-selected one (OP10) is used
    assert issues.issue_detail(db, issue(db, people, multi, line_ids=[lns[0].id]).id)["setup_lines"][0]["operation_id"] == "OP10"


def test_an_unclear_operation_must_be_chosen_and_the_choice_is_kept(db, people, multi):
    for part in ("LTC5",):
        for m in mappings.list_mappings(db, part_id=part)[1]:
            mappings.update_mapping(db, people["eng"], m["id"], fields_set={"selected"}, selected=False)
    mappings.create_mapping(db, people["eng"], part_id="LTC5", routing_revision="R2", operation_id="OP20", definition_id=multi["asm"], selected=False)
    lns = setups.lines_of(db, multi["setup"])
    with pytest.raises(ValidationError) as e:
        issue(db, people, multi, line_ids=[lns[0].id])
    assert "Choose the operation" in e.value.message and "LTC5" in e.value.message
    op20 = next(m["id"] for m in mappings.list_mappings(db, part_id="LTC5")[1] if m["operation_id"] == "OP20")
    i = issue(db, people, multi, line_ids=[lns[0].id], line_mappings={lns[0].id: op20})
    assert issues.issue_detail(db, i.id)["setup_lines"][0]["operation_id"] == "OP20"


def test_usage_goes_to_the_right_wo_with_that_parts_weight(db, people, multi):
    oms = auth.create_user(db, people["admin"], "oms", "OMS", PW, ["INTEGRATION"])
    i = issue(db, people, multi)
    issues.acknowledge_issue(db, people["prod"], i.id)
    now = datetime.utcnow()
    db.query(Issue).update({"issued_at": now - timedelta(hours=3)})
    db.query(BuildComponent).update({"fitted_at": now - timedelta(hours=4)})
    db.commit()
    t = (now - timedelta(minutes=5)).isoformat()

    def post(sid, wo, castings):
        return usage.ingest(db, oms, {"source_event_id": sid, "wo_id": wo, "operation_id": "OP10", "build": f"BLD-{multi['build']:06d}",
                                      "production_end": t, "cycles": 10, "castings": castings})
    r2 = post("E2", "WO-2", 20)  # LTC9 is 30 kg: 20 x 30 / 1000 = 0.6 t
    r3 = post("E3", "WO-3", 20)  # LTC25 is 40 kg: 0.8 t
    assert r2["result"] == "POSTED" and r2["event"]["tonnage"] == 0.6 and r3["event"]["tonnage"] == 0.8
    assert post("E9", "WO-NOT-IN-SETUP", 5)["event"]["reason_code"] == "NO_ISSUE"  # a WO that is not in the setup is never guessed


def test_handing_the_set_on_to_a_setup_with_several_lines(db, people, multi):
    first = issue(db, people, multi, line_ids=[setups.lines_of(db, multi["setup"])[0].id])
    issues.acknowledge_issue(db, people["prod"], first.id)
    nxt = setups.create_setup(db, people["plan"], name="SETUP-B", lines=[
        {"wo_id": "WO-20", "part_id": "LTC9", "tool_description": "LTC 009", "family": "LTC"},
        {"wo_id": "WO-21", "part_id": "LTC25", "tool_description": "LTC 025", "family": "LTC"}])
    new = issues.hand_over(db, people["stores"], first.id, setup_id=nxt.id)
    d = issues.issue_detail(db, new.id)
    assert d["wo_ids"] == ["WO-20", "WO-21"] and d["status"] == "PENDING_HANDOVER"
    assert issues.issue_detail(db, first.id)["status"] == "CLOSED"


# ---- over HTTP ---------------------------------------------------------------------------------------------------------------
def test_setup_with_lines_and_issue_with_chosen_lines_over_http(api, login, multi):
    body = {"name": "SETUP-H", "lines": [{"wo_id": "H1", "part_id": "LTC5", "tool_description": "LTC 005", "family": "LTC"},
                                         {"wo_id": "H2", "part_id": "LTC9", "tool_description": "LTC 009", "family": "LTC"}]}
    assert api.post("/setups", json=body, headers=login("stores")).status_code == 403
    r = api.post("/setups", json=body, headers=login("plan"))
    assert r.status_code == 201 and r.json()["wo_count"] == 2 and r.json()["match"] == "MATCHED"
    sid = r.json()["id"]
    added = api.post(f"/setups/{sid}/lines", json={"wo_id": "H3", "part_id": "LTC25", "family": "LTC", "tool_description": "LTC 025"}, headers=login("plan"))
    assert added.status_code == 201 and added.json()["wo_count"] == 3
    offered = api.get(f"/setups/for-build/{multi['build']}", headers=login("stores")).json()
    assert {o["id"]: (o["wo_count"], o["part_count"]) for o in offered}[sid] == (3, 3)
    lines = api.get(f"/setups/{sid}", headers=login("stores")).json()["lines"]
    issue_body = {"setup_id": sid, "build_id": multi["build"], "confirmed_item_ids": multi["ids"], "line_ids": [lines[0]["id"], lines[2]["id"]]}
    ok = api.post("/issues", json=issue_body, headers=login("stores"))
    assert ok.status_code == 201, ok.text
    assert ok.json()["wo_ids"] == ["H1", "H3"] and len(ok.json()["setup_lines"]) == 2
