"""Configurable, versioned inspection checklists and item inspections.

  * Checklists are DATA: one template per tooling category plus optional design-specific additions, each
    versioned and approved by Engineering. Which checks, limits and photos are mandatory is Engineering's call.
  * An inspection records the checklist versions it used, so later changes never rewrite history (A14).
  * Results: pass / fail / not applicable (with reason), numeric measurements judged against limits, text,
    photos. A failed REQUIRED check forces a HOLD; mandatory evidence is enforced at completion.
  * One inspection per item, so one failed element never clears or condemns the others returned with it.
  * A PASS never releases anything: Engineering approves the technical release afterwards, and the ledger
    refuses SERVICEABLE without a passing inspection (require_passing_inspection)."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import (CHECK_TYPES, EVIDENCE_RULES, Category, ChecklistCheck, ChecklistTemplate, ChecklistVersion,
                      DesignRevision, ElementDesign, Inspection, InspectionPhoto, InspectionResult, Item, User, _now)
from . import audit, ledger, permissions, storage
from .common import clean, new_event, require_reason, transaction
from .designs import _next_label
from .errors import NotFoundError, ValidationError

# starting content (spec): not universal limits; Engineering decides what is mandatory per tooling type
STARTER_CHECKS = [
    ("COMPLETE_ID", "Completeness and identification", "All parts present; tag/QR and legacy number legible", True, "ON_FAIL"),
    ("CLEAN", "Cleaning", "Free of metal, coating build-up and debris", True, "NONE"),
    ("CRACKS", "Cracks", "No cracks or hot tears", True, "ON_FAIL"),
    ("WEAR", "Wear", "Within the wear allowance", True, "NONE"),
    ("DAMAGE", "Damage", "No dents, gouges or distortion", True, "ON_FAIL"),
    ("CRIT_DIMS", "Critical dimensions", "Measure against the drawing (add measurement checks with limits per design)", True, "NONE"),
    ("FASTENERS", "Fasteners", "Present, undamaged, correct torque", False, "NONE"),
    ("COATING", "Coating / lubrication", "As required by the work instruction", False, "NONE"),
    ("STORAGE_READY", "Storage readiness", "Dry, preserved, ready to be stored in its home location", False, "NONE"),
]


# ================================================================= checklists
def _version(db: Session, version_id: int) -> ChecklistVersion:
    v = db.get(ChecklistVersion, version_id)
    if v is None:
        raise NotFoundError(f"Checklist version {version_id} not found")
    return v


def _draft(db: Session, version_id: int) -> ChecklistVersion:
    v = _version(db, version_id)
    if v.status != "DRAFT":
        raise ValidationError("Only a draft checklist version can be changed; approved versions are immutable. "
                              "Start a new version instead")
    return v


def _scope_check(db: Session, scope_type: str, scope_id: int) -> str:
    if scope_type == "CATEGORY":
        c = db.get(Category, scope_id)
        if c is None:
            raise ValidationError("Unknown tooling category")
        return f"{c.code} checklist"
    if scope_type == "DESIGN":
        d = db.get(ElementDesign, scope_id)
        if d is None:
            raise ValidationError("Unknown design")
        return f"{d.code} additions"
    raise ValidationError("scope_type must be CATEGORY or DESIGN")


def create_template(db: Session, actor: User, scope_type: str, scope_id: int, name=None) -> ChecklistVersion:
    permissions.require(actor, "checklist.edit")
    default_name = _scope_check(db, scope_type, scope_id)
    if db.query(ChecklistTemplate).filter_by(scope_type=scope_type, scope_id=scope_id).first():
        raise ValidationError("A checklist already exists for this scope; add a new version to it")
    with transaction(db):
        t = ChecklistTemplate(scope_type=scope_type, scope_id=scope_id, name=clean(name) or default_name)
        db.add(t)
        db.flush()
        v = ChecklistVersion(template_id=t.id, version="1", created_by=actor.id, change_reason="Initial version")
        db.add(v)
        db.flush()
        audit.record(db, actor, "CREATE", "checklist", t.id, after={"scope": scope_type, "scope_id": scope_id})
    return v


def apply_starter(db: Session, actor: User, scope_type: str, scope_id: int) -> ChecklistVersion:
    """Create the template with the standard starting checks (as a draft for Engineering to tailor/approve)."""
    v = create_template(db, actor, scope_type, scope_id)
    for pos, (code, title, guidance, required, evidence) in enumerate(STARTER_CHECKS, start=1):
        add_check(db, actor, v.id, code=code, title=title, guidance=guidance, required=required, evidence=evidence,
                  position=pos)
    return v


def _validate_check(check_type, evidence, unit, min_value, max_value):
    if check_type not in CHECK_TYPES:
        raise ValidationError(f"check_type must be one of {', '.join(CHECK_TYPES)}")
    if evidence not in EVIDENCE_RULES:
        raise ValidationError(f"evidence must be one of {', '.join(EVIDENCE_RULES)}")
    if check_type == "MEASUREMENT":
        if min_value is None and max_value is None:
            raise ValidationError("A measurement check needs at least one limit (min or max)")
        if not clean(unit):
            raise ValidationError("A measurement check needs a unit")
        if min_value is not None and max_value is not None and float(min_value) > float(max_value):
            raise ValidationError("Minimum is greater than maximum")


def add_check(db: Session, actor: User, version_id: int, *, code, title, check_type="PASS_FAIL", required=True,
              na_allowed=False, evidence="NONE", unit=None, min_value=None, max_value=None, guidance=None,
              position=None) -> ChecklistCheck:
    permissions.require(actor, "checklist.edit")
    v = _draft(db, version_id)
    code = (code or "").strip().upper().replace(" ", "_")
    if not code or not clean(title):
        raise ValidationError("Code and title are required")
    _validate_check(check_type, evidence, unit, min_value, max_value)
    if db.query(ChecklistCheck).filter_by(version_id=v.id, code=code).first():
        raise ValidationError(f"Check {code} already exists in this version")
    n = db.query(ChecklistCheck).filter_by(version_id=v.id).count()
    with transaction(db):
        c = ChecklistCheck(version_id=v.id, position=position or n + 1, code=code, title=title.strip(),
                           guidance=clean(guidance), check_type=check_type, required=bool(required),
                           na_allowed=bool(na_allowed), evidence=evidence, unit=clean(unit),
                           min_value=min_value, max_value=max_value)
        db.add(c)
        db.flush()
        audit.record(db, actor, "ADD_CHECK", "checklist_version", v.id, after={"code": code, "type": check_type})
    return c


def _check(db: Session, check_id: int) -> ChecklistCheck:
    c = db.get(ChecklistCheck, check_id)
    if c is None:
        raise NotFoundError(f"Check {check_id} not found")
    return c


def update_check(db: Session, actor: User, check_id: int, **fields) -> ChecklistCheck:
    permissions.require(actor, "checklist.edit")
    c = _check(db, check_id)
    _draft(db, c.version_id)
    allowed = {"title", "guidance", "required", "na_allowed", "evidence", "unit", "min_value", "max_value"}
    bad = set(fields) - allowed
    if bad:
        raise ValidationError(f"Cannot edit: {', '.join(sorted(bad))}")
    before = {k: getattr(c, k) for k in fields}
    with transaction(db):
        for k, v in fields.items():
            setattr(c, k, clean(v) if isinstance(v, str) else v)
        _validate_check(c.check_type, c.evidence, c.unit, c.min_value, c.max_value)
        audit.record(db, actor, "UPDATE_CHECK", "checklist_version", c.version_id, before=before,
                     after={k: getattr(c, k) for k in fields})
    return c


def remove_check(db: Session, actor: User, check_id: int) -> None:
    permissions.require(actor, "checklist.edit")
    c = _check(db, check_id)
    _draft(db, c.version_id)
    with transaction(db):
        audit.record(db, actor, "REMOVE_CHECK", "checklist_version", c.version_id, before={"code": c.code})
        db.delete(c)


def new_version(db: Session, actor: User, template_id: int, *, reason) -> ChecklistVersion:
    permissions.require(actor, "checklist.edit")
    reason = require_reason(reason, "a new checklist version")
    t = db.get(ChecklistTemplate, template_id)
    if t is None:
        raise NotFoundError(f"Checklist {template_id} not found")
    versions = db.query(ChecklistVersion).filter_by(template_id=t.id).order_by(ChecklistVersion.id).all()
    if any(v.status == "DRAFT" for v in versions):
        raise ValidationError("This checklist already has a draft version")
    last = versions[-1]
    label = str(len(versions) + 1)
    with transaction(db):
        v = ChecklistVersion(template_id=t.id, version=label, created_by=actor.id, change_reason=reason)
        db.add(v)
        db.flush()
        for c in db.query(ChecklistCheck).filter_by(version_id=last.id).order_by(ChecklistCheck.position):
            db.add(ChecklistCheck(version_id=v.id, position=c.position, code=c.code, title=c.title, guidance=c.guidance,
                                  check_type=c.check_type, required=c.required, na_allowed=c.na_allowed,
                                  evidence=c.evidence, unit=c.unit, min_value=c.min_value, max_value=c.max_value))
        audit.record(db, actor, "NEW_VERSION", "checklist", t.id, after={"version": label}, reason=reason)
    return v


def approve_version(db: Session, actor: User, version_id: int) -> ChecklistVersion:
    permissions.require(actor, "checklist.approve")
    v = _version(db, version_id)
    if v.status != "DRAFT":
        raise ValidationError(f"Only a DRAFT version can be approved (this one is {v.status})")
    if not db.query(ChecklistCheck).filter_by(version_id=v.id).count():
        raise ValidationError("A checklist needs at least one check before approval")
    with transaction(db):
        for old in db.query(ChecklistVersion).filter_by(template_id=v.template_id, status="APPROVED"):
            audit.record(db, actor, "SUPERSEDE", "checklist_version", old.id, before={"status": "APPROVED"},
                         after={"status": "OBSOLETE"}, reason=f"Superseded by version {v.version}")
            old.status = "OBSOLETE"
        v.status, v.approved_by, v.approved_at = "APPROVED", actor.id, _now()
        audit.record(db, actor, "APPROVE", "checklist_version", v.id, before={"status": "DRAFT"}, after={"status": "APPROVED"})
    return v


def current_version(db: Session, template_id: int) -> ChecklistVersion | None:
    return db.query(ChecklistVersion).filter_by(template_id=template_id, status="APPROVED").order_by(
        ChecklistVersion.id.desc()).first()


def _check_dict(c: ChecklistCheck, scope_label: str = "") -> dict:
    return {"id": c.id, "position": c.position, "code": c.code, "title": c.title, "guidance": c.guidance,
            "check_type": c.check_type, "required": c.required, "na_allowed": c.na_allowed, "evidence": c.evidence,
            "unit": c.unit, "min_value": c.min_value, "max_value": c.max_value, "scope": scope_label}


def template_detail(db: Session, template_id: int) -> dict:
    t = db.get(ChecklistTemplate, template_id)
    if t is None:
        raise NotFoundError(f"Checklist {template_id} not found")
    return {"id": t.id, "scope_type": t.scope_type, "scope_id": t.scope_id, "name": t.name,
            "versions": [{"id": v.id, "version": v.version, "status": v.status, "change_reason": v.change_reason,
                          "approved_at": v.approved_at,
                          "checks": [_check_dict(c) for c in db.query(ChecklistCheck).filter_by(version_id=v.id)
                                     .order_by(ChecklistCheck.position)]}
                         for v in db.query(ChecklistVersion).filter_by(template_id=t.id).order_by(ChecklistVersion.id)]}


def list_templates(db: Session) -> list[dict]:
    out = []
    for t in db.query(ChecklistTemplate).order_by(ChecklistTemplate.scope_type, ChecklistTemplate.id):
        cur = current_version(db, t.id)
        out.append({"id": t.id, "scope_type": t.scope_type, "scope_id": t.scope_id, "name": t.name,
                    "approved_version": cur.version if cur else None})
    return out


def effective_checklist(db: Session, item: Item) -> dict:
    """What an inspection of this item must cover now: the approved category checklist plus the approved
    design-specific additions."""
    design = db.get(ElementDesign, item.design_id)
    cat_t = db.query(ChecklistTemplate).filter_by(scope_type="CATEGORY", scope_id=design.category_id).first()
    des_t = db.query(ChecklistTemplate).filter_by(scope_type="DESIGN", scope_id=design.id).first()
    cat_v = current_version(db, cat_t.id) if cat_t else None
    des_v = current_version(db, des_t.id) if des_t else None
    checks = []
    for v, label in ((cat_v, "category"), (des_v, "design")):
        if v:
            checks += [_check_dict(c, label) for c in db.query(ChecklistCheck).filter_by(version_id=v.id).order_by(ChecklistCheck.position)]
    return {"category_version": cat_v, "design_version": des_v, "checks": checks}


# ================================================================= inspections
def _item(db: Session, item_id: int) -> Item:
    it = db.get(Item, item_id)
    if it is None:
        raise NotFoundError(f"Item {item_id} not found")
    return it


def _inspection(db: Session, inspection_id: int) -> Inspection:
    i = db.get(Inspection, inspection_id)
    if i is None:
        raise NotFoundError(f"Inspection {inspection_id} not found")
    return i


def _open(db: Session, inspection_id: int) -> Inspection:
    i = _inspection(db, inspection_id)
    if i.status != "OPEN":
        raise ValidationError(f"Inspection {inspection_id} is already completed; start a new inspection to re-inspect")
    return i


def _start(db: Session, actor: User, item: Item, issue_id=None) -> Inspection:
    s = ledger.get_state(db, item.id)
    if not item.active:
        raise ValidationError(f"{item.display_id} is retired")
    if s["item_condition"] != "AWAITING_INSPECTION":
        raise ValidationError(f"{item.display_id} is {s['item_condition']}; only items awaiting inspection are inspected "
                              f"(a held item is sent for further inspection first)")
    if db.query(Inspection).filter_by(item_id=item.id, status="OPEN").first():
        raise ValidationError(f"{item.display_id} already has an open inspection")
    eff = effective_checklist(db, item)
    if not eff["checks"]:
        cat = db.get(Category, db.get(ElementDesign, item.design_id).category_id)
        raise ValidationError(f"No approved checklist exists for category {cat.code}; Engineering must approve one first")
    i = Inspection(item_id=item.id, issue_id=issue_id, started_by=actor.id,
                   category_version_id=eff["category_version"].id if eff["category_version"] else None,
                   design_version_id=eff["design_version"].id if eff["design_version"] else None)
    db.add(i)
    db.flush()
    audit.record(db, actor, "START_INSPECTION", "inspection", i.id, after={"item": item.display_id})
    return i


def start_inspection(db: Session, actor: User, item_id: int, issue_id=None) -> Inspection:
    permissions.require(actor, "inspection.perform")
    item = _item(db, item_id)
    with transaction(db):
        return _start(db, actor, item, issue_id)


def start_for_issue(db: Session, actor: User, issue_id: int) -> list[Inspection]:
    """One inspection per returned item (component-level results). Items already under inspection are skipped."""
    from . import issues
    permissions.require(actor, "inspection.perform")
    started = []
    with transaction(db):
        for item_id, st in issues.line_states(db, issue_id).items():
            if not st or st["state"] != "RETURNED":
                continue
            item = _item(db, item_id)
            s = ledger.get_state(db, item_id)
            if s["item_condition"] == "AWAITING_INSPECTION" and not db.query(Inspection).filter_by(
                    item_id=item_id, status="OPEN").first():
                started.append(_start(db, actor, item, issue_id))
    return started


def _checks_for(db: Session, i: Inspection) -> dict[int, ChecklistCheck]:
    ids = [v for v in (i.category_version_id, i.design_version_id) if v]
    return {c.id: c for c in db.query(ChecklistCheck).filter(ChecklistCheck.version_id.in_(ids or [0]))}


def record_result(db: Session, actor: User, inspection_id: int, check_id: int, *, result=None, value=None,
                  text=None, reason=None) -> InspectionResult:
    permissions.require(actor, "inspection.perform")
    i = _open(db, inspection_id)
    c = _checks_for(db, i).get(check_id)
    if c is None:
        raise ValidationError("That check is not part of this inspection's checklist")
    reason = clean(reason)
    res = (result or "").upper() or None
    if res == "NA":
        if not c.na_allowed:
            raise ValidationError(f"{c.title}: not applicable is not allowed for this check")
        if not reason:
            raise ValidationError(f"{c.title}: give the reason it is not applicable")
        val, txt = None, None
    elif c.check_type == "PASS_FAIL":
        if res not in ("PASS", "FAIL"):
            raise ValidationError(f"{c.title}: result must be PASS, FAIL or NA")
        if res == "FAIL" and not reason:
            raise ValidationError(f"{c.title}: describe the failure")
        val, txt = None, None
    elif c.check_type == "MEASUREMENT":
        try:
            val = float(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{c.title}: enter the measured value in {c.unit}")
        low_ok = c.min_value is None or val >= c.min_value
        high_ok = c.max_value is None or val <= c.max_value
        res = "PASS" if low_ok and high_ok else "FAIL"  # the limits decide, not the inspector
        if res == "FAIL" and not reason:
            reason = f"{val:g} {c.unit} is outside the limits [{'-' if c.min_value is None else f'{c.min_value:g}'}, " \
                     f"{'-' if c.max_value is None else f'{c.max_value:g}'}]"
        txt = None
    else:  # TEXT
        txt = clean(text)
        if not txt:
            raise ValidationError(f"{c.title}: enter the text")
        res, val = "PASS", None
    with transaction(db):
        row = db.query(InspectionResult).filter_by(inspection_id=i.id, check_id=c.id).first()
        before = {"result": row.result, "value": row.value} if row else None
        if row is None:
            row = InspectionResult(inspection_id=i.id, check_id=c.id, result=res)
            db.add(row)
        row.result, row.value, row.text, row.reason = res, val, txt, reason
        db.flush()
        audit.record(db, actor, "RECORD_RESULT", "inspection", i.id, before=before,
                     after={"check": c.code, "result": res, "value": val}, reason=reason)
    return row


def add_photo(db: Session, actor: User, inspection_id: int, check_id, filename: str, data: bytes) -> InspectionPhoto:
    permissions.require(actor, "inspection.perform")
    i = _open(db, inspection_id)
    if check_id is not None and check_id not in _checks_for(db, i):
        raise ValidationError("That check is not part of this inspection's checklist")
    meta = storage.save_upload("inspections", filename, data, allowed=storage.PHOTO_EXTENSIONS,
                               max_bytes=storage.MAX_PHOTO_BYTES)
    with transaction(db):
        p = InspectionPhoto(inspection_id=i.id, check_id=check_id, uploaded_by=actor.id, **meta)
        db.add(p)
        db.flush()
        audit.record(db, actor, "ADD_PHOTO", "inspection", i.id, after={"check_id": check_id, "sha256": meta["sha256"]})
    return p


def get_photo(db: Session, photo_id: int):
    p = db.get(InspectionPhoto, photo_id)
    if p is None:
        raise NotFoundError("Photo not found")
    return p, storage.read_stored(p.stored_path)


def complete_inspection(db: Session, actor: User, inspection_id: int, *, summary=None) -> Inspection:
    """Close the inspection. Every check needs an answer and mandatory photos must be present. A failed
    required check puts the item on HOLD (in the same transaction); otherwise the item stays awaiting
    inspection until Engineering approves its release."""
    permissions.require(actor, "inspection.perform")
    i = _open(db, inspection_id)
    item = _item(db, i.item_id)
    checks = _checks_for(db, i)
    results = {r.check_id: r for r in db.query(InspectionResult).filter_by(inspection_id=i.id)}
    unanswered = [c.title for c in checks.values() if c.id not in results]
    if unanswered:
        raise ValidationError("Not every check has a result: " + "; ".join(sorted(unanswered)))
    photos: dict[int | None, int] = {}
    for p in db.query(InspectionPhoto).filter_by(inspection_id=i.id):
        photos[p.check_id] = photos.get(p.check_id, 0) + 1
    missing = []
    for c in checks.values():
        r = results[c.id]
        needs = (c.evidence == "ALWAYS" and r.result != "NA") or (c.evidence == "ON_FAIL" and r.result == "FAIL")
        if needs and not photos.get(c.id):
            missing.append(c.title)
    if missing:
        raise ValidationError("Photo evidence is mandatory for: " + "; ".join(sorted(missing)))
    failed_required = [checks[cid].title for cid, r in results.items() if r.result == "FAIL" and checks[cid].required]
    s = ledger.get_state(db, item.id)
    if s["item_condition"] != "AWAITING_INSPECTION":
        raise ValidationError(f"{item.display_id} is now {s['item_condition']}; this inspection can no longer be completed")
    event_id = new_event()
    with transaction(db):
        i.status, i.completed_by, i.completed_at, i.summary = "COMPLETED", actor.id, _now(), clean(summary)
        i.outcome = "HOLD" if failed_required else "PASS"
        if failed_required:
            ledger.apply_condition(db, actor, [item], "HOLD",
                                   f"Inspection #{i.id} failed required checks: {', '.join(failed_required)}",
                                   event_id, source="INSPECTION")
        audit.record(db, actor, "COMPLETE_INSPECTION", "inspection", i.id,
                     after={"item": item.display_id, "outcome": i.outcome, "failed_required": failed_required})
    return i


def require_passing_inspection(db: Session, item: Item, state: dict) -> None:
    """Gate used by the ledger: an item becomes SERVICEABLE only after a PASS inspection completed since it
    last became awaiting inspection. (Engineering's release is the approval; this is the evidence.)"""
    since = db.execute(text(
        "SELECT MAX(l.txn_time) FROM ledger l WHERE l.item_id = :i AND l.action IN ('RECEIVE', 'RETURN', 'SET_CONDITION') "
        "AND NOT EXISTS (SELECT 1 FROM ledger r WHERE r.action = 'REVERSAL' AND r.ref_txn_id = l.txn_id)"),
        {"i": item.id}).scalar()
    last = db.query(Inspection).filter_by(item_id=item.id, status="COMPLETED").order_by(Inspection.completed_at.desc()).first()
    if last is None or last.outcome != "PASS":
        raise ValidationError(f"{item.display_id} cannot be released: it has no passing inspection. Inspect it "
                              f"(a held item first goes for further inspection)")
    if since is not None and str(last.completed_at) < str(since):
        raise ValidationError(f"{item.display_id} cannot be released: its last passing inspection is older than its "
                              f"current awaiting-inspection status. Inspect it again")


# ================================================================= views
def inspection_detail(db: Session, inspection_id: int) -> dict:
    i = _inspection(db, inspection_id)
    item = _item(db, i.item_id)
    users = {u.id: u.username for u in db.query(User.id, User.username)}
    checks = _checks_for(db, i)
    results = {r.check_id: r for r in db.query(InspectionResult).filter_by(inspection_id=i.id)}
    photos = db.query(InspectionPhoto).filter_by(inspection_id=i.id).order_by(InspectionPhoto.id).all()
    versions = {}
    for label, vid in (("category", i.category_version_id), ("design", i.design_version_id)):
        if vid:
            v = db.get(ChecklistVersion, vid)
            versions[label] = {"id": v.id, "version": v.version, "template": db.get(ChecklistTemplate, v.template_id).name}
    rows = []
    for c in sorted(checks.values(), key=lambda c: (c.version_id, c.position)):
        r = results.get(c.id)
        d = _check_dict(c, "design" if c.version_id == i.design_version_id else "category")
        d.update({"result": r.result if r else None, "value": r.value if r else None, "text": r.text if r else None,
                  "reason": r.reason if r else None,
                  "photos": [{"id": p.id, "file_name": p.file_name} for p in photos if p.check_id == c.id]})
        rows.append(d)
    return {"id": i.id, "item": {"id": item.id, "display_id": item.display_id, "system_id": item.system_id},
            "issue_id": i.issue_id, "status": i.status, "outcome": i.outcome, "started_by": users.get(i.started_by),
            "started_at": i.started_at, "completed_by": users.get(i.completed_by), "completed_at": i.completed_at,
            "summary": i.summary, "checklist_versions": versions, "checks": rows,
            "general_photos": [{"id": p.id, "file_name": p.file_name} for p in photos if p.check_id is None]}


def list_inspections(db: Session, *, status=None, item_id=None, limit=100, offset=0):
    q = db.query(Inspection)
    if status:
        q = q.filter(Inspection.status == status)
    if item_id:
        q = q.filter(Inspection.item_id == item_id)
    total = q.count()
    users = {u.id: u.username for u in db.query(User.id, User.username)}
    rows = []
    for i in q.order_by(Inspection.id.desc()).offset(offset).limit(min(limit, 500)):
        it = _item(db, i.item_id)
        rows.append({"id": i.id, "item_id": it.id, "display_id": it.display_id, "status": i.status, "outcome": i.outcome,
                     "started_by": users.get(i.started_by), "started_at": i.started_at, "completed_at": i.completed_at})
    return total, rows


def awaiting_queue(db: Session) -> list[dict]:
    """Items waiting for inspection, with their open inspection if one was started."""
    rows = db.execute(text(
        "SELECT i.id, i.display_id, i.system_id, d.code AS design_code, s.since FROM items i "
        "JOIN item_state s ON s.item_id = i.id JOIN element_designs d ON d.id = i.design_id "
        "WHERE i.active = 1 AND s.item_condition = 'AWAITING_INSPECTION' ORDER BY s.since")).mappings().all()
    open_ = {x.item_id: x.id for x in db.query(Inspection).filter_by(status="OPEN")}
    return [{**dict(r), "open_inspection_id": open_.get(r["id"])} for r in rows]
