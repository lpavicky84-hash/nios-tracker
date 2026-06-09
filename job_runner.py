import logging
import os
from datetime import datetime
from database import get_db, init_db
from scraper import scrape_all_students
from excel_handler import read_students_from_excel, write_status_to_excel

logger = logging.getLogger(__name__)

EXCEL_PATH = os.environ.get("EXCEL_PATH", "students.xlsx")

def run_status_check():
    """
    Main job: read Excel → scrape NIOS → compare with DB → update Excel + DB.
    Called by scheduler every 6 hours.
    """
    logger.info("═══════════════════════════════════════")
    logger.info(f"🚀 Run started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("═══════════════════════════════════════")

    if not os.path.exists(EXCEL_PATH):
        logger.error(f"Excel file not found: {EXCEL_PATH}")
        return

    conn = get_db()
    c = conn.cursor()

    # Create run log entry
    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "INSERT INTO run_logs (run_at, status) VALUES (?, 'running')",
        (run_at,)
    )
    conn.commit()
    run_id = c.lastrowid

    total_checked = 0
    total_changed = 0
    total_failed  = 0
    updates_for_excel = []

    try:
        # 1. Read students from Excel
        students = read_students_from_excel(EXCEL_PATH)
        if not students:
            logger.warning("No students found in Excel")
            _finish_run(conn, run_id, 0, 0, 0, "No students in Excel")
            return

        reference_numbers = [s["reference_no"] for s in students]
        ref_to_student = {s["reference_no"]: s for s in students}

        # 2. Scrape NIOS for all reference numbers
        scrape_results = scrape_all_students(reference_numbers)

        # 3. Process each result
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for res in scrape_results:
            ref_no       = res["reference_no"]
            new_status   = res["status"]
            success      = res["success"]
            student_info = ref_to_student.get(ref_no, {})

            total_checked += 1
            if not success:
                total_failed += 1

            # Get old status from DB
            row = c.execute(
                "SELECT current_status FROM student_status WHERE reference_no = ?",
                (ref_no,)
            ).fetchone()

            old_status   = row["current_status"] if row else None
            status_changed = (old_status != new_status)

            if status_changed:
                total_changed += 1
                # Log history
                c.execute(
                    """INSERT INTO status_history
                       (reference_no, student_name, old_status, new_status, changed_at, run_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (ref_no, student_info.get("student_name", ""),
                     old_status, new_status, now_str, run_id)
                )

            # Upsert student_status
            c.execute(
                """INSERT INTO student_status
                       (reference_no, student_name, class_level, current_status,
                        last_checked, last_changed, check_count)
                   VALUES (?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(reference_no) DO UPDATE SET
                       current_status = excluded.current_status,
                       last_checked   = excluded.last_checked,
                       last_changed   = CASE WHEN current_status != excluded.current_status
                                             THEN excluded.last_changed
                                             ELSE last_changed END,
                       check_count    = check_count + 1""",
                (ref_no,
                 student_info.get("student_name", ""),
                 student_info.get("class_level", ""),
                 new_status, now_str, now_str)
            )

            updates_for_excel.append({
                "reference_no":  ref_no,
                "status_label":  new_status,
                "last_checked":  now_str,
                "changed":       status_changed,
            })

        conn.commit()

        # 4. Write all updates to Excel
        write_status_to_excel(EXCEL_PATH, updates_for_excel)

        _finish_run(conn, run_id, total_checked, total_changed, total_failed, "completed")
        logger.info(f"✅ Run complete | Checked: {total_checked} | Changed: {total_changed} | Failed: {total_failed}")

    except Exception as e:
        logger.error(f"❌ Run failed: {e}")
        _finish_run(conn, run_id, total_checked, total_changed, total_failed, f"error: {str(e)[:200]}")
    finally:
        conn.close()

def _finish_run(conn, run_id, checked, changed, failed, status):
    conn.execute(
        """UPDATE run_logs SET total_checked=?, total_changed=?, total_failed=?, status=?
           WHERE id=?""",
        (checked, changed, failed, status, run_id)
    )
    conn.commit()
