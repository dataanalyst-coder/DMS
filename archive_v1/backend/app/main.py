from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .database import engine, init_db
from .routers import excel, masters, operations, queries
from .services.ledger import BlockedError, LedgerError, NotFoundError


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db(engine)
    yield


app = FastAPI(title="VSPL DIE Management", lifespan=lifespan)
# Vite dev server only; local use, no authentication (not in spec).
app.add_middleware(CORSMiddleware,
                   allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(BlockedError)
def _blocked(_: Request, e: BlockedError):
    return JSONResponse(status_code=409, content={
        "detail": e.message, "blocking_item_id": e.item_id, "status": e.status,
        "wo_id": e.wo_id, "batch_no": e.batch_no, "blockers": e.blockers})


@app.exception_handler(LedgerError)
def _ledger_error(_: Request, e: LedgerError):
    code = 404 if isinstance(e, NotFoundError) else 422
    return JSONResponse(status_code=code, content={"detail": e.message})


for r in (masters.router, operations.router, queries.router, excel.router):
    app.include_router(r)
