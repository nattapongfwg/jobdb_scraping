"""Hiring requests: the Request form, the Requests list and their API."""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from controllers.common import cfg, current_email
from models.db import Database
from services import evaluation, offer

bp = Blueprint("hiring_requests", __name__)


@bp.get("/request")
def request_page():
    """Hiring-request form page. With ?id=<n> it loads that request for editing
    (pre-fills the form); otherwise it's a blank new-request form.
    position/department/section reuse the Evaluation popup's canonical lists;
    reason reuses the Offer flow's NEW_REPLACE_OPTIONS."""
    rid = request.args.get("id", "").strip()
    req = None
    if rid.isdigit():
        with Database(cfg) as db:
            req = db.get_request(int(rid))
    if req:
        req = _request_json(req)
    return render_template("request.html", req=req,
                           positions=evaluation.POSITIONS,
                           companies=evaluation.COMPANIES,
                           departments=evaluation.DEPARTMENTS,
                           sections=evaluation.SECTIONS,
                           reasons=offer.NEW_REPLACE_OPTIONS)


@bp.get("/requests")
def requests_page():
    """List of submitted hiring requests."""
    return render_template("requests.html")


@bp.get("/api/requests")
def api_requests_list():
    """All submitted hiring requests, newest first."""
    with Database(cfg) as db:
        rows = db.list_requests()
    return jsonify({"requests": [_request_json(r) for r in rows]})


def _request_json(r: dict) -> dict:
    """A dbo.requests row ready for JSON: timestamps as 'YYYY-MM-DD HH:MM' and
    created_by / updated_by names falling back to the email when the user row
    is gone."""
    for k in ("created_at", "updated_at", "completed_at"):
        v = r.get(k)
        r[k] = v.strftime("%Y-%m-%d %H:%M") if v is not None else ""
    r["created_by_name"] = r.get("created_by_name") or r.get("created_by") or ""
    r["updated_by_name"] = r.get("updated_by_name") or r.get("updated_by") or ""
    r["completed_by_name"] = r.get("completed_by_name") or r.get("completed_by") or ""
    r["status"] = r.get("status") or "doing"
    return r


def _request_fields(data: dict) -> tuple[dict | None, str | None]:
    """Extract + validate the hiring-request fields from a JSON body. Returns
    (fields, None) on success or (None, error) if a constrained value is invalid.
    The fields dict matches db.insert_request/update_request keyword args."""
    def _g(k: str) -> str:
        return str(data.get(k, "") or "").strip()

    inr = _g("is_new_replace")
    if inr and inr not in ("New", "Replace"):
        return None, "is_new_replace must be New or Replace."
    rtype = _g("type")
    if rtype and rtype not in ("Permanent", "Contract"):
        return None, "type must be Permanent or Contract."
    return {
        "request_code": _g("request_code"), "request_name": _g("request_name"),
        "position": _g("position"), "is_new_replace": inr, "company": _g("company"),
        "department": _g("department"), "section": _g("section"),
        "direct_supervisor": _g("direct_supervisor"), "buddy": _g("buddy"),
        "head_count": _g("head_count"), "type_": rtype, "reason": _g("reason"),
        "requested_by": _g("requested_by"),
        "acknowledge_by_1": _g("acknowledge_by_1"),
        "acknowledge_by_2": _g("acknowledge_by_2"),
    }, None


@bp.post("/api/requests")
def api_requests_save():
    """Save one NEW hiring request from the Request page to dbo.requests."""
    fields, err = _request_fields(request.get_json(force=True))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    with Database(cfg) as db:
        rid = db.insert_request(**fields, actor=current_email())
    return jsonify({"ok": True, "request_id": rid})


@bp.post("/api/requests/<int:rid>")
def api_requests_update(rid: int):
    """Update an existing hiring request (edit from the Request page). The body's
    `revision` is the one the form loaded; if a teammate saved in between, answer
    409 with who and when instead of overwriting their change. `force: true`
    (the user chose to overwrite) skips that check."""
    data = request.get_json(force=True)
    fields, err = _request_fields(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    revision = None if data.get("force") else data.get("revision")
    with Database(cfg) as db:
        res = db.update_request(rid, **fields, actor=current_email(), revision=revision)
    if res.get("missing"):
        return jsonify({"ok": False, "error": "Request not found."}), 404
    if res.get("conflict"):
        cur = _request_json(res["request"])
        who = cur["updated_by_name"] or "someone"
        return jsonify({"ok": False, "conflict": True, "request": cur,
                        "error": f"{who} saved this request at {cur['updated_at']} "
                                 "while you had it open."}), 409
    return jsonify({"ok": True, "request_id": rid, "revision": res["revision"]})


@bp.post("/api/requests/<int:rid>/status")
def api_requests_status(rid: int):
    """Move a hiring request between the Doing and Completed tabs. Body:
    {status: "completed" | "doing"}. Completing records who did it; completing
    an already-completed request keeps the original completer."""
    status = str((request.get_json(force=True) or {}).get("status", "")).strip()
    try:
        with Database(cfg) as db:
            row = db.set_request_status(rid, status, actor=current_email())
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if row is None:
        return jsonify({"ok": False, "error": "Request not found."}), 404
    return jsonify({"ok": True, "request": _request_json(row)})
