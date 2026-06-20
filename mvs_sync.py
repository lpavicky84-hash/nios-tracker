import os
import logging
import requests

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


def fetch_students_for_tracker(session=None, include_done=False):
    rows = []
    for s in _trackerlist(session, include_done):
        ref   = str(s.get("referenceNo") or "").strip()
        enr   = str(s.get("enrollmentNo") or "").strip()
        email = str(s.get("email") or "").strip()
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
            "session": str(s.get("examSession") or "").strip(), "row_key": rk,
            "student_id": str(s.get("studentId") or "").strip(),
        })
    logger.info(f"MVS: fetched {len(rows)} students")
    return rows


def _doc_link(row_key, kind):
    try:
        from links import short_doc_url
        return short_doc_url(row_key, kind)
    except Exception as e:
        logger.warning(f"doc link build failed ({kind}): {e}")
        return None


def push_student(student, status_label, conn=None, timeout=40):
    if not enabled():
        return
    sid = str(student.get("student_id") or student.get("studentId") or "").strip()
    if not sid:
        return
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
        ic = _doc_link(row_key, "id_card"); af = _doc_link(row_key, "app_form"); ht = _doc_link(row_key, "hall_ticket")
        if ic: data["idCardLink"] = ic
        if af: data["applicationFormLink"] = af
        if ht: data["hallTicketLink"] = ht
    elif is_syc:
        ht = _doc_link(row_key, "hall_ticket")
        if ht: data["hallTicketLink"] = ht
    ref = student.get("discovered_ref") or student.get("reference_no")
    if ref: data["referenceNo"] = ref
    if student.get("enrollment_no"): data["enrollmentNo"] = student["enrollment_no"]
    if student.get("remark"): data["remark"] = student["remark"]
    if len(data) <= 3:
        return
    try:
        r = requests.post(MVS_API_URL, data=data, timeout=timeout)
        out = r.json()
        if out.get("status") != "success":
            logger.warning(f"MVS push {sid}: {out.get('message')}")
    except Exception as e:
        logger.warning(f"MVS push error {sid}: {e}")
