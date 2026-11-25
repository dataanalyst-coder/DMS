import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import sessionmaker

from app.models import AuditLog, Ledger
from app.services import audit, ledger
from app.services.errors import ConflictError, PermissionDenied, ValidationError
from tests.conftest import clear_items


def state(db, item):
    s = ledger.get_state(db, item.id)
    return s["item_condition"], s["custody"], s["location_id"]


def rows(db, event_id):
    return db.query(Ledger).filter(Ledger.event_id == event_id).order_by(Ledger.txn_id).all()


def count(db):
    return db.query(Ledger).count()


# ---- append-only & derived state ------------------------------------------------------
def test_ledger_and_audit_are_append_only(db, people, make_item):
    make_item()
    db.commit()
    for table, upd in (("ledger", "UPDATE ledger SET remarks='x'"), ("audit_log", "UPDATE audit_log SET reason='x'")):
        with pytest.raises(DatabaseError):
            db.execute(text(upd))
        db.rollback()
        with pytest.raises(DatabaseError):
            db.execute(text(f"DELETE FROM {table}"))
        db.rollback()
    assert count(db) == 1 and db.query(AuditLog).count() > 0


def test_state_comes_from_the_latest_ledger_entry(db, people, world, make_item):
    it = make_item(condition="SERVICEABLE", bin_index=0)
    assert state(db, it) == ("SERVICEABLE", "STORES", world["bins"][0])
    ledger.move_items(db, people["stores"], [it.id], world["bins"][1])
    assert state(db, it) == ("SERVICEABLE", "STORES", world["bins"][1])
    ledger.set_condition(db, people["tooling"], [it.id], "HOLD", reason="crack at flange seat")
    assert state(db, it) == ("HOLD", "STORES", world["bins"][1])


# ---- location change never clears a hold --------------------------------------------
def test_moving_a_held_item_never_clears_the_hold(db, people, world, make_item):
    it = make_item()
    ledger.set_condition(db, people["tooling"], [it.id], "HOLD", reason="suspect crack")
    ledger.move_items(db, people["stores"], [it.id], world["bins"][2], remarks="to quarantine bin")
    cond, custody, loc = state(db, it)
    assert (cond, loc) == ("HOLD", world["bins"][2])
    last = db.query(Ledger).order_by(Ledger.txn_id.desc()).first()
    assert last.action == "MOVE" and last.from_condition == last.to_condition == "HOLD"


def test_move_records_who_and_when_and_needs_permission(db, people, world, make_item):
    it = make_item()
    with pytest.raises(PermissionDenied):
        ledger.move_items(db, people["prod"], [it.id], world["bins"][1])
    res = ledger.move_items(db, people["multi"], [it.id], world["bins"][1])
    (r,) = rows(db, res["event_id"])
    assert r.user_id == people["multi"].id and r.txn_time is not None
    assert r.from_location_id == world["bins"][0] and r.to_location_id == world["bins"][1]
    with pytest.raises(ValidationError):
        ledger.move_items(db, people["stores"], [], world["bins"][1])  # nothing to move


# ---- condition transitions & who may do them ----------------------------------------
def test_hold_release_needs_engineering_and_admin_cannot_bypass(db, people, make_item):
    it = make_item()
    ledger.set_condition(db, people["stores"], [it.id], "HOLD", reason="found damaged")
    with pytest.raises(ValidationError):  # a hold never jumps straight to serviceable, not even for Engineering
        ledger.set_condition(db, people["eng"], [it.id], "SERVICEABLE", reason="looks fine")
    ledger.set_condition(db, people["tooling"], [it.id], "AWAITING_INSPECTION", reason="further inspection")
    for who in ("stores", "tooling", "scrap", "mgr"):
        with pytest.raises(PermissionDenied):
            ledger.set_condition(db, people[who], [it.id], "SERVICEABLE", reason="looks fine")
    with pytest.raises(ValidationError) as e:  # Engineering releases only on the evidence of a passing inspection
        ledger.set_condition(db, people["eng"], [it.id], "SERVICEABLE", reason="looks fine")
    assert "no passing inspection" in e.value.message
    assert state(db, it)[0] == "AWAITING_INSPECTION"
    clear_items(db, people, it.id)
    assert state(db, it)[0] == "SERVICEABLE"


def test_full_repair_cycle_and_custody(db, people, make_item):
    it = make_item()
    ledger.set_condition(db, people["tooling"], [it.id], "HOLD", reason="worn bore")
    ledger.set_condition(db, people["tooling"], [it.id], "UNDER_REPAIR", reason="re-bore")
    assert state(db, it)[:2] == ("UNDER_REPAIR", "REPAIR")
    ledger.set_condition(db, people["tooling"], [it.id], "AWAITING_INSPECTION", reason="repair done")
    assert state(db, it)[:2] == ("AWAITING_INSPECTION", "STORES")
    clear_items(db, people, it.id)  # inspection passed, then Engineering's release
    assert state(db, it)[:2] == ("SERVICEABLE", "STORES")


def test_scrap_needs_the_scrap_approver_and_a_held_item(db, people, make_item):
    it = make_item()
    with pytest.raises(ValidationError):  # cannot jump straight from serviceable to scrap
        ledger.set_condition(db, people["scrap"], [it.id], "SCRAPPED", reason="x")
    ledger.set_condition(db, people["tooling"], [it.id], "HOLD", reason="beyond repair, propose scrap")
    for who in ("eng", "tooling", "stores"):
        with pytest.raises(PermissionDenied):
            ledger.set_condition(db, people[who], [it.id], "SCRAPPED", reason="scrap")
    assert state(db, it)[0] == "HOLD"  # remains held pending approval
    ledger.set_condition(db, people["scrap"], [it.id], "SCRAPPED", reason="approved scrap")
    assert state(db, it)[0] == "SCRAPPED"
    with pytest.raises(ValidationError):
        ledger.set_condition(db, people["eng"], [it.id], "SERVICEABLE", reason="resurrect")


def test_condition_change_needs_a_reason_and_valid_transition(db, people, make_item):
    it = make_item()
    with pytest.raises(ValidationError):
        ledger.set_condition(db, people["tooling"], [it.id], "HOLD", reason="  ")
    with pytest.raises(ValidationError):
        ledger.set_condition(db, people["eng"], [it.id], "SERVICEABLE", reason="already serviceable")
    with pytest.raises(ValidationError):
        ledger.set_condition(db, people["tooling"], [it.id], "BROKEN", reason="x")


def test_multi_item_condition_change_is_all_or_nothing(db, people, world, make_item):
    a, b = make_item(legacy="a1"), make_item(legacy="b1", bin_index=1, condition="HOLD")
    before = count(db)
    with pytest.raises(ValidationError):  # b is already on hold: HOLD -> HOLD not allowed
        ledger.set_condition(db, people["tooling"], [a.id, b.id], "HOLD", reason="batch hold")
    assert count(db) == before and state(db, a)[0] == "SERVICEABLE"


def test_condition_changes_are_audited(db, people, make_item):
    it = make_item()
    ledger.set_condition(db, people["tooling"], [it.id], "HOLD", reason="crack")
    _, rows_ = audit.list_audit(db, entity_type="item", entity_id=it.id, action="SET_CONDITION")
    assert len(rows_) == 1 and "HOLD" in rows_[0].after and rows_[0].reason == "crack"


# ---- events and event-based reversal ------------------------------------------------
def test_one_event_id_per_call_shared_by_all_its_rows(db, people, world, make_item):
    a, b, c = (make_item(legacy=f"x{i}", bin_index=0) for i in (1, 2, 3))
    e1 = ledger.move_items(db, people["stores"], [a.id, b.id], world["bins"][1])["event_id"]
    e2 = ledger.move_items(db, people["stores"], [c.id], world["bins"][1])["event_id"]
    assert e1 != e2 and len(rows(db, e1)) == 2 and len(rows(db, e2)) == 1


def test_reversal_restores_previous_state_and_reverses_whole_event(db, people, world, make_item):
    a, b = make_item(legacy="a1"), make_item(legacy="b1", bin_index=1)
    ev = ledger.move_items(db, people["stores"], [a.id, b.id], world["bins"][2], remarks="oops")["event_id"]
    pick = rows(db, ev)[1]
    out = ledger.reverse(db, people["stores"], pick.txn_id, reason="wrong bin scanned")
    assert sorted(r.ref_txn_id for r in out) == sorted(r.txn_id for r in rows(db, ev))
    assert len({r.event_id for r in out}) == 1 and out[0].event_id != ev
    assert state(db, a) == ("SERVICEABLE", "STORES", world["bins"][0])
    assert state(db, b) == ("SERVICEABLE", "STORES", world["bins"][1])
    with pytest.raises(ValidationError):  # already reversed
        ledger.reverse(db, people["stores"], rows(db, ev)[0].txn_id, reason="again")


def test_reversal_needs_reason_and_original_actions_permission(db, people, world, make_item):
    it = make_item()
    ev = ledger.set_condition(db, people["tooling"], [it.id], "HOLD", reason="crack")["event_id"]
    txn = rows(db, ev)[0].txn_id
    with pytest.raises(ValidationError):
        ledger.reverse(db, people["tooling"], txn, reason=" ")
    with pytest.raises(PermissionDenied):
        ledger.reverse(db, people["prod"], txn, reason="mistake")
    ledger.reverse(db, people["tooling"], txn, reason="raised on wrong item")
    assert state(db, it)[0] == "SERVICEABLE"


def test_reversing_a_reversal_or_first_entry_is_refused(db, people, make_item):
    it = make_item()
    first = db.query(Ledger).first()
    with pytest.raises(ValidationError) as e:
        ledger.reverse(db, people["stores"], first.txn_id, reason="undo registration")
    assert "first entry" in e.value.message
    ev = ledger.set_condition(db, people["tooling"], [it.id], "HOLD", reason="crack")["event_id"]
    (rev,) = ledger.reverse(db, people["tooling"], rows(db, ev)[0].txn_id, reason="oops")
    with pytest.raises(ValidationError):
        ledger.reverse(db, people["tooling"], rev.txn_id, reason="undo the undo")


def test_event_reversal_rejected_if_any_row_was_superseded_and_writes_nothing(db, people, world, make_item):
    a, b = make_item(legacy="a1"), make_item(legacy="b1", bin_index=1)
    ev = ledger.move_items(db, people["stores"], [a.id, b.id], world["bins"][2])["event_id"]
    ledger.move_items(db, people["stores"], [b.id], world["bins"][0])  # b has moved again since
    before = count(db)
    with pytest.raises(ValidationError) as e:
        ledger.reverse(db, people["stores"], rows(db, ev)[0].txn_id, reason="undo")
    assert "nothing was reversed" in e.value.message
    assert count(db) == before and state(db, a)[2] == world["bins"][2]


def test_event_reversal_is_atomic_on_midway_failure(db, people, world, make_item, monkeypatch):
    a, b, c = (make_item(legacy=f"m{i}") for i in (1, 2, 3))
    ev = ledger.move_items(db, people["stores"], [a.id, b.id, c.id], world["bins"][2])["event_id"]
    real = audit.record
    calls = {"n": 0}

    def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk error")
        return real(*args, **kw)

    monkeypatch.setattr(audit, "record", flaky)
    before = count(db)
    with pytest.raises(RuntimeError):
        ledger.reverse(db, people["stores"], rows(db, ev)[0].txn_id, reason="undo")
    assert count(db) == before
    assert all(state(db, i)[2] == world["bins"][2] for i in (a, b, c))


# ---- concurrency ------------------------------------------------------------------
def test_stale_writer_conflicts_instead_of_both_succeeding(db, engine, people, world, make_item):
    it = make_item()
    stale = ledger.get_state(db, it.id)  # writer A reads the state ...
    other = sessionmaker(bind=engine, autoflush=False, future=True)()
    ledger.move_items(other, people_in(other, "stores"), [it.id], world["bins"][1])  # ... B writes first
    other.close()
    row = ledger._append(db, it.id, "MOVE", event_id="e-stale", prev=stale, user=people["stores"],
                         to_condition="SERVICEABLE", to_custody="STORES", to_location_id=world["bins"][2])
    from app.services.common import transaction
    with pytest.raises(ConflictError):
        with transaction(db):
            db.flush()
    assert row.item_seq == stale["last_seq"] + 1
    assert state(db, it)[2] == world["bins"][1]  # only B's write exists


def people_in(session, username):
    from app.models import User
    from app.services import auth
    return auth.load_roles(session, session.query(User).filter(User.username == username).one())


def test_a_txn_can_be_reversed_only_once_even_if_raced(db, engine, people, world, make_item):
    it = make_item()
    ev = ledger.set_condition(db, people["tooling"], [it.id], "HOLD", reason="crack")["event_id"]
    txn = rows(db, ev)[0].txn_id
    ledger.reverse(db, people["tooling"], txn, reason="first")
    db.add(Ledger(item_id=it.id, action="REVERSAL", event_id="dup", user_id=people["tooling"].id,
                  ref_txn_id=txn, item_seq=99, to_condition="SERVICEABLE", to_custody="STORES"))
    with pytest.raises(Exception):
        db.commit()
    db.rollback()
