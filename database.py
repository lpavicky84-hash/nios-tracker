import sqlite3
import os
from datetime import datetime

DB_PATH = "nios_tracker.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    # Run logs table
    c.execute("""
        CREATE TABLE IF NOT EXISTS run_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            total_checked INTEGER DEFAULT 0,
            total_changed INTEGER DEFAULT 0,
            total_failed INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running',
            notes TEXT
        )
    """)

    # Student status history
    c.execute("""
        CREATE TABLE IF NOT EXISTS status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_no TEXT NOT NULL,
            student_name TEXT,
            old_status TEXT,
            new_status TEXT,
            changed_at TEXT NOT NULL,
            run_id INTEGER
        )
    """)

    # Current student statuses cache
    c.execute("""
        CREATE TABLE IF NOT EXISTS student_status (
            reference_no TEXT PRIMARY KEY,
            student_name TEXT,
            class_level TEXT,
            current_status TEXT,
            last_checked TEXT,
            last_changed TEXT,
            check_count INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()
    print("✅ Database initialized")

if __name__ == "__main__":
    init_db()
