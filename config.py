"""Configuration loading for the SEEK employer-portal scraper.

Loads settings from a local .env file and resolves the Windows host IP so the
script (running in WSL2) can reach a SQL Server instance on the Windows host.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent

load_dotenv(PROJECT_ROOT / ".env")


def _resolve_windows_host() -> str:
    """Best-effort discovery of the Windows host IP from inside WSL2.

    WSL2 (NAT mode) runs in its own network namespace, so 'localhost' does not
    reach services on the Windows host. In NAT mode the Windows host is reachable
    via the default-route gateway (e.g. 172.x.x.1). The /etc/resolv.conf
    nameserver (often 10.255.255.254) is only a DNS stub and does NOT route to
    arbitrary Windows TCP services, so we prefer the gateway.
    """
    # 1) default gateway from `ip route` (the Windows host in WSL2 NAT mode)
    try:
        out = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, check=False,
        ).stdout
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except OSError:
        pass

    # 2) nameserver in /etc/resolv.conf (fallback; may be a non-routable stub)
    try:
        resolv = Path("/etc/resolv.conf").read_text(encoding="utf-8")
        m = re.search(r"^\s*nameserver\s+(\d+\.\d+\.\d+\.\d+)", resolv, re.MULTILINE)
        if m:
            return m.group(1)
    except OSError:
        pass

    # 3) last resort
    return "localhost"


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # SEEK
    seek_email: str
    seek_password: str
    seek_base_url: str
    advertiser_id: str     # which employer account to scrape (blank = first listed)

    # SQL Server
    db_host: str
    db_instance: str       # named instance, e.g. SQLEXPRESS (blank for default)
    db_port: str
    db_name: str
    db_user: str
    db_password: str
    db_driver: str
    db_trusted: bool       # True -> Windows Authentication (no user/password)

    # Scraper
    resume_dir: Path
    shortlist_dir: Path        # local OneDrive path for per-shortlist folders (fallback copy)
    shortlist_onedrive_dir: str  # OneDrive-relative folder for Graph upload + share link
    reply_exam_dir: Path       # local OneDrive path: Email_Reply_Exam/<name>/ (résumé + reply files)
    debug_dir: Path
    profile_dir: Path      # persistent browser user-data dir (keeps the login)
    storage_state: Path
    schema_sql: Path
    delay_min: float
    delay_max: float

    # Microsoft Graph (exam email) + exam content
    graph_tenant_id: str
    graph_client_id: str
    graph_client_secret: str
    graph_sender: str       # mailbox the exam is sent from (e.g. HumanResources@...)
    exam_subject: str
    exam_body: str          # may contain {name}
    exam_attachment: str    # path to the exam file to attach (optional)

    @property
    def _server(self) -> str:
        """SERVER= value. Named instance uses host\\instance; otherwise host,port."""
        if self.db_instance:
            return f"{self.db_host}\\{self.db_instance}"
        return f"{self.db_host},{self.db_port}"

    @property
    def _auth(self) -> str:
        if self.db_trusted:
            return "Trusted_Connection=yes;"
        return f"UID={self.db_user};PWD={self.db_password};"

    def _conn_str(self, database: str) -> str:
        return (
            f"DRIVER={{{self.db_driver}}};"
            f"SERVER={self._server};"
            f"DATABASE={database};"
            f"{self._auth}"
            "Encrypt=yes;TrustServerCertificate=yes;"
        )

    @property
    def odbc_connection_string(self) -> str:
        return self._conn_str(self.db_name)

    @property
    def odbc_connection_string_master(self) -> str:
        """Targets the 'master' DB, used to CREATE DATABASE if missing."""
        return self._conn_str("master")


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _default_host() -> str:
    """When DB_HOST is unset: 'localhost' on native Windows; on Linux/WSL fall
    back to the Windows host gateway (only relevant if reaching a remote server).
    """
    if sys.platform == "win32":
        return "localhost"
    return _resolve_windows_host()


def load_config() -> Config:
    db_host = os.getenv("DB_HOST", "").strip() or _default_host()
    # Default to Windows Authentication when no DB_USER is provided.
    db_user = os.getenv("DB_USER", "").strip()
    db_trusted = _get_bool("DB_TRUSTED", default=(db_user == ""))
    resume_env = os.getenv("RESUME_DIR", "").strip()
    resume_dir = Path(resume_env).resolve() if resume_env else (PROJECT_ROOT / "resume")
    # OneDrive base where the app stores candidate folders (synced, shareable).
    # Override per-machine via ONEDRIVE_BASE in .env (the default is one user's path).
    onedrive_base = (os.getenv("ONEDRIVE_BASE", "").strip()
                     or r"C:\Users\nattapong_yuw.FREEWILLGROUP\OneDrive - "
                        r"freewillsolutions.com\Candidate_JobDB_Scraping")
    # Shortlist resume folders go in OneDrive (so the folder gets a shareable link).
    shortlist_env = os.getenv("SHORTLIST_DIR", "").strip()
    shortlist_dir = Path(shortlist_env) if shortlist_env else Path(onedrive_base) / "Shortlists"
    # OneDrive-relative folder (under the drive root) for the Graph upload + share link.
    shortlist_onedrive_dir = (os.getenv("SHORTLIST_ONEDRIVE_DIR", "").strip()
                              or "Candidate_JobDB_Scraping/Shortlists")
    # Local OneDrive folder where exam-reply files (résumé + reply attachments) are saved.
    reply_env = os.getenv("REPLY_EXAM_DIR", "").strip()
    reply_exam_dir = Path(reply_env) if reply_env else Path(onedrive_base) / "Email_Reply_Exam"

    return Config(
        seek_email=os.getenv("SEEK_EMAIL", "").strip(),
        seek_password=os.getenv("SEEK_PASSWORD", ""),
        seek_base_url=os.getenv("SEEK_BASE_URL", "https://th.employer.seek.com").rstrip("/"),
        advertiser_id=os.getenv("ADVERTISER_ID", "").strip(),
        db_host=db_host,
        db_instance=os.getenv("DB_INSTANCE", "").strip(),
        db_port=os.getenv("DB_PORT", "1433").strip(),
        db_name=os.getenv("DB_NAME", "jobdb_scraping").strip(),
        db_user=db_user,
        db_password=os.getenv("DB_PASSWORD", ""),
        db_driver=os.getenv("DB_DRIVER", "ODBC Driver 18 for SQL Server").strip(),
        db_trusted=db_trusted,
        resume_dir=resume_dir,
        shortlist_dir=shortlist_dir,
        shortlist_onedrive_dir=shortlist_onedrive_dir,
        reply_exam_dir=reply_exam_dir,
        debug_dir=(PROJECT_ROOT / "debug"),
        profile_dir=(PROJECT_ROOT / ".browser_profile"),
        storage_state=(PROJECT_ROOT / "storage_state.json"),
        schema_sql=(PROJECT_ROOT / "schema.sql"),
        delay_min=_get_float("DELAY_MIN", 1.0),
        delay_max=_get_float("DELAY_MAX", 2.5),
        graph_tenant_id=os.getenv("GRAPH_TENANT_ID", "").strip(),
        graph_client_id=os.getenv("GRAPH_CLIENT_ID", "").strip(),
        graph_client_secret=os.getenv("GRAPH_CLIENT_SECRET", "").strip(),
        graph_sender=os.getenv("GRAPH_SENDER", "").strip(),
        exam_subject=os.getenv("EXAM_SUBJECT", "Pre-employment exam invitation"),
        # .env can't hold real newlines, so allow literal "\n" in EXAM_BODY.
        exam_body=os.getenv(
            "EXAM_BODY",
            "Dear {name},\n\nThank you for applying. Please complete the attached "
            "exam and reply with your answers.\n\nBest regards,\nRecruitment Team"
        ).replace("\\n", "\n"),
        exam_attachment=os.getenv("EXAM_ATTACHMENT", "").strip(),
    )


if __name__ == "__main__":
    cfg = load_config()
    print("Platform         :", sys.platform)
    print("DB server        :", cfg._server)
    print("DB auth          :", "Windows Auth" if cfg.db_trusted else f"SQL login ({cfg.db_user})")
    print("Resume dir       :", cfg.resume_dir)
    print("SEEK base URL    :", cfg.seek_base_url)
    print("SEEK email       :", cfg.seek_email or "(not set)")
