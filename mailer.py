"""Send the interview/exam email via Microsoft Graph using DELEGATED auth
(device-code flow).

One shared delegated app (the "Recruit" mailbox) is used for everything: sending the
Sent-Exam mail, creating the shortlist/interview/evaluation/offer drafts/events, and
detecting replies. The user signs in ONCE (via the web UI's "Sign in" button): they
open microsoft.com/devicelogin, enter a code, and log in. The token — together with a
refresh token — is cached to disk, so later operations are silent and go out AS the
signed-in user (/me/...).

To keep each teammate's work tidy in the shared mailbox, sent exams and drafts can be
filed into per-teammate mail folders (e.g. "Na_Sent_Exam", "Na_Drafts") via the
`sent_folder`/`folder` arguments below.

App registration needs DELEGATED Mail.ReadWrite + Mail.Send + Calendars.ReadWrite +
Files.ReadWrite + User.Read, and Authentication → "Allow public client flows" = Yes.
No client secret is used. .env: GRAPH_TENANT_ID, GRAPH_CLIENT_ID.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import time
from pathlib import Path
from urllib.parse import quote

import msal
import requests

from config import Config

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
# Delegated; offline_access added by MSAL. Tokens are requested PER OPERATION so a
# missing permission only breaks that feature (e.g. no Files.ReadWrite → OneDrive
# upload falls back to a local copy, but the mail draft still works).
#   Mail.ReadWrite — CREATE drafts (POST /me/messages) for the shortlist group email
#   Mail.Send      — the exam send (/me/sendMail)
#   Files.ReadWrite — upload shortlist résumés to OneDrive + create a share link
MAIL_SCOPES = ["Mail.ReadWrite", "Mail.Send", "User.Read"]
FILES_SCOPES = ["Files.ReadWrite", "User.Read"]
CALENDAR_SCOPES = ["Calendars.ReadWrite", "User.Read"]   # create the interview event
# Union, requested at sign-in so one consent covers everything the app is granted.
SCOPES = ["Mail.ReadWrite", "Mail.Send", "Files.ReadWrite", "Calendars.ReadWrite", "User.Read"]
_ROOT = Path(__file__).resolve().parent
CACHE_PATH = _ROOT / ".graph_token_cache.json"


class MailerError(RuntimeError):
    pass


class GraphMailer:
    """Talks to Graph as the single shared delegated account (the "Recruit" mailbox),
    signed in once via device code. Used for sending the exam, creating drafts/events,
    and detecting replies — all AS the signed-in user (/me/...)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        client_id, tenant_id = cfg.graph_client_id, cfg.graph_tenant_id
        self._cache_path = CACHE_PATH
        keys = {"GRAPH_TENANT_ID": tenant_id, "GRAPH_CLIENT_ID": client_id}
        missing = [k for k, v in keys.items() if not v]
        if missing:
            raise MailerError("Missing Graph config in .env: " + ", ".join(missing))
        self._cache = msal.SerializableTokenCache()
        if self._cache_path.exists():
            try:
                self._cache.deserialize(self._cache_path.read_text(encoding="utf-8"))
            except OSError:
                pass
        self._app = msal.PublicClientApplication(
            client_id=client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            token_cache=self._cache,
        )

    def _save_cache(self) -> None:
        if self._cache.has_state_changed:
            try:
                self._cache_path.write_text(self._cache.serialize(), encoding="utf-8")
            except OSError as exc:  # noqa: BLE001
                log.warning("Could not save Graph token cache: %s", exc)

    # -- sign-in (device-code) -------------------------------------------------
    def signed_in_account(self) -> str | None:
        """Username of the cached signed-in account, or None."""
        accounts = self._app.get_accounts()
        return accounts[0]["username"] if accounts else None

    def begin_device_login(self) -> dict:
        """Start a device-code flow; returns the flow (user_code, verification_uri…)."""
        flow = self._app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise MailerError("Could not start sign-in: "
                              + str(flow.get("error_description") or flow))
        return flow

    def complete_device_login(self, flow: dict) -> str:
        """Block until the user finishes signing in; cache the token. Returns the
        account username. Raises MailerError on failure/timeout."""
        result = self._app.acquire_token_by_device_flow(flow)
        self._save_cache()
        if "access_token" not in result:
            raise MailerError(f"Sign-in failed: {result.get('error')}: "
                              f"{result.get('error_description')}")
        return self.signed_in_account() or "unknown"

    def _silent_token(self, scopes: list[str] | None = None) -> str:
        accounts = self._app.get_accounts()
        if not accounts:
            raise MailerError("Not signed in. Open Config Email Template and click "
                              "'Sign in to email'.")
        result = self._app.acquire_token_silent(scopes or SCOPES, account=accounts[0])
        self._save_cache()
        if not result or "access_token" not in result:
            raise MailerError("Email sign-in expired (or this permission isn't granted "
                              "yet). Open Config Email Template and sign in again.")
        return result["access_token"]

    # -- sending ---------------------------------------------------------------
    @staticmethod
    def _attachments(paths: list[str] | None) -> list[dict]:
        out = []
        for path in paths or []:
            if not path:
                continue
            p = Path(path)
            if not p.is_file():
                raise MailerError(f"Attachment file not found: {path}")
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            out.append({
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": p.name,
                "contentType": ctype,
                "contentBytes": base64.b64encode(p.read_bytes()).decode("ascii"),
            })
        return out

    # -- mail folders (per-teammate organisation of the shared mailbox) ---------
    def _ensure_mail_folder(self, name: str, token: str) -> str:
        """Return the id of the top-level mail folder `name`, creating it if absent.
        Used to keep each teammate's sent exams/drafts in their own folder inside the
        shared mailbox. Raises MailerError on a Graph error."""
        hdr = {"Authorization": f"Bearer {token}"}
        safe = name.replace("'", "''")                 # OData string-literal escaping
        resp = requests.get(
            f"{GRAPH}/me/mailFolders",
            headers=hdr,
            params={"$filter": f"displayName eq '{safe}'", "$select": "id", "$top": "1"},
            timeout=30)
        if resp.status_code == 200:
            items = resp.json().get("value", [])
            if items:
                return items[0]["id"]
        elif resp.status_code != 404:
            raise MailerError(f"Graph mailFolder lookup failed ({resp.status_code}): {resp.text[:200]}")
        # Not found → create it.
        resp = requests.post(
            f"{GRAPH}/me/mailFolders",
            headers={**hdr, "Content-Type": "application/json"},
            json={"displayName": name}, timeout=30)
        if resp.status_code not in (200, 201):
            raise MailerError(f"Graph create-folder failed ({resp.status_code}): {resp.text[:200]}")
        return resp.json()["id"]

    def _move_sent_to_folder(self, subject: str, to_email: str, folder_name: str,
                             token: str) -> bool:
        """Best-effort: move the just-sent message from Sent Items into `folder_name`
        (created if needed). Matches the newest Sent-Items message by subject + the
        recipient. Returns True if moved; False if it couldn't be located (the mail is
        already sent and simply stays in Sent Items). Never raises."""
        try:
            fid = self._ensure_mail_folder(folder_name, token)
            hdr = {"Authorization": f"Bearer {token}"}
            safe = (subject or "").replace("'", "''")
            target = (to_email or "").strip().lower()
            # /me/sendMail returns 202 and writes to Sent Items ASYNCHRONOUSLY, so the
            # message often isn't there the instant send() returns. Poll a few times.
            # No $orderby: Graph rejects orderby combined with a $filter here
            # ("InefficientFilter"); pick the newest match client-side.
            msg_id = None
            for attempt in range(8):
                resp = requests.get(
                    f"{GRAPH}/me/mailFolders/sentitems/messages",
                    headers=hdr,
                    params={"$filter": f"subject eq '{safe}'",
                            "$select": "id,toRecipients,sentDateTime",
                            "$top": "25"},
                    timeout=30)
                if resp.status_code != 200:
                    log.warning("Sent-Items lookup failed (%s): %s", resp.status_code, resp.text[:200])
                    return False
                candidates = []
                for m in resp.json().get("value", []):
                    addrs = [(r.get("emailAddress", {}).get("address") or "").lower()
                             for r in m.get("toRecipients", [])]
                    if not target or target in addrs:
                        candidates.append(m)
                if candidates:
                    msg_id = max(candidates, key=lambda m: m.get("sentDateTime", "")).get("id")
                    break
                time.sleep(1.0)            # give Sent Items a moment to populate
            if not msg_id:
                log.warning("Sent message not found in Sent Items yet; left it there "
                            "(folder=%s, subject=%r)", folder_name, subject)
                return False
            resp = requests.post(
                f"{GRAPH}/me/messages/{msg_id}/move",
                headers={**hdr, "Content-Type": "application/json"},
                json={"destinationId": fid}, timeout=30)
            if resp.status_code not in (200, 201):
                log.warning("Move to %s failed (%s): %s", folder_name, resp.status_code, resp.text[:200])
                return False
            return True
        except (MailerError, requests.RequestException) as exc:  # noqa: BLE001
            log.warning("Could not file sent mail into %s: %s", folder_name, exc)
            return False

    # -- sending ---------------------------------------------------------------
    def send(self, to_email: str, subject: str, body: str,
             attachment_paths: list[str] | None = None, is_html: bool = False,
             cc_emails: list[str] | None = None, sent_folder: str | None = None) -> None:
        """Send one email AS the signed-in user (/me/sendMail) with a text/HTML body
        and optional file attachments. Optional `cc_emails` are added as CC recipients.
        If `sent_folder` is given, the sent message is then moved (best-effort) from
        Sent Items into that mail folder (created if needed). Raises MailerError only
        on send failure — the folder move never blocks a successful send."""
        if not to_email:
            raise MailerError("Candidate has no email address.")
        token = self._silent_token(MAIL_SCOPES)
        message: dict = {
            "subject": subject,
            "body": {"contentType": "HTML" if is_html else "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        }
        cc = [{"emailAddress": {"address": a}} for a in (cc_emails or []) if a]
        if cc:
            message["ccRecipients"] = cc
        attachments = self._attachments(attachment_paths)
        if attachments:
            message["attachments"] = attachments

        resp = requests.post(
            f"{GRAPH}/me/sendMail",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"message": message, "saveToSentItems": True},
            timeout=30,
        )
        if resp.status_code not in (200, 202):
            raise MailerError(f"Graph sendMail failed ({resp.status_code}): {resp.text[:300]}")
        log.info("Email sent to %s", to_email)
        if sent_folder:
            if self._move_sent_to_folder(subject, to_email, sent_folder, token):
                log.info("Filed sent mail into %s", sent_folder)

    def create_draft(self, subject: str, body: str,
                     to_recipients: list[str] | None = None,
                     is_html: bool = False,
                     attachment_paths: list[str] | None = None,
                     folder: str | None = None) -> None:
        """Create an email DRAFT in the signed-in user's mailbox so HR can review/edit
        and send it from Outlook. When `folder` is given the draft is created inside
        that mail folder (created if needed: POST /me/mailFolders/{id}/messages),
        otherwise in the default Drafts (POST /me/messages). Optional file attachments
        are embedded inline (fine for the small evaluation form ~200 KB; Graph allows
        up to ~3 MB this way). Raises MailerError on failure."""
        token = self._silent_token(MAIL_SCOPES)
        message: dict = {
            "subject": subject,
            "body": {"contentType": "HTML" if is_html else "Text", "content": body},
        }
        recips = [{"emailAddress": {"address": a}} for a in (to_recipients or []) if a]
        if recips:
            message["toRecipients"] = recips
        attachments = self._attachments(attachment_paths)
        if attachments:
            message["attachments"] = attachments
        if folder:
            fid = self._ensure_mail_folder(folder, token)
            url = f"{GRAPH}/me/mailFolders/{fid}/messages"
        else:
            url = f"{GRAPH}/me/messages"
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json=message, timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise MailerError(f"Graph create-draft failed ({resp.status_code}): {resp.text[:300]}")
        log.info("Draft created in mailbox (%s)%s", self.signed_in_account() or "unknown",
                 f" → {folder}" if folder else "")

    # -- calendar (interview event draft) -------------------------------------
    def create_event(self, subject: str, body_html: str, start: str, end: str,
                     timezone: str = "SE Asia Standard Time", location: str = "",
                     online: bool = True) -> dict:
        """Create a calendar event on the signed-in user's calendar (a "draft" —
        no attendees added, so no invitations are sent). HR opens it in Outlook to
        set the real time, add attendees, and send. `start`/`end` are local
        ISO8601 (no tz suffix), interpreted in `timezone`. Returns the event JSON
        (incl. webLink). Needs Calendars.ReadWrite."""
        token = self._silent_token(CALENDAR_SCOPES)
        ev: dict = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "start": {"dateTime": start, "timeZone": timezone},
            "end": {"dateTime": end, "timeZone": timezone},
        }
        if location:
            ev["location"] = {"displayName": location}
        if online:
            ev["isOnlineMeeting"] = True
            ev["onlineMeetingProvider"] = "teamsForBusiness"
        resp = requests.post(
            f"{GRAPH}/me/events",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json=ev, timeout=30)
        if resp.status_code not in (200, 201):
            raise MailerError(f"Graph create-event failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    # -- OneDrive (shortlist résumé folder + share link) -----------------------
    @staticmethod
    def _drive_path(rel_path: str) -> str:
        """URL-encode an OneDrive-relative path for the /me/drive/root: addressing
        (keeps '/' separators)."""
        return quote(rel_path.strip("/"), safe="/")

    # -- exam reply detection + attachment download ---------------------------
    def check_reply(self, from_email: str, since_iso_utc: str) -> dict | None:
        """Find the newest message in the signed-in mailbox FROM `from_email`
        received at/after `since_iso_utc` (UTC ISO8601, e.g. 2026-06-08T03:21:00Z).
        Returns {"id", "subject", "at" (UTC iso), "webLink"} or None. Mail.Read
        (granted via Mail.ReadWrite) is sufficient — no new permission."""
        if not from_email:
            return None
        token = self._silent_token(MAIL_SCOPES)
        safe = from_email.replace("'", "''")        # OData string-literal escaping
        params = {
            "$filter": (f"from/emailAddress/address eq '{safe}' "
                        f"and receivedDateTime ge {since_iso_utc}"),
            "$select": "id,subject,receivedDateTime,webLink,hasAttachments",
            "$top": "15",
            # No $orderby: Graph rejects orderby on receivedDateTime combined with a
            # filter on `from`. Pick the newest client-side.
        }
        resp = requests.get(f"{GRAPH}/me/messages",
                            headers={"Authorization": f"Bearer {token}"},
                            params=params, timeout=30)
        if resp.status_code != 200:
            raise MailerError(f"Graph reply lookup failed ({resp.status_code}): {resp.text[:300]}")
        items = resp.json().get("value", [])
        if not items:
            return None
        latest = max(items, key=lambda m: m.get("receivedDateTime", ""))
        return {"id": latest.get("id"),
                "subject": latest.get("subject") or "(no subject)",
                "at": latest.get("receivedDateTime"),
                "webLink": latest.get("webLink"),
                "hasAttachments": bool(latest.get("hasAttachments"))}

    def download_attachments(self, message_id: str) -> list[dict]:
        """Return the file attachments of a message as [{"name", "bytes"}].
        Skips inline images and non-file (item) attachments."""
        if not message_id:
            return []
        token = self._silent_token(MAIL_SCOPES)
        # No $select: contentBytes lives on the derived fileAttachment type and can't
        # be selected on the base attachment collection; the default response includes it.
        resp = requests.get(
            f"{GRAPH}/me/messages/{message_id}/attachments",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60)
        if resp.status_code != 200:
            raise MailerError(f"Graph attachment fetch failed ({resp.status_code}): {resp.text[:300]}")
        out = []
        for a in resp.json().get("value", []):
            if a.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            if a.get("isInline"):
                continue
            content = a.get("contentBytes")
            if not content:
                continue
            out.append({"name": a.get("name") or "attachment",
                        "bytes": base64.b64decode(content)})
        return out

    def path_exists(self, drive_rel_path: str) -> bool:
        """True if an item exists at the given OneDrive-relative path."""
        token = self._silent_token(FILES_SCOPES)
        url = f"{GRAPH}/me/drive/root:/{self._drive_path(drive_rel_path)}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        raise MailerError(f"OneDrive lookup failed ({resp.status_code}): {resp.text[:200]}")

    def list_folder(self, drive_rel_path: str) -> list[dict]:
        """List child items of a OneDrive folder. Returns each item's JSON (name,
        folder, …); [] if the folder doesn't exist. Used to locate a candidate's
        shortlist subfolder when building the offer-email share link."""
        token = self._silent_token(FILES_SCOPES)
        url = (f"{GRAPH}/me/drive/root:/{self._drive_path(drive_rel_path)}:/children"
               "?$select=name,folder&$top=200")
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise MailerError(f"OneDrive list failed ({resp.status_code}): {resp.text[:200]}")
        return resp.json().get("value", [])

    def upload_file(self, drive_rel_path: str, content: bytes) -> dict:
        """Upload bytes to the signed-in user's OneDrive at `drive_rel_path`
        (parent folders auto-created). Simple upload (fine for résumé-sized files)."""
        token = self._silent_token(FILES_SCOPES)
        url = f"{GRAPH}/me/drive/root:/{self._drive_path(drive_rel_path)}:/content"
        resp = requests.put(
            url, headers={"Authorization": f"Bearer {token}",
                          "Content-Type": "application/octet-stream"},
            data=content, timeout=120)
        if resp.status_code not in (200, 201):
            raise MailerError(f"OneDrive upload failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def create_share_link(self, drive_rel_folder: str,
                          link_type: str = "view", scope: str = "organization") -> str:
        """Create (or fetch) a sharing link for a OneDrive folder; returns its webUrl.
        scope='organization' = anyone in the company with the link."""
        token = self._silent_token(FILES_SCOPES)
        url = f"{GRAPH}/me/drive/root:/{self._drive_path(drive_rel_folder)}:/createLink"
        resp = requests.post(
            url, headers={"Authorization": f"Bearer {token}",
                          "Content-Type": "application/json"},
            json={"type": link_type, "scope": scope}, timeout=30)
        if resp.status_code not in (200, 201):
            raise MailerError(f"OneDrive create-link failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json().get("link", {}).get("webUrl", "")
