"""
WhatsApp sender via AiSensy API campaigns (Combirds resells AiSensy).

Each NIOS session group has its OWN approved template -> its OWN API campaign:
  On Demand  -> AISENSY_CAMPAIGN_ONDEMAND  (template "3inone")
                params: [name, idCardLink, registrationLink, hallTicketLink]
  Stream 2   -> AISENSY_CAMPAIGN_STREAM2   (template "str2toc1")
                params: [name, idCardLink, registrationLink, regionalCentreAddress]
  Public     -> AISENSY_CAMPAIGN_PUBLIC    (April/October; id card only)
                params: [name, idCardLink]

Endpoint: POST https://backend.aisensy.com/campaign/t1/api/v2
Body: { apiKey, campaignName, destination, userName, templateParams: [...] }

Credentials/campaign names come from env vars (never hard-code):
  AISENSY_API_KEY, AISENSY_CAMPAIGN_ONDEMAND, AISENSY_CAMPAIGN_STREAM2, AISENSY_CAMPAIGN_PUBLIC
"""
import os
import re
import logging
import requests

logger = logging.getLogger(__name__)

AISENSY_URL = "https://backend.aisensy.com/campaign/t1/api/v2"

_CAMPAIGN_ENV = {
    "ondemand": "AISENSY_CAMPAIGN_ONDEMAND",
    "stream2":  "AISENSY_CAMPAIGN_STREAM2",
    "public":   "AISENSY_CAMPAIGN_PUBLIC",
    "syc":      "AISENSY_CAMPAIGN_SYC",
}


def _api_key() -> str:
    return os.environ.get("AISENSY_API_KEY", "").strip()


def campaign_for(group: str) -> str:
    return os.environ.get(_CAMPAIGN_ENV.get(group, ""), "").strip()


def is_configured() -> bool:
    return bool(_api_key())


def group_of(session) -> str:
    """Classify a session into ondemand / stream2 / public (or syc).
    Priority order so extra text never confuses it:
      - 'SYC'                                  -> syc
      - 'Stream 2'  (any extra text)           -> stream2
      - 'On Demand' (e.g. 'On Demand June-Sept.') -> ondemand
      - EVERYTHING ELSE (April/October, 'apr-27', 'oct-26', blank, unknown) -> public

    SAFETY: public is the default. Public only sends if AISENSY_CAMPAIGN_PUBLIC is set,
    so an unrecognised session can never be mistaken for On Demand and receive the
    On Demand documents (ID card + application form + hall ticket)."""
    s = (session or "").lower()
    if "syc" in s:
        return "syc"
    if "stream 2" in s or "stream2" in s or "stream-2" in s:
        return "stream2"
    if "on demand" in s or "ondemand" in s or "on-demand" in s or "odes" in s:
        return "ondemand"
    return "public"


def normalize_number(num) -> str:
    """Return digits with country code, e.g. '919876543210'."""
    d = re.sub(r"\D", "", str(num or ""))
    if not d:
        return ""
    if len(d) == 10:
        d = "91" + d
    elif len(d) == 11 and d.startswith("0"):
        d = "91" + d[1:]
    return d


def _post(campaign, phone, name, params):
    key = _api_key()
    if not key:
        return False, "AISENSY_API_KEY not set"
    if not campaign:
        return False, "campaign name not set (Railway env var)"
    dest = normalize_number(phone)
    if len(dest) < 11:
        return False, f"bad number: {phone}"
    payload = {
        "apiKey": key,
        "campaignName": campaign,
        "destination": dest,
        "userName": name or "Student",
        "templateParams": [str(p) for p in params],
    }
    try:
        r = requests.post(AISENSY_URL, json=payload, timeout=40)
        body = (r.text or "")[:200]
        if r.status_code in (200, 201):
            # HTTP 200 = AiSensy ACCEPTED the message into its queue, NOT that WhatsApp
            # delivered it. Real delivery is confirmed later via the delivery webhook.
            low = body.replace(" ", "").lower()
            if '"success":false' in low or '"error"' in low or "errormessage" in low:
                return False, f"gateway rejected: {body}"
            return True, "accepted by WhatsApp gateway (delivery pending)"
        return False, f"HTTP {r.status_code}: {body}"
    except Exception as e:
        return False, f"error: {e}"


def make_report_params(label, confirmed, required, error, unchanged, total, url):
    """The ONE fixed order of the 7 report variables — matches the original, proven
    WhatsApp report template. NEVER reorder these; the template depends on this exact order:
      {{1}} run label + time   {{2}} confirmed records   {{3}} documents required
      {{4}} error records       {{5}} unchanged records   {{6}} total records checked
      {{7}} excel report link
    (Admission-Verified and Docs-Verification counts stay in the Excel report, which has
     the full breakdown — the WhatsApp text is a quick summary.)"""
    return [str(label), str(confirmed), str(required), str(error),
            str(unchanged), str(total), str(url)]


def send_report(phone, params, media_url=None, filename="NIOS_Report.xlsx"):
    """Send the run report to ONE admin number via the report campaign
    (env AISENSY_CAMPAIGN_REPORT). If media_url is given and the template has a
    document header, the Excel is attached; otherwise the link is passed in params."""
    key = _api_key()
    campaign = os.environ.get("AISENSY_CAMPAIGN_REPORT", "").strip()
    if not key:
        return False, "AISENSY_API_KEY not set"
    if not campaign:
        return False, "AISENSY_CAMPAIGN_REPORT not set (Railway env var)"
    dest = normalize_number(phone)
    if len(dest) < 11:
        return False, f"bad number: {phone}"
    payload = {
        "apiKey": key,
        "campaignName": campaign,
        "destination": dest,
        "userName": "MVS Admin",
        "templateParams": [str(p) for p in params],
    }
    if media_url:
        payload["media"] = {"url": media_url, "filename": filename}
    try:
        r = requests.post(AISENSY_URL, json=payload, timeout=40)
        body = (r.text or "")[:200]
        if r.status_code in (200, 201):
            # HTTP 200 = AiSensy ACCEPTED the message into its queue, NOT that WhatsApp
            # delivered it. Real delivery is confirmed later via the delivery webhook.
            low = body.replace(" ", "").lower()
            if '"success":false' in low or '"error"' in low or "errormessage" in low:
                return False, f"gateway rejected: {body}"
            return True, "accepted by WhatsApp gateway (delivery pending)"
        return False, f"HTTP {r.status_code}: {body}"
    except Exception as e:
        return False, f"error: {e}"


def send_report_to_all(numbers, params, media_url=None, filename="NIOS_Report.xlsx"):
    """Send the report to every admin number. Returns (sent_count, [errors])."""
    sent, errs = 0, []
    for num in numbers:
        num = (num or "").strip()
        if not num:
            continue
        ok, msg = send_report(num, params, media_url=media_url, filename=filename)
        if ok:
            sent += 1
        else:
            errs.append(f"{num}: {msg}")
    return sent, errs


def send_for_student(student):
    """Send the right template for this student's session group.
    student: dict with row_key, student_name, mobile, session, reference_no, dob.
    Returns (ok, info)."""
    from links import short_doc_url as doc_file_url
    group = group_of(student.get("session"))
    campaign = campaign_for(group)
    if not campaign:
        return False, f"no campaign set for {group}"
    name = (str(student.get("student_name") or "Student").strip() or "Student")
    rk = student.get("row_key", "")

    if group == "ondemand":
        params = [name, doc_file_url(rk, "id_card"),
                  doc_file_url(rk, "app_form"), doc_file_url(rk, "hall_ticket")]
    elif group == "stream2":
        from nios_login import fetch_regional_address
        addr = fetch_regional_address(student.get("reference_no"), student.get("dob")) or ""
        if not addr:
            # don't send a broken/blank-address message; will retry next run
            return False, "regional address fetch failed"
        params = [name, doc_file_url(rk, "id_card"), doc_file_url(rk, "app_form"), addr]
    elif group == "syc":
        params = [name, doc_file_url(rk, "hall_ticket")]   # SYC: hall ticket only
    else:  # public
        params = [name, doc_file_url(rk, "id_card")]

    ok, info = _post(campaign, student.get("mobile"), name, params)
    return ok, info


def send_test(number, name="Test Student", group="ondemand"):
    """Send a test message of the chosen template to any number (demo links)."""
    from links import PUBLIC_BASE_URL
    campaign = campaign_for(group)
    if not campaign:
        return False, f"no campaign set for {group} (set its Railway env var)"
    demo = PUBLIC_BASE_URL
    if group == "ondemand":
        params = [name, demo, demo, demo]
    elif group == "stream2":
        params = [name, demo, demo, "NIOS Regional Centre, Sample Address - 110001"]
    else:
        params = [name, demo]
    return _post(campaign, number, name, params)
