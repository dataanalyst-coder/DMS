"""Role -> permission table. Server-side only; routers and the UI just ask the service.

ADMIN has full access: it may perform every action, including the technical approvals (a decision of the
business, which departs from the separation of duties in spec section 7). Everything an admin does is still
recorded under the admin's own name in the audit trail. The table below is what every OTHER role may do.
"""
from .errors import PermissionDenied

PERMISSIONS: dict[str, set[str]] = {
    "config.manage": {"ADMIN"},                       # categories, UOMs, locations
    "users.manage": {"ADMIN"},
    "design.edit": {"ENGINEERING"},
    "design.approve": {"ENGINEERING"},
    "document.upload": {"ENGINEERING"},
    "document.approve": {"ENGINEERING"},
    "item.register": {"STORES", "ENGINEERING"},       # Engineering also keeps the die (and other tooling) master
    "item.import": {"STORES", "ENGINEERING"},
    "item.edit": {"STORES", "ENGINEERING"},
    "item.retire": {"ENGINEERING", "ADMIN"},
    "item.move": {"STORES", "TOOLING"},
    "item.photo": {"STORES", "ENGINEERING", "TOOLING"},
    "item.label": {"STORES"},
    "alias.request": {"STORES", "ENGINEERING"},
    "alias.approve": {"ENGINEERING"},
    "review.resolve": {"STORES", "ENGINEERING"},
    "condition.hold": {"TOOLING", "STORES", "ENGINEERING"},
    "condition.repair": {"TOOLING"},
    "condition.reinspect": {"STORES", "TOOLING"},
    "condition.release": {"ENGINEERING"},             # technical release back to SERVICEABLE
    "condition.scrap": {"SCRAP_APPROVER"},
    "assembly.edit": {"ENGINEERING"},                 # definitions, lines, substitutes
    "assembly.approve": {"ENGINEERING"},
    "setup.plan": {"PLANNING"},                       # enter / import / cancel planned setups
    "dieset.manage": {"ENGINEERING"},                 # which pattern / flanges belong to which physical die
    "setup.map": {"ENGINEERING"},                     # description -> tool assembly table
    "mapping.manage": {"ENGINEERING"},                # stand-in for ME until the integration exists
    "build.assemble": {"STORES", "TOOLING"},          # create a build, fit / remove / replace parts
    "build.verify": {"STORES", "TOOLING", "ENGINEERING"},
    "build.move": {"STORES", "TOOLING"},
    "build.dismantle": {"STORES", "TOOLING"},
    "readiness.check": {"PRODUCTION", "STORES", "ENGINEERING", "TOOLING", "MANAGER"},  # stand-in for OMS
    "issue.create": {"STORES"},
    "issue.acknowledge": {"PRODUCTION"},
    "return.receive": {"STORES"},
    "checklist.edit": {"ENGINEERING"},
    "checklist.approve": {"ENGINEERING"},
    "inspection.perform": {"TOOLING", "ENGINEERING"},
    "disposition.decide": {"TOOLING"},
    "repair.complete": {"TOOLING"},
    "scrap.propose": {"TOOLING"},
    "rework.revision": {"ENGINEERING"},
    "batch.receive": {"STORES"},
    "batch.move": {"STORES", "TOOLING"},
    "batch.hold": {"TOOLING", "STORES", "ENGINEERING"},
    "batch.release": {"ENGINEERING"},
    "batch.adjust": {"ENGINEERING"},                 # authorised adjustments only, always with a reason
    "usage.ingest": {"INTEGRATION", "ENGINEERING"},   # the OMS feed (ENGINEERING only as a manual stand-in)
    "usage.reconcile": {"ENGINEERING", "PRODUCTION"},  # retry / resolve events queued for reconciliation
    "count.perform": {"STORES", "TOOLING"},
    "stock.adjust": {"ENGINEERING"},                  # authorised adjustments of items and locations after a count
    "audit.view": {"ADMIN", "ENGINEERING", "MANAGER"},
}


def has(actor, permission: str) -> bool:
    allowed = PERMISSIONS[permission]  # (an unknown permission name is a programming error, not "denied")
    return "ADMIN" in actor.roles or any(r in allowed for r in actor.roles)


def require(actor, permission: str) -> None:
    if not has(actor, permission):
        need = ", ".join(sorted(PERMISSIONS[permission]))
        raise PermissionDenied(f"Not allowed: this action needs one of the roles: {need}")


def granted(actor) -> list[str]:
    """Permissions the actor holds. The UI only uses this to show/hide buttons; the server still
    enforces every action itself."""
    return sorted(p for p in PERMISSIONS if has(actor, p))
