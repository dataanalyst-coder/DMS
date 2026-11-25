from io import BytesIO

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.database import init_db, make_engine
from app.deps import get_db
from app.main import app
from app.services import excel_io


@pytest.fixture
def client(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'x.db'}")
    init_db(engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


def xlsx(df: pd.DataFrame) -> dict:
    buf = BytesIO()
    df.to_excel(buf, index=False)
    return {"file": ("f.xlsx", buf.getvalue(), "application/octet-stream")}


ITEMS = pd.DataFrame([
    {"item_id": " c56 ", "item_type": "die", "description": "Die 56", "od": 300, "id_dim": 250, "length": 1000, "home_location": "RACK-A"},
    {"item_id": "p12", "item_type": "PATTERN", "home_location": "PAT"},
    {"item_id": "tf-01", "item_type": "TOP_FLANGE", "home_location": "RACK-B"},
])


def load_items(client):
    r = client.post("/import/items", params={"commit": True}, files=xlsx(ITEMS))
    assert r.status_code == 200 and r.json()["committed"], r.json()


def test_templates_have_spec_columns_and_validate_as_empty(client):
    for kind, cols in (("items", excel_io.ITEM_COLUMNS), ("assemblies", excel_io.ASSEMBLY_COLUMNS)):
        r = client.get(f"/templates/{kind}")
        assert r.status_code == 200
        df = pd.read_excel(BytesIO(r.content), sheet_name="data")
        assert list(df.columns) == cols
        res = client.post(f"/import/{kind}", files={"file": ("t.xlsx", r.content)}).json()
        assert res["valid"] is False and "no data rows" in res["errors"][0]["message"]


def test_items_import_creates_items_and_receive_entries(client):
    load_items(client)
    d = client.get("/items/C56").json()
    assert d["item"]["item_type"] == "DIE" and d["item"]["od"] == 300
    assert d["status"]["status"] == "AVAILABLE" and d["status"]["current_location"] == "RACK-A"
    assert [h["action"] for h in d["history"]] == ["RECEIVE"]
    assert client.get("/items/TF-1").status_code == 200  # tf-01 normalised


def test_preview_does_not_write(client):
    r = client.post("/import/items", files=xlsx(ITEMS)).json()
    assert r["valid"] and not r["committed"] and r["rows"] == 3
    assert client.get("/items").json() == []


def test_items_validation_errors_all_or_nothing(client):
    bad = pd.DataFrame([
        {"item_id": "a1", "item_type": "DIE", "od": "abc"},
        {"item_id": "A1", "item_type": "DIE"},
        {"item_id": "b1", "item_type": "BOLT"},
        {"item_id": "", "item_type": "DIE"},
        {"item_id": "ok1", "item_type": "PATTERN"},
    ])
    r = client.post("/import/items", params={"commit": True}, files=xlsx(bad)).json()
    assert not r["valid"] and not r["committed"]
    msgs = {(e["row"], e["field"]): e["message"] for e in r["errors"]}
    assert "number" in msgs[(2, "od")]
    assert "Duplicate" in msgs[(3, "item_id")]
    assert "Invalid item_type" in msgs[(4, "item_type")]
    assert "required" in msgs[(5, "item_id")]
    assert client.get("/items").json() == []  # valid row ok1 was NOT written


def test_existing_item_rejected_on_reimport(client):
    load_items(client)
    r = client.post("/import/items", files=xlsx(ITEMS)).json()
    assert not r["valid"] and "already exists" in r["errors"][0]["message"]


def test_missing_column(client):
    r = client.post("/import/items", files=xlsx(ITEMS.drop(columns=["item_type"]))).json()
    assert r["errors"][0]["message"] == "Missing column: item_type"


def test_assemblies_import_shared_die_no_flange_and_validation(client):
    load_items(client)
    ok = pd.DataFrame([
        {"assembly_id": "t1", "die_id": "c56", "pattern_id": "p12", "top_flange_id": "tf-01", "pouring_wt": 12.5},
        {"assembly_id": "t2", "die_id": "C56", "pattern_id": "P12"},  # shared die, no flanges
    ])
    r = client.post("/import/assemblies", params={"commit": True}, files=xlsx(ok)).json()
    assert r["committed"] and r["created"] == 2
    assert client.get("/assemblies/T2").json()["ready_to_issue"] is True

    bad = pd.DataFrame([
        {"assembly_id": "t3", "die_id": "p12", "pattern_id": "p12"},          # pattern in die_id
        {"assembly_id": "t4", "die_id": "nope", "pattern_id": "p12"},         # unknown
        {"assembly_id": "t5", "die_id": "c56", "pattern_id": "p12", "bottom_flange_id": "tf-01"},  # wrong flange type
        {"assembly_id": "t1", "die_id": "c56", "pattern_id": "p12"},          # exists
        {"assembly_id": "t6", "die_id": "c56", "pattern_id": "p12"},
        {"assembly_id": "T6", "die_id": "c56", "pattern_id": "p12"},          # duplicate in file
    ])
    r = client.post("/import/assemblies", params={"commit": True}, files=xlsx(bad)).json()
    m = {(e["row"], e["field"]): e["message"] for e in r["errors"]}
    assert "is a PATTERN, expected DIE" in m[(2, "die_id")]
    assert "unknown item NOPE" in m[(3, "die_id")]
    assert "expected BOTTOM_FLANGE" in m[(4, "bottom_flange_id")]
    assert "already exists" in m[(5, "assembly_id")]
    assert "Duplicate" in m[(7, "assembly_id")]
    assert not r["committed"] and len(client.get("/assemblies").json()) == 2


def test_numeric_ids_and_blank_rows_keep_excel_row_numbers(client):
    df = pd.DataFrame([{"item_id": 56, "item_type": "DIE"}, {"item_id": None, "item_type": None},
                       {"item_id": "x", "item_type": "NOPE"}])
    r = client.post("/import/items", files=xlsx(df)).json()
    assert [e["row"] for e in r["errors"]] == [4]


def test_bad_file_and_unknown_kind(client):
    assert client.post("/import/items", files={"file": ("x.xlsx", b"junk")}).status_code == 422
    assert client.post("/import/widgets", files=xlsx(ITEMS)).status_code == 404


def test_exports(client):
    load_items(client)
    client.post("/issue", json={"item_ids": ["c56"], "wo_id": "WO-1", "batch_no": "B1",
                                "issued_by": "a", "issued_to": "b"})
    led = pd.read_excel(BytesIO(client.get("/export/ledger").content))
    assert len(led) == 4 and "ISSUE" in set(led["action"])
    only = pd.read_excel(BytesIO(client.get("/export/ledger", params={"wo": "WO-1"}).content))
    assert len(only) == 1
    st = pd.read_excel(BytesIO(client.get("/export/status").content))
    assert dict(zip(st["item_id"], st["status"]))["C56"] == "IN_USE"
