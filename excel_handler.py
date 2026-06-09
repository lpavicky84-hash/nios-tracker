import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Colour map: status label -> Excel fill colour
STATUS_FILL_MAP = {
    "Pending":                              "FFF9C4",
    "Documents Verification In Progress":   "FFE0B2",
    "Verified":                             "C8E6C9",
    "Approved":                             "B2DFDB",
    "Admission Confirmed":                  "69F0AE",
    "Admitted":                             "BBDEFB",
    "Rejected":                             "FFCDD2",
    "Fetch Error":                          "E0E0E0",
    "Not Found":                            "F8BBD0",
    "Unknown":                              "F5F5F5",
}

def get_fill(status_label: str) -> PatternFill:
    colour = STATUS_FILL_MAP.get(status_label, "F5F5F5")
    return PatternFill(start_color=colour, end_color=colour, fill_type="solid")

def thin_border():
    thin = Side(style="thin", color="CCCCCC")
    return Border(left=thin, right=thin, top=thin, bottom=thin)

def _clean(val):
    """Normalize header for matching: lowercase, remove dots/underscores/dashes."""
    if not val:
        return ""
    return str(val).strip().lower().replace(".", "").replace("_", " ").replace("-", " ")

def _find_col(headers_clean: list, keywords: list):
    """Find column index (1-based) by checking if any keyword is in cleaned header."""
    for kw in keywords:
        for i, h in enumerate(headers_clean):
            if kw in h:
                return i + 1
    return None

def read_students_from_excel(filepath: str) -> list:
    wb = openpyxl.load_workbook(filepath)
    ws = wb.active

    # Clean headers for robust matching
    headers_clean = [_clean(ws.cell(1, c).value) for c in range(1, ws.max_column + 1)]

    # Auto-detect columns
    ref_col   = _find_col(headers_clean, ["reference no", "ref no", "reference number", "refno", "reference"])
    name_col  = _find_col(headers_clean, ["name", "student name"])
    class_col = _find_col(headers_clean, ["class", "class level", "std", "standard"])

    logger.info(f"Detected columns — ref:{ref_col} name:{name_col} class:{class_col}")
    logger.info(f"Headers found: {headers_clean}")

    if ref_col is None:
        wb.close()
        raise ValueError(
            f"Could not find 'Reference No' column. Headers found: {headers_clean}"
        )

    students = []
    for row in range(2, ws.max_row + 1):
        ref_val = ws.cell(row, ref_col).value
        if not ref_val:
            continue
        students.append({
            "row_index":    row,
            "reference_no": str(ref_val).strip(),
            "student_name": str(ws.cell(row, name_col).value or "").strip() if name_col else "",
            "class_level":  str(ws.cell(row, class_col).value or "").strip() if class_col else "",
        })

    wb.close()
    logger.info(f"Read {len(students)} students from Excel")
    return students

def write_status_to_excel(filepath: str, updates: list):
    wb = openpyxl.load_workbook(filepath)
    ws = wb.active

    headers_raw   = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    headers_clean = [_clean(v) for v in headers_raw]

    ref_col = _find_col(headers_clean, ["reference no", "ref no", "reference number", "refno", "reference"])

    # Ensure output columns exist
    _ensure_header(ws, headers_raw, "NIOS Status")
    _ensure_header(ws, headers_raw, "Last Checked")
    _ensure_header(ws, headers_raw, "Last Changed")

    # Re-read after additions
    headers_raw   = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    headers_clean = [_clean(v) for v in headers_raw]
    status_col  = _find_col(headers_clean, ["nios status"])
    checked_col = _find_col(headers_clean, ["last checked"])
    changed_col = _find_col(headers_clean, ["last changed"])

    # Style header row for new columns
    for c in [status_col, checked_col, changed_col]:
        cell = ws.cell(1, c)
        cell.font      = Font(bold=True, color="FFFFFF")
        cell.fill      = PatternFill(start_color="37474F", end_color="37474F", fill_type="solid")
        cell.alignment = Alignment(horizontal="center")

    update_map = {u["reference_no"]: u for u in updates}

    for row in range(2, ws.max_row + 1):
        ref_val = ws.cell(row, ref_col).value
        if not ref_val:
            continue
        ref_str = str(ref_val).strip()
        if ref_str not in update_map:
            continue

        upd          = update_map[ref_str]
        now_str      = upd.get("last_checked", datetime.now().strftime("%Y-%m-%d %H:%M"))
        status_label = upd.get("status_label", "Unknown")
        changed      = upd.get("changed", False)

        sc = ws.cell(row, status_col)
        sc.value     = status_label
        sc.fill      = get_fill(status_label)
        sc.alignment = Alignment(horizontal="center")
        sc.border    = thin_border()
        if changed:
            sc.font = Font(bold=True)

        cc = ws.cell(row, checked_col)
        cc.value     = now_str
        cc.alignment = Alignment(horizontal="center")
        cc.border    = thin_border()

        if changed:
            chc = ws.cell(row, changed_col)
            chc.value     = now_str
            chc.alignment = Alignment(horizontal="center")
            chc.border    = thin_border()

    # Auto-fit widths
    for c in range(1, ws.max_column + 1):
        max_len   = 0
        col_letter = get_column_letter(c)
        for row in range(1, ws.max_row + 1):
            val = ws.cell(row, c).value
            if val:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[col_letter].width = min(max_len + 4, 40)

    wb.save(filepath)
    wb.close()
    logger.info(f"Excel updated: {filepath}")

def _ensure_header(ws, headers_raw: list, name: str):
    name_lower = name.lower()
    for i, h in enumerate(headers_raw):
        if h and str(h).strip().lower() == name_lower:
            return i + 1
    new_col = ws.max_column + 1
    ws.cell(1, new_col).value = name
    return new_col
