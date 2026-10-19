"""Device-code sign-in to the shared Recruit mailbox, run in a background thread."""
from __future__ import annotations

import threading

from config import Config
from services.mailer import GraphMailer, MailerError


class EmailAuth:
    """Drives the one-time Microsoft Graph device-code sign-in for the shared Recruit
    mailbox. Sign-in blocks until the user enters the code, so it runs in a background
    thread while the web UI polls /api/email/login-status."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self.status = "idle"            # idle | pending | done | error
        self.user_code: str | None = None
        self.verification_uri: str | None = None
        self.error: str | None = None

    def snapshot(self) -> dict:
        account = None
        try:
            account = GraphMailer(self.cfg).signed_in_account()
        except MailerError:
            account = None
        with self._lock:
            return {"signed_in": bool(account), "account": account,
                    "status": self.status, "user_code": self.user_code,
                    "verification_uri": self.verification_uri, "error": self.error}

    def start(self) -> None:
        with self._lock:
            if self.status == "pending":
                return
            self.status = "pending"
            self.user_code = self.verification_uri = self.error = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            mailer = GraphMailer(self.cfg)
            flow = mailer.begin_device_login()
            with self._lock:
                self.user_code = flow.get("user_code")
                self.verification_uri = flow.get("verification_uri")
            mailer.complete_device_login(flow)        # blocks until the user signs in
            with self._lock:
                self.status = "done"
        except Exception as exc:  # noqa: BLE001 — surface to the UI
            with self._lock:
                self.status, self.error = "error", str(exc)
