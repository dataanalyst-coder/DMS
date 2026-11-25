import pytest

from tests.test_api import api, login  # noqa: F401  (fixtures)


def test_new_routes_require_a_token(api):
    for method, path in [("get", "/assemblies"), ("get", "/assemblies/1"), ("post", "/assemblies"),
                         ("post", "/assembly-revisions/1/approve"), ("post", "/assembly-revisions/1/lines"),
                         ("put", "/assembly-lines/1"), ("delete", "/assembly-lines/1"),
                         ("delete", "/assembly-alternatives/1"), ("get", "/mappings"),
                         ("get", "/mappings/options?part_id=a&routing_revision=b&operation_id=c"),
                         ("post", "/mappings"), ("get", "/builds"), ("get", "/builds/1"), ("post", "/builds"),
                         ("post", "/builds/1/fit"), ("post", "/builds/1/remove"), ("post", "/builds/1/replace"),
                         ("post", "/builds/1/verify"), ("post", "/builds/1/move"), ("post", "/builds/1/dismantle")]:
        assert getattr(api, method)(path).status_code == 401, (method, path)


def test_definition_lifecycle_and_roles_over_http(api, login, kit):
    eng, admin = login("eng"), login("admin")
    body = {"code": "t999", "description": "New assembly", "category_id": kit["asm_cat"]}
    d = api.post("/assemblies", json=body, headers=eng).json()
    rev = d["revisions"][0]["id"]
    assert api.post(f"/assembly-revisions/{rev}/approve", headers=eng).status_code == 422  # no lines yet
    r = api.post(f"/assembly-revisions/{rev}/lines", json={"design_id": kit["die"], "quantity": 2}, headers=eng)
    assert r.status_code == 201
    line = r.json()["revisions"][0]["lines"][0]
    r = api.post(f"/assembly-lines/{line['id']}/alternatives", json={"design_id": kit["pat"]}, headers=eng)
    assert r.json()["revisions"][0]["lines"][0]["alternatives"][0]["design_code"] == "PAT-1"
    assert api.put(f"/assembly-lines/{line['id']}", json={"quantity": 3}, headers=eng).json()["revisions"][0]["lines"][0]["quantity"] == 3
    ok = api.post(f"/assembly-revisions/{rev}/approve", headers=eng)
    assert ok.status_code == 200 and ok.json()["revisions"][0]["status"] == "APPROVED"
    assert api.put(f"/assembly-lines/{line['id']}", json={"quantity": 9}, headers=eng).status_code == 422  # immutable
    assert api.delete(f"/assembly-lines/{line['id']}", headers=eng).status_code == 422
    listing = api.get("/assemblies", params={"q": "t999"}, headers=login("mgr")).json()
    assert listing["total"] == 1 and listing["rows"][0]["approved_revision"] == "A"


def test_mapping_options_over_http(api, login, kit):
    eng = login("eng")
    body = {"part_id": "pn-1", "routing_revision": "r1", "operation_id": "op10", "definition_id": kit["asm"],
            "pouring_weight_kg": 25, "pouring_weight_revision": "W1"}
    assert api.post("/mappings", json=body, headers=login("stores")).status_code == 403
    m = api.post("/mappings", json=body, headers=eng)
    assert m.status_code == 201 and m.json()["selectable"] and m.json()["pouring_weight_kg"] == 25
    opts = api.get("/mappings/options", params={"part_id": "PN-1", "routing_revision": "R1", "operation_id": "OP10"},
                   headers=login("prod")).json()
    assert opts[0]["assembly_code"] == "T211" and opts[0]["effective_revision"] == "A"
    r = api.put(f"/mappings/{m.json()['id']}", json={"tooling_status": "TO_BE_DEVELOPED"}, headers=eng)
    assert r.json()["selectable"] is False
    assert api.put(f"/mappings/{m.json()['id']}", json={"active": False}, headers=eng).status_code == 422  # needs a reason
    assert api.put(f"/mappings/{m.json()['id']}", json={"active": False, "reason": "withdrawn"}, headers=eng).status_code == 200


def test_build_flow_over_http(api, login, kit, world):
    st = login("stores")
    b = api.post("/builds", json={"definition_id": kit["asm"], "location_id": world["bins"][0]}, headers=st).json()
    assert api.post("/builds", json={"definition_id": kit["asm"], "location_id": world["bins"][0]},
                    headers=login("prod")).status_code == 403
    for key in ("d1", "p1", "t1"):
        r = api.post(f"/builds/{b['id']}/fit", json={"item_id": kit["items"][key]}, headers=st)
        assert r.status_code == 200
    r = api.post(f"/builds/{b['id']}/fit", json={"item_id": kit["items"]["d2"]}, headers=st)  # no second die position
    assert r.status_code == 422 and "no free place" in r.json()["detail"]
    r = api.post(f"/builds/{b['id']}/verify", headers=login("eng"))
    assert r.status_code == 422 and "missing 1 x BF-A" in r.json()["detail"]
    api.post(f"/builds/{b['id']}/fit", json={"item_id": kit["items"]["b1"]}, headers=st)
    d = api.post(f"/builds/{b['id']}/verify", headers=login("eng")).json()
    assert d["status"] == "VERIFIED" and d["ready_to_issue"] and d["verified_by"] == "eng"
    # item page shows which build holds it; its custody is the assembly area
    it = api.get(f"/items/{kit['items']['d1']}", headers=st).json()
    assert it["build"]["code"] == d["code"] and it["custody"] == "ASSEMBLY_AREA"
    # moving a fitted part alone is refused, moving the build is fine
    r = api.post("/items/move", json={"item_ids": [kit["items"]["d1"]], "to_location_id": world["bins"][1]}, headers=st)
    assert r.status_code == 422 and "move the whole build" in r.json()["detail"]
    assert api.post(f"/builds/{b['id']}/move", json={"to_location_id": world["bins"][1]}, headers=st).status_code == 200
    # dismantle: every component needs a destination
    dest = [{"item_id": kit["items"][k], "location_id": world["bins"][2]} for k in ("d1", "p1", "t1")]
    r = api.post(f"/builds/{b['id']}/dismantle", json={"destinations": dest, "reason": "stand-down"}, headers=st)
    assert r.status_code == 422 and "missing" in r.json()["detail"]
    dest.append({"item_id": kit["items"]["b1"], "location_id": world["bins"][2]})
    r = api.post(f"/builds/{b['id']}/dismantle", json={"destinations": dest, "reason": "stand-down"}, headers=st)
    assert r.status_code == 200 and r.json()["status"] == "DISMANTLED" and len(r.json()["history"]) == 4
    assert api.get("/builds", params={"status": "DISMANTLED"}, headers=st).json()["total"] == 1
    led = api.get("/ledger", params={"item_id": kit["items"]["d1"]}, headers=st).json()["rows"]
    assert [r["action"] for r in led][:3] == ["REMOVE", "MOVE", "FIT"] and led[0]["build_id"] == b["id"]
