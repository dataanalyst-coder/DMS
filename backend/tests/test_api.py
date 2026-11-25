from io import BytesIO

import pandas as pd
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.deps import begin_write, get_db
from app.main import app
from tests.conftest import PW

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
PDF = b"%PDF-1.4\n"


@pytest.fixture
def api(engine, people, world):
    Session = sessionmaker(bind=engine, autoflush=False, future=True)

    def _db(request: Request = None):
        s = Session()
        try:
            begin_write(s, request)
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def login(api):
    cache = {}

    def _login(user):
        if user not in cache:
            r = api.post("/auth/login", json={"username": user, "password": PW})
            assert r.status_code == 200, r.text
            cache[user] = {"Authorization": f"Bearer {r.json()['token']}"}
        return cache[user]
    return _login


def register(api, login, world, legacy="b32-01", **kw):
    body = {"design_id": world["die"], "legacy_no": legacy, "condition": "SERVICEABLE",
            "location_id": world["bins"][0], **kw}
    r = api.post("/items", json=body, headers=login("stores"))
    assert r.status_code == 201, r.text
    return r.json()


def test_every_route_except_login_requires_a_token(api):
    paths = [("get", "/auth/me"), ("get", "/users"), ("get", "/items"), ("get", "/items/1"),
             ("post", "/items"), ("get", "/designs"), ("get", "/locations"), ("get", "/categories"),
             ("get", "/documents"), ("get", "/ledger"), ("get", "/dashboard"), ("get", "/lookup?code=a"),
             ("get", "/photos/1"), ("get", "/document-versions/1/file"), ("get", "/export/ledger"),
             ("get", "/export/items"), ("get", "/templates/items"), ("get", "/audit"),
             ("post", "/items/move"), ("post", "/items/condition"), ("post", "/import/items"),
             ("post", "/ledger/1/reverse")]
    for method, path in paths:
        r = getattr(api, method)(path)
        assert r.status_code == 401, (method, path, r.status_code)
    assert api.get("/items", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert api.get("/openapi.json").status_code == 200  # docs only describe, they expose no data


def test_login_flow_and_bad_credentials(api, login):
    assert api.post("/auth/login", json={"username": "stores", "password": "nope-nope-nope"}).status_code == 401
    me = api.get("/auth/me", headers=login("multi")).json()
    assert me["roles"] == ["STORES", "TOOLING"] and "password" not in str(me)
    assert "item.register" in me["permissions"] and "condition.release" not in me["permissions"]
    admin = api.get("/auth/me", headers=login("admin")).json()["permissions"]
    assert "config.manage" in admin and "design.approve" in admin  # admin has full access


def test_roles_enforced_server_side(api, login, world):
    body = {"design_id": world["die"], "legacy_no": "x1", "condition": "SERVICEABLE",
            "location_id": world["bins"][0]}
    assert api.post("/items", json=body, headers=login("prod")).status_code == 403
    assert api.post("/items", json=body, headers=login("stores")).status_code == 201
    r = api.post("/categories", json={"code": "NEW", "name": "New"}, headers=login("eng"))
    assert r.status_code == 403 and "ADMIN" in r.json()["detail"]
    assert api.get("/audit", headers=login("stores")).status_code == 403
    assert api.get("/audit", headers=login("mgr")).status_code == 200
    assert api.get("/users", headers=login("eng")).status_code == 403


def test_design_approval_over_http_admin_cannot_bypass(api, login, world):
    d = api.post("/designs", json={"code": "d-77", "description": "New die", "category_id": world["cat"],
                                   "base_uom": "EA"}, headers=login("eng")).json()
    rev = d["revisions"][0]["id"]
    r = api.post(f"/design-revisions/{rev}/approve", headers=login("eng"))
    assert r.status_code == 200 and r.json()["revisions"][0]["status"] == "APPROVED"


def test_item_lifecycle_over_http(api, login, world):
    it = register(api, login, world)
    assert it["system_id"].startswith("TI-") and it["condition"] == "SERVICEABLE"
    assert "password" not in str(it)
    h = login("stores")
    r = api.post("/items/move", json={"item_ids": [it["id"]], "to_location_id": world["bins"][1]}, headers=h)
    assert r.status_code == 200
    r = api.post("/items/condition", json={"item_ids": [it["id"]], "to_condition": "HOLD", "reason": "crack"},
                 headers=h)
    assert r.status_code == 200
    d = api.get(f"/items/{it['id']}", headers=h).json()
    assert d["condition"] == "HOLD" and d["location"].endswith("B2") and len(d["history"]) == 3
    # a hold never jumps to serviceable: further inspection first, and release needs a passing inspection
    body = {"item_ids": [it["id"]], "to_condition": "SERVICEABLE", "reason": "re-inspected"}
    assert api.post("/items/condition", json=body, headers=login("eng")).status_code == 422
    r = api.post("/items/condition", json={**body, "to_condition": "AWAITING_INSPECTION", "reason": "further inspection"},
                 headers=login("tooling"))
    assert r.status_code == 200
    assert api.post("/items/condition", json=body, headers=h).status_code == 403  # stores cannot release
    r = api.post("/items/condition", json=body, headers=login("eng"))
    assert r.status_code == 422 and "no passing inspection" in r.json()["detail"]
    # reverse the last event (the move to further inspection) -> HOLD again
    last = api.get(f"/items/{it['id']}", headers=h).json()["history"][0]
    r = api.post(f"/ledger/{last['txn_id']}/reverse", json={"reason": "raised in error"},
                 headers=login("tooling"))
    assert r.status_code == 201 and r.json()["reversed_txn_ids"] == [last["txn_id"]]
    assert api.get(f"/items/{it['id']}", headers=h).json()["condition"] == "HOLD"
    assert api.post(f"/ledger/{last['txn_id']}/reverse", json={"reason": "again"},
                    headers=login("tooling")).status_code == 422


def test_status_and_identity_cannot_be_set_through_update(api, login, world):
    it = register(api, login, world)
    r = api.put(f"/items/{it['id']}", json={"remarks": "scratch on side", "condition": "SCRAPPED",
                                            "display_id": "HACK"}, headers=login("stores"))
    assert r.status_code == 200
    d = r.json()
    assert d["remarks"] == "scratch on side" and d["condition"] == "SERVICEABLE" and d["display_id"] == "B32-1"


def test_search_pagination_and_scan_lookup(api, login, world):
    for i in range(1, 8):
        register(api, login, world, legacy=f"b32-0{i}")
    h = login("mgr")
    r = api.get("/items", params={"q": "b32", "limit": 3, "offset": 6}, headers=h).json()
    assert r["total"] == 7 and len(r["rows"]) == 1
    one = api.get("/lookup", params={"code": "b32-04"}, headers=h).json()
    assert len(one) == 1 and one[0]["display_id"] == "B32-4"
    assert api.get("/lookup", params={"code": one[0]["system_id"]}, headers=h).json()[0]["id"] == one[0]["id"]


def test_photo_upload_and_download_require_auth_and_validate(api, login, world):
    it = register(api, login, world)
    h = login("stores")
    r = api.post(f"/items/{it['id']}/photos", data={"kind": "ITEM"}, files={"file": ("p.png", PNG)}, headers=h)
    assert r.status_code == 201
    pid = r.json()["id"]
    assert api.get(f"/photos/{pid}").status_code == 401  # no anonymous file access
    got = api.get(f"/photos/{pid}", headers=login("mgr"))
    assert got.status_code == 200 and got.content == PNG and got.headers["x-content-type-options"] == "nosniff"
    bad = api.post(f"/items/{it['id']}/photos", data={"kind": "ITEM"}, files={"file": ("p.exe", b"MZ")}, headers=h)
    assert bad.status_code == 422
    assert api.post(f"/items/{it['id']}/photos", data={"kind": "ITEM"}, files={"file": ("p.png", PNG)},
                    headers=login("prod")).status_code == 403


def test_document_upload_approve_and_protected_download(api, login, world):
    e = login("eng")
    d = api.post("/documents", json={"doc_key": "dwg-1", "doc_type": "DIE_DRAWING", "title": "Die 32"}, headers=e).json()
    r = api.post(f"/documents/{d['id']}/versions", data={}, files={"file": ("a.pdf", PDF)}, headers=e)
    assert r.status_code == 201
    vid = r.json()["versions"][0]["id"]
    assert api.get(f"/document-versions/{vid}/file").status_code == 401
    assert api.post(f"/document-versions/{vid}/approve", headers=e).status_code == 200
    f = api.get(f"/document-versions/{vid}/file", headers=login("stores"))
    assert f.status_code == 200 and f.content == PDF
    assert "attachment" in f.headers["content-disposition"]  # never rendered inline by the browser
    assert api.post(f"/documents/{d['id']}/versions", data={}, files={"file": ("a.html", b"<b>")},
                    headers=e).status_code == 422


def test_import_over_http_with_review_queue(api, login, world):
    register(api, login, world, legacy="b32-1")
    buf = BytesIO()
    pd.DataFrame([{"legacy_no": "B32-1", "design_code": "die-32", "condition": "SERVICEABLE",
                   "home_location": "P1/A1/R1/S1/B2"},
                  {"legacy_no": "n-9", "design_code": "die-32", "condition": "HOLD",
                   "home_location": "P1/A1/R1/S1/B2"}]).to_excel(buf, index=False)
    h = login("stores")
    files = {"file": ("items.xlsx", buf.getvalue())}
    pre = api.post("/import/items", files=files, headers=h).json()
    assert pre["valid"] and not pre["committed"] and pre["counts"]["DUPLICATE"] == 1
    done = api.post("/import/items", params={"commit": True}, files=files, headers=h).json()
    assert done["created"] == 1 and done["queued_for_review"] == 1
    (rv,) = api.get("/import/review", headers=h).json()
    r = api.post(f"/import/review/{rv['id']}/resolve", json={"display_id": "B32-1-B"}, headers=h)
    assert r.status_code == 200 and r.json()["display_id"] == "B32-1-B"
    assert api.get("/dashboard", headers=h).json()["pending_import_review"] == 0


def test_exports_and_template_download(api, login, world):
    register(api, login, world)
    h = login("mgr")
    t = api.get("/templates/items", headers=h)
    assert t.status_code == 200 and "data" in pd.ExcelFile(BytesIO(t.content)).sheet_names
    led = pd.read_excel(BytesIO(api.get("/export/ledger", headers=h).content))
    assert list(led["action"]) == ["RECEIVE"] and led["user"][0] == "stores"
    its = pd.read_excel(BytesIO(api.get("/export/items", headers=h).content))
    assert its["display_id"][0] == "B32-1"


def test_user_admin_endpoints(api, login):
    a = login("admin")
    r = api.post("/users", json={"username": "newbie", "full_name": "New", "password": "a-long-password-1",
                                 "roles": ["STORES"]}, headers=a)
    assert r.status_code == 201 and "password" not in r.text
    assert api.post("/users", json={"username": "weak", "full_name": "W", "password": "short", "roles": []},
                    headers=a).status_code == 422
    users = api.get("/users", headers=a).json()
    assert all("password" not in str(u) and "hash" not in str(u) for u in users)
    tok = api.post("/auth/login", json={"username": "newbie", "password": "a-long-password-1"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    uid = r.json()["id"]
    api.put(f"/users/{uid}/active", json={"active": False}, headers=a)
    assert api.get("/auth/me", headers=h).status_code == 401  # deactivated users lose access at once
