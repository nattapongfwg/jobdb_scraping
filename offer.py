"""Builds the "ขอยืนยันข้อมูลเสนอจ้างพนักงาน" (job-offer confirmation) draft email.

The webapp collects popup inputs + DB fields and calls build_offer_email() with the
HR-editable "offer" template (Config Email Template). This module computes the data —
the plain placeholder values plus the pre-rendered HTML blocks (experience bullets,
candidate link, comment lists) — and lets templates.render_offer fill the template.
The body/subject text itself lives in email_template.json, editable in the web UI.
"""
from __future__ import annotations

from email_kit.signature import RECRUITER_FIRSTNAME
from email_kit.templates import render_offer

# Type dropdown value (Thai) -> the New/Replace label shown in the email.
NEW_LABELS = {"รับใหม่": "New", "ทดแทน": "Replace"}

# New/Replace dropdown options (the reason line under the candidate's details).
NEW_REPLACE_OPTIONS = [
    "เข้า Implement Project & Support Maintenance Product ของทีม",
    "รับทดแทน",
    "เพื่อ Support Project",
]

# Default interviewer-comments block (prefilled in the popup textarea, editable).
DEFAULT_INTERVIEWER_COMMENTS = "1. \n2. \n3. \n4. "

# Default recruiter-comments block (prefilled in the popup textarea, editable).
DEFAULT_RECRUITER_COMMENTS = (
    "1. มีความรู้ทาง technical ที่ดี เพียงพอต่อการทำงาน\n"
    "2. สามารถออกแบบ database ที่ซับซ้อนได้\n"
    "3. สามารถเรียนรู้ technical และเครื่องมือต่างๆได้ด้วยตนเอง\n"
    "4. การสื่อสาร สามารถอธิบาย Technical หรือแนวคิดการออกแบบ database ได้ดี\n"
    "5. สามารถแก้ปัญหาเฉพาะหน้าได้ดี มีความคิดในการแก้ไขปัญหาที่รวดเร็ว\n"
    "6. สามารถเข้าออฟฟิศ 5 วันได้"
)


def _html_escape(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _lines_to_html(text: str) -> str:
    """Multi-line text -> HTML, one <br> per line (blank lines dropped)."""
    return "<br>".join(_html_escape(ln) for ln in str(text or "").splitlines() if ln.strip())


def build_offer_email(*, template: dict, prefix_name: str, position: str, job_level: str,
                      role: str, department: str, section: str, people_count: str,
                      offer_type: str, new_replace_text: str, supervisor: str, buddy: str,
                      expected_salary: str, current_salary: str, start_date: str,
                      experience: str, experience_detail: str, interviewer: str,
                      interviewer_comments: str, recruiter_comments: str,
                      link_text: str, link_url: str,
                      today_str: str) -> tuple[str, str]:
    """Return (subject, HTML body) for the job-offer confirmation draft, rendered from
    the HR-editable `template` (the "offer" Config Email Template).

    `offer_type` is the Thai dropdown value (รับใหม่ / ทดแทน); its New/Replace label
    is derived from NEW_LABELS. `experience` is the free-text headline on the
    "Experience:" line; `experience_detail` is the AI 2-paragraph text rendered as
    bullet points beneath it. `link_url` empty -> candidate name shown as plain text.
    """
    e = _html_escape

    # Experience: free-text headline + the AI 2-paragraph detail as bullet points.
    exp_bullets = [e(ln) for ln in str(experience_detail or "").splitlines() if ln.strip()]
    exp_list = ("<ul style=\"margin:6px 0 0;padding-left:24px\">"
                + "".join(f"<li style=\"margin:0 0 10px\">{b}</li>" for b in exp_bullets)
                + "</ul>") if exp_bullets else ""

    # Link: candidate name as the clickable text (plain text if no URL).
    link_html = (f'<a href="{e(link_url)}" style="color:#1155cc;text-decoration:underline">{e(link_text)}</a>'
                 if link_url else e(link_text))

    # Interviewer comments: use what was typed, else the empty 1.–4. template.
    ic_src = interviewer_comments if (interviewer_comments or "").strip() else DEFAULT_INTERVIEWER_COMMENTS

    # Plain values — HTML-escaped into the body by render_offer, raw in the subject.
    fields = {
        "prefix_name": prefix_name, "position": position, "job_level": job_level,
        "role": role, "department": department, "section": section,
        "people_count": people_count, "offer_type": (offer_type or "").strip(),
        "new_replace_label": NEW_LABELS.get((offer_type or "").strip(), "New"),
        "new_replace_text": new_replace_text, "supervisor": supervisor, "buddy": buddy,
        "expected_salary": expected_salary, "current_salary": current_salary,
        "start_date": start_date, "experience": experience, "interviewer": interviewer,
        "recruiter_firstname": RECRUITER_FIRSTNAME, "today": today_str,
    }
    # Pre-rendered HTML fragments — dropped into the body raw (already escaped above).
    blocks = {
        "experience_bullets": exp_list, "link": link_html,
        "interviewer_comments": _lines_to_html(ic_src),
        "recruiter_comments": _lines_to_html(recruiter_comments),
    }
    fields = {k: ("" if v is None else str(v)) for k, v in fields.items()}
    return render_offer(template, fields=fields, blocks=blocks)
