from datetime import datetime, timedelta
from io import BytesIO

import pandas as pd
import pytest

from app.models import BuildComponent, Issue
from app.services import auth
from tests.conftest import PW, mk_setup, setup_body
from tests.test_api import api, login  # noqa: F401  (fixtures)

KEYS = ("d1", "p1", "t1", "b1")


def test_new_routes_require_a_token(api):
    for method, path in [("post", "/integration/usage"), ("get", "/integration/usage"), ("get", "/integration/exceptions"),
                         ("get", "/integration/usage/1"), ("post", "/integration/usage/1/retry"),
                         ("post", "/integration/usage/1/resolve"), ("get", "/oms/associations?wo_id=W"),
                         ("get", "/usage/assembly"), ("get", "/usage/items/1"), ("get", "/usage/output"),
                         ("post", "/issues/1/replace-component"), ("get", "/counts"), ("post", "/counts"),
                         ("get", "/counts/1"), ("post", "/counts/1/scan"), ("post", "/counts/1/complete"),
                         ("post", "/counts/1/cancel"), ("post", "/counts/1/apply"), ("get", "/reports"),
                         ("get", "/reports/inventory"), ("get", "/reports/inventory?format=xlsx")]:
        assert getattr(api, method)(path).status_code == 401, (method, path)


@pytest.fixture
def issued(api, login, db, people, kit, world):
    auth.create_user(db, people["admin"], "oms", "OMS feed", PW, ["INTEGRATION"])
    m = api.post("/mappings", json={"part_id": "PN-1", "routing_revision": "R1", "operation_id": "OP10", "definition_id": kit["asm"],
                                    "selected": True, "pouring_weight_kg": 25, "pouring_weight_revision": "W1"}, headers=login("eng")).json()
    b = api.post("/builds", json={"definition_id": kit["asm"], "location_id": world["bins"][0]}, headers=login("stores")).json()
    for k in KEYS:
        api.post(f"/builds/{b['id']}/fit", json={"item_id": kit["items"][k]}, headers=login("stores"))
    api.post(f"/builds/{b['id']}/verify", headers=login("eng"))
    ids = [kit["items"][k] for k in KEYS]
    i = api.post("/issues", json={**setup_body(api, login, "WO-1", m["id"]), "mapping_id": m["id"], "build_id": b["id"], "confirmed_item_ids": ids},
                 headers=login("stores")).json()
    api.post(f"/issues/{i['id']}/acknowledge", headers=login("prod"))
    db.query(Issue).update({"issued_at": datetime.utcnow() - timedelta(days=1)})
    db.query(BuildComponent).update({"fitted_at": datetime.utcnow() - timedelta(days=1, hours=1)})
    db.commit()
    return {**kit, "issue": i["id"], "build": b["id"], "ids": ids, "mapping": m["id"]}


def event(**kw):
    t = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    base = {"source_event_id": "EV-1", "version": 1, "wo_id": "WO-1", "operation_id": "OP10", "build": "BLD-000001",
            "production_end": t, "cycles": 10, "castings": 20}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def test_usage_feed_over_http_is_idempotent_and_versioned(api, login, issued):
    oms = login("oms")
    e1 = event()  # the exact message OMS sends (and may resend)
    r = api.post("/integration/usage", json=e1, headers=oms)
    assert r.status_code == 201 and r.json()["result"] == "POSTED" and r.json()["event"]["tonnage"] == 0.5
    again = api.post("/integration/usage", json=e1, headers=oms)
    assert again.status_code == 200 and again.json()["result"] == "DUPLICATE"
    fix = api.post("/integration/usage", json={**e1, "version": 2, "castings": 18}, headers=oms)
    assert fix.status_code == 201 and fix.json()["event"]["tonnage"] == 0.45
    assert api.post("/integration/usage", json=e1, headers=oms).json()["result"] == "DUPLICATE"  # version 1 again: already seen
    api.post("/integration/usage", json={**e1, "source_event_id": "EV-9", "version": 3}, headers=oms)
    stale = api.post("/integration/usage", json={**e1, "source_event_id": "EV-9", "version": 2}, headers=oms)  # arrives late
    assert stale.status_code == 200 and stale.json()["result"] == "STALE"
    assert api.post("/integration/usage", json={**e1, "version": 2, "castings": 99}, headers=oms).status_code == 409  # same id+version, new content
    assert api.get("/usage/assembly", params={"build_id": issued["build"]}, headers=login("mgr")).json()["tonnage"] == 0.95  # v2 of EV-1 + EV-9 v3
    assert api.get("/usage/output", headers=login("mgr")).json()["tonnage"] == 0.95
    u = api.get(f"/usage/items/{issued['ids'][0]}", headers=login("mgr")).json()
    assert u["recorded_cycles"] == 20 and u["opening_cycles"] is None and u["total_cycles"] is None
    for who in ("stores", "prod", "mgr"):
        assert api.post("/integration/usage", json=event(source_event_id="X"), headers=login(who)).status_code == 403


def test_integration_exceptions_retry_and_reconcile_over_http(api, login, issued):
    oms = login("oms")
    r = api.post("/integration/usage", json=event(build=None), headers=oms)
    assert r.status_code == 201 and r.json()["result"] == "PENDING"
    ex = api.get("/integration/exceptions", headers=login("prod")).json()
    assert ex["pending"] == 1 and ex["events"][0]["reason_code"] == "MISSING_BUILD" and ex["last_successful_sync"] is None
    eid = ex["events"][0]["id"]
    assert api.post(f"/integration/usage/{eid}/resolve", json={"build": "BLD-000001", "reason": "supervisor confirmed"}, headers=login("stores")).status_code == 403
    assert api.post(f"/integration/usage/{eid}/resolve", json={"reason": "nothing entered"}, headers=login("prod")).status_code == 422
    done = api.post(f"/integration/usage/{eid}/resolve", json={"build": "BLD-000001", "reason": "supervisor confirmed the build"}, headers=login("prod"))
    assert done.status_code == 200 and done.json()["status"] == "POSTED"
    ex = api.get("/integration/exceptions", headers=login("prod")).json()
    assert ex["pending"] == 0 and ex["last_successful_sync"]
    assert api.post(f"/integration/usage/{eid}/retry", headers=login("prod")).status_code == 422  # already posted


def test_failed_integration_calls_show_the_error_and_leave_a_record(api, login, issued):
    """Spec A15 (integration half)."""
    oms = login("oms")
    r = api.post("/integration/usage", json=event(castings=-4), headers=oms)
    assert r.status_code == 422 and "whole number" in r.json()["detail"]
    assert api.post("/integration/usage", json={"version": 1}, headers=oms).status_code == 422
    ex = api.get("/integration/exceptions", headers=login("eng")).json()
    assert ex["rejected"] == 1 and ex["events"][0]["reason_code"] == "INVALID_NUMBER"
    audit = api.get("/audit", params={"limit": 50}, headers=login("mgr")).json()["rows"]
    assert {"USAGE_REJECTED", "REJECT_USAGE"} <= {a["action"] for a in audit}


def test_unauthorised_attempts_are_refused_and_recorded(api, login, make_item, issued):
    """Spec A15 (release half): the error is shown, nothing is approved unchecked, and the attempt is on record."""
    it = make_item(legacy="held1", bin_index=1)
    api.post("/items/condition", json={"item_ids": [it.id], "to_condition": "HOLD", "reason": "crack"}, headers=login("tooling"))
    api.post("/items/condition", json={"item_ids": [it.id], "to_condition": "AWAITING_INSPECTION", "reason": "further"}, headers=login("tooling"))
    attempt = {"item_ids": [it.id], "to_condition": "SERVICEABLE", "reason": "just release it"}
    for who in ("prod", "stores"):
        r = api.post("/items/condition", json=attempt, headers=login(who))
        assert r.status_code == 403 and "ENGINEERING" in r.json()["detail"]
    assert api.get(f"/items/{it.id}", headers=login("mgr")).json()["condition"] == "AWAITING_INSPECTION"  # not released
    rows = api.get("/audit", params={"action": "DENIED"}, headers=login("mgr")).json()["rows"]
    assert sorted(r["username"] for r in rows) == ["prod", "stores"]
    assert all(r["entity_type"] == "request" and "POST /items/condition" in r["entity_id"] and "ENGINEERING" in r["reason"] for r in rows)


def test_oms_association_over_http(api, login, issued):
    (a,) = api.get("/oms/associations", params={"wo_id": "WO-1"}, headers=login("prod")).json()
    assert a["build"] == "BLD-000001" and len(a["constituents"]) == 4 and a["operation_id"] == "OP10"


def test_replace_component_over_http(api, login, make_item, issued, world):
    spare = api.post("/items", json={"design_id": issued["tf"], "legacy_no": "t2", "condition": "SERVICEABLE",
                                     "location_id": world["bins"][2]}, headers=login("stores")).json()
    body = {"outgoing_item_id": issued["items"]["t1"], "incoming_item_id": spare["id"], "reason": "worn through"}
    assert api.post(f"/issues/{issued['issue']}/replace-component", json=body, headers=login("prod")).status_code == 403
    r = api.post(f"/issues/{issued['issue']}/replace-component", json=body, headers=login("stores"))
    assert r.status_code == 200 and r.json()["incoming"] == "T2" and r.json()["build_status"] == "ASSEMBLING"
    states = {l["display_id"]: l["state"] for l in r.json()["issue"]["lines"]}
    assert states["T1"] == "IN_PRODUCTION" and states["T2"] == "PENDING_HANDOVER"


def test_stock_count_over_http(api, login, make_item, world):
    a, b = make_item(legacy="a1"), make_item(legacy="b1")
    st, eng = login("stores"), login("eng")
    c = api.post("/counts", json={"location_id": world["bins"][0]}, headers=st).json()
    assert api.post("/counts", json={"location_id": world["bins"][0]}, headers=login("prod")).status_code == 403
    r = api.post(f"/counts/{c['id']}/scan", json={"code": "a1"}, headers=st)
    assert r.status_code == 200 and r.json()["report"]["summary"]["found"] == 1
    assert api.post(f"/counts/{c['id']}/scan", json={"code": "nothing"}, headers=st).status_code == 422
    done = api.post(f"/counts/{c['id']}/complete", json={}, headers=st).json()
    assert [m["display_id"] for m in done["missing"]] == ["B1"] and done["summary"]["missing"] == 1
    act = {"item_actions": [{"item_id": b.id, "action": "MARK_MISSING", "reason": "not on the rack"}]}
    assert api.post(f"/counts/{c['id']}/apply", json=act, headers=st).status_code == 403  # counting is not adjusting
    assert api.post(f"/counts/{c['id']}/apply", json=act, headers=eng).status_code == 200
    assert api.get(f"/items/{b.id}", headers=st).json()["custody"] == "MISSING"
    assert api.get("/dashboard", headers=st).json()["custody"]["MISSING"] == 1
    assert api.get("/counts", headers=st).json()[0]["status"] == "COMPLETED"


def test_reports_over_http_json_and_excel(api, login, issued):
    h = login("mgr")
    assert "custody-by-wo" in api.get("/reports", headers=h).json()
    inv = api.get("/reports/inventory", headers=h).json()
    assert inv["summary"]["items"] == 7 and inv["columns"][0] == "display_id"
    wo = api.get("/reports/custody-by-wo", headers=h).json()
    assert wo["rows"][0]["wo_id"] == "WO-1"
    x = api.get("/reports/custody-by-wo", params={"format": "xlsx"}, headers=h)
    assert x.status_code == 200 and "custody-by-wo.xlsx" in x.headers["content-disposition"]
    assert pd.read_excel(BytesIO(x.content))["wo_id"][0] == "WO-1"
    filtered = api.get("/reports/inventory", params={"condition": "SERVICEABLE", "location_id": ""}, headers=h).json()
    assert filtered["summary"]["items"] == 7
    assert api.get("/reports/nonsense", headers=h).status_code == 404
    assert api.get("/reports/inventory", params={"format": "pdf"}, headers=h).status_code == 404
    assert api.get("/reports/missing-overdue", params={"days": 0}, headers=h).json()["summary"]["overdue"] == 4
