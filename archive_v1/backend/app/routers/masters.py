from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas
from ..deps import get_db
from ..models import Assembly
from ..services import ledger as svc

router = APIRouter()


@router.get("/items")
def list_items(item_type: str | None = None, active: bool | None = None,
               db: Session = Depends(get_db)):
    return svc.list_items(db, item_type, active)


@router.post("/items", response_model=schemas.ItemOut, status_code=201)
def create_item(body: schemas.ItemCreate, db: Session = Depends(get_db)):
    data = body.model_dump()
    return svc.create_item(db, data.pop("item_id"), data.pop("item_type"), **data)


@router.get("/items/{item_id}")
def get_item(item_id: str, db: Session = Depends(get_db)):
    d = svc.get_item_detail(db, item_id)
    return {
        "item": schemas.ItemOut.model_validate(d["item"]),
        "status": d["status"],
        "assemblies": [schemas.AssemblyOut.model_validate(a) for a in d["assemblies"]],
        "history": [schemas.LedgerOut.model_validate(r) for r in d["history"]],
    }


@router.put("/items/{item_id}", response_model=schemas.ItemOut)
def update_item(item_id: str, body: schemas.ItemUpdate, db: Session = Depends(get_db)):
    return svc.update_item(db, item_id, **body.model_dump(exclude_unset=True))


@router.get("/assemblies", response_model=list[schemas.AssemblyOut])
def list_assemblies(db: Session = Depends(get_db)):
    return db.query(Assembly).order_by(Assembly.assembly_id).all()


@router.post("/assemblies", response_model=schemas.AssemblyOut, status_code=201)
def create_assembly(body: schemas.AssemblyCreate, db: Session = Depends(get_db)):
    data = body.model_dump()
    return svc.create_assembly(db, data.pop("assembly_id"), **data)


@router.get("/assemblies/{assembly_id}")
def get_assembly(assembly_id: str, db: Session = Depends(get_db)):
    view = svc.get_assembly_view(db, assembly_id)
    view["assembly"] = schemas.AssemblyOut.model_validate(db.get(Assembly, view["assembly_id"]))
    return view


@router.put("/assemblies/{assembly_id}", response_model=schemas.AssemblyOut)
def update_assembly(assembly_id: str, body: schemas.AssemblyUpdate, db: Session = Depends(get_db)):
    return svc.update_assembly(db, assembly_id, **body.model_dump(exclude_unset=True))
