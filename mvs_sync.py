"""
mvs_sync.py  —  MVS Foundation  <->  NIOS Status Tracker bridge
================================================================
Tracker ko MVS backend se jodta hai. Excel ki jagah:
  - fetch_students_for_tracker()  -> MVS se students (Excel jaisा hi format)
  - push_student(student, status)  -> status + doc links WAPAS MVS ko (live)

Tracker ka baaki kaam (NIOS check, WhatsApp, DB) waisा hi rehta hai.

SETUP (Railway -> Variables):
  MVS_MODE        = 1                      (ON karne ke liye; 0/khali = purana Excel flow)
  MVS_API_URL     = <Apps Script Web App /exec URL>
  MVS_TRACKER_KEY = <Apps Script Script Property 'TRACKER_KEY' wali value>
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

MVS_API_URL     = os.environ.get("MVS_API_URL", "").strip()
MVS_TRACKER_KEY = os.environ.get("MVS_TRACKER_KEY", "").strip()

SESSIONS = ["April", "October", "On Demand", "Stream 2", "SYC"]


def enabled():
    """MVS mode ON hai ya nahi (Railway env MVS_MODE)."""
    on = os.environ.get("MVS_MODE", "").strip().lower() in ("1", "true", "yes", "on")
    return on and bool(MVS_API_URL) and bool(MVS_TRACKER_KEY)


# ───────────────────────── FETCH (Excel read ki jagah) ─────────────────────────
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
    """
    MVS se students laata hai aur tracker ke NATIVE format me deta hai
    (bilkul read_students_from_excel jaisा), taaki seedha drop-in ho jaye.
    Extra key: 'student_id' (MVS id) — push-back ke liye zaroori, ise mat hatana.
    """
    rows = []
    for s in _trackerlist(session, include_done):
        ref   = str(s.get("referenceNo") or "").strip()
        enr   = str(s.get("enrollmentNo") or "").strip()
        email = str(s.get("email") or "").strip()
        # row_key: wahi scheme jo excel_handler use karta hai (email > ref > enr)
        if email:
            rk = "email:" + email.lower()
        elif ref:
            rk = "ref:" + ref
        elif enr:
            rk = "enr:" + enr
        else:
            continue
        rows.append({
            "row_index":     0,
            "reference_no":  ref,
            "enrollment_no": enr,
            "email":         email,
            "dob":           str(s.get("dob") or "").strip(),
            "student_name":  str(s.get("name") or "").strip(),
            "mobile":        str(s.get("mobile") or "").strip(),
            "class_level":   str(s.get("class") or "").strip(),
            "session":       str(s.get("examSession") or "").strip(),
            "row_key":       rk,
            "student_id":    str(s.get("studentId") or "").strip(),
        })
    logger.info(f"MVS: fetched {len(rows)} students")
    return rows


# ───────────────────────── PUSH (Excel write ki jagah) ─────────────────────────
def _doc_link(row_key, kind):
    """Tracker ka apna signed/short doc URL (Railway) — MVS dashboard isse khol lega."""
    try:
        from links import short_doc_url
        return short_doc_url(row_key, kind)
    except Exception as e:
        logger.warning(f"doc link build failed ({kind}): {e}")
        return None


def push_student(student, status_label, conn=None, timeout=40):
    """
    Ek student ka result MVS ko bhejta hai (live, har student ke baad).
    student: tracker ka student/result dict (student_id, row_key, session ... ke saath).
    status_label: NIOS se mila status (e.g. 'Admission Confirmed', 'Verified', 'SYC').
    Jaise hi MVS me likhega -> dashboard auto-update + WhatsApp/email auto.
    """
    if not enabled():
        return
    sid = str(student.get("student_id") or student.get("studentId") or "").strip()
    if not sid:
        return  # MVS ka student nahi (Excel-only) -> skip

    row_key = student.get("row_key", "")
    session = str(student.get("session") or "").lower()
    low     = str(status_label or "").strip().lower()
    is_syc  = "syc" in session

    data = {"action": "trackerUpdate", "trackerKey": MVS_TRACKER_KEY, "studentId": sid}

    # ---- status mapping (tracker label -> MVS: in progress / verified / confirmed) ----
    confirmed = ("confirm" in low or "admitted" in low)
    if not is_syc:
        if confirmed:
            data["niosAdmissionStatus"] = "confirmed"
        elif ("verified" in low or "approved" in low):
            data["niosAdmissionStatus"] = "verified"
        elif ("in progress" in low or "document required" in low or "pending" in low):
            data["niosAdmissionStatus"] = "in progress"
        # rejected / fetch error / unknown -> status nahi bhejte (galat downgrade na ho)

    # ---- document links ----
    if confirmed:
        ic = _doc_link(row_key, "id_card")
        af = _doc_link(row_key, "app_form")
        ht = _doc_link(row_key, "hall_ticket")
        if ic: data["idCardLink"] = ic
        if af: data["applicationFormLink"] = af
        if ht: data["hallTicketLink"] = ht
    elif is_syc:
        ht = _doc_link(row_key, "hall_ticket")
        if ht: data["hallTicketLink"] = ht

    # ---- extras ----
    ref = student.get("discovered_ref") or student.get("reference_no")
    if ref:                       data["referenceNo"]  = ref
    if student.get("enrollment_no"): data["enrollmentNo"] = student["enrollment_no"]
    if student.get("remark"):     data["remark"]       = student["remark"]

    if len(data) <= 3:            # sirf keys -> kuch bhejne layak nahi
        return
    try:
        r = requests.post(MVS_API_URL, data=data, timeout=timeout)
        out = r.json()
        if out.get("status") != "success":
            logger.warning(f"MVS push {sid}: {out.get('message')}")
    except Exception as e:
        logger.warning(f"MVS push error {sid}: {e}")


# ───────────────────────── self-test (kuch likhta nahi) ─────────────────────────
if __name__ == "__main__":
    print("MVS_MODE   :", os.environ.get("MVS_MODE"))
    print("API set    :", bool(MVS_API_URL))
    print("KEY set    :", bool(MVS_TRACKER_KEY))
    if not (MVS_API_URL and MVS_TRACKER_KEY):
        print("Set MVS_API_URL + MVS_TRACKER_KEY first."); raise SystemExit
    try:
        for sess in SESSIONS:
            st = _trackerlist(sess)
            print(f"  {sess:12s} -> {len(st)} students")
            for s in st[:2]:
                print(f"      {str(s.get('name',''))[:18]:18s} ref={s.get('referenceNo','')} "
                      f"enrol={s.get('enrollmentNo','')} dob={s.get('dob','')} {s.get('mobile','')}")
        print("OK — MVS connected (read-only test, kuch likha nahi).")
    except Exception as e:
        print("ERROR:", e)
