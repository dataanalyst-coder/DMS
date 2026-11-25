from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import sessionmaker

from app.models import AuditLog, BatchLedger
from app.services import batches, config, designs
from app.services.common import transaction
from app.services.errors import ConflictError, PermissionDenied, ValidationError


@pytest.fixture
def stock(db, people, world):
    """A consumable design (litres) and a received batch of 100 L in the first bin."""
    config.create_uom(db, people["admin"], "L", "Litre")
    d = designs.create_design(db, people["eng"], code="coat-1", description="Die coating", category_id=world["cat"],
                              base_uom="L", kind="CONSUMABLE")
    designs.approve_revision(db, people["eng"], designs.revisions(db, d.id)[0].id)
    b = batches.receive_batch(db, people["stores"], design_id=d.id, quantity=100, location_id=world["bins"][0],
                              supplier_lot="LOT-77")
    return {"design": d.id, "batch": b.id, "bin": world["bins"]}


def bal(db, stock):
    return batches.balance(db, stock["batch"])


# ---- receive -----------------------------------------------------------------------------
def test_only_stores_receives_and_only_a_consumable_design(db, people, world, stock):
    for who in ("eng", "prod"):
        with pytest.raises(PermissionDenied):
            batches.receive_batch(db, people[who], design_id=stock["design"], quantity=5, location_id=world["bins"][0])
    with pytest.raises(ValidationError) as e:
        batches.receive_batch(db, people["stores"], design_id=world["die"], quantity=5, location_id=world["bins"][0])
    assert "reusable element" in e.value.message
    for q in (0, -3, "abc"):
        with pytest.raises(ValidationError):
            batches.receive_batch(db, people["stores"], design_id=stock["design"], quantity=q, location_id=world["bins"][0])
    draft = designs.create_design(db, people["eng"], code="coat-2", description="x", category_id=world["cat"],
                                  base_uom="L", kind="CONSUMABLE")
    with pytest.raises(ValidationError) as e:
        batches.receive_batch(db, people["stores"], design_id=draft.id, quantity=5, location_id=world["bins"][0])
    assert "no approved revision" in e.value.message


def test_received_batch_has_a_code_balance_location_and_audit(db, people, stock):
    d = batches.batch_dict(db, batches.get_batch(db, stock["batch"]), detail=True)
    assert d["code"] == f"BATCH-{stock['batch']:06d}" and d["uom"] == "L" and d["supplier_lot"] == "LOT-77"
    assert (d["usable"], d["held"], d["outstanding"], d["consumed"]) == (100, 0, 0, 0)
    assert d["status"] == "AVAILABLE" and d["location"] == "P1/A1/R1/S1/B1"
    assert [h["action"] for h in d["history"]] == ["RECEIVE"] and d["history"][0]["user"] == "stores"
    assert db.query(AuditLog).filter_by(entity_type="batch", action="RECEIVE").count() == 1


# ---- hold / release / adjust ---------------------------------------------------------------
def test_hold_moves_usable_to_held_and_release_needs_engineering(db, people, stock):
    batches.hold_quantity(db, people["tooling"], stock["batch"], 30, reason="suspect lot")
    assert (bal(db, stock)["usable"], bal(db, stock)["held"]) == (70, 30)
    for who in ("stores", "tooling"):
        with pytest.raises(PermissionDenied):
            batches.release_hold(db, people[who], stock["batch"], 10, reason="looks fine")
    batches.release_hold(db, people["eng"], stock["batch"], 10, reason="lab result OK")
    assert (bal(db, stock)["usable"], bal(db, stock)["held"]) == (80, 20)
    with pytest.raises(ValidationError) as e:
        batches.release_hold(db, people["eng"], stock["batch"], 25, reason="too much")
    assert "only 20.0 L is on hold" in e.value.message


def test_balances_never_go_negative_and_reasons_are_required(db, people, stock):
    with pytest.raises(ValidationError) as e:
        batches.hold_quantity(db, people["tooling"], stock["batch"], 101, reason="x")
    assert "negative" in e.value.message
    with pytest.raises(ValidationError):
        batches.adjust(db, people["eng"], stock["batch"], -100.5, reason="count")
    with pytest.raises(ValidationError):
        batches.adjust(db, people["eng"], stock["batch"], -5, reason=" ")
    with pytest.raises(ValidationError):
        batches.adjust(db, people["eng"], stock["batch"], 0, reason="nothing")
    assert bal(db, stock)["usable"] == 100  # nothing changed by the rejected calls


def test_adjustment_is_authorised_only_and_keeps_history(db, people, stock):
    for who in ("stores", "tooling", "mgr"):
        with pytest.raises(PermissionDenied):
            batches.adjust(db, people[who], stock["batch"], -5, reason="count")
    batches.adjust(db, people["eng"], stock["batch"], -12.5, reason="physical count 87.5 L")
    batches.adjust(db, people["eng"], stock["batch"], 2, reason="recount")
    assert bal(db, stock)["usable"] == 89.5
    d = batches.batch_dict(db, batches.get_batch(db, stock["batch"]), detail=True)
    assert [h["action"] for h in d["history"]] == ["ADJUST", "ADJUST", "RECEIVE"]  # history is preserved
    assert d["history"][1]["remarks"] == "physical count 87.5 L"
    assert db.query(AuditLog).filter_by(entity_type="batch", action="ADJUST").count() == 2


# ---- database-level guarantees ------------------------------------------------------------
def test_database_refuses_a_negative_balance_even_if_the_service_is_bypassed(db, people, stock):
    db.add(BatchLedger(batch_id=stock["batch"], batch_seq=99, action="ADJUST", event_id="x", user_id=people["eng"].id,
                       d_usable=-150))
    with pytest.raises(ConflictError):
        with transaction(db):
            db.flush()
    assert bal(db, stock)["usable"] == 100


def test_batch_ledger_is_append_only(db, people, stock):
    db.commit()
    with pytest.raises(DatabaseError):
        db.execute(text("UPDATE batch_ledger SET d_usable = 1000"))
    db.rollback()
    with pytest.raises(DatabaseError):
        db.execute(text("DELETE FROM batch_ledger"))
    db.rollback()
    assert bal(db, stock)["usable"] == 100


def test_stale_writer_collides_instead_of_overdrawing(db, engine, people, stock, monkeypatch):
    stale = batches.balance(db, stock["batch"])  # slow request A already read 100 L usable
    s2 = sessionmaker(bind=engine, autoflush=False, future=True)()
    from app.models import User
    from app.services import auth
    eng2 = auth.load_roles(s2, s2.query(User).filter_by(username="eng").one())
    batches.adjust(s2, eng2, stock["batch"], -90, reason="B wrote first")  # 10 L left
    s2.close()
    monkeypatch.setattr(batches, "balance", lambda d, b: stale)
    with pytest.raises(ConflictError):
        batches.adjust(db, people["eng"], stock["batch"], -80, reason="A acts on a stale balance")
    monkeypatch.undo()
    assert bal(db, stock)["usable"] == 10


# ---- location & status ---------------------------------------------------------------------
def test_move_changes_location_and_reverses(db, people, stock):
    batches.move_batch(db, people["tooling"], stock["batch"], stock["bin"][1], remarks="to shelf 2")
    assert batches.batch_dict(db, batches.get_batch(db, stock["batch"]))["location_id"] == stock["bin"][1]
    with pytest.raises(PermissionDenied):
        batches.move_batch(db, people["prod"], stock["batch"], stock["bin"][0])
    row = db.query(BatchLedger).filter_by(action="MOVE").one()
    batches.reverse(db, people["tooling"], row.txn_id, reason="wrong shelf")
    assert batches.batch_dict(db, batches.get_batch(db, stock["batch"]))["location_id"] == stock["bin"][0]


def test_status_expired_held_exhausted(db, people, stock):
    b = batches.get_batch(db, stock["batch"])
    assert batches.status_of(b, bal(db, stock)) == "AVAILABLE"
    assert batches.status_of(b, bal(db, stock), today=date.today()) == "AVAILABLE"
    b.expiry_date = date.today() - timedelta(days=1)
    assert batches.status_of(b, bal(db, stock)) == "EXPIRED"
    b.expiry_date = None
    batches.hold_quantity(db, people["tooling"], stock["batch"], 100, reason="all suspect")
    assert batches.status_of(b, bal(db, stock)) == "HELD"
    with pytest.raises(ValidationError) as e:
        batches.check_issuable(b, bal(db, stock), 1)
    assert "on hold" in e.value.message
    batches.release_hold(db, people["eng"], stock["batch"], 100, reason="ok")
    batches.adjust(db, people["eng"], stock["batch"], -100, reason="all used")
    assert batches.status_of(b, bal(db, stock)) == "EXHAUSTED"


def test_expired_and_insufficient_batches_are_not_issuable(db, people, stock):
    b = batches.get_batch(db, stock["batch"])
    with pytest.raises(ValidationError) as e:
        batches.check_issuable(b, bal(db, stock), 150)
    assert "insufficient balance, 100.0 L usable but 150.0 L needed" in e.value.message
    b.expiry_date = date.today() - timedelta(days=2)
    with pytest.raises(ValidationError) as e:
        batches.check_issuable(b, bal(db, stock), 1)
    assert "expired" in e.value.message


def test_available_batches_exclude_expired_held_and_empty_and_sort_by_expiry(db, people, world, stock):
    d, loc = stock["design"], world["bins"][0]
    soon = batches.receive_batch(db, people["stores"], design_id=d, quantity=10, location_id=loc,
                                 expiry_date=date.today() + timedelta(days=5))
    batches.receive_batch(db, people["stores"], design_id=d, quantity=10, location_id=loc, expiry_date=date.today() - timedelta(days=1))
    held = batches.receive_batch(db, people["stores"], design_id=d, quantity=10, location_id=loc)
    batches.hold_quantity(db, people["tooling"], held.id, 10, reason="hold all")
    got = batches.available_for_designs(db, [d])
    assert [g["id"] for g in got] == [soon.id, stock["batch"]]  # earliest expiry first; no expiry last; others excluded


# ---- reversal ---------------------------------------------------------------------------
def test_reversing_a_hold_restores_the_balance_and_cannot_be_repeated(db, people, stock):
    batches.hold_quantity(db, people["tooling"], stock["batch"], 40, reason="suspect")
    row = db.query(BatchLedger).filter_by(action="HOLD").one()
    with pytest.raises(PermissionDenied):
        batches.reverse(db, people["prod"], row.txn_id, reason="x")
    with pytest.raises(ValidationError):
        batches.reverse(db, people["tooling"], row.txn_id, reason=" ")
    batches.reverse(db, people["tooling"], row.txn_id, reason="raised on wrong batch")
    assert (bal(db, stock)["usable"], bal(db, stock)["held"]) == (100, 0)
    with pytest.raises(ValidationError):
        batches.reverse(db, people["tooling"], row.txn_id, reason="again")
    rev = db.query(BatchLedger).filter_by(action="REVERSAL").one()
    with pytest.raises(ValidationError):
        batches.reverse(db, people["tooling"], rev.txn_id, reason="undo the undo")


def test_a_receipt_cannot_be_reversed_once_the_batch_has_activity(db, people, stock):
    receipt = db.query(BatchLedger).filter_by(action="RECEIVE").one()
    batches.adjust(db, people["eng"], stock["batch"], -5, reason="count")
    with pytest.raises(ValidationError) as e:
        batches.reverse(db, people["stores"], receipt.txn_id, reason="entered twice")
    assert "other activity" in e.value.message
    # a fresh batch with no activity can have its receipt reversed
    fresh = batches.receive_batch(db, people["stores"], design_id=stock["design"], quantity=7, location_id=stock["bin"][0])
    r2 = db.query(BatchLedger).filter_by(batch_id=fresh.id, action="RECEIVE").one()
    batches.reverse(db, people["stores"], r2.txn_id, reason="entered by mistake")
    assert batches.balance(db, fresh.id)["usable"] == 0
    assert batches.status_of(fresh, batches.balance(db, fresh.id)) == "EXHAUSTED"


def test_reversal_that_would_go_negative_is_refused(db, people, stock):
    batches.hold_quantity(db, people["tooling"], stock["batch"], 100, reason="all on hold")
    hold_row = db.query(BatchLedger).filter_by(action="HOLD").one()
    batches.release_hold(db, people["eng"], stock["batch"], 60, reason="partly ok")
    batches.adjust(db, people["eng"], stock["batch"], -60, reason="used 60")
    # reversing the hold would need 100 usable back from a batch with 0 usable
    with pytest.raises(ValidationError) as e:
        batches.reverse(db, people["tooling"], hold_row.txn_id, reason="undo")
    assert "negative" in e.value.message and "Nothing was reversed" in e.value.message
