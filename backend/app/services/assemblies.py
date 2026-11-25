"""Assembly definitions: versioned bills of element designs, with approved substitutes.

A definition says what an assembly consists of. It never says which physical items are in it; that is
a build (services/builds.py). Engineering owns composition and substitutes; ME's operation mapping is
separate (services/mappings.py)."""
from sqlalchemy.orm import Session

from ..models import (AssemblyDefinition, AssemblyLine, AssemblyLineAlternative, AssemblyRevision, Category,
                      DesignRevision, ElementDesign, _now)
from . import audit, designs, permissions, recheck
from .common import clean, normalize_id, require_reason, transaction
from .designs import _next_label
from .errors import NotFoundError, ValidationError
from .matching import Line


# ---------------------------------------------------------------- lookups
def get_definition(db: Session, definition_id: int) -> AssemblyDefinition:
    d = db.get(AssemblyDefinition, definition_id)
    if d is None:
        raise NotFoundError(f"Assembly {definition_id} not found")
    return d


def get_revision(db: Session, revision_id: int) -> AssemblyRevision:
    r = db.get(AssemblyRevision, revision_id)
    if r is None:
        raise NotFoundError(f"Assembly revision {revision_id} not found")
    return r


def revisions(db: Session, definition_id: int) -> list[AssemblyRevision]:
    return db.query(AssemblyRevision).filter(AssemblyRevision.definition_id == definition_id).order_by(
        AssemblyRevision.id).all()


def current_approved(db: Session, definition_id: int) -> AssemblyRevision | None:
    return db.query(AssemblyRevision).filter(
        AssemblyRevision.definition_id == definition_id, AssemblyRevision.status == "APPROVED"
    ).order_by(AssemblyRevision.id.desc()).first()


def _lines(db: Session, revision_id: int) -> list[AssemblyLine]:
    return db.query(AssemblyLine).filter(AssemblyLine.revision_id == revision_id).order_by(AssemblyLine.id).all()


def _alts(db: Session, line_id: int) -> list[AssemblyLineAlternative]:
    return db.query(AssemblyLineAlternative).filter(AssemblyLineAlternative.line_id == line_id).order_by(
        AssemblyLineAlternative.id).all()


def _design(db: Session, design_id: int) -> ElementDesign:
    d = db.get(ElementDesign, design_id)
    if d is None:
        raise ValidationError(f"Unknown design {design_id}")
    return d


def _draft(db: Session, revision_id: int) -> AssemblyRevision:
    rev = get_revision(db, revision_id)
    if rev.status != "DRAFT":
        raise ValidationError("Only a draft revision can be changed; approved revisions are immutable. "
                              "Start a new revision instead")
    return rev


def _norm_revisions(db: Session, design: ElementDesign, raw) -> str | None:
    """'a, b' -> 'A,B': each label is matched (case-insensitively) to a real revision of that design."""
    labels = [x.strip() for x in str(raw or "").replace(";", ",").split(",") if x.strip()]
    if not labels:
        return None
    known = {r.revision.upper(): r.revision for r in designs.revisions(db, design.id)}
    bad = [x for x in labels if x.upper() not in known]
    if bad:
        raise ValidationError(f"Design {design.code} has no revision {', '.join(bad)} "
                              f"(known: {', '.join(sorted(known.values()))})")
    return ",".join(dict.fromkeys(known[x.upper()] for x in labels))


def _check_quantity(design: ElementDesign, quantity, basis):
    try:
        q = float(quantity)
    except (TypeError, ValueError):
        raise ValidationError("Quantity must be a number")
    if q <= 0:
        raise ValidationError("Quantity must be greater than zero")
    if design.kind == "REUSABLE":
        if q != int(q):
            raise ValidationError("A reusable element is counted in whole items")
        return float(int(q)), None
    if not clean(basis):
        raise ValidationError("A consumable line needs the basis of its quantity (ME requirement or an "
                              "authorised entry); do not guess a consumption rate")
    return q, basis.strip()


# ---------------------------------------------------------------- definitions
def create_assembly(db: Session, actor, *, code, description, category_id, instructions=None,
                    checklist_refs=None) -> AssemblyDefinition:
    permissions.require(actor, "assembly.edit")
    code = normalize_id(code)
    if not clean(description):
        raise ValidationError("Description is required")
    cat = db.get(Category, category_id)
    if cat is None or not cat.active:
        raise ValidationError("Unknown or inactive category")
    if db.query(AssemblyDefinition).filter(AssemblyDefinition.code == code).first():
        raise ValidationError(f"Assembly {code} already exists")
    with transaction(db):
        d = AssemblyDefinition(code=code, description=description.strip(), category_id=category_id)
        db.add(d)
        db.flush()
        db.add(AssemblyRevision(definition_id=d.id, revision="A", created_by=actor.id,
                                instructions=clean(instructions), checklist_refs=clean(checklist_refs),
                                change_reason="Initial revision"))
        audit.record(db, actor, "CREATE", "assembly", d.id, after={"code": code, "category_id": category_id})
    return d


def update_assembly(db: Session, actor, definition_id: int, *, description=None, category_id=None,
                    active=None) -> AssemblyDefinition:
    permissions.require(actor, "assembly.edit")
    d = get_definition(db, definition_id)
    before = {"description": d.description, "category_id": d.category_id, "active": d.active}
    if category_id is not None and db.get(Category, category_id) is None:
        raise ValidationError("Unknown category")
    with transaction(db):
        if description is not None:
            if not clean(description):
                raise ValidationError("Description is required")
            d.description = description.strip()
        if category_id is not None:
            d.category_id = category_id
        if active is not None:
            d.active = bool(active)
        audit.record(db, actor, "UPDATE", "assembly", d.id, before=before,
                     after={"description": d.description, "category_id": d.category_id, "active": d.active})
    return d


# ---------------------------------------------------------------- revisions
def new_revision(db: Session, actor, definition_id: int, *, reason, instructions=None,
                 checklist_refs=None, revision=None) -> AssemblyRevision:
    """Start a DRAFT revision that copies the latest revision's lines and substitutes."""
    permissions.require(actor, "assembly.edit")
    reason = require_reason(reason, "a new assembly revision")
    d = get_definition(db, definition_id)
    revs = revisions(db, d.id)
    if any(r.status == "DRAFT" for r in revs):
        raise ValidationError("This assembly already has a draft revision; finish or approve that one")
    label = clean(revision) or _next_label([r.revision for r in revs])
    if any(r.revision == label for r in revs):
        raise ValidationError(f"Revision {label} already exists")
    last = revs[-1]
    with transaction(db):
        rev = AssemblyRevision(
            definition_id=d.id, revision=label, created_by=actor.id, change_reason=reason,
            instructions=clean(instructions) if instructions is not None else last.instructions,
            checklist_refs=clean(checklist_refs) if checklist_refs is not None else last.checklist_refs)
        db.add(rev)
        db.flush()
        for ln in _lines(db, last.id):
            new = AssemblyLine(revision_id=rev.id, design_id=ln.design_id, quantity=ln.quantity,
                               quantity_basis=ln.quantity_basis, allowed_revisions=ln.allowed_revisions,
                               remarks=ln.remarks)
            db.add(new)
            db.flush()
            for a in _alts(db, ln.id):
                db.add(AssemblyLineAlternative(line_id=new.id, design_id=a.design_id,
                                               allowed_revisions=a.allowed_revisions))
        audit.record(db, actor, "NEW_REVISION", "assembly", d.id,
                     after={"revision": label, "copied_from": last.revision}, reason=reason)
    return rev


def update_revision(db: Session, actor, revision_id: int, *, instructions=None, checklist_refs=None):
    permissions.require(actor, "assembly.edit")
    rev = _draft(db, revision_id)
    before = {"instructions": rev.instructions, "checklist_refs": rev.checklist_refs}
    with transaction(db):
        if instructions is not None:
            rev.instructions = clean(instructions)
        if checklist_refs is not None:
            rev.checklist_refs = clean(checklist_refs)
        audit.record(db, actor, "UPDATE", "assembly_revision", rev.id, before=before,
                     after={"instructions": rev.instructions, "checklist_refs": rev.checklist_refs})
    return rev


def add_line(db: Session, actor, revision_id: int, *, design_id, quantity=1, quantity_basis=None,
             allowed_revisions=None, remarks=None) -> AssemblyLine:
    permissions.require(actor, "assembly.edit")
    rev = _draft(db, revision_id)
    design = _design(db, design_id)
    if not design.active:
        raise ValidationError(f"Design {design.code} is inactive")
    if db.query(AssemblyLine).filter_by(revision_id=rev.id, design_id=design.id).first():
        raise ValidationError(f"{design.code} already has a line here; raise its quantity instead")
    qty, basis = _check_quantity(design, quantity, quantity_basis)
    with transaction(db):
        ln = AssemblyLine(revision_id=rev.id, design_id=design.id, quantity=qty, quantity_basis=basis,
                          allowed_revisions=_norm_revisions(db, design, allowed_revisions),
                          remarks=clean(remarks))
        db.add(ln)
        db.flush()
        audit.record(db, actor, "ADD_LINE", "assembly_revision", rev.id,
                     after={"design": design.code, "quantity": qty})
    return ln


def _line(db: Session, line_id: int) -> AssemblyLine:
    ln = db.get(AssemblyLine, line_id)
    if ln is None:
        raise NotFoundError(f"Line {line_id} not found")
    return ln


def update_line(db: Session, actor, line_id: int, *, quantity=None, quantity_basis=None,
                allowed_revisions=None, remarks=None, fields_set: set | None = None) -> AssemblyLine:
    permissions.require(actor, "assembly.edit")
    ln = _line(db, line_id)
    _draft(db, ln.revision_id)
    design = _design(db, ln.design_id)
    sent = fields_set if fields_set is not None else {
        k for k, v in dict(quantity=quantity, quantity_basis=quantity_basis,
                           allowed_revisions=allowed_revisions, remarks=remarks).items() if v is not None}
    before = {"quantity": ln.quantity, "allowed_revisions": ln.allowed_revisions}
    with transaction(db):
        if "quantity" in sent or "quantity_basis" in sent:
            ln.quantity, ln.quantity_basis = _check_quantity(
                design, quantity if "quantity" in sent else ln.quantity,
                quantity_basis if "quantity_basis" in sent else ln.quantity_basis)
        if "allowed_revisions" in sent:
            ln.allowed_revisions = _norm_revisions(db, design, allowed_revisions)
        if "remarks" in sent:
            ln.remarks = clean(remarks)
        audit.record(db, actor, "UPDATE_LINE", "assembly_revision", ln.revision_id, before=before,
                     after={"design": design.code, "quantity": ln.quantity,
                            "allowed_revisions": ln.allowed_revisions})
    return ln


def remove_line(db: Session, actor, line_id: int) -> None:
    permissions.require(actor, "assembly.edit")
    ln = _line(db, line_id)
    _draft(db, ln.revision_id)
    design = _design(db, ln.design_id)
    with transaction(db):
        for a in _alts(db, ln.id):
            db.delete(a)
        db.delete(ln)
        audit.record(db, actor, "REMOVE_LINE", "assembly_revision", ln.revision_id,
                     before={"design": design.code, "quantity": ln.quantity})


def add_alternative(db: Session, actor, line_id: int, *, design_id, allowed_revisions=None):
    permissions.require(actor, "assembly.edit")
    ln = _line(db, line_id)
    _draft(db, ln.revision_id)
    primary, alt = _design(db, ln.design_id), _design(db, design_id)
    if alt.id == primary.id:
        raise ValidationError("A design cannot be its own substitute")
    if not alt.active:
        raise ValidationError(f"Design {alt.code} is inactive")
    if alt.kind != primary.kind:
        raise ValidationError("A substitute must be the same kind (reusable/consumable) as the line")
    if db.query(AssemblyLineAlternative).filter_by(line_id=ln.id, design_id=alt.id).first():
        raise ValidationError(f"{alt.code} is already a substitute on this line")
    with transaction(db):
        a = AssemblyLineAlternative(line_id=ln.id, design_id=alt.id,
                                    allowed_revisions=_norm_revisions(db, alt, allowed_revisions))
        db.add(a)
        db.flush()
        audit.record(db, actor, "ADD_SUBSTITUTE", "assembly_revision", ln.revision_id,
                     after={"line": primary.code, "substitute": alt.code})
    return a


def remove_alternative(db: Session, actor, alternative_id: int) -> None:
    permissions.require(actor, "assembly.edit")
    a = db.get(AssemblyLineAlternative, alternative_id)
    if a is None:
        raise NotFoundError("Substitute not found")
    ln = _line(db, a.line_id)
    _draft(db, ln.revision_id)
    with transaction(db):
        db.delete(a)
        audit.record(db, actor, "REMOVE_SUBSTITUTE", "assembly_revision", ln.revision_id,
                     before={"line_design": ln.design_id, "substitute_design": a.design_id})


def approve_revision(db: Session, actor, revision_id: int) -> AssemblyRevision:
    permissions.require(actor, "assembly.approve")
    rev = get_revision(db, revision_id)
    if rev.status != "DRAFT":
        raise ValidationError(f"Only a DRAFT revision can be approved (this one is {rev.status})")
    lines = _lines(db, rev.id)
    if not lines:
        raise ValidationError("An assembly needs at least one element line before approval")
    problems = []
    for ln in lines:
        for d_id in [ln.design_id] + [a.design_id for a in _alts(db, ln.id)]:
            d = _design(db, d_id)
            if not d.active:
                problems.append(f"{d.code} is inactive")
            elif designs.current_approved(db, d.id) is None:
                problems.append(f"{d.code} has no approved revision")
    if problems:
        raise ValidationError("Cannot approve: " + "; ".join(sorted(set(problems))))
    with transaction(db):
        for old in db.query(AssemblyRevision).filter(
                AssemblyRevision.definition_id == rev.definition_id, AssemblyRevision.status == "APPROVED"):
            audit.record(db, actor, "SUPERSEDE", "assembly_revision", old.id, before={"status": "APPROVED"},
                         after={"status": "OBSOLETE"}, reason=f"Superseded by revision {rev.revision}")
            old.status = "OBSOLETE"
        rev.status, rev.approved_by, rev.approved_at = "APPROVED", actor.id, _now()
        audit.record(db, actor, "APPROVE", "assembly_revision", rev.id, before={"status": "DRAFT"},
                     after={"status": "APPROVED"})
        recheck.flag(db, actor, f"Assembly revision {rev.revision} approved: composition may have changed",
                     definition_id=rev.definition_id)
    return rev


def obsolete_revision(db: Session, actor, revision_id: int, *, reason) -> AssemblyRevision:
    permissions.require(actor, "assembly.approve")
    reason = require_reason(reason, "obsoleting an assembly revision")
    rev = get_revision(db, revision_id)
    if rev.status == "OBSOLETE":
        raise ValidationError("Revision is already obsolete")
    with transaction(db):
        audit.record(db, actor, "OBSOLETE", "assembly_revision", rev.id, before={"status": rev.status},
                     after={"status": "OBSOLETE"}, reason=reason)
        rev.status = "OBSOLETE"
        recheck.flag(db, actor, f"Assembly revision {rev.revision} made obsolete: {reason}",
                     definition_id=rev.definition_id)
    return rev


# ---------------------------------------------------------------- matching inputs
def _accepted_revision_ids(db: Session, design_id: int, allowed: str | None) -> set[int]:
    """Revision ids of `design_id` that satisfy a line's revision rule. No explicit list means the
    design's current approved revision only (so a superseded revision no longer fits by default)."""
    if allowed:
        labels = set(allowed.split(","))
        return {r.id for r in designs.revisions(db, design_id) if r.revision in labels}
    cur = designs.current_approved(db, design_id)
    return {cur.id} if cur else set()


def matching_lines(db: Session, revision_id: int, *, reusable_only=True) -> list[Line]:
    out = []
    for ln in _lines(db, revision_id):
        design = _design(db, ln.design_id)
        if reusable_only and design.kind != "REUSABLE":
            continue
        accepts = {ln.design_id: _accepted_revision_ids(db, ln.design_id, ln.allowed_revisions)}
        for a in _alts(db, ln.id):
            accepts[a.design_id] = _accepted_revision_ids(db, a.design_id, a.allowed_revisions)
        out.append(Line(line_id=ln.id, label=design.code, quantity=int(ln.quantity), accepts=accepts))
    return out


def consumable_lines(db: Session, revision_id: int) -> list[dict]:
    """The consumable requirements of a revision (quantity + the basis it comes from) and which designs
    may supply them. Reusable lines are handled by matching; these are handled by batch picks."""
    out = []
    for ln in _lines(db, revision_id):
        d = _design(db, ln.design_id)
        if d.kind != "CONSUMABLE":
            continue
        out.append({"line_id": ln.id, "design_id": d.id, "design_code": d.code, "uom": d.base_uom,
                    "quantity": ln.quantity, "basis": ln.quantity_basis,
                    "accepted_design_ids": [ln.design_id] + [a.design_id for a in _alts(db, ln.id)]})
    return out


# ---------------------------------------------------------------- views
def revision_dict(db: Session, rev: AssemblyRevision) -> dict:
    lines = []
    for ln in _lines(db, rev.id):
        d = _design(db, ln.design_id)
        lines.append({
            "id": ln.id, "design_id": d.id, "design_code": d.code, "description": d.description,
            "kind": d.kind, "quantity": ln.quantity, "quantity_basis": ln.quantity_basis,
            "allowed_revisions": ln.allowed_revisions, "remarks": ln.remarks,
            "alternatives": [{"id": a.id, "design_id": a.design_id, "design_code": _design(db, a.design_id).code,
                              "allowed_revisions": a.allowed_revisions} for a in _alts(db, ln.id)]})
    return {"id": rev.id, "revision": rev.revision, "status": rev.status, "instructions": rev.instructions,
            "checklist_refs": rev.checklist_refs, "change_reason": rev.change_reason,
            "approved_at": rev.approved_at, "lines": lines}


def assembly_detail(db: Session, definition_id: int) -> dict:
    from ..models import MeMapping
    d = get_definition(db, definition_id)
    cat = db.get(Category, d.category_id)
    return {"id": d.id, "code": d.code, "description": d.description, "active": d.active,
            "category": {"id": cat.id, "code": cat.code},
            "revisions": [revision_dict(db, r) for r in revisions(db, d.id)],
            "mapping_count": db.query(MeMapping).filter(MeMapping.definition_id == d.id,
                                                        MeMapping.active.is_(True)).count()}


def list_assemblies(db: Session, *, q=None, category_id=None, active=None, limit=100, offset=0):
    qry = db.query(AssemblyDefinition)
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(AssemblyDefinition.code.ilike(like) | AssemblyDefinition.description.ilike(like))
    if category_id:
        qry = qry.filter(AssemblyDefinition.category_id == category_id)
    if active is not None:
        qry = qry.filter(AssemblyDefinition.active == active)
    total = qry.count()
    rows = []
    for d in qry.order_by(AssemblyDefinition.code).offset(offset).limit(min(limit, 500)):
        cur = current_approved(db, d.id)
        rows.append({"id": d.id, "code": d.code, "description": d.description, "active": d.active,
                     "approved_revision": cur.revision if cur else None,
                     "approved_revision_id": cur.id if cur else None})
    return total, rows
