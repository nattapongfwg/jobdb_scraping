"""The Recruitment board web app (Flask), laid out MVC-style under src/:

  models/       the SQL Server data layer (db.py) and its schema
  views/        Jinja templates and static assets
  controllers/  one Flask blueprint per area of the board (the routes)
  services/     business logic and integrations (SEEK scraper, Graph mail, OpenAI, Excel)

Run:  .venv\\Scripts\\python.exe src\\webapp.py   then open http://localhost:2757
(normally started by service.ps1 / run_webapp.cmd).
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta

from flask import Flask

from config import PROJECT_ROOT
from controllers import (auth, candidates, emails, evaluation_offer, hiring_requests, jobs,
                         tracking, users)
from controllers.common import cfg, current_email
from models.db import Database, ensure_database, ensure_schema
from services.email_kit import signature
from services.email_kit.templates import configure_store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
app = Flask(__name__, template_folder="views/templates", static_folder="views/static")
app.config["MAX_CONTENT_LENGTH"] = candidates.MAX_RESUME_MB * 1024 * 1024


def _secret_key() -> str:
    """Signs the session cookie. SECRET_KEY in .env wins; otherwise a random key is
    generated once and kept in .secret_key (gitignored) so sign-ins survive restarts."""
    env = os.getenv("SECRET_KEY", "").strip()
    if env:
        return env
    path = PROJECT_ROOT / ".secret_key"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    path.write_text(key, encoding="utf-8")
    return key


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=7))

for _controller in (auth, jobs, tracking, candidates, evaluation_offer, emails, users,
                    hiring_requests):
    app.register_blueprint(_controller.bp)


# Email templates live in the shared database so every recruiter edits the same set
# (the JSON file next to email_kit keeps only this PC's settings). The first load
# migrates any templates still in that file.
def _db_load_templates() -> list[dict] | None:
    with Database(cfg) as db:
        return db.load_email_templates()


def _db_save_templates(templates: list[dict]) -> None:
    with Database(cfg) as db:
        db.save_email_templates(templates, actor=current_email())


configure_store(_db_load_templates, _db_save_templates)


# Each user's saved signature label per email type (fills a template's {signature}).
def _db_load_signatures(user_id: int) -> dict[str, str]:
    with Database(cfg) as db:
        return db.get_user_signatures(user_id)


signature.configure_label_store(_db_load_signatures)


if __name__ == "__main__":
    # Ensure DB + schema (adds the new columns) before serving.
    ensure_database(cfg)
    ensure_schema(cfg)
    # First run: give the board one admin user built from the signature defaults and
    # this machine's folder prefix, so emails keep signing exactly as before.
    with Database(cfg) as _db:
        if _db.seed_default_user(email=signature.RECRUITER_EMAIL, name=signature.RECRUITER_NAME,
                                 firstname=signature.RECRUITER_FIRSTNAME,
                                 mobile=signature.RECRUITER_MOBILE, tel=signature.RECRUITER_TEL,
                                 prefix="Na"):
            logging.info("Seeded the first board user (admin).")
    # PORT lets a dev copy of the board run beside the live one (default 2757).
    # HOST: 127.0.0.1 (default) = this PC only; 0.0.0.0 = other PCs on the LAN too
    # (open the port with  service.ps1 firewall).
    host = (os.getenv("HOST") or "127.0.0.1").strip()
    port = int(os.getenv("PORT", "2757"))
    try:
        from waitress import serve        # production WSGI server: many users at once
    except ImportError:
        serve = None
    if serve:
        logging.info("Serving on http://%s:%s (waitress)", host, port)
        serve(app, host=host, port=port, threads=8, ident="Recruitment")
    else:
        app.run(host=host, port=port, debug=False, threaded=True)
