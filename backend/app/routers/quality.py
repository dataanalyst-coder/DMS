from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import schemas
from ..deps import current_user, get_db
from ..models import ConsumableBatch, Item
from ..services import audit, batches, dispositions, inspections, labels, permissions
from ..services.common import transaction
from ..services.errors import NotFoundError

router = APIRouter(dependencies=[Depends(current_user)])


def _svg(svg: str) -> Response:
    return Response(svg, media_type="image/svg+xml", headers={"X-Content-Type-Options": "nosniff"})


# ---- consumable batches
@router.get("/batches")
def list_batches(q: str | None = None, design_id: int | None = None, status: str | None = None,
                 limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    total, rows = batches.list_batches(db, q=q, design_id=design_id, status=status, limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.post("/batches", status_code=201)
def receive_batch(body: schemas.BatchReceiveIn, user=Depends(current_user), db: Session = Depends(get_db)):
    b = batches.receive_batch(db, user, **body.model_dump())
    return batches.batch_dict(db, b, detail=True)


@router.get("/batches/{batch_id}")
def batch(batch_id: int, db: Session = Depends(get_db)):
    return batches.batch_dict(db, batches.get_batch(db, batch_id), detail=True)


@router.post("/batches/{batch_id}/hold")
def hold(batch_id: int, body: schemas.BatchQtyIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return batches.batch_dict(db, batches.hold_quantity(db, user, batch_id, body.quantity, reason=body.reason), detail=True)


@router.post("/batches/{batch_id}/release")
def release(batch_id: int, body: schemas.BatchQtyIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return batches.batch_dict(db, batches.release_hold(db, user, batch_id, body.quantity, reason=body.reason), detail=True)


@router.post("/batches/{batch_id}/adjust")
def adjust(batch_id: int, body: schemas.BatchAdjustIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return batches.batch_dict(db, batches.adjust(db, user, batch_id, body.delta, reason=body.reason), detail=True)


@router.post("/batches/{batch_id}/move")
def move(batch_id: int, body: schemas.BatchMoveIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return batches.batch_dict(db, batches.move_batch(db, user, batch_id, body.to_location_id, remarks=body.remarks), detail=True)


@router.post("/batch-ledger/{txn_id}/reverse", status_code=201)
def reverse_batch(txn_id: int, body: schemas.ReasonIn, user=Depends(current_user), db: Session = Depends(get_db)):
    rows = batches.reverse(db, user, txn_id, reason=body.reason)
    return {"event_id": rows[0].event_id, "reversed_txn_ids": [r.ref_txn_id for r in rows]}


@router.get("/batches/{batch_id}/label.svg")
def batch_label(batch_id: int, reprint: bool = False, user=Depends(current_user), db: Session = Depends(get_db)):
    b = db.get(ConsumableBatch, batch_id)
    if b is None:
        raise NotFoundError(f"Batch {batch_id} not found")
    if reprint:
        permissions.require(user, "item.label")
        with transaction(db):
            audit.record(db, user, "PRINT_LABEL", "batch", batch_id)
    d = batches.batch_dict(db, b)
    return _svg(labels.batch_label_svg(b.batch_code, d["design_code"], d["description"], b.uom, b.supplier_lot, b.expiry_date))


# ---- checklists
@router.get("/checklists")
def list_checklists(db: Session = Depends(get_db)):
    return inspections.list_templates(db)


@router.post("/checklists", status_code=201)
def create_checklist(body: schemas.ChecklistCreate, user=Depends(current_user), db: Session = Depends(get_db)):
    v = (inspections.apply_starter(db, user, body.scope_type, body.scope_id) if body.starter
         else inspections.create_template(db, user, body.scope_type, body.scope_id, body.name))
    return inspections.template_detail(db, v.template_id)


@router.get("/checklists/{template_id}")
def checklist(template_id: int, db: Session = Depends(get_db)):
    return inspections.template_detail(db, template_id)


@router.post("/checklists/{template_id}/versions", status_code=201)
def new_version(template_id: int, body: schemas.ReasonIn, user=Depends(current_user), db: Session = Depends(get_db)):
    inspections.new_version(db, user, template_id, reason=body.reason)
    return inspections.template_detail(db, template_id)


def _tpl_of_version(db, version_id):
    return inspections._version(db, version_id).template_id


@router.post("/checklist-versions/{version_id}/checks", status_code=201)
def add_check(version_id: int, body: schemas.CheckIn, user=Depends(current_user), db: Session = Depends(get_db)):
    inspections.add_check(db, user, version_id, **body.model_dump())
    return inspections.template_detail(db, _tpl_of_version(db, version_id))


@router.post("/checklist-versions/{version_id}/approve")
def approve_version(version_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    inspections.approve_version(db, user, version_id)
    return inspections.template_detail(db, _tpl_of_version(db, version_id))


@router.put("/checklist-checks/{check_id}")
def update_check(check_id: int, body: schemas.CheckUpdate, user=Depends(current_user), db: Session = Depends(get_db)):
    c = inspections.update_check(db, user, check_id, **body.model_dump(exclude_unset=True))
    return inspections.template_detail(db, _tpl_of_version(db, c.version_id))


@router.delete("/checklist-checks/{check_id}")
def remove_check(check_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    c = inspections._check(db, check_id)
    tpl = _tpl_of_version(db, c.version_id)
    inspections.remove_check(db, user, check_id)
    return inspections.template_detail(db, tpl)


@router.get("/items/{item_id}/checklist")
def item_checklist(item_id: int, db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None:
        raise NotFoundError(f"Item {item_id} not found")
    e = inspections.effective_checklist(db, item)
    return {"category_version": e["category_version"].id if e["category_version"] else None,
            "design_version": e["design_version"].id if e["design_version"] else None, "checks": e["checks"]}


# ---- inspections
@router.get("/inspections")
def list_inspections(status: str | None = None, item_id: int | None = None, limit: int = 100, offset: int = 0,
                     db: Session = Depends(get_db)):
    total, rows = inspections.list_inspections(db, status=status, item_id=item_id, limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.get("/inspections/queue")
def awaiting_queue(db: Session = Depends(get_db)):
    return inspections.awaiting_queue(db)


@router.post("/inspections", status_code=201)
def start_inspection(body: schemas.InspectionStart, user=Depends(current_user), db: Session = Depends(get_db)):
    return inspections.inspection_detail(db, inspections.start_inspection(db, user, body.item_id).id)


@router.post("/issues/{issue_id}/inspections", status_code=201)
def start_for_issue(issue_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    started = inspections.start_for_issue(db, user, issue_id)
    return {"started": [i.id for i in started]}


@router.get("/inspections/{inspection_id}")
def inspection(inspection_id: int, db: Session = Depends(get_db)):
    return inspections.inspection_detail(db, inspection_id)


@router.put("/inspections/{inspection_id}/results")
def record_result(inspection_id: int, body: schemas.ResultIn, user=Depends(current_user), db: Session = Depends(get_db)):
    inspections.record_result(db, user, inspection_id, body.check_id, result=body.result, value=body.value,
                              text=body.text, reason=body.reason)
    return inspections.inspection_detail(db, inspection_id)


@router.post("/inspections/{inspection_id}/photos", status_code=201)
async def add_inspection_photo(inspection_id: int, check_id: int | None = Form(None), file: UploadFile = File(...),
                               user=Depends(current_user), db: Session = Depends(get_db)):
    p = inspections.add_photo(db, user, inspection_id, check_id, file.filename or "photo", await file.read())
    return {"id": p.id, "check_id": p.check_id}


@router.get("/inspection-photos/{photo_id}")
def inspection_photo(photo_id: int, db: Session = Depends(get_db)):
    p, data = inspections.get_photo(db, photo_id)
    return Response(data, media_type=p.mime, headers={"Content-Disposition": f'inline; filename="{p.file_name}"',
                                                       "X-Content-Type-Options": "nosniff"})


@router.post("/inspections/{inspection_id}/complete")
def complete_inspection(inspection_id: int, body: schemas.CompleteIn, user=Depends(current_user),
                        db: Session = Depends(get_db)):
    inspections.complete_inspection(db, user, inspection_id, summary=body.summary)
    return inspections.inspection_detail(db, inspection_id)


# ---- held items, repair, rework, scrap
@router.get("/held")
def held_queue(db: Session = Depends(get_db)):
    return dispositions.held_queue(db)


@router.post("/items/{item_id}/disposition", status_code=201)
def decide(item_id: int, body: schemas.DispositionIn, user=Depends(current_user), db: Session = Depends(get_db)):
    d = dispositions.decide(db, user, item_id, body.decision, findings=body.findings, responsible=body.responsible,
                            inspection_id=body.inspection_id)
    return {"id": d.id, "decision": d.decision, "history": dispositions.item_history(db, item_id)}


@router.post("/repairs/{repair_id}/complete")
def complete_repair(repair_id: int, body: schemas.RepairComplete, user=Depends(current_user), db: Session = Depends(get_db)):
    r = dispositions.complete_repair(db, user, repair_id, work_done=body.work_done, measurements=body.measurements)
    return {"id": r.id, "status": r.status, "history": dispositions.item_history(db, r.item_id)}


@router.post("/items/{item_id}/rework-revision")
def rework(item_id: int, body: schemas.ReworkIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return dispositions.rework_revision(db, user, item_id, body.new_revision_id, reason=body.reason, repair_id=body.repair_id)


@router.post("/scrap-proposals/{proposal_id}/decide")
def decide_scrap(proposal_id: int, body: schemas.ScrapDecision, user=Depends(current_user), db: Session = Depends(get_db)):
    p = dispositions.decide_scrap(db, user, proposal_id, approve=body.approve, note=body.note)
    return {"id": p.id, "status": p.status}


@router.get("/items/{item_id}/quality-history")
def quality_history(item_id: int, db: Session = Depends(get_db)):
    return dispositions.item_history(db, item_id)
