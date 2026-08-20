"""MAL OAuth2 authorization (PRD F4, M3).

MAL only supports the *plain* PKCE method (code_challenge == code_verifier) —
see https://myanimelist.net/apiconfig/references/authorization. Access tokens
live ~1 hour, refresh tokens ~1 month. Tokens are stored in
~/.local/share/animew-widget/token.json (chmod 600).
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser

import requests

from . import config

_VERIFIER_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


def generate_code_verifier(length: int = 64) -> str:
    """MAL requires 43-128 chars; plain method means challenge == verifier."""
    if not 43 <= length <= 128:
        raise ValueError("verifier length must be 43-128")
    return "".join(secrets.choice(_VERIFIER_CHARS) for _ in range(length))


def build_authorize_url(client_id: str, code_verifier: str, callback_url: str, state: str) -> str:
    qs = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "code_challenge": code_verifier,  # plain method
            "code_challenge_method": "plain",
            "state": state,
            "redirect_uri": callback_url,
        }
    )
    return f"{config.MAL_OAUTH_AUTHORIZE}?{qs}"


class TokenStore:
    def __init__(self, path=None):
        self.path = path or config.TOKEN_FILE

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, tokens: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(tokens, indent=2) + "\n")
        os.chmod(self.path, 0o600)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


def _token_request(data: dict) -> dict:
    resp = requests.post(config.MAL_OAUTH_TOKEN, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


def exchange_code(client_id: str, client_secret: str, code: str, code_verifier: str, callback_url: str) -> dict:
    data = {
        "client_id": client_id,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": callback_url,
    }
    if client_secret:
        data["client_secret"] = client_secret
    tokens = _token_request(data)
    tokens.setdefault("expires_at", time.time() + int(tokens.get("expires_in", 3600)))
    return tokens


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    data = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if client_secret:
        data["client_secret"] = client_secret
    tokens = _token_request(data)
    tokens.setdefault("refresh_token", refresh_token)
    tokens.setdefault("expires_at", time.time() + int(tokens.get("expires_in", 3600)))
    return tokens


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict | None = None

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {
            "code": qs.get("code", [None])[0],
            "state": qs.get("state", [None])[0],
        }
        body = (
            b"<html><body style='font-family:sans-serif;background:#111;color:#eee;"
            b"display:flex;align-items:center;justify-content:center;height:100vh'>"
            b"Authorization complete. You can close this tab.</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence server logging
        pass


def login(client_id: str, client_secret: str, callback_url: str,
          timeout: int = 180, state: str | None = None) -> dict:
    """Run the full OAuth flow: open the browser, wait for the localhost
    callback, exchange the code. Returns the tokens dict."""
    verifier = generate_code_verifier()
    state = state or secrets.token_urlsafe(16)
    parsed = urllib.parse.urlparse(callback_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80

    _CallbackHandler.result = None
    server = http.server.HTTPServer((host, port), _CallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    webbrowser.open(build_authorize_url(client_id, verifier, callback_url, state))

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _CallbackHandler.result is not None:
            break
        time.sleep(0.2)
    server.server_close()

    result = _CallbackHandler.result
    if result is None or not result["code"]:
        raise TimeoutError("authorization timed out or was cancelled")
    if result["state"] != state:
        raise ValueError("OAuth state mismatch")
    return exchange_code(client_id, client_secret, result["code"], verifier, callback_url)
