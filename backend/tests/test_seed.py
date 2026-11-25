from app import seed
from app.services import assemblies, auth, batches, builds, inspections, issues, items, mappings, readiness


def test_seed_builds_a_coherent_demo_world(db):
    passwords = seed.seed(db)
    assert set(passwords) == {u for u, _, _ in seed.USERS} and len(set(passwords.values())) == len(passwords)
    assert auth.authenticate(db, "stores", passwords["stores"]).roles == ["STORES"]
    total, rows = items.search_items(db, active=None, limit=50)
    assert total == 10
    # one verified build holds four items, which therefore are not individually in stores custody
    b = builds.build_detail(db, 1)
    assert b["status"] == "VERIFIED" and b["ready_to_issue"] and len(b["components"]) == 4
    assert {r["display_id"] for r in items.search_items(db, custody="STORES")[1]}.isdisjoint(
        {c["display_id"] for c in b["components"]})
    # shared die across T211/T212; assemblies without flanges exist; ME mapping has a not-selectable option
    _, asm = assemblies.list_assemblies(db)
    assert {a["code"] for a in asm} == {"T211", "T212", "T214", "T215"} and all(a["approved_revision"] == "A" for a in asm)
    # consumables: the coating batch is in stock and the build's issue preview asks for it, with its basis
    (batch,) = batches.list_batches(db)[1]
    assert batch["usable"] == 50 and batch["uom"] == "L" and batch["status"] == "AVAILABLE"
    (need,) = issues.issue_preview(db, 1)["consumables"]
    assert (need["quantity"], need["basis"]) == (2, "ME spec ME-17") and need["batches"][0]["id"] == batch["id"]
    # approved starter checklists exist, so the awaiting-inspection item can be inspected
    assert len(inspections.list_templates(db)) == 4 and all(t["approved_version"] == "1" for t in inspections.list_templates(db))
    queue = inspections.awaiting_queue(db)
    assert [q["display_id"] for q in queue] == ["P103"]
    total, checks = readiness.list_checks(db, wo_id="WO-1001")
    assert total == 1 and checks[0]["result"] == "READY"
    opts = mappings.options(db, part_id="PART-2002", routing_revision="R1", operation_id="OP10")
    assert [o["selectable"] for o in opts] == [False]
