"""Resume summarisation via OpenAI (ChatGPT).

Reads a candidate's resume PDF, extracts the text, and asks the model for a
concise summary (≈30-100 words, 2-4 short paragraphs) of the candidate's
background, skills and experience so HR can understand the candidate at a glance.
Used when a candidate reaches the "Sent Exam" pipeline stage.

The OpenAI key/model come from .env (OPENAI_API_KEY, OPENAI_MODEL); config.py
loads .env into the environment at import time, so they are available here.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests
from pypdf import PdfReader

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5.4-nano"
# Target length, expressed in words (the model is asked to finish its sentences).
MIN_WORDS, MAX_WORDS = 30, 100
# Generous output cap so a ~100-word summary is never cut off mid-sentence
# (≈1.4 tokens/word + headroom).
MAX_OUTPUT_TOKENS = 250


class SummaryError(Exception):
    """Raised when a resume cannot be summarised (no key, no text, API error)."""


def _extract_text(pdf_path: Path, max_chars: int = 16000) -> str:
    """Pull plain text out of the resume PDF (truncated to keep the prompt small)."""
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:  # noqa: BLE001 — corrupt/locked PDF, surface a clean error
        raise SummaryError(f"Could not read the resume PDF: {exc}") from exc
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        try:
            txt = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — skip a page that fails to extract
            continue
        if txt:
            parts.append(txt)
            total += len(txt)
        if total >= max_chars:
            break
    return "\n".join(parts).strip()[:max_chars]


def _require_credentials() -> tuple[str, str]:
    """Read (api_key, model) from .env. Raises SummaryError if no key is set."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise SummaryError("OPENAI_API_KEY is not set in .env.")
    model = os.getenv("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
    return api_key, model


def _message_from(resp: requests.Response, empty_label: str) -> str:
    """Pull the assistant message text out of a chat-completions response, trimming
    surrounding quotes. Raises SummaryError on an API error, an unexpected response
    shape, or empty output. `empty_label` names the thing for the empty-output error
    (e.g. 'summary', 'experience summary')."""
    if resp.status_code != 200:
        detail = resp.text[:300]
        try:
            detail = resp.json().get("error", {}).get("message", detail)
        except ValueError:
            pass
        raise SummaryError(f"OpenAI API error ({resp.status_code}): {detail}")
    try:
        text = resp.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError) as exc:
        raise SummaryError(f"Unexpected OpenAI response: {exc}") from exc
    text = text.strip().strip('"').strip()
    if not text:
        raise SummaryError(f"OpenAI returned an empty {empty_label}.")
    return text


def _chat(api_key: str, model: str, messages: list[dict]) -> requests.Response:
    """POST to the chat completions endpoint, adapting to model-specific param
    rules. Newer models (e.g. GPT-5 / reasoning tiers) reject `max_tokens` (want
    `max_completion_tokens`) and/or a non-default `temperature`; we retry without
    the offending param so any model set in OPENAI_MODEL just works."""
    payload = {"model": model, "messages": messages,
               "max_tokens": MAX_OUTPUT_TOKENS, "temperature": 0.4}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last = None
    for _ in range(3):
        try:
            last = requests.post(OPENAI_URL, headers=headers, json=payload, timeout=90)
        except requests.RequestException as exc:
            raise SummaryError(f"Could not reach OpenAI: {exc}") from exc
        if last.status_code == 200:
            return last
        try:
            msg = (last.json().get("error", {}).get("message") or "").lower()
        except ValueError:
            msg = (last.text or "").lower()
        changed = False
        if "max_tokens" in msg and "max_completion_tokens" in msg:
            payload.pop("max_tokens", None)
            payload["max_completion_tokens"] = MAX_OUTPUT_TOKENS
            changed = True
        if "temperature" in msg and ("unsupported" in msg or "does not support" in msg
                                     or "only the default" in msg):
            payload.pop("temperature", None)
            changed = True
        if not changed:
            break   # unrecoverable error — let the caller surface it
    return last     # type: ignore[return-value]


def summarize_resume(resume_path: str, candidate_name: str = "",
                     job_title: str = "") -> str:
    """Return a ≈30-100 word summary (2-4 paragraphs) of one candidate's resume.

    Raises SummaryError on any failure (missing key, unreadable PDF, empty text,
    or an OpenAI API error) so the caller can surface a clean message.
    """
    api_key, model = _require_credentials()

    p = Path(resume_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        raise SummaryError(f"Resume file not found: {p}")

    text = _extract_text(p)
    if not text:
        raise SummaryError("No text could be extracted from the resume PDF "
                           "(it may be a scanned image).")

    who = candidate_name or "the candidate"
    role = f" who is applying for the role of \"{job_title}\"" if job_title else ""
    system = (
        "You are an HR recruiting assistant. You read a candidate's resume and "
        "write a clear, concise summary, in flowing English prose, that lets a "
        "hiring manager quickly understand the candidate. "
        f"Write {MIN_WORDS}-{MAX_WORDS} words, and ALWAYS split it into 2 to 4 "
        "short paragraphs. Put one blank line (two newlines) between each "
        "paragraph — do NOT return a single block of text. Keep each paragraph "
        "brief and to the point. "
        "IMPORTANT: always finish your sentences — never cut off mid-sentence and "
        "never end with an ellipsis ('...'). End on a complete, natural sentence. "
        "Cover, where the resume provides it: the current or most recent role, "
        "total years and field of experience, core technical skills and tools, "
        "notable projects or achievements, education, and key strengths. "
        "Output ONLY the summary text — no headings, labels, bullet points, or "
        "quotation marks."
    )
    user = (
        f"Summarise {who}{role}.\n\n--- RESUME TEXT ---\n{text}"
    )

    resp = _chat(api_key, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])
    return _message_from(resp, "summary")


def summarize_experience(*, ai_summary: str = "", resume_path: str = "",
                         candidate_name: str = "", job_title: str = "") -> str:
    """Return a short, experience-focused blurb for the job-offer email's
    "Experience:" line. Prefers the first-step résumé summary (`ai_summary`) as the
    source when available (no PDF re-read); otherwise reads the résumé PDF again.

    Raises SummaryError on any failure so the caller can surface a clean message.
    """
    api_key, model = _require_credentials()

    source = (ai_summary or "").strip()
    origin = "candidate summary"
    if not source:
        # No first-step summary on file — read the résumé PDF again.
        if not resume_path:
            raise SummaryError("No résumé summary or résumé file to base the "
                               "experience on.")
        p = Path(resume_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if not p.exists():
            raise SummaryError(f"Resume file not found: {p}")
        source = _extract_text(p)
        origin = "résumé"
        if not source:
            raise SummaryError("No text could be extracted from the résumé PDF "
                               "(it may be a scanned image).")

    who = candidate_name or "the candidate"
    role = f" for the role of \"{job_title}\"" if job_title else ""
    system = (
        "You are an HR recruiting assistant preparing a job-offer confirmation. "
        "From the source text, write a SHORT, experience-focused description of the "
        "candidate suitable for an 'Experience:' line in an offer email. "
        "Write EXACTLY 2 SHORT paragraphs (they will be shown as 2 bullet points). "
        "Each paragraph MUST be a single line of only 10-20 words (never more than 20 "
        "words). Separate the 2 paragraphs with one blank line (two newlines). Keep it "
        "crisp and easy to scan at a glance. "
        "Focus ONLY on the most relevant work experience, key projects/achievements, "
        "and core skills/tools — omit education, contact details, and soft filler. "
        "Always finish your sentences; never end mid-sentence or with an ellipsis. "
        "Output ONLY the description — no headings, labels, the word 'Experience', "
        "bullet characters, or quotation marks."
    )
    user = f"Summarise the work experience of {who}{role}.\n\n--- {origin.upper()} ---\n{source}"

    resp = _chat(api_key, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])
    return _message_from(resp, "experience summary")
