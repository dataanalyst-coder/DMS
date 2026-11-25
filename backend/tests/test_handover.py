from datetime import datetime, timedelta

import pytest

from app.models import BuildComponent, Issue, Ledger, Setup
from app.services import auth, batches, builds, issues, ledger, mappings, setups, usage
from app.services.errors import LedgerError, PermissionDenied, ValidationError
from tests.conftest import PW, mk_setup, setup_body
from tests.test_api import api, login  # noqa: F401  (fixtures)
from tests.test_consumable_issue import crig  # noqa: F401  (fixture: a build whose assembly needs 2 L of coating)

KEYS = ("d1", "p1", "t1", "b1")


@pytest.fixture
def rig(db, people, kit, world):
    m = mappings.create_mapping(db, people["eng"], part_id="LTC9", routing_revision="R1", operation_id="OP10",
                                definition_id=kit["asm"], selected=True)
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    for k in KEYS:
        builds.fit_item(db, people["stores"], b.id, kit["items"][k])
    builds.verify_build(db, people["eng"], b.id)
    kit.update(mapping=m.id, build=b.id, ids=[kit["items"][k] for k in KEYS])
    return kit


def issue_and_acknowledge(db, people, rig, wo="WO-1"):
    i = issues.create_issue(db, people["stores"], setup_id=mk_setup(db, people, wo, rig["mapping"]), mapping_id=rig["mapping"],
                            build_id=rig["build"], confirmed_item_ids=rig["ids"])
    issues.acknowledge_issue(db, people["prod"], i.id)
    return i


def hand(db, people, rig, old, wo="WO-2", **kw):
    return issues.hand_over(db, people["stores"], old.id, setup_id=mk_setup(db, people, wo, rig["mapping"]),
                            mapping_id=rig["mapping"], **kw)


def test_the_set_moves_to_the_next_setup_without_going_back_to_stores(db, people, rig):
    a = issue_and_acknowledge(db, people, rig)
    b = hand(db, people, rig, a)
    da, dbb = issues.issue_detail(db, a.id), issues.issue_detail(db, b.id)
    assert da["status"] == "CLOSED" and {l["state"] for l in da["lines"]} == {"HANDED_ON"}
    assert da["handed_over_to"]["id"] == b.id and da["handed_over_at"]
    assert dbb["status"] == "PENDING_HANDOVER" and dbb["wo_id"] == "WO-2" and dbb["setup"]["code"].startswith("SET-")
    assert {l["state"] for l in dbb["lines"]} == {"PENDING_HANDOVER"}
    for i in rig["ids"]:  # still serviceable, never went through return/inspection, never "free stock"
        s = ledger.get_state(db, i)
        assert s["item_condition"] == "SERVICEABLE" and s["custody"] == "PENDING_HANDOVER"
        assert issues.open_issue_for_item(db, i)[0].id == b.id
    assert setups.setup_dict(db, db.get(Setup, a.setup_id))["state"] == "DONE"
    assert setups.setup_dict(db, db.get(Setup, b.setup_id))["state"] == "ISSUED"
    issues.acknowledge_issue(db, people["prod"], b.id)  # Production confirms again, under their own name
    assert {l["state"] for l in issues.issue_detail(db, b.id)["lines"]} == {"IN_PRODUCTION"}
    assert issues.outstanding_issue_ids(db) == [b.id]
    assert db.query(Ledger).filter(Ledger.action == "RETURN").count() == 0


def test_handover_rows_belong_to_the_workflow_and_are_not_reversible_by_hand(db, people, rig):
    a = issue_and_acknowledge(db, people, rig)
    hand(db, people, rig, a)
    row = db.query(Ledger).filter(Ledger.action == "HANDOVER").first()
    with pytest.raises(ValidationError) as e:
        ledger.reverse(db, people["stores"], row.txn_id, reason="oops")
    assert "cannot be reversed by hand" in e.value.message


def test_handover_is_refused_when_it_would_hide_something(db, people, rig, world):
    a = issue_and_acknowledge(db, people, rig)
    for who in ("prod", "tooling", "mgr", "plan"):
        with pytest.raises(PermissionDenied):
            issues.hand_over(db, people[who], a.id, setup_id=mk_setup(db, people, "WO-2", rig["mapping"]), mapping_id=rig["mapping"])
    with pytest.raises(ValidationError) as e:  # the setup it is already issued for
        issues.hand_over(db, people["stores"], a.id, setup_id=a.setup_id, mapping_id=rig["mapping"])
    assert "already issued" in e.value.message
    with pytest.raises(ValidationError):  # an unknown setup / no mapping
        issues.hand_over(db, people["stores"], a.id, setup_id=None, mapping_id=rig["mapping"])
    other = setups.create_setup(db, people["plan"], wo_id="WO-3", part_id="NOPE", tool_description="x")
    with pytest.raises(ValidationError) as e:  # no tooling rule for it
        issues.hand_over(db, people["stores"], a.id, setup_id=other.id, mapping_id=rig["mapping"])
    assert "no tooling assigned" in e.value.message
    nxt = mk_setup(db, people, "WO-4", rig["mapping"])
    issues.return_items(db, people["stores"], a.id, [{"item_id": rig["ids"][0], "to_location_id": world["bins"][1]}])
    with pytest.raises(ValidationError) as e:  # one part already came back: the set is not whole in production
        issues.hand_over(db, people["stores"], a.id, setup_id=nxt, mapping_id=rig["mapping"])
    assert "every part is in production" in e.value.message


def test_not_acknowledged_issue_cannot_be_handed_on(db, people, rig):
    i = issues.create_issue(db, people["stores"], setup_id=mk_setup(db, people, "WO-1", rig["mapping"]), mapping_id=rig["mapping"],
                            build_id=rig["build"], confirmed_item_ids=rig["ids"])
    with pytest.raises(ValidationError) as e:
        hand(db, people, rig, i)
    assert "acknowledged" in e.value.message


def test_the_next_setups_operation_must_belong_to_its_part(db, people, rig):
    a = issue_and_acknowledge(db, people, rig)
    mappings.create_mapping(db, people["eng"], part_id="OTHER1", routing_revision="R1", operation_id="OP10", definition_id=rig["asm"],
                            selected=True)
    wrong = mappings.list_mappings(db, part_id="OTHER1")[1][0]["id"]
    with pytest.raises(ValidationError) as e:
        issues.hand_over(db, people["stores"], a.id, setup_id=mk_setup(db, people, "WO-2", rig["mapping"]), mapping_id=wrong)
    assert "is for part OTHER1" in e.value.message


def test_coating_of_the_first_setup_is_settled_and_the_next_one_issued_together(db, people, crig, world):  # noqa: F811
    first = issues.create_issue(db, people["stores"], setup_id=mk_setup(db, people, "WO-1", crig["mapping"]), mapping_id=crig["mapping"],
                                build_id=crig["build"], confirmed_item_ids=crig["ids"],
                                consumables=[{"line_id": crig["line"], "batch_id": crig["batch"], "quantity": 2}])
    issues.acknowledge_issue(db, people["prod"], first.id)
    nxt = mk_setup(db, people, "WO-2", crig["mapping"])
    picks = [{"line_id": crig["line"], "batch_id": crig["batch"], "quantity": 2}]
    with pytest.raises(ValidationError) as e:  # the previous coating must be settled first
        issues.hand_over(db, people["stores"], first.id, setup_id=nxt, mapping_id=crig["mapping"], consumables=picks)
    assert "unused quantity" in e.value.message and "BATCH-" in e.value.message
    with pytest.raises(ValidationError):  # and the new setup needs its own coating
        issues.hand_over(db, people["stores"], first.id, setup_id=nxt, mapping_id=crig["mapping"],
                         consumable_returns=[{"batch_id": crig["batch"], "unused_quantity": 1}])
    assert batches.balance(db, crig["batch"])["usable"] == 98  # nothing changed by the refusals
    second = issues.hand_over(db, people["stores"], first.id, setup_id=nxt, mapping_id=crig["mapping"], consumables=picks,
                              consumable_returns=[{"batch_id": crig["batch"], "unused_quantity": 1.5}])
    bal = batches.balance(db, crig["batch"])
    assert bal["usable"] == 97.5 and bal["consumed"] == 0.5 and bal["outstanding"] == 2  # 100 - 0.5 consumed - 2 now out
    d1, d2 = issues.issue_detail(db, first.id), issues.issue_detail(db, second.id)
    assert d1["status"] == "CLOSED" and d1["consumables"][0]["state"] == "RETURNED" and d1["consumables"][0]["unused"] == 1.5
    assert d2["consumables"][0]["state"] == "ISSUED" and d2["consumables"][0]["quantity"] == 2


def test_usage_after_the_handover_goes_to_the_new_setup_only(db, people, rig):
    oms = auth.create_user(db, people["admin"], "oms", "OMS", PW, ["INTEGRATION"])
    a = issue_and_acknowledge(db, people, rig, "WO-1")
    b = hand(db, people, rig, a, "WO-2")
    now = datetime.utcnow()
    db.query(Issue).filter(Issue.id == a.id).update({"issued_at": now - timedelta(hours=5), "handed_over_at": now - timedelta(hours=1)})
    db.query(Issue).filter(Issue.id == b.id).update({"issued_at": now - timedelta(hours=1)})
    db.query(BuildComponent).update({"fitted_at": now - timedelta(hours=6)})
    db.commit()

    def event(sid, wo, ago):
        return usage.ingest(db, oms, {"source_event_id": sid, "wo_id": wo, "operation_id": "OP10", "build": f"BLD-{rig['build']:06d}",
                                      "production_end": (now - timedelta(minutes=ago)).isoformat(), "cycles": 5, "castings": 10})
    assert event("E1", "WO-1", 180)["result"] == "POSTED"  # while it was WO-1's
    late = event("E2", "WO-1", 10)  # after it moved on: not WO-1's any more
    assert late["result"] == "PENDING" and late["event"]["reason_code"] == "NO_ISSUE"
    assert event("E3", "WO-2", 10)["result"] == "POSTED"
    assert usage.assembly_usage(db, wo_id="WO-1")["castings"] == 10 and usage.assembly_usage(db, wo_id="WO-2")["castings"] == 10


def test_handover_over_http(api, login, rig):
    m = rig["mapping"]
    first = api.post("/issues", json={**setup_body(api, login, "WO-1", m), "mapping_id": m, "build_id": rig["build"], "confirmed_item_ids": rig["ids"]},
                     headers=login("stores")).json()
    path = f"/issues/{first['id']}/hand-over"
    body = {**setup_body(api, login, "WO-2", m), "mapping_id": m}
    assert api.post(path, json=body).status_code == 401
    assert api.post(path, json=body, headers=login("stores")).status_code == 422  # not acknowledged yet
    assert api.post(f"/issues/{first['id']}/acknowledge", headers=login("prod")).status_code == 200
    assert api.post(path, json=body, headers=login("prod")).status_code == 403
    r = api.post(path, json=body, headers=login("stores"))
    assert r.status_code == 201, r.text
    assert r.json()["from"]["status"] == "CLOSED" and r.json()["to"]["wo_id"] == "WO-2" and r.json()["to"]["status"] == "PENDING_HANDOVER"
    t = api.get(f"/setups/{r.json()['to']['setup']['id']}", headers=login("prod")).json()["tooling"]
    assert t["builds"][0]["status"] == "ISSUED" and t["builds"][0]["open_issue"]["wo_id"] == "WO-2"
