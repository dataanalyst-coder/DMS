"""Element designs and their approved revisions (Engineering owns these)."""
from sqlalchemy.orm import Session

from ..models import KINDS, Category, DesignRevision, ElementDesign, Item, Uom, _now
from . import audit, permissions, recheck
from .common import clean, normalize_id, require_reason, transaction
from .errors import NotFoundError, ValidationError


def _rev_dict(r: DesignRevision) -> dict:
    return {"id": r.id, "design_id": r.design_id, "revision": r.revision, "status": r.status,
            "specifications": r.specifications, "storage_instructions": r.storage_instructions,
            "change_reason": r.change_reason, "created_by": r.created_by,
            "created_at": r.created_at, "approved_by": r.approved_by, "approved_at": r.approved_at}


def get_design(db: Session, design_id: int) -> ElementDesign:
    d = db.get(ElementDesign, design_id)
    if d is None:
        raise NotFoundError(f"Design {design_id} not found")
    return d


def get_revision(db: Session, revision_id: int) -> DesignRevision:
    r = db.get(DesignRevision, revision_id)
    if r is None:
        raise NotFoundError(f"Design revision {revision_id} not found")
    return r


def revisions(db: Session, design_id: int) -> list[DesignRevision]:
    return db.query(DesignRevision).filter(DesignRevision.design_id == design_id).order_by(
        DesignRevision.id).all()


def current_approved(db: Session, design_id: int) -> DesignRevision | None:
    return db.query(DesignRevision).filter(
        DesignRevision.design_id == design_id, DesignRevision.status == "APPROVED"
    ).order_by(DesignRevision.id.desc()).first()


def create_design(db: Session, actor, *, code, description, category_id, base_uom,
                  kind="REUSABLE", specifications=None, storage_instructions=None) -> ElementDesign:
    permissions.require(actor, "design.edit")
    code = normalize_id(code)
    if not clean(description):
        raise ValidationError("Description is required")
    if kind not in KINDS:
        raise ValidationError(f"kind must be one of {', '.join(KINDS)}")
    cat = db.get(Category, category_id)
    if cat is None or not cat.active:
        raise ValidationError("Unknown or inactive tooling category")
    if db.get(Uom, (base_uom or "").strip().upper()) is None:
        raise ValidationError(f"Unknown unit of measure {base_uom!r}")
    if db.query(ElementDesign).filter(ElementDesign.code == code).first():
        raise ValidationError(f"Design {code} already exists")
    with transaction(db):
        d = ElementDesign(code=code, description=description.strip(), category_id=category_id,
                          kind=kind, base_uom=base_uom.strip().upper())
        db.add(d)
        db.flush()
        rev = DesignRevision(design_id=d.id, revision="A", status="DRAFT", created_by=actor.id,
                             specifications=clean(specifications),
                             storage_instructions=clean(storage_instructions),
                             change_reason="Initial revision")
        db.add(rev)
        db.flush()
        audit.record(db, actor, "CREATE", "design", d.id,
                     after={"code": code, "kind": kind, "category_id": category_id})
    return d


def update_design(db: Session, actor, design_id: int, *, description=None, category_id=None,
                  active=None) -> ElementDesign:
    permissions.require(actor, "design.edit")
    d = get_design(db, design_id)
    before = {"description": d.description, "category_id": d.category_id, "active": d.active}
    if category_id is not None and category_id != d.category_id:
        if db.query(Item).filter(Item.design_id == d.id).count():
            raise ValidationError("Category cannot change once physical items exist for this design")
        cat = db.get(Category, category_id)
        if cat is None or not cat.active:
            raise ValidationError("Unknown or inactive tooling category")
    with transaction(db):
        if description is not None:
            if not clean(description):
                raise ValidationError("Description is required")
            d.description = description.strip()
        if category_id is not None:
            d.category_id = category_id
        if active is not None:
            d.active = bool(active)
        audit.record(db, actor, "UPDATE", "design", d.id, before=before,
                     after={"description": d.description, "category_id": d.category_id,
                            "active": d.active})
    return d


def _next_label(existing: list[str]) -> str:
    n = len(existing)
    label = ""
    n += 1
    while n:
        n, rem = divmod(n - 1, 26)
        label = chr(65 + rem) + label
    return label


def new_revision(db: Session, actor, design_id: int, *, reason, specifications=None,
                 storage_instructions=None, revision=None) -> DesignRevision:
    """Start a DRAFT revision (copying the latest content unless new content is supplied)."""
    permissions.require(actor, "design.edit")
    reason = require_reason(reason, "a new design revision")
    d = get_design(db, design_id)
    revs = revisions(db, d.id)
    if any(r.status == "DRAFT" for r in revs):
        raise ValidationError("This design already has a draft revision; approve or edit that one")
    label = clean(revision) or _next_label([r.revision for r in revs])
    if any(r.revision == label for r in revs):
        raise ValidationError(f"Revision {label} already exists")
    last = revs[-1] if revs else None
    with transaction(db):
        rev = DesignRevision(
            design_id=d.id, revision=label, status="DRAFT", created_by=actor.id,
            change_reason=reason,
            specifications=clean(specifications) if specifications is not None else (last and last.specifications),
            storage_instructions=clean(storage_instructions) if storage_instructions is not None
            else (last and last.storage_instructions))
        db.add(rev)
        db.flush()
        audit.record(db, actor, "NEW_REVISION", "design", d.id, after=_rev_dict(rev), reason=reason)
    return rev


def update_draft_revision(db: Session, actor, revision_id: int, *, specifications=None,
                          storage_instructions=None) -> DesignRevision:
    permissions.require(actor, "design.edit")
    rev = get_revision(db, revision_id)
    if rev.status != "DRAFT":
        raise ValidationError("Only a draft revision can be edited; released revisions are immutable")
    before = _rev_dict(rev)
    with transaction(db):
        if specifications is not None:
            rev.specifications = clean(specifications)
        if storage_instructions is not None:
            rev.storage_instructions = clean(storage_instructions)
        audit.record(db, actor, "UPDATE", "design_revision", rev.id, before=before,
                     after=_rev_dict(rev))
    return rev


def approve_revision(db: Session, actor, revision_id: int) -> DesignRevision:
    """Engineering approval. The previously approved revision is superseded (OBSOLETE) for new
    work; physical items and past records keep pointing at the revision they were made to."""
    permissions.require(actor, "design.approve")
    rev = get_revision(db, revision_id)
    if rev.status != "DRAFT":
        raise ValidationError(f"Only a DRAFT revision can be approved (this one is {rev.status})")
    with transaction(db):
        for old in db.query(DesignRevision).filter(
                DesignRevision.design_id == rev.design_id, DesignRevision.status == "APPROVED"):
            audit.record(db, actor, "SUPERSEDE", "design_revision", old.id,
                         before={"status": "APPROVED"}, after={"status": "OBSOLETE"},
                         reason=f"Superseded by revision {rev.revision}")
            old.status = "OBSOLETE"
        rev.status, rev.approved_by, rev.approved_at = "APPROVED", actor.id, _now()
        audit.record(db, actor, "APPROVE", "design_revision", rev.id,
                     before={"status": "DRAFT"}, after={"status": "APPROVED"})
        recheck.flag(db, actor, f"Design revision {rev.revision} approved (earlier revisions no longer accepted by default)",
                     design_id=rev.design_id)
    return rev


def obsolete_revision(db: Session, actor, revision_id: int, *, reason) -> DesignRevision:
    permissions.require(actor, "design.approve")
    reason = require_reason(reason, "obsoleting a revision")
    rev = get_revision(db, revision_id)
    if rev.status == "OBSOLETE":
        raise ValidationError("Revision is already obsolete")
    with transaction(db):
        audit.record(db, actor, "OBSOLETE", "design_revision", rev.id,
                     before={"status": rev.status}, after={"status": "OBSOLETE"}, reason=reason)
        rev.status = "OBSOLETE"
        recheck.flag(db, actor, f"Design revision {rev.revision} made obsolete: {reason}", design_id=rev.design_id)
    return rev


def list_designs(db: Session, *, q=None, category_id=None, kind=None, active=None, limit=100,
                 offset=0) -> tuple[int, list[dict]]:
    qry = db.query(ElementDesign)
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(ElementDesign.code.ilike(like) | ElementDesign.description.ilike(like))
    if category_id:
        qry = qry.filter(ElementDesign.category_id == category_id)
    if kind:
        qry = qry.filter(ElementDesign.kind == kind)
    if active is not None:
        qry = qry.filter(ElementDesign.active == active)
    total = qry.count()
    out = []
    for d in qry.order_by(ElementDesign.code).offset(offset).limit(min(limit, 500)):
        cur = current_approved(db, d.id)
        out.append({"id": d.id, "code": d.code, "description": d.description,
                    "category_id": d.category_id, "kind": d.kind, "base_uom": d.base_uom,
                    "active": d.active, "approved_revision": cur.revision if cur else None,
                    "approved_revision_id": cur.id if cur else None})
    return total, out


def design_detail(db: Session, design_id: int) -> dict:
    d = get_design(db, design_id)
    cat = db.get(Category, d.category_id)
    return {"id": d.id, "code": d.code, "description": d.description, "kind": d.kind,
            "base_uom": d.base_uom, "active": d.active,
            "category": {"id": cat.id, "code": cat.code, "name": cat.name},
            "revisions": [_rev_dict(r) for r in revisions(db, d.id)],
            "item_count": db.query(Item).filter(Item.design_id == d.id).count()}
