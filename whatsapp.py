"""
WhatsApp sender via AiSensy API campaigns (Combirds resells AiSensy).

Each NIOS session group has its OWN approved template -> its OWN API campaign:
  On Demand  -> AISENSY_CAMPAIGN_ONDEMAND  (template "3inone")
                params: [name, idCardLink, registrationLink, hallTicketLink]
  Stream 2   -> AISENSY_CAMPAIGN_STREAM2   (template "str2toc1")
                params: [name, idCardLink, registrationLink, regionalCentreAddress]
  Public     -> AISENSY_CAMPAIGN_PUBLIC    (April/October; id card only)
                params: [name, referenceNo, idCardLink]

Endpoint: POST https://backend.aisensy.com/campaign/t1/api/v2
Body: { apiKey, campaignName, destination, userName, templateParams: [...] }

Credentials/campaign names come from env vars (never hard-code):
  AISENSY_API_KEY, AISENSY_CAMPAIGN_ONDEMAND, AISENSY_CAMPAIGN_STREAM2, AISENSY_CAMPAIGN_PUBLIC

Public (April/October) can optionally use a SEPARATE WhatsApp API so it never mixes with
the main number: set AISENSY_API_KEY_PUBLIC (+ AISENSY_CAMPAIGN_PUBLIC on that account).
If AISENSY_API_KEY_PUBLIC is not set, public falls back to the main AISENSY_API_KEY.
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


def _api_key_for(group: str) -> str:
    """Public (April/October) students can be sent from a SEPARATE WhatsApp API so they
    never mix with the main number. If AISENSY_API_KEY_PUBLIC is set, the public group
    uses it; everything else (On Demand / Stream 2 / SYC / report) uses the main key.
    Falls back to the main key when the public key is not configured."""
    if group == "public":
        pub = os.environ.get("AISENSY_API_KEY_PUBLIC", "").strip()
        if pub:
            return pub
    return _api_key()


def campaign_for(group: str) -> str:
    # A campaign name set from the Settings page (stored in the DB) takes priority over the
    # Railway env var — so campaigns can be fixed right in the app without touching Railway.
    try:
        from database import get_db
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key=?", ("wa_campaign_" + group,)).fetchone()
        conn.close()
        ov = ((row["value"] if row else "") or "").strip()
        if ov:
            return ov
    except Exception:
        pass
    return os.environ.get(_CAMPAIGN_ENV.get(group, ""), "").strip()


# No-TOC variants: students whose tocStatus is 'no' did NOT take Transfer of Credit, so they
# get a SHORTER document set and need their OWN AiSensy campaign/template:
#   On Demand no-TOC -> {{1}} name, {{2}} id card, {{3}} hall ticket   (NO application form)
#   Stream 2  no-TOC -> {{1}} name, {{2}} id card                      (id card only)
_CAMPAIGN_ENV_NOTOC = {
    "ondemand": "AISENSY_CAMPAIGN_ONDEMAND_NOTOC",
    "stream2":  "AISENSY_CAMPAIGN_STREAM2_NOTOC",
}
_CAMPAIGN_DEFAULT_NOTOC = {
    "ondemand": "withouttochallticket",   # template: name + id card + hall ticket
    "stream2":  "WITHOUT_TOC",            # template "withouttoc2": name + id card
}


def campaign_for_notoc(group: str) -> str:
    """Campaign for students whose tocStatus is 'no'. A Settings override
    (wa_campaign_<group>_notoc) wins, then the Railway env var, then the built-in default."""
    try:
        from database import get_db
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key=?",
                           ("wa_campaign_" + group + "_notoc",)).fetchone()
        conn.close()
        ov = ((row["value"] if row else "") or "").strip()
        if ov:
            return ov
    except Exception:
        pass
    env = os.environ.get(_CAMPAIGN_ENV_NOTOC.get(group, ""), "").strip()
    return env or _CAMPAIGN_DEFAULT_NOTOC.get(group, "")


def allowed_docs(session, toc_status):
    """Which document kinds a student may download, by session group + tocStatus. Mirrors
    send_for_student exactly, so once tocStatus is known any OLD/WRONG link a student already
    received auto-blocks (e.g. an On Demand no-TOC student can no longer open an Application Form).
      On Demand TOC : id_card + app_form + hall_ticket   | no-TOC: id_card + hall_ticket
      Stream 2  TOC : id_card + app_form                 | no-TOC: id_card
      SYC           : hall_ticket   |   Public: id_card"""
    try:
        from excel_handler import normalize_toc
        notoc = (normalize_toc(toc_status) == "no")
    except Exception:
        notoc = (str(toc_status or "").strip().lower() == "no")
    group = group_of(session)
    if group == "ondemand":
        return {"id_card", "hall_ticket"} if notoc else {"id_card", "app_form", "hall_ticket"}
    if group == "stream2":
        return {"id_card"} if notoc else {"id_card", "app_form"}
    if group == "syc":
        return {"hall_ticket"}
    return {"id_card"}   # public (April / October)


def doc_allowed(session, toc_status, kind):
    """True if this document kind may be served for this student's session + tocStatus."""
    return kind in allowed_docs(session, toc_status)


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


def _post(campaign, phone, name, params, group=None, media_url=None):
    key = _api_key_for(group)
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
    # Image header (e.g. a demo screenshot). Only works with a template whose header is an
    # IMAGE; the URL must be publicly reachable so the WhatsApp gateway can fetch it.
    if media_url:
        payload["media"] = {"url": media_url, "filename": "screenshot.jpg"}
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


def send_for_student(student, only_number=None):
    """Send the right template for this student's session group.
    student: dict with row_key, student_name, mobile, session, reference_no, dob.
    only_number: if given, send ONLY to that number (used to deliver to a freshly-added
    alternate number when the primary already received the documents).
    Returns (ok, info)."""
    from links import short_doc_url as doc_file_url
    from excel_handler import normalize_toc
    group = group_of(student.get("session"))
    name = (str(student.get("student_name") or "Student").strip() or "Student")
    rk = student.get("row_key", "")
    toc = normalize_toc(student.get("toc_status"))   # 'yes' / 'no' / ''
    notoc = (toc == "no")                            # blank/unknown -> treated as YES (current)

    if group == "ondemand":
        if notoc:                                    # On Demand, no TOC: id card + hall ticket
            campaign = campaign_for_notoc("ondemand")
            params = [name, doc_file_url(rk, "id_card"), doc_file_url(rk, "hall_ticket")]
        else:                                        # On Demand, TOC: id card + app form + hall ticket
            campaign = campaign_for("ondemand")
            params = [name, doc_file_url(rk, "id_card"),
                      doc_file_url(rk, "app_form"), doc_file_url(rk, "hall_ticket")]
    elif group == "stream2":
        if notoc:                                    # Stream 2, no TOC: id card only
            campaign = campaign_for_notoc("stream2")
            params = [name, doc_file_url(rk, "id_card")]
        else:                                        # Stream 2, TOC: id card + app form (+ address)
            from nios_login import fetch_regional_address
            addr = fetch_regional_address(student.get("reference_no"), student.get("dob")) or ""
            if not addr:
                # don't send a broken/blank-address message; will retry next run
                return False, "regional address fetch failed"
            campaign = campaign_for("stream2")
            params = [name, doc_file_url(rk, "id_card"), doc_file_url(rk, "app_form"), addr]
    elif group == "syc":
        campaign = campaign_for("syc")
        params = [name, doc_file_url(rk, "hall_ticket")]   # SYC: hall ticket only
    else:  # public (April / October) — template: {{1}} name, {{2}} reference no, {{3}} id card link
        campaign = campaign_for("public")
        ref = (str(student.get("reference_no") or "").strip())
        params = [name, ref, doc_file_url(rk, "id_card")]

    if not campaign:
        return False, f"no campaign set for {group}" + (" (no-TOC)" if notoc else "")

    primary = student.get("mobile")
    # Targeted send (e.g. only the newly-added alternate number).
    if only_number:
        on = normalize_number(only_number)
        if not on or len(on) < 11:
            return False, f"bad number: {only_number}"
        return _post(campaign, only_number, name, params, group=group)
    ok, info = _post(campaign, primary, name, params, group=group)
    # If the student gave an ALTERNATE number, send the same documents there too.
    alt = str(student.get("alt_mobile") or "").strip()
    pn, an = normalize_number(primary), normalize_number(alt)
    if an and len(an) >= 11 and an != pn:
        ok2, info2 = _post(campaign, alt, name, params, group=group)
        if ok or ok2:
            note = "Sent to 2 numbers (own + alternate)"
            if not (ok and ok2):
                note += " — one still pending"
            return True, note
        return False, f"both numbers failed: {info} | {info2}"
    return ok, info


def required_campaign_for(group):
    """ONE universal document-request template per AiSensy account (image-header type):
    main account (on-demand + stream2) vs public account. Same template design on both, so
    only TWO campaigns are ever needed."""
    if group == "public":
        return os.environ.get("AISENSY_CAMPAIGN_REQUIRED_PUBLIC", "").strip()
    return os.environ.get("AISENSY_CAMPAIGN_REQUIRED", "").strip()


def send_required_reminder(student, message=None, media_url=None, default_img=None):
    """Polite reminder sent when a student is 'Document Required'. The counsellor reviews/edits
    the document line on the portal first; that text goes into the approved template as {{2}}
    ({{1}} = name). The template is a single UNIVERSAL image-header template, so it ALWAYS
    carries an image: the uploaded demo screenshot when present, otherwise the default MVS
    banner. Routing: public -> public API; on-demand/stream2 -> main API. Sends to primary +
    alternate. Returns (ok, info)."""
    group = group_of(student.get("session"))
    campaign = required_campaign_for(group)
    if not campaign:
        return False, f"no document-required campaign set for {group}"
    name = (str(student.get("student_name") or "Student").strip() or "Student")
    msg = (str(message or "").strip()
           or "kuch zaroori documents jinki aapke admission ko poora karne ke liye zarurat hai")
    params = [name, msg]   # approved template: {{1}} = name, {{2}} = document request
    # Universal image-header template -> always send an image: screenshot if attached,
    # else the default MVS banner. This lets ONE template (per account) cover every case.
    media = (str(media_url or "").strip() or str(default_img or "").strip() or None)
    primary = student.get("mobile")
    ok, info = _post(campaign, primary, name, params, group=group, media_url=media)
    alt = str(student.get("alt_mobile") or "").strip()
    pn, an = normalize_number(primary), normalize_number(alt)
    if an and len(an) >= 11 and an != pn:
        ok2, info2 = _post(campaign, alt, name, params, group=group, media_url=media)
        if ok or ok2:
            note = "Reminder sent to 2 numbers (own + alternate)"
            if not (ok and ok2):
                note += " — one still pending"
            return True, note
        return False, f"both numbers failed: {info} | {info2}"
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
    elif group == "public":
        params = [name, "REF1234567", demo]   # {{1}} name, {{2}} reference no, {{3}} id card link
    else:
        params = [name, demo]
    return _post(campaign, number, name, params, group=group)
