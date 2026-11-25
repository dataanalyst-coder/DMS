from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schemas
from ..deps import current_user, get_db
from ..services import audit, auth, permissions

router = APIRouter()


def _me(u) -> dict:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "roles": u.roles,
            "permissions": permissions.granted(u)}


@router.post("/auth/login")
def login(body: schemas.LoginIn, db: Session = Depends(get_db)):
    user = auth.authenticate(db, body.username, body.password)
    return {"token": auth.make_token(user), "user": _me(user)}


@router.get("/auth/me")
def me(user=Depends(current_user)):
    return _me(user)


@router.post("/auth/change-password", status_code=204)
def change_password(body: schemas.PasswordChangeIn, user=Depends(current_user), db: Session = Depends(get_db)):
    auth.change_own_password(db, user, body.old_password, body.new_password)


@router.get("/users")
def list_users(user=Depends(current_user), db: Session = Depends(get_db)):
    return auth.list_users(db, user)


@router.post("/users", status_code=201)
def create_user(body: schemas.UserCreate, user=Depends(current_user), db: Session = Depends(get_db)):
    return _me(auth.create_user(db, user, body.username, body.full_name, body.password, body.roles))


@router.put("/users/{user_id}/roles")
def set_roles(user_id: int, body: schemas.RolesIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return _me(auth.set_roles(db, user, user_id, body.roles))


@router.put("/users/{user_id}/active")
def set_active(user_id: int, body: schemas.ActiveIn, user=Depends(current_user), db: Session = Depends(get_db)):
    return _me(auth.set_active(db, user, user_id, body.active))


@router.post("/users/{user_id}/reset-password", status_code=204)
def reset_password(user_id: int, body: schemas.PasswordResetIn, user=Depends(current_user),
                   db: Session = Depends(get_db)):
    auth.reset_password(db, user, user_id, body.new_password)


@router.get("/audit")
def audit_log(entity_type: str | None = None, entity_id: str | None = None, user_name: str | None = None,
              action: str | None = None, limit: int = 200, offset: int = 0,
              user=Depends(current_user), db: Session = Depends(get_db)):
    permissions.require(user, "audit.view")
    total, rows = audit.list_audit(db, entity_type=entity_type, entity_id=entity_id, user=user_name,
                                   action=action, limit=limit, offset=offset)
    return {"total": total, "rows": [
        {"id": r.id, "at": r.at, "username": r.username, "action": r.action,
         "entity_type": r.entity_type, "entity_id": r.entity_id, "before": r.before,
         "after": r.after, "reason": r.reason} for r in rows]}
