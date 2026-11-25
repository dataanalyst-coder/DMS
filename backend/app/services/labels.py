"""Printable QR labels as SVG (no image library needed: the QR is drawn from its module matrix).

The QR payload is always the stable identifier, so a reprint never creates a new identity:
  item     -> its system ID, e.g. TI-000123
  build    -> BLD-000012
  location -> LOC:P1/A1/R1/S1/B1"""
from xml.sax.saxutils import escape

import qrcode
from qrcode.constants import ERROR_CORRECT_M

from ..models import DesignRevision, ElementDesign, Item, Location
from . import config


def qr_modules(data: str) -> list[list[bool]]:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


def _qr_path(data: str, x: float, y: float, size: float) -> str:
    m = qr_modules(data)
    n = len(m)
    cell = size / n
    parts = []
    for r, row in enumerate(m):
        c = 0
        while c < n:
            if row[c]:
                start = c
                while c < n and row[c]:
                    c += 1
                parts.append(f"M{x + start * cell:.3f} {y + r * cell:.3f}h{(c - start) * cell:.3f}v{cell:.3f}h-{(c - start) * cell:.3f}z")
            else:
                c += 1
    return f'<path d="{"".join(parts)}" fill="#000"/>'


def _label(payload: str, lines: list[tuple[str, int, bool]], caption: str) -> str:
    """60 x 32 mm label: QR on the left, text on the right."""
    W, H = 60, 32
    text = []
    y = 7
    for t, size, bold in lines:
        text.append(f'<text x="{27}" y="{y}" font-family="Arial, sans-serif" font-size="{size / 10:.1f}" '
                    f'font-weight="{"700" if bold else "400"}">{escape(t[:26])}</text>')
        y += size / 10 + 2.2
    text.append(f'<text x="2" y="{H - 1.5}" font-family="Consolas, monospace" font-size="2.4">{escape(caption)}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}mm" height="{H}mm" viewBox="0 0 {W} {H}">'
            f'<rect x="0.3" y="0.3" width="{W - 0.6}" height="{H - 0.6}" fill="#fff" stroke="#000" stroke-width="0.3"/>'
            f'{_qr_path(payload, 2, 2, 23)}{"".join(text)}</svg>')


def item_label_svg(db, item: Item) -> str:
    design = db.get(ElementDesign, item.design_id)
    rev = db.get(DesignRevision, item.design_revision_id)
    big = item.legacy_no or item.display_id
    return _label(item.system_id, [(big, 62, True), (item.system_id, 36, False),
                                   (f"{design.code} rev {rev.revision}", 30, False), (design.description, 26, False)],
                  f"{item.system_id}  {item.display_id}")


def build_label_svg(code: str, assembly_code: str, description: str, revision: str) -> str:
    return _label(code, [(code, 56, True), (f"{assembly_code} rev {revision}", 36, False), (description, 26, False)],
                  f"{code}  assembly build")


def batch_label_svg(code: str, design_code: str, description: str, uom: str, supplier_lot, expiry) -> str:
    lines = [(code, 56, True), (design_code, 38, False), (description, 26, False)]
    if supplier_lot:
        lines.append((f"Lot {supplier_lot}", 26, False))
    if expiry:
        lines.append((f"Exp {expiry}", 26, False))
    return _label(code, lines, f"{code}  consumable batch ({uom})")


def location_label_svg(db, location: Location) -> str:
    path = config.location_path(db, location.id)
    return _label(f"LOC:{path}", [(location.code, 62, True), (location.level, 34, False), (path, 26, False),
                                  (location.name or "", 26, False)], f"LOC:{path}")
