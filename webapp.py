"""Local web UI to send exam emails to selected candidates.

Run:  .venv\\Scripts\\python.exe webapp.py   then open http://localhost:2757
Flow: pick a job ad -> search/select candidates by name -> Send exam.
The job picker also lets you toggle a job's active/inactive flag.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

from config import load_config
from db import (ALLOWED_MOVES, STAGE_LABELS, STAGES, Database, ensure_database,
                ensure_schema)
from email_kit.templates import (delete_template, get_template, load_settings,
                                 load_templates, render, render_group, render_interview,
                                 save_settings, save_template)
import shortlist
import evaluation
import offer
from mailer import GraphMailer, MailerError
from summarizer import (SummaryError, extract_resume_fields, load_majors,
                        load_universities, summarize_experience,
                        summarize_experience_headline, summarize_resume)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
app = Flask(__name__)
cfg = load_config()
PROJECT_ROOT = Path(__file__).resolve().parent

# Pipeline stages (key + label, in order) made available to every template.
STAGE_LIST = [{"key": k, "label": STAGE_LABELS[k]} for k in STAGES]


class ScrapeManager:
    """Runs the SEEK scraper (main.py) as a background subprocess and tracks its
    live status/log so the web UI can poll progress without blocking a request.

    Two actions:
      * fetch-jobs — `main.py --headed --list-jobs-json`: logs in (you solve the
        CAPTCHA in the browser window the first time) and returns the active jobs.
      * download   — `main.py --headed --job-id <id>`: downloads that job's
        candidates + resumes into SQL Server.
    Only one action runs at a time (login holds the single browser profile)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status = "idle"      # idle | running | done | error
        self.action: str | None = None
        self.log: list[str] = []
        self.jobs: list[dict] | None = None
        self.error: str | None = None
        self.progress: dict = {"done": 0, "total": 0}
        self.started_at: float | None = None

    def is_running(self) -> bool:
        return self.status == "running"

    def snapshot(self) -> dict:
        with self._lock:
            prog = dict(self.progress)
            if self.started_at is not None:
                elapsed = time.monotonic() - self.started_at
                prog["elapsed_sec"] = int(elapsed)
                done, total = prog.get("done", 0), prog.get("total", 0)
                # ETA = average time per candidate so far × candidates remaining.
                if done > 0 and total and done <= total:
                    prog["eta_sec"] = int((elapsed / done) * (total - done))
            return {"status": self.status, "action": self.action,
                    "log": self.log[-50:], "jobs": self.jobs,
                    "error": self.error, "progress": prog}

    def _start(self, action: str, args: list[str], parse_jobs: bool = False) -> bool:
        with self._lock:
            if self.status == "running":
                return False
            self.status, self.action = "running", action
            self.log, self.error, self.jobs = [], None, None
            self.progress = {"done": 0, "total": 0}
            self.started_at = time.monotonic()
        threading.Thread(target=self._run, args=(args, parse_jobs), daemon=True).start()
        return True

    def _append(self, line: str) -> None:
        with self._lock:
            self.log.append(line)
            if len(self.log) > 300:
                self.log = self.log[-300:]

    def _run(self, args: list[str], parse_jobs: bool) -> None:
        try:
            # Force UTF-8 in the child (Windows defaults to cp1252, which raises
            # UnicodeEncodeError on Thai names/titles) and decode its output as UTF-8.
            env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            proc = subprocess.Popen(
                [sys.executable, "main.py", *args], cwd=str(PROJECT_ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            for line in proc.stdout:                       # type: ignore[union-attr]
                line = line.rstrip("\n")
                if parse_jobs and line.startswith("JOBS_JSON:"):
                    try:
                        with self._lock:
                            self.jobs = json.loads(line[len("JOBS_JSON:"):])
                    except json.JSONDecodeError:
                        with self._lock:
                            self.jobs = []
                    continue
                if line.startswith("JOBS_JSON_ERROR"):
                    with self._lock:
                        self.error = ("Not logged in to SEEK. A browser window should "
                                      "open — solve the login/CAPTCHA, then try again.")
                    continue
                if line.startswith("SCRAPE_TOTAL:"):
                    with self._lock:
                        self.progress["total"] = int(line.split(":", 1)[1] or 0)
                    continue
                if line.startswith("SCRAPE_PROGRESS:"):
                    with self._lock:
                        self.progress["done"] = int(line.split(":", 1)[1] or 0)
                    continue
                if line:
                    self._append(line)
            proc.wait()
            with self._lock:
                if self.error:
                    self.status = "error"
                elif proc.returncode == 0:
                    self.status = "done"
                else:
                    self.status = "error"
                    self.error = f"Scraper exited with code {proc.returncode}."
        except Exception as exc:  # noqa: BLE001 — surface any launch failure to the UI
            with self._lock:
                self.status, self.error = "error", str(exc)

    def fetch_jobs(self) -> bool:
        return self._start("fetch-jobs", ["--headed", "--list-jobs-json"], parse_jobs=True)

    def download(self, job_id: str) -> bool:
        return self._start("download", ["--headed", "--job-id", str(job_id)])


scraper_mgr = ScrapeManager()


def _user_prefix() -> str:
    """The teammate's folder prefix configured on the Config Email page (e.g. 'Na').
    Used to file sent exams / drafts into per-teammate mail folders and to suffix the
    Email_Reply_Exam folders. '' when not set yet."""
    return str(load_settings().get("user_prefix") or "").strip()


class EmailAuth:
    """Drives the one-time Microsoft Graph device-code sign-in for the shared Recruit
    mailbox. Sign-in blocks until the user enters the code, so it runs in a background
    thread while the web UI polls /api/email/login-status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status = "idle"            # idle | pending | done | error
        self.user_code: str | None = None
        self.verification_uri: str | None = None
        self.error: str | None = None

    def snapshot(self) -> dict:
        account = None
        try:
            account = GraphMailer(cfg).signed_in_account()
        except MailerError:
            account = None
        with self._lock:
            return {"signed_in": bool(account), "account": account,
                    "status": self.status, "user_code": self.user_code,
                    "verification_uri": self.verification_uri, "error": self.error}

    def start(self) -> None:
        with self._lock:
            if self.status == "pending":
                return
            self.status = "pending"
            self.user_code = self.verification_uri = self.error = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            mailer = GraphMailer(cfg)
            flow = mailer.begin_device_login()
            with self._lock:
                self.user_code = flow.get("user_code")
                self.verification_uri = flow.get("verification_uri")
            mailer.complete_device_login(flow)        # blocks until the user signs in
            with self._lock:
                self.status = "done"
        except Exception as exc:  # noqa: BLE001 — surface to the UI
            with self._lock:
                self.status, self.error = "error", str(exc)


email_auth = EmailAuth()


@app.route("/")
def index():
    """Job-postings landing page (the HR board entry point)."""
    return render_template("index.html")


@app.get("/email-templates")
def email_templates_page():
    """Config Email Template page — manage the per-stage email templates."""
    return render_template("email_templates.html")


@app.route("/job/<job_id>")
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


@app.route("/tracking")
def tracking():
    """Status-tracking table across all jobs (HR can edit info here)."""
    return render_template("tracking.html", stages=STAGE_LIST)


@app.get("/api/jobs")
def api_jobs():
    with Database(cfg) as db:
        return jsonify(db.list_jobs_with_counts())


# -- SEEK scraping (background subprocess) ---------------------------------
@app.post("/api/scrape/fetch-jobs")
def api_scrape_fetch_jobs():
    """Start fetching the active job ads from SEEK (logs in if needed)."""
    started = scraper_mgr.fetch_jobs()
    return jsonify({"ok": True, "started": started, "running": scraper_mgr.is_running()})


@app.post("/api/scrape/download")
def api_scrape_download():
    """Start downloading candidates + resumes for one selected job id."""
    data = request.get_json(force=True)
    job_id = str(data.get("job_id", "")).strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id required"}), 400
    started = scraper_mgr.download(job_id)
    return jsonify({"ok": True, "started": started, "running": scraper_mgr.is_running()})


@app.get("/api/scrape/status")
def api_scrape_status():
    return jsonify(scraper_mgr.snapshot())


@app.get("/api/tracking")
def api_tracking():
    job_id = request.args.get("job_id", "").strip()
    stage = request.args.get("stage", "").strip()
    q = request.args.get("q", "").strip()
    with Database(cfg) as db:
        return jsonify(db.list_all_candidates(job_id, stage, q))


@app.post("/api/candidates/stage")
def api_candidate_stage():
    """Advance one candidate one step forward in the pipeline (server-enforced)."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    stage = str(data.get("stage", "")).strip()
    date = (data.get("date") or "").strip() or None
    if not aid or not stage:
        return jsonify({"ok": False, "error": "application_id and stage required"}), 400
    with Database(cfg) as db:
        result = db.set_stage(aid, stage, date)
    return jsonify(result), (200 if result.get("ok") else 400)


@app.get("/api/candidates")
def api_candidates():
    job_id = request.args.get("job_id", "").strip()
    q = request.args.get("q", "").strip()
    if not job_id:
        return jsonify([])
    with Database(cfg) as db:
        return jsonify(db.list_candidates(job_id, q))


@app.post("/api/candidates/summary")
def api_candidate_summary():
    """Return the ChatGPT resume summary for one candidate, generating + caching
    it on first request. Used by the pipeline board's "Sent Exam" stage.

    Body: {application_id, force?}. Pass force=true to regenerate."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    force = bool(data.get("force"))
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        info = db.get_summary_inputs(aid)
        if not info:
            return jsonify({"ok": False, "error": "Candidate not found."}), 404
        # Return the cached summary unless a regenerate was explicitly requested.
        if info.get("ai_summary") and not force:
            return jsonify({"ok": True, "summary": info["ai_summary"], "cached": True})
        if not info.get("resume_downloaded") or not info.get("resume_path"):
            return jsonify({"ok": False, "error": "No resume on file to summarise."}), 400
        try:
            summary = summarize_resume(
                info["resume_path"],
                candidate_name=info.get("full_name_edit") or info.get("full_name_jobdb") or "",
                job_title=info.get("job_title") or "")
        except SummaryError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
        db.save_ai_summary(aid, summary)
    return jsonify({"ok": True, "summary": summary, "cached": False})


@app.post("/api/candidates/extract")
def api_candidate_extract():
    """Return the ChatGPT structured résumé extraction (full_name, university,
    major) for one candidate, generating + caching it on first request. Mirrors
    /api/candidates/summary and is auto-triggered when a candidate enters the
    "Wait Pre-screen" stage.

    full_name overwrites full_name_edit server-side; university/major are returned
    as suggestions for the card inputs (saved only when HR saves the candidate).

    Body: {application_id, force?}. Pass force=true to regenerate."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    force = bool(data.get("force"))
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        info = db.get_summary_inputs(aid)
        if not info:
            return jsonify({"ok": False, "error": "Candidate not found."}), 404
        # Return the cached extraction unless a regenerate was explicitly requested.
        if info.get("ai_extract_json") and not force:
            try:
                cached = json.loads(info["ai_extract_json"])
            except ValueError:
                cached = {}
            return jsonify({"ok": True, "extract": cached, "cached": True})
        if not info.get("resume_downloaded") or not info.get("resume_path"):
            return jsonify({"ok": False, "error": "No resume on file to extract."}), 400
        try:
            extract = extract_resume_fields(
                info["resume_path"],
                candidate_name=info.get("full_name_edit") or info.get("full_name_jobdb") or "",
                job_title=info.get("job_title") or "")
        except SummaryError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
        db.save_ai_extract(aid, json.dumps(extract, ensure_ascii=False),
                           full_name=extract.get("full_name"),
                           exp_total=extract.get("exp_total"),
                           exp_directly=extract.get("exp_directly"))
    return jsonify({"ok": True, "extract": extract, "cached": False})


def _utc_to_thai(iso_utc: str | None) -> str | None:
    """Graph UTC timestamp ('2026-06-08T03:21:00Z' / with fractions) → Thai-time
    'YYYY-MM-DD HH:MM:SS' string (UTC+7)."""
    if not iso_utc:
        return None
    s = iso_utc.replace("Z", "")[:19]
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return (dt + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")


@app.post("/api/candidates/check-reply")
def api_candidate_check_reply():
    """Detect whether a candidate replied to the exam email (reads the signed-in
    mailbox). On a reply, download the reply's attachments + the résumé into
    Email_Reply_Exam/<name>/. Cached in the DB unless force=true.
    Body: {application_id, force?}."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    force = bool(data.get("force"))
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        info = db.get_reply_inputs(aid)
        if not info:
            return jsonify({"ok": False, "error": "Candidate not found."}), 404
        # Nothing to reply to until an exam was actually sent.
        if not info.get("is_sent_exam") or not info.get("exam_sent_at"):
            return jsonify({"ok": True, "replied": False, "at": None,
                            "subject": None, "cached": True, "skipped": True})
        if not info.get("email"):
            return jsonify({"ok": False, "error": "Candidate has no email address."}), 400
        # Cached result unless an explicit recheck was requested.
        if info.get("reply_received") is not None and not force:
            return jsonify({"ok": True, "replied": bool(info["reply_received"]),
                            "at": info.get("reply_at"), "subject": info.get("reply_subject"),
                            "cached": True})

        # exam_sent_at is naive Thai (UTC+7) → UTC floor for the Graph filter.
        since_utc = (info["exam_sent_at"] - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            mailer = GraphMailer(cfg)
            hit = mailer.check_reply(info["email"], since_utc)
        except MailerError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

        replied = hit is not None
        reply_at = _utc_to_thai(hit["at"]) if hit else None
        subject = hit["subject"] if hit else None
        saved = 0
        if replied:
            # Download the reply's attachments and save them + the résumé to OneDrive.
            try:
                atts = mailer.download_attachments(hit["id"]) if hit.get("hasAttachments") else []
            except MailerError as exc:
                logging.warning("Reply attachment download failed for %s: %s", aid, exc)
                atts = []
            name = info.get("full_name_edit") or info.get("full_name_jobdb") or aid
            try:
                _, saved = shortlist.build_reply_folder(
                    cfg.reply_exam_dir, name, info.get("resume_path"), atts,
                    email_name=_user_prefix())
            except OSError as exc:
                logging.warning("Reply folder build failed for %s: %s", aid, exc)
        db.save_reply_status(aid, replied, reply_at, subject)
    return jsonify({"ok": True, "replied": replied, "at": reply_at,
                    "subject": subject, "files_saved": saved, "cached": False})


@app.post("/api/candidates/summary/save")
def api_candidate_summary_save():
    """Save an HR-edited (or manually written) résumé summary for one candidate."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    summary = (data.get("summary") or "").strip()
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        if not db.get_summary_inputs(aid):
            return jsonify({"ok": False, "error": "Candidate not found."}), 404
        db.save_ai_summary(aid, summary)
    return jsonify({"ok": True})


@app.get("/resume/<application_id>")
def serve_resume(application_id: str):
    """Serve a candidate's resume PDF inline (so the viewer can embed it)."""
    with Database(cfg) as db:
        path = db.get_resume_path(application_id)
    if not path:
        abort(404)
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="application/pdf")


@app.post("/api/jobs/active")
def api_job_active():
    data = request.get_json(force=True)
    with Database(cfg) as db:
        db.set_job_active(str(data["job_id"]), bool(data["is_active"]))
    return jsonify({"ok": True})


@app.post("/api/candidates/update")
def api_candidate_update():
    """Save the editable fields (editable name, email, phone) for one candidate."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        db.update_candidate(
            aid,
            (data.get("full_name_edit") or "").strip(),
            (data.get("email") or "").strip(),
            (data.get("phone") or "").strip(),
            (data.get("nickname") or "").strip(),
            (data.get("name_title") or "").strip(),
            (data.get("university") or "").strip(),
            (data.get("major") or "").strip(),
            (data.get("remark") or "").strip(),
            exp_total=(data.get("exp_total") or "").strip(),
            exp_directly=(data.get("exp_directly") or "").strip(),
        )
    return jsonify({"ok": True})


# -- Email template config (Config Email Template panel) ------------------------
@app.get("/api/email-templates")
def api_email_templates_get():
    return jsonify({"templates": load_templates()})


@app.get("/api/email-templates/fields")
def api_email_template_fields():
    """Placeholder fields HR can insert into a template: every applicants column
    (read live from the DB, so new columns appear automatically) plus the derived
    name/company/deadline fields. Powers the insert-field chips."""
    derived = ["full_name_edit", "title", "title_name", "firstname",
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


@app.post("/api/email-templates")
def api_email_template_save():
    data = request.get_json(force=True)
    saved = save_template(data)
    return jsonify({"ok": True, "template": saved, "templates": load_templates()})


@app.post("/api/email-templates/delete")
def api_email_template_delete():
    data = request.get_json(force=True)
    templates = delete_template(str(data.get("id", "")))
    return jsonify({"ok": True, "templates": templates})


@app.get("/api/email/login-status")
def api_email_login_status():
    """Sign-in snapshot for the shared Recruit mailbox."""
    return jsonify(email_auth.snapshot())


@app.post("/api/email/login-start")
def api_email_login_start():
    """Begin the device-code sign-in for the shared Recruit mailbox."""
    email_auth.start()
    return jsonify(email_auth.snapshot())


@app.get("/api/email/settings")
def api_email_settings_get():
    """Per-machine Config-Email settings (currently the teammate folder prefix)."""
    return jsonify({"user_prefix": _user_prefix()})


@app.post("/api/email/settings")
def api_email_settings_save():
    """Save the teammate folder prefix (e.g. 'Na'). Stored per-machine."""
    data = request.get_json(force=True)
    prefix = str(data.get("user_prefix", "") or "").strip()
    saved = save_settings({"user_prefix": prefix})
    return jsonify({"ok": True, "user_prefix": saved.get("user_prefix", "")})


def _format_deadline(raw: str) -> str:
    """Turn a datetime-local value ('2026-04-20T23:59') into e.g.
    '20 April 2026, before 11.59 pm'. Falls back to the raw string if unparseable."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return raw
    hour12 = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    return f"{dt.day} {dt.strftime('%B')} {dt.year}, before {hour12}.{dt.minute:02d} {ampm}"


@app.post("/api/candidates/send-exam")
def api_candidate_send_exam():
    """Send the interview/exam email to ONE candidate, then advance them to
    'Sent Exam'. The stage advances ONLY if the email actually sent."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    deadline = _format_deadline(data.get("deadline", ""))

    with Database(cfg) as db:
        cand = db.get_candidate(aid)
        if not cand:
            return jsonify({"ok": False, "error": "Candidate not found."}), 404
        if not cand.get("email"):
            return jsonify({"ok": False, "error":
                            "This candidate has no email. Add one on the tracking page."}), 400
        template_id = str(data.get("template_id", "")).strip()
        tmpl = get_template(template_id) if template_id else None
        if tmpl is None:
            exam_tmpls = [t for t in load_templates() if t.get("type") != "shortlist"]
            tmpl = exam_tmpls[0] if exam_tmpls else None
        if tmpl is None:
            return jsonify({"ok": False, "error": "No email template configured."}), 400
        subject, body = render(
            tmpl, cand=db.get_candidate_fields(aid) or cand, deadline=deadline)
        prefix = _user_prefix()
        try:
            GraphMailer(cfg).send(cand["email"], subject, body,
                                  attachment_paths=tmpl.get("attachments", []),
                                  is_html=bool(tmpl.get("is_html")),
                                  cc_emails=tmpl.get("cc", []),
                                  sent_folder=f"{prefix}_Sent_Exam" if prefix else None)
        except MailerError as exc:
            # Block the move — surface the error so HR can fix and retry.
            return jsonify({"ok": False, "error": str(exc)}), 400
        # Sent OK → advance to Sent Exam (backfills is_sent_exam + exam_sent_at).
        res = db.set_stage(aid, "sent_exam", None)
        # Pre-create the candidate's Email_Reply_Exam folder (résumé inside) so the
        # reply files have a home when they answer.
        try:
            name = (cand.get("full_name_edit") or cand.get("full_name_jobdb") or aid)
            rp = db.get_resume_path(aid)
            folder, _ = shortlist.build_reply_folder(
                cfg.reply_exam_dir, name, rp, [], email_name=prefix)
            logging.info("Reply folder ready: %s", folder)
        except Exception as exc:  # noqa: BLE001 — folder is best-effort, never block the send
            logging.warning("Could not pre-create reply folder for %s: %s", aid, exc)
    if not res.get("ok"):
        return jsonify({"ok": False, "error":
                        "Email sent, but stage move was rejected: " + res.get("error", "")}), 409
    return jsonify({"ok": True, "stage_label": res.get("stage_label", "Sent Exam")})


@app.post("/api/candidates/interview-event")
def api_candidate_interview_event():
    """Create an interview calendar-event draft (on the signed-in user's calendar)
    for one candidate. Body: {application_id, template_id?}. The event has a
    placeholder time (tomorrow 14:00–15:00 Thai) and no attendees — HR edits the
    time, adds attendees, and sends from Outlook."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        cand = db.get_candidate_fields(aid)
    if not cand:
        return jsonify({"ok": False, "error": "Candidate not found."}), 404

    template_id = str(data.get("template_id", "")).strip()
    tmpl = get_template(template_id) if template_id else None
    if tmpl is None or tmpl.get("type") != "interview":
        tmpl = next((t for t in load_templates() if t.get("type") == "interview"), None)
    if tmpl is None:
        return jsonify({"ok": False, "error": "No interview template configured."}), 400

    subject, body = render_interview(tmpl, cand=cand)
    # Placeholder slot (HR edits it): tomorrow 14:00–15:00, Thailand local time.
    day = (datetime.now() + timedelta(days=1)).date()
    start = f"{day}T14:00:00"
    end = f"{day}T15:00:00"
    try:
        GraphMailer(cfg).create_event(
            subject, body, start, end, location="Online conference via MS Team")
    except MailerError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify({"ok": True})


@app.post("/api/candidates/evaluation")
def api_candidate_evaluation():
    """Move a candidate to 'Evaluation', generate the interview-evaluation Excel form
    (filled from the popup), create an Outlook DRAFT email (form attached, manual PDF
    linked), and SAVE the form fields on the candidate record. Body: {application_id,
    position, role, company, department, section, interview_date, interviewer,
    recruiter}. The stage advances only if the form + draft are produced successfully."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400

    def _g(k):
        return str(data.get(k, "") or "").strip()

    with Database(cfg) as db:
        cand = db.get_candidate(aid)
        if not cand:
            return jsonify({"ok": False, "error": "Candidate not found."}), 404
        name_edit = cand.get("full_name_edit") or cand.get("full_name_jobdb") or ""
        title = cand.get("name_title") or ""
        prefix_name = (f"{title} {name_edit}".strip() if title else name_edit)
        section, interview_date = _g("section"), _g("interview_date")
        position, role, interviewer = _g("position"), _g("role"), _g("interviewer")

        # 1) generate the filled Excel form (preserves the template's images/layout).
        values = {
            "full_name": prefix_name, "position": position, "role": role,
            "company": _g("company"), "department": _g("department"),
            "section": section, "interview_date": interview_date,
            "interviewer": interviewer, "recruiter": _g("recruiter"),
        }
        out_name = evaluation.eval_filename(section, name_edit)
        out_path = evaluation.EVAL_DIR / out_name
        try:
            evaluation.build_eval_xlsx(values, out_path)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"Could not build the Excel form: {exc}"}), 500

        # 2) create the Outlook draft (form attached, manual PDF linked). To is left
        #    blank so HR picks the interviewer's address in Outlook before sending.
        subject, body = evaluation.build_eval_email(
            prefix_name=prefix_name, interviewer=interviewer, position=position, role=role)
        _prefix = _user_prefix()
        try:
            GraphMailer(cfg).create_draft(
                subject, body, is_html=True, attachment_paths=[str(out_path)],
                folder=f"{_prefix}_Drafts" if _prefix else None)
        except MailerError as exc:
            return jsonify({"ok": False, "error":
                            f"Form created ({out_name}), but draft email failed: {exc}"}), 400

        # 3) persist the evaluation form fields on the candidate record.
        db.save_evaluation(
            aid, position=position, role=role, company=_g("company"),
            department=_g("department"), section=section,
            interview_date=interview_date or None, interviewer=interviewer,
            recruiter_name=_g("recruiter"))

        # 4) advance to Evaluation (records evaluation_date = the interview date picked).
        res = db.set_stage(aid, "evaluation", interview_date or None)
    if not res.get("ok"):
        return jsonify({"ok": False, "error":
                        "Form + draft created, but stage move was rejected: "
                        + res.get("error", "")}), 409
    return jsonify({"ok": True, "stage_label": res.get("stage_label", "Evaluation"),
                    "file": out_name})


@app.post("/api/candidates/offer-experience")
def api_candidate_offer_experience():
    """Return the ChatGPT experience blurb for the offer popup's Experience field,
    generating + caching it (in offer_experience) on first request. Prefers the
    first-step résumé summary; falls back to re-reading the résumé PDF.
    Body: {application_id, force?}. force=true regenerates."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    force = bool(data.get("force"))
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        info = db.get_experience_inputs(aid)
        if not info:
            return jsonify({"ok": False, "error": "Candidate not found."}), 404
        # Return the cached value unless a regenerate was explicitly requested.
        if info.get("offer_experience_ai") and not force:
            return jsonify({"ok": True, "experience": info["offer_experience_ai"], "cached": True})
        if not (info.get("ai_summary") or
                (info.get("resume_downloaded") and info.get("resume_path"))):
            return jsonify({"ok": False, "error":
                            "No résumé summary or résumé on file to base the experience on."}), 400
        try:
            experience = summarize_experience(
                ai_summary=info.get("ai_summary") or "",
                resume_path=info.get("resume_path") or "",
                candidate_name=info.get("full_name_edit") or info.get("full_name_jobdb") or "",
                job_title=info.get("job_title") or "")
        except SummaryError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
        db.save_offer_experience(aid, experience)
    return jsonify({"ok": True, "experience": experience, "cached": False})


@app.post("/api/candidates/offer-headline")
def api_candidate_offer_headline():
    """Return the ChatGPT one-line experience HEADLINE for the offer popup's
    "Experience (headline)" field — "<Position> <Company> <duration>, …" — generating
    + caching it (in offer_experience) on first request. Prefers the résumé PDF;
    falls back to the first-step summary. Body: {application_id, force?}. force=true
    regenerates."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    force = bool(data.get("force"))
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        info = db.get_experience_inputs(aid)
        if not info:
            return jsonify({"ok": False, "error": "Candidate not found."}), 404
        # Return the cached headline unless a regenerate was explicitly requested.
        if info.get("offer_experience") and not force:
            return jsonify({"ok": True, "headline": info["offer_experience"], "cached": True})
        if not (info.get("ai_summary") or
                (info.get("resume_downloaded") and info.get("resume_path"))):
            return jsonify({"ok": False, "error":
                            "No résumé summary or résumé on file to base the experience on."}), 400
        try:
            headline = summarize_experience_headline(
                ai_summary=info.get("ai_summary") or "",
                resume_path=info.get("resume_path") or "",
                candidate_name=info.get("full_name_edit") or info.get("full_name_jobdb") or "",
                job_title=info.get("job_title") or "")
        except SummaryError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
        db.save_offer_headline(aid, headline)
    return jsonify({"ok": True, "headline": headline, "cached": False})


def _find_shortlist_link(mailer: GraphMailer, shortlists_base: str,
                         candidate_name: str) -> str:
    """Locate the candidate's shortlist subfolder under `shortlists_base`
    (Candidate_JobDB_Scraping/Shortlists/<job>_dd_mm_yyyy/<name>) and return an
    organization share link for it. "" if no matching subfolder is found."""
    sub = shortlist.subfolder_name(candidate_name)
    for item in mailer.list_folder(shortlists_base):
        if not item.get("folder"):
            continue
        path = f"{shortlists_base}/{item.get('name', '')}/{sub}"
        if mailer.path_exists(path):
            return mailer.create_share_link(path)
    return ""


@app.post("/api/candidates/offer")
def api_candidate_offer():
    """Move a candidate to 'Offered' and create an Outlook DRAFT of the job-offer
    confirmation email ("ขอยืนยันข้อมูลเสนอจ้างพนักงาน…"). Popup inputs (body) +
    DB fields (department/section/role/position/interviewer/AI summary) feed the
    template; the candidate's Shortlists folder share link is auto-discovered. The
    stage advances only if the draft is created. To is left blank for HR to fill."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400

    def _g(k):
        return str(data.get(k, "") or "").strip()

    with Database(cfg) as db:
        info = db.get_offer_inputs(aid)
        if not info:
            return jsonify({"ok": False, "error": "Candidate not found."}), 404
        name_edit = info.get("full_name_edit") or info.get("full_name_jobdb") or ""
        title = info.get("name_title") or ""
        prefix_name = (f"{title} {name_edit}".strip() if title else name_edit)
        position = info.get("position") or ""
        role = info.get("role") or info.get("job_title") or ""
        job_level = evaluation.POSITION_LEVEL.get(position, "")

        try:
            mailer = GraphMailer(cfg)
        except MailerError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        # Auto-find the OneDrive share link to this candidate's shortlist folder.
        link_url = ""
        try:
            link_url = _find_shortlist_link(mailer, cfg.shortlist_onedrive_dir, name_edit)
        except MailerError as exc:
            logging.warning("offer: shortlist link lookup failed: %s", exc)

        offer_tmpl = next((t for t in load_templates() if t.get("type") == "offer"), None)
        if offer_tmpl is None:
            return jsonify({"ok": False, "error": "No offer email template configured."}), 400

        subject, body = offer.build_offer_email(
            template=offer_tmpl,
            prefix_name=prefix_name, position=position, job_level=job_level, role=role,
            department=info.get("department") or "", section=info.get("section") or "",
            people_count=_g("people_count") or "1", offer_type=_g("offer_type") or "รับใหม่",
            new_replace_text=_g("new_replace_text"), supervisor=_g("supervisor"),
            buddy=_g("buddy"), expected_salary=_g("expected_salary"),
            current_salary=_g("current_salary") or "-", start_date=_g("start_date"),
            experience=_g("experience"), experience_detail=_g("experience_detail"),
            interviewer=_g("interviewer") or (info.get("interviewer") or ""),
            interviewer_comments=_g("interviewer_comments"),
            recruiter_comments=_g("recruiter_comments"),
            link_text=prefix_name or name_edit, link_url=link_url,
            today_str=datetime.now().strftime("%d %b %Y"))
        user_pfx = _user_prefix()
        try:
            mailer.create_draft(subject, body, is_html=bool(offer_tmpl.get("is_html", True)),
                                folder=f"{user_pfx}_Drafts" if user_pfx else None)
        except MailerError as exc:
            return jsonify({"ok": False, "error": f"Draft email failed: {exc}"}), 400

        # Persist the popup inputs on the candidate record (interviewer reuses its column).
        db.save_offer(
            aid, people_count=_g("people_count") or "1", offer_type=_g("offer_type") or "รับใหม่",
            new_replace_text=_g("new_replace_text"), supervisor=_g("supervisor"),
            buddy=_g("buddy"), expected_salary=_g("expected_salary"),
            current_salary=_g("current_salary") or "-", start_date=_g("start_date"),
            experience=_g("experience"), experience_ai=_g("experience_detail"),
            interviewer=_g("interviewer"),
            interviewer_comments=_g("interviewer_comments"),
            recruiter_comments=_g("recruiter_comments"))

        res = db.set_stage(aid, "offered", datetime.now().strftime("%Y-%m-%d"))
    if not res.get("ok"):
        return jsonify({"ok": False, "error":
                        "Draft created, but stage move was rejected: "
                        + res.get("error", "")}), 409
    return jsonify({"ok": True, "stage_label": res.get("stage_label", "Offered"),
                    "has_link": bool(link_url)})


@app.post("/api/candidates/shortlist-email")
def api_candidates_shortlist_email():
    """Build ONE group "shortlist" email listing the selected candidates (each with
    their AI résumé summary) and save it as a DRAFT in the signed-in HR mailbox.
    Body: {application_ids: [...], template_id?}."""
    data = request.get_json(force=True)
    ids = [str(x).strip() for x in (data.get("application_ids") or []) if str(x).strip()]
    if not ids:
        return jsonify({"ok": False, "error": "No candidates selected."}), 400

    with Database(cfg) as db:
        cands, job_title = [], ""
        for aid in ids:
            info = db.get_summary_inputs(aid)
            if not info:
                continue
            cands.append({
                "name": info.get("full_name_edit") or info.get("full_name_jobdb") or "",
                "title": info.get("name_title") or "",
                "summary": info.get("ai_summary") or "",
                "resume_path": info.get("resume_path") or "",
            })
            if not job_title:
                job_title = info.get("job_title") or ""
    if not cands:
        return jsonify({"ok": False, "error": "Selected candidates not found."}), 404

    # Pick the requested template if it's a shortlist one, else the first shortlist template.
    template_id = str(data.get("template_id", "")).strip()
    tmpl = get_template(template_id) if template_id else None
    if tmpl is None or tmpl.get("type") != "shortlist":
        tmpl = next((t for t in load_templates() if t.get("type") == "shortlist"), None)
    if tmpl is None:
        return jsonify({"ok": False, "error": "No shortlist email template configured."}), 400

    try:
        mailer = GraphMailer(cfg)
    except MailerError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    # Move each candidate's Email_Reply_Exam/<name> folder (résumé + reply files) into a
    # fresh Shortlists/<job_title>_dd_mm_yyyy[_N]/. Cloud-first via Graph so the share
    # link works immediately (no waiting for local→cloud sync); then remove the local
    # source (= a move). Falls back to a pure local move if Graph/Files is unavailable.
    drive_base = cfg.shortlist_onedrive_dir
    # Reply folders were named with the teammate prefix suffix at send time, so
    # locate them with the same suffix.
    email_name = _user_prefix()
    folder_name, link_url, copied = shortlist.folder_name_for(job_title), "", 0
    try:
        folder_name = shortlist.unique_folder_name(
            folder_name,
            lambda n: (Path(cfg.shortlist_dir) / n).exists() or mailer.path_exists(f"{drive_base}/{n}"))
        for c in cands:
            sub = shortlist.subfolder_name(c.get("name") or "")
            srcdir = shortlist.candidate_dir(cfg.reply_exam_dir, c.get("name") or "", email_name)
            if srcdir:                                   # upload the whole reply folder
                # Recurse so nested subfolders are preserved (not just top-level
                # files). rel keeps each file's path under the candidate subfolder,
                # using forward slashes for the Graph drive path.
                for f in sorted(srcdir.rglob("*")):
                    if f.is_file():
                        rel = f.relative_to(srcdir).as_posix()
                        mailer.upload_file(f"{drive_base}/{folder_name}/{sub}/{rel}", f.read_bytes())
                shortlist.remove_dir(srcdir)             # source removed → moved
                copied += 1
            else:                                        # no reply folder → résumé only
                src = shortlist.resolve_resume(c.get("resume_path"))
                if src:
                    mailer.upload_file(
                        f"{drive_base}/{folder_name}/{sub}/{sub}{src.suffix or '.pdf'}", src.read_bytes())
                copied += 1
        link_url = mailer.create_share_link(f"{drive_base}/{folder_name}")
    except MailerError as exc:
        # Files not granted / Graph error → local move, link falls back to the folder name.
        logging.warning("OneDrive shortlist upload failed; using local move: %s", exc)
        try:
            folder_name, _, copied = shortlist.move_to_shortlist(
                cfg.shortlist_dir, cfg.reply_exam_dir, job_title, cands, email_name=email_name)
        except OSError as exc2:
            logging.warning("Local shortlist move also failed: %s", exc2)
        link_url = ""

    # link_text = folder name (shown), link_url = OneDrive share link (clickable; "" if none)
    subject, body = render_group(tmpl, job_title=job_title, candidates=cands,
                                 link_text=folder_name, link_url=link_url)
    try:
        me = mailer.signed_in_account()       # draft addressed to the HR mailbox itself
        mailer.create_draft(subject, body,
                            to_recipients=[me] if me else None,
                            is_html=bool(tmpl.get("is_html")),
                            folder=f"{email_name}_Drafts" if email_name else None)
    except MailerError as exc:
        return jsonify({"ok": False, "error": str(exc),
                        "folder": folder_name, "copied": copied}), 400
    return jsonify({"ok": True, "count": len(cands), "folder": folder_name,
                    "moved": copied, "link": link_url or folder_name,
                    "has_link": bool(link_url)})


if __name__ == "__main__":
    # Ensure DB + schema (adds the new columns) before serving.
    ensure_database(cfg)
    ensure_schema(cfg)
    app.run(host="127.0.0.1", port=2757, debug=False)
