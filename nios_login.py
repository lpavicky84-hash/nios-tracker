"""
NIOS student-portal login + download-link discovery (Phase 2).
Logs in with Reference No + Date of Birth (solving reCAPTCHA v3 via CapSolver),
then parses the post-login dashboard for I-Card / Application Form / Hall Ticket links.
"""
import os
import re
import time
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

BASE      = "https://sdmis.nios.ac.in"
LOGIN_URL = "https://sdmis.nios.ac.in/auth/other-login"

# Same reCAPTCHA v3 key as the status page (same domain). If login fails on
# captcha, debug output will reveal a different key.
RECAPTCHA_SITE_KEY = "6Lc07T4iAAAAADsnW1ZXbEz0GUissRcasTnSS4Nj"

CAPSOLVER_API_KEY = os.environ.get("CAPTCHA_API_KEY", "")
CAPSOLVER_CREATE  = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT  = "https://api.capsolver.com/getTaskResult"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": LOGIN_URL,
}

def solve_recaptcha_v3(page_url=LOGIN_URL, page_action=None):
    if not CAPSOLVER_API_KEY:
        logger.error("CAPTCHA_API_KEY not set!")
        return ""
    try:
        task = {
            "type": "ReCaptchaV3TaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": RECAPTCHA_SITE_KEY,
        }
        if page_action:
            task["pageAction"] = page_action
        r = requests.post(CAPSOLVER_CREATE,
                          json={"clientKey": CAPSOLVER_API_KEY, "task": task}, timeout=30).json()
        if r.get("errorId") != 0:
            logger.error(f"CapSolver create error: {r.get('errorDescription')}")
            return ""
        task_id = r.get("taskId")
        for _ in range(30):
            time.sleep(2)
            rr = requests.post(CAPSOLVER_RESULT,
                              json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}, timeout=30).json()
            if rr.get("errorId") != 0:
                return ""
            if rr.get("status") == "ready":
                return rr.get("solution", {}).get("gRecaptchaResponse", "")
        return ""
    except Exception as e:
        logger.error(f"CapSolver error: {e}")
        return ""

def format_dob(dob):
    """Return DOB as DD-MM-YYYY (NIOS login format)."""
    if isinstance(dob, (datetime, date)):
        return dob.strftime("%d-%m-%Y")
    s = str(dob or "").strip()
    if not s:
        return ""
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d",
                "%Y-%m-%d %H:%M:%S", "%d-%m-%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return s   # assume already DD-MM-YYYY

def get_login_csrf(session):
    r = session.get(LOGIN_URL, headers=HEADERS, timeout=20)
    soup = BeautifulSoup(r.text, "html.parser")
    meta = soup.find("meta", {"name": "_csrf"})
    if meta and meta.get("content"):
        return meta["content"]
    inp = soup.find("input", {"name": "_csrf"})
    return inp.get("value", "") if inp else ""

def login_student(reference_no, dob, page_action=None):
    """Login. Returns (session, final_response)."""
    session = requests.Session()
    csrf = get_login_csrf(session)
    token = solve_recaptcha_v3(LOGIN_URL, page_action)
    if not token:
        logger.error("Login captcha failed")
        return session, None
    payload = {
        "_csrf": csrf,
        "LoginForm[reference_no]": reference_no,
        "LoginForm[enrollment_no]": "",
        "LoginForm[application_no]": "",
        "LoginForm[date_of_birth]": format_dob(dob),
        "LoginForm[google_recapcha_response]": token,
        "login-button": "",
    }
    resp = session.post(LOGIN_URL, data=payload,
                        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded",
                                 "Origin": BASE},
                        timeout=30, allow_redirects=True)
    return session, resp

# ── Link discovery ──
LINK_KEYWORDS = {
    "id_card":    ["i card", "icard", "id card", "id-card", "identity"],
    "app_form":   ["application form", "application-form", "print application", "appform", "admission form"],
    "hall_ticket":["hall ticket", "hall-ticket", "hallticket", "admit card", "admit-card"],
}

def _classify_link(text, href):
    blob = (text + " " + href).lower()
    for kind, kws in LINK_KEYWORDS.items():
        if any(kw in blob for kw in kws):
            return kind
    return None

def find_download_links(session, html, base_url=BASE):
    """Parse dashboard HTML for download links. Returns dict of kind->absolute url."""
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("javascript") or href == "#":
            continue
        text = a.get_text(" ", strip=True)
        kind = _classify_link(text, href)
        if kind and kind not in found:
            found[kind] = urljoin(base_url, href)
    return found

def debug_login(reference_no, dob, page_action=None):
    """Test login and return everything we can see, for link discovery."""
    if not CAPSOLVER_API_KEY:
        return {"error": "CAPTCHA_API_KEY not set"}
    session, resp = login_student(reference_no, dob, page_action)
    if resp is None:
        return {"error": "captcha failed or no response"}
    soup = BeautifulSoup(resp.text, "html.parser")
    all_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)
        if href and not href.lower().startswith("javascript"):
            all_links.append({"text": text[:60], "href": href})
    logged_in = is_logged_in(resp.text)
    classified = find_download_links(session, resp.text)
    probe = probe_links(session, classified) if logged_in else {}
    title = soup.find("title")
    return {
        "final_url": resp.url,
        "status_code": resp.status_code,
        "logged_in_guess": logged_in,
        "page_title": title.get_text(strip=True) if title else "",
        "classified_links": classified,
        "link_probe": probe,
        "all_links": all_links[:40],
    }

def is_logged_in(html):
    body = (html or "").lower()
    return any(m in body for m in ["admission status", "my documents", "payment status",
                                    "logout", "enroll no", "i card", "dashboard"])

# ── Document fetching (proxy download) ──
DOC_URLS = {
    "id_card":     "/registration/id-card",
    "app_form":    "/home/print-form",
    "hall_ticket": "/registration/hall-ticket",
}

_session_cache = {}   # reference_no -> (session, expiry_ts)

def get_logged_in_session(reference_no, dob):
    """Return a logged-in session (cached ~5 min) or None."""
    now = time.time()
    cached = _session_cache.get(reference_no)
    if cached and cached[1] > now:
        return cached[0]
    session, resp = login_student(reference_no, dob)
    if resp is None or not is_logged_in(resp.text):
        return None
    _session_cache[reference_no] = (session, now + 300)
    return session

def _extract_pdf_from_html(session, html, base):
    """If the doc page is HTML, find the embedded/linked PDF and fetch it."""
    soup = BeautifulSoup(html, "html.parser")
    cand = None
    for tag, attr in [("iframe", "src"), ("embed", "src"), ("object", "data")]:
        el = soup.find(tag)
        if el and el.get(attr):
            cand = urljoin(base, el[attr]); break
    if not cand:
        for a in soup.find_all("a", href=True):
            h = a["href"].lower()
            if ".pdf" in h or "download" in h or "print" in h:
                cand = urljoin(base, a["href"]); break
    if cand:
        r = session.get(cand, headers=HEADERS, timeout=45)
        return r.content, r.headers.get("Content-Type", "")
    return None, ""

def fetch_document(reference_no, dob, kind):
    """Login as student & fetch the document. Returns (bytes, content_type, filename) or (None, error, None)."""
    if not CAPSOLVER_API_KEY:
        return None, "CAPTCHA_API_KEY not set", None
    path = DOC_URLS.get(kind)
    if not path:
        return None, "invalid document kind", None
    session = get_logged_in_session(reference_no, dob)
    if session is None:
        return None, "login failed (check reference/DOB)", None
    target = urljoin(BASE, path)
    try:
        r = session.get(target, headers=HEADERS, timeout=45)
    except Exception as e:
        return None, f"fetch error: {e}", None
    ct = r.headers.get("Content-Type", "").lower()
    fname = f"{kind}_{reference_no}.pdf"
    # Direct PDF?
    if "pdf" in ct or r.content[:4] == b"%PDF":
        return r.content, "application/pdf", fname
    # HTML page wrapping a PDF
    if "html" in ct:
        content, ct2 = _extract_pdf_from_html(session, r.text, target)
        if content and (content[:4] == b"%PDF" or "pdf" in ct2.lower()):
            return content, "application/pdf", fname
        if content:
            return content, ct2 or "application/octet-stream", f"{kind}_{reference_no}"
    return None, f"no PDF found (got {ct or 'unknown'})", None

def probe_links(session, classified):
    """Fetch each classified link to report content-type/size (debug)."""
    out = {}
    for kind, url in classified.items():
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            ct = r.headers.get("Content-Type", "")
            info = {"status": r.status_code, "content_type": ct, "size_bytes": len(r.content),
                    "is_pdf": (r.content[:4] == b"%PDF" or "pdf" in ct.lower())}
            if "html" in ct.lower():
                soup = BeautifulSoup(r.text, "html.parser")
                pdfs = []
                for tag, attr in [("iframe", "src"), ("embed", "src"), ("object", "data")]:
                    el = soup.find(tag)
                    if el and el.get(attr):
                        pdfs.append(el[attr])
                for a in soup.find_all("a", href=True):
                    if ".pdf" in a["href"].lower():
                        pdfs.append(a["href"])
                info["pdf_links_inside"] = pdfs[:5]
            out[kind] = info
        except Exception as e:
            out[kind] = {"error": str(e)}
    return out
