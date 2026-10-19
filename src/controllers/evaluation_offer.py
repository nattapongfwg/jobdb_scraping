"""The Evaluation and Offered moves: their popups' live previews, the evaluation
Excel form + draft, the AI experience fields and the offer-confirmation draft."""
from __future__ import annotations

import logging
import re
from datetime import datetime

from flask import Blueprint, jsonify, request

from controllers.common import cfg, current_email, user_prefix
from models.db import Database
from services import evaluation, offer, shortlist
from services.email_kit.templates import load_templates
from services.mailer import GraphMailer, MailerError
from services.summarizer import (SummaryError, summarize_experience,
                                 summarize_experience_headline)

bp = Blueprint("evaluation_offer", __name__)


@bp.get("/api/evaluation/templates")
def api_evaluation_templates():
    """Team evaluation forms under files_evaluation/ for the popup's template dropdown."""
    return jsonify({"templates": evaluation.list_templates()})


def _candidate_names(rec: dict) -> tuple[str, str]:
    """(name_edit, prefix_name): the HR-edited name (else the scraped one) and the
    same with the honorific in front ("Mr. Jane Doe")."""
    name_edit = rec.get("full_name_edit") or rec.get("full_name_jobdb") or ""
    title = rec.get("name_title") or ""
    return name_edit, (f"{title} {name_edit}".strip() if title else name_edit)


def _eval_values(cand: dict, data: dict) -> dict:
    """The 9 Evaluation-form values (CELL_MAP keys) from the candidate record + the
    popup body. Shared by the real move and its live preview so both always build
    the same Excel form and draft email."""
    def _g(k):
        return str(data.get(k, "") or "").strip()
    return {"full_name": _candidate_names(cand)[1], "position": _g("position"),
            "role": _g("role"), "company": _g("company"), "department": _g("department"),
            "section": _g("section"), "interview_date": _g("interview_date"),
            "interviewer": _g("interviewer"), "recruiter": _g("recruiter")}


def _offer_email_args(info: dict, data: dict) -> dict:
    """build_offer_email arguments from the candidate record (get_offer_inputs) + the
    popup body — every argument except template / link_url / wrap. Shared by the real
    move and its live preview so both always build the same email."""
    def _g(k):
        return str(data.get(k, "") or "").strip()
    name_edit, prefix_name = _candidate_names(info)
    position = info.get("position") or ""
    return dict(
        prefix_name=prefix_name, position=position,
        job_level=evaluation.POSITION_LEVEL.get(position, ""),
        role=info.get("role") or info.get("job_title") or "",
        department=info.get("department") or "", section=info.get("section") or "",
        people_count=_g("people_count") or "1", offer_type=_g("offer_type") or "รับใหม่",
        new_replace_text=_g("new_replace_text"), supervisor=_g("supervisor"),
        buddy=_g("buddy"), expected_salary=_g("expected_salary"),
        current_salary=_g("current_salary") or "-", start_date=_g("start_date"),
        experience=_g("experience"), experience_detail=_g("experience_detail"),
        interviewer=_g("interviewer") or (info.get("interviewer") or ""),
        interviewer_comments=_g("interviewer_comments"),
        recruiter_comments=_g("recruiter_comments"),
        link_text=prefix_name or name_edit,
        today_str=datetime.now().strftime("%d %b %Y"))


def _preview_mark(key: str, html: str) -> str:
    """Live-preview marker for one filled value: data-f names its field so the popup
    can link it to the form and edit it in place. Block HTML (the experience bullet
    list, a signature) gets a <div>; inline values a <span>."""
    tag = "div" if re.search(r"<(?:ul|ol|p|div|table)\b", html or "", re.I) else "span"
    return f'<{tag} class="pv-f" data-f="{key}">{html}</{tag}>'


@bp.post("/api/candidates/evaluation/preview")
def api_candidate_evaluation_preview():
    """Live preview for the Evaluation popup: the Excel header cells, the file name and
    the draft email exactly as Confirm would build them from the current popup values
    (same body as /api/candidates/evaluation). Read-only: saves nothing, creates no
    file and no draft."""
    data = request.get_json(force=True) or {}
    aid = str(data.get("application_id", "")).strip()
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        cand = db.get_candidate(aid)
    if not cand:
        return jsonify({"ok": False, "error": "Candidate not found."}), 404
    values = _eval_values(cand, data)
    subject, body = evaluation.build_eval_email(
        prefix_name=values["full_name"], interviewer=values["interviewer"],
        position=values["position"], role=values["role"], wrap=_preview_mark)
    template_id = str(data.get("template", "") or "").strip()
    return jsonify({
        "ok": True, "subject_html": subject, "body_html": body,
        "file_html": evaluation.eval_filename(values["section"], _candidate_names(cand)[0],
                                              wrap=_preview_mark),
        "template": template_id or evaluation.TEMPLATE_PATH.name,
        "cells": [{"cell": cell, "key": key, "label": evaluation.CELL_LABELS.get(key, key),
                   "value": values.get(key, "")} for key, cell in evaluation.CELL_MAP.items()],
    })


@bp.post("/api/candidates/offer/preview")
def api_candidate_offer_preview():
    """Live preview for the Offer popup: the offer-confirmation draft exactly as
    Confirm would build it from the current popup values (same body as
    /api/candidates/offer). The Shortlists share link is looked up only on Confirm, so
    the preview shows the candidate's name unlinked. Read-only: saves nothing."""
    data = request.get_json(force=True) or {}
    aid = str(data.get("application_id", "")).strip()
    if not aid:
        return jsonify({"ok": False, "error": "application_id required"}), 400
    with Database(cfg) as db:
        info = db.get_offer_inputs(aid)
    if not info:
        return jsonify({"ok": False, "error": "Candidate not found."}), 404
    offer_tmpl = next((t for t in load_templates() if t.get("type") == "offer"), None)
    if offer_tmpl is None:
        return jsonify({"ok": False, "error": "No offer email template configured."}), 400
    subject, body = offer.build_offer_email(template=offer_tmpl, link_url="",
                                            wrap=_preview_mark, **_offer_email_args(info, data))
    return jsonify({"ok": True, "subject_html": subject, "body_html": body,
                    "is_html": bool(offer_tmpl.get("is_html", True))})


@bp.post("/api/candidates/evaluation")
def api_candidate_evaluation():
    """Move a candidate to 'Evaluation', generate the interview-evaluation Excel form
    (filled from the popup), create an Outlook DRAFT email (form attached, manual PDF
    linked), and SAVE the form fields on the candidate record. Body: {application_id,
    position, role, company, department, section, interview_date, interviewer,
    recruiter, template?}. `template` picks a team form under files_evaluation/
    (blank = the default template). The stage advances only if the form + draft are
    produced successfully."""
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
        name_edit, prefix_name = _candidate_names(cand)
        # 1) generate the filled Excel form (preserves the template's images/layout).
        #    _eval_values is shared with the popup's live preview.
        values = _eval_values(cand, data)
        section, interview_date = values["section"], values["interview_date"]
        position, role, interviewer = values["position"], values["role"], values["interviewer"]
        out_name = evaluation.eval_filename(section, name_edit)
        out_path = evaluation.EVAL_DIR / out_name
        try:
            # `template` = a file under files_evaluation/ picked in the popup; blank = default.
            template_path = evaluation.resolve_template(_g("template"))
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        try:
            evaluation.build_eval_xlsx(values, out_path, template_path=template_path)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"Could not build the Excel form: {exc}"}), 500

        # 2) create the Outlook draft (form attached, manual PDF linked). To is left
        #    blank so HR picks the interviewer's address in Outlook before sending.
        subject, body = evaluation.build_eval_email(
            prefix_name=prefix_name, interviewer=interviewer, position=position, role=role)
        _prefix = user_prefix()
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
            recruiter_name=_g("recruiter"), request_id=data.get("request_id"))

        # 4) advance to Evaluation (records evaluation_date = the interview date picked).
        res = db.set_stage(aid, "evaluation", interview_date or None, actor=current_email())
    if not res.get("ok"):
        return jsonify({"ok": False, "error":
                        "Form + draft created, but stage move was rejected: "
                        + res.get("error", "")}), 409
    return jsonify({"ok": True, "stage_label": res.get("stage_label", "Evaluation"),
                    "file": out_name})


@bp.post("/api/candidates/offer-experience")
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


@bp.post("/api/candidates/offer-headline")
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


@bp.post("/api/candidates/offer")
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
        name_edit = _candidate_names(info)[0]

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

        # _offer_email_args is shared with the popup's live preview.
        subject, body = offer.build_offer_email(template=offer_tmpl, link_url=link_url,
                                                **_offer_email_args(info, data))
        user_pfx = user_prefix()
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

        res = db.set_stage(aid, "offered", datetime.now().strftime("%Y-%m-%d"),
                           actor=current_email())
    if not res.get("ok"):
        return jsonify({"ok": False, "error":
                        "Draft created, but stage move was rejected: "
                        + res.get("error", "")}), 409
    return jsonify({"ok": True, "stage_label": res.get("stage_label", "Offered"),
                    "has_link": bool(link_url)})
