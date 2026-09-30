"""Single source of truth for the recruiter's email signature / contact block.

Every outgoing or draft email (exam/interview, shortlist, evaluation, offer) ends with
the same signature. The recruiter's identity is PER-TEAMMATE and resolved in this order:

1. the ACTIVE recruiter — the dbo.users row for whoever is using the board, bound per
   request by the web app with set_active_recruiter() (multi-user Phase 2);
2. the RECRUITER_* vars in this machine's `.env` (the older per-PC setup);
3. the fallback constants below.

- signature_text()  -> plain-text form (used by the default exam template).
- signature_html()  -> HTML form (shortlist / evaluation / offer drafts).

Both lead with "Best regard,". The recruiter values are read live (per render), so the
evaluation/offer drafts and any template using the {signature} placeholder pick up this
machine's recruiter automatically. Templates that baked the signature text on an earlier
seed are edited in the web UI ("Config Email Template").
"""
from __future__ import annotations

import os
from contextvars import ContextVar

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


# The recruiter currently using the board (a dbo.users row as a dict), bound per
# request by the web app. A ContextVar so concurrent requests from different
# teammates never see each other's identity.
_active: ContextVar[dict | None] = ContextVar("active_recruiter", default=None)


def set_active_recruiter(user: dict | None) -> None:
    """Bind the recruiter every signature in this request/thread renders as
    (None → fall back to .env / defaults)."""
    _active.set(user)


def active_recruiter() -> dict | None:
    return _active.get()


def _env(name: str, default: str) -> str:
    """Per-machine override from .env (blank/unset → the module default)."""
    return (os.getenv(name, "") or "").strip() or default


def _val(key: str, env_name: str, default: str) -> str:
    """Active recruiter's field → .env var → module default."""
    user = _active.get()
    if user and str(user.get(key) or "").strip():
        return str(user[key]).strip()
    return _env(env_name, default)


# Per-teammate recruiter identity.
def recruiter_name() -> str:      return _val("name", "RECRUITER_NAME", RECRUITER_NAME)
def recruiter_firstname() -> str: return _val("firstname", "RECRUITER_FIRSTNAME", RECRUITER_FIRSTNAME)
def recruiter_mobile() -> str:    return _val("mobile", "RECRUITER_MOBILE", RECRUITER_MOBILE)
def recruiter_tel() -> str:       return _val("tel", "RECRUITER_TEL", RECRUITER_TEL)
def recruiter_email() -> str:     return _val("email", "RECRUITER_EMAIL", RECRUITER_EMAIL)


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
