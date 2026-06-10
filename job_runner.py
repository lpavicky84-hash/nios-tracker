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
from excel_handler import read_students_from_excel, write_status_to_excel

logger = logging.getLogger(__name__)
EXCEL_PATH = os.environ.get("EXCEL_PATH", "students.xlsx")

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
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO run_logs (run_at, group_type, status) VALUES (?,?, 'running')",
              (run_at, group_type))
    conn.commit()
    run_id = c.lastrowid

    checked = changed = failed = 0
    excel_updates = []

    try:
        all_students = read_students_from_excel(EXCEL_PATH)

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

        results = scrape_students(to_check)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for res in results:
            checked += 1
            if not res.get("success"):
                failed += 1

            row_key = res["row_key"]
            new_status = res["status"]
            new_ref = res.get("discovered_ref") or res.get("reference_no") or ""
            is_conf = 1 if new_status == "Admission Confirmed" else 0

            old = c.execute("SELECT current_status FROM student_status WHERE row_key=?",
                            (row_key,)).fetchone()
            old_status = old["current_status"] if old else None
            status_changed = (old_status != new_status)

            if status_changed:
                changed += 1
                c.execute("""INSERT INTO status_history
                    (reference_no, student_name, old_status, new_status, changed_at, run_id)
                    VALUES (?,?,?,?,?,?)""",
                    (new_ref, res.get("student_name", ""), old_status, new_status, now, run_id))

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
                 res.get("session", ""), new_status, res.get("remark", ""), is_conf, now, now))

            excel_updates.append({
                "row_key": row_key,
                "reference_no": new_ref,
                "email": res.get("email", ""),
                "status_label": new_status,
                "remark": res.get("remark", ""),
                "last_checked": now,
                "changed": status_changed,
            })

        conn.commit()
        write_status_to_excel(EXCEL_PATH, excel_updates)
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
