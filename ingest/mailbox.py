"""IMAP mailbox reader for portal alert emails.

Uses stdlib imaplib — no extra dependencies. Connects with a Gmail
app-specific password (never the account password).

Reads UNSEEN messages, hands raw (sender, subject, html, text) to the
parsers, and marks messages as seen so each alert is processed once.
"""
import email
import imaplib
import logging
import os
import re
from dataclasses import dataclass
from email.header import decode_header, make_header

logger = logging.getLogger(__name__)

# Max messages to pull in a single poll — safety valve against a huge backlog
MAX_PER_POLL = 60


@dataclass
class RawEmail:
    uid: str
    sender: str          # lowercase email address only, e.g. "no-reply@idealista.com"
    subject: str
    html: str
    text: str

    @property
    def body(self) -> str:
        """Prefer HTML, fall back to plain text."""
        return self.html or self.text


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_address(from_header: str) -> str:
    """'Idealista <no-reply@idealista.com>' -> 'no-reply@idealista.com'"""
    m = re.search(r"<([^>]+)>", from_header)
    addr = m.group(1) if m else from_header
    return addr.strip().lower()


def _walk_parts(msg) -> tuple[str, str]:
    """Return (html, text) from a possibly-multipart message."""
    html_parts, text_parts = [], []

    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        # Skip attachments
        if "attachment" in (part.get("Content-Disposition") or ""):
            continue

        ctype = part.get_content_type()
        if ctype not in ("text/html", "text/plain"):
            continue

        payload = part.get_payload(decode=True)
        if payload is None:
            continue

        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            decoded = payload.decode("utf-8", errors="replace")

        if ctype == "text/html":
            html_parts.append(decoded)
        else:
            text_parts.append(decoded)

    return "\n".join(html_parts), "\n".join(text_parts)


class Mailbox:
    """Read-only-ish IMAP client for a single alert inbox."""

    def __init__(self, config: dict):
        mail_cfg = config.get("email_ingest", {}) or {}
        self.host = mail_cfg.get("imap_host", "imap.gmail.com")
        self.port = int(mail_cfg.get("imap_port", 993))
        self.folder = mail_cfg.get("folder", "INBOX")

        self.user = os.environ.get("MAIL_USER", "").strip()
        self.password = os.environ.get("MAIL_APP_PASSWORD", "").replace(" ", "").strip()

        self.enabled = bool(self.user and self.password)
        if self.enabled:
            logger.info(f"Email ingest ENABLED (user={self.user}, host={self.host})")
        else:
            logger.warning(
                "Email ingest DISABLED — set MAIL_USER and MAIL_APP_PASSWORD env vars."
            )

    def fetch_unseen(self, mark_seen: bool = True) -> list[RawEmail]:
        """Fetch unseen messages. Returns [] on any connection problem."""
        if not self.enabled:
            return []

        try:
            conn = imaplib.IMAP4_SSL(self.host, self.port)
        except Exception as e:
            logger.error(f"IMAP connect failed: {e}")
            return []

        results: list[RawEmail] = []
        try:
            conn.login(self.user, self.password)
            conn.select(self.folder)

            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                logger.warning(f"IMAP search returned {status}")
                return []

            uids = data[0].split()
            if not uids:
                logger.info("Email ingest: no unseen messages")
                return []

            if len(uids) > MAX_PER_POLL:
                logger.warning(
                    f"Email ingest: {len(uids)} unseen, processing newest {MAX_PER_POLL}"
                )
                uids = uids[-MAX_PER_POLL:]

            # Peek so we control the \Seen flag ourselves
            fetch_cmd = "(BODY.PEEK[])" if not mark_seen else "(RFC822)"

            for uid in uids:
                try:
                    status, msg_data = conn.fetch(uid, fetch_cmd)
                    if status != "OK" or not msg_data or not msg_data[0]:
                        continue

                    msg = email.message_from_bytes(msg_data[0][1])
                    html, text = _walk_parts(msg)

                    results.append(
                        RawEmail(
                            uid=uid.decode(),
                            sender=_extract_address(_decode(msg.get("From"))),
                            subject=_decode(msg.get("Subject")),
                            html=html,
                            text=text,
                        )
                    )
                except Exception as e:
                    logger.error(f"Error reading message {uid}: {e}")

            logger.info(f"Email ingest: fetched {len(results)} messages")

        except imaplib.IMAP4.error as e:
            logger.error(f"IMAP error (check MAIL_APP_PASSWORD / IMAP enabled): {e}")
        except Exception as e:
            logger.error(f"Unexpected IMAP failure: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn.logout()
            except Exception:
                pass

        return results

    def check_connection(self) -> tuple[bool, str]:
        """Diagnostic helper: verify credentials without reading anything."""
        if not self.enabled:
            return False, "MAIL_USER / MAIL_APP_PASSWORD not set"
        try:
            conn = imaplib.IMAP4_SSL(self.host, self.port)
            conn.login(self.user, self.password)
            status, data = conn.select(self.folder)
            count = data[0].decode() if data and data[0] else "?"
            conn.logout()
            return True, f"OK — {count} messages in {self.folder}"
        except imaplib.IMAP4.error as e:
            return False, f"Login/select failed: {e}"
        except Exception as e:
            return False, f"Connection failed: {e}"
