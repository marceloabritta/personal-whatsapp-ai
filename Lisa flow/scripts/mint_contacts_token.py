"""Mint the CONTACTS refresh token — a second token, separate from the calendar one.

Why separate: the live GOOGLE_REFRESH_TOKEN is consumed by BOTH the lisa and mary services, and
google-auth only *warns* when a granted scope is missing rather than failing. So re-minting that one
shared variable to add contacts scope risks two production calendars at once, and a scope that
quietly failed to be granted would surface later as a generic error on "book lunch Friday". This
script never touches the calendar token. It asks for contacts scope only, and it verifies the
result against the live API before printing anything.

Run it on your laptop (it opens a browser), not on the droplet:

    cd "Lisa flow"
    .venv/bin/python scripts/mint_contacts_token.py

It reads GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET from the environment or from ./.env, and you can
pass them explicitly with --client-id / --client-secret.

Stdlib only — a loopback OAuth flow with PKCE. No new dependency for a one-time job.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
# Contacts ONLY. The calendar token is a different variable and is not reissued here.
SCOPE = "https://www.googleapis.com/auth/contacts"


def _read_dotenv(path: str = ".env") -> dict:
    out: dict = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Receives Google's redirect and hands the code back to the main thread."""

    code: str | None = None
    error: str | None = None
    state: str = ""

    def do_GET(self) -> None:  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("state", [""])[0] != type(self).state:
            type(self).error = "state mismatch (possible CSRF); nothing was minted"
        elif "error" in q:
            type(self).error = q["error"][0]
        else:
            type(self).code = q.get("code", [None])[0]
        ok = type(self).code is not None
        body = (
            "<html><body style='font:16px system-ui;padding:40px'>"
            + ("<h2>Done.</h2><p>Token received. You can close this tab and go back to the terminal.</p>"
               if ok else
               f"<h2>Something went wrong.</h2><p>{type(self).error}</p>")
            + "</body></html>"
        ).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a) -> None:  # silence the default access log
        pass


def _post(url: str, data: dict) -> dict:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _verify(client_id: str, client_secret: str, refresh_token: str) -> tuple[bool, str]:
    """Prove the token works BEFORE it is pasted into production."""
    try:
        tok = _post(TOKEN_URI, {
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        })
    except Exception as exc:
        return False, f"refresh failed: {exc}"
    granted = set((tok.get("scope") or "").split())
    if SCOPE not in granted:
        return False, f"contacts scope NOT granted (got: {sorted(granted) or 'nothing'})"
    try:
        req = urllib.request.Request(
            "https://people.googleapis.com/v1/people/me/connections"
            "?pageSize=1&personFields=names",
            headers={"Authorization": f"Bearer {tok['access_token']}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:
        return False, f"People API call failed: {exc}"
    total = body.get("totalItems", body.get("totalPeople", "?"))
    return True, f"People API answered — {total} contacts visible"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--client-id", default=None)
    ap.add_argument("--client-secret", default=None)
    args = ap.parse_args()

    env = {**_read_dotenv(), **os.environ}
    client_id = args.client_id or env.get("GOOGLE_CLIENT_ID", "")
    client_secret = args.client_secret or env.get("GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("Missing GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.\n"
              "Pass --client-id / --client-secret, or put them in Lisa flow/.env.", file=sys.stderr)
        return 2

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    port = _free_port()
    redirect_uri = f"http://localhost:{port}/"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        # Both are required to be HANDED a refresh token: offline asks for one, and consent
        # forces the screen even when this account has already approved the app — without it
        # Google returns an access token only and the whole exercise is silently pointless.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{AUTH_URI}?{urllib.parse.urlencode(params)}"

    _Catcher.state = state
    server = http.server.HTTPServer(("127.0.0.1", port), _Catcher)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("\n1. A browser tab is opening. Sign in as the account that owns the calendar,")
    print("   and approve the CONTACTS permission.")
    print("2. If the tab does not open, paste this URL yourself:\n")
    print(f"   {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("   waiting for the redirect...")

    server.timeout = 300
    for _ in range(300):
        if _Catcher.code or _Catcher.error:
            break
        threading.Event().wait(1)

    if _Catcher.error:
        print(f"\nFAILED: {_Catcher.error}", file=sys.stderr)
        return 1
    if not _Catcher.code:
        print("\nTimed out waiting for the browser redirect.", file=sys.stderr)
        return 1

    try:
        tok = _post(TOKEN_URI, {
            "client_id": client_id, "client_secret": client_secret,
            "code": _Catcher.code, "code_verifier": verifier,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri,
        })
    except Exception as exc:
        print(f"\nToken exchange failed: {exc}", file=sys.stderr)
        return 1

    refresh = tok.get("refresh_token")
    if not refresh:
        print("\nGoogle returned no refresh_token. That happens when the app was already approved "
              "and prompt=consent was not honoured — revoke access at "
              "https://myaccount.google.com/permissions and run this again.", file=sys.stderr)
        return 1

    ok, detail = _verify(client_id, client_secret, refresh)
    print("\n" + "=" * 72)
    if ok:
        print("VERIFIED AGAINST THE LIVE API —", detail)
    else:
        print("WARNING — the token was minted but did NOT verify:", detail)
    print("=" * 72)
    print("\nGOOGLE_CONTACTS_REFRESH_TOKEN=" + refresh)
    print("\nPut that in the droplet's docker-compose.override.yml under the lisa service, as a")
    print("NEW variable. Do not touch GOOGLE_REFRESH_TOKEN — the calendar keeps using it.\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
