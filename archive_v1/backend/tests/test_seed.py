from app import seed
from app.services import ledger as svc


def test_seed_data_behaves(db_empty):
    seed.seed(db_empty)
    db = db_empty
    assert len(svc.list_items(db)) == 10
    # T213 was issued in the demo; T211/T212 are unaffected, T215 is blocked by the repair
    assert svc.get_assembly_view(db, "T213")["ready_to_issue"] is False
    assert svc.get_assembly_view(db, "T211")["ready_to_issue"] is True
    assert svc.get_assembly_view(db, "T215")["ready_to_issue"] is False
    # shared die: issuing T211 blocks T212 and names the WO
    svc.issue(db, assembly_id="T211", wo_id="WO-2", batch_no="B-2", issued_by="a", issued_to="b")
    try:
        svc.issue(db, assembly_id="T212", wo_id="WO-3", batch_no="B-3", issued_by="a", issued_to="b")
        assert False, "expected block"
    except svc.BlockedError as e:
        assert e.item_id == "B32-1" and e.wo_id == "WO-2"
    # assemblies without flanges work
    assert len(svc.get_assembly_view(db, "T214")["items"]) == 2
