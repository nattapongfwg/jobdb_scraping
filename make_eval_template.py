"""One-time (re-runnable) processor: derive Evaluation_Template.xlsx from the HR
master Evaluate_Original.xlsx, WITHOUT openpyxl (which would drop the 8 images).

The form used is the **"Evaluate Interview form (Demo)" sheet** (sheet2) — the sheet
that is visible/active when the master is opened, and the one HR considers current.
(The other "Evaluate Interview form" sheet is an older, hidden version we leave alone.)

Changes applied to the Demo sheet:
  1. clear the 9 header value cells (remove the example candidate's name/position/etc.)
     so a generated form starts blank; build_eval_xlsx then fills them per candidate.
  2. hide column Q and everything after it (Q -> end).
The body needs no clearing — scores are already blank and the only example data was the
header. The workbook already opens on this sheet, so no workbook/visibility edits.

Evaluate_Original.xlsx is only READ; rerun this whenever the master form changes.
"""
import re
import sys
import zipfile
from pathlib import Path

import evaluation   # for the 9 value-cell coordinates (CELL_MAP)

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "Evaluate_Original.xlsx"
DST = ROOT / "Evaluation_Template.xlsx"
FORM_SHEET = "xl/worksheets/sheet2.xml"            # "Evaluate Interview form (Demo)"
VALUE_CELLS = list(evaluation.CELL_MAP.values())   # B2,I2,N2,B3,I3,N3,B4,I4,N4


def clear_cell(sheet_xml: str, cell: str) -> str:
    """Empty one cell's value while preserving its style (`s="…"`)."""
    pat = re.compile(rf'<c r="{cell}"((?:\s+[\w:]+="[^"]*")*)\s*(?:/>|>.*?</c>)', re.S)
    def repl(m):
        sm = re.search(r'\s+s="\d+"', m.group(1))
        return f'<c r="{cell}"{sm.group(0) if sm else ""}/>'
    return pat.sub(repl, sheet_xml, count=1)


def hide_cols_from_Q(sheet_xml: str) -> str:
    """Keep column widths up to P (col 16); hide column Q (17) and everything after."""
    m = re.search(r"<cols>(.*?)</cols>", sheet_xml, re.S)
    if not m:
        return sheet_xml
    cols = re.findall(r"<col [^>]*/>", m.group(1))
    kept = [c for c in cols if int(re.search(r'min="(\d+)"', c).group(1)) <= 16]
    kept.append('<col min="17" max="16384" width="9.140625" hidden="1" customWidth="1"/>')
    return sheet_xml[:m.start()] + "<cols>" + "".join(kept) + "</cols>" + sheet_xml[m.end():]


def main() -> int:
    zin = zipfile.ZipFile(SRC)
    parts = {i.filename: zin.read(i.filename) for i in zin.infolist()}
    infos = list(zin.infolist())

    s2 = parts[FORM_SHEET].decode("utf-8")
    for cell in VALUE_CELLS:
        s2 = clear_cell(s2, cell)
    s2 = hide_cols_from_Q(s2)
    parts[FORM_SHEET] = s2.encode("utf-8")

    with zipfile.ZipFile(DST, "w", zipfile.ZIP_DEFLATED) as zout:
        for it in infos:
            zout.writestr(it, parts[it.filename])
    print(f"Wrote {DST.name} ({DST.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
