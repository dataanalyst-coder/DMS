from xml.dom import minidom

import pytest
from tests.conftest import mk_setup, setup_body

from tests.test_api import api, login  # noqa: F401  (fixtures)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
KEYS = ("d1", "p1", "t1", "b1")


def test_new_routes_require_a_token(api):
    for method, path in [("get", "/batches"), ("post", "/batches"), ("get", "/batches/1"), ("post", "/batches/1/hold"),
                         ("post", "/batches/1/release"), ("post", "/batches/1/adjust"), ("post", "/batches/1/move"),
                         ("post", "/batch-ledger/1/reverse"), ("get", "/batches/1/label.svg"),
                         ("get", "/checklists"), ("post", "/checklists"), ("get", "/checklists/1"),
                         ("post", "/checklist-versions/1/approve"), ("get", "/items/1/checklist"),
                         ("get", "/inspections"), ("get", "/inspections/queue"), ("post", "/inspections"),
                         ("get", "/inspections/1"), ("put", "/inspections/1/results"), ("post", "/inspections/1/complete"),
                         ("get", "/inspection-photos/1"), ("post", "/issues/1/inspections"), ("get", "/held"),
                         ("post", "/items/1/disposition"), ("post", "/repairs/1/complete"),
                         ("post", "/items/1/rework-revision"), ("post", "/scrap-proposals/1/decide"),
                         ("get", "/items/1/quality-history")]:
        assert getattr(api, method)(path).status_code == 401, (method, path)


@pytest.fixture
def stock(api, login, kit, world):
    login_h = login("admin")
    api.post("/uoms", json={"code": "L", "name": "Litre"}, headers=login_h)
    d = api.post("/designs", json={"code": "coat", "description": "Coating", "category_id": kit["asm_cat"], "base_uom": "L",
                                   "kind": "CONSUMABLE"}, headers=login("eng")).json()
    api.post(f"/design-revisions/{d['revisions'][0]['id']}/approve", headers=login("eng"))
    return {"design": d["id"], "bin": world["bins"]}


def test_batches_over_http(api, login, stock):
    body = {"design_id": stock["design"], "quantity": 50, "location_id": stock["bin"][0], "supplier_lot": "L-1"}
    assert api.post("/batches", json=body, headers=login("prod")).status_code == 403
    b = api.post("/batches", json=body, headers=login("stores"))
    assert b.status_code == 201 and b.json()["code"] == "BATCH-000001" and b.json()["usable"] == 50
    bid = b.json()["id"]
    assert api.post(f"/batches/{bid}/hold", json={"quantity": 20, "reason": "suspect"}, headers=login("tooling")).json()["held"] == 20
    assert api.post(f"/batches/{bid}/release", json={"quantity": 5, "reason": "ok"}, headers=login("stores")).status_code == 403
    assert api.post(f"/batches/{bid}/release", json={"quantity": 5, "reason": "lab ok"}, headers=login("eng")).json()["held"] == 15
    r = api.post(f"/batches/{bid}/adjust", json={"delta": -999, "reason": "count"}, headers=login("eng"))
    assert r.status_code == 422 and "negative" in r.json()["detail"]
    assert api.post(f"/batches/{bid}/adjust", json={"delta": -5, "reason": "count"}, headers=login("stores")).status_code == 403
    assert api.post(f"/batches/{bid}/adjust", json={"delta": -5, "reason": "count 40"}, headers=login("eng")).json()["usable"] == 30
    hist = api.get(f"/batches/{bid}", headers=login("mgr")).json()["history"]
    assert [h["action"] for h in hist] == ["ADJUST", "RELEASE_HOLD", "HOLD", "RECEIVE"]
    r = api.post(f"/batch-ledger/{hist[0]['txn_id']}/reverse", json={"reason": "miscount"}, headers=login("eng"))
    assert r.status_code == 201 and api.get(f"/batches/{bid}", headers=login("mgr")).json()["usable"] == 35
    assert api.get("/batches", params={"q": "L-1"}, headers=login("mgr")).json()["total"] == 1
    svg = api.get(f"/batches/{bid}/label.svg", headers=login("stores"))
    assert svg.status_code == 200 and minidom.parseString(svg.text) and "BATCH-000001" in svg.text
    assert api.get(f"/batches/{bid}/label.svg", params={"reprint": True}, headers=login("prod")).status_code == 403
    assert api.get("/scan", params={"code": "batch-000001"}, headers=login("stores")).json()["type"] == "BATCH"


def test_checklist_inspection_and_release_over_http(api, login, make_item, world):
    it = make_item()
    h_t, h_e = login("tooling"), login("eng")
    api.post("/items/condition", json={"item_ids": [it.id], "to_condition": "AWAITING_INSPECTION", "reason": "routine"}, headers=h_t)
    tpl = api.get("/checklists", headers=h_t).json()[0]
    # Engineering adds a measured design-specific check and approves it
    t = api.post("/checklists", json={"scope_type": "DESIGN", "scope_id": world["die"]}, headers=h_e).json()
    vid = t["versions"][0]["id"]
    r = api.post(f"/checklist-versions/{vid}/checks", json={"code": "bore", "title": "Bore", "check_type": "MEASUREMENT",
                                                            "unit": "mm", "min_value": 269.5, "max_value": 270.5, "evidence": "ON_FAIL"}, headers=h_e)
    assert r.status_code == 201
    assert api.post(f"/checklist-versions/{vid}/approve", headers=h_t).status_code == 403
    assert api.post(f"/checklist-versions/{vid}/approve", headers=h_e).status_code == 200
    eff = api.get(f"/items/{it.id}/checklist", headers=h_t).json()["checks"]
    assert [c["code"] for c in eff] == ["COMPLETE_ID", "CRACKS", "BORE"]
    # inspect: an out-of-limits bore needs a photo and forces a hold
    i = api.post("/inspections", json={"item_id": it.id}, headers=h_t)
    assert i.status_code == 201
    iid = i.json()["id"]
    for c in eff:
        body = {"check_id": c["id"], "value": 272.0} if c["code"] == "BORE" else {"check_id": c["id"], "result": "PASS"}
        assert api.put(f"/inspections/{iid}/results", json=body, headers=h_t).status_code == 200
    r = api.post(f"/inspections/{iid}/complete", json={}, headers=h_t)
    assert r.status_code == 422 and "Photo evidence is mandatory for: Bore" in r.json()["detail"]
    bore = next(c for c in eff if c["code"] == "BORE")["id"]
    p = api.post(f"/inspections/{iid}/photos", data={"check_id": str(bore)}, files={"file": ("b.png", PNG)}, headers=h_t)
    assert p.status_code == 201
    assert api.get(f"/inspection-photos/{p.json()['id']}").status_code == 401  # photos are protected
    assert api.get(f"/inspection-photos/{p.json()['id']}", headers=login("mgr")).content == PNG
    done = api.post(f"/inspections/{iid}/complete", json={"summary": "bore oversize"}, headers=h_t).json()
    assert done["outcome"] == "HOLD" and done["checklist_versions"]["design"]["version"] == "1"
    assert api.get(f"/items/{it.id}", headers=h_t).json()["condition"] == "HOLD"
    # hold -> repair decision -> repair -> back to inspection -> pass -> Engineering release
    d = api.post(f"/items/{it.id}/disposition", json={"decision": "REPAIR", "findings": "bore oversize 272.0",
                                                      "responsible": "tool room"}, headers=h_t)
    assert d.status_code == 201
    assert api.post(f"/items/{it.id}/disposition", json={"decision": "REPAIR", "findings": "x", "responsible": "y"},
                    headers=h_t).status_code == 422  # no longer held
    assert api.post(f"/items/{it.id}/disposition", json={"decision": "REPAIR", "findings": "x", "responsible": "y"},
                    headers=h_e).status_code == 403  # Engineering decides releases, not dispositions
    held = api.get("/held", headers=h_t).json()
    rid = held["open_repairs"][0]["id"]
    assert api.post(f"/repairs/{rid}/complete", json={"work_done": "sleeved and re-bored"}, headers=h_e).status_code == 403
    assert api.post(f"/repairs/{rid}/complete", json={"work_done": "sleeved and re-bored"}, headers=h_t).status_code == 200
    q = api.get("/inspections/queue", headers=h_t).json()
    assert [x["id"] for x in q] == [it.id]
    i2 = api.post("/inspections", json={"item_id": it.id}, headers=h_t).json()["id"]
    for c in eff:
        body = {"check_id": c["id"], "value": 270.0} if c["code"] == "BORE" else {"check_id": c["id"], "result": "PASS"}
        api.put(f"/inspections/{i2}/results", json=body, headers=h_t)
    assert api.post(f"/inspections/{i2}/complete", json={}, headers=h_t).json()["outcome"] == "PASS"
    rel = {"item_ids": [it.id], "to_condition": "SERVICEABLE", "reason": "technical release"}
    assert api.post("/items/condition", json=rel, headers=h_t).status_code == 403
    assert api.post("/items/condition", json=rel, headers=h_e).status_code == 200
    hist = api.get(f"/items/{it.id}/quality-history", headers=h_t).json()
    assert [x["outcome"] for x in hist["inspections"]] == ["PASS", "HOLD"] and hist["repairs"][0]["status"] == "COMPLETED"


def test_scrap_approval_over_http(api, login, make_item):
    it = make_item()
    api.post("/items/condition", json={"item_ids": [it.id], "to_condition": "HOLD", "reason": "cracked through"}, headers=login("tooling"))
    r = api.post(f"/items/{it.id}/disposition", json={"decision": "PROPOSE_SCRAP", "findings": "cracked through",
                                                      "responsible": "tooling"}, headers=login("tooling"))
    assert r.status_code == 201
    pid = api.get("/held", headers=login("mgr")).json()["pending_scrap"][0]["id"]
    assert api.get("/dashboard", headers=login("mgr")).json()["pending_scrap_proposals"] == 1
    assert api.post(f"/scrap-proposals/{pid}/decide", json={"approve": True}, headers=login("eng")).status_code == 403
    assert api.post(f"/scrap-proposals/{pid}/decide", json={"approve": False}, headers=login("scrap")).status_code == 422
    assert api.get(f"/items/{it.id}", headers=login("mgr")).json()["condition"] == "HOLD"
    assert api.post(f"/scrap-proposals/{pid}/decide", json={"approve": True, "note": "approved"}, headers=login("scrap")).status_code == 200
    assert api.get(f"/items/{it.id}", headers=login("mgr")).json()["condition"] == "SCRAPPED"


def test_consumable_issue_and_return_over_http(api, login, kit, world, stock):
    eng, st, pr = login("eng"), login("stores"), login("prod")
    # the assembly now needs 2 L of coating
    rv = api.post(f"/assemblies/{kit['asm']}/revisions", json={"reason": "add coating"}, headers=eng).json()["revisions"][-1]["id"]
    api.post(f"/assembly-revisions/{rv}/lines", json={"design_id": stock["design"], "quantity": 2, "quantity_basis": "ME spec"}, headers=eng)
    api.post(f"/assembly-revisions/{rv}/approve", headers=eng)
    line = next(l for l in api.get(f"/assemblies/{kit['asm']}", headers=eng).json()["revisions"][-1]["lines"] if l["kind"] == "CONSUMABLE")["id"]
    m = api.post("/mappings", json={"part_id": "PN-1", "routing_revision": "R1", "operation_id": "OP10",
                                    "definition_id": kit["asm"], "selected": True}, headers=eng).json()
    b = api.post("/builds", json={"definition_id": kit["asm"], "location_id": world["bins"][0]}, headers=st).json()
    for k in KEYS:
        api.post(f"/builds/{b['id']}/fit", json={"item_id": kit["items"][k]}, headers=st)
    api.post(f"/builds/{b['id']}/verify", headers=eng)
    batch = api.post("/batches", json={"design_id": stock["design"], "quantity": 10, "location_id": world["bins"][1]}, headers=st).json()
    pre = api.get(f"/builds/{b['id']}/issue-preview", headers=st).json()
    assert pre["consumables"][0]["quantity"] == 2 and pre["consumables"][0]["basis"] == "ME spec"
    assert pre["consumables"][0]["batches"][0]["id"] == batch["id"]
    ids = [kit["items"][k] for k in KEYS]
    base = {**setup_body(api, login, "WO-5", m["id"]), "mapping_id": m["id"], "build_id": b["id"], "confirmed_item_ids": ids}
    r = api.post("/issues", json=base, headers=st)
    assert r.status_code == 422 and "Consumables are required" in r.json()["detail"]
    pick = [{"line_id": line, "batch_id": batch["id"], "quantity": 2}]
    r = api.post("/issues", json={**base, "consumables": pick}, headers=st)
    assert r.status_code == 201 and r.json()["consumables"][0]["state"] == "ISSUED"
    iid = r.json()["id"]
    assert api.get(f"/batches/{batch['id']}", headers=st).json()["usable"] == 8
    api.post(f"/issues/{iid}/acknowledge", headers=pr)
    ret = api.post(f"/issues/{iid}/return", json={"consumables": [{"batch_id": batch["id"], "unused_quantity": 0.5}]}, headers=st)
    assert ret.status_code == 200 and ret.json()["consumables_settled"] == 1
    bb = api.get(f"/batches/{batch['id']}", headers=st).json()
    assert (bb["usable"], bb["consumed"], bb["outstanding"]) == (8.5, 1.5, 0)
    again = api.post(f"/issues/{iid}/return", json={"consumables": [{"batch_id": batch["id"], "unused_quantity": 0.5}]}, headers=st)
    assert again.status_code == 422 and "not outstanding" in again.json()["detail"]
    assert api.get("/dashboard", headers=st).json()["outstanding_returns"] == 1  # the items are still out
