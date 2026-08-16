"""Read portal alert emails through the Gmail REST API (read-only scope).

Why the REST API instead of IMAP: Google is progressively disabling
app-specific passwords, which IMAP needs. OAuth with gmail.readonly always
works and is strictly less privileged — the bot cannot send, delete or
modify anything.

Because the scope is read-only we cannot mark messages as read. Instead we
remember the newest processed message timestamp in the app state table and
query `after:` that. A small overlap window is intentional: re-reading a
message is harmless (listings dedupe by id downstream), skipping one is not.
"""
import base64
import logging
import time

import requests

from ingest.gmail_oauth import GmailAuthError, get_access_token
from ingest.mailbox import RawEmail, _decode, _extract_address, _walk_parts

logger = logging.getLogger(__name__)

API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
STATE_KEY = "gmail_last_internal_date_ms"

MAX_PER_POLL = 60
OVERLAP_SECONDS = 120       # re-scan window, guards against clock skew
FIRST_RUN_LOOKBACK_S = 3600  # on a fresh install only look 1h back, not forever


class MailboxUnavailable(RuntimeError):
    """The mailbox could not be read at all.

    Distinct from "no new mail", and the distinction is the whole point: an
    expired token returned an empty list, which the caller could not tell
    from a quiet Saturday. The mailbox stayed dead for forty-four hours and
    nothing said so, while the bot reported itself healthy.
    """


class GmailReader:
    """Fetches unprocessed portal alert emails."""

    # Portal alerts are bulk mail and land in Spam often enough that ignoring
    # the folder loses whole portals silently: Idealista's alerts were all
    # filtered there while the inbox looked simply empty. Trash stays excluded,
    # since deleting a message is a deliberate act.
    DEFAULT_QUERY = "in:anywhere -in:trash"

    def __init__(self, config: dict):
        self.config = config
        cfg = config.get("email_ingest", {}) or {}
        self.query = (cfg.get("query") or self.DEFAULT_QUERY).strip()
        self.enabled = self._check_enabled()

    def _check_enabled(self) -> bool:
        import os
        from ingest.gmail_oauth import load_refresh_token

        has_client = bool(os.environ.get("GMAIL_CLIENT_ID") and os.environ.get("GMAIL_CLIENT_SECRET"))
        has_token = bool(load_refresh_token(self.config))

        if has_client and has_token:
            logger.info("Gmail ingest ENABLED (OAuth, read-only scope)")
            return True

        logger.warning(
            f"Gmail ingest DISABLED — client_credentials={has_client}, refresh_token={has_token}. "
            "Run: python main.py mail-auth"
        )
        return False

    # ── State ────────────────────────────────────────────────────────────

    def _get_last_ts_ms(self) -> int:
        from models.db import get_telegram_state
        raw = get_telegram_state(STATE_KEY, "")
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
        return int((time.time() - FIRST_RUN_LOOKBACK_S) * 1000)

    def _set_last_ts_ms(self, ts_ms: int) -> None:
        from models.db import set_telegram_state
        set_telegram_state(STATE_KEY, str(ts_ms))

    def account_address(self) -> str | None:
        """Which mailbox the refresh token actually opens.

        Nothing in the repo records it — the token is the whole credential —
        so the only way to be sure which account the bot is reading is to
        ask Google.
        """
        if not self.enabled:
            return None
        try:
            token = get_access_token(self.config)
            resp = requests.get(
                f"{API_BASE}/profile",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.error(f"Gmail profile failed ({resp.status_code})")
                return None
            return resp.json().get("emailAddress")
        except (GmailAuthError, requests.RequestException) as e:
            logger.error(f"Gmail profile failed: {e}")
            return None

    # ── API calls ────────────────────────────────────────────────────────

    def _list_message_ids(self, token: str, after_epoch_s: int) -> list[str]:
        query = f"after:{after_epoch_s}"
        if self.query:
            query = f"{query} {self.query}"

        resp = requests.get(
            f"{API_BASE}/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "maxResults": MAX_PER_POLL},
            timeout=30,
        )
        if resp.status_code != 200:
            raise MailboxUnavailable(
                f"Gmail respondió {resp.status_code}: {resp.text[:120]}"
            )
        return [m["id"] for m in resp.json().get("messages", [])]

    def _get_message(self, token: str, msg_id: str) -> tuple[RawEmail, int] | None:
        """Returns (email, internalDate_ms) or None."""
        resp = requests.get(
            f"{API_BASE}/messages/{msg_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "raw"},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.error(f"Gmail get {msg_id} failed ({resp.status_code})")
            return None

        payload = resp.json()
        raw_b64 = payload.get("raw")
        if not raw_b64:
            return None

        import email as email_mod

        msg = email_mod.message_from_bytes(base64.urlsafe_b64decode(raw_b64))
        html, text = _walk_parts(msg)

        return (
            RawEmail(
                uid=msg_id,
                sender=_extract_address(_decode(msg.get("From"))),
                subject=_decode(msg.get("Subject")),
                html=html,
                text=text,
            ),
            int(payload.get("internalDate", 0)),
        )

    # ── Public API ───────────────────────────────────────────────────────

    def fetch_new(self) -> list[RawEmail]:
        """Messages newer than the last processed one.

        Raises MailboxUnavailable when the mailbox cannot be read. It used to
        return [] for that too, which made a dead token look exactly like a
        quiet mailbox — and that is how forty-four hours passed with the bot
        reporting itself healthy and no listing ever reaching the user.
        """
        if not self.enabled:
            return []

        try:
            token = get_access_token(self.config)
        except GmailAuthError as e:
            raise MailboxUnavailable(f"no pude autenticarme: {e}") from e

        last_ts_ms = self._get_last_ts_ms()
        after_s = max(0, int(last_ts_ms / 1000) - OVERLAP_SECONDS)

        try:
            msg_ids = self._list_message_ids(token, after_s)
        except MailboxUnavailable:
            raise
        except Exception as e:
            raise MailboxUnavailable(f"fallo listando mensajes: {e}") from e

        if not msg_ids:
            logger.info("Gmail ingest: no new messages")
            return []

        emails: list[RawEmail] = []
        newest_ts = last_ts_ms

        for msg_id in msg_ids:
            try:
                result = self._get_message(token, msg_id)
            except Exception as e:
                logger.error(f"Gmail get error for {msg_id}: {e}")
                continue
            if not result:
                continue

            raw_email, internal_ts = result
            emails.append(raw_email)
            newest_ts = max(newest_ts, internal_ts)

        if newest_ts > last_ts_ms:
            self._set_last_ts_ms(newest_ts)

        logger.info(f"Gmail ingest: fetched {len(emails)} messages")
        return emails

    def check_connection(self) -> tuple[bool, str]:
        """Diagnostic: verify OAuth works, without processing anything."""
        try:
            token = get_access_token(self.config)
        except GmailAuthError as e:
            return False, str(e)

        try:
            resp = requests.get(
                f"{API_BASE}/profile",
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
        except Exception as e:
            return False, f"Network error: {e}"

        if resp.status_code != 200:
            return False, f"Profile call failed ({resp.status_code}): {resp.text[:200]}"

        data = resp.json()
        return True, f"OK — {data.get('emailAddress')} ({data.get('messagesTotal')} mensajes)"
