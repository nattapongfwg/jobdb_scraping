"""SQL Server access layer using pyodbc.

Responsibilities:
  - connect to the local Windows SQL Server (from WSL2),
  - create the target database if it does not exist,
  - apply the idempotent schema in schema.sql,
  - MERGE-upsert jobs and applicants so re-runs update instead of duplicating.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import pyodbc

from config import Config, load_config

log = logging.getLogger(__name__)

# Current time in Thailand (Asia/Bangkok, UTC+7) as a DATETIME2 — used for all
# scraped_at / exam_sent_at values so every table stores Thai local time.
THAI_NOW = "CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'SE Asia Standard Time' AS DATETIME2)"


def _years_or_none(v: Any) -> float | None:
    """Coerce an AI experience value ('4.50', 4.5, '') to a float for the
    DECIMAL(5,2) columns, or None when it isn't a plain number."""
    s = str(v if v is not None else "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def candidate_key(app: dict[str, Any]) -> str | None:
    """Stable per-candidate identity within a job, used for deduping across
    re-scrapes. SEEK's application_id (selected=<uuid>) is regenerated every
    scraping session, so it can't recognise a returning candidate — keying on it
    produced duplicate rows. Instead derive a key from stable application content:
    the normalized name + the application timestamp (both stable, and both
    readable from the card BEFORE clicking, so the fast pre-click skip can use it
    too). Returns None when there's nothing to key on.

    Accepts both scraper dicts ('full_name') and DB rows ('full_name_jobdb')."""
    name = app.get("full_name") or app.get("full_name_jobdb") or ""
    name = re.sub(r"\s+", " ", name).strip().lower()
    applied = (app.get("applied_at") or "").strip()
    if not name and not applied:
        return None
    return f"{name}|{applied}"

# Hiring pipeline stages, in strict forward order. A candidate may only advance
# one step at a time: Pending -> Wait Pre-screen -> Sent Exam -> Shortlist -> Interview -> Evaluation -> Offered.
# NOTE: the first stage keeps its original key "prescreen" (only the label changed
# to "Pending") so existing rows — all stored as 'prescreen' — stay valid with no
# data migration.
# Display order (sidebar, left→right): Not Interest is the off-ramp left of Pending.
# The forward flow prescreen→wait_prescreen→sent_exam→…→offered stays monotonic so the
# index-based milestone backfill below keeps working.
STAGES = ["not_interest", "prescreen", "wait_prescreen", "sent_exam", "shortlist", "interview", "evaluation", "offered"]
STAGE_LABELS = {
    "not_interest": "Not Interest",
    "prescreen": "Pending",
    "wait_prescreen": "Wait Pre-screen",
    "sent_exam": "Sent Exam",
    "shortlist": "Shortlist",
    "interview": "Interview",
    "evaluation": "Evaluation",
    "offered": "Offered",
}
# Allowed stage transitions (branching, not strictly linear):
#   Not Interest ← Pending → Wait Pre-screen → Sent Exam → Shortlist → Interview → Evaluation → Offered
# Pending advances to Wait Pre-screen (or the Not Interest off-ramp). Wait Pre-screen
# can go to Sent Exam or Not Interest. Not Interest can only go back to Pending.
# Each list = forward target(s) first, then the one-step "back" target. A backward
# move is a plain stage correction: the webapp does NOT fire that stage's side
# effects (email/draft/folder move), and set_stage preserves the recorded dates.
ALLOWED_MOVES = {
    "prescreen":      ["wait_prescreen", "not_interest"],  # forward first, off-ramp second
    "wait_prescreen": ["sent_exam", "not_interest"],       # forward to exam, or off-ramp
    "not_interest":   ["prescreen"],
    "sent_exam":      ["shortlist", "wait_prescreen"],
    "shortlist":      ["interview", "sent_exam"],
    "interview":      ["evaluation", "shortlist"],
    "evaluation":     ["offered", "interview"],
    "offered":        ["evaluation"],
}
# Which date column each stage records when a candidate is moved into it. The
# "Sent Exam" stage has no date column — it sets is_sent_exam/exam_sent_at instead.
STAGE_DATE_COLUMN = {
    "shortlist": "shortlist_date",
    "interview": "interview_date",
    "evaluation": "evaluation_date",
    "offered": "offer_date",
}
# Auto-stamped entry timestamp per stage: the exact moment set_stage moves a
# candidate into the stage (Thai time, stamped once via COALESCE — never reset).
# Unlike STAGE_DATE_COLUMN these are not HR-supplied dates but a true audit trail.
STAGE_STAMP_COLUMN = {
    "sent_exam":  "sent_exam_stamped_date",
    "shortlist":  "shortlist_stamped_date",
    "interview":  "interview_stamped_date",
    "evaluation": "evaluation_stamped_date",
    "offered":    "offered_stamped_date",
}


def stage_index(stage: str) -> int:
    try:
        return STAGES.index(stage)
    except ValueError:
        return -1


def _connect(conn_str: str, autocommit: bool = False) -> pyodbc.Connection:
    return pyodbc.connect(conn_str, autocommit=autocommit, timeout=15)


def ensure_database(cfg: Config) -> None:
    """CREATE DATABASE if missing. Runs against the 'master' database."""
    with _connect(cfg.odbc_connection_string_master, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "IF DB_ID(?) IS NULL EXEC('CREATE DATABASE [' + ? + ']')",
            cfg.db_name, cfg.db_name,
        )
    log.info("Database '%s' is present.", cfg.db_name)


def ensure_schema(cfg: Config) -> None:
    """Apply schema.sql. The script is written to be idempotent."""
    ddl = cfg.schema_sql.read_text(encoding="utf-8")
    # Split on GO batch separators if present; otherwise run as one batch.
    batches = [b for b in _split_batches(ddl) if b.strip()]
    with _connect(cfg.odbc_connection_string, autocommit=True) as conn:
        cur = conn.cursor()
        for batch in batches:
            cur.execute(batch)
    log.info("Schema applied.")


def _split_batches(sql: str) -> list[str]:
    out, current = [], []
    for line in sql.splitlines():
        if line.strip().upper() == "GO":
            out.append("\n".join(current))
            current = []
        else:
            current.append(line)
    out.append("\n".join(current))
    return out


def ping(cfg: Config | None = None) -> bool:
    """Connectivity check + schema bootstrap. Returns True on success.

    Usable from the CLI: `python -c "import db; db.ping()"`.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = cfg or load_config()
    try:
        ensure_database(cfg)
        ensure_schema(cfg)
        with _connect(cfg.odbc_connection_string) as conn:
            ver = conn.cursor().execute("SELECT @@VERSION").fetchone()[0]
        log.info("Connected OK to %s:%s. Server: %s",
                 cfg.db_host, cfg.db_port, ver.splitlines()[0])
        return True
    except pyodbc.Error as exc:
        log.error("DB connection FAILED: %s", exc)
        log.error("Check: DB_HOST=%s, port=%s, TCP/IP enabled, SQL auth on, firewall 1433.",
                  cfg.db_host, cfg.db_port)
        return False


class Database:
    """Thin connection wrapper exposing upsert helpers."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.conn = _connect(cfg.odbc_connection_string, autocommit=False)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.conn.close()
        except pyodbc.Error:
            pass

    def upsert_job(self, job: dict[str, Any]) -> None:
        # is_active is deliberately NOT updated here so a manual UI toggle sticks;
        # new jobs default to active (1) via the table default.
        sql = f"""
        MERGE dbo.jobs AS tgt
        USING (SELECT ? AS job_id, ? AS title, ? AS location, ? AS url) AS src
            ON tgt.job_id = src.job_id
        WHEN MATCHED THEN UPDATE SET
            -- COALESCE so a re-scrape never wipes a good value with NULL: when a
            -- --job-id run can't find the job in the open-jobs list it passes
            -- title/location/url = NULL, which used to blank the stored title
            -- (UI then showed "(untitled)"). Keep the existing value in that case.
            title = COALESCE(src.title, tgt.title),
            location = COALESCE(src.location, tgt.location),
            url = COALESCE(src.url, tgt.url),
            scraped_at = {THAI_NOW}
        WHEN NOT MATCHED THEN
            INSERT (job_id, title, location, url)
            VALUES (src.job_id, src.title, src.location, src.url);
        """
        self.conn.cursor().execute(
            sql, str(job["job_id"]), job.get("title"),
            job.get("location"), job.get("url"),
        )
        self.conn.commit()

    def upsert_applicant(self, app: dict[str, Any]) -> None:
        raw = app.get("raw_json")
        if raw is not None and not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
        sql = f"""
        MERGE dbo.applicants AS tgt
        USING (SELECT ? AS job_id, ? AS candidate_key) AS src
            ON tgt.job_id = src.job_id AND tgt.candidate_key = src.candidate_key
        WHEN MATCHED THEN UPDATE SET
            full_name_jobdb = ?, email = ?, phone = ?, expect_salary = ?,
            location = ?, applied_at = ?, status = ?, resume_filename = ?, resume_path = ?,
            resume_downloaded = ?, raw_json = ?, scraped_at = {THAI_NOW}
        WHEN NOT MATCHED THEN
            INSERT (application_id, job_id, candidate_key, full_name_jobdb, full_name_edit,
                    email, phone, expect_salary, location, applied_at, status,
                    resume_filename, resume_path, resume_downloaded, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        # The MERGE keys on (job_id, candidate_key) — a STABLE identity — NOT on
        # application_id, which SEEK regenerates each scrape. On MATCH we therefore
        # update the existing row in place and deliberately do NOT touch
        # application_id (the PK keeps its first-seen value) or the HR pipeline
        # columns (stage/dates/full_name_edit), so a returning candidate stays put
        # instead of spawning a duplicate Pending row.
        aid = str(app["application_id"])
        ckey = candidate_key(app)
        vals_update = (
            app.get("full_name"), app.get("email"),
            app.get("phone"), app.get("expect_salary"), app.get("location"),
            app.get("applied_at"), app.get("status"), app.get("resume_filename"),
            app.get("resume_path"), 1 if app.get("resume_downloaded") else 0, raw,
        )
        vals_insert = (
            aid, app.get("job_id"), ckey, app.get("full_name"), app.get("full_name"),
            app.get("email"), app.get("phone"), app.get("expect_salary"),
            app.get("location"), app.get("applied_at"), app.get("status"),
            app.get("resume_filename"), app.get("resume_path"),
            1 if app.get("resume_downloaded") else 0, raw,
        )
        self.conn.cursor().execute(
            sql, app.get("job_id"), ckey, *vals_update, *vals_insert)
        self.conn.commit()

    def light_upsert_applicant(self, app: dict[str, Any]) -> None:
        """Refresh only the cheap pre-click card fields for an already-downloaded
        candidate (used by the fast duplicate-skip path on re-downloads). Resume,
        email/phone and the HR-editable full_name_edit are PRESERVED — COALESCE keeps
        the existing value when the incoming one is NULL. No INSERT branch: a skip
        target is, by definition, already in the table."""
        ckey = candidate_key(app)
        sql = f"""
        MERGE dbo.applicants AS tgt
        USING (SELECT ? AS job_id, ? AS candidate_key) AS src
            ON tgt.job_id = src.job_id AND tgt.candidate_key = src.candidate_key
        WHEN MATCHED THEN UPDATE SET
            full_name_jobdb = COALESCE(?, tgt.full_name_jobdb),
            applied_at      = COALESCE(?, tgt.applied_at),
            expect_salary   = COALESCE(?, tgt.expect_salary),
            scraped_at      = {THAI_NOW};
        """
        self.conn.cursor().execute(
            sql, app.get("job_id"), ckey, app.get("full_name"),
            app.get("applied_at"), app.get("expect_salary"))
        self.conn.commit()

    def get_downloaded_candidate_keys(self, job_id: str) -> set[str]:
        """candidate_keys for this job that already have a downloaded resume
        (resume_downloaded=1). Preloaded once per run for O(1) duplicate checks.

        Keyed on the STABLE candidate_key (not the volatile application_id) so a
        returning candidate is recognised on re-scrape and skipped pre-click
        instead of being re-downloaded and re-inserted as a duplicate."""
        cur = self.conn.cursor()
        cur.execute("SELECT candidate_key FROM dbo.applicants "
                    "WHERE job_id = ? AND resume_downloaded = 1 "
                    "AND candidate_key IS NOT NULL", str(job_id))
        return {r[0] for r in cur.fetchall()}

    def count(self, table: str) -> int:
        if table not in {"jobs", "applicants"}:
            raise ValueError(f"Unknown table: {table}")
        row = self.conn.cursor().execute(f"SELECT COUNT(*) FROM dbo.{table}").fetchone()
        return int(row[0])

    # -- queries for the web UI --------------------------------------------
    def list_jobs_with_counts(self) -> list[dict[str, Any]]:
        """Jobs plus their applicant counts, for the job-postings landing page.

        `applicants` = total candidates; `new` = those not yet sent the exam
        (drives the "N New" badge on the HR board)."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT j.job_id, j.title, j.location, j.is_active, j.scraped_at,
                   (SELECT COUNT(*) FROM dbo.applicants a WHERE a.job_id = j.job_id) AS n,
                   (SELECT COUNT(*) FROM dbo.applicants a
                      WHERE a.job_id = j.job_id AND a.is_sent_exam = 0) AS n_new
            FROM dbo.jobs j
            ORDER BY j.is_active DESC, j.title
        """)
        return [
            {
                "job_id": r[0], "title": r[1], "location": r[2],
                "is_active": bool(r[3]),
                "scraped_at": r[4].isoformat(sep=" ", timespec="minutes") if r[4] else None,
                "applicants": int(r[5]), "new": int(r[6]),
            }
            for r in cur.fetchall()
        ]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute("SELECT job_id, title, location, is_active FROM dbo.jobs WHERE job_id = ?",
                    job_id)
        r = cur.fetchone()
        if not r:
            return None
        return {"job_id": r[0], "title": r[1], "location": r[2], "is_active": bool(r[3])}

    # Columns selected for any candidate row shown in the UI, in order.
    _CAND_COLS = (
        "application_id, job_id, full_name_jobdb, full_name_edit, email, phone, applied_at, "
        "resume_downloaded, is_sent_exam, exam_sent_at, "
        "stage, cv_sent, shortlist_date, interview_date, offer_date, nickname, expect_salary, "
        "ai_summary, "
        "reply_received, reply_at, reply_subject, "   # r[18], r[19], r[20]
        "evaluation_date, "                           # r[21] (appended last so earlier indices stay put)
        "name_title, "                                # r[22] (honorific prefix: Mr./Ms./Mrs.)
        "[position], [role], company, department, section, interviewer, recruiter_name, "  # r[23]-r[29] (eval form)
        "sent_exam_stamped_date, shortlist_stamped_date, interview_stamped_date, "  # r[30]-r[32]
        "evaluation_stamped_date, offered_stamped_date, "                           # r[33]-r[34] (stage entry stamps)
        "university, major, ai_extract_json, "                                      # r[35]-r[37] (AI résumé extraction)
        "remark, "                                                                   # r[38] (HR free-text note)
        "exp_total, exp_directly"                                                    # r[39]-r[40] (AI experience, years)
    )

    @staticmethod
    def _row_to_candidate(r: Any) -> dict[str, Any]:
        def _d(v: Any) -> str | None:
            return v.isoformat() if v else None
        def _ts(v: Any) -> str | None:   # datetime -> "YYYY-MM-DD HH:MM" (Thai)
            return v.isoformat(sep=" ", timespec="minutes") if v else None
        def _dec(v: Any) -> str | None:  # Decimal years -> "4.50" (JSON-safe), else None
            return f"{v:.2f}" if v is not None else None
        return {
            "application_id": r[0], "job_id": r[1], "full_name_jobdb": r[2], "full_name_edit": r[3],
            "email": r[4], "phone": r[5], "applied_at": r[6],
            "resume_downloaded": bool(r[7]), "is_sent_exam": bool(r[8]),
            "exam_sent_at": r[9].isoformat(sep=" ", timespec="seconds") if r[9] else None,
            "stage": r[10] or "prescreen", "stage_label": STAGE_LABELS.get(r[10] or "prescreen"),
            "cv_sent": bool(r[11]),
            "shortlist_date": _d(r[12]), "interview_date": _d(r[13]), "offer_date": _d(r[14]),
            "nickname": r[15], "expect_salary": r[16], "ai_summary": r[17],
            # reply_received is tri-state: None (never checked) / False / True.
            "reply_received": (None if r[18] is None else bool(r[18])),
            "reply_at": r[19].isoformat(sep=" ", timespec="minutes") if r[19] else None,
            "reply_subject": r[20],
            "evaluation_date": _d(r[21]),
            "name_title": r[22],
            "position": r[23], "role": r[24], "company": r[25], "department": r[26],
            "section": r[27], "interviewer": r[28], "recruiter_name": r[29],
            "sent_exam_stamped_date": _ts(r[30]), "shortlist_stamped_date": _ts(r[31]),
            "interview_stamped_date": _ts(r[32]), "evaluation_stamped_date": _ts(r[33]),
            "offered_stamped_date": _ts(r[34]),
            "university": r[35], "major": r[36], "ai_extract_json": r[37],
            "remark": r[38],
            "exp_total": _dec(r[39]), "exp_directly": _dec(r[40]),
        }

    def list_candidates(self, job_id: str, name_query: str = "") -> list[dict[str, Any]]:
        """Candidates for a job, optionally filtered by a name substring
        (matches either the scraped name or the editable real name)."""
        cur = self.conn.cursor()
        sql = f"SELECT {self._CAND_COLS} FROM dbo.applicants WHERE job_id = ?"
        params: list[Any] = [job_id]
        if name_query:
            sql += " AND (full_name_jobdb LIKE ? OR full_name_edit LIKE ?)"
            params += [f"%{name_query}%", f"%{name_query}%"]
        sql += " ORDER BY is_sent_exam, full_name_edit, full_name_jobdb"
        cur.execute(sql, *params)
        return [self._row_to_candidate(r) for r in cur.fetchall()]

    def list_all_candidates(self, job_id: str = "", stage: str = "",
                            name_query: str = "") -> list[dict[str, Any]]:
        """All candidates across jobs (status-tracking table), with optional
        job / stage / name filters. Includes the job title for display."""
        cur = self.conn.cursor()
        cols = ", ".join(f"a.{c.strip()}" for c in self._CAND_COLS.split(","))
        sql = f"SELECT {cols}, j.title FROM dbo.applicants a " \
              "LEFT JOIN dbo.jobs j ON j.job_id = a.job_id WHERE 1=1"
        params: list[Any] = []
        if job_id:
            sql += " AND a.job_id = ?"
            params.append(job_id)
        if stage:
            sql += " AND a.stage = ?"
            params.append(stage)
        if name_query:
            sql += " AND (a.full_name_jobdb LIKE ? OR a.full_name_edit LIKE ?)"
            params += [f"%{name_query}%", f"%{name_query}%"]
        sql += " ORDER BY j.title, a.full_name_edit, a.full_name_jobdb"
        cur.execute(sql, *params)
        out = []
        for r in cur.fetchall():
            cand = self._row_to_candidate(r)
            cand["job_title"] = r[-1]   # j.title is always the last selected column
            out.append(cand)
        return out

    def set_stage(self, application_id: str, new_stage: str, date: str | None) -> dict[str, Any]:
        """Move a candidate to an ALLOWED next stage (branching, server-enforced).

        Returns {"ok": True} on success, or {"ok": False, "error": ...} if the move
        is invalid. Moving forward records the stage's date and backfills the
        is_sent_exam / cv_sent milestones so the pipeline timeline never has gaps."""
        if new_stage not in STAGES:
            return {"ok": False, "error": f"Unknown stage: {new_stage}"}
        cur = self.conn.cursor()
        row = cur.execute("SELECT stage FROM dbo.applicants WHERE application_id = ?",
                          application_id).fetchone()
        if not row:
            return {"ok": False, "error": "Candidate not found"}
        current = row[0] or "prescreen"
        if new_stage not in ALLOWED_MOVES.get(current, []):
            return {"ok": False, "error":
                    f"Cannot move from {STAGE_LABELS.get(current, current)} "
                    f"to {STAGE_LABELS.get(new_stage, new_stage)}."}

        sets = ["stage = ?"]
        params: list[Any] = [new_stage]
        # Only write the stage's date when one is supplied — a backward move passes
        # no date and must NOT blank the date already recorded for that stage.
        date_col = STAGE_DATE_COLUMN.get(new_stage)
        if date_col and date:
            sets.append(f"{date_col} = ?")
            params.append(date)
        # Auto-stamp the moment this stage was entered (Thai time). COALESCE keeps
        # the first stamp if the candidate ever revisits the stage.
        stamp_col = STAGE_STAMP_COLUMN.get(new_stage)
        if stamp_col:
            sets.append(f"{stamp_col} = COALESCE({stamp_col}, {THAI_NOW})")
        # Reaching "Sent Exam" (or beyond) implies the exam was sent: set the flag
        # and stamp exam_sent_at (keep an existing real send time if already set).
        if stage_index(new_stage) >= stage_index("sent_exam"):
            sets.append("is_sent_exam = 1")
            sets.append(f"exam_sent_at = COALESCE(exam_sent_at, {THAI_NOW})")
        # CV is considered sent once a candidate is shortlisted or beyond.
        if stage_index(new_stage) >= stage_index("shortlist"):
            sets.append("cv_sent = 1")
        params.append(application_id)
        cur.execute(f"UPDATE dbo.applicants SET {', '.join(sets)} WHERE application_id = ?",
                    *params)
        self.conn.commit()
        return {"ok": True, "stage": new_stage, "stage_label": STAGE_LABELS[new_stage]}

    def get_candidate(self, application_id: str) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT a.application_id, a.full_name_jobdb, a.full_name_edit, a.email, "
            "a.job_id, j.title, a.stage, a.name_title "
            "FROM dbo.applicants a LEFT JOIN dbo.jobs j ON j.job_id = a.job_id "
            "WHERE a.application_id = ?", application_id)
        r = cur.fetchone()
        if not r:
            return None
        # Prefer the edited name for the email greeting.
        return {"application_id": r[0], "full_name_jobdb": r[1],
                "full_name_edit": r[2], "email": r[3], "job_id": r[4],
                "job_title": r[5], "stage": r[6] or "prescreen",
                "name_title": r[7]}

    def list_applicant_fields(self) -> list[str]:
        """Every column name on dbo.applicants, read live from the catalog so a newly
        added column appears automatically, plus the derived 'job_title'. Powers the
        insert-field chips in Config Email Template."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'applicants' "
            "ORDER BY ORDINAL_POSITION")
        cols = [row[0] for row in cur.fetchall()]
        if "job_title" not in cols:
            cols.append("job_title")
        return cols

    def get_candidate_fields(self, application_id: str) -> dict[str, Any] | None:
        """Every applicants column for one candidate (keyed by column name) plus the
        job title — the full placeholder map for filling email templates. Returns None
        if the candidate is not found."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT a.*, j.title AS job_title "
            "FROM dbo.applicants a LEFT JOIN dbo.jobs j ON j.job_id = a.job_id "
            "WHERE a.application_id = ?", application_id)
        r = cur.fetchone()
        if not r:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, r))

    def get_offer_inputs(self, application_id: str) -> dict[str, Any] | None:
        """Everything the job-offer draft email needs: names + honorific, the
        evaluation-stage fields (position/role/department/section/interviewer),
        the AI résumé summary, expected salary, and the job title (role fallback)."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT a.name_title, a.full_name_edit, a.full_name_jobdb, a.[position], "
            "a.[role], a.department, a.section, a.interviewer, a.ai_summary, "
            "a.expect_salary, j.title "
            "FROM dbo.applicants a LEFT JOIN dbo.jobs j ON j.job_id = a.job_id "
            "WHERE a.application_id = ?", application_id)
        r = cur.fetchone()
        if not r:
            return None
        return {"name_title": r[0], "full_name_edit": r[1], "full_name_jobdb": r[2],
                "position": r[3], "role": r[4], "department": r[5], "section": r[6],
                "interviewer": r[7], "ai_summary": r[8], "expect_salary": r[9],
                "job_title": r[10]}

    def get_experience_inputs(self, application_id: str) -> dict[str, Any] | None:
        """Inputs to (re)generate the offer-email AI experience detail: the cached
        value (offer_experience_ai), the first-step résumé summary, the résumé path,
        names, and the job title."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT a.offer_experience_ai, a.ai_summary, a.resume_path, a.resume_downloaded, "
            "a.full_name_edit, a.full_name_jobdb, j.title, a.offer_experience "
            "FROM dbo.applicants a LEFT JOIN dbo.jobs j ON j.job_id = a.job_id "
            "WHERE a.application_id = ?", application_id)
        r = cur.fetchone()
        if not r:
            return None
        return {"offer_experience_ai": r[0], "ai_summary": r[1], "resume_path": r[2],
                "resume_downloaded": bool(r[3]), "full_name_edit": r[4],
                "full_name_jobdb": r[5], "job_title": r[6], "offer_experience": r[7]}

    def save_offer_experience(self, application_id: str, experience_ai: str) -> None:
        """Cache the generated/edited AI experience detail (the bullet paragraphs)."""
        self.conn.cursor().execute(
            "UPDATE dbo.applicants SET offer_experience_ai = ? WHERE application_id = ?",
            (experience_ai or None), application_id)
        self.conn.commit()

    def save_offer_headline(self, application_id: str, headline: str) -> None:
        """Cache the generated/edited AI experience HEADLINE (the one-line
        "<Position> <Company> <duration>, …" text). Reuses the offer_experience
        column — the same column save_offer writes the final headline to."""
        self.conn.cursor().execute(
            "UPDATE dbo.applicants SET offer_experience = ? WHERE application_id = ?",
            (headline or None), application_id)
        self.conn.commit()

    def save_offer(self, application_id: str, *, people_count: str | None,
                   offer_type: str | None, new_replace_text: str | None,
                   supervisor: str | None, buddy: str | None,
                   expected_salary: str | None, current_salary: str | None,
                   start_date: str | None, experience: str | None,
                   experience_ai: str | None, interviewer: str | None,
                   interviewer_comments: str | None,
                   recruiter_comments: str | None) -> None:
        """Persist the job-offer popup inputs on the candidate record. The edited
        interviewer overwrites the existing column only when non-empty (NULLIF)."""
        self.conn.cursor().execute(
            "UPDATE dbo.applicants SET offer_people_count = ?, offer_type = ?, "
            "offer_new_replace = ?, offer_supervisor = ?, offer_buddy = ?, "
            "offer_expected_salary = ?, offer_current_salary = ?, offer_start_date = ?, "
            "offer_experience = ?, offer_experience_ai = ?, offer_interviewer_comments = ?, "
            "offer_recruiter_comments = ?, "
            "interviewer = COALESCE(NULLIF(?, ''), interviewer) WHERE application_id = ?",
            (people_count or None), (offer_type or None), (new_replace_text or None),
            (supervisor or None), (buddy or None), (expected_salary or None),
            (current_salary or None), (start_date or None), (experience or None),
            (experience_ai or None), (interviewer_comments or None),
            (recruiter_comments or None), (interviewer or ""), application_id)
        self.conn.commit()

    def update_candidate(self, application_id: str, full_name_edit: str | None,
                         email: str | None, phone: str | None,
                         nickname: str | None = None,
                         name_title: str | None = None,
                         university: str | None = None,
                         major: str | None = None,
                         remark: str | None = None,
                         exp_total: Any = None, exp_directly: Any = None) -> None:
        """Update the user-editable fields for one candidate. exp_total/exp_directly
        are the (HR-editable) AI experience years — coerced to a number or NULL."""
        self.conn.cursor().execute(
            "UPDATE dbo.applicants SET full_name_edit = ?, email = ?, phone = ?, "
            "nickname = ?, name_title = ?, university = ?, major = ?, remark = ?, "
            "exp_total = ?, exp_directly = ? "
            "WHERE application_id = ?",
            (full_name_edit or None), (email or None), (phone or None),
            (nickname or None), (name_title or None),
            (university or None), (major or None), ((remark or "")[:1000] or None),
            _years_or_none(exp_total), _years_or_none(exp_directly),
            application_id)
        self.conn.commit()

    def save_evaluation(self, application_id: str, *, position: str | None,
                        role: str | None, company: str | None, department: str | None,
                        section: str | None, interview_date: str | None,
                        interviewer: str | None, recruiter_name: str | None) -> None:
        """Persist the interview-evaluation form fields captured from the popup.
        `interview_date` reuses the existing interview_date column."""
        self.conn.cursor().execute(
            "UPDATE dbo.applicants SET [position] = ?, [role] = ?, company = ?, "
            "department = ?, section = ?, interview_date = ?, interviewer = ?, "
            "recruiter_name = ? WHERE application_id = ?",
            (position or None), (role or None), (company or None), (department or None),
            (section or None), (interview_date or None), (interviewer or None),
            (recruiter_name or None), application_id)
        self.conn.commit()

    def get_resume_path(self, application_id: str) -> str | None:
        """Return the stored resume file path for one candidate (or None)."""
        cur = self.conn.cursor()
        cur.execute("SELECT resume_path FROM dbo.applicants WHERE application_id = ?",
                    application_id)
        row = cur.fetchone()
        return row[0] if row else None

    def get_summary_inputs(self, application_id: str) -> dict[str, Any] | None:
        """Everything needed to (re)build a candidate's AI resume summary:
        the resume path, names, job title, and any summary already stored."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT a.resume_path, a.resume_downloaded, a.full_name_edit, "
            "a.full_name_jobdb, j.title, a.ai_summary, a.name_title, "
            "a.university, a.major, a.ai_extract_json "
            "FROM dbo.applicants a LEFT JOIN dbo.jobs j ON j.job_id = a.job_id "
            "WHERE a.application_id = ?", application_id)
        r = cur.fetchone()
        if not r:
            return None
        return {"resume_path": r[0], "resume_downloaded": bool(r[1]),
                "full_name_edit": r[2], "full_name_jobdb": r[3],
                "job_title": r[4], "ai_summary": r[5], "name_title": r[6],
                "university": r[7], "major": r[8], "ai_extract_json": r[9]}

    def save_ai_summary(self, application_id: str, summary: str) -> None:
        """Store a generated resume summary (stamped with Thai local time)."""
        self.conn.cursor().execute(
            f"UPDATE dbo.applicants SET ai_summary = ?, ai_summary_at = {THAI_NOW} "
            "WHERE application_id = ?", summary, application_id)
        self.conn.commit()

    def save_ai_extract(self, application_id: str, extract_json: str,
                        full_name: str | None = None,
                        exp_total: Any = None, exp_directly: Any = None) -> None:
        """Cache the raw AI résumé-extraction JSON (stamped with Thai local time) and
        persist the computed experience years (exp_total / exp_directly). When
        `full_name` is non-empty, also overwrite full_name_edit with the AI-formatted
        name (university/major stay as suggestions until HR saves)."""
        sets = ["ai_extract_json = ?", f"ai_extract_at = {THAI_NOW}",
                "exp_total = ?", "exp_directly = ?"]
        params: list[Any] = [extract_json, _years_or_none(exp_total),
                             _years_or_none(exp_directly)]
        if full_name and full_name.strip():
            sets.append("full_name_edit = ?")
            params.append(full_name.strip())
        params.append(application_id)
        self.conn.cursor().execute(
            f"UPDATE dbo.applicants SET {', '.join(sets)} WHERE application_id = ?",
            *params)
        self.conn.commit()

    def get_reply_inputs(self, application_id: str) -> dict[str, Any] | None:
        """Inputs to (re)check a candidate's exam reply + save reply files:
        email, exam send time, names, résumé path, and the cached reply status."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT email, is_sent_exam, exam_sent_at, full_name_edit, full_name_jobdb, "
            "resume_path, reply_received, reply_at, reply_subject "
            "FROM dbo.applicants WHERE application_id = ?", application_id)
        r = cur.fetchone()
        if not r:
            return None
        return {"email": r[0], "is_sent_exam": bool(r[1]),
                "exam_sent_at": r[2],            # raw naive Thai datetime (convert in caller)
                "full_name_edit": r[3], "full_name_jobdb": r[4], "resume_path": r[5],
                "reply_received": (None if r[6] is None else bool(r[6])),
                "reply_at": r[7].isoformat(sep=" ", timespec="minutes") if r[7] else None,
                "reply_subject": r[8]}

    def save_reply_status(self, application_id: str, replied: bool,
                          reply_at: str | None, subject: str | None) -> None:
        """Cache a mailbox reply-check result (stamped with Thai check time).
        `reply_at` is an ISO datetime string in Thai time (or None)."""
        self.conn.cursor().execute(
            f"UPDATE dbo.applicants SET reply_received = ?, reply_at = ?, "
            f"reply_subject = ?, reply_checked_at = {THAI_NOW} WHERE application_id = ?",
            1 if replied else 0, reply_at, (subject or "")[:500], application_id)
        self.conn.commit()

    def mark_exam_sent(self, application_id: str) -> None:
        self.conn.cursor().execute(
            f"UPDATE dbo.applicants SET is_sent_exam = 1, exam_sent_at = {THAI_NOW} "
            "WHERE application_id = ?", application_id)
        self.conn.commit()

    def set_job_active(self, job_id: str, is_active: bool) -> None:
        self.conn.cursor().execute(
            "UPDATE dbo.jobs SET is_active = ? WHERE job_id = ?",
            1 if is_active else 0, job_id)
        self.conn.commit()


if __name__ == "__main__":
    raise SystemExit(0 if ping() else 1)
