"""Playwright scraper for the SEEK Thailand employer portal.

IMPORTANT — SELECTORS ARE PLACEHOLDERS.
The portal is an authenticated SPA whose exact DOM/URLs are not known until you
log in and inspect it. Every URL path and CSS/text selector lives in the
SELECTORS block below so you can correct them in one place after the first
headed inspection run (`python main.py --headed --login-only`, then use the
Playwright Inspector / browser devtools to read real selectors).

The extraction methods are written defensively: if a selector misses, they log
a warning and continue rather than crashing the whole run.
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Download,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from config import Config
from db import candidate_key

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SELECTORS / URLS — confirm these against the live portal, then edit here only.
# ---------------------------------------------------------------------------
SELECTORS: dict[str, str] = {
    # Login
    "login_path": "/oauth/login/",            # relative to base_url; adjust if redirected
    "login_email": "input[name='email'], input[type='email']",
    "login_email_next": "button[type='submit']",   # SEEK often splits email then password
    "login_password": "input[name='password'], input[type='password']",
    "login_submit": "button[type='submit']",
    # A selector that ONLY exists once logged in (used to confirm session validity)
    "logged_in_marker": "[data-automation='account-menu'], nav[aria-label='Account']",

    # Job ads list (/jobs): each job's title links to /candidates/?jobid=<ID>
    "jobs_path": "/jobs",
    "job_link": "a[href*='/candidates/?jobid=']",

    # Per-job candidate list (/candidates/?jobid=<ID>): virtualized cards.
    # NOTE: SEEK uses data-testid (not data-automation) for these.
    "applicant_card": "[data-testid^='job-application-card']",
    # Each card's Resumé button id IS the application UUID.
    "card_resume_button": "button[aria-label='Resumé'], button[aria-label='Resume']",
    # Candidate name is most reliably in the per-card "Select candidate <name>" checkbox.
    "card_name_checkbox": "input[aria-label^='Select candidate']",
    "applicant_next_page": "button[aria-label='Next'], [data-testid='pagination-next'], a[rel='next']",

    # Detail panel (rendered inline once a card is selected) — single per page.
    "detail_email": "a[href^='mailto:']",
    "detail_phone": "a[href^='tel:']",
}

# Job id is numeric in /candidates/?jobid=<ID>; application id is the UUID used in
# &selected=<UUID> and as the Resumé button's element id.
JOB_ID_RE = re.compile(r"jobid=(\d+)", re.I)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


@dataclass
class ScrapeStats:
    jobs: int = 0
    applicants: int = 0
    resumes: int = 0
    skipped: int = 0          # resumes already downloaded (deduped)
    failures: list[str] = field(default_factory=list)


# Characters Windows forbids in a file/folder name (plus control chars). We strip
# only these so that all other Unicode — including Thai letters AND their combining
# vowel/tone marks (e.g. ◌ั) — is preserved; \w alone drops the combining marks.
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _safe_filename(value: str, fallback: str) -> str:
    cleaned = _ILLEGAL_FS.sub("_", (value or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    return (cleaned[:120] or fallback)   # cap length to stay well under MAX_PATH


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Thai/intl mobile: +66/0 followed by 8-9 more digits, allowing spaces/dashes.
_PHONE_RE = re.compile(r"(?:\+?66[\s-]?|0)\d(?:[\s-]?\d){7,9}")


def _contact_from_pdf(path: Path) -> tuple[str | None, str | None]:
    """Best-effort extraction of email + phone from a resume PDF's text.

    SEEK's generated CVs often extract with a space between every glyph (e.g.
    'p i c h a m o n . a p k @ g m a i l . c o m'), so we also search a variant
    with intra-line spaces removed.
    """
    try:
        from pypdf import PdfReader
        text = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
    except Exception as exc:  # noqa: BLE001 — extraction is best-effort
        log.debug("PDF parse failed for %s: %s", path.name, exc)
        return None, None

    compact = re.sub(r"[ \t]+", "", text)  # collapse char-spacing; keep newlines

    email = None
    for src in (text, compact):
        m = _EMAIL_RE.search(src)
        if m:
            email = m.group(0).strip(".")
            break
    phone = None
    for src in (text, compact):
        m = _PHONE_RE.search(src)
        if m:
            phone = re.sub(r"[\s-]", "", m.group(0))
            break
    return email, phone


class SeekScraper:
    def __init__(self, cfg: Config, headed: bool = False) -> None:
        self.cfg = cfg
        self.headed = headed
        self._pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._detail_dumped = False  # one-time detail-panel dump for selector QA
        self._resume_net_logged = False  # one-time resume-modal dump for selector QA

    # -- lifecycle ----------------------------------------------------------
    def __enter__(self) -> "SeekScraper":
        self._pw = sync_playwright().start()
        # A PERSISTENT context (on-disk user-data dir) keeps the full browser
        # profile — cookies, localStorage, IndexedDB, and the Auth0 SSO session
        # — so a manual login survives across runs. storage_state alone is not
        # enough for SEEK's Auth0 SPA (it does silent re-auth on load).
        self.cfg.profile_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.debug_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.resume_dir.mkdir(parents=True, exist_ok=True)
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.cfg.profile_dir),
            headless=not self.headed,
            accept_downloads=True,
        )
        self.browser = None  # persistent context owns the browser process
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self.context:
                self.context.close()  # flushes the profile to disk
        finally:
            if self._pw:
                self._pw.stop()

    # -- helpers ------------------------------------------------------------
    def _pause(self) -> None:
        time.sleep(random.uniform(self.cfg.delay_min, self.cfg.delay_max))

    def _url(self, path: str) -> str:
        return f"{self.cfg.seek_base_url}{path}"

    def _text(self, scope, selector: str) -> str | None:
        try:
            el = scope.query_selector(selector)
            if el:
                return (el.inner_text() or "").strip() or None
        except PWTimeout:
            pass
        return None

    def _attr(self, scope, selector: str, attr: str) -> str | None:
        try:
            el = scope.query_selector(selector)
            if el:
                return el.get_attribute(attr)
        except PWTimeout:
            pass
        return None

    def _screenshot(self, name: str) -> None:
        try:
            self.page.screenshot(path=str(self.cfg.debug_dir / f"{name}.png"), full_page=True)
        except Exception:  # noqa: BLE001 — debugging aid only
            pass

    def dump(self, name: str) -> None:
        """Save a screenshot + the current HTML to debug/ (for selector discovery)."""
        self._screenshot(name)
        try:
            (self.cfg.debug_dir / f"{name}.html").write_text(
                self.page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        log.info("Dumped %s (url=%s)", name, self.page.url[:100])

    def ensure_session(self, allow_manual: bool = True) -> bool:
        """Make sure we have a live session AND an employer account selected.

        Reuses the persistent profile if it still authenticates; otherwise, when
        running headed and allow_manual is set, performs an interactive login
        (you solve the CAPTCHA) in this same browser session. Finally selects the
        configured employer account so the dashboard/jobs are in scope.
        """
        ok = self.is_logged_in()
        if not ok and allow_manual and self.headed:
            log.info("No valid session — starting interactive login.")
            ok = self.manual_login()
        if not ok:
            log.error("No valid session. Re-run with --headed to log in interactively.")
            return False
        self._ensure_account()
        return True

    def _ensure_account(self) -> None:
        """Make sure the desired employer account is in scope.

        With ADVERTISER_ID set, force that scope via the loginWithScope flow
        (the profile may otherwise remember a different account). Without it,
        fall back to picking the first account on the /account/select page.
        """
        adv = self.cfg.advertiser_id
        if adv:
            scope_url = (f"/oauth/integration/?#/?fn=loginWithScope"
                         f"&return_uri=%2Fdashboard&scope=advertiser%3A{adv}")
            try:
                self.page.goto(self._url(scope_url), wait_until="domcontentloaded", timeout=45000)
                self.page.wait_for_url(
                    lambda u: "/account/select" not in u and "/oauth/" not in u,
                    timeout=30000,
                )
                self.page.wait_for_timeout(2000)
                log.info("Scoped to advertiser %s; now at %s", adv, self.page.url[:80])
            except PWTimeout:
                log.warning("Could not force scope to advertiser %s.", adv)
                self._select_account_if_needed()
        else:
            self._select_account_if_needed()

    def _goto_scoped(self, path: str, wait_selector: str | None = None,
                     timeout: int = 45000) -> None:
        """Navigate to a path; if SEEK bounces to the account picker, select the
        account and navigate again so the employer scope is applied.
        """
        page = self.page
        page.goto(self._url(path), wait_until="domcontentloaded", timeout=timeout)
        page.wait_for_timeout(1500)
        if "/account/select" in (page.url or ""):
            self._select_account_if_needed()
            page.goto(self._url(path), wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(1500)
        if wait_selector:
            page.wait_for_selector(wait_selector, timeout=timeout)

    def _select_account_if_needed(self) -> None:
        """If on the 'Select an account' page, pick the configured advertiser
        (or the first listed) to set the employer scope and reach the dashboard.
        """
        if "/account/select" not in (self.page.url or ""):
            return
        adv = self.cfg.advertiser_id
        selector = (
            f"a[href*='advertiser%3A{adv}'], a[href*='advertiser:{adv}']"
            if adv else "a[href*='loginWithScope']"
        )
        try:
            self.page.wait_for_selector(selector, timeout=15000)
            self.page.click(selector)
            self.page.wait_for_url(lambda u: "/account/select" not in u, timeout=30000)
            self.page.wait_for_timeout(2500)
            log.info("Selected employer account; now at %s", self.page.url[:90])
        except PWTimeout:
            log.warning("Could not auto-select an account on /account/select "
                        "(check ADVERTISER_ID in .env).")
            self._screenshot("account_select")

    # -- auth ---------------------------------------------------------------
    # SEEK protects login with a "Verify you are human" reCAPTCHA, so fully
    # automated login is not possible. The supported flow is:
    #   1) run `--manual-login` once: a visible browser opens, you solve the
    #      CAPTCHA and sign in, and the authenticated session is saved to
    #      storage_state.json;
    #   2) all later runs reuse that saved session (no CAPTCHA).
    _LOGIN_URL_HINTS = ("login", "signin", "sign-in", "oauth", "auth")

    def _on_login_page(self) -> bool:
        url = (self.page.url or "").lower()
        return any(hint in url for hint in self._LOGIN_URL_HINTS)

    def is_logged_in(self) -> bool:
        """True if the saved session lands on the dashboard (not a login page)."""
        try:
            self.page.goto(self._url("/"), wait_until="domcontentloaded", timeout=30000)
            self.page.wait_for_timeout(1500)  # allow any client-side redirect
        except PWTimeout:
            return False
        return not self._on_login_page()

    def login(self) -> bool:
        """Ensure we have a valid session (from a previous manual login).

        Does NOT attempt to bypass the CAPTCHA. If there is no valid session,
        returns False and tells the user to run `--manual-login`.
        """
        if self.is_logged_in():
            log.info("Existing session is valid; skipping login.")
            return True
        log.error("No valid SEEK session found.")
        log.error("Run a one-time manual login first:  python main.py --manual-login")
        return False

    def manual_login(self) -> bool:
        """Interactive login: open a visible browser, pre-fill credentials, and
        wait for the user to solve the CAPTCHA and sign in. Saves the session.

        Requires headed mode (a visible browser window).
        """
        page = self.page
        try:
            page.goto(self._url(SELECTORS["login_path"]), wait_until="domcontentloaded", timeout=60000)
        except PWTimeout:
            log.error("Could not open the login page.")
            return False

        # Best-effort pre-fill so you only have to solve the CAPTCHA + click.
        try:
            if self.cfg.seek_email:
                page.fill(SELECTORS["login_email"], self.cfg.seek_email)
            if self.cfg.seek_password and page.query_selector(SELECTORS["login_password"]):
                page.fill(SELECTORS["login_password"], self.cfg.seek_password)
        except Exception:  # noqa: BLE001 — prefill is convenience only
            pass

        log.info("=" * 64)
        log.info("MANUAL LOGIN — a browser window is open on your desktop:")
        log.info("  1) Tick 'Verify you are human' / solve the reCAPTCHA.")
        log.info("  2) Click 'Sign in'.")
        log.info("Waiting up to 5 minutes for you to reach the dashboard ...")
        log.info("=" * 64)

        try:
            page.wait_for_url(
                lambda u: not any(h in u.lower() for h in self._LOGIN_URL_HINTS),
                timeout=300000,
            )
            page.wait_for_timeout(2000)
        except PWTimeout:
            log.error("Login not detected within 5 minutes. See debug/manual_login_timeout.png")
            self._screenshot("manual_login_timeout")
            return False

        if self._on_login_page():
            log.error("Still on a login page; login did not complete.")
            return False

        # The persistent profile is flushed to disk when the context closes, so
        # the session is reused automatically on subsequent runs.
        log.info("Login detected. Session stored in the browser profile (%s).",
                 self.cfg.profile_dir.name)
        return True

    # -- jobs ---------------------------------------------------------------
    def list_jobs(self) -> list[dict[str, Any]]:
        """Open jobs from /jobs. Each job title links to /candidates/?jobid=<ID>."""
        page = self.page
        jobs: dict[str, dict[str, Any]] = {}
        try:
            self._goto_scoped(SELECTORS["jobs_path"], wait_selector=SELECTORS["job_link"], timeout=30000)
            page.wait_for_timeout(2000)
        except PWTimeout:
            log.warning("No job links at /jobs (landed on %s). Dumping page.", page.url[:90])
            self.dump("jobs_list_miss")
            return []

        for link in page.query_selector_all(SELECTORS["job_link"]):
            href = link.get_attribute("href") or ""
            m = JOB_ID_RE.search(href)
            if not m:
                continue
            job_id = m.group(1)
            if job_id in jobs:
                continue
            jobs[job_id] = {
                "job_id": job_id,
                "title": (link.inner_text() or "").strip() or None,
                "location": None,
                "url": self._url(f"/candidates/?jobid={job_id}"),
            }
        log.info("Found %d job ad(s).", len(jobs))
        return list(jobs.values())

    def find_jobs_by_title(self, query: str) -> list[dict[str, Any]]:
        """Return open jobs whose title contains `query` (case-insensitive)."""
        q = (query or "").strip().lower()
        matches = [j for j in self.list_jobs() if q in (j.get("title") or "").lower()]
        log.info("Title %r matched %d job(s).", query, len(matches))
        return matches

    # -- applicants ---------------------------------------------------------
    def _resume_dir_for(self, applicant: dict[str, Any]) -> Path:
        """Resume folder for an applicant: a per-job-title subfolder of resume_dir
        (e.g. resume/Software Implementer/). Falls back to resume_dir when the job
        title is unknown. The folder name keeps Thai/Unicode letters (\\w matches
        them) and replaces only filesystem-unfriendly characters."""
        title = (applicant.get("job_title") or "").strip()
        sub = _safe_filename(title, "") if title else ""
        target = self.cfg.resume_dir / sub if sub else self.cfg.resume_dir
        target.mkdir(parents=True, exist_ok=True)
        return target

    def reuse_existing_resume(self, applicant: dict[str, Any]) -> bool:
        """If a resume for this application_id was already downloaded, reuse it
        (populate path + email/phone from the existing file) instead of fetching
        again. Returns True when an existing file was found.
        """
        # Match the id anywhere in the name, RECURSIVELY, so a file already saved
        # either in resume/ (old flat layout) or in a per-job subfolder both count.
        matches = sorted(self.cfg.resume_dir.rglob(f"*{applicant['application_id']}*.pdf"))
        if not matches:
            return False
        self._record_resume(applicant, matches[0])  # sets fields + parses contact
        log.info("Already have resume for %s — skipping download.",
                 applicant["application_id"])
        return True

    def iter_job_applicants(self, job: dict[str, Any], limit: int | None = None,
                            skip_keys: set[str] | None = None):
        """Yield fully-extracted applicants for a job in a SINGLE pass.

        The candidate list is virtualized (only ~25 cards stay in the DOM), so we
        must process each candidate WHILE its card is loaded: pick the first
        unprocessed card, select it (renders the detail panel + keeps its Resumé
        button in the DOM), extract fields, yield it — then scroll to reveal more.
        The caller downloads the resume and upserts before the next yield.

        `skip_keys` holds candidate_keys already downloaded in a previous run; cards
        whose key is derived (cheaply, pre-click, from name + applied date) and
        found here are yielded as light duplicates without clicking — making
        re-downloads near-instant.
        """
        page = self.page
        job_id = job["job_id"]
        skip_keys = skip_keys or set()
        try:
            self._goto_scoped(f"/candidates/?jobid={job_id}",
                              wait_selector=SELECTORS["applicant_card"], timeout=40000)
            page.wait_for_timeout(2000)
        except PWTimeout:
            log.warning("No candidate cards for job %s (landed on %s). Dumping page.",
                        job_id, page.url[:90])
            self.dump(f"candlist_miss_{job_id}")
            return

        # Total across ALL folder tabs (inbox + shortlist + rejected + …) so the
        # progress bar reflects every bucket we're about to scrape.
        self.last_candidate_total = self._all_folder_total()

        processed: set[str] = set()
        # 1) The default (inbox / กล่องข้อความ) view.
        yield from self._process_view(job, processed, limit, skip_keys)
        # 2) Every other non-empty folder tab (ชอร์ตลิสต์, คุณสมบัติไม่ตรง, …). Clicking a
        #    tab re-filters the list; dedup is via `processed` here PLUS the DB MERGE on
        #    candidate_key, so a candidate is never downloaded or stored twice.
        if not (limit and len(processed) >= limit):
            try:
                yield from self._iter_other_folders(job, processed, limit, skip_keys)
            except Exception as exc:  # noqa: BLE001 — never lose the inbox results
                log.warning("Folder iteration stopped early: %s", exc)
        log.info("Job %s: processed %d candidate(s) across all folders.",
                 job_id, len(processed))

    def _process_view(self, job: dict[str, Any], processed: set[str],
                      limit: int | None, skip_keys: set[str]):
        """Process every candidate card in the CURRENT list view (one folder tab),
        yielding each. The list is virtualized, so pick the first unprocessed card,
        select it, extract, yield — then scroll for more until none are new. The
        shared `processed` set (card keys) dedups across folders. Cards whose
        candidate_key is in `skip_keys` are yielded as light duplicates (no click).
        """
        page = self.page
        job_id = job["job_id"]
        stale = 0
        while not (limit and len(processed) >= limit):
            # Find the first card in the current DOM we haven't handled yet.
            target = None
            for card in page.query_selector_all(SELECTORS["applicant_card"]):
                key = self._card_key(card)
                if key and key not in processed:
                    target = (card, key)
                    break

            if target is None:
                # Reveal more: scroll the last card into view; try Next as a fallback.
                cards = page.query_selector_all(SELECTORS["applicant_card"])
                if cards:
                    try:
                        cards[-1].scroll_into_view_if_needed(timeout=5000)
                        page.wait_for_timeout(1500)
                    except Exception:  # noqa: BLE001
                        pass
                nxt = page.query_selector(SELECTORS["applicant_next_page"])
                if nxt and nxt.is_enabled():
                    try:
                        nxt.click()
                        page.wait_for_timeout(2000)
                    except Exception:  # noqa: BLE001
                        pass
                stale += 1
                if stale >= 3:
                    break
                continue

            stale = 0
            card, key = target
            processed.add(key)
            try:
                card.scroll_into_view_if_needed(timeout=5000)
            except Exception:  # noqa: BLE001
                pass
            name = self._card_name(card)
            applied = self._card_applied(card)
            expect_salary = self._card_expected_salary(card)

            # Fast path: a candidate already downloaded in a previous run. The name
            # and applied date are readable from the card (no click), so derive the
            # stable candidate_key and skip if we've already got their resume — no
            # detail panel, no resume modal, no 1.5s wait. (We must NOT key on the
            # card's UUID here: SEEK regenerates it every scrape, so it would never
            # match and every candidate would be re-downloaded + re-inserted.)
            ckey = candidate_key({"full_name": name, "applied_at": applied})
            if ckey and ckey in skip_keys:
                light = {
                    "candidate_key": ckey,
                    "job_id": job_id,
                    "job_title": job.get("title"),
                    "full_name": name,
                    "applied_at": applied,
                    "expect_salary": expect_salary,
                    "_skipped_dupe": True,
                }
                yield light
                continue

            resume_btn_id = self._card_resume_btn_id(card)
            try:
                card.click()
            except Exception:  # noqa: BLE001
                try:
                    card.click(force=True)
                except Exception:  # noqa: BLE001
                    continue
            page.wait_for_timeout(1500)

            m = re.search(r"selected=([0-9a-f-]{36})", page.url, re.I)
            application_id = m.group(1) if m else key

            if not self._detail_dumped:
                self.dump("applicant_detail_sample")
                self._detail_dumped = True

            email = self._attr(page, SELECTORS["detail_email"], "href")
            if email:
                email = email.replace("mailto:", "").split("?")[0]
            phone = self._attr(page, SELECTORS["detail_phone"], "href")
            if phone:
                phone = phone.replace("tel:", "").strip()

            applicant: dict[str, Any] = {
                "application_id": application_id,
                "job_id": job_id,
                "job_title": job.get("title"),   # used to folder the resume by job
                "full_name": name,
                "email": email,
                "phone": phone,
                "expect_salary": expect_salary,
                "location": None,
                "status": None,
                "applied_at": applied,
                "resume_btn_id": resume_btn_id,
                "resume_filename": None,
                "resume_path": None,
                "resume_downloaded": False,
                "url": self._url(f"/candidates/?jobid={job_id}&selected={application_id}"),
            }
            applicant["raw_json"] = {k: v for k, v in applicant.items()
                                     if k not in ("raw_json", "resume_btn_id")}
            yield applicant

    def _all_folder_total(self) -> int | None:
        """Sum of every folder tab's count ('<N> applications'): total candidates
        across inbox + shortlist + rejected + … (None if unreadable)."""
        try:
            counts = self.page.evaluate(
                r"""() => {
                    const out = [];
                    for (const el of document.querySelectorAll('[aria-label]')) {
                        const m = (el.getAttribute('aria-label')||'').match(/^(\d+) applications?$/);
                        if (m) out.push(parseInt(m[1], 10));
                    }
                    return out;
                }"""
            )
        except Exception:  # noqa: BLE001
            return None
        return sum(counts) if counts else None

    def _folder_tabs(self):
        """Sidebar folder-tab buttons that carry a bare '<N> applications' badge, as
        (handle, count, label) in DOM order. The active inbox tab is NOT a button, so
        this returns the OTHER folders. One DOM pass; handles map back by index."""
        buttons = self.page.query_selector_all("button")
        infos = self.page.evaluate(
            r"""() => {
                const out = [];
                document.querySelectorAll('button').forEach((el, i) => {
                    let cnt = null;
                    for (const x of el.querySelectorAll('[aria-label]')) {
                        const m = (x.getAttribute('aria-label')||'').match(/^(\d+) applications?$/);
                        if (m) { cnt = parseInt(m[1], 10); break; }
                    }
                    if (cnt !== null)
                        out.push({i, cnt, label: (el.textContent||'').replace(/\s+/g,' ').trim()});
                });
                return out;
            }"""
        )
        tabs = []
        for info in infos:
            if info["i"] < len(buttons):
                tabs.append((buttons[info["i"]], info["cnt"], info["label"]))
        return tabs

    def _iter_other_folders(self, job: dict[str, Any], processed: set[str],
                            limit: int | None, skip_keys: set[str]):
        """Click through every non-empty folder tab (besides the inbox) and process
        its candidates. Tabs are identified by their (stable) label, so the fact that
        the active tab stops being a <button> after a click doesn't shift our cursor."""
        page = self.page
        done_labels: set[str] = set()
        for _ in range(12):                       # ≤7 folders; hard cap as a safety net
            if limit and len(processed) >= limit:
                break
            target = next(((h, c, lab) for h, c, lab in self._folder_tabs()
                           if c > 0 and lab not in done_labels), None)
            if target is None:
                break
            handle, count, label = target
            done_labels.add(label)
            try:
                handle.scroll_into_view_if_needed(timeout=4000)
                handle.click()
                page.wait_for_timeout(2500)        # let the folder's list render
                page.wait_for_selector(SELECTORS["applicant_card"], timeout=15000)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not open folder %r (count=%d): %s", label, count, exc)
                continue
            log.info("Folder %r (count=%d): scraping ...", label, count)
            yield from self._process_view(job, processed, limit, skip_keys)

    def _card_key(self, card) -> str | None:
        """A stable per-card id for dedup within a run. Prefer the Resumé button's
        UUID; fall back to the numeric profile id (present on every card) so that
        candidates without a resume are still processed.
        """
        btn = card.query_selector(SELECTORS["card_resume_button"])
        if btn:
            m = UUID_RE.search(btn.get_attribute("id") or "")
            if m:
                return m.group(0)
        el = card.query_selector("[id^='checkbox-'], [id^='avatar-']")
        if el:
            m = re.search(r"(\d{5,})", el.get_attribute("id") or "")
            if m:
                return m.group(1)
        m = UUID_RE.search(card.get_attribute("id") or "")
        return m.group(0) if m else None

    def _card_resume_btn_id(self, card) -> str | None:
        """The element id of the card's Resumé button (None if no resume)."""
        btn = card.query_selector(SELECTORS["card_resume_button"])
        return btn.get_attribute("id") if btn else None

    def _card_name(self, card) -> str | None:
        """Candidate name from the card's 'Select candidate <name>' checkbox."""
        cb = card.query_selector(SELECTORS["card_name_checkbox"])
        if cb:
            al = (cb.get_attribute("aria-label") or "").strip()
            name = re.sub(r"^Select candidate\s*", "", al).strip()
            return name or None
        return None

    def _card_applied(self, card) -> str | None:
        """ISO applied timestamp (from the 'Applied … ago' element's aria-describedby)."""
        try:
            return card.evaluate(
                r"""c => {
                    for (const x of c.querySelectorAll('[aria-describedby]')) {
                        const v = x.getAttribute('aria-describedby') || '';
                        if (/^\d{4}-\d{2}-\d{2}T/.test(v)) return v;
                    }
                    return null;
                }"""
            )
        except Exception:  # noqa: BLE001
            return None

    def _card_expected_salary(self, card) -> str | None:
        """Expected-salary answer from the card's screening Q&A (เงินเดือนที่คาดหวัง).

        The screening block uses stable data-cy hooks: a [data-cy=role-requirement]
        per question, with [data-cy=question] holding the label and [data-cy^=answer-]
        the answer (e.g. '฿30K'). Returns the answer with the ฿ symbol stripped
        ('30K'), or None if the question isn't present.
        """
        try:
            raw = card.evaluate(
                r"""c => {
                    for (const rr of c.querySelectorAll('[data-cy="role-requirement"]')) {
                        const q = rr.querySelector('[data-cy="question"]');
                        // Match the expected-salary question in either the Thai
                        // ('เงินเดือนที่คาดหวัง') or English ('Expected monthly salary')
                        // wording — job postings ask it in whichever language they
                        // were created in.
                        const txt = q ? q.textContent : '';
                        const isSalary = txt.includes('เงินเดือนที่คาดหวัง')
                            || (/expected/i.test(txt) && /salary/i.test(txt));
                        if (isSalary) {
                            const a = [...rr.querySelectorAll('[data-cy^="answer-"]')]
                                .map(x => x.textContent.trim()).filter(Boolean);
                            return a.join(', ') || null;
                        }
                    }
                    return null;
                }"""
            )
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        return raw.replace("฿", "").strip() or None

    def download_resume(self, applicant: dict[str, Any]) -> bool:
        """Open the Resumé modal and capture the PDF from the network.

        Clicking the Resumé button opens an in-page PDF viewer (react-pdf) rather
        than downloading. We watch network responses for the PDF, save its bytes,
        then close the modal so the next candidate can be processed.
        """
        page = self.page
        app_id = applicant["application_id"]
        btn_id = applicant.get("resume_btn_id")
        btn = page.query_selector(f"button[id='{btn_id}']") if btn_id else None
        if not btn:
            log.info("No resume control for applicant %s", app_id)
            return False
        name_part = _safe_filename(applicant.get("full_name") or "", app_id)

        def _is_pdf(resp) -> bool:
            try:
                ct = (resp.headers or {}).get("content-type", "").lower()
                low = resp.url.lower()
                return ("application/pdf" in ct or low.split("?")[0].endswith(".pdf")) \
                    and ".js" not in low
            except Exception:  # noqa: BLE001
                return False

        ok = False
        try:
            # Clicking the Resumé button opens an in-page PDF viewer that fetches
            # the file; grab the actual response bytes (no re-fetch of a signed URL).
            with page.expect_response(_is_pdf, timeout=25000) as resp_info:
                try:
                    btn.click()
                except Exception:  # noqa: BLE001
                    btn.click(force=True)
            resp = resp_info.value
            body = resp.body()
            if body:
                dest = self._resume_dir_for(applicant) / f"{name_part}_{app_id}.pdf"
                dest.write_bytes(body)
                self._record_resume(applicant, dest)
                log.info("Saved resume -> %s", dest)
                ok = True
            else:
                log.warning("Empty resume response for %s", app_id)
        except PWTimeout:
            log.warning("No resume PDF captured for %s", app_id)
            if not self._resume_net_logged:
                self.dump("resume_modal_sample")
                self._resume_net_logged = True
        finally:
            # Dismiss the PDF modal so it doesn't block the next candidate.
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001
                pass
        return ok

    def _record_resume(self, applicant: dict[str, Any], dest: Path) -> None:
        applicant["resume_filename"] = dest.name
        applicant["resume_path"] = str(dest)
        applicant["resume_downloaded"] = True
        # SEEK hides email/phone from the page DOM; recover them from the CV text.
        if not applicant.get("email") or not applicant.get("phone"):
            email, phone = _contact_from_pdf(dest)
            if email and not applicant.get("email"):
                applicant["email"] = email
            if phone and not applicant.get("phone"):
                applicant["phone"] = phone
        applicant["raw_json"] = {k: v for k, v in applicant.items()
                                 if k not in ("raw_json", "resume_btn_id")}
