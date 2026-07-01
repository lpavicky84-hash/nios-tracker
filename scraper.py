import os
import re
import time
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NIOS_URL = "https://sdmis.nios.ac.in/registration/check-admission-status"
RECAPTCHA_SITE_KEY = "6Lc07T4iAAAAADsnW1ZXbEz0GUissRcasTnSS4Nj"
_SITEKEY_CACHE = {"key": RECAPTCHA_SITE_KEY, "action": None}   # refreshed from the live check-status page

def _extract_sitekey(html):
    """Pull the live reCAPTCHA site key out of the page. NIOS can rotate it; solving for a
    stale key makes NIOS silently reject the request. Returns '' if not found."""
    if not html:
        return ""
    for pat in (r'api\.js\?render=([\w\-]{20,})',
                r'data-sitekey=["\']([\w\-]{20,})["\']',
                r'grecaptcha\.execute\(\s*["\']([\w\-]{20,})["\']',
                r'["\'](6L[\w\-]{30,})["\']'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return ""

def _extract_action(html):
    """Pull the reCAPTCHA v3 action the page uses. Token is bound to its action; a mismatch
    makes NIOS reject the check. Returns '' if none."""
    if not html:
        return ""
    for pat in (r'grecaptcha\.execute\([^)]*\{\s*action\s*:\s*["\']([\w\-/]+)["\']',
                r'["\']action["\']\s*:\s*["\']([\w\-/]+)["\']',
                r'data-action=["\']([\w\-/]+)["\']'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return ""

CAPSOLVER_API_KEY = os.environ.get("CAPTCHA_API_KEY", "")
CAPSOLVER_CREATE  = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT  = "https://api.capsolver.com/getTaskResult"

# A perfectly valid student can momentarily read as 'Fetch Error' (captcha flake, NIOS
# hiccup, network blip). Auto-retry the status read a few times so the run self-heals
# instead of needing a manual re-run. Only retries on FAILURE — a clean read costs 1 try.
STATUS_MAX_TRIES = 3

# Login-portal status fallback safety limits (per run): never let it drain the captcha balance.
_FB_MAX = 20            # at most this many login-fallback reads per run
_FB_STREAK_STOP = 5     # stop the fallback entirely after this many failures in a row

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

def _parse_proxy_fields(proxy):
    """Parse CAPSOLVER_PROXY into CapSolver's separate proxy fields."""
    p = (proxy or "").strip()
    if not p:
        return None
    m = re.match(r'^(\w+)://([^:]+):([^@]+)@([^:]+):(\d+)$', p)
    if m:
        return {"proxyType": m.group(1), "proxyAddress": m.group(4), "proxyPort": int(m.group(5)),
                "proxyLogin": m.group(2), "proxyPassword": m.group(3)}
    parts = p.split(":")
    try:
        if len(parts) == 5:
            ptype, host, port, user, pw = parts
        elif len(parts) == 4:
            ptype, host, port, user, pw = "http", parts[0], parts[1], parts[2], parts[3]
        elif len(parts) == 2:
            ptype, host, port, user, pw = "http", parts[0], parts[1], "", ""
        else:
            return None
        out = {"proxyType": (ptype or "http").lower(), "proxyAddress": host, "proxyPort": int(port)}
        if user:
            out["proxyLogin"] = user
            out["proxyPassword"] = pw
        return out
    except Exception:
        return None

def solve_recaptcha_v3():
    if not CAPSOLVER_API_KEY:
        logger.error("CAPTCHA_API_KEY not set!")
        return ""
    try:
        proxy = os.environ.get("CAPSOLVER_PROXY", "").strip()
        pf = _parse_proxy_fields(proxy)
        if pf:
            task = {"type": "ReCaptchaV3Task", "websiteURL": NIOS_URL,
                    "websiteKey": _SITEKEY_CACHE.get("key") or RECAPTCHA_SITE_KEY}
            task.update(pf)
        else:
            task = {"type": "ReCaptchaV3TaskProxyLess", "websiteURL": NIOS_URL,
                    "websiteKey": _SITEKEY_CACHE.get("key") or RECAPTCHA_SITE_KEY}
        try:
            task["minScore"] = float(os.environ.get("CAPSOLVER_MIN_SCORE", "0.9"))
        except Exception:
            task["minScore"] = 0.9
        task["pageAction"] = _SITEKEY_CACHE.get("action") or "login"
        payload = {"clientKey": CAPSOLVER_API_KEY, "task": task}
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
    html = resp.text or ""
    sk = _extract_sitekey(html)          # keep captcha key in sync with the live page
    if sk:
        if sk != _SITEKEY_CACHE.get("key"):
            logger.info(f"NIOS check-status site-key updated -> {sk}")
        _SITEKEY_CACHE["key"] = sk
    act = _extract_action(html)
    if act:
        _SITEKEY_CACHE["action"] = act
    soup = BeautifulSoup(html, "html.parser")
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
            "CheckStatus[google_recaptcha_response]": token, "CheckStatus[google_recapcha_response]": token,
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
        result["nios_name"] = (data.get("name of candidate", "") or "").strip()
        result["remark"] = remark[:400] if (label == "Document Required" and remark) else ""
        result["success"] = (label != "Unknown")
        logger.info(f"  {ref_no or email} -> {label}" + (f" | remark: {remark[:50]}" if remark else ""))

    except Exception as e:
        logger.error(f"Error fetching {ref_no or email}: {e}")
        result["raw_text"] = str(e)[:200]
    return result

def scrape_students(students, should_cancel=None, progress_cb=None, on_result=None):
    """students: list of dicts with reference_no/email. Returns results list.
    should_cancel: optional callable -> True to stop early (cooperative cancel).
    progress_cb: optional callable(done, total) called after each student.
    on_result: optional callable(result_dict) called right after each student is
               fetched, so the caller can persist it live (incremental updates)."""
    logger.info(f"Scraping {len(students)} students...")
    results = []
    total = len(students)
    _fb_used = [0]      # per-run login-fallback counter (mutable so the inner block can bump it)
    _fb_streak = [0]    # consecutive fallback failures — stops the fallback if captcha is down
    if not CAPSOLVER_API_KEY:
        for s in students:
            r = {**s, "status": "Fetch Error", "raw_text": "No captcha key",
                 "success": False, "remark": "", "discovered_ref": ""}
            results.append(r)
            if on_result:
                on_result(r)
        return results
    try:
        session = requests.Session()
        csrf = get_csrf(session)
        for i, s in enumerate(students):
            if should_cancel and should_cancel():
                logger.info(f"Scrape cancelled at {i}/{total} by request")
                break
            ref = s.get("reference_no", "")
            email = s.get("email", "")
            logger.info(f"[{i+1}/{total}] {ref or email}")
            if i > 0 and i % 15 == 0:
                csrf = get_csrf(session)
            # Auto-retry on transient failure so a valid student never gets stuck as
            # 'Fetch Error'. Stops as soon as a real status comes back.
            res = {"reference_no": ref, "email": email, "status": "Fetch Error",
                   "raw_text": "", "success": False, "remark": "", "discovered_ref": ""}
            attempt = 0
            for attempt in range(STATUS_MAX_TRIES):
                if should_cancel and should_cancel():
                    break
                token = solve_recaptcha_v3()
                if not token:
                    res = {"reference_no": ref, "email": email, "status": "Fetch Error",
                           "raw_text": "Captcha failed", "success": False, "remark": "",
                           "discovered_ref": ""}
                else:
                    res = fetch_status(session, ref, email, csrf, token)
                if res.get("success"):
                    break
                if attempt < STATUS_MAX_TRIES - 1:
                    logger.info(f"  retry {attempt+1}/{STATUS_MAX_TRIES-1} for {ref or email} "
                                f"(was {res.get('status')})")
                    time.sleep(3)
                    csrf = get_csrf(session)   # refresh token context before retrying
            res["attempts"] = attempt + 1
            # Fallback: the public check-admission-status page sometimes returns a page with NO
            # readable status for a valid student (low captcha score / newly confirmed). If we
            # have a DOB, read the status straight off the student's login dashboard.
            # SAFETY: only for a true "Unknown" (a page came back, just no status) — NOT for
            # "Fetch Error" (captcha/network already failing; a login would just burn more
            # captcha). Capped per run, and stops entirely after a streak of failures so a
            # captcha-gateway outage can never drain the balance.
            if (res.get("status") == "Unknown" and not res.get("success")
                    and _fb_used[0] < _FB_MAX and _fb_streak[0] < _FB_STREAK_STOP):
                dob = s.get("dob", "")
                enr = s.get("enrollment_no", "")
                if dob and (ref or enr):
                    _fb_used[0] += 1
                    try:
                        import nios_login
                        lab = nios_login.fetch_status_via_login(ref, dob, enr)
                        if lab and lab != "Unknown":
                            res["status"] = lab
                            res["success"] = True
                            res["raw_text"] = ((res.get("raw_text", "") + " | read via login portal").strip())[:300]
                            _fb_streak[0] = 0
                            logger.info(f"  {ref or email} -> {lab} (via login fallback)")
                        else:
                            _fb_streak[0] += 1
                    except Exception as e:
                        _fb_streak[0] += 1
                        logger.warning(f"login-status fallback error for {ref or email}: {e}")
            merged = {**s, **res}
            results.append(merged)
            if on_result:
                on_result(merged)
            if progress_cb:
                progress_cb(i + 1, total)
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
        "CheckStatus[google_recaptcha_response]": token, "CheckStatus[google_recapcha_response]": token,
    }
    resp = session.post(NIOS_URL, data=payload,
                        headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                        timeout=25)
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "meta", "link"]):
        tag.decompose()
    lines = [l.strip() for l in soup.get_text(separator="\n", strip=True).split("\n") if l.strip()]
    return f"STATUS: {resp.status_code}\n\n" + "\n".join(lines[:60])
