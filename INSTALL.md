# Installing on another computer

This guide sets up the **JobDB recruitment app** (SEEK scraper + HR web board) on a
fresh machine.

> **This is a Windows application.** It relies on **SQL Server Express** with
> **Windows Authentication**, plus Microsoft Outlook / Graph integration. It must be
> installed on **Windows** to run fully. (A Mac/Linux machine would need a different
> database setup and loses the Outlook calendar/draft features.)

> **Important:** this Git repository contains the *code* only. The secrets (`.env`),
> the database, the Python environment (`.venv`), and the browser are deliberately
> **excluded** from Git. Installing = clone the code, then rebuild everything around
> it (Steps below).

---

## 1. Prerequisites (install once, in order)

| # | Software | Notes |
|---|----------|-------|
| 1 | **Python 3.12+** | From [python.org](https://www.python.org/). Tick **"Add Python to PATH"** during install. |
| 2 | **Git** | From [git-scm.com](https://git-scm.com/) — needed to clone this repo. |
| 3 | **SQL Server Express** | The free edition, installed as a named instance **`SQLEXPRESS`**. The app auto-creates the `jobdb_scraping` database + tables on first run. |
| 4 | **ODBC Driver 18 for SQL Server** | Microsoft's separate download — the bridge Python uses to reach SQL Server. |
| 5 | *(optional)* **SQL Server Management Studio (SSMS)** | Only if you want to browse/back up the data by hand. |

---

## 2. Get the code

Open **PowerShell** where you want the project (e.g. `E:\`) and run:

```powershell
cd E:\
git clone https://github.com/nattapongfwg/jobdb_scraping.git
cd jobdb_scraping
```

---

## 3. Build the Python environment + browser

A setup script does this for you — it creates `.venv`, installs all dependencies from
`requirements.txt`, and downloads the Playwright Chromium browser the scraper uses:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
```

---

## 4. Create the secrets file (`.env`)

**This is the part the repo cannot give you** — `.env` is git-ignored on purpose.

```powershell
copy .env.example .env
notepad .env
```

Fill in the real values. The ones that matter:

| Setting | Used for |
|---------|----------|
| `SEEK_EMAIL` / `SEEK_PASSWORD` | SEEK employer-portal login |
| `ADVERTISER_ID` | Which SEEK employer account to scrape (optional) |
| `DB_INSTANCE=SQLEXPRESS`, `DB_TRUSTED=yes` | Local SQL Server with Windows Auth |
| `DB_DRIVER=ODBC Driver 18 for SQL Server` | Must match the installed ODBC driver name |
| `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` | Email / Outlook draft / calendar features (Microsoft Graph) |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | ChatGPT résumé summaries |

> **Easiest path:** copy your existing `.env` from the old computer (USB/secure
> transfer) into this folder instead of refilling it by hand.

---

## 5. Test the database connection

```powershell
.\.venv\Scripts\python.exe -c "import db; db.ping()"
```

This connects and creates the `jobdb_scraping` database + tables if they don't exist.
You want to see **"Connected OK"**.

---

## 6. Run

**The HR web board** — install it once as a background task that starts at every logon:
```powershell
.\service.ps1 install
```
Then open **http://localhost:2757**. No window stays open; the board is simply always up.

Day-to-day commands (PowerShell, in the project folder):

| Command | What it does |
|---------|--------------|
| `.\service.ps1 status` | Task state, HTTP health, server PID, current commit |
| `.\service.ps1 restart` | Stop + start — run after editing any `.py` file or `.env` |
| `.\service.ps1 deploy` | `pip install` + restart + health check (**or just double-click `deploy.cmd`**) |
| `.\service.ps1 deploy -Pull` | Same, but `git pull --ff-only` first (use on a teammate's machine) |
| `.\service.ps1 logs` | Last 60 lines of `logs\webapp.log` (`-Follow` to tail live) |
| `.\service.ps1 stop` / `start` / `uninstall` | Manual control / remove the task |

How it works: a Scheduled Task named **"JobDB Recruitment Board"** runs `run_webapp.cmd`
hidden **under your own account** at logon (so SQL Server Windows Auth, the Graph token
cache and OneDrive keep working). The wrapper relaunches `webapp.py` within 5 s if it ever
crashes, and writes `logs\webapp.log` + `logs\service.log`.

To run it in the foreground instead (debugging), stop the task first:
```powershell
.\service.ps1 stop
.\.venv\Scripts\python.exe webapp.py
```

**The SEEK scraper** (always keep `--headed` so you can solve the CAPTCHA):
```powershell
.\.venv\Scripts\python.exe main.py --headed --job-title "Software Implementer"
```

See `QUICKSTART.md` and `README.md` for full scraper usage and flags.

---

## What to bring over separately (not in Git)

These are excluded from the repo; copy them from the old computer if you want them:

| Item | Why it's not in Git | What to do |
|------|---------------------|------------|
| `.env` | Holds all secrets | Copy it over, or refill from `.env.example` |
| `.graph_token_cache.json` | Outlook sign-in token | Copy it, **or** just click "Sign in to email" once in the UI |
| `email_kit/email_template.json` | Your customised email templates | Copy it (otherwise sensible defaults regenerate automatically) |
| `resume/`, `Email_Reply_Exam/`, `Shortlists/` | Already-scraped candidate files | Copy if you want history; otherwise re-scrape |
| SQL Server data | The candidate database itself | Re-scrape on the new machine, **or** back up / restore the DB in SSMS |

The Excel evaluation templates (`Evaluate_Original.xlsx`, `Evaluation_Template.xlsx`)
**are** in the repo, so they arrive with the clone automatically.

---

## Troubleshooting

- **`db.ping()` fails** → check SQL Server Express is running (Services → "SQL Server
  (SQLEXPRESS)"), that the instance is named `SQLEXPRESS`, and that the ODBC driver
  name in `.env` exactly matches what's installed.
- **Scraper fails at login** → make sure you used `--headed` and solved the CAPTCHA in
  the browser window.
- **"Missing Graph config" when sending email** → the `GRAPH_*` values in `.env` are
  blank; fill them in and run `.\service.ps1 restart`.
- **Always restart the board** after editing any backend `.py` file or `.env`:
  `.\service.ps1 restart` (or double-click `deploy.cmd`).
- **Board not up after logon?** → `.\service.ps1 status`, then `.\service.ps1 logs`. If the
  task is missing, run `.\service.ps1 install` again.
