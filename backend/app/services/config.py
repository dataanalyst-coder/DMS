"""Administrator-maintained configuration: tooling categories, units of measure, locations."""
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import LOCATION_LEVELS, Category, Location, Uom
from . import audit, permissions
from .common import clean, transaction
from .errors import NotFoundError, ValidationError


def _code(raw, what="Code") -> str:
    code = re.sub(r"\s+", "", str(raw or "")).upper()
    if not code:
        raise ValidationError(f"{what} is required")
    return code


# ---------------------------------------------------------------- categories
def create_category(db: Session, actor, code, name) -> Category:
    permissions.require(actor, "config.manage")
    code = _code(code)
    if not clean(name):
        raise ValidationError("Name is required")
    if db.query(Category).filter(Category.code == code).first():
        raise ValidationError(f"Category {code} already exists")
    with transaction(db):
        cat = Category(code=code, name=name.strip())
        db.add(cat)
        db.flush()
        audit.record(db, actor, "CREATE", "category", cat.id, after={"code": code, "name": cat.name})
    return cat


def update_category(db: Session, actor, category_id: int, *, name=None, active=None) -> Category:
    permissions.require(actor, "config.manage")
    cat = db.get(Category, category_id)
    if cat is None:
        raise NotFoundError(f"Category {category_id} not found")
    before = {"name": cat.name, "active": cat.active}
    with transaction(db):
        if name is not None:
            if not clean(name):
                raise ValidationError("Name is required")
            cat.name = name.strip()
        if active is not None:
            cat.active = bool(active)
        audit.record(db, actor, "UPDATE", "category", cat.id, before=before,
                     after={"name": cat.name, "active": cat.active})
    return cat


def list_categories(db: Session) -> list[Category]:
    return db.query(Category).order_by(Category.code).all()


# ---------------------------------------------------------------- units of measure
def create_uom(db: Session, actor, code, name) -> Uom:
    permissions.require(actor, "config.manage")
    code = _code(code)
    if not clean(name):
        raise ValidationError("Name is required")
    if db.get(Uom, code):
        raise ValidationError(f"Unit {code} already exists")
    with transaction(db):
        uom = Uom(code=code, name=name.strip())
        db.add(uom)
        audit.record(db, actor, "CREATE", "uom", code, after={"name": uom.name})
    return uom


def list_uoms(db: Session) -> list[Uom]:
    return db.query(Uom).order_by(Uom.code).all()


# ---------------------------------------------------------------- locations
def create_location(db: Session, actor, *, code, name=None, parent_id=None) -> Location:
    """Level is implied by the parent: no parent = PLANT, then AREA, RACK, SHELF, BIN."""
    permissions.require(actor, "config.manage")
    code = _code(code)
    if parent_id is None:
        level = LOCATION_LEVELS[0]
    else:
        parent = db.get(Location, parent_id)
        if parent is None:
            raise NotFoundError(f"Parent location {parent_id} not found")
        idx = LOCATION_LEVELS.index(parent.level)
        if idx + 1 >= len(LOCATION_LEVELS):
            raise ValidationError(f"A {parent.level} cannot contain further locations")
        level = LOCATION_LEVELS[idx + 1]
    if db.query(Location).filter(Location.parent_id.is_(parent_id) if parent_id is None
                                 else Location.parent_id == parent_id, Location.code == code).first():
        raise ValidationError(f"Location {code} already exists here")
    with transaction(db):
        loc = Location(parent_id=parent_id, level=level, code=code, name=clean(name))
        db.add(loc)
        db.flush()
        audit.record(db, actor, "CREATE", "location", loc.id,
                     after={"code": code, "level": level, "parent_id": parent_id})
    return loc


def update_location(db: Session, actor, location_id: int, *, name=None, active=None) -> Location:
    permissions.require(actor, "config.manage")
    loc = get_location(db, location_id)
    before = {"name": loc.name, "active": loc.active}
    if active is False:
        children = db.query(Location).filter(Location.parent_id == loc.id, Location.active).count()
        if children:
            raise ValidationError("Deactivate the child locations first")
        # location_id of the latest effective ledger row = where the item physically is
        here = db.execute(text("SELECT COUNT(*) FROM item_state WHERE location_id = :l"),
                          {"l": loc.id}).scalar()
        if here:
            raise ValidationError(f"{here} item(s) are currently in this location; move them first")
    with transaction(db):
        if name is not None:
            loc.name = clean(name)
        if active is not None:
            loc.active = bool(active)
        audit.record(db, actor, "UPDATE", "location", loc.id, before=before,
                     after={"name": loc.name, "active": loc.active})
    return loc


def get_location(db: Session, location_id: int) -> Location:
    loc = db.get(Location, location_id)
    if loc is None:
        raise NotFoundError(f"Location {location_id} not found")
    return loc


def _all(db: Session) -> dict[int, Location]:
    return {l.id: l for l in db.query(Location)}


def location_path(db: Session, location_id: int | None, _cache=None) -> str | None:
    if location_id is None:
        return None
    locs = _cache or _all(db)
    parts, cur = [], locs.get(location_id)
    while cur is not None:
        parts.append(cur.code)
        cur = locs.get(cur.parent_id) if cur.parent_id else None
    return "/".join(reversed(parts)) or None


def list_locations(db: Session) -> list[dict]:
    locs = _all(db)
    rows = [{"id": l.id, "parent_id": l.parent_id, "level": l.level, "code": l.code, "name": l.name,
             "active": l.active, "path": location_path(db, l.id, locs)} for l in locs.values()]
    return sorted(rows, key=lambda r: r["path"])


def resolve_path(db: Session, path, *, active_only=True) -> Location:
    """'P1/A1/R3/S2/B4' (case-insensitive) -> Location. Used by import and scanning."""
    parts = [_code(p) for p in str(path or "").split("/") if p.strip()]
    if not parts:
        raise ValidationError("Location is required")
    parent_id, loc = None, None
    for p in parts:
        q = db.query(Location).filter(Location.code == p)
        q = q.filter(Location.parent_id.is_(None)) if parent_id is None else q.filter(
            Location.parent_id == parent_id)
        loc = q.first()
        if loc is None:
            raise ValidationError(f"Unknown location {path!r} (no {p} under "
                                  f"{location_path(db, parent_id) or 'top level'})")
        parent_id = loc.id
    if active_only and not loc.active:
        raise ValidationError(f"Location {path} is inactive")
    return loc


def subtree_ids(db: Session, location_id: int) -> list[int]:
    locs = _all(db)
    out, stack = [], [location_id]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(l.id for l in locs.values() if l.parent_id == cur)
    return out
