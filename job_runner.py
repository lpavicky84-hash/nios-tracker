import logging
import os
import time as _time
os.environ["TZ"] = "Asia/Kolkata"
try:
    _time.tzset()
except Exception:
    pass
from datetime import datetime
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


def run_status_check(group_type="all", source_only=None, scope=None, only_keys=None):
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
    # Auto-cancel any previously 'running' run so two checks never overlap.
    # The old run's worker polls its own status and stops cooperatively.
    prev = c.execute("SELECT id FROM run_logs WHERE status='running'").fetchall()
    if prev:
        c.execute("UPDATE run_logs SET status='cancelled' WHERE status='running'")
        conn.commit()
        logger.info(f"Auto-cancelled {len(prev)} previous running run(s)")
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
                 "verified": 0, "docs_progress": 0}

        # Register SYC students first (fast; no NIOS status check) with live progress.
        if syc_list:
            _process_syc(conn, c, syc_list, run_id, stats)

        def process_one(res):
            """Persist ONE student's result immediately so the dashboard/filters
            update live as the run progresses (not all at the end)."""
            stats["checked"] += 1
            if not res.get("success"):
                stats["failed"] += 1
            row_key = res["row_key"]
            new_status = res["status"]
            # Per-run outcome buckets for the WhatsApp report.
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

            old = c.execute("SELECT current_status, source FROM student_status WHERE row_key=?",
                            (row_key,)).fetchone()
            old_status = old["current_status"] if old else None
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
            status_changed = (old_status != new_status)
            if status_changed:
                stats["changed"] += 1
                c.execute("""INSERT INTO status_history
                    (reference_no, student_name, old_status, new_status, changed_at, run_id, source)
                    VALUES (?,?,?,?,?,?,?)""",
                    (new_ref, res.get("student_name", ""), old_status, new_status, now_s, run_id, final_source))
            else:
                stats["same"] += 1

            c.execute("""INSERT INTO student_status
                (row_key, reference_no, enrollment_no, email, dob, student_name, mobile, alt_mobile, toc_status, class_level,
                 session, current_status, remark, is_confirmed, last_checked, last_changed,
                 source, cross_dup, check_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
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
                    current_status = excluded.current_status,
                    remark = excluded.remark,
                    is_confirmed = excluded.is_confirmed,
                    last_checked = excluded.last_checked,
                    last_changed = CASE WHEN current_status != excluded.current_status
                                        THEN excluded.last_changed ELSE last_changed END,
                    source = excluded.source,
                    cross_dup = CASE WHEN excluded.cross_dup=1 THEN 1 ELSE cross_dup END,
                    check_count = check_count + 1""",
                (row_key, new_ref, res.get("enrollment_no", ""), res.get("email", ""), res.get("dob", ""),
                 res.get("student_name", ""), res.get("mobile", ""), alt_by_key.get(row_key, ""),
                 toc_by_key.get(row_key, ""),
                 res.get("class_level", ""),
                 res.get("session", ""), new_status, res.get("remark", ""), is_conf, now_s, now_s,
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
            if new_status == "Unknown":
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
                c.execute("UPDATE student_status SET check_failed=0 WHERE row_key=?", (row_key,))

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
                        ok, info = whatsapp.send_for_student({
                            "row_key": row_key,
                            "student_name": res.get("student_name", ""),
                            "mobile": phone,
                            "alt_mobile": _eff_alt,
                            "session": res.get("session", ""),
                            "reference_no": new_ref,
                            "dob": res.get("dob", ""),
                        })
                        # Only mark as sent on success; failures retry next run.
                        if ok:
                            c.execute("UPDATE student_status SET whatsapp_sent=1, whatsapp_info=?, "
                                      "whatsapp_sent_at=?, whatsapp_delivery='' WHERE row_key=?",
                                      (str(info)[:180], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), row_key))
                        else:
                            c.execute("UPDATE student_status SET whatsapp_info=? WHERE row_key=?",
                                      (str(info)[:180], row_key))
                        conn.commit()
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

            # ── MVS portal: push status + doc links back (only if login isn't broken) ──
            try:
                if mvs_on and not login_blocked and sid_by_key.get(row_key):
                    conn.commit()   # ensure student row + links are persisted first
                    mvs_sync.push_student({**res,
                        "student_id": sid_by_key[row_key], "row_key": row_key,
                        "discovered_ref": new_ref, "session": res.get("session", "")},
                        new_status, conn)
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
            _s = src_by_key.get(row_key, "mvs_tracker")
            _col = "progress_done_mvs" if _s == "mvs_portal" else "progress_done_trk"
            c.execute(f"UPDATE run_logs SET progress_current=?, progress_changed=?, "
                      f"progress_same=?, {_col}={_col}+1 WHERE id=?",
                      (stats["checked"], stats["changed"], stats["same"], run_id))
            conn.commit()

        if to_check:
            scrape_students(to_check, should_cancel=_is_cancelled, on_result=process_one)
        checked, changed, failed = stats["checked"], stats["changed"], stats["failed"]

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
        old = c.execute("SELECT current_status FROM student_status WHERE row_key=?", (row_key,)).fetchone()
        old_status = old["current_status"] if old else None
        if old_status != new_status:
            c.execute("""INSERT INTO status_history
                (reference_no, student_name, old_status, new_status, changed_at, run_id, source)
                VALUES (?,?,?,?,?,?,?)""",
                (new_ref, student["student_name"], old_status, new_status, now_s, None, final_source))
        c.execute("""UPDATE student_status SET reference_no=?, current_status=?, remark=?,
                     is_confirmed=?, last_checked=?, check_count=check_count+1 WHERE row_key=?""",
                  (new_ref, new_status, res.get("remark", ""), is_conf, now_s, row_key))
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
