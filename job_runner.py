import logging
import os
import json
import time as _time
import threading
os.environ["TZ"] = "Asia/Kolkata"
try:
    _time.tzset()
except Exception:
    pass
from datetime import datetime, timedelta
from database import get_db, get_setting, set_setting
from scraper import scrape_students
from excel_handler import read_students_from_excel, write_status_to_excel, dedupe_students
try:
    from nios_login import verify_login, verify_login_autofix
except Exception:
    verify_login = None
    verify_login_autofix = None
try:
    import mvs_sync
except Exception:
    mvs_sync = None

logger = logging.getLogger(__name__)

# Shown in 'Failed to Run' when NIOS didn't return a readable status for a student.
_CHECK_FAIL_MSG = ("Status check failed — NIOS didn't return a readable status. "
                   "Verify the Reference No, or run this student again (may be temporary).")


def _names_disagree(sheet_name, nios_name):
    """True only when BOTH names exist and share NO common word — a strong signal the
    Reference No belongs to a different student. Deliberately conservative: a single
    shared word (or a missing name on either side) is treated as a match, so spelling,
    word-order or middle-name differences don't raise a false alarm."""
    import re as _re
    def _toks(x):
        return {t for t in _re.sub(r"[^a-z]", " ", (x or "").lower()).split() if len(t) >= 3}
    a, b = _toks(sheet_name), _toks(nios_name)
    if not a or not b:
        return False
    return len(a & b) == 0


EXCEL_PATH = os.environ.get("EXCEL_PATH", os.path.join(os.environ.get("DATA_DIR", "."), "students.xlsx"))

def _mvs_on():
    """True when the live MVS portal bridge is configured (MVS_MODE + URL + KEY)."""
    try:
        return bool(mvs_sync) and mvs_sync.enabled()
    except Exception:
        return False

# Which sessions belong to "public exam" group (April/October + year).
# Stream 2 / On Demand always count as REGULAR even if other words appear.
def is_public_session(session):
    from excel_handler import session_category
    return session_category(session) == "public"

def is_syc_session(session):
    """SYC students: no status check; documents via enrollment+DOB login only."""
    from excel_handler import session_category
    return session_category(session) == "syc"

def session_group(session):
    """Classify a session into its run group: 'ondemand', 'stream2' or 'public'.
    On Demand and Stream 2 are separate groups (own interval + manual run).

    All spelling tolerance lives in excel_handler.session_category (the single source
    of truth shared with the list filter and document rules), so 'str-2', 'STREAM 2',
    'ode', 'apr-27' etc. can never disagree between the filter and the run again.
    SAFETY: anything not clearly On Demand / Stream 2 — April/October, Stream 1, blank
    or unknown — is treated as PUBLIC, which never auto-sends unless set up."""
    from excel_handler import session_category
    cat = session_category(session)
    if cat == "stream2":
        return "stream2"
    if cat == "ondemand":
        return "ondemand"
    return "public"   # stream1 / public / syc / blank all sit in the Public run group

def _load_db_students(c):
    """Re-check source of truth: every student already in the DB. This makes runs
    work even when students.xlsx was wiped (Railway redeploy) and lets us re-check
    everyone who isn't confirmed without needing the Excel each time."""
    out = []
    try:
        rows = c.execute(
            "SELECT row_key, reference_no, enrollment_no, email, dob, student_name, "
            "mobile, alt_mobile, toc_status, class_level, session, source FROM student_status "
            "WHERE COALESCE(deleted,0)=0").fetchall()
    except Exception:
        return out
    for r in rows:
        out.append({
            "row_key":       r["row_key"],
            "reference_no":  r["reference_no"] or "",
            "enrollment_no": (r["enrollment_no"] if "enrollment_no" in r.keys() else "") or "",
            "email":         r["email"] or "",
            "dob":           r["dob"] or "",
            "student_name":  r["student_name"] or "",
            "mobile":        r["mobile"] or "",
            "alt_mobile":    (r["alt_mobile"] if "alt_mobile" in r.keys() else "") or "",
            "toc_status":    (r["toc_status"] if "toc_status" in r.keys() else "") or "",
            "class_level":   r["class_level"] or "",
            "session":       r["session"] or "",
            "source":        (r["source"] if "source" in r.keys() else "") or "mvs_tracker",
        })
    return out

def _process_syc(conn, c, syc_list, run_id, stats=None):
    """Register SYC students (NO NIOS status check). Store enrollment_no, mark status
    'SYC', and send the hall-ticket WhatsApp once — only if a SYC campaign is set up.
    Updates the live progress bar as each student is registered."""
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wa_on = get_setting("wa_enabled", "0") == "1"
    for s in syc_list:
        row_key = s["row_key"]
        new_source = s.get("source", "mvs_tracker")
        prev = c.execute("SELECT source FROM student_status WHERE row_key=?", (row_key,)).fetchone()
        prev_source = (prev["source"] if prev and prev["source"] else "")
        cross = 0
        final_source = new_source
        if prev_source and prev_source != new_source:
            final_source = "mvs_portal"
            cross = 1
        c.execute("""INSERT INTO student_status
            (row_key, reference_no, enrollment_no, email, dob, student_name, mobile, alt_mobile, toc_status, class_level,
             session, current_status, remark, is_confirmed, last_checked, last_changed,
             source, cross_dup, check_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(row_key) DO UPDATE SET
                enrollment_no = CASE WHEN excluded.enrollment_no != '' THEN excluded.enrollment_no ELSE enrollment_no END,
                student_name = excluded.student_name,
                mobile = excluded.mobile,
                alt_mobile = CASE WHEN excluded.alt_mobile != '' THEN excluded.alt_mobile ELSE alt_mobile END,
                toc_status = CASE WHEN excluded.toc_status != '' THEN excluded.toc_status ELSE toc_status END,
                dob = excluded.dob,
                session = excluded.session,
                current_status = 'SYC',
                last_checked = excluded.last_checked,
                source = excluded.source,
                cross_dup = CASE WHEN excluded.cross_dup=1 THEN 1 ELSE cross_dup END,
                check_count = check_count + 1""",
            (row_key, s.get("reference_no", ""), s.get("enrollment_no", ""), s.get("email", ""),
             s.get("dob", ""), s.get("student_name", ""), s.get("mobile", ""), s.get("alt_mobile", ""),
             s.get("toc_status", ""),
             s.get("class_level", ""),
             s.get("session", ""), "SYC", "", 0, now_s, now_s, final_source, cross))
        if stats is not None:
            stats["checked"] += 1
            stats["same"] += 1
            _col = "progress_done_mvs" if final_source == "mvs_portal" else "progress_done_trk"
            c.execute(f"UPDATE run_logs SET progress_current=?, progress_same=?, "
                      f"{_col}={_col}+1 WHERE id=?",
                      (stats["checked"], stats["same"], run_id))
        conn.commit()
        # WhatsApp hall ticket — once per student, only when a SYC campaign is configured
        try:
            if wa_on:
                wrow = c.execute("SELECT whatsapp_sent FROM student_status WHERE row_key=?",
                                 (row_key,)).fetchone()
                already = bool(wrow and wrow["whatsapp_sent"] == 1)
                phone = s.get("mobile", "")
                if not already and phone:
                    import whatsapp
                    ok, info = whatsapp.send_for_student({
                        "row_key": row_key,
                        "student_name": s.get("student_name", ""),
                        "mobile": phone,
                        "alt_mobile": s.get("alt_mobile", ""),
                        "session": s.get("session", ""),
                        "reference_no": s.get("reference_no", ""),
                        "enrollment_no": s.get("enrollment_no", ""),
                        "toc_status": s.get("toc_status", ""),
                        "dob": s.get("dob", ""),
                    })
                    if ok:
                        c.execute("UPDATE student_status SET whatsapp_sent=1, whatsapp_info=? WHERE row_key=?",
                                  (str(info)[:180], row_key))
                    else:
                        c.execute("UPDATE student_status SET whatsapp_info=? WHERE row_key=?",
                                  (str(info)[:180], row_key))
                    conn.commit()
                    logger.info(f"SYC WhatsApp {'sent' if ok else 'skip/fail'} -> {phone}: {info}")
        except Exception as we:
            logger.warning(f"SYC WhatsApp error: {we}")

        # ── MVS portal: push SYC status + hall-ticket link back ──
        try:
            if _mvs_on() and s.get("student_id"):
                conn.commit()
                mvs_sync.push_student(s, "SYC", conn)
        except Exception as pe:
            logger.warning(f"MVS SYC push error: {pe}")


def _run_label(group_type, source_only=None, scope=None):
    """Same human label the run_logs row gets — used for queue messages."""
    _GL = {"all": "All", "regular": "On Demand + Stream 2", "ondemand": "On Demand",
           "stream2": "Stream 2", "public": "Public"}
    if scope == "upload":
        return "Uploaded sheet"
    if scope == "selected":
        return "Selected students"
    if scope == "required":
        return "Document Required (re-check)"
    if scope == "new":
        return "New data"
    if source_only == "mvs_portal":
        return "MVS Portal" + (f" — {_GL[group_type]}" if group_type in ("ondemand", "stream2", "public") else "")
    if source_only == "mvs_tracker":
        return "MVS Tracker"
    return _GL.get(group_type, group_type)


# Scheduled (auto) runs that arrived while another run was busy. They are started, in order,
# as soon as the current run finishes — nothing is ever cancelled to make room.
RUN_QUEUE = []
_QUEUE_LOCK = threading.Lock()


def run_is_active():
    """True if a status run is currently running."""
    try:
        cc = get_db()
        row = cc.execute("SELECT id FROM run_logs WHERE status='running'").fetchone()
        cc.close()
        return bool(row)
    except Exception:
        return False


def queued_runs():
    """Human-readable list of runs waiting their turn."""
    with _QUEUE_LOCK:
        return [q["label"] for q in RUN_QUEUE]


def _drain_run_queue():
    """After a run finishes, start the next queued auto-run (if any)."""
    with _QUEUE_LOCK:
        if not RUN_QUEUE:
            return
        nxt = RUN_QUEUE.pop(0)
    logger.info(f"Queue: starting the next waiting run -> {nxt['label']} "
                f"({len(RUN_QUEUE)} still waiting)")
    t = threading.Thread(target=run_status_check,
                         kwargs={**nxt["kwargs"], "is_auto": True}, daemon=True)
    t.start()


def run_status_check(group_type="all", source_only=None, scope=None, only_keys=None, is_auto=False):
    """
    group_type: 'all' | 'regular' | 'public'
    source_only: None (both) | 'mvs_portal' | 'mvs_tracker'.
    scope: None  -> full run: MVS Portal (live) + Excel + every existing DB student.
           'upload'   -> run ONLY the students in the just-uploaded Excel sheet.
           'selected' -> run ONLY the students whose row_key is in only_keys (manual
                         hand-picked run, 1..20 students — saves CapSolver credits).
    only_keys: list of row_keys to run when scope == 'selected'.
    Skips students already 'Admission Confirmed'.
    Processes in batches with delays (handled inside scraper).
    """
    logger.info("=" * 50)
    logger.info(f"Run started [{group_type}/{source_only or 'both'}/{scope or 'full'}] at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    mvs_on = _mvs_on()
    # NOTE: the DB itself is now a data source (we re-check existing students), so a
    # run is valid even without students.xlsx and even when MVS mode is off.

    conn = get_db()
    c = conn.cursor()
    # "new" scope: only check students that are NOT already in the DB — i.e. students
    # that just arrived on the MVS Portal. This skips every already-tracked/active
    # student, so a frequent "new data only" run uses far fewer CapSolver credits.
    _new_only = (scope == "new")
    _existing_keys = set()
    if _new_only:
        _existing_keys = {r["row_key"] for r in c.execute(
            "SELECT row_key FROM student_status WHERE COALESCE(deleted,0)=0").fetchall()}
    # Transfers detected during any run are labelled 'auto'. A user-clicked Portal sync
    # logs its own 'manual' rows separately.
    _transfer_mode = "auto"
    # NEVER cancel a run that is already going. A run in progress always finishes.
    #   • AUTO (scheduled) run arriving while busy  -> QUEUE it; it starts the moment the
    #     current run ends. Nothing is lost, nothing is cancelled.
    #   • MANUAL run arriving while busy            -> REFUSED (the caller shows the operator
    #     "a run is active; cancel it first"). Only a human cancel can stop a run.
    prev = c.execute("SELECT id, group_type FROM run_logs WHERE status='running'").fetchone()
    if prev:
        conn.close()
        _label = _run_label(group_type, source_only, scope)
        if is_auto:
            with _QUEUE_LOCK:
                # don't queue the same scheduled group twice
                if any(q["label"] == _label for q in RUN_QUEUE):
                    logger.info(f"Queue: '{_label}' is already waiting — not queued again")
                    return {"queued": False, "already": True, "label": _label}
                RUN_QUEUE.append({"label": _label,
                                  "kwargs": {"group_type": group_type, "source_only": source_only,
                                             "scope": scope, "only_keys": only_keys}})
                pos = len(RUN_QUEUE)
            logger.info(f"Queue: '{prev['group_type']}' is still running — "
                        f"'{_label}' queued (position {pos}); it will start automatically")
            return {"queued": True, "position": pos, "label": _label}
        logger.info(f"Manual run refused — '{prev['group_type']}' is still running")
        return {"refused": True, "running": prev["group_type"]}
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _GL = {"all": "All", "regular": "On Demand + Stream 2", "ondemand": "On Demand",
           "stream2": "Stream 2", "public": "Public"}
    log_group = _GL.get(group_type, group_type)
    if scope == "upload":
        log_group = "Uploaded sheet"
    elif scope == "selected":
        log_group = "Selected students"
    elif scope == "required":
        log_group = "Document Required (re-check)"
    elif source_only == "mvs_portal":
        log_group = "MVS Portal" + (f" — {_GL[group_type]}" if group_type in ("ondemand", "stream2", "public") else "")
    elif source_only == "mvs_tracker":
        log_group = "MVS Tracker"
    c.execute("INSERT INTO run_logs (run_at, group_type, status) VALUES (?,?, 'running')",
              (run_at, log_group))
    conn.commit()
    run_id = c.lastrowid

    def _is_cancelled():
        """Cooperative cancel check (fresh connection; sees other threads' commits)."""
        try:
            cc = get_db()
            row = cc.execute("SELECT status FROM run_logs WHERE id=?", (run_id,)).fetchone()
            cc.close()
            return bool(row and row["status"] != "running")
        except Exception:
            return False

    checked = changed = failed = 0
    excel_updates = []

    try:
        all_students = []
        if scope in ("selected", "required"):
            # Hand-picked / required-only run: only the chosen students, using their
            # CURRENT DB values. 'required' passes the keys of every Document Required
            # student so a counsellor can re-check just those after resolving them.
            keys = set(only_keys or [])
            all_students = [s for s in _load_db_students(c) if s["row_key"] in keys]
            dup_count = 0
            logger.info(f"{scope.capitalize()} run: {len(all_students)} of {len(keys)} requested student(s)")
        elif scope == "failed":
            # Re-run EVERY 'Failed to Run' student (status-check OR login failed) with
            # auto-fix: the status read auto-retries (scraper) and a confirmed student's
            # DOB is auto-flipped if a date/month swap fixes the login. Confirmed students
            # are intentionally NOT skipped here (a confirmed-but-login-failed student must
            # be re-verified). Uses current DB values, no MVS fetch / no Excel.
            rows = c.execute("SELECT row_key FROM student_status WHERE COALESCE(deleted,0)=0 "
                             "AND (COALESCE(login_failed,0)=1 OR COALESCE(check_failed,0)=1)").fetchall()
            fkeys = {r["row_key"] for r in rows}
            all_students = [s for s in _load_db_students(c) if s["row_key"] in fkeys]
            dup_count = 0
            logger.info(f"Failed re-run (auto-fix): {len(all_students)} failed student(s)")
        elif scope == "unknown":
            # Re-run only students stuck at 'Unknown' (NIOS returned no recognisable
            # status) — usually a wrong/late reference or a transient blip.
            rows = c.execute("SELECT row_key FROM student_status WHERE COALESCE(deleted,0)=0 "
                             "AND current_status='Unknown'").fetchall()
            ukeys = {r["row_key"] for r in rows}
            all_students = [s for s in _load_db_students(c) if s["row_key"] in ukeys]
            dup_count = 0
            logger.info(f"Unknown re-run: {len(all_students)} unknown-status student(s)")
        elif scope == "upload":
            # UPLOAD RUN: only the students in the just-uploaded sheet, ALWAYS treated as
            # MVS Tracker data. The Upload feature is the tracker entry point, so never
            # auto-label an uploaded sheet as Portal just because it carries portal-style
            # headers (referenceNo / examSession / ...). No MVS fetch, no other DB rows.
            if os.path.exists(EXCEL_PATH):
                try:
                    sheet = read_students_from_excel(EXCEL_PATH)
                    for s in sheet:
                        s["source"] = "mvs_tracker"
                    all_students += sheet
                except Exception as e:
                    logger.warning(f"Excel read failed: {e}")
            all_students, dup_count = dedupe_students(all_students)
            # Skip students already tracked (same reference / key already in the DB) so
            # "Run Check Now" after an upload checks only the NEWLY added students — the
            # exact count the upload report promised ("X new after duplicates removed").
            # Existing students keep their status and are refreshed by their normal run.
            existing_refs, existing_keys = set(), set()
            for r in c.execute("SELECT reference_no, row_key FROM student_status "
                               "WHERE COALESCE(deleted,0)=0").fetchall():
                if r["reference_no"] and str(r["reference_no"]).strip():
                    existing_refs.add(str(r["reference_no"]).strip())
                if r["row_key"]:
                    existing_keys.add(r["row_key"])
            before = len(all_students)
            all_students = [s for s in all_students
                            if str(s.get("reference_no") or "").strip() not in existing_refs
                            and s.get("row_key") not in existing_keys]
            skipped = before - len(all_students)
            logger.info(f"Upload run: {len(all_students)} NEW student(s) from the uploaded sheet "
                        f"(MVS Tracker) | {skipped} already tracked & skipped | {dup_count} in-sheet dup(s) merged")
        else:
            # ── MVS portal: auto-fetch live students (no Excel upload needed) ──
            # IMPORTANT: skip this on a TRACKER-only run. Otherwise the live portal
            # fetch pulls in every portal student, and any tracker student who ALSO
            # exists in the portal has its source flipped to mvs_portal during dedupe
            # (portal rows are added first and win) — so the later source_only filter
            # silently drops them. That made a "Stream 2 (MVS Tracker)" run check 69
            # instead of 88. A tracker run must consider Excel + DB only.
            if mvs_on and source_only != "mvs_tracker":
                try:
                    mvs_students = mvs_sync.fetch_students_for_tracker(include_done=True)
                    for s in mvs_students:
                        s["source"] = "mvs_portal"
                    all_students += mvs_students
                    logger.info(f"MVS: auto-fetched {len(mvs_students)} students")
                except Exception as e:
                    logger.warning(f"MVS fetch failed: {e}")
            elif source_only == "mvs_tracker":
                logger.info("Tracker-only run: skipping live MVS Portal fetch")
            # ── Excel upload (MVS Tracker data) — skip on a PORTAL-only run so the
            # mirror case can't happen (tracker Excel rows leaking into a portal run).
            if os.path.exists(EXCEL_PATH) and source_only != "mvs_portal":
                try:
                    all_students += read_students_from_excel(EXCEL_PATH)
                except Exception as e:
                    logger.warning(f"Excel read failed: {e}")
            # Cross-source duplicates = same student present in BOTH live sources
            # (MVS Portal + MVS Tracker). Count these BEFORE folding in the DB so the
            # "duplicates merged" number stays meaningful (DB overlaps don't inflate it).
            all_students, dup_count = dedupe_students(all_students)
            if dup_count:
                logger.info(f"Cross-source duplicates merged: {dup_count}")
            # ── Existing students in the DB (re-check everyone, even without Excel) ──
            # Added LAST so fresh MVS / Excel rows win on dedupe; the DB only fills in
            # students who aren't in the live sources this run. This dedupe is silent.
            db_students = _load_db_students(c)
            all_students += db_students
            all_students, _ = dedupe_students(all_students)
            logger.info(f"Total students to consider (live + DB): {len(all_students)}")
        # Map each student's row_key to the data source (mvs_portal = MVS portal/
        # detected, mvs_tracker = manual upload).
        src_by_key = {s["row_key"]: s.get("source", "mvs_tracker") for s in all_students}
        alt_by_key = {s["row_key"]: (s.get("alt_mobile") or "") for s in all_students}
        toc_by_key = {s["row_key"]: (s.get("toc_status") or "") for s in all_students}
        # TOC actually READ from each student's NIOS page during THIS run. The admission-status
        # page already carries 'Previous Subject Details', so the TOC comes free with the status
        # fetch — no second page load, no extra captcha. The confirm flow below trusts this.
        run_toc_read = {}
        # Manual override (set in the Upload section) wins over auto-detection — but ONLY
        # for the upload run it was set for. It used to apply to EVERY run and was never
        # cleared, so a stale 'mvs_tracker' override forced all students to Tracker and a
        # "MVS Portal" run (source_only='mvs_portal') filtered everyone out -> "Nothing to
        # check". Now it is one-shot and upload-only: explicit Portal/Tracker/All/scheduled
        # runs always use the real auto-detected source.
        override = get_setting("source_override", "")
        if override in ("mvs_portal", "mvs_tracker") and scope == "upload":
            src_by_key = {k: override for k in src_by_key}
            logger.info(f"Source override active (upload only): all -> {override}")
            set_setting("source_override", "")   # one-shot — clear after this upload run
        elif override in ("mvs_portal", "mvs_tracker"):
            logger.info(f"Ignoring stale source_override='{override}' (not an upload run)")
        # MVS student-id map (for pushing status + doc links back to the portal).
        sid_by_key = {s["row_key"]: s.get("student_id", "")
                      for s in all_students if s.get("student_id")}
        # Persist student_id so the portal-resync sweep can re-push a missed status later
        # WITHOUT re-fetching the whole portal list.
        try:
            _sid_rows = [(sid, rk) for rk, sid in sid_by_key.items() if sid]
            if _sid_rows:
                c.executemany("UPDATE student_status SET student_id=? WHERE row_key=?", _sid_rows)
                conn.commit()
        except Exception as _se:
            logger.warning(f"student_id persist skipped: {_se}")
        # ── MVS-ID LINK SWEEP ──
        # The MVS studentId is the student's PERMANENT identity on the Portal — unlike the
        # reference number, it can never be wrong. A wrong-reference student produces two
        # different row_keys (tracker has the corrected ref, Portal the wrong one), so the
        # direct row_key persist above misses them and they stay unlinked (no pushes work).
        # Here every still-unlinked tracker row is matched against Portal students by
        # email / mobile / alternate / name+DOB and the studentId is attached automatically —
        # so after any run, everything is linked without checking anything by hand.
        try:
            def _digits(x):
                return "".join(ch for ch in str(x or "") if ch.isdigit())
            _taken = {r[0] for r in c.execute(
                "SELECT DISTINCT student_id FROM student_status "
                "WHERE COALESCE(student_id,'') != ''").fetchall()}
            _by_email, _by_phone, _by_namedob = {}, {}, {}
            for s in all_students:
                _sid = str(s.get("student_id") or "").strip()
                if not _sid or _sid in _taken:
                    continue     # no id, or this id is already linked to a tracker row
                _em = str(s.get("email") or "").strip().lower()
                if "@" in _em:
                    _by_email.setdefault(_em, set()).add(_sid)
                for _ph in (_digits(s.get("mobile"))[-10:], _digits(s.get("alt_mobile"))[-10:]):
                    if len(_ph) == 10:
                        _by_phone.setdefault(_ph, set()).add(_sid)
                _nm = " ".join(str(s.get("student_name") or "").split()).lower()
                _dd = _digits(s.get("dob"))
                if _nm and _dd:
                    _by_namedob.setdefault(_nm + "|" + _dd, set()).add(_sid)
            if _by_email or _by_phone or _by_namedob:
                _linked = 0
                for r in c.execute(
                        "SELECT row_key, email, mobile, alt_mobile, student_name, dob "
                        "FROM student_status WHERE COALESCE(deleted,0)=0 "
                        "AND COALESCE(student_id,'')=''").fetchall():
                    _cands = set()
                    _em = str(r["email"] or "").strip().lower()
                    if "@" in _em and _em in _by_email:
                        _cands |= _by_email[_em]
                    if not _cands:
                        for _ph in (_digits(r["mobile"])[-10:], _digits(r["alt_mobile"])[-10:]):
                            if len(_ph) == 10 and _ph in _by_phone:
                                _cands |= _by_phone[_ph]
                    if not _cands:
                        _nm = " ".join(str(r["student_name"] or "").split()).lower()
                        _dd = _digits(r["dob"])
                        if _nm and _dd:
                            _cands |= _by_namedob.get(_nm + "|" + _dd, set())
                    _cands -= _taken
                    if len(_cands) == 1:
                        _sid = _cands.pop()
                        c.execute("UPDATE student_status SET student_id=? WHERE row_key=?",
                                  (_sid, r["row_key"]))
                        _taken.add(_sid)
                        _linked += 1
                if _linked:
                    conn.commit()
                    logger.info(f"MVS-ID link sweep: auto-linked {_linked} tracker row(s) to Portal students")
        except Exception as _le:
            logger.warning(f"MVS-ID link sweep skipped: {_le}")
        # Persist how each PORTAL student was added (enrol form vs sheet) so the dashboard can
        # split "Enrol. MVS Portal" vs "MVS Portal" accurately.
        try:
            _org_rows = [(s.get("portal_origin", ""), s["row_key"]) for s in all_students
                         if s.get("portal_origin")]
            if _org_rows:
                c.executemany("UPDATE student_status SET portal_origin=? WHERE row_key=?", _org_rows)
                conn.commit()
        except Exception as _oe:
            logger.warning(f"portal_origin persist skipped: {_oe}")

        # Clean any pre-existing duplicate rows (same reference under multiple keys):
        # keep the confirmed / most-recently-checked one.
        dups = c.execute("SELECT reference_no FROM student_status WHERE reference_no!='' "
                         "GROUP BY reference_no HAVING COUNT(*)>1").fetchall()
        for d in dups:
            ref = d["reference_no"]
            best = c.execute("SELECT row_key FROM student_status WHERE reference_no=? "
                             "ORDER BY is_confirmed DESC, last_checked DESC LIMIT 1", (ref,)).fetchone()
            if best:
                c.execute("DELETE FROM student_status WHERE reference_no=? AND row_key!=?",
                          (ref, best["row_key"]))
        if dups:
            conn.commit()
            logger.info(f"Cleaned duplicates for {len(dups)} references")
        # Build the check list.
        #  • Confirmed (Admission Confirmed) students are NEVER re-checked.
        #  • Everyone else (Verified / Docs-in-progress / Required / Unknown) IS re-checked.
        #  • group_type only decides WHICH session group runs on this interval:
        #       regular  -> On Demand + Stream 2
        #       public   -> April / October
        #       all      -> everyone non-confirmed (manual Run Now)
        #  Both data sources (MVS Portal + MVS Tracker) run together.
        to_check = []
        syc_list = []
        _portal_noref = 0
        _skipped_existing = 0
        for s in all_students:
            if source_only and src_by_key.get(s["row_key"], "mvs_tracker") != source_only:
                continue
            # "new" scope: skip every student we already track — only brand-new arrivals run.
            if _new_only and s["row_key"] in _existing_keys:
                _skipped_existing += 1
                continue
            # MVS Portal data must be checked by Reference No ONLY — never fall back to
            # email for portal students. Skip any portal student whose reference is
            # missing OR invalid (an email/placeholder in the reference field is NOT
            # a reference — checking it always comes back Unknown and wastes credits).
            _ref_v = (s.get("reference_no") or "").strip()
            if (src_by_key.get(s["row_key"], "mvs_tracker") == "mvs_portal"
                    and not (_ref_v and "@" not in _ref_v
                             and any(ch.isdigit() for ch in _ref_v))):
                _portal_noref += 1
                continue
            if is_syc_session(s["session"]):
                if group_type == "all":
                    syc_list.append(s)
                continue
            if scope != "failed":
                row = c.execute("SELECT is_confirmed FROM student_status WHERE row_key=?",
                                (s["row_key"],)).fetchone()
                if row and row["is_confirmed"] == 1:
                    continue                          # confirmed -> never re-check
            grp = session_group(s["session"])   # 'ondemand' | 'stream2' | 'public'
            if group_type == "regular" and grp not in ("ondemand", "stream2"):
                continue
            if group_type == "ondemand" and grp != "ondemand":
                continue
            if group_type == "stream2" and grp != "stream2":
                continue
            if group_type == "public" and grp != "public":
                continue
            to_check.append(s)

        # Per-source totals (for the separate MVS Portal / MVS Tracker progress bars).
        def _src(s):
            return src_by_key.get(s["row_key"], "mvs_tracker")
        work = to_check + syc_list
        tot_mvs = sum(1 for s in work if _src(s) == "mvs_portal")
        tot_trk = len(work) - tot_mvs

        logger.info(f"{len(to_check)} to check (group={group_type}); SYC: {len(syc_list)} "
                    f"| MVS Portal:{tot_mvs} MVS Tracker:{tot_trk}")
        if _portal_noref:
            logger.info(f"Skipped {_portal_noref} MVS Portal student(s) with no/invalid Reference No "
                        f"(email-in-reference & email fallback disabled for portal)")
        if _new_only:
            logger.info(f"New-only run: skipped {_skipped_existing} already-tracked student(s); "
                        f"{len(to_check)} new student(s) to check")

        total = len(work)
        if total == 0:
            msg = "Nothing to check"
            if _new_only:
                msg = "Nothing to check — no new MVS Portal students since last time"
            elif source_only == "mvs_portal":
                _pool = sum(1 for s in all_students
                            if src_by_key.get(s["row_key"], "mvs_tracker") == "mvs_portal")
                if _pool > 0:
                    msg = f"Nothing to check — all {_pool} MVS Portal student(s) already confirmed"
                else:
                    msg = "Nothing to check — no MVS Portal students in this run (bridge returned none)"
            elif source_only == "mvs_tracker":
                _pool = sum(1 for s in all_students
                            if src_by_key.get(s["row_key"], "mvs_tracker") == "mvs_tracker")
                if _pool > 0:
                    msg = f"Nothing to check — all {_pool} MVS Tracker student(s) already confirmed"
            _finish(conn, run_id, 0, 0, 0, msg)
            return

        # Live progress: overall + per-source counters.
        conn.execute("UPDATE run_logs SET progress_total=?, progress_current=0, "
                     "progress_changed=0, progress_same=0, "
                     "progress_total_mvs=?, progress_done_mvs=0, "
                     "progress_total_trk=?, progress_done_trk=0 WHERE id=?",
                     (total, tot_mvs, tot_trk, run_id))
        conn.commit()

        stats = {"checked": 0, "changed": 0, "same": 0, "failed": 0,
                 "confirmed": 0, "required": 0, "error": 0,
                 "verified": 0, "docs_progress": 0, "not_checked": 0}
        # When True, we're in the post-run auto-retry pass — the same per-student handler runs
        # (so DB updates, WhatsApp, downgrade-protection all stay identical), but we don't touch
        # the live progress bar (it's already at 100%) or double-count the run stats.
        _retry_mode = [False]

        # Register SYC students first (fast; no NIOS status check) with live progress.
        if syc_list:
            _process_syc(conn, c, syc_list, run_id, stats)

        def process_one(res):
            """Persist ONE student's result immediately so the dashboard/filters
            update live as the run progresses (not all at the end)."""
            if not _retry_mode[0]:
                stats["checked"] += 1
            if (not res.get("success")) and not _retry_mode[0]:
                stats["failed"] += 1
            row_key = res["row_key"]
            new_status = res["status"]
            # Break the failures down so the operator sees WHY, and can tell a real 'our-side'
            # miss (captcha/proxy — auto-retried) apart from a 'data-side' issue (wrong/pending
            # reference — needs a human) that no amount of re-running will fix.
            if (not res.get("success")) and not _retry_mode[0]:
                _rmk = (res.get("remark", "") or "").upper()
                if new_status == "Fetch Error" or "CAPTCHA" in _rmk:
                    stats["fail_captcha"] = stats.get("fail_captcha", 0) + 1
                elif "MISMATCH" in _rmk or "DATA" in _rmk:
                    stats["fail_data"] = stats.get("fail_data", 0) + 1
                else:  # Unknown — page came back but NIOS showed no status (often pending ref)
                    stats["fail_pending"] = stats.get("fail_pending", 0) + 1
            # Per-run outcome buckets for the WhatsApp report.
            if not _retry_mode[0]:
                if new_status == "Admission Confirmed":
                    stats["confirmed"] += 1
                elif new_status == "Document Required":
                    stats["required"] += 1
                elif new_status == "Verified":
                    stats["verified"] += 1
                elif new_status == "Documents Verification In Progress":
                    stats["docs_progress"] += 1
                if not res.get("success") or new_status in ("Unknown", "Fetch Error"):
                    stats["error"] += 1
            new_ref = res.get("discovered_ref") or res.get("reference_no") or ""
            is_conf = 1 if new_status == "Admission Confirmed" else 0
            now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            old = c.execute("SELECT current_status, source, COALESCE(is_confirmed,0) AS was_confirmed "
                            "FROM student_status WHERE row_key=?",
                            (row_key,)).fetchone()
            old_status = old["current_status"] if old else None
            _was_confirmed = bool(old and old["was_confirmed"] == 1)
            # Data source + cross-source duplicate detection. If this student already
            # exists from a DIFFERENT source, it's the SAME student in both MVS Portal
            # and MVS Tracker -> keep ONE row, give priority to MVS Portal, and flag it.
            new_source = src_by_key.get(row_key, "mvs_tracker")
            prev_source = (old["source"] if old and old["source"] else "")
            cross = 0
            final_source = new_source
            if prev_source and prev_source != new_source:
                final_source = "mvs_portal"
                cross = 1
                # Log this Tracker -> Portal transfer ONCE (the moment the merge is first
                # detected). prev was 'mvs_tracker', it now lives in BOTH and is managed as
                # Portal. old_status = what Tracker had, new_status = the fresh Portal check.
                if prev_source == "mvs_tracker":
                    c.execute("""INSERT INTO transfer_log
                        (row_key, reference_no, enrollment_no, student_name, mobile, session,
                         old_status, new_status, transferred_at, mode)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (row_key, new_ref, res.get("enrollment_no", ""), res.get("student_name", ""),
                         res.get("mobile", ""), res.get("session", ""), old_status or "",
                         new_status, now_s, _transfer_mode))
            # A failed check (Unknown/Fetch Error) that lands on a student who ALREADY has a
            # real status is IGNORED above (status is kept). So it is NOT a real change — don't
            # log it to history and don't count it, otherwise the change log fills with false
            # "Verified -> Unknown" flips that never actually happened on NIOS.
            _failed_check = new_status in ("Unknown", "Fetch Error")
            _has_real_old = (old_status not in (None, "", "Unknown", "Fetch Error"))
            # NEW: a CONFIRMED student must never be silently downgraded by a lower real status
            # either. NIOS occasionally flips a confirmed student's page back to 'Documents
            # Verification In Progress' (RC re-processing) — the documents already went out on
            # WhatsApp, so downgrading here (and pushing that to the Portal) only creates the
            # tracker-vs-Portal mismatch the operator saw. Keep 'Admission Confirmed'; log it.
            _conf_downgrade = (_was_confirmed and not _failed_check
                               and new_status != "Admission Confirmed")
            if _conf_downgrade:
                logger.info(f"  {row_key}: NIOS showed '{new_status}' for a CONFIRMED student — "
                            f"keeping Admission Confirmed (no downgrade, no portal push)")
            _ignored_downgrade = (_failed_check and _has_real_old) or _conf_downgrade
            # "Not checked this run" = the run could not read a real NIOS status (captcha/proxy
            # fell back, or NIOS returned nothing). Whether we kept an old status or it stays
            # Unknown, the student was effectively NOT freshly checked — count it so the operator
            # sees how many still need a re-run.
            if _failed_check and not _retry_mode[0]:
                stats["not_checked"] += 1
            status_changed = (old_status != new_status) and not _ignored_downgrade
            if not _retry_mode[0]:
                if status_changed:
                    stats["changed"] += 1
                    c.execute("""INSERT INTO status_history
                        (reference_no, student_name, old_status, new_status, changed_at, run_id, source)
                        VALUES (?,?,?,?,?,?,?)""",
                        (new_ref, res.get("student_name", ""), old_status, new_status, now_s, run_id, final_source))
                else:
                    stats["same"] += 1
            elif status_changed:
                # Retry pass recovered a real status (e.g. Unknown -> Verified) — record the
                # recovery in history so the change log is accurate, but don't touch run stats.
                c.execute("""INSERT INTO status_history
                    (reference_no, student_name, old_status, new_status, changed_at, run_id, source)
                    VALUES (?,?,?,?,?,?,?)""",
                    (new_ref, res.get("student_name", ""), old_status, new_status, now_s, run_id, final_source))

            _verified_ts = "" if _failed_check else now_s
            c.execute("""INSERT INTO student_status
                (row_key, reference_no, enrollment_no, email, dob, student_name, mobile, alt_mobile, toc_status, class_level,
                 session, current_status, remark, is_confirmed, last_checked, last_changed, last_verified,
                 source, cross_dup, check_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(row_key) DO UPDATE SET
                    reference_no = CASE WHEN excluded.reference_no != '' THEN excluded.reference_no ELSE reference_no END,
                    enrollment_no = CASE WHEN excluded.enrollment_no != '' THEN excluded.enrollment_no ELSE enrollment_no END,
                    dob = CASE WHEN excluded.dob != '' THEN excluded.dob ELSE dob END,
                    student_name = CASE WHEN excluded.student_name != '' THEN excluded.student_name ELSE student_name END,
                    mobile = CASE WHEN excluded.mobile != '' THEN excluded.mobile ELSE mobile END,
                    alt_mobile = CASE WHEN excluded.alt_mobile != '' THEN excluded.alt_mobile ELSE alt_mobile END,
                    toc_status = CASE WHEN excluded.toc_status != '' THEN excluded.toc_status ELSE toc_status END,
                    email = CASE WHEN excluded.email != '' THEN excluded.email ELSE email END,
                    class_level = CASE WHEN excluded.class_level != '' THEN excluded.class_level ELSE class_level END,
                    session = CASE WHEN excluded.session != '' THEN excluded.session ELSE session END,
                    -- A failed check (Unknown / Fetch Error = captcha/proxy/NIOS hiccup, NOT a
                    -- real NIOS status) must NEVER downgrade a student who already has a real
                    -- status. Status only moves forward. Keep the old status; only refresh
                    -- last_checked. A student with NO prior real status (fresh/blank) still
                    -- shows Unknown so the counsellor can see it needs attention.
                    current_status = CASE
                        WHEN excluded.current_status IN ('Unknown','Fetch Error')
                             AND current_status IS NOT NULL
                             AND current_status NOT IN ('Unknown','Fetch Error','')
                        THEN current_status
                        WHEN COALESCE(is_confirmed,0)=1
                             AND excluded.current_status != 'Admission Confirmed'
                        THEN current_status
                        ELSE excluded.current_status END,
                    remark = CASE
                        WHEN excluded.current_status IN ('Unknown','Fetch Error')
                             AND current_status IS NOT NULL
                             AND current_status NOT IN ('Unknown','Fetch Error','')
                        THEN remark
                        WHEN COALESCE(is_confirmed,0)=1
                             AND excluded.current_status != 'Admission Confirmed'
                        THEN remark
                        ELSE excluded.remark END,
                    is_confirmed = CASE
                        WHEN COALESCE(is_confirmed,0)=1 THEN 1
                        WHEN excluded.current_status IN ('Unknown','Fetch Error')
                        THEN is_confirmed
                        ELSE excluded.is_confirmed END,
                    last_checked = excluded.last_checked,
                    last_verified = CASE WHEN excluded.current_status NOT IN ('Unknown','Fetch Error')
                                         THEN excluded.last_verified ELSE last_verified END,
                    last_changed = CASE WHEN current_status != excluded.current_status
                                        AND excluded.current_status NOT IN ('Unknown','Fetch Error')
                                        THEN excluded.last_changed ELSE last_changed END,
                    source = excluded.source,
                    cross_dup = CASE WHEN excluded.cross_dup=1 THEN 1 ELSE cross_dup END,
                    check_count = check_count + 1""",
                (row_key, new_ref, res.get("enrollment_no", ""), res.get("email", ""), res.get("dob", ""),
                 res.get("student_name", ""), res.get("mobile", ""), alt_by_key.get(row_key, ""),
                 toc_by_key.get(row_key, ""),
                 res.get("class_level", ""),
                 res.get("session", ""), new_status, res.get("remark", ""), is_conf, now_s, now_s, _verified_ts,
                 final_source, cross))

            if new_ref:
                c.execute("DELETE FROM student_status WHERE reference_no=? AND row_key!=?",
                          (new_ref, row_key))

            # ── Status-check outcome -> 'Failed to Run' visibility ──────────────
            # If NIOS didn't return a readable status (success=False: 'Unknown' /
            # 'Fetch Error' — usually a wrong Reference No, or a temporary NIOS /
            # network / captcha issue), surface the student in 'Failed to Run' with a
            # clear remark so the counsellor can verify the reference or just run it
            # again. It clears automatically on the next successful check.
            if _ignored_downgrade:
                # A failed check on a student who already has a real status — we KEPT the
                # real status above. Do NOT flag it as failed and do NOT surface it in
                # 'Failed to Run'/'Unknown'; only its last_checked moved. It stays where it is.
                c.execute("UPDATE student_status SET check_failed=0 WHERE row_key=?", (row_key,))
            elif res.get("fail_kind") == "data":
                # NIOS explicitly rejected the Reference/DOB — genuine WRONG DATA. Flag it so the
                # auto-retry pass skips it (retrying can't fix wrong data) and the counsellor sees
                # a precise reason. Clears automatically once the data is fixed and a check succeeds.
                _why = res.get("remark") or ("NIOS rejected the login — Reference No / DOB does not "
                                             "match NIOS records. Please verify and fix.")
                c.execute("UPDATE student_status SET check_failed=1, data_error=1, login_remark=? WHERE row_key=?",
                          (_why[:300], row_key))
            elif new_status == "Unknown":
                _why = ("NIOS returned no recognizable status — verify the Reference No "
                        "(it may be wrong or not found yet), or NIOS may be showing a status not tracked yet.")
                if res.get("raw_text"):
                    _why += f" [NIOS: {res['raw_text'][:110]}]"
                c.execute("UPDATE student_status SET check_failed=1, login_remark=? WHERE row_key=?",
                          (_why[:300], row_key))
            elif not res.get("success"):
                _tries = res.get("attempts", 1)
                _msg = _CHECK_FAIL_MSG + (f" (auto-retried {_tries}x this run)" if _tries and _tries > 1 else "")
                c.execute("UPDATE student_status SET check_failed=1, login_remark=? WHERE row_key=?",
                          (_msg, row_key))
            else:
                # Real status obtained — clear both flags (data is evidently fine now).
                c.execute("UPDATE student_status SET check_failed=0, data_error=0 WHERE row_key=?", (row_key,))

            # ── TOC verification (runs on EVERY successful check, any status) ──
            # The status page we just fetched ALSO carries the real TOC (Previous Subject Details),
            # so the TOC is read from the SAME response as the status — one page, one captcha.
            # If NIOS's TOC disagrees with the Portal's tocStatus it is auto-corrected here (and
            # pushed), and the confirm flow below uses this value for the WhatsApp campaign.
            try:
                _nt = (res.get("nios_toc") or "").lower()
                if not _nt and res.get("nios_toc_absent"):
                    # Page read fine, table absent. For April/October NIOS hides the table for
                    # no-TOC students -> absence IS the answer: TOC = No. On Demand / Stream 2
                    # always show the table, so absence there stays 'could not read'.
                    import nios_login as _nl
                    if _nl.is_public_session(res.get("session", "")):
                        _nt = "no"
                        res["toc_src"] = ("No Previous-Subject-Details table on the NIOS page — "
                                          "for April/October sessions this means TOC = No")
                if _nt in ("yes", "no"):
                    run_toc_read[row_key] = _nt       # read from this run's own page fetch
                    _ptoc = (toc_by_key.get(row_key, "") or "").lower()
                    _subs = res.get("toc_subjects") or []
                    _subs_json = json.dumps(_subs) if _subs else ""
                    _tsrc = (res.get("toc_src") or "")[:280]
                    # NIOS always wins — a past 'verified' mark must not suppress a real
                    # disagreement. Only guard: no evidence-less yes -> no downgrade.
                    _mismatch = (_ptoc in ("yes", "no") and _ptoc != _nt)
                    if _mismatch and _ptoc == "yes" and _nt == "no" and not _tsrc.strip():
                        logger.warning(f"  TOC: refusing yes->no for {row_key} (no evidence)")
                        _mismatch = False
                        _nt = "yes"   # keep the Portal value; re-check will settle it
                    if not _mismatch:
                        # In sync — record the read + evidence, clear stale flags/remarks.
                        if _subs_json:
                            c.execute("UPDATE student_status SET nios_toc=?, toc_subjects=?, toc_mismatch=0, toc_src=? WHERE row_key=?",
                                      (_nt, _subs_json, _tsrc, row_key))
                        else:
                            c.execute("UPDATE student_status SET nios_toc=?, toc_mismatch=0, toc_src=? WHERE row_key=?",
                                      (_nt, _tsrc, row_key))
                        c.execute("UPDATE student_status SET remark='' WHERE row_key=? "
                                  "AND remark LIKE 'mismatch toc status%'", (row_key,))
                        toc_by_key[row_key] = _nt
                    else:
                        # AUTO-CORRECT: NIOS is the source of truth. Apply on the tracker, push the
                        # corrected TOC (+subjects) to the Portal, and log it for the audit list.
                        c.execute("UPDATE student_status SET toc_status=?, nios_toc=?, toc_subjects=?, toc_src=?, "
                                  "toc_mismatch=0, toc_verified=1, remark=? WHERE row_key=?",
                                  (_nt, _nt, _subs_json, _tsrc,
                                   f"TOC auto-corrected: MVS Portal said {_ptoc.upper()}, NIOS official site "
                                   f"says {_nt.upper()} — updated and pushed to the Portal", row_key))
                        # The rest of this run (WhatsApp campaign choice) must use the CORRECTED value.
                        toc_by_key[row_key] = _nt
                        _pushed = 0
                        try:
                            if mvs_on and sid_by_key.get(row_key):
                                if mvs_sync.push_toc(sid_by_key[row_key], _nt, _subs):
                                    _pushed = 1
                        except Exception as _tpe:
                            logger.warning(f"TOC auto-fix push failed {row_key}: {_tpe}")
                        c.execute("INSERT INTO toc_fix_log (row_key, reference_no, student_name, session, "
                                  "current_status, old_toc, new_toc, subjects, fixed_at, pushed) "
                                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  (row_key, new_ref, res.get("student_name", ""), res.get("session", ""),
                                   new_status, _ptoc, _nt, ", ".join(_subs),
                                   datetime.now().strftime("%Y-%m-%d %H:%M:%S"), _pushed))
                        logger.info(f"  TOC auto-corrected {row_key}: Portal {_ptoc} -> NIOS {_nt} (pushed={_pushed})")
            except Exception as _te:
                logger.warning(f"TOC check error {row_key}: {_te}")

            # ── On confirm: VERIFY the NIOS login works BEFORE sharing any document ──
            # If the uploaded Reference/Enrollment No or DOB is wrong, the login at
            # https://sdmis.nios.ac.in/auth/other-login fails. We must NOT send a link
            # in that case (the student would open it to a login error and panic).
            # Mark the student failed + store a clear remark so it can be edited & re-run.
            login_blocked = False
            if new_status == "Admission Confirmed" and verify_login_autofix is not None:
                vrow = c.execute("SELECT whatsapp_sent, login_failed FROM student_status WHERE row_key=?",
                                 (row_key,)).fetchone()
                already_sent = bool(vrow and vrow["whatsapp_sent"] == 1)
                if not already_sent:
                    conn.commit()   # release lock before the network login
                    ok_login, lmsg, fixed_dob = verify_login_autofix(
                        new_ref, res.get("dob", ""), res.get("enrollment_no", ""))
                    if ok_login:
                        # If a date<->month flip fixed it, persist the corrected DOB so
                        # future runs (and the document link) use the right one.
                        if fixed_dob:
                            c.execute("UPDATE student_status SET dob=?, login_failed=0, login_remark='' WHERE row_key=?",
                                      (fixed_dob, row_key))
                            logger.info(f"DOB auto-corrected for {row_key}: -> {fixed_dob}")
                        else:
                            c.execute("UPDATE student_status SET login_failed=0, login_remark='' WHERE row_key=?",
                                      (row_key,))
                    else:
                        login_blocked = True
                        if str(lmsg).startswith("CAPTCHA_BUSY"):
                            # captcha/login gateway busy — NOT wrong data. Do NOT flag the
                            # student; just skip the WhatsApp this run (retries next run when
                            # captcha is healthy). Leaves the confirmed status intact.
                            logger.warning(f"Login verify skipped (captcha busy) {row_key}")
                        else:
                            stats["failed"] += 1
                            # Login failed = "data mismatch". Pinpoint the cause: if the name
                            # NIOS shows for this reference shares NO word with the student's
                            # name, the Reference No likely belongs to a DIFFERENT student
                            # (vs. just a wrong DOB). Counsellor verifies on the portal, then
                            # ticks "Name correct" (sets name_verified=1) or fixes the reference.
                            _nv = c.execute("SELECT name_verified FROM student_status WHERE row_key=?",
                                            (row_key,)).fetchone()
                            _already_ok = bool(_nv and _nv["name_verified"] == 1)
                            _nm = res.get("nios_name", "")
                            if (not _already_ok) and _names_disagree(res.get("student_name", ""), _nm):
                                lmsg = (f"\u26a0 Reference No may belong to a DIFFERENT student — NIOS shows "
                                        f"'{_nm}' but name here is '{res.get('student_name','')}'. Check the "
                                        f"portal: tick 'Name correct' if fine, else update the Reference No.")
                            c.execute("UPDATE student_status SET login_failed=1, login_remark=?, "
                                      "whatsapp_info=?, whatsapp_sent=0 WHERE row_key=?",
                                      (lmsg[:240], ("Not sent — " + lmsg)[:180], row_key))
                            logger.warning(f"Login verify FAILED {row_key}: {lmsg}")
                    conn.commit()

            # ── WhatsApp: auto-send documents ONCE when admission is confirmed ──
            try:
                if (new_status == "Admission Confirmed" and not login_blocked
                        and get_setting("wa_enabled", "0") == "1"):
                    conn.commit()   # release write lock so short-link creation can write
                    wrow = c.execute("SELECT whatsapp_sent, alt_mobile FROM student_status WHERE row_key=?",
                                     (row_key,)).fetchone()
                    already = bool(wrow and wrow["whatsapp_sent"] == 1)
                    phone = res.get("mobile", "")
                    # Alt number from THIS run if present, else the one saved in the DB
                    # (e.g. added later via the bulk alternate-number upload) — so documents
                    # reach the alternate number even when the student confirms in a later run.
                    _eff_alt = (alt_by_key.get(row_key, "")
                                or (wrow["alt_mobile"] if (wrow and "alt_mobile" in wrow.keys()) else "") or "")
                    if not already and phone:
                        import whatsapp
                        # STEP 1: save this student's documents on OUR server FIRST —
                        # so the very first click on the WhatsApp link opens instantly
                        # from our copy (no NIOS, no captcha, no loader).
                        _complete = False
                        try:
                            from main import cache_student_docs, docs_all_cached
                            _cs, _cf = cache_student_docs(row_key)
                            if _cs or _cf:
                                logger.info(f"Docs cached before WhatsApp {row_key}: saved={_cs} failed={_cf}")
                            _complete = docs_all_cached(row_key)
                        except Exception as ce:
                            logger.warning(f"Doc pre-cache skipped {row_key}: {ce}")
                        # STEP 2: only send once ALL documents are saved — so the student's
                        # first tap opens instantly (no live fetch = no error/panic). If not
                        # complete yet, DON'T send now; the WhatsApp auto-sweep re-tries caching
                        # and sends the moment it's complete (with an attempt-based fallback so a
                        # rarely-available doc can't block the link forever).
                        # ALSO hold if this student has an unverified TOC mismatch — sending the
                        # wrong (no-TOC) campaign is exactly what panics students. The counsellor
                        # verifies in 'TOC Status Error' first; then the sweep sends correctly.
                        # FINAL TOC CHECK — the student is confirming RIGHT NOW and the documents
                        # are about to go out, so the campaign must use a TOC we actually read from
                        # NIOS this run. The admission-status page we just fetched ALREADY contains
                        # 'Previous Subject Details', so that read is right here — no second page
                        # load, no extra captcha. Any disagreement was already auto-corrected and
                        # pushed by the TOC block above.
                        _final_toc = run_toc_read.get(row_key, "")
                        if _final_toc not in ("yes", "no"):
                            # The page didn't give a TOC (rare: partial page). Only THEN re-read once.
                            _final_toc = final_toc_verify(row_key, new_ref, res.get("enrollment_no", ""),
                                                          res.get("dob", ""), res.get("session", ""))
                        if _final_toc in ("yes", "no"):
                            toc_by_key[row_key] = _final_toc
                        _toc_hold = (_final_toc not in ("yes", "no"))
                        if _toc_hold:
                            c.execute("UPDATE student_status SET whatsapp_info=? WHERE row_key=?",
                                      ("Held — final TOC check could not be read from NIOS; will retry", row_key))
                            conn.commit()
                            logger.info(f"WhatsApp held (final TOC unreadable) {row_key}")
                            ok, info = None, None
                        elif not _complete:
                            c.execute("UPDATE student_status SET whatsapp_info=? WHERE row_key=?",
                                      ("Waiting — saving documents before sending", row_key))
                            conn.commit()
                            logger.info(f"WhatsApp deferred {row_key}: documents not fully saved yet")
                            ok, info = None, None
                        else:
                            ok, info = whatsapp.send_for_student({
                            "row_key": row_key,
                            "student_name": res.get("student_name", ""),
                            "mobile": phone,
                            "alt_mobile": _eff_alt,
                            "session": res.get("session", ""),
                            "reference_no": new_ref,
                            "enrollment_no": res.get("enrollment_no", ""),
                            "toc_status": toc_by_key.get(row_key, res.get("toc_status", "")),
                            "dob": res.get("dob", ""),
                        })
                        # Only mark as sent on success; failures retry next run.
                        if ok is None:
                            pass  # deferred — the sweep will handle it once complete
                        elif ok:
                            c.execute("UPDATE student_status SET whatsapp_sent=1, whatsapp_info=?, "
                                      "whatsapp_sent_at=?, whatsapp_delivery='' WHERE row_key=?",
                                      (str(info)[:180], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), row_key))
                        else:
                            c.execute("UPDATE student_status SET whatsapp_info=? WHERE row_key=?",
                                      (str(info)[:180], row_key))
                        conn.commit()
                        if ok is not None:
                            logger.info(f"WhatsApp {'sent' if ok else 'FAILED'} -> {phone}: {info}")
            except Exception as we:
                logger.warning(f"WhatsApp trigger error: {we}")

            # ── Document-Required: prepare for the counsellor-reviewed reminder ──
            # We DO NOT auto-send. The counsellor reviews/edits each student's document
            # request on the "Doc Requests" page and sends manually. Here we only reset the
            # 'sent' flag when a student LEAVES the Document Required state, so that if they
            # fall back into it later, they appear as a fresh pending request (not "sent").
            try:
                if new_status != "Document Required":
                    c.execute("UPDATE student_status SET required_notified=0 "
                              "WHERE row_key=? AND COALESCE(required_notified,0)=1", (row_key,))
            except Exception as re_:
                logger.warning(f"Required-reset error: {re_}")

            # ── MVS portal: push status + doc links back ──
            # The STATUS push is a plain API call (no NIOS login needed), so it must happen even
            # when the WhatsApp-verify login was blocked (captcha busy) — otherwise a correctly-
            # detected 'Confirmed' would never reach the Portal and the two would disagree.
            # Doc links are separately gated by document-completeness inside push_student.
            try:
                if mvs_on and sid_by_key.get(row_key) and not _conf_downgrade:
                    conn.commit()   # ensure student row + links are persisted first
                    _pushed = mvs_sync.push_student({**res,
                        "student_id": sid_by_key[row_key], "row_key": row_key,
                        "discovered_ref": new_ref, "session": res.get("session", "")},
                        new_status, conn)
                    if _pushed:
                        # Remember exactly which status the Portal has now, so the resync sweep
                        # can tell when the Portal is behind and re-push only what's needed.
                        c.execute("UPDATE student_status SET portal_pushed=? WHERE row_key=?",
                                  (new_status, row_key))
                        conn.commit()
            except Exception as pe:
                logger.warning(f"MVS push error {row_key}: {pe}")

            excel_updates.append({
                "row_key": row_key,
                "reference_no": new_ref,
                "email": res.get("email", ""),
                "status_label": new_status,
                "remark": res.get("remark", ""),
                "last_checked": now_s,
                "changed": status_changed,
            })
            # Live progress (current / changed / same) — commit so the dashboard sees it
            if not _retry_mode[0]:
                _s = src_by_key.get(row_key, "mvs_tracker")
                _col = "progress_done_mvs" if _s == "mvs_portal" else "progress_done_trk"
                c.execute(f"UPDATE run_logs SET progress_current=?, progress_changed=?, "
                          f"progress_same=?, progress_notchecked=?, {_col}={_col}+1 WHERE id=?",
                          (stats["checked"], stats["changed"], stats["same"], stats["not_checked"], run_id))
                conn.commit()
            else:
                conn.commit()

        if to_check:
            scrape_students(to_check, should_cancel=_is_cancelled, on_result=process_one)
            # ── Auto-retry pass ────────────────────────────────────────────────────────
            # Exactly what the operator used to do by hand: re-check the students that came
            # back Unknown/Fetch Error (a captcha/proxy hiccup, NOT a data problem) several
            # more times until their real status comes through. Runs AFTER the main pass
            # (never in parallel — the portal stays fast) and BEFORE the counsellor report,
            # so the report reflects the resolved numbers, not the transient misses.
            if not (_is_cancelled and _is_cancelled()):
                try:
                    _auto_retry_failed(conn, c, to_check, process_one, _retry_mode, _is_cancelled, run_id)
                except Exception as _re:
                    logger.warning(f"Auto-retry pass error (run continues): {_re}")
                    _retry_mode[0] = False
                # Make sure every status this run settled on has actually reached the Portal
                # (catches any push that failed mid-run) BEFORE the report goes out.
                try:
                    _resync_after_run(conn, c, to_check)
                except Exception as _pe:
                    logger.warning(f"Post-run portal resync error (run continues): {_pe}")

        # Recompute the headline numbers from the FINAL DB state so the auto-retry pass is
        # reflected accurately (retries don't inflate the counters — they were guarded above).
        checked, changed, failed = stats["checked"], stats["changed"], stats["failed"]
        try:
            _keys = [s["row_key"] for s in to_check]
            if _keys:
                _qm = ",".join("?" * len(_keys))
                failed = c.execute(
                    f"SELECT COUNT(*) FROM student_status WHERE row_key IN ({_qm}) "
                    "AND current_status IN ('Unknown','Fetch Error')", _keys).fetchone()[0]
        except Exception:
            pass

        # Write status back to Excel ONLY when an Excel sheet actually exists.
        # In MVS-only mode there is no students.xlsx, so skip (no error).
        if os.path.exists(EXCEL_PATH):
            try:
                write_status_to_excel(EXCEL_PATH, excel_updates)
            except Exception as xe:
                logger.warning(f"Excel write skipped: {xe}")
        # If this run was cancelled mid-way, keep it 'cancelled' (save partial counts).
        cur = c.execute("SELECT status FROM run_logs WHERE id=?", (run_id,)).fetchone()
        if cur and cur["status"] == "cancelled":
            conn.execute("UPDATE run_logs SET total_checked=?, total_changed=?, total_failed=? WHERE id=?",
                         (checked, changed, failed, run_id))
            conn.commit()
            logger.info(f"Run cancelled | Checked:{checked} Changed:{changed} Failed:{failed}")
        else:
            extras = []
            if syc_list:
                extras.append(f"{len(syc_list)} SYC registered")
            if dup_count:
                extras.append(f"{dup_count} duplicate{'s' if dup_count != 1 else ''} merged")
            done_msg = "completed" + (f" ({', '.join(extras)})" if extras else "")
            _finish(conn, run_id, checked, changed, failed, done_msg)
            logger.info(f"Run done | Checked:{checked} Changed:{changed} Failed:{failed} "
                        f"SYC:{len(syc_list)} Dups:{dup_count}")
            # Honest breakdown: separate 'our-side' misses (captcha/proxy — auto-retried) from
            # 'data-side' problems (wrong/pending reference — need a human). Success rate is
            # measured over students we COULD check (excludes pending/no-data references).
            _fc = stats.get("fail_captcha", 0)
            _fd = stats.get("fail_data", 0)
            _fp = stats.get("fail_pending", 0)
            _ok = max(0, checked - failed)
            _checkable = _ok + _fc          # valid, active references (exclude data/pending)
            _rate = (100.0 * _ok / _checkable) if _checkable else 100.0
            logger.info(f"Run breakdown | success:{_ok} captcha/proxy(retryable):{_fc} "
                        f"data-mismatch(needs fix):{_fd} pending/no-status:{_fp} "
                        f"| success-rate-among-checkable: {_rate:.1f}%")
            # Save this run's report breakdown so it can be (re)sent later from Settings.
            try:
                import json as _json
                when = datetime.now().strftime("%d %b, %I:%M %p")
                conn.execute("UPDATE run_logs SET report_json=?, report_label=? WHERE id=?",
                             (_json.dumps(stats), f"{log_group} - {when}", run_id))
                conn.commit()
            except Exception:
                pass
            _send_run_report(stats, log_group)

    except Exception as e:
        logger.error(f"Run failed: {e}")
        _finish(conn, run_id, checked, changed, failed, f"error: {str(e)[:150]}")
    finally:
        conn.close()
        # This run is over (completed, cancelled, or errored) — start the next queued auto-run.
        try:
            _drain_run_queue()
        except Exception as _qe:
            logger.warning(f"Queue drain error: {_qe}")

def _live_report_counts():
    """Live portal totals using the EXACT same definitions as the Excel report Summary,
    so the WhatsApp run-report numbers always match the attached Excel sheet (instead of
    per-run counters, which only count what one run touched)."""
    out = {"confirmed": 0, "required": 0, "error": 0}
    try:
        conn = get_db()
        ND = "COALESCE(deleted,0)=0"
        NF = "COALESCE(login_failed,0)=0 AND COALESCE(check_failed,0)=0"
        def cnt(where):
            return conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND {where}").fetchone()[0]
        out["confirmed"] = cnt(f"is_confirmed=1 AND {NF}")
        out["required"] = cnt(f"current_status='Document Required' AND {NF}")
        out["error"] = cnt("(current_status IN ('Unknown','Fetch Error') "
                           "OR COALESCE(check_failed,0)=1 OR COALESCE(login_failed,0)=1)")
        conn.close()
    except Exception:
        pass
    return out


def _auto_retry_failed(conn, c, to_check, process_one, retry_mode, is_cancelled, run_id):
    """Post-run pass: re-check every student that came back Unknown/Fetch Error, up to a few
    rounds, until their real status comes through. Reuses the run's own per-student handler
    (so WhatsApp, downgrade-protection and doc-caching all behave identically), just without
    touching run stats or the progress bar.

    We do NOT guess upfront which failures are wrong-data — but the login fallback now tells us
    for sure: when NIOS itself rejects the Reference/DOB it is marked data_error=1 and skipped
    here (retrying can't fix wrong data). Everything else is treated as a transient captcha/proxy
    miss and retried until it clears — exactly the by-hand retrying the operator used to do.

    Guards for speed + cost:
      • Rounds capped (RUN_RETRY_ROUNDS, default 8 — some students only clear after several tries).
        A student that succeeds OR is confirmed wrong-data drops out, so rounds shrink fast.
      • Only students with a reference/enrollment AND a DOB (else there's nothing to check with).
      • Runs after the main pass (never in parallel) and respects run cancellation.
    """
    try:
        rounds = int(os.environ.get("RUN_RETRY_ROUNDS", "8"))
    except Exception:
        rounds = 8
    by_key = {s["row_key"]: s for s in to_check if s.get("row_key")}
    if not by_key:
        return
    keys = list(by_key.keys())
    qm = ",".join("?" * len(keys))

    def _count_failing():
        return c.execute(
            f"SELECT COUNT(*) FROM student_status WHERE row_key IN ({qm}) "
            "AND current_status IN ('Unknown','Fetch Error') "
            "AND COALESCE(is_confirmed,0)=0 AND COALESCE(data_error,0)=0 "
            "AND COALESCE(dob,'') != '' "
            "AND (COALESCE(reference_no,'') != '' OR COALESCE(enrollment_no,'') != '')",
            keys).fetchone()[0]

    # The fixable (transient) failures we're about to resolve — this is the retry-phase
    # denominator the dashboard shows live ("X of Y resolved"), so the operator can watch it work.
    initial_failing = _count_failing()
    try:
        c.execute("UPDATE run_logs SET retry_total=?, retry_done=0 WHERE id=?", (initial_failing, run_id))
        conn.commit()
    except Exception:
        pass
    if initial_failing == 0:
        return
    logger.info(f"Auto-retry: {initial_failing} fixable (transient) failure(s) to resolve")

    for rnd in range(max(1, rounds)):
        if is_cancelled and is_cancelled():
            logger.info("Auto-retry: run cancelled — stopping retry pass")
            return
        failing = c.execute(
            f"SELECT row_key FROM student_status WHERE row_key IN ({qm}) "
            "AND current_status IN ('Unknown','Fetch Error') "
            "AND COALESCE(is_confirmed,0)=0 "
            "AND COALESCE(data_error,0)=0 "
            "AND COALESCE(dob,'') != '' "
            "AND (COALESCE(reference_no,'') != '' OR COALESCE(enrollment_no,'') != '')",
            keys).fetchall()
        retry_students = [by_key[r["row_key"]] for r in failing if r["row_key"] in by_key]
        if not retry_students:
            logger.info(f"Auto-retry: all fixable failures cleared after {rnd} round(s)")
            break
        logger.info(f"Auto-retry round {rnd + 1}/{rounds}: re-checking {len(retry_students)} "
                    f"still-Unknown student(s)")
        retry_mode[0] = True
        try:
            scrape_students(retry_students, should_cancel=is_cancelled, on_result=process_one)
        except Exception as e:
            logger.warning(f"Auto-retry round error: {e}")
        finally:
            retry_mode[0] = False
        # Live retry progress: how many of the initial pool are now resolved.
        try:
            done = max(0, initial_failing - _count_failing())
            c.execute("UPDATE run_logs SET retry_done=? WHERE id=?", (done, run_id))
            conn.commit()
        except Exception:
            pass
    # Final tally for the log.
    still = c.execute(
        f"SELECT COUNT(*) FROM student_status WHERE row_key IN ({qm}) "
        "AND current_status IN ('Unknown','Fetch Error')", keys).fetchone()[0]
    logger.info(f"Auto-retry finished | still-Unknown after {rounds} rounds "
                f"(these need a manual Reference/DOB check): {still}")


def _send_run_report(stats, group_label):
    """After a completed run, WhatsApp a summary + Excel-report link to the admin numbers
    set in Settings (only if reporting is enabled). Never breaks the run on failure.
    Records the outcome (report_last_status) so the counsellor can see it in Settings."""
    try:
        if get_setting("report_enabled", "") != "1":
            set_setting("report_last_status", "Reporting is OFF — enable it in Settings to send run reports to management.")
            return
        raw = (get_setting("report_numbers", "") or "").replace("\n", ",")
        nums = [n.strip() for n in raw.split(",") if n.strip()]
        if not nums:
            set_setting("report_last_status", "No WhatsApp numbers added — add management numbers in Settings.")
            return
        import os as _os
        if not _os.environ.get("AISENSY_CAMPAIGN_REPORT", "").strip():
            set_setting("report_last_status", "AISENSY_CAMPAIGN_REPORT env var not set on Railway — report cannot send.")
            return
        import whatsapp, links
        today = datetime.now().strftime("%Y-%m-%d")
        when = datetime.now().strftime("%d %b, %I:%M %p")
        url = links.report_url(today)
        same = max(0, stats.get("checked", 0) - stats.get("changed", 0))
        # Confirmed / Document-Required / Error come from LIVE portal totals (same as the
        # Excel Summary) — NOT per-run counters — so the text always matches the Excel.
        live = _live_report_counts()
        params = whatsapp.make_report_params(
            f"{group_label} - {when}", live["confirmed"], live["required"],
            live["error"], same, stats.get("checked", 0), url)
        sent, errs = whatsapp.send_report_to_all(nums, params)
        stamp = datetime.now().strftime("%d %b %I:%M %p")
        if sent:
            set_setting("report_last_status", f"Sent to {sent}/{len(nums)} on {stamp}."
                        + (" Some failed: " + "; ".join(errs)[:160] if errs else ""))
        else:
            set_setting("report_last_status", f"FAILED to send on {stamp}: " + ("; ".join(errs)[:200] if errs else "unknown error"))
        logger.info(f"Run report: sent to {sent}/{len(nums)} admin(s)."
                    + (" errors: " + "; ".join(errs) if errs else ""))
    except Exception as e:
        try:
            set_setting("report_last_status", f"Error: {str(e)[:160]}")
        except Exception:
            pass
        logger.warning(f"Run report skipped: {e}")

def _finish(conn, run_id, ch, cg, fl, status):
    conn.execute("UPDATE run_logs SET total_checked=?, total_changed=?, total_failed=?, status=? WHERE id=?",
                 (ch, cg, fl, status, run_id))
    conn.commit()

def recheck_one(row_key):
    """Re-run ONE student after the counsellor edits their details. Uses the student's
    CURRENT DB values (the edited ones) — does NOT re-read Excel/MVS, so the fix sticks.
    Re-checks NIOS status, re-verifies the NIOS login, and (if confirmed + login OK)
    sends the WhatsApp documents. Returns a summary dict for the UI."""
    conn = get_db(); c = conn.cursor()
    r = c.execute("SELECT * FROM student_status WHERE row_key=?", (row_key,)).fetchone()
    if not r:
        conn.close()
        return {"ok": False, "error": "Student not found"}
    enr = (r["enrollment_no"] if "enrollment_no" in r.keys() else "") or ""
    student = {
        "row_key": row_key,
        "reference_no": r["reference_no"] or "",
        "enrollment_no": enr,
        "email": r["email"] or "",
        "dob": r["dob"] or "",
        "student_name": r["student_name"] or "",
        "mobile": r["mobile"] or "",
        "alt_mobile": (r["alt_mobile"] if "alt_mobile" in r.keys() else "") or "",
        "class_level": r["class_level"] or "",
        "session": r["session"] or "",
        "toc_status": (r["toc_status"] if "toc_status" in r.keys() else "") or "",
    }
    final_source = (r["source"] if "source" in r.keys() else "") or "mvs_tracker"
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = {"ok": True, "status": "", "login_failed": False, "check_failed": False, "login_remark": "", "whatsapp_sent": False}

    def _cb(res):
        new_status = res["status"]
        new_ref = res.get("discovered_ref") or res.get("reference_no") or student["reference_no"]
        is_conf = 1 if new_status == "Admission Confirmed" else 0
        old = c.execute("SELECT current_status, COALESCE(is_confirmed,0) AS was_confirmed "
                        "FROM student_status WHERE row_key=?", (row_key,)).fetchone()
        old_status = old["current_status"] if old else None
        _was_confirmed = bool(old and old["was_confirmed"] == 1)
        # A failed check (Unknown/Fetch Error) must NOT downgrade a student who already has a
        # real status — keep it, only refresh last_checked. Status only moves forward.
        # A CONFIRMED student is likewise never downgraded by a lower real status (NIOS
        # sometimes flips a confirmed page back to 'Documents Verification In Progress').
        _failed_check = new_status in ("Unknown", "Fetch Error")
        _has_real_old = (old_status not in (None, "", "Unknown", "Fetch Error"))
        _conf_downgrade = (_was_confirmed and not _failed_check
                           and new_status != "Admission Confirmed")
        if _conf_downgrade:
            logger.info(f"  {row_key}: NIOS showed '{new_status}' for a CONFIRMED student — "
                        f"keeping Admission Confirmed (manual run)")
        _ignored_downgrade = (_failed_check and _has_real_old) or _conf_downgrade
        if (old_status != new_status) and not _ignored_downgrade:
            c.execute("""INSERT INTO status_history
                (reference_no, student_name, old_status, new_status, changed_at, run_id, source)
                VALUES (?,?,?,?,?,?,?)""",
                (new_ref, student["student_name"], old_status, new_status, now_s, None, final_source))
        if _ignored_downgrade:
            # Keep the existing real status; only note we checked. Not a failure to surface.
            c.execute("""UPDATE student_status SET reference_no=CASE WHEN ?!='' THEN ? ELSE reference_no END,
                         last_checked=?, check_count=check_count+1, check_failed=0 WHERE row_key=?""",
                      (new_ref, new_ref, now_s, row_key))
            out["status"] = old_status
            out["check_failed"] = False
            conn.commit()
            return
        c.execute("""UPDATE student_status SET reference_no=?, current_status=?, remark=?,
                     is_confirmed=?, last_checked=?, last_verified=?, check_count=check_count+1 WHERE row_key=?""",
                  (new_ref, new_status, res.get("remark", ""), is_conf, now_s,
                   (now_s if new_status not in ("Unknown", "Fetch Error") else
                    (c.execute("SELECT last_verified FROM student_status WHERE row_key=?", (row_key,)).fetchone() or [""])[0]),
                   row_key))
        # Status-check outcome -> 'Failed to Run' visibility (same as a full run).
        if not res.get("success"):
            c.execute("UPDATE student_status SET check_failed=1, login_remark=? WHERE row_key=?",
                      (_CHECK_FAIL_MSG, row_key))
            out["login_remark"] = out["login_remark"] or _CHECK_FAIL_MSG
            out["check_failed"] = True
        else:
            c.execute("UPDATE student_status SET check_failed=0 WHERE row_key=?", (row_key,))
            out["check_failed"] = False
        conn.commit()
        out["status"] = new_status
        # Verify NIOS login before any document share
        login_blocked = False
        if new_status == "Admission Confirmed" and verify_login_autofix is not None:
            ok_login, lmsg, fixed_dob = verify_login_autofix(new_ref, student["dob"], student["enrollment_no"])
            if ok_login:
                if fixed_dob:
                    c.execute("UPDATE student_status SET dob=?, login_failed=0, login_remark='' WHERE row_key=?",
                              (fixed_dob, row_key))
                    student["dob"] = fixed_dob
                else:
                    c.execute("UPDATE student_status SET login_failed=0, login_remark='' WHERE row_key=?", (row_key,))
            else:
                login_blocked = True
                if str(lmsg).startswith("CAPTCHA_BUSY"):
                    # captcha/login gateway busy — NOT wrong data. Skip WhatsApp this time,
                    # keep the confirmed status, do NOT move to 'Failed to Run'.
                    out["login_remark"] = "Captcha/login service busy — try again shortly."
                    logger.warning(f"recheck login skipped (captcha busy) {row_key}")
                else:
                    out["login_failed"] = True
                    _nv = c.execute("SELECT name_verified FROM student_status WHERE row_key=?",
                                    (row_key,)).fetchone()
                    _nm = res.get("nios_name", "")
                    if (not (_nv and _nv["name_verified"] == 1)) and _names_disagree(student["student_name"], _nm):
                        lmsg = (f"\u26a0 Reference No may belong to a DIFFERENT student — NIOS shows "
                                f"'{_nm}' but name here is '{student['student_name']}'. Check the portal: "
                                f"tick 'Name correct' if fine, else update the Reference No.")
                    out["login_remark"] = lmsg
                    c.execute("UPDATE student_status SET login_failed=1, login_remark=? WHERE row_key=?",
                              (lmsg[:240], row_key))
            conn.commit()
        # WhatsApp (force a fresh send for the fixed student)
        if new_status == "Admission Confirmed" and not login_blocked and get_setting("wa_enabled", "0") == "1":
            phone = student["mobile"]
            if phone:
                try:
                    import whatsapp
                    # Save documents on OUR server FIRST, then send — first click opens instantly.
                    try:
                        from main import cache_student_docs
                        cache_student_docs(row_key)
                    except Exception as ce:
                        logger.warning(f"Doc pre-cache skipped {row_key}: {ce}")
                    ok, info = whatsapp.send_for_student({
                        "row_key": row_key, "student_name": student["student_name"],
                        "mobile": phone, "session": student["session"],
                        "alt_mobile": (student["alt_mobile"] if "alt_mobile" in student.keys() else ""),
                        "reference_no": new_ref, "dob": student["dob"],
                        "toc_status": student.get("toc_status", "")})
                    if ok:
                        c.execute("UPDATE student_status SET whatsapp_sent=1, whatsapp_info=? WHERE row_key=?",
                                  (str(info)[:180], row_key))
                        out["whatsapp_sent"] = True
                    else:
                        c.execute("UPDATE student_status SET whatsapp_info=? WHERE row_key=?",
                                  (str(info)[:180], row_key))
                    conn.commit()
                except Exception as we:
                    logger.warning(f"recheck WhatsApp error {row_key}: {we}")
        # Document-Required reminders are sent manually from the "Doc Requests" page after the
        # counsellor reviews them — never auto-sent. Just reset the flag when leaving the state.
        try:
            if new_status != "Document Required":
                c.execute("UPDATE student_status SET required_notified=0 WHERE row_key=? AND COALESCE(required_notified,0)=1", (row_key,))
                conn.commit()
        except Exception as re2:
            logger.warning(f"recheck required-reset error {row_key}: {re2}")

    try:
        scrape_students([student], on_result=_cb)
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)[:200]}
    conn.close()
    return out


# ── Auto-retry for failed/unknown students ──────────────────────────────────
# A student whose data is CORRECT can still fail a check (low captcha score,
# NIOS hiccup, proxy timeout). Instead of the operator pressing "Run now"
# manually, this small sweep re-checks a few of them automatically.
#
# Strict guards so it can never slow the tracker or drain CapSolver credits:
#   • Skips entirely while ANY run is in progress (a new run would cancel it).
#   • Max 10 students per sweep, oldest-checked first (fair rotation).
#   • A student is only retried if last checked > 6 hours ago (max ~4 auto
#     retries per student per day — a genuinely-wrong reference can't loop
#     endlessly, and it stays visible in Failed/Unknown for Edit & fix).
#   • Only students with a valid Reference/Enrollment No (digits, no '@').
#   • Can be disabled with setting auto_retry_failed = '0'.
def _resync_after_run(conn, c, to_check):
    """After a run (and its retry pass), re-push any of THIS run's students whose settled status
    didn't make it to the Portal, so the Portal matches the tracker before the report is sent."""
    if not _mvs_on() or not to_check:
        return
    keys = [s["row_key"] for s in to_check if s.get("row_key")]
    if not keys:
        return
    qm = ",".join("?" * len(keys))
    rows = c.execute(
        f"SELECT row_key, student_id, reference_no, enrollment_no, session, current_status, remark "
        f"FROM student_status WHERE row_key IN ({qm}) AND COALESCE(deleted,0)=0 "
        "AND COALESCE(student_id,'') != '' "
        "AND current_status IN ('Admission Confirmed','Verified','Documents Verification In Progress','Document Required') "
        "AND COALESCE(portal_pushed,'') != current_status", keys).fetchall()
    if not rows:
        return
    logger.info(f"Post-run resync: {len(rows)} student(s) not yet on the Portal — pushing")
    import mvs_sync
    for r in rows:
        try:
            ok = mvs_sync.push_student({
                "student_id": r["student_id"], "row_key": r["row_key"],
                "reference_no": r["reference_no"], "enrollment_no": r["enrollment_no"],
                "discovered_ref": r["reference_no"], "session": r["session"] or "",
                "remark": r["remark"] or ""}, r["current_status"])
            if ok:
                c.execute("UPDATE student_status SET portal_pushed=? WHERE row_key=?",
                          (r["current_status"], r["row_key"]))
                conn.commit()
        except Exception as e:
            logger.warning(f"Post-run resync push error {r['row_key']}: {e}")


def toc_backfill_sweep(max_students=12):
    """Confirmed students are never re-run, so their TOC would otherwise never be read from NIOS.
    This sweep reads the REAL TOC (Previous Subject Details) for a small batch of students who
    (a) have never been TOC-checked yet (nios_toc empty), (b) the Portal marks tocStatus='no',
    and (c) aren't already verified — i.e. exactly the ones at risk of a wrong no-TOC WhatsApp.
    Any mismatch is flagged into 'TOC Status Error' for the counsellor. Once a student's TOC is
    read it's never re-read (one-time per student). Bounded + skips while a run is active."""
    try:
        conn = get_db()
        c = conn.cursor()
        if c.execute("SELECT id FROM run_logs WHERE status='running'").fetchone():
            conn.close()
            return
        rows = c.execute(
            "SELECT row_key, reference_no, enrollment_no, dob FROM student_status "
            "WHERE COALESCE(deleted,0)=0 AND COALESCE(toc_status,'')='no' "
            "AND COALESCE(toc_verified,0)=0 AND COALESCE(nios_toc,'')='' "
            "AND COALESCE(dob,'')!='' "
            "AND (COALESCE(reference_no,'')!='' OR COALESCE(enrollment_no,'')!='') "
            "ORDER BY is_confirmed DESC, last_checked DESC LIMIT ?", (max_students,)).fetchall()
        conn.close()
        if not rows:
            return
        import nios_login
        logger.info(f"TOC backfill: reading NIOS TOC for {len(rows)} not-yet-checked no-TOC student(s)")
        checked = flagged = 0
        for r in rows:
            # stop if a run has since started (don't clash with it)
            cc = get_db()
            if cc.execute("SELECT id FROM run_logs WHERE status='running'").fetchone():
                cc.close(); break
            cc.close()
            try:
                nios_toc, subs, ok = nios_login.fetch_nios_toc(r["reference_no"], r["dob"],
                                                               r["enrollment_no"] or "")
            except Exception as e:
                logger.warning(f"TOC backfill fetch error {r['row_key']}: {e}")
                continue
            if not ok or not nios_toc:
                continue
            checked += 1
            cc = get_db()
            subs_json = json.dumps(subs) if subs else ""
            if nios_toc == "yes":
                cc.execute("UPDATE student_status SET nios_toc='yes', toc_subjects=?, toc_mismatch=1, "
                           "remark=? WHERE row_key=? AND COALESCE(toc_verified,0)=0",
                           (subs_json,
                            "mismatch toc status: NIOS official site says YES, MVS Portal says NO",
                            r["row_key"]))
                flagged += 1
            else:
                cc.execute("UPDATE student_status SET nios_toc='no', toc_mismatch=0 WHERE row_key=?",
                           (r["row_key"],))
            cc.commit(); cc.close()
        logger.info(f"TOC backfill done | checked {checked}, mismatches flagged {flagged}")
    except Exception as e:
        logger.warning(f"TOC backfill sweep error: {e}")


def final_toc_verify(row_key, reference_no="", enrollment_no="", dob="", session=""):
    """LAST-CHANCE TOC check, done the moment a student is confirmed and right before the
    documents go out. It re-reads the student's OWN NIOS page fresh (one captcha) and, if the
    TOC disagrees with what the tracker/Portal hold, applies the NIOS value + pushes it, so the
    WhatsApp can only ever go with the correct campaign.
    Returns the trustworthy TOC ('yes'/'no') or '' if NIOS could not be read (caller then keeps
    whatever it had — it never invents a value)."""
    try:
        conn = get_db()
        r = conn.execute("SELECT reference_no, enrollment_no, dob, session, COALESCE(toc_status,'') "
                         "FROM student_status WHERE row_key=?", (row_key,)).fetchone()
        conn.close()
        ref = reference_no or (r["reference_no"] if r else "")
        enr = enrollment_no or (r["enrollment_no"] if r else "")
        d_o_b = dob or (r["dob"] if r else "")
        sess = session or (r["session"] if r else "")
        if not d_o_b or not (ref or enr):
            return ""
        from scraper import scrape_students
        out = {}

        def _cb(res):
            out.update(res or {})

        scrape_students([{"row_key": row_key, "reference_no": ref, "enrollment_no": enr,
                          "dob": d_o_b, "session": sess}], on_result=_cb)
        nt = (out.get("nios_toc") or "").lower()
        if not nt and out.get("nios_toc_absent"):
            import nios_login as _nl
            if _nl.is_public_session(sess):
                nt = "no"
                out["toc_src"] = ("No Previous-Subject-Details table on the NIOS page — "
                                  "for April/October sessions this means TOC = No")
        if nt not in ("yes", "no"):
            logger.warning(f"Final TOC verify {row_key}: NIOS could not be read — keeping current value")
            return ""
        try:
            from main import _auto_fix_toc
            _auto_fix_toc(row_key, nt, out.get("toc_subjects") or [], out.get("toc_src") or "")
        except Exception as e:
            logger.warning(f"Final TOC verify apply failed {row_key}: {e}")
        conn = get_db()
        cur = conn.execute("SELECT COALESCE(toc_status,'') FROM student_status WHERE row_key=?",
                           (row_key,)).fetchone()
        conn.close()
        final = (cur[0] or "").lower() if cur else nt
        logger.info(f"Final TOC verify {row_key}: NIOS={nt} -> using '{final}' for the campaign")
        return final
    except Exception as e:
        logger.warning(f"Final TOC verify error {row_key}: {e}")
        return ""


def portal_resync_sweep(max_students=150, progress_cb=None):
    """Guarantees the Portal eventually matches the tracker for confirmed/verified/in-progress.
    Finds students whose current NIOS status has NOT been successfully pushed to the Portal yet
    (push failed, Portal was briefly down, or an old confirm predates this tracking) and re-pushes
    them. Bounded and never overlaps a run, so it can't slow the tracker or clash with writes."""
    try:
        if not _mvs_on():
            return
        conn = get_db()
        c = conn.cursor()
        active = c.execute("SELECT id FROM run_logs WHERE status='running'").fetchone()
        if active:
            conn.close()
            return
        # Students with a real, mapped status that the Portal doesn't have yet:
        #   portal_pushed is NULL/empty  -> never pushed
        #   portal_pushed != current_status -> status changed since last successful push
        rows = c.execute(
            "SELECT row_key, student_id, reference_no, enrollment_no, session, current_status, remark "
            "FROM student_status WHERE COALESCE(deleted,0)=0 "
            "AND COALESCE(student_id,'') != '' "
            "AND current_status IN ('Admission Confirmed','Verified','Documents Verification In Progress','Document Required') "
            "AND COALESCE(portal_pushed,'') != current_status "
            "ORDER BY (current_status='Admission Confirmed') DESC, last_changed DESC "
            "LIMIT ?", (max_students,)).fetchall()
        conn.close()
        if not rows:
            return
        logger.info(f"Portal resync: {len(rows)} student(s) whose status the Portal is missing — re-pushing")
        import mvs_sync
        pushed = 0
        done = 0
        for r in rows:
            try:
                ok = mvs_sync.push_student({
                    "student_id": r["student_id"], "row_key": r["row_key"],
                    "reference_no": r["reference_no"], "enrollment_no": r["enrollment_no"],
                    "discovered_ref": r["reference_no"], "session": r["session"] or "",
                    "remark": r["remark"] or ""}, r["current_status"])
                if ok:
                    cc = get_db()
                    cc.execute("UPDATE student_status SET portal_pushed=? WHERE row_key=?",
                               (r["current_status"], r["row_key"]))
                    cc.commit(); cc.close()
                    pushed += 1
            except Exception as e:
                logger.warning(f"Portal resync push error {r['row_key']}: {e}")
            done += 1
            if progress_cb:
                try:
                    progress_cb(done, len(rows), pushed)
                except Exception:
                    pass
        logger.info(f"Portal resync done | re-pushed {pushed}/{len(rows)}")
        # Also retry any TOC auto-corrections whose Portal push failed at the time.
        try:
            conn2 = get_db()
            trows = conn2.execute(
                "SELECT l.id, l.row_key, l.new_toc, l.subjects, s.student_id "
                "FROM toc_fix_log l JOIN student_status s ON s.row_key = l.row_key "
                "WHERE COALESCE(l.pushed,0)=0 AND COALESCE(s.student_id,'') != '' "
                "ORDER BY l.id DESC LIMIT 25").fetchall()
            conn2.close()
            if trows:
                tp = 0
                for t in trows:
                    try:
                        subs = [x.strip() for x in (t["subjects"] or "").split(",") if x.strip()]
                        if mvs_sync.push_toc(t["student_id"], t["new_toc"], subs):
                            cx = get_db()
                            cx.execute("UPDATE toc_fix_log SET pushed=1 WHERE id=?", (t["id"],))
                            cx.commit(); cx.close()
                            tp += 1
                    except Exception as te:
                        logger.warning(f"TOC push retry error {t['row_key']}: {te}")
                logger.info(f"TOC push retry | {tp}/{len(trows)} pending corrections pushed")
        except Exception as e2:
            logger.warning(f"TOC push retry sweep error: {e2}")
    except Exception as e:
        logger.warning(f"Portal resync sweep error: {e}")


def auto_retry_failed_sweep(max_students=10, min_age_hours=6):
    try:
        if get_setting("auto_retry_failed", "1") != "1":
            return
        conn = get_db()
        c = conn.cursor()
        # Never overlap a run — starting one would cancel the active run.
        active = c.execute("SELECT id FROM run_logs WHERE status='running'").fetchone()
        if active:
            conn.close()
            logger.info("Auto-retry sweep: a run is in progress — skipping this sweep")
            return
        cutoff = (datetime.now() - timedelta(hours=min_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
        rows = c.execute(
            "SELECT row_key, reference_no, enrollment_no FROM student_status "
            "WHERE COALESCE(deleted,0)=0 AND COALESCE(is_confirmed,0)=0 "
            "AND current_status IN ('Fetch Error','Unknown') "
            "AND COALESCE(last_checked,'') != '' AND last_checked <= ? "
            "ORDER BY last_checked ASC LIMIT 60", (cutoff,)).fetchall()
        conn.close()

        def _ok_id(v):
            v = (v or "").strip()
            return bool(v) and ("@" not in v) and any(ch.isdigit() for ch in v)

        keys = [r["row_key"] for r in rows
                if _ok_id(r["reference_no"]) or _ok_id(r["enrollment_no"])][:max_students]
        if not keys:
            logger.info("Auto-retry sweep: nothing eligible to retry")
            return
        logger.info(f"Auto-retry sweep: re-checking {len(keys)} failed/unknown student(s)")
        run_status_check("all", None, "selected", keys)
    except Exception as e:
        logger.warning(f"Auto-retry sweep error: {e}")
