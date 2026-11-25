"""Controlled documents: drawings, work instructions, inspection/repair records.

Uploading creates a DRAFT version only. Approval is a separate Engineering action, and neither
upload nor approval changes any item's condition or any design's approval (spec section 6)."""
from sqlalchemy.orm import Session

from ..models import (DOC_TYPES, AssemblyDefinition, BuildComponent, Document, DocumentLink, DocumentVersion,
                      ElementDesign, Item, User, _now)
from . import audit, permissions, storage
from .common import clean, normalize_id, require_reason, transaction
from .designs import _next_label
from .errors import NotFoundError, ValidationError


def _get(db: Session, document_id: int) -> Document:
    d = db.get(Document, document_id)
    if d is None:
        raise NotFoundError(f"Document {document_id} not found")
    return d


def _ver(v: DocumentVersion, users: dict) -> dict:
    return {"id": v.id, "document_id": v.document_id, "revision": v.revision, "status": v.status,
            "file_name": v.file_name, "mime": v.mime, "size": v.size, "sha256": v.sha256,
            "change_reason": v.change_reason, "author": users.get(v.author_id),
            "created_at": v.created_at, "approved_by": users.get(v.approved_by),
            "approved_at": v.approved_at}


def _users(db: Session) -> dict:
    return {u.id: u.username for u in db.query(User.id, User.username)}


def current_version(db: Session, document_id: int) -> DocumentVersion | None:
    """The approved version that new work must use."""
    return db.query(DocumentVersion).filter(
        DocumentVersion.document_id == document_id, DocumentVersion.status == "APPROVED"
    ).order_by(DocumentVersion.id.desc()).first()


def create_document(db: Session, actor: User, *, doc_key, doc_type, title) -> Document:
    permissions.require(actor, "document.upload")
    key = normalize_id(doc_key)
    if doc_type not in DOC_TYPES:
        raise ValidationError(f"doc_type must be one of {', '.join(DOC_TYPES)}")
    if not clean(title):
        raise ValidationError("Title is required")
    if db.query(Document).filter(Document.doc_key == key).first():
        raise ValidationError(f"Document {key} already exists")
    with transaction(db):
        d = Document(doc_key=key, doc_type=doc_type, title=title.strip(), created_by=actor.id)
        db.add(d)
        db.flush()
        audit.record(db, actor, "CREATE", "document", d.id, after={"doc_key": key, "type": doc_type})
    return d


def add_version(db: Session, actor: User, document_id: int, *, filename, data: bytes,
                change_reason=None, revision=None) -> DocumentVersion:
    permissions.require(actor, "document.upload")
    doc = _get(db, document_id)
    existing = db.query(DocumentVersion).filter(DocumentVersion.document_id == doc.id).all()
    if existing:
        change_reason = require_reason(change_reason, "a new document revision")
    if any(v.status == "DRAFT" for v in existing):
        raise ValidationError("This document already has a draft version; approve or obsolete it first")
    label = clean(revision) or _next_label([v.revision for v in existing])
    if any(v.revision == label for v in existing):
        raise ValidationError(f"Revision {label} already exists")
    meta = storage.save_upload("documents", filename, data, allowed=storage.DOC_EXTENSIONS,
                               max_bytes=storage.MAX_DOC_BYTES)
    with transaction(db):
        v = DocumentVersion(document_id=doc.id, revision=label, status="DRAFT", author_id=actor.id,
                            change_reason=clean(change_reason) or "Initial version", **meta)
        db.add(v)
        db.flush()
        audit.record(db, actor, "UPLOAD", "document", doc.id,
                     after={"revision": label, "sha256": meta["sha256"], "file": meta["file_name"]},
                     reason=change_reason)
    return v


def _get_version(db: Session, version_id: int) -> DocumentVersion:
    v = db.get(DocumentVersion, version_id)
    if v is None:
        raise NotFoundError(f"Document version {version_id} not found")
    return v


def approve_version(db: Session, actor: User, version_id: int) -> DocumentVersion:
    permissions.require(actor, "document.approve")
    v = _get_version(db, version_id)
    if v.status != "DRAFT":
        raise ValidationError(f"Only a DRAFT version can be approved (this one is {v.status})")
    with transaction(db):
        for old in db.query(DocumentVersion).filter(
                DocumentVersion.document_id == v.document_id, DocumentVersion.status == "APPROVED"):
            audit.record(db, actor, "SUPERSEDE", "document_version", old.id,
                         before={"status": "APPROVED"}, after={"status": "OBSOLETE"},
                         reason=f"Superseded by revision {v.revision}")
            old.status = "OBSOLETE"
        v.status, v.approved_by, v.approved_at = "APPROVED", actor.id, _now()
        audit.record(db, actor, "APPROVE", "document_version", v.id, before={"status": "DRAFT"},
                     after={"status": "APPROVED"})
    return v


def obsolete_version(db: Session, actor: User, version_id: int, *, reason) -> DocumentVersion:
    permissions.require(actor, "document.approve")
    reason = require_reason(reason, "obsoleting a document version")
    v = _get_version(db, version_id)
    if v.status == "OBSOLETE":
        raise ValidationError("Version is already obsolete")
    with transaction(db):
        audit.record(db, actor, "OBSOLETE", "document_version", v.id, before={"status": v.status},
                     after={"status": "OBSOLETE"}, reason=reason)
        v.status = "OBSOLETE"
    return v


def _target_exists(db: Session, target_type: str, target_id: int):
    model = {"DESIGN": ElementDesign, "ITEM": Item, "ASSEMBLY": AssemblyDefinition}.get(target_type)
    if model is None:
        raise ValidationError("target_type must be DESIGN, ITEM or ASSEMBLY")
    if db.get(model, target_id) is None:
        raise NotFoundError(f"{target_type} {target_id} not found")


def link(db: Session, actor: User, document_id: int, target_type: str, target_id: int) -> DocumentLink:
    permissions.require(actor, "document.upload")
    _get(db, document_id)
    _target_exists(db, target_type, target_id)
    if db.query(DocumentLink).filter_by(document_id=document_id, target_type=target_type,
                                        target_id=target_id).first():
        raise ValidationError("Already linked")
    with transaction(db):
        l = DocumentLink(document_id=document_id, target_type=target_type, target_id=target_id)
        db.add(l)
        db.flush()
        audit.record(db, actor, "LINK", "document", document_id,
                     after={"target_type": target_type, "target_id": target_id})
    return l


def unlink(db: Session, actor: User, document_id: int, target_type: str, target_id: int) -> None:
    permissions.require(actor, "document.upload")
    l = db.query(DocumentLink).filter_by(document_id=document_id, target_type=target_type,
                                         target_id=target_id).first()
    if l is None:
        raise NotFoundError("Link not found")
    with transaction(db):
        db.delete(l)
        audit.record(db, actor, "UNLINK", "document", document_id,
                     before={"target_type": target_type, "target_id": target_id})


def document_detail(db: Session, document_id: int) -> dict:
    d = _get(db, document_id)
    users = _users(db)
    versions = db.query(DocumentVersion).filter(DocumentVersion.document_id == d.id).order_by(
        DocumentVersion.id).all()
    cur = current_version(db, d.id)
    return {"id": d.id, "doc_key": d.doc_key, "doc_type": d.doc_type, "title": d.title,
            "active": d.active, "current_version_id": cur.id if cur else None,
            "versions": [_ver(v, users) for v in versions],
            "links": [{"target_type": l.target_type, "target_id": l.target_id}
                      for l in db.query(DocumentLink).filter(DocumentLink.document_id == d.id)]}


def list_documents(db: Session, *, q=None, doc_type=None, target_type=None, target_id=None,
                   limit=100, offset=0) -> tuple[int, list[dict]]:
    qry = db.query(Document)
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(Document.doc_key.ilike(like) | Document.title.ilike(like))
    if doc_type:
        qry = qry.filter(Document.doc_type == doc_type)
    if target_type and target_id:
        ids = [l.document_id for l in db.query(DocumentLink).filter_by(
            target_type=target_type, target_id=target_id)]
        qry = qry.filter(Document.id.in_(ids))
    total = qry.count()
    out = []
    for d in qry.order_by(Document.doc_key).offset(offset).limit(min(limit, 500)):
        cur = current_version(db, d.id)
        out.append({"id": d.id, "doc_key": d.doc_key, "doc_type": d.doc_type, "title": d.title,
                    "current_revision": cur.revision if cur else None,
                    "current_version_id": cur.id if cur else None})
    return total, out


def versions_in_force(db: Session, build) -> list[DocumentVersion]:
    """The current APPROVED version of every document that applies to this build: its assembly, its component
    designs and its component items. Snapshotted at issue so past issues keep the version they used."""
    items = [c.item_id for c in db.query(BuildComponent).filter(BuildComponent.build_id == build.id,
                                                              BuildComponent.removed_at.is_(None))]
    designs = {i.design_id for i in db.query(Item).filter(Item.id.in_(items or [0]))}
    wanted = {("ASSEMBLY", build.definition_id)} | {("ITEM", i) for i in items} | {("DESIGN", d) for d in designs}
    doc_ids = {l.document_id for l in db.query(DocumentLink) if (l.target_type, l.target_id) in wanted}
    out = []
    for d in sorted(doc_ids):
        v = current_version(db, d)
        if v is not None:
            out.append(v)
    return out


def read_version_file(db: Session, version_id: int) -> tuple[DocumentVersion, bytes]:
    v = _get_version(db, version_id)
    return v, storage.read_stored(v.stored_path)
