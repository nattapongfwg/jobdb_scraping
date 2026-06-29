-- Schema for SEEK employer-portal scraping.
-- Idempotent: safe to run on every startup.
-- All scraped_at timestamps are stored in THAILAND time (Asia/Bangkok, UTC+7).

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'jobs')
BEGIN
    CREATE TABLE dbo.jobs (
        job_id        NVARCHAR(100)  NOT NULL PRIMARY KEY,
        title         NVARCHAR(500)  NULL,
        location      NVARCHAR(300)  NULL,
        url           NVARCHAR(1000) NULL,
        is_active     BIT            NOT NULL DEFAULT 1,
        scraped_at    DATETIME2      NOT NULL
            DEFAULT CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'SE Asia Standard Time' AS DATETIME2)
    );
END;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'applicants')
BEGIN
    CREATE TABLE dbo.applicants (
        application_id    NVARCHAR(100)  NOT NULL PRIMARY KEY,
        job_id            NVARCHAR(100)  NULL,
        full_name_jobdb   NVARCHAR(300)  NULL,   -- name as scraped from JobDB (read-only)
        full_name_edit    NVARCHAR(300)  NULL,   -- HR-editable copy of the name (UI)
        email             NVARCHAR(300)  NULL,
        phone             NVARCHAR(100)  NULL,
        expect_salary     NVARCHAR(100)  NULL,   -- screening answer เงินเดือนที่คาดหวัง (e.g. '30K')
        location          NVARCHAR(300)  NULL,
        applied_at        NVARCHAR(100)  NULL,   -- raw portal value; parse downstream if needed
        status            NVARCHAR(100)  NULL,
        resume_filename   NVARCHAR(500)  NULL,
        resume_path       NVARCHAR(1000) NULL,
        resume_downloaded BIT            NOT NULL DEFAULT 0,
        is_sent_exam      BIT            NOT NULL DEFAULT 0,
        exam_sent_at      DATETIME2      NULL,    -- Thailand time when the exam email was sent
        raw_json          NVARCHAR(MAX)  NULL,
        scraped_at        DATETIME2      NOT NULL
            DEFAULT CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'SE Asia Standard Time' AS DATETIME2),
        CONSTRAINT FK_applicants_jobs FOREIGN KEY (job_id) REFERENCES dbo.jobs(job_id)
    );
END;

-- Hiring requests (submitted from the "Request" page on the Job Postings board).
-- Standalone table: a manager records a request to open/replace a position.
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'requests')
BEGIN
    CREATE TABLE dbo.requests (
        request_id        INT            NOT NULL IDENTITY(1,1) PRIMARY KEY,
        request_code      NVARCHAR(100)  NULL,
        request_name      NVARCHAR(200)  NULL,
        [position]        NVARCHAR(200)  NULL,
        is_new_replace    NVARCHAR(20)   NULL,   -- 'New' or 'Replace'
        company           NVARCHAR(200)  NULL,
        department        NVARCHAR(200)  NULL,
        section           NVARCHAR(200)  NULL,
        direct_supervisor NVARCHAR(200)  NULL,
        buddy             NVARCHAR(200)  NULL,
        head_count        INT            NULL,
        [type]            NVARCHAR(20)   NULL,    -- 'Permanent' or 'Contract'
        reason            NVARCHAR(1000) NULL,    -- free-text reason for the request
        requested_by      NVARCHAR(200)  NULL,
        acknowledge_by_1  NVARCHAR(200)  NULL,
        acknowledge_by_2  NVARCHAR(200)  NULL,
        created_at        DATETIME2      NOT NULL
            DEFAULT CAST(SYSDATETIMEOFFSET() AT TIME ZONE 'SE Asia Standard Time' AS DATETIME2)
    );
END;

-- Add newer columns to pre-existing tables (idempotent).
IF COL_LENGTH('dbo.requests', 'request_code') IS NULL
    ALTER TABLE dbo.requests ADD request_code NVARCHAR(100) NULL;
IF COL_LENGTH('dbo.requests', 'company') IS NULL
    ALTER TABLE dbo.requests ADD company NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.requests', 'reason') IS NULL
    ALTER TABLE dbo.requests ADD reason NVARCHAR(1000) NULL;
IF COL_LENGTH('dbo.requests', 'requested_by') IS NULL
    ALTER TABLE dbo.requests ADD requested_by NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.requests', 'acknowledge_by_1') IS NULL
    ALTER TABLE dbo.requests ADD acknowledge_by_1 NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.requests', 'acknowledge_by_2') IS NULL
    ALTER TABLE dbo.requests ADD acknowledge_by_2 NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.jobs', 'is_active') IS NULL
    ALTER TABLE dbo.jobs ADD is_active BIT NOT NULL DEFAULT 1;
IF COL_LENGTH('dbo.applicants', 'is_sent_exam') IS NULL
    ALTER TABLE dbo.applicants ADD is_sent_exam BIT NOT NULL DEFAULT 0;
IF COL_LENGTH('dbo.applicants', 'exam_sent_at') IS NULL
    ALTER TABLE dbo.applicants ADD exam_sent_at DATETIME2 NULL;
-- Rename name columns (idempotent): full_name -> full_name_jobdb (scraped, read-only),
-- name_real -> full_name_edit (HR-editable). Older DBs still carry the old names.
IF COL_LENGTH('dbo.applicants', 'full_name_jobdb') IS NULL
   AND COL_LENGTH('dbo.applicants', 'full_name') IS NOT NULL
    EXEC sp_rename 'dbo.applicants.full_name', 'full_name_jobdb', 'COLUMN';
IF COL_LENGTH('dbo.applicants', 'full_name_edit') IS NULL
   AND COL_LENGTH('dbo.applicants', 'name_real') IS NOT NULL
    EXEC sp_rename 'dbo.applicants.name_real', 'full_name_edit', 'COLUMN';
IF COL_LENGTH('dbo.applicants', 'full_name_edit') IS NULL
    ALTER TABLE dbo.applicants ADD full_name_edit NVARCHAR(300) NULL;

-- HR hiring-pipeline columns (mirrors the Ezwow HR board). Stage advances one
-- step at a time: prescreen -> shortlist -> interview -> offered. The scraper's
-- upsert never touches these, so a candidate's pipeline position is preserved
-- across re-scrapes. Existing rows take the DEFAULTs (stage='prescreen').
IF COL_LENGTH('dbo.applicants', 'stage') IS NULL
    ALTER TABLE dbo.applicants ADD stage NVARCHAR(20) NOT NULL DEFAULT 'prescreen';
IF COL_LENGTH('dbo.applicants', 'cv_sent') IS NULL
    ALTER TABLE dbo.applicants ADD cv_sent BIT NOT NULL DEFAULT 0;
IF COL_LENGTH('dbo.applicants', 'shortlist_date') IS NULL
    ALTER TABLE dbo.applicants ADD shortlist_date DATE NULL;
IF COL_LENGTH('dbo.applicants', 'interview_date') IS NULL
    ALTER TABLE dbo.applicants ADD interview_date DATE NULL;
IF COL_LENGTH('dbo.applicants', 'offer_date') IS NULL
    ALTER TABLE dbo.applicants ADD offer_date DATE NULL;
IF COL_LENGTH('dbo.applicants', 'evaluation_date') IS NULL
    ALTER TABLE dbo.applicants ADD evaluation_date DATE NULL;
IF COL_LENGTH('dbo.applicants', 'nickname') IS NULL
    ALTER TABLE dbo.applicants ADD nickname NVARCHAR(100) NULL;
-- HR-editable honorific/prefix (Mr. / Ms. / Mrs.). NULL = unset.
IF COL_LENGTH('dbo.applicants', 'name_title') IS NULL
    ALTER TABLE dbo.applicants ADD name_title NVARCHAR(10) NULL;
-- Interview-evaluation form fields (captured from the Evaluation popup; interview_date
-- reuses the existing column). Bracketed because position/role can be SQL keywords.
IF COL_LENGTH('dbo.applicants', 'position') IS NULL
    ALTER TABLE dbo.applicants ADD [position] NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.applicants', 'role') IS NULL
    ALTER TABLE dbo.applicants ADD [role] NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.applicants', 'company') IS NULL
    ALTER TABLE dbo.applicants ADD company NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.applicants', 'department') IS NULL
    ALTER TABLE dbo.applicants ADD department NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.applicants', 'section') IS NULL
    ALTER TABLE dbo.applicants ADD section NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.applicants', 'interviewer') IS NULL
    ALTER TABLE dbo.applicants ADD interviewer NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.applicants', 'recruiter_name') IS NULL
    ALTER TABLE dbo.applicants ADD recruiter_name NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.applicants', 'expect_salary') IS NULL
    ALTER TABLE dbo.applicants ADD expect_salary NVARCHAR(100) NULL;
-- AI (ChatGPT) resume summary, generated when a candidate reaches "Wait Pre-screen".
IF COL_LENGTH('dbo.applicants', 'ai_summary') IS NULL
    ALTER TABLE dbo.applicants ADD ai_summary NVARCHAR(MAX) NULL;
IF COL_LENGTH('dbo.applicants', 'ai_summary_at') IS NULL
    ALTER TABLE dbo.applicants ADD ai_summary_at DATETIME2 NULL;
-- AI (ChatGPT) structured extraction from the résumé, generated alongside the
-- summary at "Wait Pre-screen". university/major are HR-editable; full_name overwrites
-- full_name_edit. ai_extract_json caches the raw suggestion so we don't re-call
-- the API and so university/major can pre-fill inputs without being auto-saved.
IF COL_LENGTH('dbo.applicants', 'university') IS NULL
    ALTER TABLE dbo.applicants ADD university NVARCHAR(300) NULL;
IF COL_LENGTH('dbo.applicants', 'major') IS NULL
    ALTER TABLE dbo.applicants ADD major NVARCHAR(300) NULL;
IF COL_LENGTH('dbo.applicants', 'ai_extract_json') IS NULL
    ALTER TABLE dbo.applicants ADD ai_extract_json NVARCHAR(MAX) NULL;
IF COL_LENGTH('dbo.applicants', 'ai_extract_at') IS NULL
    ALTER TABLE dbo.applicants ADD ai_extract_at DATETIME2 NULL;
-- Exam-reply detection (read from the signed-in mailbox). reply_received tri-state:
-- NULL = never checked, 0 = checked/no reply, 1 = replied. reply_at in Thai time.
IF COL_LENGTH('dbo.applicants', 'reply_received') IS NULL
    ALTER TABLE dbo.applicants ADD reply_received BIT NULL;
IF COL_LENGTH('dbo.applicants', 'reply_at') IS NULL
    ALTER TABLE dbo.applicants ADD reply_at DATETIME2 NULL;
IF COL_LENGTH('dbo.applicants', 'reply_subject') IS NULL
    ALTER TABLE dbo.applicants ADD reply_subject NVARCHAR(500) NULL;
IF COL_LENGTH('dbo.applicants', 'reply_checked_at') IS NULL
    ALTER TABLE dbo.applicants ADD reply_checked_at DATETIME2 NULL;
-- Per-stage entry timestamps (Thai time): the exact moment a candidate was MOVED
-- into each stage. Distinct from the HR-meaningful *_date columns above (e.g.
-- interview_date = scheduled interview day). Stamped once by db.set_stage and never
-- overwritten thereafter. NULL = the candidate never reached that stage.
IF COL_LENGTH('dbo.applicants', 'sent_exam_stamped_date') IS NULL
    ALTER TABLE dbo.applicants ADD sent_exam_stamped_date DATETIME2 NULL;
IF COL_LENGTH('dbo.applicants', 'shortlist_stamped_date') IS NULL
    ALTER TABLE dbo.applicants ADD shortlist_stamped_date DATETIME2 NULL;
IF COL_LENGTH('dbo.applicants', 'interview_stamped_date') IS NULL
    ALTER TABLE dbo.applicants ADD interview_stamped_date DATETIME2 NULL;
IF COL_LENGTH('dbo.applicants', 'evaluation_stamped_date') IS NULL
    ALTER TABLE dbo.applicants ADD evaluation_stamped_date DATETIME2 NULL;
IF COL_LENGTH('dbo.applicants', 'offered_stamped_date') IS NULL
    ALTER TABLE dbo.applicants ADD offered_stamped_date DATETIME2 NULL;
-- Job-offer popup inputs (captured from the Offer form; reused to rebuild the
-- offer-confirmation draft). interviewer reuses the existing column.
IF COL_LENGTH('dbo.applicants', 'offer_people_count') IS NULL
    ALTER TABLE dbo.applicants ADD offer_people_count NVARCHAR(20) NULL;
IF COL_LENGTH('dbo.applicants', 'offer_type') IS NULL
    ALTER TABLE dbo.applicants ADD offer_type NVARCHAR(20) NULL;
IF COL_LENGTH('dbo.applicants', 'offer_new_replace') IS NULL
    ALTER TABLE dbo.applicants ADD offer_new_replace NVARCHAR(500) NULL;
IF COL_LENGTH('dbo.applicants', 'offer_supervisor') IS NULL
    ALTER TABLE dbo.applicants ADD offer_supervisor NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.applicants', 'offer_buddy') IS NULL
    ALTER TABLE dbo.applicants ADD offer_buddy NVARCHAR(200) NULL;
IF COL_LENGTH('dbo.applicants', 'offer_expected_salary') IS NULL
    ALTER TABLE dbo.applicants ADD offer_expected_salary NVARCHAR(100) NULL;
IF COL_LENGTH('dbo.applicants', 'offer_current_salary') IS NULL
    ALTER TABLE dbo.applicants ADD offer_current_salary NVARCHAR(100) NULL;
IF COL_LENGTH('dbo.applicants', 'offer_start_date') IS NULL
    ALTER TABLE dbo.applicants ADD offer_start_date NVARCHAR(100) NULL;
IF COL_LENGTH('dbo.applicants', 'offer_experience') IS NULL
    ALTER TABLE dbo.applicants ADD offer_experience NVARCHAR(MAX) NULL;
IF COL_LENGTH('dbo.applicants', 'offer_recruiter_comments') IS NULL
    ALTER TABLE dbo.applicants ADD offer_recruiter_comments NVARCHAR(MAX) NULL;
IF COL_LENGTH('dbo.applicants', 'offer_interviewer_comments') IS NULL
    ALTER TABLE dbo.applicants ADD offer_interviewer_comments NVARCHAR(MAX) NULL;
-- offer_experience = free-text "Experience:" headline; offer_experience_ai = the
-- AI-generated 2-paragraph detail rendered as bullet points beneath it.
IF COL_LENGTH('dbo.applicants', 'offer_experience_ai') IS NULL
    ALTER TABLE dbo.applicants ADD offer_experience_ai NVARCHAR(MAX) NULL;
-- Stable per-candidate dedup key within a job: normalized name + applied_at.
-- SEEK's application_id (the selected=<uuid>) is regenerated every scraping
-- session, so it CANNOT be used to recognise a candidate across re-scrapes —
-- doing so created duplicate rows (and resurrected rejected/exam-sent candidates
-- back into Pending). candidate_key is derived from stable application content
-- (db.candidate_key) and is what the upsert MERGEs on. Backfilled for existing
-- rows by the one-off dedup migration; the upsert populates it going forward.
IF COL_LENGTH('dbo.applicants', 'candidate_key') IS NULL
    ALTER TABLE dbo.applicants ADD candidate_key NVARCHAR(450) NULL;
-- Free-text HR remark/note per candidate (editable in every pipeline stage, max 1000 chars).
IF COL_LENGTH('dbo.applicants', 'remark') IS NULL
    ALTER TABLE dbo.applicants ADD remark NVARCHAR(1000) NULL;
-- AI-computed experience (years), generated alongside the résumé extraction at
-- "Wait Pre-screen". exp_total = total work experience across all jobs;
-- exp_directly = experience matching the job-title keywords (always <= exp_total).
IF COL_LENGTH('dbo.applicants', 'exp_total') IS NULL
    ALTER TABLE dbo.applicants ADD exp_total DECIMAL(5,2) NULL;
IF COL_LENGTH('dbo.applicants', 'exp_directly') IS NULL
    ALTER TABLE dbo.applicants ADD exp_directly DECIMAL(5,2) NULL;
-- HR-editable salary fields captured at "Wait Pre-screen" (free text like expect_salary, e.g. '30K').
IF COL_LENGTH('dbo.applicants', 'current_salary_edit') IS NULL
    ALTER TABLE dbo.applicants ADD current_salary_edit NVARCHAR(100) NULL;
IF COL_LENGTH('dbo.applicants', 'minimum_expect_salary_edit') IS NULL
    ALTER TABLE dbo.applicants ADD minimum_expect_salary_edit NVARCHAR(100) NULL;
IF COL_LENGTH('dbo.applicants', 'expect_salary_edit') IS NULL
    ALTER TABLE dbo.applicants ADD expect_salary_edit NVARCHAR(100) NULL;
-- Hiring request a candidate is linked to (chosen at Wait Pre-screen, editable on
-- Sent Exam / Shortlist / Interview cards). Picking one fills position/company/
-- department/section (+ role from the job title) — see db.set_request_fields.
IF COL_LENGTH('dbo.applicants', 'request_id') IS NULL
    ALTER TABLE dbo.applicants ADD request_id INT NULL;
GO

-- Seed full_name_edit from full_name_jobdb for any rows that don't have it yet
-- (never overwrites an edited value, since those are non-NULL). Separate batch so
-- the newly-renamed/added column is resolvable.
UPDATE dbo.applicants SET full_name_edit = full_name_jobdb WHERE full_name_edit IS NULL;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_applicants_job_id')
    CREATE INDEX IX_applicants_job_id ON dbo.applicants(job_id);

-- Speeds up the dedup MERGE (ON job_id + candidate_key) and the pre-click
-- skip lookup. Non-unique: uniqueness is enforced in code by the MERGE.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_applicants_job_candkey')
    CREATE INDEX IX_applicants_job_candkey ON dbo.applicants(job_id, candidate_key);
