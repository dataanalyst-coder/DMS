from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- items / assemblies
class ItemCreate(BaseModel):
    item_id: str
    item_type: str
    description: str | None = None
    od: float | None = None
    id_dim: float | None = None
    length: float | None = None
    home_location: str | None = None
    remarks: str | None = None


class ItemUpdate(BaseModel):
    description: str | None = None
    od: float | None = None
    id_dim: float | None = None
    length: float | None = None
    home_location: str | None = None
    remarks: str | None = None
    active: bool | None = None


class ItemOut(ORM):
    item_id: str
    item_type: str
    description: str | None
    od: float | None
    id_dim: float | None
    length: float | None
    home_location: str | None
    remarks: str | None
    active: bool
    created_at: datetime


class AssemblyCreate(BaseModel):
    assembly_id: str
    description: str | None = None
    od: float | None = None
    length: float | None = None
    die_id: str
    pattern_id: str
    top_flange_id: str | None = None
    bottom_flange_id: str | None = None
    pouring_wt: float | None = None
    gating_wt: float | None = None
    rcd_wt: float | None = None


class AssemblyUpdate(BaseModel):
    description: str | None = None
    od: float | None = None
    length: float | None = None
    die_id: str | None = None
    pattern_id: str | None = None
    top_flange_id: str | None = None
    bottom_flange_id: str | None = None
    pouring_wt: float | None = None
    gating_wt: float | None = None
    rcd_wt: float | None = None
    active: bool | None = None


class AssemblyOut(ORM):
    assembly_id: str
    description: str | None
    od: float | None
    length: float | None
    die_id: str
    pattern_id: str
    top_flange_id: str | None
    bottom_flange_id: str | None
    pouring_wt: float | None
    gating_wt: float | None
    rcd_wt: float | None
    active: bool


class LedgerOut(ORM):
    txn_id: int
    txn_time: datetime
    item_id: str
    assembly_id: str | None
    action: str
    from_location: str | None
    to_location: str | None
    wo_id: str | None
    batch_no: str | None
    issued_by: str | None
    issued_to: str | None
    machine: str | None
    remarks: str | None
    ref_txn_id: int | None
    group_id: str | None
    event_id: str | None


# ---- operations (required-field checks live in the service so messages are uniform)
class IssueIn(BaseModel):
    assembly_id: str | None = None
    item_ids: list[str] | None = None
    wo_id: str | None = None
    batch_no: str | None = None
    issued_by: str | None = None
    issued_to: str | None = None
    machine: str | None = None
    remarks: str | None = None


class ReturnIn(BaseModel):
    group_id: str | None = None
    item_ids: list[str] | None = None
    to_location: str | None = None
    condition_remarks: str | None = None
    returned_by: str | None = None


class MoveIn(BaseModel):
    item_ids: list[str]
    to_location: str | None = None
    by: str | None = None
    remarks: str | None = None


class ReverseIn(BaseModel):
    by: str | None = None
    remarks: str | None = None
