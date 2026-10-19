"""Status Tracking: the cross-job table and its Excel export."""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request, send_file

from controllers.common import STAGE_LIST, cfg
from models.db import Database
from services import report

bp = Blueprint("tracking", __name__)


@bp.route("/tracking")
def tracking():
    """Status-tracking table across all jobs (HR can edit info here)."""
    return render_template("tracking.html", stages=STAGE_LIST)


@bp.get("/api/tracking")
def api_tracking():
    job_id = request.args.get("job_id", "").strip()
    stage = request.args.get("stage", "").strip()
    q = request.args.get("q", "").strip()
    with Database(cfg) as db:
        return jsonify(db.list_all_candidates(job_id, stage, q))


# Stages excluded from the export: the funnel report covers only candidates who
# entered the pipeline (Wait Pre-screen onward), not Pending or Not Interest.
_EXPORT_SKIP_STAGES = {"not_interest", "prescreen"}


@bp.get("/api/tracking/export")
def api_tracking_export():
    """Export the Status-Tracking report (Excel) for one job title — pipeline
    candidates only — saved to Report_Files/ and returned as a download."""
    job_id = request.args.get("job_id", "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "Select a Job title to export."}), 400
    with Database(cfg) as db:
        rows = db.list_all_candidates(job_id)
        job = db.get_job(job_id)
    rows = [c for c in rows if (c.get("stage") or "prescreen") not in _EXPORT_SKIP_STAGES]
    job_title = (job or {}).get("title") or "Report"
    path = report.build_report(rows, job_title)
    return send_file(path, as_attachment=True, download_name=path.name)
