import pytest
from sqlalchemy.orm import sessionmaker

from app.database import init_db, make_engine
from app.models import MeMapping, SetupToolMap
from app.services import auth, config, designs, inspections, items, ledger, setups

PW = "correct-horse-battery"


def ensure_checklist(db, people, category_id):
    """Every tooling category gets a small approved checklist, so items can be inspected and released."""
    eng = people["eng"]
    v = inspections.create_template(db, eng, "CATEGORY", category_id)
    inspections.add_check(db, eng, v.id, code="COMPLETE_ID", title="Completeness and identification")
    inspections.add_check(db, eng, v.id, code="CRACKS", title="Cracks", evidence="ON_FAIL")
    inspections.approve_version(db, eng, v.id)


def clear_items(db, people, *item_ids):
    """The proper route back to SERVICEABLE: (further inspection if held) -> inspect, all PASS -> Engineering release."""
    for item_id in item_ids:
        s = ledger.get_state(db, item_id)
        if s["item_condition"] == "SERVICEABLE":
            continue
        if s["item_condition"] == "HOLD":
            ledger.set_condition(db, people["tooling"], [item_id], "AWAITING_INSPECTION", reason="further inspection")
        i = inspections.start_inspection(db, people["tooling"], item_id)
        for c in inspections.effective_checklist(db, db.get(items.Item, item_id))["checks"]:
            inspections.record_result(db, people["tooling"], i.id, c["id"], result="PASS")
        inspections.complete_inspection(db, people["tooling"], i.id)
        ledger.set_condition(db, people["eng"], [item_id], "SERVICEABLE", reason="inspection passed; released")


@pytest.fixture(autouse=True)
def _secret_and_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("DIE_SECRET_KEY", "test-secret-key")
    from app.services import storage
    monkeypatch.setattr(storage, "STORAGE_DIR", tmp_path / "storage")
    monkeypatch.setitem(auth._SCRYPT, "n", 16)  # fast hashes in tests; format still verified
    auth._failed.clear()


@pytest.fixture
def engine(tmp_path):
    e = make_engine(f"sqlite:///{tmp_path / 'test.db'}", fast=True)
    init_db(e)
    yield e
    e.dispose()


@pytest.fixture
def db(engine):
    s = sessionmaker(bind=engine, autoflush=False, future=True)()
    yield s
    s.close()


@pytest.fixture
def people(db):
    """One user per role, plus a multi-role user. Returns {username: user}."""
    admin = auth.bootstrap_admin(db, "admin", "Admin", PW)
    out = {"admin": admin}
    for uname, roles in [("eng", ["ENGINEERING"]), ("stores", ["STORES"]), ("prod", ["PRODUCTION"]),
                         ("tooling", ["TOOLING"]), ("scrap", ["SCRAP_APPROVER"]), ("plan", ["PLANNING"]),
                         ("mgr", ["MANAGER"]), ("multi", ["STORES", "TOOLING"])]:
        out[uname] = auth.create_user(db, admin, uname, uname.title(), PW, roles)
    return out


@pytest.fixture
def world(db, people):
    """Config + two approved designs + a location tree. Returns a dict of handy ids."""
    admin, eng = people["admin"], people["eng"]
    cat = config.create_category(db, admin, "DIE", "Die body")
    pat = config.create_category(db, admin, "PATTERN", "Pattern")
    ensure_checklist(db, people, cat.id)
    ensure_checklist(db, people, pat.id)
    config.create_uom(db, admin, "EA", "Each")
    plant = config.create_location(db, admin, code="P1")
    area = config.create_location(db, admin, code="A1", parent_id=plant.id)
    rack = config.create_location(db, admin, code="R1", parent_id=area.id)
    shelf = config.create_location(db, admin, code="S1", parent_id=rack.id)
    bins = [config.create_location(db, admin, code=f"B{i}", parent_id=shelf.id) for i in (1, 2, 3)]
    die = designs.create_design(db, eng, code="die-32", description="Die 32", category_id=cat.id, base_uom="ea")
    designs.approve_revision(db, eng, designs.revisions(db, die.id)[0].id)
    patt = designs.create_design(db, eng, code="pat-1", description="Pattern 1", category_id=pat.id,
                                 base_uom="EA")
    designs.approve_revision(db, eng, designs.revisions(db, patt.id)[0].id)
    return {"cat": cat.id, "die": die.id, "pat": patt.id, "plant": plant.id, "area": area.id,
            "rack": rack.id, "shelf": shelf.id, "bins": [b.id for b in bins]}


@pytest.fixture
def make_item(db, people, world):
    def _make(legacy="b32-01", condition="SERVICEABLE", bin_index=0, design="die", **kw):
        return items.register_item(db, people["stores"], design_id=world[design], legacy_no=legacy,
                                   condition=condition, location_id=world["bins"][bin_index], **kw)
    return _make


@pytest.fixture
def kit(db, people, world):
    """Approved assembly T211 = die + pattern (substitute pattern allowed) + top flange + bottom flange,
    with spare physical items for each design."""
    from app.services import assemblies
    admin, eng = people["admin"], people["eng"]
    cats = {c: config.create_category(db, admin, c, c).id for c in ("TOP-FLANGE", "BOTTOM-FLANGE", "ASSEMBLY")}
    for key in ("TOP-FLANGE", "BOTTOM-FLANGE"):
        ensure_checklist(db, people, cats[key])
    ids = {"die": world["die"], "pat": world["pat"]}

    def design(code, cat):
        d = designs.create_design(db, eng, code=code, description=code, category_id=cat, base_uom="EA")
        designs.approve_revision(db, eng, designs.revisions(db, d.id)[0].id)
        return d.id

    ids["pat2"] = design("pat-2", world["cat"])
    ids["tf"] = design("tf-a", cats["TOP-FLANGE"])
    ids["bf"] = design("bf-a", cats["BOTTOM-FLANGE"])
    ids["asm_cat"] = cats["ASSEMBLY"]
    a = assemblies.create_assembly(db, eng, code="t211", description="32 NB assembly", category_id=cats["ASSEMBLY"])
    rev = assemblies.revisions(db, a.id)[0]
    lines = {}
    for key in ("die", "pat", "tf", "bf"):
        lines[key] = assemblies.add_line(db, eng, rev.id, design_id=ids[key])
    assemblies.add_alternative(db, eng, lines["pat"].id, design_id=ids["pat2"])
    assemblies.approve_revision(db, eng, rev.id)
    ids.update(asm=a.id, rev=rev.id, lines={k: v.id for k, v in lines.items()})

    stores = people["stores"]
    loc = world["bins"]

    def item(legacy, design_key, bin_i=0):
        return items.register_item(db, stores, design_id=ids[design_key], legacy_no=legacy,
                                   condition="SERVICEABLE", location_id=loc[bin_i]).id

    ids["items"] = {"d1": item("d1", "die"), "d2": item("d2", "die", 1), "p1": item("p1", "pat"),
                    "p2": item("p2", "pat2"), "p3": item("p3", "pat", 2), "t1": item("t1", "tf"),
                    "b1": item("b1", "bf")}
    return ids


def mk_setup(db, people, wo_id, mapping_id):
    """A planned setup for this WO whose part is served by the mapping's assembly (the tool-mapping rule is added if
    missing). Every issue must name a setup, so tests that just need an issue call this."""
    m = db.get(MeMapping, int(mapping_id))
    if not db.query(SetupToolMap).filter_by(part_id=m.part_id, definition_id=m.definition_id, active=True).first():
        setups.add_map(db, people["eng"], definition_id=m.definition_id, part_id=m.part_id)
    from app.models import SetupLine
    ln = db.query(SetupLine).filter_by(wo_id=wo_id, part_id=m.part_id).first()
    return ln.setup_id if ln else setups.create_setup(db, people["plan"], wo_id=wo_id, part_id=m.part_id, tool_description=m.part_id).id


def setup_body(api, login, wo_id, mapping_id):
    """HTTP twin of mk_setup: returns {"wo_id", "setup_id"} to merge into an issue request."""
    rows = api.get("/mappings", headers=login("eng")).json()["rows"]
    m = next(r for r in rows if r["id"] == int(mapping_id))
    if not any(x["part_id"] == m["part_id"] and x["definition_id"] == m["definition_id"] for x in api.get("/setup-maps", headers=login("eng")).json()):
        r = api.post("/setup-maps", json={"definition_id": m["definition_id"], "part_id": m["part_id"]}, headers=login("eng"))
        assert r.status_code == 201, r.text
    hit = api.get("/setups", params={"wo_id": wo_id}, headers=login("plan")).json()["rows"]
    sid = next((x["id"] for x in hit if x["part_id"] == m["part_id"]), None)
    if sid is None:
        r = api.post("/setups", json={"wo_id": wo_id, "part_id": m["part_id"], "tool_description": m["part_id"]}, headers=login("plan"))
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
    return {"wo_id": wo_id, "setup_id": sid}
