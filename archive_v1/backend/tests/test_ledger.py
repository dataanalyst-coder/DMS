import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app.models import Item, Ledger
from app.services import ledger as svc
from tests.conftest import ISSUE


def statuses(db, ids):
    return {i: svc.get_status(db, i) for i in ids}


def ledger_count(db):
    return db.query(Ledger).count()


# ---- spec: "Tests that must pass" -------------------------------------------------
def test_issue_blocked_when_die_in_use_under_other_assembly_names_wo(db):
    svc.issue(db, assembly_id="T1", **ISSUE)
    with pytest.raises(svc.BlockedError) as e:
        svc.issue(db, assembly_id="T2", wo_id="WO-200", batch_no="B-2",
                  issued_by="x", issued_to="y")
    assert e.value.item_id == "C56"
    assert e.value.wo_id == "WO-100"
    assert e.value.batch_no == "B-1"
    assert "WO-100" in e.value.message and "C56" in e.value.message


def test_partial_failure_writes_no_ledger_rows(db):
    # P12 is shared by T1 and T3. Issue T3 first so T1 fails on P12 *after* C56 passes the check.
    svc.issue(db, assembly_id="T3", **ISSUE)
    before = ledger_count(db)
    with pytest.raises(svc.BlockedError):
        svc.issue(db, assembly_id="T1", wo_id="WO-2", batch_no="B-2", issued_by="a", issued_to="b")
    assert ledger_count(db) == before
    assert svc.get_status(db, "C56")["status"] == "AVAILABLE"
    assert svc.get_status(db, "TF-1")["status"] == "AVAILABLE"


def test_return_makes_all_items_available_at_location(db):
    res = svc.issue(db, assembly_id="T1", **ISSUE)
    assert all(s["status"] == "IN_USE" for s in statuses(db, res["item_ids"]).values())
    svc.return_items(db, group_id=res["group_id"], to_location="RACK-Z")
    for s in statuses(db, res["item_ids"]).values():
        assert s["status"] == "AVAILABLE"
        assert s["current_location"] == "RACK-Z"
        assert s["wo_id"] is None


def test_reversal_restores_previous_status(db):
    res = svc.issue(db, assembly_id="T2", **ISSUE)
    svc.reverse(db, svc.get_status(db, "C56")["txn_id"])  # group reversal covers every item
    for i, s in statuses(db, res["item_ids"]).items():
        assert s["status"] == "AVAILABLE"
        assert s["current_location"] in ("RACK-A", "PAT-STORE")


def test_reversal_of_return_puts_item_back_in_use(db):
    res = svc.issue(db, item_ids=["c56"], **ISSUE)
    svc.return_items(db, group_id=res["group_id"], to_location="RACK-A")
    svc.reverse(db, svc.get_status(db, "C56")["txn_id"])
    s = svc.get_status(db, "C56")
    assert s["status"] == "IN_USE" and s["wo_id"] == "WO-100"


def test_ids_resolve_to_same_item(db):
    assert svc.normalize_id("c56") == svc.normalize_id("C56") == "C56"
    assert svc.get_status(db, "c56") == svc.get_status(db, "C56")
    with pytest.raises(svc.ValidationError):
        svc.create_item(db, " c56 ", "DIE")  # duplicate after normalisation


# ---- normalisation -----------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("b32-01", "B32-1"), ("  b32-01  ", "B32-1"), ("B32-007", "B32-7"),
    ("B32-00", "B32-0"), ("B32-10", "B32-10"), ("t211", "T211"), ("tf_03", "TF_3"),
])
def test_normalize(raw, want):
    assert svc.normalize_id(raw) == want


def test_normalize_empty_rejected():
    with pytest.raises(svc.ValidationError):
        svc.normalize_id("   ")


# ---- core rules ---------------------------------------------------------------------
def test_ledger_is_append_only(db):
    db.commit()
    with pytest.raises(DatabaseError):
        db.execute(text("UPDATE ledger SET remarks='x' WHERE txn_id=1"))
    db.rollback()
    with pytest.raises(DatabaseError):
        db.execute(text("DELETE FROM ledger"))
    db.rollback()


def test_no_status_columns_on_items():
    cols = {c.name for c in Item.__table__.columns}
    assert not cols & {"status", "current_location", "location"}


def test_system_works_without_flanges(db):
    view = svc.get_assembly_view(db, "T2")
    assert len(view["items"]) == 2 and view["ready_to_issue"]
    res = svc.issue(db, assembly_id="T2", **ISSUE)
    assert sorted(res["item_ids"]) == ["C56", "P13"]


def test_issue_requires_wo_batch_by_to(db):
    for missing in ("wo_id", "batch_no", "issued_by", "issued_to"):
        args = {**ISSUE, missing: " "}
        with pytest.raises(svc.ValidationError):
            svc.issue(db, assembly_id="T1", **args)
    assert ledger_count(db) == 6  # only the initial RECEIVEs


def test_issue_blocked_for_repair_and_scrapped(db):
    svc.send_repair(db, item_ids=["tf-1"])
    with pytest.raises(svc.BlockedError) as e:
        svc.issue(db, assembly_id="T1", **ISSUE)
    assert e.value.item_id == "TF-1" and e.value.status == "UNDER_REPAIR"
    svc.back_from_repair(db, item_ids=["TF-1"], to_location="RACK-B")
    svc.scrap(db, item_ids=["TF-1"])
    with pytest.raises(svc.BlockedError) as e:
        svc.issue(db, assembly_id="T1", **ISSUE)
    assert e.value.status == "SCRAPPED"


def test_issue_by_item_ids_and_open_issues(db):
    res = svc.issue(db, item_ids=["c56", "p13"], **ISSUE)
    (oi,) = svc.open_issues(db)
    assert oi["group_id"] == res["group_id"] and oi["wo_id"] == "WO-100"


def test_transfer_changes_location_only_when_available(db):
    svc.transfer(db, item_ids=["d60"], to_location="RACK-Q")
    assert svc.get_status(db, "D60")["current_location"] == "RACK-Q"
    svc.issue(db, item_ids=["d60"], **ISSUE)
    with pytest.raises(svc.BlockedError):
        svc.transfer(db, item_ids=["D60"], to_location="RACK-R")


def test_cannot_double_reverse_or_reverse_non_latest(db):
    res = svc.issue(db, item_ids=["c56"], **ISSUE)
    issue_txn = svc.get_status(db, "C56")["txn_id"]
    svc.return_items(db, group_id=res["group_id"], to_location="RACK-A")
    with pytest.raises(svc.ValidationError):  # issue is no longer the latest entry
        svc.reverse(db, issue_txn)
    (rev,) = svc.reverse(db, svc.get_status(db, "C56")["txn_id"])
    with pytest.raises(svc.ValidationError):
        svc.reverse(db, rev.txn_id)


def test_assembly_type_mismatch_rejected(db):
    with pytest.raises(svc.ValidationError):
        svc.create_assembly(db, "t9", die_id="p12", pattern_id="p13")
    with pytest.raises(svc.ValidationError):
        svc.create_assembly(db, "t9", die_id="zzz", pattern_id="p13")


def test_failure_midway_through_writes_rolls_back_everything(db, monkeypatch):
    real, calls = svc._append, {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk error")
        return real(*a, **kw)

    monkeypatch.setattr(svc, "_append", flaky)
    before = ledger_count(db)
    with pytest.raises(RuntimeError):
        svc.issue(db, assembly_id="T1", **ISSUE)
    assert ledger_count(db) == before
    assert all(s["status"] == "AVAILABLE" for s in statuses(db, ["C56", "P12", "TF-1", "BF-1"]).values())


# ---- event_id / event reversal ------------------------------------------------------
def reversal_rows(db):
    return db.query(Ledger).filter(Ledger.action == "REVERSAL").all()


def event_rows(db, event_id):
    return db.query(Ledger).filter(Ledger.event_id == event_id).order_by(Ledger.txn_id).all()


def test_every_api_call_gets_one_event_id_and_group_id_stays_the_issue_link(db):
    # initial setup: each create_item call is its own event
    receives = db.query(Ledger).filter(Ledger.action == "RECEIVE").all()
    assert len({r.event_id for r in receives}) == len(receives) == 6

    res = svc.issue(db, assembly_id="T1", **ISSUE)
    rows = event_rows(db, res["event_id"])
    assert len(rows) == 4 and {r.action for r in rows} == {"ISSUE"}
    assert {r.group_id for r in rows} == {res["group_id"]}

    r1 = svc.return_items(db, item_ids=["c56"], to_location="Z")
    r2 = svc.return_items(db, item_ids=["p12"], to_location="Z")
    assert len({res["event_id"], r1["event_id"], r2["event_id"]}) == 3  # new id per call
    rets = db.query(Ledger).filter(Ledger.action == "RETURN").all()
    assert {r.group_id for r in rets} == {res["group_id"]}  # still linked to the issue
    assert {r.event_id for r in rets} == {r1["event_id"], r2["event_id"]}

    t = svc.transfer(db, item_ids=["d60"], to_location="Q")
    assert len(event_rows(db, t["event_id"])) == 1
    assert event_rows(db, t["event_id"])[0].group_id is None


def test_event_id_is_required_on_every_ledger_row(db):
    with pytest.raises(TypeError):
        svc._append(db, "C56", "TRANSFER")  # no event_id -> can't write an untagged row
    assert db.query(Ledger).filter(Ledger.event_id.is_(None)).count() == 0


def test_reversing_one_row_of_an_assembly_issue_reverses_the_whole_event(db):
    res = svc.issue(db, assembly_id="T1", **ISSUE)  # 4 items, one event
    rows = event_rows(db, res["event_id"])
    out = svc.reverse(db, rows[2].txn_id)  # any row of the event, not the first
    assert sorted(r.ref_txn_id for r in out) == sorted(r.txn_id for r in rows)
    assert {r.action for r in out} == {"REVERSAL"}
    assert len({r.event_id for r in out}) == 1 and out[0].event_id != res["event_id"]
    assert {r.group_id for r in out} == {res["group_id"]}  # link back to the issue is kept
    for s in statuses(db, res["item_ids"]).values():
        assert s["status"] == "AVAILABLE" and s["wo_id"] is None
    assert svc.get_assembly_view(db, "T1")["ready_to_issue"]
    with pytest.raises(svc.ValidationError):  # already reversed
        svc.reverse(db, rows[0].txn_id)


def test_reversing_only_the_second_of_two_partial_returns(db):
    res = svc.issue(db, assembly_id="T1", **ISSUE)
    first = svc.return_items(db, item_ids=["c56", "p12"], to_location="RACK-1")
    second = svc.return_items(db, item_ids=["tf-1", "bf-1"], to_location="RACK-2")
    assert first["event_id"] != second["event_id"]
    # same group (same original issue), different events
    assert {r.group_id for r in event_rows(db, first["event_id"])
            + event_rows(db, second["event_id"])} == {res["group_id"]}

    svc.reverse(db, svc.get_status(db, "TF-1")["txn_id"])  # a row of the SECOND return

    # only the second return is undone
    for i in ("TF-1", "BF-1"):
        s = svc.get_status(db, i)
        assert s["status"] == "IN_USE" and s["wo_id"] == "WO-100"
    for i in ("C56", "P12"):
        s = svc.get_status(db, i)
        assert s["status"] == "AVAILABLE" and s["current_location"] == "RACK-1"
    reversed_refs = {r.ref_txn_id for r in reversal_rows(db)}
    assert reversed_refs == {r.txn_id for r in event_rows(db, second["event_id"])}


def test_two_partial_returns_reverse_independently_in_either_order(db):
    svc.issue(db, assembly_id="T1", **ISSUE)
    first = svc.return_items(db, item_ids=["c56", "p12"], to_location="RACK-1")
    svc.return_items(db, item_ids=["tf-1", "bf-1"], to_location="RACK-2")
    # undo the FIRST return while the second stands
    svc.reverse(db, svc.get_status(db, "C56")["txn_id"])
    assert svc.get_status(db, "C56")["status"] == "IN_USE"
    assert svc.get_status(db, "P12")["status"] == "IN_USE"
    assert svc.get_status(db, "TF-1")["status"] == "AVAILABLE"
    assert svc.get_status(db, "TF-1")["current_location"] == "RACK-2"
    assert first["event_id"]


def test_event_reversal_rejected_if_any_row_cannot_be_reversed_and_writes_nothing(db):
    svc.issue(db, assembly_id="T1", **ISSUE)
    issue_txn = svc.get_status(db, "C56")["txn_id"]
    # one item is returned on its own, so its ISSUE row is no longer its latest entry
    svc.return_items(db, item_ids=["TF-1"], to_location="RACK-Z")
    before = ledger_count(db)
    with pytest.raises(svc.ValidationError) as e:
        svc.reverse(db, issue_txn)
    assert "TF-1" in e.value.message and "nothing was reversed" in e.value.message
    assert ledger_count(db) == before and not reversal_rows(db)
    assert svc.get_status(db, "C56")["status"] == "IN_USE"  # untouched
    assert svc.get_status(db, "TF-1")["status"] == "AVAILABLE"


def test_event_reversal_is_atomic_on_midway_failure(db, monkeypatch):
    res = svc.issue(db, assembly_id="T1", **ISSUE)
    txn = svc.get_status(db, "C56")["txn_id"]
    real, calls = svc._append, {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk error")
        return real(*a, **kw)

    monkeypatch.setattr(svc, "_append", flaky)
    before = ledger_count(db)
    with pytest.raises(RuntimeError):
        svc.reverse(db, txn)
    assert ledger_count(db) == before and not reversal_rows(db)
    assert all(s["status"] == "IN_USE" for s in statuses(db, res["item_ids"]).values())


def test_multi_item_transfer_is_one_event_and_reverses_together(db):
    svc.transfer(db, item_ids=["c56", "d60"], to_location="RACK-Q")
    out = svc.reverse(db, svc.get_status(db, "C56")["txn_id"])
    assert len(out) == 2
    assert svc.get_status(db, "C56")["current_location"] == "RACK-A"
    assert svc.get_status(db, "D60")["current_location"] == "RACK-A"


def test_separate_transfer_calls_reverse_independently(db):
    svc.transfer(db, item_ids=["c56"], to_location="RACK-Q")
    svc.transfer(db, item_ids=["d60"], to_location="RACK-Q")
    out = svc.reverse(db, svc.get_status(db, "C56")["txn_id"])
    assert len(out) == 1
    assert svc.get_status(db, "C56")["current_location"] == "RACK-A"
    assert svc.get_status(db, "D60")["current_location"] == "RACK-Q"
