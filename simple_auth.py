"""Simple single-user login for SG Transport Pulse (used by auth.py by default).

Default sign-in:   Username  Admin     Password  123456
Change it on the server (Render > Environment) with APP_USERNAME and APP_PASSWORD. No new upload is needed.
Turn the login off with AUTH_PROVIDER=none. To use another identity provider, set AUTH_PROVIDER to its module name (see auth.py).
"""
import hmac
import os

USERNAME = os.getenv("APP_USERNAME", "Admin").strip() or "Admin"
PASSWORD = os.getenv("APP_PASSWORD", "123456") or "123456"


def authenticate(identifier, password):
    """Returns the user dict when the username (any capitals) and the password match, else None."""
    ok_user = hmac.compare_digest(str(identifier or "").strip().lower().encode(), USERNAME.lower().encode())
    ok_pass = hmac.compare_digest(str(password or "").encode(), PASSWORD.encode())
    if ok_user and ok_pass:
        return {"staff_id": USERNAME, "display_name": USERNAME, "role": "Administrator", "department": ""}
    return None
