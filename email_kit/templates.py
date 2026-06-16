"""Editable exam/interview email templates, persisted as JSON so HR can manage
several of them in the web UI (Config Email Template). All templates send from the
same signed-in mailbox (delegated /me/sendMail), so there is no per-template sender.

File shape:  {"templates": [ {id, name, type, company, attachments,
                              default_deadline_time, is_html, custom_vars,
                              subject, body}, ... ]}

Placeholders in `subject` and `body` (filled per-candidate at send time):
    {<column>}        ANY column of the applicants table (full_name_jobdb, email,
                      phone, expect_salary, nickname, …) — auto-available, so a newly
                      added DB column works with no code change
    {full_name_edit}  the candidate's (editable) name
    {title}           the candidate's honorific prefix (Mr./Ms./Mrs.), "" if unset
    {title_name}      title + name with smart spacing ("Mr. Jane Doe", or just the
                      name when no title) — best for plain-text subjects
    {firstname}       the first word of the candidate's name
    {job_title}       the job posting title
    {company}         the template's `company` field
    {deadline}        the deadline HR picks when moving to "Sent Exam"
    {<custom_var>}    any HR-defined custom variable on the template (custom_vars)
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .signature import RECRUITER_FIRSTNAME, signature_html, signature_text

TEMPLATE_PATH = Path(__file__).resolve().parent / "email_template.json"

DEFAULT_BODY = """Dear {title_name},

{company} would like to confirm the test before interview as below;

Company: {company}
Location: Lumpini Tower 29th Floor, Bangkok 10120
Website: www.freewillsolutions.com

Position: {job_title}

To-do list before interview

1. Complete MBTI personalities test >>> https://www.16personalities.com/th

2. Transcript (PDF)

3. Complete "Implementer test" >>> https://forms.office.com/r/cpgBQL8FH9

4. Logic thinking design >>> Attach file (INS-Imp last section 2)

5. Send back your result of MBTI personalities, Transcript and Ins/Imp file (save as PDF) before the interview time

Deadline: {deadline}

""" + signature_text()

# Per-template fields and their defaults (id + name handled separately).
DEFAULT_FIELDS: dict = {
    "type": "exam",                         # "exam" (to candidate) | "shortlist" (group, to team)
    "company": "Freewill Solutions Co., Ltd.",
    "attachments": [],                      # list of file paths to attach (optional)
    "is_html": False,                       # send body as HTML (else plain text)
    "default_deadline_time": "23:59",       # prefilled time in the deadline picker
    "subject": "Freewill Solutions_Interview: {title_name} ({job_title})",
    "body": DEFAULT_BODY,
}

# Group "shortlist" email (HTML) — one draft listing all selected candidates, each
# with their AI résumé summary as bullet points. Placeholders: {candidates} (the
# numbered name + bulleted-summary blocks, built at send time), {job_title},
# {company}, {link_document} (the shared OneDrive folder, hyperlinked).
DEFAULT_SHORTLIST_BODY = """<div style="font-family:'Aptos','Segoe UI',Arial,sans-serif;font-size:11pt;color:#000000;line-height:1.45">
<p style="margin:0 0 14px"><b>Dear Team,</b></p>
<p style="margin:0 0 20px"><b>Please see candidate for {job_title} at the attached link</b></p>
{candidates}
<p style="margin:14px 0 0"><b>Link document:</b> {link_document}</p>
""" + signature_html(top_margin=26) + """
</div>"""

DEFAULT_SHORTLIST_FIELDS: dict = {
    "type": "shortlist",
    "company": "Freewill Solutions Co., Ltd.",
    "attachments": [],
    "is_html": True,
    "default_deadline_time": "23:59",
    "subject": "Freewill Solutions_Shortlist: {job_title}",
    "body": DEFAULT_SHORTLIST_BODY,
}


def _esc(s: str) -> str:
    """Minimal HTML escape for text dropped into the HTML email body."""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _fill(text: str, fields: dict) -> str:
    """Replace every {placeholder} in `text` with its value from `fields`."""
    for key, val in fields.items():
        text = text.replace("{" + key + "}", val)
    return text


# Interview calendar-event body (HTML). Placeholders: {firstname} (first word of the
# candidate's name), {title} (Mr./Ms./Mrs.), {job_title}. <TEAM> and the Date & Time
# line are left as literal text for HR to fill in Outlook.
DEFAULT_INTERVIEW_BODY = """<div style="font-family:'Aptos','Segoe UI',Arial,sans-serif;font-size:11pt;color:#000000;line-height:1.5">
<p style="margin:0 0 14px">Dear {title} {firstname} &amp; team,</p>
<p style="margin:0 0 2px">Freewill Solutions Co., Ltd. would like to confirm the interview appointment as below.</p>
<p style="margin:0">Company: Freewill Solutions Co., Ltd.<br>
Location: Lumpini Tower 29th Floor, Bangkok 10120<br>
Website: www.freewillsolutions.com</p>
<p style="margin:20px 0 0">Position: {job_title}<br>
Date &amp; Time: DD MM YYYY / HH:MM AM. - HH.MM PM.<br>
Place: Online conference via MS Team</p>
<hr style="margin:18px 0 0;border:none;border-top:1px solid #000">
</div>"""

DEFAULT_INTERVIEW_FIELDS: dict = {
    "type": "interview",
    "company": "Freewill Solutions Co., Ltd.",
    "attachments": [],
    "is_html": True,
    "default_deadline_time": "23:59",
    # <TEAM> is a literal placeholder HR fills; {title_name}/{job_title} auto-fill.
    "subject": "[<TEAM>] Freewill Solutions_Interview: {title_name} ({job_title})",
    "body": DEFAULT_INTERVIEW_BODY,
}


# Job-offer confirmation draft (HTML) — the "ขอยืนยันข้อมูลเสนอจ้างพนักงาน" email built
# when HR moves a candidate to "Offered". Plain placeholders auto-fill from the popup +
# candidate record; the {experience_bullets}/{link}/{interviewer_comments}/
# {recruiter_comments} placeholders are pre-rendered HTML blocks (see offer.build_offer_email).
DEFAULT_OFFER_BODY = """<div style="font-family:'Aptos','Segoe UI',Arial,sans-serif;font-size:11pt;color:#000000;line-height:1.5">
<p style="margin:0 0 14px">Dear Job Offering Team,</p>
<p style="margin:0 0 14px">ขอยืนยันข้อมูลเสนอจ้างพนักงาน {department}: {section} ({role}) จำนวน {people_count} คน ({offer_type})</p>
<p style="margin:0 0 14px">
Name: {prefix_name}<br>
Position: {position}<br>
Job Level : {job_level}<br>
{new_replace_label}:&nbsp; {new_replace_text}<br>
Department: {department}<br>
Section: {section}
</p>
<p style="margin:0 0 14px">
Direct Supervisor: {supervisor}<br>
Buddy: {buddy}
</p>
<p style="margin:0 0 14px">
Expected Salary : {expected_salary}<br>
Current Salary: {current_salary}<br>
Start Date: {start_date}
</p>
<p style="margin:0 0 4px">Experience: {experience}</p>
{experience_bullets}
<p style="margin:14px 0 14px">Link: {link}</p>
<hr style="border:none;border-top:1px solid #999;margin:14px 0">
<p style="margin:0 0 14px">
Interviewer : {interviewer}<br>
ความเห็นผู้สัมภาษณ์ :<br>
{interviewer_comments}
</p>
<p style="margin:0 0 14px">
ความเห็นฝ่าย Recruit : {recruiter_firstname}<br>
{recruiter_comments}
</p>
<hr style="border:none;border-top:1px solid #999;margin:14px 0">
<p style="margin:0 0 14px">ช่องทางการสรรหา (Sourcing): JOB DB</p>
""" + signature_html() + """
</div>"""

DEFAULT_OFFER_FIELDS: dict = {
    "type": "offer",
    "company": "Freewill Solutions Co., Ltd.",
    "attachments": [],
    "is_html": True,
    "default_deadline_time": "23:59",
    "subject": "ขอยืนยันข้อมูลเสนอจ้างพนักงาน {department}: {section} ({role}) as of {today}",
    "body": DEFAULT_OFFER_BODY,
}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _normalize(t: dict) -> dict:
    out = {"id": t.get("id") or _new_id(), "name": (t.get("name") or "Untitled").strip()}
    _t = str(t.get("type", "")).strip()
    out["type"] = _t if _t in ("exam", "shortlist", "interview", "offer") else "exam"
    out["company"] = str(t["company"]) if t.get("company") is not None else DEFAULT_FIELDS["company"]
    out["default_deadline_time"] = str(t.get("default_deadline_time") or "23:59")
    out["subject"] = "" if t.get("subject") is None else str(t.get("subject"))
    out["body"] = "" if t.get("body") is None else str(t.get("body"))
    out["is_html"] = bool(t.get("is_html", False))
    # attachments: a list of paths; migrate the old single 'attachment' string.
    atts = t.get("attachments")
    if atts is None and t.get("attachment"):
        atts = [t.get("attachment")]
    if not isinstance(atts, list):
        atts = []
    out["attachments"] = [str(a).strip() for a in atts if str(a).strip()]
    # custom_vars: HR-defined {name: value} placeholders usable as {name} in the
    # subject/body. Accepts a dict or a [{name, value}, ...] list; empty names dropped.
    out["custom_vars"] = _normalize_custom_vars(t.get("custom_vars"))
    return out


def _normalize_custom_vars(raw) -> dict:
    """Coerce custom variables to an ordered {name: value} dict (names stripped,
    blanks dropped). Accepts a dict or a list of {name, value} pairs."""
    out: dict = {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = [(d.get("name"), d.get("value")) for d in raw if isinstance(d, dict)]
    else:
        return out
    for name, val in items:
        key = str(name or "").strip()
        if key:
            out[key] = "" if val is None else str(val)
    return out


def _field_maps(cand: dict, template: dict, *, deadline: str = "") -> tuple[dict, dict]:
    """Build the placeholder map for one candidate. Returns (base, custom):
      base   — every DB column (stringified) plus derived name fields
               (full_name_edit, title, title_name, firstname), company, deadline.
      custom — the template's HR-defined custom variables.
    Custom vars never shadow a real column (they fill only otherwise-unused names)."""
    cand = cand or {}
    name = (cand.get("full_name_edit") or cand.get("full_name_jobdb") or "")
    title = cand.get("name_title") or ""
    first = name.strip().split(" ")[0] if name.strip() else ""
    base = {k: ("" if v is None else str(v)) for k, v in cand.items()}
    base.update({
        "full_name_edit": name, "title": title,
        "title_name": (f"{title} {name}".strip() if title else name),
        "firstname": first,
        "company": template.get("company", ""), "deadline": deadline or "",
    })
    custom = {k: v for k, v in (template.get("custom_vars") or {}).items() if k not in base}
    return base, custom


def _seed() -> list[dict]:
    return [_normalize({"name": "Interview / Exam", **DEFAULT_FIELDS})]


def load_templates() -> list[dict]:
    """All templates (defaults seeded on first run; old single-template files migrated).
    Guarantees one group "shortlist" template exists. Seeding/migration is persisted
    immediately so ids stay stable across reads."""
    templates: list[dict] | None = None
    if TEMPLATE_PATH.is_file():
        try:
            data = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("templates"), list) and data["templates"]:
            templates = [_normalize(t) for t in data["templates"]]
        elif isinstance(data, dict) and ("subject" in data or "body" in data):
            # Migrate the old single flat template.
            templates = [_normalize({**data, "name": data.get("name") or "Interview / Exam"})]
    if templates is None:
        templates = _seed()
    # Ensure a group shortlist template is always available.
    if not any(t.get("type") == "shortlist" for t in templates):
        templates.append(_normalize({"name": "Shortlist (group)", **DEFAULT_SHORTLIST_FIELDS}))
    # Ensure an interview calendar-event template is always available.
    if not any(t.get("type") == "interview" for t in templates):
        templates.append(_normalize({"name": "Interview (calendar)", **DEFAULT_INTERVIEW_FIELDS}))
    # Ensure a job-offer confirmation template is always available.
    if not any(t.get("type") == "offer" for t in templates):
        templates.append(_normalize({"name": "Job Offer (confirmation)", **DEFAULT_OFFER_FIELDS}))
    _write(templates)   # persist seeds/migrations/added templates (stable ids)
    return templates


def _write(templates: list[dict]) -> None:
    TEMPLATE_PATH.write_text(
        json.dumps({"templates": templates}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def save_template(data: dict) -> dict:
    """Upsert one template (matched by id; a new id is assigned if absent).
    Returns the saved template."""
    templates = load_templates()
    saved = _normalize(data)
    for i, t in enumerate(templates):
        if t["id"] == saved["id"]:
            templates[i] = saved
            break
    else:
        templates.append(saved)
    _write(templates)
    return saved


def delete_template(template_id: str) -> list[dict]:
    """Remove a template; never leaves zero templates. Returns the remaining list."""
    templates = [t for t in load_templates() if t["id"] != template_id]
    if not templates:
        templates = _seed()
    _write(templates)
    return templates


def get_template(template_id: str) -> dict | None:
    for t in load_templates():
        if t["id"] == template_id:
            return t
    return None


def _render_with_fields(template: dict, base: dict, custom: dict) -> tuple[str, str]:
    """Fill subject + body from a (base, custom) field-map. The body HTML-escapes the
    base (DB/derived) values when the template is HTML; custom variables stay raw so HR
    can embed links/markup. The subject is always plain text. Returns (subject, body)."""
    is_html = bool(template.get("is_html"))
    e = _esc if is_html else (lambda s: s)
    body_fields = {**{k: e(v) for k, v in base.items()}, **custom}
    subj_fields = {**base, **custom}
    return (_fill(template.get("subject", ""), subj_fields),
            _fill(template.get("body", ""), body_fields))


def render(template: dict, *, cand: dict, deadline: str = "") -> tuple[str, str]:
    """Fill an exam/interview template for one candidate. `cand` is the full applicants
    field-map (see Database.get_candidate_fields); any {column} placeholder works, plus
    derived {full_name_edit}/{title}/{title_name}/{firstname}, {company}, {deadline}, and
    the template's custom variables. Returns (subject, body)."""
    base, custom = _field_maps(cand, template, deadline=deadline)
    return _render_with_fields(template, base, custom)


def render_interview(template: dict, *, cand: dict) -> tuple[str, str]:
    """Fill an interview calendar-event template for one candidate (same placeholder
    set as render(), no deadline). Returns (subject, body)."""
    base, custom = _field_maps(cand, template)
    return _render_with_fields(template, base, custom)


def render_group(template: dict, *, job_title: str, candidates: list[dict],
                 link_text: str = "", link_url: str = "") -> tuple[str, str]:
    """Fill a group "shortlist" template. `candidates` is a list of
    {name, summary, title?} dicts → a numbered name (honorific prefix prepended when
    present) + bulleted-summary list in place of {candidates}.
    `link_text` (the shared folder name) is the visible link; `link_url` (the
    OneDrive share URL, may be empty) makes it clickable. Honours the template's
    is_html flag (HTML markup vs plain text). Returns (subject, body)."""
    is_html = bool(template.get("is_html"))

    def _bullets(summary: str) -> list[str]:
        # each paragraph/line of the summary becomes one bullet
        return [s.strip() for s in re.split(r"\n+", summary or "") if s.strip()]

    def _disp_name(c: dict) -> str:
        # honorific prefix (Mr./Ms./Mrs.) + name, or just the name when unset
        nm = (c.get("name") or "").strip() or "(unknown)"
        t = (c.get("title") or "").strip()
        return f"{t} {nm}" if t else nm

    if is_html:
        parts = []
        for i, c in enumerate(candidates, 1):
            name = _esc(_disp_name(c))
            items = [_esc(b) for b in _bullets(c.get("summary", ""))] or ["(No résumé summary yet.)"]
            lis = "".join(f'<li style="margin:0 0 4px">{it}</li>' for it in items)
            parts.append(f'<p style="margin:0 0 2px"><b>{i}.&nbsp; {name}</b></p>'
                         f'<ul style="margin:4px 0 16px 22px;padding:0">{lis}</ul>')
        candidates_str = "\n".join(parts)
        if link_url:
            link_str = (f'<a href="{_esc(link_url)}" style="color:#1155cc;'
                        f'font-weight:bold;text-decoration:underline">'
                        f'{_esc(link_text or link_url)}</a>')
        else:
            link_str = f"<b>{_esc(link_text)}</b>" if link_text else ""
        job, comp = _esc(job_title), _esc(template.get("company", ""))
    else:
        blocks = []
        for i, c in enumerate(candidates, 1):
            name = _disp_name(c)
            summary = (c.get("summary") or "").strip() or "(No résumé summary yet.)"
            blocks.append(f"{i}.  {name}\n{summary}")
        candidates_str = "\n\n".join(blocks)
        link_str = link_url or link_text or ""
        job, comp = (job_title or ""), template.get("company", "")

    custom = {k: v for k, v in (template.get("custom_vars") or {}).items()
              if k not in ("job_title", "company", "candidates", "link_document")}
    body_fields = {"job_title": job, "company": comp,
                   "candidates": candidates_str, "link_document": link_str, **custom}
    # Subject is always plain text (never HTML-escaped / no markup).
    subj_fields = {"job_title": job_title or "", "company": template.get("company", ""),
                   "candidates": "", "link_document": link_text or link_url or "", **custom}

    subject = _fill(template.get("subject", ""), subj_fields)
    raw_body = template.get("body", "")
    body = _fill(raw_body, body_fields)
    # Fallback for templates lacking the {link_document} placeholder.
    if (link_url or link_text) and "{link_document}" not in raw_body:
        body = re.sub(r"(?mi)^(\s*Link document:).*$",
                      lambda m: f"{m.group(1)} {link_str}", body)
    return subject, body


def render_offer(template: dict, *, fields: dict, blocks: dict) -> tuple[str, str]:
    """Fill a job-offer template. `fields` are plain values (HTML-escaped into the body,
    raw in the subject); `blocks` are pre-rendered HTML fragments (experience bullets,
    link, comment lists) dropped in raw. The template's custom variables also fill (raw,
    where they don't collide with a field/block). Returns (subject, body)."""
    is_html = bool(template.get("is_html", True))
    e = _esc if is_html else (lambda s: s)
    custom = {k: v for k, v in (template.get("custom_vars") or {}).items()
              if k not in fields and k not in blocks}
    body_fields = {**{k: e(v) for k, v in fields.items()}, **blocks, **custom}
    subj_fields = {**fields, **custom}
    return (_fill(template.get("subject", ""), subj_fields),
            _fill(template.get("body", ""), body_fields))
