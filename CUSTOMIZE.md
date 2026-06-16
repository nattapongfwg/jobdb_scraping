# Values to change before someone else uses this project

**Audience: an AI agent (Claude Code) helping a new person/company adopt this app.**
This lists every value that is specific to the original owner (Freewill Solutions /
Nattapong Yuwasirinun) and machine. Walk the user through these. They fall into two groups:

- **Group A — `.env` settings** (the intended config; no code editing).
- **Group B — hardcoded in the Python source** (must edit a `.py` file, or — for emails —
  edit the template in the web UI). These will otherwise send the *original owner's* name,
  phone, address, and links to the new company's candidates.

> Line numbers are accurate as of this writing but may drift — search the quoted anchor
> text if a line doesn't match.

---

## ⚠️ First: `.env.example` ships with REAL secrets

`.env.example` currently contains the original owner's **real SEEK login and password**
(`SEEK_EMAIL` / `SEEK_PASSWORD`) and sender mailbox. Before handing this repo to anyone:
- Replace those with blanks/placeholders in `.env.example`, **and**
- Rotate the SEEK password if it was ever shared.

Treat `.env.example` as documentation, not as working credentials.

---

## Group A — `.env` settings (no code change)

Copy `.env.example` → `.env` and set these for the new user/machine:

| Setting | Change to | Notes |
|---------|-----------|-------|
| `SEEK_EMAIL`, `SEEK_PASSWORD` | The new company's SEEK employer login | Required to scrape |
| `ADVERTISER_ID` | New company's SEEK account id (blank = first) | |
| `DB_INSTANCE`, `DB_TRUSTED`, `DB_DRIVER`, `DB_HOST` | Match the new machine's SQL Server | `SQLEXPRESS` + Windows Auth is the tested setup |
| `ONEDRIVE_BASE` | The new user's OneDrive folder | e.g. `C:\Users\<you>\OneDrive - <org>\Candidate_JobDB_Scraping` (falls back to the original owner's path if blank — see [[INSTALL]]) |
| `RESUME_DIR`, `SHORTLIST_DIR`, `REPLY_EXAM_DIR`, `SHORTLIST_ONEDRIVE_DIR` | Optional overrides | Résumé dir defaults to `<project>/resume` and needs no change |
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID` | New company's Azure AD app registration | Delegated device-code flow; see ONBOARDING §4 |
| `GRAPH_SENDER` | New sender mailbox (informational under delegated auth) | Actual FROM = whoever signs in |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | New OpenAI key | For AI résumé summaries |
| `EXAM_SUBJECT`, `EXAM_BODY` | Optional exam-email defaults | Has a generic default ("Recruitment Team") |

---

## Group B — hardcoded in source (must edit code / templates)

### B1. Email signature block — **now one shared file: `email_kit/signature.py`**
The recruiter's name, mobile, office phone, email, and company address that appear at the
bottom of every outgoing/draft email live in **one place**: the constants at the top of
**`email_kit/signature.py`** (`RECRUITER_NAME`, `RECRUITER_FIRSTNAME`, `RECRUITER_MOBILE`,
`RECRUITER_TEL`, `RECRUITER_EMAIL`, `HR_DEPARTMENT`, `COMPANY_NAME`, `COMPANY_ADDRESS_1/2`).
**Edit those constants once** and all four emails update:

| Email | Renders signature via |
|-------|-----------------------|
| Exam / interview (`email_kit/templates.py` `DEFAULT_BODY`) | `signature_text()` |
| Shortlist group (`email_kit/templates.py` `DEFAULT_SHORTLIST_BODY`) | `signature_html(26)` |
| Evaluation draft (`evaluation.py`) | `signature_html()` |
| Job-offer draft (`offer.py`, incl. the Thai `ความเห็นฝ่าย Recruit` line) | `signature_html()` + `RECRUITER_FIRSTNAME` |

> Caveat: the two `email_kit/templates.py` defaults only *seed* `email_kit/email_template.json` on first
> run. On a machine where that file already exists, editing `email_kit/signature.py` won't rewrite it
> — change those two templates in the web UI ("Config Email Template"). The evaluation and
> offer drafts read `email_kit/signature.py` directly, so they update immediately.

### B2. Company name
`"Freewill Solutions Co., Ltd."` / `"Freewill Solutions Company Limited"` and
`www.freewillsolutions.com` appear in `email_kit/templates.py` defaults (editable in UI) and
hardcoded in `evaluation.py` / `offer.py` signatures (same edits as B1).

### B3. Fixed external links
| Link | Location | What it is |
|------|----------|------------|
| MS Form test link `https://forms.office.com/r/cpgBQL8FH9` | `email_template.py:42` (in `DEFAULT_BODY`) | The "Implementer test" form — editable in the UI template. Replace with the new company's test, or remove. |
| SharePoint user-manual PDF (`MANUAL_URL`) | `evaluation.py:96-106` + `MANUAL_LINK_TEXT` at `:107` | A link to **tanakrit_jai's personal SharePoint** (interview-evaluation-form manual). Hardcoded — replace the URL or remove the link from `build_eval_email`. |

### B4. Company-specific dropdown lists (evaluation form)
`evaluation.py` defines the option lists the HR team supplied — Freewill-specific org data:
| List | Location |
|------|----------|
| `COMPANIES` (Freewill group entities) | `evaluation.py:76-79` |
| `POSITION_LEVELS` / `POSITIONS` | `evaluation.py` (`POSITIONS` at `:74`) |
| `DEPARTMENTS` | `evaluation.py:80` |
| `SECTIONS` | `evaluation.py:84` |
Replace these with the new company's positions/departments/sections/companies.

### B5. Default recruiter comments (offer popup)
`DEFAULT_RECRUITER_COMMENTS` at `offer.py:23` prefills the offer popup textarea. It's
editable per-use in the UI, but the *default* is hardcoded — change it if the new team wants
a different starting text.

### B6. Excel evaluation templates
`Evaluate_Original.xlsx` (the HR master, in the repo) embeds Freewill branding/competencies.
A new company should replace it with their own master, then run
`make_eval_template.py` to regenerate `Evaluation_Template.xlsx`. Keep the 9 header cells
(B2/I2/N2/B3/I3/N3/B4/I4/N4) so `evaluation.py` can fill them. See ONBOARDING §7.

### B7. Cosmetic-only (safe to ignore)
- `templates/index.html:106` — attachment textarea placeholder shows `E:\jobdb_scraping\exam\...`. Just placeholder text, not functional.
- `setup_windows.ps1:3` — a `cd E:\jobdb_scraping` example in a comment.
- `mailer.py` comments mention `nattapong_yuw@` — comments only; sign-in is interactive.

---

## Suggested order to walk a new user through

1. Fix `.env.example` secrets (above), then do the full install (`INSTALL.md` / ONBOARDING §3).
2. Fill `.env` (Group A).
3. Sign in to email in the UI, then **edit the email templates in "Config Email Template"**
   (covers B1/B2/B3 for exam + shortlist emails — no code needed).
4. Edit the hardcoded signatures in `evaluation.py` and `offer.py` (B1/B2), the `MANUAL_URL`
   (B3), and the dropdown lists (B4) in the source.
5. Replace the Excel master (B6) if they use the Evaluation feature.
6. **Restart `webapp.py`** after any `.py` edit.
