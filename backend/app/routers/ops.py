from datetime import datetime

from fastapi import APIRouter, Body, Depends, Request, Response
from sqlalchemy.orm import Session

from .. import schemas
from ..deps import current_user, get_db
from ..services import counts, excel_io, issues, reports, usage
from ..services.errors import NotFoundError

router = APIRouter(dependencies=[Depends(current_user)])


# ---- OMS usage feed and integration console
@router.post("/integration/usage")
def ingest_usage(response: Response, payload: dict = Body(...), user=Depends(current_user), db: Session = Depends(get_db)):
    """The OMS actual-usage feed. Idempotent: replays and stale versions answer 200 and change nothing."""
    res = usage.ingest(db, user, payload)
    if res["result"] == "REJECTED":  # the error is shown to the sender, and the event is kept with its reason
        response.status_code = 422
        return {"detail": res["event"]["reason"], **res}
    response.status_code = 200 if res["result"] in ("DUPLICATE", "STALE") else 201
    return res


@router.get("/integration/usage")
def list_usage(wo_id: str | None = None, build_id: int | None = None, status: str | None = None,
               source_event_id: str | None = None, limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    total, rows = usage.list_events(db, wo_id=wo_id, build_id=build_id, status=status, source_event_id=source_event_id,
                                    limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.get("/integration/exceptions")
def usage_exceptions(db: Session = Depends(get_db)):
    return usage.exceptions(db)


@router.get("/integration/usage/{event_id}")
def usage_event(event_id: int, db: Session = Depends(get_db)):
    return usage.event_dict(db, usage._get(db, event_id))


@router.post("/integration/usage/{event_id}/retry")
def retry_usage(event_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    return usage.retry(db, user, event_id)


@router.post("/integration/usage/{event_id}/resolve")
def resolve_usage(event_id: int, body: schemas.ResolveUsage, user=Depends(current_user), db: Session = Depends(get_db)):
    return usage.resolve(db, user, event_id, **body.model_dump())


@router.get("/oms/associations")
def associations(wo_id: str, db: Session = Depends(get_db)):
    """Issue association for OMS: WO/operation, build, constituent items and the periods they were fitted."""
    return usage.associations(db, wo_id)


@router.get("/usage/assembly")
def assembly_usage(build_id: int | None = None, wo_id: str | None = None, definition_id: int | None = None,
                   db: Session = Depends(get_db)):
    return usage.assembly_usage(db, build_id=build_id, wo_id=wo_id, definition_id=definition_id)


@router.get("/usage/items/{item_id}")
def item_usage(item_id: int, db: Session = Depends(get_db)):
    return usage.item_usage(db, item_id)


@router.get("/usage/output")
def foundry_output(date_from: datetime | None = None, date_to: datetime | None = None, db: Session = Depends(get_db)):
    return usage.foundry_output(db, date_from=date_from, date_to=date_to)


@router.post("/issues/{issue_id}/replace-component")
def replace_component(issue_id: int, body: schemas.ReplaceInProduction, user=Depends(current_user),
                      db: Session = Depends(get_db)):
    res = issues.replace_in_production(db, user, issue_id, body.outgoing_item_id, body.incoming_item_id, reason=body.reason)
    return {**res, "issue": issues.issue_detail(db, issue_id)}


# ---- stock counts
@router.get("/counts")
def list_counts(db: Session = Depends(get_db)):
    return counts.list_counts(db)


@router.post("/counts", status_code=201)
def start_count(body: schemas.CountStart, user=Depends(current_user), db: Session = Depends(get_db)):
    return counts.count_detail(db, counts.start_count(db, user, body.location_id, body.note).id)


@router.get("/counts/{count_id}")
def count(count_id: int, db: Session = Depends(get_db)):
    return counts.count_detail(db, count_id)


@router.post("/counts/{count_id}/scan")
def count_scan(count_id: int, body: schemas.CountScan, user=Depends(current_user), db: Session = Depends(get_db)):
    res = counts.scan(db, user, count_id, body.code, body.quantity)
    return {**res, "report": counts.report(db, count_id)}


@router.post("/counts/{count_id}/complete")
def count_complete(count_id: int, body: schemas.CountComplete, user=Depends(current_user), db: Session = Depends(get_db)):
    return counts.complete(db, user, count_id, body.note)


@router.post("/counts/{count_id}/cancel")
def count_cancel(count_id: int, body: schemas.ReasonIn, user=Depends(current_user), db: Session = Depends(get_db)):
    c = counts.cancel(db, user, count_id, reason=body.reason)
    return {"id": c.id, "status": c.status}


@router.post("/counts/{count_id}/apply")
def count_apply(count_id: int, body: schemas.CountApply, user=Depends(current_user), db: Session = Depends(get_db)):
    return counts.apply(db, user, count_id, [a.model_dump() for a in body.item_actions],
                        [b.model_dump() for b in body.batch_adjustments])


# ---- reports
@router.get("/reports")
def report_names():
    return sorted(reports.REPORTS)


@router.get("/reports/{name}")
def report(name: str, request: Request, format: str = "json", db: Session = Depends(get_db)):
    ints = {"location_id", "days"}
    params = {k: (int(v) if k in ints and v != "" else v) for k, v in request.query_params.items() if k != "format"}
    rep = reports.run(db, name, **params)
    if format == "xlsx":
        return Response(excel_io.rows_xlsx(name, rep["columns"], rep["rows"]), media_type=excel_io.XLSX,
                        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
    if format != "json":
        raise NotFoundError("format must be json or xlsx")
    return rep
