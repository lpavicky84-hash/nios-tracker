import time
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NIOS_URL = "https://sdmis.nios.ac.in/registration/check-admission-status"

STATUS_COLORS = {
    "pending":                {"hex": "FFF9C4", "label": "Pending"},
    "documents verification": {"hex": "FFE0B2", "label": "Documents Verification In Progress"},
    "verified":               {"hex": "C8E6C9", "label": "Verified"},
    "approved":               {"hex": "B2DFDB", "label": "Approved"},
    "admission confirmed":    {"hex": "69F0AE", "label": "Admission Confirmed"},
    "admitted":               {"hex": "BBDEFB", "label": "Admitted"},
    "rejected":               {"hex": "FFCDD2", "label": "Rejected"},
    "error":                  {"hex": "E0E0E0", "label": "Fetch Error"},
    "not found":              {"hex": "F8BBD0", "label": "Not Found"},
    "unknown":                {"hex": "F5F5F5", "label": "Unknown"},
}

def get_status_label(text: str) -> str:
    if not text:
        return "Unknown"
    t = text.lower()
    for key, val in STATUS_COLORS.items():
        if key in t:
            return val["label"]
    return "Unknown"

SESSION_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": NIOS_URL,
}

def get_csrf_and_fields(session: requests.Session):
    """Load the page, extract CSRF token and all form fields."""
    resp = session.get(NIOS_URL, headers=SESSION_HEADERS, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")

    # Get CSRF
    csrf = ""
    meta = soup.find("meta", {"name": "_csrf"}) or soup.find("meta", {"name": "csrf-token"})
    if meta:
        csrf = meta.get("content", "")
    else:
        inp = soup.find("input", {"name": "_csrf"}) or soup.find("input", {"name": "csrf_token"})
        if inp:
            csrf = inp.get("value", "")

    # Get ALL form fields (to send complete form)
    form = soup.find("form")
    form_fields = {}
    if form:
        for inp in form.find_all("input"):
            name = inp.get("name", "")
            val  = inp.get("value", "")
            if name:
                form_fields[name] = val

    logger.info(f"CSRF: {csrf[:20]}... | Form fields: {list(form_fields.keys())}")
    return csrf, form_fields, resp.cookies

def debug_fetch_one(reference_no: str) -> str:
    """Debug: fetch one reference and return raw HTML snippet."""
    session = requests.Session()
    csrf, form_fields, cookies = get_csrf_and_fields(session)

    # Try different field name combinations
    payloads_to_try = [
        {"_csrf": csrf, "referenceNo": reference_no},
        {"_csrf": csrf, "reference_no": reference_no},
        {"_csrf": csrf, "refNo": reference_no},
        {"_csrf": csrf, "enrollmentNo": "", "email": "", "referenceNo": reference_no},
    ]

    results = []
    for payload in payloads_to_try:
        try:
            resp = session.post(
                NIOS_URL,
                data=payload,
                headers={**SESSION_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                timeout=20
            )
            soup = BeautifulSoup(resp.text, "html.parser")
            # Remove scripts/styles
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            # Get first 1000 chars of meaningful text
            lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 3]
            snippet = "\n".join(lines[:30])
            results.append(f"PAYLOAD {list(payload.keys())}: {snippet[:500]}")
        except Exception as e:
            results.append(f"PAYLOAD ERROR: {e}")

    return "\n\n---\n\n".join(results)

def fetch_status_for_reference(session: requests.Session, reference_no: str, 
                                csrf: str, form_fields: dict) -> dict:
    result = {
        "reference_no": reference_no,
        "status": "Fetch Error",
        "raw_text": "",
        "success": False,
    }
    try:
        # Build payload with all original form fields + our reference
        payload = dict(form_fields)
        payload["_csrf"] = csrf
        # Try to find the right field name for reference number
        payload["referenceNo"] = reference_no
        # Clear other search fields
        for key in ["email", "Email", "enrollmentNo", "EnrollmentNo"]:
            if key in payload:
                payload[key] = ""

        resp = session.post(
            NIOS_URL,
            data=payload,
            headers={**SESSION_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=20
        )

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noise
        for tag in soup(["script", "style", "nav", "header", "footer", "meta", "link"]):
            tag.decompose()

        # Try specific result containers first
        status_text = ""
        selectors = [
            ".alert-success", ".alert-danger", ".alert-warning", ".alert-info",
            ".alert", "#result", "#status", ".status-result",
            "table tbody tr", ".admission-status", ".card .card-body",
            "[class*='admission']", "[class*='status']", "[id*='result']",
            "h3", "h4", ".well", ".panel-body"
        ]
        for sel in selectors:
            els = soup.select(sel)
            for el in els:
                txt = el.get_text(strip=True)
                if txt and len(txt) > 8 and any(
                    kw in txt.lower() for kw in [
                        "pending", "verified", "rejected", "admitted", "confirmed",
                        "approved", "verification", "status", "admission", "document"
                    ]
                ):
                    status_text = txt
                    logger.info(f"  Found via selector '{sel}': {txt[:100]}")
                    break
            if status_text:
                break

        # Fallback: all page text
        if not status_text:
            all_text = soup.get_text(separator=" ", strip=True)
            status_text = all_text[:800]
            logger.info(f"  Fallback text for {reference_no}: {all_text[:200]}")

        result["raw_text"] = status_text[:500]
        result["status"] = get_status_label(status_text)
        result["success"] = True
        logger.info(f"  {reference_no} → {result['status']} | text: {status_text[:80]}")

    except Exception as e:
        logger.error(f"Error fetching {reference_no}: {e}")
        result["raw_text"] = str(e)[:200]

    return result

def scrape_all_students(reference_numbers: list) -> list:
    logger.info(f"Starting scrape for {len(reference_numbers)} students...")
    results = []

    try:
        session = requests.Session()
        csrf, form_fields, _ = get_csrf_and_fields(session)

        for i, ref_no in enumerate(reference_numbers):
            logger.info(f"[{i+1}/{len(reference_numbers)}] Checking: {ref_no}")
            if i > 0 and i % 10 == 0:
                csrf, form_fields, _ = get_csrf_and_fields(session)
            res = fetch_status_for_reference(session, ref_no, csrf, form_fields)
            results.append(res)
            time.sleep(1.5)

    except Exception as e:
        logger.error(f"Session error: {e}")
        checked = {r["reference_no"] for r in results}
        for ref in reference_numbers:
            if ref not in checked:
                results.append({
                    "reference_no": ref, "status": "Fetch Error",
                    "raw_text": str(e)[:200], "success": False,
                })

    logger.info(f"Scrape complete. {len(results)} results.")
    return results
