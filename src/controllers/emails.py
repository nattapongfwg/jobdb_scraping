"""Config Email Template: the templates, each user's signatures and the shared
Recruit mailbox sign-in."""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, render_template, request

from controllers.common import (admin_required, cfg, current_email, current_user, is_admin,
                                user_prefix)
from models.db import Database
from services.email_auth import EmailAuth
from services.email_kit import signature
from services.email_kit.templates import (delete_template, load_templates, save_template,
                                          type_defaults)

bp = Blueprint("emails", __name__)
email_auth = EmailAuth(cfg)


@bp.get("/email-templates")
def email_templates_page():
    """Config Email Template page — manage the per-stage email templates. The
    Signature box lists every active user for an Admin, only yourself for HR."""
    if is_admin():
        with Database(cfg) as db:
            sig_users = db.list_users(active_only=True)
    else:
        sig_users = [current_user()]
    return render_template("email_templates.html", sig_users=sig_users)


# -- Email template config (Config Email Template panel) ------------------------
@bp.get("/api/email-templates")
def api_email_templates_get():
    return jsonify({"templates": load_templates(), "defaults": type_defaults()})


@bp.get("/api/email-templates/fields")
def api_email_template_fields():
    """Placeholder fields HR can insert into a template: every applicants column
    (read live from the DB, so new columns appear automatically) plus the derived
    name/company/deadline fields. Powers the insert-field chips."""
    derived = ["signature", "full_name_edit", "title", "title_name", "firstname",
               "job_title", "company", "deadline"]
    cols: list[str] = []
    try:
        with Database(cfg) as db:
            cols = db.list_applicant_fields()
    except Exception as exc:  # noqa: BLE001 — chips are best-effort; never 500 the modal
        logging.warning("Could not list applicant fields: %s", exc)
    fields, seen = list(derived), set(derived)
    for c in cols:
        if c not in seen:
            seen.add(c)
            fields.append(c)
    return jsonify({"fields": fields})


@bp.post("/api/email-templates")
def api_email_template_save():
    data = request.get_json(force=True)
    saved = save_template(data)
    return jsonify({"ok": True, "template": saved, "templates": load_templates()})


@bp.post("/api/email-templates/delete")
def api_email_template_delete():
    data = request.get_json(force=True)
    templates = delete_template(str(data.get("id", "")))
    return jsonify({"ok": True, "templates": templates})


# -- Signature: each user's label per email type (the {signature} placeholder) ----
_SIGNATURE_FORBIDDEN = ({"ok": False, "error": "You can only change your own signature."}, 403)


def _may_edit_signature(user_id: int) -> bool:
    """Everyone edits their own signature; an Admin edits anyone's."""
    return is_admin() or current_user()["user_id"] == user_id


def _signatures_response(user_id: int):
    """One user's label for every email type: the saved one or the default."""
    with Database(cfg) as db:
        user = db.get_user_by_id(user_id)
        saved = db.get_user_signatures(user_id) if user else {}
    if not user:
        return jsonify({"ok": False, "error": "User not found."}), 404
    sigs = {}
    for t in signature.EMAIL_TYPES:
        default = signature.default_label(t, user)
        sigs[t] = {"body": saved.get(t) or default, "saved": t in saved, "default": default}
    return jsonify({"ok": True, "user_id": user_id, "signatures": sigs})


@bp.get("/api/signatures/<int:user_id>")
def api_signatures_get(user_id: int):
    if not _may_edit_signature(user_id):
        return _SIGNATURE_FORBIDDEN
    return _signatures_response(user_id)


@bp.post("/api/signatures/<int:user_id>")
def api_signatures_save(user_id: int):
    """Save one label: {type, body}. A blank body, or the default itself, removes the
    saved one so the label follows the profile again."""
    if not _may_edit_signature(user_id):
        return _SIGNATURE_FORBIDDEN
    data = request.get_json(force=True) or {}
    email_type = str(data.get("type", ""))
    if email_type not in signature.EMAIL_TYPES:
        return jsonify({"ok": False, "error": "Unknown email type."}), 400
    body = str(data.get("body") or "").strip()
    with Database(cfg) as db:
        user = db.get_user_by_id(user_id)
        if not user:
            return jsonify({"ok": False, "error": "User not found."}), 404
        if body == signature.default_label(email_type, user):
            body = ""
        db.save_user_signature(user_id, email_type, body, actor=current_email())
    return _signatures_response(user_id)


@bp.get("/api/email/login-status")
def api_email_login_status():
    """Sign-in snapshot for the shared Recruit mailbox."""
    return jsonify(email_auth.snapshot())


@bp.post("/api/email/login-start")
@admin_required
def api_email_login_start():
    """Begin the device-code sign-in for the shared Recruit mailbox."""
    email_auth.start()
    return jsonify(email_auth.snapshot())


@bp.get("/api/email/settings")
def api_email_settings_get():
    """The signed-in user's folder prefix and the users list (kept for callers of
    the old per-machine settings endpoint)."""
    with Database(cfg) as db:
        users = db.list_users()
    return jsonify({"user_prefix": user_prefix(), "users": users, "current": current_user()})
