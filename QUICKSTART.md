# Quick Start — run the SEEK resume scraper

Everything is already installed. Follow these steps each time you want to pull resumes.

## Step 1 — Open PowerShell in the project folder
- Press the **Windows key**, type **PowerShell**, press **Enter**.
- In the window, type this and press **Enter**:
  ```powershell
  cd E:\jobdb_scraping
  ```

## Step 2 — Run the scraper for a job ad (by its title)
Type one of these and press **Enter**.

**A small test first (only 3 applicants):**
```powershell
.\.venv\Scripts\python.exe main.py --headed --job-title "Software Implementer" --limit 3
```

**The full job (all applicants):**
```powershell
.\.venv\Scripts\python.exe main.py --headed --job-title "Software Implementer"
```

> Change `"Software Implementer"` to whatever job ad (ประกาศงาน) title you want.
> If the title doesn't match, the program prints the list of your open job titles.

## Step 3 — Sign in (one time per run)
- A **browser window opens** with your email and password already filled in.
- Tick **"Verify you are human"** (solve the CAPTCHA) and click **Sign in**.
- That's the only manual step — the program then does everything automatically.
  Don't close the browser; it closes itself when finished.

## Step 4 — Wait for it to finish
You'll see lines like:
```
Saved resume -> E:\jobdb_scraping\resume\....pdf
Already have resume for ... — skipping download.
...
Done. jobs=1 applicants=229 resumes=226 skipped(dupes)=3 failures=0
```
- `resumes=` how many new resumes were downloaded
- `skipped(dupes)=` how many you already had (not re-downloaded)

## Step 5 — Find your results
- **Resume files:** `E:\jobdb_scraping\resume\` (one PDF per applicant).
- **Applicant data:** in SQL Server. Open **SQL Server Management Studio**, connect to
  `localhost\SQLEXPRESS` (Windows Authentication), then:
  `jobdb_scraping` → Tables → `dbo.applicants`  (name, email, phone, applied date, resume path).

---

---

# Web UI — HR recruitment board (blue/black theme)

A local website with an HR hiring board: job postings → per-job candidate
pipeline → status tracking.

## Start the website
```powershell
cd E:\jobdb_scraping
.\.venv\Scripts\python.exe webapp.py
```
Then open **http://localhost:2757** in your browser.

## Pages
1. **Job Postings (home)** — your scraped jobs as cards, split into **Online / Offline**
   tabs. Each card shows the applicant count and a **"N New"** badge (candidates not yet
   sent the exam). The **Active** checkbox toggles a job online/offline. Click a card to
   open its pipeline.
2. **Pipeline** (`/job/<id>`) — the candidate board. Four stages in order:
   **Pre-Screen → Shortlist → Interview → Offered**. Pick a stage on the left to see its
   candidates; **drag a card onto the next stage (or click "Move to … →")** to advance —
   you can only move **one step forward**, never skip or go back. Each move asks for a date
   (shortlist / interview / offer). Each card also has a **✉ Send exam** button.
3. **Status Tracking** (`/tracking`) — one table of every candidate with their stage and
   milestones. Filter by job title / stage / name. **Name, email and phone are editable
   inline** — changes save automatically.

The pipeline stage, dates and CV flag live in new `dbo.applicants` columns and are **never
overwritten by the scraper**, so re-running `main.py` keeps each candidate's progress.

## One-time email setup (Microsoft Graph)
Sending uses Microsoft Graph. In **Azure Portal → App registrations**, create an app with the
**Mail.Send** *application* permission (grant admin consent), then fill these in `.env`:
```
GRAPH_TENANT_ID=...
GRAPH_CLIENT_ID=...
GRAPH_CLIENT_SECRET=...
GRAPH_SENDER=HumanResources@freewillsolutions.com
EXAM_SUBJECT=Pre-employment exam invitation
EXAM_BODY=Dear {name},\n\n...your text...   (\n = new line; {name} = candidate name)
EXAM_ATTACHMENT=E:\jobdb_scraping\exam\exam.pdf   (the exam file to attach)
```
Until these are filled, the UI works but clicking *Send* shows a "Missing Graph config" message.

---

## Notes
- **Re-running is safe.** It skips resumes you already downloaded and only fetches new
  applicants. To force re-downloading everything, add `--redownload`.
- All `scraped_at` / `exam_sent_at` timestamps are stored in **Thailand time**.
- **Always keep `--headed`** so you can solve the CAPTCHA.
- **See all open jobs:** if you're unsure of a title, run with a wrong title on purpose and
  it will list them, e.g.:
  ```powershell
  .\.venv\Scripts\python.exe main.py --headed --job-title "zzz"
  ```
- **Useful extras:** `--limit 10` (only first 10 applicants), `--no-resumes` (data only, no
  PDFs), `-v` (detailed logs).
