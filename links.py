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
import logging

# Doc-link tokens are HMAC-signed with SECRET_KEY. This MUST match the value set in the
# environment so links stay valid across restarts. We keep a fixed fallback (rather than
# a random one) so already-sent WhatsApp links don't break — but warn loudly if the env
# var is missing, because the fallback is public and would make links forgeable.
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    logging.getLogger(__name__).warning(
        "SECRET_KEY not set — document links use a public fallback key and are NOT secure. "
        "Set SECRET_KEY in the environment before going live.")
    SECRET_KEY = "nios-tracker-secret-2025-mvs"

# Public base URL of the deployed portal (used to build absolute links for WhatsApp).
# Default is the new custom domain; override with the PUBLIC_BASE_URL env var if needed.
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://status.mvsfoundation.in"
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


# ── Per-document direct links (used by the WhatsApp templates) ──
def make_doc_link(row_key: str, kind: str) -> str:
    payload = _b64e(row_key.encode()) + "~" + kind
    return payload + "." + _sign(payload)


def verify_doc_link(token: str):
    """Return (row_key, kind) if valid, else (None, None)."""
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None, None
        b64rk, kind = payload.split("~", 1)
        return _b64d(b64rk).decode("utf-8", "ignore"), kind
    except Exception:
        return None, None


def doc_file_url(row_key: str, kind: str) -> str:
    """Absolute URL that opens ONE document directly (id_card/app_form/hall_ticket)."""
    return f"{PUBLIC_BASE_URL}/doc/{make_doc_link(row_key, kind)}"


def short_doc_url(row_key: str, kind: str) -> str:
    """Compact /s/<code> URL for a document (falls back to the long URL if needed)."""
    try:
        from shortlinks import create_short
        code = create_short(row_key, kind)
        if code:
            return f"{PUBLIC_BASE_URL}/s/{code}"
    except Exception:
        pass
    return doc_file_url(row_key, kind)
