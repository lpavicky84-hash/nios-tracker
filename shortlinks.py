"""
Short links for student document URLs.
Turns the long signed token URL into a compact /s/<code> link, e.g.
  https://web-production-09671.up.railway.app/s/Ab3kP9
The code maps to (row_key, kind) in the DB so the same per-document link is reused.
"""
import secrets
import string
from database import get_db

_ALPHABET = string.ascii_letters + string.digits


def _ensure_table():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS short_links (
        code TEXT PRIMARY KEY,
        row_key TEXT,
        kind TEXT,
        created TEXT DEFAULT (datetime('now'))
    )""")
    conn.commit()
    conn.close()


def create_short(row_key, kind):
    """Return a short code for (row_key, kind), reusing an existing one if present."""
    _ensure_table()
    conn = get_db()
    row = conn.execute("SELECT code FROM short_links WHERE row_key=? AND kind=?",
                       (row_key, kind)).fetchone()
    if row:
        conn.close()
        return row["code"]
    for _ in range(8):
        code = "".join(secrets.choice(_ALPHABET) for _ in range(6))
        try:
            conn.execute("INSERT INTO short_links (code, row_key, kind) VALUES (?,?,?)",
                         (code, row_key, kind))
            conn.commit()
            conn.close()
            return code
        except Exception:
            continue   # collision, try another
    conn.close()
    return None


def resolve_short(code):
    """Return (row_key, kind) for a code, or (None, None)."""
    try:
        conn = get_db()
        row = conn.execute("SELECT row_key, kind FROM short_links WHERE code=?",
                           (code,)).fetchone()
        conn.close()
        return (row["row_key"], row["kind"]) if row else (None, None)
    except Exception:
        return (None, None)
