from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas
from ..deps import current_user, get_db
from ..models import AssemblyLineAlternative
from ..services import assemblies, builds, mappings

router = APIRouter(dependencies=[Depends(current_user)])


def _by_revision(db, revision_id):
    return assemblies.assembly_detail(db, assemblies.get_revision(db, revision_id).definition_id)


# ---- definitions
@router.get("/assemblies")
def list_assemblies(q: str | None = None, category_id: int | None = None, active: bool | None = None,
                    limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    total, rows = assemblies.list_assemblies(db, q=q, category_id=category_id, active=active,
                                             limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.post("/assemblies", status_code=201)
def create_assembly(body: schemas.AssemblyIn, user=Depends(current_user), db: Session = Depends(get_db)):
    d = assemblies.create_assembly(db, user, **body.model_dump())
    return assemblies.assembly_detail(db, d.id)


@router.get("/assemblies/{definition_id}")
def assembly(definition_id: int, db: Session = Depends(get_db)):
    return assemblies.assembly_detail(db, definition_id)


@router.put("/assemblies/{definition_id}")
def update_assembly(definition_id: int, body: schemas.AssemblyUpdate, user=Depends(current_user),
                    db: Session = Depends(get_db)):
    assemblies.update_assembly(db, user, definition_id, **body.model_dump(exclude_unset=True))
    return assemblies.assembly_detail(db, definition_id)


@router.post("/assemblies/{definition_id}/revisions", status_code=201)
def new_revision(definition_id: int, body: schemas.AssemblyRevisionIn, user=Depends(current_user),
                 db: Session = Depends(get_db)):
    assemblies.new_revision(db, user, definition_id, **body.model_dump())
    return assemblies.assembly_detail(db, definition_id)


@router.put("/assembly-revisions/{rev_id}")
def update_revision(rev_id: int, body: schemas.AssemblyRevisionUpdate, user=Depends(current_user),
                    db: Session = Depends(get_db)):
    assemblies.update_revision(db, user, rev_id, **body.model_dump(exclude_unset=True))
    return _by_revision(db, rev_id)


@router.post("/assembly-revisions/{rev_id}/approve")
def approve_revision(rev_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    assemblies.approve_revision(db, user, rev_id)
    return _by_revision(db, rev_id)


@router.post("/assembly-revisions/{rev_id}/obsolete")
def obsolete_revision(rev_id: int, body: schemas.ReasonIn, user=Depends(current_user),
                      db: Session = Depends(get_db)):
    assemblies.obsolete_revision(db, user, rev_id, reason=body.reason)
    return _by_revision(db, rev_id)


@router.post("/assembly-revisions/{rev_id}/lines", status_code=201)
def add_line(rev_id: int, body: schemas.LineIn, user=Depends(current_user), db: Session = Depends(get_db)):
    assemblies.add_line(db, user, rev_id, **body.model_dump())
    return _by_revision(db, rev_id)


@router.put("/assembly-lines/{line_id}")
def update_line(line_id: int, body: schemas.LineUpdate, user=Depends(current_user), db: Session = Depends(get_db)):
    sent = body.model_dump(exclude_unset=True)
    ln = assemblies.update_line(db, user, line_id, fields_set=set(sent), **sent)
    return _by_revision(db, ln.revision_id)


@router.delete("/assembly-lines/{line_id}")
def remove_line(line_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    rev_id = assemblies._line(db, line_id).revision_id
    assemblies.remove_line(db, user, line_id)
    return _by_revision(db, rev_id)


@router.post("/assembly-lines/{line_id}/alternatives", status_code=201)
def add_alternative(line_id: int, body: schemas.AlternativeIn, user=Depends(current_user),
                    db: Session = Depends(get_db)):
    assemblies.add_alternative(db, user, line_id, **body.model_dump())
    return _by_revision(db, assemblies._line(db, line_id).revision_id)


@router.delete("/assembly-alternatives/{alt_id}")
def remove_alternative(alt_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    alt = db.get(AssemblyLineAlternative, alt_id)
    rev_id = assemblies._line(db, alt.line_id).revision_id if alt else None
    assemblies.remove_alternative(db, user, alt_id)
    return _by_revision(db, rev_id)


# ---- ME mapping (reference copy; stand-in until the ME integration exists)
@router.get("/mappings")
def list_mappings(part_id: str | None = None, operation_id: str | None = None,
                  definition_id: int | None = None, active: bool | None = None, limit: int = 200,
                  offset: int = 0, db: Session = Depends(get_db)):
    total, rows = mappings.list_mappings(db, part_id=part_id, operation_id=operation_id,
                                         definition_id=definition_id, active=active, limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.get("/mappings/options")
def mapping_options(part_id: str, routing_revision: str, operation_id: str, db: Session = Depends(get_db)):
    return mappings.options(db, part_id=part_id, routing_revision=routing_revision, operation_id=operation_id)


@router.post("/mappings", status_code=201)
def create_mapping(body: schemas.MappingIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return mappings.mapping_dict(db, mappings.create_mapping(db, user, **body.model_dump()))


@router.put("/mappings/{mapping_id}")
def update_mapping(mapping_id: int, body: schemas.MappingUpdate, user=Depends(current_user),
                   db: Session = Depends(get_db)):
    sent = body.model_dump(exclude_unset=True)
    reason = sent.pop("reason", None)
    m = mappings.update_mapping(db, user, mapping_id, fields_set=set(sent), reason=reason, **sent)
    return mappings.mapping_dict(db, m)


# ---- builds
@router.get("/builds")
def list_builds(status: str | None = None, definition_id: int | None = None, item_id: int | None = None,
                limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    total, rows = builds.list_builds(db, status=status, definition_id=definition_id, item_id=item_id,
                                     limit=limit, offset=offset)
    return {"total": total, "rows": rows}


@router.post("/builds", status_code=201)
def create_build(body: schemas.BuildIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return builds.build_detail(db, builds.create_build(db, user, **body.model_dump()).id)


@router.get("/builds/{build_id}")
def build(build_id: int, db: Session = Depends(get_db)):
    return builds.build_detail(db, build_id)


@router.post("/builds/{build_id}/fit")
def fit(build_id: int, body: schemas.FitIn, user=Depends(current_user), db: Session = Depends(get_db)):
    builds.fit_item(db, user, build_id, body.item_id)
    return builds.build_detail(db, build_id)


@router.post("/builds/{build_id}/remove")
def remove(build_id: int, body: schemas.RemoveIn, user=Depends(current_user), db: Session = Depends(get_db)):
    builds.remove_item(db, user, build_id, body.item_id, body.to_location_id, reason=body.reason)
    return builds.build_detail(db, build_id)


@router.post("/builds/{build_id}/replace")
def replace(build_id: int, body: schemas.ReplaceIn, user=Depends(current_user), db: Session = Depends(get_db)):
    builds.replace_component(db, user, build_id, body.remove_item_id, body.new_item_id, body.to_location_id,
                             reason=body.reason)
    return builds.build_detail(db, build_id)


@router.post("/builds/{build_id}/verify")
def verify(build_id: int, user=Depends(current_user), db: Session = Depends(get_db)):
    builds.verify_build(db, user, build_id)
    return builds.build_detail(db, build_id)


@router.post("/builds/{build_id}/move")
def move(build_id: int, body: schemas.BuildMoveIn, user=Depends(current_user), db: Session = Depends(get_db)):
    builds.move_build(db, user, build_id, body.to_location_id, remarks=body.remarks)
    return builds.build_detail(db, build_id)


@router.post("/builds/{build_id}/dismantle")
def dismantle(build_id: int, body: schemas.DismantleIn, user=Depends(current_user), db: Session = Depends(get_db)):
    builds.dismantle_build(db, user, build_id, {d.item_id: d.location_id for d in body.destinations},
                           reason=body.reason)
    return builds.build_detail(db, build_id)
