import os
import sqlite3
from datetime import datetime, timezone, timedelta

# Persist data on a mounted volume when DATA_DIR is set (Railway volume).
# Falls back to the current folder for local runs.
DATA_DIR = os.environ.get("DATA_DIR", ".")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = "."
DB_PATH = os.path.join(DATA_DIR, "nios_tracker.db")

# India Standard Time (UTC+5:30) — Railway runs in UTC
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(IST)

def now_ist_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=8000")
    except Exception:
        pass
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS run_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            group_type TEXT DEFAULT 'all',
            total_checked INTEGER DEFAULT 0,
            total_changed INTEGER DEFAULT 0,
            total_failed INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running',
            progress_current INTEGER DEFAULT 0,
            progress_total INTEGER DEFAULT 0,
            progress_changed INTEGER DEFAULT 0,
            progress_same INTEGER DEFAULT 0,
            notes TEXT
        )
    """)
    # Safe migration for older DBs missing the progress columns
    for col in ("progress_current", "progress_total", "progress_changed", "progress_same"):
        try:
            c.execute(f"ALTER TABLE run_logs ADD COLUMN {col} INTEGER DEFAULT 0")
        except Exception:
            pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_no TEXT,
            student_name TEXT,
            old_status TEXT,
            new_status TEXT,
            changed_at TEXT NOT NULL,
            run_id INTEGER,
            source TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS student_status (
            row_key TEXT PRIMARY KEY,
            reference_no TEXT,
            email TEXT,
            dob TEXT,
            student_name TEXT,
            mobile TEXT,
            class_level TEXT,
            session TEXT,
            current_status TEXT,
            remark TEXT,
            id_card_link TEXT,
            app_form_link TEXT,
            hall_ticket_link TEXT,
            is_confirmed INTEGER DEFAULT 0,
            last_checked TEXT,
            last_changed TEXT,
            check_count INTEGER DEFAULT 0,
            whatsapp_sent INTEGER DEFAULT 0,
            whatsapp_info TEXT,
            enrollment_no TEXT,
            source TEXT DEFAULT 'mvs_tracker',
            cross_dup INTEGER DEFAULT 0
        )
    """)
    # Safe migration for older DBs missing the WhatsApp / enrollment / source columns
    for col, decl in (("whatsapp_sent", "INTEGER DEFAULT 0"), ("whatsapp_info", "TEXT"),
                      ("enrollment_no", "TEXT"),
                      ("source", "TEXT DEFAULT 'mvs_tracker'"),
                      ("cross_dup", "INTEGER DEFAULT 0")):
        try:
            c.execute(f"ALTER TABLE student_status ADD COLUMN {col} {decl}")
        except Exception:
            pass

    # Settings table for interval config
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Default intervals
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('interval_regular', '6')")
    c.execute("""
        CREATE TABLE IF NOT EXISTS short_links (
            code TEXT PRIMARY KEY,
            row_key TEXT,
            kind TEXT,
            created TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('interval_public', '12')")
    # WhatsApp auto-send disabled until configured + turned on from the portal
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('wa_enabled', '0')")

    # Safe migration: status_history.source (older DBs). Store source per history row
    # so the History page never has to guess via a fragile reference-no lookup.
    try:
        c.execute("ALTER TABLE status_history ADD COLUMN source TEXT")
    except Exception:
        pass
    # Backfill existing history rows from student_status (by reference_no, then by name).
    try:
        c.execute("""UPDATE status_history SET source = (
                       SELECT ss.source FROM student_status ss
                       WHERE ss.reference_no != '' AND ss.reference_no = status_history.reference_no
                         AND ss.source IS NOT NULL LIMIT 1)
                     WHERE (source IS NULL OR source='') AND reference_no != ''""")
        c.execute("""UPDATE status_history SET source = (
                       SELECT ss.source FROM student_status ss
                       WHERE ss.student_name != '' AND ss.student_name = status_history.student_name
                         AND ss.source IS NOT NULL LIMIT 1)
                     WHERE (source IS NULL OR source='')""")
    except Exception:
        pass

    conn.commit()
    conn.close()
    print("Database initialized")

def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = get_db()
    conn.execute("INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=?",
                 (key, str(value), str(value)))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
