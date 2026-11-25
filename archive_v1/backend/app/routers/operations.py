from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas
from ..deps import get_db
from ..services import ledger as svc

router = APIRouter()


@router.post("/issue", status_code=201)
def issue(body: schemas.IssueIn, db: Session = Depends(get_db)):
    return svc.issue(db, **body.model_dump())


@router.post("/return")
def return_items(body: schemas.ReturnIn, db: Session = Depends(get_db)):
    return svc.return_items(db, **body.model_dump())


@router.get("/issues/open")
def open_issues(db: Session = Depends(get_db)):
    return svc.open_issues(db)


@router.post("/transfer")
def transfer(body: schemas.MoveIn, db: Session = Depends(get_db)):
    return svc.transfer(db, item_ids=body.item_ids, to_location=body.to_location,
                        by=body.by, remarks=body.remarks)


@router.post("/repair/send")
def repair_send(body: schemas.MoveIn, db: Session = Depends(get_db)):
    kw = {"to_location": body.to_location} if body.to_location else {}
    return svc.send_repair(db, item_ids=body.item_ids, by=body.by, remarks=body.remarks, **kw)


@router.post("/repair/back")
def repair_back(body: schemas.MoveIn, db: Session = Depends(get_db)):
    return svc.back_from_repair(db, item_ids=body.item_ids, to_location=body.to_location,
                                by=body.by, remarks=body.remarks)


@router.post("/scrap")
def scrap(body: schemas.MoveIn, db: Session = Depends(get_db)):
    return svc.scrap(db, item_ids=body.item_ids, by=body.by, remarks=body.remarks)


@router.post("/reverse/{txn_id}", response_model=list[schemas.LedgerOut], status_code=201)
def reverse(txn_id: int, body: schemas.ReverseIn | None = None, db: Session = Depends(get_db)):
    body = body or schemas.ReverseIn()
    return svc.reverse(db, txn_id, by=body.by, remarks=body.remarks)
