import os
import logging
import requests

from excel_handler import canonicalize_session, normalize_toc

logger = logging.getLogger(__name__)

MVS_API_URL     = os.environ.get("MVS_API_URL", "").strip()
MVS_TRACKER_KEY = os.environ.get("MVS_TRACKER_KEY", "").strip()

SESSIONS = ["April", "October", "On Demand", "Stream 2", "SYC"]


def enabled():
    on = os.environ.get("MVS_MODE", "").strip().lower() in ("1", "true", "yes", "on")
    return on and bool(MVS_API_URL) and bool(MVS_TRACKER_KEY)


def _trackerlist(session=None, include_done=False, timeout=60):
    data = {"action": "trackerList", "trackerKey": MVS_TRACKER_KEY}
    if session:
        data["session"] = session
    if include_done:
        data["all"] = "1"
    r = requests.post(MVS_API_URL, data=data, timeout=timeout)
    r.raise_for_status()
    out = r.json()
    if out.get("status") != "success":
        raise RuntimeError("trackerList failed: " + str(out.get("message")))
    return out.get("students", [])


def _valid_ref(v):
    """A usable NIOS reference/enrollment: has at least one digit and is not an email.
    Portal sometimes has an email or placeholder sitting in the referenceNo field for
    pending students — that must NEVER be checked as a reference."""
    v = (v or "").strip()
    return bool(v) and ("@" not in v) and any(ch.isdigit() for ch in v)


def fetch_students_for_tracker(session=None, include_done=False):
    rows = []
    for s in _trackerlist(session, include_done):
        ref   = str(s.get("referenceNo") or "").strip()
        enr   = str(s.get("enrollmentNo") or "").strip()
        email = str(s.get("email") or "").strip()
        # If an email/placeholder is sitting in the reference or enrollment field,
        # drop it from there (and keep it as the email if we don't have one).
        if ref and not _valid_ref(ref):
            if "@" in ref and not email:
                email = ref
            ref = ""
        if enr and not _valid_ref(enr):
            enr = ""
        # Real email keeps its "email:" key (existing students stay matched). Placeholder
        # emails ("temp"/"na"/...) fall back to reference / enrollment so students sharing
        # a placeholder are not wrongly merged into one duplicate.
        _em = email.lower()
        _real_email = ("@" in _em and "." in _em.split("@")[-1]
                       and _em not in ("temp", "na", "none", "nil", "-", "null", "n/a"))
        if _real_email:
            rk = "email:" + _em
        elif ref:
            rk = "ref:" + ref
        elif enr:
            rk = "enr:" + enr
        else:
            continue
        rows.append({
            "row_index": 0, "reference_no": ref, "enrollment_no": enr, "email": email,
            "dob": str(s.get("dob") or "").strip(), "student_name": str(s.get("name") or "").strip(),
            "mobile": str(s.get("mobile") or "").strip(), "class_level": str(s.get("class") or "").strip(),
            "alt_mobile": str(s.get("alternateMobile") or s.get("alternateNumber") or s.get("altMobile")
                              or s.get("whatsappNumber") or s.get("alternate_number") or "").strip(),
            "session": canonicalize_session(str(s.get("examSession") or "").strip()), "row_key": rk,
            "student_id": str(s.get("studentId") or "").strip(),
            "toc_status": normalize_toc(s.get("tocStatus") or s.get("tocstatus")
                                        or s.get("toc_status") or s.get("toc") or ""),
            # How the student was added ON THE PORTAL: 'enrol' (enrollment form) vs 'sheet'
            # (bulk sheet upload). The Portal knows this; if it sends any of these fields we use
            # it to split the dashboard's "Enrol. MVS Portal" vs "MVS Portal" cards. If the Portal
            # sends nothing, origin stays '' and the student is treated as enrol by default.
            "portal_origin": _detect_origin(s),
        })
    try:
        from collections import Counter
        _oc = Counter((r.get("portal_origin") or "(none)") for r in rows)
        logger.info(f"MVS: fetched {len(rows)} students | origin breakdown: {dict(_oc)}")
    except Exception:
        logger.info(f"MVS: fetched {len(rows)} students")
    return rows


def _detect_origin(s):
    """Work out how the Portal added this student — 'enrol' (Real enrolments) vs 'sheet'
    (Bulk imported / legacy) — from WHATEVER field name the Portal uses. Checks the known
    explicit names first, then scans every key whose name hints at an origin, then
    boolean-style flags (isLegacy / bulkImported / realEnrolment = true)."""
    # 1) explicit well-known names
    for f in ("origin", "createdVia", "addedVia", "dataSource", "enrolledVia", "entrySource",
              "source", "studentSource", "importSource", "enrollmentType", "enrolmentType",
              "regType", "registrationType", "uploadType", "addedBy", "createdFrom", "entryType"):
        v = _norm_origin(s.get(f))
        if v:
            return v
    # 2) generic scan — any key whose NAME hints at origin, with a recognizable VALUE
    for k, val in s.items():
        kl = str(k).lower()
        if any(h in kl for h in ("origin", "source", "legacy", "import", "createdvia",
                                 "entrytype", "enroltype", "enrollmenttype", "regtype",
                                 "uploadtype", "addedvia", "createdfrom")):
            v = _norm_origin(val)
            if v:
                return v
    # 3) boolean-style flags (isLegacy: true / bulkImported: 1 / realEnrolment: "yes")
    for k, val in s.items():
        kl = str(k).lower()
        sval = str(val).strip().lower()
        if sval in ("1", "true", "yes"):
            if any(h in kl for h in ("legacy", "bulk", "imported", "sheet")):
                return "sheet"
            if any(h in kl for h in ("realenrol", "real_enrol", "isreal", "isenrol")):
                return "enrol"
        if sval in ("0", "false", "no") and any(h in kl for h in ("legacy", "bulk", "imported")):
            return "enrol"   # explicitly NOT legacy/bulk -> a real enrolment
    return ""


def _norm_origin(v):
    v = str(v or "").strip().lower()
    if not v:
        return ""
    # Portal's own split: "Real enrolments" vs "Bulk imported (legacy)".
    if any(k in v for k in ("sheet", "excel", "csv", "bulk", "import", "upload", "legacy")):
        return "sheet"
    if any(k in v for k in ("real", "enrol", "enroll", "form", "register", "signup", "portal")):
        return "enrol"
    return ""


def _doc_link(row_key, kind):
    try:
        from links import short_doc_url
        return short_doc_url(row_key, kind)
    except Exception as e:
        logger.warning(f"doc link build failed ({kind}): {e}")
        return None


def _docs_complete(row_key):
    """True only if every document this student should have is already saved in our DB.
    The Portal link is pushed only when complete, so the student's first tap opens instantly
    (no live NIOS fetch = no error/panic). Queries the DB directly (no import cycle)."""
    try:
        from database import get_db
        import whatsapp
        conn = get_db()
        r = conn.execute("SELECT session, toc_status FROM student_status WHERE row_key=?", (row_key,)).fetchone()
        if not r:
            conn.close(); return False
        allowed = whatsapp.allowed_docs(r["session"], (r["toc_status"] or ""))
        if not allowed:
            conn.close(); return True
        have = {x["kind"] for x in
                conn.execute("SELECT kind FROM document_cache WHERE row_key=?", (row_key,)).fetchall()}
        conn.close()
        return allowed.issubset(have)
    except Exception:
        return False


def push_toc(student_id, toc_status, toc_subjects=None):
    """Push a counsellor-verified TOC (and the TOC subjects) to the Portal, so the Portal is
    corrected without a manual edit. Returns True on success."""
    if not enabled():
        return False
    sid = str(student_id or "").strip()
    if not sid:
        return False
    st = "yes" if str(toc_status).lower() == "yes" else "no"
    data = {"action": "trackerUpdate", "trackerKey": MVS_TRACKER_KEY, "studentId": sid,
            "tocStatus": st}
    if st == "no":
        # CRITICAL: the Portal auto-flips tocStatus back to YES if ANYTHING is written in
        # the TOC-subject field (e.g. a stale "WITHOUT TOC" text). So a NO push must ALSO
        # blank the subjects, otherwise the correction is undone by the Portal itself.
        data["tocSubjects"] = ""
        data["tocSubject"]  = ""          # Portal UI calls the field "TOC SUBJECT" (singular)
        data["clearTocSubjects"] = "1"    # explicit hint in case the Portal skips empty params
    elif toc_subjects:
        try:
            joined = ", ".join([str(s).strip() for s in toc_subjects if str(s).strip()])
            if joined:
                data["tocSubjects"] = joined
                data["tocSubject"]  = joined
        except Exception:
            pass
    try:
        r = requests.post(MVS_API_URL, data=data, timeout=40)
        out = r.json()
        if out.get("status") != "success":
            logger.warning(f"MVS push_toc {sid}: {out.get('message')}")
            return False
        return True
    except Exception as e:
        logger.warning(f"MVS push_toc error {sid}: {e}")
        return False


def push_student(student, status_label, conn=None, timeout=40):
    if not enabled():
        return False
    sid = str(student.get("student_id") or student.get("studentId") or "").strip()
    if not sid:
        return False
    row_key = student.get("row_key", "")
    session = str(student.get("session") or "").lower()
    low     = str(status_label or "").strip().lower()
    is_syc  = "syc" in session
    data = {"action": "trackerUpdate", "trackerKey": MVS_TRACKER_KEY, "studentId": sid}
    confirmed = ("confirm" in low or "admitted" in low)
    if not is_syc:
        if confirmed:
            data["niosAdmissionStatus"] = "confirmed"
        elif ("verified" in low or "approved" in low):
            data["niosAdmissionStatus"] = "verified"
        elif ("in progress" in low or "document required" in low or "pending" in low):
            data["niosAdmissionStatus"] = "in progress"
    if confirmed:
        # Push document links to the Portal ONLY when every document is saved in our DB, so the
        # student's first tap on the Portal opens instantly (no live fetch = no error/panic).
        # If not complete yet, we still push the status; links go on a later sync once complete.
        if _docs_complete(row_key):
            ic = _doc_link(row_key, "id_card"); af = _doc_link(row_key, "app_form"); ht = _doc_link(row_key, "hall_ticket")
            if ic: data["idCardLink"] = ic
            if af: data["applicationFormLink"] = af
            if ht: data["hallTicketLink"] = ht
    elif is_syc:
        if _docs_complete(row_key):
            ht = _doc_link(row_key, "hall_ticket")
            if ht: data["hallTicketLink"] = ht
    ref = student.get("discovered_ref") or student.get("reference_no")
    if ref: data["referenceNo"] = ref
    if student.get("enrollment_no"): data["enrollmentNo"] = student["enrollment_no"]
    if student.get("remark"): data["remark"] = student["remark"]
    if len(data) <= 3:
        return False
    try:
        r = requests.post(MVS_API_URL, data=data, timeout=timeout)
        out = r.json()
        if out.get("status") != "success":
            logger.warning(f"MVS push {sid}: {out.get('message')}")
            return False
        return True
    except Exception as e:
        logger.warning(f"MVS push error {sid}: {e}")
        return False


# Tracker DB column  ->  Portal trackerUpdate param name.
# Any student detail edited on the TRACKER is pushed with these keys so the Portal copy
# never lags behind. (If the Portal's trackerUpdate script ignores an unknown key, that
# field simply stays as-is on the Portal — nothing breaks.)
DETAIL_FIELD_MAP = {
    "student_name":  "name",
    "reference_no":  "referenceNo",
    "enrollment_no": "enrollmentNo",
    "dob":           "dob",
    "mobile":        "mobile",
    "alt_mobile":    "alternateMobile",
    "email":         "email",
    "class_level":   "class",
    "session":       "examSession",
}


def push_details(student_id, fields, timeout=40):
    """Push a student's IDENTITY DETAILS (name / reference / enrollment / DOB / mobile /
    alt mobile / email / class / session) to the Portal, so any edit made on the tracker
    is mirrored on the Portal automatically — no double data-entry, no gap.

    fields: dict of tracker column -> value (only non-empty values are sent, so a blank
    tracker field never wipes Portal data). Returns (ok: bool, message: str)."""
    if not enabled():
        return False, "Portal sync is OFF (MVS_MODE not enabled)"
    sid = str(student_id or "").strip()
    if not sid:
        return False, "no Portal studentId linked to this student"
    data = {"action": "trackerUpdate", "trackerKey": MVS_TRACKER_KEY, "studentId": sid}
    for col, key in DETAIL_FIELD_MAP.items():
        v = str((fields or {}).get(col) or "").strip()
        if not v:
            continue
        if col in ("reference_no", "enrollment_no") and not _valid_ref(v):
            continue    # never push an email/placeholder into the reference field
        data[key] = v
    if len(data) <= 3:
        return False, "nothing to push (all fields empty)"
    try:
        r = requests.post(MVS_API_URL, data=data, timeout=timeout)
        out = r.json()
        if out.get("status") != "success":
            msg = str(out.get("message") or "Portal rejected the update")
            logger.warning(f"MVS push_details {sid}: {msg}")
            return False, msg
        return True, "ok"
    except Exception as e:
        logger.warning(f"MVS push_details error {sid}: {e}")
        return False, str(e)
