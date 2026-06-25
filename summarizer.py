"""Resume summarisation via OpenAI (ChatGPT).

Reads a candidate's resume PDF, extracts the text, and asks the model for a
concise summary — exactly 3 short paragraphs of 10-20 words each — of the
candidate's background, skills and experience so HR can understand the candidate
at a glance. Used when a candidate reaches the "Wait Pre-screen" pipeline stage.

The OpenAI key/model come from .env (OPENAI_API_KEY, OPENAI_MODEL); config.py
loads .env into the environment at import time, so they are available here.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import requests
from pypdf import PdfReader

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5.4-nano"
# Canonical Thai-university list ChatGPT maps a résumé's university onto.
UNIVERSITIES_FILE = PROJECT_ROOT / "thai_universities.json"
# Major/field-of-study options (powers the Major dropdown; free text still allowed).
MAJORS_FILE = PROJECT_ROOT / "thai_majors.json"
# The Sent-Exam summary is exactly 3 paragraphs of this many words each.
PARAGRAPHS = 3
MIN_WORDS_PER_PARA, MAX_WORDS_PER_PARA = 10, 20
# Generous output cap so a ~60-word summary is never cut off mid-sentence
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


def _chat(api_key: str, model: str, messages: list[dict],
          response_format: dict | None = None) -> requests.Response:
    """POST to the chat completions endpoint, adapting to model-specific param
    rules. Newer models (e.g. GPT-5 / reasoning tiers) reject `max_tokens` (want
    `max_completion_tokens`) and/or a non-default `temperature`; we retry without
    the offending param so any model set in OPENAI_MODEL just works.

    `response_format` (e.g. {"type": "json_object"}) is forwarded when given so the
    model returns strict JSON; it is dropped on retry if the model rejects it."""
    payload = {"model": model, "messages": messages,
               "max_tokens": MAX_OUTPUT_TOKENS, "temperature": 0.4}
    if response_format is not None:
        payload["response_format"] = response_format
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
        if "response_format" in msg and "response_format" in payload:
            payload.pop("response_format", None)   # model can't do JSON mode; rely on the prompt
            changed = True
        if not changed:
            break   # unrecoverable error — let the caller surface it
    return last     # type: ignore[return-value]


def summarize_resume(resume_path: str, candidate_name: str = "",
                     job_title: str = "") -> str:
    """Return a summary of one candidate's resume — exactly 3 short paragraphs of
    10-20 words each.

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
        "write a clear, concise summary that lets a hiring manager quickly "
        "understand the candidate. "
        f"Write EXACTLY {PARAGRAPHS} SHORT paragraphs. Each paragraph MUST be a "
        f"single line of only {MIN_WORDS_PER_PARA}-{MAX_WORDS_PER_PARA} words "
        f"(never more than {MAX_WORDS_PER_PARA} words). Separate the paragraphs "
        "with one blank line (two newlines) — do NOT return a single block of text. "
        "Make the 3 paragraphs cover, in order: (1) the current or most recent role "
        "with total years and field of experience; (2) core technical skills and "
        "tools; (3) notable projects, achievements, or key strengths. "
        "IMPORTANT: always finish your sentences — never cut off mid-sentence and "
        "never end with an ellipsis ('...'). End on a complete, natural sentence. "
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


def summarize_experience_headline(*, ai_summary: str = "", resume_path: str = "",
                                  candidate_name: str = "", job_title: str = "") -> str:
    """Return a ONE-LINE experience headline for the offer popup's "Experience
    (headline)" field: each past job as "<Position> <Company> <duration>", joined by
    commas, e.g. "Programmer Company A 2 years, C# developer Company B 1 year 2 months".

    Prefers the résumé PDF (it carries per-job company names + dates needed to compute
    durations); falls back to the first-step résumé summary (`ai_summary`).

    Raises SummaryError on any failure so the caller can surface a clean message.
    """
    api_key, model = _require_credentials()

    # Prefer the résumé text — it has the company names and start/end dates the
    # headline needs; the 3-paragraph summary usually omits them.
    source, origin = "", ""
    if resume_path:
        p = Path(resume_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if p.exists():
            source, origin = _extract_text(p), "résumé"
    if not source:
        source, origin = (ai_summary or "").strip(), "candidate summary"
    if not source:
        raise SummaryError("No résumé or résumé summary to base the experience "
                           "headline on.")

    who = candidate_name or "the candidate"
    role = f" for the role of \"{job_title}\"" if job_title else ""
    system = (
        "You are an HR recruiting assistant preparing a job-offer confirmation. From "
        "the source text, write a ONE-LINE experience headline summarising the "
        "candidate's work history. List EACH professional job as "
        "\"<Position> <Company> <duration>\", most recent first, separated by commas. "
        "The duration is the time spent in that job, written as years and months: use "
        "the forms '2 years', '1 year 2 months', '6 months' (singular 'year'/'month' "
        "for 1; omit a zero part). Compute each duration from the job's start/end dates "
        "(count a 'Present'/'Current' role up to today) when an explicit duration is "
        "not stated. Example output: "
        "\"Programmer Company A 2 years, C# developer Company B 1 year 2 months\". "
        "Include ONLY real work experience (skip education, internships only if clearly "
        "not jobs, certifications, and skills). Output ONLY the single line — no labels, "
        "no bullets, no quotation marks, no trailing period."
    )
    user = (f"Write the experience headline for {who}{role}.\n\n"
            f"--- {origin.upper()} ---\n{source}")

    resp = _chat(api_key, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])
    # Collapse any stray line breaks into a single clean line.
    text = _message_from(resp, "experience headline")
    return " ".join(text.split())


def _load_string_list(path: Path) -> list[str]:
    """Read a JSON array of strings, trimmed and blanks dropped. Returns [] on any
    error so callers never break when the file is missing/unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(x).strip() for x in data if str(x).strip()]
    except Exception as exc:  # noqa: BLE001 — best-effort; never block on a missing list
        log.warning("Could not load %s: %s", path.name, exc)
        return []


_universities_cache: list[str] | None = None
_majors_cache: list[str] | None = None


def load_universities() -> list[str]:
    """Load (and cache) the canonical Thai-university list ChatGPT maps onto.
    Returns [] if the file is missing/unreadable so extraction still works
    (university just won't be constrained to the list)."""
    global _universities_cache
    if _universities_cache is None:
        _universities_cache = _load_string_list(UNIVERSITIES_FILE)
    return _universities_cache


def load_majors() -> list[str]:
    """Load (and cache) the Major options used by the Major dropdown."""
    global _majors_cache
    if _majors_cache is None:
        _majors_cache = _load_string_list(MAJORS_FILE)
    return _majors_cache


def _fmt_years(v: object) -> str:
    """Normalise a year value from the model to a 2-decimal string ('4.50').
    Falls back to the stripped raw text if it isn't a plain number, and to '' for
    empty/None so the UI shows a dash."""
    s = str(v if v is not None else "").strip()
    if not s:
        return ""
    try:
        return f"{float(s):.2f}"
    except ValueError:
        return s   # keep whatever the model said rather than dropping it


def extract_resume_fields(resume_path: str, candidate_name: str = "", *,
                          job_title: str = "",
                          universities: list[str] | None = None) -> dict[str, str]:
    """Read one candidate's résumé PDF and return structured fields as a dict:
    {"full_name", "university", "major", "exp_total", "exp_directly"}.

    `full_name` is a cleanly formatted full name. `university` is mapped to the
    closest entry in `universities` (the canonical list) or "" when no confident
    match exists. `major` is the field of study as written (free text).

    `exp_total` = total years of ALL work experience (decimal, e.g. "4.50").
    `exp_directly` = years of experience directly relevant to `job_title` (its
    role/technology keywords), as a decimal; always <= exp_total.

    Raises SummaryError on any failure (missing key, unreadable PDF, empty text,
    OpenAI API error, or unparseable JSON) so the caller can surface a clean message.
    """
    api_key, model = _require_credentials()
    if universities is None:
        universities = load_universities()

    p = Path(resume_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        raise SummaryError(f"Resume file not found: {p}")

    text = _extract_text(p)
    if not text:
        raise SummaryError("No text could be extracted from the resume PDF "
                           "(it may be a scanned image).")

    hint = candidate_name.strip() if candidate_name else ""
    jt = (job_title or "").strip()
    uni_list = "\n".join(f"- {u}" for u in universities) if universities else "(no list provided)"
    jt_clause = (f"The candidate is applying for the job title \"{jt}\". "
                 if jt else "No job title was provided. ")
    system = (
        "You are an HR recruiting assistant that extracts structured data from a "
        "candidate's résumé. Return ONLY a JSON object with exactly these keys: "
        "\"full_name\", \"university\", \"major\", \"exp_total\", \"exp_directly\". "
        "\"full_name\" = the candidate's full name EXACTLY as it appears in the résumé "
        "text (usually in the header/title or contact section at the top), cleanly "
        "formatted in Title Case. Read it from the résumé — do NOT invent a name and "
        "do NOT just echo any name given to you; if the résumé shows a fuller or "
        "different name than expected, use the résumé's. "
        "\"university\" = the university/college the candidate studied at, mapped to "
        "the SINGLE closest match from the allowed list below — copy that entry "
        "EXACTLY as written. If none is a confident match, use an empty string. "
        "\"major\" = the field of study / major as written on the résumé. "
        "\"exp_total\" = the candidate's TOTAL years of professional work experience, "
        "summing the duration of EVERY job/position in the résumé. Return a decimal "
        "number as a string with two decimals. Convert months to a fraction of a year "
        "(6 months = 0.5; 1 year 6 months = 1.50). For a role marked \"Present\" or "
        "\"Current\", count it up to today. Do NOT count internships shorter than the "
        "listed jobs separately if already included. Example: a 3-year job plus a "
        "1-year-6-month job = \"4.50\". If no work experience is found, use \"0.00\". "
        "\"exp_directly\" = the years of work experience DIRECTLY relevant to the job "
        "title above, as a decimal string with two decimals. Derive the key role and "
        "technology keywords from the job title (e.g. \"C# Developer\" -> \"c#\", "
        "\"developer\"; \"Java Backend Engineer\" -> \"java\", \"backend\", \"engineer\") "
        "and sum ONLY the durations of jobs whose role title or responsibilities match "
        "ANY of those keywords (same technology or role). This value must be LESS THAN "
        "OR EQUAL TO exp_total. If no job matches, use \"0.00\". "
        f"{jt_clause}"
        "If a text value cannot be found in the résumé, use an empty string. "
        "Output JSON only — no prose."
        f"\n\nALLOWED UNIVERSITIES:\n{uni_list}"
    )
    # The scraped name is only a faint disambiguation hint — the résumé text is the
    # source of truth for the full name.
    hint_line = (f"(For reference only, the portal lists this candidate as "
                 f"\"{hint}\"; prefer the name written in the résumé if it differs.)\n\n"
                 if hint else "")
    user = f"Extract the fields from this résumé.\n\n{hint_line}--- RESUME TEXT ---\n{text}"

    resp = _chat(api_key, model, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], response_format={"type": "json_object"})
    raw = _message_from(resp, "extracted fields")

    # Models sometimes wrap JSON in ```json fences despite instructions — strip them.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        data = json.loads(cleaned)
    except ValueError as exc:
        raise SummaryError(f"OpenAI did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SummaryError("OpenAI returned JSON that was not an object.")
    return {
        "full_name": str(data.get("full_name") or "").strip(),
        "university": str(data.get("university") or "").strip(),
        "major": str(data.get("major") or "").strip(),
        "exp_total": _fmt_years(data.get("exp_total")),
        "exp_directly": _fmt_years(data.get("exp_directly")),
    }
