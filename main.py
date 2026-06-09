import os
import logging
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import aiofiles

from database import init_db, get_db
from job_runner import run_status_check
from scraper import debug_fetch_one

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SECRET_KEY  = os.environ.get("SECRET_KEY",  "nios-tracker-secret-2025-mvs")
PORTAL_USER = os.environ.get("PORTAL_USER", "admin")
PORTAL_PASS = os.environ.get("PORTAL_PASS", "MVS2025")
EXCEL_PATH  = os.environ.get("EXCEL_PATH",  "students.xlsx")
ALGORITHM   = "HS256"
TOKEN_EXPIRE_HOURS = 12

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer  = HTTPBearer()

app = FastAPI(title="NIOS Status Tracker", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

PORTAL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NIOS Status Tracker — MVS Foundation</title>
<style>
  :root {
    --primary: #1565C0;
    --primary-light: #1976D2;
    --accent: #FF6F00;
    --bg: #F0F4F8;
    --card: #FFFFFF;
    --text: #212121;
    --muted: #757575;
    --border: #E0E0E0;
    --success: #2E7D32;
    --warning: #F57F17;
    --danger: #C62828;
    --radius: 12px;
    --shadow: 0 2px 12px rgba(0,0,0,0.08);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); }

  /* ── Login ── */
  #login-page {
    min-height: 100vh; display: flex; align-items: center;
    justify-content: center; background: linear-gradient(135deg, #1565C0 0%, #0D47A1 100%);
  }
  .login-card {
    background: white; border-radius: 20px; padding: 48px 40px;
    width: 100%; max-width: 400px; box-shadow: 0 20px 60px rgba(0,0,0,0.3);
    text-align: center;
  }
  .login-card .logo { font-size: 48px; margin-bottom: 8px; }
  .login-card h1 { font-size: 22px; color: var(--primary); margin-bottom: 4px; }
  .login-card p  { color: var(--muted); font-size: 13px; margin-bottom: 28px; }
  .form-group { margin-bottom: 16px; text-align: left; }
  .form-group label { display: block; font-size: 12px; font-weight: 600;
    color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px; }
  .form-group input {
    width: 100%; padding: 12px 16px; border: 2px solid var(--border);
    border-radius: 8px; font-size: 15px; transition: border-color .2s;
  }
  .form-group input:focus { outline: none; border-color: var(--primary); }
  .btn-primary {
    width: 100%; padding: 14px; background: var(--primary); color: white;
    border: none; border-radius: 8px; font-size: 15px; font-weight: 600;
    cursor: pointer; transition: background .2s; margin-top: 8px;
  }
  .btn-primary:hover { background: var(--primary-light); }
  .btn-primary:disabled { background: #BDBDBD; cursor: not-allowed; }
  .error-msg { color: var(--danger); font-size: 13px; margin-top: 12px; }

  /* ── App Shell ── */
  #app { display: none; min-height: 100vh; }

  /* Sidebar */
  .sidebar {
    position: fixed; left: 0; top: 0; bottom: 0; width: 240px;
    background: var(--primary); color: white; display: flex;
    flex-direction: column; z-index: 100; box-shadow: 2px 0 12px rgba(0,0,0,0.15);
  }
  .sidebar-header {
    padding: 24px 20px; border-bottom: 1px solid rgba(255,255,255,0.15);
  }
  .sidebar-header h2 { font-size: 17px; font-weight: 700; }
  .sidebar-header p  { font-size: 12px; opacity: .7; margin-top: 3px; }
  .nav-menu { flex: 1; padding: 16px 0; }
  .nav-item {
    display: flex; align-items: center; gap: 12px;
    padding: 12px 20px; cursor: pointer; font-size: 14px;
    transition: background .15s; border-left: 3px solid transparent;
  }
  .nav-item:hover  { background: rgba(255,255,255,0.1); }
  .nav-item.active { background: rgba(255,255,255,0.15); border-left-color: #FFD54F; font-weight: 600; }
  .nav-item .icon  { font-size: 18px; width: 22px; text-align: center; }
  .sidebar-footer { padding: 16px 20px; border-top: 1px solid rgba(255,255,255,0.15); }
  .sidebar-footer small { font-size: 11px; opacity: .6; }

  /* Main content */
  .main { margin-left: 240px; padding: 28px 32px; min-height: 100vh; }

  /* Top bar */
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 28px;
  }
  .topbar h1 { font-size: 22px; font-weight: 700; color: var(--primary); }
  .topbar-right { display: flex; align-items: center; gap: 12px; }
  .btn-sm {
    padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600;
    cursor: pointer; border: none; transition: .2s;
  }
  .btn-outline {
    background: white; border: 2px solid var(--primary); color: var(--primary);
  }
  .btn-outline:hover { background: var(--primary); color: white; }
  .btn-danger-sm { background: var(--danger); color: white; }
  .btn-danger-sm:hover { opacity: .85; }
  .btn-success { background: var(--success); color: white; }
  .btn-success:hover { opacity: .85; }

  /* Stat cards */
  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 16px; margin-bottom: 28px;
  }
  .stat-card {
    background: white; border-radius: var(--radius); padding: 20px;
    box-shadow: var(--shadow); display: flex; flex-direction: column; gap: 8px;
  }
  .stat-card .label { font-size: 12px; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: .5px; }
  .stat-card .value { font-size: 32px; font-weight: 700; color: var(--primary); line-height: 1; }
  .stat-card .sub   { font-size: 12px; color: var(--muted); }

  /* Cards */
  .card {
    background: white; border-radius: var(--radius);
    padding: 24px; box-shadow: var(--shadow); margin-bottom: 24px;
  }
  .card-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 18px;
  }
  .card-header h3 { font-size: 15px; font-weight: 700; color: var(--text); }

  /* Run log table */
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th {
    background: #F5F7FA; padding: 10px 14px; text-align: left;
    font-size: 11px; font-weight: 700; color: var(--muted);
    text-transform: uppercase; letter-spacing: .5px; border-bottom: 2px solid var(--border);
  }
  td { padding: 10px 14px; border-bottom: 1px solid var(--border); }
  tr:hover td { background: #FAFAFA; }
  tr:last-child td { border-bottom: none; }

  /* Status badges */
  .badge {
    display: inline-block; padding: 3px 10px; border-radius: 20px;
    font-size: 11px; font-weight: 600; white-space: nowrap;
  }
  .badge-pending       { background: #FFF9C4; color: #F57F17; }
  .badge-docs          { background: #FFE0B2; color: #E65100; }
  .badge-verified      { background: #C8E6C9; color: #2E7D32; }
  .badge-approved      { background: #B2DFDB; color: #00695C; }
  .badge-confirmed     { background: #69F0AE; color: #1B5E20; }
  .badge-admitted      { background: #BBDEFB; color: #1565C0; }
  .badge-rejected      { background: #FFCDD2; color: #C62828; }
  .badge-error         { background: #F5F5F5; color: #616161; }
  .badge-running       { background: #E3F2FD; color: #1565C0; }
  .badge-completed     { background: #E8F5E9; color: #2E7D32; }

  /* Run log status */
  .run-status { display: inline-block; padding: 3px 8px; border-radius: 6px;
    font-size: 11px; font-weight: 600; }
  .run-completed { background: #E8F5E9; color: #2E7D32; }
  .run-running   { background: #E3F2FD; color: #1565C0; }
  .run-error     { background: #FFEBEE; color: #C62828; }

  /* Search / filter bar */
  .filter-bar {
    display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap;
  }
  .filter-bar input, .filter-bar select {
    padding: 8px 14px; border: 2px solid var(--border); border-radius: 8px;
    font-size: 13px; flex: 1; min-width: 160px;
  }
  .filter-bar input:focus, .filter-bar select:focus {
    outline: none; border-color: var(--primary);
  }

  /* Pagination */
  .pagination {
    display: flex; gap: 6px; align-items: center;
    justify-content: center; margin-top: 16px;
  }
  .page-btn {
    padding: 6px 12px; border-radius: 6px; border: 2px solid var(--border);
    cursor: pointer; font-size: 13px; background: white;
    transition: .15s;
  }
  .page-btn:hover   { border-color: var(--primary); color: var(--primary); }
  .page-btn.active  { background: var(--primary); color: white; border-color: var(--primary); }
  .page-btn:disabled { opacity: .4; cursor: not-allowed; }

  /* Upload zone */
  .upload-zone {
    border: 2px dashed var(--border); border-radius: var(--radius);
    padding: 32px; text-align: center; cursor: pointer;
    transition: border-color .2s, background .2s;
  }
  .upload-zone:hover { border-color: var(--primary); background: #F3F8FF; }
  .upload-zone .icon { font-size: 40px; margin-bottom: 8px; }
  .upload-zone p { color: var(--muted); font-size: 14px; }

  /* Status dot on live indicator */
  .live-dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    background: #4CAF50; margin-right: 6px;
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: .3; }
  }

  /* Charts / distribution bar */
  .dist-bar {
    display: flex; height: 20px; border-radius: 10px; overflow: hidden;
    margin: 12px 0 6px;
  }
  .dist-segment { height: 100%; transition: width .4s; }
  .dist-legend  { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 8px; }
  .dist-item    { display: flex; align-items: center; gap: 6px; font-size: 12px; }
  .dist-dot     { width: 10px; height: 10px; border-radius: 3px; }

  /* Toast */
  .toast {
    position: fixed; bottom: 24px; right: 24px;
    background: #212121; color: white; padding: 12px 20px;
    border-radius: 10px; font-size: 13px; font-weight: 500;
    z-index: 9999; transform: translateY(80px); opacity: 0;
    transition: .3s; box-shadow: 0 8px 24px rgba(0,0,0,0.3);
  }
  .toast.show { transform: translateY(0); opacity: 1; }

  /* Page sections */
  .page-section { display: none; }
  .page-section.active { display: block; }

  /* Next run countdown */
  #countdown { font-size: 13px; color: var(--muted); }

  /* Responsive tweaks */
  @media (max-width: 768px) {
    .sidebar { width: 200px; }
    .main    { margin-left: 200px; padding: 16px; }
  }
</style>
</head>
<body>

<!-- ══════════════════ LOGIN PAGE ══════════════════ -->
<div id="login-page">
  <div class="login-card">
    <div class="logo">🏫</div>
    <h1>MVS Foundation</h1>
    <p>NIOS Status Tracker — Admin Portal</p>
    <div class="form-group">
      <label>Username</label>
      <input type="text" id="login-user" placeholder="Enter username" autocomplete="username">
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" id="login-pass" placeholder="Enter password" autocomplete="current-password">
    </div>
    <button class="btn-primary" id="login-btn" onclick="doLogin()">Login</button>
    <div class="error-msg" id="login-err"></div>
  </div>
</div>

<!-- ══════════════════ MAIN APP ══════════════════ -->
<div id="app">

  <!-- Sidebar -->
  <nav class="sidebar">
    <div class="sidebar-header">
      <h2>🏫 NIOS Tracker</h2>
      <p>MVS Foundation</p>
    </div>
    <div class="nav-menu">
      <div class="nav-item active" onclick="showPage('dashboard')">
        <span class="icon">📊</span> Dashboard
      </div>
      <div class="nav-item" onclick="showPage('students')">
        <span class="icon">👥</span> Students
      </div>
      <div class="nav-item" onclick="showPage('history')">
        <span class="icon">📋</span> Change History
      </div>
      <div class="nav-item" onclick="showPage('run-logs')">
        <span class="icon">🔄</span> Run Logs
      </div>
      <div class="nav-item" onclick="showPage('upload')">
        <span class="icon">📤</span> Upload Excel
      </div>
      <div class="nav-item" onclick="showPage('settings')">
        <span class="icon">⚙️</span> Settings
      </div>
    </div>
    <div class="sidebar-footer">
      <div><span class="live-dot"></span><small>System Live</small></div>
      <div style="margin-top:6px">
        <small id="sidebar-user" style="opacity:.7"></small>
      </div>
      <button onclick="logout()" class="btn-sm btn-danger-sm" style="margin-top:10px;width:100%">
        Logout
      </button>
    </div>
  </nav>

  <!-- Main -->
  <main class="main">

    <!-- ── DASHBOARD ── -->
    <section id="page-dashboard" class="page-section active">
      <div class="topbar">
        <h1>📊 Dashboard</h1>
        <div class="topbar-right">
          <span id="countdown"></span>
          <button class="btn-sm btn-outline" onclick="loadDashboard()">↻ Refresh</button>
          <button class="btn-sm btn-success" onclick="runNow()">▶ Run Now</button>
        </div>
      </div>

      <div class="stats-grid" id="stats-grid">
        <div class="stat-card">
          <div class="label">Total Students</div>
          <div class="value" id="stat-total">—</div>
          <div class="sub">In Excel</div>
        </div>
        <div class="stat-card">
          <div class="label">Last Run</div>
          <div class="value" style="font-size:18px" id="stat-last-run">—</div>
          <div class="sub" id="stat-last-run-sub"></div>
        </div>
        <div class="stat-card">
          <div class="label">Next Run</div>
          <div class="value" style="font-size:18px" id="stat-next-run">—</div>
          <div class="sub">Auto scheduled</div>
        </div>
        <div class="stat-card">
          <div class="label">Changes Today</div>
          <div class="value" id="stat-changes">—</div>
          <div class="sub">Status updates</div>
        </div>
      </div>

      <!-- Status distribution -->
      <div class="card">
        <div class="card-header">
          <h3>Status Distribution</h3>
        </div>
        <div class="dist-bar" id="dist-bar"></div>
        <div class="dist-legend" id="dist-legend"></div>
      </div>

      <!-- Recent runs -->
      <div class="card">
        <div class="card-header">
          <h3>Recent Runs</h3>
        </div>
        <table>
          <thead>
            <tr>
              <th>#</th><th>Run At</th><th>Checked</th>
              <th>Changed</th><th>Failed</th><th>Status</th>
            </tr>
          </thead>
          <tbody id="recent-runs-body">
            <tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px">Loading...</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- ── STUDENTS ── -->
    <section id="page-students" class="page-section">
      <div class="topbar">
        <h1>👥 Students Status</h1>
        <div class="topbar-right">
          <button class="btn-sm btn-outline" onclick="downloadExcel()">⬇ Download Excel</button>
        </div>
      </div>
      <div class="card">
        <div class="filter-bar">
          <input type="text" id="search-input" placeholder="🔍  Search by name or reference no..."
            oninput="debounceLoadStudents()">
          <select id="status-filter" onchange="loadStudents(1)">
            <option value="">All Statuses</option>
            <option value="Pending">Pending</option>
            <option value="Documents Verification In Progress">Documents Verification In Progress</option>
            <option value="Verified">Verified</option>
            <option value="Approved">Approved</option>
            <option value="Admission Confirmed">Admission Confirmed</option>
            <option value="Admitted">Admitted</option>
            <option value="Rejected">Rejected</option>
            <option value="Fetch Error">Fetch Error</option>
          </select>
        </div>
        <div id="students-count" style="font-size:13px;color:var(--muted);margin-bottom:12px"></div>
        <div style="overflow-x:auto">
          <table>
            <thead>
              <tr>
                <th>#</th><th>Reference No</th><th>Student Name</th>
                <th>Class</th><th>Status</th><th>Last Checked</th><th>Last Changed</th><th>Checks</th>
              </tr>
            </thead>
            <tbody id="students-body">
              <tr><td colspan="8" style="text-align:center;color:var(--muted);padding:24px">Loading...</td></tr>
            </tbody>
          </table>
        </div>
        <div class="pagination" id="students-pagination"></div>
      </div>
    </section>

    <!-- ── HISTORY ── -->
    <section id="page-history" class="page-section">
      <div class="topbar"><h1>📋 Change History</h1></div>
      <div class="card">
        <table>
          <thead>
            <tr>
              <th>#</th><th>Reference No</th><th>Student Name</th>
              <th>Old Status</th><th>New Status</th><th>Changed At</th>
            </tr>
          </thead>
          <tbody id="history-body">
            <tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px">Loading...</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- ── RUN LOGS ── -->
    <section id="page-run-logs" class="page-section">
      <div class="topbar">
        <h1>🔄 Run Logs</h1>
        <button class="btn-sm btn-success" onclick="runNow()">▶ Run Now</button>
      </div>
      <div class="card">
        <table>
          <thead>
            <tr>
              <th>Run ID</th><th>Started At</th><th>Checked</th>
              <th>Changed</th><th>Failed</th><th>Status</th><th>Notes</th>
            </tr>
          </thead>
          <tbody id="run-logs-body">
            <tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px">Loading...</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- ── UPLOAD ── -->
    <section id="page-upload" class="page-section">
      <div class="topbar"><h1>📤 Upload Excel</h1></div>
      <div class="card">
        <!-- Drag & Drop Zone -->
        <div class="upload-zone" id="upload-zone"
          onclick="document.getElementById('excel-file').click()"
          ondragover="event.preventDefault();this.style.borderColor='var(--primary)';this.style.background='#F3F8FF'"
          ondragleave="this.style.borderColor='';this.style.background=''"
          ondrop="handleDrop(event)">
          <div class="icon">📊</div>
          <p><strong>Click to upload</strong> or drag & drop your Excel file here</p>
          <p style="font-size:12px;margin-top:6px;color:var(--muted)">Supports .xlsx — must have "Reference No" column</p>
        </div>
        <input type="file" id="excel-file" accept=".xlsx,.xls" style="display:none" onchange="uploadExcel(event)">
        <div id="upload-status" style="margin-top:16px;font-size:14px;text-align:center"></div>

        <!-- Student Preview (shown after upload) -->
        <div id="upload-preview" style="display:none;margin-top:24px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
            <h4 style="font-size:15px">👥 Students Detected</h4>
            <button class="btn-sm btn-success" onclick="confirmAndRunNow()" id="confirm-run-btn">
              ✅ Confirm & Run Now
            </button>
          </div>
          <div style="overflow-x:auto">
            <table>
              <thead>
                <tr>
                  <th>#</th><th>Name</th><th>Phone No</th><th>Reference No</th>
                </tr>
              </thead>
              <tbody id="preview-body"></tbody>
            </table>
          </div>
          <p id="preview-count" style="margin-top:10px;font-size:13px;color:var(--muted);text-align:center"></p>
        </div>

        <div style="margin-top:28px;padding-top:20px;border-top:1px solid var(--border)">
          <h4 style="margin-bottom:12px;font-size:14px">📌 Required Excel Format</h4>
          <p style="font-size:13px;color:var(--muted);line-height:1.8">
            Your Excel must have at least a <strong>"Reference No"</strong> column.<br>
            Optional columns detected automatically: <strong>Name / Student Name, Phone No</strong><br>
            The tracker will add these columns automatically:<br>
            <code style="background:#F5F5F5;padding:2px 6px;border-radius:4px">NIOS Status</code> &nbsp;
            <code style="background:#F5F5F5;padding:2px 6px;border-radius:4px">Last Checked</code> &nbsp;
            <code style="background:#F5F5F5;padding:2px 6px;border-radius:4px">Last Changed</code>
          </p>
        </div>
      </div>
    </section>

    <!-- ── SETTINGS ── -->
    <section id="page-settings" class="page-section">
      <div class="topbar"><h1>⚙️ Settings</h1></div>
      <div class="card">
        <h3 style="margin-bottom:16px;font-size:15px">⏱ Run Interval</h3>
        <p style="font-size:13px;color:var(--muted);margin-bottom:12px">
          Change how often the system auto-checks NIOS website.
        </p>
        <div style="display:flex;gap:10px;align-items:center">
          <input type="number" id="interval-hours" value="6" min="1" max="24"
            style="width:80px;padding:10px;border:2px solid var(--border);border-radius:8px;font-size:15px">
          <span style="font-size:14px;color:var(--muted)">hours</span>
          <button class="btn-sm btn-outline" onclick="updateInterval()">Update Interval</button>
        </div>
      </div>
      <div class="card">
        <h3 style="margin-bottom:16px;font-size:15px">🎨 Status Colour Legend</h3>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px">
          <div style="display:flex;align-items:center;gap:10px;padding:10px;border-radius:8px;background:#FFF9C4">
            <span style="font-size:18px">🟡</span>
            <div><div style="font-weight:600;font-size:13px">Pending</div><div style="font-size:11px;color:#888">Application submitted, awaiting review</div></div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;padding:10px;border-radius:8px;background:#FFE0B2">
            <span style="font-size:18px">🟠</span>
            <div><div style="font-weight:600;font-size:13px">Documents Verification In Progress</div><div style="font-size:11px;color:#888">Documents under review</div></div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;padding:10px;border-radius:8px;background:#C8E6C9">
            <span style="font-size:18px">🟢</span>
            <div><div style="font-weight:600;font-size:13px">Verified</div><div style="font-size:11px;color:#888">Documents verified</div></div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;padding:10px;border-radius:8px;background:#B2DFDB">
            <span style="font-size:18px">✅</span>
            <div><div style="font-weight:600;font-size:13px">Approved</div><div style="font-size:11px;color:#888">Application approved</div></div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;padding:10px;border-radius:8px;background:#69F0AE">
            <span style="font-size:18px">🎉</span>
            <div><div style="font-weight:600;font-size:13px">Admission Confirmed</div><div style="font-size:11px;color:#1B5E20">Student ka admission pakka!</div></div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;padding:10px;border-radius:8px;background:#BBDEFB">
            <span style="font-size:18px">🔵</span>
            <div><div style="font-weight:600;font-size:13px">Admitted</div><div style="font-size:11px;color:#888">Student admitted</div></div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;padding:10px;border-radius:8px;background:#FFCDD2">
            <span style="font-size:18px">🔴</span>
            <div><div style="font-weight:600;font-size:13px">Rejected</div><div style="font-size:11px;color:#888">Application rejected</div></div>
          </div>
        </div>
      </div>
    </section>

  </main>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
const API = "";   // Same origin
let TOKEN = localStorage.getItem("nios_token") || "";
let currentPage = "dashboard";
let searchTimer = null;
let countdownTimer = null;
let nextRunTime = null;

// ══ Auth ══════════════════════════════════════════════════════════════════
async function doLogin() {
  const btn = document.getElementById("login-btn");
  const err = document.getElementById("login-err");
  err.textContent = "";
  btn.disabled = true; btn.textContent = "Logging in...";
  try {
    const r = await apiFetch("/api/login", "POST", {
      username: document.getElementById("login-user").value,
      password: document.getElementById("login-pass").value,
    }, false);
    TOKEN = r.token;
    localStorage.setItem("nios_token", TOKEN);
    document.getElementById("sidebar-user").textContent = "👤 " + r.username;
    document.getElementById("login-page").style.display = "none";
    document.getElementById("app").style.display = "block";
    loadDashboard();
  } catch (e) {
    err.textContent = "❌ " + (e.message || "Invalid credentials");
  } finally {
    btn.disabled = false; btn.textContent = "Login";
  }
}

document.getElementById("login-pass").addEventListener("keydown", e => {
  if (e.key === "Enter") doLogin();
});

function logout() {
  TOKEN = ""; localStorage.removeItem("nios_token");
  document.getElementById("app").style.display = "none";
  document.getElementById("login-page").style.display = "flex";
  clearInterval(countdownTimer);
}

// Auto-login if token exists
if (TOKEN) {
  document.getElementById("login-page").style.display = "none";
  document.getElementById("app").style.display = "block";
  loadDashboard();
}

// ══ API helper ════════════════════════════════════════════════════════════
async function apiFetch(path, method = "GET", body = null, auth = true) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json" }
  };
  if (auth) opts.headers["Authorization"] = "Bearer " + TOKEN;
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(API + path, opts);
  if (r.status === 401) { logout(); throw new Error("Session expired"); }
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

// ══ Navigation ════════════════════════════════════════════════════════════
function showPage(name) {
  document.querySelectorAll(".page-section").forEach(s => s.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
  document.getElementById("page-" + name).classList.add("active");
  event.currentTarget.classList.add("active");
  currentPage = name;
  if (name === "students")  loadStudents(1);
  if (name === "history")   loadHistory();
  if (name === "run-logs")  loadRunLogs();
}

// ══ Dashboard ═════════════════════════════════════════════════════════════
async function loadDashboard() {
  try {
    const d = await apiFetch("/api/dashboard");
    document.getElementById("stat-total").textContent = d.total_students;

    const runs = d.recent_runs;
    if (runs.length > 0) {
      const last = runs[0];
      document.getElementById("stat-last-run").textContent =
        last.run_at.substring(11, 16);
      document.getElementById("stat-last-run-sub").textContent =
        last.run_at.substring(0, 10);
    }

    // Next run
    if (d.next_run && d.next_run !== "Not scheduled") {
      nextRunTime = new Date(d.next_run);
      document.getElementById("stat-next-run").textContent =
        nextRunTime.toTimeString().substring(0, 5);
      startCountdown();
    }

    // Changes today
    const today = new Date().toISOString().substring(0, 10);
    let todayChanges = 0;
    runs.forEach(r => {
      if (r.run_at.startsWith(today)) todayChanges += (r.total_changed || 0);
    });
    document.getElementById("stat-changes").textContent = todayChanges;

    // Status distribution
    renderDistribution(d.status_distribution);

    // Recent runs table
    const tbody = document.getElementById("recent-runs-body");
    tbody.innerHTML = runs.length === 0
      ? `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px">No runs yet</td></tr>`
      : runs.map((r, i) => `
          <tr>
            <td>${r.id}</td>
            <td>${r.run_at}</td>
            <td><strong>${r.total_checked || 0}</strong></td>
            <td style="color:${r.total_changed > 0 ? 'var(--success)' : 'inherit'}">${r.total_changed || 0}</td>
            <td style="color:${r.total_failed > 0 ? 'var(--danger)' : 'inherit'}">${r.total_failed || 0}</td>
            <td><span class="run-status run-${r.status}">${r.status}</span></td>
          </tr>`).join("");
  } catch (e) {
    showToast("Error loading dashboard: " + e.message);
  }
}

function renderDistribution(dist) {
  const colors = {
    "Pending": "#FFF176",
    "Documents Verification In Progress": "#FFB74D",
    "Verified": "#81C784",
    "Approved": "#4DB6AC",
    "Admitted": "#64B5F6",
    "Rejected": "#E57373",
    "Fetch Error": "#BDBDBD",
    "Unknown": "#E0E0E0",
  };
  const total = dist.reduce((s, d) => s + d.cnt, 0) || 1;
  const bar = document.getElementById("dist-bar");
  const legend = document.getElementById("dist-legend");
  bar.innerHTML = dist.map(d => `
    <div class="dist-segment" title="${d.current_status}: ${d.cnt}"
      style="width:${(d.cnt/total*100).toFixed(1)}%;background:${colors[d.current_status]||'#E0E0E0'}"></div>
  `).join("");
  legend.innerHTML = dist.map(d => `
    <div class="dist-item">
      <div class="dist-dot" style="background:${colors[d.current_status]||'#E0E0E0'}"></div>
      <span>${d.current_status} (${d.cnt})</span>
    </div>
  `).join("");
}

function startCountdown() {
  clearInterval(countdownTimer);
  countdownTimer = setInterval(() => {
    if (!nextRunTime) return;
    const diff = nextRunTime - new Date();
    if (diff <= 0) { document.getElementById("countdown").textContent = "Running..."; return; }
    const h = Math.floor(diff / 3600000);
    const m = Math.floor((diff % 3600000) / 60000);
    const s = Math.floor((diff % 60000) / 1000);
    document.getElementById("countdown").textContent =
      `⏱ Next run in ${h}h ${m}m ${s}s`;
  }, 1000);
}

// ══ Run Now ═══════════════════════════════════════════════════════════════
async function runNow() {
  try {
    const r = await apiFetch("/api/run-now", "POST");
    showToast("✅ " + r.message);
    setTimeout(loadDashboard, 3000);
  } catch (e) {
    showToast("❌ " + e.message);
  }
}

// ══ Students ══════════════════════════════════════════════════════════════
let studentsCurrentPage = 1;
function debounceLoadStudents() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadStudents(1), 400);
}

async function loadStudents(page = 1) {
  studentsCurrentPage = page;
  const search = document.getElementById("search-input").value;
  const status = document.getElementById("status-filter").value;
  try {
    const d = await apiFetch(
      `/api/students?page=${page}&per_page=50&search=${encodeURIComponent(search)}&status_filter=${encodeURIComponent(status)}`
    );
    document.getElementById("students-count").textContent =
      `Showing ${d.students.length} of ${d.total} students`;

    const tbody = document.getElementById("students-body");
    tbody.innerHTML = d.students.length === 0
      ? `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:24px">No students found</td></tr>`
      : d.students.map((s, i) => `
          <tr>
            <td style="color:var(--muted)">${(page-1)*50+i+1}</td>
            <td><code style="background:#F5F5F5;padding:2px 6px;border-radius:4px">${s.reference_no}</code></td>
            <td>${s.student_name || "—"}</td>
            <td>${s.class_level || "—"}</td>
            <td>${statusBadge(s.current_status)}</td>
            <td style="font-size:12px;color:var(--muted)">${s.last_checked || "—"}</td>
            <td style="font-size:12px;color:var(--muted)">${s.last_changed || "—"}</td>
            <td style="text-align:center">${s.check_count || 0}</td>
          </tr>`).join("");

    // Pagination
    renderPagination("students-pagination", page, d.pages, loadStudents);
  } catch (e) {
    showToast("Error loading students: " + e.message);
  }
}

function statusBadge(status) {
  const map = {
    "Pending":                              "badge-pending",
    "Documents Verification In Progress":   "badge-docs",
    "Verified":                             "badge-verified",
    "Approved":                             "badge-approved",
    "Admission Confirmed":                  "badge-confirmed",
    "Admitted":                             "badge-admitted",
    "Rejected":                             "badge-rejected",
    "Fetch Error":                          "badge-error",
    "Not Found":                            "badge-error",
  };
  const cls = map[status] || "badge-error";
  return `<span class="badge ${cls}">${status || "Unknown"}</span>`;
}

function renderPagination(containerId, current, total, loadFn) {
  const c = document.getElementById(containerId);
  if (total <= 1) { c.innerHTML = ""; return; }
  let html = `<button class="page-btn" onclick="${loadFn.name}(${current-1})" ${current===1?"disabled":""}>‹ Prev</button>`;
  const start = Math.max(1, current - 2);
  const end   = Math.min(total, current + 2);
  for (let p = start; p <= end; p++) {
    html += `<button class="page-btn ${p===current?"active":""}" onclick="${loadFn.name}(${p})">${p}</button>`;
  }
  html += `<button class="page-btn" onclick="${loadFn.name}(${current+1})" ${current===total?"disabled":""}>Next ›</button>`;
  c.innerHTML = html;
}

// ══ History ═══════════════════════════════════════════════════════════════
async function loadHistory() {
  try {
    const data = await apiFetch("/api/history?limit=200");
    const tbody = document.getElementById("history-body");
    tbody.innerHTML = data.length === 0
      ? `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px">No changes recorded yet</td></tr>`
      : data.map((h, i) => `
          <tr>
            <td style="color:var(--muted)">${i+1}</td>
            <td><code style="background:#F5F5F5;padding:2px 6px;border-radius:4px">${h.reference_no}</code></td>
            <td>${h.student_name || "—"}</td>
            <td>${h.old_status ? statusBadge(h.old_status) : '<span style="color:var(--muted)">—</span>'}</td>
            <td>${statusBadge(h.new_status)}</td>
            <td style="font-size:12px;color:var(--muted)">${h.changed_at}</td>
          </tr>`).join("");
  } catch (e) {
    showToast("Error loading history: " + e.message);
  }
}

// ══ Run Logs ══════════════════════════════════════════════════════════════
async function loadRunLogs() {
  try {
    const data = await apiFetch("/api/run-logs?limit=100");
    const tbody = document.getElementById("run-logs-body");
    tbody.innerHTML = data.length === 0
      ? `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px">No runs yet</td></tr>`
      : data.map(r => `
          <tr>
            <td>${r.id}</td>
            <td>${r.run_at}</td>
            <td>${r.total_checked || 0}</td>
            <td style="color:${r.total_changed>0?'var(--success)':'inherit'}">${r.total_changed || 0}</td>
            <td style="color:${r.total_failed>0?'var(--danger)':'inherit'}">${r.total_failed || 0}</td>
            <td><span class="run-status run-${r.status}">${r.status}</span></td>
            <td style="font-size:12px;color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis">
              ${r.notes || "—"}
            </td>
          </tr>`).join("");
  } catch (e) {
    showToast("Error loading run logs: " + e.message);
  }
}

// ══ Upload Excel ══════════════════════════════════════════════════════════
function handleDrop(event) {
  event.preventDefault();
  const zone = document.getElementById("upload-zone");
  zone.style.borderColor = ""; zone.style.background = "";
  const file = event.dataTransfer.files[0];
  if (file) processExcelFile(file);
}

async function uploadExcel(event) {
  const file = event.target.files[0];
  if (file) processExcelFile(file);
}

async function processExcelFile(file) {
  const statusEl = document.getElementById("upload-status");
  statusEl.textContent = "⏳ Uploading...";
  document.getElementById("upload-preview").style.display = "none";

  const formData = new FormData();
  formData.append("file", file);
  try {
    const r = await fetch(API + "/api/upload-excel", {
      method: "POST",
      headers: { "Authorization": "Bearer " + TOKEN },
      body: formData
    });
    if (!r.ok) throw new Error((await r.json()).detail);
    const data = await r.json();
    statusEl.innerHTML = `<span style="color:var(--success)">✅ ${data.message}</span>`;
    // Show preview
    await showUploadPreview(data);
  } catch (e) {
    statusEl.innerHTML = `<span style="color:var(--danger)">❌ ${e.message}</span>`;
  }
}

async function showUploadPreview(uploadData) {
  try {
    // Fetch students list to preview
    const d = await apiFetch("/api/students?page=1&per_page=200");
    if (!d.students || d.students.length === 0) {
      // Students not in DB yet — show run prompt
      document.getElementById("upload-preview").style.display = "block";
      document.getElementById("preview-body").innerHTML =
        `<tr><td colspan="4" style="text-align:center;padding:20px;color:var(--muted)">
          Click "Confirm & Run Now" to fetch statuses from NIOS website
        </td></tr>`;
      document.getElementById("preview-count").textContent = `File uploaded. Students will be loaded after first run.`;
      return;
    }
    const tbody = document.getElementById("preview-body");
    tbody.innerHTML = d.students.map((s, i) => `
      <tr>
        <td style="color:var(--muted)">${i+1}</td>
        <td>${s.student_name || "—"}</td>
        <td>—</td>
        <td><code style="background:#F5F5F5;padding:2px 6px;border-radius:4px">${s.reference_no}</code></td>
      </tr>`).join("");
    document.getElementById("preview-count").textContent =
      `${d.total} students loaded. Click "Confirm & Run Now" to check NIOS status.`;
    document.getElementById("upload-preview").style.display = "block";
  } catch(e) {
    // After first upload DB is empty — just show confirm button
    document.getElementById("upload-preview").style.display = "block";
    document.getElementById("preview-body").innerHTML =
      `<tr><td colspan="4" style="text-align:center;padding:20px;color:var(--muted)">
        File ready. Click "Confirm & Run Now" to fetch NIOS statuses.
      </td></tr>`;
  }
}

async function confirmAndRunNow() {
  const btn = document.getElementById("confirm-run-btn");
  btn.disabled = true; btn.textContent = "⏳ Running...";
  try {
    const r = await apiFetch("/api/run-now", "POST");
    showToast("🚀 " + r.message);
    btn.textContent = "✅ Run Started!";
    // Switch to dashboard after 2s
    setTimeout(() => {
      document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
      document.querySelectorAll(".nav-item")[0].classList.add("active");
      showPage2("dashboard");
      loadDashboard();
    }, 2000);
  } catch(e) {
    showToast("❌ " + e.message);
    btn.disabled = false; btn.textContent = "✅ Confirm & Run Now";
  }
}

function showPage2(name) {
  document.querySelectorAll(".page-section").forEach(s => s.classList.remove("active"));
  document.getElementById("page-" + name).classList.add("active");
}

async function downloadExcel() {
  const r = await fetch(API + "/api/download-excel", {
    headers: { "Authorization": "Bearer " + TOKEN }
  });
  if (!r.ok) { showToast("❌ Excel file not found. Run a check first."); return; }
  const blob = await r.blob();
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href = url; a.download = "nios_status_updated.xlsx"; a.click();
  URL.revokeObjectURL(url);
}

// ══ Settings ══════════════════════════════════════════════════════════════
async function updateInterval() {
  const hours = parseInt(document.getElementById("interval-hours").value);
  try {
    const r = await apiFetch("/api/reschedule", "POST", { hours });
    showToast("✅ " + r.message);
    loadDashboard();
  } catch (e) {
    showToast("❌ " + e.message);
  }
}

// ══ Toast ═════════════════════════════════════════════════════════════════
function showToast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 3500);
}

// Auto-refresh dashboard every 60 seconds
setInterval(() => {
  if (currentPage === "dashboard") loadDashboard();
}, 60000);
</script>
</body>
</html>
"""

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

scheduler = BackgroundScheduler()

@app.on_event("startup")
async def startup():
    init_db()
    scheduler.add_job(run_status_check, trigger=IntervalTrigger(hours=6),
                      id="nios_check", replace_existing=True, next_run_time=None)
    scheduler.start()
    logger.info("Scheduler started")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)

@app.get("/", response_class=HTMLResponse)
async def serve_portal():
    return PORTAL_HTML

@app.get("/health")
async def health():
    return {"status": "ok"}

# DEBUG endpoint — see raw NIOS response for one reference number
@app.get("/debug/nios/{ref_no}", response_class=PlainTextResponse)
async def debug_nios(ref_no: str, user=Depends(verify_token)):
    result = debug_fetch_one(ref_no)
    return result

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
    runs = conn.execute("SELECT * FROM run_logs ORDER BY id DESC LIMIT 10").fetchall()
    status_dist = conn.execute(
        "SELECT current_status, COUNT(*) as cnt FROM student_status GROUP BY current_status"
    ).fetchall()
    job = scheduler.get_job("nios_check")
    next_run = str(job.next_run_time) if job and job.next_run_time else "Not scheduled"
    conn.close()
    return {
        "total_students": total_students,
        "next_run": next_run,
        "status_distribution": [dict(r) for r in status_dist],
        "recent_runs": [dict(r) for r in runs],
    }

@app.get("/api/students")
async def get_students(page: int=1, per_page: int=50, search: str="",
                       status_filter: str="", user=Depends(verify_token)):
    conn = get_db()
    offset = (page - 1) * per_page
    where_clauses, params = [], []
    if search:
        where_clauses.append("(reference_no LIKE ? OR student_name LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if status_filter:
        where_clauses.append("current_status = ?")
        params.append(status_filter)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    total = conn.execute(f"SELECT COUNT(*) FROM student_status {where_sql}", params).fetchone()[0]
    students = conn.execute(
        f"SELECT * FROM student_status {where_sql} ORDER BY student_name LIMIT ? OFFSET ?",
        params + [per_page, offset]
    ).fetchall()
    conn.close()
    return {"students": [dict(s) for s in students], "total": total,
            "page": page, "per_page": per_page, "pages": (total+per_page-1)//per_page}

@app.get("/api/history")
async def get_history(limit: int=100, user=Depends(verify_token)):
    conn = get_db()
    history = conn.execute("SELECT * FROM status_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(h) for h in history]

@app.get("/api/run-logs")
async def get_run_logs(limit: int=50, user=Depends(verify_token)):
    conn = get_db()
    logs = conn.execute("SELECT * FROM run_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
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
    return {"message": f"Excel uploaded ({len(content)} bytes)", "filename": file.filename}

@app.get("/api/download-excel")
async def download_excel(user=Depends(verify_token)):
    if not os.path.exists(EXCEL_PATH):
        raise HTTPException(status_code=404, detail="Excel file not found")
    return FileResponse(EXCEL_PATH,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="nios_status_updated.xlsx")

@app.get("/api/scheduler-status")
async def scheduler_status(user=Depends(verify_token)):
    job = scheduler.get_job("nios_check")
    return {"running": scheduler.running,
            "next_run": str(job.next_run_time) if job and job.next_run_time else None}

@app.post("/api/reschedule")
async def reschedule(body: dict, user=Depends(verify_token)):
    hours = int(body.get("hours", 6))
    if hours < 1 or hours > 24:
        raise HTTPException(status_code=400, detail="Hours must be 1-24")
    scheduler.reschedule_job("nios_check", trigger=IntervalTrigger(hours=hours))
    return {"message": f"Rescheduled to every {hours} hours"}
