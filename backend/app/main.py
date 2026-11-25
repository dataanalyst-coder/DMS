from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .database import engine, init_db
from .deps import audit_denied
from .routers import accounts, assembly, inventory, masters, ops, planning, production, quality
from .services.errors import LedgerError, PermissionDenied


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db(engine)
    yield


app = FastAPI(title="VSPL Tool and Die Management", lifespan=lifespan)
# Vite dev server only. Authentication is a bearer token, so no cookies/credentials are involved.
app.add_middleware(CORSMiddleware,
                   allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5180", "http://127.0.0.1:5180"],
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["Content-Disposition"])


@app.exception_handler(PermissionDenied)
def _denied(request: Request, e: PermissionDenied):
    try:
        audit_denied(request, e)
    except Exception:  # never hide the refusal because the record could not be written
        pass
    return JSONResponse(status_code=403, content={"detail": e.message})


@app.exception_handler(LedgerError)
def _ledger_error(_: Request, e: LedgerError):
    return JSONResponse(status_code=e.status_code, content={"detail": e.message})


for r in (accounts.router, masters.router, inventory.router, assembly.router, production.router, quality.router, ops.router, planning.router):
    app.include_router(r)
