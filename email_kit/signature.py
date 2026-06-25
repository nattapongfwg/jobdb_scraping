"""Single source of truth for the recruiter's email signature / contact block.

Every outgoing or draft email (exam/interview, shortlist, evaluation, offer) ends with
the same signature. The recruiter's identity is PER-TEAMMATE: each teammate sets their
own name/contacts in their `.env` (see CUSTOMIZE.md / .env.example) so emails sent from
their machine sign as them, even though the Recruit mailbox is shared. The constants
below are only fallback defaults used when an env var is unset.

- signature_text()  -> plain-text form (used by the default exam template).
- signature_html()  -> HTML form (shortlist / evaluation / offer drafts).

Both lead with "Best regard,". The recruiter values are read live (per render), so the
evaluation/offer drafts and any template using the {signature} placeholder pick up this
machine's recruiter automatically. Templates that baked the signature text on an earlier
seed are edited in the web UI ("Config Email Template").
"""
from __future__ import annotations

import os

# --- Fallback defaults (overridden per-machine by the matching .env var) ------
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


def _env(name: str, default: str) -> str:
    """Per-machine override from .env (blank/unset → the module default)."""
    return (os.getenv(name, "") or "").strip() or default


# Per-teammate recruiter identity (each reads its own RECRUITER_* env var).
def recruiter_name() -> str:      return _env("RECRUITER_NAME", RECRUITER_NAME)
def recruiter_firstname() -> str: return _env("RECRUITER_FIRSTNAME", RECRUITER_FIRSTNAME)
def recruiter_mobile() -> str:    return _env("RECRUITER_MOBILE", RECRUITER_MOBILE)
def recruiter_tel() -> str:       return _env("RECRUITER_TEL", RECRUITER_TEL)
def recruiter_email() -> str:     return _env("RECRUITER_EMAIL", RECRUITER_EMAIL)


def signature_text() -> str:
    """Plain-text signature block (leads with 'Best regard,'), for THIS machine's
    recruiter."""
    return (
        "Best regard,\n"
        f"{recruiter_name()}\n"
        f"Mobile {recruiter_mobile()}, Tel. {recruiter_tel()}\n"
        f"E-mail: {recruiter_email()}\n"
        "\n"
        f"{HR_DEPARTMENT}\n"
        f"{COMPANY_NAME}\n"
        f"{COMPANY_ADDRESS_1}\n"
        f"{COMPANY_ADDRESS_2}"
    )


def signature_html(top_margin: int = 18) -> str:
    """HTML signature block: two <p> paragraphs, leads with 'Best regard,', for THIS
    machine's recruiter. `top_margin` is the top margin (px) of the first paragraph
    (shortlist uses 26)."""
    addr1 = COMPANY_ADDRESS_1.replace("  ", "&nbsp; ")
    return (
        f'<p style="margin:{top_margin}px 0 0">\n'
        "<b>Best regard,</b><br>\n"
        f"<b>{recruiter_name()}</b><br>\n"
        f"<b>Mobile</b> {recruiter_mobile()}<b>,</b> <b>Tel.</b> {recruiter_tel()}<br>\n"
        f"<b>E-mail: {recruiter_email()}</b>\n"
        "</p>\n"
        '<p style="margin:18px 0 0">\n'
        f"{HR_DEPARTMENT}<br>\n"
        f"<b>{COMPANY_NAME}</b><br>\n"
        f"{addr1}<br>\n"
        f"{COMPANY_ADDRESS_2}\n"
        "</p>"
    )
