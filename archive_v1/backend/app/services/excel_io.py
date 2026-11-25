"""Excel reading/writing only (pandas + openpyxl). Business rules live in ledger.py."""
from io import BytesIO
from pathlib import Path

import pandas as pd

from .ledger import ASSEMBLY_COLUMNS, ITEM_COLUMNS, ValidationError

TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "templates"
TEMPLATES = {
    "items": ("items_template.xlsx", ITEM_COLUMNS, [
        "item_id: required. Normalised on import (trimmed, uppercased, 'b32-01' becomes 'B32-1').",
        "item_type: DIE, TOP_FLANGE, BOTTOM_FLANGE or PATTERN.",
        "od, id_dim, length: numbers (optional).",
        "home_location: where the item is received. Import writes one RECEIVE ledger entry per new item.",
        "Item IDs that already exist are rejected; the whole file is imported or nothing is.",
    ]),
    "assemblies": ("assemblies_template.xlsx", ASSEMBLY_COLUMNS, [
        "assembly_id, die_id, pattern_id: required.",
        "top_flange_id, bottom_flange_id: optional; leave blank when there is no flange.",
        "All referenced IDs must already exist as items of the right type (import items first).",
        "One die may appear in many assemblies.",
        "od, length, pouring_wt, gating_wt, rcd_wt: numbers (optional).",
    ]),
}


def _to_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for i, col in enumerate(df.columns, start=1):
                width = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)]) + 2
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(width, 60)
    return buf.getvalue()


def read_rows(data: bytes) -> tuple[list[str], list[dict]]:
    """First sheet -> (lower-case column names, rows as dicts; blanks become None)."""
    try:
        df = pd.read_excel(BytesIO(data), dtype=str, keep_default_na=False, sheet_name=0)
    except Exception as e:
        raise ValidationError(f"Could not read file as .xlsx: {e}") from e
    df.columns = [str(c).strip().lower() for c in df.columns]
    rows = [{k: (v.strip() if isinstance(v, str) and v.strip() else None) for k, v in r.items()}
            for r in df.to_dict("records")]
    return list(df.columns), rows


def make_template(kind: str) -> bytes:
    _, cols, notes = TEMPLATES[kind]
    return _to_bytes({"data": pd.DataFrame(columns=cols), "instructions": pd.DataFrame({"Notes": notes})})


def write_templates(directory: Path = TEMPLATE_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for kind, (fname, _, _) in TEMPLATES.items():
        p = directory / fname
        p.write_bytes(make_template(kind))
        paths.append(p)
    return paths


LEDGER_EXPORT_COLUMNS = ["txn_id", "txn_time", "item_id", "assembly_id", "action", "from_location",
                         "to_location", "wo_id", "batch_no", "issued_by", "issued_to", "machine",
                         "remarks", "ref_txn_id", "group_id", "event_id"]
STATUS_EXPORT_COLUMNS = ["item_id", "item_type", "description", "status", "current_location",
                         "wo_id", "batch_no", "issued_to", "since", "home_location", "active"]


def ledger_xlsx(rows) -> bytes:
    df = pd.DataFrame([{c: getattr(r, c) for c in LEDGER_EXPORT_COLUMNS} for r in rows],
                      columns=LEDGER_EXPORT_COLUMNS)
    return _to_bytes({"ledger": df})


def status_xlsx(rows: list[dict]) -> bytes:
    return _to_bytes({"status": pd.DataFrame(rows, columns=STATUS_EXPORT_COLUMNS)})
