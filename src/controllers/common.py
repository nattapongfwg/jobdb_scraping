"""Shared by every controller: the loaded config, the signed-in user and the
Admin-only route guard."""
from __future__ import annotations

from functools import wraps

from flask import abort, g, jsonify, request, session

from config import load_config
from models.db import STAGE_LABELS, STAGES, Database

cfg = load_config()

# Pipeline stages (key + label, in order) for the pipeline and tracking pages.
STAGE_LIST = [{"key": k, "label": STAGE_LABELS[k]} for k in STAGES]


def current_user() -> dict | None:
    """The signed-in user (a dbo.users row) for this request, or None. Read once per
    request from the session cookie; an account that was disabled or un-approved
    since signing in counts as signed out."""
    if "user" in g:
        return g.user
    user = None
    uid = session.get("uid")
    if uid:
        with Database(cfg) as db:
            user = db.get_user_by_id(uid)
        if user and not (user["is_active"] and user["is_approved"]):
            user = None
    g.user = user
    return user


def current_email() -> str | None:
    u = current_user()
    return u["email"] if u else None


def user_prefix() -> str:
    """The signed-in user's folder prefix (e.g. 'Na'): files their sent exams /
    drafts into <prefix>_Sent_Exam and <prefix>_Drafts and suffixes their
    Email_Reply_Exam folders. '' when nobody is signed in."""
    u = current_user()
    return str(u.get("prefix") or "").strip() if u else ""


def is_admin() -> bool:
    u = current_user()
    return bool(u and u.get("role") == "admin")


def admin_required(view):
    """Route guard: only the Admin role may call (mailbox sign-in, user
    management). Email templates and SEEK scraping are open to Admin and HR alike."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin():
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Admin only."}), 403
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def sender_prefix(info: dict) -> str:
    """The prefix the exam was actually sent under (applicants.exam_sent_by), so
    reply checks and shortlists find a teammate's folders. NULL means the row
    predates the column, i.e. it was sent from this machine → our own prefix."""
    sent_by = info.get("exam_sent_by")
    return str(sent_by).strip() if sent_by is not None else user_prefix()
