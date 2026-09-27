"""Authentication boundary (V16.0) - SG Transport Pulse.

The application has NO built-in user store and NO built-in credentials. This module is only the seam where a real
identity provider plugs in, so the login page, session and sign-out work end to end once one is connected.

Connect a provider
    AUTH_PROVIDER = "package.module"        a module exposing
        authenticate(identifier: str, password: str) -> dict | None
            returns the user on success, e.g. {"staff_id": "S1234", "display_name": "K. Sim", "role": "Duty Controller",
            "department": "OCC"}; None (or raises) on failure. Never log the password.
        change_password(identifier, old, new) -> bool      (optional)
    AUTH_SECRET   = long random string used to sign the session cookie (required when AUTH_PROVIDER is set)
    AUTH_SESSION_HOURS = session length, default 12

Without AUTH_PROVIDER the platform runs in OPEN ACCESS mode exactly as before V16 (no page is gated); the login page says
so plainly and offers "Continue" instead of pretending to check a password.
"""
import base64
import hashlib
import hmac
import importlib
import json
import os
import time

COOKIE = "sgtp_session"
PROVIDER_PATH = os.getenv("AUTH_PROVIDER", "").strip()
SECRET = os.getenv("AUTH_SECRET", "").strip()
SESSION_HOURS = float(os.getenv("AUTH_SESSION_HOURS", "12") or 12)

_provider = None
_provider_err = None


def provider():
    """The configured provider module, or None. Import errors are reported by config(), never raised to the page."""
    global _provider, _provider_err
    if not PROVIDER_PATH:
        return None
    if _provider is None and _provider_err is None:
        try:
            _provider = importlib.import_module(PROVIDER_PATH)
            if not callable(getattr(_provider, "authenticate", None)):
                _provider_err, _provider = f"{PROVIDER_PATH} has no authenticate(identifier, password)", None
        except Exception as e:                      # pragma: no cover - depends on deployment
            _provider_err = f"{type(e).__name__}: {e}"
    return _provider


def config():
    p = provider()
    enabled = p is not None and bool(SECRET)
    problem = _provider_err or (None if not PROVIDER_PATH else (None if SECRET else "AUTH_SECRET is not set"))
    return {"enabled": enabled, "provider": PROVIDER_PATH or None, "problem": problem, "mode": "provider" if enabled else "open",
            "change_password": bool(enabled and callable(getattr(p, "change_password", None))), "session_hours": SESSION_HOURS}


def _sign(payload: bytes) -> str:
    return hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()


def issue(user: dict) -> str:
    now = time.time()
    body = {"u": {k: str(user.get(k) or "")[:80] for k in ("staff_id", "display_name", "role", "department")}, "iat": now, "exp": now + SESSION_HOURS * 3600}
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()
    return raw + "." + _sign(raw.encode())


def read(token: str):
    """The session in a cookie value, or None if missing / tampered / expired / auth not enabled."""
    if not token or not config()["enabled"] or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(raw.encode())):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except ValueError:
        return None
    if body.get("exp", 0) < time.time():
        return None
    return body


def authenticate(identifier: str, password: str):
    """-> (user | None, error | None). Only ever answers from the configured provider."""
    cfg = config()
    if not cfg["enabled"]:
        return None, "No authentication provider is configured on this server."
    try:
        user = provider().authenticate(identifier, password)
    except Exception:
        user = None
    if not user:
        return None, "Staff ID or password not recognised."
    user = dict(user)
    user.setdefault("staff_id", identifier)
    return user, None
