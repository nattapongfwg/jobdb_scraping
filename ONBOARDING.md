# Onboarding — JobDB Recruitment App (for an AI agent)

You are an AI coding agent (e.g. Claude Code) helping a new user **set up, run, and use**
this project on their machine. This document is written for *you*, the agent — it tells
you what the project is, how to install it step-by-step, what to verify after each step,
and the non-obvious gotchas that will otherwise waste the user's time.

For the human-facing version, see `INSTALL.md`. This file goes further: it explains the
*why*, the failure modes, and how to operate the running app.

> **Adopting this for a different person or company?** See **`CUSTOMIZE.md`** — it lists
> every hardcoded value (email signatures, phone, company name, SharePoint/form links,
> dropdown lists) that must change so the app stops sending the original owner's details.

---

## 1. What this project is

A **recruitment pipeline tool** for Freewill Solutions HR, built on top of a SEEK /
JobDB employer-portal scraper. Two halves:

1. **Scraper** (`main.py` + `scraper.py`) — a Playwright browser automation that logs into
   the SEEK employer portal, walks every candidate folder for a job, and stores applicants
   + résumés into a local **SQL Server** database.
2. **HR web board** (`webapp.py` + `templates/` + `static/`) — a Flask app (themed
   "Recruit Alchemists", Fullmetal Alchemist styling) at **http://localhost:2757** where HR
   manages candidates through a branching pipeline, sends templated emails, generates Excel
   evaluation forms, creates Outlook calendar events, and gets AI résumé summaries.

**Pipeline stages:** Not Interest ⇄ Pending → Sent Exam → Shortlist → Interview →
Evaluation → Offered (moves are one-step, server-enforced).

### Module map

| File | Role |
|------|------|
| `main.py` | Scraper CLI entry point (argparse; `--headed`, `--job-id`, `--list-jobs-json`, …) |
| `scraper.py` | Playwright scraping logic (login, folders, cards, résumé download) |
| `webapp.py` | Flask app — all routes + the `ScrapeManager` / `EmailAuth` background managers |
| `db.py` | SQL Server access (pyodbc), schema bootstrap, stage logic, all queries |
| `config.py` | Loads `.env` into a `Config`; builds the ODBC connection string |
| `mailer.py` | Microsoft Graph (delegated device-code) — send mail, create draft, create calendar event |
| `email_kit/` | Email package: `templates.py` (editable named templates, persisted to `email_kit/email_template.json`) + `signature.py` (the shared recruiter signature) |
| `summarizer.py` | AI résumé summaries via OpenAI chat completions (`requests`, no `openai` pkg) |
| `evaluation.py` / `make_eval_template.py` | Excel evaluation-form generation (surgical zip edit, preserves images) |
| `shortlist.py` / `offer.py` | Shortlist folder moves + OneDrive share links; offer step |
| `schema.sql` | Idempotent `ALTER`s — applied automatically by `db.ensure_schema()` |

---

## 2. CRITICAL environment facts (read before you touch anything)

- **This is a Windows application.** It requires **SQL Server Express** with **Windows
  Authentication**, the **ODBC Driver 18 for SQL Server**, and (for email/calendar)
  **Microsoft Graph** against the user's Microsoft 365 tenant. It will not run fully on
  Mac/Linux.
- **You may be operating from WSL.** If your shell is WSL/Linux but the project lives on a
  Windows drive (e.g. `/mnt/e/jobdb_scraping`), **do not use a Linux Python.** Drive the
  **Windows venv Python** instead so it gets pyodbc + Windows Auth to SQL Server:
  ```bash
  ./.venv/Scripts/python.exe script.py
  ```
  On native Windows PowerShell the equivalent is `.\.venv\Scripts\python.exe script.py`.
- **The repo contains code only.** These are git-ignored and must be rebuilt/brought over:
  `.env` (secrets), `.venv/` (Python env), `.graph_token_cache.json` (Outlook sign-in),
  `email_kit/email_template.json` (custom templates), `resume/`, `*.log`, the browser profile, and
  the SQL Server data itself.
- **Web app port is 2757**, not 5000.
- **Restart `webapp.py` after editing ANY backend `.py` file or `.env`.** Flask is not
  reloading them for you. (Static-only edits to `static/` or `templates/` just need a hard
  browser refresh.)
- **Detached webapp survives a naive kill on Windows.** A backgrounded webapp keeps holding
  port 2757 and serving the OLD code. To fully kill it:
  ```
  netstat -ano | findstr :2757 | findstr LISTENING   →   taskkill /F /PID <pid>
  ```
  Never launch the webapp with a trailing `&` inside a background task.
- **Everything is UTF-8 / Thai-safe.** Subprocesses (the scraper) must run with
  `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`, or Thai names/titles crash on cp1252. The
  webapp already sets this when it spawns the scraper.
- **All timestamps are Thailand time** (Asia/Bangkok) — handled in SQL via
  `AT TIME ZONE 'SE Asia Standard Time'`. Don't "fix" this to UTC.
- **Shared mailbox, but per-machine database — "each teammate owns their candidates."**
  Every teammate signs into the *same* "Recruit" Graph mailbox, so all sent exams (filed
  into per-prefix `<Prefix>_Sent_Exam` folders), drafts, replies, calendar events, and
  OneDrive shortlist folders land in one shared place. But the pipeline **database is local
  to each machine** (`.env` `DB_HOST=localhost\SQLEXPRESS`, Windows Auth) — there is **no
  cross-machine sync**. Everything the pipeline *knows* lives in that local DB: the candidate
  rows (only on the machine that scraped that job), `stage` + entry dates, `is_sent_exam` /
  `exam_sent_at`, the cached `reply_received` / `reply_at` / `reply_subject`, `ai_summary`,
  name/email edits, and the local résumé + `Email_Reply_Exam/<name>/` files. Consequences
  to keep in mind:
  - **Reply detection can't be handed off.** Check Replies only runs when the **local** DB
    has `exam_sent_at` for that candidate (else it returns `skipped`, `webapp.py:420`). The
    reply itself is in the shared Inbox and *visible* to everyone, but only the machine that
    **sent** the exam can detect/record it. So the person who sends an exam must also be the
    one who checks its replies and drives the rest of that candidate's pipeline.
  - **No shared visibility.** One teammate can't see another's pipeline state. If two people
    scrape the **same job**, both could email the same candidate (two exams in the shared
    mailbox, neither DB aware of the other). **Avoid two people working the same job_id.**
  - This is by design for the **one-owner-per-candidate** workflow. If the team ever moves to
    a shared pool (anyone handles anyone's candidates), the fix is to point every machine's
    `DB_HOST` at one central SQL Server (needs TCP + firewall + auth setup) — not in scope today.

---

## 3. Install — step by step

Run these on **Windows** (PowerShell), or from WSL using the `./.venv/Scripts/python.exe`
form noted above. Verify after each step before moving on.

### Step 1 — Prerequisites (install once)
1. **Python 3.12+** — tick "Add Python to PATH" during install.
2. **Git**.
3. **SQL Server Express** — install as a named instance. The user's existing setup uses
   instance **`SQLEXPRESS`** with Windows Auth (`localhost\SQLEXPRESS`).
4. **ODBC Driver 18 for SQL Server** (separate Microsoft download).
5. *(optional)* SSMS, only for browsing/backing up the DB by hand.

### Step 2 — Get the code
```powershell
cd E:\
git clone https://github.com/nattapongfwg/jobdb_scraping.git
cd jobdb_scraping
```

### Step 3 — Build the Python env + browser
```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
```
This creates `.venv`, installs `requirements.txt` (playwright, pyodbc, python-dotenv, pypdf,
flask, msal, requests), and downloads the Playwright Chromium browser.

**Verify:** `.\.venv\Scripts\python.exe -c "import flask, pyodbc, playwright; print('ok')"`

### Step 4 — Create the secrets file (`.env`)
The repo cannot supply this. Copy the template and fill it in:
```powershell
copy .env.example .env
notepad .env
```
Key settings (see §4 for what to put):

| Setting | Purpose |
|---------|---------|
| `SEEK_EMAIL` / `SEEK_PASSWORD` / `ADVERTISER_ID` | SEEK employer-portal login + which account |
| `DB_INSTANCE=SQLEXPRESS`, `DB_TRUSTED=yes` | Local SQL Server, Windows Auth |
| `DB_DRIVER=ODBC Driver 18 for SQL Server` | Must EXACTLY match the installed driver name |
| `ONEDRIVE_BASE` | Base OneDrive folder for Shortlists/ + Email_Reply_Exam/ — **auto-detected; leave blank** (see note below) |
| `RECRUITER_NAME` / `RECRUITER_FIRSTNAME` / `RECRUITER_MOBILE` / `RECRUITER_TEL` / `RECRUITER_EMAIL` | **Per-teammate email signature** — set so emails from this machine sign as you |
| `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` | Email / Outlook draft / calendar (Microsoft Graph) |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | AI résumé summaries |

> **Paths & portability:** the résumé folder (`resume/`) defaults to `<project>/resume`,
> so it follows the project automatically — no change needed (override with `RESUME_DIR`
> only if you want it elsewhere). **`ONEDRIVE_BASE` now auto-detects** per machine: when
> blank, the app joins Windows' own OneDrive location (the `OneDriveCommercial`/`OneDrive`
> env var) with `Recruit's files - Recruitment\Recruite_Scraping`, so each teammate's own
> `C:\Users\<them>\OneDrive - …` resolves automatically. Set it only if your synced library
> lives elsewhere — a `OneDrive base folder not found` warning at startup flags a bad path.
> It only affects the Shortlist and email-reply features, not basic scraping or résumé
> viewing. **Signature is per-teammate:** set the `RECRUITER_*` vars so drafts/sent mail from
> this machine sign as you (blank → built-in default).
> Note: résumé paths stored in the DB are absolute — if you *restore an old database*
> onto a project at a different path, re-download (`--redownload`) to refresh them.

> **Fastest path:** copy the existing `.env` from the old computer (USB/secure transfer)
> rather than refilling by hand. Same for `.graph_token_cache.json` and
> `email_kit/email_template.json` if you want sign-in state and custom templates preserved.

### Step 5 — Test the database connection
```powershell
.\.venv\Scripts\python.exe -c "import db; db.ping()"
```
`db.ping()` connects, **creates the `jobdb_scraping` database if missing**, and applies the
schema. You want to see **"Connected OK"**. If it fails, see §6.

### Step 6 — Run
**Web board:**
```powershell
.\.venv\Scripts\python.exe webapp.py
```
Open **http://localhost:2757**.

**Scraper** (always `--headed` so the user can solve the CAPTCHA):
```powershell
.\.venv\Scripts\python.exe main.py --headed --login-only          # verify login works
.\.venv\Scripts\python.exe main.py --headed --job-id <ID> --limit 3   # small dry run
```

---

## 4. Configuration & external services — what the user must provide

These are **the user's own accounts** — you cannot create them. Guide the user to supply:

1. **SEEK employer portal** login (`SEEK_EMAIL` / `SEEK_PASSWORD`) and the `ADVERTISER_ID`
   for the account they want to scrape (blank = first listed).
2. **SQL Server Express** running locally as instance `SQLEXPRESS` with Windows Auth. The
   app bootstraps the database and tables itself — no manual schema work.
3. **Microsoft Graph / Azure AD app registration** for email, Outlook drafts, and calendar:
   - Auth is **DELEGATED device-code flow**, NOT app-only client-credentials. (App-only was
     tried and returned 403 — the tenant only granted delegated permissions.)
   - `.env` needs `GRAPH_TENANT_ID` + `GRAPH_CLIENT_ID` only. `GRAPH_CLIENT_SECRET` is
     present but **unused** under device-code. *(Note: `.env.example` still describes the old
     app-only "Mail.Send application" model — that comment is stale; the live app uses
     delegated device-code.)*
   - Azure requirement: **Authentication → "Allow public client flows" = Yes**.
   - Delegated scopes granted on the tenant: **Mail.ReadWrite, Files.ReadWrite,
     Calendars.ReadWrite** (plus Mail.Send, User.Read).
   - **First-time sign-in is interactive:** in the web UI's "Config Email Template" modal,
     click **"Sign in to email"**, then open microsoft.com/devicelogin and enter the code.
     The token is cached to `.graph_token_cache.json`; later sends refresh silently.
4. **OpenAI API key** (`OPENAI_API_KEY`) for résumé summaries. Model is configurable via
   `OPENAI_MODEL` (the user runs `gpt-5.4-nano`).

---

## 5. Using the app (so you can guide the user)

- **Scrape candidates from the browser:** on the landing page use the **"Scrape from SEEK"**
  panel → *Fetch active jobs* → pick a job → *Download candidates* (live progress bar). The
  scraper runs `--headed`, so the user solves the CAPTCHA in the popped browser window.
  Re-downloads fast-skip already-downloaded candidates (use `--redownload` to force).
- **Pipeline board** (`/job/<id>`): drag or click to move a candidate one stage. Moving to
  **Sent Exam** sends the templated email (and only advances if the send succeeds). Other
  buttons: Shortlist (moves résumé folders to OneDrive + share link), Interview (Outlook
  calendar event + Teams meeting), Evaluation (popup form → filled Excel + Outlook draft).
- **Status Tracking** (`/tracking`): all candidates, filters, inline-edit name/email/phone,
  read-only evaluation fields.
- **Email templates:** "Config Email Template" modal — multiple named templates, multiple
  attachments, HTML body + preview. All mail sends FROM the signed-in mailbox.

---

## 6. Troubleshooting (most likely failures, in order)

| Symptom | Cause / fix |
|---------|-------------|
| `db.ping()` fails | SQL Server (SQLEXPRESS) service not running; wrong instance name; `DB_DRIVER` in `.env` doesn't match the installed ODBC driver string exactly. |
| Using Linux Python from WSL → pyodbc / auth errors | Use `./.venv/Scripts/python.exe` (the Windows venv), not a Linux interpreter. |
| Scraper fails at login | Must run with `--headed` and solve the CAPTCHA manually. Headless session reuse is unreliable (Auth0) — that's expected. |
| Thai names garbled / cp1252 crash | A subprocess wasn't launched with `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`. |
| "Missing Graph config" when emailing | `GRAPH_*` values blank in `.env`; fill them and restart `webapp.py`. |
| Email features fail with 403 | Don't switch to app-only credentials. Use delegated device-code; click "Sign in to email" in the UI. |
| Edited a `.py` but nothing changed | You didn't restart `webapp.py`. On Windows, a detached process may still hold port 2757 — `taskkill /F /PID <pid>` first (see §2). |
| Webapp serving old template/code | Same stale-detached-process issue — fully kill the PID on port 2757, then relaunch. |

---

## 7. If the user wants you to develop, not just install

- Make **surgical** changes — the codebase is already clean and the user prefers minimal,
  targeted edits over rewrites.
- Schema changes go in `schema.sql` as **idempotent ALTERs** (applied by
  `db.ensure_schema()`); the scraper upsert must never overwrite HR-edited columns.
- After backend edits: **kill + restart `webapp.py`** and re-verify in the browser.
- `Evaluate_Original.xlsx` is the HR master template (kept in repo, untouched reference);
  `make_eval_template.py` regenerates `Evaluation_Template.xlsx` from it. Excel edits are
  surgical zip rewrites (never openpyxl — it would destroy the 8 embedded images).
