from datetime import date

from pydantic import BaseModel


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str


class UserCreate(BaseModel):
    username: str
    full_name: str
    password: str
    roles: list[str] = []


class RolesIn(BaseModel):
    roles: list[str]


class ActiveIn(BaseModel):
    active: bool


class PasswordResetIn(BaseModel):
    new_password: str


class CategoryIn(BaseModel):
    code: str
    name: str


class CategoryUpdate(BaseModel):
    name: str | None = None
    active: bool | None = None


class UomIn(BaseModel):
    code: str
    name: str


class LocationIn(BaseModel):
    code: str
    name: str | None = None
    parent_id: int | None = None


class LocationUpdate(BaseModel):
    name: str | None = None
    active: bool | None = None


class DesignIn(BaseModel):
    code: str
    description: str
    category_id: int
    base_uom: str
    kind: str = "REUSABLE"
    specifications: str | None = None
    storage_instructions: str | None = None


class DesignUpdate(BaseModel):
    description: str | None = None
    category_id: int | None = None
    active: bool | None = None


class RevisionIn(BaseModel):
    reason: str | None = None
    specifications: str | None = None
    storage_instructions: str | None = None
    revision: str | None = None


class RevisionUpdate(BaseModel):
    specifications: str | None = None
    storage_instructions: str | None = None


class ReasonIn(BaseModel):
    reason: str | None = None


class ItemIn(BaseModel):
    design_id: int
    design_revision_id: int | None = None
    legacy_no: str | None = None
    display_id: str | None = None
    condition: str
    location_id: int
    current_location_id: int | None = None
    acquisition_date: date | None = None
    provenance: str | None = None
    opening_cycles: int | None = None
    opening_tonnage: float | None = None
    remarks: str | None = None


class ItemUpdate(BaseModel):
    remarks: str | None = None
    provenance: str | None = None
    acquisition_date: date | None = None
    home_location_id: int | None = None
    opening_cycles: int | None = None
    opening_tonnage: float | None = None


class MoveIn(BaseModel):
    item_ids: list[int]
    to_location_id: int
    remarks: str | None = None


class ConditionIn(BaseModel):
    item_ids: list[int]
    to_condition: str
    reason: str | None = None


class AliasIn(BaseModel):
    alias: str
    reason: str | None = None


class AliasDecisionIn(BaseModel):
    note: str | None = None


class DocumentIn(BaseModel):
    doc_key: str
    doc_type: str
    title: str


class LinkIn(BaseModel):
    target_type: str
    target_id: int


class ResolveIn(BaseModel):
    display_id: str
    note: str | None = None


class RejectIn(BaseModel):
    note: str | None = None


# ---- assemblies / mappings / builds
class AssemblyIn(BaseModel):
    code: str
    description: str
    category_id: int
    instructions: str | None = None
    checklist_refs: str | None = None


class AssemblyUpdate(BaseModel):
    description: str | None = None
    category_id: int | None = None
    active: bool | None = None


class AssemblyRevisionIn(BaseModel):
    reason: str | None = None
    instructions: str | None = None
    checklist_refs: str | None = None
    revision: str | None = None


class AssemblyRevisionUpdate(BaseModel):
    instructions: str | None = None
    checklist_refs: str | None = None


class LineIn(BaseModel):
    design_id: int
    quantity: float = 1
    quantity_basis: str | None = None
    allowed_revisions: str | None = None
    remarks: str | None = None


class LineUpdate(BaseModel):
    quantity: float | None = None
    quantity_basis: str | None = None
    allowed_revisions: str | None = None
    remarks: str | None = None


class AlternativeIn(BaseModel):
    design_id: int
    allowed_revisions: str | None = None


class MappingIn(BaseModel):
    part_id: str
    routing_revision: str
    operation_id: str
    definition_id: int
    definition_revision_id: int | None = None
    selected: bool = False
    tooling_status: str = "READY"
    pouring_weight_kg: float | None = None
    pouring_weight_revision: str | None = None
    external_ref: str | None = None


class MappingUpdate(BaseModel):
    definition_revision_id: int | None = None
    selected: bool | None = None
    tooling_status: str | None = None
    pouring_weight_kg: float | None = None
    pouring_weight_revision: str | None = None
    external_ref: str | None = None
    active: bool | None = None
    reason: str | None = None


class BuildIn(BaseModel):
    definition_id: int
    location_id: int
    revision_id: int | None = None
    remarks: str | None = None


class FitIn(BaseModel):
    item_id: int


class RemoveIn(BaseModel):
    item_id: int
    to_location_id: int
    reason: str | None = None


class ReplaceIn(BaseModel):
    remove_item_id: int
    new_item_id: int
    to_location_id: int
    reason: str | None = None


class BuildMoveIn(BaseModel):
    to_location_id: int
    remarks: str | None = None


class Destination(BaseModel):
    item_id: int
    location_id: int


class DismantleIn(BaseModel):
    destinations: list[Destination]
    reason: str | None = None


# ---- readiness / issue / return
class ReadinessIn(BaseModel):
    wo_id: str
    part_id: str
    routing_revision: str
    operation_id: str
    mapping_id: int | None = None


class BatchPick(BaseModel):
    line_id: int
    batch_id: int
    quantity: float


class SetupLineIn(BaseModel):
    wo_id: str
    part_id: str
    tool_description: str | None = None
    family: str | None = None
    grade: str | None = None
    internal_id: str | None = None
    fg_qty: int | None = None
    rcd_qty: int | None = None
    weight: float | None = None


class SetupIn(BaseModel):
    """A setup with its WO / part lines (`lines`); for a single line wo_id / part_id may be given directly."""
    name: str | None = None
    plan_ref: str | None = None
    lines: list[SetupLineIn] | None = None
    wo_id: str | None = None
    part_id: str | None = None
    tool_description: str | None = None
    family: str | None = None
    grade: str | None = None
    internal_id: str | None = None
    fg_qty: int | None = None
    rcd_qty: int | None = None
    weight: float | None = None


class SetupMapIn(BaseModel):
    definition_id: int
    part_id: str | None = None
    description: str | None = None
    family: str | None = None
    note: str | None = None


class IssueCreate(BaseModel):
    setup_id: int  # every issue is for a planned setup
    wo_id: str | None = None  # optional: only the lines of this WO
    line_ids: list[int] | None = None  # which WO / part lines of the setup this issue covers (default: all)
    line_mappings: dict[int, int] | None = None  # line id -> ME mapping (operation); default: the ME-selected one for the part
    mapping_id: int | None = None  # one operation for every line of that part
    build_id: int
    confirmed_item_ids: list[int]
    scanned_item_ids: list[int] | None = None
    destination_location_id: int | None = None
    remarks: str | None = None
    consumables: list[BatchPick] | None = None
    override_reason: str | None = None


class ReturnLine(BaseModel):
    item_id: int
    to_location_id: int | None = None  # blank = the part's home bin
    damaged: bool = False
    remarks: str | None = None


class ConsumableReturn(BaseModel):
    batch_id: int
    unused_quantity: float


class ReturnIn(BaseModel):
    lines: list[ReturnLine] = []
    consumables: list[ConsumableReturn] = []


# ---- batches
class BatchReceiveIn(BaseModel):
    design_id: int
    quantity: float
    location_id: int
    supplier_lot: str | None = None
    expiry_date: date | None = None
    remarks: str | None = None


class BatchQtyIn(BaseModel):
    quantity: float
    reason: str | None = None


class BatchAdjustIn(BaseModel):
    delta: float
    reason: str | None = None


class BatchMoveIn(BaseModel):
    to_location_id: int
    remarks: str | None = None


# ---- checklists / inspections / disposition
class ChecklistCreate(BaseModel):
    scope_type: str
    scope_id: int
    name: str | None = None
    starter: bool = False


class CheckIn(BaseModel):
    code: str
    title: str
    check_type: str = "PASS_FAIL"
    required: bool = True
    na_allowed: bool = False
    evidence: str = "NONE"
    unit: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    guidance: str | None = None


class CheckUpdate(BaseModel):
    title: str | None = None
    guidance: str | None = None
    required: bool | None = None
    na_allowed: bool | None = None
    evidence: str | None = None
    unit: str | None = None
    min_value: float | None = None
    max_value: float | None = None


class InspectionStart(BaseModel):
    item_id: int


class ResultIn(BaseModel):
    check_id: int
    result: str | None = None
    value: float | None = None
    text: str | None = None
    reason: str | None = None


class CompleteIn(BaseModel):
    summary: str | None = None


class DispositionIn(BaseModel):
    decision: str
    findings: str | None = None
    responsible: str | None = None
    inspection_id: int | None = None


class RepairComplete(BaseModel):
    work_done: str | None = None
    measurements: str | None = None


class ReworkIn(BaseModel):
    new_revision_id: int
    reason: str | None = None
    repair_id: int | None = None


class ScrapDecision(BaseModel):
    approve: bool
    note: str | None = None


# ---- usage / counts / replacement
class ResolveUsage(BaseModel):
    reason: str | None = None
    build: str | None = None
    castings: int | None = None
    cycles: int | None = None
    weight_kg: float | None = None
    weight_revision: str | None = None
    wo_id: str | None = None


class ReplaceInProduction(BaseModel):
    outgoing_item_id: int
    incoming_item_id: int
    reason: str | None = None


class CountStart(BaseModel):
    location_id: int
    note: str | None = None


class CountScan(BaseModel):
    code: str
    quantity: float | None = None


class CountComplete(BaseModel):
    note: str | None = None


class CountItemAction(BaseModel):
    item_id: int
    action: str
    reason: str | None = None
    location_id: int | None = None


class CountBatchAdjust(BaseModel):
    batch_id: int
    reason: str | None = None


class CountApply(BaseModel):
    item_actions: list[CountItemAction] = []
    batch_adjustments: list[CountBatchAdjust] = []


class DieSetIn(BaseModel):
    die_item_id: int
    pattern_item_id: int | None = None
    top_flange_item_id: int | None = None
    bottom_flange_item_id: int | None = None
    note: str | None = None


class HandOverIn(BaseModel):
    setup_id: int
    wo_id: str | None = None
    line_ids: list[int] | None = None
    line_mappings: dict[int, int] | None = None
    mapping_id: int | None = None
    consumable_returns: list[ConsumableReturn] = []
    consumables: list[BatchPick] | None = None
    override_reason: str | None = None
    remarks: str | None = None
