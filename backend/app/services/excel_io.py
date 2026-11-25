"""Excel reading/writing only (pandas + openpyxl). Business rules live in the services."""
from io import BytesIO
from pathlib import Path

import pandas as pd

from .errors import ValidationError
from .imports import ITEM_COLUMNS

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "templates"
ITEMS_TEMPLATE = "items_template.xlsx"
NOTES = [
    "One row per physical item. Item identity (system ID, QR) is generated on import.",
    "legacy_no: the existing tooling number as printed on the tool. May repeat; see display_id.",
    "display_id: optional unique display ID. If blank, the legacy number is used. A legacy number that "
    "already exists (in the system or earlier in the file) is NOT overwritten: the row is held in the "
    "review queue until someone assigns a unique display ID or rejects it.",
    "design_code: an existing element design with an approved revision. design_revision: optional "
    "(blank = current approved revision).",
    "condition: SERVICEABLE, AWAITING_INSPECTION, HOLD, UNDER_REPAIR or SCRAPPED.",
    "home_location / current_location: location path like P1/A1/R1/S1/B1 (plant/area/rack/shelf/bin). "
    "current_location is optional (blank = home).",
    "acquisition_date: YYYY-MM-DD (optional). opening_cycles / opening_tonnage: leave BLANK if unknown. "
    "Blank is stored as unknown, never as zero.",
    "Nothing is saved until you commit the preview; any error rejects the whole file.",
]


def _to_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for i, col in enumerate(df.columns, start=1):
                width = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)]) + 2
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(width, 70)
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


def items_template() -> bytes:
    return _to_bytes({"data": pd.DataFrame(columns=ITEM_COLUMNS), "instructions": pd.DataFrame({"Notes": NOTES})})


def write_templates(directory: Path = TEMPLATE_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / ITEMS_TEMPLATE
    p.write_bytes(items_template())
    return [p]


LEDGER_COLUMNS = ["txn_id", "txn_time", "event_id", "display_id", "action", "from_condition",
                  "to_condition", "from_custody", "to_custody", "from_location", "to_location",
                  "user", "remarks", "ref_txn_id", "group_id"]
ITEM_EXPORT_COLUMNS = ["system_id", "display_id", "legacy_no", "design_code", "description", "category",
                       "revision", "condition", "custody", "location", "home_location", "home_mismatch",
                       "active", "since"]


def rows_xlsx(sheet: str, columns: list[str], rows: list[dict]) -> bytes:
    """Any report as a workbook: one sheet, the given columns, plain values."""
    flat = [{c: (", ".join(map(str, r.get(c))) if isinstance(r.get(c), (list, tuple)) else r.get(c)) for c in columns}
            for r in rows]
    return _to_bytes({sheet[:30]: pd.DataFrame(flat, columns=columns)})


def ledger_xlsx(rows: list[dict]) -> bytes:
    return _to_bytes({"ledger": pd.DataFrame(rows, columns=LEDGER_COLUMNS)})


def items_xlsx(rows: list[dict]) -> bytes:
    return _to_bytes({"items": pd.DataFrame(rows, columns=ITEM_EXPORT_COLUMNS)})
