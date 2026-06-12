"""Builds the "ขอยืนยันข้อมูลเสนอจ้างพนักงาน" (job-offer confirmation) draft email.

Mirrors evaluation.build_eval_email: the webapp collects popup inputs + DB fields,
calls build_offer_email() to render (subject, HTML body), then creates an Outlook
DRAFT for HR to review and send. No data is persisted — the draft is the artifact.
"""
from __future__ import annotations

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


def build_offer_email(*, prefix_name: str, position: str, job_level: str, role: str,
                      department: str, section: str, people_count: str, offer_type: str,
                      new_replace_text: str, supervisor: str, buddy: str,
                      expected_salary: str, current_salary: str, start_date: str,
                      experience: str, experience_detail: str, interviewer: str,
                      interviewer_comments: str, recruiter_comments: str,
                      link_text: str, link_url: str,
                      today_str: str) -> tuple[str, str]:
    """Return (subject, HTML body) for the job-offer confirmation draft.

    `offer_type` is the Thai dropdown value (รับใหม่ / ทดแทน); its New/Replace label
    is derived from NEW_LABELS. `experience` is the free-text headline on the
    "Experience:" line; `experience_detail` is the AI 2-paragraph text rendered as
    bullet points beneath it. `link_url` empty -> candidate name shown as plain text.
    """
    e = _html_escape
    dept, sect, rl = e(department), e(section), e(role)
    otype = (offer_type or "").strip()
    nr_label = NEW_LABELS.get(otype, "New")

    subject = f"ขอยืนยันข้อมูลเสนอจ้างพนักงาน {department}: {section} ({role}) as of {today_str}"

    # Experience: free-text headline + the AI 2-paragraph detail as bullet points.
    exp_bullets = [e(ln) for ln in str(experience_detail or "").splitlines() if ln.strip()]
    exp_list = ("<ul style=\"margin:6px 0 0;padding-left:24px\">"
                + "".join(f"<li style=\"margin:0 0 10px\">{b}</li>" for b in exp_bullets)
                + "</ul>") if exp_bullets else ""

    # Link: candidate name as the clickable text (plain text if no URL).
    link_html = (f'<a href="{e(link_url)}" style="color:#1155cc;text-decoration:underline">{e(link_text)}</a>'
                 if link_url else e(link_text))

    rec_html = _lines_to_html(recruiter_comments)
    # Interviewer comments: use what was typed, else the empty 1.–4. template.
    ic_src = interviewer_comments if (interviewer_comments or "").strip() else DEFAULT_INTERVIEWER_COMMENTS
    ic_html = _lines_to_html(ic_src)
    rule = '<hr style="border:none;border-top:1px solid #999;margin:14px 0">'

    body = f"""<div style="font-family:'Aptos','Segoe UI',Arial,sans-serif;font-size:11pt;color:#000000;line-height:1.5">
<p style="margin:0 0 14px">Dear Job Offering Team,</p>
<p style="margin:0 0 14px">ขอยืนยันข้อมูลเสนอจ้างพนักงาน {dept}: {sect} ({rl}) จำนวน {e(people_count)} คน ({e(otype)})</p>
<p style="margin:0 0 14px">
Name: {e(prefix_name)}<br>
Position: {e(position)}<br>
Job Level : {e(job_level)}<br>
{e(nr_label)}:&nbsp; {e(new_replace_text)}<br>
Department: {dept}<br>
Section: {sect}
</p>
<p style="margin:0 0 14px">
Direct Supervisor: {e(supervisor)}<br>
Buddy: {e(buddy)}
</p>
<p style="margin:0 0 14px">
Expected Salary : {e(expected_salary)}<br>
Current Salary: {e(current_salary)}<br>
Start Date: {e(start_date)}
</p>
<p style="margin:0 0 4px">Experience: {e(experience)}</p>
{exp_list}
<p style="margin:14px 0 14px">Link: {link_html}</p>
{rule}
<p style="margin:0 0 14px">
Interviewer : {e(interviewer)}<br>
ความเห็นผู้สัมภาษณ์ :<br>
{ic_html}
</p>
<p style="margin:0 0 14px">
ความเห็นฝ่าย Recruit : Nattapong<br>
{rec_html}
</p>
{rule}
<p style="margin:0 0 14px">ช่องทางการสรรหา (Sourcing): JOB DB</p>
<p style="margin:18px 0 0">
<b>Best regard,</b><br>
<b>Nattapong Yuwasirinun (นะ)</b><br>
<b>Mobile</b> 064-615-2113<b>,</b> <b>Tel.</b> 0-2034-4147<br>
<b>E-mail: nattapong_yuw@freewillsolutions.com</b>
</p>
<p style="margin:18px 0 0">
Human Resources Department<br>
<b>Freewill Solutions Company Limited</b><br>
1168/86-88&nbsp; Lumpini Tower, 29th Floor,<br>
Rama IV Road, Tungmahamek, Sathorn, Bangkok 10120
</p>
</div>"""
    return subject, body
