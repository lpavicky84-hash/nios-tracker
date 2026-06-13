"""
NIOS student-portal login + download-link discovery (Phase 2).
Logs in with Reference No + Date of Birth (solving reCAPTCHA v3 via CapSolver),
then parses the post-login dashboard for I-Card / Application Form / Hall Ticket links.
"""
import os
import re
import time
import base64
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
    """Return DOB as DD-MM-YYYY (NIOS login format). Robust to any time component
    and common date formats."""
    if isinstance(dob, (datetime, date)):
        return dob.strftime("%d-%m-%Y")
    s = str(dob or "").strip()
    if not s:
        return ""
    # JavaScript Date.toString(), e.g. the MVS student portal sends:
    #   "Wed Aug 08 2007 12:30:00 GMT+0530 (India Standard Time)"
    # The local date part ("Aug 08 2007") is the correct DOB — extract it.
    m = re.match(r"^[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{4})\b", s)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}",
                                     "%b %d %Y").strftime("%d-%m-%Y")
        except ValueError:
            pass
    # Strip ONLY a trailing time component (e.g. " 00:00:00", "T00:00:00.000Z"),
    # while preserving month-name dates like "8 August 2007".
    s = re.sub(r"[ T]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$", "", s).strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d",
                "%d-%m-%y", "%m/%d/%Y", "%d.%m.%Y", "%Y.%m.%d",
                "%d %m %Y", "%Y %m %d", "%d %B %Y", "%d %b %Y",
                "%B %d %Y", "%b %d %Y"):
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

def login_student(reference_no, dob, page_action=None, enrollment_no=""):
    """Login with reference_no OR enrollment_no (+ DOB). Returns (session, final_response)."""
    session = requests.Session()
    csrf = get_login_csrf(session)
    token = solve_recaptcha_v3(LOGIN_URL, page_action)
    if not token:
        logger.error("Login captcha failed")
        return session, None
    payload = {
        "_csrf": csrf,
        "LoginForm[reference_no]": "" if enrollment_no else reference_no,
        "LoginForm[enrollment_no]": enrollment_no or "",
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
    "app_form":   ["application form", "application-form", "print application", "appform",
                   "admission form", "registration summary", "registration-summary",
                   "reg summary", "registration form", "print registration"],
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
    # NEGATIVE signal first: if NIOS is showing the login page, we are NOT logged in,
    # even if some menu words happen to appear. This is the same bounce-back signature
    # that fetch_document uses, so verification and download agree.
    if ("login to your account" in body or "loginform[" in body
            or ("username / email" in body and "reset password" in body)
            or "google_recapcha_response" in body):
        return False
    return any(m in body for m in ["admission status", "my documents", "payment status",
                                    "logout", "enroll no", "i card", "dashboard"])

# ── Document fetching (proxy download) ──
DOC_URLS = {
    "id_card":     "/registration/id-card",
    "app_form":    "/home/print-form",
    "hall_ticket": "/registration/hall-ticket",
}

_session_cache = {}   # reference_no -> (session, expiry_ts)

def get_logged_in_session(reference_no, dob, enrollment_no="", force=False):
    """Return a logged-in session (cached ~5 min) or None.
    Uses enrollment_no for login when given (SYC students), else reference_no.
    force=True ignores+clears the cache and logs in fresh (used for a retry)."""
    now = time.time()
    key = ("enr:" + enrollment_no) if enrollment_no else reference_no
    if force:
        _session_cache.pop(key, None)
    else:
        cached = _session_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]
    session, resp = login_student(reference_no, dob, enrollment_no=enrollment_no)
    if resp is None or not is_logged_in(resp.text):
        return None
    _session_cache[key] = (session, now + 300)
    return session

def verify_login(reference_no, dob, enrollment_no=""):
    """TRUE verification: log in AND fetch a protected page (ID card) — exactly the path
    the student's WhatsApp link takes. If NIOS bounces back to the login page (wrong DOB
    / Reference / Enrollment), the link would also fail, so we report failure and the
    caller blocks WhatsApp + marks it Failed to Run. Two attempts (reCAPTCHA v3 can flake)
    so a correct student is never falsely failed. Returns (ok: bool, message: str)."""
    key = ("enr:" + enrollment_no) if enrollment_no else reference_no
    try:
        target = urljoin(BASE, DOC_URLS["id_card"])
        for attempt in range(2):
            session = get_logged_in_session(reference_no, dob, enrollment_no=enrollment_no,
                                            force=(attempt == 1))
            if session is None:
                continue                                   # login page bounce -> retry
            try:
                r = session.get(target, headers=HEADERS, timeout=35)
            except Exception:
                continue
            ct = r.headers.get("Content-Type", "").lower()
            low = r.text.lower()
            if "pdf" in ct or r.content[:4] == b"%PDF":
                return True, ""
            if ("login to your account" in low or "loginform[" in low
                    or ("username / email" in low and "reset password" in low)):
                _session_cache.pop(key, None)              # bounced -> drop + retry
                continue
            if "html" in ct:
                return True, ""                            # got the protected doc page
        who = "Enrollment No" if enrollment_no else "Reference No"
        return False, (f"NIOS login failed — data mismatch. Check {who} & Date of Birth "
                       f"(DOB used: '{format_dob(dob)}').")
    except Exception as e:
        return False, f"NIOS login error: {str(e)[:120]}"

def _fetch_bytes(url, session):
    try:
        sess = session if "sdmis.nios.ac.in" in url else requests
        r = sess.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            return r.content, r.headers.get("Content-Type", "")
    except Exception as e:
        logger.warning(f"resource fetch failed {url}: {e}")
    return None, ""

def _guess_mime(url, ctype, data):
    if ctype and "/" in ctype:
        return ctype.split(";")[0].strip()
    u = url.lower()
    if u.endswith(".png"): return "image/png"
    if u.endswith(".jpg") or u.endswith(".jpeg"): return "image/jpeg"
    if u.endswith(".gif"): return "image/gif"
    if u.endswith(".svg"): return "image/svg+xml"
    if data[:4] == b"\x89PNG": return "image/png"
    if data[:3] == b"\xff\xd8\xff": return "image/jpeg"
    return "image/png"

def _size_rules_from_soup(soup):
    """Map class-name -> {prop: value} for width/height/max-width rules declared
    in the page's OWN <style> blocks. Each NIOS document sizes its photo (img.icone),
    signature and QR via its own CSS, and DIFFERENTLY per document (ID-card photo
    60x77, hall-ticket photo 122x157). We read those exact sizes so we can pin them
    inline (with !important) — this survives document.write / loader rendering and
    any external print.css that would otherwise override them."""
    rules = {}
    for st in soup.find_all("style"):
        css = st.get_text() or ""
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            props = {}
            for prop in ("width", "height", "max-width", "max-height"):
                m = re.search(r"(?<![\w-])" + prop + r"\s*:\s*([^;]+)", body, re.I)
                if m:
                    props[prop] = m.group(1).strip()
            if not props:
                continue
            for cls in re.findall(r"\.([A-Za-z0-9_-]+)", sel):
                rules.setdefault(cls, {}).update(props)
    return rules

def inline_resources(html, session):
    """Fetch images & CSS (using student's session for protected ones) and embed inline,
    so the document renders fully in the counsellor's / student's browser.

    Each NIOS document (ID card / hall ticket / app form) already ships its OWN
    <style> block that sizes the photo (img.icone), signature (img.sign /
    .signature--img) and QR (img.code) correctly and DIFFERENTLY per document
    (e.g. ID-card photo 60x77, hall-ticket photo 122x157). We read those sizes and
    pin them inline with !important so the photo can NEVER blow up to natural size,
    regardless of how the page is later rendered. Unsized images (the header logo)
    are simply bounded to max-width:100%."""
    soup = BeautifulSoup(html, "html.parser")
    size_map = _size_rules_from_soup(soup)
    SIZED_CLASSES = ("icone", "sign", "code", "signature", "icon")
    for img in soup.find_all("img"):
        src = img.get("src")
        classes = img.get("class") or []
        classes_l = " ".join(classes).lower()
        style_l = (img.get("style") or "").lower()
        has_size = bool(img.get("width") or img.get("height")
                        or "width" in style_l or "height" in style_l)
        sized_by_css = any(cl in classes_l for cl in SIZED_CLASSES)
        # 1) Pin the page's OWN declared size inline (!important) for known classes.
        pinned = False
        for cls in classes:
            if cls in size_map:
                cur = (img.get("style") or "").strip().rstrip(";")
                pins = ";".join(f"{p}:{v} !important" for p, v in size_map[cls].items())
                img["style"] = (cur + ";" + pins) if cur else pins
                pinned = True
                break
        # 2) Bound images the page does NOT size at all (header logo, etc.)
        if not pinned and not has_size and not sized_by_css:
            cur = (img.get("style") or "").strip().rstrip(";")
            img["style"] = (cur + ";max-width:100%") if cur else "max-width:100%"
        if not src or src.startswith("data:"):
            continue
        # resolve + inline the bytes (session for protected sdmis/relative URLs)
        full = src if src.startswith("http") else urljoin(BASE, src)
        data, ctype = _fetch_bytes(full, session)
        if data:
            mime = _guess_mime(full, ctype, data)
            img["src"] = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        elif not src.startswith("http"):
            # couldn't inline a relative URL -> at least point it at NIOS (not the portal)
            img["src"] = full
    # Inline external stylesheets (so the page's own sizing applies). Preserve the
    # media attribute: NIOS app-form sizes the photo via style.css (media=screen)
    # and ships a separate print.css (media=print). Dropping media made print.css
    # leak onto the screen view and distort the layout.
    for link in soup.find_all("link"):
        rel = link.get("rel") or []
        if "stylesheet" not in [r.lower() for r in rel]:
            continue
        href = link.get("href")
        if not href:
            continue
        full = href if href.startswith("http") else urljoin(BASE, href)
        data, _ = _fetch_bytes(full, session)
        if data:
            style = soup.new_tag("style")
            media = link.get("media")
            if media:
                style["media"] = media
            style.string = data.decode("utf-8", "ignore")
            link.replace_with(style)
    return str(soup)

PRINT_BANNER = """
<div id="__mvs_bar" style="position:fixed;top:0;left:0;right:0;z-index:999999;background:#4F46E5;
color:#fff;padding:10px 14px;text-align:center;font-family:-apple-system,Arial,sans-serif;
box-shadow:0 2px 8px rgba(0,0,0,.2)">
  <button onclick="mvsPrint()" style="padding:10px 24px;font-size:15px;border:none;border-radius:8px;
  background:#fff;color:#4F46E5;font-weight:700;cursor:pointer">&#128196; Save as PDF / Print</button>
  <div style="font-size:12px;margin-top:6px;opacity:.95">If the button does not respond, open your browser
  menu ( &#8942; or the Share icon ) and tap <b>Print</b> or <b>Save as PDF</b>.</div>
</div>
<div id="__mvs_inapp" style="display:none;position:fixed;left:0;right:0;z-index:999998;background:#FEF3C7;
color:#92400E;padding:9px 14px;text-align:center;font-family:-apple-system,Arial,sans-serif;
font-size:12.5px;font-weight:600;border-bottom:1px solid #FCD34D">
  For best results open this page in <b>Chrome</b> or <b>Safari</b>: tap the menu ( &#8942; ) at the top-right
  &rarr; <b>Open in browser</b>, then tap Save as PDF.
</div>
<style>@media print{#__mvs_bar,#__mvs_inapp{display:none!important}} body{padding-top:70px!important}</style>
<script>
function mvsPrint(){
  try{ window.focus(); }catch(e){}
  try{ window.print(); }
  catch(e){ alert("To save: open your browser menu ( the three dots or Share icon ) and choose Print or Save as PDF."); }
}
(function(){
  try{
    var ua = navigator.userAgent || "";
    if(/(WhatsApp|Instagram|FBAN|FBAV|FB_IAB|Line|Snapchat|Twitter|Threads|MicroMessenger|GSA)/i.test(ua)){
      var b = document.getElementById("__mvs_inapp");
      if(b){
        var topBar = document.getElementById("__mvs_bar");
        b.style.top = (topBar ? topBar.offsetHeight : 64) + "px";
        b.style.display = "block";
        document.body.style.paddingTop = ((topBar ? topBar.offsetHeight : 64) + b.offsetHeight + 6) + "px";
      }
    }
  }catch(e){}
})();
</script>
"""

def _inject_banner(html):
    if "<body" in html.lower():
        idx = html.lower().find("<body")
        end = html.find(">", idx)
        if end != -1:
            return html[:end+1] + PRINT_BANNER + html[end+1:]
    return PRINT_BANNER + html

def html_to_pdf(html, session, base_url=BASE):
    """Render the print-ready HTML to a real PDF using the print stylesheet.
    Images (incl. session-protected ones) are fetched via the student's session."""
    from weasyprint import HTML, default_url_fetcher
    def fetcher(url):
        try:
            if "sdmis.nios.ac.in" in url:
                r = session.get(url, headers=HEADERS, timeout=30)
                return {"string": r.content,
                        "mime_type": r.headers.get("Content-Type", "application/octet-stream").split(";")[0]}
        except Exception:
            pass
        return default_url_fetcher(url)
    return HTML(string=html, base_url=base_url, url_fetcher=fetcher).write_pdf()

def fetch_document(reference_no, dob, kind, enrollment_no=""):
    """Login as student & return the document. Logs in with enrollment_no when given
    (SYC students), else reference_no.
    Returns (bytes, content_type, filename) or (None, error, None)."""
    if not CAPSOLVER_API_KEY:
        return None, "CAPTCHA_API_KEY not set", None
    path = DOC_URLS.get(kind)
    if not path:
        return None, "invalid document kind", None
    ident = reference_no or enrollment_no or "doc"
    target = urljoin(BASE, path)
    dob_str = format_dob(dob)
    cache_key = ("enr:" + enrollment_no) if enrollment_no else reference_no
    last_err = None
    # Try twice: NIOS reCAPTCHA v3 is score-based and can intermittently reject an
    # automated login even when the details are correct. On failure we drop the
    # cached session and retry once with a fresh CSRF + captcha.
    for attempt in range(2):
        session = get_logged_in_session(reference_no, dob, enrollment_no=enrollment_no,
                                        force=(attempt == 1))
        if session is None:
            last_err = (f"login failed — DOB used was '{dob_str}'. "
                        f"Verify it matches NIOS records.")
            continue   # retry with a fresh login
        try:
            r = session.get(target, headers=HEADERS, timeout=45)
        except Exception as e:
            last_err = f"fetch error: {e}"
            continue
        ct = r.headers.get("Content-Type", "").lower()
        # Already a PDF? serve directly
        if "pdf" in ct or r.content[:4] == b"%PDF":
            return r.content, "application/pdf", f"{kind}_{ident}.pdf"
        # If NIOS bounced us back to the login page (session not valid / captcha
        # rejected), drop the session and retry once before giving up.
        low = r.text.lower()
        if ("login to your account" in low or 'loginform[' in low
                or ("username / email" in low and "reset password" in low)):
            last_err = (f"NIOS rejected the login — DOB used was '{dob_str}'. "
                        f"Please verify this DOB matches NIOS records, then Run Now.")
            _session_cache.pop(cache_key, None)
            continue
        # Print-ready HTML -> render a real PDF (best: print layout + embedded images)
        if "html" in ct:
            try:
                pdf = html_to_pdf(r.text, session)
                if pdf and pdf[:4] == b"%PDF":
                    return pdf, "application/pdf", f"{kind}_{ident}.pdf"
            except Exception as e:
                logger.warning(f"PDF render failed for {kind}, falling back to HTML: {e}")
            # Fallback: inline images + Save-as-PDF banner
            html = inline_resources(r.text, session)
            html = _inject_banner(html)
            return html.encode("utf-8"), "text/html; charset=utf-8", f"{kind}_{ident}.html"
        last_err = f"unexpected content ({ct or 'unknown'})"
    return None, last_err or "login failed", None

def fetch_id_card_html(reference_no, dob):
    """Return the raw ID-card page HTML (logged in as the student), or ''. """
    session = get_logged_in_session(reference_no, dob)
    if session is None:
        return ""
    try:
        r = session.get(urljoin(BASE, DOC_URLS["id_card"]), headers=HEADERS, timeout=45)
        return r.text or ""
    except Exception as e:
        logger.warning(f"id-card fetch failed: {e}")
        return ""

def _address_from_text(text):
    """Pull the Regional Centre address from the ID-card's visible text.
    NIOS card structure:
        Regional Centre: CHANDIGARH
        YMCA Complex, Sector-11C, Chandigarh - 160011
    Returns a clean single-line address ending at the 6-digit PIN code."""
    if not text:
        return ""
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    # Primary: explicit "Regional Centre" label (NOT "Study Centre")
    for i, l in enumerate(lines):
        m = re.search(r"regional\s*cent(?:re|er)\s*[:\-]?\s*(.*)$", l, re.I)
        if not m:
            continue
        head = m.group(1).strip(" :-")
        parts = [head] if head else []
        if not re.search(r"\d{6}", head):           # address continues on next line(s)
            for nxt in lines[i + 1:i + 6]:
                if re.match(r"(?i)^note\b", nxt):    # stop at the Note section
                    break
                parts.append(nxt)
                if re.search(r"\d{6}", nxt):
                    break
        full = ", ".join(p for p in parts if p)
        full = re.sub(r"\s*,\s*,\s*", ", ", full).strip(" ,")
        if full:
            return full if full.lower().startswith("nios") else ("NIOS Regional Centre " + full)
    # Fallback: any block ending in a 6-digit PIN (skip the student's own "Address")
    for i, l in enumerate(lines):
        if re.search(r"\b\d{6}\b", l) and len(l) > 12 and "regional" not in lines[max(0, i - 1)].lower():
            start = max(0, i - 2)
            return ", ".join(lines[start:i + 1]).strip(" ,")
    return ""

def extract_regional_address(html):
    """Best-effort Regional Centre address from the ID card HTML."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return _address_from_text(soup.get_text("\n"))

def fetch_regional_address(reference_no, dob):
    return extract_regional_address(fetch_id_card_html(reference_no, dob))

def debug_idcard_text(reference_no, dob):
    """Return the ID card's visible text + best-effort extracted address (for tuning)."""
    if not CAPSOLVER_API_KEY:
        return {"error": "CAPTCHA_API_KEY not set"}
    html = fetch_id_card_html(reference_no, dob)
    if not html:
        return {"error": "login failed or empty id card"}
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\n\s*\n+", "\n", soup.get_text("\n")).strip()
    return {
        "extracted_address": extract_regional_address(html),
        "id_card_text": text[:4000],
    }

def probe_links(session, classified):
    """Fetch each classified link to report content-type/size (debug)."""
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

def debug_doc(reference_no, dob, kind):
    """Inspect a document page's HTML structure to find how the PDF is served."""
    if not CAPSOLVER_API_KEY:
        return {"error": "CAPTCHA_API_KEY not set"}
    path = DOC_URLS.get(kind)
    if not path:
        return {"error": "invalid kind"}
    session = get_logged_in_session(reference_no, dob)
    if session is None:
        return {"error": "login failed (check reference/DOB)"}
    target = urljoin(BASE, path)
    try:
        r = session.get(target, headers=HEADERS, timeout=45)
    except Exception as e:
        return {"error": f"fetch error: {e}"}
    ct = r.headers.get("Content-Type", "")
    soup = BeautifulSoup(r.text, "html.parser")
    iframes = [el.get("src") for el in soup.find_all(["iframe", "embed"]) if el.get("src")]
    objects = [el.get("data") for el in soup.find_all("object") if el.get("data")]
    forms = []
    for f in soup.find_all("form"):
        forms.append({
            "action": f.get("action"), "method": (f.get("method") or "get").lower(),
            "inputs": [{"name": i.get("name"), "value": (i.get("value") or "")[:30]}
                       for i in f.find_all(["input", "button"]) if i.get("name")],
        })
    links = [{"text": a.get_text(" ", strip=True)[:35], "href": a["href"]}
             for a in soup.find_all("a", href=True)
             if not a["href"].lower().startswith("javascript")][:40]
    scripts = [s.get("src") for s in soup.find_all("script") if s.get("src")][:15]
    inline = " ".join(s.get_text() for s in soup.find_all("script") if not s.get("src"))
    hints = re.findall(r'["\']([^"\']*(?:pdf|print|download|id.?card|hall|app|form|registration)[^"\']*)["\']',
                       inline, re.I)

    # --- NEW: surface every <img> with its sizing/identity attributes ---
    images = []
    for im in soup.find_all("img"):
        src = im.get("src") or ""
        if src.startswith("data:"):
            src = src[:40] + "...(base64)"
        images.append({
            "src": src[:120],
            "width": im.get("width"), "height": im.get("height"),
            "class": " ".join(im.get("class") or []),
            "id": im.get("id"),
            "style": (im.get("style") or "")[:120],
            "alt": (im.get("alt") or "")[:30],
        })

    # --- NEW: stylesheets (note any media="print") + inline <style> blocks ---
    stylesheets = [{"href": l.get("href"), "media": l.get("media") or "all"}
                   for l in soup.find_all("link", rel="stylesheet")]
    style_blocks = []
    for st in soup.find_all("style"):
        txt = re.sub(r"\s+", " ", st.get_text()).strip()
        style_blocks.append({"media": st.get("media") or "all", "css": txt[:600]})

    body = soup.find("body")
    body_class = " ".join(body.get("class") or []) if body else None

    # body-only preview (skips the long <head>/favicon block)
    body_preview = re.sub(r"\s+", " ", body.decode_contents()).strip()[:2500] if body else ""

    return {
        "content_type": ct,
        "status": r.status_code,
        "iframes_embeds": iframes,
        "objects": objects,
        "forms": forms,
        "links": links,
        "script_srcs": scripts,
        "url_hints_in_js": sorted(set(h for h in hints if len(h) > 3))[:25],
        "images": images,
        "stylesheets": stylesheets,
        "style_blocks": style_blocks,
        "body_class": body_class,
        "body_preview": body_preview,
    }
