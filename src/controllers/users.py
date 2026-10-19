"""Manage users (the Admin's user list API)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from controllers.common import admin_required, cfg, current_email, current_user
from models.db import Database

bp = Blueprint("users", __name__)


@bp.get("/api/users")
def api_users_list():
    """Every recruiter on the board (inactive ones flagged, listed last)."""
    with Database(cfg) as db:
        return jsonify({"users": db.list_users(), "current": current_user()})


@bp.post("/api/users")
@admin_required
def api_users_save():
    """Admin: add or edit one user. Body: {user_id?, email, name, firstname?, mobile?,
    tel?, prefix, role, is_active, username?, password?, is_approved?}. A password
    here sets/resets it; blank keeps it. Deactivate instead of deleting so history
    and owner badges keep resolving to a name."""
    data = request.get_json(force=True) or {}
    try:
        with Database(cfg) as db:
            user = db.save_user(data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "user": user})


@bp.post("/api/users/<int:uid>/approve")
@admin_required
def api_users_approve(uid: int):
    """Approve a self-registered HR account so it can sign in."""
    with Database(cfg) as db:
        user = db.approve_user(uid, current_email())
    if not user:
        return jsonify({"ok": False, "error": "User not found."}), 404
    return jsonify({"ok": True, "user": user})
