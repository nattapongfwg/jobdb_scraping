# SEEK Employer Portal Scraper → SQL Server

Logs into the SEEK / JobsDB Thailand **employer** portal (`https://th.employer.seek.com/`),
extracts applicant/candidate information into a local **SQL Server** database, and downloads
each applicant's resume/CV into `resume/`.

**Runs natively on Windows** (Python 3.14) against a local **SQL Server Express**
(`localhost\SQLEXPRESS`) using **Windows Authentication** — no TCP/IP, firewall, or `sa`
setup required.

> Use this only against **your own** employer account and your own applicants' data. The
> script reuses your manually-authenticated session and rate-limits requests.

## Login & the CAPTCHA (important)

SEEK's sign-in uses a **"Verify you are human" reCAPTCHA** plus Auth0 SSO, so login can't be
automated and the session does **not** reliably survive a browser restart. The working model
is therefore **single-session**: each run logs in interactively, then scrapes immediately.

- Always run scrapes with **`--headed`**. When the browser opens, **solve the CAPTCHA and
  click *Sign in*** (your email/password are pre-filled). The script then scrapes in the same
  session.
- Your login may show a **"Select an account"** page. Set **`ADVERTISER_ID`** in `.env` to the
  account that owns the jobs you want (e.g. `61193320`); the script forces that scope.
- **Email & phone** aren't shown in the portal DOM — they're parsed from each downloaded
  **resume PDF** (via `pypdf`) and stored alongside name/applied-date.

## Architecture

| File | Role |
|------|------|
| `config.py` | Loads `.env`; builds the SQL Server connection (named instance + Windows/SQL auth). |
| `schema.sql` | Idempotent DDL: `dbo.jobs`, `dbo.applicants`. |
| `db.py` | pyodbc connect, auto-creates DB + schema, `MERGE` upserts, `ping()`. |
| `scraper.py` | Playwright login/session, job + applicant scraping, resume download. **Selectors at top.** |
| `main.py` | CLI orchestrator. |

## Setup (Windows)

From **PowerShell** in the project folder (`E:\jobdb_scraping`):

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

This creates `.venv`, installs dependencies, and downloads Chromium. (Already done on this
machine.) Then copy and review config:

```powershell
copy .env.example .env   # then edit .env if needed
```

`.env` is preconfigured for this machine: `DB_HOST=localhost`, `DB_INSTANCE=SQLEXPRESS`,
`DB_TRUSTED=yes` (Windows Auth). To use a SQL login instead, set `DB_TRUSTED=no` +
`DB_USER`/`DB_PASSWORD`.

## Run (use `.venv\Scripts\python.exe`)

```powershell
# 1) Test DB + create database/tables (already verified working)
.\.venv\Scripts\python.exe -c "import db; db.ping()"

# 2) Pick a job ad by its TITLE and download all its resumes
.\.venv\Scripts\python.exe main.py --headed --job-title "Software Implementer"

# 3) Small dry run (cap to 3 applicants)
.\.venv\Scripts\python.exe main.py --headed --job-title "Software Implementer" --limit 3

# 4) Full run — all open jobs, all applicants
.\.venv\Scripts\python.exe main.py --headed
```

Flags:
- `--job-title "<text>"` — scrape open jobs whose title contains this text (case-insensitive).
  If nothing matches, it lists the available open job titles.
- `--job-id <ID>` — restrict to one job by numeric id.
- `--limit N` — cap applicants per job (for testing).
- `--no-resumes` — skip resume downloads. `-v` — verbose.
- `--redownload` — re-fetch resumes even if already saved (see dedup below).
- `--discover` — dump page structure to `debug/` if SEEK's markup changes.

> Always use `--headed` so you can solve the CAPTCHA. A run without `--headed` fails at login.

### Duplicate handling
By default, if a resume for an applicant was already downloaded (a file containing that
`application_id` exists in `resume/`), the scraper **skips re-downloading it** and
reuses the existing file — so re-running the same job only fetches *new* applicants. The run
summary reports `skipped(dupes)=N`. Pass `--redownload` to force fetching them again.

## What gets captured

Per applicant, into `dbo.applicants`: `application_id` (SEEK's applicationCorrelationId),
`full_name`, `applied_at`, `email` + `phone` (parsed from the resume PDF), `resume_path`,
and a `raw_json` snapshot. The resume PDF is saved to `resume/<name>_<application_id>.pdf`.

## View your data

In **SQL Server Management Studio** (or Azure Data Studio): connect to
`localhost\SQLEXPRESS` (Windows Authentication) → database `jobdb_scraping` → tables
`dbo.applicants`, `dbo.jobs`.

## Maintenance notes
- Re-runs **update** rows (keyed on `application_id`/`job_id`) — no duplicates.
- All portal URLs/selectors live in the `SELECTORS` dict at the top of `scraper.py`. If SEEK
  changes its markup, run `--discover` and update that one block; the scraper logs warnings
  and saves screenshots/HTML to `debug/` when a selector misses.
- `.env` and `.browser_profile/` hold secrets/session state and are git-ignored.
