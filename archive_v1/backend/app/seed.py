"""Fake test data: 10 items, 5 assemblies (shared dies/flanges, some without flanges).

    python -m app.seed            # only into an empty database
    python -m app.seed --reset    # delete die_mgmt.db first (the ledger can't be edited, so we drop the file)
    python -m app.seed --no-activity   # skip the demo issue / repair entries
"""
import sys

from sqlalchemy.orm import Session

from .database import DB_PATH, SessionLocal, engine, init_db
from .models import Item
from .services import ledger as svc

ITEMS = [
    ("B32-1", "DIE", "Die 32 (A)", 320, 270, 1200, "DIE-RACK-A"),
    ("B32-2", "DIE", "Die 32 (B)", 320, 270, 1200, "DIE-RACK-A"),
    ("C56", "DIE", "Die 56", 560, 500, 1500, "DIE-RACK-B"),
    ("P101", "PATTERN", "Pattern 101", None, None, 1100, "PATTERN-STORE"),
    ("P102", "PATTERN", "Pattern 102", None, None, 1100, "PATTERN-STORE"),
    ("P103", "PATTERN", "Pattern 103", None, None, 1400, "PATTERN-STORE"),
    ("TF-1", "TOP_FLANGE", "Top flange 1", 400, 270, 60, "FLANGE-RACK"),
    ("TF-2", "TOP_FLANGE", "Top flange 2", 640, 500, 70, "FLANGE-RACK"),
    ("BF-1", "BOTTOM_FLANGE", "Bottom flange 1", 400, 270, 60, "FLANGE-RACK"),
    ("BF-2", "BOTTOM_FLANGE", "Bottom flange 2", 640, 500, 70, "FLANGE-RACK"),
]

ASSEMBLIES = [
    # id, description, die, pattern, top flange, bottom flange, od, length, pouring, gating, rcd
    ("T211", "32 NB pipe", "B32-1", "P101", "TF-1", "BF-1", 320, 1100, 410, 45, 365),
    ("T212", "32 NB pipe, long", "B32-1", "P102", "TF-1", "BF-1", 320, 1100, 430, 48, 382),  # shares die + flanges
    ("T213", "32 NB pipe, no flanges", "B32-2", "P103", None, None, 320, 1400, 520, 55, 465),
    ("T214", "56 NB pipe", "C56", "P101", None, None, 560, 1100, 980, 90, 890),  # shares pattern
    ("T215", "56 NB pipe, top flange only", "C56", "P102", "TF-2", None, 560, 1100, 990, 92, 898),  # shares die
]


def seed(db: Session, activity: bool = True) -> None:
    for iid, typ, desc, od, id_dim, length, home in ITEMS:
        svc.create_item(db, iid, typ, description=desc, od=od, id_dim=id_dim, length=length,
                        home_location=home, received_by="SEED")
    for aid, desc, die, pat, tf, bf, od, length, pour, gate, rcd in ASSEMBLIES:
        svc.create_assembly(db, aid, description=desc, die_id=die, pattern_id=pat,
                            top_flange_id=tf, bottom_flange_id=bf, od=od, length=length,
                            pouring_wt=pour, gating_wt=gate, rcd_wt=rcd)
    if activity:
        # T213 is on a machine; TF-2 is in repair, so T215 cannot be issued
        svc.issue(db, assembly_id="T213", wo_id="WO-1001", batch_no="B-07", issued_by="SEED",
                  issued_to="Operator 1", machine="Machine 2", remarks="Demo issue")
        svc.send_repair(db, item_ids=["TF-2"], by="SEED", remarks="Demo: weld repair")


def main(argv: list[str]) -> int:
    if "--reset" in argv:
        engine.dispose()
        DB_PATH.unlink(missing_ok=True)
    init_db(engine)
    with SessionLocal() as db:
        if db.query(Item).count():
            print("Database already has items; refusing to seed. Use --reset to start over.")
            return 1
        seed(db, activity="--no-activity" not in argv)
    print(f"Seeded {len(ITEMS)} items and {len(ASSEMBLIES)} assemblies into {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
