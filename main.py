import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import FileResponse, HTMLResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import aiofiles

from database import init_db, get_db
from job_runner import run_status_check

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
SECRET_KEY  = os.environ.get("SECRET_KEY",  "nios-tracker-secret-2025-mvs")
PORTAL_USER = os.environ.get("PORTAL_USER", "admin")
PORTAL_PASS = os.environ.get("PORTAL_PASS", "MVS2025")
EXCEL_PATH  = os.environ.get("EXCEL_PATH",  "students.xlsx")
ALGORITHM   = "HS256"
TOKEN_EXPIRE_HOURS = 12

pwd_ctx  = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer   = HTTPBearer()

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="NIOS Status Tracker", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth helpers ──────────────────────────────────────────────────────────────
def create_token(username: str) -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") != PORTAL_USER:
            raise HTTPException(status_code=401, detail="Invalid token")
        return payload["sub"]
    except JWTError:
        raise HTTPException(status_code=401, detail="Token expired or invalid")

# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler()

@app.on_event("startup")
async def startup():
    init_db()
    scheduler.add_job(
        run_status_check,
        trigger=IntervalTrigger(hours=6),
        id="nios_check",
        replace_existing=True,
        next_run_time=None
    )
    scheduler.start()
    logger.info("Scheduler started — runs every 6 hours")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)

# ── Routes ────────────────────────────────────────────────────────────────────

def find_portal_html():
    """Find portal.html in multiple possible locations."""
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, "portal.html"),
        "portal.html",
        "/app/portal.html",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

@app.get("/", response_class=HTMLResponse)
async def serve_portal():
    path = find_portal_html()
    if path:
        async with aiofiles.open(path, "r") as f:
            return await f.read()
    return HTMLResponse("<h1>portal.html not found</h1>", status_code=404)

@app.get("/health")
async def health():
    return {"status": "ok", "portal_html": find_portal_html() is not None}

@app.post("/api/login")
async def login(body: dict):
    username = body.get("username", "")
    password = body.get("password", "")
    if username != PORTAL_USER or password != PORTAL_PASS:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"token": create_token(username), "username": username}

@app.get("/api/dashboard")
async def dashboard(user=Depends(verify_token)):
    conn = get_db()

    total_students = conn.execute("SELECT COUNT(*) FROM student_status").fetchone()[0]

    runs = conn.execute(
        "SELECT * FROM run_logs ORDER BY id DESC LIMIT 10"
    ).fetchall()

    status_dist = conn.execute(
        "SELECT current_status, COUNT(*) as cnt FROM student_status GROUP BY current_status"
    ).fetchall()

    job = scheduler.get_job("nios_check")
    next_run = str(job.next_run_time) if job and job.next_run_time else "Not scheduled"

    conn.close()
    return {
        "total_students":      total_students,
        "next_run":            next_run,
        "status_distribution": [dict(r) for r in status_dist],
        "recent_runs":         [dict(r) for r in runs],
    }

@app.get("/api/students")
async def get_students(
    page: int = 1,
    per_page: int = 50,
    search: str = "",
    status_filter: str = "",
    user=Depends(verify_token)
):
    conn = get_db()
    offset = (page - 1) * per_page

    where_clauses = []
    params = []
    if search:
        where_clauses.append("(reference_no LIKE ? OR student_name LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if status_filter:
        where_clauses.append("current_status = ?")
        params.append(status_filter)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM student_status {where_sql}", params
    ).fetchone()[0]

    students = conn.execute(
        f"SELECT * FROM student_status {where_sql} ORDER BY student_name LIMIT ? OFFSET ?",
        params + [per_page, offset]
    ).fetchall()

    conn.close()
    return {
        "students": [dict(s) for s in students],
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    (total + per_page - 1) // per_page,
    }

@app.get("/api/history")
async def get_history(limit: int = 100, user=Depends(verify_token)):
    conn = get_db()
    history = conn.execute(
        "SELECT * FROM status_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(h) for h in history]

@app.get("/api/run-logs")
async def get_run_logs(limit: int = 50, user=Depends(verify_token)):
    conn = get_db()
    logs = conn.execute(
        "SELECT * FROM run_logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(l) for l in logs]

@app.post("/api/run-now")
async def trigger_run_now(background_tasks: BackgroundTasks, user=Depends(verify_token)):
    background_tasks.add_task(run_status_check)
    return {"message": "Run triggered! Check dashboard for progress."}

@app.post("/api/upload-excel")
async def upload_excel(file: UploadFile = File(...), user=Depends(verify_token)):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only .xlsx files allowed")
    async with aiofiles.open(EXCEL_PATH, "wb") as f:
        content = await file.read()
        await f.write(content)
    return {"message": f"Excel uploaded successfully ({len(content)} bytes)", "filename": file.filename}

@app.get("/api/download-excel")
async def download_excel(user=Depends(verify_token)):
    if not os.path.exists(EXCEL_PATH):
        raise HTTPException(status_code=404, detail="Excel file not found")
    return FileResponse(
        EXCEL_PATH,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="nios_status_updated.xlsx"
    )

@app.get("/api/scheduler-status")
async def scheduler_status(user=Depends(verify_token)):
    job = scheduler.get_job("nios_check")
    return {
        "running":  scheduler.running,
        "next_run": str(job.next_run_time) if job and job.next_run_time else None,
        "job_id":   "nios_check"
    }

@app.post("/api/reschedule")
async def reschedule(body: dict, user=Depends(verify_token)):
    hours = int(body.get("hours", 6))
    if hours < 1 or hours > 24:
        raise HTTPException(status_code=400, detail="Hours must be 1-24")
    scheduler.reschedule_job("nios_check", trigger=IntervalTrigger(hours=hours))
    return {"message": f"Rescheduled to every {hours} hours"}
