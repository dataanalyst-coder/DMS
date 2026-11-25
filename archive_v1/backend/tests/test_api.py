import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.database import init_db, make_engine
from app.deps import get_db
from app.main import app

ISSUE = {"wo_id": "WO-100", "batch_no": "B-1", "issued_by": "tejas", "issued_to": "M3"}


@pytest.fixture
def client(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'api.db'}")
    init_db(engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    c = TestClient(app)
    for iid, typ in [("c56", "DIE"), ("p12", "PATTERN"), ("p13", "PATTERN"),
                     ("tf-01", "TOP_FLANGE"), ("bf-01", "BOTTOM_FLANGE")]:
        assert c.post("/items", json={"item_id": iid, "item_type": typ,
                                      "home_location": "RACK-A"}).status_code == 201
    base = {"die_id": "c56", "pattern_id": "p12"}
    assert c.post("/assemblies", json={"assembly_id": "t1", **base, "top_flange_id": "tf-01",
                                       "bottom_flange_id": "bf-01"}).status_code == 201
    assert c.post("/assemblies", json={"assembly_id": "t2", "die_id": "C56",
                                       "pattern_id": "p13"}).status_code == 201
    yield c
    app.dependency_overrides.clear()
    engine.dispose()


def test_item_normalised_and_detail(client):
    r = client.get("/items/c56")
    assert r.status_code == 200
    body = r.json()
    assert body["item"]["item_id"] == "C56"
    assert body["status"]["status"] == "AVAILABLE"
    assert [a["assembly_id"] for a in body["assemblies"]] == ["T1", "T2"]  # shared die
    assert body["history"][0]["action"] == "RECEIVE"


def test_item_status_not_editable(client):
    r = client.put("/items/C56", json={"description": "x", "active": True})
    assert r.status_code == 200 and r.json()["description"] == "x"
    assert "status" not in r.json()
    assert client.get("/items/C56").json()["status"]["status"] == "AVAILABLE"


def test_assembly_view_ready_flag_and_issue_blocked(client):
    assert client.get("/assemblies/T1").json()["ready_to_issue"] is True
    r = client.post("/issue", json={"assembly_id": "T1", **ISSUE})
    assert r.status_code == 201
    assert client.get("/assemblies/T2").json()["ready_to_issue"] is False  # shared die in use
    r = client.post("/issue", json={"assembly_id": "T2", "wo_id": "WO-2", "batch_no": "B-2",
                                    "issued_by": "a", "issued_to": "b"})
    assert r.status_code == 409
    b = r.json()
    assert b["blocking_item_id"] == "C56" and b["wo_id"] == "WO-100" and b["batch_no"] == "B-1"
    assert len(client.get("/ledger", params={"action": "ISSUE"}).json()) == 4  # nothing from T2


def test_issue_requires_fields(client):
    r = client.post("/issue", json={"assembly_id": "T1", "wo_id": "WO-1"})
    assert r.status_code == 422 and "batch_no" in r.json()["detail"]


def test_return_by_group_and_open_issues(client):
    g = client.post("/issue", json={"assembly_id": "T1", **ISSUE}).json()["group_id"]
    assert client.get("/issues/open").json()[0]["group_id"] == g
    r = client.post("/return", json={"group_id": g, "to_location": "RACK-Z"})
    assert r.status_code == 200
    s = client.get("/items/BF-1").json()["status"]
    assert s["status"] == "AVAILABLE" and s["current_location"] == "RACK-Z"
    assert client.get("/issues/open").json() == []


def test_reverse_endpoint(client):
    client.post("/issue", json={"item_ids": ["c56"], **ISSUE})
    txn = client.get("/items/C56").json()["status"]["txn_id"]
    r = client.post(f"/reverse/{txn}")
    assert r.status_code == 201 and [x["action"] for x in r.json()] == ["REVERSAL"]
    assert client.get("/items/C56").json()["status"]["status"] == "AVAILABLE"
    assert client.post(f"/reverse/{txn}").status_code == 422


def test_repair_scrap_transfer_flow(client):
    assert client.post("/repair/send", json={"item_ids": ["tf-01"]}).status_code == 200
    assert client.get("/items/TF-1").json()["status"]["status"] == "UNDER_REPAIR"
    assert client.post("/issue", json={"assembly_id": "T1", **ISSUE}).status_code == 409
    assert client.post("/repair/back", json={"item_ids": ["TF-1"],
                                             "to_location": "RACK-B"}).status_code == 200
    assert client.post("/transfer", json={"item_ids": ["TF-1"],
                                          "to_location": "RACK-C"}).status_code == 200
    assert client.post("/scrap", json={"item_ids": ["TF-1"]}).status_code == 200
    assert client.get("/dashboard").json()["SCRAPPED"] == 1


def test_search_by_item_assembly_wo_batch(client):
    client.post("/issue", json={"assembly_id": "T1", **ISSUE})
    r = client.get("/search", params={"q": "c56"}).json()
    assert r["items"][0]["item_id"] == "C56" and r["items"][0]["issued_to"] == "M3"
    assert client.get("/search", params={"q": "t1"}).json()["assemblies"][0]["assembly_id"] == "T1"
    by_wo = client.get("/search", params={"q": "wo-100"}).json()
    assert by_wo["issues"][0]["assembly_id"] == "T1" and len(by_wo["items"]) == 4
    assert client.get("/search", params={"q": "b-1"}).json()["issues"]


def test_ledger_filters(client):
    client.post("/issue", json={"assembly_id": "T1", **ISSUE})
    assert len(client.get("/ledger", params={"wo": "WO-100"}).json()) == 4
    assert len(client.get("/ledger", params={"item": "tf-1"}).json()) == 2
    assert len(client.get("/ledger", params={"person": "tejas"}).json()) == 4
    assert len(client.get("/ledger", params={"assembly": "t1"}).json()) == 4
    assert client.get("/ledger", params={"date_from": "2999-01-01T00:00:00"}).json() == []


def test_no_flange_assembly_works_via_api(client):
    r = client.get("/assemblies/T2").json()
    assert len(r["items"]) == 2 and r["ready_to_issue"]


def test_errors(client):
    assert client.get("/items/NOPE").status_code == 404
    assert client.post("/items", json={"item_id": "x", "item_type": "BOLT"}).status_code == 422
    assert client.post("/items", json={"item_id": "C-056", "item_type": "DIE"}).status_code == 201
    assert client.post("/assemblies", json={"assembly_id": "t9", "die_id": "p12",
                                            "pattern_id": "p13"}).status_code == 422


def test_reverse_endpoint_reverses_whole_group_or_rejects(client):
    client.post("/issue", json={"assembly_id": "T1", **ISSUE})
    txn = client.get("/items/BF-1").json()["status"]["txn_id"]
    r = client.post(f"/reverse/{txn}")
    assert r.status_code == 201 and len(r.json()) == 4
    assert all(client.get(f"/items/{i}").json()["status"]["status"] == "AVAILABLE"
               for i in ("C56", "P12", "TF-1", "BF-1"))

    g = client.post("/issue", json={"assembly_id": "T1", **ISSUE}).json()["group_id"]
    issue_txn = client.get("/items/C56").json()["status"]["txn_id"]
    client.post("/return", json={"item_ids": ["tf-1"], "to_location": "Z"})
    before = len(client.get("/ledger").json())
    r = client.post(f"/reverse/{issue_txn}")
    assert r.status_code == 422 and "nothing was reversed" in r.json()["detail"]
    assert len(client.get("/ledger").json()) == before
    assert g
