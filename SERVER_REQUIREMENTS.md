# Server Requirements: Windows or Linux

Written 2026-10-15. This lists what to install on the server for the Recruitment Board.
IT has not yet said whether the server will run Windows or Linux, so both are covered.
For the move itself (steps, questions for IT, rollback), see `SERVER_DEPLOYMENT.md`.

## In short

- **Windows is easier.** The board was built on Windows. On a Windows server it needs
  almost no code changes, about 1 day of work.
- **Linux works too, but takes more work.** Plan on 2–3 days. The database sign-in,
  file paths, OneDrive and the startup script all have to change (see *Linux: changes needed*).
- **No separate web server is required on either OS.** The board brings its own (**waitress**,
  a Python package). A front web server (IIS on Windows, Nginx on Linux) is only needed
  if IT wants **HTTPS** or a clean address without `:2777`.

---

## 1. Server size (same for both)

| Item | Minimum | Recommended | Why |
|---|---|---|---|
| CPU | 2 cores | 4 cores | The scraper runs a Chromium browser alongside the web app and database |
| RAM | 4 GB | **8 GB** | SQL Server alone wants 2 GB+ (and needs 2 GB minimum on Linux) |
| Disk | 40 GB | 80 GB | OS + software ~20 GB; data today ~1.2 GB (résumés 441 MB, SEEK browser profile 738 MB); room for growth and local backups |
| 64-bit | Required | | Python, SQL Server and Chromium are all 64-bit |

If the database lives on an **existing company SQL Server** (not on this machine),
2 cores / 4 GB is enough.

## 2. Network (same for both)

**Inbound** (from the office LAN / VPN only; not open to the internet):

| Port | For |
|---|---|
| 2777/TCP | The board itself (today's port) |
| 443/TCP (and 80) | Only if a front web server provides HTTPS |
| 3389 (Windows) / 22 (Linux) | Remote admin: Remote Desktop / SSH, for whoever maintains the board |

**Outbound** (the server must be allowed to reach these over HTTPS 443):

| Address | Used for |
|---|---|
| `th.employer.seek.com` (+ SEEK login pages) | Scraping candidates and downloading résumés |
| `api.openai.com` | AI résumé summaries |
| `login.microsoftonline.com`, `graph.microsoft.com` | Sending exam emails, reading replies, OneDrive |
| `github.com` | Downloading / updating the code |
| `pypi.org`, `files.pythonhosted.org` | Installing Python packages |
| `playwright.azureedge.net` / `cdn.playwright.dev` | Downloading the scraper's Chromium browser (one time) |

Also ask IT for a **fixed IP or DNS name** (e.g. `recruitment.<company domain>`).

---

## 3. Windows server: what to install

**OS:** Windows Server 2022 or 2025 (Standard), or Windows 10/11 Pro for an always-on PC.

| # | Software | Version | Notes |
|---|---|---|---|
| 1 | **Python** | 3.12 (64-bit) | From python.org. Tick *Add Python to PATH* and install *for all users*. |
| 2 | **Git** | latest | To download and update the code from GitHub. |
| 3 | **SQL Server** | 2022 Express (free) | Instance name `SQLEXPRESS`. Express limit is 10 GB per database: far more than needed. *Skip if IT gives a database on an existing SQL Server.* |
| 4 | **ODBC Driver 18 for SQL Server** | 18.x | Lets Python talk to SQL Server. Needed even if the DB is elsewhere. |
| 5 | **Microsoft Visual C++ Redistributable** | 2015–2022 x64 | Usually already present; needed by pyodbc and SQL Server. |
| 6 | **Python packages** | from `requirements.txt` | Flask, waitress, pyodbc, Playwright, msal, openpyxl, pypdf, requests, python-dotenv. Installed by `setup_windows.ps1`. |
| 7 | **Chromium for Playwright** | auto | Installed by `setup_windows.ps1` (`playwright install chromium`). |
| 8 | **OneDrive client** | latest | Only if the board keeps saving shortlists / exam replies into the synced "Recruit's files - Recruitment" library. Must be signed in as the board's account. |
| 9 | SQL Server Management Studio (SSMS) | optional | For backups and looking at data by hand. |

**Web server:** none required: waitress serves the board on port 2777.
If HTTPS is required, add **IIS** with the **URL Rewrite** and **Application Request Routing (ARR)**
modules, set up as a reverse proxy from `https://…` to `http://localhost:2777`,
with a certificate from IT.

**Startup:** the board must start with the server, without anyone logged in:
either a Task Scheduler task set to *Run whether user is logged on or not*
(a small change to `service.ps1`), or a Windows service via **NSSM** (free tool).

**Accounts and permissions:**
- A **service account** (domain account) the board runs as.
- That account needs: a login on SQL Server (Windows Authentication, `db_owner` on `jobdb_multiuser`),
  write access to the board folder, and access to the Recruit OneDrive / SharePoint library.
- A **Windows Firewall** rule for port 2777 (`.\service.ps1 firewall`).

**Code changes needed on Windows:** only the startup change above. Everything else works as today.

---

## 4. Linux server: what to install

**OS:** Ubuntu Server 24.04 LTS (recommended) or 22.04 LTS. RHEL / Rocky 9 also work
(use `dnf` instead of `apt`).

| # | Software | How to get it | Notes |
|---|---|---|---|
| 1 | **Python** | 3.12 + `python3.12-venv`, `python3-pip` | Built into Ubuntu 24.04. |
| 2 | **Git** | `apt install git` | |
| 3 | **SQL Server for Linux** | 2022, Express or Developer edition, from Microsoft's apt repo | Needs at least 2 GB RAM. *Skip if IT gives a database on an existing SQL Server (Windows or Linux).* |
| 4 | **unixODBC** | `apt install unixodbc unixodbc-dev` | Needed by pyodbc. |
| 5 | **ODBC Driver 18 for SQL Server** | `msodbcsql18` from Microsoft's apt repo | Same driver name as Windows, so `DB_DRIVER` in `.env` stays the same. |
| 6 | **Python packages** | `pip install -r requirements.txt` inside a `.venv` | Same list as Windows. |
| 7 | **Chromium + its system libraries** | `python -m playwright install --with-deps chromium` | `--with-deps` installs the Linux libraries Chromium needs (fonts, libnss3, etc.). |
| 8 | **Thai fonts** | `apt install fonts-thai-tlwg` | So the scraper's browser renders Thai text correctly. |
| 9 | **Nginx** | `apt install nginx` | Optional: only for HTTPS / port 443 in front of waitress. |
| 10 | **Firewall** | `ufw allow 2777/tcp` (or 443) | |
| 11 | sqlcmd / `mssql-tools18` | Microsoft's apt repo | Optional, for backups from the command line. |

**Web server:** same as Windows: waitress serves the board directly on port 2777.
For HTTPS, put **Nginx** in front as a reverse proxy (`proxy_pass http://127.0.0.1:2777;`)
with IT's certificate.

**Startup:** a **systemd** service (e.g. `/etc/systemd/system/recruitment.service`) running
`.venv/bin/python webapp.py` as a dedicated user, set to restart on failure.
This replaces `service.ps1`, which is Windows-only.

### Linux: changes needed

These are the reasons Linux takes longer. None is large, but each must be done and tested.

| Change | Why | Size |
|---|---|---|
| **Database sign-in** | Linux has no Windows Authentication by default. Use a SQL login instead: in `.env` set `DB_TRUSTED=no`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`. | Small (settings only) |
| **Startup script** | `service.ps1` / `.cmd` / `.vbs` are Windows-only. Write a systemd unit file. | Small |
| **Résumé file paths** | The database stores each résumé as a Windows path (`E:\jobdb_multiuser\resume\…`). Run a one-time SQL update to change them to the Linux folder (e.g. `/opt/recruitment/resume/…`), the same way it was done on 2026-10-09. | Small |
| **OneDrive folders** | There is **no official OneDrive client for Linux**. Shortlists and exam replies are saved into the synced OneDrive folder today. On Linux, the board should upload them through Microsoft Graph instead (it already signs in to Graph for email). | **Medium**: the largest change |
| **OneDrive default path** | `config.py` builds the folder path with Windows backslashes; set `ONEDRIVE_BASE` / `REPLY_EXAM_DIR` explicitly or fix the default. | Small |
| **Excel forms** | Evaluation and Report files are made with openpyxl (no Excel needed), so they work as-is. | None |

---

## 5. Side-by-side summary

| | Windows | Linux |
|---|---|---|
| OS | Windows Server 2022/2025 | Ubuntu 24.04 LTS |
| Language | Python 3.12 | Python 3.12 |
| Web server | waitress (built in); IIS only for HTTPS | waitress (built in); Nginx only for HTTPS |
| Database | SQL Server 2022 Express | SQL Server 2022 for Linux, or a remote SQL Server |
| DB driver | ODBC Driver 18 | unixODBC + ODBC Driver 18 |
| DB sign-in | Windows Authentication (service account) | SQL login (user + password) |
| Scraper browser | Playwright Chromium | Playwright Chromium + system libraries + Thai fonts |
| Runs at boot via | Task Scheduler or NSSM | systemd |
| OneDrive | OneDrive client sync (as today) | Upload via Microsoft Graph (code change) |
| Licence cost | Windows Server licence (IT usually has it); SQL Express free | All free |
| Effort to move | ~1 day | ~2–3 days |

## 6. What to send IT

Copy this to IT when asking for the server:

> We need a server (a VM is fine) for an internal HR web app (Python + SQL Server).
> - **OS:** Windows Server 2022/2025 preferred; Ubuntu 24.04 LTS also possible.
> - **Size:** 4 CPU cores, 8 GB RAM, 80 GB disk.
> - **Database:** SQL Server 2022 Express installed on the server, *or* one database on an existing company SQL Server.
> - **Network:** reachable from the office LAN/VPN on port 2777 (or 443 with HTTPS); fixed IP or DNS name.
>   Outbound HTTPS to SEEK (`th.employer.seek.com`), OpenAI (`api.openai.com`), Microsoft 365
>   (`login.microsoftonline.com`, `graph.microsoft.com`), GitHub and PyPI.
> - **Access:** a service account for the app; admin rights (or IT's help) to install software;
>   Remote Desktop / SSH for maintenance.
> - **Backup:** daily backup of the server and the database.
