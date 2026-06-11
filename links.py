"""
Signed public document links.
Lets a student open their own NIOS documents WITHOUT logging into the portal.
The token is an HMAC-signed (SECRET_KEY) wrapper around the student's row_key,
so links are unguessable but don't expire (student may need the hall ticket later).
"""
import os
import hmac
import base64
import hashlib

SECRET_KEY = os.environ.get("SECRET_KEY", "nios-tracker-secret-2025-mvs")

# Public base URL of the deployed portal (used to build absolute links for WhatsApp).
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://web-production-09671.up.railway.app"
).rstrip("/")


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(data: str) -> str:
    sig = hmac.new(SECRET_KEY.encode(), data.encode(), hashlib.sha256).digest()
    return _b64e(sig)[:24]


def make_doc_token(row_key: str) -> str:
    payload = _b64e(row_key.encode())
    return payload + "." + _sign(payload)


def verify_doc_token(token: str):
    """Return the original row_key if the token is valid, else None."""
    try:
        payload, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        return _b64d(payload).decode("utf-8", "ignore")
    except Exception:
        return None


def doc_page_url(row_key: str) -> str:
    """Absolute URL the student taps to see all their documents."""
    return f"{PUBLIC_BASE_URL}/d/{make_doc_token(row_key)}"
