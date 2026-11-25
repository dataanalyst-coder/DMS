"""Authentication with the standard library only: scrypt password hashes and HMAC-signed tokens."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from sqlalchemy.orm import Session

from ..database import BASE_DIR
from ..models import ROLES, User, UserRole
from . import audit, permissions
from .common import clean, transaction
from .errors import AuthError, NotFoundError, ValidationError

TOKEN_TTL_SECONDS = 12 * 3600
MIN_PASSWORD_LENGTH = 10
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 300
_SCRYPT = dict(n=2 ** 14, r=8, p=1)
_failed: dict[str, tuple[int, float]] = {}  # username -> (count, locked_until); per-process


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${}${}".format(
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode(), **_SCRYPT)


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r),
                              p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def _check_password_policy(password: str):
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")


# ---------------------------------------------------------------- tokens
def _secret() -> bytes:
    env = os.environ.get("DIE_SECRET_KEY")
    if env:
        return env.encode()
    path = BASE_DIR / "secret.key"
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
    return path.read_bytes()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _pw_version(user: User) -> str:
    # changes whenever the password changes, so old tokens stop working
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:12]


def make_token(user: User, now: float | None = None) -> str:
    now = time.time() if now is None else now
    payload = _b64(json.dumps({"uid": user.id, "iat": int(now), "exp": int(now) + TOKEN_TTL_SECONDS,
                               "pv": _pw_version(user)}).encode())
    sig = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def user_from_token(db: Session, token: str, now: float | None = None) -> User:
    try:
        payload, sig = token.split(".")
        good = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(good, _unb64(sig)):
            raise ValueError
        data = json.loads(_unb64(payload))
    except Exception:
        raise AuthError("Invalid session; please sign in again") from None
    if data["exp"] < (time.time() if now is None else now):
        raise AuthError("Session expired; please sign in again")
    user = db.get(User, data["uid"])
    if user is None or not user.active or data.get("pv") != _pw_version(user):
        raise AuthError("Invalid session; please sign in again")
    return load_roles(db, user)


def load_roles(db: Session, user: User) -> User:
    user.roles = sorted(r.role for r in db.query(UserRole).filter(UserRole.user_id == user.id))
    return user


# ---------------------------------------------------------------- login
def authenticate(db: Session, username: str, password: str, now: float | None = None) -> User:
    now = time.time() if now is None else now
    uname = (username or "").strip().lower()
    count, locked_until = _failed.get(uname, (0, 0.0))
    if locked_until > now:
        raise AuthError("Too many failed sign-ins; try again in a few minutes")
    user = db.query(User).filter(User.username == uname).first()
    # verify even for unknown users so timing does not reveal which usernames exist
    ok = verify_password(password or "", user.password_hash if user else hash_password("x"))
    if not (user and ok and user.active):
        count += 1
        _failed[uname] = (count, now + LOCKOUT_SECONDS if count >= MAX_FAILED_LOGINS else 0.0)
        with transaction(db):
            audit.record(db, user, "LOGIN_FAILED", "user", uname)
        raise AuthError("Wrong username or password")
    _failed.pop(uname, None)
    return load_roles(db, user)


# ---------------------------------------------------------------- user administration
def _validate_roles(roles) -> list[str]:
    roles = sorted(set(roles or []))
    bad = [r for r in roles if r not in ROLES]
    if bad:
        raise ValidationError(f"Unknown role(s): {', '.join(bad)}; expected {', '.join(ROLES)}")
    return roles


def _user_dict(u: User, roles) -> dict:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "active": u.active,
            "roles": list(roles)}


def _add_user(db: Session, actor, username, full_name, password, roles) -> User:
    uname = (username or "").strip().lower()
    if not uname or not clean(full_name):
        raise ValidationError("Username and full name are required")
    _check_password_policy(password)
    roles = _validate_roles(roles)
    if db.query(User).filter(User.username == uname).first():
        raise ValidationError(f"User {uname} already exists")
    user = User(username=uname, full_name=full_name.strip(), password_hash=hash_password(password))
    db.add(user)
    db.flush()
    for r in roles:
        db.add(UserRole(user_id=user.id, role=r))
    audit.record(db, actor, "CREATE", "user", user.id, after=_user_dict(user, roles))
    return user


def create_user(db: Session, actor, username, full_name, password, roles) -> User:
    permissions.require(actor, "users.manage")
    with transaction(db):
        user = _add_user(db, actor, username, full_name, password, roles)
    return load_roles(db, user)


def bootstrap_admin(db: Session, username, full_name, password) -> User:
    """First-run helper (CLI). Only allowed while no user exists; there is no default account."""
    if db.query(User).count():
        raise ValidationError("Users already exist; sign in as an administrator instead")
    with transaction(db):
        user = _add_user(db, None, username, full_name, password, ["ADMIN"])
    return load_roles(db, user)


def list_users(db: Session, actor) -> list[dict]:
    permissions.require(actor, "users.manage")
    return [_user_dict(u, sorted(r.role for r in db.query(UserRole).filter(UserRole.user_id == u.id)))
            for u in db.query(User).order_by(User.username)]


def _get_user(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise NotFoundError(f"User {user_id} not found")
    return u


def set_roles(db: Session, actor, user_id: int, roles) -> User:
    permissions.require(actor, "users.manage")
    roles = _validate_roles(roles)
    user = _get_user(db, user_id)
    before = sorted(r.role for r in db.query(UserRole).filter(UserRole.user_id == user_id))
    if user_id == actor.id and "ADMIN" in before and "ADMIN" not in roles:
        raise ValidationError("You cannot remove your own ADMIN role")
    with transaction(db):
        db.query(UserRole).filter(UserRole.user_id == user_id).delete()
        for r in roles:
            db.add(UserRole(user_id=user_id, role=r))
        audit.record(db, actor, "SET_ROLES", "user", user_id, before={"roles": before},
                     after={"roles": roles})
    return load_roles(db, user)


def set_active(db: Session, actor, user_id: int, active: bool) -> User:
    permissions.require(actor, "users.manage")
    user = _get_user(db, user_id)
    if user_id == actor.id and not active:
        raise ValidationError("You cannot deactivate your own account")
    with transaction(db):
        audit.record(db, actor, "SET_ACTIVE", "user", user_id, before={"active": user.active},
                     after={"active": active})
        user.active = active
    return load_roles(db, user)


def reset_password(db: Session, actor, user_id: int, new_password: str) -> None:
    permissions.require(actor, "users.manage")
    _check_password_policy(new_password)
    user = _get_user(db, user_id)
    with transaction(db):
        user.password_hash = hash_password(new_password)
        audit.record(db, actor, "RESET_PASSWORD", "user", user_id)  # never log the password
    _failed.pop(user.username, None)


def change_own_password(db: Session, actor: User, old: str, new: str) -> None:
    if not verify_password(old or "", actor.password_hash):
        raise AuthError("Current password is wrong")
    _check_password_policy(new)
    with transaction(db):
        actor.password_hash = hash_password(new)
        audit.record(db, actor, "CHANGE_PASSWORD", "user", actor.id)
