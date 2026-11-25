"""Die mapping: each physical die has one fixed pattern, top flange and bottom flange (Engineering keeps the pairing).

It is a reference (find the right parts, see all IDs and descriptions together). It does not move, reserve or issue
anything; issuing still goes through builds and setups."""
import re

from sqlalchemy.orm import Session

from ..models import Category, DieSet, ElementDesign, Item
from . import audit, config, ledger, permissions
from .common import clean, require_reason, transaction
from .errors import NotFoundError, ValidationError

SLOTS = (("die", "die_item_id", "DIE", "die"), ("pattern", "pattern_item_id", "PATTERN", "pattern"),
         ("top_flange", "top_flange_item_id", "TOPFLANGE", "top flange"),
         ("bottom_flange", "bottom_flange_item_id", "BOTTOMFLANGE", "bottom flange"))


def _kind(db: Session, item: Item) -> str:
    """Tooling type from the category code, ignoring case and punctuation (TOP-FLANGE == top_flange)."""
    cat = db.get(Category, db.get(ElementDesign, item.design_id).category_id)
    return re.sub(r"[^A-Z]", "", cat.code.upper())


def _item(db: Session, item_id, label: str) -> Item:
    it = db.get(Item, int(item_id))
    if it is None:
        raise NotFoundError(f"The {label} item {item_id} does not exist")
    return it


def _slot_row(db: Session, item_id, locs) -> dict | None:
    if not item_id:
        return None
    it = db.get(Item, item_id)
    d = db.get(ElementDesign, it.design_id)
    st = ledger.get_state(db, it.id)
    return {"id": it.id, "display_id": it.display_id, "description": d.description, "design_code": d.code,
            "condition": st["item_condition"], "custody": st["custody"], "location": locs.get(st["location_id"]),
            "active": it.active}


def set_dict(db: Session, s: DieSet, locs=None) -> dict:
    locs = locs or {l["id"]: l["path"] for l in config.list_locations(db)}
    return {"id": s.id, "note": s.note, "active": s.active,
            "die": _slot_row(db, s.die_item_id, locs), "pattern": _slot_row(db, s.pattern_item_id, locs),
            "top_flange": _slot_row(db, s.top_flange_item_id, locs), "bottom_flange": _slot_row(db, s.bottom_flange_item_id, locs)}


def _active_sets(db: Session):
    return db.query(DieSet).filter(DieSet.active.is_(True)).all()


def _users_of(db: Session, item_id: int, *, except_die=None) -> DieSet | None:
    for s in _active_sets(db):
        if except_die is not None and s.die_item_id == except_die:
            continue
        if item_id in (s.die_item_id, s.pattern_item_id, s.top_flange_item_id, s.bottom_flange_item_id):
            return s
    return None


def save_set(db: Session, actor, *, die_item_id, pattern_item_id=None, top_flange_item_id=None, bottom_flange_item_id=None,
             note=None) -> DieSet:
    """Create the set for a die, or replace the existing one (parts no longer listed become free again)."""
    permissions.require(actor, "dieset.manage")
    if not die_item_id:
        raise ValidationError("Choose the die")
    ids = {"die": die_item_id, "pattern": pattern_item_id or None, "top_flange": top_flange_item_id or None,
           "bottom_flange": bottom_flange_item_id or None}
    chosen = [int(v) for v in ids.values() if v]
    if len(set(chosen)) != len(chosen):
        raise ValidationError("The same item cannot be used twice in one set")
    for slot, _, want, label in SLOTS:
        if not ids[slot]:
            continue
        it = _item(db, ids[slot], label)
        if not it.active:
            raise ValidationError(f"{it.display_id} is retired and cannot be mapped")
        if _kind(db, it) != want:
            raise ValidationError(f"{it.display_id} is not a {label} (it is {db.get(Category, db.get(ElementDesign, it.design_id).category_id).code})")
        other = _users_of(db, it.id, except_die=int(die_item_id))
        if other is not None:
            raise ValidationError(f"{it.display_id} already belongs to the set of die {db.get(Item, other.die_item_id).display_id}; "
                                  f"take it out of that set first")
    existing = db.query(DieSet).filter(DieSet.active.is_(True), DieSet.die_item_id == int(die_item_id)).first()
    with transaction(db):
        if existing:
            before = set_dict(db, existing)
            s = existing
        else:
            before = None
            s = DieSet(die_item_id=int(die_item_id), created_by=actor.id)
            db.add(s)
        s.pattern_item_id = int(pattern_item_id) if pattern_item_id else None
        s.top_flange_item_id = int(top_flange_item_id) if top_flange_item_id else None
        s.bottom_flange_item_id = int(bottom_flange_item_id) if bottom_flange_item_id else None
        s.note = clean(note)
        db.flush()
        names = lambda d: {k: (d[k]["display_id"] if d.get(k) else None) for k in ("die", "pattern", "top_flange", "bottom_flange")}
        audit.record(db, actor, "DIESET_SAVE", "die_set", s.id, before=names(before) if before else None, after=names(set_dict(db, s)))
    return s


def remove_set(db: Session, actor, die_item_id: int, *, reason) -> DieSet:
    permissions.require(actor, "dieset.manage")
    reason = require_reason(reason, "removing a die mapping")
    s = db.query(DieSet).filter(DieSet.active.is_(True), DieSet.die_item_id == int(die_item_id)).first()
    if s is None:
        raise NotFoundError("That die has no mapping")
    with transaction(db):
        s.active = False
        audit.record(db, actor, "DIESET_REMOVE", "die_set", s.id, reason=reason)
    return s


def list_sets(db: Session, *, q=None, limit=100, offset=0) -> tuple[int, list[dict]]:
    locs = {l["id"]: l["path"] for l in config.list_locations(db)}
    rows = [set_dict(db, s, locs) for s in _active_sets(db)]
    rows.sort(key=lambda r: r["die"]["display_id"])
    if q and q.strip():
        needle = q.strip().upper()
        rows = [r for r in rows if any(needle in f"{p['display_id']} {p['description']} {p['design_code']}".upper()
                                       for p in (r["die"], r["pattern"], r["top_flange"], r["bottom_flange"]) if p)]
    return len(rows), rows[offset: offset + min(limit, 500)]


def for_item(db: Session, item_id: int) -> dict | None:
    """The set this item belongs to, whichever slot it fills, and which slot that is."""
    s = _users_of(db, int(item_id))
    if s is None:
        return None
    slot = next(n for n, col, _, _ in SLOTS if getattr(s, col) == int(item_id))
    return {**set_dict(db, s), "slot": slot}
