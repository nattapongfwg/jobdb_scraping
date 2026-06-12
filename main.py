"""Orchestrator for scraping the SEEK employer portal into SQL Server.

Usage examples:
    python main.py --headed --login-only       # confirm login works
    python main.py --job-id ABC123 --limit 3    # small dry run
    python main.py                              # full run (headless)
"""
from __future__ import annotations

import argparse
import logging
import sys

from config import load_config
from db import Database, ensure_database, ensure_schema
from scraper import ScrapeStats, SeekScraper


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape SEEK employer portal into SQL Server.")
    p.add_argument("--headed", action="store_true", help="Show the browser window (default headless).")
    p.add_argument("--manual-login", action="store_true",
                   help="One-time interactive login: solve the CAPTCHA yourself; saves the session.")
    p.add_argument("--login-only", action="store_true", help="Verify the saved session and exit.")
    p.add_argument("--discover", action="store_true",
                   help="Log in, then dump the landing + job pages to debug/ for selector discovery.")
    p.add_argument("--list-jobs-json", action="store_true",
                   help="Log in, list the active job ads, print them as JSON (for the web UI), and exit.")
    p.add_argument("--job-id", help="Restrict to a single job id.")
    p.add_argument("--job-title", help="Restrict to open jobs whose title contains this text "
                                       "(e.g. \"Software Implementer\").")
    p.add_argument("--limit", type=int, help="Max applicants per job (for testing).")
    p.add_argument("--no-resumes", action="store_true", help="Skip resume downloads.")
    p.add_argument("--redownload", action="store_true",
                   help="Re-download resumes even if a file already exists (default: skip dupes).")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("main")
    cfg = load_config()

    # Manual login is a standalone, DB-free flow: open a visible browser, let the
    # user solve the CAPTCHA, and save the session into the persistent profile.
    if args.manual_login:
        with SeekScraper(cfg, headed=True) as scraper:
            ok = scraper.manual_login()
        log.info("Manual login %s.", "succeeded" if ok else "FAILED")
        return 0 if ok else 3

    # Discovery is a DB-free flow: log in (interactively if needed), then dump the
    # landing page and several candidate "job ads" pages to debug/ so the real
    # selectors/URLs can be read and wired into scraper.py SELECTORS.
    if args.discover:
        import json

        def capture_links(scraper, name):
            try:
                links = scraper.page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => ({text: (e.innerText||'').trim().slice(0,80),"
                    " href: e.getAttribute('href')})).filter(l => l.href)",
                )
                (cfg.debug_dir / f"{name}_links.json").write_text(
                    json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")
                log.info("  %s: %d links -> debug/%s_links.json", name, len(links), name)
                return links
            except Exception as exc:  # noqa: BLE001
                log.warning("link capture (%s): %s", name, exc)
                return []

        with SeekScraper(cfg, headed=True) as scraper:
            if not scraper.ensure_session():
                return 3
            scraper.page.wait_for_timeout(2000)
            scraper.dump("discover_dashboard")
            log.info("Dashboard URL: %s", scraper.page.url)
            capture_links(scraper, "discover_dashboard")

            import re as _re
            # /jobs -> capture, find first jobid
            scraper.page.goto(scraper._url("/jobs"), wait_until="domcontentloaded", timeout=40000)
            scraper.page.wait_for_timeout(4000)
            scraper.dump("discover_jobs")
            jl = capture_links(scraper, "discover_jobs")
            jobid = None
            for l in jl:
                m = _re.search(r"jobid=(\d+)", l["href"], _re.I)
                if m:
                    jobid = m.group(1); break
            log.info("First jobid: %s", jobid)

            if jobid:
                # per-job candidate list
                scraper.page.goto(scraper._url(f"/candidates/?jobid={jobid}"),
                                  wait_until="domcontentloaded", timeout=40000)
                scraper.page.wait_for_timeout(5000)
                scraper.dump("discover_candlist")
                cl = capture_links(scraper, "discover_candlist")
                uuid = None
                for l in cl:
                    m = _re.search(r"selected=([0-9a-f-]{36})", l["href"], _re.I)
                    if m:
                        uuid = m.group(1); break
                log.info("First candidate uuid: %s", uuid)

                if uuid:
                    # candidate detail panel
                    scraper.page.goto(scraper._url(f"/candidates/?jobid={jobid}&selected={uuid}"),
                                      wait_until="domcontentloaded", timeout=40000)
                    scraper.page.wait_for_timeout(5000)
                    scraper.dump("discover_canddetail")
                    capture_links(scraper, "discover_canddetail")
        log.info("Discovery dumps written to %s", cfg.debug_dir)
        return 0

    # List the active job ads as JSON, for the web UI's "Scrape from SEEK" dropdown.
    # DB-free: log in (interactively if headed and needed), list jobs, print, exit.
    # The web UI keys on the JOBS_JSON: / JOBS_JSON_ERROR: prefixes below.
    if args.list_jobs_json:
        import json
        with SeekScraper(cfg, headed=args.headed) as scraper:
            if not scraper.ensure_session():
                print("JOBS_JSON_ERROR:no-session", flush=True)
                return 3
            jobs = scraper.list_jobs()
        # ensure_ascii=True keeps this line pure-ASCII so it survives a Windows
        # console / pipe whatever its encoding (Thai titles become \uXXXX escapes,
        # which the browser's JSON.parse decodes back).
        print("JOBS_JSON:" + json.dumps(
            [{"job_id": j["job_id"], "title": j.get("title")} for j in jobs],
            ensure_ascii=True), flush=True)
        return 0

    # 1) DB connectivity + schema bootstrap.
    log.info("Connecting to SQL Server %s (db=%s) ...", cfg._server, cfg.db_name)
    try:
        ensure_database(cfg)
        ensure_schema(cfg)
    except Exception as exc:  # noqa: BLE001 — surface clearly and stop
        log.error("Database setup failed: %s", exc)
        log.error("Verify SQL Server is running and DB_HOST/DB_INSTANCE in .env are correct "
                  "(e.g. localhost + SQLEXPRESS).")
        return 2

    stats = ScrapeStats()
    with SeekScraper(cfg, headed=args.headed) as scraper, Database(cfg) as db:
        # 2) Ensure a live session (interactive login if headed and needed).
        if not scraper.ensure_session():
            log.error("No session; aborting. Re-run with --headed to log in.")
            return 3
        if args.login_only:
            log.info("Session OK. Exiting (--login-only).")
            return 0

        # 3) Choose which job(s) to scrape.
        if args.job_id:
            # Resolve the real title/location from the open-jobs list so the job is
            # stored with its proper (often Thai) name instead of "(untitled)" and
            # so resumes are foldered under the correct title.
            match = next((j for j in scraper.list_jobs() if j["job_id"] == args.job_id), None)
            jobs = [match] if match else [
                {"job_id": args.job_id, "title": None, "location": None, "url": None}]
        elif args.job_title:
            jobs = scraper.find_jobs_by_title(args.job_title)
            if not jobs:
                log.error("No open job matches title %r. Available open jobs:", args.job_title)
                for j in scraper.list_jobs():
                    log.error("  - %s (jobid=%s)", j.get("title"), j["job_id"])
                return 4
        else:
            jobs = scraper.list_jobs()

        for job in jobs:
            try:
                db.upsert_job(job)
                stats.jobs += 1
            except Exception as exc:  # noqa: BLE001
                stats.failures.append(f"job {job['job_id']}: {exc}")
                continue

            # 4) Applicants for this job — single pass over the virtualized list.
            log.info("Job %s (%s): scraping applicants ...",
                     job["job_id"], job.get("title") or "?")
            # Fast re-downloads: skip candidates already downloaded last time WITHOUT
            # clicking their card. --redownload disables the skip (re-fetches all).
            skip_ids = (set() if args.redownload
                        else db.get_downloaded_application_ids(job["job_id"]))
            if skip_ids:
                log.info("  %d already-downloaded candidate(s) will be skipped fast "
                         "(use --redownload to force re-fetch).", len(skip_ids))
            # Progress lines (SCRAPE_TOTAL / SCRAPE_PROGRESS) drive the web UI's
            # progress bar; the ScrapeManager parses them from stdout.
            done = 0
            total_emitted = False
            for applicant in scraper.iter_job_applicants(
                    job, limit=args.limit, skip_ids=skip_ids):
                if not total_emitted:
                    total = getattr(scraper, "last_candidate_total", None) or 0
                    if args.limit:
                        total = min(total, args.limit) if total else args.limit
                    print(f"SCRAPE_TOTAL:{total}", flush=True)
                    total_emitted = True
                try:
                    if applicant.get("_skipped_dupe"):
                        # Already downloaded — refresh only the cheap card fields,
                        # preserving resume/email/phone/edited name. No resume work.
                        db.light_upsert_applicant(applicant)
                        stats.skipped += 1
                    else:
                        # 5) Resume: skip the file if already on disk (unless --redownload).
                        if not args.no_resumes:
                            if not args.redownload and scraper.reuse_existing_resume(applicant):
                                stats.skipped += 1
                            elif scraper.download_resume(applicant):
                                stats.resumes += 1
                        db.upsert_applicant(applicant)
                        stats.applicants += 1
                        scraper._pause()
                except Exception as exc:  # noqa: BLE001
                    aid = applicant.get("application_id")
                    stats.failures.append(f"applicant {aid}: {exc}")
                    log.warning("Failed applicant %s: %s", aid, exc)
                done += 1
                print(f"SCRAPE_PROGRESS:{done}", flush=True)

    # 6) Summary.
    log.info("=" * 60)
    log.info("Done. jobs=%d applicants=%d resumes=%d skipped(dupes)=%d failures=%d",
             stats.jobs, stats.applicants, stats.resumes, stats.skipped, len(stats.failures))
    if stats.failures:
        for f in stats.failures[:20]:
            log.warning("  - %s", f)
    return 0


if __name__ == "__main__":
    sys.exit(run(parse_args()))
