import json
from datetime import datetime, timedelta

import pytest

from app.models import (AuditLog, BuildComponent, Issue, MeMapping, UsageEvent, UsageExposure)
from app.services import auth, builds, issues, items, ledger, mappings, usage
from app.services.errors import ConflictError, PermissionDenied, ValidationError
from tests.conftest import PW, mk_setup, setup_body

KEYS = ("d1", "p1", "t1", "b1")
BASE = datetime.utcnow() - timedelta(days=1)


@pytest.fixture
def urig(db, people, kit, world):
    """A verified build issued to WO-1 (option weight 25 kg, ME revision W1), with an OMS service account."""
    m = mappings.create_mapping(db, people["eng"], part_id="PN-1", routing_revision="R1", operation_id="OP10",
                                definition_id=kit["asm"], selected=True, pouring_weight_kg=25, pouring_weight_revision="W1")
    b = builds.create_build(db, people["stores"], definition_id=kit["asm"], location_id=world["bins"][0])
    for k in KEYS:
        builds.fit_item(db, people["stores"], b.id, kit["items"][k])
    builds.verify_build(db, people["eng"], b.id)
    ids = [kit["items"][k] for k in KEYS]
    iss = issues.create_issue(db, people["stores"], setup_id=mk_setup(db, people, "WO-1", m.id), wo_id="WO-1", mapping_id=m.id, build_id=b.id, confirmed_item_ids=ids)
    issues.acknowledge_issue(db, people["prod"], iss.id)
    # move the clock back so production times can be placed deliberately around membership changes
    db.query(Issue).filter_by(id=iss.id).update({"issued_at": BASE})
    db.query(BuildComponent).update({"fitted_at": BASE - timedelta(hours=1)})
    db.commit()
    oms = auth.create_user(db, people["admin"], "oms", "OMS feed", PW, ["INTEGRATION"])
    kit.update(mapping=m.id, build=b.id, issue=iss.id, ids=ids, oms=oms)
    return kit


def ev(**kw):
    base = {"source_event_id": "EV-1", "version": 1, "wo_id": "WO-1", "operation_id": "OP10", "build": "BLD-000001",
            "production_start": (BASE + timedelta(minutes=5)).isoformat(), "production_end": (BASE + timedelta(minutes=5)).isoformat(),
            "cycles": 10, "castings": 20}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def post(db, rig, **kw):
    return usage.ingest(db, rig["oms"], ev(**kw))


def totals(db):
    return usage.assembly_usage(db)


# ---- A12: cycles, castings, tonnage; replay; correction; later weight change -------------------------------
def test_ten_cycles_twenty_castings_at_25kg_is_half_a_tonne_on_the_assembly_and_every_element(db, people, urig):
    r = post(db, urig)
    e = r["event"]
    assert r["result"] == "POSTED" and (e["cycles"], e["castings"], e["weight_kg"], e["tonnage"]) == (10, 20, 25.0, 0.5)
    assert (e["weight_uom"], e["weight_revision"], e["weight_source"]) == ("kg", "W1", "ME_MAPPING")
    assert totals(db)["cycles"] == 10 and totals(db)["castings"] == 20 and totals(db)["tonnage"] == 0.5
    assert len(e["elements"]) == 4
    for item_id in urig["ids"]:
        u = usage.item_usage(db, item_id)
        assert (u["recorded_cycles"], u["recorded_tonnage"]) == (10, 0.5)


def test_replay_has_no_extra_effect(db, urig):
    first = post(db, urig)
    rows_before = (db.query(UsageEvent).count(), db.query(UsageExposure).count())
    again = post(db, urig)
    assert again["result"] == "DUPLICATE" and again["event"]["id"] == first["event"]["id"]
    assert (db.query(UsageEvent).count(), db.query(UsageExposure).count()) == rows_before
    assert totals(db)["tonnage"] == 0.5


def test_a_correction_adjusts_exactly_once(db, urig):
    post(db, urig)
    corrected = post(db, urig, version=2, castings=18)
    assert corrected["result"] == "POSTED" and corrected["event"]["tonnage"] == 0.45
    assert totals(db) == {"events": 1, "cycles": 10, "castings": 18, "tonnage": 0.45, "events_with_unknown_tonnage": 0}
    assert post(db, urig, version=2, castings=18)["result"] == "DUPLICATE"  # replaying the correction does nothing more
    assert totals(db)["tonnage"] == 0.45
    old = usage.list_events(db, source_event_id="EV-1")[1]
    assert {(x["version"], x["status"]) for x in old} == {(1, "SUPERSEDED"), (2, "POSTED")}  # nothing deleted


def test_a_later_me_weight_change_leaves_history_intact(db, people, urig):
    post(db, urig)
    mappings.update_mapping(db, people["eng"], urig["mapping"], fields_set={"pouring_weight_kg", "pouring_weight_revision"},
                            pouring_weight_kg=30, pouring_weight_revision="W2")
    assert totals(db)["tonnage"] == 0.5  # unchanged: the weight is snapshotted on the event
    second = post(db, urig, source_event_id="EV-2")["event"]
    assert second["tonnage"] == 0.6 and second["weight_revision"] == "W2"
    assert totals(db)["tonnage"] == 1.1
    first = usage.list_events(db, source_event_id="EV-1")[1][0]
    assert first["weight_kg"] == 25.0 and first["weight_revision"] == "W1"


def test_payload_weight_overrides_and_unknown_weight_stays_unknown(db, people, urig):
    e = post(db, urig, pouring_weight_kg=26.5, pouring_weight_revision="W9")["event"]
    assert (e["weight_source"], e["tonnage"]) == ("PAYLOAD", 0.53) and e["weight_revision"] == "W9"
    db.query(MeMapping).update({"pouring_weight_kg": None, "pouring_weight_revision": None})
    db.commit()
    u = post(db, urig, source_event_id="EV-2")["event"]
    assert u["tonnage"] is None and u["weight_source"] is None and u["status"] == "POSTED"  # unknown, not zero
    assert totals(db)["events_with_unknown_tonnage"] == 1 and totals(db)["tonnage"] == 0.53
    item = usage.item_usage(db, urig["ids"][0])
    assert item["events_with_unknown_tonnage"] == 1 and item["total_tonnage"] is None


def test_zero_castings_is_a_real_zero_and_is_not_unknown(db, urig):
    e = post(db, urig, castings=0)["event"]
    assert e["status"] == "POSTED" and e["tonnage"] == 0.0 and e["castings"] == 0


def test_foundry_output_counts_assembly_events_only_never_component_tonnage(db, urig):
    post(db, urig)
    post(db, urig, source_event_id="EV-2", castings=10)
    elements = sum(x.tonnage for x in db.query(UsageExposure).filter_by(kind="ELEMENT"))
    assert elements == 4 * (0.5 + 0.25)  # each element is exposed to the same metal
    assert usage.foundry_output(db)["tonnage"] == 0.75  # the real output: component exposure is not added up
    assert usage.item_usage(db, urig["ids"][0])["recorded_tonnage"] == 0.75


def test_unknown_opening_usage_is_never_presented_as_zero(db, people, world, urig):
    post(db, urig)
    u = usage.item_usage(db, urig["ids"][0])
    assert u["opening_cycles"] is None and u["total_cycles"] is None and u["recorded_cycles"] == 10
    items.update_item(db, people["stores"], urig["ids"][0], opening_cycles=500, opening_tonnage=12.5,
                      fields_set={"opening_cycles", "opening_tonnage"})
    u = usage.item_usage(db, urig["ids"][0])
    assert u["total_cycles"] == 510 and u["total_tonnage"] == 13.0


# ---- ordering, cancellation, conflicts -------------------------------------------------------------------------
def test_an_older_version_arriving_late_is_ignored(db, urig):
    post(db, urig, version=2, castings=18)
    late = post(db, urig, version=1, castings=20)
    assert late["result"] == "STALE" and late["event"]["status"] == "SUPERSEDED"
    assert totals(db)["castings"] == 18


def test_cancellation_withdraws_the_effect_and_can_be_followed_by_a_new_version(db, urig):
    post(db, urig)
    assert post(db, urig, version=2, cancelled=True)["event"]["status"] == "CANCELLED"
    assert totals(db)["events"] == 0
    assert post(db, urig, version=3, castings=4)["event"]["status"] == "POSTED"
    assert totals(db)["castings"] == 4


def test_same_id_and_version_with_different_content_is_refused_and_audited(db, urig):
    post(db, urig)
    with pytest.raises(ConflictError):
        post(db, urig, castings=99)
    assert totals(db)["castings"] == 20
    assert db.query(AuditLog).filter_by(action="REJECT_USAGE").count() == 1


# ---- never guessed: queue for reconciliation -------------------------------------------------------------------
def test_missing_build_association_is_queued_not_guessed(db, urig):
    r = post(db, urig, build=None)
    e = r["event"]
    assert r["result"] == "PENDING" and e["reason_code"] == "MISSING_BUILD" and e["build"] is None
    assert totals(db)["events"] == 0  # not silently assigned to the current build
    q = usage.exceptions(db)
    assert q["pending"] == 1 and q["events"][0]["reason"].startswith("No build association") and q["last_successful_sync"] is None


def test_missing_castings_or_cycles_are_queued_not_taken_from_the_plan(db, urig):
    assert post(db, urig, source_event_id="A", castings=None)["event"]["reason_code"] == "MISSING_CASTINGS"
    assert post(db, urig, source_event_id="B", cycles=None)["event"]["reason_code"] == "MISSING_CYCLES"
    assert post(db, urig, source_event_id="C", production_end=None, production_start=None)["event"]["reason_code"] == "MISSING_TIME"
    assert totals(db)["events"] == 0


def test_reconciliation_enters_the_missing_value_with_a_reason_and_keeps_the_original(db, people, urig):
    q = post(db, urig, build=None)["event"]
    for who in ("stores", "mgr"):
        with pytest.raises(PermissionDenied):
            usage.resolve(db, people[who], q["id"], build="BLD-000001", reason="the line supervisor confirmed it")
    with pytest.raises(ValidationError):
        usage.resolve(db, people["prod"], q["id"], build="BLD-000001", reason=" ")
    with pytest.raises(ValidationError):
        usage.resolve(db, people["prod"], q["id"], reason="nothing entered")
    done = usage.resolve(db, people["prod"], q["id"], build="BLD-000001", reason="line supervisor confirmed the build")
    assert done["status"] == "POSTED" and done["build"] == "BLD-000001" and done["tonnage"] == 0.5
    assert done["overrides"] == {"build": "BLD-000001"} and "supervisor" in done["resolution_note"]
    assert post(db, urig, build=None)["result"] == "DUPLICATE"  # the original message is kept exactly as received
    assert db.query(AuditLog).filter_by(action="USAGE_RESOLVE").one().username == "prod"
    with pytest.raises(ValidationError):  # once posted it cannot be 'resolved' again
        usage.resolve(db, people["prod"], q["id"], build="BLD-000001", reason="again")


def test_event_for_a_wo_that_had_no_issue_waits_and_posts_on_retry(db, people, urig):
    e = post(db, urig, wo_id="WO-9")["event"]
    assert e["reason_code"] == "NO_ISSUE" and e["status"] == "PENDING" and e["retry_count"] == 0
    again = usage.retry(db, people["eng"], e["id"])  # still no issue: stays queued, retry counted
    assert again["status"] == "PENDING" and again["retry_count"] == 1 and again["last_attempt_at"]
    # production records the missing association (the issue exists now) and the retry succeeds
    db.query(Issue).update({"wo_id": "WO-9"})
    db.commit()
    done = usage.retry(db, people["prod"], e["id"])
    assert done["status"] == "POSTED" and done["retry_count"] == 2
    with pytest.raises(PermissionDenied):
        usage.retry(db, people["stores"], e["id"])
    with pytest.raises(ValidationError):
        usage.retry(db, people["eng"], e["id"])  # posted events cannot be retried


def test_unknown_build_and_event_before_the_issue_are_queued(db, urig):
    assert post(db, urig, source_event_id="A", build="BLD-000777")["event"]["reason_code"] == "UNKNOWN_BUILD"
    early = (BASE - timedelta(hours=3)).isoformat()
    e = post(db, urig, source_event_id="B", production_start=early, production_end=early)["event"]
    assert e["reason_code"] == "NO_ISSUE"  # the build was not issued to that WO yet at that time


def test_bad_data_is_rejected_with_a_reason_and_an_audit_record(db, urig):
    future = (datetime.utcnow() + timedelta(hours=2)).isoformat()
    cases = {"A": dict(castings=-1), "B": dict(castings=2.5), "C": dict(cycles="ten"), "D": dict(production_end=future, production_start=future),
             "E": dict(production_end="not-a-date", production_start=None), "F": dict(pouring_weight_kg=0)}
    codes = {k: post(db, urig, source_event_id=k, **v)["event"]["reason_code"] for k, v in cases.items()}
    assert codes == {"A": "INVALID_NUMBER", "B": "INVALID_NUMBER", "C": "INVALID_NUMBER", "D": "FUTURE_TIME",
                     "E": "INVALID_TIME", "F": "INVALID_NUMBER"}
    ex = usage.exceptions(db)
    assert ex["rejected"] == 6 and all(e["status"] == "REJECTED" for e in ex["events"])
    assert db.query(AuditLog).filter_by(action="USAGE_REJECTED").count() == 6
    assert totals(db)["events"] == 0


def test_malformed_input_leaves_an_audit_record_and_an_error(db, urig):
    for bad in ({}, {"source_event_id": " "}, {"source_event_id": "X", "version": 0}, {"source_event_id": "X", "version": "two"}):
        with pytest.raises(ValidationError):
            usage.ingest(db, urig["oms"], bad)
    assert db.query(AuditLog).filter_by(action="REJECT_USAGE").count() == 4


def test_only_the_feed_or_engineering_can_ingest(db, people, urig):
    for who in ("stores", "prod", "mgr", "tooling"):
        with pytest.raises(PermissionDenied):
            usage.ingest(db, people[who], ev())
    assert usage.ingest(db, people["eng"], ev())["result"] == "POSTED"  # manual stand-in


# ---- A13: replace a component during production; attribute usage before and after ---------------------------------
def test_usage_before_and_after_a_replacement_goes_to_the_right_items(db, people, urig):
    old, new_item = urig["items"]["t1"], urig["items"]["b1"]
    spare = items.register_item(db, people["stores"], design_id=urig["tf"], legacy_no="t2", condition="SERVICEABLE",
                                location_id=1)  # a second top flange in stores
    res = issues.replace_in_production(db, people["stores"], urig["issue"], old, spare.id, reason="top flange worn through")
    assert res["outgoing"] == "T1" and res["incoming"] == "T2"
    t_swap = BASE + timedelta(minutes=10)
    db.query(BuildComponent).filter_by(item_id=old).update({"removed_at": t_swap})
    db.query(BuildComponent).filter_by(item_id=spare.id).update({"fitted_at": t_swap})
    db.commit()
    post(db, urig, source_event_id="BEFORE", production_start=(BASE + timedelta(minutes=5)).isoformat(),
         production_end=(BASE + timedelta(minutes=5)).isoformat(), cycles=10, castings=20)
    post(db, urig, source_event_id="AFTER", production_start=(BASE + timedelta(minutes=15)).isoformat(),
         production_end=(BASE + timedelta(minutes=15)).isoformat(), cycles=6, castings=12)
    assert usage.item_usage(db, old)["recorded_cycles"] == 10 and usage.item_usage(db, spare.id)["recorded_cycles"] == 6
    assert usage.item_usage(db, new_item)["recorded_cycles"] == 16  # a part that stayed saw both runs
    assert [e["wo_id"] for e in usage.item_usage(db, spare.id)["events"]] == ["WO-1"]
    assert totals(db)["cycles"] == 16


def test_an_event_spanning_a_membership_change_must_be_split_by_oms(db, people, urig):
    old = urig["items"]["t1"]
    spare = items.register_item(db, people["stores"], design_id=urig["tf"], legacy_no="t2", condition="SERVICEABLE", location_id=1)
    issues.replace_in_production(db, people["stores"], urig["issue"], old, spare.id, reason="worn")
    t_swap = BASE + timedelta(minutes=10)
    db.query(BuildComponent).filter_by(item_id=old).update({"removed_at": t_swap})
    db.query(BuildComponent).filter_by(item_id=spare.id).update({"fitted_at": t_swap})
    db.commit()
    e = post(db, urig, production_start=(BASE + timedelta(minutes=5)).isoformat(),
             production_end=(BASE + timedelta(minutes=15)).isoformat())["event"]
    assert e["status"] == "PENDING" and e["reason_code"] == "SPLIT_REQUIRED" and totals(db)["events"] == 0
    # OMS resubmits as two events, one each side of the change
    post(db, urig, version=2, production_start=(BASE + timedelta(minutes=5)).isoformat(),
         production_end=(BASE + timedelta(minutes=9)).isoformat(), cycles=4, castings=8)
    post(db, urig, source_event_id="EV-1B", production_start=(BASE + timedelta(minutes=11)).isoformat(),
         production_end=(BASE + timedelta(minutes=15)).isoformat(), cycles=6, castings=12)
    assert usage.item_usage(db, old)["recorded_cycles"] == 4 and usage.item_usage(db, spare.id)["recorded_cycles"] == 6


def test_oms_association_publishes_constituents_and_their_fit_history(db, people, urig):
    old = urig["items"]["t1"]
    spare = items.register_item(db, people["stores"], design_id=urig["tf"], legacy_no="t2", condition="SERVICEABLE", location_id=1)
    issues.replace_in_production(db, people["stores"], urig["issue"], old, spare.id, reason="worn")
    (a,) = usage.associations(db, "WO-1")
    assert a["build"] == "BLD-000001" and a["operation_id"] == "OP10" and a["part_id"] == "PN-1"
    by = {c["display_id"]: c for c in a["constituents"]}
    assert by["T1"]["removed_at"] is not None and by["T2"]["removed_at"] is None and by["D1"]["removed_at"] is None
    assert usage.associations(db, "NOPE") == []
