# Multi-user board — work in progress

This folder (`E:\jobdb_multiuser`) is the **development copy** of the recruitment board.
It is where the "one board, many recruiters" upgrade is being built. The **live board**
your team uses every day is the separate folder `E:\jobdb_scraping`; it must stay
untouched by this work until you decide to roll the upgrade out.

Read this file first when you come back to the project after a break.

---

## 1. The goal, in one paragraph

Today every recruiter runs their own copy of the board with their own database, and the
SEEK scraper runs on each PC. The goal is **one board and one database shared by the whole
HR team**: scrape once, everyone sees the same candidates, every action is recorded with
who did it, and the app's own logic says who is responsible for each candidate.

### Decisions already made (do not re-debate; change only on purpose)

| Topic | Decision |
|---|---|
| Who sees what | **Everyone sees and can act on every candidate.** The "owner" is a label, not a lock. |
| Owner | The **first move out of Pending** (to Wait Pre-screen *or* Not Interest) makes the mover the owner. **Anyone can reassign** by clicking the badge. |
| Sign-in | **Local accounts, not Microsoft 365.** Two roles: **Admin** (system) and **HR**. **Only an Admin creates HR accounts** (➕ Create HR in the top bar, `/users/new`); they can sign in at once. Self-registration was removed on 2026-10-07 — `/register` now just sends people to `/login` with a note. |
| Profile | Your name in the top bar opens **/profile**. **Everyone can view their own account; HR is view-only** (an Admin changes HR details in Manage users) — **except HR can change their own password** after typing the current one (🔑 Change password box under the profile). An **Admin can edit their own** details and password there, and **Manage users lives on the Admin's /profile** (moved from Email Templates on 2026-10-07). The red logo + "Recruitment" links to the Job Postings page. |
| Usernames | **1-50 letters, dots, dashes or underscores** — no digits, no spaces (`db._USERNAME_RE`, and `pattern="[A-Za-z._\-]{1,50}"` on the forms; the `-` must stay escaped or Chromium ignores the pattern). |
| Passwords | **SHA-256 only** (salted per user, stored as `sha256$<salt>$<digest>`). A bare SHA-256 hex digest also verifies. |
| Templates | Email templates live in the **database**, shared by all; the JSON file keeps only per-PC settings. **Admin and HR both create, edit and delete any template** (changed 2026-10-07; HR was view-only before). Mailbox sign-in stays Admin-only. |
| Hiring requests | **Admin and HR both edit.** Each request records who created it and who last edited it (and when); everyone sees that in the list and on the form. Requests sit in a **Doing** tab until someone clicks **✔ Complete** (recorded who/when); **Completed** tab can **↩ Reopen**. |
| Manual candidates | **Admin and HR can type a candidate in by hand** (➕ Add Candidate on a job's Pending stage, optional PDF résumé) for people who did not come through JobDB. Tagged `source='manual'`; the scraper can never match or overwrite them. **No owner on add** — the first move out of Pending still claims it, like every other card. |
| Hosting (Phase 4) | **Parked.** The user does not want to discuss servers yet. Nothing in Phase 4 has started. |

---

## 2. Where everything is

| Item | Value |
|---|---|
| Dev folder | `E:\jobdb_multiuser` (WSL: `/mnt/e/jobdb_multiuser`) — a git clone of the live repo, origin = `github.com/nattapongfwg/jobdb_scraping` |
| Live folder | `E:\jobdb_scraping` — runs as the scheduled task "JobDB Recruitment Board" on port **2757**. Manage with `.\service.ps1 status|restart|deploy`; never start its `webapp.py` by hand. |
| Dev database | `jobdb_multiuser` on `localhost\SQLEXPRESS` — a copy-only restore of the live database taken 2026-09-30. Safe to break; re-create with `debug\make_dev_db.py` (drop the DB first). |
| Dev port | **2777** (`PORT=2777` in the dev `.env`; ports 2756–2760 are taken by other apps on this PC). |
| Dev `.env` | Non-secret only: DB settings, `PORT`, `ONEDRIVE_BASE=E:\jobdb_multiuser\dev_onedrive`, a dev OneDrive path for shortlists. **No SEEK / Graph / OpenAI secrets** — copy those lines from the live `.env` when you need to test emailing, scraping or AI summaries. |
| Branches (dev repo) | `master` = same as live. Work is stacked: `feat/multiuser-phase1-safety` → `feat/multiuser-phase2-users` → **`feat/multiuser-phase3-login`** (the tip, check this one out). **Nothing is merged into master.** The tip branch is pushed to GitHub as a backup (2026-10-07); `master` and the live board are unchanged. |
| Session secret | `.secret_key` (auto-created, gitignored). Deleting it signs everyone out. |

### Commits on the tip branch (oldest first)

| Commit | What |
|---|---|
| `c35afb5` | Phase 1 — claim the exam send before emailing; atomic stage moves; `exam_sent_by` |
| `8fdba33` | Phase 2a — `users` table, per-user email signature |
| `6844931` | Phase 2b — candidate owner, "My candidates" filter, `stage_history` trail |
| `c2398bc` | Phase 2c — email templates stored in the database |
| `a2bf0e3` | Phase 3 — local sign-in, Admin/HR roles, register → approve |
| `a2b64e2` | Perf — removed the decorative animations (**also shipped to live as `c31963d`**) |
| `fc319d8` | Email templates view-only for HR; requests record creator / last editor, catch stale saves |
| `f20cdd2` | Status Tracking — 20/40/60 rows per page; Owner (name + folder prefix) as the first column |
| `59e73df` | Tracking — Owner column shows the first name (full name on hover) |
| `d302363` | Requests — Doing / Completed tabs, ✔ Complete beside Edit, ↩ Reopen |
| `3944e5f` | Evaluation popup — pick a per-team Excel form |
| `bb3f9aa` | **Add Candidate** by hand on the Pending stage, with a PDF résumé (`source='manual'`) |
| `e3aba52` | Email templates — HR can create / edit / delete too |
| `25e7961` | Add Candidate form — First / Last name, inline e-mail check, phone typed as `xxx-xxx-xxxx` |

---

## 3. How to run the dev copy

```
cd /d E:\jobdb_multiuser
.venv\Scripts\python.exe webapp.py
```
Open <http://localhost:2777>. Startup applies `schema.sql` (idempotent, so new columns and
tables appear automatically) and seeds one Admin row if the `users` table is empty.

- **First visit** goes to `/setup` to create the Admin account, pre-filled from the seeded
  row. After that `/setup` is closed and `/login` is used.
- **HR colleagues:** the Admin creates them with **➕ Create HR** (top bar → `/users/new`). Older self-registrations still appear as *Pending approval* in
  My profile → **Manage users** (`/profile#users`; also reachable via the "N pending" link in the top
  bar). Approve them there.
- **Reset to first-run** (to see `/setup` again):
  `UPDATE dbo.users SET password_hash = NULL WHERE role = 'admin';`
- **See who exists:** `.venv\Scripts\python.exe debug\list_users.py`
- **State on 2026-10-07:** the dev DB has **only the Admin account** (username `admin`, prefix `N`,
  you set the password in the browser). All other users were removed so you can create the
  HR accounts yourself with ➕ Create HR.

Restart `webapp.py` after editing any `.py` file (templates are cached too — Flask runs
with `debug=False`). Hard-refresh the browser (Ctrl+F5) after CSS changes.

### Tests and checks (`debug\` is gitignored)

Run each with `set PYTHONUTF8=1&& .venv\Scripts\python.exe debug\<file>` from the dev folder.
They hit the **dev** database only and clean up after themselves.

| Script | Covers |
|---|---|
| `phase1_test.py` | exam-send claim / undo / already-sent guard / race on stage moves (9 checks) |
| `phase2_users_test.py` | users API, duplicate checks, signature follows the current user |
| `phase2_owner_test.py` | owner claim on first move, reassign, history order and actors |
| `phase2_templates_test.py` | templates in DB, JSON migration, no rewrite when unchanged |
| `phase3_auth_test.py` | setup, `/register` closed, Admin creates HR at `/users/new` (mismatch / duplicate / 403 for HR), 401/403 rules, reset, deactivate (39 checks) |
| `profile_test.py` | /profile: HR view-only (disabled fields, POST 403) but can change own password (wrong current / mismatch / short / same → 400; success → signs in with the new one), Admin edits own details / password (mismatch, duplicate prefix, blank keeps), logo → / and name → /profile on every page, Manage users only for Admins |
| `ui_profile.py` | **browser check**: /profile as HR and as Admin (screenshots), logo click lands on / |
| `ui_create_hr.py` | **browser check**: login page (no Register link), ➕ Create HR from the top bar, error + success, the new HR signs in; screenshots, cleans up |
| `request_audit_test.py` | request created/edited by, stale-save 409 + overwrite, complete/reopen, HR creates/edits/deletes templates, sign-in + user management still Admin-only (30 checks) |
| `manual_add_test.py` | Add Candidate: fields land in Pending, résumé stored + served, 'added' history, 409 duplicate / force, 400/404/401/413, phone stored as `xxx-xxx-xxxx`, bad phone 400 (28 checks) |
| `ui_add_candidate.py` | **browser check** (Playwright, headless): starts `webapp.py`, signs in via a signed cookie, tries bad first name / e-mail / phone (inline errors), types `+66 …` into Phone, adds a candidate with a PDF, screenshots to `debug\shots\`, cleans up |
| `ui_tracking_stages.py` | **browser check**: Status Tracking Stage multi-select — counts match the DB, button text, remembered after reload, Escape / outside click, Clear filters, Select all (13 checks) |
| `render_pages.py` | renders every page as Admin into `debug\rendered\` → then `node` syntax-checks the inline JS (see below) |
| `boot_check.py` | starts `webapp.py` on a free port and confirms it answers |
| `make_dev_db.py` | copies the live DB into the dev DB (only when the dev DB does not exist) |

**Known stale tests (2026-09-30):** `phase2_users_test.py`, `phase2_owner_test.py` and
`phase2_templates_test.py` fail since Phase 3 — they call the API without signing in
(401 / no app context). The code they cover is fine; the scripts need a signed-in
test client (see `request_audit_test.py`, which sets `session["uid"]`).

**From WSL** the same Windows interpreter works — `.venv/Scripts/python.exe -X utf8 debug/<file>` —
but pass `-X utf8`: WSL's `PYTHONUTF8` is not forwarded to Windows processes, and without it the
PASS/FAIL lines with → or é crash on cp1252.

JS syntax check after `render_pages.py` (from WSL, needs node):
```
node -e 'const fs=require("fs"),vm=require("vm");for(const f of fs.readdirSync("debug/rendered")){const h=fs.readFileSync("debug/rendered/"+f,"utf8");let i=0;for(const m of h.matchAll(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g)){i++;try{new vm.Script(m[1],{filename:f});console.log("OK",f,i)}catch(e){console.log("FAIL",f,i,e.message)}}}'
```

---

## 4. What was built, file by file

### Database (`schema.sql`, all idempotent `IF … ADD/CREATE`)
- `applicants.exam_sent_by` — prefix of whoever sent the exam (reply checks look in *their* mail folder).
- `applicants.owner_email` — responsible recruiter (`users.email`).
- `dbo.users` — `email, name, firstname, mobile, tel, prefix, role('admin'|'hr'), is_active, username, password_hash, is_approved, approved_by, approved_at, last_login_at`.
- `applicants.source` — `NULL` = scraped from JobDB, `'manual'` = typed in with Add Candidate (its `application_id` is `manual-<uuid>`, its `candidate_key` `manual|<uuid>`, `status` `'Manual'`, `applied_at` the Thai time it was added).
- `dbo.stage_history` — `application_id, action, from_value, to_value, actor_email, at`. Actions: `move`, `exam_sent`, `exam_resent`, `exam_failed`, `owner`, `added` (to_value = the starting stage).
- `dbo.email_templates` — one row per template (`template_id, doc JSON, sort_order, updated_by, updated_at`).
- `dbo.requests.created_by / updated_by` (`users.email`), `updated_at`, `revision` (INT, +1 per save).
- `dbo.requests.status` (`'doing'` default | `'completed'`), `completed_by`, `completed_at`.

### `db.py`
- `set_stage(aid, stage, date, exam_sent_by=None, actor=None)` — `UPDATE … WHERE stage = <what we read>`; returns `conflict=True` if someone moved the card first; claims the owner on the first move out of Pending; writes `stage_history`.
- `claim_exam_send / undo_exam_claim` — reserve the card **before** Graph sends; `already_sent=True` blocks a second email unless `resend=True`.
- `set_owner`, `list_history`.
- Manual candidates: `add_manual_candidate(job_id, full_name=…, …, actor)` inserts the Pending row
  (name copied into both `full_name_jobdb` and `full_name_edit`; the typed Expect salary also fills
  `expect_salary` so the 💰 pill and salary sort work) and logs `added`; `find_duplicate_candidate`
  (same e-mail or same normalised name within the job); `set_resume`; `delete_candidate` (rollback only).
- Users: `list_users, get_user_by_id/username/prefix/email, save_user (validates, hashes password), approve_user, touch_login, count_pending_users, admin_has_password, first_admin_without_password, seed_default_user`.
- `hash_password / verify_password` — salted SHA-256.
- `load_email_templates / save_email_templates` — the store behind `email_kit.templates`.
- `insert_request(..., actor)` records the creator. `update_request(..., actor, revision)` records the
  editor and only writes when `revision` still matches; otherwise returns `conflict=True` + the
  current row. `list_requests / get_request` share `_REQUEST_SELECT`, which joins `users` for
  `created_by_name / updated_by_name / completed_by_name`.
- `set_request_status(rid, status, actor)` — `'completed'` records who/when (a second complete keeps
  the first completer); `'doing'` clears both. Does **not** bump `revision`, so an open edit form still saves.

### `webapp.py`
- `current_user()` — from `session["uid"]`, cached in `flask.g`; must be active **and** approved.
- `@before_request _require_login` — pages → `/login?next=…`, APIs → `401 {"login": true}`; binds the user as the active email-signature recruiter.
- `admin_required` on: `/api/scrape/*`, `/api/email/login-start`, `POST /api/users`, `POST /api/users/<id>/approve`.
- Auth pages: `/setup`, `/login`, `/logout`; `/register` only redirects to `/login` (self-registration closed).
- `/profile` (`profile_page`) — GET for everyone (own account); POST Admin only (403 for HR), keeps role/active/approved, blank password keeps the old one; `?saved=1` on success.
  `POST /profile/password` (`profile_password`, any signed-in user; the box is shown to HR) — `old_password`, `new_password`,
  `confirm_password`; `db.change_own_password` checks the current one, 8+ chars, different from the current; success →
  `/profile?pw=1#password`. Admins change their own password with the New password field instead.
  For Admins the page also holds **👥 Manage users** (`#users`): the users table (edit, reset password, role, Active, ✔ Approve),
  saving through `/api/users` — the code moved here from `email_templates.html`. Saving your own row reloads the page.
  Browser check: `debug\ui_manage_users.py`.
- `/users/new` (Admin, `create_hr_page`) — Create HR form (`create_hr.html`, reuses `_account_fields.html`); saves an active, approved HR user and redirects back with `?created=<username>`.
- New APIs: `POST /api/candidates/owner`, `GET /api/candidates/history?application_id=`, `GET/POST /api/users`, `POST /api/users/<id>/approve`.
- `POST /api/requests/<id>` takes `revision` (the one the form loaded) and answers **409**
  `{"conflict": true, "error": "<name> saved this request at <time> …"}` when a teammate saved
  first; `force: true` overwrites. `_request_json()` formats request rows for JSON/templates.
- `POST /api/requests/<id>/status` `{status: "completed"|"doing"}` — Admin and HR.
- `POST /api/candidates/add` — **multipart** form (`job_id, name_title, nickname, full_name*, email,
  phone, remark, current_salary, minimum_expect_salary, expect_salary, resume (PDF), force`). Admin and
  HR. **409** `{"duplicate": {...}}` when the same e-mail or name is already in the job (the UI asks
  "add anyway?" and resends with `force=1`); 400 for a missing name / bad e-mail / bad phone / non-PDF; 404 unknown
  job; **413** JSON above `MAX_RESUME_MB` (20). The résumé is written where the scraper would put it —
  `resume/<job title>/<name>_<application_id>.pdf` (`_safe_filename` mirrors `scraper._safe_filename`
  so the web app never imports Playwright) — and the row is deleted again if the file cannot be saved.
  The form's First + Last name are joined into `full_name` by the page. `phone` must be 10 digits
  (a `+66` prefix becomes `0`) and is stored as `xxx-xxx-xxxx` (`_format_phone`); blank is fine.
- `_user_prefix()` = signed-in user's prefix (the old per-PC "I am"/`user_prefix` setting is no longer used).
- Context processor injects `me` and `pending_users` into every template; `templates/_who.html` renders the top-bar identity.
- `PORT` env var chooses the listening port.

### `email_kit/`
- `signature.py` — `set_active_recruiter(user)` (ContextVar); `recruiter_*()` read the active user → `.env` `RECRUITER_*` → defaults.
- `templates.py` — `configure_store(load, save)`; the web app plugs the DB in; the JSON file is migrated on first load and then only holds settings.

### Evaluation forms per team (`evaluation.py`, added 2026-09-30)
- The Evaluation popup has an **Evaluation Template** dropdown listing every `.xlsx` under
  `files_evaluation/` (grouped by team folder; gitignored, copy the folder by hand).
  Blank = the default `Evaluation_Template.xlsx`.
- `find_form_sheet()` locates the form sheet by its labels (A2 ชื่อ-นามสกุล, M2 บทบาท) because
  some team files keep the form on sheet3 ("SA", "Dev"); the nine header cells are the same.
- Blank popup fields now **clear** the cell, so a team file's example candidate never leaks.
- `GET /api/evaluation/templates`; `POST /api/candidates/evaluation` takes `template`.
- Test: `debug\eval_templates_test.py` (fills every form + the default).

### Templates / static
- `login.html`, `create_hr.html`, `profile.html`, `setup.html`, `_account_fields.html`, `_who.html` (name → /profile, role, 🚪 Logout).
- Every page's top bar: the brand is `<a class="brand" href="/">` (logo + "Recruitment" → Job Postings).
- **Main menu = `templates/_nav.html`**, included in every signed-in page's top bar: 💼 Job Postings · 📊 Status Tracking ·
  📝 Requests · ✉️ Email Templates · ➕ Create HR (Admin only). The active item comes from `request.path`
  (`/job/…` → Job Postings, `/request` → Requests). The old buttons on the Job Postings page are gone. Below 1240 px
  the menu moves to its own row. Logout shows 🚪. Browser check: `debug\ui_nav.py` (Admin + HR at 1700/1300/1100 px).
- `pipeline.html` — 👤 owner pill (click → reassign), **My candidates** toggle, 🕘 history modal, reload on stage conflict, "already sent → send again?" confirm.
  **➕ Add Candidate** toolbar on the Pending stage (shown even when Pending is empty) → `#addModal`
  (`openAddModal / confirmAdd`, `FormData` upload; after a successful add the list reloads, the new
  card scrolls into view with a gold `just-added` ring). Fields: Title, Nickname, **First name*** /
  Last name, Email (checked on blur), Phone (digits only, formats to `xxx-xxx-xxxx` as you type), salaries,
  résumé, remark. Bad fields get a red border + a one-line hint under the box (fixed height, so the
  form never shifts under the mouse). Manual cards show "✍ Added by hand" instead of
  the JobDB line plus a dashed **✍ Manual** pill; history reads "Added by hand → Pending".
- `tracking.html` — the same "✍ Added by hand" line for manual rows (`list_all_candidates` carries `source`).
- `tracking.html` — **Stage filter is a checkbox dropdown** (tick several stages; none ticked = all; the choice is remembered in
  `localStorage` as `tracking.stages`). `/api/tracking?stage=` takes a comma list (`sent_exam,shortlist`) → `a.stage IN (…)`.
- `tracking.html` — **Owner first column** (first name, full name on hover, over 📁 folder prefix); **client-side paging**
  (20 default, 20/40/60, remembered in `localStorage` as `tracking.pageSize`; filters reset to page 1).
- `email_templates.html` — users manager (Admin). The template editor is the same for Admin and HR
  (create / edit / delete); only the mailbox sign-in button (`CAN_SIGN_IN`) is Admin-only. The page keeps a server-rendered "Signed in as … · folders" line (Admins get a 👥 Manage users link to `/profile#users`).
- `requests.html` — **Doing / Completed tabs** (with counts; `#completed` in the URL opens that tab),
  **✔ Complete** beside Edit on Doing, **↩ Reopen** + a Completed (who/when) column on Completed;
  **Created by** / **Last edited** columns. `request.html` — the same on top of
  the edit form, and an overwrite-or-reload confirm on a 409.
- `index.html` — scrape panel hidden for HR.
- `styles.css` — animations/blur removed; owner, history, auth and top-bar styles added.

---

## 5. Things to know (gotchas)

- **Do not develop in `E:\jobdb_scraping`.** It is live and in daily use. Bring finished
  work over as commits (cherry-pick, like the perf change) or by merging when the rollout
  is decided.
- The live database already has the harmless empty `exam_sent_by` column (from an early
  test on 2026-09-30). Everything else in the live DB is untouched.
- The live repo still has a leftover `git stash` ("phase1 WIP") and an empty branch
  `feat/multiuser-phase1-safety`; both can be dropped.
- An old June clone `E:\jobdb_scraping_upgrade` exists and is unrelated; leave it.
- `email_kit/email_template.json` in the dev copy holds settings only (`user_prefix` is
  ignored now). Templates come from the DB.
- Deleting the last **exam**-type template leaves none (pre-existing behaviour); the
  send-exam button then reports "No email template configured". Add one back in the UI.
- Old rows without `exam_sent_by` fall back to the signed-in user's prefix when checking
  replies — correct for rows sent from this PC.

---

## 6. What is next (when you are ready)

1. **Try it in the browser** in the dev copy: `/setup`, register a test HR account in a
   private window, approve it, move a card as HR, check the badge and history.
2. Decide the rollout. Options that need **no server**: merge the dev branches into
   `master` and deploy to your own PC (`.\service.ps1 deploy` in the live folder), so the
   live board gains sign-in, owners and history while still being single-PC.
3. **Phase 4 (parked):** shared hosting (IT server or a designated always-on PC), merge
   the per-PC databases (dedup on `job_id + candidate_key`, keep the furthest stage), move
   résumés to the shared disk, SEEK/Recruit sign-in done once on the host. Discuss only
   when you want to.

### Git workflow used here
Short-lived branch → fast-forward merge into `master` → push → delete the branch.
Commit messages end with a `Co-Authored-By: Claude …` line (the current model, e.g.
`Claude Opus 5.5 <noreply@anthropic.com>`).
