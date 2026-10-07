"""SQL Server access layer using pyodbc.

Responsibilities:
  - connect to the local Windows SQL Server (from WSL2),
  - create the target database if it does not exist,
  - apply the idempotent schema in schema.sql,
  - MERGE-upsert jobs and applicants so re-runs update instead of duplicating.
"""
from __future__ import annotations

import hashlib
import json
import uuid
import re
import secrets
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


def _int_or_none(v: Any) -> int | None:
    """Coerce a value (1, '1', '') to an int for the INT columns, or None."""
    s = str(v if v is not None else "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


# Hiring-request columns plus the display names of whoever created / last edited
# the row (append a WHERE / ORDER BY).
_REQUEST_SELECT = (
    "SELECT r.request_id, r.request_code, r.request_name, r.[position], r.is_new_replace, "
    "r.company, r.department, r.section, r.direct_supervisor, r.buddy, r.head_count, "
    "r.[type], r.reason, r.requested_by, r.acknowledge_by_1, r.acknowledge_by_2, "
    "r.created_at, r.created_by, cu.name AS created_by_name, "
    "r.updated_at, r.updated_by, uu.name AS updated_by_name, r.revision, "
    "r.status, r.completed_at, r.completed_by, pu.name AS completed_by_name "
    "FROM dbo.requests r "
    "LEFT JOIN dbo.users cu ON cu.email = r.created_by "
    "LEFT JOIN dbo.users uu ON uu.email = r.updated_by "
    "LEFT JOIN dbo.users pu ON pu.email = r.completed_by ")


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


def hash_password(password: str) -> str:
    """SHA-256 of a per-user random salt + the password, stored as
    'sha256$<salt hex>$<digest hex>' (the salt stops two users with the same
    password sharing a hash). SHA-256 only — no other algorithm is used."""
    salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"sha256${salt}${digest}"


def verify_password(password: str, stored: str | None) -> bool:
    """Check a password against hash_password() output. A bare 64-hex value is
    accepted as an unsalted SHA-256 digest (rows imported from another system)."""
    if not stored:
        return False
    parts = stored.split("$")
    if len(parts) == 3 and parts[0] == "sha256":
        digest = hashlib.sha256((parts[1] + password).encode("utf-8")).hexdigest()
        return secrets.compare_digest(digest, parts[2])
    if len(stored) == 64:
        return secrets.compare_digest(hashlib.sha256(password.encode("utf-8")).hexdigest(),
                                      stored.lower())
    return False


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
        "exp_total, exp_directly, "                                                  # r[39]-r[40] (AI experience, years)
        "current_salary_edit, minimum_expect_salary_edit, expect_salary_edit, "      # r[41]-r[43] (HR salary fields)
        "request_id, "                                                               # r[44] (linked hiring request)
        "owner_email, "                                                              # r[45] (responsible recruiter)
        "[source]"                                                                   # r[46] (NULL = scraped, 'manual' = typed in)
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
            "current_salary_edit": r[41], "minimum_expect_salary_edit": r[42],
            "expect_salary_edit": r[43],
            "request_id": r[44],
            "owner_email": r[45],
            "source": r[46],
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
        job / stage / name filters. `stage` may list several stages, comma-separated
        ("sent_exam,shortlist"); blank = every stage. Includes the job title for display."""
        cur = self.conn.cursor()
        cols = ", ".join(f"a.{c.strip()}" for c in self._CAND_COLS.split(","))
        sql = f"SELECT {cols}, j.title FROM dbo.applicants a " \
              "LEFT JOIN dbo.jobs j ON j.job_id = a.job_id WHERE 1=1"
        params: list[Any] = []
        if job_id:
            sql += " AND a.job_id = ?"
            params.append(job_id)
        stages = [x.strip() for x in (stage or "").split(",") if x.strip()]
        if stages:
            sql += f" AND a.stage IN ({', '.join('?' * len(stages))})"
            params += stages
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

    def _log_history(self, cur: Any, application_id: str, action: str,
                     from_value: str | None, to_value: str | None,
                     actor: str | None) -> None:
        """Append one dbo.stage_history row on the caller's cursor (caller commits)."""
        cur.execute(
            "INSERT INTO dbo.stage_history (application_id, action, from_value, to_value, "
            "actor_email) VALUES (?, ?, ?, ?, ?)",
            application_id, action, from_value, to_value, actor)

    def set_stage(self, application_id: str, new_stage: str, date: str | None,
                  exam_sent_by: str | None = None,
                  actor: str | None = None) -> dict[str, Any]:
        """Move a candidate to an ALLOWED next stage (branching, server-enforced).

        Returns {"ok": True} on success, or {"ok": False, "error": ...} if the move
        is invalid. Moving forward records the stage's date and backfills the
        is_sent_exam / cv_sent milestones so the pipeline timeline never has gaps.

        The write is conditional on the stage still being the one we read, so two
        teammates (or a double click) racing on the same card can't both "win":
        the loser gets {"ok": False, "conflict": True}. `exam_sent_by` marks a real
        exam send (see claim_exam_send): it stamps exam_sent_at to now and records
        the sender instead of just backfilling the milestone. `actor` (a users
        email) is written to stage_history and, on the first move out of Pending,
        becomes the candidate's owner."""
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
        # Owner = whoever first moves the card out of Pending (to Wait Pre-screen OR
        # Not Interest). COALESCE keeps an owner that was set or reassigned earlier.
        if actor and current == "prescreen":
            sets.append("owner_email = COALESCE(owner_email, ?)")
            params.append(actor)
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
            if exam_sent_by is not None:
                sets.append(f"exam_sent_at = {THAI_NOW}")
                sets.append("exam_sent_by = ?")
                params.append(exam_sent_by)
            else:
                sets.append(f"exam_sent_at = COALESCE(exam_sent_at, {THAI_NOW})")
        # CV is considered sent once a candidate is shortlisted or beyond.
        if stage_index(new_stage) >= stage_index("shortlist"):
            sets.append("cv_sent = 1")
        params += [application_id, current]
        cur.execute(f"UPDATE dbo.applicants SET {', '.join(sets)} "
                    "WHERE application_id = ? AND ISNULL(stage, 'prescreen') = ?", *params)
        if cur.rowcount == 0:
            # The stage changed between our read and our write: someone else moved
            # this card first. Leave their move alone and tell the caller.
            now = cur.execute("SELECT stage FROM dbo.applicants WHERE application_id = ?",
                              application_id).fetchone()
            label = STAGE_LABELS.get((now[0] if now else None) or "prescreen", "another stage")
            return {"ok": False, "conflict": True, "error":
                    f"Someone else just moved this candidate to {label}. "
                    "Refresh the board to see the latest."}
        self._log_history(cur, application_id,
                          "exam_sent" if exam_sent_by is not None else "move",
                          current, new_stage, actor)
        self.conn.commit()
        return {"ok": True, "stage": new_stage, "stage_label": STAGE_LABELS[new_stage]}

    def set_owner(self, application_id: str, owner_email: str | None,
                  actor: str | None = None) -> dict[str, Any]:
        """Reassign (or clear, with None/'') the recruiter responsible for a
        candidate, recording the change in stage_history."""
        owner_email = (owner_email or "").strip() or None
        cur = self.conn.cursor()
        row = cur.execute("SELECT owner_email FROM dbo.applicants WHERE application_id = ?",
                          application_id).fetchone()
        if not row:
            return {"ok": False, "error": "Candidate not found"}
        cur.execute("UPDATE dbo.applicants SET owner_email = ? WHERE application_id = ?",
                    owner_email, application_id)
        self._log_history(cur, application_id, "owner", row[0], owner_email, actor)
        self.conn.commit()
        return {"ok": True, "owner_email": owner_email}

    def list_history(self, application_id: str) -> list[dict[str, Any]]:
        """A candidate's audit trail, newest first, with the actor's display name."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT h.action, h.from_value, h.to_value, h.actor_email, u.name, h.at "
            "FROM dbo.stage_history h LEFT JOIN dbo.users u ON u.email = h.actor_email "
            "WHERE h.application_id = ? ORDER BY h.at DESC, h.history_id DESC", application_id)
        return [{"action": r[0], "from_value": r[1], "to_value": r[2], "actor_email": r[3],
                 "actor_name": r[4],
                 "at": r[5].isoformat(sep=" ", timespec="minutes") if r[5] else None}
                for r in cur.fetchall()]

    def claim_exam_send(self, application_id: str, sent_by: str,
                        resend: bool = False, actor: str | None = None) -> dict[str, Any]:
        """Reserve the exam send for ONE caller BEFORE the email goes out.

        The first caller moves the card to Sent Exam (atomic via set_stage) and is
        recorded as the sender; a double click or a second teammate is told the
        exam is already sent instead of emailing the candidate twice. `resend=True`
        is HR's explicit confirmation to email again: it skips that guard and
        refreshes exam_sent_at / exam_sent_by. Returns {"ok": True, "prev": ...}
        (pass `prev` to undo_exam_claim if the email then fails) or {"ok": False,
        "error": ...} with `already_sent` set when the guard fired."""
        cur = self.conn.cursor()
        row = cur.execute(
            "SELECT stage, is_sent_exam, exam_sent_at, exam_sent_by, sent_exam_stamped_date "
            "FROM dbo.applicants WHERE application_id = ?", application_id).fetchone()
        if not row:
            return {"ok": False, "error": "Candidate not found"}
        prev = {"stage": row[0] or "prescreen", "is_sent_exam": bool(row[1]),
                "exam_sent_at": row[2], "exam_sent_by": row[3], "stamp": row[4]}
        if prev["is_sent_exam"] and not resend:
            when = f" on {row[2]:%d %b %Y %H:%M}" if row[2] else ""
            who = f" by {row[3]}" if row[3] else ""
            return {"ok": False, "already_sent": True, "error":
                    f"The exam was already sent to this candidate{when}{who}."}
        if stage_index(prev["stage"]) < stage_index("sent_exam"):
            res = self.set_stage(application_id, "sent_exam", None, exam_sent_by=sent_by,
                                 actor=actor)
            if not res.get("ok"):
                return res
        else:
            # Already at/after Sent Exam (a confirmed re-send): only refresh the record.
            cur.execute(f"UPDATE dbo.applicants SET is_sent_exam = 1, exam_sent_at = {THAI_NOW}, "
                        "exam_sent_by = ? WHERE application_id = ?", sent_by, application_id)
            self._log_history(cur, application_id, "exam_resent", prev["stage"], prev["stage"], actor)
            self.conn.commit()
        return {"ok": True, "prev": prev}

    def undo_exam_claim(self, application_id: str, prev: dict[str, Any],
                        actor: str | None = None) -> None:
        """Put the send record back after the email failed (see claim_exam_send), so
        HR can fix the problem and retry. Only touches the row while it still sits
        where our claim left it, so a move made in the meantime is kept."""
        moved = stage_index(prev["stage"]) < stage_index("sent_exam")
        self.conn.cursor().execute(
            "UPDATE dbo.applicants SET stage = ?, is_sent_exam = ?, exam_sent_at = ?, "
            "exam_sent_by = ?, sent_exam_stamped_date = ? "
            "WHERE application_id = ? AND ISNULL(stage, 'prescreen') = ?",
            prev["stage"], 1 if prev["is_sent_exam"] else 0, prev["exam_sent_at"],
            prev["exam_sent_by"], prev["stamp"], application_id,
            "sent_exam" if moved else prev["stage"])
        self._log_history(self.conn.cursor(), application_id, "exam_failed",
                          "sent_exam" if moved else prev["stage"], prev["stage"], actor)
        self.conn.commit()

    # ---- manual candidates ("Add Candidate" on the pipeline page) ----------------
    def find_duplicate_candidate(self, job_id: str, full_name: str,
                                 email: str | None) -> dict[str, Any] | None:
        """An existing row in this job that looks like the same person: the same
        e-mail (case-insensitive) or the same normalised full name. Used to warn
        before a manual add creates a second card for someone already scraped."""
        name = re.sub(r"\s+", " ", (full_name or "")).strip().lower()
        mail = (email or "").strip().lower()
        if not name and not mail:
            return None
        cur = self.conn.cursor()
        cur.execute(
            "SELECT TOP 1 application_id, full_name_edit, full_name_jobdb, stage, email "
            "FROM dbo.applicants WHERE job_id = ? AND ("
            "  (? <> '' AND LOWER(LTRIM(RTRIM(email))) = ?) OR "
            "  (? <> '' AND LOWER(LTRIM(RTRIM(COALESCE(full_name_edit, full_name_jobdb)))) = ?)) "
            "ORDER BY scraped_at DESC",
            job_id, mail, mail, name, name)
        r = cur.fetchone()
        if not r:
            return None
        return {"application_id": r[0], "name": r[1] or r[2] or "", "stage": r[3] or "prescreen",
                "stage_label": STAGE_LABELS.get(r[3] or "prescreen", r[3]), "email": r[4]}

    def add_manual_candidate(self, job_id: str, *, full_name: str,
                             name_title: str | None = None, nickname: str | None = None,
                             email: str | None = None, phone: str | None = None,
                             remark: str | None = None,
                             current_salary: str | None = None,
                             minimum_expect_salary: str | None = None,
                             expect_salary: str | None = None,
                             actor: str | None) -> str:
        """Insert a candidate a recruiter typed in by hand. Lands in Pending with the
        same editable fields a Pending card shows. application_id is 'manual-<uuid>'
        and candidate_key 'manual|<uuid>' so a later SEEK scrape can neither match nor
        overwrite the row. Logs an 'added' history row for the actor. Returns the id."""
        aid = f"manual-{uuid.uuid4().hex}"
        name = re.sub(r"\s+", " ", full_name or "").strip()
        raw = json.dumps({"source": "manual", "added_by": actor}, ensure_ascii=False)
        cur = self.conn.cursor()
        cur.execute(
            f"""INSERT INTO dbo.applicants
                (application_id, job_id, candidate_key, full_name_jobdb, full_name_edit,
                 name_title, nickname, email, phone, remark,
                 current_salary_edit, minimum_expect_salary_edit, expect_salary_edit, expect_salary,
                 applied_at, status, stage, [source], resume_downloaded, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        CONVERT(NVARCHAR(16), {THAI_NOW}, 120), 'Manual', 'prescreen', 'manual',
                        0, ?)""",
            aid, job_id, f"manual|{aid}", name, name,
            (name_title or None), (nickname or None), (email or None), (phone or None),
            ((remark or "")[:1000] or None),
            (current_salary or None), (minimum_expect_salary or None),
            (expect_salary or None), (expect_salary or None), raw)
        self._log_history(cur, aid, "added", None, "prescreen", actor)
        self.conn.commit()
        return aid

    def set_resume(self, application_id: str, filename: str | None, path: str | None) -> None:
        """Record (or clear) the résumé file for one candidate."""
        self.conn.cursor().execute(
            "UPDATE dbo.applicants SET resume_filename = ?, resume_path = ?, resume_downloaded = ? "
            "WHERE application_id = ?",
            filename, path, 1 if path else 0, application_id)
        self.conn.commit()

    def delete_candidate(self, application_id: str) -> None:
        """Remove one candidate and its history (used to roll back a failed manual add)."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM dbo.stage_history WHERE application_id = ?", application_id)
        cur.execute("DELETE FROM dbo.applicants WHERE application_id = ?", application_id)
        self.conn.commit()

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
                         exp_total: Any = None, exp_directly: Any = None,
                         current_salary_edit: str | None = None,
                         minimum_expect_salary_edit: str | None = None,
                         expect_salary_edit: str | None = None,
                         interview_date: str | None = None) -> None:
        """Update the user-editable fields for one candidate. exp_total/exp_directly
        are the (HR-editable) AI experience years — coerced to a number or NULL.
        interview_date is HR-editable on the Interview-stage card (the Evaluation
        form reuses it); blank leaves it cleared."""
        self.conn.cursor().execute(
            "UPDATE dbo.applicants SET full_name_edit = ?, email = ?, phone = ?, "
            "nickname = ?, name_title = ?, university = ?, major = ?, remark = ?, "
            "exp_total = ?, exp_directly = ?, current_salary_edit = ?, "
            "minimum_expect_salary_edit = ?, expect_salary_edit = ?, interview_date = ? "
            "WHERE application_id = ?",
            (full_name_edit or None), (email or None), (phone or None),
            (nickname or None), (name_title or None),
            (university or None), (major or None), ((remark or "")[:1000] or None),
            _years_or_none(exp_total), _years_or_none(exp_directly),
            (current_salary_edit or None), (minimum_expect_salary_edit or None),
            (expect_salary_edit or None), (interview_date or None),
            application_id)
        self.conn.commit()

    def save_evaluation(self, application_id: str, *, position: str | None,
                        role: str | None, company: str | None, department: str | None,
                        section: str | None, interview_date: str | None,
                        interviewer: str | None, recruiter_name: str | None,
                        request_id: Any = None) -> None:
        """Persist the interview-evaluation form fields captured from the popup.
        `interview_date` reuses the existing interview_date column. `request_id`
        keeps the candidate's linked hiring request in sync if HR changed it."""
        self.conn.cursor().execute(
            "UPDATE dbo.applicants SET [position] = ?, [role] = ?, company = ?, "
            "department = ?, section = ?, interview_date = ?, interviewer = ?, "
            "recruiter_name = ?, request_id = ? WHERE application_id = ?",
            (position or None), (role or None), (company or None), (department or None),
            (section or None), (interview_date or None), (interviewer or None),
            (recruiter_name or None), _int_or_none(request_id), application_id)
        self.conn.commit()

    def set_request_fields(self, application_id: str, *, request_id: Any = None,
                           position: str | None, role: str | None,
                           company: str | None, department: str | None,
                           section: str | None) -> None:
        """Link a candidate to a hiring request and copy its
        Position/Role/Company/Department/Section onto the candidate. Chosen at Wait
        Pre-screen and editable on the Sent Exam / Shortlist / Interview cards;
        request_id is stored so those dropdowns can show the current selection.
        head-less role comes from the job title (the request has no role)."""
        self.conn.cursor().execute(
            "UPDATE dbo.applicants SET request_id = ?, [position] = ?, [role] = ?, "
            "company = ?, department = ?, section = ? WHERE application_id = ?",
            _int_or_none(request_id), (position or None), (role or None), (company or None),
            (department or None), (section or None), application_id)
        self.conn.commit()

    def insert_request(self, *, request_code: str | None, request_name: str | None,
                       position: str | None, is_new_replace: str | None,
                       company: str | None, department: str | None, section: str | None,
                       direct_supervisor: str | None, buddy: str | None,
                       head_count: Any, type_: str | None, reason: str | None,
                       requested_by: str | None, acknowledge_by_1: str | None,
                       acknowledge_by_2: str | None, actor: str | None = None) -> int:
        """Insert one hiring request (from the Request page). head_count is coerced
        to an int or NULL; `actor` (users.email) is recorded as the creator.
        Returns the new request_id."""
        try:
            hc = int(str(head_count).strip()) if str(head_count or "").strip() else None
        except (TypeError, ValueError):
            hc = None
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO dbo.requests (request_code, request_name, [position], "
            "is_new_replace, company, department, section, direct_supervisor, buddy, "
            "head_count, [type], reason, requested_by, acknowledge_by_1, acknowledge_by_2, "
            "created_by) "
            "OUTPUT INSERTED.request_id "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_code or None), (request_name or None), (position or None),
            (is_new_replace or None), (company or None), (department or None),
            (section or None), (direct_supervisor or None), (buddy or None), hc,
            (type_ or None), (reason or None), (requested_by or None),
            (acknowledge_by_1 or None), (acknowledge_by_2 or None), (actor or None))
        new_id = cur.fetchone()[0]
        self.conn.commit()
        return int(new_id)

    def list_requests(self) -> list[dict[str, Any]]:
        """All hiring requests, newest first (for the Requests list page)."""
        cur = self.conn.cursor()
        cur.execute(_REQUEST_SELECT + "ORDER BY r.request_id DESC")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        """One hiring request by id (for pre-filling the edit form), or None."""
        cur = self.conn.cursor()
        cur.execute(_REQUEST_SELECT + "WHERE r.request_id = ?", request_id)
        r = cur.fetchone()
        if not r:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, r))

    def update_request(self, request_id: int, *, request_code: str | None,
                       request_name: str | None, position: str | None,
                       is_new_replace: str | None, company: str | None,
                       department: str | None, section: str | None,
                       direct_supervisor: str | None, buddy: str | None,
                       head_count: Any, type_: str | None, reason: str | None,
                       requested_by: str | None, acknowledge_by_1: str | None,
                       acknowledge_by_2: str | None, actor: str | None = None,
                       revision: Any = None) -> dict[str, Any]:
        """Update one hiring request, recording `actor` (users.email) as the last
        editor. head_count is coerced to an int or NULL.

        `revision` is the revision the editor loaded: if someone else saved since,
        nothing is written and the result carries `conflict=True` plus the current
        row (who edited it and when). None skips that check. Returns {"ok": True,
        "revision": n}, {"ok": False, "conflict": True, "request": {...}} or
        {"ok": False, "missing": True}."""
        try:
            hc = int(str(head_count).strip()) if str(head_count or "").strip() else None
        except (TypeError, ValueError):
            hc = None
        rev = _int_or_none(revision)
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE dbo.requests SET request_code = ?, request_name = ?, [position] = ?, "
            "is_new_replace = ?, company = ?, department = ?, section = ?, "
            "direct_supervisor = ?, buddy = ?, head_count = ?, [type] = ?, reason = ?, "
            "requested_by = ?, acknowledge_by_1 = ?, acknowledge_by_2 = ?, "
            f"updated_by = ?, updated_at = {THAI_NOW}, revision = revision + 1 "
            "OUTPUT INSERTED.revision "
            "WHERE request_id = ?" + ("" if rev is None else " AND revision = ?"),
            (request_code or None), (request_name or None), (position or None),
            (is_new_replace or None), (company or None), (department or None),
            (section or None), (direct_supervisor or None), (buddy or None), hc,
            (type_ or None), (reason or None), (requested_by or None),
            (acknowledge_by_1 or None), (acknowledge_by_2 or None), (actor or None),
            request_id, *(() if rev is None else (rev,)))
        row = cur.fetchone()
        self.conn.commit()
        if row:
            return {"ok": True, "revision": int(row[0])}
        current = self.get_request(request_id)
        if current is None:
            return {"ok": False, "missing": True}
        return {"ok": False, "conflict": True, "request": current}

    REQUEST_STATUSES = ("doing", "completed")

    def set_request_status(self, request_id: int, status: str,
                           actor: str | None = None) -> dict[str, Any] | None:
        """Mark a hiring request 'completed' (recording `actor` and the time) or
        back to 'doing' (clearing both). Leaves the form fields and `revision`
        alone, so an edit form open elsewhere still saves cleanly. Returns the
        updated row, or None if the id doesn't exist."""
        if status not in self.REQUEST_STATUSES:
            raise ValueError("status must be doing or completed.")
        cur = self.conn.cursor()
        if status == "completed":
            cur.execute(
                f"UPDATE dbo.requests SET status = 'completed', completed_by = ?, "
                f"completed_at = {THAI_NOW} WHERE request_id = ? AND status <> 'completed'",
                (actor or None), request_id)
        else:
            cur.execute(
                "UPDATE dbo.requests SET status = 'doing', completed_by = NULL, "
                "completed_at = NULL WHERE request_id = ?", request_id)
        self.conn.commit()
        return self.get_request(request_id)

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
            "a.university, a.major, a.ai_extract_json, a.exam_sent_by "
            "FROM dbo.applicants a LEFT JOIN dbo.jobs j ON j.job_id = a.job_id "
            "WHERE a.application_id = ?", application_id)
        r = cur.fetchone()
        if not r:
            return None
        return {"resume_path": r[0], "resume_downloaded": bool(r[1]),
                "full_name_edit": r[2], "full_name_jobdb": r[3],
                "job_title": r[4], "ai_summary": r[5], "name_title": r[6],
                "university": r[7], "major": r[8], "ai_extract_json": r[9],
                "exam_sent_by": r[10]}

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
            "resume_path, reply_received, reply_at, reply_subject, exam_sent_by "
            "FROM dbo.applicants WHERE application_id = ?", application_id)
        r = cur.fetchone()
        if not r:
            return None
        return {"email": r[0], "is_sent_exam": bool(r[1]),
                "exam_sent_at": r[2],            # raw naive Thai datetime (convert in caller)
                "full_name_edit": r[3], "full_name_jobdb": r[4], "resume_path": r[5],
                "reply_received": (None if r[6] is None else bool(r[6])),
                "reply_at": r[7].isoformat(sep=" ", timespec="minutes") if r[7] else None,
                "reply_subject": r[8],
                "exam_sent_by": r[9]}            # None = sent before the column existed

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

    # ---- users: the people who use the board (multi-user Phases 2-3) ----------
    _USER_COLS = ("user_id, email, name, firstname, mobile, tel, prefix, role, is_active, "
                  "username, is_approved, password_hash, last_login_at")
    USER_ROLES = ("admin", "hr")
    _USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,50}$")
    MIN_PASSWORD = 8

    @staticmethod
    def _row_to_user(r: Any, with_hash: bool = False) -> dict[str, Any]:
        """A users row as the API exposes it: never the hash itself (has_password
        says whether one is set); `with_hash` adds it under `_password_hash` for the
        login check only."""
        role = r[7] or "hr"
        out = {"user_id": r[0], "email": r[1], "name": r[2], "firstname": r[3],
               "mobile": r[4], "tel": r[5], "prefix": r[6],
               "role": "hr" if role == "recruiter" else role, "is_active": bool(r[8]),
               "username": r[9], "is_approved": bool(r[10]), "has_password": bool(r[11]),
               "last_login_at": r[12].isoformat(sep=" ", timespec="minutes") if r[12] else None}
        if with_hash:
            out["_password_hash"] = r[11]
        return out

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        r = self.conn.cursor().execute(
            f"SELECT {self._USER_COLS} FROM dbo.users WHERE user_id = ?", int(user_id)).fetchone()
        return self._row_to_user(r) if r else None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """For the sign-in check: the row (with `_password_hash`) or None."""
        r = self.conn.cursor().execute(
            f"SELECT {self._USER_COLS} FROM dbo.users WHERE username = ?",
            (username or "").strip()).fetchone()
        return self._row_to_user(r, with_hash=True) if r else None

    def admin_has_password(self) -> bool:
        """False until the first Admin account has been set up (/setup)."""
        return bool(self.conn.cursor().execute(
            "SELECT TOP 1 1 FROM dbo.users WHERE role = 'admin' AND is_active = 1 "
            "AND password_hash IS NOT NULL").fetchone())

    def first_admin_without_password(self) -> dict[str, Any] | None:
        """The seeded Admin row that /setup turns into the first real account."""
        r = self.conn.cursor().execute(
            f"SELECT TOP 1 {self._USER_COLS} FROM dbo.users WHERE role = 'admin' "
            "AND password_hash IS NULL ORDER BY user_id").fetchone()
        return self._row_to_user(r) if r else None

    def register_user(self, data: dict[str, Any]) -> dict[str, Any]:
        """Self-registration: an HR account that cannot sign in until an Admin
        approves it. Username and password are required here."""
        if not str(data.get("username") or "").strip():
            raise ValueError("Please choose a username.")
        if not data.get("password"):
            raise ValueError("Please choose a password.")
        return self.save_user({**data, "role": "hr", "is_active": True, "is_approved": False})

    def approve_user(self, user_id: int, by_email: str | None) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(f"UPDATE dbo.users SET is_approved = 1, approved_by = ?, approved_at = {THAI_NOW} "
                    "WHERE user_id = ?", by_email, int(user_id))
        self.conn.commit()
        return self.get_user_by_id(user_id)

    def touch_login(self, user_id: int) -> None:
        self.conn.cursor().execute(
            f"UPDATE dbo.users SET last_login_at = {THAI_NOW} WHERE user_id = ?", int(user_id))
        self.conn.commit()

    def count_pending_users(self) -> int:
        return int(self.conn.cursor().execute(
            "SELECT COUNT(*) FROM dbo.users WHERE is_approved = 0 AND is_active = 1").fetchone()[0])

    def list_users(self, active_only: bool = False) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        sql = f"SELECT {self._USER_COLS} FROM dbo.users"
        if active_only:
            sql += " WHERE is_active = 1"
        cur.execute(sql + " ORDER BY is_active DESC, name")
        return [self._row_to_user(r) for r in cur.fetchall()]

    def get_user_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        """The ACTIVE user filing mail under `prefix` (how a machine identifies its
        recruiter before the M365 login exists)."""
        r = self.conn.cursor().execute(
            f"SELECT {self._USER_COLS} FROM dbo.users WHERE prefix = ? AND is_active = 1",
            (prefix or "").strip()).fetchone()
        return self._row_to_user(r) if r else None

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        r = self.conn.cursor().execute(
            f"SELECT {self._USER_COLS} FROM dbo.users WHERE email = ?",
            (email or "").strip()).fetchone()
        return self._row_to_user(r) if r else None

    def save_user(self, data: dict[str, Any]) -> dict[str, Any]:
        """Insert (no user_id) or update one user. Raises ValueError with a message
        fit for the UI when a required field is missing or email/prefix/username
        clash with another user. `password` (optional) is hashed; blank keeps the
        current one. `is_approved` defaults to True (Admin-created users can sign
        in at once); register_user passes False. Returns the saved row."""
        def _s(k: str, n: int) -> str:
            return str(data.get(k) or "").strip()[:n]
        def _b(k: str, default: bool) -> int:
            v = data.get(k, default)
            return 1 if v in (True, 1, "1", "true", "on") else 0
        email, name, prefix = _s("email", 300), _s("name", 200), _s("prefix", 20)
        if not email or not name or not prefix:
            raise ValueError("Email, name and folder prefix are required.")
        role = _s("role", 20) or "hr"
        role = "hr" if role == "recruiter" else role
        if role not in self.USER_ROLES:
            raise ValueError("Role must be Admin or HR.")
        username = _s("username", 100) or None
        if username and not self._USERNAME_RE.match(username):
            raise ValueError("Username: 3-50 letters, digits, dots, dashes or underscores.")
        password = data.get("password") or None          # None/blank = keep the current one
        if password is not None and len(str(password)) < self.MIN_PASSWORD:
            raise ValueError(f"Password must be at least {self.MIN_PASSWORD} characters.")
        uid = data.get("user_id")
        uid = int(uid) if uid not in (None, "", 0, "0") else None
        cur = self.conn.cursor()
        clash = cur.execute(
            "SELECT TOP 1 email, prefix, username FROM dbo.users "
            "WHERE (email = ? OR prefix = ? OR (username IS NOT NULL AND username = ?)) "
            "AND user_id <> ISNULL(?, -1)", email, prefix, username or "", uid).fetchone()
        if clash:
            what = ("email" if clash[0] == email else
                    "folder prefix" if clash[1] == prefix else "username")
            raise ValueError(f"Another user already has that {what}.")
        vals: list[Any] = [email, name, _s("firstname", 100) or None, _s("mobile", 50) or None,
                           _s("tel", 50) or None, prefix, role, _b("is_active", True), username,
                           _b("is_approved", True)]
        pw_set = ", password_hash = ?" if password is not None else ""
        pw_vals = [hash_password(str(password))] if password is not None else []
        if uid is None:
            cur.execute(
                "INSERT INTO dbo.users (email, name, firstname, mobile, tel, prefix, role, "
                "is_active, username, is_approved, password_hash) OUTPUT INSERTED.user_id "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", *vals, *(pw_vals or [None]))
            uid = int(cur.fetchone()[0])
        else:
            cur.execute(
                "UPDATE dbo.users SET email = ?, name = ?, firstname = ?, mobile = ?, tel = ?, "
                f"prefix = ?, role = ?, is_active = ?, username = ?, is_approved = ?{pw_set} "
                "WHERE user_id = ?", *vals, *pw_vals, uid)
            if cur.rowcount == 0:
                raise ValueError("User not found.")
        self.conn.commit()
        r = cur.execute(f"SELECT {self._USER_COLS} FROM dbo.users WHERE user_id = ?", uid).fetchone()
        return self._row_to_user(r)

    def seed_default_user(self, **fields: Any) -> dict[str, Any] | None:
        """First run only: when the users table is empty, insert one admin from the
        given fields (the signature defaults + this machine's prefix) so the board
        has a recruiter to sign as. Returns the row inserted, or None if users exist."""
        if self.conn.cursor().execute("SELECT TOP 1 1 FROM dbo.users").fetchone():
            return None
        return self.save_user({**fields, "role": "admin", "is_active": True})

    # ---- email templates: the shared store behind email_kit.templates ---------
    def load_email_templates(self) -> list[dict[str, Any]] | None:
        """Every template doc in display order, or None when the table is empty so
        email_kit.templates falls back to (and migrates) the JSON file."""
        cur = self.conn.cursor()
        cur.execute("SELECT doc FROM dbo.email_templates ORDER BY sort_order, template_id")
        rows = cur.fetchall()
        if not rows:
            return None
        out: list[dict[str, Any]] = []
        for (doc,) in rows:
            try:
                t = json.loads(doc)
            except ValueError:
                continue
            if isinstance(t, dict):
                out.append(t)
        return out

    def save_email_templates(self, templates: list[dict[str, Any]],
                             actor: str | None = None) -> None:
        """Replace the whole template list (the store's unit of change) in one
        transaction, stamping who saved it."""
        cur = self.conn.cursor()
        try:
            cur.execute("DELETE FROM dbo.email_templates")
            for i, t in enumerate(templates):
                cur.execute(
                    "INSERT INTO dbo.email_templates (template_id, doc, sort_order, updated_by, "
                    f"updated_at) VALUES (?, ?, ?, ?, {THAI_NOW})",
                    str(t.get("id")), json.dumps(t, ensure_ascii=False), i, actor)
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()

    def set_job_active(self, job_id: str, is_active: bool) -> None:
        self.conn.cursor().execute(
            "UPDATE dbo.jobs SET is_active = ? WHERE job_id = ?",
            1 if is_active else 0, job_id)
        self.conn.commit()


if __name__ == "__main__":
    raise SystemExit(0 if ping() else 1)
