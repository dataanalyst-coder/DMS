"""Reference copy of ME's mapping of product operations to approved assembly options.

ME owns the mapping (and the pouring weight); Die Management owns what an assembly is made of. Until the
ME integration contract is agreed, rows are entered by Engineering and flagged source='MANUAL'. Part IDs
are the shared part-master IDs: referenced as given (trimmed, upper-case), never created here."""
import re

from sqlalchemy.orm import Session

from ..models import TOOLING_STATUSES, AssemblyDefinition, AssemblyRevision, MeMapping
from . import assemblies, audit, permissions, recheck
from .common import clean, require_reason, transaction
from .errors import NotFoundError, ValidationError


def _ref(raw, what) -> str:
    v = re.sub(r"\s+", "", str(raw or "")).upper()
    if not v:
        raise ValidationError(f"{what} is required")
    return v


def _get(db: Session, mapping_id: int) -> MeMapping:
    m = db.get(MeMapping, mapping_id)
    if m is None:
        raise NotFoundError(f"Mapping {mapping_id} not found")
    return m


def _weight(w):
    if w is None:
        return None
    if float(w) <= 0:
        raise ValidationError("Pouring weight must be greater than zero (leave blank if unknown)")
    return float(w)


def _check_pinned(db: Session, definition_id: int, revision_id):
    if revision_id is None:
        return
    rev = db.get(AssemblyRevision, revision_id)
    if rev is None or rev.definition_id != definition_id:
        raise ValidationError("That revision does not belong to this assembly")
    if rev.status == "DRAFT":
        raise ValidationError("A mapping cannot point at a draft revision")


def _unselect_others(db: Session, m: MeMapping):
    for o in db.query(MeMapping).filter(
            MeMapping.part_id == m.part_id, MeMapping.routing_revision == m.routing_revision,
            MeMapping.operation_id == m.operation_id, MeMapping.id != m.id, MeMapping.selected.is_(True)):
        o.selected = False


def _snap(m: MeMapping) -> dict:
    return {"part_id": m.part_id, "routing_revision": m.routing_revision, "operation_id": m.operation_id,
            "definition_id": m.definition_id, "pinned_revision": m.definition_revision_id,
            "selected": m.selected, "tooling_status": m.tooling_status, "active": m.active,
            "pouring_weight_kg": m.pouring_weight_kg, "pouring_weight_revision": m.pouring_weight_revision}


def create_mapping(db: Session, actor, *, part_id, routing_revision, operation_id, definition_id,
                   definition_revision_id=None, selected=False, tooling_status="READY",
                   pouring_weight_kg=None, pouring_weight_revision=None, external_ref=None) -> MeMapping:
    permissions.require(actor, "mapping.manage")
    part, rrev, op = _ref(part_id, "Part ID"), _ref(routing_revision, "Routing revision"), _ref(operation_id, "Operation ID")
    assemblies.get_definition(db, definition_id)
    if tooling_status not in TOOLING_STATUSES:
        raise ValidationError(f"tooling_status must be one of {', '.join(TOOLING_STATUSES)}")
    _check_pinned(db, definition_id, definition_revision_id)
    if db.query(MeMapping).filter_by(part_id=part, routing_revision=rrev, operation_id=op,
                                     definition_id=definition_id).first():
        raise ValidationError("This part/routing/operation is already mapped to that assembly")
    with transaction(db):
        m = MeMapping(part_id=part, routing_revision=rrev, operation_id=op, definition_id=definition_id,
                      definition_revision_id=definition_revision_id, selected=bool(selected),
                      tooling_status=tooling_status, pouring_weight_kg=_weight(pouring_weight_kg),
                      pouring_weight_revision=clean(pouring_weight_revision), external_ref=clean(external_ref),
                      created_by=actor.id)
        db.add(m)
        db.flush()
        if m.selected:
            _unselect_others(db, m)
        audit.record(db, actor, "CREATE", "me_mapping", m.id, after=_snap(m))
        recheck.flag(db, actor, "A new assembly option was mapped to this operation",
                     operation=(part, rrev, op))
    return m


def update_mapping(db: Session, actor, mapping_id: int, *, fields_set: set, reason=None, **vals) -> MeMapping:
    """Update the mutable parts of a mapping. Deactivating needs a reason; nothing is ever deleted."""
    permissions.require(actor, "mapping.manage")
    m = _get(db, mapping_id)
    before = _snap(m)
    if "tooling_status" in fields_set and vals["tooling_status"] not in TOOLING_STATUSES:
        raise ValidationError(f"tooling_status must be one of {', '.join(TOOLING_STATUSES)}")
    if "definition_revision_id" in fields_set:
        _check_pinned(db, m.definition_id, vals["definition_revision_id"])
    if "active" in fields_set and not vals["active"]:
        reason = require_reason(reason, "deactivating a mapping")
    with transaction(db):
        if "definition_revision_id" in fields_set:
            m.definition_revision_id = vals["definition_revision_id"]
        if "tooling_status" in fields_set:
            m.tooling_status = vals["tooling_status"]
        if "pouring_weight_kg" in fields_set:
            m.pouring_weight_kg = _weight(vals["pouring_weight_kg"])
        if "pouring_weight_revision" in fields_set:
            m.pouring_weight_revision = clean(vals["pouring_weight_revision"])
        if "external_ref" in fields_set:
            m.external_ref = clean(vals["external_ref"])
        if "active" in fields_set:
            m.active = bool(vals["active"])
        if "selected" in fields_set:
            m.selected = bool(vals["selected"])
            if m.selected:
                _unselect_others(db, m)
        audit.record(db, actor, "UPDATE", "me_mapping", m.id, before=before, after=_snap(m), reason=reason)
        if before != _snap(m):
            recheck.flag(db, actor, "ME mapping changed" + (f": {reason}" if reason else ""), mapping_id=m.id,
                         operation=(m.part_id, m.routing_revision, m.operation_id))
    return m


def _effective_revision(db: Session, m: MeMapping):
    if m.definition_revision_id:
        return db.get(AssemblyRevision, m.definition_revision_id)
    return assemblies.current_approved(db, m.definition_id)


def mapping_dict(db: Session, m: MeMapping) -> dict:
    d = db.get(AssemblyDefinition, m.definition_id)
    rev = _effective_revision(db, m)
    reasons = []
    if not m.active:
        reasons.append("mapping is inactive")
    if not d.active:
        reasons.append("assembly is inactive")
    if rev is None or rev.status != "APPROVED":
        reasons.append("assembly has no approved revision" if m.definition_revision_id is None
                       else f"pinned revision {rev.revision if rev else '?'} is not approved")
    if m.tooling_status != "READY":
        reasons.append("tooling is marked to be developed")
    return {"id": m.id, "part_id": m.part_id, "routing_revision": m.routing_revision,
            "operation_id": m.operation_id, "definition_id": d.id, "assembly_code": d.code,
            "assembly_description": d.description, "pinned_revision_id": m.definition_revision_id,
            "effective_revision": rev.revision if rev else None,
            "effective_revision_id": rev.id if rev else None, "selected": m.selected,
            "tooling_status": m.tooling_status, "pouring_weight_kg": m.pouring_weight_kg,
            "pouring_weight_revision": m.pouring_weight_revision, "source": m.source,
            "external_ref": m.external_ref, "active": m.active,
            "selectable": not reasons, "not_selectable_because": reasons}


def list_mappings(db: Session, *, part_id=None, operation_id=None, definition_id=None, active=None,
                  limit=200, offset=0) -> tuple[int, list[dict]]:
    q = db.query(MeMapping)
    if part_id:
        q = q.filter(MeMapping.part_id == _ref(part_id, "Part ID"))
    if operation_id:
        q = q.filter(MeMapping.operation_id == _ref(operation_id, "Operation ID"))
    if definition_id:
        q = q.filter(MeMapping.definition_id == definition_id)
    if active is not None:
        q = q.filter(MeMapping.active == active)
    total = q.count()
    rows = q.order_by(MeMapping.part_id, MeMapping.operation_id, MeMapping.id).offset(offset).limit(
        min(limit, 1000)).all()
    return total, [mapping_dict(db, m) for m in rows]


def options(db: Session, *, part_id, routing_revision, operation_id) -> list[dict]:
    """The assembly options ME has linked to this operation. Only selectable ones may be chosen; the
    rest are shown with the reason, never silently dropped."""
    part, rrev, op = _ref(part_id, "Part ID"), _ref(routing_revision, "Routing revision"), _ref(operation_id, "Operation ID")
    rows = db.query(MeMapping).filter_by(part_id=part, routing_revision=rrev, operation_id=op,
                                         active=True).order_by(MeMapping.id).all()
    return [mapping_dict(db, m) for m in rows]
