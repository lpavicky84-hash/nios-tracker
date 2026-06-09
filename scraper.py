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

def get_csrf_token(session: requests.Session) -> str:
    """Load the page and extract CSRF token."""
    try:
        resp = session.get(NIOS_URL, headers=SESSION_HEADERS, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        # Try meta tag
        meta = soup.find("meta", {"name": "_csrf"}) or soup.find("meta", {"name": "csrf-token"})
        if meta:
            token = meta.get("content", "")
            logger.info(f"CSRF from meta: {token[:20]}...")
            return token
        # Try hidden input
        inp = soup.find("input", {"name": "_csrf"}) or soup.find("input", {"name": "csrf_token"})
        if inp:
            token = inp.get("value", "")
            logger.info(f"CSRF from input: {token[:20]}...")
            return token
        logger.warning("CSRF token not found")
        return ""
    except Exception as e:
        logger.error(f"Error getting CSRF: {e}")
        return ""

def fetch_status_for_reference(session: requests.Session, reference_no: str, csrf_token: str) -> dict:
    result = {
        "reference_no": reference_no,
        "status": "Fetch Error",
        "raw_text": "",
        "success": False,
    }
    try:
        payload = {
            "_csrf": csrf_token,
            "referenceNo": str(reference_no).strip(),
        }
        headers = {**SESSION_HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
        resp = session.post(NIOS_URL, data=payload, headers=headers, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Try to find status in response
        status_text = ""
        for selector in [
            ".alert", ".status", ".result", ".admission-status",
            "[class*='status']", "[class*='result']", "[class*='alert']",
            "table", ".card-body", ".panel-body"
        ]:
            el = soup.select_one(selector)
            if el:
                txt = el.get_text(strip=True)
                if txt and len(txt) > 5:
                    status_text = txt
                    break

        if not status_text:
            # Fallback: get all visible text
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            status_text = soup.get_text(separator=" ", strip=True)

        if status_text and len(status_text) > 5:
            result["raw_text"] = status_text[:500]
            result["status"] = get_status_label(status_text)
            result["success"] = True
            logger.info(f"  {reference_no} → {result['status']}")
        else:
            result["status"] = "Not Found"
            result["raw_text"] = "Empty response"

    except Exception as e:
        logger.error(f"Error fetching {reference_no}: {e}")
        result["raw_text"] = str(e)[:200]

    return result

def scrape_all_students(reference_numbers: list) -> list:
    logger.info(f"Starting scrape for {len(reference_numbers)} students...")
    results = []

    try:
        session = requests.Session()
        # Get fresh CSRF token
        csrf_token = get_csrf_token(session)

        for i, ref_no in enumerate(reference_numbers):
            logger.info(f"[{i+1}/{len(reference_numbers)}] Checking: {ref_no}")
            # Refresh CSRF every 10 requests
            if i > 0 and i % 10 == 0:
                csrf_token = get_csrf_token(session)
            res = fetch_status_for_reference(session, ref_no, csrf_token)
            results.append(res)
            time.sleep(1.5)  # polite delay

    except Exception as e:
        logger.error(f"Session error: {e}")
        checked = {r["reference_no"] for r in results}
        for ref in reference_numbers:
            if ref not in checked:
                results.append({
                    "reference_no": ref,
                    "status": "Fetch Error",
                    "raw_text": str(e)[:200],
                    "success": False,
                })

    logger.info(f"Scrape complete. {len(results)} results.")
    return results
