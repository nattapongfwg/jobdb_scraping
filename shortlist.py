"""Build a per-shortlist folder of candidate résumés.

When HR moves selected candidates to Shortlist, we create a folder named
`<job_title>_dd_mm_yyyy` under the OneDrive shortlist directory (cfg.shortlist_dir)
and copy each selected candidate's résumé into it, renamed to just the candidate's
name (the internal UUID suffix is dropped). Because the folder lives in OneDrive it
can be shared; its NAME goes into the email's "Link document:" line.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
# Windows-illegal filename chars (keep Thai and other unicode letters).
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_folder(name: str) -> str:
    """Folder-safe: drop illegal chars, spaces -> underscore, collapse repeats."""
    name = _ILLEGAL.sub("", name or "").strip()
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"_+", "_", name).strip("._")
    return name or "Shortlist"


def _safe_file(name: str) -> str:
    """File-safe: drop illegal chars, keep spaces, trim trailing dots/spaces."""
    name = _ILLEGAL.sub("", name or "").strip()
    name = re.sub(r"\s+", " ", name).strip(". ")
    return name or "candidate"


def resolve_resume(p: str | None) -> Path | None:
    """Resolve a stored résumé path to an existing file, or None."""
    if not p:
        return None
    path = Path(p)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path if path.is_file() else None


def folder_name_for(job_title: str, today: datetime | None = None,
                    prefix: str = "") -> str:
    """Base name `<job_title>_dd_mm_yyyy`, e.g. 'Software_Implementer_08_06_2026'.
    When `prefix` (the teammate tag, e.g. 'Na') is set it is appended as a suffix
    so each teammate's shortlist folders are distinct: `..._dd_mm_yyyy_Na`. Blank
    prefix keeps the plain name unchanged."""
    today = today or datetime.now()
    base = f"{_safe_folder(job_title)}_{today:%d_%m_%Y}"
    # Strip illegal chars from the tag, but unlike _safe_folder don't fall back to
    # "Shortlist" — a blank/empty prefix must leave the name untagged.
    tag = re.sub(r"_+", "_", _ILLEGAL.sub("", prefix or "").strip().replace(" ", "_")).strip("._")
    return f"{base}_{tag}" if tag else base


def unique_folder_name(base: str, exists) -> str:
    """`base` if not exists(base), else `base_1`, `base_2`, … (first free name).
    `exists` is a callable taking a candidate name and returning True if taken."""
    if not exists(base):
        return base
    i = 1
    while exists(f"{base}_{i}"):
        i += 1
    return f"{base}_{i}"


def candidate_folder(name: str, used: set) -> str:
    """Folder-name for one candidate (their name_edit, illegal chars stripped,
    spaces kept), de-duped against `used` (mutated): 'John', 'John (2)', …"""
    base = _safe_file(name)
    sub, i = base, 2
    while sub.lower() in used:
        sub = f"{base} ({i})"
        i += 1
    used.add(sub.lower())
    return sub


def reply_folder_name(candidate_name: str, email_name: str = "") -> str:
    """Folder name for a candidate's Email_Reply_Exam store:
    `<firstname>_<lastname>_<email_name>` — the candidate's name with spaces turned
    into underscores, suffixed with the sending mailbox's local-part (`email_name`,
    e.g. 'nattapong_yuw'). When `email_name` is empty, just the underscored name."""
    base = _safe_folder(candidate_name)
    email_name = (email_name or "").strip()
    return f"{base}_{_safe_folder(email_name)}" if email_name else base


def build_reply_folder(base_dir: str | Path, candidate_name: str,
                       resume_path: str | None, attachments: list[dict],
                       email_name: str = "") -> tuple[str, int]:
    """Create <base_dir>/<firstname_lastname_emailname>/ and save the candidate's
    résumé plus the files they replied with. `attachments`: list of {name, bytes}.
    `email_name` is the sender mailbox's local-part (see reply_folder_name). Returns
    (folder_path, saved_count). Used for the Email_Reply_Exam store."""
    name = reply_folder_name(candidate_name, email_name)
    folder = Path(base_dir) / name
    folder.mkdir(parents=True, exist_ok=True)
    saved, used = 0, set()

    src = resolve_resume(resume_path)
    if src:
        dest = folder / f"{name}{src.suffix or '.pdf'}"
        used.add(dest.name.lower())
        try:
            shutil.copy2(src, dest)
            saved += 1
        except OSError as exc:
            log.warning("Reply folder résumé copy failed %s: %s", dest, exc)

    for a in attachments or []:
        fname = _safe_file(a.get("name") or "attachment") or "attachment"
        final, i = fname, 2
        while final.lower() in used:                 # de-dupe, keep extension
            stem, dot, ext = fname.rpartition(".")
            final = f"{stem} ({i}).{ext}" if dot else f"{fname} ({i})"
            i += 1
        used.add(final.lower())
        try:
            (folder / final).write_bytes(a.get("bytes") or b"")
            saved += 1
        except OSError as exc:
            log.warning("Reply attachment save failed %s: %s", final, exc)
    return str(folder), saved


def subfolder_name(name: str) -> str:
    """Folder-safe candidate name (illegal chars stripped, spaces kept)."""
    return _safe_file(name) or "candidate"


def _norm_folder(s: str) -> str:
    """Loose key for matching reply-folder names: illegal chars dropped, spaces and
    underscores unified, lower-cased. So 'Panchanok Pirom', 'Panchanok_Pirom' and
    'Panchanok   Pirom' all compare equal."""
    s = _ILLEGAL.sub("", s or "").strip().lower()
    return re.sub(r"[ _]+", "_", s).strip("_")


def candidate_dir(reply_base: str | Path, name: str, email_name: str = "") -> Path | None:
    """The candidate's Email_Reply_Exam folder, or None if it doesn't exist.

    Tolerant of how the folder was named when the reply was downloaded: matches
    space-vs-underscore differences and whether the teammate prefix suffix is
    present. This matters because legacy folders were saved as 'Firstname Lastname'
    (spaces, no prefix) while current code writes 'Firstname_Lastname_<prefix>' —
    reconstructing only the current name missed the older folders and silently fell
    back to a résumé-only shortlist."""
    reply_base = Path(reply_base)
    # Fast path: exact names current or prefix-less code would have written.
    for cand in (reply_folder_name(name, email_name), reply_folder_name(name, "")):
        d = reply_base / cand
        if d.is_dir():
            return d
    # Tolerant scan: match ignoring space/underscore, with or without our suffix.
    if not reply_base.is_dir():
        return None
    target = _norm_folder(name)
    pfx = _norm_folder(email_name)
    for d in reply_base.iterdir():
        if not d.is_dir():
            continue
        nd = _norm_folder(d.name)
        if nd == target or (pfx and nd == f"{target}_{pfx}"):
            return d
    return None


def _rmtree_retry(func, p, _exc):
    """rmtree error hook: clear a read-only flag (common on Windows) and retry the
    failed op once. Shared by both the onexc (3.12+) and onerror (<3.12) signatures."""
    try:
        os.chmod(p, stat.S_IWRITE)
        func(p)
    except OSError:
        pass   # leave it; the caller's retry loop / final rmdir handles the rest


def read_bytes_retry(path: str | Path, attempts: int = 5, delay: float = 0.5) -> bytes:
    """Read a file's bytes, retrying to ride out a OneDrive online-only placeholder
    that must hydrate (download on first access) or a file briefly locked by the
    sync client. Raises the last OSError if every attempt fails.

    Without this, a single OSError mid-upload aborts the shortlist batch after only
    the first file(s) reached the cloud, leaving an incomplete Shortlists folder."""
    path = Path(path)
    last: OSError | None = None
    for attempt in range(attempts):
        try:
            return path.read_bytes()
        except OSError as exc:
            last = exc
            log.warning("read %s failed (attempt %d/%d): %s", path, attempt + 1, attempts, exc)
            time.sleep(delay)
    assert last is not None
    raise last


def remove_dir(path: str | Path) -> bool:
    """Delete a directory tree. Used after a cloud upload = 'move'. Returns True
    once the tree is gone.

    Hardened for Windows + OneDrive: a folder that was just synced is often
    briefly locked by the sync client, so rmtree deletes the files but then fails
    to remove the now-empty directory — leaving an empty folder behind. We clear
    read-only flags, retry a few times to ride out the transient lock, and finish
    with a bare rmdir for the emptied-but-locked case."""
    path = Path(path)
    # rmtree's error hook was renamed onerror -> onexc in 3.12 (onerror removed in 3.14).
    hook = {"onexc": _rmtree_retry} if sys.version_info >= (3, 12) else {"onerror": _rmtree_retry}
    for attempt in range(5):
        try:
            shutil.rmtree(path, **hook)
        except OSError as exc:
            log.warning("rmtree %s failed (attempt %d/5): %s", path, attempt + 1, exc)
        if not path.exists():
            return True
        time.sleep(0.4)
    # The tree may now be empty but the directory itself still locked — try once more.
    try:
        os.rmdir(path)
    except OSError:
        pass
    if not path.exists():
        return True
    log.warning("Could not remove %s after retries; leaving it in place.", path)
    return False


def move_to_shortlist(shortlist_base: str | Path, reply_base: str | Path,
                      job_title: str, candidates: list[dict],
                      email_name: str = "",
                      today: datetime | None = None) -> tuple[str, str, int]:
    """Create a fresh Shortlists/<job_title>_dd_mm_yyyy[_N] folder and MOVE each
    selected candidate's Email_Reply_Exam/<firstname_lastname_emailname> folder
    (résumé + reply files) into it, renamed to the candidate's plain name. If a
    candidate has no reply folder, fall back to copying just their résumé.
    `email_name` must match what build_reply_folder used. `candidates`: list of
    {name, resume_path}. Returns (folder_name, folder_path, moved)."""
    sl_base, rp_base = Path(shortlist_base), Path(reply_base)
    folder_name = unique_folder_name(folder_name_for(job_title, today, email_name),
                                     lambda n: (sl_base / n).exists())
    top = sl_base / folder_name
    top.mkdir(parents=True, exist_ok=True)

    moved, used = 0, set()
    for c in candidates:
        name = _safe_file(c.get("name") or "") or "candidate"
        dest_name, i = name, 2
        while dest_name.lower() in used:
            dest_name = f"{name} ({i})"
            i += 1
        used.add(dest_name.lower())
        dest = top / dest_name
        # The Email_Reply_Exam folder built at send time (firstname_lastname_emailname).
        srcfolder = rp_base / reply_folder_name(c.get("name") or "", email_name)
        try:
            if srcfolder.is_dir():
                shutil.move(str(srcfolder), str(dest))   # whole folder (résumé + reply files)
                moved += 1
            else:
                # No reply folder (exam not sent via app, or folder missing) → résumé only.
                dest.mkdir(parents=True, exist_ok=True)
                src = resolve_resume(c.get("resume_path"))
                if src:
                    shutil.copy2(src, dest / f"{dest_name}{src.suffix or '.pdf'}")
                moved += 1
        except OSError as exc:
            log.warning("Could not move/copy shortlist folder for %s: %s", name, exc)
    return folder_name, str(top), moved


def build_shortlist_folder(base_dir: str | Path, job_title: str,
                           candidates: list[dict],
                           today: datetime | None = None) -> tuple[str, str, int]:
    """Local fallback. Create a fresh `<job_title>_dd_mm_yyyy` (suffixed _1, _2… if
    that name already exists today) under base_dir; inside it a subfolder per
    candidate (their name) holding their résumé. `candidates`: list of
    {name, resume_path}. Returns (folder_name, folder_path, copied_count)."""
    base_path = Path(base_dir)
    folder_name = unique_folder_name(folder_name_for(job_title, today),
                                     lambda n: (base_path / n).exists())
    top = base_path / folder_name
    top.mkdir(parents=True, exist_ok=True)

    copied, used = 0, set()
    for c in candidates:
        src = resolve_resume(c.get("resume_path"))
        if not src:
            continue
        sub = candidate_folder(c.get("name") or src.stem, used)
        cdir = top / sub
        cdir.mkdir(parents=True, exist_ok=True)
        dest = cdir / f"{sub}{src.suffix or '.pdf'}"
        try:
            shutil.copy2(src, dest)
            copied += 1
        except OSError as exc:
            log.warning("Could not copy résumé %s -> %s: %s", src, dest, exc)
    return folder_name, str(top), copied
