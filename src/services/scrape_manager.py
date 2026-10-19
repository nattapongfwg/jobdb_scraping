"""Background runner for the SEEK scraper CLI (src/main.py), polled by the web UI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

from config import PROJECT_ROOT, SRC_DIR


class ScrapeManager:
    """Runs the SEEK scraper (main.py) as a background subprocess and tracks its
    live status/log so the web UI can poll progress without blocking a request.

    Two actions:
      * fetch-jobs — `main.py --headed --list-jobs-json`: logs in (you solve the
        CAPTCHA in the browser window the first time) and returns the active jobs.
      * download   — `main.py --headed --job-id <id>`: downloads that job's
        candidates + resumes into SQL Server.
    Only one action runs at a time (login holds the single browser profile)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status = "idle"      # idle | running | done | error
        self.action: str | None = None
        self.log: list[str] = []
        self.jobs: list[dict] | None = None
        self.error: str | None = None
        self.progress: dict = {"done": 0, "total": 0}
        self.started_at: float | None = None

    def is_running(self) -> bool:
        return self.status == "running"

    def snapshot(self) -> dict:
        with self._lock:
            prog = dict(self.progress)
            if self.started_at is not None:
                elapsed = time.monotonic() - self.started_at
                prog["elapsed_sec"] = int(elapsed)
                done, total = prog.get("done", 0), prog.get("total", 0)
                # ETA = average time per candidate so far × candidates remaining.
                if done > 0 and total and done <= total:
                    prog["eta_sec"] = int((elapsed / done) * (total - done))
            return {"status": self.status, "action": self.action,
                    "log": self.log[-50:], "jobs": self.jobs,
                    "error": self.error, "progress": prog}

    def _start(self, action: str, args: list[str], parse_jobs: bool = False) -> bool:
        with self._lock:
            if self.status == "running":
                return False
            self.status, self.action = "running", action
            self.log, self.error, self.jobs = [], None, None
            self.progress = {"done": 0, "total": 0}
            self.started_at = time.monotonic()
        threading.Thread(target=self._run, args=(args, parse_jobs), daemon=True).start()
        return True

    def _append(self, line: str) -> None:
        with self._lock:
            self.log.append(line)
            if len(self.log) > 300:
                self.log = self.log[-300:]

    def _run(self, args: list[str], parse_jobs: bool) -> None:
        try:
            # Force UTF-8 in the child (Windows defaults to cp1252, which raises
            # UnicodeEncodeError on Thai names/titles) and decode its output as UTF-8.
            env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            proc = subprocess.Popen(
                [sys.executable, str(SRC_DIR / "main.py"), *args], cwd=str(PROJECT_ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            for line in proc.stdout:                       # type: ignore[union-attr]
                line = line.rstrip("\n")
                if parse_jobs and line.startswith("JOBS_JSON:"):
                    try:
                        with self._lock:
                            self.jobs = json.loads(line[len("JOBS_JSON:"):])
                    except json.JSONDecodeError:
                        with self._lock:
                            self.jobs = []
                    continue
                if line.startswith("JOBS_JSON_ERROR"):
                    with self._lock:
                        self.error = ("Not logged in to SEEK. A browser window should "
                                      "open — solve the login/CAPTCHA, then try again.")
                    continue
                if line.startswith("SCRAPE_TOTAL:"):
                    with self._lock:
                        self.progress["total"] = int(line.split(":", 1)[1] or 0)
                    continue
                if line.startswith("SCRAPE_PROGRESS:"):
                    with self._lock:
                        self.progress["done"] = int(line.split(":", 1)[1] or 0)
                    continue
                if line:
                    self._append(line)
            proc.wait()
            with self._lock:
                if self.error:
                    self.status = "error"
                elif proc.returncode == 0:
                    self.status = "done"
                else:
                    self.status = "error"
                    self.error = f"Scraper exited with code {proc.returncode}."
        except Exception as exc:  # noqa: BLE001 — surface any launch failure to the UI
            with self._lock:
                self.status, self.error = "error", str(exc)

    def fetch_jobs(self) -> bool:
        return self._start("fetch-jobs", ["--headed", "--list-jobs-json"], parse_jobs=True)

    def download(self, job_id: str) -> bool:
        return self._start("download", ["--headed", "--job-id", str(job_id)])
