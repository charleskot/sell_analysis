"""Gmail OAuth2 — token acquisition and refresh.

Deliberately dependency-free beyond `requests`: the google-auth stack pulls
in a lot for what is, in the end, two HTTP POSTs and a loopback redirect.

Scope is gmail.readonly. The bot can read messages and nothing else — it
cannot send, delete, or modify anything in the mailbox.

One-time flow (run `python main.py mail-auth` on the machine with a browser):
    1. Spin a loopback HTTP server on 127.0.0.1
    2. Open Google's consent screen
    3. Google redirects back with ?code=...
    4. Exchange the code for a refresh_token, store it in token file

Ongoing (every poll): refresh_token -> short-lived access_token.
"""
import json
import logging
import os
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

DEFAULT_TOKEN_PATH = "data/gmail_token.json"


class GmailAuthError(RuntimeError):
    pass


# ── Token storage ────────────────────────────────────────────────────────

def _token_path(config: dict) -> Path:
    cfg = config.get("email_ingest", {}) or {}
    return Path(cfg.get("token_path", DEFAULT_TOKEN_PATH))


def load_refresh_token(config: dict) -> str | None:
    """Env var wins over the token file (useful for containers)."""
    env = os.environ.get("GMAIL_REFRESH_TOKEN", "").strip()
    if env:
        return env

    path = _token_path(config)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("refresh_token")
    except Exception as e:
        logger.error(f"Could not read {path}: {e}")
        return None


def save_refresh_token(config: dict, refresh_token: str, email: str = "") -> Path:
    path = _token_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"refresh_token": refresh_token, "email": email}, indent=2))
    path.chmod(0o600)   # token is a credential — keep it owner-only
    return path


def get_client_credentials() -> tuple[str, str]:
    client_id = os.environ.get("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise GmailAuthError(
            "GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET not set. "
            "Create an OAuth client (Desktop app) in Google Cloud Console."
        )
    return client_id, client_secret


# ── Access token refresh ─────────────────────────────────────────────────

def get_access_token(config: dict) -> str:
    """Exchange the stored refresh token for a short-lived access token."""
    client_id, client_secret = get_client_credentials()
    refresh_token = load_refresh_token(config)
    if not refresh_token:
        raise GmailAuthError("No refresh token stored. Run: python main.py mail-auth")

    resp = requests.post(
        TOKEN_URI,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise GmailAuthError(
            f"Token refresh failed ({resp.status_code}): {resp.text[:300]}. "
            "If this says invalid_grant, re-run: python main.py mail-auth"
        )
    token = resp.json().get("access_token")
    if not token:
        raise GmailAuthError(f"No access_token in response: {resp.text[:200]}")
    return token


# ── One-time interactive authorisation ───────────────────────────────────

class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the ?code= Google sends to the loopback redirect."""

    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self):  # noqa: N802 (stdlib naming)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code = (params.get("code") or [None])[0]
        _CallbackHandler.state = (params.get("state") or [None])[0]
        _CallbackHandler.error = (params.get("error") or [None])[0]

        ok = _CallbackHandler.code and not _CallbackHandler.error
        body = (
            "<h2>✅ Listo</h2><p>Ya puedes cerrar esta pestaña y volver a la terminal.</p>"
            if ok else
            f"<h2>❌ Error</h2><p>{_CallbackHandler.error or 'sin código'}</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<html><body style='font-family:sans-serif'>{body}</body></html>".encode())

    def log_message(self, *args):
        pass  # keep the console clean


def run_auth_flow(config: dict, port: int = 8765) -> tuple[str, str]:
    """Interactive OAuth. Returns (refresh_token, email). Needs a browser."""
    client_id, client_secret = get_client_credentials()
    redirect_uri = f"http://127.0.0.1:{port}"
    state = secrets.token_urlsafe(16)

    _CallbackHandler.code = _CallbackHandler.state = _CallbackHandler.error = None

    try:
        server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    except OSError as e:
        raise GmailAuthError(f"Port {port} busy ({e}). Retry with --port 8766.") from e

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    auth_url = f"{AUTH_URI}?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",     # required to receive a refresh_token
        "prompt": "consent",          # force refresh_token even on re-auth
        "state": state,
    })

    print("\nAbre esta URL en el navegador si no se abre sola:\n")
    print(auth_url + "\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print("Esperando autorización…")
    thread.join(timeout=300)
    server.server_close()

    if _CallbackHandler.error:
        raise GmailAuthError(f"Authorisation denied: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise GmailAuthError("Timed out waiting for authorisation (5 min).")
    if _CallbackHandler.state != state:
        raise GmailAuthError("State mismatch — possible CSRF, aborting.")

    resp = requests.post(
        TOKEN_URI,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": _CallbackHandler.code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise GmailAuthError(f"Code exchange failed ({resp.status_code}): {resp.text[:300]}")

    payload = resp.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise GmailAuthError(
            "Google returned no refresh_token. Revoke access at "
            "myaccount.google.com/permissions and run mail-auth again."
        )

    # Identify which mailbox we just got access to
    email = ""
    try:
        prof = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {payload['access_token']}"},
            timeout=15,
        )
        if prof.status_code == 200:
            email = prof.json().get("emailAddress", "")
    except Exception:
        pass

    return refresh_token, email
