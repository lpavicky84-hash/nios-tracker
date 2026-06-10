import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import logging
from datetime import datetime, date

logger = logging.getLogger(__name__)

STATUS_FILL_MAP = {
    "Pending":                              "FFF9C4",
    "Documents Verification In Progress":   "FFE0B2",
    "Document Required":                    "FFCC80",
    "Verified":                             "C8E6C9",
    "Approved":                             "B2DFDB",
    "Admission Confirmed":                  "69F0AE",
    "Admitted":                             "BBDEFB",
    "Rejected":                             "FFCDD2",
    "Fetch Error":                          "E0E0E0",
    "Not Found":                            "F8BBD0",
    "Unknown":                              "F5F5F5",
}

def get_fill(label):
    colour = STATUS_FILL_MAP.get(label, "F5F5F5")
    return PatternFill(start_color=colour, end_color=colour, fill_type="solid")

def thin_border():
    s = Side(style="thin", color="CCCCCC")
    return Border(left=s, right=s, top=s, bottom=s)

def _clean(val):
    if not val:
        return ""
    return str(val).strip().lower().replace(".", "").replace("_", " ").replace("-", " ")

def _find_col(headers_clean, keywords):
    for kw in keywords:
        for i, h in enumerate(headers_clean):
            if kw in h:
                return i + 1
    return None

def read_students_from_excel(filepath):
    wb = openpyxl.load_workbook(filepath)
    ws = wb.active
    hc = [_clean(ws.cell(1, c).value) for c in range(1, ws.max_column + 1)]

    ref_col     = _find_col(hc, ["reference number", "reference no", "ref no", "refno", "reference"])
    name_col    = _find_col(hc, ["student name", "name"])
    mobile_col  = _find_col(hc, ["mobile no", "mobile", "phone no", "phone"])
    class_col   = _find_col(hc, ["class"])
    email_col   = _find_col(hc, ["email"])
    dob_col     = _find_col(hc, ["date of birth", "dob", "birth"])
    session_col = _find_col(hc, ["admission session", "session"])

    logger.info(f"Cols ref:{ref_col} name:{name_col} email:{email_col} dob:{dob_col} session:{session_col}")

    if ref_col is None and email_col is None:
        wb.close()
        raise ValueError(f"Need 'Reference Number' or 'Email' column. Found: {hc}")

    def cell(row, col):
        return str(ws.cell(row, col).value or "").strip() if col else ""

    def session_cell(row, col):
        """Format session: dates become 'Month YYYY', text stays as-is."""
        if not col:
            return ""
        v = ws.cell(row, col).value
        if isinstance(v, (datetime, date)):
            return v.strftime("%B %Y")   # e.g. April 2027
        return str(v or "").strip()

    students = []
    for row in range(2, ws.max_row + 1):
        ref   = cell(row, ref_col)
        email = cell(row, email_col)
        if not ref and not email:
            continue
        students.append({
            "row_index":    row,
            "reference_no": ref,
            "email":        email,
            "dob":          cell(row, dob_col),
            "student_name": cell(row, name_col),
            "mobile":       cell(row, mobile_col),
            "class_level":  cell(row, class_col),
            "session":      session_cell(row, session_col),
            "row_key":      ref if ref else f"email:{email}",
        })
    wb.close()
    logger.info(f"Read {len(students)} students from Excel")
    return students

def write_status_to_excel(filepath, updates):
    """updates: list of dicts with row_key/reference_no/email matching + status fields."""
    wb = openpyxl.load_workbook(filepath)
    ws = wb.active
    hr = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    hc = [_clean(v) for v in hr]

    ref_col   = _find_col(hc, ["reference number", "reference no", "ref no", "refno", "reference"])
    email_col = _find_col(hc, ["email"])

    # Ensure output columns
    status_col = _ensure(ws, "Admission Status")
    remark_col = _ensure(ws, "Remarks")
    idc_col    = _ensure(ws, "Download ID Card")
    app_col    = _ensure(ws, "Download Application Form")
    hall_col   = _ensure(ws, "Hall Ticket")
    chk_col    = _ensure(ws, "Last Checked")

    # Re-read headers
    hr = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    hc = [_clean(v) for v in hr]
    status_col = _find_col(hc, ["admission status"])
    remark_col = _find_col(hc, ["remarks", "remark"])
    idc_col    = _find_col(hc, ["download id card"])
    app_col    = _find_col(hc, ["download application form"])
    hall_col   = _find_col(hc, ["hall ticket"])
    chk_col    = _find_col(hc, ["last checked"])
    ref_col    = _find_col(hc, ["reference number", "reference no", "ref no", "refno", "reference"])
    email_col  = _find_col(hc, ["email"])

    # Build lookup by ref and by email
    by_ref   = {u["reference_no"]: u for u in updates if u.get("reference_no")}
    by_email = {u["email"]: u for u in updates if u.get("email")}

    for row in range(2, ws.max_row + 1):
        ref   = str(ws.cell(row, ref_col).value or "").strip() if ref_col else ""
        email = str(ws.cell(row, email_col).value or "").strip() if email_col else ""
        upd = by_ref.get(ref) or by_email.get(email)
        if not upd:
            continue

        now = upd.get("last_checked", datetime.now().strftime("%Y-%m-%d %H:%M"))
        label = upd.get("status_label", "Unknown")

        # If reference was discovered via email, write it back
        if upd.get("reference_no") and ref_col and not ref:
            rc = ws.cell(row, ref_col)
            rc.value = upd["reference_no"]
            rc.font = Font(bold=True, color="1565C0")

        sc = ws.cell(row, status_col)
        sc.value = label
        sc.fill = get_fill(label)
        sc.alignment = Alignment(horizontal="center")
        sc.border = thin_border()
        if upd.get("changed"):
            sc.font = Font(bold=True)

        if upd.get("remark"):
            rmc = ws.cell(row, remark_col)
            rmc.value = upd["remark"]
            rmc.alignment = Alignment(wrap_text=True)

        if upd.get("id_card_link"):
            ws.cell(row, idc_col).value = upd["id_card_link"]
        if upd.get("app_form_link"):
            ws.cell(row, app_col).value = upd["app_form_link"]
        if upd.get("hall_ticket_link"):
            ws.cell(row, hall_col).value = upd["hall_ticket_link"]

        cc = ws.cell(row, chk_col)
        cc.value = now
        cc.alignment = Alignment(horizontal="center")

    # Auto width
    for c in range(1, ws.max_column + 1):
        ml = 0
        cl = get_column_letter(c)
        for row in range(1, ws.max_row + 1):
            v = ws.cell(row, c).value
            if v:
                ml = max(ml, len(str(v)))
        ws.column_dimensions[cl].width = min(ml + 3, 45)

    wb.save(filepath)
    wb.close()
    logger.info(f"Excel updated: {filepath}")

def _ensure(ws, name):
    nl = name.lower()
    for i in range(1, ws.max_column + 1):
        h = ws.cell(1, i).value
        if h and str(h).strip().lower() == nl:
            return i
    nc = ws.max_column + 1
    cell = ws.cell(1, nc)
    cell.value = name
    cell.font = Font(bold=True, color="FFFFFF")
    cell.fill = PatternFill(start_color="37474F", end_color="37474F", fill_type="solid")
    cell.alignment = Alignment(horizontal="center")
    return nc
