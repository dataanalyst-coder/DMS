from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import schemas
from ..deps import current_user, get_db
from ..services import counts, documents, excel_io, imports, issues, items, ledger, recheck, setups, usage
from ..services.dispositions import quality_counts
from ..services.errors import ValidationError

router = APIRouter(dependencies=[Depends(current_user)])


def _file_response(data: bytes, mime: str, name: str, inline=False) -> Response:
    disp = "inline" if inline else "attachment"
    return Response(data, media_type=mime, headers={
        "Content-Disposition": f'{disp}; filename="{name}"', "X-Content-Type-Options": "nosniff"})


# ---- items
@router.get("/items")
def search_items(q: str | None = None, design_id: int | None = None, category_id: int | None = None,
                 condition: str | None = None, custody: str | None = None,
                 location_id: int | None = None, active: bool | None = True,
                 home_mismatch: bool | None = None, limit: int = 50, offset: int = 0,
                 db: Session = Depends(get_db)):
    total, rows = items.search_items(db, q=q, design_id=design_id, category_id=category_id,
                                     condition=condition, custody=custody, location_id=location_id,
                                     active=active, home_mismatch=home_mismatch, limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.post("/items", status_code=201)
def register_item(body: schemas.ItemIn, user=Depends(current_user), db: Session = Depends(get_db)):
    it = items.register_item(db, user, **body.model_dump())
    return items.item_detail(db, it.id)


@router.get("/items/{item_id}")
def item(item_id: int, db: Session = Depends(get_db)):
    d = items.item_detail(db, item_id)
    _, docs = documents.list_documents(db, target_type="ITEM", target_id=item_id)
    _, design_docs = documents.list_documents(db, target_type="DESIGN", target_id=d["design_id"])
    return {**d, "documents": docs, "design_documents": design_docs}


@router.put("/items/{item_id}")
def update_item(item_id: int, body: schemas.ItemUpdate, user=Depends(current_user),
                db: Session = Depends(get_db)):
    sent = body.model_dump(exclude_unset=True)
    items.update_item(db, user, item_id, fields_set=set(sent), **sent)
    return items.item_detail(db, item_id)


@router.post("/items/{item_id}/retire")
def retire(item_id: int, body: schemas.ReasonIn, user=Depends(current_user), db: Session = Depends(get_db)):
    items.retire_item(db, user, item_id, reason=body.reason)
    return items.item_detail(db, item_id)


@router.post("/items/{item_id}/label")
def label(item_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    return items.label_data(db, user, item_id)


@router.get("/lookup")
def lookup(code: str, db: Session = Depends(get_db)):
    return items.lookup(db, code)


@router.post("/items/{item_id}/aliases", status_code=201)
def request_alias(item_id: int, body: schemas.AliasIn, user=Depends(current_user), db: Session = Depends(get_db)):
    a = items.request_alias(db, user, item_id, body.alias, reason=body.reason)
    return {"id": a.id, "alias": a.alias, "status": a.status}


@router.post("/aliases/{alias_id}/approve")
def approve_alias(alias_id: int, body: schemas.AliasDecisionIn, user=Depends(current_user),
                  db: Session = Depends(get_db)):
    a = items.approve_alias(db, user, alias_id, body.note)
    return {"id": a.id, "alias": a.alias, "status": a.status}


@router.post("/aliases/{alias_id}/reject")
def reject_alias(alias_id: int, body: schemas.AliasDecisionIn, user=Depends(current_user),
                 db: Session = Depends(get_db)):
    a = items.reject_alias(db, user, alias_id, body.note)
    return {"id": a.id, "alias": a.alias, "status": a.status}


@router.post("/items/{item_id}/photos", status_code=201)
async def add_photo(item_id: int, kind: str = Form(...), file: UploadFile = File(...),
                    user=Depends(current_user), db: Session = Depends(get_db)):
    p = items.add_photo(db, user, item_id, kind, file.filename or "photo", await file.read())
    return {"id": p.id, "kind": p.kind, "file_name": p.file_name}


@router.get("/photos/{photo_id}")
def photo(photo_id: int, db: Session = Depends(get_db)):
    p, data = items.get_photo(db, photo_id)
    return _file_response(data, p.mime, p.file_name, inline=True)


# ---- movement / condition / ledger
@router.post("/items/move")
def move(body: schemas.MoveIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return ledger.move_items(db, user, body.item_ids, body.to_location_id, remarks=body.remarks)


@router.post("/items/condition")
def condition(body: schemas.ConditionIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return ledger.set_condition(db, user, body.item_ids, body.to_condition, reason=body.reason)


@router.get("/ledger")
def ledger_list(item_id: int | None = None, event_id: str | None = None, user_name: str | None = None,
                action: str | None = None, date_from: datetime | None = None,
                date_to: datetime | None = None, limit: int = 200, offset: int = 0,
                db: Session = Depends(get_db)):
    total, rows = ledger.ledger_rows(db, item_id=item_id, event_id=event_id, user=user_name,
                                     action=action, date_from=date_from, date_to=date_to,
                                     limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.post("/ledger/{txn_id}/reverse", status_code=201)
def reverse(txn_id: int, body: schemas.ReasonIn, user=Depends(current_user), db: Session = Depends(get_db)):
    rows = ledger.reverse(db, user, txn_id, reason=body.reason)
    return {"event_id": rows[0].event_id, "reversed_txn_ids": [r.ref_txn_id for r in rows]}


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    return {**ledger.dashboard_counts(db), **issues.issue_counts(db), "wos_needing_recheck": len(recheck.flagged(db)), **quality_counts(db),
            "integration_exceptions": usage.exception_count(db), "open_counts": counts.open_count(db), **setups.counts(db)}


# ---- exports
@router.get("/export/ledger")
def export_ledger(item_id: int | None = None, user_name: str | None = None, action: str | None = None,
                  date_from: datetime | None = None, date_to: datetime | None = None,
                  db: Session = Depends(get_db)):
    _, rows = ledger.ledger_rows(db, item_id=item_id, user=user_name, action=action,
                                 date_from=date_from, date_to=date_to, limit=5000)
    return _file_response(excel_io.ledger_xlsx(rows), excel_io.XLSX, "ledger.xlsx")


@router.get("/export/items")
def export_items(q: str | None = None, condition: str | None = None, custody: str | None = None,
                 location_id: int | None = None, db: Session = Depends(get_db)):
    rows, offset = [], 0
    while True:
        total, page = items.search_items(db, q=q, condition=condition, custody=custody,
                                         location_id=location_id, limit=500, offset=offset)
        rows += page
        offset += 500
        if offset >= total:
            break
    return _file_response(excel_io.items_xlsx(rows), excel_io.XLSX, "items.xlsx")


# ---- import
@router.get("/templates/items")
def template():
    return _file_response(excel_io.items_template(), excel_io.XLSX, excel_io.ITEMS_TEMPLATE)


@router.post("/import/items")
async def import_items(commit: bool = False, file: UploadFile = File(...), user=Depends(current_user),
                       db: Session = Depends(get_db)):
    columns, rows = excel_io.read_rows(await file.read())
    return imports.import_items(db, user, columns, rows, commit=commit, source_file=file.filename)


@router.get("/import/review")
def review(status: str | None = "PENDING", db: Session = Depends(get_db)):
    return imports.list_review(db, status)


@router.post("/import/review/{review_id}/resolve")
def resolve(review_id: int, body: schemas.ResolveIn, user=Depends(current_user), db: Session = Depends(get_db)):
    it = imports.resolve_review(db, user, review_id, display_id=body.display_id, note=body.note)
    return items.item_detail(db, it.id)


@router.post("/import/review/{review_id}/reject")
def reject(review_id: int, body: schemas.RejectIn, user=Depends(current_user), db: Session = Depends(get_db)):
    r = imports.reject_review(db, user, review_id, note=body.note)
    return {"id": r.id, "status": r.status}


# ---- documents
@router.get("/documents")
def list_documents(q: str | None = None, doc_type: str | None = None, target_type: str | None = None,
                   target_id: int | None = None, limit: int = 100, offset: int = 0,
                   db: Session = Depends(get_db)):
    total, rows = documents.list_documents(db, q=q, doc_type=doc_type, target_type=target_type,
                                           target_id=target_id, limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.post("/documents", status_code=201)
def create_document(body: schemas.DocumentIn, user=Depends(current_user), db: Session = Depends(get_db)):
    d = documents.create_document(db, user, **body.model_dump())
    return documents.document_detail(db, d.id)


@router.get("/documents/{document_id}")
def document(document_id: int, db: Session = Depends(get_db)):
    return documents.document_detail(db, document_id)


@router.post("/documents/{document_id}/versions", status_code=201)
async def add_version(document_id: int, change_reason: str | None = Form(None),
                      revision: str | None = Form(None), file: UploadFile = File(...),
                      user=Depends(current_user), db: Session = Depends(get_db)):
    documents.add_version(db, user, document_id, filename=file.filename or "file",
                          data=await file.read(), change_reason=change_reason, revision=revision)
    return documents.document_detail(db, document_id)


@router.post("/document-versions/{version_id}/approve")
def approve_version(version_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    v = documents.approve_version(db, user, version_id)
    return documents.document_detail(db, v.document_id)


@router.post("/document-versions/{version_id}/obsolete")
def obsolete_version(version_id: int, body: schemas.ReasonIn, user=Depends(current_user),
                     db: Session = Depends(get_db)):
    v = documents.obsolete_version(db, user, version_id, reason=body.reason)
    return documents.document_detail(db, v.document_id)


@router.get("/document-versions/{version_id}/file")
def version_file(version_id: int, db: Session = Depends(get_db)):
    v, data = documents.read_version_file(db, version_id)
    return _file_response(data, v.mime, v.file_name)


@router.post("/documents/{document_id}/links", status_code=201)
def link(document_id: int, body: schemas.LinkIn, user=Depends(current_user), db: Session = Depends(get_db)):
    documents.link(db, user, document_id, body.target_type, body.target_id)
    return documents.document_detail(db, document_id)


@router.delete("/documents/{document_id}/links/{target_type}/{target_id}")
def unlink(document_id: int, target_type: str, target_id: int, user=Depends(current_user),
           db: Session = Depends(get_db)):
    documents.unlink(db, user, document_id, target_type, target_id)
    return documents.document_detail(db, document_id)
