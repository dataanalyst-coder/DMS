from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import schemas
from ..deps import current_user, get_db
from ..services import die_sets, excel_io, setups

router = APIRouter(dependencies=[Depends(current_user)])


@router.get("/setups")
def list_setups(q: str | None = None, match: str | None = None, state: str | None = None, wo_id: str | None = None,
                limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    total, rows = setups.list_setups(db, q=q, match=match, state=state, wo_id=wo_id, limit=limit, offset=offset)
    return {"total": total, "rows": rows, **setups.counts(db)}


@router.post("/setups", status_code=201)
def create_setup(body: schemas.SetupIn, user=Depends(current_user), db: Session = Depends(get_db)):
    data = body.model_dump()
    data["lines"] = [ln for ln in data["lines"]] if data["lines"] else None
    return setups.setup_dict(db, setups.create_setup(db, user, **data), detail=True)


@router.post("/setups/{setup_id}/lines", status_code=201)
def add_setup_line(setup_id: int, body: schemas.SetupLineIn, user=Depends(current_user), db: Session = Depends(get_db)):
    setups.add_line(db, user, setup_id, **body.model_dump())
    return setups.setup_dict(db, setups._get(db, setup_id), detail=True)


@router.post("/setups/import")
async def import_setups(commit: bool = False, file: UploadFile = File(...), user=Depends(current_user),
                        db: Session = Depends(get_db)):
    return setups.import_sheet(db, user, await file.read(), commit=commit, source_file=file.filename)


@router.get("/setups/for-build/{build_id}")
def setups_for_build(build_id: int, db: Session = Depends(get_db)):
    return setups.for_build(db, build_id)


@router.get("/setups/{setup_id}")
def setup(setup_id: int, db: Session = Depends(get_db)):
    return setups.setup_dict(db, setups._get(db, setup_id), detail=True)


@router.post("/setups/{setup_id}/cancel")
def cancel_setup(setup_id: int, body: schemas.ReasonIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return setups.setup_dict(db, setups.cancel_setup(db, user, setup_id, reason=body.reason))


@router.get("/templates/setups")
def template():
    return Response(setups.template_xlsx(), media_type=excel_io.XLSX,
                    headers={"Content-Disposition": 'attachment; filename="setups_template.xlsx"'})


# ---- Engineering's description -> assembly table
@router.get("/setup-maps")
def list_maps(active: bool | None = True, db: Session = Depends(get_db)):
    return setups.list_maps(db, active=active)


@router.post("/setup-maps", status_code=201)
def add_map(body: schemas.SetupMapIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return setups.map_dict(db, setups.add_map(db, user, **body.model_dump()))


@router.post("/setup-maps/{map_id}/remove")
def remove_map(map_id: int, body: schemas.ReasonIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return setups.map_dict(db, setups.deactivate_map(db, user, map_id, reason=body.reason))


# ---- die mapping: one fixed pattern / top flange / bottom flange per physical die
@router.get("/die-sets")
def list_die_sets(q: str | None = None, limit: int = 200, offset: int = 0, db: Session = Depends(get_db)):
    total, rows = die_sets.list_sets(db, q=q, limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.post("/die-sets", status_code=201)
def save_die_set(body: schemas.DieSetIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return die_sets.set_dict(db, die_sets.save_set(db, user, **body.model_dump()))


@router.post("/die-sets/{die_item_id}/remove")
def remove_die_set(die_item_id: int, body: schemas.ReasonIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return die_sets.set_dict(db, die_sets.remove_set(db, user, die_item_id, reason=body.reason))


@router.get("/items/{item_id}/die-set")
def item_die_set(item_id: int, db: Session = Depends(get_db)):
    return die_sets.for_item(db, item_id)
