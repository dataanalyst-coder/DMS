from xml.dom import minidom

import pytest
from tests.conftest import mk_setup, setup_body

from tests.test_api import api, login  # noqa: F401  (fixtures)

KEYS = ("d1", "p1", "t1", "b1")


@pytest.fixture
def rig(api, login, kit, world):
    m = api.post("/mappings", json={"part_id": "PN-1", "routing_revision": "R1", "operation_id": "OP10",
                                    "definition_id": kit["asm"], "selected": True}, headers=login("eng")).json()
    b = api.post("/builds", json={"definition_id": kit["asm"], "location_id": world["bins"][0]}, headers=login("stores")).json()
    for k in KEYS:
        assert api.post(f"/builds/{b['id']}/fit", json={"item_id": kit["items"][k]}, headers=login("stores")).status_code == 200
    assert api.post(f"/builds/{b['id']}/verify", headers=login("eng")).status_code == 200
    return {**kit, "mapping": m["id"], "build": b["id"], "ids": [kit["items"][k] for k in KEYS]}


def test_new_routes_require_a_token(api):
    for method, path in [("get", "/scan?code=a"), ("post", "/readiness/checks"), ("get", "/readiness/checks"),
                         ("get", "/readiness/checks/1"), ("get", "/readiness/recheck"),
                         ("post", "/readiness/wo/W/close"), ("get", "/builds/1/issue-preview"),
                         ("post", "/issues"), ("get", "/issues"), ("get", "/issues/1"),
                         ("post", "/issues/1/acknowledge"), ("post", "/issues/1/return"),
                         ("get", "/items/1/label.svg"), ("get", "/builds/1/label.svg"), ("get", "/locations/1/label.svg")]:
        assert getattr(api, method)(path).status_code == 401, (method, path)


def test_readiness_over_http_and_roles(api, login, rig):
    body = {"wo_id": "WO-1", "part_id": "pn-1", "routing_revision": "r1", "operation_id": "op10"}
    r = api.post("/readiness/checks", json=body, headers=login("prod"))
    assert r.status_code == 201 and r.json()["result"] == "READY" and r.json()["can_release"]
    assert api.post("/readiness/checks", json={**body, "wo_id": "WO-2", "operation_id": "OP99"}, headers=login("prod")).json()["result"] == "UNABLE_TO_VERIFY"
    hist = api.get("/readiness/checks", params={"wo_id": "WO-1"}, headers=login("mgr")).json()
    assert hist["total"] == 1 and hist["rows"][0]["candidate_item_ids"]
    # holding a part the check counted on flags the WO for recheck (and cancels nothing)
    cand = hist["rows"][0]["candidate_item_ids"][0]
    assert api.post("/items/condition", json={"item_ids": [cand], "to_condition": "HOLD", "reason": "crack"},
                    headers=login("tooling")).status_code == 200
    flagged = api.get("/readiness/recheck", headers=login("prod")).json()
    assert [f["wo_id"] for f in flagged] == ["WO-1"] and flagged[0]["result"] == "READY"
    assert api.get("/dashboard", headers=login("mgr")).json()["wos_needing_recheck"] == 1
    assert api.post("/readiness/wo/WO-1/close", headers=login("prod")).json()["closed_checks"] == 1
    assert api.get("/readiness/recheck", headers=login("prod")).json() == []


def test_issue_acknowledge_return_over_http(api, login, rig, world):
    st, pr = login("stores"), login("prod")
    p = api.get(f"/builds/{rig['build']}/issue-preview", headers=st).json()
    assert p["can_issue"] and len(p["components"]) == 4
    body = {**setup_body(api, login, "WO-1", rig["mapping"]), "mapping_id": rig["mapping"], "build_id": rig["build"], "confirmed_item_ids": rig["ids"][:3]}
    r = api.post("/issues", json=body, headers=st)
    assert r.status_code == 422 and "not confirmed" in r.json()["detail"]  # nothing concealed
    assert api.post("/issues", json={**body, "confirmed_item_ids": rig["ids"]}, headers=pr).status_code == 403
    r = api.post("/issues", json={**body, "confirmed_item_ids": rig["ids"]}, headers=st)
    assert r.status_code == 201 and r.json()["status"] == "PENDING_HANDOVER"
    iid = r.json()["id"]
    again = api.post("/issues", json={**body, **setup_body(api, login, "WO-2", rig["mapping"]), "confirmed_item_ids": rig["ids"]}, headers=st)
    assert again.status_code == 422 and "WO WO-1" in again.json()["detail"]
    assert api.post(f"/issues/{iid}/acknowledge", headers=st).status_code == 403
    ack = api.post(f"/issues/{iid}/acknowledge", headers=pr).json()
    assert ack["status"] == "ACTIVE" and ack["received_by"] == ["prod"]
    dash = api.get("/dashboard", headers=st).json()
    assert dash["outstanding_returns"] == 1
    lines = [{"item_id": i, "to_location_id": world["bins"][1]} for i in rig["ids"][:3]]
    ret = api.post(f"/issues/{iid}/return", json={"lines": lines}, headers=st).json()
    assert ret["returned"] == 3 and ret["outstanding"] == ["B1"] and ret["issue"]["status"] == "PARTIALLY_RETURNED"
    assert api.get("/issues", params={"status": "open"}, headers=st).json()["total"] == 1
    item = api.get(f"/items/{rig['ids'][3]}", headers=st).json()
    assert item["issue"]["wo_id"] == "WO-1" and item["custody"] == "PRODUCTION"
    done = api.post(f"/issues/{iid}/return", json={"lines": [{"item_id": rig["ids"][3], "to_location_id": world["bins"][1]}]},
                    headers=st).json()
    assert done["issue"]["status"] == "CLOSED" and api.get("/dashboard", headers=st).json()["outstanding_returns"] == 0


def test_labels_and_scan_over_http(api, login, rig):
    st = login("stores")
    item_id = rig["items"]["d1"]
    svg = api.get(f"/items/{item_id}/label.svg", headers=st)
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml")
    minidom.parseString(svg.text)
    assert api.get(f"/items/{item_id}/label.svg", headers=st).text == svg.text  # reprint: same identity
    assert api.get(f"/builds/{rig['build']}/label.svg", headers=st).status_code == 200
    assert api.get("/locations/1/label.svg", headers=st).status_code == 200
    assert api.get("/items/999999/label.svg", headers=st).status_code == 404
    # reprint=true is a stores action and is audited; plain viewing is not
    assert api.get(f"/items/{item_id}/label.svg", params={"reprint": True}, headers=login("prod")).status_code == 403
    assert api.get(f"/items/{item_id}/label.svg", params={"reprint": True}, headers=st).status_code == 200
    audit = api.get("/audit", params={"action": "PRINT_LABEL"}, headers=login("mgr")).json()
    assert audit["total"] == 1 and audit["rows"][0]["username"] == "stores"
    r = api.get("/scan", params={"code": "BLD-000001"}, headers=st).json()
    assert r["type"] == "BUILD"
    r = api.get("/scan", params={"code": "d1"}, headers=st).json()
    assert r["type"] == "ITEM" and r["items"][0]["build"]["code"] == "BLD-000001"
