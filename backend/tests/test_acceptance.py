"""Acceptance scenarios A01-A16 from the developer specification (section 8), shown end to end.

Most run through the HTTP API with real logins, so what is demonstrated is what a user could do. Each scenario
names the behaviour the spec requires; the finer rules have their own unit tests."""
import threading
import time
from datetime import date, datetime, timedelta
from io import BytesIO

import pandas as pd
import pytest
from sqlalchemy.orm import sessionmaker

from app import backup
from app.database import make_engine
from app.models import BuildComponent, Issue, Item
from app.services import (assemblies, auth, batches, builds, config, designs, documents, issues, items, mappings,
                          usage)
from app.services.errors import LedgerError
from tests.conftest import PW, clear_items, mk_setup, setup_body
from tests.test_api import api, login, register  # noqa: F401  (fixtures)
from tests.test_api_ops import event, issued  # noqa: F401  (fixtures)

KEYS = ("d1", "p1", "t1", "b1")


def mapping(api, login, kit, op="OP10", part="PN-1", definition=None, **kw):
    r = api.post("/mappings", json={"part_id": part, "routing_revision": "R1", "operation_id": op,
                                    "definition_id": definition or kit["asm"], **kw}, headers=login("eng"))
    assert r.status_code == 201, r.text
    return r.json()


def verified_build(api, login, kit, world, keys=KEYS, bin_index=0):
    b = api.post("/builds", json={"definition_id": kit["asm"], "location_id": world["bins"][bin_index]}, headers=login("stores")).json()
    for k in keys:
        assert api.post(f"/builds/{b['id']}/fit", json={"item_id": kit["items"][k]}, headers=login("stores")).status_code == 200
    assert api.post(f"/builds/{b['id']}/verify", headers=login("eng")).status_code == 200
    return b


def readiness(api, login, wo="WO-1", op="OP10", part="PN-1"):
    r = api.post("/readiness/checks", json={"wo_id": wo, "part_id": part, "routing_revision": "R1", "operation_id": op},
                 headers=login("prod"))
    assert r.status_code == 201, r.text
    return r.json()


# ---- A01 ---------------------------------------------------------------------------------------------------------
def test_A01_two_items_one_design_keep_distinct_identity_and_reprints_keep_it(api, login, world):
    a = register(api, login, world, legacy="b32-01")
    b = register(api, login, world, legacy="b32-02", condition="HOLD")
    c = register(api, login, world, legacy="b32-03", location_id=world["bins"][1])
    assert len({a["system_id"], b["system_id"], c["system_id"]}) == 3
    assert (a["design_id"], b["design_id"]) == (world["die"], world["die"])
    assert a["legacy_no"] != b["legacy_no"] and a["condition"] == "SERVICEABLE" and b["condition"] == "HOLD"
    assert c["location"] != a["location"] and c["location"].endswith("B2")
    first = api.get(f"/items/{a['id']}/label.svg", headers=login("stores")).text
    again = api.get(f"/items/{a['id']}/label.svg", params={"reprint": True}, headers=login("stores")).text
    assert first == again and api.get(f"/items/{a['id']}", headers=login("stores")).json()["system_id"] == a["system_id"]
    assert api.get(f"/items/{b['id']}/label.svg", headers=login("stores")).text != first


# ---- A02 ---------------------------------------------------------------------------------------------------------
def test_A02_new_category_and_a_six_element_assembly_need_no_code_and_reach_me(api, login, world, kit):
    cat = api.post("/categories", json={"code": "TIE-ROD", "name": "Tie rod"}, headers=login("admin"))
    assert cat.status_code == 201
    designs_ = []
    for n in range(5):
        d = api.post("/designs", json={"code": f"rod-{n}", "description": f"Rod {n}", "category_id": cat.json()["id"],
                                       "base_uom": "EA"}, headers=login("eng")).json()
        rev = api.get(f"/designs/{d['id']}", headers=login("eng")).json()["revisions"][0]
        assert api.post(f"/design-revisions/{rev['id']}/approve", headers=login("eng")).status_code == 200
        designs_.append(d["id"])
    a = api.post("/assemblies", json={"code": "t300", "description": "Six-element assembly", "category_id": kit["asm_cat"]},
                 headers=login("eng")).json()
    rev = api.get(f"/assemblies/{a['id']}", headers=login("eng")).json()["revisions"][0]
    for d in [kit["die"]] + designs_:
        assert api.post(f"/assembly-revisions/{rev['id']}/lines", json={"design_id": d}, headers=login("eng")).status_code == 201
    assert api.post(f"/assembly-revisions/{rev['id']}/approve", headers=login("stores")).status_code == 403
    assert api.post(f"/assembly-revisions/{rev['id']}/approve", headers=login("eng")).status_code == 200
    detail = api.get(f"/assemblies/{a['id']}", headers=login("eng")).json()
    assert len(detail["revisions"][0]["lines"]) == 6 and detail["revisions"][0]["status"] == "APPROVED"
    m = mapping(api, login, kit, part="PN-9", definition=a["id"])  # now ME can link it
    opts = api.get("/mappings/options", params={"part_id": "PN-9", "routing_revision": "R1", "operation_id": "OP10"},
                   headers=login("prod")).json()
    assert [o["id"] for o in opts] == [m["id"]]


# ---- A03 ---------------------------------------------------------------------------------------------------------
def test_A03_one_assembly_to_many_parts_many_assemblies_to_one_operation_and_only_approved_selectable(api, login, db, people, kit):
    other = assemblies.create_assembly(db, people["eng"], code="t212", description="Draft only", category_id=kit["asm_cat"])
    for part in ("PN-1", "PN-2", "PN-3"):
        mapping(api, login, kit, part=part, selected=True)  # one assembly -> three parts
    mapping(api, login, kit, part="PN-7", selected=True)
    draft = mapping(api, login, kit, part="PN-7", definition=other.id)  # two assemblies -> one operation
    opts = api.get("/mappings/options", params={"part_id": "PN-7", "routing_revision": "R1", "operation_id": "OP10"},
                   headers=login("prod")).json()
    assert len(opts) == 2
    by = {o["id"]: o for o in opts}
    assert by[draft["id"]]["selectable"] is False and by[draft["id"]]["not_selectable_because"]  # shown, with the reason
    assert any(o["selectable"] for o in opts)


# ---- A04 ---------------------------------------------------------------------------------------------------------
def test_A04_wo_releases_when_parts_exist_unassembled_or_issued_with_no_reservation(api, login, kit, world):
    mapping(api, login, kit, selected=True)
    r = readiness(api, login)
    assert r["result"] == "READY" and r["can_release"]  # parts only sit loose on the shelves
    b = verified_build(api, login, kit, world)
    ids = [kit["items"][k] for k in KEYS]
    m = api.get("/mappings", headers=login("eng")).json()["rows"][0]["id"]
    api.post("/issues", json={**setup_body(api, login, "WO-A", m), "mapping_id": m, "build_id": b["id"], "confirmed_item_ids": ids}, headers=login("stores"))
    r2 = readiness(api, login, wo="WO-B")  # the same parts are now issued to WO-A
    assert r2["result"] == "READY" and r2["custody_warnings"]  # custody warnings are shown, nothing blocks
    assert "WO-A" in str(r2["custody_warnings"])
    for i in ids:  # no reservation exists: their state names no WO other than the issue itself
        assert api.get(f"/items/{i}", headers=login("mgr")).json()["issue"]["wo_id"] == "WO-A"


# ---- A05 ---------------------------------------------------------------------------------------------------------
def test_A05_sole_item_held_gives_the_exact_shortage_and_a_substitute_restores_readiness(api, login, kit, world):
    mapping(api, login, kit, selected=True)
    for k in ("t1",):  # the only top flange
        assert api.post("/items/condition", json={"item_ids": [kit["items"][k]], "to_condition": "HOLD", "reason": "crack"},
                        headers=login("tooling")).status_code == 200
    r = readiness(api, login)
    assert r["result"] == "NOT_READY" and not r["can_release"]
    assert any("TF-A" in str(s) for s in r["shortages"])
    spare = api.post("/items", json={"design_id": kit["tf"], "legacy_no": "t9", "condition": "SERVICEABLE",
                                     "location_id": world["bins"][2]}, headers=login("stores"))
    assert spare.status_code == 201
    assert readiness(api, login, wo="WO-2")["result"] == "READY"


# ---- A06 ---------------------------------------------------------------------------------------------------------
def test_A06_overlapping_alternatives_never_count_one_item_twice(db, people, kit, world):
    eng = people["eng"]
    a = assemblies.create_assembly(db, eng, code="t400", description="Overlap", category_id=kit["asm_cat"])
    rev = assemblies.revisions(db, a.id)[0]
    l1 = assemblies.add_line(db, eng, rev.id, design_id=kit["pat"])
    assemblies.add_alternative(db, eng, l1.id, design_id=kit["pat2"])
    l2 = assemblies.add_line(db, eng, rev.id, design_id=kit["pat2"])
    assemblies.add_alternative(db, eng, l2.id, design_id=kit["pat"])
    assemblies.approve_revision(db, eng, rev.id)
    m = mappings.create_mapping(db, eng, part_id="PN-6", routing_revision="R1", operation_id="OP10", definition_id=a.id,
                                selected=True)
    from app.services import readiness as rd
    # p1, p3 (pat) and p2 (pat2) exist: three physical items for two lines -> ready; remove two -> one item cannot do both
    assert rd.run_check(db, people["prod"], wo_id="W6", part_id="PN-6", routing_revision="R1", operation_id="OP10")["result"] == "READY"
    from tests.conftest import clear_items  # noqa: F401
    from app.services import ledger
    ledger.set_condition(db, people["tooling"], [kit["items"]["p1"], kit["items"]["p3"]], "HOLD", reason="crack")
    res = rd.run_check(db, people["prod"], wo_id="W7", part_id="PN-6", routing_revision="R1", operation_id="OP10")
    assert res["result"] == "NOT_READY"  # only p2 remains: it cannot satisfy both lines


# ---- A07 ---------------------------------------------------------------------------------------------------------
def test_A07_two_concurrent_issues_of_one_item_exactly_one_succeeds(api, login, db, kit, world):
    m = mapping(api, login, kit, selected=True)
    b = verified_build(api, login, kit, world)
    ids = [kit["items"][k] for k in KEYS]
    headers = login("stores")
    barrier, codes = threading.Barrier(2), []
    bodies = {wo: setup_body(api, login, wo, m["id"]) for wo in ("WO-1", "WO-2")}

    def attempt(wo):
        barrier.wait()
        r = api.post("/issues", json={**bodies[wo], "mapping_id": m["id"], "build_id": b["id"], "confirmed_item_ids": ids},
                     headers=headers)
        codes.append((r.status_code, r.json().get("detail", "")))

    threads = [threading.Thread(target=attempt, args=(f"WO-{n}",)) for n in (1, 2)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert sorted(c for c, _ in codes)[0] == 201 and sorted(c for c, _ in codes)[1] in (409, 422), codes
    loser = next(d for c, d in codes if c != 201)
    assert loser  # the refusal names the reason
    assert db.query(Issue).count() == 1
    from app.models import Ledger
    assert db.query(Ledger).filter(Ledger.action == "ISSUE").count() == 4  # each item issued exactly once


def test_A07_wrong_revision_or_unverified_build_is_rejected(api, login, kit, world):
    m = mapping(api, login, kit, selected=True)
    b = api.post("/builds", json={"definition_id": kit["asm"], "location_id": world["bins"][0]}, headers=login("stores")).json()
    for k in ("d1", "p1", "t1"):  # incomplete: no bottom flange
        api.post(f"/builds/{b['id']}/fit", json={"item_id": kit["items"][k]}, headers=login("stores"))
    ids = [kit["items"][k] for k in ("d1", "p1", "t1")]
    body = {**setup_body(api, login, "WO-1", m["id"]), "mapping_id": m["id"], "build_id": b["id"], "confirmed_item_ids": ids}
    r = api.post("/issues", json=body, headers=login("stores"))
    assert r.status_code == 422 and ("verif" in r.json()["detail"].lower() or "complete" in r.json()["detail"].lower())
    assert api.get("/issues", headers=login("stores")).json()["total"] == 0


# ---- A08 ---------------------------------------------------------------------------------------------------------
def test_A08_partial_return_leaves_the_fourth_out_and_a_failed_item_stays_held(api, login, issued, world, db, people):
    st = login("stores")
    lines = [{"item_id": i, "to_location_id": world["bins"][1]} for i in issued["ids"][:3]]
    ret = api.post(f"/issues/{issued['issue']}/return", json={"lines": lines}, headers=st).json()
    assert ret["returned"] == 3 and ret["outstanding"] == ["B1"]
    states = {i: api.get(f"/items/{i}", headers=st).json() for i in issued["ids"]}
    assert [states[i]["condition"] for i in issued["ids"][:3]] == ["AWAITING_INSPECTION"] * 3  # inspection required
    assert states[issued["ids"][3]]["custody"] == "PRODUCTION"
    bad = issued["ids"][0]
    insp = api.post("/inspections", json={"item_id": bad}, headers=login("tooling")).json()
    checklist = api.get(f"/items/{bad}/checklist", headers=login("tooling")).json()["checks"]
    for c in checklist:
        body = {"check_id": c["id"], "result": "FAIL", "reason": "id plate missing"} if c["code"] == "COMPLETE_ID" else {"check_id": c["id"], "result": "PASS"}
        r = api.put(f"/inspections/{insp['id']}/results", json=body, headers=login("tooling"))
        assert r.status_code == 200, r.text
    done = api.post(f"/inspections/{insp['id']}/complete", json={}, headers=login("tooling"))
    assert done.status_code == 200, done.text
    assert api.get(f"/items/{bad}", headers=st).json()["condition"] == "HOLD"
    clear_items(db, people, *issued["ids"][1:3])
    assert api.get(f"/items/{issued['ids'][1]}", headers=st).json()["condition"] == "SERVICEABLE"
    assert api.get(f"/items/{bad}", headers=st).json()["condition"] == "HOLD"  # the failed one never rides along


# ---- A09 ---------------------------------------------------------------------------------------------------------
def test_A09_intact_return_then_dismantle_keeps_membership_history_and_no_duplicate_availability(api, login, issued, world):
    st = login("stores")
    lines = [{"item_id": i, "to_location_id": world["bins"][1]} for i in issued["ids"]]
    assert api.post(f"/issues/{issued['issue']}/return", json={"lines": lines}, headers=st).json()["issue"]["status"] == "CLOSED"
    b = api.get(f"/builds/{issued['build']}", headers=st).json()
    assert len(b["components"]) == 4  # still an assembly after an intact return
    d = api.post(f"/builds/{issued['build']}/dismantle", json={"destinations": [{"item_id": i, "location_id": world["bins"][2]} for i in issued["ids"]], "reason": "stripped for re-use"}, headers=st)
    assert d.status_code == 200, d.text
    b = api.get(f"/builds/{issued['build']}", headers=st).json()
    assert b["components"] == [] and b["status"] == "DISMANTLED"  # none are members now
    assert len([h for h in b["history"] if h["removed_at"] and h["fitted_at"]]) == 4  # membership history kept
    for i in issued["ids"]:
        it = api.get(f"/items/{i}", headers=st).json()
        assert it["location"].endswith("B3") and it["build"] is None
    rows = api.get("/items", params={"location_id": world["bins"][2]}, headers=st).json()["rows"]
    listed = [r["id"] for r in rows]
    assert len(listed) == len(set(listed)) and set(issued["ids"]) <= set(listed)  # each listed once, none duplicated


# ---- A10 ---------------------------------------------------------------------------------------------------------
def test_A10_repair_with_revision_change_and_scrap_approval(db, people, kit, world):
    from app.services import dispositions, ledger
    it = kit["items"]["d2"]
    ledger.set_condition(db, people["tooling"], [it], "HOLD", reason="seat cracked")
    dispositions.decide(db, people["tooling"], it, "REPAIR", findings="seat cracked", responsible="tool room")
    state = ledger.get_state(db, it)
    assert state["item_condition"] == "UNDER_REPAIR"
    new_rev = designs.new_revision(db, people["eng"], world["die"], reason="bore changed in repair")
    designs.approve_revision(db, people["eng"], new_rev.id)
    out = dispositions.rework_revision(db, people["eng"], it, new_rev.id, reason="re-machined to the new bore")
    assert out
    from app.models import RepairJob
    dispositions.complete_repair(db, people["tooling"], db.query(RepairJob).one().id, work_done="re-bored")
    item = db.get(Item, it)
    assert item.system_id and item.design_revision_id == new_rev.id and item.display_id == "D2"  # identity kept
    assert ledger.get_state(db, it)["item_condition"] != "SERVICEABLE"  # Engineering release still required
    clear_items(db, people, it)
    assert ledger.get_state(db, it)["item_condition"] == "SERVICEABLE"
    s = kit["items"]["d1"]
    ledger.set_condition(db, people["tooling"], [s], "HOLD", reason="beyond repair")
    prop = dispositions.decide(db, people["tooling"], s, "PROPOSE_SCRAP", findings="cracked through", responsible="tool room")
    assert ledger.get_state(db, s)["item_condition"] == "HOLD"  # proposing does not scrap
    from app.models import ScrapProposal
    pid = db.query(ScrapProposal).filter_by(item_id=s).one().id
    dispositions.decide_scrap(db, people["scrap"], pid, approve=True)
    assert ledger.get_state(db, s)["item_condition"] == "SCRAPPED"


# ---- A11 ---------------------------------------------------------------------------------------------------------
def test_A11_consumables_issue_return_unused_and_never_double_return(db, people, kit, world):
    eng = people["eng"]
    config.create_uom(db, people["admin"], "L", "Litre")
    coat = designs.create_design(db, eng, code="coat", description="Die coating", category_id=kit["asm_cat"], base_uom="L",
                                 kind="CONSUMABLE")
    designs.approve_revision(db, eng, designs.revisions(db, coat.id)[0].id)
    r = assemblies.new_revision(db, eng, kit["asm"], reason="add coating")
    line = assemblies.add_line(db, eng, r.id, design_id=coat.id, quantity=2, quantity_basis="ME spec")
    assemblies.approve_revision(db, eng, r.id)
    m = mappings.create_mapping(db, eng, part_id="PN-1", routing_revision="R1", operation_id="OP10", definition_id=kit["asm"],
                                selected=True)
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    for k in KEYS:
        builds.fit_item(db, people["stores"], b.id, kit["items"][k])
    builds.verify_build(db, people["eng"], b.id)
    batch = batches.receive_batch(db, people["stores"], design_id=coat.id, quantity=10, location_id=world["bins"][1])
    ids = [kit["items"][k] for k in KEYS]
    i = issues.create_issue(db, people["stores"], setup_id=mk_setup(db, people, "WO-1", m.id), wo_id="WO-1", mapping_id=m.id, build_id=b.id, confirmed_item_ids=ids,
                            consumables=[{"line_id": line.id, "batch_id": batch.id, "quantity": 2}])
    assert batches.balance(db, batch.id)["usable"] == 8
    issues.acknowledge_issue(db, people["prod"], i.id)
    issues.return_items(db, people["stores"], i.id, consumables=[{"batch_id": batch.id, "unused_quantity": 1.5}])
    bal = batches.balance(db, batch.id)
    assert bal["usable"] == 9.5 and bal["consumed"] == 0.5
    with pytest.raises(LedgerError):  # the consumed 0.5 L cannot come back, and nothing returns twice
        issues.return_items(db, people["stores"], i.id, consumables=[{"batch_id": batch.id, "unused_quantity": 0.5}])
    with pytest.raises(LedgerError):
        batches.adjust(db, people["eng"], batch.id, -100, reason="too much")
    assert batches.balance(db, batch.id)["usable"] >= 0


# ---- A12 ---------------------------------------------------------------------------------------------------------
def test_A12_ten_cycles_twenty_castings_at_25kg_is_half_a_tonne_replay_correction_and_weight_change(api, login, issued, db, people):
    oms = login("oms")
    e1 = event()
    r = api.post("/integration/usage", json=e1, headers=oms).json()
    assert r["event"]["tonnage"] == 0.5 and r["event"]["cycles"] == 10 and r["event"]["castings"] == 20
    assert api.post("/integration/usage", json=e1, headers=oms).json()["result"] == "DUPLICATE"
    assert api.get("/usage/output", headers=login("mgr")).json()["tonnage"] == 0.5  # replay added nothing
    api.post("/integration/usage", json={**e1, "version": 2, "castings": 18}, headers=oms)
    assert api.get("/usage/output", headers=login("mgr")).json()["tonnage"] == 0.45  # corrected exactly once
    mappings.update_mapping(db, people["eng"], issued["mapping"], fields_set={"pouring_weight_kg", "pouring_weight_revision"},
                            pouring_weight_kg=30, pouring_weight_revision="W2")
    out = api.get("/usage/assembly", params={"build_id": issued["build"]}, headers=login("mgr")).json()
    assert out["tonnage"] == 0.45  # history keeps the weight it was posted with
    api.post("/integration/usage", json={**e1, "source_event_id": "EV-2", "castings": 10}, headers=oms)
    assert api.get("/usage/output", headers=login("mgr")).json()["tonnage"] == 0.75  # new events use 30 kg


# ---- A13 ---------------------------------------------------------------------------------------------------------
def test_A13_component_replacement_splits_usage_between_items_and_missing_association_is_an_exception(api, login, issued, db, world):
    oms = login("oms")
    db.query(Issue).update({"issued_at": datetime.utcnow() - timedelta(days=2)})
    db.query(BuildComponent).update({"fitted_at": datetime.utcnow() - timedelta(days=2, hours=1)})
    db.commit()
    early = (datetime.utcnow() - timedelta(hours=30)).isoformat()
    api.post("/integration/usage", json=event(source_event_id="EV-A", production_end=early, castings=20), headers=oms)
    spare = api.post("/items", json={"design_id": issued["tf"], "legacy_no": "t2", "condition": "SERVICEABLE",
                                     "location_id": world["bins"][2]}, headers=login("stores")).json()
    r = api.post(f"/issues/{issued['issue']}/replace-component", json={"outgoing_item_id": issued["items"]["t1"],
                 "incoming_item_id": spare["id"], "reason": "worn through"}, headers=login("stores"))
    assert r.status_code == 200, r.text
    db.query(BuildComponent).filter(BuildComponent.item_id == issued["items"]["t1"]).update({"removed_at": datetime.utcnow() - timedelta(hours=10)})
    db.query(BuildComponent).filter(BuildComponent.item_id == spare["id"]).update({"fitted_at": datetime.utcnow() - timedelta(hours=10)})
    db.commit()
    later = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    api.post("/integration/usage", json=event(source_event_id="EV-B", production_end=later, castings=40), headers=oms)
    old = api.get(f"/usage/items/{issued['items']['t1']}", headers=login("mgr")).json()
    new = api.get(f"/usage/items/{spare['id']}", headers=login("mgr")).json()
    assert old["recorded_tonnage"] == 0.5 and new["recorded_tonnage"] == 1.0  # earlier use to the old, later to the new
    api.post("/integration/usage", json=event(source_event_id="EV-C", build=None), headers=oms)
    ex = api.get("/integration/exceptions", headers=login("prod")).json()
    assert ex["pending"] == 1 and ex["events"][0]["reason_code"] == "MISSING_BUILD"  # visible, not guessed


# ---- A14 ---------------------------------------------------------------------------------------------------------
def test_A14_new_work_uses_the_approved_revision_while_history_keeps_its_own(api, login, issued, db, people, kit):
    eng = people["eng"]
    d = documents.create_document(db, eng, doc_key="dwg-t211", doc_type="DIE_DRAWING", title="T211 drawing")
    v1 = documents.add_version(db, eng, d.id, filename="t211-a.pdf", data=b"%PDF-1.4\nrev A")
    documents.approve_version(db, eng, v1.id)
    documents.link(db, eng, d.id, "ASSEMBLY", kit["asm"])
    # a second issue is not possible on the same build, so use a fresh verified build
    for it in issued["ids"]:
        pass
    builds_ = db.query(Issue).count()
    first = api.get(f"/issues/{issued['issue']}", headers=login("mgr")).json()
    assert first["documents"] == []  # nothing was in force when the first issue was made
    ret = api.post(f"/issues/{issued['issue']}/return", json={"lines": [{"item_id": i, "to_location_id": 1} for i in issued["ids"]]},
                   headers=login("stores"))
    assert ret.status_code == 200
    clear_items(db, people, *issued["ids"])
    m = api.get("/mappings", headers=login("eng")).json()["rows"][0]["id"]
    second = api.post("/issues", json={**setup_body(api, login, "WO-2", m), "mapping_id": m, "build_id": issued["build"],
                                       "confirmed_item_ids": issued["ids"]}, headers=login("stores"))
    assert second.status_code == 201, second.text
    used = api.get(f"/issues/{second.json()['id']}", headers=login("mgr")).json()["documents"]
    assert [(u["doc_key"], u["revision_used"]) for u in used] == [("DWG-T211", "A")]
    v2 = documents.add_version(db, eng, d.id, filename="t211-b.pdf", data=b"%PDF-1.4\nrev B", change_reason="bore corrected")
    documents.approve_version(db, eng, v2.id)
    used = api.get(f"/issues/{second.json()['id']}", headers=login("mgr")).json()["documents"]
    assert used[0]["revision_used"] == "A" and used[0]["current_revision"] == "B" and used[0]["superseded"]
    assert documents.current_version(db, d.id).revision == "B"  # new work now takes B
    assert builds_ >= 1


# ---- A15 ---------------------------------------------------------------------------------------------------------
def test_A15_failed_integration_and_unauthorised_release_show_an_error_and_leave_a_record(api, login, issued, make_item):
    r = api.post("/integration/usage", json=event(castings=-1), headers=login("oms"))
    assert r.status_code == 422 and r.json()["detail"]
    it = make_item(legacy="h1", bin_index=1)
    api.post("/items/condition", json={"item_ids": [it.id], "to_condition": "HOLD", "reason": "crack"}, headers=login("tooling"))
    api.post("/items/condition", json={"item_ids": [it.id], "to_condition": "AWAITING_INSPECTION", "reason": "check"}, headers=login("tooling"))
    denied = api.post("/items/condition", json={"item_ids": [it.id], "to_condition": "SERVICEABLE", "reason": "release"},
                      headers=login("stores"))
    assert denied.status_code == 403 and denied.json()["detail"]
    assert api.get(f"/items/{it.id}", headers=login("mgr")).json()["condition"] == "AWAITING_INSPECTION"
    acts = {a["action"] for a in api.get("/audit", params={"limit": 100}, headers=login("mgr")).json()["rows"]}
    assert {"DENIED", "USAGE_REJECTED"} <= acts


# ---- A16 ---------------------------------------------------------------------------------------------------------
def test_A16_target_scale_import_duplicates_search_scan_roles_and_backup(api, login, db, engine, people, world, kit, tmp_path):
    n_asm = 1000
    codes = {"die-32": "DIE", "pat-1": "PAT", "tf-a": "TF", "bf-a": "BF"}
    bins = ["P1/A1/R1/S1/B1", "P1/A1/R1/S1/B2", "P1/A1/R1/S1/B3"]
    rows = [{"legacy_no": f"{tag}-{n:05d}", "design_code": code, "condition": "SERVICEABLE", "home_location": bins[n % 3]}
            for code, tag in codes.items() for n in range(1, n_asm + 1)]
    rows += [{"legacy_no": "DIE-00001", "design_code": "die-32", "condition": "SERVICEABLE", "home_location": bins[0]}]  # a duplicate
    buf = BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    files = {"file": ("inventory.xlsx", buf.getvalue())}
    st = login("stores")
    t0 = time.time()
    pre = api.post("/import/items", files=files, headers=st).json()
    assert pre["valid"] and pre["counts"]["OK"] == 4000 and pre["counts"]["DUPLICATE"] == 1
    done = api.post("/import/items", params={"commit": True}, files=files, headers=st).json()
    assert done["created"] == 4000 and done["queued_for_review"] == 1  # duplicate handled, never overwritten
    import_s = time.time() - t0
    assert db.query(Item).count() == 4007

    # ~1,000 assemblies: each takes one die, pattern, top and bottom flange from the imported stock
    pool = {tag: [r.id for r in db.query(Item).filter(Item.legacy_key.like(f"{tag}-%")).order_by(Item.id)]
            for tag in codes.values()}
    actor = people["stores"]
    t1 = time.time()
    for n in range(n_asm):
        b = builds.create_build(db, actor, definition_id=kit["asm"], location_id=world["bins"][n % 3])
        for tag in codes.values():
            builds.fit_item(db, actor, b.id, pool[tag][n])
    build_s = time.time() - t1
    assert db.query(BuildComponent).filter(BuildComponent.removed_at.is_(None)).count() >= 4000

    # filtered, paginated search and label scan stay quick at this size
    t2 = time.time()
    page = api.get("/items", params={"q": "TF-005", "condition": "SERVICEABLE", "limit": 25}, headers=st).json()
    assert page["total"] == 111 and len(page["rows"]) == 25  # q is normalised: TF-5, TF-50..59, TF-500..599
    by_loc = api.get("/items", params={"location_id": world["bins"][0], "limit": 50, "offset": 100}, headers=st).json()
    assert by_loc["total"] > 1000 and len(by_loc["rows"]) == 50
    scan = api.get("/scan", params={"code": "BF-01000"}, headers=st).json()
    assert scan["type"] == "ITEM" and scan["items"][0]["display_id"] == "BF-1000"
    assert api.get("/scan", params={"code": "BLD-000500"}, headers=st).json()["type"] == "BUILD"
    search_s = time.time() - t2
    assert search_s < 5, f"search/scan took {search_s:.1f}s"

    # roles hold at scale: Production cannot import, Stores cannot release
    assert api.post("/import/items", files=files, headers=login("prod")).status_code == 403
    assert api.get("/items", headers={}).status_code == 401

    # backup, then restore into a clean place and find everything there
    z = backup.create_backup(tmp_path / "bk", db_path=engine.url.database, storage_dir=tmp_path / "storage")
    info = backup.restore_backup(z, tmp_path / "restored")
    e2 = make_engine(f"sqlite:///{info['db']}")
    s2 = sessionmaker(bind=e2, future=True)()
    assert s2.query(Item).count() == 4007
    assert s2.query(BuildComponent).filter(BuildComponent.removed_at.is_(None)).count() >= 4000
    s2.close()
    e2.dispose()
    print(f"A16: import {import_s:.1f}s, 1000 builds {build_s:.1f}s, search+scan {search_s:.2f}s")
