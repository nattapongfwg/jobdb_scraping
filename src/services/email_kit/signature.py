"""Single source of truth for the recruiter's email signature / contact block.

Every outgoing or draft email (exam/interview, shortlist, evaluation, offer) ends with
the same signature. The recruiter's identity is PER-TEAMMATE and resolved in this order:

1. the ACTIVE recruiter — the dbo.users row for whoever is using the board, bound per
   request by the web app with set_active_recruiter() (multi-user Phase 2);
2. the RECRUITER_* vars in this machine's `.env` (the older per-PC setup);
3. the fallback constants below.

- signature_text()  -> plain-text form.
- signature_html()  -> HTML form (evaluation drafts).

Both lead with "Best regard,". The recruiter values are read live (per render).

Email templates instead use the {signature} placeholder = the sender's *label* (name +
contact lines only; "Best regard," and the company footer stay in the template). Each
user can save their own label per email type (exam | shortlist | interview | offer) on
the Email Templates page; until they do, default_label() builds one from their profile.
- label_html(type) -> the active recruiter's label for that type.
- html_to_text()   -> the same as plain text (for templates not sent as HTML).
"""
from __future__ import annotations

import html
import os
import re
from contextvars import ContextVar
from typing import Callable

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


# --- Per-user label per email type (the {signature} placeholder) --------------
EMAIL_TYPES = ("exam", "shortlist", "interview", "offer")

# Saved labels, plugged in by the web app: load(user_id) -> {email_type: label}.
_label_load: Callable[[int], dict[str, str]] | None = None


def configure_label_store(load: Callable[[int], dict[str, str]]) -> None:
    global _label_load
    _label_load = load


def _contact(user: dict | None) -> tuple[str, str, str, str]:
    """(name, mobile, tel, email) of `user` — only their own profile values — or,
    when no user is given, of the active recruiter (→ .env → defaults when nobody
    is bound, e.g. the CLI)."""
    user = user if user is not None else _active.get()
    if user is None:
        return recruiter_name(), recruiter_mobile(), recruiter_tel(), recruiter_email()
    return tuple(str(user.get(k) or "").strip() for k in ("name", "mobile", "tel", "email"))


def default_label(email_type: str, user: dict | None = None) -> str:
    """The label a user gets for `email_type` until they save their own, in the style
    the live templates used before labels became per-user (2026-10-16): exam emails
    use <strong> and a mailto link, the other types bold captions. Missing phone
    numbers are left out."""
    name, mobile, tel, email = (html.escape(v) for v in _contact(user))
    if email_type == "exam":
        phones = ", ".join(p for p in (mobile and f"Mobile: {mobile}", tel and f"Tel. {tel}") if p)
        lines = [f"<strong>{name}</strong>", phones,
                 email and f'E-mail: <a href="mailto:{email}" style="color:#0b3d91;">{email}</a>']
    else:
        phones = "<b>,</b> ".join(
            p for p in (mobile and f"<b>Mobile</b> {mobile}", tel and f"<b>Tel.</b> {tel}") if p)
        lines = [f"<b>{name}</b>", phones, email and f"<b>E-mail: {email}</b>"]
    return "<br>\n".join(line for line in lines if line)


def label_html(email_type: str) -> str:
    """The active recruiter's label for `email_type`: the one they saved, else the
    default from their profile."""
    user = _active.get()
    if user and user.get("user_id") is not None and _label_load is not None:
        saved = (_label_load(int(user["user_id"])).get(email_type) or "").strip()
        if saved:
            return saved
    return default_label(email_type)


def html_to_text(label: str) -> str:
    """A label as plain text: one line per <br>, tags dropped, spaces collapsed."""
    lines = (" ".join(html.unescape(re.sub(r"<[^>]+>", "", part)).split())
             for part in re.split(r"(?i)<br\s*/?>", label or ""))
    return "\n".join(line for line in lines if line)
