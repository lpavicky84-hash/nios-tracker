"""
WhatsApp sender via AiSensy API campaigns.
(Combirds resells AiSensy — the API key is an AiSensy token.)

Docs: POST https://backend.aisensy.com/campaign/t1/api/v2
Body: { apiKey, campaignName, destination, userName, templateParams: [...] }

Setup required on the AiSensy/Combirds dashboard:
  1. Create + get approved a template (default name below) with 2 variables:
       {{1}} = student name, {{2}} = secure documents link
  2. Create an API Campaign that uses that template, set it LIVE.
     The campaign name must match AISENSY_CAMPAIGN (default 'admission_confirmed').

Credentials come from env vars (never hard-code):
  AISENSY_API_KEY   -> the long JWT api key
  AISENSY_CAMPAIGN  -> the API-campaign name (default 'admission_confirmed')
"""
import os
import re
import logging
import requests

logger = logging.getLogger(__name__)

AISENSY_URL = "https://backend.aisensy.com/campaign/t1/api/v2"


def _api_key() -> str:
    return os.environ.get("AISENSY_API_KEY", "").strip()


def _campaign() -> str:
    return os.environ.get("AISENSY_CAMPAIGN", "admission_confirmed").strip()


def is_configured() -> bool:
    return bool(_api_key())


def normalize_number(num) -> str:
    """Return digits with country code, e.g. '919876543210'."""
    d = re.sub(r"\D", "", str(num or ""))
    if not d:
        return ""
    if len(d) == 10:                       # bare 10-digit Indian number
        d = "91" + d
    elif len(d) == 11 and d.startswith("0"):
        d = "91" + d[1:]
    return d


def send_confirmation(name, phone, doc_link):
    """Send the approved confirmation template to one student.
    Returns (ok: bool, info: str)."""
    key = _api_key()
    if not key:
        return False, "AISENSY_API_KEY not set"
    dest = normalize_number(phone)
    if len(dest) < 11:
        return False, f"bad number: {phone}"
    name = (str(name).strip() or "Student")
    payload = {
        "apiKey": key,
        "campaignName": _campaign(),
        "destination": dest,
        "userName": name,
        "templateParams": [name, doc_link],
    }
    try:
        r = requests.post(AISENSY_URL, json=payload, timeout=30)
        if r.status_code in (200, 201):
            return True, "sent"
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, f"error: {e}"
