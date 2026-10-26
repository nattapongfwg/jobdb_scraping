"""Candidate cards on the pipeline board: listing, moving, editing, AI résumé
summaries, the résumé PDF, exam emails, interview events and the shortlist email."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, abort, jsonify, request, send_file

from config import PROJECT_ROOT
from controllers.common import cfg, current_email, sender_prefix, user_prefix
from models.db import STAGE_LABELS, Database
from services import shortlist
from services.email_kit.templates import (get_template, load_templates, render, render_group,
                                          render_interview)
from services.mailer import GraphMailer, MailerError
from services.summarizer import SummaryError, extract_resume_fields, summarize_resume
from utils import phone

bp = Blueprint("candidates", __name__)

# Only "Add Candidate" uploads a file (a PDF résumé); webapp caps request bodies to this.
MAX_RESUME_MB = 20


@bp.post("/api/candidates/stage")
def api_candidate_stage():
    """Advance one candidate one step forward in the pipeline (server-enforced)."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    stage = str(data.get("stage", "")).strip()
    date = (data.get("date") or "").strip() or None
    if not aid or not stage:
        return jsonify({"ok": False, "error": "application_id and stage required"}), 400
    with Database(cfg) as db:
        result = db.set_stage(aid, stage, date, actor=current_email())
    return jsonify(result), (200 if result.get("ok") else 400)


@bp.post("/api/candidates/owner")
def api_candidate_owner():
    """Reassign the recruiter responsible for a candidate (owner badge).
    Body: {application_id, owner_email} — blank owner_email clears it."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    owner = str(data.get("owner_email", "") or "").strip()
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        if owner and not db.get_user_by_email(owner):
            return jsonify({"ok": False, "error": "Unknown user."}), 400
        res = db.set_owner(aid, owner, actor=current_email())
    return jsonify(res), (200 if res.get("ok") else 404)


@bp.post("/api/candidates/stop")
def api_candidate_stop():
    """Set or clear the "Stop Here!" mark (red card border).
    Body: {application_id, stopped: true|false}."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        res = db.set_stopped(aid, bool(data.get("stopped")), actor=current_email())
    return jsonify(res), (200 if res.get("ok") else 400)


@bp.get("/api/candidates/history")
def api_candidate_history():
    """Who moved / emailed / reassigned this candidate, newest first."""
    aid = request.args.get("application_id", "").strip()
    if not aid:
        return jsonify([])
    with Database(cfg) as db:
        return jsonify(db.list_history(aid))


@bp.get("/api/candidates")
def api_candidates():
    job_id = request.args.get("job_id", "").strip()
    q = request.args.get("q", "").strip()
    if not job_id:
        return jsonify([])
    with Database(cfg) as db:
        return jsonify(db.list_candidates(job_id, q))


@bp.post("/api/candidates/summary")
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


@bp.post("/api/candidates/extract")
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


@bp.post("/api/candidates/check-reply")
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
        # Look in the SENDER's mail + reply folders: a teammate may have sent this
        # exam under their own prefix. Rows from before exam_sent_by existed were
        # sent from this machine, so they fall back to our prefix.
        prefix = sender_prefix(info)
        try:
            mailer = GraphMailer(cfg)
            hit = mailer.check_reply(info["email"], since_utc,
                                     sent_folder=f"{prefix}_Sent_Exam" if prefix else None)
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
                    email_name=prefix)
            except OSError as exc:
                logging.warning("Reply folder build failed for %s: %s", aid, exc)
        db.save_reply_status(aid, replied, reply_at, subject)
    return jsonify({"ok": True, "replied": replied, "at": reply_at,
                    "subject": subject, "files_saved": saved, "cached": False})


@bp.post("/api/candidates/summary/save")
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


@bp.get("/resume/<application_id>")
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


# Mirrors scraper._safe_filename (kept here so the web app never imports Playwright):
# keep Thai/Unicode letters, swap filesystem-unfriendly characters for "_".
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_EMAIL_OK = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _format_phone(value: str) -> str | None:
    """Add Candidate phone → 'xxx-xxx-xxxx' (phone.normalize, mobile rule)."""
    return phone.normalize(value, "mobile")


def _safe_filename(value: str, fallback: str) -> str:
    cleaned = _ILLEGAL_FS.sub("_", (value or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    return cleaned[:120] or fallback


@bp.app_errorhandler(413)
def _request_too_large(_e):
    return jsonify({"ok": False,
                    "error": f"File too large — the résumé must be under {MAX_RESUME_MB} MB."}), 413


@bp.post("/api/candidates/add")
def api_candidate_add():
    """"Add Candidate" on the pipeline page: a recruiter (Admin or HR) types a candidate
    in by hand. Multipart form with the Pending-card fields plus an optional PDF résumé.
    The row lands in **Pending** for that job, tagged source='manual', and an 'added'
    history row names who typed it in. Answers 409 with the look-alike row when the
    same e-mail or name is already in the job — resend with force=1 to add anyway."""
    f = request.form
    job_id = (f.get("job_id") or "").strip()
    full_name = re.sub(r"\s+", " ", f.get("full_name") or "").strip()
    email = (f.get("email") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id required"}), 400
    if not full_name:
        return jsonify({"ok": False, "error": "Full name is required."}), 400
    if email and not _EMAIL_OK.fullmatch(email):
        return jsonify({"ok": False, "error": "That e-mail address doesn't look right."}), 400
    cand_phone = _format_phone(f.get("phone") or "")
    if cand_phone is None:
        return jsonify({"ok": False, "error": "Phone must be 10 digits (xxx-xxx-xxxx)."}), 400
    up = request.files.get("resume")
    pdf_bytes = b""
    if up is not None and up.filename:
        pdf_bytes = up.read()
        if not up.filename.lower().endswith(".pdf") or not pdf_bytes.startswith(b"%PDF"):
            return jsonify({"ok": False, "error": "The résumé must be a PDF file."}), 400
    with Database(cfg) as db:
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found."}), 404
        if not f.get("force"):
            dup = db.find_duplicate_candidate(job_id, full_name, email)
            if dup:
                return jsonify({"ok": False, "duplicate": dup,
                                "error": f"{dup['name']} is already in this job "
                                         f"({dup['stage_label']})."}), 409
        aid = db.add_manual_candidate(
            job_id, full_name=full_name,
            name_title=(f.get("name_title") or "").strip()[:10],
            nickname=(f.get("nickname") or "").strip()[:100],
            email=email[:300], phone=cand_phone,
            remark=(f.get("remark") or "").strip(),
            current_salary=(f.get("current_salary") or "").strip()[:100],
            minimum_expect_salary=(f.get("minimum_expect_salary") or "").strip()[:100],
            expect_salary=(f.get("expect_salary") or "").strip()[:100],
            actor=current_email())
        if pdf_bytes:
            # Same layout the scraper uses: resume/<job title>/<name>_<application_id>.pdf
            sub = _safe_filename(job.get("title") or "", "")
            folder = cfg.resume_dir / sub if sub else cfg.resume_dir
            dest = folder / f"{_safe_filename(full_name, 'candidate')}_{aid}.pdf"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(pdf_bytes)
            except OSError as e:
                db.delete_candidate(aid)   # roll back: no half-added card without its file
                logging.exception("manual add: could not save résumé")
                return jsonify({"ok": False, "error": f"Could not save the résumé: {e}"}), 500
            db.set_resume(aid, dest.name, str(dest))
    logging.info("Manual candidate %s added to job %s by %s", aid, job_id, current_email())
    return jsonify({"ok": True, "application_id": aid, "full_name": full_name,
                    "resume": bool(pdf_bytes)})


@bp.post("/api/candidates/update")
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
            current_salary_edit=(data.get("current_salary_edit") or "").strip(),
            minimum_expect_salary_edit=(data.get("minimum_expect_salary_edit") or "").strip(),
            expect_salary_edit=(data.get("expect_salary_edit") or "").strip(),
            interview_date=(data.get("interview_date") or "").strip(),
        )
    return jsonify({"ok": True})


@bp.post("/api/candidates/request-fields")
def api_candidate_request_fields():
    """Record the Position/Role/Company/Department/Section from the hiring request
    chosen when a candidate moves Pending -> Wait Pre-screen."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        db.set_request_fields(
            aid,
            request_id=data.get("request_id"),
            position=(data.get("position") or "").strip(),
            role=(data.get("role") or "").strip(),
            company=(data.get("company") or "").strip(),
            department=(data.get("department") or "").strip(),
            section=(data.get("section") or "").strip(),
        )
    return jsonify({"ok": True})


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


@bp.post("/api/candidates/send-exam")
def api_candidate_send_exam():
    """Send the interview/exam email to ONE candidate and advance them to
    'Sent Exam'. The card is claimed (moved + sender recorded) BEFORE the email
    goes out so a double click or a second teammate gets "already sent" instead
    of a second email; if the send then fails the claim is undone so HR can retry.
    Body: {application_id, deadline, template_id?, resend?}. resend=true is HR's
    confirmation to email a candidate who already received the exam."""
    data = request.get_json(force=True)
    aid = str(data.get("application_id", "")).strip()
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    deadline = _format_deadline(data.get("deadline", ""))
    resend = bool(data.get("resend"))

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
        prefix = user_prefix()
        claim = db.claim_exam_send(aid, prefix, resend=resend, actor=current_email())
        if not claim.get("ok"):
            return jsonify(claim), 409
        try:
            GraphMailer(cfg).send(cand["email"], subject, body,
                                  attachment_paths=tmpl.get("attachments", []),
                                  is_html=bool(tmpl.get("is_html")),
                                  cc_emails=tmpl.get("cc", []),
                                  sent_folder=f"{prefix}_Sent_Exam" if prefix else None)
        except MailerError as exc:
            # Release the claim and surface the error so HR can fix and retry.
            db.undo_exam_claim(aid, claim["prev"], actor=current_email())
            return jsonify({"ok": False, "error": str(exc)}), 400
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
    return jsonify({"ok": True, "stage_label": STAGE_LABELS["sent_exam"]})


@bp.post("/api/candidates/interview-event")
def api_candidate_interview_event():
    """Create an interview calendar-event draft (on the signed-in user's calendar)
    for one candidate. Body: {application_id, template_id?}. The event has a
    placeholder time (tomorrow 14:00–15:00 Thai) and no attendees — HR edits the
    time, adds attendees, and sends from Outlook. The default optional-attendee
    addresses (3 internal + the candidate's email) are listed in the event body
    for quick copy, since Graph can't pre-fill attendees without sending."""
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
    # Default optional attendees. Graph can't pre-fill attendees on a calendar
    # event without firing the invite immediately (sendInvitationMessage isn't
    # supported), so to keep this a true no-invite draft we surface the addresses
    # in the body — HR copies them into the Optional line in Outlook, then sends.
    optional_attendees = [
        "Tanakrit_Jai@FreewillSolutions.com",
        "Suttharinthon_tap@freewillsolutions.com",
        "Nattapong_Yuw@freewillsolutions.com",
    ]
    cand_email = (cand.get("email") or "").strip()
    if cand_email:
        optional_attendees.append(cand_email)
    body += ('<p style="margin:16px 0 0;color:#666;font-size:12px">'
             '<b>Optional attendees</b> (add in Outlook before sending):<br>'
             + "; ".join(optional_attendees) + '</p>')
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


@bp.post("/api/candidates/shortlist-email")
def api_candidates_shortlist_email():
    """Build ONE group "shortlist" email listing the selected candidates (each with
    their AI résumé summary) and save it as a DRAFT in the signed-in HR mailbox.
    Body: {application_ids: [...], template_id?}."""
    data = request.get_json(force=True)
    ids = [str(x).strip() for x in (data.get("application_ids") or []) if str(x).strip()]
    if not ids:
        return jsonify({"ok": False, "error": "No candidates selected."}), 400

    # Tag the shortlist folder with the teammate prefix (suffix) so two teammates
    # shortlisting the same job on the same day get distinct folders. Blank prefix
    # keeps the plain <job>_dd_mm_yyyy name.
    email_name = user_prefix()
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
                # Each reply folder carries the suffix of whoever SENT that exam.
                "email_name": sender_prefix(info),
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
    folder_name, link_url, copied = shortlist.folder_name_for(job_title, prefix=email_name), "", 0
    try:
        folder_name = shortlist.unique_folder_name(
            folder_name,
            lambda n: (Path(cfg.shortlist_dir) / n).exists() or mailer.path_exists(f"{drive_base}/{n}"))
        for c in cands:
            sub = shortlist.subfolder_name(c.get("name") or "")
            srcdir = shortlist.candidate_dir(cfg.reply_exam_dir, c.get("name") or "",
                                             c.get("email_name", email_name))
            if srcdir:                                   # upload the whole reply folder
                # Recurse so nested subfolders are preserved (not just top-level
                # files). rel keeps each file's path under the candidate subfolder,
                # using forward slashes for the Graph drive path. read_bytes_retry
                # rides out OneDrive online-only/locked files so one unreadable file
                # doesn't abort the batch (which left an incomplete folder before);
                # a real Graph/permission error still raises MailerError → local move.
                files = [f for f in sorted(srcdir.rglob("*")) if f.is_file()]
                uploaded = 0
                for f in files:
                    rel = f.relative_to(srcdir).as_posix()
                    mailer.upload_file(f"{drive_base}/{folder_name}/{sub}/{rel}",
                                       shortlist.read_bytes_retry(f))
                    uploaded += 1
                if files and uploaded == len(files):
                    shortlist.remove_dir(srcdir)         # all files safely up → move
                else:
                    logging.warning("shortlist: uploaded %d/%d files for %s; keeping "
                                    "source folder %s", uploaded, len(files), sub, srcdir)
                copied += 1
            else:                                        # no reply folder → résumé only
                src = shortlist.resolve_resume(c.get("resume_path"))
                if src:
                    mailer.upload_file(
                        f"{drive_base}/{folder_name}/{sub}/{sub}{src.suffix or '.pdf'}",
                        shortlist.read_bytes_retry(src))
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
