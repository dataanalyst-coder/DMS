from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas
from ..deps import get_db
from ..services import ledger as svc

router = APIRouter()


@router.get("/ledger", response_model=list[schemas.LedgerOut])
def ledger(item: str | None = None, assembly: str | None = None, wo: str | None = None,
           batch: str | None = None, person: str | None = None, action: str | None = None,
           date_from: datetime | None = None, date_to: datetime | None = None,
           limit: int = 500, offset: int = 0, db: Session = Depends(get_db)):
    return svc.query_ledger(db, item=item, assembly=assembly, wo=wo, batch=batch, person=person,
                            action=action, date_from=date_from, date_to=date_to,
                            limit=min(limit, 5000), offset=offset)


@router.get("/search")
def search(q: str = "", db: Session = Depends(get_db)):
    return svc.search(db, q)


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    return svc.dashboard_counts(db)
