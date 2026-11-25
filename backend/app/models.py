from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ROLES = ("ADMIN", "ENGINEERING", "STORES", "PRODUCTION", "TOOLING", "SCRAP_APPROVER", "MANAGER", "INTEGRATION", "PLANNING")
KINDS = ("REUSABLE", "CONSUMABLE")
APPROVAL_STATUSES = ("DRAFT", "APPROVED", "OBSOLETE")
# Condition is separate from custody and location (spec section 5).
CONDITIONS = ("SERVICEABLE", "AWAITING_INSPECTION", "HOLD", "UNDER_REPAIR", "SCRAPPED")
CUSTODIES = ("STORES", "ASSEMBLY_AREA", "PENDING_HANDOVER", "PRODUCTION", "REPAIR", "MISSING")
LOCATION_LEVELS = ("PLANT", "AREA", "RACK", "SHELF", "BIN")
ACTIONS = ("RECEIVE", "MOVE", "SET_CONDITION", "FIT", "REMOVE", "ISSUE", "ACKNOWLEDGE", "RETURN",
           "MARK_MISSING", "RECOVER", "HANDOVER", "REVERSAL")
USAGE_STATUSES = ("POSTED", "PENDING", "SUPERSEDED", "CANCELLED", "REJECTED")
COUNT_STATUSES = ("OPEN", "COMPLETED", "CANCELLED")
READINESS_RESULTS = ("READY", "NOT_READY", "UNABLE_TO_VERIFY")
BATCH_ACTIONS = ("RECEIVE", "MOVE", "HOLD", "RELEASE_HOLD", "ISSUE", "RETURN", "ADJUST", "REVERSAL")
CHECKLIST_SCOPES = ("CATEGORY", "DESIGN")
CHECK_TYPES = ("PASS_FAIL", "MEASUREMENT", "TEXT")
EVIDENCE_RULES = ("NONE", "ON_FAIL", "ALWAYS")
INSPECTION_STATUSES = ("OPEN", "COMPLETED")
INSPECTION_OUTCOMES = ("PASS", "HOLD")
CHECK_RESULTS = ("PASS", "FAIL", "NA")
DISPOSITION_DECISIONS = ("REPAIR", "FURTHER_INSPECTION", "PROPOSE_SCRAP")
REPAIR_STATUSES = ("OPEN", "COMPLETED")
SCRAP_STATUSES = ("PENDING", "APPROVED", "REJECTED")
BUILD_STATUSES = ("ASSEMBLING", "VERIFIED", "DISMANTLED")
TOOLING_STATUSES = ("READY", "TO_BE_DEVELOPED")  # ME's flag on a mapping
DOC_TYPES = ("DIE_DRAWING", "TOOL_DRAWING", "WORK_INSTRUCTION", "INSPECTION_REPORT",
             "REPAIR_RECORD", "OTHER")
LINK_TARGETS = ("DESIGN", "ITEM", "ASSEMBLY")
PHOTO_KINDS = ("ITEM", "STORAGE")
ALIAS_STATUSES = ("PENDING", "APPROVED", "REJECTED")
REVIEW_STATUSES = ("PENDING", "RESOLVED", "REJECTED")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _ck(column: str, values, name: str) -> CheckConstraint:
    return CheckConstraint(f"{column} IN ({_in(values)})", name=name)


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ users / roles
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    # filled by services.auth.load_roles; plain attribute, not a column
    roles: list[str] = []


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (_ck("role", ROLES, "ck_user_role"),)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String, primary_key=True)


# ------------------------------------------------------------------ configuration
class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Uom(Base):
    __tablename__ = "uoms"
    code: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)


class Location(Base):
    """plant -> area -> rack -> shelf -> bin."""
    __tablename__ = "locations"
    __table_args__ = (_ck("level", LOCATION_LEVELS, "ck_location_level"),
                      UniqueConstraint("parent_id", "code", name="uq_location_code"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    level: Mapped[str] = mapped_column(String, nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# ------------------------------------------------------------------ designs
class ElementDesign(Base):
    __tablename__ = "element_designs"
    __table_args__ = (_ck("kind", KINDS, "ck_design_kind"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="REUSABLE")
    base_uom: Mapped[str] = mapped_column(ForeignKey("uoms.code"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class DesignRevision(Base):
    __tablename__ = "design_revisions"
    __table_args__ = (_ck("status", APPROVAL_STATUSES, "ck_rev_status"),
                      UniqueConstraint("design_id", "revision", name="uq_design_revision"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    design_id: Mapped[int] = mapped_column(ForeignKey("element_designs.id"), nullable=False)
    revision: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    specifications: Mapped[str | None] = mapped_column(Text)
    storage_instructions: Mapped[str | None] = mapped_column(Text)
    change_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


# ------------------------------------------------------------------ physical items
class Item(Base):
    """Reusable physical item. Condition, custody and location are NOT columns: they are derived
    from the latest effective ledger entry (see item_state view)."""
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # immutable internal id
    system_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # QR target
    legacy_no: Mapped[str | None] = mapped_column(String)  # as printed on the tool
    legacy_key: Mapped[str | None] = mapped_column(String, index=True)  # normalised, searchable
    display_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    design_id: Mapped[int] = mapped_column(ForeignKey("element_designs.id"), nullable=False, index=True)
    design_revision_id: Mapped[int] = mapped_column(ForeignKey("design_revisions.id"), nullable=False)
    home_location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    acquisition_date: Mapped[date | None] = mapped_column(Date)
    provenance: Mapped[str | None] = mapped_column(Text)
    opening_cycles: Mapped[int | None] = mapped_column(Integer)      # NULL = unknown, never 0
    opening_tonnage: Mapped[float | None] = mapped_column(Float)     # NULL = unknown, never 0
    remarks: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    retired_reason: Mapped[str | None] = mapped_column(Text)
    retired_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class ItemAlias(Base):
    __tablename__ = "item_aliases"
    __table_args__ = (_ck("status", ALIAS_STATUSES, "ck_alias_status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String, nullable=False)
    alias_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    reason: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class ItemPhoto(Base):
    __tablename__ = "item_photos"
    __table_args__ = (_ck("kind", PHOTO_KINDS, "ck_photo_kind"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    file_name: Mapped[str] = mapped_column(String, nullable=False)
    stored_path: Mapped[str] = mapped_column(String, nullable=False)
    mime: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


# ------------------------------------------------------------------ ledger
class Ledger(Base):
    """Append-only. Each row stores the *resulting* condition/custody/location, so the item's
    state is simply its latest effective row. Corrections are REVERSAL rows."""
    __tablename__ = "ledger"
    __table_args__ = (
        _ck("action", ACTIONS, "ck_ledger_action"),
        UniqueConstraint("item_id", "item_seq", name="uq_ledger_item_seq"),
    )
    txn_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    txn_time: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    event_id: Mapped[str] = mapped_column(String, nullable=False, index=True)  # one per API call
    group_id: Mapped[str | None] = mapped_column(String, index=True)  # link to original issue (phase 3)
    build_id: Mapped[int | None] = mapped_column(ForeignKey("builds.id"), index=True)  # FIT/REMOVE/build MOVE
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id"), index=True)  # ISSUE/ACKNOWLEDGE/RETURN
    source: Mapped[str | None] = mapped_column(String)  # workflow that wrote it (INSPECTION, REPAIR, ...): not reversible by hand
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    # Per-item sequence = previous last_seq + 1, taken from the state the writer validated against.
    # Two writers acting on the same stale state collide here instead of both succeeding.
    item_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    from_condition: Mapped[str | None] = mapped_column(String)
    to_condition: Mapped[str] = mapped_column(String, nullable=False)
    from_custody: Mapped[str | None] = mapped_column(String)
    to_custody: Mapped[str] = mapped_column(String, nullable=False)
    from_location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    to_location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text)
    ref_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger.txn_id"))


# ------------------------------------------------------------------ assembly definitions
class AssemblyDefinition(Base):
    """What an assembly IS (a versioned bill of element designs). Physical builds are separate."""
    __tablename__ = "assembly_definitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class AssemblyRevision(Base):
    __tablename__ = "assembly_revisions"
    __table_args__ = (_ck("status", APPROVAL_STATUSES, "ck_asmrev_status"),
                      UniqueConstraint("definition_id", "revision", name="uq_assembly_revision"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    definition_id: Mapped[int] = mapped_column(ForeignKey("assembly_definitions.id"), nullable=False)
    revision: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    instructions: Mapped[str | None] = mapped_column(Text)
    checklist_refs: Mapped[str | None] = mapped_column(Text)  # inspection checklist references (phase 4)
    change_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class AssemblyLine(Base):
    """One required element design (and quantity) in a revision. Any number of lines."""
    __tablename__ = "assembly_lines"
    __table_args__ = (UniqueConstraint("revision_id", "design_id", name="uq_line_design"),
                      CheckConstraint("quantity > 0", name="ck_line_qty"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("assembly_revisions.id"), nullable=False, index=True)
    design_id: Mapped[int] = mapped_column(ForeignKey("element_designs.id"), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    quantity_basis: Mapped[str | None] = mapped_column(Text)  # where a consumable quantity comes from
    allowed_revisions: Mapped[str | None] = mapped_column(String)  # "A,B"; blank = current approved only
    remarks: Mapped[str | None] = mapped_column(Text)


class AssemblyLineAlternative(Base):
    """An approved substitute design for a line (Engineering owns substitutes)."""
    __tablename__ = "assembly_line_alternatives"
    __table_args__ = (UniqueConstraint("line_id", "design_id", name="uq_line_alt"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("assembly_lines.id"), nullable=False, index=True)
    design_id: Mapped[int] = mapped_column(ForeignKey("element_designs.id"), nullable=False)
    allowed_revisions: Mapped[str | None] = mapped_column(String)


class MeMapping(Base):
    """Reference to Manufacturing Engineering's part/operation -> assembly option mapping. ME is
    authoritative; until the integration contract is agreed these rows are entered by Engineering
    (source='MANUAL'). Die Management never creates part IDs: part_id is the shared part-master ID."""
    __tablename__ = "me_mappings"
    __table_args__ = (
        _ck("tooling_status", TOOLING_STATUSES, "ck_map_tooling"),
        UniqueConstraint("part_id", "routing_revision", "operation_id", "definition_id", name="uq_me_mapping"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    routing_revision: Mapped[str] = mapped_column(String, nullable=False)
    operation_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    definition_id: Mapped[int] = mapped_column(ForeignKey("assembly_definitions.id"), nullable=False, index=True)
    definition_revision_id: Mapped[int | None] = mapped_column(ForeignKey("assembly_revisions.id"))  # NULL = current approved
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tooling_status: Mapped[str] = mapped_column(String, nullable=False, default="READY")
    pouring_weight_kg: Mapped[float | None] = mapped_column(Float)  # ME's value; NULL = unknown
    pouring_weight_revision: Mapped[str | None] = mapped_column(String)
    source: Mapped[str] = mapped_column(String, nullable=False, default="MANUAL")
    external_ref: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


# ------------------------------------------------------------------ planning: setups
class Setup(Base):
    """One planned setup from Planning: one tooling set mounted once, running one or more WO / part lines. Which tooling
    it needs is NOT stored: it is matched live from SetupToolMap, so a corrected rule applies at once. Whether it is
    issued is derived from the issues that carry its id."""
    __tablename__ = "setups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)  # the SETUP column of the sheet, as written
    name_key: Mapped[str | None] = mapped_column(String, index=True)  # upper case, no spaces: lines with the same key belong together
    plan_ref: Mapped[str | None] = mapped_column(String)  # sheet / plan name, e.g. "NOVEMBER 1"
    source: Mapped[str] = mapped_column(String, nullable=False, default="MANUAL")  # EXCEL | MANUAL
    source_file: Mapped[str | None] = mapped_column(String)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancel_reason: Mapped[str | None] = mapped_column(String)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class SetupLine(Base):
    """A WO for a part within a setup, with the tool description Planning wrote."""
    __tablename__ = "setup_lines"
    __table_args__ = (UniqueConstraint("wo_id", "part_id", name="uq_setup_line_wo_part"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    setup_id: Mapped[int] = mapped_column(ForeignKey("setups.id"), nullable=False, index=True)
    wo_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    part_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    tool_description: Mapped[str | None] = mapped_column(String)
    family: Mapped[str | None] = mapped_column(String)
    grade: Mapped[str | None] = mapped_column(String)  # Planning's internal grade
    internal_id: Mapped[str | None] = mapped_column(String)  # Planning's own number (INTERNAL ID or SL NO column)
    fg_qty: Mapped[int | None] = mapped_column(Integer)
    rcd_qty: Mapped[int | None] = mapped_column(Integer)
    weight: Mapped[float | None] = mapped_column(Float)


class IssueLine(Base):
    """The WO / part lines of a setup that one issue covers, each with the ME mapping (operation) it runs under."""
    __tablename__ = "issue_lines"
    __table_args__ = (UniqueConstraint("issue_id", "setup_line_id", name="uq_issue_line"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"), nullable=False, index=True)
    setup_line_id: Mapped[int] = mapped_column(ForeignKey("setup_lines.id"), nullable=False, index=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("me_mappings.id"), nullable=False)


class SetupToolMap(Base):
    """Engineering's table: which tool assembly serves a setup. Any of part / description / family may be left
    blank (= any); the most specific rule wins and two equally specific rules that disagree are reported, not guessed."""
    __tablename__ = "setup_tool_maps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_id: Mapped[str | None] = mapped_column(String, index=True)
    description_key: Mapped[str | None] = mapped_column(String, index=True)
    family_key: Mapped[str | None] = mapped_column(String)
    description_text: Mapped[str | None] = mapped_column(String)  # as typed, for display
    definition_id: Mapped[int] = mapped_column(ForeignKey("assembly_definitions.id"), nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class DieSet(Base):
    """Engineering's fixed pairing of physical items: one die with its own pattern, top flange and bottom flange.
    A reference for finding the right parts; it does not move or reserve anything."""
    __tablename__ = "die_sets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    die_item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    pattern_item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), index=True)
    top_flange_item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), index=True)
    bottom_flange_item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), index=True)
    note: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


# ------------------------------------------------------------------ physical builds
class Build(Base):
    """A physical assembly: exact item IDs fitted against one definition revision."""
    __tablename__ = "builds"
    __table_args__ = (_ck("status", BUILD_STATUSES, "ck_build_status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    definition_id: Mapped[int] = mapped_column(ForeignKey("assembly_definitions.id"), nullable=False, index=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("assembly_revisions.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="ASSEMBLING")
    created_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    assembler_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    verified_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    close_reason: Mapped[str | None] = mapped_column(Text)
    remarks: Mapped[str | None] = mapped_column(Text)


class BuildComponent(Base):
    """Membership history. A row with removed_at NULL is the item's one active membership."""
    __tablename__ = "build_components"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("builds.id"), nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    fitted_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    fitted_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    fit_event_id: Mapped[str] = mapped_column(String, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)
    removed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    removal_reason: Mapped[str | None] = mapped_column(Text)
    remove_event_id: Mapped[str | None] = mapped_column(String)


# ------------------------------------------------------------------ issue to production
class Issue(Base):
    """Header of one issue of a verified build to a WO. Which lines are still out, acknowledged or
    returned is NOT stored here: it is derived from the ledger rows carrying this issue_id."""
    __tablename__ = "issues"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wo_id: Mapped[str] = mapped_column(String, nullable=False, index=True)  # OMS work order
    part_id: Mapped[str] = mapped_column(String, nullable=False)
    routing_revision: Mapped[str] = mapped_column(String, nullable=False)
    operation_id: Mapped[str] = mapped_column(String, nullable=False)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("me_mappings.id"), nullable=False)
    build_id: Mapped[int] = mapped_column(ForeignKey("builds.id"), nullable=False, index=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("assembly_revisions.id"), nullable=False)
    setup_id: Mapped[int | None] = mapped_column(ForeignKey("setups.id"), index=True)  # the setup this issue is FOR
    handed_over_at: Mapped[datetime | None] = mapped_column(DateTime)  # tooling moved on to the next setup without a return
    handed_over_to: Mapped[int | None] = mapped_column(ForeignKey("issues.id"))
    destination_location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    issued_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text)


class IssueItem(Base):
    __tablename__ = "issue_items"
    __table_args__ = (UniqueConstraint("issue_id", "item_id", name="uq_issue_item"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"), nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)


# ------------------------------------------------------------------ readiness checks (evidence only)
class ReadinessCheck(Base):
    """Result of one WO readiness check, kept as evidence. Candidate items are NOT allocations."""
    __tablename__ = "readiness_checks"
    __table_args__ = (_ck("result", READINESS_RESULTS, "ck_ready_result"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    wo_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    part_id: Mapped[str] = mapped_column(String, nullable=False)
    routing_revision: Mapped[str] = mapped_column(String, nullable=False)
    operation_id: Mapped[str] = mapped_column(String, nullable=False)
    mapping_id: Mapped[int | None] = mapped_column(ForeignKey("me_mappings.id"), index=True)
    definition_id: Mapped[int | None] = mapped_column(ForeignKey("assembly_definitions.id"), index=True)
    definition_revision_id: Mapped[int | None] = mapped_column(ForeignKey("assembly_revisions.id"))
    result: Mapped[str] = mapped_column(String, nullable=False)
    reasons: Mapped[str] = mapped_column(Text, nullable=False, default="[]")          # JSON: [{code, message}]
    shortages: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    custody_warnings: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    warnings: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    alternatives: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    needs_recheck: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recheck_reason: Mapped[str | None] = mapped_column(Text)
    wo_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ReadinessCandidate(Base):
    __tablename__ = "readiness_candidates"
    __table_args__ = (UniqueConstraint("check_id", "item_id", name="uq_ready_candidate"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("readiness_checks.id"), nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)


# ------------------------------------------------------------------ consumable batches
class ConsumableBatch(Base):
    """Batch-controlled consumable. Quantities and location are NOT columns: they are derived from the
    append-only batch ledger (batch_ledger), exactly like item state."""
    __tablename__ = "consumable_batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_code: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # BATCH-000012, the QR payload
    design_id: Mapped[int] = mapped_column(ForeignKey("element_designs.id"), nullable=False, index=True)
    design_revision_id: Mapped[int] = mapped_column(ForeignKey("design_revisions.id"), nullable=False)
    supplier_lot: Mapped[str | None] = mapped_column(String)
    uom: Mapped[str] = mapped_column(ForeignKey("uoms.code"), nullable=False)
    received_qty: Mapped[float] = mapped_column(Float, nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date)  # only where applicable
    remarks: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class BatchLedger(Base):
    """Append-only. Each row carries signed quantity deltas; balances are their sums. Corrections are
    REVERSAL rows with negated deltas. A trigger refuses any row that would make a balance negative."""
    __tablename__ = "batch_ledger"
    __table_args__ = (_ck("action", BATCH_ACTIONS, "ck_batch_action"),
                      UniqueConstraint("batch_id", "batch_seq", name="uq_batch_seq"))
    txn_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    txn_time: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    event_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("consumable_batches.id"), nullable=False, index=True)
    batch_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    d_usable: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    d_held: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    d_outstanding: Mapped[float] = mapped_column(Float, nullable=False, default=0)  # issued out, not yet settled
    d_consumed: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    to_location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text)
    ref_txn_id: Mapped[int | None] = mapped_column(ForeignKey("batch_ledger.txn_id"))


class IssueBatch(Base):
    """Consumable quantity issued to a WO with the basis of the requirement. Settled by the return."""
    __tablename__ = "issue_batches"
    __table_args__ = (UniqueConstraint("issue_id", "batch_id", name="uq_issue_batch"),
                      CheckConstraint("quantity > 0", name="ck_issue_batch_qty"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"), nullable=False, index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("consumable_batches.id"), nullable=False, index=True)
    line_id: Mapped[int | None] = mapped_column(ForeignKey("assembly_lines.id"))
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    basis: Mapped[str | None] = mapped_column(Text)  # ME requirement, or the authorised entry
    override_reason: Mapped[str | None] = mapped_column(Text)


# ------------------------------------------------------------------ inspection checklists
class ChecklistTemplate(Base):
    """A checklist for a tooling category, or design-specific additions. Configurable, not code."""
    __tablename__ = "checklist_templates"
    __table_args__ = (_ck("scope_type", CHECKLIST_SCOPES, "ck_tpl_scope"),
                      UniqueConstraint("scope_type", "scope_id", name="uq_checklist_scope"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)
    scope_id: Mapped[int] = mapped_column(Integer, nullable=False)  # category id or design id
    name: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ChecklistVersion(Base):
    __tablename__ = "checklist_versions"
    __table_args__ = (_ck("status", APPROVAL_STATUSES, "ck_clv_status"),
                      UniqueConstraint("template_id", "version", name="uq_checklist_version"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("checklist_templates.id"), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    change_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class ChecklistCheck(Base):
    __tablename__ = "checklist_checks"
    __table_args__ = (_ck("check_type", CHECK_TYPES, "ck_check_type"), _ck("evidence", EVIDENCE_RULES, "ck_check_evidence"),
                      UniqueConstraint("version_id", "code", name="uq_check_code"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("checklist_versions.id"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    code: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    guidance: Mapped[str | None] = mapped_column(Text)
    check_type: Mapped[str] = mapped_column(String, nullable=False, default="PASS_FAIL")
    required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # a failed required check forces a hold
    na_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evidence: Mapped[str] = mapped_column(String, nullable=False, default="NONE")  # photo needed: never / on fail / always
    unit: Mapped[str | None] = mapped_column(String)
    min_value: Mapped[float | None] = mapped_column(Float)
    max_value: Mapped[float | None] = mapped_column(Float)


# ------------------------------------------------------------------ inspections, disposition, repair
class Inspection(Base):
    """One inspection of one item. The checklist versions used are kept, so later checklist changes never
    rewrite what was inspected."""
    __tablename__ = "inspections"
    __table_args__ = (_ck("status", INSPECTION_STATUSES, "ck_insp_status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id"))  # the issue it was returned from
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    outcome: Mapped[str | None] = mapped_column(String)
    category_version_id: Mapped[int | None] = mapped_column(ForeignKey("checklist_versions.id"))
    design_version_id: Mapped[int | None] = mapped_column(ForeignKey("checklist_versions.id"))
    started_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    completed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    summary: Mapped[str | None] = mapped_column(Text)


class InspectionResult(Base):
    __tablename__ = "inspection_results"
    __table_args__ = (_ck("result", CHECK_RESULTS, "ck_result"),
                      UniqueConstraint("inspection_id", "check_id", name="uq_inspection_check"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inspection_id: Mapped[int] = mapped_column(ForeignKey("inspections.id"), nullable=False, index=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("checklist_checks.id"), nullable=False)
    result: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    text: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)


class InspectionPhoto(Base):
    __tablename__ = "inspection_photos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inspection_id: Mapped[int] = mapped_column(ForeignKey("inspections.id"), nullable=False, index=True)
    check_id: Mapped[int | None] = mapped_column(ForeignKey("checklist_checks.id"))
    file_name: Mapped[str] = mapped_column(String, nullable=False)
    stored_path: Mapped[str] = mapped_column(String, nullable=False)
    mime: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class Disposition(Base):
    """What the tooling person decided for a held item, with findings and who is responsible."""
    __tablename__ = "dispositions"
    __table_args__ = (_ck("decision", DISPOSITION_DECISIONS, "ck_disp_decision"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    inspection_id: Mapped[int | None] = mapped_column(ForeignKey("inspections.id"))
    decision: Mapped[str] = mapped_column(String, nullable=False)
    findings: Mapped[str] = mapped_column(Text, nullable=False)
    responsible: Mapped[str] = mapped_column(String, nullable=False)
    decided_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class RepairJob(Base):
    __tablename__ = "repair_jobs"
    __table_args__ = (_ck("status", REPAIR_STATUSES, "ck_repair_status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    disposition_id: Mapped[int] = mapped_column(ForeignKey("dispositions.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    opened_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    work_done: Mapped[str | None] = mapped_column(Text)
    measurements: Mapped[str | None] = mapped_column(Text)
    completed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class ScrapProposal(Base):
    """The item stays on HOLD until the designated scrap approver decides."""
    __tablename__ = "scrap_proposals"
    __table_args__ = (_ck("status", SCRAP_STATUSES, "ck_scrap_status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    disposition_id: Mapped[int] = mapped_column(ForeignKey("dispositions.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    findings: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_note: Mapped[str | None] = mapped_column(Text)


class ItemRevisionChange(Base):
    """Rework of one item to a new design revision: identity and history are kept."""
    __tablename__ = "item_revision_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    old_revision_id: Mapped[int] = mapped_column(ForeignKey("design_revisions.id"), nullable=False)
    new_revision_id: Mapped[int] = mapped_column(ForeignKey("design_revisions.id"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    repair_id: Mapped[int | None] = mapped_column(ForeignKey("repair_jobs.id"))
    changed_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)  # Engineering approver
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


# ------------------------------------------------------------------ documents in force at issue
class IssueDocument(Base):
    """The approved document versions in force when an issue was made. New work uses the then-current
    approved version; this keeps the version past issues actually used (spec section 6)."""
    __tablename__ = "issue_documents"
    __table_args__ = (UniqueConstraint("issue_id", "document_version_id", name="uq_issue_doc"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"), nullable=False, index=True)
    document_version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), nullable=False)


# ------------------------------------------------------------------ usage from OMS
class UsageEvent(Base):
    """One production usage report from OMS (cycles and castings are kept separate). Uniquely identified by
    (source_event_id, version); a higher version supersedes the earlier one, a replay changes nothing. Only
    POSTED events count towards totals, so a correction takes effect exactly once. Weight, UOM and ME revision
    are snapshotted per event, so later master-data changes never rewrite history."""
    __tablename__ = "usage_events"
    __table_args__ = (_ck("status", USAGE_STATUSES, "ck_usage_status"),
                      UniqueConstraint("source_event_id", "version", name="uq_usage_source_version"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_event_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(Text)
    received_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    wo_id: Mapped[str | None] = mapped_column(String, index=True)
    operation_id: Mapped[str | None] = mapped_column(String)
    build_id: Mapped[int | None] = mapped_column(ForeignKey("builds.id"), index=True)
    issue_id: Mapped[int | None] = mapped_column(ForeignKey("issues.id"))
    production_start: Mapped[datetime | None] = mapped_column(DateTime)
    production_end: Mapped[datetime | None] = mapped_column(DateTime)
    cycles: Mapped[int | None] = mapped_column(Integer)
    castings: Mapped[int | None] = mapped_column(Integer)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    weight_uom: Mapped[str] = mapped_column(String, nullable=False, default="kg")
    weight_revision: Mapped[str | None] = mapped_column(String)
    weight_source: Mapped[str | None] = mapped_column(String)  # PAYLOAD or ME_MAPPING; NULL = unknown
    tonnage: Mapped[float | None] = mapped_column(Float)  # NULL = unknown, never 0
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    overrides: Mapped[str | None] = mapped_column(Text)  # JSON: values entered at reconciliation (payload stays as received)


class UsageExposure(Base):
    """Usage attributed to the assembly (kind ASSEMBLY, item NULL) and to each reusable element that was
    fitted in the build at production time (kind ELEMENT). Element tonnage is exposure, not output: never
    sum it to get foundry output."""
    __tablename__ = "usage_exposures"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("usage_events.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    build_id: Mapped[int] = mapped_column(ForeignKey("builds.id"), nullable=False, index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), index=True)
    cycles: Mapped[int] = mapped_column(Integer, nullable=False)
    tonnage: Mapped[float | None] = mapped_column(Float)


# ------------------------------------------------------------------ physical stock counts
class StockCount(Base):
    __tablename__ = "stock_counts"
    __table_args__ = (_ck("status", COUNT_STATUSES, "ck_count_status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="OPEN")
    started_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    completed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)  # JSON snapshot of the report at completion


class StockCountScan(Base):
    __tablename__ = "stock_count_scans"
    __table_args__ = (UniqueConstraint("count_id", "item_id", name="uq_count_item"),
                      UniqueConstraint("count_id", "batch_id", name="uq_count_batch"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    count_id: Mapped[int] = mapped_column(ForeignKey("stock_counts.id"), nullable=False, index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("consumable_batches.id"))
    quantity: Mapped[float | None] = mapped_column(Float)  # batches: quantity actually found
    scanned_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


# ------------------------------------------------------------------ documents
class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (_ck("doc_type", DOC_TYPES, "ck_doc_type"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    doc_type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (_ck("status", APPROVAL_STATUSES, "ck_docver_status"),
                      UniqueConstraint("document_id", "revision", name="uq_document_revision"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    revision: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="DRAFT")
    file_name: Mapped[str] = mapped_column(String, nullable=False)
    stored_path: Mapped[str] = mapped_column(String, nullable=False)
    mime: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    change_reason: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class DocumentLink(Base):
    __tablename__ = "document_links"
    __table_args__ = (_ck("target_type", LINK_TARGETS, "ck_link_target"),
                      UniqueConstraint("document_id", "target_type", "target_id", name="uq_doc_link"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)


# ------------------------------------------------------------------ audit / import review
class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    username: Mapped[str | None] = mapped_column(String)
    action: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    entity_id: Mapped[str | None] = mapped_column(String, index=True)
    before: Mapped[str | None] = mapped_column(Text)
    after: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)


class ImportReview(Base):
    """Rows from an import that clash on legacy number; held for a human decision."""
    __tablename__ = "import_review"
    __table_args__ = (_ck("status", REVIEW_STATUSES, "ck_review_status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    source_file: Mapped[str | None] = mapped_column(String)
    row_number: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[str] = mapped_column(Text, nullable=False)  # the row as JSON
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))


# ------------------------------------------------------------------ view + triggers
ITEM_STATE_VIEW = """
CREATE VIEW item_state AS
SELECT
    l.item_id AS item_id,
    l.to_condition AS item_condition,
    l.to_custody AS custody,
    l.to_location_id AS location_id,
    l.txn_time AS since,
    l.txn_id AS txn_id,
    l.event_id AS event_id,
    (SELECT MAX(x.item_seq) FROM ledger x WHERE x.item_id = l.item_id) AS last_seq
FROM ledger l
WHERE l.txn_id = (
    SELECT MAX(l2.txn_id) FROM ledger l2
    WHERE l2.item_id = l.item_id
      AND l2.action <> 'REVERSAL'
      AND NOT EXISTS (
          SELECT 1 FROM ledger r WHERE r.action = 'REVERSAL' AND r.ref_txn_id = l2.txn_id
      )
)
"""


def _append_only(table: str) -> list[str]:
    return [
        f"""CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, '{table} is append-only: UPDATE not allowed'); END""",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, '{table} is append-only: DELETE not allowed'); END""",
    ]


_NON_NEGATIVE = """CREATE TRIGGER IF NOT EXISTS batch_ledger_non_negative AFTER INSERT ON batch_ledger
BEGIN
    SELECT RAISE(ABORT, 'batch balance cannot go negative') WHERE
        (SELECT COALESCE(SUM(d_usable), 0) FROM batch_ledger WHERE batch_id = NEW.batch_id) < -0.000001
     OR (SELECT COALESCE(SUM(d_held), 0) FROM batch_ledger WHERE batch_id = NEW.batch_id) < -0.000001
     OR (SELECT COALESCE(SUM(d_outstanding), 0) FROM batch_ledger WHERE batch_id = NEW.batch_id) < -0.000001;
END"""

EXTRA_DDL = (
    _append_only("ledger") + _append_only("audit_log") + _append_only("batch_ledger")
    + [
        _NON_NEGATIVE,
        # one reversal per batch row, one open inspection / open repair / pending scrap proposal per item
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_batch_one_reversal ON batch_ledger(ref_txn_id) WHERE action = 'REVERSAL'",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_one_open_inspection ON inspections(item_id) WHERE status = 'OPEN'",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_one_open_repair ON repair_jobs(item_id) WHERE status = 'OPEN'",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_one_pending_scrap ON scrap_proposals(item_id) WHERE status = 'PENDING'",
        # a txn can be reversed at most once, even if two requests race
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_one_reversal ON ledger(ref_txn_id) "
        "WHERE action = 'REVERSAL'",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_location_root_code ON locations(code) "
        "WHERE parent_id IS NULL",
        # an item can belong to only one active physical build at any instant (even under races)
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_membership ON build_components(item_id) "
        "WHERE removed_at IS NULL",
    ]
)


def create_views_and_triggers(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP VIEW IF EXISTS item_state"))
        conn.execute(text(ITEM_STATE_VIEW))
        for ddl in EXTRA_DDL:
            conn.execute(text(ddl))
