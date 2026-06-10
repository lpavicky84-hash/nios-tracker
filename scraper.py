import os
import re
import time
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NIOS_URL = "https://sdmis.nios.ac.in/registration/check-admission-status"
RECAPTCHA_SITE_KEY = "6Lc07T4iAAAAADsnW1ZXbEz0GUissRcasTnSS4Nj"

CAPSOLVER_API_KEY = os.environ.get("CAPTCHA_API_KEY", "")
CAPSOLVER_CREATE  = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT  = "https://api.capsolver.com/getTaskResult"

# Order matters: most specific first
STATUS_KEYWORDS = [
    ("admission confirmed",               "Admission Confirmed"),
    ("document required",                 "Document Required"),
    ("documents required",                "Document Required"),
    ("document verification in progress", "Documents Verification In Progress"),
    ("documents verification",            "Documents Verification In Progress"),
    ("document verification",             "Documents Verification In Progress"),
    ("rejected",                          "Rejected"),
    ("admitted",                          "Admitted"),
    ("approved",                          "Approved"),
    ("verified",                          "Verified"),
    ("pending",                           "Pending"),
]

def get_status_label(text):
    if not text:
        return "Unknown"
    t = text.lower()
    for kw, label in STATUS_KEYWORDS:
        if kw in t:
            return label
    return "Unknown"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": NIOS_URL,
}

def solve_recaptcha_v3():
    if not CAPSOLVER_API_KEY:
        logger.error("CAPTCHA_API_KEY not set!")
        return ""
    try:
        payload = {
            "clientKey": CAPSOLVER_API_KEY,
            "task": {
                "type": "ReCaptchaV3TaskProxyLess",
                "websiteURL": NIOS_URL,
                "websiteKey": RECAPTCHA_SITE_KEY,
            }
        }
        r = requests.post(CAPSOLVER_CREATE, json=payload, timeout=30).json()
        if r.get("errorId") != 0:
            logger.error(f"CapSolver create error: {r.get('errorDescription')}")
            return ""
        task_id = r.get("taskId")
        for _ in range(30):
            time.sleep(2)
            rr = requests.post(CAPSOLVER_RESULT,
                               json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id},
                               timeout=30).json()
            if rr.get("errorId") != 0:
                logger.error(f"CapSolver result error: {rr.get('errorDescription')}")
                return ""
            if rr.get("status") == "ready":
                return rr.get("solution", {}).get("gRecaptchaResponse", "")
        return ""
    except Exception as e:
        logger.error(f"CapSolver error: {e}")
        return ""

def get_csrf(session):
    resp = session.get(NIOS_URL, headers=HEADERS, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")
    meta = soup.find("meta", {"name": "_csrf"})
    if meta:
        return meta.get("content", "")
    inp = soup.find("input", {"name": "_csrf"})
    return inp.get("value", "") if inp else ""

def _extract_fields(soup):
    """Parse the NIOS result page into a dict of label->value."""
    full = soup.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in full.split("\n") if l.strip()]
    data = {}
    for i, line in enumerate(lines):
        low = line.lower()
        if low in ("admission status", "reference no", "reference number",
                   "enrollment no", "name of candidate", "academic year") and i + 1 < len(lines):
            data[low] = lines[i + 1].strip()
    remark = _extract_remark(lines)
    return data, lines, remark

def _extract_remark(lines):
    """Find the actual RC Comment, strictly inside the RC Comments section."""
    start = -1
    for i, l in enumerate(lines):
        if "rc comment" in l.lower():
            start = i
            break
    if start < 0:
        return ""   # no RC Comments section -> no remark at all
    # Collect lines until the next section header
    stoppers = ("basic details", "admission details", "personal details",
                "payment details", "document details", "subject details")
    chunk = []
    for l in lines[start + 1:]:
        if any(s in l.lower() for s in stoppers):
            break
        chunk.append(l)
    skip = {"date", "comment", "#", "s.no", "sno"}
    best = ""
    for l in chunk:
        if l.lower() in skip:
            continue
        if len(l) > 25 and " " in l and any(c.isalpha() for c in l) and len(l) > len(best):
            best = l
    return best[:400]

def fetch_status(session, ref_no, email, csrf, token):
    """Check by reference if available else by email. Returns result dict."""
    result = {
        "reference_no": ref_no, "email": email,
        "status": "Fetch Error", "remark": "", "raw_text": "", "success": False,
        "discovered_ref": "",
    }
    try:
        payload = {
            "_csrf": csrf,
            "CheckStatus[email]": email if (email and not ref_no) else "",
            "CheckStatus[reference_no]": ref_no if ref_no else "",
            "CheckStatus[enrollment_no]": "",
            "CheckStatus[google_recapcha_response]": token,
        }
        headers = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
        resp = session.post(NIOS_URL, data=payload, headers=headers, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "meta", "link"]):
            tag.decompose()

        data, lines, remark = _extract_fields(soup)

        # status text
        status_text = data.get("admission status", "")
        if not status_text or get_status_label(status_text) == "Unknown":
            for line in lines:
                if get_status_label(line) != "Unknown":
                    status_text = line
                    break

        label = get_status_label(status_text)

        # If we checked by email, try to discover the reference number
        disc_ref = data.get("reference no") or data.get("reference number") or ""
        if disc_ref and re.match(r'^[A-Z]?\d{6,}', disc_ref.replace(" ", "")):
            result["discovered_ref"] = disc_ref.strip()

        result["status"] = label
        result["raw_text"] = status_text[:300]
        result["remark"] = remark[:400] if (label == "Document Required" and remark) else ""
        result["success"] = (label != "Unknown")
        logger.info(f"  {ref_no or email} -> {label}" + (f" | remark: {remark[:50]}" if remark else ""))

    except Exception as e:
        logger.error(f"Error fetching {ref_no or email}: {e}")
        result["raw_text"] = str(e)[:200]
    return result

def scrape_students(students):
    """students: list of dicts with reference_no/email. Returns results list."""
    logger.info(f"Scraping {len(students)} students...")
    results = []
    if not CAPSOLVER_API_KEY:
        for s in students:
            results.append({**s, "status": "Fetch Error", "raw_text": "No captcha key",
                            "success": False, "remark": "", "discovered_ref": ""})
        return results
    try:
        session = requests.Session()
        csrf = get_csrf(session)
        for i, s in enumerate(students):
            ref = s.get("reference_no", "")
            email = s.get("email", "")
            logger.info(f"[{i+1}/{len(students)}] {ref or email}")
            token = solve_recaptcha_v3()
            if not token:
                results.append({**s, "status": "Fetch Error", "raw_text": "Captcha failed",
                                "success": False, "remark": "", "discovered_ref": ""})
                continue
            if i > 0 and i % 15 == 0:
                csrf = get_csrf(session)
            res = fetch_status(session, ref, email, csrf, token)
            results.append({**s, **res})
            time.sleep(2)   # polite gap between students
    except Exception as e:
        logger.error(f"Session error: {e}")
    logger.info(f"Scrape complete. {len(results)} results.")
    return results

# Debug helper
def debug_full_response(reference_no):
    if not CAPSOLVER_API_KEY:
        return "ERROR: CAPTCHA_API_KEY not set"
    session = requests.Session()
    csrf = get_csrf(session)
    token = solve_recaptcha_v3()
    if not token:
        return "ERROR: captcha failed"
    payload = {
        "_csrf": csrf, "CheckStatus[email]": "",
        "CheckStatus[reference_no]": reference_no,
        "CheckStatus[enrollment_no]": "",
        "CheckStatus[google_recapcha_response]": token,
    }
    resp = session.post(NIOS_URL, data=payload,
                        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                        timeout=25)
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "meta", "link"]):
        tag.decompose()
    lines = [l.strip() for l in soup.get_text(separator="\n", strip=True).split("\n") if l.strip()]
    return f"STATUS: {resp.status_code}\n\n" + "\n".join(lines[:60])
