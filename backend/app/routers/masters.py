from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas
from ..deps import current_user, get_db
from ..services import config, designs

router = APIRouter(dependencies=[Depends(current_user)])


def _cat(c):
    return {"id": c.id, "code": c.code, "name": c.name, "active": c.active}


@router.get("/categories")
def categories(db: Session = Depends(get_db)):
    return [_cat(c) for c in config.list_categories(db)]


@router.post("/categories", status_code=201)
def create_category(body: schemas.CategoryIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return _cat(config.create_category(db, user, body.code, body.name))


@router.put("/categories/{cid}")
def update_category(cid: int, body: schemas.CategoryUpdate, user=Depends(current_user),
                    db: Session = Depends(get_db)):
    return _cat(config.update_category(db, user, cid, name=body.name, active=body.active))


@router.get("/uoms")
def uoms(db: Session = Depends(get_db)):
    return [{"code": u.code, "name": u.name} for u in config.list_uoms(db)]


@router.post("/uoms", status_code=201)
def create_uom(body: schemas.UomIn, user=Depends(current_user), db: Session = Depends(get_db)):
    u = config.create_uom(db, user, body.code, body.name)
    return {"code": u.code, "name": u.name}


@router.get("/locations")
def locations(db: Session = Depends(get_db)):
    return config.list_locations(db)


@router.post("/locations", status_code=201)
def create_location(body: schemas.LocationIn, user=Depends(current_user), db: Session = Depends(get_db)):
    l = config.create_location(db, user, code=body.code, name=body.name, parent_id=body.parent_id)
    return {"id": l.id, "level": l.level, "code": l.code, "path": config.location_path(db, l.id)}


@router.put("/locations/{lid}")
def update_location(lid: int, body: schemas.LocationUpdate, user=Depends(current_user),
                    db: Session = Depends(get_db)):
    l = config.update_location(db, user, lid, name=body.name, active=body.active)
    return {"id": l.id, "active": l.active, "name": l.name}


# ---- designs
@router.get("/designs")
def list_designs(q: str | None = None, category_id: int | None = None, kind: str | None = None,
                 active: bool | None = None, limit: int = 100, offset: int = 0,
                 db: Session = Depends(get_db)):
    total, rows = designs.list_designs(db, q=q, category_id=category_id, kind=kind, active=active,
                                       limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.post("/designs", status_code=201)
def create_design(body: schemas.DesignIn, user=Depends(current_user), db: Session = Depends(get_db)):
    d = designs.create_design(db, user, **body.model_dump())
    return designs.design_detail(db, d.id)


@router.get("/designs/{design_id}")
def design(design_id: int, db: Session = Depends(get_db)):
    return designs.design_detail(db, design_id)


@router.put("/designs/{design_id}")
def update_design(design_id: int, body: schemas.DesignUpdate, user=Depends(current_user),
                  db: Session = Depends(get_db)):
    designs.update_design(db, user, design_id, **body.model_dump(exclude_unset=True))
    return designs.design_detail(db, design_id)


@router.post("/designs/{design_id}/revisions", status_code=201)
def new_revision(design_id: int, body: schemas.RevisionIn, user=Depends(current_user),
                 db: Session = Depends(get_db)):
    designs.new_revision(db, user, design_id, **body.model_dump())
    return designs.design_detail(db, design_id)


@router.put("/design-revisions/{rev_id}")
def update_revision(rev_id: int, body: schemas.RevisionUpdate, user=Depends(current_user),
                    db: Session = Depends(get_db)):
    r = designs.update_draft_revision(db, user, rev_id, **body.model_dump(exclude_unset=True))
    return designs.design_detail(db, r.design_id)


@router.post("/design-revisions/{rev_id}/approve")
def approve_revision(rev_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    r = designs.approve_revision(db, user, rev_id)
    return designs.design_detail(db, r.design_id)


@router.post("/design-revisions/{rev_id}/obsolete")
def obsolete_revision(rev_id: int, body: schemas.ReasonIn, user=Depends(current_user),
                      db: Session = Depends(get_db)):
    r = designs.obsolete_revision(db, user, rev_id, reason=body.reason)
    return designs.design_detail(db, r.design_id)
