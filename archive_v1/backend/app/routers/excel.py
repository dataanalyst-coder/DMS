from datetime import datetime

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..deps import get_db
from ..services import excel_io
from ..services import ledger as svc

router = APIRouter()
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(data: bytes, filename: str) -> Response:
    return Response(data, media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/import/{kind}")
async def import_file(kind: str, commit: bool = False, file: UploadFile = File(...),
                      db: Session = Depends(get_db)):
    """commit=false (default): validate only and return the error report.
    commit=true: write everything, but only if the file has zero errors."""
    if kind not in excel_io.TEMPLATES:
        raise svc.NotFoundError(f"Unknown import type {kind!r}; use items or assemblies")
    columns, rows = excel_io.read_rows(await file.read())
    return svc.import_rows(db, kind, columns, rows, commit)


@router.get("/templates/{kind}")
def template(kind: str):
    if kind not in excel_io.TEMPLATES:
        raise svc.NotFoundError(f"Unknown template {kind!r}")
    return _xlsx(excel_io.make_template(kind), excel_io.TEMPLATES[kind][0])


@router.get("/export/ledger")
def export_ledger(item: str | None = None, assembly: str | None = None, wo: str | None = None,
                  batch: str | None = None, person: str | None = None, action: str | None = None,
                  date_from: datetime | None = None, date_to: datetime | None = None,
                  db: Session = Depends(get_db)):
    rows = svc.query_ledger(db, item=item, assembly=assembly, wo=wo, batch=batch, person=person,
                            action=action, date_from=date_from, date_to=date_to, limit=1_000_000)
    return _xlsx(excel_io.ledger_xlsx(rows), "ledger.xlsx")


@router.get("/export/status")
def export_status(db: Session = Depends(get_db)):
    return _xlsx(excel_io.status_xlsx(svc.status_report(db)), "item_status.xlsx")
