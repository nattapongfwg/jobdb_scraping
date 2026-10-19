"""Sign-in, first-run setup, My profile and creating HR accounts. Also the
login wall in front of every other page and API."""
from __future__ import annotations

import logging

from flask import (Blueprint, abort, jsonify, redirect, render_template, request,
                   session, url_for)

from controllers.common import admin_required, cfg, current_email, current_user, is_admin
from models.db import Database, verify_password
from services.email_kit import signature

bp = Blueprint("auth", __name__)


# Pages/APIs reachable without signing in.
_PUBLIC_PATHS = {"/login", "/register", "/setup", "/logout"}


@bp.before_app_request
def _require_login():
    """Every page and API needs a signed-in, approved user. Pages bounce to /login
    (coming back to the same URL afterwards); APIs answer 401 JSON. The signed-in
    user also becomes the active recruiter, so emails sign as them."""
    p = request.path
    if p.startswith("/static/") or p in _PUBLIC_PATHS:
        return None
    user = current_user()
    if user is None:
        if p.startswith("/api/"):
            return jsonify({"ok": False, "error": "Please sign in.", "login": True}), 401
        nxt = p if request.method == "GET" else "/"
        return redirect(url_for("auth.login_page", next=nxt))
    signature.set_active_recruiter(user)
    return None


@bp.app_context_processor
def _inject_identity():
    """`me` (the signed-in user) and, for Admins, the count of registrations
    waiting for approval — used by the top bar on every page."""
    me = current_user()
    pending = 0
    if me and me.get("role") == "admin":
        with Database(cfg) as db:
            pending = db.count_pending_users()
    return {"me": me, "pending_users": pending}


def _safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects after sign-in."""
    t = (target or "").strip()
    return t if t.startswith("/") and not t.startswith("//") else "/"


_ACCOUNT_FIELDS = ("name", "firstname", "email", "username", "prefix", "mobile", "tel")


def _account_form() -> dict:
    """The account fields posted by /users/new or /setup (never raises, so the
    page can re-show what was typed after a validation error)."""
    form = {k: request.form.get(k, "").strip() for k in _ACCOUNT_FIELDS}
    form["password"] = request.form.get("password", "")
    return form


def _check_passwords(form: dict) -> None:
    if form["password"] != request.form.get("confirm", ""):
        raise ValueError("The two passwords do not match.")


@bp.route("/login", methods=["GET", "POST"])
def login_page():
    """Username + password sign-in. Until the first Admin account exists the
    board sends everyone to /setup instead."""
    with Database(cfg) as db:
        if not db.admin_has_password():
            return redirect(url_for("auth.setup_page"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            user = db.get_user_by_username(username)
            error = None
            if not user or not verify_password(request.form.get("password", ""),
                                               user.get("_password_hash")):
                error = "Invalid username or password."
            elif not user["is_active"]:
                error = "This account is disabled. Please ask an Admin."
            elif not user["is_approved"]:
                error = "Your account is waiting for an Admin to approve it."
            if error:
                return render_template("login.html", error=error, username=username,
                                       next=request.form.get("next", "")), 401
            session.clear()
            session["uid"] = user["user_id"]
            session.permanent = True
            db.touch_login(user["user_id"])
            return redirect(_safe_next(request.form.get("next")))
    if current_user():
        return redirect("/")
    return render_template("login.html", next=request.args.get("next", ""),
                           notice=request.args.get("notice"))


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login_page"))


@bp.route("/register", methods=["GET", "POST"])
def register_page():
    """Self-registration is closed — an Admin creates HR accounts (/users/new).
    Old links land on the sign-in page with a note."""
    return redirect(url_for("auth.login_page",
                            notice="Accounts are created by an Admin. Ask an Admin for "
                                   "your username and password."))


@bp.route("/users/new", methods=["GET", "POST"])
@admin_required
def create_hr_page():
    """Admin: create an HR account (active and approved, so it can sign in at once).
    Post/redirect/get: success comes back as ?created=<username>."""
    form: dict = {}
    if request.method == "POST":
        form = _account_form()
        try:
            _check_passwords(form)
            if not form["username"] or not form["password"]:
                raise ValueError("Username and password are required.")
            with Database(cfg) as db:
                user = db.save_user({**form, "role": "hr", "is_active": True, "is_approved": True})
        except ValueError as exc:
            return render_template("create_hr.html", error=str(exc), form=form), 400
        logging.info("HR account %s created by %s", user["username"], current_email())
        return redirect(url_for("auth.create_hr_page", created=user["username"]))
    return render_template("create_hr.html", form=form, created=request.args.get("created"))


@bp.route("/profile", methods=["GET", "POST"])
def profile_page():
    """The signed-in user's own account. Everyone can view it; HR is view-only
    (an Admin changes their details or resets their password in Manage users).
    An Admin can edit their own details and password here; role, active and
    approved stay as they are. Post/redirect/get: success comes back as ?saved=1."""
    me = current_user()
    if request.method == "POST":
        if not is_admin():
            abort(403)
        form = _account_form()
        try:
            _check_passwords(form)
            if not form["username"]:
                raise ValueError("Username is required — you sign in with it.")
            with Database(cfg) as db:
                db.save_user({**me, **form, "user_id": me["user_id"], "role": me["role"],
                              "is_active": True, "is_approved": True})
        except ValueError as exc:
            return render_template("profile.html", error=str(exc), form=form), 400
        return redirect(url_for("auth.profile_page", saved=1))
    return render_template("profile.html", form=me, saved=request.args.get("saved"),
                           pw_saved=request.args.get("pw"))


@bp.post("/profile/password")
def profile_password():
    """Change your own password (HR's only self-service on My profile): the current
    password must be right and the new one typed twice. Post/redirect/get: success
    comes back as /profile?pw=1."""
    me = current_user()
    new = request.form.get("new_password", "")
    try:
        if new != request.form.get("confirm_password", ""):
            raise ValueError("The two new passwords do not match.")
        with Database(cfg) as db:
            db.change_own_password(me["user_id"], request.form.get("old_password", ""), new)
    except ValueError as exc:
        return render_template("profile.html", form=me, pw_error=str(exc)), 400
    logging.info("User %s changed their password", me.get("username"))
    return redirect(url_for("auth.profile_page", pw=1, _anchor="password"))


@bp.route("/setup", methods=["GET", "POST"])
def setup_page():
    """First run only: turn the seeded Admin row into a real account (username +
    password) and sign in. Disabled as soon as an Admin has a password."""
    with Database(cfg) as db:
        if db.admin_has_password():
            return redirect(url_for("auth.login_page"))
        seed = db.first_admin_without_password() or {}
        if request.method == "POST":
            form = _account_form()
            try:
                _check_passwords(form)
                if not form["username"] or not form["password"]:
                    raise ValueError("Username and password are required.")
                user = db.save_user({**seed, **form, "role": "admin", "is_active": True,
                                     "is_approved": True})
            except ValueError as exc:
                return render_template("setup.html", error=str(exc), form=form), 400
            session.clear()
            session["uid"] = user["user_id"]
            session.permanent = True
            db.touch_login(user["user_id"])
            return redirect("/")
    return render_template("setup.html", form=seed)
