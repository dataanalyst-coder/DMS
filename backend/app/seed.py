"""Fake test data. Creates one user per role with RANDOM passwords written to seed_credentials.txt
(not printed), configuration, designs, locations and ~10 items.

    python -m app.seed            # only into an empty database
    python -m app.seed --reset    # delete die_mgmt.db first (the ledger cannot be edited, so the file is dropped)
"""
import secrets
import sys

from sqlalchemy.orm import Session

from .database import BASE_DIR, DB_PATH, SessionLocal, engine, init_db
from .models import User
from .services import assemblies, auth, batches, builds, config, designs, inspections, items, mappings, readiness, setups, die_sets

CRED_FILE = BASE_DIR / "seed_credentials.txt"
USERS = [
    ("admin", "Admin User", ["ADMIN"]),
    ("engineer", "Eng. Ravi", ["ENGINEERING"]),
    ("stores", "Stores Sunil", ["STORES"]),
    ("production", "Production Pooja", ["PRODUCTION"]),
    ("tooling", "Tooling Tarun", ["TOOLING"]),
    ("scrap", "Scrap Approver Sheela", ["SCRAP_APPROVER"]),
    ("manager", "Manager Meera", ["MANAGER"]),
    ("oms", "OMS usage feed (service account)", ["INTEGRATION"]),
    ("planning", "Planning Priya", ["PLANNING"]),
]
CATEGORIES = [("DIE", "Die body"), ("TOP_FLANGE", "Top flange"), ("BOTTOM_FLANGE", "Bottom flange"),
              ("PATTERN", "Pattern"), ("CORE", "Core"), ("ASSEMBLY", "Tool assembly")]
# code, description, [(design, qty, [substitute designs])]
ASSEMBLIES = [
    ("T211", "32 NB tool assembly", [("DIE-32", 1, []), ("PAT-101", 1, ["PAT-102"]), ("TF-A", 1, []), ("BF-A", 1, []),
                                    ("COAT-1", 2, [])]),  # 2 L of coating per issue, basis: ME spec
    ("T212", "32 NB long (shares die and flanges)", [("DIE-32", 1, []), ("PAT-102", 1, []), ("TF-A", 1, []), ("BF-A", 1, [])]),
    ("T214", "56 NB, no flanges", [("DIE-56", 1, []), ("PAT-101", 1, [])]),
    ("T215", "56 NB, top flange only", [("DIE-56", 1, []), ("PAT-102", 1, []), ("TF-A", 1, [])]),
]
# stand-in for ME: part, routing rev, operation, assembly, selected, tooling status, pouring weight kg
MAPPINGS = [("PART-1001", "R1", "OP10", "T211", True, "READY", 25.0), ("PART-1001", "R1", "OP10", "T212", False, "READY", 25.0),
            ("PART-1002", "R1", "OP10", "T211", True, "READY", 31.5), ("PART-2001", "R2", "OP20", "T214", True, "READY", 80.0),
            ("PART-2001", "R2", "OP20", "T215", False, "READY", 80.0), ("PART-2002", "R1", "OP10", "T215", True, "TO_BE_DEVELOPED", None)]
UOMS = [("EA", "Each"), ("KG", "Kilogram"), ("L", "Litre"), ("M", "Metre")]
# design code, description, category
DESIGNS = [("DIE-32", "Die 32 NB", "DIE"), ("DIE-56", "Die 56 NB", "DIE"),
           ("PAT-101", "Pattern 101", "PATTERN"), ("PAT-102", "Pattern 102", "PATTERN"),
           ("TF-A", "Top flange A", "TOP_FLANGE"), ("BF-A", "Bottom flange A", "BOTTOM_FLANGE")]
CONSUMABLES = [("COAT-1", "Die coating", "CORE")]  # design code, description, category (litres)
# legacy no, design code, condition, bin path
ITEMS = [("B32-1", "DIE-32", "SERVICEABLE", "P1/A1/R1/S1/B1"),
         ("B32-2", "DIE-32", "SERVICEABLE", "P1/A1/R1/S1/B2"),
         ("C56", "DIE-56", "SERVICEABLE", "P1/A1/R2/S1/B1"),
         ("P101", "PAT-101", "SERVICEABLE", "P1/A2/R1/S1/B1"),
         ("P102", "PAT-102", "SERVICEABLE", "P1/A2/R1/S1/B2"),
         ("P103", "PAT-101", "AWAITING_INSPECTION", "P1/A2/R1/S2/B1"),
         ("TF-1", "TF-A", "SERVICEABLE", "P1/A3/R1/S1/B1"),
         ("TF-2", "TF-A", "HOLD", "P1/A3/R1/S1/B2"),
         ("BF-1", "BF-A", "SERVICEABLE", "P1/A3/R2/S1/B1"),
         ("BF-2", "BF-A", "SERVICEABLE", "P1/A3/R2/S1/B2")]


def _build_locations(db, admin):
    plant = config.create_location(db, admin, code="P1", name="Main plant")
    for area, name, racks in (("A1", "Die store", ("R1", "R2")), ("A2", "Pattern store", ("R1",)),
                              ("A3", "Flange store", ("R1", "R2"))):
        a = config.create_location(db, admin, code=area, name=name, parent_id=plant.id)
        for r in racks:
            rack = config.create_location(db, admin, code=r, parent_id=a.id)
            for s in ("S1", "S2"):
                shelf = config.create_location(db, admin, code=s, parent_id=rack.id)
                for b in ("B1", "B2"):
                    config.create_location(db, admin, code=b, parent_id=shelf.id)


SIMPLE_PASSWORD = "vspl@12345"


def seed(db: Session, simple: bool = False) -> dict[str, str]:
    """Returns {username: password}. Passwords are random unless `simple` (one easy password for everyone: local test data only)."""
    passwords = {u: (SIMPLE_PASSWORD if simple else secrets.token_urlsafe(12)) for u, _, _ in USERS}
    admin = auth.bootstrap_admin(db, "admin", "Admin User", passwords["admin"])
    for uname, full, roles in USERS[1:]:
        auth.create_user(db, admin, uname, full, passwords[uname], roles)
    actors = {u.username: auth.load_roles(db, u) for u in db.query(User)}
    eng, stores = actors["engineer"], actors["stores"]

    for code, name in CATEGORIES:
        config.create_category(db, admin, code, name)
    for code, name in UOMS:
        config.create_uom(db, admin, code, name)
    _build_locations(db, admin)

    cats = {c.code: c.id for c in config.list_categories(db)}
    design_ids = {}
    for code, desc, cat in DESIGNS:
        d = designs.create_design(db, eng, code=code, description=desc, category_id=cats[cat],
                                  base_uom="EA", storage_instructions="Store dry, on timber bearers")
        designs.approve_revision(db, eng, designs.revisions(db, d.id)[0].id)
        design_ids[code] = d.id
    for code, desc, cat in CONSUMABLES:
        d = designs.create_design(db, eng, code=code, description=desc, category_id=cats[cat], base_uom="L",
                                  kind="CONSUMABLE")
        designs.approve_revision(db, eng, designs.revisions(db, d.id)[0].id)
        design_ids[code] = d.id

    # inspection checklists: the standard starting checks per tooling category, approved by Engineering
    for cat in ("DIE", "TOP_FLANGE", "BOTTOM_FLANGE", "PATTERN"):
        v = inspections.apply_starter(db, eng, "CATEGORY", cats[cat])
        inspections.approve_version(db, eng, v.id)

    item_ids = {}
    for legacy, dcode, cond, path in ITEMS:
        it = items.register_item(db, stores, design_id=design_ids[dcode], legacy_no=legacy, condition=cond,
                                 location_id=config.resolve_path(db, path).id,
                                 provenance="Seed data", opening_cycles=None, opening_tonnage=None)
        item_ids[legacy] = it.id

    asm_cat = cats["ASSEMBLY"]
    asm_ids = {}
    for code, desc, lines in ASSEMBLIES:
        d = assemblies.create_assembly(db, eng, code=code, description=desc, category_id=asm_cat,
                                       instructions="Fit flanges after the die; torque to spec.")
        rev = assemblies.revisions(db, d.id)[0]
        for dcode, qty, subs in lines:
            ln = assemblies.add_line(db, eng, rev.id, design_id=design_ids[dcode], quantity=qty,
                                     quantity_basis="ME spec ME-17" if dcode == "COAT-1" else None)
            for sub in subs:
                assemblies.add_alternative(db, eng, ln.id, design_id=design_ids[sub])
        assemblies.approve_revision(db, eng, rev.id)
        asm_ids[code] = d.id
    for part, rrev, op, code, selected, tooling, weight in MAPPINGS:
        mappings.create_mapping(db, eng, part_id=part, routing_revision=rrev, operation_id=op,
                                definition_id=asm_ids[code], selected=selected, tooling_status=tooling,
                                pouring_weight_kg=weight, pouring_weight_revision="W1" if weight else None)

    # Planning's setups (as on the planning sheet) and Engineering's rule that part PART-1001 uses assembly T211
    planning = actors["planning"]
    setups.add_map(db, eng, definition_id=asm_ids["T211"], part_id="PART-1001")
    setups.add_map(db, eng, definition_id=asm_ids["T211"], part_id="PART-1002")
    # one setup (one tooling set) running two WOs / parts, and one more like it for the next run
    setups.create_setup(db, planning, name="SETUP-A", plan_ref="DEMO", lines=[
        {"wo_id": "WO-1001", "part_id": "PART-1001", "tool_description": "32 NB DIE SET", "family": "DIE SET", "grade": "G1", "fg_qty": 20, "rcd_qty": 10},
        {"wo_id": "WO-1002", "part_id": "PART-1002", "tool_description": "32 NB DIE SET", "family": "DIE SET", "grade": "G1", "fg_qty": 12, "rcd_qty": 6}])
    setups.create_setup(db, planning, wo_id="WO-17835", part_id="BLV3", tool_description="BLV 003", family="ANNELEAD PART", fg_qty=125, rcd_qty=25, plan_ref="DEMO")
    setups.create_setup(db, planning, wo_id="WO-17833", part_id="ADN2", tool_description="ADN 2", family="ADANI", fg_qty=5, rcd_qty=5, plan_ref="DEMO")

    # Engineering's die mapping: each physical die with its own pattern and flanges
    for die, pat, top, bot in (("B32-1", "P101", "TF-1", "BF-1"), ("B32-2", "P102", "TF-2", "BF-2"), ("C56", None, None, None)):
        die_sets.save_set(db, eng, die_item_id=item_ids[die], pattern_item_id=item_ids.get(pat), top_flange_item_id=item_ids.get(top),
                          bottom_flange_item_id=item_ids.get(bot))

    batches.receive_batch(db, stores, design_id=design_ids["COAT-1"], quantity=50, supplier_lot="LOT-2026-114",
                          location_id=config.resolve_path(db, "P1/A1/R2/S2/B1").id)

    # one verified build: T211 from B32-1, P101, TF-1, BF-1 (assembled by stores, verified by engineering)
    b = builds.create_build(db, stores, definition_id=asm_ids["T211"],
                            location_id=config.resolve_path(db, "P1/A1/R1/S2/B1").id)
    for legacy in ("B32-1", "P101", "TF-1", "BF-1"):
        builds.fit_item(db, stores, b.id, item_ids[legacy])
    builds.verify_build(db, eng, b.id)

    # a stand-in OMS release check for the demo WO (production runs it; ready to issue is a separate step)
    readiness.run_check(db, actors["production"], wo_id="WO-1001", part_id="PART-1001",
                        routing_revision="R1", operation_id="OP10")
    return passwords


def main(argv: list[str]) -> int:
    if "--reset" in argv:
        engine.dispose()
        DB_PATH.unlink(missing_ok=True)
    init_db(engine)
    with SessionLocal() as db:
        if db.query(User).count():
            print("Database already has users; refusing to seed. Use --reset to start over.")
            return 1
        passwords = seed(db, simple="--simple" in argv)
    CRED_FILE.write_text("\n".join(f"{u}: {p}" for u, p in passwords.items()) + "\n")
    print(f"Seeded {len(ITEMS)} items, {len(DESIGNS)} designs, {len(ASSEMBLIES)} assemblies, {len(MAPPINGS)} mappings, 1 build, {len(USERS)} users into {DB_PATH}")
    print(f"Passwords for the seeded users are in {CRED_FILE} (test data only; delete with the database).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
