import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Colour map: status keyword → Excel fill colour ──────────────────────────
STATUS_FILL_MAP = {
    "Pending":                              "FFF9C4",   # Light Yellow
    "Documents Verification In Progress":   "FFE0B2",   # Light Orange
    "Verified":                             "C8E6C9",   # Light Green
    "Approved":                             "B2DFDB",   # Teal Light
    "Admission Confirmed":                  "69F0AE",   # Bright Green ✅
    "Admitted":                             "BBDEFB",   # Light Blue
    "Rejected":                             "FFCDD2",   # Light Red/Pink
    "Fetch Error":                          "E0E0E0",   # Grey
    "Not Found":                            "F8BBD0",   # Pink
    "Unknown":                              "F5F5F5",   # White-ish
}

def get_fill(status_label: str) -> PatternFill:
    colour = STATUS_FILL_MAP.get(status_label, "F5F5F5")
    return PatternFill(start_color=colour, end_color=colour, fill_type="solid")

def thin_border():
    thin = Side(style="thin", color="CCCCCC")
    return Border(left=thin, right=thin, top=thin, bottom=thin)

# ── Read all reference numbers from Excel ────────────────────────────────────
def read_students_from_excel(filepath: str) -> list:
    """
    Read the Excel and return a list of dicts:
      { row_index, reference_no, student_name, class_level, ...all other cols }
    Tries to auto-detect which column has reference numbers.
    """
    wb = openpyxl.load_workbook(filepath)
    ws = wb.active

    headers = [str(ws.cell(1, c).value).strip().lower() if ws.cell(1, c).value else ""
               for c in range(1, ws.max_column + 1)]

    # Auto-detect column indices (1-based)
    ref_col   = _find_col(headers, ["reference no", "ref no", "reference number", "ref_no", "reference"])
    name_col  = _find_col(headers, ["name", "student name", "student_name"])
    class_col = _find_col(headers, ["class", "class level", "class_level", "std", "standard"])

    if ref_col is None:
        raise ValueError("Could not find 'Reference No' column in Excel. "
                         "Please ensure a column header contains 'Reference No'.")

    students = []
    for row in range(2, ws.max_row + 1):
        ref_val = ws.cell(row, ref_col).value
        if not ref_val:
            continue
        students.append({
            "row_index":     row,
            "reference_no":  str(ref_val).strip(),
            "student_name":  str(ws.cell(row, name_col).value or "").strip() if name_col else "",
            "class_level":   str(ws.cell(row, class_col).value or "").strip() if class_col else "",
        })

    wb.close()
    logger.info(f"Read {len(students)} students from Excel")
    return students

def _find_col(headers: list, keywords: list):
    for kw in keywords:
        for i, h in enumerate(headers):
            if kw in h:
                return i + 1   # 1-based
    return None

# ── Write status back to Excel ───────────────────────────────────────────────
def write_status_to_excel(filepath: str, updates: list):
    """
    updates: list of { reference_no, status_label, last_checked }
    Adds/updates 'NIOS Status', 'Last Checked', 'Last Changed' columns.
    Applies colour fill to the status cell.
    Only writes if status changed (caller passes only changed records ideally,
    but we check here too).
    """
    wb = openpyxl.load_workbook(filepath)
    ws = wb.active

    headers_raw = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    headers     = [str(h).strip().lower() if h else "" for h in headers_raw]

    ref_col = _find_col(headers, ["reference no", "ref no", "reference number", "ref_no", "reference"])

    # Ensure our output columns exist
    status_col  = _ensure_header(ws, headers, headers_raw, "NIOS Status")
    checked_col = _ensure_header(ws, headers, headers_raw, "Last Checked")
    changed_col = _ensure_header(ws, headers, headers_raw, "Last Changed")

    # Re-read headers after possible additions
    headers_raw = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    headers     = [str(h).strip().lower() if h else "" for h in headers_raw]
    status_col  = _find_col(headers, ["nios status"])
    checked_col = _find_col(headers, ["last checked"])
    changed_col = _find_col(headers, ["last changed"])

    # Build reference → update dict
    update_map = {u["reference_no"]: u for u in updates}

    # Style header row
    for c in [status_col, checked_col, changed_col]:
        cell = ws.cell(1, c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="37474F", end_color="37474F", fill_type="solid")
        cell.alignment = Alignment(horizontal="center")

    for row in range(2, ws.max_row + 1):
        ref_val = ws.cell(row, ref_col).value
        if not ref_val:
            continue
        ref_str = str(ref_val).strip()
        if ref_str not in update_map:
            continue

        upd = update_map[ref_str]
        now_str = upd.get("last_checked", datetime.now().strftime("%Y-%m-%d %H:%M"))
        status_label = upd.get("status_label", "Unknown")
        changed = upd.get("changed", False)

        # Status cell
        sc = ws.cell(row, status_col)
        sc.value = status_label
        sc.fill  = get_fill(status_label)
        sc.alignment = Alignment(horizontal="center")
        sc.border = thin_border()
        if changed:
            sc.font = Font(bold=True)   # Bold if recently changed

        # Last Checked cell
        cc = ws.cell(row, checked_col)
        cc.value = now_str
        cc.alignment = Alignment(horizontal="center")
        cc.border = thin_border()

        # Last Changed cell (only update if changed)
        if changed:
            chc = ws.cell(row, changed_col)
            chc.value = now_str
            chc.alignment = Alignment(horizontal="center")
            chc.border = thin_border()

    # Auto-fit column widths
    for c in range(1, ws.max_column + 1):
        max_len = 0
        col_letter = get_column_letter(c)
        for row in range(1, ws.max_row + 1):
            val = ws.cell(row, c).value
            if val:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[col_letter].width = min(max_len + 4, 40)

    wb.save(filepath)
    wb.close()
    logger.info(f"Excel updated: {filepath}")

def _ensure_header(ws, headers_lower, headers_raw, name: str):
    """Add column header if not exists. Returns column index (1-based)."""
    name_lower = name.lower()
    for i, h in enumerate(headers_lower):
        if h == name_lower:
            return i + 1
    # Add new column at end
    new_col = ws.max_column + 1
    ws.cell(1, new_col).value = name
    return new_col
