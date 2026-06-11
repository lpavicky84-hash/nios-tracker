import logging
import os
import time as _time
os.environ["TZ"] = "Asia/Kolkata"
try:
    _time.tzset()
except Exception:
    pass
from datetime import datetime
from database import get_db
from scraper import scrape_students
from excel_handler import read_students_from_excel, write_status_to_excel, dedupe_students

logger = logging.getLogger(__name__)
EXCEL_PATH = os.environ.get("EXCEL_PATH", os.path.join(os.environ.get("DATA_DIR", "."), "students.xlsx"))

# Which sessions belong to "public exam" group (April/October + year)
def is_public_session(session):
    s = (session or "").lower()
    return ("april" in s) or ("october" in s) or ("public" in s)

def session_group(session):
    return "public" if is_public_session(session) else "regular"

def run_status_check(group_type="all"):
    """
    group_type: 'all' | 'regular' | 'public'
    Skips students already 'Admission Confirmed'.
    Processes in batches with delays (handled inside scraper).
    """
    logger.info("=" * 50)
    logger.info(f"Run started [{group_type}] at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if not os.path.exists(EXCEL_PATH):
        logger.error(f"Excel not found: {EXCEL_PATH}")
        return

    conn = get_db()
    c = conn.cursor()
    # Auto-cancel any previously 'running' run so two checks never overlap.
    # The old run's worker polls its own status and stops cooperatively.
    prev = c.execute("SELECT id FROM run_logs WHERE status='running'").fetchall()
    if prev:
        c.execute("UPDATE run_logs SET status='cancelled' WHERE status='running'")
        conn.commit()
        logger.info(f"Auto-cancelled {len(prev)} previous running run(s)")
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO run_logs (run_at, group_type, status) VALUES (?,?, 'running')",
              (run_at, group_type))
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
        all_students = read_students_from_excel(EXCEL_PATH)
        # Drop duplicate rows from the uploaded Excel (same student twice) so the
        # run only checks new/unique students, never the duplicates.
        all_students, dup_count = dedupe_students(all_students)
        if dup_count:
            logger.info(f"Skipped {dup_count} duplicate row(s) from Excel")

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
        # Filter by group; confirmed students are re-checked in the 'public'
        # (slower) job so NIOS detail changes are still caught — not skipped forever.
        to_check = []
        for s in all_students:
            row = c.execute("SELECT is_confirmed FROM student_status WHERE row_key=?",
                            (s["row_key"],)).fetchone()
            confirmed = bool(row and row["is_confirmed"] == 1)
            grp = session_group(s["session"])
            if group_type == "regular":
                if confirmed:
                    continue                      # confirmed -> handled by public job
                if grp != "regular":
                    continue
            elif group_type == "public":
                if not confirmed and grp != "public":
                    continue                      # public job: public-session active + ALL confirmed
            # group_type == "all": check everyone
            to_check.append(s)

        logger.info(f"{len(to_check)} students to check (group={group_type})")

        if not to_check:
            _finish(conn, run_id, 0, 0, 0, "Nothing to check")
            return

        # Record how many students this run will check (for the live progress bar)
        conn.execute("UPDATE run_logs SET progress_total=?, progress_current=0, "
                     "progress_changed=0, progress_same=0 WHERE id=?",
                     (len(to_check), run_id))
        conn.commit()

        stats = {"checked": 0, "changed": 0, "same": 0, "failed": 0}

        def process_one(res):
            """Persist ONE student's result immediately so the dashboard/filters
            update live as the run progresses (not all at the end)."""
            stats["checked"] += 1
            if not res.get("success"):
                stats["failed"] += 1
            row_key = res["row_key"]
            new_status = res["status"]
            new_ref = res.get("discovered_ref") or res.get("reference_no") or ""
            is_conf = 1 if new_status == "Admission Confirmed" else 0
            now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            old = c.execute("SELECT current_status FROM student_status WHERE row_key=?",
                            (row_key,)).fetchone()
            old_status = old["current_status"] if old else None
            status_changed = (old_status != new_status)
            if status_changed:
                stats["changed"] += 1
                c.execute("""INSERT INTO status_history
                    (reference_no, student_name, old_status, new_status, changed_at, run_id)
                    VALUES (?,?,?,?,?,?)""",
                    (new_ref, res.get("student_name", ""), old_status, new_status, now_s, run_id))
            else:
                stats["same"] += 1

            c.execute("""INSERT INTO student_status
                (row_key, reference_no, email, dob, student_name, mobile, class_level,
                 session, current_status, remark, is_confirmed, last_checked, last_changed, check_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(row_key) DO UPDATE SET
                    reference_no = CASE WHEN excluded.reference_no != '' THEN excluded.reference_no ELSE reference_no END,
                    current_status = excluded.current_status,
                    remark = excluded.remark,
                    is_confirmed = excluded.is_confirmed,
                    last_checked = excluded.last_checked,
                    last_changed = CASE WHEN current_status != excluded.current_status
                                        THEN excluded.last_changed ELSE last_changed END,
                    check_count = check_count + 1""",
                (row_key, new_ref, res.get("email", ""), res.get("dob", ""),
                 res.get("student_name", ""), res.get("mobile", ""), res.get("class_level", ""),
                 res.get("session", ""), new_status, res.get("remark", ""), is_conf, now_s, now_s))

            if new_ref:
                c.execute("DELETE FROM student_status WHERE reference_no=? AND row_key!=?",
                          (new_ref, row_key))

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
            c.execute("UPDATE run_logs SET progress_current=?, progress_changed=?, progress_same=? WHERE id=?",
                      (stats["checked"], stats["changed"], stats["same"], run_id))
            conn.commit()

        scrape_students(to_check, should_cancel=_is_cancelled, on_result=process_one)
        checked, changed, failed = stats["checked"], stats["changed"], stats["failed"]

        write_status_to_excel(EXCEL_PATH, excel_updates)
        # If this run was cancelled mid-way, keep it 'cancelled' (save partial counts).
        cur = c.execute("SELECT status FROM run_logs WHERE id=?", (run_id,)).fetchone()
        if cur and cur["status"] == "cancelled":
            conn.execute("UPDATE run_logs SET total_checked=?, total_changed=?, total_failed=? WHERE id=?",
                         (checked, changed, failed, run_id))
            conn.commit()
            logger.info(f"Run cancelled | Checked:{checked} Changed:{changed} Failed:{failed}")
        else:
            _finish(conn, run_id, checked, changed, failed, "completed")
            logger.info(f"Run done | Checked:{checked} Changed:{changed} Failed:{failed}")

    except Exception as e:
        logger.error(f"Run failed: {e}")
        _finish(conn, run_id, checked, changed, failed, f"error: {str(e)[:150]}")
    finally:
        conn.close()

def _finish(conn, run_id, ch, cg, fl, status):
    conn.execute("UPDATE run_logs SET total_checked=?, total_changed=?, total_failed=?, status=? WHERE id=?",
                 (ch, cg, fl, status, run_id))
    conn.commit()
