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
| `ONEDRIVE_BASE` | The new user's OneDrive folder | **Auto-detected** from Windows' OneDrive location + `Recruit's files - Recruitment\Recruite_Scraping` — leave blank on a normal install. Set only if your synced library lives elsewhere (a startup warning flags a missing folder). |
| `RESUME_DIR`, `SHORTLIST_DIR`, `REPLY_EXAM_DIR`, `SHORTLIST_ONEDRIVE_DIR` | Optional overrides | Résumé dir defaults to `<project>/resume` and needs no change |
| `RECRUITER_NAME`, `RECRUITER_FIRSTNAME`, `RECRUITER_MOBILE`, `RECRUITER_TEL`, `RECRUITER_EMAIL` | **Per-teammate** signature identity | Each teammate sets their own so emails from their machine sign as them (blank → built-in default). See B1. |
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID` | New company's Azure AD app registration | Delegated device-code flow; see ONBOARDING §4 |
| `GRAPH_SENDER` | New sender mailbox (informational under delegated auth) | Actual FROM = whoever signs in |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | New OpenAI key | For AI résumé summaries |
| `EXAM_SUBJECT`, `EXAM_BODY` | Optional exam-email defaults | Has a generic default ("Recruitment Team") |

---

## Group B — hardcoded in source (must edit code / templates)

### B1. Email signature block — **per teammate, via `.env`**
The recruiter's **identity** (name, first name, mobile, office phone, email) is now
**per-machine**: each teammate sets `RECRUITER_NAME` / `RECRUITER_FIRSTNAME` /
`RECRUITER_MOBILE` / `RECRUITER_TEL` / `RECRUITER_EMAIL` in their own `.env` (Group A
above), so emails sent or drafted from their machine sign as **them** — even though the
Recruit mailbox is shared. `email_kit/signature.py` reads those env vars at render time;
its module constants (plus `HR_DEPARTMENT`, `COMPANY_NAME`, `COMPANY_ADDRESS_1/2`, which
stay shared) are only the fallback defaults when a var is unset.

All four emails render the signature live via a **`{signature}`** placeholder in the
default templates, so they reflect this machine's recruiter automatically:

| Email | Renders signature via |
|-------|-----------------------|
| Exam / interview (`email_kit/templates.py` `DEFAULT_BODY`) | `{signature}` → `signature_text()` |
| Shortlist group (`email_kit/templates.py` `DEFAULT_SHORTLIST_BODY`) | `{signature}` → `signature_html()` |
| Evaluation draft (`evaluation.py`) | `signature_html()` |
| Job-offer draft (`offer.py`, incl. the Thai `ความเห็นฝ่าย Recruit` line) | `{signature}` → `signature_html()` + `recruiter_firstname()` |

> Caveat: the `email_kit/templates.py` defaults only *seed* `email_kit/email_template.json` on first
> run. A template that was **already saved with the signature text baked in** (no `{signature}`
> placeholder) keeps that text — edit it in the web UI ("Config Email Template"), or insert a
> literal `{signature}` where you want the live block. Freshly-seeded templates and the
> evaluation/offer drafts pick up the env recruiter immediately.

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
