import time

import pytest

from app.models import AuditLog, User
from app.services import audit, auth, permissions
from app.services.errors import AuthError, PermissionDenied, ValidationError
from tests.conftest import PW


def test_password_hash_roundtrip_with_real_scrypt_params(monkeypatch):
    monkeypatch.setitem(auth._SCRYPT, "n", 2 ** 14)
    h = auth.hash_password("a long enough password")
    assert h.startswith("scrypt$16384$") and "a long enough password" not in h
    assert auth.verify_password("a long enough password", h)
    assert not auth.verify_password("another password!!", h)
    assert auth.hash_password("a long enough password") != h  # random salt
    assert not auth.verify_password("x", "garbage")


def test_no_default_accounts_and_bootstrap_only_once(db):
    assert db.query(User).count() == 0
    auth.bootstrap_admin(db, "Root", "Root User", PW)
    assert db.query(User).one().username == "root"
    with pytest.raises(ValidationError):
        auth.bootstrap_admin(db, "again", "Again", PW)


def test_login_and_token_roundtrip(db, people):
    u = auth.authenticate(db, "  STORES ", PW)
    assert u.roles == ["STORES"]
    back = auth.user_from_token(db, auth.make_token(u))
    assert back.id == u.id and back.roles == ["STORES"]


def test_wrong_password_and_unknown_user_look_the_same(db, people):
    for name, pw in [("stores", "wrong-password-1"), ("nobody", PW)]:
        with pytest.raises(AuthError) as e:
            auth.authenticate(db, name, pw)
        assert e.value.message == "Wrong username or password"


def test_lockout_after_repeated_failures_then_expires(db, people):
    t = time.time()
    for _ in range(auth.MAX_FAILED_LOGINS):
        with pytest.raises(AuthError):
            auth.authenticate(db, "stores", "bad-password-x", now=t)
    with pytest.raises(AuthError) as e:  # even the right password is refused while locked
        auth.authenticate(db, "stores", PW, now=t + 10)
    assert "Too many" in e.value.message
    assert auth.authenticate(db, "stores", PW, now=t + auth.LOCKOUT_SECONDS + 1).username == "stores"


def test_failed_logins_are_audited(db, people):
    with pytest.raises(AuthError):
        auth.authenticate(db, "stores", "bad-password-x")
    row = db.query(AuditLog).filter(AuditLog.action == "LOGIN_FAILED").one()
    assert row.entity_id == "stores"


def test_tampered_expired_and_inactive_tokens_rejected(db, people):
    u = people["stores"]
    tok = auth.make_token(u)
    payload, sig = tok.split(".")
    with pytest.raises(AuthError):
        auth.user_from_token(db, payload + "." + sig[:-2] + "AA")
    with pytest.raises(AuthError):
        auth.user_from_token(db, "garbage")
    with pytest.raises(AuthError) as e:
        auth.user_from_token(db, tok, now=time.time() + auth.TOKEN_TTL_SECONDS + 5)
    assert "expired" in e.value.message
    auth.set_active(db, people["admin"], u.id, False)
    with pytest.raises(AuthError):
        auth.user_from_token(db, tok)
    with pytest.raises(AuthError):
        auth.authenticate(db, "stores", PW)


def test_password_change_invalidates_existing_tokens(db, people):
    u = auth.authenticate(db, "stores", PW)
    tok = auth.make_token(u)
    auth.change_own_password(db, u, PW, "a-brand-new-password")
    with pytest.raises(AuthError):
        auth.user_from_token(db, tok)
    assert auth.authenticate(db, "stores", "a-brand-new-password")


def test_password_policy_and_wrong_old_password(db, people):
    with pytest.raises(ValidationError):
        auth.create_user(db, people["admin"], "x", "X", "short", ["STORES"])
    with pytest.raises(AuthError):
        auth.change_own_password(db, people["stores"], "not-my-password", "a-brand-new-password")


def test_only_admin_manages_users_and_roles_are_validated(db, people):
    with pytest.raises(PermissionDenied):
        auth.create_user(db, people["eng"], "y", "Y", PW, ["STORES"])
    with pytest.raises(ValidationError):
        auth.create_user(db, people["admin"], "y", "Y", PW, ["SUPERUSER"])
    with pytest.raises(PermissionDenied):
        auth.list_users(db, people["stores"])
    assert len(auth.list_users(db, people["admin"])) == 9


def test_admin_cannot_lock_themselves_out(db, people):
    a = people["admin"]
    with pytest.raises(ValidationError):
        auth.set_roles(db, a, a.id, ["STORES"])
    with pytest.raises(ValidationError):
        auth.set_active(db, a, a.id, False)


def test_user_changes_are_audited_without_secrets(db, people):
    auth.set_roles(db, people["admin"], people["stores"].id, ["STORES", "TOOLING"])
    auth.reset_password(db, people["admin"], people["stores"].id, "another-long-password")
    total, rows = audit.list_audit(db, entity_type="user")
    dumped = " ".join((r.before or "") + (r.after or "") for r in rows)
    assert "SET_ROLES" in {r.action for r in rows} and "RESET_PASSWORD" in {r.action for r in rows}
    assert "another-long-password" not in dumped and PW not in dumped and "scrypt$" not in dumped


def test_admin_has_full_access_and_other_roles_keep_their_limits(people):
    admin = people["admin"]
    assert all(permissions.has(admin, perm) for perm in permissions.PERMISSIONS)  # every action, approvals included
    assert permissions.granted(admin) == sorted(permissions.PERMISSIONS)
    for who in ("stores", "prod", "tooling", "mgr", "plan"):  # nobody else gains technical approvals
        assert not permissions.has(people[who], "design.approve") and not permissions.has(people[who], "condition.release")
    multi = people["multi"]  # STORES + TOOLING: permissions add up, technical approvals still absent
    assert permissions.has(multi, "item.register") and permissions.has(multi, "condition.repair")
    assert not permissions.has(multi, "condition.release") and not permissions.has(multi, "design.approve")
