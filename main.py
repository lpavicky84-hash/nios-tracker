import os
import logging
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import FileResponse, HTMLResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import aiofiles

from database import init_db, get_db, get_setting, set_setting
from job_runner import run_status_check

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

app = FastAPI(title="NIOS Status Tracker", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

PORTAL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NIOS Status Tracker — MVS Foundation</title>
<style>
  :root{
    --primary:#4F46E5; --primary-dark:#4338CA; --primary-light:#EEF2FF;
    --sidebar:#1E293B; --sidebar-hover:#334155;
    --bg:#F1F5F9; --card:#FFFFFF; --border:#E2E8F0; --text:#0F172A;
    --muted:#64748B; --success:#16A34A; --danger:#DC2626; --warn:#EA580C;
    --shadow:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.04);
    --shadow-lg:0 10px 25px rgba(0,0,0,.08);
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}
  a{text-decoration:none}
  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-thumb{background:#CBD5E1;border-radius:4px}

  #login-screen{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
    background:linear-gradient(135deg,#4F46E5 0%,#7C3AED 100%);z-index:1000}
  .login-card{background:#fff;border-radius:18px;padding:42px;width:380px;
    box-shadow:0 20px 60px rgba(0,0,0,.3)}
  .login-card .logo{width:60px;height:60px;background:linear-gradient(135deg,#4F46E5,#7C3AED);
    border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:28px;margin:0 auto 18px}
  .login-card h2{text-align:center;font-size:21px;margin-bottom:4px}
  .login-card p.sub{text-align:center;color:var(--muted);font-size:13px;margin-bottom:26px}
  .login-card label{display:block;font-size:13px;font-weight:600;margin-bottom:6px;color:#334155}
  .login-card input{width:100%;padding:12px 14px;border:2px solid var(--border);
    border-radius:10px;font-size:14px;margin-bottom:16px;transition:.2s}
  .login-card input:focus{outline:none;border-color:var(--primary)}
  .login-card button{width:100%;padding:13px;background:var(--primary);color:#fff;border:none;
    border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;transition:.2s}
  .login-card button:hover{background:var(--primary-dark)}
  #login-error{color:var(--danger);font-size:13px;text-align:center;margin-top:12px;min-height:18px}

  #app{display:none;min-height:100vh}
  .sidebar{position:fixed;left:0;top:0;bottom:0;width:240px;background:var(--sidebar);
    padding:20px 0;display:flex;flex-direction:column;z-index:100}
  .sidebar .brand{padding:0 22px 22px;display:flex;align-items:center;gap:10px;
    border-bottom:1px solid rgba(255,255,255,.08);margin-bottom:14px}
  .sidebar .brand .ic{width:38px;height:38px;background:linear-gradient(135deg,#4F46E5,#7C3AED);
    border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px}
  .sidebar .brand .tx b{color:#fff;font-size:15px;display:block}
  .sidebar .brand .tx span{color:#94A3B8;font-size:11px}
  .nav-item{display:flex;align-items:center;gap:12px;padding:12px 22px;color:#CBD5E1;
    cursor:pointer;font-size:14px;font-weight:500;transition:.15s;border-left:3px solid transparent}
  .nav-item:hover{background:var(--sidebar-hover);color:#fff}
  .nav-item.active{background:var(--sidebar-hover);color:#fff;border-left-color:#818CF8}
  .nav-item .ic{font-size:17px;width:20px;text-align:center}
  .nav-item .badge-count{margin-left:auto;background:var(--danger);color:#fff;
    font-size:11px;padding:1px 7px;border-radius:10px;font-weight:700;display:none}
  .nav-sep{padding:14px 22px 6px;color:#64748B;font-size:11px;font-weight:700;
    text-transform:uppercase;letter-spacing:.5px}

  .main{margin-left:240px;min-height:100vh}
  .topbar{background:#fff;border-bottom:1px solid var(--border);padding:0 28px;height:64px;
    display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50}
  .topbar h1{font-size:19px;font-weight:700}
  .topbar .right{display:flex;align-items:center;gap:16px}

  .bell-wrap{position:relative;cursor:pointer}
  .bell-btn{width:42px;height:42px;border-radius:10px;background:var(--bg);display:flex;
    align-items:center;justify-content:center;font-size:19px;transition:.15s;border:1px solid var(--border)}
  .bell-btn:hover{background:var(--primary-light)}
  .bell-badge{position:absolute;top:-4px;right:-4px;background:var(--danger);color:#fff;
    font-size:10px;font-weight:700;min-width:18px;height:18px;border-radius:9px;
    display:none;align-items:center;justify-content:center;padding:0 4px;border:2px solid #fff}
  .bell-dropdown{position:absolute;right:0;top:52px;width:360px;max-height:440px;overflow-y:auto;
    background:#fff;border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow-lg);
    display:none;z-index:200}
  .bell-dropdown.open{display:block}
  .bell-head{padding:14px 18px;border-bottom:1px solid var(--border);font-weight:700;
    font-size:14px;display:flex;align-items:center;justify-content:space-between}
  .bell-head .cnt{background:var(--warn);color:#fff;font-size:11px;padding:2px 9px;border-radius:10px}
  .notif-item{padding:13px 18px;border-bottom:1px solid #F1F5F9;display:flex;gap:11px}
  .notif-item:last-child{border-bottom:none}
  .notif-item .dot{width:9px;height:9px;border-radius:50%;background:var(--warn);margin-top:5px;flex-shrink:0}
  .notif-item .nm{font-weight:600;font-size:13px}
  .notif-item .rf{font-size:12px;color:var(--primary);font-family:monospace}
  .notif-item .rk{font-size:11px;color:var(--muted);margin-top:3px;line-height:1.4}
  .notif-empty{padding:34px 18px;text-align:center;color:var(--muted);font-size:13px}

  .content{padding:28px}
  .page-section{display:none}
  .page-section.active{display:block}

  .card{background:var(--card);border:1px solid var(--border);border-radius:14px;
    padding:22px;box-shadow:var(--shadow);margin-bottom:20px}
  .card h3{font-size:15px;margin-bottom:16px;font-weight:700}

  .stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:16px;margin-bottom:22px}
  .stat{background:#fff;border:1px solid var(--border);border-radius:14px;padding:20px;
    box-shadow:var(--shadow);position:relative;overflow:hidden}
  .stat .ic{position:absolute;right:16px;top:16px;font-size:26px;opacity:.85}
  .stat .lbl{font-size:13px;color:var(--muted);font-weight:600;margin-bottom:8px}
  .stat .val{font-size:30px;font-weight:800;line-height:1}
  .stat .bar{position:absolute;left:0;bottom:0;height:4px;width:100%}

  .btn{display:inline-flex;align-items:center;gap:8px;padding:11px 20px;border-radius:10px;
    font-size:14px;font-weight:600;cursor:pointer;border:none;transition:.15s}
  .btn-primary{background:var(--primary);color:#fff}
  .btn-primary:hover{background:var(--primary-dark);transform:translateY(-1px);box-shadow:0 4px 12px rgba(79,70,229,.3)}
  .btn-outline{background:#fff;color:var(--primary);border:2px solid var(--primary)}
  .btn-outline:hover{background:var(--primary-light)}
  .btn-success{background:var(--success);color:#fff}
  .btn-success:hover{filter:brightness(1.08)}
  .btn-sm{padding:8px 15px;font-size:13px;border-radius:8px}

  .filter-bar{display:flex;gap:12px;margin-bottom:18px;flex-wrap:wrap}
  .filter-bar input,.filter-bar select{padding:11px 14px;border:2px solid var(--border);
    border-radius:10px;font-size:14px;transition:.15s;background:#fff}
  .filter-bar input{flex:1;min-width:200px}
  .filter-bar input:focus,.filter-bar select:focus{outline:none;border-color:var(--primary)}
  table{width:100%;border-collapse:collapse}
  thead th{text-align:left;padding:13px 14px;font-size:12px;font-weight:700;color:var(--muted);
    text-transform:uppercase;letter-spacing:.4px;background:#F8FAFC;border-bottom:2px solid var(--border)}
  tbody td{padding:14px;border-bottom:1px solid #F1F5F9;font-size:14px;vertical-align:top}
  tbody tr:hover{background:#FAFBFF}
  .ref-tag{background:#F1F5F9;padding:3px 8px;border-radius:6px;font-family:monospace;font-size:13px}

  .badge{display:inline-block;padding:5px 12px;border-radius:20px;font-size:12px;font-weight:700;white-space:nowrap}
  .b-pending{background:#FEF9C3;color:#854D0E}
  .b-docs{background:#FFEDD5;color:#9A3412}
  .b-required{background:#FED7AA;color:#9A3412}
  .b-verified{background:#DCFCE7;color:#166534}
  .b-approved{background:#CCFBF1;color:#115E59}
  .b-confirmed{background:#86EFAC;color:#14532D}
  .b-rejected{background:#FECACA;color:#991B1B}
  .b-error{background:#E2E8F0;color:#475569}

  .pg-bar{display:flex;align-items:center;justify-content:space-between;margin-top:18px;flex-wrap:wrap;gap:12px}
  .pg-controls{display:flex;gap:6px;align-items:center}
  .pg-controls button{padding:8px 13px;border:1px solid var(--border);background:#fff;
    border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;transition:.15s}
  .pg-controls button:hover:not(:disabled){background:var(--primary-light);border-color:var(--primary)}
  .pg-controls button.active{background:var(--primary);color:#fff;border-color:var(--primary)}
  .pg-controls button:disabled{opacity:.4;cursor:not-allowed}
  .perpage{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}
  .perpage select{padding:7px 10px;border:1px solid var(--border);border-radius:8px;font-size:13px}

  .drop{border:2.5px dashed var(--border);border-radius:14px;padding:48px;text-align:center;
    transition:.2s;cursor:pointer;background:#FAFBFF}
  .drop:hover,.drop.drag{border-color:var(--primary);background:var(--primary-light)}
  .drop .ic{font-size:42px;margin-bottom:12px}

  .legend-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
  .legend-item{padding:14px 16px;border-radius:11px}
  .legend-item .nm{font-weight:700;font-size:13px}
  .legend-item .ds{font-size:11px;opacity:.75;font-weight:500}

  .dist-row{display:flex;align-items:center;gap:12px;margin-bottom:11px}
  .dist-row .nm{width:230px;font-size:13px;font-weight:600;flex-shrink:0}
  .dist-row .track{flex:1;height:22px;background:#F1F5F9;border-radius:11px;overflow:hidden}
  .dist-row .fill{height:100%;border-radius:11px;transition:width .5s}
  .dist-row .ct{width:40px;text-align:right;font-weight:700;font-size:13px}

  .empty{text-align:center;color:var(--muted);padding:40px;font-size:14px}

  #toast{position:fixed;bottom:28px;right:28px;background:var(--text);color:#fff;padding:14px 22px;
    border-radius:11px;font-size:14px;box-shadow:var(--shadow-lg);opacity:0;transform:translateY(20px);
    transition:.3s;z-index:2000;max-width:360px}
  #toast.show{opacity:1;transform:translateY(0)}

  @media(max-width:820px){
    .sidebar{width:64px}.sidebar .brand .tx,.nav-item span.lbl,.nav-sep{display:none}
    .nav-item{justify-content:center;padding:14px}.main{margin-left:64px}
    .bell-dropdown{width:300px}
  }
</style>
</head>
<body>

<div id="login-screen">
  <div class="login-card">
    <div class="logo">🎓</div>
    <h2>NIOS Status Tracker</h2>
    <p class="sub">MVS Foundation — Admin Portal</p>
    <label>Username</label>
    <input type="text" id="lg-user" placeholder="admin" autocomplete="username">
    <label>Password</label>
    <input type="password" id="lg-pass" placeholder="enter password" autocomplete="current-password"
      onkeydown="if(event.key==='Enter')doLogin()">
    <button onclick="doLogin()">Sign In</button>
    <div id="login-error"></div>
  </div>
</div>

<div id="app">
  <aside class="sidebar">
    <div class="brand">
      <div class="ic">🎓</div>
      <div class="tx"><b>NIOS Tracker</b><span>MVS Foundation</span></div>
    </div>
    <div class="nav-item active" data-page="dashboard" onclick="nav('dashboard')">
      <span class="ic">📊</span><span class="lbl">Dashboard</span></div>
    <div class="nav-item" data-page="students" onclick="nav('students')">
      <span class="ic">👥</span><span class="lbl">Active Students</span></div>
    <div class="nav-item" data-page="confirmed" onclick="nav('confirmed')">
      <span class="ic">✅</span><span class="lbl">Confirmed</span></div>
    <div class="nav-item" data-page="required" onclick="nav('required')">
      <span class="ic">📄</span><span class="lbl">Required</span>
      <span class="badge-count" id="nav-required-badge">0</span></div>
    <div class="nav-sep">Activity</div>
    <div class="nav-item" data-page="history" onclick="nav('history')">
      <span class="ic">🕑</span><span class="lbl">Change History</span></div>
    <div class="nav-item" data-page="runlogs" onclick="nav('runlogs')">
      <span class="ic">🔄</span><span class="lbl">Run Logs</span></div>
    <div class="nav-sep">Manage</div>
    <div class="nav-item" data-page="upload" onclick="nav('upload')">
      <span class="ic">📤</span><span class="lbl">Upload Excel</span></div>
    <div class="nav-item" data-page="settings" onclick="nav('settings')">
      <span class="ic">⚙️</span><span class="lbl">Settings</span></div>
  </aside>

  <div class="main">
    <div class="topbar">
      <h1 id="page-title">Dashboard</h1>
      <div class="right">
        <button class="btn btn-success btn-sm" onclick="runNow()">▶ Run Now</button>
        <div class="bell-wrap">
          <div class="bell-btn" onclick="toggleBell(event)">🔔
            <span class="bell-badge" id="bell-badge">0</span></div>
          <div class="bell-dropdown" id="bell-dropdown" onclick="event.stopPropagation()">
            <div class="bell-head">Document Required
              <span class="cnt" id="bell-head-cnt">0</span></div>
            <div id="bell-list"></div>
          </div>
        </div>
      </div>
    </div>

    <div class="content">
      <section id="sec-dashboard" class="page-section active">
        <div class="stat-grid" id="stat-grid"></div>
        <div class="card">
          <h3>📈 Status Distribution</h3>
          <div id="distribution"><div class="empty">Loading...</div></div>
        </div>
        <div class="card">
          <h3>🔄 Recent Runs</h3>
          <div style="overflow-x:auto"><table>
            <thead><tr><th>Run At</th><th>Type</th><th>Checked</th><th>Changed</th><th>Failed</th><th>Status</th></tr></thead>
            <tbody id="recent-runs"></tbody>
          </table></div>
        </div>
      </section>

      <section id="sec-students" class="page-section">
        <div class="card">
          <div class="filter-bar">
            <input type="text" id="s-search" placeholder="🔍  Search name / reference / email..." oninput="debounceStudents()">
            <select id="s-status" onchange="loadStudents(1)">
              <option value="">All Statuses</option>
              <option>Pending</option><option>Documents Verification In Progress</option>
              <option>Document Required</option><option>Verified</option>
              <option>Approved</option><option>Rejected</option><option>Fetch Error</option>
            </select>
            <select id="s-session" onchange="loadStudents(1)"><option value="">All Sessions</option></select>
          </div>
          <div id="s-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>#</th><th>Reference No</th><th>Student Name</th><th>Session</th>
              <th>Status</th><th>Last Checked</th>
            </tr></thead><tbody id="s-body"></tbody></table>
          </div>
          <div class="pg-bar" id="s-pg"></div>
        </div>
      </section>

      <section id="sec-confirmed" class="page-section">
        <div class="card">
          <h3>✅ Admission Confirmed Students</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            Inka admission pakka ho gaya. Download links (Phase 2) yahin aayenge.</p>
          <div class="filter-bar">
            <input type="text" id="c-search" placeholder="🔍  Search..." oninput="debounceConfirmed()">
            <select id="c-session" onchange="loadConfirmed(1)"><option value="">All Sessions</option></select>
          </div>
          <div id="c-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>#</th><th>Reference No</th><th>Student Name</th><th>Session</th>
              <th>Status</th><th>Downloads</th><th>Confirmed On</th>
            </tr></thead><tbody id="c-body"></tbody></table>
          </div>
          <div class="pg-bar" id="c-pg"></div>
        </div>
      </section>

      <section id="sec-required" class="page-section">
        <div class="card">
          <h3>📄 Document Required — Action Needed</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            Counsellor inko resolve kare. Resolve hone ke baad next run mein wapas Active list mein chale jayenge.</p>
          <div class="filter-bar">
            <input type="text" id="r-search" placeholder="🔍  Search..." oninput="debounceRequired()">
            <select id="r-session" onchange="loadRequired(1)"><option value="">All Sessions</option></select>
          </div>
          <div id="r-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>#</th><th>Reference No</th><th>Student Name</th><th>Session</th><th>RC Comment / Remark</th>
            </tr></thead><tbody id="r-body"></tbody></table>
          </div>
          <div class="pg-bar" id="r-pg"></div>
        </div>
      </section>

      <section id="sec-history" class="page-section">
        <div class="card">
          <h3>🕑 Status Change History</h3>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>Reference No</th><th>Student</th><th>Old Status</th><th>New Status</th><th>Changed At</th>
            </tr></thead><tbody id="h-body"></tbody></table>
          </div>
        </div>
      </section>

      <section id="sec-runlogs" class="page-section">
        <div class="card">
          <h3>🔄 Run Logs</h3>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>Run At</th><th>Type</th><th>Checked</th><th>Changed</th><th>Failed</th><th>Status</th>
            </tr></thead><tbody id="rl-body"></tbody></table>
          </div>
        </div>
      </section>

      <section id="sec-upload" class="page-section">
        <div class="card">
          <h3>📤 Upload Student Excel</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:18px">
            Excel upload karo (.xlsx). Columns: Student Name, Mobile, Class, Reference Number, Email, DOB, Admission Session.</p>
          <div class="drop" id="drop" onclick="document.getElementById('file-input').click()">
            <div class="ic">📁</div>
            <div style="font-weight:600;font-size:15px">Click or drag Excel file here</div>
            <div style="color:var(--muted);font-size:13px;margin-top:5px">.xlsx files only</div>
          </div>
          <input type="file" id="file-input" accept=".xlsx,.xls" style="display:none" onchange="handleFile(this.files[0])">
          <div id="upload-status" style="margin-top:16px"></div>
          <div style="margin-top:18px;display:flex;gap:12px">
            <button class="btn btn-outline btn-sm" onclick="downloadExcel()">⬇ Download Updated Excel</button>
          </div>
        </div>
      </section>

      <section id="sec-settings" class="page-section">
        <div class="card">
          <h3>⏱ Recheck Intervals</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:18px">
            Alag-alag intervals. Confirmed students automatically skip ho jaate hain.</p>
          <div style="display:flex;gap:12px;align-items:center;margin-bottom:14px;flex-wrap:wrap">
            <span style="width:280px;font-weight:600">📗 Regular (On Demand + Stream 2)</span>
            <input type="number" id="iv-regular" min="1" max="72" value="6"
              style="width:90px;padding:11px;border:2px solid var(--border);border-radius:10px;font-size:15px">
            <span style="color:var(--muted)">hours</span>
          </div>
          <div style="display:flex;gap:12px;align-items:center;margin-bottom:18px;flex-wrap:wrap">
            <span style="width:280px;font-weight:600">📘 Public Exam (April / October + any year)</span>
            <input type="number" id="iv-public" min="1" max="72" value="12"
              style="width:90px;padding:11px;border:2px solid var(--border);border-radius:10px;font-size:15px">
            <span style="color:var(--muted)">hours</span>
          </div>
          <button class="btn btn-primary btn-sm" onclick="saveIntervals()">💾 Save Intervals</button>
          <div id="iv-status" style="margin-top:12px;font-size:13px"></div>
        </div>
        <div class="card">
          <h3>🎨 Status Colour Legend</h3>
          <div class="legend-grid">
            <div class="legend-item b-pending"><div class="nm">🟡 Pending</div><div class="ds">Awaiting review</div></div>
            <div class="legend-item b-docs"><div class="nm">🟠 Documents Verification In Progress</div><div class="ds">Under review</div></div>
            <div class="legend-item b-required"><div class="nm">📄 Document Required</div><div class="ds">Action needed by counsellor</div></div>
            <div class="legend-item b-verified"><div class="nm">🟢 Verified</div><div class="ds">Documents verified</div></div>
            <div class="legend-item b-approved"><div class="nm">🔷 Approved</div><div class="ds">Application approved</div></div>
            <div class="legend-item b-confirmed"><div class="nm">🎉 Admission Confirmed</div><div class="ds">Admission pakka!</div></div>
            <div class="legend-item b-rejected"><div class="nm">🔴 Rejected</div><div class="ds">Application rejected</div></div>
          </div>
        </div>
      </section>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
const API = window.location.origin;
let TOKEN = "";
let perPage = 20;
let sessionsLoaded = false;

async function doLogin(){
  const u=document.getElementById("lg-user").value;
  const p=document.getElementById("lg-pass").value;
  const err=document.getElementById("login-error");
  err.textContent="";
  try{
    const r=await fetch(API+"/api/login",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({username:u,password:p})});
    if(!r.ok){err.textContent="Invalid username or password";return;}
    const d=await r.json();
    TOKEN=d.token;
    document.getElementById("login-screen").style.display="none";
    document.getElementById("app").style.display="block";
    loadDashboard();
    setInterval(refreshBell,60000);
  }catch(e){err.textContent="Connection error: "+e.message;}
}

async function api(path,method="GET",body=null){
  const opt={method,headers:{"Authorization":"Bearer "+TOKEN}};
  if(body){opt.headers["Content-Type"]="application/json";opt.body=JSON.stringify(body);}
  const r=await fetch(API+path,opt);
  if(r.status===401){location.reload();throw new Error("Session expired");}
  if(!r.ok){const e=await r.json().catch(()=>({detail:"Error"}));throw new Error(e.detail||"Request failed");}
  return r.json();
}

const titles={dashboard:"Dashboard",students:"Active Students",confirmed:"Confirmed Students",
  required:"Document Required",history:"Change History",runlogs:"Run Logs",upload:"Upload Excel",settings:"Settings"};
function nav(page){
  document.querySelectorAll(".nav-item").forEach(n=>n.classList.toggle("active",n.dataset.page===page));
  document.querySelectorAll(".page-section").forEach(s=>s.classList.remove("active"));
  document.getElementById("sec-"+page).classList.add("active");
  document.getElementById("page-title").textContent=titles[page];
  if(page==="dashboard")loadDashboard();
  if(page==="students")loadStudents(1);
  if(page==="confirmed")loadConfirmed(1);
  if(page==="required")loadRequired(1);
  if(page==="history")loadHistory();
  if(page==="runlogs")loadRunLogs();
  if(page==="settings")loadIntervals();
}

async function loadDashboard(){
  try{
    const d=await api("/api/dashboard");
    const c=d.counts||{};
    document.getElementById("stat-grid").innerHTML=
      statCard("Total Students",d.total_students,"👥","#4F46E5")+
      statCard("Changes Today",c.changes_today||0,"🔄","#0891B2")+
      statCard("Admission Confirmed",c.confirmed||0,"🎉","#16A34A")+
      statCard("Verified",c.verified||0,"🟢","#65A30D")+
      statCard("Document Required",c.document_required||0,"📄","#EA580C")+
      statCard("In Verification",c.doc_verification||0,"🟠","#D97706");
    renderDistribution(d.status_distribution,d.total_students);
    renderRuns(d.recent_runs,"recent-runs");
    renderBell(d.notifications||[]);
  }catch(e){showToast("❌ "+e.message);}
}
function statCard(lbl,val,ic,col){
  return '<div class="stat"><div class="ic">'+ic+'</div><div class="lbl">'+lbl+
    '</div><div class="val">'+val+'</div><div class="bar" style="background:'+col+'"></div></div>';
}
const DIST_COLOURS={
  "Pending":"#FBBF24","Documents Verification In Progress":"#FB923C","Document Required":"#F97316",
  "Verified":"#4ADE80","Approved":"#2DD4BF","Admission Confirmed":"#22C55E",
  "Rejected":"#EF4444","Fetch Error":"#94A3B8","Unknown":"#CBD5E1"};
function renderDistribution(dist,total){
  const el=document.getElementById("distribution");
  if(!dist||!dist.length){el.innerHTML='<div class="empty">No data yet. Upload Excel & run a check.</div>';return;}
  const max=Math.max.apply(null,dist.map(d=>d.cnt).concat([1]));
  el.innerHTML=dist.map(d=>{
    const col=DIST_COLOURS[d.current_status]||"#CBD5E1";
    const w=(d.cnt/max*100).toFixed(0);
    return '<div class="dist-row"><div class="nm">'+(d.current_status||"Unknown")+'</div>'+
      '<div class="track"><div class="fill" style="width:'+w+'%;background:'+col+'"></div></div>'+
      '<div class="ct">'+d.cnt+'</div></div>';
  }).join("");
}
function renderRuns(runs,id){
  const el=document.getElementById(id);
  if(!runs||!runs.length){el.innerHTML='<tr><td colspan="6" class="empty">No runs yet</td></tr>';return;}
  el.innerHTML=runs.map(r=>'<tr><td>'+r.run_at+'</td><td><span class="ref-tag">'+(r.group_type||"all")+
    '</span></td><td>'+r.total_checked+'</td><td style="color:var(--primary);font-weight:700">'+r.total_changed+
    '</td><td style="color:'+(r.total_failed?'var(--danger)':'inherit')+'">'+r.total_failed+
    '</td><td>'+r.status+'</td></tr>').join("");
}

function toggleBell(e){e.stopPropagation();document.getElementById("bell-dropdown").classList.toggle("open");}
document.addEventListener("click",()=>document.getElementById("bell-dropdown").classList.remove("open"));
async function refreshBell(){try{const d=await api("/api/dashboard");renderBell(d.notifications||[]);}catch(e){}}
function renderBell(notifs){
  const n=notifs.length;
  const badge=document.getElementById("bell-badge");
  const navB=document.getElementById("nav-required-badge");
  badge.textContent=n;badge.style.display=n?"flex":"none";
  navB.textContent=n;navB.style.display=n?"inline-block":"none";
  document.getElementById("bell-head-cnt").textContent=n;
  const list=document.getElementById("bell-list");
  list.innerHTML=n?notifs.map(x=>'<div class="notif-item"><div class="dot"></div><div>'+
    '<div class="nm">'+(x.student_name||"—")+'</div>'+
    '<div class="rf">'+(x.reference_no||"No ref")+'</div>'+
    (x.remark?'<div class="rk">'+x.remark+'</div>':"")+'</div></div>').join("")
    :'<div class="notif-empty">🎉 No pending documents!</div>';
}

let stTimer,cTimer,rTimer;
function debounceStudents(){clearTimeout(stTimer);stTimer=setTimeout(()=>loadStudents(1),400);}
function debounceConfirmed(){clearTimeout(cTimer);cTimer=setTimeout(()=>loadConfirmed(1),400);}
function debounceRequired(){clearTimeout(rTimer);rTimer=setTimeout(()=>loadRequired(1),400);}

function badge(s){
  const m={"Pending":"b-pending","Documents Verification In Progress":"b-docs","Document Required":"b-required",
    "Verified":"b-verified","Approved":"b-approved","Admission Confirmed":"b-confirmed","Rejected":"b-rejected"};
  return '<span class="badge '+(m[s]||'b-error')+'">'+(s||'Unknown')+'</span>';
}
function dlLinks(s){
  let l=[];
  if(s.id_card_link)l.push('<a href="'+s.id_card_link+'" target="_blank" style="color:var(--primary)">ID Card</a>');
  if(s.app_form_link)l.push('<a href="'+s.app_form_link+'" target="_blank" style="color:var(--primary)">App Form</a>');
  if(s.hall_ticket_link)l.push('<a href="'+s.hall_ticket_link+'" target="_blank" style="color:var(--primary)">Hall Ticket</a>');
  return l.length?l.join("<br>"):'<span style="color:var(--muted)">Phase 2</span>';
}
function fillSessions(arr){
  if(!arr)return;
  ["s-session","c-session","r-session"].forEach(id=>{
    const sel=document.getElementById(id);
    if(!sel)return;
    const cur=sel.value;
    sel.length=1;  // keep only the first "All Sessions" option
    arr.forEach(s=>{if(s){const o=document.createElement("option");o.value=s;o.textContent=s;sel.appendChild(o);}});
    sel.value=cur; // restore previous selection
  });
}

async function loadStudents(page){
  page=page||1;
  const q=new URLSearchParams({page:page,per_page:perPage,view:"normal",
    search:document.getElementById("s-search").value,
    status_filter:document.getElementById("s-status").value,
    session_filter:document.getElementById("s-session").value});
  try{
    const d=await api("/api/students?"+q.toString());
    fillSessions(d.sessions);
    document.getElementById("s-count").textContent="Showing "+d.students.length+" of "+d.total+" active students";
    const b=document.getElementById("s-body");
    b.innerHTML=d.students.length?d.students.map((s,i)=>'<tr>'+
      '<td style="color:var(--muted)">'+((page-1)*perPage+i+1)+'</td>'+
      '<td><span class="ref-tag">'+(s.reference_no||"—")+'</span></td>'+
      '<td>'+(s.student_name||"—")+'</td><td style="font-size:13px">'+(s.session||"—")+'</td>'+
      '<td>'+badge(s.current_status)+'</td>'+
      '<td style="font-size:12px;color:var(--muted)">'+(s.last_checked||"—")+'</td></tr>').join("")
      :'<tr><td colspan="6" class="empty">No active students found</td></tr>';
    renderPg("s-pg",page,d.pages,"loadStudents");
  }catch(e){showToast("❌ "+e.message);}
}

async function loadConfirmed(page){
  page=page||1;
  const q=new URLSearchParams({page:page,per_page:perPage,view:"confirmed",
    search:document.getElementById("c-search").value,
    session_filter:document.getElementById("c-session").value});
  try{
    const d=await api("/api/students?"+q.toString());
    fillSessions(d.sessions);
    document.getElementById("c-count").textContent=d.total+" confirmed students";
    const b=document.getElementById("c-body");
    b.innerHTML=d.students.length?d.students.map((s,i)=>'<tr>'+
      '<td style="color:var(--muted)">'+((page-1)*perPage+i+1)+'</td>'+
      '<td><span class="ref-tag">'+(s.reference_no||"—")+'</span></td>'+
      '<td>'+(s.student_name||"—")+'</td><td style="font-size:13px">'+(s.session||"—")+'</td>'+
      '<td>'+badge(s.current_status)+'</td><td style="font-size:12px">'+dlLinks(s)+'</td>'+
      '<td style="font-size:12px;color:var(--muted)">'+(s.last_changed||"—")+'</td></tr>').join("")
      :'<tr><td colspan="7" class="empty">No confirmed students yet</td></tr>';
    renderPg("c-pg",page,d.pages,"loadConfirmed");
  }catch(e){showToast("❌ "+e.message);}
}

async function loadRequired(page){
  page=page||1;
  const q=new URLSearchParams({page:page,per_page:perPage,view:"required",
    search:document.getElementById("r-search").value,
    session_filter:document.getElementById("r-session").value});
  try{
    const d=await api("/api/students?"+q.toString());
    fillSessions(d.sessions);
    document.getElementById("r-count").textContent=d.total+" students need documents";
    const b=document.getElementById("r-body");
    b.innerHTML=d.students.length?d.students.map((s,i)=>'<tr>'+
      '<td style="color:var(--muted)">'+((page-1)*perPage+i+1)+'</td>'+
      '<td><span class="ref-tag">'+(s.reference_no||"—")+'</span></td>'+
      '<td>'+(s.student_name||"—")+'</td><td style="font-size:13px">'+(s.session||"—")+'</td>'+
      '<td style="font-size:13px;color:var(--warn);max-width:420px">'+(s.remark||"(no comment captured)")+'</td></tr>').join("")
      :'<tr><td colspan="5" class="empty">🎉 No pending documents!</td></tr>';
    renderPg("r-pg",page,d.pages,"loadRequired");
  }catch(e){showToast("❌ "+e.message);}
}

function renderPg(id,page,total,fnName){
  const el=document.getElementById(id);
  let ctrl='<div class="pg-controls">';
  ctrl+='<button onclick="'+fnName+'('+(page-1)+')" '+(page<=1?"disabled":"")+'>‹ Prev</button>';
  const start=Math.max(1,page-2),end=Math.min(total,page+2);
  for(let i=start;i<=end;i++)ctrl+='<button class="'+(i===page?'active':'')+'" onclick="'+fnName+'('+i+')">'+i+'</button>';
  ctrl+='<button onclick="'+fnName+'('+(page+1)+')" '+(page>=total?"disabled":"")+'>Next ›</button></div>';
  const sel='<div class="perpage">Per page: <select onchange="setPerPage(this.value,&quot;'+fnName+'&quot;)">'+
    [10,20,50,100].map(n=>'<option value="'+n+'" '+(n===perPage?"selected":"")+'>'+n+'</option>').join("")+
    '</select></div>';
  el.innerHTML=ctrl+sel;
}
function setPerPage(v,fnName){perPage=parseInt(v);window[fnName](1);}

async function loadHistory(){
  try{const h=await api("/api/history");
    document.getElementById("h-body").innerHTML=h.length?h.map(x=>'<tr>'+
      '<td><span class="ref-tag">'+(x.reference_no||"—")+'</span></td><td>'+(x.student_name||"—")+'</td>'+
      '<td>'+badge(x.old_status)+'</td><td>'+badge(x.new_status)+'</td>'+
      '<td style="font-size:12px;color:var(--muted)">'+x.changed_at+'</td></tr>').join("")
      :'<tr><td colspan="5" class="empty">No changes recorded yet</td></tr>';
  }catch(e){showToast("❌ "+e.message);}
}
async function loadRunLogs(){
  try{const l=await api("/api/run-logs");renderRuns(l,"rl-body");}catch(e){showToast("❌ "+e.message);}
}

const drop=document.getElementById("drop");
["dragover","dragenter"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add("drag");}));
["dragleave","drop"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove("drag");}));
drop.addEventListener("drop",e=>{if(e.dataTransfer.files[0])handleFile(e.dataTransfer.files[0]);});
async function handleFile(file){
  if(!file)return;
  const st=document.getElementById("upload-status");
  st.innerHTML='<div style="color:var(--muted)">⏳ Uploading...</div>';
  const fd=new FormData();fd.append("file",file);
  try{
    const r=await fetch(API+"/api/upload-excel",{method:"POST",headers:{"Authorization":"Bearer "+TOKEN},body:fd});
    const d=await r.json();
    if(!r.ok)throw new Error(d.detail||"Upload failed");
    st.innerHTML='<div style="color:var(--success);font-weight:600">✅ '+d.message+'</div>'+
      '<div style="margin-top:12px"><button class="btn btn-success btn-sm" onclick="runNow()">▶ Run Check Now</button></div>';
    showToast("✅ Excel uploaded!");
  }catch(e){st.innerHTML='<div style="color:var(--danger)">❌ '+e.message+'</div>';}
}
async function downloadExcel(){
  const r=await fetch(API+"/api/download-excel",{headers:{"Authorization":"Bearer "+TOKEN}});
  if(!r.ok){showToast("❌ No Excel found. Run a check first.");return;}
  const blob=await r.blob();const url=URL.createObjectURL(blob);
  const a=document.createElement("a");a.href=url;a.download="nios_status_updated.xlsx";a.click();
  URL.revokeObjectURL(url);
}
async function runNow(){
  try{const r=await api("/api/run-now","POST");showToast("▶ "+r.message+" (background mein chal raha hai)");}
  catch(e){showToast("❌ "+e.message);}
}

async function loadIntervals(){
  try{const r=await api("/api/intervals");
    document.getElementById("iv-regular").value=r.regular;
    document.getElementById("iv-public").value=r.public;}catch(e){}
}
async function saveIntervals(){
  const regular=parseInt(document.getElementById("iv-regular").value);
  const pub=parseInt(document.getElementById("iv-public").value);
  try{const r=await api("/api/intervals","POST",{regular:regular,public:pub});
    document.getElementById("iv-status").innerHTML='<span style="color:var(--success)">✅ '+r.message+'</span>';
    showToast("✅ Intervals saved!");}
  catch(e){document.getElementById("iv-status").innerHTML='<span style="color:var(--danger)">❌ '+e.message+'</span>';}
}

function showToast(msg){
  const t=document.getElementById("toast");t.textContent=msg;t.classList.add("show");
  setTimeout(()=>t.classList.remove("show"),3500);
}
</script>
</body>
</html>
"""

def create_token(username):
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

def reschedule_jobs():
    """Set up the two interval jobs from DB settings."""
    reg = int(get_setting("interval_regular", "6"))
    pub = int(get_setting("interval_public", "12"))
    scheduler.add_job(lambda: run_status_check("regular"), trigger=IntervalTrigger(hours=reg),
                      id="job_regular", replace_existing=True, next_run_time=None)
    scheduler.add_job(lambda: run_status_check("public"), trigger=IntervalTrigger(hours=pub),
                      id="job_public", replace_existing=True, next_run_time=None)
    logger.info(f"Jobs scheduled — regular:{reg}h public:{pub}h")

@app.on_event("startup")
async def startup():
    init_db()
    reschedule_jobs()
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
    return {"status": "ok", "captcha_key_set": bool(os.environ.get("CAPTCHA_API_KEY", ""))}

@app.post("/api/login")
async def login(body: dict):
    if body.get("username") != PORTAL_USER or body.get("password") != PORTAL_PASS:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"token": create_token(body.get("username")), "username": body.get("username")}

@app.get("/api/dashboard")
async def dashboard(user=Depends(verify_token)):
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM student_status").fetchone()[0]
    runs = conn.execute("SELECT * FROM run_logs ORDER BY id DESC LIMIT 10").fetchall()
    dist = conn.execute("SELECT current_status, COUNT(*) as cnt FROM student_status GROUP BY current_status").fetchall()

    # Counts by status
    def count_status(s):
        return conn.execute("SELECT COUNT(*) FROM student_status WHERE current_status=?", (s,)).fetchone()[0]
    confirmed_cnt = count_status("Admission Confirmed")
    verified_cnt  = count_status("Verified")
    docreq_cnt    = count_status("Document Required")
    docverif_cnt  = count_status("Documents Verification In Progress")

    # Changes today
    today = datetime.now().strftime("%Y-%m-%d")
    changes_today = conn.execute(
        "SELECT COUNT(*) FROM status_history WHERE changed_at LIKE ?", (f"{today}%",)).fetchone()[0]

    # Notifications: document required students (name + ref)
    notifs = conn.execute(
        "SELECT student_name, reference_no, remark FROM student_status WHERE current_status='Document Required' ORDER BY last_changed DESC LIMIT 50"
    ).fetchall()

    conn.close()
    jr = scheduler.get_job("job_regular")
    next_run = str(jr.next_run_time) if jr and jr.next_run_time else "Not scheduled"
    return {
        "total_students": total, "next_run": next_run,
        "status_distribution": [dict(r) for r in dist],
        "recent_runs": [dict(r) for r in runs],
        "counts": {
            "confirmed": confirmed_cnt, "verified": verified_cnt,
            "document_required": docreq_cnt, "doc_verification": docverif_cnt,
            "changes_today": changes_today,
        },
        "notifications": [dict(n) for n in notifs],
    }

@app.get("/api/students")
async def get_students(page: int=1, per_page: int=50, search: str="",
                       status_filter: str="", session_filter: str="",
                       view: str="normal", user=Depends(verify_token)):
    conn = get_db()
    offset = (page - 1) * per_page
    wc, params = [], []

    # View determines base filter
    if view == "confirmed":
        wc.append("is_confirmed = 1")
    elif view == "required":
        wc.append("current_status = 'Document Required'")
    else:  # normal = active students, exclude confirmed
        wc.append("is_confirmed = 0")

    if search:
        wc.append("(reference_no LIKE ? OR student_name LIKE ? OR email LIKE ?)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]
    if status_filter:
        wc.append("current_status = ?"); params.append(status_filter)
    if session_filter:
        wc.append("session = ?"); params.append(session_filter)
    where = ("WHERE " + " AND ".join(wc)) if wc else ""
    total = conn.execute(f"SELECT COUNT(*) FROM student_status {where}", params).fetchone()[0]
    students = conn.execute(
        f"SELECT * FROM student_status {where} ORDER BY student_name LIMIT ? OFFSET ?",
        params + [per_page, offset]).fetchall()
    sessions = conn.execute("SELECT DISTINCT session FROM student_status WHERE session != ''").fetchall()
    conn.close()
    return {"students": [dict(s) for s in students], "total": total, "page": page,
            "per_page": per_page, "pages": max(1, (total+per_page-1)//per_page),
            "sessions": [s["session"] for s in sessions]}

@app.get("/api/history")
async def get_history(limit: int=200, user=Depends(verify_token)):
    conn = get_db()
    h = conn.execute("SELECT * FROM status_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(x) for x in h]

@app.get("/api/run-logs")
async def get_run_logs(limit: int=50, user=Depends(verify_token)):
    conn = get_db()
    l = conn.execute("SELECT * FROM run_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(x) for x in l]

@app.post("/api/run-now")
async def run_now(background_tasks: BackgroundTasks, user=Depends(verify_token)):
    background_tasks.add_task(run_status_check, "all")
    return {"message": "Run triggered for all students!"}

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

@app.get("/api/intervals")
async def get_intervals(user=Depends(verify_token)):
    return {"regular": int(get_setting("interval_regular", "6")),
            "public": int(get_setting("interval_public", "12"))}

@app.post("/api/intervals")
async def set_intervals(body: dict, user=Depends(verify_token)):
    reg = int(body.get("regular", 6))
    pub = int(body.get("public", 12))
    if not (1 <= reg <= 72) or not (1 <= pub <= 72):
        raise HTTPException(status_code=400, detail="Hours must be 1-72")
    set_setting("interval_regular", reg)
    set_setting("interval_public", pub)
    reschedule_jobs()
    return {"message": f"Regular: {reg}h, Public: {pub}h", "regular": reg, "public": pub}
