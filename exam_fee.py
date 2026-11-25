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

# --- Bundled amount font ----------------------------------------------------------------------
# The rupee sign (U+20B9) is missing from Arial/Liberation and is NOT guaranteed to exist in any
# system font on the server (that is why it showed as a box). So a tiny DejaVu subset that DOES
# contain the rupee sign and the digits is embedded here as base64 and used for every value that
# has a rupee sign. This makes the amount render correctly on ANY server, with no font installed.
_AMT_FONT_B64 = (
    "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"
    "LwsFMA0DLwsDLi0JBS4QAy0JAywyAysqJQUrZAMqKRIFKiUDKRIDKCclBShBAyclAyYlCwUmDwMlCwMk/gMj/gMiDwMhARAFIRID"
    "IGQDH/oDHh0NBR5kAx0NAxwRQgUc/gMb+gMaQgMZEUIFGf4DGGQDFxYZBRf+AxYBEAUWGQMV/gMU/gMT/gMSEUIFEv4DEQItBRFC"
    "AxB9Aw9kAw7+Aw0MFgUN/gMMARAFDBYDC/4DChADCf4DCAItBQj+AwcUAwZkAwQBEAUE/gNAFQMCLQUD/gMCARAFAi0DARADAP4D"
    "AbgBZIWNASsrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr"
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr"
    "KysrKysrKysrKysrKysrKysrKysrKwArKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr"
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysrKysr"
    "KysrKysrKysrKysrKysrKysrKysrKysrKysrKysrHQE1ALgAywDLAMEAqgCcAaYAuABmAAAAcQDLAKACsgCFAHUAuADDAcsBiQIt"
    "AMsApgDwANMAqgCHAMsDqgQAAUoAMwDLAAAA2QUCAPQBVAC0AJwBOQEUATkHBgQABE4EtARSBLgE5wTNADcEcwTNBGAEcwEzA6IF"
    "VgWmBVYFOQPFAhIAyQAfALgB3wBzALoD6QMzA7wERAQOAN8DzQOqAOUDqgQEAAAAywCPAKQAewC4ABQBbwB/AnsCUgCPAMcFzQCa"
    "AJoAbwDLAM0BngHTAPAAugGDANUAmAMEAkgAngHVAMEAywD2AIMDVAJ/AAADMwJmANMAxwCkAM0AjwCaAHMEAAXVAQoA/gIrAKQA"
    "tACcAAAAYgCcAAAAHQMtBdUF1QXVBfAAfwB7AFQApAa4BhQHIwHTALgAywCmAcMB7AaTAKAA0wNcA3ED2wGFBCMEqARIAI8BOQEU"
    "ATkDYACPBdUBmgYUByMGZgF5BGAEYARgBHsAnAAAAncEYAGqAOkEYAdiAHsAxQB/AnsAAAC0AlIFzQBmALwAZgB3BhAAzQE7AYUD"
    "iQCPAHsAAAAdAM0HSgQvAJwAnAAAB30AbwAAAG8DNQBqAG8AewCuALIALQOWAI8CewD2AIMDVAY3BfYAjwCcBOECZgCPAY0C9gDN"
    "A0QAKQBmBO4AcwAAFAAAlgAAAAAAAAAGAAAAAwAAAAAAAP/YAFoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIACAAC//8AAwABAAAA"
    "DAAAAAAAAAACAAEAAQAcAAEAAAABAAAACgAcAB4AAURGTFQACAAEAAAAAP//AAAAAAAAAAEAAAAKAJIAlAAUREZMVAB6YXJhYgCE"
    "YXJtbgCEYnJhaQCEY2FucwCEY2hlcgCEY3lybACEZ2VvcgCEZ3JlawCEaGFuaQCEaGVicgCEa2FuYQCEbGFvIACEbGF0bgCEbWF0"
    "aACEbmtvIACEb2dhbQCEcnVucgCEdGZuZwCEdGhhaQCEAAQAAAAA//8AAAAAAAAAAAAAAAAAAQAAAAoA4ADoAFAAPAwAB90AAAAA"
    "AoIAAARgAAAF1QAAAAAAAARgAAAAAAAAAAAAAAAAAAAEYAAAAAAAAAFoAAAEYAAAAFUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAQ4AAAJ2AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABaAAABDgAAAFoAAABaAAABDgAAAAAAAAAA"
    "AAABDgAAAFoAAABaAAABDgAAAFoAAABaAAAAWgAAAXIAAABaAAAAWgAAAjgAAPuPAAAAPAAAAAAAAAAAACgADgAWAAIAAAAaAEIA"
    "AQACAAIAAwABAAAABAAAAAAAAAADABkAAAAoCXUAAAAYACgAKAmNAAEAFwAoAAAJlgAAAAQAAAAAAAAAAwAcAAAAKAl1AAAAGwAo"
    "ACgJjQABABoAKAAACZYAAAAA"
)
_AMT_FONT_BYTES = None
_AMT_FONT_CACHE = {}
def _amount_font(size):
    """A font guaranteed to have the rupee sign, loaded from the bundled subset above."""
    global _AMT_FONT_BYTES
    from PIL import ImageFont
    if size in _AMT_FONT_CACHE:
        return _AMT_FONT_CACHE[size]
    f = None
    try:
        if _AMT_FONT_BYTES is None:
            import base64 as _b64
            _AMT_FONT_BYTES = _b64.b64decode(_AMT_FONT_B64)
        f = ImageFont.truetype(io.BytesIO(_AMT_FONT_BYTES), size)
    except Exception:
        # last resort: a system DejaVu if the bundled font somehow fails to load
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",):
            try:
                f = ImageFont.truetype(p, size); break
            except Exception:
                continue
    _AMT_FONT_CACHE[size] = f
    return f

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
    """Reproduce the NIOS 'Online Payment Receipt' EXACTLY as it looks on the NIOS portal — the real
    NIOS logo, the #56bdea blue heading bar with 'Online Payment Receipt' in bold black, the centred
    thank-you line, the black-bordered details table and the footer, all in Arial at NIOS's own point
    sizes (its wording and typos kept). The image is cropped tight to the receipt (not a blank A4) and
    rendered at 2x for a crisp, premium result. The rupee amount uses a bundled font so it ALWAYS
    shows the rupee sign, never a box, on any server. Returns (png_bytes, pdf_bytes). Pillow only, so
    it is 100% reliable on the server (no browser / weasyprint / system libraries)."""
    from PIL import Image, ImageDraw
    SC = 2                              # 2x the NIOS CSS pixels -> crisp output
    PXMM = 96.0 / 25.4 * SC            # CSS px per millimetre, scaled (7.559)
    def mm(v):
        return int(round(v * PXMM))
    def pt(p):                         # NIOS point size -> device px
        return max(1, int(round(p * 96.0 / 72.0 * SC)))
    CW = 740 * SC                      # NIOS container width (740px) at 2x
    OUT = mm(5)                        # small white margin around the receipt
    W = CW + 2 * OUT
    CL = OUT
    CR = OUT + CW
    DARK = (51, 51, 51)                # #333 (NIOS body text colour)
    BLACK = (0, 0, 0)
    BLUE = (86, 189, 234)              # #56bdea (exact NIOS heading-bar colour)

    f_title = _font(pt(11), True)      # "Online Payment Receipt" : 11pt bold
    f_thanks = _font(pt(11), False)
    f_td = _font(pt(11), True)         # "Transaction Details"
    f_lbl = _font(pt(10), True)        # table label : 10pt bold
    f_val = _font(pt(10), False)       # table value : 10pt
    f_amt = _amount_font(pt(10))       # bundled font for ₹ values (always has the rupee sign)
    f_foot = _font(pt(11), False)
    f_foot_b = _font(pt(11), True)

    scratch = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    def th(font, text="Hg"):
        b = scratch.textbbox((0, 0), text, font=font)
        return b[3] - b[1]

    # ---- logo (the real NIOS receipt logo), 400px of the 740px container = 54%, centred
    logo_im = None
    logo_w = int(CW * (400.0 / 740.0))
    logo_h = 0
    if logo_bytes:
        try:
            logo_im = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
            logo_h = int(logo_im.height * (logo_w / logo_im.width))
            logo_im = logo_im.resize((logo_w, logo_h))
        except Exception:
            logo_im = None
    if logo_im is None:
        logo_h = pt(20)

    pad_cell = mm(2)                               # NIOS cell padding = 2mm
    row_h = th(f_lbl) + 2 * pad_cell + mm(1)
    bw = max(2, int(round(0.75 * 96.0 / 72.0 * SC)))   # NIOS border 0.75pt
    bar_h = th(f_title) + 2 * mm(2)                # blue bar: text + 2mm top/bottom

    # ---- measure the content block height (crop tight to the receipt)
    y = mm(3)
    y += logo_h + mm(5)
    y += bar_h + mm(7)
    y += th(f_thanks) + mm(2) + th(f_td) + mm(7)
    y += row_h * max(1, len(pairs))
    y += mm(12)
    y += th(f_foot) + mm(3) + th(f_foot_b) + mm(3) + th(f_foot)
    H = y + mm(6)

    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    cy = mm(3)
    if logo_im is not None:
        img.paste(logo_im, (CL + (CW - logo_w) // 2, cy), logo_im)
        cy += logo_h + mm(5)
    else:
        d.text((W // 2, cy), "National Institute of Open Schooling",
               font=f_td, fill=(40, 40, 120), anchor="ma")
        cy += logo_h + mm(5)

    # ---- blue heading bar (full container width) with bold black text, exactly like the portal
    d.rectangle([CL, cy, CR, cy + bar_h], fill=BLUE)
    d.text(((CL + CR) // 2, cy + bar_h // 2), "Online Payment Receipt",
           font=f_title, fill=BLACK, anchor="mm")
    cy += bar_h + mm(7)

    # ---- thank-you + Transaction Details (centred, NIOS's exact wording)
    d.text((W // 2, cy),
           "Thank You Please Quote this transaction reference number for any queries in future related to this transaction",
           font=f_thanks, fill=DARK, anchor="ma")
    cy += th(f_thanks) + mm(2)
    d.text((W // 2, cy), "Transaction Details", font=f_td, fill=DARK, anchor="ma")
    cy += th(f_td) + mm(7)

    # ---- bordered details table: 80% of container width, centred, bold label (60%) | value (40%)
    tw = int(CW * 0.8)
    tx0 = CL + (CW - tw) // 2
    tx1 = tx0 + tw
    split = tx0 + int(tw * 0.6)
    ty = cy
    for (label, value) in pairs:
        d.rectangle([tx0, ty, tx1, ty + row_h], outline=BLACK, width=bw)
        d.line([split, ty, split, ty + row_h], fill=BLACK, width=bw)
        d.text((tx0 + pad_cell, ty + row_h // 2), _clean(label), font=f_lbl, fill=DARK, anchor="lm")
        vf = f_amt if "₹" in (value or "") else f_val
        d.text((split + pad_cell, ty + row_h // 2), _clean(value), font=vf, fill=DARK, anchor="lm")
        ty += row_h
    cy = ty + mm(12)

    # ---- footer (left-aligned at the container left, NIOS's exact text incl. its own spelling)
    d.text((CL, cy), "Best Regards.", font=f_foot, fill=DARK)
    cy += th(f_foot) + mm(3)
    d.text((CL, cy), "National Institute of Open Schooing (NIOS)", font=f_foot_b, fill=DARK)
    cy += th(f_foot_b) + mm(3)
    d.text((CL, cy), "For futher assistance. call 1800-180-9393 for general queries.",
           font=f_foot, fill=DARK)

    png_buf = io.BytesIO()
    img.save(png_buf, "PNG")
    pdf_buf = io.BytesIO()
    img.convert("RGB").save(pdf_buf, "PDF", resolution=192.0)
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
           "png": None, "pdf": None, "html": None}
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
        # --- THE REAL NIOS RECEIPT PAGE (same as the ID card / application form) ------------------
        # Inline the receipt page's own logo/CSS and add the Save-as-PDF / Print banner, exactly like
        # a document. Opening this shows the genuine NIOS receipt (identical to the portal) and the
        # Print button makes a perfect PDF that never breaks.
        if receipt_html:
            try:
                html_doc = nios_login.inline_resources(receipt_html, session)
                html_doc = nios_login._inject_banner(html_doc)
                res["html"] = html_doc.encode("utf-8")
            except Exception as e:
                logger.warning(f"exam_fee inline receipt failed {ref}: {e}")
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
