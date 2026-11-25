import pytest

from app.services import die_sets
from app.services.errors import PermissionDenied, ValidationError
from tests.test_api import api, login  # noqa: F401  (fixtures)


def ids(kit):
    i = kit["items"]
    return {"die_item_id": i["d1"], "pattern_item_id": i["p1"], "top_flange_item_id": i["t1"], "bottom_flange_item_id": i["b1"]}


def test_a_die_maps_to_its_own_pattern_and_flanges_with_ids_and_descriptions(db, people, kit):
    s = die_sets.save_set(db, people["eng"], **ids(kit), note="32 NB set")
    d = die_sets.set_dict(db, s)
    assert d["die"]["display_id"] == "D1" and d["die"]["description"] == "Die 32"
    assert d["pattern"]["display_id"] == "P1" and d["pattern"]["description"] == "Pattern 1"
    assert d["top_flange"]["display_id"] == "T1" and d["top_flange"]["description"] == "tf-a"
    assert d["bottom_flange"]["display_id"] == "B1" and d["bottom_flange"]["location"].endswith("B1")
    total, rows = die_sets.list_sets(db)
    assert total == 1 and rows[0]["note"] == "32 NB set"
    assert die_sets.list_sets(db, q="pattern 1")[0] == 1 and die_sets.list_sets(db, q="nothing")[0] == 0
    back = die_sets.for_item(db, kit["items"]["t1"])
    assert back["slot"] == "top_flange" and back["die"]["display_id"] == "D1"
    assert die_sets.for_item(db, kit["items"]["d2"]) is None


def test_flanges_and_pattern_are_optional(db, people, kit):
    s = die_sets.save_set(db, people["eng"], die_item_id=kit["items"]["d2"])
    d = die_sets.set_dict(db, s)
    assert d["die"]["display_id"] == "D2" and d["pattern"] is None and d["top_flange"] is None


def test_each_slot_takes_only_its_own_type(db, people, kit):
    i = kit["items"]
    with pytest.raises(ValidationError) as e:
        die_sets.save_set(db, people["eng"], die_item_id=i["t1"])  # a flange is not a die
    assert "not a die" in e.value.message
    with pytest.raises(ValidationError):
        die_sets.save_set(db, people["eng"], die_item_id=i["d1"], pattern_item_id=i["b1"])
    with pytest.raises(ValidationError):
        die_sets.save_set(db, people["eng"], die_item_id=i["d1"], top_flange_item_id=i["b1"])
    with pytest.raises(ValidationError):
        die_sets.save_set(db, people["eng"], die_item_id=i["d1"], pattern_item_id=i["d1"])
    with pytest.raises(ValidationError):
        die_sets.save_set(db, people["eng"], die_item_id=None)


def test_an_item_belongs_to_one_set_and_changing_a_set_frees_the_old_parts(db, people, kit):
    i = kit["items"]
    die_sets.save_set(db, people["eng"], **ids(kit))
    with pytest.raises(ValidationError) as e:  # P1 and T1 are D1's
        die_sets.save_set(db, people["eng"], die_item_id=i["d2"], pattern_item_id=i["p1"])
    assert "already belongs to the set of die D1" in e.value.message
    die_sets.save_set(db, people["eng"], die_item_id=i["d1"], pattern_item_id=i["p3"], top_flange_item_id=i["t1"])  # replace D1's set
    assert die_sets.list_sets(db)[0] == 1
    die_sets.save_set(db, people["eng"], die_item_id=i["d2"], pattern_item_id=i["p1"], bottom_flange_item_id=i["b1"])  # now free
    assert die_sets.list_sets(db)[0] == 2


def test_only_engineering_maps_and_removal_needs_a_reason(db, people, kit):
    for who in ("stores", "plan", "prod"):
        with pytest.raises(PermissionDenied):
            die_sets.save_set(db, people[who], **ids(kit))
    die_sets.save_set(db, people["eng"], **ids(kit))
    with pytest.raises(PermissionDenied):
        die_sets.remove_set(db, people["stores"], kit["items"]["d1"], reason="x")
    with pytest.raises(ValidationError):
        die_sets.remove_set(db, people["eng"], kit["items"]["d1"], reason=" ")
    die_sets.remove_set(db, people["eng"], kit["items"]["d1"], reason="die retired from service")
    assert die_sets.list_sets(db)[0] == 0 and die_sets.for_item(db, kit["items"]["p1"]) is None
    die_sets.save_set(db, people["eng"], **ids(kit))  # parts are free to map again


def test_die_set_over_http_and_inventory_by_type(api, login, kit):
    for method, path in [("get", "/die-sets"), ("post", "/die-sets"), ("post", "/die-sets/1/remove"), ("get", "/items/1/die-set")]:
        assert getattr(api, method)(path).status_code == 401
    body = ids(kit)
    assert api.post("/die-sets", json=body, headers=login("stores")).status_code == 403
    r = api.post("/die-sets", json=body, headers=login("eng"))
    assert r.status_code == 201 and r.json()["pattern"]["description"] == "Pattern 1"
    assert api.get("/die-sets", headers=login("prod")).json()["total"] == 1
    assert api.get(f"/items/{kit['items']['b1']}/die-set", headers=login("prod")).json()["slot"] == "bottom_flange"
    cats = {c["code"]: c["id"] for c in api.get("/categories", headers=login("prod")).json()}
    dies = api.get("/items", params={"category_id": cats["DIE"]}, headers=login("prod")).json()["rows"]
    assert {x["category"] for x in dies} == {"DIE"} and {"D1", "D2"} <= {x["display_id"] for x in dies}
    flanges = api.get("/items", params={"category_id": cats["TOP-FLANGE"]}, headers=login("prod")).json()["rows"]
    assert [x["display_id"] for x in flanges] == ["T1"]
