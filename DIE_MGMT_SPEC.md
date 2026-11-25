# DIE Management System — Build Spec (v1)

VSPL, SG iron centrifugal casting. Build the module first; real data is imported later via Excel templates.

## Stack
- Backend: Python, FastAPI, SQLAlchemy, SQLite (file: `die_mgmt.db`)
- Frontend: React + Vite
- Excel import/export: pandas + openpyxl
- Run locally on Windows from `C:\VSPL\die_mgmt`

## Core rules (non-negotiable)
1. Availability is tracked on **physical items** (Die, Top Flange, Bottom Flange, Pattern), never on an assembly. One die can belong to many assemblies.
2. The **ledger is append-only**. No updates and no deletes. Corrections are new reversing entries.
3. **Status and location are derived** from each item's latest ledger entry, never typed in manually.
4. Issuing an assembly is **atomic**: all its items are issued together, or none are.
5. An issue is **blocked** if any item is In Use, Under Repair, or Scrapped. The error must name the blocking item, its WO and batch.
6. Every item ID is normalised on save: trimmed, uppercased, zero-padding removed (`b32-01` → `B32-1`).

## Data model
**items**: item_id (PK), item_type (DIE | TOP_FLANGE | BOTTOM_FLANGE | PATTERN), description, od, id_dim, length, home_location, remarks, active, created_at

**assemblies**: assembly_id (PK, e.g. T211), description, od, length, die_id (FK), pattern_id (FK), top_flange_id (FK, nullable), bottom_flange_id (FK, nullable), pouring_wt, gating_wt, rcd_wt, active

**ledger**: txn_id (PK, auto), txn_time, item_id (FK), assembly_id (nullable), action (RECEIVE | ISSUE | RETURN | TRANSFER | SEND_REPAIR | BACK_FROM_REPAIR | SCRAP | REVERSAL), from_location, to_location, wo_id, batch_no, issued_by, issued_to, machine, remarks, ref_txn_id (for REVERSAL), group_id (links all rows of one assembly issue)

**item_status** (view): item_id, item_type, status (AVAILABLE | IN_USE | UNDER_REPAIR | SCRAPPED), current_location, wo_id, batch_no, issued_to, since

Status from latest action: RECEIVE / RETURN / TRANSFER / BACK_FROM_REPAIR → AVAILABLE; ISSUE → IN_USE; SEND_REPAIR → UNDER_REPAIR; SCRAP → SCRAPPED.

## API
- `GET/POST/PUT /items`, `GET /items/{id}` (with status + assemblies it belongs to + ledger history)
- `GET/POST/PUT /assemblies`, `GET /assemblies/{id}` (all 4 items with live status; flag "ready to issue")
- `POST /issue` → {assembly_id or item_ids[], wo_id, batch_no, issued_by, issued_to, machine, remarks}. WO, batch, issued_by and issued_to are required.
- `POST /return` → {group_id or item_ids[], to_location, condition_remarks}
- `POST /transfer`, `POST /repair/send`, `POST /repair/back`, `POST /scrap`, `POST /reverse/{txn_id}`
- `GET /ledger` with filters: item, assembly, WO, batch, person, action, date range
- `GET /search?q=` → matches item, assembly, WO or batch
- `POST /import/{items|assemblies}` (Excel upload, validate-then-commit with an error report), `GET /export/{ledger|status}`

## Screens
1. **Search / Dashboard**: one search box. Typing a die, flange, pattern, assembly, WO or batch shows status, location, and who has it. Counts of Available / In Use / Repair.
2. **Assembly view**: the 4 mapped items with status chips; Issue button enabled only when all are available.
3. **Issue form**: assembly → auto-fills items; enter WO ID, batch, issued by, issued to, machine.
4. **Return form**: pick an open issue (by WO or assembly) and set the return location.
5. **Masters**: CRUD for items and assemblies (mapping editor).
6. **Ledger**: filterable table, Excel export.
7. **Import**: upload templates, preview, see errors, commit.

## Import templates (data comes later)
- `items_template.xlsx`: item_id, item_type, description, od, id_dim, length, home_location
- `assemblies_template.xlsx`: assembly_id, description, od, length, die_id, pattern_id, top_flange_id, bottom_flange_id, pouring_wt, gating_wt, rcd_wt
- Import creates a RECEIVE ledger entry per new item at its home_location.
- Validation: unknown item references, duplicate IDs, and an item_type mismatch (e.g. a pattern ID in die_id).

## Build phases
1. DB models, ID normalisation, status view, plus unit tests for the issue/return/blocking rules
2. API endpoints
3. React screens: Search → Issue → Return → Masters → Ledger
4. Excel import/export + templates
5. Seed with ~10 fake items/assemblies for testing

## Tests that must pass
- Issuing an assembly whose die is in use under another assembly fails and names the WO.
- A partial failure writes no ledger rows.
- Return makes all items Available at the given location.
- A reversal restores the previous status.
- `c56` and `C56` resolve to the same item.
