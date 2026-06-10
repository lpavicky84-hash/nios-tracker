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
    # All links
    all_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)
        if href and not href.lower().startswith("javascript"):
            all_links.append({"text": text[:60], "href": href})
    # Detect login success: presence of dashboard markers
    body = resp.text.lower()
    logged_in = any(m in body for m in ["admission status", "my documents", "payment status",
                                         "logout", "enroll no", "i card"])
    classified = find_download_links(session, resp.text)
    # page title / any error text
    title = soup.find("title")
    return {
        "final_url": resp.url,
        "status_code": resp.status_code,
        "logged_in_guess": logged_in,
        "page_title": title.get_text(strip=True) if title else "",
        "classified_links": classified,
        "all_links": all_links[:60],
        "html_snippet": re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:1500],
    }
