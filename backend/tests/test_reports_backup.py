import zipfile
from datetime import datetime, timedelta
from io import BytesIO

import pandas as pd
import pytest
from sqlalchemy.orm import sessionmaker

from app import backup
from app.database import make_engine
from app.models import Issue, Item
from app.services import (auth, builds, counts, dispositions, documents, excel_io, issues, items, ledger, mappings, reports,
                          usage)
from app.services.errors import NotFoundError, ValidationError
from tests.conftest import PW, mk_setup, setup_body

KEYS = ("d1", "p1", "t1", "b1")


@pytest.fixture
def world2(db, people, kit, world):
    mappings.create_mapping(db, people["eng"], part_id="PN-1", routing_revision="R1", operation_id="OP10",
                            definition_id=kit["asm"], selected=True, pouring_weight_kg=25, pouring_weight_revision="W1")
    return kit


@pytest.fixture
def issued(db, people, world2, world):
    k = world2
    b = builds.create_build(db, people["stores"], definition_id=k["asm"], location_id=world["bins"][0])
    for key in KEYS:
        builds.fit_item(db, people["stores"], b.id, k["items"][key])
    builds.verify_build(db, people["eng"], b.id)
    ids = [k["items"][key] for key in KEYS]
    m = mappings.list_mappings(db)[1][0]["id"]
    i = issues.create_issue(db, people["stores"], setup_id=mk_setup(db, people, "WO-1", m), wo_id="WO-1", mapping_id=m, build_id=b.id, confirmed_item_ids=ids)
    issues.acknowledge_issue(db, people["prod"], i.id)
    k.update(issue=i.id, build=b.id, ids=ids)
    return k


# ---- reports -----------------------------------------------------------------------------------------------------
def test_inventory_report_groups_by_location_and_condition(db, people, kit):
    ledger.set_condition(db, people["tooling"], [kit["items"]["d2"]], "HOLD", reason="crack")
    rep = reports.run(db, "inventory")
    assert rep["summary"]["items"] == 7 and rep["summary"]["by_condition"] == {"SERVICEABLE": 6, "HOLD": 1}
    assert {"display_id", "condition", "custody", "location", "home_location"} <= set(rep["columns"])
    only_hold = reports.run(db, "inventory", condition="HOLD")
    assert [r["display_id"] for r in only_hold["rows"]] == ["D2"]
    assert any(k.endswith("| HOLD") for k in rep["summary"]["by_location_and_condition"])


def test_assembly_readiness_report_shows_ready_and_not_ready_with_the_shortage(db, people, kit):
    rep = reports.run(db, "assembly-readiness")
    assert [(r["assembly"], r["result"]) for r in rep["rows"]] == [("T211", "READY")]
    for k in ("d1", "d2"):
        ledger.set_condition(db, people["tooling"], [kit["items"][k]], "HOLD", reason="crack")
    rep = reports.run(db, "assembly-readiness")
    assert rep["rows"][0]["result"] == "NOT_READY" and "DIE-32" in rep["rows"][0]["shortages"]
    assert rep["summary"] == {"assemblies": 1, "ready": 0, "not_ready": 1}


def test_custody_by_wo_lists_what_is_out_and_for_how_long(db, people, issued):
    rep = reports.run(db, "custody-by-wo")
    (row,) = rep["rows"]
    assert row["wo_id"] == "WO-1" and row["days_out"] == 0 and sorted(row["outstanding"]) == ["B1", "D1", "P1", "T1"]
    assert rep["columns"][0] == "wo_id" and rep["summary"] == {"open_issues": 1}


def test_missing_and_overdue_items_are_listed_for_manual_review(db, people, issued, world, make_item):
    db.query(Issue).update({"issued_at": datetime.utcnow() - timedelta(days=40)})
    db.commit()
    lost = make_item(legacy="lost", bin_index=1)
    c = counts.start_count(db, people["stores"], world["bins"][1])
    counts.complete(db, people["stores"], c.id)
    counts.apply(db, people["eng"], c.id, [{"item_id": lost.id, "action": "MARK_MISSING", "reason": "not on the rack"}])
    rep = reports.run(db, "missing-overdue", days=30)
    kinds = sorted((r["kind"], r["item"]) for r in rep["rows"])
    assert ("MISSING", "LOST") in kinds and [k for k, _ in kinds].count("OVERDUE") == 4
    assert reports.run(db, "missing-overdue", days=60)["summary"]["overdue"] == 0
    assert "review manually" in next(r for r in rep["rows"] if r["kind"] == "OVERDUE")["detail"]
    with pytest.raises(ValidationError):
        reports.run(db, "missing-overdue", days=-1)


def test_repairs_report(db, people, make_item):
    it = make_item()
    ledger.set_condition(db, people["tooling"], [it.id], "HOLD", reason="crack")
    dispositions.decide(db, people["tooling"], it.id, "REPAIR", findings="seat cracked", responsible="tool room")
    rep = reports.run(db, "repairs")
    assert rep["summary"] == {"open": 1, "completed": 0} and rep["rows"][0]["findings"] == "seat cracked"
    dispositions.complete_repair(db, people["tooling"], rep["rows"][0]["repair"], work_done="welded")
    assert reports.run(db, "repairs")["summary"] == {"open": 0, "completed": 1}


def test_item_usage_report_shows_unknown_opening_as_unknown_and_foundry_output_once(db, people, issued):
    oms = auth.create_user(db, people["admin"], "oms", "OMS", PW, ["INTEGRATION"])
    from app.models import BuildComponent
    db.query(Issue).update({"issued_at": datetime.utcnow() - timedelta(hours=2)})
    db.query(BuildComponent).update({"fitted_at": datetime.utcnow() - timedelta(hours=3)})
    db.commit()
    t = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    usage.ingest(db, oms, {"source_event_id": "E1", "wo_id": "WO-1", "build": "BLD-000001", "production_end": t,
                           "cycles": 10, "castings": 20})
    rep = reports.run(db, "item-usage")
    assert len(rep["rows"]) == 4 and {r["opening_cycles"] for r in rep["rows"]} == {"unknown"}
    assert {r["total_tonnage"] for r in rep["rows"]} == {"unknown"} and all(r["recorded_tonnage"] == 0.5 for r in rep["rows"])
    assert rep["summary"]["foundry_output_tonnes"] == 0.5 and "never summed" in rep["summary"]["note"]


def test_5s_report_flags_items_away_from_home_with_or_without_a_reason(db, people, world, make_item):
    a, b = make_item(legacy="a1", bin_index=0), make_item(legacy="b1", bin_index=1)
    ledger.move_items(db, people["stores"], [a.id], world["bins"][2], remarks="bin B1 is being repaired")
    ledger.move_items(db, people["stores"], [b.id], world["bins"][2])
    rep = reports.run(db, "5s-mismatches")
    by = {r["item"]: r for r in rep["rows"]}
    assert by["A1"]["reason"] == "bin B1 is being repaired" and by["A1"]["reason_recorded"] is True
    assert by["B1"]["reason_recorded"] is False and by["A1"]["actual_location"].endswith("B3")  # the real location is shown
    assert rep["summary"] == {"away_from_home": 2, "without_a_reason": 1}


def test_batch_audit_reports_and_unknown_report(db, people, world, kit):
    assert reports.run(db, "audit")["summary"]["total"] > 0 and reports.run(db, "audit", action="APPROVE")["rows"]
    assert reports.run(db, "batch-stock")["rows"] == []
    with pytest.raises(NotFoundError):
        reports.run(db, "nonsense")


def test_report_exports_to_excel(db, people, kit):
    rep = reports.run(db, "inventory")
    df = pd.read_excel(BytesIO(excel_io.rows_xlsx("inventory", rep["columns"], rep["rows"])))
    assert list(df.columns) == rep["columns"] and len(df) == 7


# ---- backup and restore (records AND attachments) -------------------------------------------------------------------------
@pytest.fixture
def populated(db, engine, people, make_item, tmp_path):
    make_item(legacy="a1")
    d = documents.create_document(db, people["eng"], doc_key="dwg-1", doc_type="DIE_DRAWING", title="Die drawing")
    v = documents.add_version(db, people["eng"], d.id, filename="die.pdf", data=b"%PDF-1.4\nreal drawing bytes")
    documents.approve_version(db, people["eng"], v.id)
    return {"db_path": engine.url.database, "storage": tmp_path / "storage", "version": v, "count": db.query(Item).count()}


def test_backup_restores_records_and_attachments(db, people, populated, tmp_path):
    z = backup.create_backup(tmp_path / "bk", db_path=populated["db_path"], storage_dir=populated["storage"])
    manifest = backup.verify_backup(z)
    assert "die_mgmt.db" in manifest["files"] and any(n.startswith("storage/documents/") for n in manifest["files"])
    info = backup.restore_backup(z, tmp_path / "restored")
    e2 = make_engine(f"sqlite:///{info['db']}")
    s2 = sessionmaker(bind=e2, future=True)()
    assert s2.query(Item).count() == populated["count"]
    v = s2.get(type(populated["version"]), populated["version"].id)
    assert (tmp_path / "restored" / "storage" / v.stored_path).read_bytes() == b"%PDF-1.4\nreal drawing bytes"
    s2.close()
    e2.dispose()


def test_backup_is_consistent_while_the_system_is_in_use_and_keeps_ledger_triggers(db, people, populated, tmp_path, make_item):
    z = backup.create_backup(tmp_path / "bk", db_path=populated["db_path"], storage_dir=populated["storage"])
    make_item(legacy="after-backup")  # changes after the backup do not leak into it
    info = backup.restore_backup(z, tmp_path / "r2")
    e2 = make_engine(f"sqlite:///{info['db']}")
    s2 = sessionmaker(bind=e2, future=True)()
    assert s2.query(Item).count() == populated["count"]
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError
    with pytest.raises(DatabaseError):  # the append-only protection survives a restore
        s2.execute(text("DELETE FROM ledger"))
    s2.close()
    e2.dispose()


def test_damaged_or_foreign_archives_are_refused_and_nothing_is_overwritten(db, people, populated, tmp_path):
    z = backup.create_backup(tmp_path / "bk", db_path=populated["db_path"], storage_dir=populated["storage"])
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(bad, "w") as dst:
        for n in src.namelist():
            data = src.read(n)
            dst.writestr(n, data[:-1] + b"X" if n == "die_mgmt.db" else data)
    with pytest.raises(ValidationError) as e:
        backup.verify_backup(bad)
    assert "damaged" in e.value.message
    with pytest.raises(ValidationError):
        backup.restore_backup(bad, tmp_path / "nope")
    assert not (tmp_path / "nope" / "die_mgmt.db").exists()
    foreign = tmp_path / "foreign.zip"
    with zipfile.ZipFile(foreign, "w") as zf:
        zf.writestr("hello.txt", "hi")
    with pytest.raises(ValidationError) as e:
        backup.verify_backup(foreign)
    assert "manifest" in e.value.message
    backup.restore_backup(z, tmp_path / "ok")
    with pytest.raises(ValidationError) as e:  # an existing database is never overwritten by accident
        backup.restore_backup(z, tmp_path / "ok")
    assert "already exists" in e.value.message
    backup.restore_backup(z, tmp_path / "ok", overwrite=True)


def test_backup_cli(tmp_path, populated, capsys, monkeypatch):
    monkeypatch.setattr(backup, "DB_PATH", populated["db_path"])
    monkeypatch.setattr(backup, "STORAGE_DIR", populated["storage"])
    assert backup.main(["create", str(tmp_path / "cli")]) == 0
    zips = list((tmp_path / "cli").glob("*.zip"))
    assert len(zips) == 1 and backup.main(["verify", str(zips[0])]) == 0
    assert backup.main(["restore", str(zips[0]), str(tmp_path / "restored")]) == 0
    assert backup.main(["restore", str(zips[0]), str(tmp_path / "restored")]) == 1  # refuses to overwrite
    assert backup.main([]) == 2
