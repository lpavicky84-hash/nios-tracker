"""
Examination-fee checker for the NIOS Admissions Tracker.

For a confirmed student it logs into the NIOS student portal (Reference No + Date of Birth — the
SAME login the document fetch already uses), opens Payment History, finds the EXAM_FEE payment and
reads its status (PAID / not paid), and for a PAID fee it opens the official receipt and renders it
as BOTH a PNG image and a PDF so the team can download either from the tracker.

Design choices that keep this robust on the live server:
  * Re-uses nios_login.get_logged_in_session() — no new login code, no new captcha path.
  * The receipt is rebuilt with Pillow (already a dependency) from the REAL data on the NIOS receipt
    page, so it never depends on a browser / weasyprint / system libraries that may not be installed.
  * Every network step is wrapped so one bad student can never crash a whole run.
"""
import io
import re
import logging

logging.getLogger(__name__)
logger = logging.getLogger(__name__)

try:
    import nios_login
    from nios_login import BASE, HEADERS, get_logged_in_session, format_dob, _LAST_FAIL_REASON  # noqa
except Exception:                      # pragma: no cover - only if imported standalone
    nios_login = None
    BASE = "https://sdmis.nios.ac.in"
    HEADERS = {}

PAYMENT_HISTORY_URL = BASE + "/home/payment-history"
RECEIPT_URL = BASE + "/home/payment-receipt?transactionId="

# ----------------------------------------------------------------------------- parsing

def _text(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""

def parse_payment_history(html):
    """Pull every EXAM_FEE payment out of the Payment History page.

    The page lists each payment as a PAIR of rows: a header row (Ref No / Module / Print link) then
    a detail row (Date / Amount / Status). Returns a list of dicts, newest-first is NOT guaranteed
    so the caller decides; each dict: {module, txn_id, date, amount, status, receipt_path}."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    rows = soup.find_all("tr")
    out = []
    i = 0
    while i < len(rows):
        cells = rows[i].find_all("td")
        joined = " ".join(_text(c) for c in cells).lower()
        if "module" in joined:
            module = ""
            txn_id = ""
            receipt_path = ""
            for c in cells:
                ct = _text(c)
                if ct.lower().startswith("module"):
                    module = re.sub(r"(?i)^module\s*:?\s*", "", ct).strip()
                a = c.find("a", href=True)
                if a and "payment-receipt" in a["href"]:
                    receipt_path = a["href"]
                    m = re.search(r"transactionId=([^&]+)", a["href"])
                    if m:
                        txn_id = m.group(1)
            date = amount = status = ""
            if i + 1 < len(rows):
                for c in rows[i + 1].find_all("td"):
                    ct = _text(c)
                    low = ct.lower()
                    if low.startswith("date"):
                        date = re.sub(r"(?i)^date\s*:?\s*", "", ct).strip()
                    elif low.startswith("amount"):
                        amount = re.sub(r"(?i)^amount\s*:?\s*", "", ct).strip()
                    elif low.startswith("status"):
                        status = re.sub(r"(?i)^status\s*:?\s*", "", ct).strip()
            if "exam" in module.lower():      # EXAM_FEE / EXAM FEE / Examination fee etc.
                out.append({"module": module, "txn_id": txn_id, "date": date,
                            "amount": amount, "status": status, "receipt_path": receipt_path})
            i += 2
            continue
        i += 1
    return out

def parse_receipt_fields(html):
    """Read the Online Payment Receipt page into an ordered list of (label, value) pairs — robust to
    the exact label set, since we just take every two-cell row of its details table."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    pairs = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all(["td", "th"])
        if len(tds) == 2:
            label = _text(tds[0])
            value = _text(tds[1])
            if label and value and len(label) < 60:
                pairs.append((label.rstrip(":").strip(), value.strip()))
    return pairs

# ----------------------------------------------------------------------------- rendering

_FONT_CACHE = {}

def _font(size, bold=False):
    from PIL import ImageFont
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    cands = ([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ] if bold else [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ])
    f = None
    for p in cands:
        try:
            f = ImageFont.truetype(p, size)
            break
        except Exception:
            continue
    if f is None:
        try:
            f = ImageFont.load_default()
        except Exception:
            f = None
    _FONT_CACHE[key] = f
    return f

def _clean(v):
    # keep text drawable on any font: the rupee sign is not in every TTF, so show "Rs."
    return (v or "").replace("₹", "Rs. ").replace("\xa0", " ").strip()

def render_receipt(pairs, student_name="", logo_bytes=None, status="PAID"):
    """Draw a clean, official-looking NIOS payment receipt from the real receipt data and return
    (png_bytes, pdf_bytes). Pillow only — no browser, no system renderer."""
    from PIL import Image, ImageDraw
    W = 1000
    pad = 48
    # ---- measure needed height first
    row_h = 46
    n_rows = max(1, len(pairs))
    header_h = 210
    title_h = 70
    thanks_h = 70
    footer_h = 150
    H = header_h + title_h + thanks_h + n_rows * row_h + footer_h + pad
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    blue = (33, 95, 160)
    headbar = (108, 170, 220)
    dark = (30, 30, 30)
    grey = (90, 90, 90)
    line = (200, 200, 200)

    y = pad
    # ---- logo (fetched from NIOS) or text header
    logo_drawn = False
    if logo_bytes:
        try:
            lg = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
            lw = 360
            lh = int(lg.height * (lw / lg.width))
            lg = lg.resize((lw, lh))
            img.paste(lg, ((W - lw) // 2, y), lg)
            y += lh + 10
            logo_drawn = True
        except Exception:
            logo_drawn = False
    if not logo_drawn:
        d.text((W // 2, y + 14), "National Institute of Open Schooling",
               font=_font(30, True), fill=blue, anchor="mm")
        d.text((W // 2, y + 54), "(An autonomous institution under Ministry of Education, Govt. of India)",
               font=_font(16), fill=grey, anchor="mm")
        y += 100

    # ---- title bar
    d.rectangle([pad, y, W - pad, y + 54], fill=headbar)
    d.text((W // 2, y + 27), "Online Payment Receipt", font=_font(24, True), fill="white", anchor="mm")
    y += 54 + 22

    # ---- thank-you line
    d.text((W // 2, y), "Thank You. Please quote this transaction reference number for any future query.",
           font=_font(16), fill=dark, anchor="mm")
    y += 28
    d.text((W // 2, y), "Transaction Details", font=_font(18, True), fill=dark, anchor="mm")
    y += 26

    # ---- table
    left = pad
    right = W - pad
    split = pad + 360
    for (label, value) in pairs:
        d.rectangle([left, y, right, y + row_h], outline=line, width=1)
        d.line([split, y, split, y + row_h], fill=line, width=1)
        d.text((left + 16, y + row_h // 2), _clean(label), font=_font(17, True), fill=dark, anchor="lm")
        d.text((split + 16, y + row_h // 2), _clean(value), font=_font(17), fill=dark, anchor="lm")
        y += row_h
    y += 30

    # ---- footer
    d.text((left, y), "Best Regards,", font=_font(16), fill=dark)
    y += 26
    d.text((left, y), "National Institute of Open Schooling (NIOS)", font=_font(16, True), fill=dark)
    y += 30
    d.text((left, y), "For further assistance, call 1800-180-9393 for general queries.",
           font=_font(15), fill=grey)

    png_buf = io.BytesIO()
    img.save(png_buf, "PNG")
    pdf_buf = io.BytesIO()
    img.convert("RGB").save(pdf_buf, "PDF", resolution=100.0)
    return png_buf.getvalue(), pdf_buf.getvalue()

# ----------------------------------------------------------------------------- main entry

_LOGO_CACHE = {"bytes": None, "tried": False}

def _get_logo(session):
    if _LOGO_CACHE["tried"]:
        return _LOGO_CACHE["bytes"]
    _LOGO_CACHE["tried"] = True
    for url in ("https://dq4kzxd7fbbni.cloudfront.net/static/dist/pdf/images/NIOSlogo_MoE_without_ISO_2025.png",
                "https://dq4kzxd7fbbni.cloudfront.net/static/dist/images/favicon/android-icon-192x192.png"):
        try:
            r = session.get(url, headers=HEADERS, timeout=20)
            if r.ok and r.content[:4] == b"\x89PNG":
                _LOGO_CACHE["bytes"] = r.content
                return r.content
        except Exception:
            continue
    return None

def check_exam_fee(reference_no, dob, enrollment_no=""):
    """Check a student's examination fee on the NIOS portal and, if PAID, build the receipt.

    Returns a dict:
      status  : 'paid' | 'failed' | 'pending' | 'login_failed' | 'error'
      amount, date, txn_id, remark
      png, pdf : receipt bytes (only when status == 'paid'), else None
    'pending' = no EXAM_FEE payment is on the portal yet. 'failed' = an EXAM_FEE payment exists but
    its status is not PAID. 'login_failed' = NIOS login did not go through (wrong Reference/DOB or the
    captcha service was busy) — a transient/data issue the operator can fix, never a wrong 'pending'."""
    res = {"status": "error", "amount": "", "date": "", "txn_id": "", "remark": "",
           "png": None, "pdf": None}
    if nios_login is None:
        res["remark"] = "login module unavailable"
        return res
    ref = (reference_no or "").strip()
    if not ref or not (dob or "").strip():
        res["status"] = "login_failed"
        res["remark"] = "reference number or date of birth missing"
        return res
    try:
        session = None
        for attempt in range(2):
            session = get_logged_in_session(ref, dob, enrollment_no="", force=(attempt >= 1))
            if session is not None:
                break
        if session is None:
            reason = getattr(nios_login, "_LAST_FAIL_REASON", "")
            res["status"] = "login_failed"
            res["remark"] = ("captcha/login service busy — try again" if reason == "captcha"
                             else f"NIOS login failed — check Reference No / DOB (used '{format_dob(dob)}')")
            return res
        r = session.get(PAYMENT_HISTORY_URL, headers=HEADERS, timeout=45)
        low = (r.text or "").lower()
        if ("login to your account" in low or "username / email" in low) and "payment history" not in low:
            res["status"] = "login_failed"
            res["remark"] = "NIOS bounced back to login — verify Reference No / DOB"
            return res
        fees = parse_payment_history(r.text)
        if not fees:
            res["status"] = "pending"
            res["remark"] = "no examination-fee payment found on the portal yet"
            return res
        paid = next((f for f in fees if "paid" in (f["status"] or "").lower()), None)
        chosen = paid or fees[-1]
        res["amount"] = chosen.get("amount", "")
        res["date"] = chosen.get("date", "")
        res["txn_id"] = chosen.get("txn_id", "")
        if not paid:
            res["status"] = "failed"
            res["remark"] = f"examination fee present but not paid (status: {chosen.get('status','?')})"
            return res
        # PAID -> fetch + render the receipt
        res["status"] = "paid"
        txn = chosen.get("txn_id", "")
        receipt_html = ""
        try:
            rp = chosen.get("receipt_path") or (RECEIPT_URL + txn)
            if rp.startswith("/"):
                rp = BASE + rp
            rr = session.get(rp, headers=HEADERS, timeout=45)
            receipt_html = rr.text or ""
        except Exception as e:
            res["remark"] = f"paid, but receipt page failed: {e}"
        pairs = parse_receipt_fields(receipt_html) if receipt_html else []
        if not pairs:
            # Fall back to the data we already have so a receipt is still produced.
            pairs = [("Reference Number", ref),
                     ("Module", chosen.get("module", "EXAM_FEE")),
                     ("Transaction Reference Number", txn),
                     ("Transaction Date", chosen.get("date", "")),
                     ("Pay Status", "PAID"),
                     ("Amount Paid (Rs.)", chosen.get("amount", ""))]
        try:
            logo = _get_logo(session)
            png, pdf = render_receipt(pairs, logo_bytes=logo, status="PAID")
            res["png"], res["pdf"] = png, pdf
        except Exception as e:
            res["remark"] = f"paid, but receipt render failed: {e}"
            logger.warning(f"exam_fee render failed {ref}: {e}")
        return res
    except Exception as e:
        res["status"] = "error"
        res["remark"] = f"error: {str(e)[:150]}"
        logger.warning(f"check_exam_fee error {ref}: {e}")
        return res
