"""Job postings: the landing page, one job's pipeline board, the active flag and
fetching jobs/candidates from SEEK."""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from controllers.common import STAGE_LIST, cfg
from models.db import ALLOWED_MOVES, Database
from services import evaluation, offer
from services.scrape_manager import ScrapeManager
from services.summarizer import load_majors, load_universities

bp = Blueprint("jobs", __name__)
scraper_mgr = ScrapeManager()


@bp.route("/")
def index():
    """Job-postings landing page (the HR board entry point)."""
    return render_template("index.html")


@bp.route("/job/<job_id>")
def job_pipeline(job_id: str):
    """Candidate pipeline board for one job posting."""
    with Database(cfg) as db:
        job = db.get_job(job_id)
    if not job:
        return render_template("index.html"), 404
    return render_template("pipeline.html", job=job, stages=STAGE_LIST,
                           moves=ALLOWED_MOVES,
                           universities=load_universities(), majors=load_majors(),
                           eval_options={
                               "positions": evaluation.POSITIONS,
                               "companies": evaluation.COMPANIES,
                               "departments": evaluation.DEPARTMENTS,
                               "sections": evaluation.SECTIONS,
                           },
                           offer_defaults={
                               "types": list(offer.NEW_LABELS.keys()),
                               "new_replace_options": offer.NEW_REPLACE_OPTIONS,
                               "recruiter_comments": offer.DEFAULT_RECRUITER_COMMENTS,
                               "interviewer_comments": offer.DEFAULT_INTERVIEWER_COMMENTS,
                           })


@bp.get("/api/jobs")
def api_jobs():
    with Database(cfg) as db:
        return jsonify(db.list_jobs_with_counts())


@bp.post("/api/jobs/active")
def api_job_active():
    data = request.get_json(force=True)
    with Database(cfg) as db:
        db.set_job_active(str(data["job_id"]), bool(data["is_active"]))
    return jsonify({"ok": True})


# -- SEEK scraping (background subprocess) ---------------------------------
@bp.post("/api/scrape/fetch-jobs")
def api_scrape_fetch_jobs():
    """Start fetching the active job ads from SEEK (logs in if needed)."""
    started = scraper_mgr.fetch_jobs()
    return jsonify({"ok": True, "started": started, "running": scraper_mgr.is_running()})


@bp.post("/api/scrape/download")
def api_scrape_download():
    """Start downloading candidates + resumes for one selected job id."""
    data = request.get_json(force=True)
    job_id = str(data.get("job_id", "")).strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id required"}), 400
    started = scraper_mgr.download(job_id)
    return jsonify({"ok": True, "started": started, "running": scraper_mgr.is_running()})


@bp.get("/api/scrape/status")
def api_scrape_status():
    return jsonify(scraper_mgr.snapshot())
