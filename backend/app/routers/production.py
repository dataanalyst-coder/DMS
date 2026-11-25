from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import schemas
from ..deps import current_user, get_db
from ..models import Build, Item, Location
from ..services import (audit, builds, assemblies, issues, labels, permissions, readiness, recheck, scan)
from ..services.errors import NotFoundError
from ..services.common import transaction

router = APIRouter(dependencies=[Depends(current_user)])


def _svg(svg: str) -> Response:
    return Response(svg, media_type="image/svg+xml", headers={"X-Content-Type-Options": "nosniff"})


# ---- scan: one resolver for QR / scanner / typed codes
@router.get("/scan")
def scan_code(code: str, db: Session = Depends(get_db)):
    return scan.resolve(db, code)


# ---- readiness (the OMS release gate; this API is what OMS will call)
@router.post("/readiness/checks", status_code=201)
def run_check(body: schemas.ReadinessIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return readiness.run_check(db, user, **body.model_dump())


@router.get("/readiness/checks")
def list_checks(wo_id: str | None = None, limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    total, rows = readiness.list_checks(db, wo_id=wo_id, limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.get("/readiness/checks/{check_id}")
def get_check(check_id: int, db: Session = Depends(get_db)):
    return readiness.get_check(db, check_id)


@router.get("/readiness/recheck")
def needs_recheck(db: Session = Depends(get_db)):
    """Open WOs whose latest READY check no longer holds for certain. Nothing is cancelled."""
    return [readiness.check_dict(db, c) for c in recheck.flagged(db)]


@router.post("/readiness/wo/{wo_id}/close")
def close_wo(wo_id: str, user=Depends(current_user), db: Session = Depends(get_db)):
    return {"closed_checks": readiness.close_wo(db, user, wo_id)}


# ---- issue / acknowledge / return
@router.get("/builds/{build_id}/issue-preview")
def issue_preview(build_id: int, db: Session = Depends(get_db)):
    return issues.issue_preview(db, build_id)


@router.post("/issues", status_code=201)
def create_issue(body: schemas.IssueCreate, user=Depends(current_user), db: Session = Depends(get_db)):
    return issues.issue_detail(db, issues.create_issue(db, user, **body.model_dump()).id)


@router.post("/issues/{issue_id}/hand-over", status_code=201)
def hand_over(issue_id: int, body: schemas.HandOverIn, user=Depends(current_user), db: Session = Depends(get_db)):
    """The tooling in production moves on to the next setup without a return."""
    new = issues.hand_over(db, user, issue_id, **body.model_dump(exclude={"consumable_returns", "consumables"}),
                           consumable_returns=[c.model_dump() for c in body.consumable_returns],
                           consumables=[c.model_dump() for c in body.consumables] if body.consumables is not None else None)
    return {"from": issues.issue_detail(db, issue_id), "to": issues.issue_detail(db, new.id)}


@router.get("/issues")
def list_issues(wo_id: str | None = None, status: str | None = None, build_id: int | None = None,
                item_id: int | None = None, limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    total, rows = issues.list_issues(db, wo_id=wo_id, status=status, build_id=build_id, item_id=item_id,
                                     limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.get("/issues/{issue_id}")
def issue(issue_id: int, db: Session = Depends(get_db)):
    return issues.issue_detail(db, issue_id)


@router.post("/issues/{issue_id}/acknowledge")
def acknowledge(issue_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    issues.acknowledge_issue(db, user, issue_id)
    return issues.issue_detail(db, issue_id)


@router.post("/issues/{issue_id}/return")
def return_items(issue_id: int, body: schemas.ReturnIn, user=Depends(current_user), db: Session = Depends(get_db)):
    res = issues.return_items(db, user, issue_id, [l.model_dump() for l in body.lines],
                              [c.model_dump() for c in body.consumables])
    return {**res, "issue": issues.issue_detail(db, issue_id)}


# ---- labels (QR payload is the stable ID, so a reprint never changes identity)
def _audit_print(db, user, entity, entity_id):
    permissions.require(user, "item.label")
    with transaction(db):
        audit.record(db, user, "PRINT_LABEL", entity, entity_id)


@router.get("/items/{item_id}/label.svg")
def item_label(item_id: int, reprint: bool = False, user=Depends(current_user), db: Session = Depends(get_db)):
    item = db.get(Item, item_id)
    if item is None:
        raise NotFoundError(f"Item {item_id} not found")
    if reprint:
        _audit_print(db, user, "item", item_id)
    return _svg(labels.item_label_svg(db, item))


@router.get("/builds/{build_id}/label.svg")
def build_label(build_id: int, reprint: bool = False, user=Depends(current_user), db: Session = Depends(get_db)):
    b = db.get(Build, build_id)
    if b is None:
        raise NotFoundError(f"Build {build_id} not found")
    if reprint:
        _audit_print(db, user, "build", build_id)
    d = assemblies.get_definition(db, b.definition_id)
    rev = assemblies.get_revision(db, b.revision_id)
    return _svg(labels.build_label_svg(builds.build_code(b.id), d.code, d.description, rev.revision))


@router.get("/locations/{location_id}/label.svg")
def location_label(location_id: int, reprint: bool = False, user=Depends(current_user), db: Session = Depends(get_db)):
    loc = db.get(Location, location_id)
    if loc is None:
        raise NotFoundError(f"Location {location_id} not found")
    if reprint:
        _audit_print(db, user, "location", location_id)
    return _svg(labels.location_label_svg(db, loc))
