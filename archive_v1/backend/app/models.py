from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ITEM_TYPES = ("DIE", "TOP_FLANGE", "BOTTOM_FLANGE", "PATTERN")
ACTIONS = (
    "RECEIVE", "ISSUE", "RETURN", "TRANSFER",
    "SEND_REPAIR", "BACK_FROM_REPAIR", "SCRAP", "REVERSAL",
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (CheckConstraint(f"item_type IN ({_in(ITEM_TYPES)})", name="ck_item_type"),)

    item_id: Mapped[str] = mapped_column(String, primary_key=True)
    item_type: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    od: Mapped[float | None] = mapped_column(Float)
    id_dim: Mapped[float | None] = mapped_column(Float)
    length: Mapped[float | None] = mapped_column(Float)
    home_location: Mapped[str | None] = mapped_column(String)
    remarks: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    # NOTE: deliberately no status / location columns. They are derived (see item_status view).


class Assembly(Base):
    __tablename__ = "assemblies"

    assembly_id: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str | None] = mapped_column(Text)
    od: Mapped[float | None] = mapped_column(Float)
    length: Mapped[float | None] = mapped_column(Float)
    die_id: Mapped[str] = mapped_column(ForeignKey("items.item_id"), nullable=False)
    pattern_id: Mapped[str] = mapped_column(ForeignKey("items.item_id"), nullable=False)
    top_flange_id: Mapped[str | None] = mapped_column(ForeignKey("items.item_id"))
    bottom_flange_id: Mapped[str | None] = mapped_column(ForeignKey("items.item_id"))
    pouring_wt: Mapped[float | None] = mapped_column(Float)
    gating_wt: Mapped[float | None] = mapped_column(Float)
    rcd_wt: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Ledger(Base):
    __tablename__ = "ledger"
    __table_args__ = (CheckConstraint(f"action IN ({_in(ACTIONS)})", name="ck_ledger_action"),)

    txn_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    txn_time: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.item_id"), nullable=False, index=True)
    assembly_id: Mapped[str | None] = mapped_column(ForeignKey("assemblies.assembly_id"))
    action: Mapped[str] = mapped_column(String, nullable=False)
    from_location: Mapped[str | None] = mapped_column(String)
    to_location: Mapped[str | None] = mapped_column(String)
    wo_id: Mapped[str | None] = mapped_column(String, index=True)
    batch_no: Mapped[str | None] = mapped_column(String, index=True)
    issued_by: Mapped[str | None] = mapped_column(String)
    issued_to: Mapped[str | None] = mapped_column(String)
    machine: Mapped[str | None] = mapped_column(String)
    remarks: Mapped[str | None] = mapped_column(Text)
    ref_txn_id: Mapped[int | None] = mapped_column(ForeignKey("ledger.txn_id"))
    group_id: Mapped[str | None] = mapped_column(String, index=True)  # link back to the original issue
    event_id: Mapped[str] = mapped_column(String, nullable=False, index=True)  # one per API call


# Effective entry of an item = its latest ledger row that is neither a REVERSAL nor itself
# reversed. A reversal therefore "un-does" the entry it points at, and the item falls back to
# the previous entry's state, all without ever updating or deleting a row.
ITEM_STATUS_VIEW = """
CREATE VIEW item_status AS
SELECT
    i.item_id AS item_id,
    i.item_type AS item_type,
    CASE l.action
        WHEN 'ISSUE' THEN 'IN_USE'
        WHEN 'SEND_REPAIR' THEN 'UNDER_REPAIR'
        WHEN 'SCRAP' THEN 'SCRAPPED'
        ELSE 'AVAILABLE'
    END AS status,
    l.to_location AS current_location,
    CASE WHEN l.action = 'ISSUE' THEN l.wo_id END AS wo_id,
    CASE WHEN l.action = 'ISSUE' THEN l.batch_no END AS batch_no,
    CASE WHEN l.action = 'ISSUE' THEN l.issued_to END AS issued_to,
    l.txn_time AS since,
    l.txn_id AS txn_id,
    l.assembly_id AS assembly_id,
    l.group_id AS group_id
FROM items i
JOIN ledger l ON l.txn_id = (
    SELECT MAX(l2.txn_id) FROM ledger l2
    WHERE l2.item_id = i.item_id
      AND l2.action <> 'REVERSAL'
      AND NOT EXISTS (
          SELECT 1 FROM ledger r WHERE r.action = 'REVERSAL' AND r.ref_txn_id = l2.txn_id
      )
)
"""

APPEND_ONLY_TRIGGERS = [
    """CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
       BEGIN SELECT RAISE(ABORT, 'ledger is append-only: UPDATE not allowed'); END""",
    """CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger
       BEGIN SELECT RAISE(ABORT, 'ledger is append-only: DELETE not allowed'); END""",
]


def create_views_and_triggers(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP VIEW IF EXISTS item_status"))
        conn.execute(text(ITEM_STATUS_VIEW))
        for ddl in APPEND_ONLY_TRIGGERS:
            conn.execute(text(ddl.replace("CREATE TRIGGER", "CREATE TRIGGER IF NOT EXISTS")))
