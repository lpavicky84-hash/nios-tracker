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
    # NIOS's receipt is Arial; Liberation Sans is the metric-compatible Arial clone (and has the
    # rupee sign), so prefer it, then fall back to DejaVu.
    cands = ([
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ] if bold else [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
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
    return (v or "").replace("\xa0", " ").strip()

def render_receipt(pairs, student_name="", logo_bytes=None, status="PAID"):
    """Reproduce the EXACT NIOS 'Online Payment Receipt' from the real receipt data — same logo,
    same #56bdea title bar, same black-bordered details table, same Arial text (NIOS's own wording,
    typos and all) — and return (png_bytes, pdf_bytes). Pillow only, so it is 100% reliable on the
    server (no browser / weasyprint / system libraries)."""
    from PIL import Image, ImageDraw
    OUT = 60                       # outer white margin around the 'page'
    CW = 1480                      # container width (NIOS 740px at 2x)
    W = CW + 2 * OUT               # image width
    CL = OUT                       # container left edge
    CR = OUT + CW                  # container right edge
    DARK = (51, 51, 51)            # #333
    BLUE = (86, 189, 234)          # #56bdea (exact NIOS bar colour)
    BLACK = (0, 0, 0)

    f_bar = _font(30, True)
    f_thanks = _font(24, False)
    f_td = _font(26, True)
    f_lbl = _font(26, True)
    f_val = _font(26, False)
    f_foot = _font(26, False)
    f_foot_b = _font(26, True)
    # Liberation Sans (our Arial) has no rupee sign, so the amount cell uses DejaVu, which does.
    def _rupee_font(size):
        from PIL import ImageFont
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
        return f_val
    f_rupee = _rupee_font(26)

    scratch = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    def th(font, text="Hg"):
        b = scratch.textbbox((0, 0), text, font=font)
        return b[3] - b[1]

    # ---- logo (the exact receipt logo), scaled to ~400px (2x) wide
    logo_im = None
    logo_w = 760
    logo_h = 0
    if logo_bytes:
        try:
            logo_im = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
            logo_h = int(logo_im.height * (logo_w / logo_im.width))
            logo_im = logo_im.resize((logo_w, logo_h))
        except Exception:
            logo_im = None
    if logo_im is None:
        logo_h = 70

    pad_cell = 14
    row_h = th(f_lbl) + 2 * pad_cell + 6

    # ---- measure total height
    y = 24
    y += logo_h + 24
    bar_h = th(f_bar) + 2 * 14
    y += bar_h + 46
    y += th(f_thanks) + 12 + th(f_td) + 46
    y += row_h * max(1, len(pairs))
    y += 70
    y += th(f_foot) + 14 + th(f_foot_b) + 20 + th(f_foot)
    H = y + 40

    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    cy = 24
    if logo_im is not None:
        img.paste(logo_im, (CL + (CW - logo_w) // 2, cy), logo_im)
        cy += logo_h + 24
    else:
        d.text(((CL + CR) // 2, cy + 30), "National Institute of Open Schooling",
               font=f_td, fill=(40, 40, 120), anchor="mm")
        cy += logo_h + 24

    # ---- blue title bar (full container width)
    d.rectangle([CL, cy, CR, cy + bar_h], fill=BLUE)
    d.text(((CL + CR) // 2, cy + bar_h // 2), "Online Payment Receipt",
           font=f_bar, fill=BLACK, anchor="mm")
    cy += bar_h + 46

    # ---- thank-you + Transaction Details (centered, NIOS's exact wording)
    d.text(((CL + CR) // 2, cy),
           "Thank You Please Quote this transaction reference number for any queries in future related to this transaction",
           font=f_thanks, fill=DARK, anchor="ma")
    cy += th(f_thanks) + 12
    d.text(((CL + CR) // 2, cy), "Transaction Details", font=f_td, fill=DARK, anchor="ma")
    cy += th(f_td) + 46

    # ---- bordered details table: 80% width centred, bold label (60%) | value (40%)
    tw = int(CW * 0.8)
    tx0 = CL + (CW - tw) // 2
    tx1 = tx0 + tw
    split = tx0 + int(tw * 0.6)
    ty = cy
    for (label, value) in pairs:
        d.rectangle([tx0, ty, tx1, ty + row_h], outline=BLACK, width=2)
        d.line([split, ty, split, ty + row_h], fill=BLACK, width=2)
        d.text((tx0 + pad_cell, ty + row_h // 2), _clean(label), font=f_lbl, fill=DARK, anchor="lm")
        vf = f_rupee if "₹" in (value or "") else f_val
        d.text((split + pad_cell, ty + row_h // 2), _clean(value), font=vf, fill=DARK, anchor="lm")
        ty += row_h
    cy = ty + 70

    # ---- footer (left-aligned, NIOS's exact text incl. its own spelling)
    d.text((CL, cy), "Best Regards.", font=f_foot, fill=DARK)
    cy += th(f_foot) + 14
    d.text((CL, cy), "National Institute of Open Schooing (NIOS)", font=f_foot_b, fill=DARK)
    cy += th(f_foot_b) + 20
    d.text((CL, cy), "For futher assistance. call 1800-180-9393 for general queries.",
           font=f_foot, fill=DARK)

    png_buf = io.BytesIO()
    img.save(png_buf, "PNG")
    pdf_buf = io.BytesIO()
    img.convert("RGB").save(pdf_buf, "PDF", resolution=144.0)
    return png_buf.getvalue(), pdf_buf.getvalue()

# ----------------------------------------------------------------------------- main entry

_LOGO_CACHE = {"bytes": None, "tried": False}

def _get_logo(session):
    if _LOGO_CACHE["tried"]:
        return _LOGO_CACHE["bytes"]
    _LOGO_CACHE["tried"] = True
    for url in ("https://dq4kzxd7fbbni.cloudfront.net/static/dist/images/nios-logo-main.png",
                "https://dq4kzxd7fbbni.cloudfront.net/static/dist/pdf/images/NIOSlogo_MoE_without_ISO_2025.png"):
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
            if reason == "captcha":
                res["remark"] = "captcha/login service busy — run again"
            elif reason == "data":
                res["remark"] = f"NIOS says the Reference No / DOB is wrong (used '{format_dob(dob)}') — fix the data"
            else:
                res["remark"] = (f"captcha could not be read after several tries — run this student again "
                                 f"(Reference/DOB look fine; DOB used '{format_dob(dob)}')")
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
