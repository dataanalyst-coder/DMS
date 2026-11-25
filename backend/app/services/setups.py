"""Planned setups: what Planning wants made, matched to the tool assembly that serves it.

A setup is one tooling set mounted once; it runs one or more WO / part lines. Planning sends a sheet (a SETUP column
groups its rows) or types lines in. Matching to tooling is a table kept by Engineering (SetupToolMap), evaluated live,
so nothing is guessed and a corrected rule applies at once. Issuing must name a setup (see issues.create_issue)."""
import re
from io import BytesIO

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import (AssemblyDefinition, Build, Category, ElementDesign, Issue, IssueLine, Item, Setup, SetupLine,
                      SetupToolMap, User)
from . import assemblies, audit, builds, config, ledger, permissions
from .common import clean, require_reason, transaction
from .errors import NotFoundError, ValidationError

COLUMN_ALIASES = {
    "setup": ("SETUP", "SETUPNO", "SETUPID", "SETUPNAME", "SETUPNUMBER"),
    "wo": ("WONO", "WO", "WONUMBER", "WOID", "WORKORDER", "WORKORDERNO"),
    "part": ("PARTID", "PART", "PARTNO", "PARTNUMBER"),
    "description": ("TOOLDESCRIPTION", "TOOLDESC", "TOOL", "DESCRIPTION"),
    "family": ("FAMILY", "PARTFAMILY"),
    "grade": ("INTERNALGRADE", "GRADE"),
    "internal_id": ("INTERNALID", "SLNO", "SL", "SRNO", "SNO"),
    "fg_qty": ("FGQTY", "FG"),
    "rcd_qty": ("RCDQTY", "RCD", "RECEIVEDQTY"),
    "weight": ("WEIGHT", "WT"),
}
TEMPLATE_COLUMNS = ["SETUP", "INTERNAL ID", "WO NO", "PART ID", "TOOL DESCRIPTION", "FAMILY", "INTERNAL GRADE", "FG QTY", "RCD QTY", "WEIGHT"]


def key(raw) -> str | None:
    """Comparison key: upper case, no spaces ('BLV 003' == 'blv003')."""
    v = re.sub(r"\s+", "", str(raw or "")).upper()
    return v or None


def _norm_part(raw) -> str:
    v = key(raw)
    if not v:
        raise ValidationError("PART ID is required")
    return v


def _get(db: Session, setup_id: int) -> Setup:
    s = db.get(Setup, setup_id)
    if s is None:
        raise NotFoundError(f"Setup {setup_id} not found")
    return s


def setup_code(setup_id: int) -> str:
    return f"SET-{setup_id:06d}"


def lines_of(db: Session, setup_id: int) -> list[SetupLine]:
    return db.query(SetupLine).filter(SetupLine.setup_id == setup_id).order_by(SetupLine.id).all()


def _int(raw, what):
    if raw in (None, ""):
        return None
    try:
        f = float(str(raw).replace(",", ""))
    except ValueError:
        raise ValidationError(f"{what} must be a number, got {raw!r}")
    if f < 0 or f != int(f):
        raise ValidationError(f"{what} must be a whole number >= 0, got {raw!r}")
    return int(f)


def _float(raw, what):
    if raw in (None, ""):
        return None
    try:
        f = float(str(raw).replace(",", ""))
    except ValueError:
        raise ValidationError(f"{what} must be a number, got {raw!r}")
    if f < 0:
        raise ValidationError(f"{what} cannot be negative")
    return f


# ------------------------------------------------------------------ the tool-mapping table (Engineering)
def add_map(db: Session, actor: User, *, definition_id: int, part_id=None, description=None, family=None, note=None) -> SetupToolMap:
    permissions.require(actor, "setup.map")
    d = assemblies.get_definition(db, definition_id)
    part, desc, fam = key(part_id), key(description), key(family)
    if not (part or desc or fam):
        raise ValidationError("Give at least one of part ID, tool description or family for the rule")
    dup = db.query(SetupToolMap).filter_by(part_id=part, description_key=desc, family_key=fam, definition_id=d.id, active=True).first()
    if dup:
        raise ValidationError("That rule already exists")
    with transaction(db):
        m = SetupToolMap(part_id=part, description_key=desc, family_key=fam, description_text=clean(description),
                         definition_id=d.id, note=clean(note), created_by=actor.id)
        db.add(m)
        db.flush()
        audit.record(db, actor, "SETUP_MAP_ADD", "setup_map", m.id,
                     after={"part": part, "description": desc, "family": fam, "assembly": d.code})
    return m


def deactivate_map(db: Session, actor: User, map_id: int, *, reason) -> SetupToolMap:
    permissions.require(actor, "setup.map")
    reason = require_reason(reason, "removing a rule")
    m = db.get(SetupToolMap, map_id)
    if m is None:
        raise NotFoundError(f"Rule {map_id} not found")
    with transaction(db):
        m.active = False
        audit.record(db, actor, "SETUP_MAP_REMOVE", "setup_map", m.id, reason=reason)
    return m


def map_dict(db: Session, m: SetupToolMap) -> dict:
    d = db.get(AssemblyDefinition, m.definition_id)
    return {"id": m.id, "part_id": m.part_id, "description": m.description_text or m.description_key, "family": m.family_key,
            "definition_id": d.id, "assembly_code": d.code, "assembly_description": d.description, "note": m.note, "active": m.active}


def list_maps(db: Session, *, active=True) -> list[dict]:
    q = db.query(SetupToolMap)
    if active is not None:
        q = q.filter(SetupToolMap.active == active)
    return [map_dict(db, m) for m in q.order_by(SetupToolMap.id)]


def _active_maps(db: Session) -> list[SetupToolMap]:
    return db.query(SetupToolMap).filter(SetupToolMap.active.is_(True)).all()


def match_line(db: Session, ln: SetupLine, maps: list[SetupToolMap] | None = None) -> dict:
    """Which assembly serves this line. The most specific matching rule wins; a tie between different assemblies is
    AMBIGUOUS and no assembly is chosen."""
    maps = maps if maps is not None else _active_maps(db)
    part, desc, fam = ln.part_id, key(ln.tool_description), key(ln.family)
    hits = []
    for m in maps:
        if m.part_id and m.part_id != part:
            continue
        if m.description_key and m.description_key != desc:
            continue
        if m.family_key and m.family_key != fam:
            continue
        hits.append((sum(bool(x) for x in (m.part_id, m.description_key, m.family_key)), m))
    if not hits:
        return {"status": "UNMATCHED", "definition_id": None, "reason": "No tool mapping rule covers this part / description / family"}
    best = max(n for n, _ in hits)
    top = [m for n, m in hits if n == best]
    defs = sorted({m.definition_id for m in top})
    if len(defs) > 1:
        codes = ", ".join(db.get(AssemblyDefinition, i).code for i in defs)
        return {"status": "AMBIGUOUS", "definition_id": None, "reason": f"Rules of equal specificity point to different assemblies ({codes})"}
    d = db.get(AssemblyDefinition, defs[0])
    if not d.active:
        return {"status": "UNMATCHED", "definition_id": None, "reason": f"Assembly {d.code} is inactive"}
    if assemblies.current_approved(db, d.id) is None:
        return {"status": "UNMATCHED", "definition_id": d.id, "reason": f"Assembly {d.code} has no approved revision yet"}
    return {"status": "MATCHED", "definition_id": d.id, "rule_id": top[0].id, "reason": None}


def match_setup(db: Session, s: Setup, maps: list[SetupToolMap] | None = None, lines=None) -> dict:
    """The setup is MATCHED only when every line points to the SAME assembly (one tooling set serves the whole setup)."""
    maps = maps if maps is not None else _active_maps(db)
    lines = lines if lines is not None else lines_of(db, s.id)
    if not lines:
        return {"status": "UNMATCHED", "definition_id": None, "reason": "The setup has no WO / part lines"}
    res = [(ln, match_line(db, ln, maps)) for ln in lines]
    bad = [(ln, r) for ln, r in res if r["status"] != "MATCHED"]
    if bad:
        ln, r = bad[0]
        more = f" (and {len(bad) - 1} more)" if len(bad) > 1 else ""
        status = "AMBIGUOUS" if any(r2["status"] == "AMBIGUOUS" for _, r2 in bad) else "UNMATCHED"
        return {"status": status, "definition_id": None, "reason": f"{ln.wo_id} / {ln.part_id}: {r['reason']}{more}"}
    defs = {r["definition_id"] for _, r in res}
    if len(defs) > 1:
        names = ", ".join(sorted(db.get(AssemblyDefinition, i).code for i in defs))
        return {"status": "AMBIGUOUS", "definition_id": None, "reason": f"The lines of this setup need different assemblies ({names}); one setup is one tooling set"}
    return {"status": "MATCHED", "definition_id": defs.pop(), "reason": None}


# ------------------------------------------------------------------ creating setups
def _fields(raw: dict) -> dict:
    wo = clean(raw.get("wo_id"))
    if not wo:
        raise ValidationError("WO NO is required")
    return {
        "wo_id": str(wo).strip(), "part_id": _norm_part(raw.get("part_id")),
        "tool_description": clean(raw.get("tool_description")), "family": clean(raw.get("family")),
        "grade": clean(raw.get("grade")), "internal_id": clean(raw.get("internal_id")),
        "fg_qty": _int(raw.get("fg_qty"), "FG QTY"), "rcd_qty": _int(raw.get("rcd_qty"), "RCD QTY"),
        "weight": _float(raw.get("weight"), "WEIGHT"),
    }


def _existing_line(db: Session, f: dict) -> SetupLine | None:
    return db.query(SetupLine).filter_by(wo_id=f["wo_id"], part_id=f["part_id"]).first()


def create_setup(db: Session, actor: User, *, name=None, lines=None, plan_ref=None, **single) -> Setup:
    """A setup with its WO / part lines. For one line you may pass wo_id, part_id... directly."""
    permissions.require(actor, "setup.plan")
    raw_lines = list(lines) if lines else [single]
    fl = [_fields(r) for r in raw_lines]
    seen = set()
    for f in fl:
        ex = _existing_line(db, f)
        if ex:
            raise ValidationError(f"{f['wo_id']} / {f['part_id']} is already planned in {setup_code(ex.setup_id)}")
        if (f["wo_id"], f["part_id"]) in seen:
            raise ValidationError(f"{f['wo_id']} / {f['part_id']} appears twice")
        seen.add((f["wo_id"], f["part_id"]))
    with transaction(db):
        s = Setup(name=clean(name), name_key=key(name), plan_ref=clean(plan_ref), source="MANUAL", created_by=actor.id)
        db.add(s)
        db.flush()
        for f in fl:
            db.add(SetupLine(setup_id=s.id, **f))
        audit.record(db, actor, "SETUP_CREATE", "setup", s.id, after={"name": s.name, "lines": [f"{f['wo_id']}/{f['part_id']}" for f in fl]})
    return s


def add_line(db: Session, actor: User, setup_id: int, **raw) -> SetupLine:
    permissions.require(actor, "setup.plan")
    s = _get(db, setup_id)
    if s.cancelled:
        raise ValidationError(f"{setup_code(s.id)} was cancelled")
    if _open_issue(db, s.id):
        raise ValidationError(f"{setup_code(s.id)} has tooling out on an open issue; add lines after it is returned")
    f = _fields(raw)
    ex = _existing_line(db, f)
    if ex:
        raise ValidationError(f"{f['wo_id']} / {f['part_id']} is already planned in {setup_code(ex.setup_id)}")
    with transaction(db):
        ln = SetupLine(setup_id=s.id, **f)
        db.add(ln)
        db.flush()
        audit.record(db, actor, "SETUP_ADD_LINE", "setup", s.id, after={"wo_id": f["wo_id"], "part_id": f["part_id"]})
    return ln


def cancel_setup(db: Session, actor: User, setup_id: int, *, reason) -> Setup:
    permissions.require(actor, "setup.plan")
    reason = require_reason(reason, "cancelling a setup")
    s = _get(db, setup_id)
    if _open_issue(db, s.id):
        raise ValidationError(f"{setup_code(s.id)} has tooling out on an open issue; return it before cancelling")
    with transaction(db):
        s.cancelled, s.cancel_reason = True, reason
        audit.record(db, actor, "SETUP_CANCEL", "setup", s.id, reason=reason)
    return s


# ------------------------------------------------------------------ importing Planning's sheet
def _find_header(raw: pd.DataFrame):
    for i in range(min(len(raw), 20)):
        cells = [key(c) or "" for c in raw.iloc[i].tolist()]
        if any(c in COLUMN_ALIASES["wo"] for c in cells) and any(c in COLUMN_ALIASES["part"] for c in cells):
            return i, cells
    raise ValidationError("Could not find the header row: the sheet needs at least the columns WO NO and PART ID")


def read_sheet(data: bytes) -> tuple[list[dict], str | None]:
    try:
        xl = pd.ExcelFile(BytesIO(data))
        sheet = xl.sheet_names[0]
        raw = pd.read_excel(xl, sheet_name=sheet, header=None, dtype=str, keep_default_na=False)
    except Exception as e:
        raise ValidationError(f"Could not read file as .xlsx: {e}") from e
    h, cells = _find_header(raw)
    cols = {}
    for field, aliases in COLUMN_ALIASES.items():
        for idx, c in enumerate(cells):
            if c in aliases and field not in cols:
                cols[field] = idx
    rows = []
    for i in range(h + 1, len(raw)):
        r = raw.iloc[i].tolist()
        rec = {f: (str(r[idx]).strip() or None) for f, idx in cols.items()}
        for f, v in rec.items():
            if v and v.startswith("#"):  # an Excel error cell (#REF!) is no data
                rec[f] = None
        if not any(rec.values()):
            continue
        rows.append({"row": i + 1, **rec})
    return rows, sheet


def import_sheet(db: Session, actor: User, data: bytes, *, commit: bool, source_file=None) -> dict:
    """Preview (commit=False) or commit. Rows with the same SETUP value form one setup (a row with no SETUP is a setup of its
    own). A row with an error blocks the whole commit; a WO / part already planned is reported as DUPLICATE, left alone and
    skipped; new rows naming a setup that already exists are added to it."""
    permissions.require(actor, "setup.plan")
    rows, sheet = read_sheet(data)
    if not rows:
        raise ValidationError("The sheet has no data rows")
    seen, out, to_make = set(), [], []
    for r in rows:
        rec = {"row": r["row"], "status": "OK", "message": None, "setup": r.get("setup")}
        try:
            f = _fields({"wo_id": r.get("wo"), "part_id": r.get("part"), "tool_description": r.get("description"),
                         "family": r.get("family"), "grade": r.get("grade"), "internal_id": r.get("internal_id"),
                         "fg_qty": r.get("fg_qty"), "rcd_qty": r.get("rcd_qty"), "weight": r.get("weight")})
            rec["data"] = {k: v for k, v in f.items() if v is not None}
            k2 = (f["wo_id"], f["part_id"])
            ex = _existing_line(db, f)
            if ex or k2 in seen:
                rec["status"], rec["message"] = "DUPLICATE", (f"already planned in {setup_code(ex.setup_id)}" if ex else "appears twice in this file")
            else:
                existing = db.query(Setup).filter(Setup.name_key == key(r.get("setup"))).first() if key(r.get("setup")) else None
                if existing is not None and (existing.cancelled or _open_issue(db, existing.id)):
                    raise ValidationError(f"setup {r.get('setup')} ({setup_code(existing.id)}) is "
                                          + ("cancelled" if existing.cancelled else "out on an issue; add lines after it is returned"))
                seen.add(k2)
                to_make.append((r.get("setup"), f, existing))
        except ValidationError as e:
            rec["status"], rec["message"] = "ERROR", e.message
        out.append(rec)
    counts = {s: sum(1 for r in out if r["status"] == s) for s in ("OK", "ERROR", "DUPLICATE")}
    groups = {(key(n) or f"row{i}") for i, (n, _, _) in enumerate(to_make)}
    res = {"valid": counts["ERROR"] == 0, "committed": False, "sheet": sheet, "counts": counts, "rows": out, "setups": len(groups),
           "errors": [{"row": r["row"], "message": r["message"]} for r in out if r["status"] == "ERROR"], "created": 0}
    if commit and res["valid"]:
        made = {}
        with transaction(db):
            for n, f, existing in to_make:
                nk = key(n)
                if nk and nk in made:
                    s = made[nk]
                elif existing is not None:
                    s = existing
                    made[nk] = s
                else:
                    s = Setup(name=clean(n), name_key=nk, plan_ref=sheet, source="EXCEL", source_file=source_file, created_by=actor.id)
                    db.add(s)
                    db.flush()
                    if nk:
                        made[nk] = s
                db.add(SetupLine(setup_id=s.id, **f))
            audit.record(db, actor, "SETUP_IMPORT", "setup", None,
                         after={"file": source_file, "sheet": sheet, "lines": len(to_make), "duplicates": counts["DUPLICATE"]})
        res.update(committed=True, created=len(to_make))
    return res


def template_xlsx() -> bytes:
    notes = ["One row per WO / part. Rows with the same SETUP belong to one setup (one tooling set); leave SETUP blank for a setup of its own.",
             "Required: WO NO, PART ID. The header can be on any of the first 20 rows; other columns are ignored.",
             "TOOL DESCRIPTION, FAMILY and INTERNAL GRADE help the system find the right tooling.",
             "A WO NO + PART ID that is already planned is skipped, never overwritten."]
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        pd.DataFrame(columns=TEMPLATE_COLUMNS).to_excel(w, sheet_name="setups", index=False)
        pd.DataFrame({"Notes": notes}).to_excel(w, sheet_name="instructions", index=False)
    return buf.getvalue()


# ------------------------------------------------------------------ views
def _issues_of(db: Session, setup_id: int) -> list[Issue]:
    return db.query(Issue).filter(Issue.setup_id == setup_id).order_by(Issue.id).all()


def _open_issue(db: Session, setup_id: int) -> Issue | None:
    from . import issues  # local: issues imports this module
    out = set(issues.outstanding_issue_ids(db))
    return next((i for i in _issues_of(db, setup_id) if i.id in out), None)


def tooling_for(db: Session, definition_id: int) -> dict:
    """The physical tooling that can serve this assembly: its builds and the loose serviceable items per line, each with
    its die/pattern number and bin."""
    locs = {l["id"]: l["path"] for l in config.list_locations(db)}
    d = assemblies.get_definition(db, definition_id)
    rev = assemblies.current_approved(db, definition_id)
    cat = {c.id: c.code for c in db.query(Category)}
    out_builds = []
    for b in db.query(Build).filter(Build.definition_id == definition_id, Build.status != "DISMANTLED").order_by(Build.id):
        comps = []
        for c in builds.members(db, b.id):
            it = db.get(Item, c.item_id)
            st = ledger.get_state(db, it.id)
            des = db.get(ElementDesign, it.design_id)
            comps.append({"item_id": it.id, "display_id": it.display_id, "design_code": des.code, "category": cat.get(des.category_id),
                          "location": locs.get(st["location_id"]), "custody": st["custody"]})
        out = any(c["custody"] in ("PENDING_HANDOVER", "PRODUCTION") for c in comps)
        open_issue = None
        if out:
            from . import issues
            o = issues.open_issue_for_item(db, comps[0]["item_id"])
            if o:
                open_issue = {"id": o[0].id, "code": issues.issue_code(o[0].id), "wo_id": issues.wo_text(db, o[0]), "state": o[1],
                              "setup": setup_code(o[0].setup_id) if o[0].setup_id else None,
                              "can_hand_over": all(c["custody"] == "PRODUCTION" for c in comps)}
        out_builds.append({"id": b.id, "code": builds.build_code(b.id), "status": "ISSUED" if out else b.status,
                           "components": comps, "open_issue": open_issue})
    lines = []
    if rev is not None:
        rows = db.execute(text("SELECT i.id, i.display_id, i.design_id, i.design_revision_id, s.custody, s.location_id FROM items i "
                               "JOIN item_state s ON s.item_id = i.id WHERE i.active = 1 AND s.item_condition = 'SERVICEABLE' "
                               "AND s.custody IN ('STORES', 'ASSEMBLY_AREA') ORDER BY i.display_id")).mappings().all()
        for ln in assemblies.matching_lines(db, rev.id):
            found = []
            for r in rows:
                acc = ln.accepts.get(r["design_id"])
                if acc is not None and r["design_revision_id"] in acc and len(found) < 25:
                    found.append({"item_id": r["id"], "display_id": r["display_id"], "location": locs.get(r["location_id"]),
                                  "custody": r["custody"], "design_code": db.get(ElementDesign, r["design_id"]).code})
            designs = sorted({db.get(ElementDesign, i).code for i in ln.accepts})
            first = db.get(ElementDesign, next(iter(ln.accepts)))
            lines.append({"line_id": ln.line_id, "label": ln.label, "designs": designs, "category": cat.get(first.category_id),
                          "required": ln.quantity, "candidates": found})
    return {"assembly": {"id": d.id, "code": d.code, "description": d.description}, "builds": out_builds, "lines": lines}


def _state(s: Setup, open_issue, issues_) -> str:
    if s.cancelled:
        return "CANCELLED"
    if open_issue:
        return "ISSUED"
    return "DONE" if issues_ else "NOT_ISSUED"


def line_dict(db: Session, ln: SetupLine, maps=None) -> dict:
    m = match_line(db, ln, maps)
    code = db.get(AssemblyDefinition, m["definition_id"]).code if m.get("definition_id") else None
    return {"id": ln.id, "wo_id": ln.wo_id, "part_id": ln.part_id, "tool_description": ln.tool_description, "family": ln.family,
            "grade": ln.grade, "internal_id": ln.internal_id, "fg_qty": ln.fg_qty, "rcd_qty": ln.rcd_qty, "weight": ln.weight,
            "match": m["status"], "match_reason": m["reason"], "assembly_code": code}


def setup_dict(db: Session, s: Setup, *, detail=False, maps=None) -> dict:
    from . import issues
    maps = maps if maps is not None else _active_maps(db)
    lines = lines_of(db, s.id)
    m = match_setup(db, s, maps, lines)
    code = db.get(AssemblyDefinition, m["definition_id"]).code if m.get("definition_id") and m["status"] == "MATCHED" else None
    iss = _issues_of(db, s.id)
    out = set(issues.outstanding_issue_ids(db)) if iss else set()
    open_issue = next((i for i in iss if i.id in out), None)
    users = {u.id: u.username for u in db.query(User.id, User.username)}
    first = lines[0] if lines else None
    wos = list(dict.fromkeys(ln.wo_id for ln in lines))
    parts = list(dict.fromkeys(ln.part_id for ln in lines))
    d = {"id": s.id, "code": setup_code(s.id), "name": s.name, "plan_ref": s.plan_ref, "source": s.source,
         "created_by": users.get(s.created_by), "created_at": s.created_at, "cancelled": s.cancelled, "cancel_reason": s.cancel_reason,
         # summary of the lines, so a list can show "how many WO and parts" at a glance
         "wo_count": len(wos), "part_count": len(parts), "wo_ids": wos, "part_ids": parts, "line_count": len(lines),
         "wo_id": ", ".join(wos), "part_id": ", ".join(parts),
         "tool_description": first.tool_description if first else None, "family": first.family if first else None,
         "grade": first.grade if first else None, "internal_id": first.internal_id if first else None,
         "fg_qty": sum(ln.fg_qty or 0 for ln in lines) if lines else None, "rcd_qty": sum(ln.rcd_qty or 0 for ln in lines) if lines else None,
         "match": m["status"], "match_reason": m["reason"], "definition_id": m.get("definition_id"), "assembly_code": code,
         "state": _state(s, open_issue, iss), "lines": [line_dict(db, ln, maps) for ln in lines],
         "issues": [{"id": i.id, "code": issues.issue_code(i.id), "open": i.id in out} for i in iss]}
    if detail:
        d["tooling"] = tooling_for(db, m["definition_id"]) if m.get("definition_id") and m["status"] == "MATCHED" else None
    return d


def list_setups(db: Session, *, q=None, match=None, state=None, wo_id=None, limit=100, offset=0) -> tuple[int, list[dict]]:
    qq = db.query(Setup)
    if wo_id:
        qq = qq.filter(Setup.id.in_([l.setup_id for l in db.query(SetupLine).filter(SetupLine.wo_id == wo_id.strip())] or [0]))
    if q and q.strip():
        like = f"%{q.strip()}%"
        hit = [l.setup_id for l in db.query(SetupLine).filter(
            SetupLine.wo_id.ilike(like) | SetupLine.part_id.ilike(like) | SetupLine.tool_description.ilike(like)
            | SetupLine.family.ilike(like) | SetupLine.internal_id.ilike(like))]
        qq = qq.filter(Setup.id.in_(hit or [0]) | Setup.name.ilike(like))
    maps = _active_maps(db)
    rows = [setup_dict(db, s, maps=maps) for s in qq.order_by(Setup.id.desc())]
    if match:
        rows = [r for r in rows if r["match"] == match]
    if state:
        rows = [r for r in rows if r["state"] == state]
    return len(rows), rows[offset: offset + min(limit, 500)]


def counts(db: Session) -> dict:
    maps = _active_maps(db)
    live = [s for s in db.query(Setup).filter(Setup.cancelled.is_(False))]
    unmatched = sum(1 for s in live if match_setup(db, s, maps)["status"] != "MATCHED")
    return {"setups_planned": len(live), "setups_unmatched": unmatched}


def for_build(db: Session, build_id: int) -> list[dict]:
    """Setups a build can be issued for: matched to its assembly, not cancelled, nothing already out on them."""
    b = builds._get(db, build_id)
    maps = _active_maps(db)
    out = []
    for s in db.query(Setup).filter(Setup.cancelled.is_(False)).order_by(Setup.id):
        lines = lines_of(db, s.id)
        m = match_setup(db, s, maps, lines)
        if m["status"] == "MATCHED" and m["definition_id"] == b.definition_id and not _open_issue(db, s.id):
            d = setup_dict(db, s, maps=maps)
            out.append({k: d[k] for k in ("id", "code", "name", "wo_id", "part_id", "wo_count", "part_count", "tool_description", "family", "grade", "lines")})
    return out


def require_issuable(db: Session, setup_id, build, wo_id=None, line_ids=None) -> tuple[Setup, list[SetupLine]]:
    """Used by issue creation: the issue must name a setup that this build can serve, and which of its lines it covers
    (all of them unless a subset is given)."""
    if not setup_id:
        raise ValidationError("Choose the setup this issue is for")
    s = db.get(Setup, int(setup_id))
    if s is None:
        raise ValidationError("Choose the setup this issue is for (that setup does not exist)")
    if s.cancelled:
        raise ValidationError(f"{setup_code(s.id)} was cancelled by Planning: {s.cancel_reason}")
    all_lines = lines_of(db, s.id)
    if line_ids:
        ids = [int(i) for i in line_ids]
        lines = [ln for ln in all_lines if ln.id in ids]
        if len(set(ids)) != len(ids) or len(lines) != len(ids):
            raise ValidationError(f"A line chosen does not belong to {setup_code(s.id)}")
    else:
        lines = all_lines
    if wo_id:
        lines = [ln for ln in lines if ln.wo_id == str(wo_id).strip()]
        if not lines:
            raise ValidationError(f"{setup_code(s.id)} has no line for WO {str(wo_id).strip()}")
    if not lines:
        raise ValidationError(f"Choose at least one WO / part line of {setup_code(s.id)}")
    m = match_setup(db, s)
    if m["status"] != "MATCHED":
        raise ValidationError(f"{setup_code(s.id)} has no tooling assigned yet ({m['reason']}); Engineering must add a tool mapping")
    if m["definition_id"] != build.definition_id:
        want = db.get(AssemblyDefinition, m["definition_id"]).code
        got = db.get(AssemblyDefinition, build.definition_id).code
        label = s.name or (all_lines[0].tool_description if all_lines else None) or setup_code(s.id)
        raise ValidationError(f"{setup_code(s.id)} ({label}) needs assembly {want}, but build {builds.build_code(build.id)} is {got}")
    o = _open_issue(db, s.id)
    if o:
        from . import issues
        raise ValidationError(f"{setup_code(s.id)} is already issued on {issues.issue_code(o.id)}; return it first")
    return s, lines
