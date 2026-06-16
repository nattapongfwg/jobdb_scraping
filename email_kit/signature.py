"""Single source of truth for the recruiter's email signature / contact block.

Every outgoing or draft email (exam/interview, shortlist, evaluation, offer) ends with
the same signature. Edit the constants below ONCE to rebrand for a different recruiter or
company — see CUSTOMIZE.md.

- signature_text()  -> plain-text form (used by the default exam template).
- signature_html()  -> HTML form (shortlist / evaluation / offer drafts).

Both lead with "Best regard,". Changing these does NOT rewrite email_template.json that
was already seeded on a previous run — edit those templates in the web UI ("Config Email
Template"); the evaluation/offer drafts pick up changes here directly.
"""
from __future__ import annotations

# --- Edit these to rebrand ---------------------------------------------------
RECRUITER_NAME = "Nattapong Yuwasirinun (นะ)"
RECRUITER_FIRSTNAME = "Nattapong"          # used in the Thai offer body ("ความเห็นฝ่าย Recruit")
RECRUITER_MOBILE = "064-615-2113"
RECRUITER_TEL = "0-2034-4147"
RECRUITER_EMAIL = "nattapong_yuw@freewillsolutions.com"
HR_DEPARTMENT = "Human Resources Department"
COMPANY_NAME = "Freewill Solutions Company Limited"
COMPANY_ADDRESS_1 = "1168/86-88  Lumpini Tower, 29th Floor,"
COMPANY_ADDRESS_2 = "Rama IV Road, Tungmahamek, Sathorn, Bangkok 10120"
# -----------------------------------------------------------------------------


def signature_text() -> str:
    """Plain-text signature block (leads with 'Best regard,')."""
    return (
        "Best regard,\n"
        f"{RECRUITER_NAME}\n"
        f"Mobile {RECRUITER_MOBILE}, Tel. {RECRUITER_TEL}\n"
        f"E-mail: {RECRUITER_EMAIL}\n"
        "\n"
        f"{HR_DEPARTMENT}\n"
        f"{COMPANY_NAME}\n"
        f"{COMPANY_ADDRESS_1}\n"
        f"{COMPANY_ADDRESS_2}"
    )


def signature_html(top_margin: int = 18) -> str:
    """HTML signature block: two <p> paragraphs, leads with 'Best regard,'.
    `top_margin` is the top margin (px) of the first paragraph (shortlist uses 26)."""
    addr1 = COMPANY_ADDRESS_1.replace("  ", "&nbsp; ")
    return (
        f'<p style="margin:{top_margin}px 0 0">\n'
        "<b>Best regard,</b><br>\n"
        f"<b>{RECRUITER_NAME}</b><br>\n"
        f"<b>Mobile</b> {RECRUITER_MOBILE}<b>,</b> <b>Tel.</b> {RECRUITER_TEL}<br>\n"
        f"<b>E-mail: {RECRUITER_EMAIL}</b>\n"
        "</p>\n"
        '<p style="margin:18px 0 0">\n'
        f"{HR_DEPARTMENT}<br>\n"
        f"<b>{COMPANY_NAME}</b><br>\n"
        f"{addr1}<br>\n"
        f"{COMPANY_ADDRESS_2}\n"
        "</p>"
    )
