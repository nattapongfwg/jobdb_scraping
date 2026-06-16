"""Interview-evaluation form: fills the Excel template + builds the draft email.

The Excel template (Interview_Evaluation_Template.xlsx) is a rich workbook with
embedded images, drawings, comments and data-validation dropdowns. openpyxl would
drop the images/drawings on save, so instead we surgically rewrite ONLY the header
cells inside xl/worksheets/sheet1.xml and copy every other zip entry byte-for-byte —
preserving the whole form intact.

Header value cells (the merged cell to the right of each Thai label, row 2-4):
    B2  ชื่อ-นามสกุล              (prefix + full_name_edit)
    I2  ตำแหน่งที่สมัคร (Position)  (dropdown)
    N2  บทบาท (Role)              (job_title, editable)
    B3  บริษัท (Company)          (dropdown)
    I3  ฝ่าย (Department)         (dropdown)
    N3  แผนก (Section)            (dropdown)
    B4  วันที่สัมภาษณ์             (date)
    I4  ผู้สัมภาษณ์ (Interviewer)  (text)
    N4  ผู้สรรหา (Recruiter)       (text)
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from signature import signature_html

PROJECT_ROOT = Path(__file__).resolve().parent
EVAL_DIR = PROJECT_ROOT / "Evaluation_Files"          # generated forms land here (safe to clean)
# The template build fills is `Evaluation_Template.xlsx` — derived from the HR master
# `Evaluate_Original.xlsx` by make_eval_template.py (Demo sheet hidden, main sheet visible,
# columns after T hidden). Both live in the PROJECT ROOT (not EVAL_DIR, which gets cleaned).
# build_eval_xlsx only READS the template. To update the form: replace Evaluate_Original.xlsx
# then rerun `python make_eval_template.py`. Keep the 9 header cells (see CELL_MAP) in place.
MASTER_PATH = PROJECT_ROOT / "Evaluate_Original.xlsx"      # untouched HR reference
TEMPLATE_PATH = PROJECT_ROOT / "Evaluation_Template.xlsx"  # processed; what build fills
SHEET_XML = "xl/worksheets/sheet2.xml"   # "Evaluate Interview form (Demo)" — the form HR uses

# Header label -> the value cell that holds it.
CELL_MAP = {
    "full_name":     "B2",
    "position":      "I2",
    "role":          "N2",
    "company":       "B3",
    "department":    "I3",
    "section":       "N3",
    "interview_date": "B4",
    "interviewer":   "I4",
    "recruiter":     "N4",
}

# Dropdown option lists (order preserved as the HR team provided them).
# Positions carry a job level. Stored as (job_level, position_title) in HR's
# canonical order (ascending by level). POSITIONS below is the flat title list
# the evaluation dropdown consumes; POSITION_LEVEL maps a title back to its level.
POSITION_LEVELS = [
    ("1",       "Support Officer"),
    ("2",       "Officer"),
    ("2",       "System Engineer"),
    ("2",       "Consultant"),
    ("2",       "Software Engineer"),
    ("3",       "Specialist"),
    ("4",       "Senior Specialist"),
    ("4",       "Assistant Manager"),
    ("5",       "Expert"),
    ("5",       "Manager"),
    ("5",       "Managing Consultant"),
    ("6",       "Principal Consultant"),
    ("6",       "Assistant Vice President"),
    ("7",       "Vice President"),
    ("8",       "Director"),
    ("8",       "Executive Director"),
    ("C Level", "Chief Operations Officer"),
    ("C Level", "Chief Executive Officer"),
]
POSITIONS = [title for _level, title in POSITION_LEVELS]
POSITION_LEVEL = {title: level for level, title in POSITION_LEVELS}
COMPANIES = [
    "Freewill FX Co.,Ltd", "Freewill Compile Co.,Ltd", "Freewill Solutions Co.,Ltd.",
    "Freewill-Marstohken Co.,Ltd.", "Freewill-Mars Tohken Co.,Ltd",
]
DEPARTMENTS = [
    "BTC", "COMSERV", "FCP", "FCS", "FMT", "FX", "HR", "INS", "MDC", "AiP", "SEC",
    "Sales Team1", "Services", "Implement & Support", "TERMINUS", "CC", "EMT", "QMT",
]
SECTIONS = [
    "ACC", "ADMIN", "BANGKOK", "BCG", "QA", "BTC", "BTSI", "Implement#4", "CC",
    "DEVELOP", "ESC", "Implement#1", "ETC", "CoC", "PoP", "FCP", "CIA", "FCS", "HR",
    "ICT", "IMPLEMENT", "INS", "Services", "สติ", "Implementer#2", "ISB", "ITT",
    "PRESALE", "PSI", "QMT", "RMX", "PMX", "SALES", "SAP", "SDP", "Sales Team1", "FMT",
    "Implement & Support", "Cloud", "TERMINUS", "ICE", "NXT", "LOL", "SA#1",
    "Tech Pioneers", "Implement #2", "Implement #3", "Sec Office", "FBI", "I2P", "AiP",
    "R&D", "EMT", "Implement #4", "Insurance (Test&Support)",
]


# Fixed SharePoint link to the user manual PDF (linked in the draft, not attached).
MANUAL_URL = (
    "https://freewillsolutions-my.sharepoint.com/personal/tanakrit_jai_freewillsolutions_com/"
    "_layouts/15/onedrive.aspx?id=%2Fpersonal%2Ftanakrit%5Fjai%5Ffreewillsolutions%5Fcom%2F"
    "Documents%2FFWG%2FRecruitment%2FRecruitment%5FFile%2FInterview%20Evaluation%20Form%2F"
    "%E0%B8%84%E0%B8%B9%E0%B9%88%E0%B8%A1%E0%B8%B7%E0%B8%AD%E0%B8%81%E0%B8%B2%E0%B8%A3%E0%B9%83"
    "%E0%B8%8A%E0%B9%89%E0%B8%87%E0%B8%B2%E0%B8%99%E0%B9%81%E0%B8%9A%E0%B8%9A%E0%B8%9B%E0%B8%A3"
    "%E0%B8%B0%E0%B9%80%E0%B8%A1%E0%B8%B4%E0%B8%99%E0%B8%AA%E0%B8%B1%E0%B8%A1%E0%B8%A0%E0%B8%B2"
    "%E0%B8%A9%E0%B8%93%E0%B9%8C%E0%B8%87%E0%B8%B2%E0%B8%99%20%28User%20Manual%20for%20Interview"
    "%20Evaluation%20form%29%2Epdf&parent=%2Fpersonal%2Ftanakrit%5Fjai%5Ffreewillsolutions%5Fcom"
    "%2FDocuments%2FFWG%2FRecruitment%2FRecruitment%5FFile%2FInterview%20Evaluation%20Form"
)
MANUAL_LINK_TEXT = "คู่มือการใช้งานแบบประเมินสัมภาษณ์งาน (User Manual for Interview Evaluation form).pdf"


def _html_escape(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_eval_email(*, prefix_name: str, interviewer: str, position: str,
                     role: str) -> tuple[str, str]:
    """Build the (subject, HTML body) for the evaluation draft email — the format
    HR uses to ask the interviewer to fill the attached evaluation form.
    `interviewer` is greeted by their first word ("Upakit" from "Upakit ...")."""
    first = (interviewer or "").strip().split(" ")[0] if (interviewer or "").strip() else "team"
    p_name, pos, rl = _html_escape(prefix_name), _html_escape(position), _html_escape(role)
    subject = f"Interview Evaluation Form for {prefix_name} ({position})"
    body = f"""<div style="font-family:'Aptos','Segoe UI',Arial,sans-serif;font-size:11pt;color:#000000;line-height:1.5">
<p style="margin:0 0 14px"><b>Dear Khun {_html_escape(first)},</b></p>
<p style="margin:0 0 14px">Evaluation form for {p_name} as passed the interview as {pos} position in {rl} role. (Following by attach file)</p>
<p style="margin:0 0 14px">Return the evaluate form to HR <span style="background-color:#ffff00"><b>within as soon as possible.</b></span></p>
<p style="margin:0 0 14px"><i style="color:#c00000">"Apologies for the urgency, but if it's inconvenient in any way, please feel free to let us know."</i></p>
<p style="margin:0 0 14px"><b>Note:</b> If you have any questions, feel free to ask me anytime. I'm always available for you Krab.</p>
<p style="margin:0 0 2px">User Manual for Interview Evaluation form):</p>
<p style="margin:0 0 18px">📄 <a href="{_html_escape(MANUAL_URL)}" style="color:#1155cc;text-decoration:underline">{_html_escape(MANUAL_LINK_TEXT)}</a></p>
{signature_html()}
</div>"""
    return subject, body


def _xml_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _set_cell(sheet_xml: str, cell: str, value: str) -> str:
    """Set one cell's value as an inline string, preserving its style (`s="…"`).
    Handles both an empty self-closing cell and one that already has content."""
    esc = _xml_escape(value)
    inline = ('<c r="{cell}"{attrs} t="inlineStr"><is>'
              '<t xml:space="preserve">{v}</t></is></c>')

    def _attrs_keep_style(raw: str) -> str:
        return re.sub(r'\s+t="[^"]*"', "", raw)   # drop any prior type, keep s=…

    # empty self-closing: <c r="B2" s="361"/>
    pat_empty = re.compile(rf'<c r="{cell}"((?:\s+[\w:]+="[^"]*")*)\s*/>')
    new, n = pat_empty.subn(
        lambda m: inline.format(cell=cell, attrs=_attrs_keep_style(m.group(1)), v=esc),
        sheet_xml)
    if n == 1:
        return new
    # has content: <c r="B2" ...>…</c>
    pat_full = re.compile(rf'<c r="{cell}"((?:\s+[\w:]+="[^"]*")*)\s*>.*?</c>', re.S)
    new, n = pat_full.subn(
        lambda m: inline.format(cell=cell, attrs=_attrs_keep_style(m.group(1)), v=esc),
        sheet_xml)
    if n != 1:
        raise ValueError(f"Could not set cell {cell} (matched {n} times).")
    return new


def build_eval_xlsx(values: dict, out_path: Path, template_path: Path = TEMPLATE_PATH) -> Path:
    """Write a filled copy of the evaluation form. `values` keys match CELL_MAP.
    Only the 9 header cells on the form sheet are rewritten; every other part (images,
    drawings, validations, other sheets, the already-applied layout from
    make_eval_template.py) is copied verbatim. Returns out_path."""
    out_path = Path(out_path)
    with zipfile.ZipFile(template_path, "r") as zin:
        sheet = zin.read(SHEET_XML).decode("utf-8")
        for key, cell in CELL_MAP.items():
            v = values.get(key, "")
            if v not in (None, ""):
                sheet = _set_cell(sheet, cell, str(v))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = sheet.encode("utf-8") if item.filename == SHEET_XML else zin.read(item.filename)
                zout.writestr(item, data)
    return out_path


def _safe_filename(s: str) -> str:
    """Strip characters Windows forbids in filenames (may return '')."""
    return re.sub(r'[\\/:*?"<>|]', "", str(s or "")).strip()


def eval_filename(section: str, full_name_edit: str) -> str:
    """`<Section>_Interview Evaluate Form for <full_name_edit>.xlsx`. Empty section
    falls back to 'NA' (matches the popup's live filename hint)."""
    sec = _safe_filename(section) or "NA"
    name = _safe_filename(full_name_edit) or "Candidate"
    return f"{sec}_Interview Evaluate Form for {name}.xlsx"
