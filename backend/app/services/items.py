"""Physical reusable items: registration, master data, aliases, scan lookup, search, photos, labels.

Condition, custody and location are never stored here; they come from the ledger (item_state)."""
from datetime import date

from sqlalchemy import Column, Integer, MetaData, String, Table, exists, func, or_, select
from sqlalchemy.orm import Session

from ..models import (CONDITIONS, Category, DesignRevision, ElementDesign, Item, ItemAlias, ItemPhoto,
                      Location, User, _now)
from . import audit, config, designs, ledger, permissions, recheck, storage
from .common import clean, new_event, normalize_id, norm_opt, require_reason, transaction
from .errors import NotFoundError, ValidationError

# read-only handle on the item_state view (separate MetaData so create_all never tries to build it)
state_t = Table("item_state", MetaData(),
                Column("item_id", Integer), Column("item_condition", String), Column("custody", String),
                Column("location_id", Integer), Column("since"), Column("txn_id", Integer),
                Column("event_id", String), Column("last_seq", Integer))


def system_id_for(pk: int) -> str:
    return f"TI-{pk:06d}"


def _get(db: Session, item_id: int) -> Item:
    it = db.get(Item, item_id)
    if it is None:
        raise NotFoundError(f"Item {item_id} not found")
    return it


# ---------------------------------------------------------------- registration
def _check_usage(cycles, tonnage):
    if cycles is not None and (int(cycles) != cycles or cycles < 0):
        raise ValidationError("Opening cycles must be a whole number >= 0 (leave blank if unknown)")
    if tonnage is not None and tonnage < 0:
        raise ValidationError("Opening tonnage must be >= 0 (leave blank if unknown)")


def _pick_revision(db: Session, design: ElementDesign, revision_id) -> DesignRevision:
    if revision_id is None:
        rev = designs.current_approved(db, design.id)
        if rev is None:
            raise ValidationError(f"Design {design.code} has no approved revision; "
                                  f"Engineering must approve one first")
        return rev
    rev = designs.get_revision(db, revision_id)
    if rev.design_id != design.id:
        raise ValidationError("That revision belongs to a different design")
    if rev.status == "DRAFT":
        raise ValidationError("Items can only be registered against an approved revision")
    return rev


def _stage_item(db: Session, actor, *, design_id, design_revision_id=None, legacy_no=None,
                display_id=None, condition, location_id, current_location_id=None,
                acquisition_date=None, provenance=None, opening_cycles=None,
                opening_tonnage=None, remarks=None, event_id=None) -> Item:
    """Validate and stage an item + its first ledger row; caller owns permission and transaction."""
    design = designs.get_design(db, design_id)
    if not design.active:
        raise ValidationError(f"Design {design.code} is inactive")
    if design.kind != "REUSABLE":
        raise ValidationError(f"Design {design.code} is a consumable; register it as a batch instead")
    rev = _pick_revision(db, design, design_revision_id)
    if condition not in CONDITIONS:
        raise ValidationError(f"condition must be one of {', '.join(CONDITIONS)}")
    legacy_key = norm_opt(legacy_no)
    disp = norm_opt(display_id) or legacy_key
    if not disp:
        raise ValidationError("Give the existing tooling number, or a unique display ID")
    clash = db.query(Item).filter(Item.display_id == disp).first()
    if clash:
        raise ValidationError(
            f"Display ID {disp} is already used by {clash.system_id}. Legacy numbers may repeat, "
            f"but each item needs a unique display ID; set one for this item")
    home = config.get_location(db, location_id)
    cur = config.get_location(db, current_location_id) if current_location_id else home
    for l in (home, cur):
        if not l.active:
            raise ValidationError(f"Location {config.location_path(db, l.id)} is inactive")
    _check_usage(opening_cycles, opening_tonnage)

    item = Item(system_id="PENDING", legacy_no=clean(legacy_no), legacy_key=legacy_key, display_id=disp,
                design_id=design.id, design_revision_id=rev.id, home_location_id=home.id,
                acquisition_date=acquisition_date, provenance=clean(provenance),
                opening_cycles=opening_cycles, opening_tonnage=opening_tonnage,
                remarks=clean(remarks), created_by=actor.id)
    db.add(item)
    db.flush()
    item.system_id = system_id_for(item.id)
    ledger.receive(db, item, actor, condition=condition, location_id=cur.id,
                   event_id=event_id or new_event())
    audit.record(db, actor, "REGISTER", "item", item.id,
                 after={"system_id": item.system_id, "display_id": disp, "legacy_no": item.legacy_no,
                        "design_id": design.id, "revision": rev.revision, "condition": condition})
    return item


def register_item(db: Session, actor: User, **kw) -> Item:
    permissions.require(actor, "item.register")
    with transaction(db):
        item = _stage_item(db, actor, **kw)
    return item


# ---------------------------------------------------------------- master data
def update_item(db: Session, actor: User, item_id: int, *, remarks=None, provenance=None,
                acquisition_date=None, home_location_id=None, opening_cycles=None,
                opening_tonnage=None, fields_set: set | None = None) -> Item:
    """Edit descriptive master data. Identity (system ID, legacy number, display ID) is immutable;
    status is not editable at all. `fields_set` lists which fields the caller actually sent so a
    blank value can clear a field."""
    permissions.require(actor, "item.edit")
    item = _get(db, item_id)
    sent = fields_set if fields_set is not None else {
        k for k, v in dict(remarks=remarks, provenance=provenance, acquisition_date=acquisition_date,
                           home_location_id=home_location_id, opening_cycles=opening_cycles,
                           opening_tonnage=opening_tonnage).items() if v is not None}
    new = dict(remarks=remarks, provenance=provenance, acquisition_date=acquisition_date,
               home_location_id=home_location_id, opening_cycles=opening_cycles,
               opening_tonnage=opening_tonnage)
    before = {k: getattr(item, k) for k in sent}
    if "opening_cycles" in sent or "opening_tonnage" in sent:
        _check_usage(new["opening_cycles"] if "opening_cycles" in sent else None,
                     new["opening_tonnage"] if "opening_tonnage" in sent else None)
    if "home_location_id" in sent:
        loc = config.get_location(db, new["home_location_id"])
        if not loc.active:
            raise ValidationError("Home location is inactive")
    with transaction(db):
        for k in sent:
            v = new[k]
            setattr(item, k, clean(v) if isinstance(v, str) else v)
        audit.record(db, actor, "UPDATE", "item", item.id, before=before,
                     after={k: getattr(item, k) for k in sent})
    return item


def retire_item(db: Session, actor: User, item_id: int, *, reason) -> Item:
    """Retire the master (never delete). Items used in production keep their full history."""
    permissions.require(actor, "item.retire")
    reason = require_reason(reason, "retiring an item")
    item = _get(db, item_id)
    if not item.active:
        raise ValidationError("Item is already retired")
    s = ledger.get_state(db, item.id)
    if s["custody"] != "STORES":
        raise ValidationError(f"Item is in {s['custody']} custody; return it before retiring")
    with transaction(db):
        item.active, item.retired_reason, item.retired_by, item.retired_at = False, reason, actor.id, _now()
        audit.record(db, actor, "RETIRE", "item", item.id, before={"active": True},
                     after={"active": False}, reason=reason)
        recheck.flag(db, actor, f"Item {item.display_id} was retired: {reason}", item_ids=[item.id])
    return item


# ---------------------------------------------------------------- aliases
def request_alias(db: Session, actor: User, item_id: int, alias: str, *, reason) -> ItemAlias:
    permissions.require(actor, "alias.request")
    reason = require_reason(reason, "an alias request")
    item = _get(db, item_id)
    key = normalize_id(alias)
    taken = db.query(ItemAlias).filter(ItemAlias.alias_key == key).first()
    if taken or db.query(Item).filter(Item.display_id == key, Item.id != item.id).first():
        raise ValidationError(f"{key} is already in use as an alias or display ID")
    with transaction(db):
        a = ItemAlias(item_id=item.id, alias=alias.strip(), alias_key=key, reason=reason,
                      requested_by=actor.id)
        db.add(a)
        db.flush()
        audit.record(db, actor, "REQUEST_ALIAS", "item", item.id, after={"alias": key}, reason=reason)
    return a


def _decide_alias(db, actor, alias_id, status, note):
    permissions.require(actor, "alias.approve")
    a = db.get(ItemAlias, alias_id)
    if a is None:
        raise NotFoundError(f"Alias {alias_id} not found")
    if a.status != "PENDING":
        raise ValidationError(f"Alias is already {a.status}")
    with transaction(db):
        a.status, a.approved_by, a.approved_at = status, actor.id, _now()
        audit.record(db, actor, f"{status}_ALIAS", "item", a.item_id, after={"alias": a.alias_key},
                     reason=note)
    return a


def approve_alias(db, actor, alias_id, note=None):
    return _decide_alias(db, actor, alias_id, "APPROVED", note)


def reject_alias(db, actor, alias_id, note=None):
    return _decide_alias(db, actor, alias_id, "REJECTED", require_reason(note, "rejecting an alias"))


# ---------------------------------------------------------------- photos & labels
def add_photo(db: Session, actor: User, item_id: int, kind: str, filename: str, data: bytes) -> ItemPhoto:
    permissions.require(actor, "item.photo")
    item = _get(db, item_id)
    if kind not in ("ITEM", "STORAGE"):
        raise ValidationError("kind must be ITEM or STORAGE")
    meta = storage.save_upload("photos", filename, data, allowed=storage.PHOTO_EXTENSIONS,
                               max_bytes=storage.MAX_PHOTO_BYTES)
    with transaction(db):
        p = ItemPhoto(item_id=item.id, kind=kind, uploaded_by=actor.id, **meta)
        db.add(p)
        db.flush()
        audit.record(db, actor, "ADD_PHOTO", "item", item.id, after={"kind": kind, "sha256": meta["sha256"]})
    return p


def get_photo(db: Session, photo_id: int) -> tuple[ItemPhoto, bytes]:
    p = db.get(ItemPhoto, photo_id)
    if p is None:
        raise NotFoundError("Photo not found")
    return p, storage.read_stored(p.stored_path)


def label_data(db: Session, actor: User, item_id: int) -> dict:
    """Label content. Reprinting never creates a new identity: system_id is fixed at registration."""
    permissions.require(actor, "item.label")
    item = _get(db, item_id)
    design = db.get(ElementDesign, item.design_id)
    with transaction(db):
        audit.record(db, actor, "PRINT_LABEL", "item", item.id)
    return {"system_id": item.system_id, "qr_payload": item.system_id, "legacy_no": item.legacy_no,
            "display_id": item.display_id, "design_code": design.code, "description": design.description}


# ---------------------------------------------------------------- views
def _summary_rows(db: Session, rows) -> list[dict]:
    locs = {l["id"]: l["path"] for l in config.list_locations(db)}
    out = []
    for r in rows:
        it, design, cat, rev, condition, custody, loc_id, since = r
        out.append({
            "id": it.id, "system_id": it.system_id, "display_id": it.display_id,
            "legacy_no": it.legacy_no, "design_id": design.id, "design_code": design.code,
            "description": design.description, "category": cat.code, "revision": rev.revision,
            "active": it.active, "condition": condition, "custody": custody,
            "location": locs.get(loc_id), "home_location": locs.get(it.home_location_id),
            "home_mismatch": loc_id != it.home_location_id, "since": since})
    return out


def _base_query():
    return (select(Item, ElementDesign, Category, DesignRevision, state_t.c.item_condition,
                   state_t.c.custody, state_t.c.location_id, state_t.c.since)
            .join(ElementDesign, ElementDesign.id == Item.design_id)
            .join(Category, Category.id == ElementDesign.category_id)
            .join(DesignRevision, DesignRevision.id == Item.design_revision_id)
            .join(state_t, state_t.c.item_id == Item.id))


def _q_filter(db: Session, q: str):
    raw = q.strip().upper()
    try:
        norm = normalize_id(q)
    except ValidationError:
        norm = raw
    terms = {raw, norm}
    conds = []
    for t in terms:
        like = f"%{t}%"
        conds += [func.upper(Item.system_id).like(like), func.upper(Item.display_id).like(like),
                  func.upper(func.coalesce(Item.legacy_key, "")).like(like),
                  func.upper(func.coalesce(Item.legacy_no, "")).like(like),
                  func.upper(ElementDesign.code).like(like), func.upper(ElementDesign.description).like(like),
                  exists().where(ItemAlias.item_id == Item.id, ItemAlias.status == "APPROVED",
                                 ItemAlias.alias_key.like(like))]
    return or_(*conds)


def search_items(db: Session, *, q=None, design_id=None, category_id=None, condition=None,
                 custody=None, location_id=None, active=True, home_mismatch=None, limit=50,
                 offset=0) -> tuple[int, list[dict]]:
    stmt = _base_query()
    if q and q.strip():
        stmt = stmt.where(_q_filter(db, q))
    if design_id:
        stmt = stmt.where(Item.design_id == design_id)
    if category_id:
        stmt = stmt.where(ElementDesign.category_id == category_id)
    if condition:
        stmt = stmt.where(state_t.c.item_condition == condition)
    if custody:
        stmt = stmt.where(state_t.c.custody == custody)
    if location_id:
        stmt = stmt.where(state_t.c.location_id.in_(config.subtree_ids(db, location_id)))
    if active is not None:
        stmt = stmt.where(Item.active == active)
    if home_mismatch is True:
        stmt = stmt.where(state_t.c.location_id != Item.home_location_id)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar()
    rows = db.execute(stmt.order_by(Item.display_id).offset(max(offset, 0)).limit(min(limit, 500))).all()
    return total, _summary_rows(db, rows)


def lookup(db: Session, code: str) -> list[dict]:
    """Resolve a scanned/typed code: QR (system ID), display ID, legacy number or approved alias.
    A duplicated legacy number returns every match so the user can choose."""
    raw = (code or "").strip().upper()
    if not raw:
        raise ValidationError("Nothing to look up")
    try:
        key = normalize_id(raw)
    except ValidationError:
        key = raw
    stmt = _base_query().where(or_(
        Item.system_id == raw, Item.display_id == key, Item.legacy_key == key,
        exists().where(ItemAlias.item_id == Item.id, ItemAlias.status == "APPROVED",
                       ItemAlias.alias_key == key)))
    return _summary_rows(db, db.execute(stmt.order_by(Item.display_id)).all())


def item_detail(db: Session, item_id: int) -> dict:
    item = _get(db, item_id)
    stmt = _base_query().where(Item.id == item_id)
    summary = _summary_rows(db, db.execute(stmt).all())[0]
    s = ledger.get_state(db, item_id)
    _, history = ledger.ledger_rows(db, item_id=item_id, limit=1000)
    aliases = [{"id": a.id, "alias": a.alias, "status": a.status, "reason": a.reason}
               for a in db.query(ItemAlias).filter(ItemAlias.item_id == item_id).order_by(ItemAlias.id)]
    photos = [{"id": p.id, "kind": p.kind, "file_name": p.file_name, "uploaded_at": p.uploaded_at}
              for p in db.query(ItemPhoto).filter(ItemPhoto.item_id == item_id).order_by(ItemPhoto.id)]
    from . import builds, issues  # local imports: both depend on the ledger, not on this module
    b = builds.active_build_for_item(db, item_id)
    oi = issues.open_issue_for_item(db, item_id)
    return {**summary, "build": {"id": b.id, "code": builds.build_code(b.id), "status": b.status} if b else None,
            "issue": {"id": oi[0].id, "code": issues.issue_code(oi[0].id), "wo_id": issues.wo_text(db, oi[0]), "state": oi[1]} if oi else None,
            "provenance": item.provenance, "remarks": item.remarks,
            "acquisition_date": item.acquisition_date, "opening_cycles": item.opening_cycles,
            "opening_tonnage": item.opening_tonnage, "retired_reason": item.retired_reason,
            "home_location_id": item.home_location_id, "state_txn_id": s["txn_id"],
            "aliases": aliases, "photos": photos, "history": history}
