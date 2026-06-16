"""Editable exam/interview email templates, persisted as JSON so HR can manage
several of them in the web UI (Config Email Template). All templates send from the
same signed-in mailbox (delegated /me/sendMail), so there is no per-template sender.

File shape:  {"templates": [ {id, name, company, attachment,
                              default_deadline_time, subject, body}, ... ]}

Placeholders in `subject` and `body` (filled per-candidate at send time):
    {full_name_edit}  the candidate's (editable) name
    {title}           the candidate's honorific prefix (Mr./Ms./Mrs.), "" if unset
    {title_name}      title + name with smart spacing ("Mr. Jane Doe", or just the
                      name when no title) — best for plain-text subjects
    {job_title}       the job posting title
    {company}         the template's `company` field
    {deadline}        the deadline HR picks when moving to "Sent Exam"
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from signature import signature_html, signature_text

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


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _normalize(t: dict) -> dict:
    out = {"id": t.get("id") or _new_id(), "name": (t.get("name") or "Untitled").strip()}
    _t = str(t.get("type", "")).strip()
    out["type"] = _t if _t in ("exam", "shortlist", "interview") else "exam"
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
    return out


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


def render(template: dict, *, full_name_edit: str, job_title: str,
           deadline: str, name_title: str = "") -> tuple[str, str]:
    """Fill the placeholders and return (subject, body)."""
    name, title = (full_name_edit or ""), (name_title or "")
    fields = {
        "full_name_edit": name,
        "title": title,
        "title_name": (f"{title} {name}".strip() if title else name),
        "job_title": job_title or "",
        "company": template.get("company", ""),
        "deadline": deadline or "",
    }
    return _fill(template.get("subject", ""), fields), _fill(template.get("body", ""), fields)


def render_interview(template: dict, *, full_name_edit: str,
                     job_title: str, name_title: str = "") -> tuple[str, str]:
    """Fill an interview-event template. {firstname} = first word of the name.
    Body values are HTML-escaped when the template is HTML; the subject is plain.
    Returns (subject, body)."""
    name = full_name_edit or ""
    first = name.strip().split(" ")[0] if name.strip() else ""
    title = name_title or ""
    title_name = (f"{title} {name}".strip() if title else name)
    is_html = bool(template.get("is_html"))
    e = _esc if is_html else (lambda s: s)
    body_fields = {"full_name_edit": e(name), "firstname": e(first),
                   "title": e(title), "title_name": e(title_name),
                   "job_title": e(job_title or ""), "company": e(template.get("company", ""))}
    subj_fields = {"full_name_edit": name, "firstname": first,
                   "title": title, "title_name": title_name,
                   "job_title": job_title or "", "company": template.get("company", "")}

    return (_fill(template.get("subject", ""), subj_fields),
            _fill(template.get("body", ""), body_fields))


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

    body_fields = {"job_title": job, "company": comp,
                   "candidates": candidates_str, "link_document": link_str}
    # Subject is always plain text (never HTML-escaped / no markup).
    subj_fields = {"job_title": job_title or "", "company": template.get("company", ""),
                   "candidates": "", "link_document": link_text or link_url or ""}

    subject = _fill(template.get("subject", ""), subj_fields)
    raw_body = template.get("body", "")
    body = _fill(raw_body, body_fields)
    # Fallback for templates lacking the {link_document} placeholder.
    if (link_url or link_text) and "{link_document}" not in raw_body:
        body = re.sub(r"(?mi)^(\s*Link document:).*$",
                      lambda m: f"{m.group(1)} {link_str}", body)
    return subject, body
