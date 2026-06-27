import os
import logging
import time as _time
os.environ["TZ"] = "Asia/Kolkata"
try:
    _time.tzset()
except Exception:
    pass
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import aiofiles

from database import init_db, get_db, get_setting, set_setting
from job_runner import run_status_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

import secrets as _secrets
# SECRET_KEY signs the login tokens. NEVER fall back to a public hardcoded value (that
# would let anyone forge a valid token). If the env var is missing we generate a strong
# random key for this process — tokens then simply reset on restart (re-login needed).
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = _secrets.token_hex(32)
    logger.warning("SECRET_KEY not set in environment — using a random key for this run. "
                   "Set SECRET_KEY so logins survive restarts.")
PORTAL_USER = os.environ.get("PORTAL_USER", "admin")
PORTAL_PASS = os.environ.get("PORTAL_PASS", "MVS2025")
EXCEL_PATH  = os.environ.get("EXCEL_PATH",  os.path.join(os.environ.get("DATA_DIR", "."), "students.xlsx"))
ALGORITHM   = "HS256"
TOKEN_EXPIRE_HOURS = 12

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer  = HTTPBearer()

app = FastAPI(title="NIOS Status Tracker", version="2.0.0")
# CORS: the portal UI is served from the SAME origin as the API, so cross-origin access
# isn't needed. Restrict to the known production domains (override with ALLOWED_ORIGINS)
# so other websites cannot call the data API from a browser.
_allowed = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if not _allowed:
    _allowed = ["https://status.mvsfoundation.in",
                "https://web-production-09671.up.railway.app"]
app.add_middleware(CORSMiddleware, allow_origins=_allowed,
                   allow_methods=["GET", "POST"], allow_headers=["*"],
                   allow_credentials=False)

PORTAL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NIOS Status Tracker — MVS Foundation</title>
<style>
  :root{
    --primary:#4F46E5; --primary-dark:#4338CA; --primary-light:#EEF2FF;
    --sidebar:#1E293B; --sidebar-hover:#334155; --sidebar-text:#CBD5E1; --sidebar-muted:#64748B;
    --bg:#F1F5F9; --card:#FFFFFF; --border:#E2E8F0; --text:#0F172A;
    --muted:#64748B; --success:#16A34A; --danger:#DC2626; --warn:#EA580C;
    --th-bg:#F8FAFC; --row-hover:#FAFBFF; --chip:#F1F5F9; --soft:#F8FAFC; --dup-bg:#FFF7ED;
    --shadow:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.04);
    --shadow-lg:0 10px 25px rgba(0,0,0,.08);
  }
  html[data-theme="dark"]{
    --primary:#6366F1; --primary-dark:#4F46E5; --primary-light:#1E1B4B;
    --sidebar:#0B1220; --sidebar-hover:#1E293B; --sidebar-text:#CBD5E1; --sidebar-muted:#64748B;
    --bg:#0F172A; --card:#1E293B; --border:#334155; --text:#E2E8F0;
    --muted:#94A3B8; --success:#22C55E; --danger:#F87171; --warn:#FB923C;
    --th-bg:#172033; --row-hover:#243043; --chip:#334155; --soft:#172033; --dup-bg:#3a2a18;
    --shadow:0 1px 3px rgba(0,0,0,.4); --shadow-lg:0 10px 25px rgba(0,0,0,.5);
  }
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{overflow-x:hidden;max-width:100%}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}
  img{max-width:100%}
  a{text-decoration:none}
  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-thumb{background:#CBD5E1;border-radius:4px}

  #login-screen{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
    background:linear-gradient(135deg,#4F46E5 0%,#7C3AED 100%);z-index:1000;padding:16px}
  .login-card{background:var(--card);border-radius:18px;padding:42px;width:380px;max-width:100%;
    box-shadow:0 20px 60px rgba(0,0,0,.3)}
  @media(max-width:480px){.login-card{padding:28px 22px}}
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
    padding:20px 0 12px;display:flex;flex-direction:column;z-index:100}
  .sidebar .brand{padding:0 22px 20px;display:flex;align-items:center;gap:11px;
    border-bottom:1px solid rgba(255,255,255,.08);margin-bottom:12px}
  .sidebar .brand .ic{width:38px;height:38px;background:linear-gradient(135deg,#4F46E5,#7C3AED);
    border-radius:10px;display:flex;align-items:center;justify-content:center}
  .sidebar .brand .ic svg{width:21px;height:21px}
  .sidebar .brand .tx b{color:#fff;font-size:15px;display:block}
  .sidebar .brand .tx span{color:#94A3B8;font-size:11px}
  .nav-scroll{flex:1;overflow-y:auto}
  .side-foot{border-top:1px solid rgba(255,255,255,.08);padding-top:8px;margin-top:6px}
  .nav-item{display:flex;align-items:center;gap:12px;padding:11px 22px;color:var(--sidebar-text);
    cursor:pointer;font-size:14px;font-weight:500;transition:.15s;border-left:3px solid transparent}
  .nav-item:hover{background:var(--sidebar-hover);color:#fff}
  .nav-item.active{background:var(--sidebar-hover);color:#fff;border-left-color:#818CF8;font-weight:600}
  .nav-item.active .ic{color:#A5B4FC}
  .nav-item.logout:hover{background:rgba(248,113,113,.14);color:#FCA5A5}
  .nav-item .ic{width:20px;height:20px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
  .nav-item .ic svg{width:19px;height:19px}
  .nav-item .badge-count{margin-left:auto;background:var(--danger);color:#fff;
    font-size:11px;padding:1px 7px;border-radius:10px;font-weight:700;display:none}
  .nav-item .nav-num{margin-left:auto;background:rgba(255,255,255,.14);color:#fff;
    font-size:11px;padding:1px 8px;border-radius:10px;font-weight:700}
  .nav-item.active .nav-num{background:rgba(255,255,255,.22)}
  .nav-sep{padding:14px 22px 6px;color:var(--sidebar-muted);font-size:11px;font-weight:700;
    text-transform:uppercase;letter-spacing:.5px}

  .main{margin-left:240px;min-height:100vh}
  .topbar{background:var(--card);border-bottom:1px solid var(--border);padding:0 28px;height:64px;
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
    background:var(--card);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow-lg);
    display:none;z-index:200}
  .bell-dropdown.open{display:block}
  .bell-head{padding:14px 18px;border-bottom:1px solid var(--border);font-weight:700;
    font-size:14px;display:flex;align-items:center;justify-content:space-between}
  .bell-head .cnt{background:var(--warn);color:#fff;font-size:11px;padding:2px 9px;border-radius:10px}
  .notif-item{padding:13px 18px;border-bottom:1px solid var(--border);display:flex;gap:11px}
  .notif-item:last-child{border-bottom:none}
  .notif-item .nm{color:var(--text)}
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
  /* Settings — professional card headers with icon chips */
  .set-head{display:flex;align-items:flex-start;gap:13px;margin-bottom:18px}
  .set-ico{width:40px;height:40px;border-radius:11px;display:flex;align-items:center;
    justify-content:center;color:#fff;flex-shrink:0;box-shadow:0 3px 9px rgba(15,23,42,.12)}
  .set-ico svg{width:20px;height:20px}
  .set-head .set-tt{flex:1;min-width:0}
  .set-head h3{font-size:15.5px;font-weight:700;margin:0;line-height:1.3}
  .set-head .set-sub{font-size:12.5px;color:var(--muted);margin-top:4px;line-height:1.5}
  .set-head .set-act{margin-left:auto;flex-shrink:0}
  .iv-row{display:flex;gap:12px;align-items:center;margin-bottom:11px;flex-wrap:wrap}
  .iv-row .iv-lbl{width:250px;display:flex;align-items:center;gap:11px;font-weight:600;font-size:14px}
  .iv-row .iv-dot{width:11px;height:11px;border-radius:4px;flex-shrink:0}
  .iv-row input,.iv-row select{padding:10px 12px;border:2px solid var(--border);
    border-radius:10px;font-size:14px;background:var(--card);color:var(--text)}
  .iv-row input{width:84px}
  .iv-row input:focus,.iv-row select:focus{outline:none;border-color:var(--primary)}
  .set-note{font-size:11.5px;color:var(--muted);margin-top:10px;line-height:1.5}
  .set-foot{margin-top:18px;padding-top:16px;border-top:1px solid var(--border)}

  .stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:16px;margin-bottom:22px}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;
    box-shadow:var(--shadow);position:relative;overflow:hidden}
  .stat .ic{position:absolute;right:16px;top:16px;width:38px;height:38px;border-radius:10px;
    display:flex;align-items:center;justify-content:center}
  .stat .ic svg{width:20px;height:20px}
  .stat .lbl{font-size:13px;color:var(--muted);font-weight:600;margin-bottom:8px}
  .stat .val{font-size:30px;font-weight:800;line-height:1}
  .stat .bar{position:absolute;left:0;bottom:0;height:4px;width:100%}

  .btn{display:inline-flex;align-items:center;gap:8px;padding:11px 20px;border-radius:10px;
    font-size:14px;font-weight:600;cursor:pointer;border:none;transition:.15s}
  .btn-primary{background:var(--primary);color:#fff}
  .btn-primary:hover{background:var(--primary-dark);transform:translateY(-1px);box-shadow:0 4px 12px rgba(79,70,229,.3)}
  .btn-outline{background:var(--card);color:var(--primary);border:2px solid var(--primary)}
  .btn-outline:hover{background:var(--primary-light)}
  .btn-success{background:var(--success);color:#fff}
  .btn-success:hover{filter:brightness(1.08)}
  .btn-sm{padding:8px 15px;font-size:13px;border-radius:8px}
  .btn-dl{padding:6px 11px;font-size:12px;font-weight:600;border-radius:7px;cursor:pointer;
    border:1.5px solid var(--primary);background:var(--primary-light);color:var(--primary);transition:.15s}
  .btn-dl:hover{background:var(--primary);color:#fff}
  .btn-dl.loading{background:var(--primary);color:#fff;pointer-events:none;opacity:.92}
  .btn-dl svg{vertical-align:-2px}
  .dl-spin{display:inline-block;width:11px;height:11px;border:2px solid rgba(255,255,255,.45);
    border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;vertical-align:-1px}

  .filter-bar{display:flex;gap:12px;margin-bottom:18px;flex-wrap:wrap}
  .filter-bar input,.filter-bar select{padding:11px 14px;border:2px solid var(--border);
    border-radius:10px;font-size:14px;transition:.15s;background:var(--card);color:var(--text)}
  .filter-bar input{flex:1;min-width:200px}
  .filter-bar input:focus,.filter-bar select:focus{outline:none;border-color:var(--primary)}
  table{width:100%;border-collapse:collapse}
  thead th{text-align:left;padding:13px 14px;font-size:12px;font-weight:700;color:var(--muted);
    text-transform:uppercase;letter-spacing:.4px;background:var(--th-bg);border-bottom:2px solid var(--border)}
  tbody td{padding:14px;border-bottom:1px solid var(--border);font-size:14px;vertical-align:top}
  tbody tr:hover{background:var(--row-hover)}
  .ref-tag{background:var(--chip);padding:3px 8px;border-radius:6px;font-family:monospace;font-size:13px}

  .run-live{color:#2563EB;font-weight:700;font-size:13px}
  .run-done{color:#16A34A;font-weight:700;font-size:13px}
  .run-cancel{color:#94A3B8;font-weight:700;font-size:13px}
  .run-err{color:var(--danger);font-weight:700;font-size:12px}
  .btn-cancel{margin-left:8px;background:#FEE2E2;color:#B91C1C;border:1px solid #FCA5A5;
    padding:3px 11px;border-radius:7px;font-size:12px;font-weight:700;cursor:pointer}
  .btn-cancel:hover{background:#FCA5A5;color:#7F1D1D}

  .card-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
  .card-head h3{margin-bottom:0}
  .btn-refresh{display:inline-flex;align-items:center;gap:6px;padding:7px 13px;border-radius:8px;
    font-size:13px;font-weight:600;cursor:pointer;border:1px solid var(--border);
    background:var(--soft);color:var(--text);transition:.15s}
  .btn-refresh:hover{background:var(--primary);color:#fff;border-color:var(--primary)}
  .btn-refresh:hover svg{color:#fff}

  .progress-banner{background:var(--card);border:1px solid var(--primary);border-left:4px solid var(--primary);
    border-radius:12px;padding:16px 18px;margin-bottom:18px;box-shadow:var(--shadow)}
  .pb-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:9px}
  .pb-title{display:flex;align-items:center;gap:9px;font-weight:700;font-size:14px;color:var(--text)}
  .pb-pct{font-weight:800;font-size:18px;color:var(--primary)}
  .pb-track{height:9px;background:var(--chip);border-radius:6px;overflow:hidden}
  .pb-fill{height:100%;background:linear-gradient(90deg,#6366F1,#8B5CF6);border-radius:6px;
    transition:width .5s ease;width:0%}
  .pb-sub{font-size:12px;color:var(--muted);margin-top:7px}
  .pb-spin{width:15px;height:15px;border:2.5px solid var(--chip);border-top-color:var(--primary);
    border-radius:50%;animation:spin .8s linear infinite;display:inline-block}
  @keyframes spin{to{transform:rotate(360deg)}}

  .timer-row{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:18px}
  .timer-chip{flex:1;min-width:230px;display:flex;align-items:center;gap:12px;background:var(--card);
    border:1px solid var(--border);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}
  .timer-chip .tc-ic{width:38px;height:38px;border-radius:10px;display:flex;align-items:center;
    justify-content:center;background:var(--primary-light);color:var(--primary);flex-shrink:0}
  .timer-chip.soon{border-color:var(--warn);border-left:4px solid var(--warn)}
  .timer-chip.soon .tc-ic{background:rgba(234,88,12,.12);color:var(--warn)}
  .timer-chip.paused{border-left:4px solid #94a3b8;opacity:.9}
  .timer-chip.paused .tc-ic{background:rgba(148,163,184,.16);color:#64748b}
  .timer-chip.paused .tc-time{color:#64748b}
  .tc-btn{flex-shrink:0;display:inline-flex;align-items:center;gap:5px;border-radius:9px;padding:7px 12px;
    font-size:12.5px;font-weight:700;cursor:pointer;border:1px solid var(--border);transition:all .15s;white-space:nowrap}
  .tc-btn.pause{background:var(--soft);color:var(--text)}
  .tc-btn.pause:hover{background:#fff7ed;border-color:var(--warn);color:var(--warn)}
  .tc-btn.resume{background:#dcfce7;color:#166534;border-color:#bbf7d0}
  .tc-btn.resume:hover{background:#bbf7d0}
  .tc-lbl{font-size:12px;color:var(--muted);font-weight:600}
  .tc-time{font-size:18px;font-weight:800;color:var(--text);font-variant-numeric:tabular-nums}
  .tc-grp{font-size:11px;color:var(--muted)}
  .run-prog{font-size:11px;color:var(--muted);font-weight:600;margin-left:8px}

  .badge{display:inline-block;padding:5px 12px;border-radius:20px;font-size:12px;font-weight:700;white-space:nowrap}
  .b-pending{background:#FEF9C3;color:#854D0E}
  .b-docs{background:#FFEDD5;color:#9A3412}
  .b-required{background:#FED7AA;color:#9A3412}
  .b-verified{background:#DCFCE7;color:#166534}
  .b-approved{background:#CCFBF1;color:#115E59}
  .b-confirmed{background:#86EFAC;color:#14532D}
  .b-rejected{background:#FECACA;color:#991B1B}
  .b-error{background:#E2E8F0;color:#475569}
  .b-syc{background:#EDE9FE;color:#5B21B6}

  .pg-bar{display:flex;align-items:center;justify-content:space-between;margin-top:18px;flex-wrap:wrap;gap:12px}
  .pg-controls{display:flex;gap:6px;align-items:center}
  .pg-controls button{padding:8px 13px;border:1px solid var(--border);background:var(--card);color:var(--text);
    border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;transition:.15s}
  .pg-controls button:hover:not(:disabled){background:var(--primary-light);border-color:var(--primary)}
  .pg-controls button.active{background:var(--primary);color:#fff;border-color:var(--primary)}
  .pg-controls button:disabled{opacity:.4;cursor:not-allowed}
  .perpage{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}
  .perpage select{padding:7px 10px;border:1px solid var(--border);border-radius:8px;font-size:13px;background:var(--card);color:var(--text)}

  .drop{border:2.5px dashed var(--border);border-radius:14px;padding:48px;text-align:center;
    transition:.2s;cursor:pointer;background:#FAFBFF}
  .drop:hover,.drop.drag{border-color:var(--primary);background:var(--primary-light)}
  .drop .ic{margin-bottom:12px;color:var(--primary)}
  .drop .ic svg{width:42px;height:42px}

  .up-stats{display:flex;gap:14px;flex-wrap:wrap}
  .up-stat{flex:1;min-width:150px;background:var(--soft);border:1px solid var(--border);
    border-radius:12px;padding:14px 16px}
  .up-stat.dup{border-left:4px solid var(--warn)}
  .up-stat.new{border-left:4px solid var(--success)}
  .us-lbl{font-size:12px;color:var(--muted);font-weight:600;margin-bottom:6px}
  .us-val{font-size:26px;font-weight:800;line-height:1}
  .up-stat.dup .us-val{color:var(--warn)}
  .up-stat.new .us-val{color:var(--success)}
  .prev-head{font-size:13px;font-weight:700;margin-bottom:8px;color:var(--text)}
  .prev-box{max-height:380px;overflow:auto;border:1px solid var(--border);border-radius:12px}
  .prev-box table{width:100%}
  .prev-box thead th{position:sticky;top:0;z-index:1}
  .dup-tag{display:inline-block;background:#FEF3C7;color:#B45309;font-size:11px;font-weight:700;
    padding:2px 9px;border-radius:10px}
  .new-tag{display:inline-block;background:#DCFCE7;color:#15803D;font-size:11px;font-weight:700;
    padding:2px 9px;border-radius:10px}
  .sim-tag{display:inline-block;background:#FEF9C3;color:#854D0E;font-size:11px;font-weight:700;
    padding:2px 9px;border-radius:10px}
  .up-stat.sim{border-left:4px solid #CA8A04}
  .up-stat.sim .us-val{color:#CA8A04}
  .up-prog{background:var(--soft);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
  .up-prog-top{display:flex;justify-content:space-between;font-weight:700;font-size:13px;margin-bottom:8px}
  .up-track{height:8px;background:var(--chip);border-radius:5px;overflow:hidden}
  .up-fill{height:100%;width:0%;background:linear-gradient(90deg,#6366F1,#8B5CF6);border-radius:5px;transition:width .2s}
  .up-eta{font-size:12px;color:var(--muted);margin-top:7px}

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

  .nav-backdrop{display:none}

  /* ===== Tablet / iPad: slim icon-only sidebar, content uses the space ===== */
  @media(max-width:1024px) and (min-width:768px){
    .sidebar{width:64px}
    .sidebar .brand .tx,.nav-item span.lbl,.nav-sep{display:none}
    .nav-item{justify-content:center;padding:14px}
    .main{margin-left:64px}
    .content{padding:20px}
    .topbar{padding:0 18px}
    .bell-dropdown{width:340px}
    .stat-grid{grid-template-columns:repeat(auto-fill,minmax(170px,1fr))}
  }

  /* ===== Phone: off-canvas slide-in drawer, full-width content ===== */
  @media(max-width:767px){
    .sidebar{width:250px;transform:translateX(-100%);
      transition:transform .26s cubic-bezier(.4,0,.2,1);box-shadow:0 0 50px rgba(0,0,0,.35)}
    #app.nav-open .sidebar{transform:translateX(0)}
    .main{margin-left:0}
    /* drawer always shows full labels even if 'minimize' was toggled earlier */
    #app.sidebar-min .sidebar{width:250px}
    #app.sidebar-min .sidebar .brand .tx,
    #app.sidebar-min .nav-item span.lbl,
    #app.sidebar-min .nav-sep,
    #app.sidebar-min .nav-item .nav-num,
    #app.sidebar-min .nav-item .badge-count{display:revert!important}
    #app.sidebar-min .nav-item{justify-content:flex-start;padding:11px 22px}
    #app.sidebar-min .main{margin-left:0}
    .nav-backdrop{display:block;position:fixed;inset:0;background:rgba(15,23,42,.5);
      z-index:90;opacity:0;visibility:hidden;transition:opacity .26s,visibility .26s}
    #app.nav-open .nav-backdrop{opacity:1;visibility:visible}

    .topbar{padding:0 12px;height:58px}
    .topbar h1{font-size:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:40vw}
    .topbar .right{gap:8px}
    .content{padding:14px}
    .card{padding:16px;border-radius:12px;margin-bottom:16px}
    .card h3{font-size:14px}
    .stat-grid{grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
    .stat{padding:15px}
    .filter-bar{gap:8px;margin-bottom:14px}
    .filter-bar input,.filter-bar select{padding:10px 12px;font-size:14px}
    .filter-bar input{min-width:130px;flex:1 1 100%}
    .bell-dropdown{width:calc(100vw - 20px);right:-8px;max-height:70vh}
    #run-menu{min-width:0;width:calc(100vw - 24px);max-width:310px}
    .btn-sm{padding:9px 12px;font-size:13px}
    .legend-grid{grid-template-columns:1fr}
    table{font-size:13px}
    table th,table td{padding:10px 12px}
    .rn-txt{display:none}
    #run-now-btn{padding:9px 11px}
    /* hard guards so nothing forces a horizontal scroll / zoom-out */
    #app,.main,.content,.page-section,.card{max-width:100%;min-width:0}
    .content{overflow-x:hidden}
    #toast{left:12px;right:12px;bottom:18px;max-width:none;text-align:center}
    .iv-row .iv-lbl{width:auto;flex:1 1 100%}
    .topbar{max-width:100vw}
  }

  /* ===== Big screens / TV: cap content width so it never stretches awkwardly ===== */
  @media(min-width:1700px){
    .content{max-width:1640px;margin-left:auto;margin-right:auto}
  }
  /* Counsellor-toggled collapse (click only) — icon-only sidebar, content expands */
  #app.sidebar-min .sidebar{width:64px}
  #app.sidebar-min .sidebar .brand .tx,
  #app.sidebar-min .nav-item span.lbl,
  #app.sidebar-min .nav-sep,
  #app.sidebar-min .nav-item .nav-num,
  #app.sidebar-min .nav-item .badge-count{display:none!important}
  #app.sidebar-min .nav-item{justify-content:center;padding:14px;position:relative}
  /* small dot so a non-zero count is still visible when collapsed */
  #app.sidebar-min .nav-item .nav-num.has,
  #app.sidebar-min .nav-item .badge-count.has{display:block!important;position:absolute;top:8px;right:10px;
    min-width:0;width:8px;height:8px;padding:0;border-radius:50%;font-size:0;overflow:hidden}
  #app.sidebar-min .main{margin-left:64px}
  .sb-toggle{background:none;border:none;cursor:pointer;color:var(--text);padding:7px;border-radius:9px;
    display:flex;align-items:center;margin-right:6px}
  .sb-toggle:hover{background:var(--soft)}
</style>
<script>
function toggleSidebar(){
  var app=document.getElementById("app");if(!app)return;
  // On phones the hamburger opens an off-canvas drawer; on larger screens it
  // collapses the sidebar to icons (persisted).
  if(window.matchMedia("(max-width:767px)").matches){
    app.classList.toggle("nav-open");
    return;
  }
  var min=app.classList.toggle("sidebar-min");
  try{localStorage.setItem("mvs_sidebar_min",min?"1":"0");}catch(e){}
}
function closeNav(){var a=document.getElementById("app");if(a)a.classList.remove("nav-open");}
function applySidebarPref(){
  try{ if(localStorage.getItem("mvs_sidebar_min")==="1"){var a=document.getElementById("app");if(a)a.classList.add("sidebar-min");} }catch(e){}
}
</script>
</head>
<body>

<div id="login-screen">
  <div class="login-card">
    <img src="/logo.png" alt="MVS Foundation" style="width:68px;height:68px;border-radius:50%;display:block;margin:0 auto 18px;object-fit:cover;box-shadow:0 4px 14px rgba(0,0,0,.18)">
    <h2>NIOS Status Tracker</h2>
    <p class="sub">MVS Foundation — Admin Portal</p>
    <label>Username</label>
    <input type="text" id="lg-user" placeholder="admin" autocomplete="username">
    <label>Password</label>
    <div style="position:relative;margin-bottom:16px">
      <input type="password" id="lg-pass" placeholder="enter password" autocomplete="current-password"
        style="padding-right:44px;margin-bottom:0" onkeydown="if(event.key==='Enter')doLogin()">
      <button type="button" id="lg-pass-toggle" onclick="toggleLgPass()" aria-label="Show password"
        style="position:absolute;right:6px;top:50%;transform:translateY(-50%);width:auto!important;min-width:0;margin:0!important;background:none!important;border:none;cursor:pointer;padding:6px;color:#94a3b8;display:flex;align-items:center;justify-content:center">
        <svg id="lg-eye" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="19" height="19"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
      </button>
    </div>
    <button onclick="doLogin()">Sign In</button>
    <div id="login-error"></div>
  </div>
</div>

<div id="app">
  <aside class="sidebar">
    <div class="brand">
      <img src="/logo.png" alt="MVS" style="width:38px;height:38px;border-radius:50%;object-fit:cover;flex-shrink:0">
      <div class="tx"><b>NIOS Tracker</b><span>MVS Foundation</span></div>
    </div>
    <nav class="nav-scroll">
    <div class="nav-item active" data-page="dashboard" onclick="nav('dashboard')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg></span><span class="lbl">Dashboard</span></div>
    <div class="nav-item" data-page="students" onclick="nav('students')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg></span><span class="lbl">Active Students</span>
      <span class="nav-num" id="nav-students-badge" style="display:none">0</span></div>
    <div class="nav-item" data-page="confirmed" onclick="nav('confirmed')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></span><span class="lbl">Confirmed</span>
      <span class="nav-num" id="nav-confirmed-badge" style="display:none">0</span></div>
    <div class="nav-item" data-page="required" onclick="nav('required')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="11" x2="12" y2="15"/><line x1="12" y1="18" x2="12" y2="18"/></svg></span><span class="lbl">Required</span>
      <span class="badge-count" id="nav-required-badge">0</span></div>
    <div class="nav-item" data-page="docreq" onclick="nav('docreq')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></span><span class="lbl">Doc Requests</span>
      <span class="badge-count" id="nav-docreq-badge">0</span></div>
    <div class="nav-item" data-page="syc" onclick="nav('syc')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg></span><span class="lbl">SYC Students</span>
      <span class="nav-num" id="nav-syc-badge" style="display:none">0</span></div>
    <div class="nav-item" data-page="failed" onclick="nav('failed')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></span><span class="lbl">Failed to Run</span>
      <span class="badge-count" id="nav-failed-badge" style="display:none">0</span></div>
    <div class="nav-item" data-page="unknown" onclick="nav('unknown')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span><span class="lbl">Unknown</span>
      <span class="badge-count" id="nav-unknown-badge" style="display:none">0</span></div>
    <div class="nav-sep">Activity</div>
    <div class="nav-item" data-page="history" onclick="nav('history')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l4 2"/></svg></span><span class="lbl">Change History</span></div>
    <div class="nav-item" data-page="runlogs" onclick="nav('runlogs')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg></span><span class="lbl">Run Logs</span></div>
    <div class="nav-item" data-page="transfers" onclick="nav('transfers')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg></span><span class="lbl">Transfer Data</span></div>
    <div class="nav-sep">Manage</div>
    <div class="nav-item" data-page="upload" onclick="nav('upload')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg></span><span class="lbl">Upload Excel</span></div>
    <div class="nav-item" data-page="settings" onclick="nav('settings')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></span><span class="lbl">Settings</span></div>
    </nav>
    <div class="side-foot">
      <div class="nav-item" onclick="toggleTheme()">
        <span class="ic" id="theme-ic"></span><span class="lbl" id="theme-lbl">Dark Mode</span></div>
      <div class="nav-item logout" onclick="doLogout()">
        <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg></span><span class="lbl">Logout</span></div>
    </div>
  </aside>
  <div class="nav-backdrop" onclick="closeNav()"></div>

  <div class="main">
    <div class="topbar">
      <div style="display:flex;align-items:center;gap:4px">
        <button class="sb-toggle" onclick="toggleSidebar()" title="Minimize / maximize menu" aria-label="Toggle sidebar">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="20" height="20"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
        </button>
        <h1 id="page-title">Dashboard</h1>
      </div>
      <div class="right">
        <div class="run-menu-wrap" style="position:relative">
          <button class="btn btn-success btn-sm" id="run-now-btn" onclick="toggleRunMenu(event)">
            <svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14"><polygon points="5 3 19 12 5 21 5 3"/></svg> <span class="rn-txt">Run Now</span>
            <svg id="run-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" width="12" height="12" style="margin-left:5px;transition:transform .15s"><polyline points="6 9 12 15 18 9"/></svg></button>
          <div id="run-menu" style="display:none">
            <div class="rm-head"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> Run a status check</div>
            <button class="run-menu-item rmi-all" onclick="runChoice('all')">
              <span class="rmi-ic"><svg viewBox="0 0 24 24" fill="currentColor" width="15" height="15"><polygon points="5 3 19 12 5 21 5 3"/></svg></span>
              <span class="rmi-txt"><span class="rmi-main">Run all data</span>
              <span class="rmi-sub">MVS Tracker + MVS Portal together</span></span>
              <span class="rmi-go">&rsaquo;</span></button>

            <div class="rm-group trk">
              <div class="rm-ghead">
                <span class="rm-gic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="9" y1="10" x2="9" y2="20"/></svg></span>
                <span class="rm-gname">MVS Tracker</span>
                <span class="rm-ghint">by group</span>
              </div>
              <button class="run-menu-item trk" onclick="runChoice('tracker','ondemand')"><span class="rmi-dot"></span><span class="rmi-main">On Demand</span></button>
              <button class="run-menu-item trk" onclick="runChoice('tracker','stream2')"><span class="rmi-dot"></span><span class="rmi-main">Stream 2</span></button>
              <button class="run-menu-item trk" onclick="runChoice('tracker','public')"><span class="rmi-dot"></span><span class="rmi-main">Public</span></button>
              <button class="run-menu-item trk" onclick="runChoice('tracker','all')"><span class="rmi-dot"></span><span class="rmi-main">All Tracker</span><span class="rmi-tag">all</span></button>
            </div>

            <div class="rm-group por">
              <div class="rm-ghead">
                <span class="rm-gic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/><path d="M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18"/></svg></span>
                <span class="rm-gname">MVS Portal</span>
                <span class="rm-ghint">by group</span>
              </div>
              <button class="run-menu-item por" onclick="runChoice('portal','ondemand')"><span class="rmi-dot"></span><span class="rmi-main">On Demand</span></button>
              <button class="run-menu-item por" onclick="runChoice('portal','stream2')"><span class="rmi-dot"></span><span class="rmi-main">Stream 2</span></button>
              <button class="run-menu-item por" onclick="runChoice('portal','public')"><span class="rmi-dot"></span><span class="rmi-main">Public</span></button>
              <button class="run-menu-item por" onclick="runChoice('portal','all')"><span class="rmi-dot"></span><span class="rmi-main">All Portal</span><span class="rmi-tag">all</span></button>
              <button class="run-menu-item por" onclick="runChoice('portalnew')" style="background:#f0fdf4" title="Check only students newly arrived on the portal — skips already-active data, saves CapSolver credits"><span class="rmi-dot" style="background:#16a34a"></span><span class="rmi-main">New data only</span><span class="rmi-tag" style="background:#dcfce7;color:#15803d">saves credits</span></button>
            </div>
          </div>
        </div>
        <div class="bell-btn" onclick="refreshPage(this)" title="Refresh this page" style="cursor:pointer">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="18" height="18"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
        </div>
        <div class="bell-wrap">
          <div class="bell-btn" onclick="toggleBell(event)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="19" height="19"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
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
        <div id="run-progress" class="progress-banner" style="display:none">
          <div class="pb-top">
            <span class="pb-title"><span class="pb-spin"></span><span id="pb-label">Checking students…</span></span>
            <span class="pb-pct" id="pb-pct">0%</span>
          </div>
          <div class="pb-track"><div class="pb-fill" id="pb-fill" style="width:0%"></div></div>
          <div class="pb-sub" id="pb-sub">0 / 0 done</div>
          <div id="pb-srcwrap" style="margin-top:10px;display:none">
            <div id="pb-mvs-row">
              <div style="display:flex;justify-content:space-between;font-size:13px;font-weight:600;margin-bottom:3px">
                <span style="display:inline-flex;align-items:center;gap:7px"><span style="width:9px;height:9px;border-radius:50%;background:#7C3AED"></span>MVS Portal</span><span id="pb-mvs-txt">0 / 0</span></div>
              <div class="pb-track" style="height:8px"><div class="pb-fill" id="pb-mvs-fill" style="width:0%;background:#7C3AED"></div></div>
            </div>
            <div id="pb-trk-row">
              <div style="display:flex;justify-content:space-between;font-size:13px;font-weight:600;margin:8px 0 3px">
                <span style="display:inline-flex;align-items:center;gap:7px"><span style="width:9px;height:9px;border-radius:50%;background:#0EA5E9"></span>MVS Tracker</span><span id="pb-trk-txt">0 / 0</span></div>
              <div class="pb-track" style="height:8px"><div class="pb-fill" id="pb-trk-fill" style="width:0%;background:#0EA5E9"></div></div>
            </div>
          </div>
        </div>

        <div id="next-runs" class="timer-row"></div>

        <div class="stat-grid" id="stat-grid"></div>
        <div class="card">
          <h3>Session-wise Students</h3>
          <div id="source-counts"></div>
          <div id="session-counts"><div class="empty">Loading...</div></div>
        </div>
        <div class="card">
          <h3>Status Distribution</h3>
          <div id="distribution"><div class="empty">Loading...</div></div>
        </div>
        <div class="card">
          <div class="card-head">
            <h3>Recent Runs</h3>
            <button class="btn-refresh" onclick="loadDashboard()" title="Refresh">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
                Refresh</button>
          </div>
          <style>.recent-runs-scroll{max-height:360px;overflow:auto}.recent-runs-scroll thead th{position:sticky;top:0;background:var(--card);z-index:2}</style>
          <div class="recent-runs-scroll"><table>
            <thead><tr><th>Run At</th><th>Type</th><th>Checked</th><th>Changed</th><th>Failed</th><th>Status</th><th>Action</th></tr></thead>
            <tbody id="recent-runs"></tbody>
          </table></div>
        </div>
      </section>

      <section id="sec-students" class="page-section">
        <div class="card">
          <div class="filter-bar">
            <input type="text" id="s-search" placeholder="Search name / reference / email..." oninput="debounceStudents()">
            <select id="s-status" onchange="loadStudents(1)">
              <option value="">All Statuses</option>
              <option>Pending</option><option>Documents Verification In Progress</option>
              <option>Document Required</option><option>Verified</option>
              <option>Approved</option><option>Rejected</option><option>Fetch Error</option><option>Unknown</option>
            </select>
            <select id="s-session" onchange="loadStudents(1)"><option value="">All Sessions</option></select>
            <select id="s-class" onchange="loadStudents(1)"><option value="">All Classes</option><option value="10">Class 10</option><option value="12">Class 12</option></select>
            <select id="s-source" onchange="loadStudents(1)"><option value="">All Data Types</option><option value="mvs_portal">MVS Portal</option><option value="mvs_tracker">MVS Tracker</option><option value="both">Both (Tracker + Portal)</option></select>
            <select id="s-datepreset" onchange="onDatePreset('s',()=>loadStudents(1))">
              <option value="">All dates</option>
              <option value="today">Today</option>
              <option value="yesterday">Yesterday</option>
              <option value="7d">Last 7 days</option>
              <option value="custom">Custom range…</option>
            </select>
            <button class="btn btn-outline btn-sm" onclick="exportStudents('normal')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Export Excel</button>
          </div>
          <div class="filter-bar" id="s-daterow" style="display:none">
            <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px">From
              <input type="datetime-local" id="s-from"></label>
            <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px">To
              <input type="datetime-local" id="s-to"></label>
            <button class="btn btn-primary btn-sm" onclick="loadStudents(1)">Apply</button>
          </div>
          <div id="s-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div id="sel-bar" style="display:none;align-items:center;gap:12px;flex-wrap:wrap;background:var(--soft);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin-bottom:14px">
            <span style="font-weight:600;font-size:13.5px"><span id="sel-count">0</span> selected <span style="color:var(--muted);font-weight:400">(max 20)</span></span>
            <button class="btn btn-success btn-sm" id="sel-run-btn" onclick="runSelected()">
              <svg viewBox="0 0 24 24" fill="currentColor" width="13" height="13"><polygon points="5 3 19 12 5 21 5 3"/></svg> Run Selected</button>
            <button class="btn btn-sm" style="background:#DC2626;color:#fff" onclick="deleteSelectedStudents()">&#128465; Delete selected</button>
            <select onchange="changeSelectedSource(this)" style="max-width:170px" title="Move selected students to a data type">
              <option value="">Change source&hellip;</option>
              <option value="mvs_tracker">&rarr; MVS Tracker</option>
              <option value="mvs_portal">&rarr; MVS Portal</option>
            </select>
            <button class="btn btn-sm" style="background:transparent;color:var(--muted);border:1px solid var(--border)" onclick="clearSelection()">Clear</button>
            <span style="font-size:12px;color:var(--muted)">Pick the exact students whose status you need right now — saves credits.</span>
          </div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th style="width:34px"><input type="checkbox" id="sel-all" onclick="toggleSelectAll(this)" title="Select all on this page"></th>
              <th>#</th><th>Reference No</th><th>Student Name</th><th>Session</th>
              <th>Status</th><th>Last Checked</th><th>Action</th>
            </tr></thead><tbody id="s-body"></tbody></table>
          </div>
          <div class="pg-bar" id="s-pg"></div>
        </div>
      </section>

      <section id="sec-confirmed" class="page-section">
        <div class="card">
          <h3>Admission Confirmed Students</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            Their admission is confirmed. Documents are sent to WhatsApp <b>automatically in the background</b> —
            you do not need to click Resend. Use the <b>WhatsApp</b> filter below to see who is still pending.</p>
          <div class="filter-bar">
            <input type="text" id="c-search" placeholder="Search name / reference / email..." oninput="debounceConfirmed()">
            <select id="c-status" onchange="loadConfirmed(1)">
              <option value="">All Statuses</option>
              <option>Admission Confirmed</option><option>Admitted</option>
            </select>
            <select id="c-wa" onchange="loadConfirmed(1)">
              <option value="">All WhatsApp</option>
              <option value="delivered">Delivered</option>
              <option value="sent">Sent (pending delivery)</option>
              <option value="notsent">Not sent yet</option>
              <option value="failed">Failed</option>
            </select>
            <select id="c-session" onchange="loadConfirmed(1)"><option value="">All Sessions</option></select>
            <select id="c-class" onchange="loadConfirmed(1)"><option value="">All Classes</option><option value="10">Class 10</option><option value="12">Class 12</option></select>
            <select id="c-source" onchange="loadConfirmed(1)"><option value="">All Data Types</option><option value="mvs_portal">MVS Portal</option><option value="mvs_tracker">MVS Tracker</option><option value="both">Both (Tracker + Portal)</option></select>
            <select id="c-datepreset" onchange="onDatePreset('c',()=>loadConfirmed(1))">
              <option value="">All dates</option>
              <option value="today">Today</option>
              <option value="yesterday">Yesterday</option>
              <option value="7d">Last 7 days</option>
              <option value="custom">Custom range…</option>
            </select>
            <button class="btn btn-primary btn-sm" onclick="autoSendNow(this)" title="Send WhatsApp now to all pending confirmed students">Send all pending now</button>
            <button class="btn btn-outline btn-sm" onclick="exportStudents('confirmed')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Export Excel</button>
          </div>
          <div id="c-bulkbar" style="display:none;align-items:center;gap:12px;flex-wrap:wrap;background:var(--soft);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin-bottom:12px">
            <span style="font-weight:700;font-size:13px"><span id="c-selcount">0</span> selected</span>
            <button class="btn btn-primary btn-sm" onclick="resendSelected(this)">Resend WhatsApp to selected</button>
            <button class="btn btn-sm" style="background:#DC2626;color:#fff" onclick="bulkDeleteConf()">&#128465; Delete selected</button>
            <button class="btn btn-sm" style="background:var(--soft);color:var(--text)" onclick="clearConfSel()">Clear</button>
          </div>
          <div id="wa-progress" style="display:none;background:linear-gradient(135deg,#ECFDF5,#F0FDF4);border:1px solid #BBF7D0;border-radius:12px;padding:14px 16px;margin-bottom:14px">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:9px">
              <span style="font-weight:700;font-size:13.5px;color:#065F46"><span id="wap-title">Sending WhatsApp to pending students</span></span>
              <span style="font-weight:800;font-size:15px;color:#047857"><span id="wap-pct">0</span>%</span>
            </div>
            <div style="height:10px;background:#D1FAE5;border-radius:6px;overflow:hidden">
              <div id="wap-bar" style="height:100%;width:0%;background:linear-gradient(90deg,#10B981,#059669);transition:width .4s"></div>
            </div>
            <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:10px;font-size:12.5px;font-weight:600">
              <span style="color:#047857">Sending: <span id="wap-total">0</span></span>
              <span style="color:#059669">&#10003; Sent this run: <span id="wap-sent">0</span></span>
              <span style="color:#B45309">Remaining: <span id="wap-remaining">0</span></span>
              <span style="color:#B91C1C">Failed (auto-retry): <span id="wap-failed">0</span></span>
            </div>
          </div>
          <div class="filter-bar" id="c-daterow" style="display:none">
            <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px">From
              <input type="datetime-local" id="c-from"></label>
            <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px">To
              <input type="datetime-local" id="c-to"></label>
            <button class="btn btn-primary btn-sm" onclick="loadConfirmed(1)">Apply</button>
          </div>
          <div id="c-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th style="width:34px"><input type="checkbox" id="c-selall" onclick="toggleConfAll(this)" title="Select all on this page"></th>
              <th>#</th><th>Reference No</th><th>Student Name</th><th>Session</th>
              <th>Status</th><th>Downloads</th><th>Confirmed On</th><th>Action</th>
            </tr></thead><tbody id="c-body"></tbody></table>
          </div>
          <div class="pg-bar" id="c-pg"></div>
        </div>
      </section>

      <section id="sec-syc" class="page-section">
        <div class="card">
          <h3>SYC Students</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            SYC students are <b>not status-checked</b>. Their Hall Ticket is fetched directly using
            <b>Enrollment Number + Date of Birth</b>. Click "Download Hall Ticket" — it shows progress and opens the hall ticket in the correct format (then use Save as PDF).</p>
          <div class="filter-bar">
            <input type="text" id="syc-search" placeholder="Search name / enrollment / mobile..." oninput="debounceSyc()">
            <button class="btn btn-outline btn-sm" onclick="loadSyc(1)">Refresh</button>
          </div>
          <div id="syc-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>#</th><th>Enrollment No</th><th>Student Name</th><th>Mobile</th><th>DOB</th><th>Status</th><th>Hall Ticket</th><th>Remove</th>
            </tr></thead><tbody id="syc-body"></tbody></table>
          </div>
          <div class="pg-bar" id="syc-pg"></div>
        </div>
      </section>

      <section id="sec-required" class="page-section">
        <div class="card">
          <h3>Document Required — Action Needed</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            These need to be resolved by the counsellor. The exported / WhatsApp Excel includes
            <b>DOB</b> so you can log in directly. Once resolved, hit <b>Re-check Required</b> to update only these students.</p>
          <div class="filter-bar">
            <input type="text" id="r-search" placeholder="Search name / reference / email..." oninput="debounceRequired()">
            <select id="r-status" onchange="loadRequired(1)">
              <option value="">All Statuses</option>
              <option>Document Required</option>
            </select>
            <select id="r-session" onchange="loadRequired(1)"><option value="">All Sessions</option></select>
            <select id="r-source" onchange="loadRequired(1)"><option value="">All Data Types</option><option value="mvs_portal">MVS Portal</option><option value="mvs_tracker">MVS Tracker</option><option value="both">Both (Tracker + Portal)</option></select>
            <button class="btn btn-primary btn-sm" onclick="runRequired(this)">
              <svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14"><polygon points="5 3 19 12 5 21 5 3"/></svg>
              Re-check Required</button>
            <button class="btn btn-outline btn-sm" onclick="exportStudents('required')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Export Excel</button>
          </div>
          <div id="r-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div id="r-bulkbar" style="display:none;align-items:center;gap:12px;flex-wrap:wrap;background:#FEF2F2;border:1px solid #FECACA;border-radius:10px;padding:10px 14px;margin-bottom:12px"><span style="font-weight:700;font-size:13px"><span id="r-selcount">0</span> selected</span><button class="btn btn-sm" style="background:#DC2626;color:#fff" onclick="bulkDelete('r')">&#128465; Delete selected</button><button class="btn btn-sm" style="background:var(--soft);color:var(--text)" onclick="selClear('r')">Clear</button></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th style="width:34px"><input type="checkbox" id="r-selall" onclick="selAll('r',this)" title="Select all on this page"></th><th>#</th><th>Reference No</th><th>Student Name</th><th>Session</th><th>RC Comment / Remark</th><th>Action</th>
            </tr></thead><tbody id="r-body"></tbody></table>
          </div>
          <div class="pg-bar" id="r-pg"></div>
        </div>
      </section>

      <section id="sec-docreq" class="page-section">
        <div class="card">
          <h3>&#128172; Document Requests &mdash; Review &amp; Send on WhatsApp</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:14px;line-height:1.6">
            For every <b>Document Required</b> student, we read the NIOS remark and prepare a <b>simple, friendly message</b>
            (in your words, not NIOS's technical language). <b>Review each one, edit if needed, attach a demo screenshot if helpful, then send.</b> Nothing goes
            out automatically. Routing is automatic: <b>Public &#8594; public number</b>, <b>On Demand &amp; Stream 2 &#8594; main number</b>.</p>
          <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px">
            <button class="btn btn-outline btn-sm" onclick="loadDocReq(this)">&#8635; Refresh</button>
            <button class="btn btn-sm" style="background:#16a34a;color:#fff" onclick="sendDocReq('selected',this)">Send selected</button>
            <button class="btn btn-sm" style="background:#2563eb;color:#fff" onclick="sendDocReq('all',this)">Send all pending</button>
            <label style="display:flex;align-items:center;gap:7px;font-size:13px;cursor:pointer;margin-left:4px"><input type="checkbox" id="dr-selall" onclick="toggleDrAll(this)"> Select all</label>
            <span id="dr-count" style="font-size:13px;color:var(--muted);margin-left:auto"></span>
          </div>
          <div id="dr-warn" style="display:none;background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:10px 13px;font-size:12.5px;color:#9a3412;margin-bottom:12px"></div>
          <div id="dr-body"></div>
          <div class="pg-bar" id="dr-pg"></div>
        </div>
      </section>

      <section id="sec-failed" class="page-section">
        <div class="card">
          <h3>Failed to Run — Wrong Data (NIOS login failed)</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            These students' NIOS portal could not be opened with the uploaded details (wrong DOB / Reference / Enrollment No),
            so <b>no WhatsApp/document was sent</b> (the link would open to an error and panic the student).
            Click <b>Edit &amp; fix</b>, correct the detail, then <b>Save &amp; Run again</b> — only that one student re-runs.
            If the data is correct it leaves this list and moves to its normal category.<br>
            <span style="color:var(--success);font-weight:600">Auto-fix:</span> every run now auto-retries transient errors and auto-tries a DOB date/month swap.
            Use <b>Re-check all (auto-fix)</b> below to apply this to the current list in one go.</p>
          <div class="filter-bar">
            <input type="text" id="f-search" placeholder="Search name / reference / email..." oninput="debounceFailed()">
            <select id="f-source" onchange="loadFailed(1)"><option value="">All Data Types</option><option value="mvs_portal">MVS Portal</option><option value="mvs_tracker">MVS Tracker</option><option value="both">Both (Tracker + Portal)</option></select>
            <button class="btn btn-outline btn-sm" onclick="loadFailed(1)">Refresh</button>
            <button class="btn btn-success btn-sm" id="f-runall-btn" onclick="runAllFailed(this)" title="Re-run every failed student with auto-retry + DOB date/month auto-swap">&#8635; Re-check all (auto-fix)</button>
          </div>
          <div id="f-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div id="f-bulkbar" style="display:none;align-items:center;gap:12px;flex-wrap:wrap;background:#FEF2F2;border:1px solid #FECACA;border-radius:10px;padding:10px 14px;margin-bottom:12px"><span style="font-weight:700;font-size:13px"><span id="f-selcount">0</span> selected</span><button class="btn btn-sm" style="background:#DC2626;color:#fff" onclick="bulkDelete('f')">&#128465; Delete selected</button><button class="btn btn-sm" style="background:var(--soft);color:var(--text)" onclick="selClear('f')">Clear</button></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th style="width:34px"><input type="checkbox" id="f-selall" onclick="selAll('f',this)" title="Select all on this page"></th><th>Student</th><th>Reference / Enroll</th><th>Session</th><th>Problem</th><th>Action</th>
            </tr></thead><tbody id="f-body"></tbody></table>
          </div>
          <div class="pg-bar" id="f-pg"></div>
        </div>
      </section>

      <section id="sec-unknown" class="page-section">
        <div class="card">
          <div class="card-head">
            <h3>Unknown Status</h3>
            <button class="btn btn-success btn-sm" id="u-runall-btn" onclick="runAllUnknown(this)" title="Re-check only the Unknown students (with auto-retry)">&#8635; Run Unknown only</button>
          </div>
          <p style="color:var(--muted);font-size:13px;margin-bottom:14px">
            NIOS returned <b>no recognisable status</b> for these students — usually a wrong or not-yet-active Reference No,
            or a status NIOS shows that isn't tracked yet. The <b>reason</b> is shown for each. Fix the reference if needed, or
            click <b>Run Unknown only</b> to re-check just these (errors often clear on a retry).</p>
          <div class="filter-bar">
            <input type="text" id="u-search" placeholder="Search name / reference / email..." oninput="debounceUnknown()">
            <select id="u-session" onchange="loadUnknown(1)"><option value="">All Sessions</option></select>
            <button class="btn btn-outline btn-sm" onclick="loadUnknown(1)">Refresh</button>
          </div>
          <div id="u-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>Student</th><th>Reference / Enroll</th><th>Session</th><th>Why Unknown</th><th>Last Checked</th><th>Action</th>
            </tr></thead><tbody id="u-body"></tbody></table>
          </div>
          <div class="pg-bar" id="u-pg"></div>
        </div>
      </section>

      <section id="sec-history" class="page-section">
        <div class="card">
          <h3>Status Change History</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:14px">
            Choose a date &amp; time range — e.g. use "Custom range" to view confirmations from yesterday 6:30 PM until now.</p>
          <div class="filter-bar">
            <select id="h-preset" onchange="onHistPreset()">
              <option value="all">All time</option>
              <option value="today">Today</option>
              <option value="yesterday">Yesterday</option>
              <option value="24h">Last 24 hours</option>
              <option value="custom">Custom range…</option>
            </select>
            <select id="h-status" onchange="loadHistory(1)">
              <option value="">All status changes</option>
              <option>Admission Confirmed</option><option>Verified</option>
              <option>Documents Verification In Progress</option><option>Document Required</option>
              <option>Approved</option><option>Rejected</option>
              <option>Unknown</option><option>Fetch Error</option>
            </select>
            <select id="h-source" onchange="loadHistory(1)"><option value="">All Data Types</option><option value="mvs_portal">MVS Portal</option><option value="mvs_tracker">MVS Tracker</option><option value="both">Both (Tracker + Portal)</option></select>
            <input id="h-search" type="text" placeholder="Search name / reference / email…" oninput="histSearchDebounced()"
              style="padding:9px 12px;border:2px solid var(--border);border-radius:9px;font-size:13.5px;min-width:230px">
            <button class="btn btn-outline btn-sm" onclick="exportHistory()">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Export Excel</button>
            <button class="btn btn-sm" style="background:#fee2e2;color:#b91c1c;border:1px solid #fecaca" onclick="clearHistory()">&#128465; Clear All</button>
          </div>
          <div class="filter-bar" id="h-custom" style="display:none">
            <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px">From
              <input type="datetime-local" id="h-from"></label>
            <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px">To
              <input type="datetime-local" id="h-to"></label>
            <button class="btn btn-primary btn-sm" onclick="loadHistory(1)">Apply</button>
          </div>
          <div id="h-count" style="font-size:13px;color:var(--muted);margin-bottom:12px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>Reference No</th><th>Student</th><th>Old Status</th><th>New Status</th><th>Changed At</th><th>Action</th>
            </tr></thead><tbody id="h-body"></tbody></table>
          </div>
          <div class="pg-bar" id="h-pg"></div>
        </div>
      </section>

      <section id="sec-runlogs" class="page-section">
        <div class="card">
          <div class="card-head">
            <h3>Run Logs</h3>
            <div style="display:flex;gap:8px;align-items:center">
              <select id="rl-limit" onchange="loadRunLogs()" style="padding:6px 10px;border:2px solid var(--border);border-radius:8px;font-size:13px">
                <option value="10">10</option><option value="20">20</option><option value="30">30</option>
                <option value="50" selected>50</option><option value="100">100</option></select>
              <button class="btn btn-sm" style="background:#fee2e2;color:#b91c1c" onclick="clearRunLogs()">Clear All</button>
            </div>
          </div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>Run At</th><th>Type</th><th>Checked</th><th>Changed</th><th>Failed</th><th>Status</th><th>Action</th>
            </tr></thead><tbody id="rl-body"></tbody></table>
          </div>
        </div>
      </section>

      <section id="sec-transfers" class="page-section">
        <div class="card" style="border:1px solid #fde68a;background:#fffdf5">
          <div class="card-head">
            <h3>&#128465; Old / Removed Portal Students</h3>
            <button class="btn btn-outline btn-sm" id="stale-find-btn" onclick="findStalePortal(this)">Find old portal data</button>
          </div>
          <p style="color:var(--muted);font-size:13px;margin-bottom:6px">
            Finds students we still track as <b>MVS Portal</b> that are <b>no longer on the live portal</b> — e.g. an old batch
            you removed from the portal. It only <b>previews</b> them; nothing is deleted until you click <b>Move to Trash</b>
            (recoverable from Settings &#8594; Trash any time).</p>
          <div id="stale-box" style="margin-top:10px"></div>
        </div>
        <div class="card">
          <div class="card-head">
            <h3>Transfer Data — Tracker &#8594; Portal</h3>
            <button class="btn btn-success btn-sm" id="tr-sync-btn" onclick="syncTransfers(this)" title="Push the current status of every matched (Both) student to MVS Portal now">&#8635; Sync matched to Portal</button>
          </div>
          <p style="color:var(--muted);font-size:13px;margin-bottom:14px">
            Every student that existed in <b>MVS Tracker</b> and was then matched to <b>MVS Portal</b> appears here — it now lives in
            <b>Both</b> and is run/managed as Portal, with its status pushed to the portal. Shows the status it had before (Old)
            vs after the Portal check (New), so you can see exactly which &amp; how many records moved.</p>
          <div class="filter-bar">
            <input id="tr-search" type="text" placeholder="Master search — name / reference / enrollment / mobile…" oninput="trSearchDebounced()"
              style="padding:9px 12px;border:2px solid var(--border);border-radius:9px;font-size:13.5px;min-width:280px">
            <select id="tr-mode" onchange="loadTransfers(1)">
              <option value="">All transfers</option>
              <option value="auto">Automatic (during runs)</option>
              <option value="manual">Manual (Sync button)</option>
            </select>
            <button class="btn btn-outline btn-sm" onclick="loadTransfers(1)">Refresh</button>
          </div>
          <div id="tr-count" style="font-size:13px;color:var(--muted);margin-bottom:12px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>Student</th><th>Reference / Enroll</th><th>Session</th><th>Old Status</th><th>New Status</th><th>Transferred At</th><th>Mode</th>
            </tr></thead><tbody id="tr-body"></tbody></table>
          </div>
          <div class="pg-bar" id="tr-pg"></div>
        </div>
      </section>

      <section id="sec-upload" class="page-section">
        <div class="card">
          <h3>Upload Student Excel</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:14px">
            Upload an Excel file (.xlsx). Columns: Student Name, Mobile No, Class, Reference Number, Enrol No, Email, Date of Birth, Admission Session.</p>
          <div style="background:var(--soft);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:18px">
            <div style="font-size:13px;font-weight:600;margin-bottom:4px">&#128196; Not sure about the format? Download a sample sheet:</div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:10px">Fill your data in the same column order, then upload.</div>
            <div style="display:flex;gap:10px;flex-wrap:wrap">
              <button class="btn btn-outline btn-sm" onclick="downloadSample('regular')">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14" style="vertical-align:-2px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                Sample — Regular (On Demand / Stream 2 / April-October)</button>
              <button class="btn btn-outline btn-sm" onclick="downloadSample('syc')">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14" style="vertical-align:-2px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                Sample — SYC Students</button>
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px">
            <span style="font-size:13px;font-weight:600">Data source for this sheet:</span>
            <select id="upload-source" onchange="saveUploadSource()" style="max-width:260px">
              <option value="">Auto-detect (recommended)</option>
              <option value="mvs_portal">MVS Portal</option>
              <option value="mvs_tracker">MVS Tracker</option>
            </select>
            <span style="font-size:11px;color:var(--muted)">Applied on the next Run Now</span>
          </div>
          <div class="drop" id="drop" onclick="document.getElementById('file-input').click()">
            <div class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg></div>
            <div style="font-weight:600;font-size:15px">Click or drag Excel file here</div>
            <div style="color:var(--muted);font-size:13px;margin-top:5px">.xlsx files only</div>
          </div>
          <input type="file" id="file-input" accept=".xlsx,.xls" style="display:none" onchange="handleFile(this.files[0])">
          <div id="upload-status" style="margin-top:16px"></div>
          <div id="upload-summary" style="margin-top:16px"></div>
          <div id="upload-preview" style="margin-top:16px"></div>
          <div style="margin-top:18px;display:flex;gap:12px">
            <button class="btn btn-outline btn-sm" onclick="downloadExcel()">Download Updated Excel</button>
          </div>
        </div>

        <div class="card">
          <h3>Bulk Alternate Numbers</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:14px">
            Forgot to add alternate WhatsApp numbers in your sheet? Upload a small sheet here with just two
            columns — <b>Reference No</b> and <b>Alternate Number</b> — to add/update them in bulk. Students are
            matched by reference number. For anyone already <b>confirmed</b>, the documents are sent to the new
            alternate number automatically (the primary number is not messaged again).</p>
          <div style="background:var(--soft);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:18px">
            <div style="font-size:13px;font-weight:600;margin-bottom:4px">&#128196; Need the format? Download a sample:</div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:10px">Two columns: Reference No, Alternate Number. Fill and upload.</div>
            <button class="btn btn-outline btn-sm" onclick="downloadAltSample()">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14" style="vertical-align:-2px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Sample — Alternate Numbers</button>
          </div>
          <div class="drop" id="alt-drop" onclick="document.getElementById('alt-file-input').click()">
            <div class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg></div>
            <div style="font-weight:600;font-size:15px">Click or drag the alternate-numbers sheet here</div>
            <div style="color:var(--muted);font-size:13px;margin-top:5px">.xlsx — Reference No + Alternate Number</div>
          </div>
          <input type="file" id="alt-file-input" accept=".xlsx,.xls" style="display:none" onchange="handleAltFile(this.files[0])">
          <div id="alt-status" style="margin-top:16px"></div>
        </div>
      </section>

      <section id="sec-settings" class="page-section">
        <div class="card">
          <div class="set-head">
            <span class="set-ico" style="background:linear-gradient(135deg,#4F46E5,#7C3AED)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg></span>
            <div class="set-tt"><h3>Recheck Intervals</h3>
            <div class="set-sub">Set a separate auto-run interval for each group. Confirmed students are skipped automatically.</div></div>
          </div>
          <div class="iv-row">
            <span class="iv-lbl"><span class="iv-dot" style="background:#0EA5E9"></span>On Demand</span>
            <input type="number" id="iv-ondemand" min="1" max="43200" value="6">
            <select id="iv-ondemand-unit"><option value="hours">hours</option><option value="minutes">minutes</option><option value="days">days</option></select>
            <select id="iv-ondemand-src" title="Which data this group auto-runs"><option value="both">Both sources</option><option value="mvs_tracker">Tracker only</option><option value="mvs_portal">Portal only</option></select>
          </div>
          <div class="iv-row">
            <span class="iv-lbl"><span class="iv-dot" style="background:#F59E0B"></span>Stream 2</span>
            <input type="number" id="iv-stream2" min="1" max="43200" value="6">
            <select id="iv-stream2-unit"><option value="hours">hours</option><option value="minutes">minutes</option><option value="days">days</option></select>
            <select id="iv-stream2-src" title="Which data this group auto-runs"><option value="both">Both sources</option><option value="mvs_tracker">Tracker only</option><option value="mvs_portal">Portal only</option></select>
          </div>
          <div class="iv-row">
            <span class="iv-lbl"><span class="iv-dot" style="background:#7C3AED"></span>Public Exam <span style="font-weight:400;color:var(--muted);font-size:12px">(April / Oct)</span></span>
            <input type="number" id="iv-public" min="1" max="43200" value="12">
            <select id="iv-public-unit"><option value="hours">hours</option><option value="minutes">minutes</option><option value="days">days</option></select>
            <select id="iv-public-src" title="Which data this group auto-runs"><option value="both">Both sources</option><option value="mvs_tracker">Tracker only</option><option value="mvs_portal">Portal only</option></select>
          </div>
          <div class="iv-row" style="background:#f0fdf4;border-radius:9px;padding:8px 10px;margin-top:4px">
            <span class="iv-lbl"><span class="iv-dot" style="background:#16A34A"></span>MVS Portal — New data <span style="font-weight:400;color:var(--muted);font-size:12px">(saves credits)</span></span>
            <input type="number" id="iv-portalnew" min="1" max="43200" value="3">
            <select id="iv-portalnew-unit"><option value="hours">hours</option><option value="minutes">minutes</option><option value="days">days</option></select>
            <span style="font-size:11.5px;color:var(--muted);align-self:center">Checks only newly-arrived portal students</span>
          </div>
          <div class="set-foot">
            <button class="btn btn-primary btn-sm" onclick="saveIntervals()">Save Intervals</button>
            <div class="set-note">Range: 15 minutes to 30 days (use the <b>days</b> unit to run every few days). Shorter intervals use more CapSolver credits.</div>
            <div id="iv-status" style="margin-top:10px;font-size:13px"></div>
          </div>
        </div>
        <div class="card">
          <div class="set-head">
            <span class="set-ico" style="background:linear-gradient(135deg,#10B981,#059669)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></span>
            <div class="set-tt"><h3>Run Report on WhatsApp</h3>
            <div class="set-sub">After every run, get a WhatsApp summary (Confirmed, Document Required, Error and unchanged counts) plus a link to download the full Excel. Sent to up to 10 numbers.</div></div>
          </div>
          <label style="display:flex;align-items:center;gap:10px;font-size:14px;font-weight:600;margin-bottom:16px;cursor:pointer">
            <input type="checkbox" id="rep-enabled" style="width:18px;height:18px"> Send report after each run
          </label>
          <label style="display:block;font-size:13px;font-weight:600;margin-bottom:6px">WhatsApp numbers <span style="font-weight:400;color:var(--muted)">(one per line or comma-separated)</span></label>
          <textarea id="rep-numbers" rows="3" placeholder="9876543210&#10;9123456789&#10;9988776655"
            style="width:100%;padding:11px 13px;border:2px solid var(--border);border-radius:10px;font-size:14px;font-family:inherit;resize:vertical;background:var(--card);color:var(--text)"></textarea>
          <div class="set-note" style="margin-bottom:16px">10-digit Indian numbers (country code 91 added automatically). Report template must be set up in AiSensy (campaign <b>AISENSY_CAMPAIGN_REPORT</b>).</div>
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <button class="btn btn-primary btn-sm" onclick="saveReportSettings()">Save</button>
            <button class="btn btn-primary btn-sm" style="background:#16A34A;border-color:#16A34A" onclick="sendLatestReport(this)">Send a run report to all</button>
            <button class="btn btn-outline btn-sm" onclick="testReport(this)">Send test report now</button>
          </div>
          <div id="rep-last" style="margin-top:12px;font-size:12.5px;color:var(--muted)"></div>
          <div id="rep-status" style="margin-top:8px;font-size:13px"></div>
        </div>
        <div class="card">
          <div class="set-head">
            <span class="set-ico" style="background:linear-gradient(135deg,#0EA5E9,#0369A1)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg></span>
            <div class="set-tt"><h3>Change Data Source (MVS Portal &harr; MVS Tracker)</h3>
            <div class="set-sub">Wrongly tagged on upload (e.g. auto-detect put a Tracker sheet under MVS Portal)? Move students from one data type to the other. <b>No data is lost</b> — only the tag changes.</div></div>
            <div class="set-act"><button class="btn btn-sm" style="background:var(--soft);color:var(--text)" onclick="loadSourceCounts()">Refresh counts</button></div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">
            <span style="font-size:13px">Move&nbsp;all from</span>
            <select id="src-from" style="max-width:170px"><option value="mvs_portal">MVS Portal</option><option value="mvs_tracker">MVS Tracker</option></select>
            <span style="font-size:13px">&rarr; to</span>
            <select id="src-to" style="max-width:170px"><option value="mvs_tracker">MVS Tracker</option><option value="mvs_portal">MVS Portal</option></select>
            <button class="btn btn-primary btn-sm" onclick="moveAllSource(this)">Move all</button>
          </div>
          <div id="src-counts" style="font-size:12.5px;color:var(--muted)">Current: MVS Portal — … &middot; MVS Tracker — …</div>
          <div id="src-status" style="margin-top:8px;font-size:13px"></div>
          <div style="font-size:11.5px;color:var(--muted);margin-top:8px;line-height:1.5">Tip: to move only <b>some</b> students, go to the <b>Students</b> tab, filter by Data Type, tick the ones you want, then use <b>Change source</b> in the select bar.</div>
        </div>
        <div class="card">
          <div class="set-head">
            <span class="set-ico" style="background:linear-gradient(135deg,#EF4444,#DC2626)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></span>
            <div class="set-tt"><h3>Deleted Students (Trash)</h3>
            <div class="set-sub">Removed students land here. <b>Restore</b> brings them back; <b>Delete permanently</b> cannot be undone.</div></div>
            <div class="set-act"><button class="btn btn-sm" style="background:var(--soft);color:var(--text)" onclick="loadTrash()">Refresh</button></div>
          </div>
          <div id="trash-bulkbar" style="display:none;align-items:center;gap:10px;flex-wrap:wrap;background:#FEF2F2;border:1px solid #FECACA;border-radius:10px;padding:10px 14px;margin-bottom:12px">
            <span style="font-weight:700;font-size:13px"><span id="trash-selcount">0</span> selected</span>
            <button class="btn btn-sm" style="background:#16A34A;color:#fff" onclick="restoreSelected()">&#8617; Restore selected</button>
            <button class="btn btn-sm" style="background:#DC2626;color:#fff" onclick="purgeSelected()">&#128465; Permanently delete selected</button>
            <button class="btn btn-sm" style="background:var(--soft);color:var(--text)" onclick="selClear('trash')">Clear</button>
          </div>
          <div style="overflow-x:auto"><table>
            <thead><tr><th style="width:34px"><input type="checkbox" id="trash-selall" onclick="selAll('trash',this)" title="Select all"></th><th>Student</th><th>Reference / Enroll</th><th>Session</th><th>Deleted At</th><th>Action</th></tr></thead>
            <tbody id="trash-body"><tr><td colspan="6" class="empty">Loading…</td></tr></tbody>
          </table></div>
        </div>
        <div class="card">
          <div class="set-head">
            <span class="set-ico" style="background:linear-gradient(135deg,#22C55E,#16A34A)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg></span>
            <div class="set-tt"><h3>WhatsApp Auto-Send</h3>
            <div class="set-sub">When a student becomes <b>Admission Confirmed</b>, their documents are auto-sent to their WhatsApp as secure links (each student receives this <b>only once</b>).</div></div>
          </div>
          <div id="wa-config" style="font-size:13px;margin-bottom:14px;color:var(--muted)">Loading…</div>
          <label style="display:flex;align-items:center;gap:10px;cursor:pointer;margin-bottom:18px">
            <input type="checkbox" id="wa-enabled" onchange="saveWa()" style="width:18px;height:18px;cursor:pointer">
            <span style="font-weight:600">Enable auto-send</span>
          </label>
          <label style="display:flex;align-items:center;gap:10px;cursor:pointer;margin-bottom:10px">
            <input type="checkbox" id="wa-required-enabled" onchange="saveWa()" style="width:18px;height:18px;cursor:pointer">
            <span style="font-weight:600">Enable <b>Document Requests</b> (counsellor-reviewed reminders)</span>
          </label>
          <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:11px 13px;font-size:12.5px;color:#166534;line-height:1.6;margin-bottom:18px">
            When ON, every <b>Document Required</b> student appears on the <b>Doc Requests</b> page with a simple, friendly
            message prepared from the NIOS remark. <b>The counsellor reviews/edits and sends manually</b> &mdash; nothing goes
            out automatically. Routing: <b>Public &#8594; public number</b>; <b>On Demand &amp; Stream 2 &#8594; main number</b>.<br>
            <span style="color:#15803d">Needs just <b>2 image-header templates</b> ({{1}} = name, {{2}} = document request) &mdash; one per account &mdash; with campaigns in Railway:
            <b>AISENSY_CAMPAIGN_REQUIRED</b> (main account) and <b>AISENSY_CAMPAIGN_REQUIRED_PUBLIC</b> (public account). Every message carries an image: the attached screenshot, or a default MVS banner when none.</span>
          </div>
          <div style="border-top:1px solid var(--border);padding-top:16px">
            <p style="color:var(--muted);font-size:13px;margin-bottom:10px">
              <b>Send a test message</b> — choose a template, enter your number, and a sample message will be sent:</p>
            <div style="display:flex;gap:10px;flex-wrap:wrap">
              <select id="wa-group" style="padding:11px;border:2px solid var(--border);border-radius:10px;font-size:14px">
                <option value="ondemand">On Demand (3inone)</option>
                <option value="stream2">Stream 2 (str2toc1)</option>
                <option value="public">Public April/October</option>
                <option value="syc">SYC (Hall Ticket)</option>
              </select>
              <input type="text" id="wa-num" placeholder="WhatsApp number (e.g. 7065187637)"
                style="flex:1;min-width:180px;padding:11px;border:2px solid var(--border);border-radius:10px;font-size:14px">
              <button class="btn btn-primary btn-sm" onclick="testWa()">Send Test</button>
            </div>
          </div>
          <div id="wa-status" style="margin-top:12px;font-size:13px"></div>
          <div style="border-top:1px solid var(--border);margin-top:16px;padding-top:16px">
            <div style="font-weight:700;font-size:13.5px;margin-bottom:6px">Delivery confirmation (recommended)</div>
            <p style="color:var(--muted);font-size:12.5px;line-height:1.6;margin-bottom:10px">
              AiSensy accepting a message does <b>not</b> guarantee the student received it. Paste this
              <b>Webhook URL</b> into AiSensy (Manage → API/Webhooks → Delivery/Status webhook). Then the
              Confirmed list shows a real <b>Delivered &#10003;</b> or <b>Delivery FAILED</b> per student.</p>
            <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
              <input type="text" id="wh-url" readonly style="flex:1;min-width:220px;padding:10px 12px;border:2px solid var(--border);border-radius:10px;font-size:12.5px;background:var(--soft);color:var(--text)">
              <button class="btn btn-outline btn-sm" onclick="copyWebhook(this)">Copy</button>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="set-head">
            <span class="set-ico" style="background:linear-gradient(135deg,#F59E0B,#D97706)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="13.5" cy="6.5" r="2.5"/><circle cx="17.5" cy="10.5" r="2.5"/><circle cx="8.5" cy="7.5" r="2.5"/><circle cx="6.5" cy="12.5" r="2.5"/><path d="M12 2a10 10 0 1 0 0 20 1.5 1.5 0 0 0 1-2.6 1.5 1.5 0 0 1 1-2.6h2A5.6 5.6 0 0 0 22 11 10 10 0 0 0 12 2z"/></svg></span>
            <div class="set-tt"><h3>Status Colour Legend</h3>
            <div class="set-sub">What each status colour means across the portal.</div></div>
          </div>
          <div class="legend-grid">
            <div class="legend-item b-pending"><div class="nm">Pending</div><div class="ds">Awaiting review</div></div>
            <div class="legend-item b-docs"><div class="nm">Documents Verification In Progress</div><div class="ds">Under review</div></div>
            <div class="legend-item b-required"><div class="nm">Document Required</div><div class="ds">Action needed by counsellor</div></div>
            <div class="legend-item b-verified"><div class="nm">Verified</div><div class="ds">Documents verified</div></div>
            <div class="legend-item b-approved"><div class="nm">Approved</div><div class="ds">Application approved</div></div>
            <div class="legend-item b-confirmed"><div class="nm">Admission Confirmed</div><div class="ds">Admission confirmed</div></div>
            <div class="legend-item b-rejected"><div class="nm">Rejected</div><div class="ds">Application rejected</div></div>
            <div class="legend-item b-syc"><div class="nm">SYC</div><div class="ds">SYC student — hall ticket ready</div></div>
          </div>
        </div>
        <div class="card">
          <div class="set-head">
            <span class="set-ico" style="background:linear-gradient(135deg,#64748B,#475569)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg></span>
            <div class="set-tt"><h3>Test Login &amp; Find Download Links</h3>
            <div class="set-sub">Enter a <b>confirmed student's</b> Reference No + DOB to log in and locate their document download links (for verification).</div></div>
          </div>
          <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">
            <input type="text" id="dbg-ref" placeholder="Reference No (e.g. D0026300046)"
              style="flex:1;min-width:180px;padding:11px;border:2px solid var(--border);border-radius:10px;font-size:14px">
            <input type="text" id="dbg-dob" placeholder="DOB DD-MM-YYYY (e.g. 08-08-2007)"
              style="flex:1;min-width:180px;padding:11px;border:2px solid var(--border);border-radius:10px;font-size:14px">
          </div>
          <button class="btn btn-primary btn-sm" onclick="testLogin()">Test Login & Find Links</button>
          <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
            <p style="color:var(--muted);font-size:13px;margin-bottom:10px">
              <b>Inspect document page</b> — run this if a download fails (to see how the PDF is embedded):</p>
            <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
              <select id="dbg-kind" style="padding:10px;border:2px solid var(--border);border-radius:10px;font-size:14px">
                <option value="id_card">ID Card</option>
                <option value="app_form">Application Form</option>
                <option value="hall_ticket">Hall Ticket</option>
              </select>
              <button class="btn btn-outline btn-sm" onclick="inspectDoc()">Inspect Doc Page</button>
              <button class="btn btn-outline btn-sm" onclick="findAddr()">Find RC Address (Stream 2)</button>
            </div>
          </div>
          <div id="dbg-status" style="margin-top:12px;font-size:13px"></div>
          <pre id="dbg-result" style="margin-top:12px;background:#0F172A;color:#A5F3FC;padding:16px;
            border-radius:10px;font-size:12px;overflow-x:auto;max-height:400px;display:none;white-space:pre-wrap"></pre>
        </div>
        <div class="card" style="border:1px solid var(--danger)">
          <h3 style="color:var(--danger)">&#9888; Danger Zone — Clear All Data</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            This will <b>permanently delete</b> all students, statuses, change history and the uploaded sheet.
            Your settings (intervals, WhatsApp) will be kept. You will need to upload a new sheet afterwards.</p>
          <button class="btn btn-sm" style="background:var(--danger);color:#fff" onclick="resetData()">Clear All Data</button>
          <div id="reset-status" style="margin-top:12px;font-size:13px"></div>
        </div>
      </section>
    </div>
  </div>
</div>

<div id="toast"></div>

<div id="edit-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;align-items:center;justify-content:center;padding:16px">
  <div style="background:var(--card,#fff);border-radius:16px;max-width:480px;width:100%;max-height:90vh;overflow:auto;box-shadow:0 20px 60px rgba(0,0,0,.3)">
    <div style="padding:18px 22px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
      <h3 style="margin:0">&#9998; Edit Student</h3>
      <button onclick="closeEdit()" style="background:none;border:none;font-size:22px;cursor:pointer;color:var(--muted)">&times;</button>
    </div>
    <div style="padding:20px 22px">
      <div id="edit-warn" style="display:none;font-size:12.5px;font-weight:600;color:#b91c1c;background:#fee2e2;border:1px solid #fecaca;border-radius:8px;padding:8px 10px;margin-bottom:14px"></div>
      <input type="hidden" id="edit-rowkey">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <label style="font-size:12px;font-weight:600;grid-column:1/3">Student Name<input id="edit-student_name" class="edit-inp"></label>
        <label style="font-size:12px;font-weight:600">Reference No<input id="edit-reference_no" class="edit-inp"></label>
        <label style="font-size:12px;font-weight:600">Enrollment No<input id="edit-enrollment_no" class="edit-inp"></label>
        <label style="font-size:12px;font-weight:600">Date of Birth<input id="edit-dob" class="edit-inp" placeholder="DD-MM-YYYY"></label>
        <label style="font-size:12px;font-weight:600">Mobile No<input id="edit-mobile" class="edit-inp"></label>
        <label style="font-size:12px;font-weight:600">Alternate No <span style="color:var(--muted);font-weight:400">(optional — WhatsApp goes to both)</span><input id="edit-alt_mobile" class="edit-inp" placeholder="2nd number (optional)"></label>
        <label style="font-size:12px;font-weight:600;grid-column:1/3">Email<input id="edit-email" class="edit-inp"></label>
        <label style="font-size:12px;font-weight:600">Class<input id="edit-class_level" class="edit-inp"></label>
        <label style="font-size:12px;font-weight:600">Session<input id="edit-session" class="edit-inp"></label>
      </div>
      <div style="font-size:11.5px;color:var(--muted);margin-top:10px">Tip: if NIOS login failed, fix the <b>Date of Birth</b> or <b>Reference/Enrollment No</b>, then "Save &amp; Run again" to re-check and send.</div>
      <div id="edit-status" style="font-size:12.5px;font-weight:600;margin-top:10px"></div>
    </div>
    <div style="padding:14px 22px;border-top:1px solid var(--border);display:flex;gap:10px;justify-content:flex-end">
      <button class="btn" style="background:var(--soft);color:var(--text)" onclick="closeEdit()">Cancel</button>
      <button class="btn" style="background:#64748b;color:#fff" onclick="saveEdit(false)">Save</button>
      <button class="btn btn-primary" onclick="saveEdit(true)">&#10227; Save &amp; Run again</button>
    </div>
  </div>
</div>
<style>.edit-inp{width:100%;margin-top:4px;padding:9px 11px;border:2px solid var(--border);border-radius:9px;font-size:14px;font-weight:400}
#run-menu{display:none;position:absolute;right:0;top:calc(100% + 10px);background:var(--card,#fff);border:1px solid var(--border);border-radius:16px;box-shadow:0 20px 50px rgba(15,23,42,.20),0 3px 10px rgba(15,23,42,.07);min-width:280px;z-index:900;overflow:hidden;padding:8px}
.rm-head{display:flex;align-items:center;gap:7px;padding:7px 10px 9px;font-size:10.5px;font-weight:800;letter-spacing:.7px;color:var(--muted);text-transform:uppercase}
.rm-head svg{color:#F59E0B}
.run-menu-item{display:flex;align-items:center;width:100%;text-align:left;padding:9px 11px;border:none;background:none;cursor:pointer;border-radius:10px;transition:background .14s,transform .12s,box-shadow .14s}
.run-menu-item .rmi-main{font-size:13.5px;font-weight:600;color:var(--text)}
.run-menu-item .rmi-dot{width:7px;height:7px;border-radius:50%;margin:0 11px 0 5px;flex-shrink:0;background:var(--muted);transition:transform .14s}
.run-menu-item .rmi-tag{margin-left:auto;font-size:9.5px;font-weight:800;color:#fff;padding:2px 8px;border-radius:20px;text-transform:uppercase;letter-spacing:.4px}

/* Run-all hero */
.rmi-all{margin-bottom:8px;padding:11px;background:linear-gradient(135deg,rgba(16,185,129,.12),rgba(16,185,129,.04));border:1px solid rgba(16,185,129,.25)}
.rmi-all:hover{background:linear-gradient(135deg,rgba(16,185,129,.20),rgba(16,185,129,.09));box-shadow:0 4px 14px rgba(16,185,129,.18)}
.rmi-all .rmi-ic{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:9px;background:linear-gradient(135deg,#10B981,#059669);color:#fff;margin-right:12px;flex-shrink:0;box-shadow:0 3px 8px rgba(16,185,129,.35)}
.rmi-all .rmi-txt{display:flex;flex-direction:column;line-height:1.3}
.rmi-all .rmi-main{font-size:14px;font-weight:800}
.rmi-all .rmi-sub{font-size:11px;font-weight:500;color:var(--muted)}
.rmi-all .rmi-go{margin-left:auto;font-size:22px;font-weight:700;color:#10B981;line-height:1}

/* Source group cards */
.rm-group{border-radius:13px;padding:5px;margin-bottom:7px;border:1px solid var(--border)}
.rm-group:last-child{margin-bottom:0}
.rm-group.trk{background:linear-gradient(180deg,rgba(14,165,233,.07),rgba(14,165,233,.02));border-color:rgba(14,165,233,.20)}
.rm-group.por{background:linear-gradient(180deg,rgba(124,58,237,.07),rgba(124,58,237,.02));border-color:rgba(124,58,237,.20)}
.rm-ghead{display:flex;align-items:center;gap:9px;padding:7px 9px 8px}
.rm-gic{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:8px;color:#fff;flex-shrink:0}
.rm-group.trk .rm-gic{background:linear-gradient(135deg,#0EA5E9,#0284C7);box-shadow:0 2px 7px rgba(14,165,233,.32)}
.rm-group.por .rm-gic{background:linear-gradient(135deg,#7C3AED,#6D28D9);box-shadow:0 2px 7px rgba(124,58,237,.32)}
.rm-gname{font-size:13px;font-weight:800;letter-spacing:.2px}
.rm-group.trk .rm-gname{color:#0369A1}
.rm-group.por .rm-gname{color:#6D28D9}
.rm-ghint{margin-left:auto;font-size:9.5px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;opacity:.75}
.rm-group .run-menu-item{padding:8px 10px}
.run-menu-item.trk .rmi-dot{background:#0EA5E9}
.run-menu-item.por .rmi-dot{background:#7C3AED}
.run-menu-item.trk .rmi-tag{background:#0EA5E9}
.run-menu-item.por .rmi-tag{background:#7C3AED}
.run-menu-item.trk:hover{background:#fff;box-shadow:0 3px 10px rgba(14,165,233,.16);transform:translateX(2px)}
.run-menu-item.por:hover{background:#fff;box-shadow:0 3px 10px rgba(124,58,237,.16);transform:translateX(2px)}
.run-menu-item.trk:hover .rmi-dot,.run-menu-item.por:hover .rmi-dot{transform:scale(1.45)}
#run-now-btn.open #run-caret{transform:rotate(180deg)}</style>

<script>
const API = window.location.origin;
let TOKEN = "";
let perPage = 20;
const SELECTED = new Set();   // hand-picked active students for a manual "Run Selected"
const SEL_MAX = 20;
function toggleSel(rk,cb){
  if(cb.checked){
    if(SELECTED.size>=SEL_MAX){ cb.checked=false; showToast("Maximum "+SEL_MAX+" students can be selected at once"); return; }
    SELECTED.add(rk);
  }else{ SELECTED.delete(rk); }
  updateSelBar();
}
function toggleSelectAll(cb){
  const boxes=document.querySelectorAll(".sel-cb");
  if(cb.checked){
    boxes.forEach(b=>{
      if(!b.checked){
        if(SELECTED.size>=SEL_MAX){ b.checked=false; return; }
        b.checked=true; SELECTED.add(b.dataset.rk);
      }
    });
    if(SELECTED.size>=SEL_MAX) showToast("Selected the first "+SEL_MAX+" (maximum) on this page");
  }else{
    boxes.forEach(b=>{ b.checked=false; SELECTED.delete(b.dataset.rk); });
  }
  updateSelBar();
}
function clearSelection(){
  SELECTED.clear();
  document.querySelectorAll(".sel-cb").forEach(b=>b.checked=false);
  const sa=document.getElementById("sel-all");if(sa)sa.checked=false;
  updateSelBar();
}
function updateSelBar(){
  const bar=document.getElementById("sel-bar");if(!bar)return;
  const n=SELECTED.size;
  bar.style.display=n>0?"flex":"none";
  const cnt=document.getElementById("sel-count");if(cnt)cnt.textContent=n;
}
async function runSelected(){
  const keys=[...SELECTED];
  if(keys.length<1){showToast("Select at least 1 student");return;}
  if(keys.length>SEL_MAX){showToast("Maximum "+SEL_MAX+" students");return;}
  const btn=document.getElementById("sel-run-btn");
  if(btn){btn.disabled=true;btn.style.opacity="0.6";}
  try{
    const r=await api("/api/run-selected","POST",{row_keys:keys});
    showToast(r.message+" — running in the background");
    clearSelection();
    const box=document.getElementById("run-progress");
    if(box){box.style.display="block";document.getElementById("pb-pct").textContent="0%";
      document.getElementById("pb-fill").style.width="0%";
      document.getElementById("pb-sub").textContent="Starting…";
      document.getElementById("pb-label").textContent="Checking selected students…";}
    startProgressPoll();
  }catch(e){showToast("Error: "+e.message);}
  finally{ setTimeout(()=>{if(btn){btn.disabled=false;btn.style.opacity="";}},3000); }
}
async function deleteSelectedStudents(){
  const keys=[...SELECTED];
  if(!keys.length){showToast("Select at least 1 student");return;}
  if(!confirm("Remove "+keys.length+" selected student(s)?\\n\\nThey move to Trash — you can restore from Settings → Deleted Students."))return;
  try{
    const r=await api("/api/students-delete-bulk","POST",{row_keys:keys});
    showToast("Moved "+(r.deleted||keys.length)+" student(s) to Trash");
    clearSelection();
    refreshAllTables();
  }catch(e){showToast("Error: "+e.message);}
}
let sessionsLoaded = false;

function toggleLgPass(){
  const inp=document.getElementById("lg-pass");
  const eye=document.getElementById("lg-eye");
  if(!inp)return;
  if(inp.type==="password"){
    inp.type="text";
    if(eye)eye.innerHTML='<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>';
  }else{
    inp.type="password";
    if(eye)eye.innerHTML='<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
  }
}
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
    applySidebarPref();
    loadDashboard();
    loadNextRuns();
    startProgressPoll();
    setInterval(refreshBell,60000);
    setInterval(loadNextRuns,60000);
  }catch(e){err.textContent="Connection error: "+e.message;}
}

async function api(path,method="GET",body=null){
  const opt={method,headers:{"Authorization":"Bearer "+TOKEN}};
  if(body){opt.headers["Content-Type"]="application/json";opt.body=JSON.stringify(body);}
  const r=await fetch(API+path,opt);
  if(r.status===401){location.reload();throw new Error("Session expired");}
  if(!r.ok){const e=await r.json().catch(()=>({detail:"HTTP "+r.status}));throw new Error(e.detail||("HTTP "+r.status));}
  return r.json();
}

/* ---------- icons ---------- */
const DL_ICON='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="13" height="13" style="vertical-align:-2px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
const SUN='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>';
const MOON='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
const CLOCK='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="18" height="18"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>';

/* ---------- theme ---------- */
function applyTheme(t){
  document.documentElement.setAttribute("data-theme",t);
  const ic=document.getElementById("theme-ic"),lb=document.getElementById("theme-lbl");
  if(ic)ic.innerHTML=(t==="dark")?SUN:MOON;
  if(lb)lb.textContent=(t==="dark")?"Light Mode":"Dark Mode";
}
function toggleTheme(){
  const cur=(document.documentElement.getAttribute("data-theme")==="dark")?"light":"dark";
  try{localStorage.setItem("nios_theme",cur);}catch(e){}
  applyTheme(cur);
}
(function(){var t="light";try{t=localStorage.getItem("nios_theme")||"light";}catch(e){}applyTheme(t);})();

/* ---------- logout ---------- */
function doLogout(){
  if(!confirm("Log out of the portal?")) return;
  TOKEN="";stopProgressPoll();if(timerInt){clearInterval(timerInt);timerInt=null;}
  document.getElementById("app").style.display="none";
  document.getElementById("login-screen").style.display="flex";
  const p=document.getElementById("lg-pass");if(p)p.value="";
}

/* ---------- helpers ---------- */
function secActive(id){const e=document.getElementById("sec-"+id);return e&&e.classList.contains("active");}

/* ---------- live progress ---------- */
let progInt=null,wasRunning=false;
function startProgressPoll(){if(progInt)return;pollProgress();progInt=setInterval(pollProgress,2500);}
function stopProgressPoll(){if(progInt){clearInterval(progInt);progInt=null;}}
function setSrcRow(which,o){
  const row=document.getElementById("pb-"+which+"-row");
  if(row)row.style.display=(o.total>0)?"block":"none";
  const txt=document.getElementById("pb-"+which+"-txt");
  if(txt)txt.textContent=o.done+" / "+o.total+" ("+o.percent+"%)";
  const fill=document.getElementById("pb-"+which+"-fill");
  if(fill)fill.style.width=o.percent+"%";
}
async function pollProgress(){
  try{
    const d=await api("/api/progress");
    const box=document.getElementById("run-progress");if(!box)return;
    if(d.running){
      wasRunning=true;box.style.display="block";
      const pct=d.percent||0;
      document.getElementById("pb-pct").textContent=pct+"%";
      document.getElementById("pb-fill").style.width=pct+"%";
      document.getElementById("pb-sub").innerHTML=
        (d.current||0)+" / "+(d.total||0)+" checked &nbsp;·&nbsp; "+
        "<b style='color:var(--success)'>"+(d.changed||0)+"</b> changed &nbsp;·&nbsp; "+
        (d.same||0)+" same &nbsp;·&nbsp; <b>"+(d.remaining||0)+"</b> remaining";
      // Separate progress for MVS Portal vs MVS Tracker (only show a bar if that
      // source actually has students in THIS run — so a Tracker-only run shows just Tracker)
      const mv=d.mvs||{done:0,total:0,percent:0}, tk=d.trk||{done:0,total:0,percent:0};
      const sw=document.getElementById("pb-srcwrap");
      if(sw){
        sw.style.display=(mv.total>0||tk.total>0)?"block":"none";
        setSrcRow("mvs",mv);setSrcRow("trk",tk);
      }
      const g=d.group_type||"all";
      document.getElementById("pb-label").textContent="Checking "+g+" students…";
      updateNavCounts();   // live badge updates as students move between tabs
      // live filtering: refresh whatever view the counsellor is on, as it happens
      if(secActive("dashboard"))loadDashboard();
      else if(secActive("confirmed"))loadConfirmed(1);
      else if(secActive("required"))loadRequired(1);
      else if(secActive("students"))loadStudents(1);
      else if(secActive("failed"))loadFailed(1);
    }else{
      box.style.display="none";
      if(wasRunning){wasRunning=false;
        updateFailedBadge();
        if(secActive("dashboard"))loadDashboard();
        if(secActive("runlogs"))loadRunLogs();
        if(secActive("failed"))loadFailed(1);}
    }
  }catch(e){}
}

/* ---------- next-run timers ---------- */
let timers=[],timerInt=null,pnTimer=null;
async function loadNextRuns(){
  try{
    const d=await api("/api/next-runs");
    const all=(d.runs||[]);
    timers=all.filter(r=>r.group!=="portalnew").map(r=>({label:r.label,group:r.group,paused:!!r.paused,
      remain:(r.seconds==null?null:r.seconds),at:Date.now()}));
    const pn=all.find(r=>r.group==="portalnew");
    pnTimer=pn?{paused:!!pn.paused,remain:(pn.seconds==null?null:pn.seconds),at:Date.now()}:null;
    renderTimers();
    if(!timerInt)timerInt=setInterval(renderTimers,1000);
  }catch(e){}
}
function fmtDur(s){
  if(s==null)return "Not scheduled";if(s<0)s=0;
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=s%60;
  let o="";if(h>0)o+=h+"h ";o+=(m<10&&h>0?"0":"")+m+"m ";o+=(ss<10?"0":"")+ss+"s";return o;
}
function renderTimers(){
  const el=document.getElementById("next-runs");
  if(el){if(!timers.length){el.innerHTML="";}else{
  el.innerHTML=timers.map(t=>{
    const paused=t.paused;
    // When paused, freeze the remaining time (don't subtract elapsed).
    let rem=(t.remain==null)?null:(paused?t.remain:Math.max(0,t.remain-Math.floor((Date.now()-t.at)/1000)));
    const soon=(!paused&&rem!=null&&rem<=1800);
    const btn=paused
      ? '<button class="tc-btn resume" onclick="resumeIntervals(&quot;'+t.group+'&quot;,this)" title="Resume this timer"><svg viewBox="0 0 24 24" fill="currentColor" width="12" height="12"><polygon points="5 3 19 12 5 21 5 3"/></svg> Resume</button>'
      : '<button class="tc-btn pause" onclick="pauseIntervals(&quot;'+t.group+'&quot;,this)" title="Pause this timer"><svg viewBox="0 0 24 24" fill="currentColor" width="11" height="11"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg> Pause</button>';
    return '<div class="timer-chip'+(soon?' soon':'')+(paused?' paused':'')+'">'+
      '<div class="tc-ic">'+CLOCK+'</div>'+
      '<div style="flex:1;min-width:0">'+
        '<div class="tc-lbl">'+(paused?'Paused':('Next auto-run'+(soon?' — running soon':'')))+'</div>'+
        '<div class="tc-time">'+fmtDur(rem)+(paused?' <span style="font-size:11px;font-weight:600">(frozen)</span>':'')+'</div>'+
        '<div class="tc-grp">'+t.label+'</div>'+
      '</div>'+btn+'</div>';
  }).join("");
  }}
  updatePnTimer();
}
function updatePnTimer(){
  const t=document.getElementById("pn-timer"),b=document.getElementById("pn-btn");
  if(!t||!pnTimer)return;
  let rem=(pnTimer.remain==null)?null:(pnTimer.paused?pnTimer.remain:Math.max(0,pnTimer.remain-Math.floor((Date.now()-pnTimer.at)/1000)));
  t.textContent=(pnTimer.paused?"Paused · ":"")+fmtDur(rem);
  if(b){b.textContent=pnTimer.paused?"Resume":"Pause";b.disabled=false;b.style.opacity="";
    b.style.background=pnTimer.paused?"#dbeafe":"#dcfce7";b.style.color=pnTimer.paused?"#1d4ed8":"#15803d";
    b.style.borderColor=pnTimer.paused?"#bfdbfe":"#bbf7d0";}
}
function togglePn(btn){
  if(!pnTimer)return;
  if(pnTimer.paused)resumeIntervals("portalnew",btn);
  else pauseIntervals("portalnew",btn);
}
async function pauseIntervals(group,btn){
  if(btn){btn.disabled=true;btn.style.opacity="0.6";}
  try{const r=await api("/api/intervals-pause","POST",{group:group});showToast(r.message);await loadNextRuns();}
  catch(e){showToast("Error: "+e.message);if(btn){btn.disabled=false;btn.style.opacity="";}}
}
async function resumeIntervals(group,btn){
  if(btn){btn.disabled=true;btn.style.opacity="0.6";}
  try{const r=await api("/api/intervals-resume","POST",{group:group});showToast(r.message);await loadNextRuns();}
  catch(e){showToast("Error: "+e.message);if(btn){btn.disabled=false;btn.style.opacity="";}}
}

const titles={dashboard:"Dashboard",students:"Active Students",confirmed:"Confirmed Students",
  syc:"SYC Students",
  required:"Document Required",failed:"Failed to Run",unknown:"Unknown Status",docreq:"Document Requests",history:"Change History",runlogs:"Run Logs",transfers:"Transfer Data",upload:"Upload Excel",settings:"Settings"};
function refreshPage(btn){
  // reload the data of whichever page is currently open (no full reload, stays logged in)
  if(btn){var ic=btn.querySelector("svg");if(ic){ic.style.transition="transform .6s";ic.style.transform="rotate(360deg)";
    setTimeout(function(){ic.style.transition="";ic.style.transform="";},650);}}
  const sec=document.querySelector(".page-section.active");
  const page=sec?sec.id.replace("sec-",""):"dashboard";
  nav(page);
  refreshBell();
  showToast("Refreshed");
}

function nav(page){
  closeNav();   // close the mobile drawer after picking a page
  document.querySelectorAll(".nav-item").forEach(n=>n.classList.toggle("active",n.dataset.page===page));
  document.querySelectorAll(".page-section").forEach(s=>s.classList.remove("active"));
  document.getElementById("sec-"+page).classList.add("active");
  document.getElementById("page-title").textContent=titles[page];
  if(page==="dashboard")loadDashboard();
  if(page==="students")loadStudents(1);
  if(page==="confirmed")loadConfirmed(1);
  if(page==="syc")loadSyc(1);
  if(page==="required")loadRequired(1);
  if(page==="failed")loadFailed(1);
  if(page==="unknown")loadUnknown(1);
  if(page==="docreq")loadDocReq();
  if(page==="history")loadHistory();
  if(page==="runlogs")loadRunLogs();
  if(page==="transfers")loadTransfers(1);
  if(page==="settings"){loadIntervals();loadWa();loadTrash();loadReportSettings();loadSourceCounts();}
  if(page==="upload")loadUploadSource();
  updateNavCounts();
}

let drData=[], drPrefix="", drSuffix="", drPage=1, drPerPage=10;
function drEsc(t){return (t||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
async function loadDocReq(btn){
  if(btn)btn.disabled=true;
  const body=document.getElementById("dr-body");
  if(body)body.innerHTML='<div style="color:var(--muted);font-size:13px;padding:10px 0">Loading…</div>';
  try{
    const r=await api("/api/doc-requests");
    drData=r.requests||[]; drPrefix=r.prefix||""; drSuffix=r.suffix||"";
    drData.forEach(s=>{s._checked=!s.sent;});
    drPage=1;
    renderDocReq();
  }catch(e){if(body)body.innerHTML='<div style="color:var(--danger);font-size:13px">'+e.message+'</div>';}
  finally{if(btn)btn.disabled=false;}
}
function drItem(key){return drData.find(x=>x.row_key===key);}
function renderDrPg(pages){
  const el=document.getElementById("dr-pg");if(!el)return;
  if(drData.length<=drPerPage){
    el.innerHTML=drData.length?('<div class="perpage" style="margin-left:auto">Per page: <select onchange="drSetPerPage(this.value)">'+
      [10,20,50,100].map(n=>'<option value="'+n+'" '+(n===drPerPage?"selected":"")+'>'+n+'</option>').join("")+'</select></div>'):"";
    return;
  }
  let ctrl='<div class="pg-controls"><button onclick="drGoPage('+(drPage-1)+')" '+(drPage<=1?"disabled":"")+'>&#8249; Prev</button>';
  const start=Math.max(1,drPage-2),end=Math.min(pages,drPage+2);
  for(let i=start;i<=end;i++)ctrl+='<button class="'+(i===drPage?'active':'')+'" onclick="drGoPage('+i+')">'+i+'</button>';
  ctrl+='<button onclick="drGoPage('+(drPage+1)+')" '+(drPage>=pages?"disabled":"")+'>Next &#8250;</button></div>';
  const sel='<div class="perpage">Per page: <select onchange="drSetPerPage(this.value)">'+
    [10,20,50,100].map(n=>'<option value="'+n+'" '+(n===drPerPage?"selected":"")+'>'+n+'</option>').join("")+'</select></div>';
  el.innerHTML=ctrl+sel;
}
function drGoPage(p){const pages=Math.max(1,Math.ceil(drData.length/drPerPage));drPage=Math.min(Math.max(1,p),pages);renderDocReq();var s=document.getElementById("sec-docreq");if(s)s.scrollIntoView({behavior:"smooth",block:"start"});}
function drSetPerPage(v){drPerPage=parseInt(v)||10;drPage=1;renderDocReq();}
function renderDocReq(){
  const body=document.getElementById("dr-body");if(!body)return;
  const badge=document.getElementById("nav-docreq-badge");
  const pending=drData.filter(s=>!s.sent).length;
  if(badge){badge.textContent=pending;badge.style.display=pending?"inline-flex":"none";}
  if(!drData.length){body.innerHTML='<div style="color:var(--success);font-size:14px;padding:10px 0">&#10003; No Document-Required students right now.</div>';renderDrPg(0);updateDrCount();return;}
  const pages=Math.max(1,Math.ceil(drData.length/drPerPage));
  if(drPage>pages)drPage=pages;
  const slice=drData.slice((drPage-1)*drPerPage, drPage*drPerPage);
  body.innerHTML=slice.map((s)=>{
    const i=drData.indexOf(s);
    const sentBadge=s.sent?'<span style="background:#dcfce7;color:#15803d;font-size:11px;font-weight:700;border-radius:6px;padding:3px 9px">&#10003; Sent'+(s.sent_at?' &middot; '+s.sent_at:'')+'</span>':'<span style="background:#fef3c7;color:#92400e;font-size:11px;font-weight:700;border-radius:6px;padding:3px 9px">Pending</span>';
    const blank=!s.message;
    return '<div class="dr-card" style="border:1px solid var(--border);border-radius:12px;padding:14px;margin-bottom:12px;background:'+(s.sent?'#f6fef9':'#fff')+'">'+
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:9px;flex-wrap:wrap">'+
        '<input type="checkbox" class="dr-cb" value="'+s.row_key+'" '+(s._checked?'checked':'')+' onchange="drToggle(&quot;'+s.row_key+'&quot;,this.checked)" style="width:16px;height:16px">'+
        '<b style="font-size:14px">'+(i+1)+'. '+drEsc(s.student_name)+'</b>'+
        '<span style="font-size:12px;color:var(--muted)">'+drEsc(s.session)+' &middot; '+drEsc(s.mobile)+'</span>'+
        '<span style="margin-left:auto">'+sentBadge+'</span>'+
      '</div>'+
      '<div style="font-size:11.5px;color:var(--muted);background:var(--soft);border-radius:8px;padding:8px 10px;margin-bottom:9px"><b>NIOS remark:</b> '+(s.remark?drEsc(s.remark):'<i>none</i>')+'</div>'+
      '<label style="font-size:12px;font-weight:600">Message to send (edit if needed)'+(blank?' <span style="color:#b91c1c">&mdash; please write what to ask for</span>':'')+'</label>'+
      '<textarea class="dr-msg" data-key="'+s.row_key+'" rows="2" oninput="drDirty(this)" style="width:100%;margin-top:5px;padding:10px;border:2px solid var(--border);border-radius:9px;font-size:13.5px;font-family:inherit;resize:vertical">'+drEsc(s.message||"")+'</textarea>'+
      '<div style="display:flex;align-items:center;gap:10px;margin-top:8px;flex-wrap:wrap">'+
        '<button class="btn btn-outline btn-sm" onclick="saveDocReqMsg(this,&quot;'+s.row_key+'&quot;)">Save</button>'+
        '<button class="btn btn-sm" style="background:#16a34a;color:#fff" onclick="sendDocReqOne(this,&quot;'+s.row_key+'&quot;)">Send now</button>'+
        '<span class="dr-saved" data-key="'+s.row_key+'" style="font-size:12px;color:var(--success)"></span>'+
        '<button type="button" onclick="toggleDrPrev(this)" style="margin-left:auto;background:none;border:none;color:#2563eb;font-size:12px;cursor:pointer;text-decoration:underline">Preview full message</button>'+
      '</div>'+
      '<div class="dr-img" data-key="'+s.row_key+'" style="margin-top:9px">'+drImgHtml(s)+'</div>'+
      '<div class="dr-prev" style="display:none;white-space:pre-wrap;background:#ecfdf5;border:1px solid #bbf7d0;border-radius:9px;padding:11px;margin-top:9px;font-size:13px;color:#065f46"></div>'+
    '</div>';
  }).join("");
  renderDrPg(pages);
  updateDrCount();
}
function drToggle(key,checked){const it=drItem(key);if(it)it._checked=checked;updateDrCount();}
function drDirty(ta){const it=drItem(ta.dataset.key);if(it)it.message=ta.value;const tag=document.querySelector('.dr-saved[data-key="'+ta.dataset.key+'"]');if(tag){tag.textContent="unsaved…";tag.style.color="var(--muted)";}}
function drImgHtml(s){
  if(s.image){
    return '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'+
      '<img src="'+s.image+'" style="height:52px;border-radius:8px;border:1px solid var(--border);object-fit:cover">'+
      '<span style="font-size:12px;color:var(--success);font-weight:600">&#128247; Screenshot attached</span>'+
      '<button class="btn btn-outline btn-sm" onclick="pickDocImg(&quot;'+s.row_key+'&quot;)">Replace</button>'+
      '<button class="btn btn-outline btn-sm" onclick="pasteDocImg(&quot;'+s.row_key+'&quot;)" title="Paste an image copied to clipboard (e.g. after Win+Shift+S)">&#128203; Paste</button>'+
      '<button class="btn btn-sm" style="background:#fee2e2;color:#b91c1c" onclick="removeDocImg(&quot;'+s.row_key+'&quot;)">Remove</button>'+
      '<input type="file" accept="image/png,image/jpeg,image/webp" style="display:none" id="drf-'+s.row_key+'" onchange="uploadDocImg(this,&quot;'+s.row_key+'&quot;)">'+
    '</div>';
  }
  return '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'+
    '<button class="btn btn-outline btn-sm" onclick="pickDocImg(&quot;'+s.row_key+'&quot;)" title="Attach a demo screenshot to send with this WhatsApp message">&#128206; Attach screenshot (optional)</button>'+
    '<button class="btn btn-outline btn-sm" onclick="pasteDocImg(&quot;'+s.row_key+'&quot;)" title="Paste an image copied to clipboard (e.g. after taking a screenshot with Win+Shift+S)">&#128203; Paste from clipboard</button>'+
    '<input type="file" accept="image/png,image/jpeg,image/webp" style="display:none" id="drf-'+s.row_key+'" onchange="uploadDocImg(this,&quot;'+s.row_key+'&quot;)">'+
  '</div>';
}
function drImgBox(key){return Array.from(document.querySelectorAll('.dr-img')).find(b=>b.dataset.key===key);}
function pickDocImg(key){const f=document.getElementById("drf-"+key);if(f)f.click();}
async function uploadDocImgFile(file,key){
  if(!file)return;
  const fd=new FormData();fd.append("row_key",key);fd.append("file",file);
  try{
    const r=await fetch(API+"/api/doc-request-image",{method:"POST",headers:{"Authorization":"Bearer "+TOKEN},body:fd});
    if(!r.ok){const e=await r.json().catch(()=>({}));showToast("Upload failed: "+(e.detail||r.status));return;}
    const d=await r.json();
    const it=drData.find(x=>x.row_key===key);if(it){it.image=d.url;const box=drImgBox(key);if(box)box.innerHTML=drImgHtml(it);}
    showToast("Screenshot attached \u2713");
  }catch(e){showToast("Error: "+e.message);}
}
async function uploadDocImg(input,key){const file=input.files&&input.files[0];await uploadDocImgFile(file,key);}
async function pasteDocImg(key){
  try{
    if(!navigator.clipboard||!navigator.clipboard.read){showToast("Clipboard paste isn't supported here — use Attach instead");return;}
    const items=await navigator.clipboard.read();
    for(const it of items){
      const type=(it.types||[]).find(t=>t.startsWith("image/"));
      if(type){
        const blob=await it.getType(type);
        const ext=(type.split("/")[1]||"png").replace("jpeg","jpg");
        if(["png","jpg","webp"].indexOf(ext)<0){showToast("Pasted image type not supported");return;}
        const file=new File([blob],"pasted."+ext,{type:type});
        showToast("Uploading pasted image\u2026");
        await uploadDocImgFile(file,key);
        return;
      }
    }
    showToast("No image found in clipboard — copy a screenshot first");
  }catch(e){showToast("Couldn't read clipboard: "+(e.message||e)+" (allow clipboard permission, or use Attach)");}
}
async function removeDocImg(key){
  try{await api("/api/doc-request-image-remove","POST",{row_key:key});
    const it=drData.find(x=>x.row_key===key);if(it){it.image="";const box=drImgBox(key);if(box)box.innerHTML=drImgHtml(it);}
    showToast("Screenshot removed");
  }catch(e){showToast("Error: "+e.message);}
}
function toggleDrPrev(btn){
  const card=btn.closest(".dr-card");if(!card)return;
  const prev=card.querySelector(".dr-prev"),ta=card.querySelector(".dr-msg");
  const hasImg=!!card.querySelector(".dr-img img");
  if(prev.style.display==="none"){prev.textContent=(hasImg?"[ screenshot will be attached at the top ]\\n\\n":"")+drPrefix.replace("{name}","<student name>")+(ta.value||"…")+drSuffix;prev.style.display="block";btn.textContent="Hide preview";}
  else{prev.style.display="none";btn.textContent="Preview full message";}
}
function getDrSelected(){return drData.filter(s=>s._checked&&!s.sent).map(s=>s.row_key);}
function updateDrCount(){const el=document.getElementById("dr-count");if(el)el.textContent=getDrSelected().length+" selected · "+drData.filter(s=>!s.sent).length+" pending · "+drData.length+" total";}
function toggleDrAll(cb){drData.forEach(s=>{if(!s.sent)s._checked=cb.checked;});renderDocReq();}
async function saveDocReqMsg(btn,key){
  const ta=document.querySelector('.dr-msg[data-key="'+key+'"]');if(!ta)return;
  try{await api("/api/doc-request-save","POST",{row_key:key,message:ta.value});
    const tag=document.querySelector('.dr-saved[data-key="'+key+'"]');if(tag){tag.textContent="\u2713 saved";tag.style.color="var(--success)";}
    const it=drData.find(x=>x.row_key===key);if(it)it.message=ta.value;
  }catch(e){showToast("Error: "+e.message);}
}
async function sendDocReqOne(btn,key){
  const ta=document.querySelector('.dr-msg[data-key="'+key+'"]');
  if(ta&&!ta.value.trim()){showToast("Please write the document message first");return;}
  if(!confirm("Send this document request on WhatsApp now?"))return;
  if(ta){try{await api("/api/doc-request-save","POST",{row_key:key,message:ta.value});}catch(e){}}
  if(btn){btn.disabled=true;btn.textContent="Sending…";}
  try{const r=await api("/api/doc-request-send","POST",{row_keys:[key]});
    showToast(r.sent?"Sent \u2713":("Failed: "+((r.results&&r.results[0]&&r.results[0].info)||"")));
    loadDocReq();
  }catch(e){showToast("Error: "+e.message);if(btn){btn.disabled=false;btn.textContent="Send now";}}
}
async function sendDocReq(mode,btn){
  let keys=[];
  if(mode==="selected"){keys=getDrSelected();if(!keys.length){showToast("Select at least one student");return;}}
  else{keys=drData.filter(s=>!s.sent).map(s=>s.row_key);}
  if(!keys.length){showToast("Nothing to send");return;}
  if(!confirm("Send document request on WhatsApp to "+keys.length+" student(s)?"))return;
  if(btn)btn.disabled=true;
  try{
    // persist each message from drData (covers edits on any page)
    for(const k of keys){const it=drItem(k);if(it){try{await api("/api/doc-request-save","POST",{row_key:k,message:it.message||""});}catch(_){}}}
    const payload=mode==="all"?{all:true}:{row_keys:keys};
    const r=await api("/api/doc-request-send","POST",payload);
    showToast("Sent: "+r.sent+(r.failed?(" · Failed: "+r.failed):""));
    const warn=document.getElementById("dr-warn");
    if(warn){
      const fails=(r.results||[]).filter(x=>!x.ok);
      if(fails.length){
        warn.style.display="block";
        warn.innerHTML='<b>'+fails.length+' message(s) failed.</b> Reason from WhatsApp gateway:<br>'+
          fails.slice(0,5).map(f=>'&bull; <b>'+(f.student_name||"")+'</b>: '+(f.info||"unknown")).join("<br>")+
          (fails.length>5?'<br>&hellip; and '+(fails.length-5)+' more':'')+
          '<br><span style="color:#7c2d12">Tip: check the campaign name is exactly correct (case-sensitive) and the template is Approved &amp; Live on AiSensy.</span>';
      }else{warn.style.display="none";}
    }
    loadDocReq();
  }catch(e){showToast("Error: "+e.message);}
  finally{if(btn)btn.disabled=false;}
}
async function loadDashboard(){
  try{
    const d=await api("/api/dashboard");
    const c=d.counts||{};
    document.getElementById("stat-grid").innerHTML=
      statCard("Total Students",d.total_students,SI.users,"#4F46E5")+
      statCard("Changes Today",c.changes_today||0,SI.activity,"#0891B2")+
      statCard("Admission Confirmed",c.confirmed||0,SI.check,"#16A34A")+
      statCard("SYC Students",c.syc||0,SI.users,"#7C3AED")+
      statCard("Verified",c.verified||0,SI.shield,"#65A30D")+
      statCard("Document Required",c.document_required||0,SI.file,"#EA580C")+
      statCard("In Verification",c.doc_verification||0,SI.loader,"#D97706");
    renderDistribution(d.status_distribution,d.total_students);
    renderSessionCounts(d.session_counts||[]);
    renderSourceCounts(d.source_counts||[]);
    // Recent Runs is now a scrollable list (no page/limit dropdown) — load a healthy
    // window and let the user scroll; ~7-8 rows are visible at a time.
    try{const rr=await api("/api/run-logs?limit=30");renderRuns(rr,"recent-runs");}
    catch(e){renderRuns(d.recent_runs,"recent-runs");}
    renderBell(d.notifications||[],d.dup_notifications||[],d.unknown_notifications||[]);
  updateFailedBadge();
  }catch(e){showToast("Error: "+e.message);}
}
function renderSessionCounts(arr){
  const el=document.getElementById("session-counts");
  if(!arr.length){el.innerHTML='<div class="empty">No data yet</div>';return;}
  const cell=(label,val,color)=>'<div style="text-align:center;flex:1;min-width:0">'+
    '<div style="font-size:17px;font-weight:800;color:'+color+';line-height:1.1">'+(val||0)+'</div>'+
    '<div style="font-size:10.5px;color:var(--muted);font-weight:600;margin-top:2px">'+label+'</div></div>';
  el.innerHTML='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px">'+
    arr.map(s=>'<div style="padding:15px 16px;background:var(--soft);border:1px solid var(--border);border-radius:13px">'+
      '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">'+
        '<span style="font-size:14.5px;font-weight:700">'+(s.session||"—")+'</span>'+
        '<span style="font-size:12.5px;color:var(--muted);font-weight:600">Total&nbsp;<b style="color:var(--text);font-size:17px">'+s.cnt+'</b></span></div>'+
      '<div style="display:flex;gap:4px;padding-top:11px;border-top:1px solid var(--border)">'+
        cell("Confirmed",s.confirmed,"#16A34A")+
        cell("Verified",s.verified,"#2563EB")+
        cell("Active",s.active,"#7C3AED")+
        cell("Required",s.required,"#EA580C")+
      '</div></div>').join("")+'</div>';
}
function renderSourceCounts(arr){
  const el=document.getElementById("source-counts");
  if(!el)return;
  if(!arr.length){el.innerHTML="";return;}
  const cell=(label,val,color)=>'<div style="text-align:center;flex:1;min-width:0">'+
    '<div style="font-size:17px;font-weight:800;color:'+color+';line-height:1.1">'+(val||0)+'</div>'+
    '<div style="font-size:10.5px;color:var(--muted);font-weight:600;margin-top:2px">'+label+'</div></div>';
  el.innerHTML='<div style="font-size:11px;font-weight:700;letter-spacing:.4px;color:var(--muted);text-transform:uppercase;margin-bottom:10px">By Data Source</div>'+
    '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px;margin-bottom:18px">'+
    arr.map(s=>{
      var dot=(s.key==="mvs_portal")?"#7C3AED":"#0EA5E9";
      var pnrow=(s.key==="mvs_portal")?
        ('<div style="display:flex;align-items:center;gap:8px;margin-top:11px;padding-top:10px;border-top:1px dashed var(--border);font-size:11.5px;color:var(--muted)">'+
          '<svg viewBox="0 0 24 24" fill="none" stroke="#16A34A" stroke-width="2" width="13" height="13"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>'+
          '<span style="flex:1">New-data run in <b id="pn-timer" style="color:var(--text)">…</b></span>'+
          '<button id="pn-btn" onclick="togglePn(this)" style="background:#dcfce7;color:#15803d;border:1px solid #bbf7d0;border-radius:7px;padding:3px 11px;font-size:11px;font-weight:700;cursor:pointer">Pause</button>'+
        '</div>'):'';
      return '<div style="padding:15px 16px;background:var(--soft);border:1px solid var(--border);border-radius:13px">'+
        '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">'+
          '<span style="font-size:14.5px;font-weight:700;display:flex;align-items:center;gap:7px">'+
            '<span style="width:9px;height:9px;border-radius:50%;background:'+dot+';display:inline-block;flex:none"></span>'+(s.source||"—")+'</span>'+
          '<span style="font-size:12.5px;color:var(--muted);font-weight:600">Total&nbsp;<b style="color:var(--text);font-size:17px">'+s.cnt+'</b></span></div>'+
        '<div style="display:flex;gap:4px;padding-top:11px;border-top:1px solid var(--border)">'+
          cell("Confirmed",s.confirmed,"#16A34A")+
          cell("Verified",s.verified,"#2563EB")+
          cell("Active",s.active,"#7C3AED")+
          cell("Required",s.required,"#EA580C")+
        '</div>'+pnrow+'</div>';
    }).join("")+'</div>'+
    '<div style="font-size:11px;font-weight:700;letter-spacing:.4px;color:var(--muted);text-transform:uppercase;margin-bottom:10px">By Session</div>';
  updatePnTimer();
}
function statCard(lbl,val,svg,col){
  return '<div class="stat"><div class="ic" style="background:'+col+'1A;color:'+col+'">'+svg+
    '</div><div class="lbl">'+lbl+'</div><div class="val">'+val+
    '</div><div class="bar" style="background:'+col+'"></div></div>';
}
const SI={
  users:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
  activity:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
  check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
  shield:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/></svg>',
  file:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
  loader:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>'
};
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
  if(!runs||!runs.length){el.innerHTML='<tr><td colspan="7" class="empty">No runs yet</td></tr>';return;}
  el.innerHTML=runs.map(r=>{
    var st;
    if(r.status==="running"){
      st='<span class="run-live">● running</span>'+
         ' <button class="btn-cancel" onclick="cancelRun('+r.id+')">Cancel</button>';
    }else if(r.status==="cancelled"){
      st='<span class="run-cancel">✕ cancelled</span>';
    }else if(r.status==="completed"){
      st='<span class="run-done">✓ completed</span>';
    }else{
      st='<span class="run-err">'+r.status+'</span>';
    }
    var act=(r.status==="running")?'<span style="color:var(--muted);font-size:12px">—</span>':
      '<button title="Delete this run log" onclick="deleteRunLog('+r.id+')" '+
      'style="background:#fee2e2;color:#b91c1c;border:1px solid #fecaca;border-radius:8px;padding:4px 9px;font-size:12px;font-weight:600;cursor:pointer">&#128465;</button>';
    return '<tr><td>'+r.run_at+'</td><td><span class="ref-tag">'+(r.group_type||"all")+
      '</span></td><td>'+r.total_checked+'</td><td style="color:var(--primary);font-weight:700">'+r.total_changed+
      '</td><td style="color:'+(r.total_failed?'var(--danger)':'inherit')+'">'+r.total_failed+
      '</td><td>'+st+'</td><td>'+act+'</td></tr>';
  }).join("");
}
async function deleteRunLog(id){
  if(!confirm("Delete this run log entry?"))return;
  try{
    await api("/api/run-log-delete?id="+id,"POST");
    showToast("Run log deleted");
    if(secActive("dashboard"))loadDashboard();
    if(secActive("runlogs"))loadRunLogs();
  }catch(e){showToast(""+e.message);}
}
async function clearRunLogs(){
  if(!confirm("Delete ALL run logs (except any currently running)?"))return;
  try{const r=await api("/api/run-logs-clear","POST");showToast("Cleared "+(r.deleted||0)+" run logs");
    if(secActive("dashboard"))loadDashboard();if(secActive("runlogs"))loadRunLogs();}
  catch(e){showToast(""+e.message);}
}

async function cancelRun(rid){
  if(!confirm("Cancel this run?")) return;
  try{
    const fd=new FormData();fd.append("run_id",rid);
    const res=await fetch("/api/cancel-run",{method:"POST",headers:{Authorization:"Bearer "+TOKEN},body:fd});
    const d=await res.json();
    if(!res.ok) throw new Error(d.detail||"failed");
    showToast("✓ "+(d.message||"Cancelled"));
    const dash=document.getElementById("sec-dashboard");
    const rlog=document.getElementById("sec-runlogs");
    if(dash&&dash.classList.contains("active")) loadDashboard();
    if(rlog&&rlog.classList.contains("active")) loadRunLogs();
  }catch(e){showToast(""+e.message);}
}

function toggleBell(e){e.stopPropagation();document.getElementById("bell-dropdown").classList.toggle("open");}
document.addEventListener("click",()=>document.getElementById("bell-dropdown").classList.remove("open"));
async function refreshBell(){try{const d=await api("/api/dashboard");renderBell(d.notifications||[],d.dup_notifications||[],d.unknown_notifications||[]);}catch(e){}}
function renderBell(notifs,dups,unknowns){
  dups=dups||[];unknowns=unknowns||[];
  const n=notifs.length, total=n+dups.length+unknowns.length;
  const badge=document.getElementById("bell-badge");
  const navB=document.getElementById("nav-required-badge");
  badge.textContent=total;badge.style.display=total?"flex":"none";
  navB.textContent=n;navB.style.display=n?"inline-block":"none";
  document.getElementById("bell-head-cnt").textContent=total;
  const list=document.getElementById("bell-list");
  let html="";
  if(n){html+=notifs.map(x=>'<div class="notif-item"><div class="dot"></div><div>'+
    '<div class="nm">'+(x.student_name||"—")+'</div>'+
    '<div class="rf">'+(x.reference_no||"No ref")+'</div>'+
    (x.remark?'<div class="rk">'+x.remark+'</div>':"")+'</div></div>').join("");}
  if(unknowns.length){
    html+='<div style="padding:7px 14px;font-size:11px;font-weight:800;color:#991B1B;background:#FEE2E2;border-top:1px solid var(--border)">&#9888; STATUS NOT FOUND — re-check needed</div>';
    html+=unknowns.map(x=>'<div class="notif-item"><div class="dot" style="background:#DC2626"></div><div>'+
      '<div class="nm">'+(x.student_name||"—")+'</div>'+
      '<div class="rf">'+(x.reference_no||x.enrollment_no||"—")+'</div>'+
      '<div class="rk">'+(x.current_status||"Unknown")+' — '+(x.remark||"NIOS didn\'t return a readable status. Re-check this student.")+'</div></div></div>').join("");
  }
  if(dups.length){
    html+='<div style="padding:7px 14px;font-size:11px;font-weight:800;color:#5B21B6;background:#EDE9FE;border-top:1px solid var(--border)">&#9888; DUPLICATE — kept as MVS Portal</div>';
    html+=dups.map(x=>'<div class="notif-item"><div class="dot" style="background:#7C3AED"></div><div>'+
      '<div class="nm">'+(x.student_name||"—")+'</div>'+
      '<div class="rf">'+(x.reference_no||x.enrollment_no||"—")+'</div>'+
      '<div class="rk">Same student in MVS Portal &amp; MVS Tracker → kept once as MVS Portal</div></div></div>').join("");
  }
  list.innerHTML=html||'<div class="notif-empty">No pending items</div>';
}

let stTimer,cTimer,rTimer;
function debounceStudents(){clearTimeout(stTimer);stTimer=setTimeout(()=>loadStudents(1),400);}
function debounceConfirmed(){clearTimeout(cTimer);cTimer=setTimeout(()=>loadConfirmed(1),400);}
function debounceRequired(){clearTimeout(rTimer);rTimer=setTimeout(()=>loadRequired(1),400);}

function badge(s){
  const m={"Pending":"b-pending","Documents Verification In Progress":"b-docs","Document Required":"b-required",
    "Verified":"b-verified","Approved":"b-approved","Admission Confirmed":"b-confirmed","Rejected":"b-rejected","SYC":"b-syc"};
  return '<span class="badge '+(m[s]||'b-error')+'">'+(s||'Unknown')+'</span>';
}
function srcBadge(s){
  var dup=(s.cross_dup==1||s.cross_dup===true);
  if(dup){
    return '<span title="This student exists in BOTH MVS Tracker and MVS Portal — merged into one record" style="display:inline-block;white-space:nowrap;font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;background:linear-gradient(135deg,#EDE9FE,#E0F2FE);color:#5B21B6;border:1px solid #C4B5FD">&#8651; Both (Tracker + Portal)</span>';
  }
  var p=(s.source||"mvs_tracker")==="mvs_portal";
  return '<span style="display:inline-block;white-space:nowrap;font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;'+
    (p?'background:#EDE9FE;color:#5B21B6':'background:#E0F2FE;color:#075985')+'">'+
    (p?'MVS Portal':'MVS Tracker')+'</span>';
}
function isLoginFailed(s){return (s.login_failed==1||s.login_failed===true);}
function fixBtn(s){
  return '<button onclick="editStudent(&quot;'+s.row_key+'&quot;)" '+
    'style="background:#e0e7ff;color:#3730a3;border:1px solid #c7d2fe;border-radius:8px;padding:4px 10px;font-size:11.5px;font-weight:600;cursor:pointer">Edit &amp; fix data</button>';
}
function dlLinks(s){
  if(isLoginFailed(s))
    return '<div style="font-size:11.5px;font-weight:600;color:#b91c1c;max-width:240px">Documents blocked — NIOS login failed with this data. '+
      'Links not shared (would open to an error). <div style="margin-top:5px">'+fixBtn(s)+'</div></div>';
  if(!s.reference_no||!s.dob) return '<span style="color:var(--warn);font-size:11px">ref/DOB missing</span>';
  const sess=(s.session||"").toLowerCase();
  const isStream2=sess.includes("stream 2")||sess.includes("stream2")||sess.includes("stream-2")||sess.includes("stream ii");
  const isOnDemand=sess.includes("on demand")||sess.includes("ondemand")||sess.includes("on-demand")||sess.includes("odes");
  const isPublic=!isStream2&&!isOnDemand;   // safe default: April/October/apr-27/unknown = public (ID Card only)
  let b=[dlBtn(s,"id_card","ID Card")];
  if(isStream2){
    b.push(dlBtn(s,"app_form","App Form"));          // Stream 2: ID Card + App Form only (NO hall ticket)
  }else if(isPublic){
    // Public exam students: ONLY id card
  }else{
    b.push(dlBtn(s,"app_form","App Form"));          // On Demand: all three
    b.push(dlBtn(s,"hall_ticket","Hall Ticket"));
  }
  return '<div style="display:flex;flex-wrap:wrap;gap:5px">'+b.join("")+'</div>';
}
function dlBtn(s,kind,label){
  return '<button class="btn-dl" onclick="downloadDoc(this,&quot;'+s.reference_no+'&quot;,&quot;'+
    (s.dob||"")+'&quot;,&quot;'+kind+'&quot;,&quot;'+label+'&quot;)">'+DL_ICON+' '+label+'</button>';
}
function waBtn(s){
  if(!s.row_key)return "";
  if(isLoginFailed(s))
    return '<div style="margin-top:5px;font-size:11.5px;font-weight:700;color:#b91c1c">WhatsApp: FAILED (login error — not sent)</div>';
  const sent=(s.whatsapp_sent==1);
  const dv=(s.whatsapp_delivery||"");
  const at=(s.whatsapp_sent_at||"");
  let badge="";
  if(dv==="delivered"){
    badge='<div style="margin-top:5px;font-size:11.5px;font-weight:700;color:#15803D">&#10003; Delivered on WhatsApp'+(s.whatsapp_delivery_at?' &middot; '+s.whatsapp_delivery_at:'')+'</div>';
  }else if(dv==="failed"){
    badge='<div style="margin-top:5px;font-size:11.5px;font-weight:700;color:#b91c1c">&#9888; Delivery FAILED — please resend</div>';
  }else if(sent){
    badge='<div style="margin-top:5px;font-size:11.5px;font-weight:600;color:#B45309">Sent to WhatsApp'+(at?' &middot; '+at:'')+' <span style="font-weight:400;color:var(--muted)">(delivery not yet confirmed)</span></div>';
  }else{
    var why=(s.whatsapp_info||"");
    if(why && why.toLowerCase().indexOf("accepted")>=0){
      badge='<div style="margin-top:5px;font-size:11.5px;font-weight:600;color:#B45309">Sent to WhatsApp'+(at?' &middot; '+at:'')+' <span style="font-weight:400;color:var(--muted)">(gateway accepted, delivery pending)</span></div>';
    }else if(why && why.indexOf("2 numbers")<0){
      badge='<div style="margin-top:5px;font-size:11.5px;font-weight:700;color:#b91c1c">&#9888; Not sent &middot; '+why.replace(/</g,"&lt;").slice(0,150)+'</div>';
    }else{
      badge='<div style="margin-top:5px;font-size:11.5px;font-weight:600;color:var(--muted)">Not sent yet</div>';
    }
  }
  if((s.whatsapp_info||"").indexOf("2 numbers")>=0){
    badge+='<div style="margin-top:3px;font-size:11px;font-weight:700;color:#7C3AED">&#128241; Sent to 2 numbers (own + alternate)</div>';
  }
  const bc=(dv==="failed")?'#DC2626':'#16A34A';
  return badge+'<button class="btn-dl" style="background:'+bc+';color:#fff;border-color:'+bc+';margin-top:4px" '+
    'onclick="resendWa(&quot;'+s.row_key+'&quot;,this)">'+(sent?'Resend WhatsApp':'Send WhatsApp')+'</button>';
}
async function resendWa(rowKey,btn){
  if(btn&&btn.dataset.busy==="1")return;
  if(!confirm("Send the documents to this student on WhatsApp?"))return;
  let orig="",pct=1,fake=null;
  if(btn){btn.dataset.busy="1";orig=btn.innerHTML;
    btn.innerHTML='<span class="dl-spin"></span> '+pct+'%';
    fake=setInterval(()=>{ if(pct<90){pct+=Math.max(1,Math.floor((90-pct)/6));
      btn.innerHTML='<span class="dl-spin"></span> '+pct+'%';}},420);}
  try{
    const r=await api("/api/wa-resend","POST",{row_key:rowKey});
    if(fake){clearInterval(fake);fake=null;}
    if(btn)btn.innerHTML='100%';
    if(r.ok){ showToast("Sent \u2713 "+(r.info||"")); }
    else{ showToast("Not sent \u2014 "+(r.info||"failed")); }
    setTimeout(()=>{try{loadConfirmed(1);}catch(e){}try{loadStudents(1);}catch(e){}},1200);
  }catch(e){showToast("Error: "+e.message);}
  finally{if(fake)clearInterval(fake);if(btn){setTimeout(()=>{btn.innerHTML=orig;btn.dataset.busy="";},1600);}}
}
async function downloadDoc(btn,ref,dob,kind,name){
  if(btn&&btn.dataset.busy==="1")return;          // already running -> ignore extra clicks
  let orig="",pct=1,fake=null;
  if(btn){
    btn.dataset.busy="1";btn.classList.add("loading");orig=btn.innerHTML;
    btn.innerHTML='<span class="dl-spin"></span> '+pct+'%';
    fake=setInterval(()=>{ if(pct<92){pct+=Math.max(1,Math.floor((92-pct)/9));
      btn.innerHTML='<span class="dl-spin"></span> '+pct+'%';}},650);
  }
  const restore=(txt)=>{
    if(fake)clearInterval(fake);
    if(btn){ btn.innerHTML=txt||orig;
      setTimeout(()=>{btn.innerHTML=orig;btn.dataset.busy="";btn.classList.remove("loading");},1400); }
  };
  try{
    const q=new URLSearchParams({ref:ref,dob:dob,kind:kind});
    const r=await fetch(API+"/api/download-doc?"+q.toString(),{headers:{"Authorization":"Bearer "+TOKEN}});
    if(!r.ok){const e=await r.json().catch(()=>({detail:"failed"}));
      var msg=(e.detail||"download failed");
      showToast("Error: "+msg);
      // A login/DOB failure just moved this student to 'Failed to Run' on the server —
      // refresh the badges and current view so it disappears from here immediately.
      if(/login|rejected|dob/i.test(msg)){
        updateNavCounts();
        try{loadConfirmed(1);}catch(_e){}try{loadStudents(1);}catch(_e){}
        try{loadRequired(1);}catch(_e){}try{loadFailed(1);}catch(_e){}
        showToast("Moved to 'Failed to Run' — fix the data there & run again");
      }
      restore();return;}
    const ctype=r.headers.get("Content-Type")||"";
    const blob=await r.blob();
    const url=URL.createObjectURL(blob);
    if(ctype.includes("pdf")){
      const a=document.createElement("a");a.href=url;a.download=name.replace(/ /g,"_")+"_"+ref+".pdf";a.click();
      showToast(name+" downloaded");
    }else{
      const w=window.open(url,"_blank");
      if(!w){showToast("Popup blocked — please allow popups and click again");}
      else{showToast(name+" opened — tap 'Save as PDF' at the top");}
    }
    setTimeout(()=>URL.revokeObjectURL(url),120000);
    restore('100%');
  }catch(e){showToast("Error: "+e.message);restore();}
}
function fillSessions(arr){
  if(!arr)return;
  ["s-session","c-session","r-session","u-session"].forEach(id=>{
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
  const dr=dateRange("s");
  const q=new URLSearchParams({page:page,per_page:perPage,view:"normal",
    search:document.getElementById("s-search").value,
    status_filter:document.getElementById("s-status").value,
    session_filter:document.getElementById("s-session").value,
    class_filter:fval("s-class"),source_filter:fval("s-source"),date_from:dr.from,date_to:dr.to});
  try{
    const d=await api("/api/students?"+q.toString());
    fillSessions(d.sessions);
    document.getElementById("s-count").textContent="Showing "+d.students.length+" of "+d.total+" active students";
    const b=document.getElementById("s-body");
    b.innerHTML=d.students.length?d.students.map((s,i)=>'<tr>'+
      '<td><input type="checkbox" class="sel-cb" data-rk="'+s.row_key+'" '+(SELECTED.has(s.row_key)?'checked':'')+' onclick="toggleSel(&quot;'+s.row_key+'&quot;,this)"></td>'+
      '<td style="color:var(--muted)">'+((page-1)*perPage+i+1)+'</td>'+
      '<td><span class="ref-tag">'+(s.reference_no||"—")+'</span></td>'+
      '<td>'+(s.student_name||"—")+'<div style="margin-top:4px">'+srcBadge(s)+'</div></td><td style="font-size:13px">'+(s.session||"—")+'</td>'+
      '<td>'+badge(s.current_status)+loginWarn(s)+'</td>'+
      '<td style="font-size:12px;color:var(--muted)">'+(s.last_checked||"—")+'</td>'+
      '<td>'+delBtn(s)+'</td></tr>').join("")
      :'<tr><td colspan="8" class="empty">No active students found</td></tr>';
    renderPg("s-pg",page,d.pages,"loadStudents");
    updateSelBar();
    const sa=document.getElementById("sel-all");if(sa)sa.checked=false;
  }catch(e){showToast(""+e.message);}
}

async function loadConfirmed(page){
  page=page||1;
  const dr=dateRange("c");
  const q=new URLSearchParams({page:page,per_page:perPage,view:"confirmed",
    search:document.getElementById("c-search").value,
    status_filter:(document.getElementById("c-status")?document.getElementById("c-status").value:""),
    wa_status:fval("c-wa"),
    session_filter:document.getElementById("c-session").value,
    class_filter:fval("c-class"),source_filter:fval("c-source"),date_from:dr.from,date_to:dr.to});
  try{
    const d=await api("/api/students?"+q.toString());
    fillSessions(d.sessions);
    document.getElementById("c-count").textContent=d.total+" confirmed students";
    const b=document.getElementById("c-body");
    b.innerHTML=d.students.length?d.students.map((s,i)=>'<tr>'+
      '<td><input type="checkbox" class="c-selbox" value="'+s.row_key+'" onchange="onConfSel(this)"'+(CONF_SEL.has(s.row_key)?' checked':'')+'></td>'+
      '<td style="color:var(--muted)">'+((page-1)*perPage+i+1)+'</td>'+
      '<td><span class="ref-tag">'+(s.reference_no||"—")+'</span></td>'+
      '<td>'+(s.student_name||"—")+'<div style="margin-top:4px">'+srcBadge(s)+'</div></td><td style="font-size:13px">'+(s.session||"—")+'</td>'+
      '<td>'+badge(s.current_status)+loginWarn(s)+'</td><td style="font-size:12px">'+dlLinks(s)+waBtn(s)+'</td>'+
      '<td style="font-size:12px;color:var(--muted)">'+(s.last_changed||"—")+'</td>'+
      '<td>'+delBtn(s)+'</td></tr>').join("")
      :'<tr><td colspan="9" class="empty">No confirmed students yet</td></tr>';
    const sa=document.getElementById("c-selall");if(sa)sa.checked=false;
    renderPg("c-pg",page,d.pages,"loadConfirmed");
    if(!WA_POLL){pollWaOnce().then(p=>{if(p&&p.running){renderWaProgress(p);startWaProgressPoll();}else{setWaBox(false);}});}
  }catch(e){showToast(""+e.message);}
}
const CONF_SEL=new Set();
function updateConfBar(){
  const bar=document.getElementById("c-bulkbar");const n=CONF_SEL.size;
  if(bar)bar.style.display=n?"flex":"none";
  const c=document.getElementById("c-selcount");if(c)c.textContent=n;
}
function onConfSel(cb){ if(cb.checked)CONF_SEL.add(cb.value); else CONF_SEL.delete(cb.value); updateConfBar(); }
function toggleConfAll(cb){
  document.querySelectorAll(".c-selbox").forEach(b=>{b.checked=cb.checked; if(cb.checked)CONF_SEL.add(b.value); else CONF_SEL.delete(b.value);});
  updateConfBar();
}
function clearConfSel(){ CONF_SEL.clear(); document.querySelectorAll(".c-selbox").forEach(b=>b.checked=false); const sa=document.getElementById("c-selall");if(sa)sa.checked=false; updateConfBar(); }
async function bulkDeleteConf(){
  const keys=Array.from(CONF_SEL);
  if(!keys.length)return;
  if(!confirm("Remove "+keys.length+" selected confirmed student(s)?\\n\\nThey move to Trash — restore from Settings → Deleted Students."))return;
  try{
    const r=await api("/api/students-delete-bulk","POST",{row_keys:keys});
    showToast("Moved "+(r.deleted||keys.length)+" student(s) to Trash");
    clearConfSel();
    refreshAllTables();
  }catch(e){showToast("Error: "+e.message);}
}
async function resendSelected(btn){
  if(!CONF_SEL.size)return;
  const keys=Array.from(CONF_SEL);
  if(!confirm("Resend WhatsApp documents to "+keys.length+" selected student(s)?"))return;
  if(btn){btn.disabled=true;btn.style.opacity="0.6";}
  try{
    const r=await api("/api/wa-resend-bulk","POST",{row_keys:keys});
    showToast(r.message||("Resending "+keys.length+" student(s)"));
    clearConfSel();
    setTimeout(()=>{try{loadConfirmed(1);}catch(e){}},6000);
  }catch(e){showToast("Error: "+e.message);}
  finally{if(btn){btn.disabled=false;btn.style.opacity="";}}
}
async function autoSendNow(btn){
  if(btn){btn.disabled=true;btn.style.opacity="0.6";}
  try{
    const r=await api("/api/wa-autosend-now","POST");
    showToast(r.already_running?"A send is already running":"Sending to all pending students…");
    startWaProgressPoll();
  }catch(e){showToast("Error: "+e.message);}
  finally{setTimeout(()=>{if(btn){btn.disabled=false;btn.style.opacity="";}},2000);}
}
let WA_POLL=null, WA_SEEN_RUNNING=false;
function renderWaProgress(p){
  document.getElementById("wap-title").textContent=(p.label||"Sending WhatsApp")+(p.running?"":" — done");
  document.getElementById("wap-pct").textContent=p.pct||0;
  document.getElementById("wap-bar").style.width=(p.pct||0)+"%";
  document.getElementById("wap-total").textContent=p.total||0;
  document.getElementById("wap-sent").textContent=p.sent||0;
  document.getElementById("wap-remaining").textContent=p.remaining||0;
  document.getElementById("wap-failed").textContent=p.failed||0;
}
function setWaBox(show){const b=document.getElementById("wa-progress");if(b)b.style.display=show?"block":"none";}
async function pollWaOnce(){
  try{return await api("/api/wa-progress");}catch(e){return null;}
}
function startWaProgressPoll(){
  if(WA_POLL)clearInterval(WA_POLL);
  WA_SEEN_RUNNING=false;
  let ticks=0;
  setWaBox(true);
  const tick=async()=>{
    ticks++;
    const p=await pollWaOnce();
    if(!p)return;
    renderWaProgress(p);
    if(p.running){WA_SEEN_RUNNING=true;setWaBox(true);return;}
    if(!WA_SEEN_RUNNING && ticks<6){return;}        // give the background task a moment to start
    clearInterval(WA_POLL);WA_POLL=null;
    setWaBox(true);                                  // show the final 100% state…
    try{loadConfirmed(1);}catch(e){}
    setTimeout(()=>{if(!WA_POLL)setWaBox(false);},6000);   // …then auto-hide it
  };
  tick();
  WA_POLL=setInterval(tick,1500);
}

let sycTimer;
function debounceSyc(){clearTimeout(sycTimer);sycTimer=setTimeout(()=>loadSyc(1),400);}
async function loadSyc(page){
  page=page||1;
  const search=fval("syc-search");
  try{
    const q=new URLSearchParams({page:page,search:search});
    const d=await api("/api/syc?"+q.toString());
    document.getElementById("syc-count").textContent=(d.total||0)+" SYC student(s)";
    const tb=document.getElementById("syc-body");
    if(!d.students||!d.students.length){tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px">No SYC students yet. Upload a sheet with ADMISSION SESSION = SYC and click Run Now.</td></tr>';document.getElementById("syc-pg").innerHTML="";return;}
    tb.innerHTML=d.students.map((s,i)=>sycRow(s,(page-1)*20+i+1)).join("");
    renderPg("syc-pg",page,d.pages,"loadSyc");
  }catch(e){showToast(""+e.message);}
}
function sycRow(s,i){
  var nm=(s.student_name||'').replace(/"/g,'&quot;');
  return '<tr><td>'+i+'</td><td>'+(s.enrollment_no||'—')+'</td><td>'+(s.student_name||'')+'</td>'+
    '<td>'+(s.mobile||'')+'</td><td>'+(s.dob||'')+'</td>'+
    '<td><span class="badge b-syc">SYC &#10003; Ready</span></td><td>'+
    '<button class="btn-dl" onclick="downloadSycHall(&quot;'+s.row_key+'&quot;,this)">'+DL_ICON+' Download Hall Ticket</button>'+
    '<div class="syc-prog" style="display:none;height:7px;background:#E2E8F0;border-radius:4px;margin-top:7px;overflow:hidden;max-width:220px"><div class="syc-bar" style="height:100%;width:0;background:#4F46E5;transition:width .35s"></div></div>'+
    '<div class="syc-msg" style="font-size:11px;color:var(--muted);margin-top:3px"></div></td>'+
    '<td><button title="Delete this SYC student" onclick="deleteSyc(&quot;'+s.row_key+'&quot;,&quot;'+nm+'&quot;)" '+
    'style="background:rgba(239,68,68,.12);color:#DC2626;border:1px solid rgba(239,68,68,.3);border-radius:8px;padding:6px 9px;cursor:pointer;font-weight:600">'+
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14" style="vertical-align:-2px"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button></td></tr>';
}
async function deleteSyc(rowKey,name){
  if(!confirm("Remove SYC student "+name+"?\\n\\nThis moves the student to Trash. You can restore it from Settings → Deleted Students if removed by mistake."))return;
  try{
    await api("/api/syc-delete?row_key="+encodeURIComponent(rowKey),"POST");
    showToast("Moved "+name+" to Trash");
    loadSyc(1);
    try{loadDashboard();}catch(e){}
  }catch(e){showToast(""+e.message);}
}
function loginWarn(s){
  if(!(s.login_failed==1||s.login_failed===true))return "";
  var rm=(s.login_remark||"NIOS login failed — check Reference/Enrollment No & DOB").replace(/</g,"&lt;");
  return '<div style="margin-top:5px;font-size:11.5px;font-weight:600;color:#b91c1c;background:#fee2e2;border:1px solid #fecaca;border-radius:7px;padding:4px 8px;white-space:normal;max-width:260px">'+
    '&#9888; Not sent — '+rm+'</div>';
}
function delBtn(s){
  var nm=(s.student_name||"this student").replace(/[\\"']/g," ");
  var edit='<button title="Edit details (DOB / Reference No etc.) and re-run" '+
    'style="background:#e0e7ff;color:#3730a3;border:1px solid #c7d2fe;border-radius:8px;padding:5px 10px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;margin-right:6px" '+
    'onclick="editStudent(&quot;'+s.row_key+'&quot;)">&#9998; Edit</button>';
  var rem='<button title="Remove this student (moves to Trash — restore from Settings)" '+
    'style="background:#fee2e2;color:#b91c1c;border:1px solid #fecaca;border-radius:8px;padding:5px 10px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap" '+
    'onclick="deleteStudent(&quot;'+s.row_key+'&quot;,&quot;'+nm+'&quot;)">&#128465; Remove</button>';
  return '<div style="display:flex;flex-wrap:wrap;gap:4px">'+edit+rem+'</div>';
}
async function deleteStudent(rowKey,name){
  if(!confirm("Remove "+name+"?\\n\\nThis hides the student from the portal and moves it to Trash. You can restore it from Settings → Deleted Students if removed by mistake."))return;
  try{
    await api("/api/student-delete?row_key="+encodeURIComponent(rowKey),"POST");
    showToast("Moved "+name+" to Trash");
    try{loadStudents(1);}catch(e){}
    try{loadConfirmed(1);}catch(e){}
    try{loadRequired(1);}catch(e){}
    try{loadFailed(1);}catch(e){}
    updateFailedBadge();
    try{loadDashboard();}catch(e){}
  }catch(e){showToast(""+e.message);}
}
// ── Reusable multi-select + bulk delete (works on any table) ──
const SEL={};
function selSet(k){ if(!SEL[k])SEL[k]=new Set(); return SEL[k]; }
function selBox(k,rowKey){
  return '<input type="checkbox" class="selbox-'+k+'" value="'+rowKey+'" onchange="onSel(&quot;'+k+'&quot;,this)"'+(selSet(k).has(rowKey)?' checked':'')+'>';
}
function selHead(k){
  return '<input type="checkbox" id="'+k+'-selall" onclick="selAll(&quot;'+k+'&quot;,this)" title="Select all on this page">';
}
function onSel(k,cb){ const s=selSet(k); if(cb.checked)s.add(cb.value); else s.delete(cb.value); selBar(k); }
function selAll(k,cb){ const s=selSet(k); document.querySelectorAll(".selbox-"+k).forEach(b=>{b.checked=cb.checked; if(cb.checked)s.add(b.value); else s.delete(b.value);}); selBar(k); }
function selClear(k){ selSet(k).clear(); document.querySelectorAll(".selbox-"+k).forEach(b=>b.checked=false); const a=document.getElementById(k+"-selall"); if(a)a.checked=false; selBar(k); }
function selBar(k){ const bar=document.getElementById(k+"-bulkbar"); const n=selSet(k).size; if(bar)bar.style.display=n?"flex":"none"; const c=document.getElementById(k+"-selcount"); if(c)c.textContent=n; }
function bulkBarHtml(k){
  return '<div id="'+k+'-bulkbar" style="display:none;align-items:center;gap:12px;flex-wrap:wrap;background:#FEF2F2;border:1px solid #FECACA;border-radius:10px;padding:10px 14px;margin-bottom:12px">'+
    '<span style="font-weight:700;font-size:13px"><span id="'+k+'-selcount">0</span> selected</span>'+
    '<button class="btn btn-sm" style="background:#DC2626;color:#fff" onclick="bulkDelete(&quot;'+k+'&quot;)">&#128465; Delete selected</button>'+
    '<button class="btn btn-sm" style="background:var(--soft);color:var(--text)" onclick="selClear(&quot;'+k+'&quot;)">Clear</button></div>';
}
async function bulkDelete(k){
  const keys=Array.from(selSet(k));
  if(!keys.length)return;
  if(!confirm("Remove "+keys.length+" selected student(s)?\\n\\nThey move to Trash — you can restore from Settings → Deleted Students."))return;
  try{
    const r=await api("/api/students-delete-bulk","POST",{row_keys:keys});
    showToast("Moved "+(r.deleted||keys.length)+" student(s) to Trash");
    selClear(k);
    refreshAllTables();
  }catch(e){showToast("Error: "+e.message);}
}
function refreshAllTables(){
  ["loadStudents","loadConfirmed","loadRequired","loadFailed"].forEach(fn=>{try{window[fn](1);}catch(e){}});
  try{updateFailedBadge();}catch(e){}
  try{loadDashboard();}catch(e){}
}
const EDIT_FIELDS=["student_name","reference_no","enrollment_no","dob","mobile","alt_mobile","email","class_level","session"];
async function editStudent(rowKey){
  try{
    const s=await api("/api/student-get?row_key="+encodeURIComponent(rowKey));
    document.getElementById("edit-rowkey").value=s.row_key;
    EDIT_FIELDS.forEach(f=>{const el=document.getElementById("edit-"+f);if(el)el.value=s[f]||"";});
    const w=document.getElementById("edit-warn");
    if(s.login_failed==1||s.login_failed===true){
      w.style.display="block";
      w.innerHTML="&#9888; NIOS login failed — "+((s.login_remark||"check Reference/Enrollment No & DOB").replace(/</g,"&lt;"));
    }else{w.style.display="none";}
    document.getElementById("edit-status").textContent="";
    document.getElementById("edit-overlay").style.display="flex";
  }catch(e){showToast(""+e.message);}
}
function closeEdit(){document.getElementById("edit-overlay").style.display="none";}
async function saveEdit(thenRun){
  const rk=document.getElementById("edit-rowkey").value;
  const body={row_key:rk};
  EDIT_FIELDS.forEach(f=>{const el=document.getElementById("edit-"+f);if(el)body[f]=el.value;});
  const st=document.getElementById("edit-status");
  try{
    st.style.color="var(--muted)";st.textContent="Saving…";
    await api("/api/student-edit","POST",body);
    if(thenRun){
      st.textContent="Saved. Re-checking on NIOS (status + login)… this can take ~20–40 sec.";
      await api("/api/student-recheck?row_key="+encodeURIComponent(rk),"POST");
      pollAfterRecheck(rk,0);
    }else{
      st.style.color="var(--success)";st.textContent="Saved!";
      setTimeout(closeEdit,700);
      try{loadStudents(1);}catch(e){}try{loadConfirmed(1);}catch(e){}try{loadRequired(1);}catch(e){}
    }
  }catch(e){st.style.color="var(--danger)";st.textContent=e.message;}
}
async function pollAfterRecheck(rk,n){
  if(n>20){document.getElementById("edit-status").textContent="Still running… you can close this; the row will update shortly.";return;}
  try{
    const s=await api("/api/student-get?row_key="+encodeURIComponent(rk));
    const st=document.getElementById("edit-status");
    const failed=(s.login_failed==1||s.login_failed===true||s.check_failed==1||s.check_failed===true);
    if(failed){
      st.style.color="var(--danger)";
      st.innerHTML="&#9888; Still failing — "+((s.login_remark||"").replace(/</g,"&lt;"))+" Fix the field and run again.";
      const w=document.getElementById("edit-warn");w.style.display="block";w.innerHTML=st.innerHTML;
      try{loadStudents(1);}catch(e){}try{loadConfirmed(1);}catch(e){}try{loadDashboard();}catch(e){}
      try{loadFailed(1);}catch(e){}updateFailedBadge();
      return;
    }
    if((s.current_status||"")&&n>=1){
      st.style.color="var(--success)";
      st.textContent="✓ Done — status: "+s.current_status+(s.current_status==="Admission Confirmed"?" · login OK, documents sent.":".");
      try{loadStudents(1);}catch(e){}try{loadConfirmed(1);}catch(e){}try{loadRequired(1);}catch(e){}try{loadDashboard();}catch(e){}
      try{loadFailed(1);}catch(e){}updateFailedBadge();
      return;
    }
  }catch(e){}
  setTimeout(()=>pollAfterRecheck(rk,n+1),3000);
}
async function downloadSycHall(rowKey,btn){
  if(btn.dataset.busy==="1")return;
  btn.dataset.busy="1";
  const cell=btn.parentElement;
  const prog=cell.querySelector(".syc-prog"),bar=cell.querySelector(".syc-bar"),msg=cell.querySelector(".syc-msg");
  prog.style.display="block";bar.style.background="#4F46E5";bar.style.width="3%";msg.textContent="Fetching from NIOS... (~15 sec)";
  let p=3;const tick=setInterval(function(){p=Math.min(p+(p<70?7:2),92);bar.style.width=p+"%";},900);
  try{
    const r=await fetch(API+"/api/syc-doc?"+new URLSearchParams({row_key:rowKey}).toString(),{headers:{"Authorization":"Bearer "+TOKEN}});
    clearInterval(tick);
    if(!r.ok){let dt="failed";try{dt=(await r.json()).detail||dt;}catch(e){}throw new Error(dt);}
    bar.style.width="100%";msg.textContent="Opening...";
    const blob=await r.blob();
    window.open(URL.createObjectURL(blob),"_blank");
    msg.innerHTML='<span style="color:var(--success)">&#10003; Opened — use "Save as PDF" to download</span>';
  }catch(e){clearInterval(tick);bar.style.background="var(--danger)";bar.style.width="100%";msg.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
  finally{btn.dataset.busy="";setTimeout(function(){prog.style.display="none";},5000);}
}

async function loadRequired(page){
  page=page||1;
  const q=new URLSearchParams({page:page,per_page:perPage,view:"required",
    search:document.getElementById("r-search").value,
    status_filter:(document.getElementById("r-status")?document.getElementById("r-status").value:""),
    session_filter:document.getElementById("r-session").value,
    source_filter:fval("r-source")});
  try{
    const d=await api("/api/students?"+q.toString());
    fillSessions(d.sessions);
    document.getElementById("r-count").textContent=d.total+" students need documents";
    const b=document.getElementById("r-body");
    b.innerHTML=d.students.length?d.students.map((s,i)=>'<tr>'+
      '<td>'+selBox("r",s.row_key)+'</td>'+
      '<td style="color:var(--muted)">'+((page-1)*perPage+i+1)+'</td>'+
      '<td><span class="ref-tag">'+(s.reference_no||"—")+'</span></td>'+
      '<td>'+(s.student_name||"—")+'<div style="margin-top:4px">'+srcBadge(s)+'</div></td><td style="font-size:13px">'+(s.session||"—")+'</td>'+
      '<td style="font-size:13px;color:var(--warn);max-width:420px">'+(s.remark||"(no comment captured)")+'</td>'+
      '<td><button onclick="editStudent(&quot;'+s.row_key+'&quot;)" style="background:#e0e7ff;color:#3730a3;border:1px solid #c7d2fe;border-radius:8px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap">&#9998; Edit</button></td></tr>').join("")
      :'<tr><td colspan="7" class="empty">No pending documents</td></tr>';
    renderPg("r-pg",page,d.pages,"loadRequired");
    const sa=document.getElementById("r-selall");if(sa)sa.checked=false;
    selBar("r");
  }catch(e){showToast(""+e.message);}
}

let fTimer=null;
function debounceFailed(){clearTimeout(fTimer);fTimer=setTimeout(()=>loadFailed(1),400);}
async function loadFailed(page){
  page=page||1;
  const q=new URLSearchParams({page:page,per_page:perPage,
    search:(document.getElementById("f-search")?document.getElementById("f-search").value:""),
    source:fval("f-source")});
  try{
    const d=await api("/api/failed-students?"+q.toString());
    document.getElementById("f-count").textContent=d.total+" student(s) need data correction";
    const b=document.getElementById("f-body");
    b.innerHTML=d.students.length?d.students.map(s=>{
      var ref=(s.reference_no||s.enrollment_no||"—");
      var nm=(s.student_name||"this student").replace(/[\\"']/g," ");
      var rm=(s.login_remark||"NIOS login failed — check Reference/Enrollment No & DOB").replace(/</g,"&lt;");
      return '<tr>'+
        '<td>'+selBox("f",s.row_key)+'</td>'+
        '<td>'+(s.student_name||"—")+'<div style="margin-top:4px">'+srcBadge(s)+
          ((s.check_count&&s.check_count>1)?' <span style="font-size:10.5px;color:#6b7280;background:#f3f4f6;border-radius:6px;padding:1px 6px;font-weight:600">checked '+s.check_count+'x</span>':'')+
          '</div></td>'+
        '<td><span class="ref-tag">'+ref+'</span></td>'+
        '<td style="font-size:13px">'+(s.session||"—")+'</td>'+
        '<td style="font-size:12px;color:#b91c1c;font-weight:600;max-width:300px">'+rm+'</td>'+
        '<td><div style="display:flex;gap:6px;flex-wrap:wrap">'+
          ((rm.indexOf("DIFFERENT student")>=0)?'<button onclick="markNameOk(&quot;'+s.row_key+'&quot;,&quot;'+nm+'&quot;)" style="background:#d1fae5;color:#065f46;border:1px solid #a7f3d0;border-radius:8px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer" title="The name/Reference is actually correct — dismiss this warning">&#10003; Name correct</button>':'')+
          '<button onclick="editStudent(&quot;'+s.row_key+'&quot;)" style="background:#e0e7ff;color:#3730a3;border:1px solid #c7d2fe;border-radius:8px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer">Edit &amp; fix</button>'+
          '<button onclick="deleteStudent(&quot;'+s.row_key+'&quot;,&quot;'+nm+'&quot;)" style="background:#fee2e2;color:#b91c1c;border:1px solid #fecaca;border-radius:8px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer">Remove</button>'+
        '</div></td></tr>';
    }).join("")
      :'<tr><td colspan="6" class="empty" style="color:var(--success)">No failed students — all clear!</td></tr>';
    renderPg("f-pg",page,d.pages,"loadFailed");
    const sa=document.getElementById("f-selall");if(sa)sa.checked=false;
    selBar("f");
  }catch(e){showToast(""+e.message);}
}
async function markNameOk(rk,nm){
  if(!confirm("Mark this student's name/Reference as CORRECT?\\n\\nOnly do this after checking on the portal that the Reference No genuinely belongs to "+nm+". This dismisses the wrong-reference warning."))return;
  try{
    const fd=new FormData();fd.append("row_key",rk);
    const r=await fetch("/api/mark-name-verified",{method:"POST",headers:{Authorization:"Bearer "+TOKEN},body:fd});
    const d=await r.json();
    if(!r.ok)throw new Error(d.detail||"Failed");
    showToast(d.confirmed?"Marked correct \u2014 moved to Confirmed":"Marked correct");
    loadFailed(1);updateNavCounts();
  }catch(e){showToast(""+e.message);}
}
async function runAllFailed(btn){
  if(!confirm("Re-check ALL failed students now?\\n\\nEach one is re-run with auto-retry (transient errors) and an automatic DOB date/month swap (formatting fixes). This uses CapSolver credits for every student in the list."))return;
  const old=btn?btn.textContent:"";
  if(btn){btn.disabled=true;btn.textContent="Starting…";}
  try{
    const r=await api("/api/run-now-failed","POST");
    showToast(r.message||"Re-checking failed students…");
  }catch(e){showToast(""+e.message);}
  finally{if(btn){btn.disabled=false;btn.textContent=old;}}
}
let uTimer=null;
function debounceUnknown(){clearTimeout(uTimer);uTimer=setTimeout(()=>loadUnknown(1),400);}
async function loadUnknown(page){
  page=page||1;
  const q=new URLSearchParams({page:page,per_page:perPage,
    search:(document.getElementById("u-search")?document.getElementById("u-search").value:""),
    session_filter:fval("u-session")});
  try{
    const d=await api("/api/unknown-students?"+q.toString());
    if(d.sessions)fillSessions(d.sessions);
    document.getElementById("u-count").textContent=d.total+" student(s) with Unknown status";
    const b=document.getElementById("u-body");
    b.innerHTML=d.students.length?d.students.map(s=>{
      var ref=(s.reference_no||s.enrollment_no||"\u2014");
      var nm=(s.student_name||"this student").replace(/[\\"']/g," ");
      var rm=(s.login_remark||"NIOS returned no recognisable status.").replace(/</g,"&lt;");
      return '<tr>'+
        '<td>'+(s.student_name||"\u2014")+'<div style="margin-top:4px">'+srcBadge(s)+
          ((s.check_count&&s.check_count>1)?' <span style="font-size:10.5px;color:#6b7280;background:#f3f4f6;border-radius:6px;padding:1px 6px;font-weight:600">checked '+s.check_count+'x</span>':'')+
          '</div></td>'+
        '<td><span class="ref-tag">'+ref+'</span></td>'+
        '<td style="font-size:13px">'+(s.session||"\u2014")+'</td>'+
        '<td style="font-size:12px;color:#b45309;font-weight:500;max-width:320px">'+rm+'</td>'+
        '<td style="font-size:12px;color:var(--muted)">'+(s.last_checked||"\u2014")+'</td>'+
        '<td><div style="display:flex;gap:6px;flex-wrap:wrap">'+
          '<button onclick="editStudent(&quot;'+s.row_key+'&quot;)" style="background:#e0e7ff;color:#3730a3;border:1px solid #c7d2fe;border-radius:8px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer">Edit &amp; fix</button>'+
          '<button onclick="deleteStudent(&quot;'+s.row_key+'&quot;,&quot;'+nm+'&quot;)" style="background:#fee2e2;color:#b91c1c;border:1px solid #fecaca;border-radius:8px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer">Remove</button>'+
        '</div></td></tr>';
    }).join("")
      :'<tr><td colspan="6" class="empty" style="color:var(--success)">No Unknown students \u2014 all clear!</td></tr>';
    renderUnknownPg(page,d.pages,d.total);
  }catch(e){showToast(""+e.message);}
}
function renderUnknownPg(page,total,totalRows){
  const el=document.getElementById("u-pg");if(!el)return;
  if(!total||total<=0){el.innerHTML="";return;}
  let ctrl='<div class="pg-controls">';
  ctrl+='<button onclick="loadUnknown('+(page-1)+')" '+(page<=1?"disabled":"")+'>\u2039 Prev</button>';
  const start=Math.max(1,page-2),end=Math.min(total,page+2);
  for(let i=start;i<=end;i++)ctrl+='<button class="'+(i===page?'active':'')+'" onclick="loadUnknown('+i+')">'+i+'</button>';
  ctrl+='<button onclick="loadUnknown('+(page+1)+')" '+(page>=total?"disabled":"")+'>Next \u203a</button></div>';
  const sel='<div class="perpage">'+(totalRows!=null?'<span>'+totalRows+' students</span> \u00b7 ':'')+
    'Per page: <select onchange="perPage=parseInt(this.value);loadUnknown(1)">'+
    [10,20,50,100].map(n=>'<option value="'+n+'" '+(n===perPage?"selected":"")+'>'+n+'</option>').join("")+
    '</select></div>';
  el.innerHTML=ctrl+sel;
}
async function runAllUnknown(btn){
  if(!confirm("Re-check ALL Unknown students now?\\n\\nEach is re-run with auto-retry. Uses CapSolver credits for every student in this list."))return;
  const old=btn?btn.textContent:"";
  if(btn){btn.disabled=true;btn.textContent="Starting\u2026";}
  try{
    const r=await api("/api/run-now-unknown","POST");
    showToast(r.message||"Re-checking Unknown students\u2026");
  }catch(e){showToast(""+e.message);}
  finally{if(btn){btn.disabled=false;btn.textContent=old;}}
}
function _setNavBadge(id,n){
  const b=document.getElementById(id);if(!b)return;
  if(n>0){b.textContent=n;b.style.display="inline-block";b.classList.add("has");}
  else{b.style.display="none";b.classList.remove("has");}
}
async function updateNavCounts(){
  try{
    const d=await api("/api/nav-count");
    _setNavBadge("nav-students-badge",d.students);
    _setNavBadge("nav-confirmed-badge",d.confirmed);
    _setNavBadge("nav-required-badge",d.required);
    _setNavBadge("nav-syc-badge",d.syc);
    _setNavBadge("nav-failed-badge",d.failed);
    _setNavBadge("nav-unknown-badge",d.unknown);
  }catch(e){}
}
async function updateFailedBadge(){return updateNavCounts();}   // back-compat
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

let histPerPage=10;
function fmtDT(d){
  const p=n=>String(n).padStart(2,"0");
  return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds());
}
function dtLocalToStr(v){ // "2026-06-11T18:30" -> "2026-06-11 18:30:00"
  if(!v)return "";
  v=v.replace("T"," ");
  return v.length===16?v+":00":v;
}
function onHistPreset(){
  const custom=document.getElementById("h-preset").value==="custom";
  document.getElementById("h-custom").style.display=custom?"flex":"none";
  if(custom){
    const f=document.getElementById("h-from"),t=document.getElementById("h-to");
    if(f&&!f.value){const y=new Date();y.setDate(y.getDate()-1);y.setHours(18,30,0,0);f.value=toLocalInput(y);}
    if(t&&!t.value){t.value=toLocalInput(new Date());}
  }else loadHistory(1);
}
function toLocalInput(d){const p=n=>String(n).padStart(2,"0");
  return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+"T"+p(d.getHours())+":"+p(d.getMinutes());}
function histRange(){
  const preset=fval("h-preset");
  const now=new Date();
  let from="",to="";
  if(preset==="today"){const s=new Date(now);s.setHours(0,0,0,0);from=fmtDT(s);to=fmtDT(now);}
  else if(preset==="yesterday"){const s=new Date(now);s.setDate(s.getDate()-1);s.setHours(0,0,0,0);
    const e=new Date(s);e.setHours(23,59,59,0);from=fmtDT(s);to=fmtDT(e);}
  else if(preset==="24h"){const s=new Date(now.getTime()-86400000);from=fmtDT(s);to=fmtDT(now);}
  else if(preset==="custom"){from=dtLocalToStr(fval("h-from"));to=dtLocalToStr(fval("h-to"));}
  return {from,to};
}
let _histSearchT=null;
function histSearchDebounced(){clearTimeout(_histSearchT);_histSearchT=setTimeout(()=>loadHistory(1),350);}
async function loadHistory(page){
  page=page||1;
  const rg=histRange();
  const q=new URLSearchParams({page:page,per_page:histPerPage,from_dt:rg.from,to_dt:rg.to,
    status:fval("h-status"),source:fval("h-source"),search:fval("h-search")});
  try{
    const d=await api("/api/history?"+q.toString());
    const items=d.items||[];
    const cnt=document.getElementById("h-count");
    if(cnt)cnt.textContent=d.total+" change(s)"+(rg.from?" in selected range":"");
    document.getElementById("h-body").innerHTML=items.length?items.map(x=>'<tr>'+
      '<td><span class="ref-tag">'+(x.reference_no||"—")+'</span></td>'+
      '<td>'+(x.student_name||"—")+'<div style="margin-top:4px">'+srcBadge(x)+'</div></td>'+
      '<td>'+badge(x.old_status)+'</td><td>'+badge(x.new_status)+'</td>'+
      '<td style="font-size:12px;color:var(--muted)">'+x.changed_at+'</td>'+
      '<td><button title="Delete this history entry" style="background:#fee2e2;color:#b91c1c;border:1px solid #fecaca;border-radius:8px;padding:4px 9px;font-size:11px;font-weight:600;cursor:pointer" onclick="deleteHistory('+x.id+')">&#128465;</button></td></tr>').join("")
      :'<tr><td colspan="6" class="empty">No changes in this range</td></tr>';
    renderHistPg(d.page,d.pages,d.total);
  }catch(e){showToast(""+e.message);}
}
async function deleteHistory(id){
  if(!confirm("Delete this history entry?"))return;
  try{await api("/api/history-delete?id="+id,"POST");showToast("Deleted");loadHistory(1);}
  catch(e){showToast(""+e.message);}
}
async function clearHistory(){
  if(!confirm("Clear ALL status-change history?\\n\\nThis permanently deletes every history entry. Students are NOT affected."))return;
  try{const r=await api("/api/history-clear","POST");showToast("Cleared "+(r.deleted||0)+" entries");loadHistory(1);}
  catch(e){showToast(""+e.message);}
}
async function exportHistory(){
  const rg=histRange();
  const q=new URLSearchParams({from_dt:rg.from,to_dt:rg.to,status:fval("h-status"),source:fval("h-source")});
  try{
    showToast("Preparing Excel...");
    const r=await fetch(API+"/api/export-history?"+q.toString(),{headers:{Authorization:"Bearer "+TOKEN}});
    if(!r.ok){showToast("Export failed");return;}
    const blob=await r.blob();const url=URL.createObjectURL(blob);
    const a=document.createElement("a");a.href=url;a.download="nios_change_history.xlsx";
    document.body.appendChild(a);a.click();a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1500);showToast("Excel downloaded");
  }catch(e){showToast("Error: "+e.message);}
}
function renderHistPg(page,total,totalRows){
  const el=document.getElementById("h-pg");if(!el)return;
  if(!total||total<=0){el.innerHTML="";return;}
  let ctrl='<div class="pg-controls">';
  ctrl+='<button onclick="loadHistory('+(page-1)+')" '+(page<=1?"disabled":"")+'>‹ Prev</button>';
  const start=Math.max(1,page-2),end=Math.min(total,page+2);
  for(let i=start;i<=end;i++)ctrl+='<button class="'+(i===page?'active':'')+'" onclick="loadHistory('+i+')">'+i+'</button>';
  ctrl+='<button onclick="loadHistory('+(page+1)+')" '+(page>=total?"disabled":"")+'>Next ›</button></div>';
  const sel='<div class="perpage">'+(totalRows!=null?'<span>'+totalRows+' changes</span> · ':'')+
    'Per page: <select onchange="histPerPage=parseInt(this.value);loadHistory(1)">'+
    [10,20,30,50,100].map(n=>'<option value="'+n+'" '+(n===histPerPage?"selected":"")+'>'+n+'</option>').join("")+
    '</select></div>';
  el.innerHTML=ctrl+sel;
}
async function loadRunLogs(){
  const sel=document.getElementById("rl-limit");
  const lim=sel?parseInt(sel.value):50;
  try{const l=await api("/api/run-logs?limit="+lim);renderRuns(l,"rl-body");}catch(e){showToast(""+e.message);}
}
let transPerPage=10,_trSearchT=null;
function trSearchDebounced(){clearTimeout(_trSearchT);_trSearchT=setTimeout(()=>loadTransfers(1),350);}
async function loadTransfers(page){
  page=page||1;
  const q=new URLSearchParams({page:page,per_page:transPerPage,
    search:fval("tr-search"),mode:fval("tr-mode")});
  try{
    const d=await api("/api/transfers?"+q.toString());
    const items=d.items||[];
    const cnt=document.getElementById("tr-count");
    if(cnt)cnt.textContent=d.total+" record(s) transferred Tracker \u2192 Portal";
    document.getElementById("tr-body").innerHTML=items.length?items.map(x=>{
      var mode=(x.mode==="manual")
        ?'<span style="font-size:11px;font-weight:600;color:#3730a3;background:#e0e7ff;border-radius:6px;padding:2px 8px">Manual</span>'
        :'<span style="font-size:11px;font-weight:600;color:#065f46;background:#d1fae5;border-radius:6px;padding:2px 8px">Auto</span>';
      return '<tr>'+
        '<td>'+(x.student_name||"\u2014")+'<div style="margin-top:3px"><span style="font-size:10.5px;color:#6b7280">'+(x.mobile||"")+'</span></div></td>'+
        '<td><span class="ref-tag">'+(x.reference_no||x.enrollment_no||"\u2014")+'</span></td>'+
        '<td style="font-size:13px">'+(x.session||"\u2014")+'</td>'+
        '<td>'+badge(x.old_status||"\u2014")+'</td>'+
        '<td>'+badge(x.new_status||"\u2014")+'</td>'+
        '<td style="font-size:12px;color:var(--muted)">'+(x.transferred_at||"")+'</td>'+
        '<td>'+mode+'</td></tr>';
    }).join("")
      :'<tr><td colspan="7" class="empty">No transfers yet. When a Tracker student is matched to Portal during a run, it appears here.</td></tr>';
    renderTransPg(d.page,d.pages,d.total);
  }catch(e){showToast(""+e.message);}
}
function renderTransPg(page,total,totalRows){
  const el=document.getElementById("tr-pg");if(!el)return;
  if(!total||total<=0){el.innerHTML="";return;}
  let ctrl='<div class="pg-controls">';
  ctrl+='<button onclick="loadTransfers('+(page-1)+')" '+(page<=1?"disabled":"")+'>\u2039 Prev</button>';
  const start=Math.max(1,page-2),end=Math.min(total,page+2);
  for(let i=start;i<=end;i++)ctrl+='<button class="'+(i===page?'active':'')+'" onclick="loadTransfers('+i+')">'+i+'</button>';
  ctrl+='<button onclick="loadTransfers('+(page+1)+')" '+(page>=total?"disabled":"")+'>Next \u203a</button></div>';
  const sel='<div class="perpage">'+(totalRows!=null?'<span>'+totalRows+' records</span> \u00b7 ':'')+
    'Per page: <select onchange="transPerPage=parseInt(this.value);loadTransfers(1)">'+
    [10,20,30,50,100].map(n=>'<option value="'+n+'" '+(n===transPerPage?"selected":"")+'>'+n+'</option>').join("")+
    '</select></div>';
  el.innerHTML=ctrl+sel;
}
let staleKeys=[];
async function findStalePortal(btn){
  const box=document.getElementById("stale-box");
  if(btn){btn.disabled=true;btn.textContent="Checking\u2026";}
  if(box)box.innerHTML='<div style="color:var(--muted);font-size:13px;padding:8px 0">Fetching the live portal and comparing\u2026</div>';
  try{
    const r=await api("/api/portal-stale");
    if(r.ok===false){box.innerHTML='<div style="color:var(--warn);font-size:13px">'+(r.message||"Could not check right now.")+'</div>';return;}
    staleKeys=(r.stale||[]).map(s=>s.row_key);
    if(!r.stale||!r.stale.length){
      box.innerHTML='<div style="color:var(--success);font-size:13.5px;font-weight:600;padding:8px 0">&#10003; All clean — every tracked MVS Portal student is still on the live portal. (Compared against '+r.fetched+' live records.)</div>';
      return;
    }
    var rows=r.stale.map((s,i)=>'<tr>'+
      '<td><input type="checkbox" class="stale-cb" value="'+s.row_key+'" checked onchange="updateStaleCount()"></td>'+
      '<td style="font-size:12px;color:var(--muted)">'+(i+1)+'</td>'+
      '<td>'+(s.student_name||"\u2014")+'</td>'+
      '<td><span class="ref-tag">'+(s.reference_no||"\u2014")+'</span></td>'+
      '<td style="font-size:12.5px">'+(s.session||"\u2014")+'</td>'+
      '<td style="font-size:12.5px">'+(s.status||"\u2014")+'</td>'+
      '<td style="font-size:12px;color:var(--muted)">'+(s.last_checked||"\u2014")+'</td>'+
      '</tr>').join("");
    box.innerHTML='<div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:9px;padding:10px 13px;margin-bottom:10px;font-size:13.5px;color:#9a3412">'+
        'Found <b>'+r.stale.length+'</b> old portal student(s) no longer on the live portal (compared against '+r.fetched+' live records). '+
        '<b>Tick the ones you want</b>, then either move them to the Tracker (keeps the data) or to Trash.</div>'+
      '<div style="overflow-x:auto"><table><thead><tr>'+
        '<th><input type="checkbox" id="stale-all" checked onclick="toggleStaleAll(this)"></th>'+
        '<th>#</th><th>Student</th><th>Reference / Enroll</th><th>Session</th><th>Last Status</th><th>Last Checked</th>'+
        '</tr></thead><tbody>'+rows+'</tbody></table></div>'+
      '<div style="margin-top:13px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">'+
        '<button class="btn btn-sm" style="background:#2563eb;color:#fff" onclick="transferStaleToTracker(this)">&#8594; Transfer selected to Tracker</button>'+
        '<button class="btn btn-sm" style="background:#dc2626;color:#fff" onclick="removeStalePortal(this)">&#128465; Move selected to Trash</button>'+
        '<span id="stale-count" style="font-size:12.5px;color:var(--muted)"></span>'+
      '</div>'+
      '<div style="font-size:12px;color:var(--muted);margin-top:7px">&#8594; <b>Transfer to Tracker</b> keeps the record (re-checked as Tracker data, no longer linked to the portal). &#128465; <b>Trash</b> is recoverable from Settings.</div>';
    updateStaleCount();
  }catch(e){box.innerHTML='<div style="color:var(--danger);font-size:13px">'+e.message+'</div>';}
  finally{if(btn){btn.disabled=false;btn.textContent="Find old portal data";}}
}
function getStaleSelected(){
  return Array.from(document.querySelectorAll(".stale-cb:checked")).map(c=>c.value);
}
function updateStaleCount(){
  const el=document.getElementById("stale-count");if(!el)return;
  el.textContent=getStaleSelected().length+" selected";
}
function toggleStaleAll(cb){
  document.querySelectorAll(".stale-cb").forEach(c=>{c.checked=cb.checked;});
  updateStaleCount();
}
async function transferStaleToTracker(btn){
  const keys=getStaleSelected();
  if(!keys.length){showToast("Select at least one student first");return;}
  if(!confirm("Move these "+keys.length+" student(s) from MVS Portal to MVS Tracker?\\n\\nThe records are kept and re-checked as Tracker data — they're just no longer linked to the live portal."))return;
  if(btn){btn.disabled=true;btn.textContent="Moving\u2026";}
  try{
    const r=await api("/api/portal-to-tracker","POST",{row_keys:keys});
    showToast("Moved "+(r.moved||keys.length)+" student(s) to MVS Tracker");
    document.getElementById("stale-box").innerHTML='<div style="color:var(--success);font-size:13.5px;font-weight:600;padding:8px 0">&#10003; Done — '+(r.moved||0)+' record(s) moved to MVS Tracker. They are kept and will be re-checked as Tracker data.</div>';
    try{loadDashboard();}catch(e){}
  }catch(e){showToast("Error: "+e.message);if(btn){btn.disabled=false;btn.textContent="\u2192 Transfer selected to Tracker";}}
}
async function removeStalePortal(btn){
  const keys=getStaleSelected();
  if(!keys.length){showToast("Select at least one student first");return;}
  if(!confirm("Move these "+keys.length+" student(s) to Trash?\\n\\nThey can be restored anytime from Settings \u2192 Trash."))return;
  if(btn){btn.disabled=true;btn.textContent="Removing\u2026";}
  try{
    const r=await api("/api/students-delete-bulk","POST",{row_keys:keys});
    showToast("Moved "+(r.deleted||keys.length)+" student(s) to Trash");
    document.getElementById("stale-box").innerHTML='<div style="color:var(--success);font-size:13.5px;font-weight:600;padding:8px 0">&#10003; Done — '+(r.deleted||0)+' record(s) moved to Trash.</div>';
    try{loadDashboard();}catch(e){}
  }catch(e){showToast("Error: "+e.message);if(btn){btn.disabled=false;btn.textContent="Move selected to Trash";}}
}
async function syncTransfers(btn){
  if(!confirm("Push the current status of every matched (Both) student to MVS Portal now?\\n\\nThis re-sends each Both-student's latest NIOS status + document links to the portal and logs them as Manual transfers."))return;
  const old=btn?btn.textContent:"";
  if(btn){btn.disabled=true;btn.textContent="Syncing\u2026";}
  try{
    const r=await api("/api/transfer-sync","POST");
    showToast(r.message||"Synced to Portal");
    loadTransfers(1);
  }catch(e){showToast(""+e.message);}
  finally{if(btn){btn.disabled=false;btn.textContent=old;}}
}

const drop=document.getElementById("drop");
["dragover","dragenter"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add("drag");}));
["dragleave","drop"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove("drag");}));
drop.addEventListener("drop",e=>{if(e.dataTransfer.files[0])handleFile(e.dataTransfer.files[0]);});
function handleFile(file){
  if(!file)return;
  const st=document.getElementById("upload-status");
  const sm=document.getElementById("upload-summary");
  const pv=document.getElementById("upload-preview");
  sm.innerHTML="";pv.innerHTML="";
  st.innerHTML='<div class="up-prog"><div class="up-prog-top"><span>Uploading…</span>'+
    '<span id="up-pct">0%</span></div><div class="up-track"><div class="up-fill" id="up-fill"></div></div>'+
    '<div class="up-eta" id="up-eta"></div></div>';
  const fd=new FormData();fd.append("file",file);
  const xhr=new XMLHttpRequest();
  const startT=Date.now();
  xhr.open("POST",API+"/api/upload-excel");
  xhr.setRequestHeader("Authorization","Bearer "+TOKEN);
  xhr.upload.onprogress=function(e){
    if(!e.lengthComputable)return;
    const pct=Math.round(e.loaded*100/e.total);
    const pe=document.getElementById("up-pct"),pf=document.getElementById("up-fill"),pet=document.getElementById("up-eta");
    if(pe)pe.textContent=pct+"%";
    if(pf)pf.style.width=pct+"%";
    const elapsed=(Date.now()-startT)/1000;
    const speed=e.loaded/Math.max(elapsed,0.001);
    const eta=speed>0?(e.total-e.loaded)/speed:0;
    if(pet)pet.textContent=pct<100?("~"+fmtEta(eta)+" remaining · "+fmtBytes(e.loaded)+" / "+fmtBytes(e.total)):"Processing file on server…";
  };
  xhr.onload=function(){
    let d={};try{d=JSON.parse(xhr.responseText);}catch(e){}
    if(xhr.status<200||xhr.status>=300){st.innerHTML='<div style="color:var(--danger)">'+(d.detail||"Upload failed")+'</div>';return;}
    st.innerHTML='<div style="color:var(--success);font-weight:600">'+(d.message||"Uploaded")+'</div>'+
      '<div style="margin-top:12px"><button class="btn btn-success btn-sm" onclick="runUploaded(this)">Run Check Now (uploaded only)</button>'+
      '<div style="font-size:11.5px;color:var(--muted);margin-top:7px">This checks <b>only the '+(d.total||0)+' student(s) in this sheet</b>. To run all data (MVS Tracker + MVS Portal), use <b>Run Now</b> on the Dashboard.</div></div>';
    if(d.parse_error){sm.innerHTML='<div style="color:var(--danger);font-size:13px">Preview error: '+d.parse_error+'</div>';}
    else{renderUploadSummary(d);renderUploadPreview(d.preview||[]);}
    showToast("Excel uploaded — "+(d.unique||0)+" new students");
  };
  xhr.onerror=function(){st.innerHTML='<div style="color:var(--danger)">Upload error — try again</div>';};
  xhr.send(fd);
}
const altDrop=document.getElementById("alt-drop");
if(altDrop){
  ["dragover","dragenter"].forEach(ev=>altDrop.addEventListener(ev,e=>{e.preventDefault();altDrop.classList.add("drag");}));
  ["dragleave","drop"].forEach(ev=>altDrop.addEventListener(ev,e=>{e.preventDefault();altDrop.classList.remove("drag");}));
  altDrop.addEventListener("drop",e=>{if(e.dataTransfer.files[0])handleAltFile(e.dataTransfer.files[0]);});
}
function handleAltFile(file){
  if(!file)return;
  const st=document.getElementById("alt-status");
  st.innerHTML='<div class="up-prog"><div class="up-prog-top"><span>Uploading alternate numbers…</span>'+
    '<span id="alt-pct">0%</span></div><div class="up-track"><div class="up-fill" id="alt-fill"></div></div>'+
    '<div class="up-eta" id="alt-eta"></div></div>';
  const fd=new FormData();fd.append("file",file);
  const xhr=new XMLHttpRequest();
  const startT=Date.now();
  xhr.open("POST",API+"/api/upload-alt-numbers");
  xhr.setRequestHeader("Authorization","Bearer "+TOKEN);
  xhr.upload.onprogress=function(e){
    if(!e.lengthComputable)return;
    const pct=Math.round(e.loaded*100/e.total);
    const pe=document.getElementById("alt-pct"),pf=document.getElementById("alt-fill"),pet=document.getElementById("alt-eta");
    if(pe)pe.textContent=pct+"%";
    if(pf)pf.style.width=pct+"%";
    const elapsed=(Date.now()-startT)/1000;
    const speed=e.loaded/Math.max(elapsed,0.001);
    const eta=speed>0?(e.total-e.loaded)/speed:0;
    if(pet)pet.textContent=pct<100?("~"+fmtEta(eta)+" remaining · "+fmtBytes(e.loaded)+" / "+fmtBytes(e.total)):"Matching references & updating numbers on the server…";
  };
  xhr.onload=function(){
    let d={};try{d=JSON.parse(xhr.responseText);}catch(e){}
    if(xhr.status<200||xhr.status>=300){st.innerHTML='<div style="color:var(--danger)">'+(d.detail||"Upload failed")+'</div>';return;}
    st.innerHTML='<div style="color:var(--success);font-weight:600">'+(d.message||"Done")+'</div>'+
      '<div class="up-stats" style="margin-top:12px">'+
        '<div class="up-stat new"><div class="us-lbl">Updated</div><div class="us-val">'+(d.updated||0)+'</div></div>'+
        '<div class="up-stat sim"><div class="us-lbl">Sent to alt (confirmed)</div><div class="us-val">'+(d.to_send||0)+'</div></div>'+
        '<div class="up-stat dup"><div class="us-lbl">Not found</div><div class="us-val">'+(d.not_found||0)+'</div></div>'+
      '</div>';
    showToast("Alternate numbers — updated "+(d.updated||0));
    const fi=document.getElementById("alt-file-input");if(fi)fi.value="";
  };
  xhr.onerror=function(){st.innerHTML='<div style="color:var(--danger)">Upload error — try again</div>';};
  xhr.send(fd);
}
async function downloadAltSample(){
  try{
    const r=await fetch(API+"/api/sample-alt-sheet",{headers:{Authorization:"Bearer "+TOKEN}});
    if(!r.ok){showToast("Sample download failed ("+r.status+")");return;}
    const blob=await r.blob();const url=URL.createObjectURL(blob);
    const a=document.createElement("a");a.href=url;a.download="MVS_alternate_numbers_sample.xlsx";
    document.body.appendChild(a);a.click();a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1500);
    showToast("Sample sheet downloaded");
  }catch(e){showToast("Error: "+e.message);}
}
function fmtBytes(b){if(b<1024)return b+" B";if(b<1048576)return (b/1024).toFixed(1)+" KB";return (b/1048576).toFixed(1)+" MB";}
function fmtEta(s){s=Math.ceil(s);if(s<1)return "0s";if(s<60)return s+"s";const m=Math.floor(s/60),ss=s%60;return m+"m "+ss+"s";}
function renderUploadSummary(d){
  const sm=document.getElementById("upload-summary");
  const sim=d.similar||0;
  sm.innerHTML='<div class="up-stats">'+
    '<div class="up-stat"><div class="us-lbl">Total in list</div><div class="us-val">'+(d.total||0)+'</div></div>'+
    '<div class="up-stat new"><div class="us-lbl">New students</div><div class="us-val">'+(d.unique||0)+'</div></div>'+
    '<div class="up-stat dup"><div class="us-lbl">Already tracked</div><div class="us-val">'+(d.duplicates||0)+'</div></div>'+
    (sim>0?'<div class="up-stat sim"><div class="us-lbl">Similar (will run)</div><div class="us-val">'+sim+'</div></div>':'')+
    '</div>'+
    '<div style="font-size:12px;color:var(--muted);margin-top:8px;line-height:1.6">'+
      '<b>'+(d.unique||0)+'</b> new student(s) will be added. '+
      ((d.duplicates||0)>0?'<b>'+d.duplicates+'</b> true duplicate(s) (<b>same reference number</b>) — merged & re-checked, <b>not</b> removed. ':'')+
      (sim>0?'<b>'+sim+'</b> share a phone/email with another record but have a <b>different reference</b> — so they are separate students (e.g. siblings) and <b>WILL run</b>. ':'')+
      'Nothing is deleted on upload.</div>';
}
function renderUploadPreview(rows){
  const pv=document.getElementById("upload-preview");
  if(!rows.length){pv.innerHTML="";return;}
  const head='<tr><th>#</th><th>Name</th><th>Reference</th><th>Mobile</th><th>Session</th><th>Status / Matches</th></tr>';
  const body=rows.map((s,i)=>{
    var matchLine="";
    if((s.dup||s.similar)&&(s.match_name||s.match_ref)){
      matchLine='<div style="font-size:11px;color:var(--muted);margin-top:3px">matches: <b>'+(s.match_name||"—")+'</b>'+
        (s.match_ref?' &middot; Ref <span style="font-family:monospace">'+s.match_ref+'</span>':'')+'</div>';
    }
    var tag;
    if(s.dup){
      tag='<span class="dup-tag" title="Same reference number as an existing student — merged & re-checked (not removed).">Duplicate &middot; '+(s.dup_basis||"")+'</span>';
    }else if(s.similar){
      tag='<span class="sim-tag" title="Different reference but same '+s.similar+'. Treated as a SEPARATE student and WILL run. Check the match to confirm.">Similar &middot; '+s.similar+'</span>';
    }else{
      tag='<span class="new-tag">New</span>';
    }
    return '<tr'+(s.dup?' style="background:var(--dup-bg)"':(s.similar?' style="background:#FEF9E7"':''))+'>'+
    '<td style="color:var(--muted)">'+(i+1)+'</td>'+
    '<td>'+(s.student_name||"—")+'</td>'+
    '<td><span class="ref-tag">'+(s.reference_no||s.enrollment_no||"—")+'</span></td>'+
    '<td style="font-size:12px">'+(s.mobile||"—")+'</td>'+
    '<td style="font-size:12px">'+(s.session||"—")+'</td>'+
    '<td>'+tag+matchLine+'</td></tr>';
  }).join("");
  pv.innerHTML='<div class="prev-head">Preview — '+rows.length+' rows (scroll to see all)</div>'+
    '<div style="font-size:11.5px;color:var(--muted);margin:2px 0 8px;line-height:1.5">&#9888; <b>Similar</b> rows share a phone/email with another student but have a <b>different reference number</b>, so they are <b>NOT</b> duplicates — they run as separate students (e.g. siblings). Check &ldquo;matches&rdquo; to confirm. A true <b>Duplicate</b> has the <b>same reference number</b>. To drop a real duplicate, delete it from the Students list after upload (select &rarr; Delete).</div>'+
    '<div class="prev-box"><table>'+head+body+'</table></div>';
}
async function downloadExcel(){
  const r=await fetch(API+"/api/download-excel",{headers:{"Authorization":"Bearer "+TOKEN}});
  if(!r.ok){showToast("No Excel found. Run a check first.");return;}
  const blob=await r.blob();const url=URL.createObjectURL(blob);
  const a=document.createElement("a");a.href=url;a.download="nios_status_updated.xlsx";a.click();
  URL.revokeObjectURL(url);
}
async function saveUploadSource(){
  try{await api("/api/source-override?value="+encodeURIComponent(fval("upload-source")),"POST");
    showToast("Data source set for next run");}catch(e){showToast(""+e.message);}
}
async function loadUploadSource(){
  try{const d=await api("/api/source-override");const sel=document.getElementById("upload-source");if(sel)sel.value=d.value||"";}catch(e){}
}
async function loadSourceCounts(){
  try{
    const d=await api("/api/source-counts");
    const el=document.getElementById("src-counts");
    if(el)el.innerHTML='Current: <b>MVS Portal</b> &mdash; '+(d.mvs_portal||0)+' &middot; <b>MVS Tracker</b> &mdash; '+(d.mvs_tracker||0);
  }catch(e){}
}
function srcLabel(s){return s==="mvs_portal"?"MVS Portal":"MVS Tracker";}
async function moveAllSource(btn){
  const frm=fval("src-from"), to=fval("src-to");
  if(frm===to){showToast("Pick two different data types");return;}
  if(!confirm("Move ALL students from "+srcLabel(frm)+" to "+srcLabel(to)+"?\\n\\nOnly the data-type tag changes — no student is deleted or lost."))return;
  const st=document.getElementById("src-status");
  if(btn){btn.disabled=true;btn.textContent="Moving…";}
  try{
    const r=await api("/api/change-source-bulk","POST",{from_source:frm,to_source:to});
    if(st)st.innerHTML='<span style="color:var(--success)">&#10003; Moved '+(r.moved||0)+' student(s) to '+srcLabel(to)+'.</span>';
    loadSourceCounts();
    refreshAllTables();
  }catch(e){if(st)st.innerHTML='<span style="color:var(--danger)">Error: '+e.message+'</span>';}
  finally{if(btn){btn.disabled=false;btn.textContent="Move all";}}
}
async function changeSelectedSource(sel){
  const to=sel.value; if(!to){return;}
  const keys=[...SELECTED];
  sel.value="";
  if(!keys.length){showToast("Select at least 1 student first");return;}
  if(!confirm("Move "+keys.length+" selected student(s) to "+srcLabel(to)+"?\\n\\nOnly the data-type tag changes — nothing is deleted."))return;
  try{
    const r=await api("/api/change-source-selected","POST",{row_keys:keys,to_source:to});
    showToast("Moved "+(r.moved||keys.length)+" student(s) to "+srcLabel(to));
    clearSelection();
    refreshAllTables();
    try{loadSourceCounts();}catch(e){}
  }catch(e){showToast("Error: "+e.message);}
}
async function downloadSample(type){
  try{
    const r=await fetch(API+"/api/sample-sheet?type="+type,{headers:{"Authorization":"Bearer "+TOKEN}});
    if(!r.ok){showToast("Sample download failed ("+r.status+")");return;}
    const blob=await r.blob();const url=URL.createObjectURL(blob);
    const a=document.createElement("a");a.href=url;
    a.download="MVS_sample_"+type+".xlsx";
    document.body.appendChild(a);a.click();a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1500);
    showToast("Sample sheet downloaded");
  }catch(e){showToast("Error: "+e.message);}
}
function fval(id){const e=document.getElementById(id);return e?e.value:"";}
function dateRange(prefix){
  const preset=fval(prefix+"-datepreset");
  const now=new Date();
  let from="",to="";
  if(preset==="today"){const s=new Date(now);s.setHours(0,0,0,0);from=fmtDT(s);to=fmtDT(now);}
  else if(preset==="yesterday"){const s=new Date(now);s.setDate(s.getDate()-1);s.setHours(0,0,0,0);
    const e=new Date(s);e.setHours(23,59,59,0);from=fmtDT(s);to=fmtDT(e);}
  else if(preset==="7d"){const s=new Date(now);s.setDate(s.getDate()-6);s.setHours(0,0,0,0);from=fmtDT(s);to=fmtDT(now);}
  else if(preset==="custom"){from=dtLocalToStr(fval(prefix+"-from"));to=dtLocalToStr(fval(prefix+"-to"));}
  return {from,to};
}
function onDatePreset(prefix,reload){
  const custom=fval(prefix+"-datepreset")==="custom";
  const row=document.getElementById(prefix+"-daterow");
  if(row)row.style.display=custom?"flex":"none";
  if(!custom&&reload)reload();
}
async function exportStudents(view){
  let search="",status="",session="",cls="",from="",to="",source="";
  if(view==="confirmed"){search=fval("c-search");status=fval("c-status");session=fval("c-session");cls=fval("c-class");source=fval("c-source");const dr=dateRange("c");from=dr.from;to=dr.to;}
  else if(view==="required"){search=fval("r-search");status=fval("r-status");session=fval("r-session");source=fval("r-source");}
  else{search=fval("s-search");status=fval("s-status");session=fval("s-session");cls=fval("s-class");source=fval("s-source");const dr=dateRange("s");from=dr.from;to=dr.to;}
  const q=new URLSearchParams({view:view,search:search,status_filter:status,session_filter:session,
    class_filter:cls,source_filter:source,date_from:from,date_to:to});
  try{
    showToast("Preparing Excel...");
    const r=await fetch(API+"/api/export-students?"+q.toString(),{headers:{Authorization:"Bearer "+TOKEN}});
    if(!r.ok){showToast("Export failed");return;}
    const blob=await r.blob();const url=URL.createObjectURL(blob);
    const a=document.createElement("a");a.href=url;
    a.download="nios_"+(view==="normal"?"active":view)+"_students.xlsx";
    document.body.appendChild(a);a.click();a.remove();
    setTimeout(()=>URL.revokeObjectURL(url),1500);
    showToast("Excel downloaded");
  }catch(e){showToast("Error: "+e.message);}
}
function toggleRunMenu(e){
  if(e)e.stopPropagation();
  const m=document.getElementById("run-menu");
  if(!m)return;
  const open=(m.style.display==="none"||!m.style.display);
  m.style.display=open?"block":"none";
  const b=document.getElementById("run-now-btn");
  if(b)b.classList.toggle("open",open);
}
document.addEventListener("click",function(e){
  const w=e.target.closest&&e.target.closest(".run-menu-wrap");
  if(!w){const m=document.getElementById("run-menu");if(m)m.style.display="none";
    const b=document.getElementById("run-now-btn");if(b)b.classList.remove("open");}
});
const RUN_EP={all:"/api/run-now",tracker:"/api/run-now-tracker",portal:"/api/run-now-portal",portalnew:"/api/run-now-portal-new"};
const RUN_LBL={all:"all students",tracker:"MVS Tracker students",portal:"MVS Portal students",portalnew:"MVS Portal — new data only"};
const PORTAL_GRP_LBL={ondemand:"On Demand",stream2:"Stream 2",public:"Public",all:"all MVS Portal"};
async function runRequired(btn){
  if(btn&&btn.dataset.busy==="1")return;
  if(btn){btn.dataset.busy="1";btn.style.opacity="0.6";btn.style.pointerEvents="none";}
  try{
    const r=await api("/api/run-required","POST");
    if((r.count||0)===0){showToast("No Document Required students to run.");}
    else{
      showToast(r.message+" — running in the background");
      const box=document.getElementById("run-progress");
      if(box){box.style.display="block";document.getElementById("pb-pct").textContent="0%";
        document.getElementById("pb-fill").style.width="0%";
        document.getElementById("pb-sub").textContent="Starting…";
        document.getElementById("pb-label").textContent="Re-checking Document Required students…";}
      startProgressPoll();
    }
  }catch(e){showToast("Error: "+e.message);}
  finally{ setTimeout(()=>{if(btn){btn.dataset.busy="";btn.style.opacity="";btn.style.pointerEvents="";}},4000); }
}
async function runChoice(kind,group){
  const m=document.getElementById("run-menu");if(m)m.style.display="none";
  const btn=document.getElementById("run-now-btn");if(btn)btn.classList.remove("open");
  if(btn&&btn.dataset.busy==="1")return;
  if(btn){btn.dataset.busy="1";btn.style.opacity="0.6";btn.style.pointerEvents="none";}
  try{
    let ep=RUN_EP[kind]||RUN_EP.all;
    if((kind==="portal"||kind==="tracker")&&group)ep=RUN_EP[kind]+"?group="+encodeURIComponent(group);
    const r=await api(ep,"POST");
    showToast(r.message+" — running in the background");
    let lbl=RUN_LBL[kind]||"students";
    if((kind==="portal"||kind==="tracker")&&group){
      lbl=(kind==="portal"?"MVS Portal":"MVS Tracker")+" — "+(PORTAL_GRP_LBL[group]||group)+" students";
    }
    const box=document.getElementById("run-progress");
    if(box){box.style.display="block";document.getElementById("pb-pct").textContent="0%";
      document.getElementById("pb-fill").style.width="0%";
      document.getElementById("pb-sub").textContent="Starting…";
      document.getElementById("pb-label").textContent="Checking "+lbl+"…";}
    startProgressPoll();
  }catch(e){showToast("Error: "+e.message);}
  finally{ setTimeout(()=>{if(btn){btn.dataset.busy="";btn.style.opacity="";btn.style.pointerEvents="";}},4000); }
}
async function runNow(){return runChoice('all');}   // back-compat
async function runUploaded(btn){
  if(btn){btn.disabled=true;btn.style.opacity="0.6";}
  try{
    const r=await api("/api/run-now-upload","POST");
    showToast(r.message+" — running in the background");
    // Clear the upload preview/summary now that the run has started.
    ["upload-status","upload-summary","upload-preview"].forEach(id=>{
      const el=document.getElementById(id);if(el)el.innerHTML="";});
    const fi=document.getElementById("file-input");if(fi)fi.value="";
    const box=document.getElementById("run-progress");
    if(box){box.style.display="block";document.getElementById("pb-pct").textContent="0%";
      document.getElementById("pb-fill").style.width="0%";
      document.getElementById("pb-sub").textContent="Starting…";
      document.getElementById("pb-label").textContent="Checking uploaded students…";}
    startProgressPoll();
    showToast("Upload preview cleared — run started");
  }catch(e){showToast("Error: "+e.message);}
  finally{ setTimeout(()=>{if(btn){btn.disabled=false;btn.style.opacity="";}},4000); }
}

function setIvField(which,mins){
  const inp=document.getElementById("iv-"+which),unit=document.getElementById("iv-"+which+"-unit");
  if(!inp||!unit)return;
  if(mins>=1440 && mins%1440===0){inp.value=mins/1440;unit.value="days";}
  else if(mins>=60 && mins%60===0){inp.value=mins/60;unit.value="hours";}
  else{inp.value=mins;unit.value="minutes";}
}
function ivToMin(which){
  const v=parseInt(document.getElementById("iv-"+which).value)||0;
  const unit=document.getElementById("iv-"+which+"-unit").value;
  if(unit==="days")return v*1440;
  if(unit==="hours")return v*60;
  return v;
}
async function loadIntervals(){
  try{const r=await api("/api/intervals");
    setIvField("ondemand",r.ondemand_min);
    setIvField("stream2",r.stream2_min);
    setIvField("public",r.public_min);
    setIvField("portalnew",r.portalnew_min);
    var sm={ondemand:r.ondemand_src,stream2:r.stream2_src,public:r.public_src};
    for(var g in sm){var el=document.getElementById("iv-"+g+"-src");if(el)el.value=sm[g]||"both";}
  }catch(e){}
}
async function loadTrash(){
  const b=document.getElementById("trash-body");if(!b)return;
  try{
    const rows=await api("/api/deleted-students");
    if(!rows.length){b.innerHTML='<tr><td colspan="6" class="empty">Trash is empty</td></tr>';selBar("trash");return;}
    b.innerHTML=rows.map(s=>{
      var nm=(s.student_name||"this student").replace(/[\\"']/g," ");
      var ref=(s.reference_no||s.enrollment_no||"—");
      return '<tr><td>'+selBox("trash",s.row_key)+'</td><td>'+(s.student_name||"—")+'<div style="margin-top:4px">'+srcBadge(s)+'</div></td>'+
        '<td><span class="ref-tag">'+ref+'</span></td>'+
        '<td style="font-size:13px">'+(s.session||"—")+'</td>'+
        '<td style="font-size:12px;color:var(--muted)">'+(s.deleted_at||"—")+'</td>'+
        '<td><div style="display:flex;gap:6px;flex-wrap:wrap">'+
        '<button onclick="restoreStudent(&quot;'+s.row_key+'&quot;,&quot;'+nm+'&quot;)" style="background:#dcfce7;color:#166534;border:1px solid #bbf7d0;border-radius:8px;padding:5px 10px;font-size:12px;font-weight:600;cursor:pointer">&#8617; Restore</button>'+
        '<button onclick="purgeStudent(&quot;'+s.row_key+'&quot;,&quot;'+nm+'&quot;)" style="background:#fee2e2;color:#b91c1c;border:1px solid #fecaca;border-radius:8px;padding:5px 10px;font-size:12px;font-weight:600;cursor:pointer">Delete permanently</button>'+
        '</div></td></tr>';
    }).join("");
    const sa=document.getElementById("trash-selall");if(sa)sa.checked=false;
    selBar("trash");
  }catch(e){b.innerHTML='<tr><td colspan="6" class="empty">'+e.message+'</td></tr>';}
}
async function restoreSelected(){
  const keys=Array.from(selSet("trash"));
  if(!keys.length)return;
  try{
    const r=await api("/api/students-restore-bulk","POST",{row_keys:keys});
    showToast("Restored "+(r.restored||keys.length)+" student(s)");
    selClear("trash");loadTrash();try{loadDashboard();}catch(e){}
  }catch(e){showToast("Error: "+e.message);}
}
async function purgeSelected(){
  const keys=Array.from(selSet("trash"));
  if(!keys.length)return;
  if(!confirm("Permanently delete "+keys.length+" student(s)?\\n\\nThis CANNOT be undone — they will be gone forever."))return;
  try{
    const r=await api("/api/students-purge-bulk","POST",{row_keys:keys});
    showToast("Permanently deleted "+(r.purged||keys.length)+" student(s)");
    selClear("trash");loadTrash();
  }catch(e){showToast("Error: "+e.message);}
}
async function restoreStudent(rk,name){
  try{await api("/api/student-restore?row_key="+encodeURIComponent(rk),"POST");
    showToast("Restored "+name);loadTrash();try{loadDashboard();}catch(e){}}
  catch(e){showToast(""+e.message);}
}
async function purgeStudent(rk,name){
  if(!confirm("Permanently delete "+name+"?\\n\\nThis CANNOT be undone."))return;
  try{await api("/api/student-purge?row_key="+encodeURIComponent(rk),"POST");
    showToast("Permanently deleted "+name);loadTrash();}
  catch(e){showToast(""+e.message);}
}
async function loadReportSettings(){
  try{
    const d=await api("/api/report-settings");
    const cb=document.getElementById("rep-enabled");if(cb)cb.checked=!!d.enabled;
    const ta=document.getElementById("rep-numbers");if(ta)ta.value=(d.numbers||"").split(",").filter(Boolean).join(String.fromCharCode(10));
    const last=document.getElementById("rep-last");
    if(last)last.innerHTML=d.last_status?('Last report: '+d.last_status):'';
    if(!d.campaign_set){const s=document.getElementById("rep-status");if(s)s.innerHTML='<span style="color:var(--warn)">Note: AISENSY_CAMPAIGN_REPORT env var not set on Railway — report will not send until it is added.</span>';}
  }catch(e){}
}
async function sendLatestReport(btn){
  const s=document.getElementById("rep-status");
  s.innerHTML='<span style="color:var(--muted)">Loading recent runs…</span>';
  let runs=[];
  try{ runs=await api("/api/recent-runs"); }catch(e){}
  let h='<div style="border:1px solid var(--border);border-radius:12px;padding:14px;margin-top:8px;background:var(--card)">';
  h+='<div style="font-weight:700;font-size:13.5px;margin-bottom:4px">Which report do you want to send to all management numbers?</div>';
  h+='<div style="color:var(--muted);font-size:12px;margin-bottom:10px">Pick a run below. (Automatic reports always send the latest run on their own.)</div>';
  h+='<div style="display:flex;flex-direction:column;gap:8px">';
  h+='<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:9px;padding:9px 11px">'
    +'<span style="font-size:12.5px"><b>Current live snapshot</b> <span style="color:var(--muted)">— totals right now</span></span>'
    +'<button class="btn btn-sm btn-primary" onclick="sendSnapshotReport(this)">Send</button></div>';
  (runs||[]).forEach(r=>{
    h+='<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;border:1px solid var(--border);border-radius:9px;padding:9px 11px">'
      +'<span style="font-size:12.5px">'+(r.label||"Run")+'<br><span style="color:var(--muted);font-size:11.5px">checked '+(r.checked||0)+' &middot; confirmed '+(r.confirmed||0)+' &middot; required '+(r.required||0)+'</span></span>'
      +'<button class="btn btn-sm" style="background:#16A34A;color:#fff" onclick="sendPickedReport('+r.id+',this)">Send</button></div>';
  });
  if(!runs||!runs.length) h+='<div style="color:var(--muted);font-size:12px">No saved past runs yet — use Current snapshot. New runs will appear here automatically.</div>';
  h+='</div></div>';
  s.innerHTML=h;
}
async function _doReportSend(promise,btn){
  const s=document.getElementById("rep-status");
  if(btn){btn.disabled=true;btn.dataset.t=btn.textContent;btn.textContent="Sending…";}
  try{
    const r=await promise;
    s.innerHTML=r.sent?'<span style="color:var(--success)">&#10003; Report sent to '+r.sent+'/'+r.total+' management number(s).</span>'
                      :'<span style="color:var(--danger)">Could not send to anyone.</span>';
    if(r.errors&&r.errors.length)s.innerHTML+='<div style="color:var(--danger);font-size:12px;margin-top:6px">'+r.errors.join("<br>")+'</div>';
    try{loadReportSettings();}catch(e){}
  }catch(e){s.innerHTML='<span style="color:var(--danger)">Error: '+e.message+'</span>';
    if(btn){btn.disabled=false;btn.textContent=btn.dataset.t||"Send";}}
}
function sendSnapshotReport(btn){ _doReportSend(api("/api/send-latest-report","POST"),btn); }
function sendPickedReport(runId,btn){ _doReportSend(api("/api/send-run-report","POST",{run_id:runId}),btn); }
async function saveReportSettings(){
  const enabled=document.getElementById("rep-enabled").checked;
  const numbers=document.getElementById("rep-numbers").value;
  const s=document.getElementById("rep-status");
  try{
    const r=await api("/api/report-settings","POST",{enabled:enabled,numbers:numbers});
    s.innerHTML='<span style="color:var(--success)">Saved — '+r.count+' number(s). '+(r.enabled?"Reports ON.":"Reports OFF.")+'</span>';
    const ta=document.getElementById("rep-numbers");if(ta)ta.value=(r.numbers||"").split(",").filter(Boolean).join(String.fromCharCode(10));
  }catch(e){s.innerHTML='<span style="color:var(--danger)">Error: '+e.message+'</span>';}
}
async function testReport(btn){
  const s=document.getElementById("rep-status");
  if(btn){btn.disabled=true;btn.style.opacity="0.6";}
  s.innerHTML='<span style="color:var(--muted)">Sending test report…</span>';
  try{
    const r=await api("/api/report-test","POST");
    s.innerHTML='<span style="color:var(--success)">Test sent to '+r.sent+'/'+r.total+' number(s).</span>'+
      (r.errors&&r.errors.length?'<div style="color:var(--danger);font-size:12px;margin-top:6px">'+r.errors.join("<br>")+'</div>':"");
  }catch(e){s.innerHTML='<span style="color:var(--danger)">Error: '+e.message+'</span>';}
  finally{if(btn){btn.disabled=false;btn.style.opacity="";}}
}
async function saveIntervals(){
  const od=ivToMin("ondemand"),st=ivToMin("stream2"),pm=ivToMin("public"),pn=ivToMin("portalnew");
  const S=document.getElementById("iv-status");
  if(od<15||st<15||pm<15||pn<15){S.innerHTML='<span style="color:var(--danger)">Minimum interval is 15 minutes</span>';return;}
  if(od>43200||st>43200||pm>43200||pn>43200){S.innerHTML='<span style="color:var(--danger)">Maximum interval is 30 days</span>';return;}
  var ivsrc=function(g){var el=document.getElementById("iv-"+g+"-src");return el?el.value:"both";};
  try{const r=await api("/api/intervals","POST",{ondemand_min:od,stream2_min:st,public_min:pm,portalnew_min:pn,
        ondemand_src:ivsrc("ondemand"),stream2_src:ivsrc("stream2"),public_src:ivsrc("public")});
    S.innerHTML='<span style="color:var(--success)">'+r.message+'</span>';
    showToast("Intervals saved!");try{loadNextRuns();}catch(e){}}
  catch(e){S.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
}

async function loadWa(){
  let r=null;
  for(let attempt=0;attempt<2;attempt++){
    try{r=await api("/api/wa-settings");break;}
    catch(e){
      if(attempt>=1){const cfg=document.getElementById("wa-config");if(cfg)cfg.innerHTML='<span style="color:var(--danger)">&#10007; Could not load WhatsApp settings: '+(e.message||e)+'. <a href="#" onclick="loadWa();return false;" style="color:#2563eb;font-weight:600">Retry</a></span>';break;}
      await new Promise(s=>setTimeout(s,900));
    }
  }
  if(r){try{
    document.getElementById("wa-enabled").checked=r.enabled;
    var re=document.getElementById("wa-required-enabled");if(re)re.checked=!!r.required_enabled;
    const cfg=document.getElementById("wa-config");
    if(!r.configured){
      cfg.innerHTML='<span style="color:var(--danger)">&#10007; AISENSY_API_KEY is not set in Railway environment variables</span>';
    }else{
      const c=r.campaigns||{};const rc=r.required_campaigns||{};const ce=r.campaigns_env||{};
      const esc=(v)=>(v||"").replace(/"/g,"&quot;");
      const row=(lbl,v)=>'<div style="margin:2px 0">'+lbl+': '+
        (v?'<b style="color:var(--success)">'+v+'</b>':'<span style="color:var(--warn)">not set</span>')+'</div>';
      const camp=(lbl,key,v)=>'<div style="display:flex;align-items:center;gap:8px;margin:5px 0;flex-wrap:wrap">'+
        '<span style="width:96px;font-size:12.5px;color:var(--muted)">'+lbl+'</span>'+
        '<input class="wa-camp" data-g="'+key+'" value="'+esc(v)+'" placeholder="AiSensy campaign name" '+
        'style="flex:1;max-width:300px;padding:6px 9px;border:1px solid '+(v?'var(--border)':'#fca5a5')+';border-radius:7px;font-size:13px">'+
        '<span style="font-size:11px;font-weight:600;color:'+(ce[key]?'#15803d':'#b91c1c')+'">Railway: '+(ce[key]?'seen &#10003;':'empty &#10007;')+'</span>'+
        '</div>';
      cfg.innerHTML='<div style="color:var(--success);margin-bottom:8px">&#10003; API key configured</div>'+
        '<div style="font-size:12px;color:var(--muted);margin-bottom:4px"><b>Confirmed-send campaigns</b> — type the AiSensy campaign name for each and Save (works even if Railway shows empty):</div>'+
        camp("On Demand","ondemand",c.ondemand)+camp("Stream 2","stream2",c.stream2)+
        camp("Public","public",c.public)+camp("SYC","syc",c.syc)+
        '<button class="btn btn-sm" style="background:var(--primary);color:#fff;margin-top:6px" onclick="saveCampaigns(this)">Save campaigns</button>'+
        '<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border)"><b style="font-size:12px;color:var(--muted)">Document-Required reminder</b></div>'+
        row("Reminder &middot; main (On Demand/Stream 2)",rc.main)+row("Reminder &middot; public",rc.public);
    }
  }catch(e){}}
  try{const w=await api("/api/webhook-url");const el=document.getElementById("wh-url");if(el)el.value=w.url||"";}catch(e){}
}
function copyWebhook(btn){
  const el=document.getElementById("wh-url");if(!el)return;
  el.select();el.setSelectionRange(0,99999);
  try{navigator.clipboard.writeText(el.value);}catch(e){try{document.execCommand("copy");}catch(_){}}
  if(btn){const o=btn.textContent;btn.textContent="Copied!";setTimeout(()=>{btn.textContent=o;},1500);}
}
async function saveCampaigns(btn){
  const body={};
  document.querySelectorAll(".wa-camp").forEach(i=>{body[i.dataset.g]=i.value.trim();});
  if(btn)btn.disabled=true;
  try{await api("/api/wa-campaigns","POST",body);showToast("Campaigns saved \u2713");loadWa();}
  catch(e){showToast("Error: "+e.message);}
  finally{if(btn)btn.disabled=false;}
}
async function saveWa(){
  const en=document.getElementById("wa-enabled").checked;
  var reEl=document.getElementById("wa-required-enabled");
  const ren=reEl?reEl.checked:false;
  try{await api("/api/wa-settings","POST",{enabled:en,required_enabled:ren});
    showToast("WhatsApp settings saved");}
  catch(e){showToast("Error: "+e.message);}
}
async function testWa(){
  const num=document.getElementById("wa-num").value.trim();
  const group=document.getElementById("wa-group").value;
  const s=document.getElementById("wa-status");
  if(!num){s.innerHTML='<span style="color:var(--danger)">Please enter a number</span>';return;}
  s.innerHTML='Sending...';
  try{const r=await api("/api/wa-test","POST",{number:num,group:group});
    if(r.ok)s.innerHTML='<span style="color:var(--success)">&#10003; Test message sent. Please check your WhatsApp.</span>';
    else s.innerHTML='<span style="color:var(--danger)">&#10007; '+(r.info||"failed")+'</span>';}
  catch(e){s.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
}

function showToast(msg){
  const t=document.getElementById("toast");t.textContent=msg;t.classList.add("show");
  setTimeout(()=>t.classList.remove("show"),3500);
}

async function testLogin(){
  const ref=document.getElementById("dbg-ref").value.trim();
  const dob=document.getElementById("dbg-dob").value.trim();
  if(!ref||!dob){showToast("Please enter both Reference No and DOB");return;}
  const st=document.getElementById("dbg-status");
  const pre=document.getElementById("dbg-result");
  st.innerHTML='<span style="color:var(--muted)">Logging in (solving captcha, ~15 sec)...</span>';
  pre.style.display="none";
  try{
    const q=new URLSearchParams({ref:ref,dob:dob});
    const d=await api("/api/debug-login?"+q.toString());
    if(d.error){st.innerHTML='<span style="color:var(--danger)">'+d.error+'</span>';return;}
    const ok=d.logged_in_guess;
    st.innerHTML=ok?'<span style="color:var(--success)">Login successful! See the links below.</span>'
      :'<span style="color:var(--warn)">Login may have failed (or the page is different). Details below.</span>';
    pre.style.display="block";
    pre.textContent=JSON.stringify(d,null,2);
  }catch(e){st.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
}

async function inspectDoc(){
  const ref=document.getElementById("dbg-ref").value.trim();
  const dob=document.getElementById("dbg-dob").value.trim();
  const kind=document.getElementById("dbg-kind").value;
  if(!ref||!dob){showToast("Please enter both Reference No and DOB");return;}
  const st=document.getElementById("dbg-status");
  const pre=document.getElementById("dbg-result");
  st.innerHTML='<span style="color:var(--muted)">Inspecting the '+kind+' page (~15 sec)...</span>';
  pre.style.display="none";
  try{
    const q=new URLSearchParams({ref:ref,dob:dob,kind:kind});
    const d=await api("/api/debug-doc?"+q.toString());
    if(d.error){st.innerHTML='<span style="color:var(--danger)">'+d.error+'</span>';return;}
    st.innerHTML='<span style="color:var(--success)">Inspection complete — structure below</span>';
    pre.style.display="block";
    pre.textContent=JSON.stringify(d,null,2);
  }catch(e){st.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
}

async function findAddr(){
  const ref=document.getElementById("dbg-ref").value.trim();
  const dob=document.getElementById("dbg-dob").value.trim();
  if(!ref||!dob){showToast("Please enter both Reference No and DOB");return;}
  const st=document.getElementById("dbg-status");
  const pre=document.getElementById("dbg-result");
  st.innerHTML='<span style="color:var(--muted)">Finding the Regional Centre address from the ID card (~15 sec)...</span>';
  pre.style.display="none";
  try{
    const q=new URLSearchParams({ref:ref,dob:dob});
    const d=await api("/api/debug-idcard?"+q.toString());
    if(d.error){st.innerHTML='<span style="color:var(--danger)">'+d.error+'</span>';return;}
    st.innerHTML='<span style="color:var(--success)">Extracted address: <b>'+(d.extracted_address||"(blank — please share the text below)")+'</b></span>';
    pre.style.display="block";
    pre.textContent=JSON.stringify(d,null,2);
  }catch(e){st.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
}

async function resetData(){
  if(!confirm("Are you sure? All students, statuses, history and the sheet will be DELETED. This cannot be undone."))return;
  if(!confirm("Final confirmation — do you really want to clear all data?"))return;
  const s=document.getElementById("reset-status");
  s.innerHTML="Clearing...";
  try{const r=await api("/api/reset-data","POST",{});
    s.innerHTML='<span style="color:var(--success)">&#10003; '+r.message+' You can now upload a new sheet.</span>';
    showToast("All data cleared");}
  catch(e){s.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
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

def _last_run_time(group_type):
    """Most recent completed run for this group (or a manual 'All' run)."""
    pat = {"ondemand": "%On Demand%", "stream2": "%Stream 2%",
           "public": "%Public%"}.get(group_type, f"%{group_type}%")
    conn = get_db()
    row = conn.execute(
        "SELECT run_at FROM run_logs WHERE (group_type LIKE ? OR group_type IN ('All','all')) "
        "AND status LIKE 'completed%' ORDER BY id DESC LIMIT 1", (pat,)).fetchone()
    conn.close()
    if row and row["run_at"]:
        try:
            return datetime.strptime(row["run_at"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return None

def _interval_minutes(grp, default_h):
    """Interval in MINUTES. Prefers the group's own *_min setting; On Demand & Stream 2
    fall back to the old combined 'regular' interval (so existing setups keep working
    until they're set separately). Legacy hour setting (×60) honoured last."""
    m = get_setting(f"interval_{grp}_min", "")
    if m:
        try:
            return max(15, int(m))
        except Exception:
            pass
    if grp in ("ondemand", "stream2"):
        rm = get_setting("interval_regular_min", "")
        if rm:
            try:
                return max(15, int(rm))
            except Exception:
                pass
    try:
        return int(get_setting(f"interval_{grp}", str(default_h))) * 60
    except Exception:
        return default_h * 60

def _mvs_enabled():
    try:
        import mvs_sync
        return mvs_sync.enabled()
    except Exception:
        return False

# Run groups — each has its own interval, dashboard timer, pause/resume + manual run.
# (job id, default hours)
RUN_GROUPS = [("ondemand", "job_ondemand", 6), ("stream2", "job_stream2", 6),
              ("public", "job_public", 12)]

def _group_source(grp):
    """Per-group data-source preference for AUTO runs (set in Recheck Intervals):
    'both' (default) -> None = both sources run together; otherwise restrict the
    scheduled run to just MVS Tracker or just MVS Portal data. This is what lets a
    counsellor say e.g. 'auto-run Stream 2 from MVS Tracker only'."""
    v = get_setting(f"runsrc_{grp}", "both")
    return v if v in ("mvs_tracker", "mvs_portal") else None

def reschedule_jobs():
    """One interval job per group (On Demand / Stream 2 / Public). Both data sources
    (MVS Portal + MVS Tracker) run together; the split is by session group only.
    Automatic runs happen ONLY at the set interval — never a 'catch-up' burst after a
    restart/redeploy/upload (next run is always at least one full interval away)."""
    now = datetime.now()
    for old in ("job_mvs", "job_regular"):     # drop legacy combined jobs
        try:
            scheduler.remove_job(old)
        except Exception:
            pass
    for grp, jid, dh in RUN_GROUPS:
        mins = _interval_minutes(grp, dh)
        # If THIS group is paused, keep it off (across restarts too) and skip scheduling.
        if get_setting(f"paused_{grp}", "") == "1":
            try:
                scheduler.remove_job(jid)
            except Exception:
                pass
            logger.info(f"{jid}: PAUSED — not scheduled.")
            continue
        last = _last_run_time(grp)
        nxt = last + timedelta(minutes=mins) if last else now + timedelta(minutes=mins)
        if nxt <= now:
            nxt = now + timedelta(minutes=mins)
        scheduler.add_job(lambda g=grp: run_status_check(g, _group_source(g)),
                          trigger=IntervalTrigger(minutes=mins),
                          id=jid, replace_existing=True, next_run_time=nxt)
        logger.info(f"{jid}: every {mins}min | last_run={last} | next_run={nxt}")

    # MVS Portal — NEW data only: a cheap, frequent run that checks just the students
    # newly arrived on the portal (skips already-tracked/active data) to save CapSolver.
    if get_setting("paused_portalnew", "") == "1":
        try:
            scheduler.remove_job("job_portalnew")
        except Exception:
            pass
        logger.info("job_portalnew: PAUSED — not scheduled.")
    else:
        pn_mins = _interval_minutes("portalnew", 3)
        pn_nxt = now + timedelta(minutes=pn_mins)
        scheduler.add_job(lambda: run_status_check("all", "mvs_portal", "new"),
                          trigger=IntervalTrigger(minutes=pn_mins),
                          id="job_portalnew", replace_existing=True, next_run_time=pn_nxt)
        logger.info(f"job_portalnew: every {pn_mins}min | next_run={pn_nxt}")

@app.on_event("startup")
async def startup():
    init_db()
    reschedule_jobs()
    # Auto-send WhatsApp documents to confirmed students that still haven't received
    # them — every 10 minutes, in small batches, so nothing stays "Not sent".
    try:
        from apscheduler.triggers.interval import IntervalTrigger as _IT
        scheduler.add_job(auto_send_pending_whatsapp, trigger=_IT(minutes=10),
                          id="job_wa_autosend", replace_existing=True,
                          next_run_time=datetime.now() + timedelta(minutes=2))
    except Exception as e:
        logger.warning(f"WhatsApp auto-send job not scheduled: {e}")
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
    import asyncio
    u = str(body.get("username") or "")
    p = str(body.get("password") or "")
    # Constant-time comparison avoids leaking the password via response timing.
    ok = (_secrets.compare_digest(u, PORTAL_USER) and _secrets.compare_digest(p, PORTAL_PASS))
    if not ok:
        await asyncio.sleep(1.0)        # slow down brute-force guessing
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"token": create_token(u), "username": u}

def _dist_with_confirmed(dist_rows, confirmed_cnt):
    """Status distribution for the chart, but with 'Admission Confirmed' shown on the
    SAME definition as the card / sidebar / reports (the is_confirmed flag), so the
    dashboard never shows two different confirmed numbers. The raw-status confirmed rows
    that are actually login/check-failed are surfaced under 'Failed to Run' instead."""
    out = []
    for r in dist_rows:
        d = dict(r)
        if d.get("current_status") == "Admission Confirmed":
            d["cnt"] = confirmed_cnt
        out.append(d)
    return out

@app.get("/api/dashboard")
async def dashboard(user=Depends(verify_token)):
    conn = get_db()
    ND = "COALESCE(deleted,0)=0"          # not-deleted guard for every count
    total = conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND}").fetchone()[0]
    runs = conn.execute("SELECT * FROM run_logs ORDER BY id DESC LIMIT 10").fetchall()
    dist = conn.execute(f"SELECT current_status, COUNT(*) as cnt FROM student_status WHERE {ND} GROUP BY current_status").fetchall()

    # Counts by status
    NF = "COALESCE(login_failed,0)=0 AND COALESCE(check_failed,0)=0"   # exclude failed-to-run
    def count_status(s):
        return conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND current_status=?", (s,)).fetchone()[0]
    # "Confirmed" everywhere else (sidebar badge, session-wise card, exported reports)
    # means the is_confirmed FLAG, not the raw NIOS status text. Keep this dashboard
    # card on the SAME definition so the numbers never disagree across the screen.
    confirmed_cnt = conn.execute(
        f"SELECT COUNT(*) FROM student_status WHERE {ND} AND {NF} AND is_confirmed=1").fetchone()[0]
    verified_cnt  = count_status("Verified")
    docreq_cnt    = count_status("Document Required")
    docverif_cnt  = count_status("Documents Verification In Progress")
    syc_cnt = conn.execute(
        f"SELECT COUNT(*) FROM student_status WHERE {ND} AND (session LIKE '%syc%' OR current_status='SYC')"
    ).fetchone()[0]

    # Changes today
    today = datetime.now().strftime("%Y-%m-%d")
    changes_today = conn.execute(
        "SELECT COUNT(*) FROM status_history WHERE changed_at LIKE ?", (f"{today}%",)).fetchone()[0]

    # Notifications: document required students (name + ref)
    notifs = conn.execute(
        f"SELECT student_name, reference_no, remark FROM student_status WHERE {ND} AND current_status='Document Required' ORDER BY last_changed DESC LIMIT 50"
    ).fetchall()

    # Cross-source duplicates: same student present in BOTH MVS Portal & MVS Tracker
    # (kept once, as MVS Portal). Surfaced in the bell so the counsellor is aware.
    dup_notifs = conn.execute(
        f"SELECT student_name, reference_no, enrollment_no FROM student_status "
        f"WHERE {ND} AND COALESCE(cross_dup,0)=1 ORDER BY student_name LIMIT 50"
    ).fetchall()

    # Failed-to-run students: NIOS login failed OR status check failed -> need edit/re-run
    login_issues = conn.execute(
        f"SELECT student_name, reference_no, enrollment_no, login_remark, row_key FROM student_status "
        f"WHERE {ND} AND (COALESCE(login_failed,0)=1 OR COALESCE(check_failed,0)=1) "
        f"ORDER BY student_name LIMIT 100"
    ).fetchall()

    # Status not found (Unknown / Fetch Error): NIOS didn't return a readable status —
    # likely an error or wrong reference. Surfaced in the bell with a remark so the
    # counsellor can re-check, since these are easy to miss in Active Students.
    unknown_notifs = conn.execute(
        f"SELECT student_name, reference_no, enrollment_no, current_status, "
        f"COALESCE(NULLIF(login_remark,''), NULLIF(remark,''), '') as remark "
        f"FROM student_status WHERE {ND} AND current_status IN ('Unknown','Fetch Error') "
        f"ORDER BY last_changed DESC LIMIT 100"
    ).fetchall()

    # Session-wise totals WITH a per-session status breakdown (same definitions the
    # nav tabs use), so a counsellor can see, per session, how many are still pending.
    NFAIL = "COALESCE(login_failed,0)=0 AND COALESCE(check_failed,0)=0"
    sess_counts = conn.execute(
        f"SELECT session, COUNT(*) as cnt, "
        f"SUM(CASE WHEN is_confirmed=1 THEN 1 ELSE 0 END) as confirmed, "
        f"SUM(CASE WHEN COALESCE(is_confirmed,0)=0 AND current_status='Verified' THEN 1 ELSE 0 END) as verified, "
        f"SUM(CASE WHEN current_status='Document Required' THEN 1 ELSE 0 END) as required, "
        f"SUM(CASE WHEN COALESCE(is_confirmed,0)=0 AND COALESCE(current_status,'')!='SYC' "
        f"         AND (session IS NULL OR session NOT LIKE '%syc%') THEN 1 ELSE 0 END) as active "
        f"FROM student_status WHERE {ND} AND {NFAIL} AND session != '' GROUP BY session ORDER BY cnt DESC"
    ).fetchall()

    # Same breakdown but grouped by DATA SOURCE (MVS Portal vs MVS Tracker), so the
    # dashboard shows a card for the live portal data alongside the session cards.
    src_counts = conn.execute(
        f"SELECT source, COUNT(*) as cnt, "
        f"SUM(CASE WHEN is_confirmed=1 THEN 1 ELSE 0 END) as confirmed, "
        f"SUM(CASE WHEN COALESCE(is_confirmed,0)=0 AND current_status='Verified' THEN 1 ELSE 0 END) as verified, "
        f"SUM(CASE WHEN current_status='Document Required' THEN 1 ELSE 0 END) as required, "
        f"SUM(CASE WHEN COALESCE(is_confirmed,0)=0 AND COALESCE(current_status,'')!='SYC' "
        f"         AND (session IS NULL OR session NOT LIKE '%syc%') THEN 1 ELSE 0 END) as active "
        f"FROM student_status WHERE {ND} AND {NFAIL} GROUP BY source"
    ).fetchall()

    conn.close()
    _nexts = [j.next_run_time for j in (scheduler.get_job(jid) for _g, jid, _d in RUN_GROUPS)
              if j and j.next_run_time]
    next_run = str(min(_nexts)) if _nexts else "Not scheduled"
    return {
        "total_students": total, "next_run": next_run,
        "status_distribution": _dist_with_confirmed(dist, confirmed_cnt),
        "recent_runs": [dict(r) for r in runs],
        "counts": {
            "confirmed": confirmed_cnt, "verified": verified_cnt,
            "document_required": docreq_cnt, "doc_verification": docverif_cnt,
            "changes_today": changes_today, "syc": syc_cnt,
        },
        "notifications": [dict(n) for n in notifs],
        "dup_notifications": [dict(d) for d in dup_notifs],
        "unknown_notifications": [dict(u) for u in unknown_notifs],
        "login_issues": [dict(li) for li in login_issues],
        "session_counts": _normalized_session_counts(sess_counts),
        "source_counts": _normalized_source_counts(src_counts),
    }

def _normalized_source_counts(raw_rows):
    """Per data-source totals (MVS Portal / MVS Tracker) with the same breakdown as the
    session cards. MVS Portal is returned first so it leads the dashboard."""
    label = {"mvs_portal": "MVS Portal", "mvs_tracker": "MVS Tracker"}
    out = []
    for r in raw_rows:
        src = (r["source"] if "source" in r.keys() else "") or "mvs_tracker"
        out.append({"source": label.get(src, src), "key": src,
                    "cnt": r["cnt"] or 0, "confirmed": r["confirmed"] or 0,
                    "verified": r["verified"] or 0, "active": r["active"] or 0,
                    "required": r["required"] or 0})
    out.sort(key=lambda x: 0 if x["key"] == "mvs_portal" else 1)
    return out

def _normalized_session_counts(raw_rows):
    """Collapse raw session rows into the normalized categories (On Demand, Stream 2,
    Public, SYC) so the dashboard shows ONE row per real category, each with a
    per-session breakdown (total / confirmed / verified / active / required)."""
    agg = {}
    for r in raw_rows:
        norm = normalize_session(r["session"])
        if not norm:
            continue
        d = agg.setdefault(norm, {"session": norm, "cnt": 0, "confirmed": 0,
                                  "verified": 0, "active": 0, "required": 0})
        d["cnt"] += (r["cnt"] or 0)
        for k in ("confirmed", "verified", "active", "required"):
            d[k] += (r[k] or 0) if k in r.keys() else 0
    out = list(agg.values())
    out.sort(key=lambda x: x["cnt"], reverse=True)
    return out

def normalize_session(s):
    """Merge raw session text into the real filter categories so there is ONE entry
    per category, no matter how it's typed. Spelling tolerance lives in the shared
    excel_handler.session_category() (used by the run grouping and document rules too),
    so 'str-2'/'STREAM 2', 'ode'/'ODE', 'apr-27'/'APRIL 2027' all land in the right
    bucket. April / October / abbreviations / unknown all collapse to 'Public'."""
    from excel_handler import session_category
    return {"": "", "syc": "SYC", "ondemand": "On Demand", "stream2": "Stream 2",
            "stream1": "Stream 1", "public": "Public"}.get(session_category(s), "Public")

def _session_clause(cat):
    """SQL clause + params matching ALL raw variants of a normalized session category,
    so selecting 'On Demand' catches 'On Demand1' etc. 'Public' is defined as EVERYTHING
    that is not On Demand / Stream 2 / Stream 1 / SYC — so it catches every April / October
    spelling AND abbreviations like 'apr-27' / 'oct-26', exactly like the run grouping."""
    t = (cat or "").strip().lower()
    pats = {
        "on demand": ["%on demand%", "%ondemand%", "%on-demand%", "%on_demand%", "%odes%", "%ode%"],
        "stream 2":  ["%stream 2%", "%stream2%", "%stream-2%", "%stream ii%", "%stream_2%",
                      "%str-2%", "%str2%", "%str 2%"],
        "stream 1":  ["%stream 1%", "%stream1%", "%stream-1%", "%str_1%",
                      "%str-1%", "%str1%", "%str 1%"],
        "april":     ["%april%", "%apr-%", "%apr %"],
        "october":   ["%october%", "%oct-%", "%oct %"],
        "syc":       ["%syc%"],
    }
    if t == "public":
        # Everything that is NOT On Demand / Stream 2 / Stream 1 / SYC.
        neg = (pats["on demand"] + pats["stream 2"] + pats["stream 1"] + pats["syc"])
        clause = ("(TRIM(COALESCE(session,'')) != '' AND "
                  + " AND ".join(["LOWER(session) NOT LIKE ?"] * len(neg)) + ")")
        return clause, neg
    if t in pats:
        p = pats[t]
        return "(" + " OR ".join(["LOWER(session) LIKE ?"] * len(p)) + ")", p
    return "session = ?", [cat]

def _build_student_where(view, search, status_filter, session_filter,
                         class_filter="", date_from="", date_to="", source_filter="",
                         wa_status=""):
    """Shared WHERE builder so the table and its Excel export stay perfectly in sync.
    NULL-safe so students with missing status/date are never silently hidden."""
    wc, params = [], []
    wc.append("COALESCE(deleted,0) = 0")          # never show soft-deleted (in Trash)
    wc.append("COALESCE(login_failed,0) = 0")     # login-failed -> only in 'Failed to Run'
    wc.append("COALESCE(check_failed,0) = 0")     # status-check-failed -> only in 'Failed to Run'
    if view == "confirmed":
        wc.append("is_confirmed = 1")
    elif view == "required":
        wc.append("current_status = 'Document Required'")
    else:  # normal = active students, exclude confirmed and SYC (NULL-safe)
        wc.append("COALESCE(is_confirmed,0) = 0")
        wc.append("COALESCE(current_status,'') != 'SYC'")
        wc.append("(session IS NULL OR session NOT LIKE '%syc%')")
    if search:
        wc.append("(reference_no LIKE ? OR student_name LIKE ? OR email LIKE ?)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]
    if status_filter:
        wc.append("current_status = ?"); params.append(status_filter)
    if session_filter:
        clause, sp = _session_clause(session_filter)
        wc.append(clause); params += sp
    if source_filter:                      # mvs_portal | mvs_tracker | both (Data Type)
        if source_filter == "both":
            wc.append("COALESCE(cross_dup,0) = 1")   # exists in BOTH sources, merged
        else:
            wc.append("COALESCE(source,'mvs_tracker') = ?"); params.append(source_filter)
    if wa_status:                          # WhatsApp delivery filter (Confirmed view)
        if wa_status == "delivered":
            wc.append("whatsapp_delivery = 'delivered'")
        elif wa_status == "failed":
            wc.append("(whatsapp_delivery = 'failed' OR COALESCE(login_failed,0)=1)")
        elif wa_status == "sent":
            wc.append("COALESCE(whatsapp_sent,0)=1 AND COALESCE(whatsapp_delivery,'')=''")
        elif wa_status == "notsent":
            wc.append("COALESCE(whatsapp_sent,0)=0")
    if class_filter:                       # "10" matches 10/10TH, "12" matches 12/12TH
        wc.append("class_level LIKE ?"); params.append(f"{class_filter}%")
    # Date/time filter on when the status last changed; fall back to last_checked so a
    # student with an empty last_changed is never dropped. Accepts either a full
    # "YYYY-MM-DD HH:MM:SS" (custom range with time) or a plain "YYYY-MM-DD".
    def _dtval(v, end):
        v = (v or "").strip()
        if not v:
            return ""
        return v if len(v) > 10 else (v + (" 23:59:59" if end else " 00:00:00"))
    _dt = "COALESCE(NULLIF(last_changed,''), last_checked)"
    df, dtv = _dtval(date_from, False), _dtval(date_to, True)
    if df:
        wc.append(f"{_dt} >= ?"); params.append(df)
    if dtv:
        wc.append(f"{_dt} <= ?"); params.append(dtv)
    return (("WHERE " + " AND ".join(wc)) if wc else ""), params

@app.get("/api/students")
async def get_students(page: int=1, per_page: int=50, search: str="",
                       status_filter: str="", session_filter: str="",
                       class_filter: str="", date_from: str="", date_to: str="",
                       source_filter: str="", view: str="normal", wa_status: str="",
                       user=Depends(verify_token)):
    conn = get_db()
    offset = (page - 1) * per_page
    where, params = _build_student_where(view, search, status_filter, session_filter,
                                         class_filter, date_from, date_to, source_filter,
                                         wa_status)
    total = conn.execute(f"SELECT COUNT(*) FROM student_status {where}", params).fetchone()[0]
    students = conn.execute(
        f"SELECT * FROM student_status {where} ORDER BY student_name LIMIT ? OFFSET ?",
        params + [per_page, offset]).fetchall()
    raw_sessions = conn.execute("SELECT DISTINCT session FROM student_status WHERE session != ''").fetchall()
    norm_sessions = sorted({normalize_session(r["session"]) for r in raw_sessions})
    norm_sessions = [x for x in norm_sessions if x and x != "SYC"]   # SYC has its own page
    conn.close()
    return {"students": [dict(s) for s in students], "total": total, "page": page,
            "per_page": per_page, "pages": max(1, (total+per_page-1)//per_page),
            "sessions": norm_sessions}

@app.get("/api/failed-students")
async def failed_students(page: int = 1, per_page: int = 50, search: str = "",
                          source: str = "", user=Depends(verify_token)):
    """Students whose NIOS login failed (wrong data) — the 'Failed to Run' list."""
    conn = get_db()
    wc = ["COALESCE(deleted,0)=0", "(COALESCE(login_failed,0)=1 OR COALESCE(check_failed,0)=1)",
          "COALESCE(current_status,'')!='Unknown'"]
    params = []
    if search:
        like = f"%{search.strip()}%"
        wc.append("(student_name LIKE ? OR reference_no LIKE ? OR email LIKE ? OR enrollment_no LIKE ?)")
        params += [like, like, like, like]
    if source:
        if source == "both":
            wc.append("COALESCE(cross_dup,0)=1")
        else:
            wc.append("COALESCE(source,'mvs_tracker')=?"); params.append(source)
    where = "WHERE " + " AND ".join(wc)
    total = conn.execute(f"SELECT COUNT(*) FROM student_status {where}", params).fetchone()[0]
    per_page = max(1, min(per_page, 200)); page = max(1, page)
    offset = (page - 1) * per_page
    rows = conn.execute(f"SELECT * FROM student_status {where} ORDER BY last_checked DESC LIMIT ? OFFSET ?",
                        params + [per_page, offset]).fetchall()
    conn.close()
    return {"students": [dict(r) for r in rows], "total": total, "page": page,
            "per_page": per_page, "pages": max(1, (total + per_page - 1) // per_page)}

@app.get("/api/unknown-students")
async def unknown_students(page: int = 1, per_page: int = 50, search: str = "",
                           session_filter: str = "", user=Depends(verify_token)):
    """Students stuck at the 'Unknown' NIOS status — their own tab so they're easy to
    find, filter (by session) and re-run. Also returns the session list for the dropdown."""
    conn = get_db()
    wc = ["COALESCE(deleted,0)=0", "current_status='Unknown'"]
    params = []
    if search:
        like = f"%{search.strip()}%"
        wc.append("(student_name LIKE ? OR reference_no LIKE ? OR email LIKE ? OR enrollment_no LIKE ?)")
        params += [like, like, like, like]
    if session_filter:
        clause, sp = _session_clause(session_filter)
        wc.append(clause); params += sp
    where = "WHERE " + " AND ".join(wc)
    total = conn.execute(f"SELECT COUNT(*) FROM student_status {where}", params).fetchone()[0]
    per_page = max(1, min(per_page, 200)); page = max(1, page)
    offset = (page - 1) * per_page
    rows = conn.execute(f"SELECT * FROM student_status {where} ORDER BY last_checked DESC LIMIT ? OFFSET ?",
                        params + [per_page, offset]).fetchall()
    raw_sessions = conn.execute("SELECT DISTINCT session FROM student_status "
                                "WHERE COALESCE(deleted,0)=0 AND current_status='Unknown'").fetchall()
    norm_sessions = sorted({normalize_session(r["session"]) for r in raw_sessions})
    norm_sessions = [x for x in norm_sessions if x and x != "SYC"]
    conn.close()
    return {"students": [dict(r) for r in rows], "total": total, "page": page,
            "per_page": per_page, "pages": max(1, (total + per_page - 1) // per_page),
            "sessions": norm_sessions}

@app.post("/api/run-now-unknown")
async def run_now_unknown(background_tasks: BackgroundTasks, user=Depends(verify_token)):
    """Re-run ONLY the Unknown-status students (status auto-retries on each)."""
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM student_status WHERE COALESCE(deleted,0)=0 "
                     "AND current_status='Unknown'").fetchone()[0]
    conn.close()
    if n == 0:
        return {"message": "No Unknown students to re-check.", "count": 0}
    background_tasks.add_task(run_status_check, "all", None, "unknown")
    return {"message": f"Re-checking {n} Unknown student(s)…", "count": n}

@app.post("/api/mark-name-verified")
async def mark_name_verified(row_key: str = Form(...), user=Depends(verify_token)):
    """Counsellor confirms (after checking the portal) that the Reference No genuinely
    belongs to this student — the wrong-reference warning was a false alarm. Dismiss the
    error, remember it (name_verified=1) so the warning never re-appears, and drop the
    student into its correct bucket."""
    conn = get_db(); c = conn.cursor()
    r = c.execute("SELECT current_status FROM student_status WHERE row_key=?", (row_key,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(status_code=404, detail="Student not found")
    is_conf = 1 if (r["current_status"] or "") == "Admission Confirmed" else 0
    c.execute("""UPDATE student_status
                 SET name_verified=1, login_failed=0, check_failed=0, login_remark='',
                     whatsapp_info='', is_confirmed=?
                 WHERE row_key=?""", (is_conf, row_key))
    conn.commit(); conn.close()
    return {"ok": True, "confirmed": bool(is_conf)}

@app.get("/api/nav-count")
async def nav_count(user=Depends(verify_token)):
    """Live counts for the sidebar badges (active / confirmed / required / syc / failed)."""
    conn = get_db()
    ND = "COALESCE(deleted,0)=0"
    NF = "COALESCE(login_failed,0)=0 AND COALESCE(check_failed,0)=0"   # only in 'Failed to Run'
    def c(sql, *p):
        return conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND " + sql, p).fetchone()[0]
    active = c(NF + " AND COALESCE(is_confirmed,0)=0 AND COALESCE(current_status,'')!='SYC' "
               "AND (session IS NULL OR session NOT LIKE '%syc%')")
    confirmed = c(NF + " AND is_confirmed=1")
    required = c(NF + " AND current_status='Document Required'")
    syc = c(NF + " AND (session LIKE '%syc%' OR current_status='SYC')")
    failed = c("(COALESCE(login_failed,0)=1 OR COALESCE(check_failed,0)=1) AND COALESCE(current_status,'')!='Unknown'")
    unknown = c("current_status='Unknown'")
    conn.close()
    return {"students": active, "confirmed": confirmed, "required": required,
            "syc": syc, "failed": failed, "unknown": unknown}

@app.get("/api/failed-count")
async def failed_count(user=Depends(verify_token)):
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM student_status WHERE COALESCE(deleted,0)=0 "
                     "AND (COALESCE(login_failed,0)=1 OR COALESCE(check_failed,0)=1)").fetchone()[0]
    conn.close()
    return {"count": n}

@app.get("/api/export-students")
async def export_students(view: str="normal", search: str="", status_filter: str="",
                         session_filter: str="", class_filter: str="",
                         date_from: str="", date_to: str="", source_filter: str="",
                         user=Depends(verify_token)):
    """Export the CURRENTLY FILTERED list (active / confirmed / required) to .xlsx.
    Honours the exact same filters as the on-screen table (search, status, session,
    class 10/12, data type, and date range)."""
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    conn = get_db()
    where, params = _build_student_where(view, search, status_filter, session_filter,
                                         class_filter, date_from, date_to, source_filter)
    rows = conn.execute(f"SELECT * FROM student_status {where} ORDER BY student_name", params).fetchall()
    conn.close()

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Students"
    headers = ["#", "Reference No", "Student Name", "Mobile", "Class", "Email",
               "Session", "Status", "Data Type", "Remark", "Last Checked"]
    ws.append(headers)
    for i, r in enumerate(rows, 1):
        dt = "MVS Portal" if (r["source"] or "mvs_tracker") == "mvs_portal" else "MVS Tracker"
        ws.append([i, r["reference_no"], r["student_name"], r["mobile"], r["class_level"],
                   r["email"], r["session"], r["current_status"], dt, r["remark"], r["last_checked"]])
    hdr_fill = PatternFill("solid", fgColor="4F46E5")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = hdr_fill
    for idx, w in enumerate([5, 18, 26, 14, 8, 28, 22, 30, 44, 20], 1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.freeze_panes = "A2"

    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    label = {"confirmed": "confirmed", "required": "document_required"}.get(view, "active")
    fname = f"nios_{label}_students.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"})

@app.get("/api/history")
async def get_history(page: int = 1, per_page: int = 10, from_dt: str = "", to_dt: str = "",
                     status: str = "", source: str = "", search: str = "", user=Depends(verify_token)):
    conn = get_db()
    # Prefer the source STORED on the history row (set at write time). Fall back to a
    # reference-no lookup for old rows, then default. This keeps History in sync with
    # the Active/Confirmed pages.
    src_expr = ("COALESCE(NULLIF(status_history.source,''), "
                "(SELECT ss.source FROM student_status ss "
                "WHERE ss.reference_no != '' AND ss.reference_no = status_history.reference_no "
                "AND ss.source IS NOT NULL LIMIT 1), 'mvs_tracker')")
    cols = "id, reference_no, student_name, old_status, new_status, changed_at, run_id"
    wc, params = [], []
    if from_dt:
        wc.append("changed_at >= ?"); params.append(from_dt)
    if to_dt:
        wc.append("changed_at <= ?"); params.append(to_dt)
    if status:
        wc.append("new_status = ?"); params.append(status)
    if source == "both":
        wc.append("EXISTS (SELECT 1 FROM student_status ss2 "
                  "WHERE ss2.reference_no = status_history.reference_no "
                  "AND COALESCE(ss2.cross_dup,0) = 1)")
    elif source:
        wc.append(f"{src_expr} = ?"); params.append(source)
    if search:
        like = f"%{search.strip()}%"
        # Match reference no OR student name (stored on the row) OR the student's email
        # / enrollment (looked up from student_status by reference).
        wc.append("(status_history.reference_no LIKE ? OR status_history.student_name LIKE ? "
                  "OR EXISTS (SELECT 1 FROM student_status ss3 "
                  "WHERE ss3.reference_no = status_history.reference_no "
                  "AND (ss3.email LIKE ? OR ss3.enrollment_no LIKE ?)))")
        params += [like, like, like, like]
    where = ("WHERE " + " AND ".join(wc)) if wc else ""
    total = conn.execute(f"SELECT COUNT(*) FROM status_history {where}", params).fetchone()[0]
    per_page = max(1, min(per_page, 200))
    page = max(1, page)
    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT {cols}, {src_expr} AS source FROM status_history {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [per_page, offset]).fetchall()
    conn.close()
    pages = max(1, (total + per_page - 1) // per_page)
    return {"items": [dict(x) for x in rows], "total": total,
            "page": page, "pages": pages, "per_page": per_page}

@app.post("/api/history-delete")
async def history_delete(id: int, user=Depends(verify_token)):
    """Delete ONE status-change history entry by id."""
    conn = get_db()
    conn.execute("DELETE FROM status_history WHERE id=?", (id,))
    conn.commit(); conn.close()
    return {"ok": True}

@app.post("/api/history-clear")
async def history_clear(user=Depends(verify_token)):
    """Clear ALL status-change history (does not affect students)."""
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM status_history").fetchone()[0]
    conn.execute("DELETE FROM status_history")
    conn.commit(); conn.close()
    return {"ok": True, "deleted": n}

@app.get("/api/export-history")
async def export_history(from_dt: str = "", to_dt: str = "", status: str = "",
                         source: str = "", user=Depends(verify_token)):
    """Export the filtered Change History to .xlsx."""
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    conn = get_db()
    src_expr = ("COALESCE(NULLIF(status_history.source,''), "
                "(SELECT ss.source FROM student_status ss "
                "WHERE ss.reference_no != '' AND ss.reference_no = status_history.reference_no "
                "AND ss.source IS NOT NULL LIMIT 1), 'mvs_tracker')")
    cols = "id, reference_no, student_name, old_status, new_status, changed_at, run_id"
    wc, params = [], []
    if from_dt:
        wc.append("changed_at >= ?"); params.append(from_dt)
    if to_dt:
        wc.append("changed_at <= ?"); params.append(to_dt)
    if status:
        wc.append("new_status = ?"); params.append(status)
    if source == "both":
        wc.append("EXISTS (SELECT 1 FROM student_status ss2 "
                  "WHERE ss2.reference_no = status_history.reference_no "
                  "AND COALESCE(ss2.cross_dup,0) = 1)")
    elif source:
        wc.append(f"{src_expr} = ?"); params.append(source)
    where = ("WHERE " + " AND ".join(wc)) if wc else ""
    rows = conn.execute(f"SELECT {cols}, {src_expr} AS source FROM status_history {where} ORDER BY id DESC", params).fetchall()
    conn.close()

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Change History"
    headers = ["#", "Reference No", "Student Name", "Old Status", "New Status", "Data Type", "Changed At"]
    ws.append(headers)
    for i, r in enumerate(rows, 1):
        dt = "MVS Portal" if (r["source"] or "mvs_tracker") == "mvs_portal" else "MVS Tracker"
        ws.append([i, r["reference_no"], r["student_name"], r["old_status"],
                   r["new_status"], dt, r["changed_at"]])
    fill = PatternFill("solid", fgColor="4F46E5")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF"); cell.fill = fill
    for idx, w in enumerate([5, 18, 26, 28, 28, 16, 20], 1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.freeze_panes = "A2"
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=nios_change_history.xlsx"})

@app.get("/api/run-logs")
async def get_run_logs(limit: int=50, user=Depends(verify_token)):
    conn = get_db()
    l = conn.execute("SELECT * FROM run_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(x) for x in l]

@app.get("/api/transfers")
async def get_transfers(page: int = 1, per_page: int = 10, search: str = "",
                        mode: str = "", user=Depends(verify_token)):
    """Tracker -> Portal transfer log, with master search + pagination (10..100/page)."""
    conn = get_db()
    wc, params = [], []
    if search:
        like = f"%{search.strip()}%"
        wc.append("(student_name LIKE ? OR reference_no LIKE ? OR enrollment_no LIKE ? OR mobile LIKE ?)")
        params += [like, like, like, like]
    if mode in ("auto", "manual"):
        wc.append("mode=?"); params.append(mode)
    where = ("WHERE " + " AND ".join(wc)) if wc else ""
    total = conn.execute(f"SELECT COUNT(*) FROM transfer_log {where}", params).fetchone()[0]
    per_page = max(10, min(per_page, 100)); page = max(1, page)
    offset = (page - 1) * per_page
    rows = conn.execute(f"SELECT * FROM transfer_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                        params + [per_page, offset]).fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows], "total": total, "page": page,
            "per_page": per_page, "pages": max(1, (total + per_page - 1) // per_page)}

@app.post("/api/transfer-sync")
async def transfer_sync(user=Depends(verify_token)):
    """Manually push the current NIOS status + document links of every matched (Both /
    cross-source) student to MVS Portal, and log each as a 'manual' transfer."""
    import mvs_sync
    if not mvs_sync.enabled():
        raise HTTPException(status_code=400, detail="MVS Portal bridge is not enabled (set MVS_MODE).")
    try:
        portal = mvs_sync.fetch_students_for_tracker(include_done=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach MVS Portal: {e}")
    sid_by_key = {p["row_key"]: p.get("student_id", "") for p in portal if p.get("student_id")}
    conn = get_db(); c = conn.cursor()
    rows = c.execute("SELECT * FROM student_status WHERE COALESCE(deleted,0)=0 "
                     "AND COALESCE(cross_dup,0)=1").fetchall()
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pushed = 0
    for r in rows:
        rk = r["row_key"]; sid = sid_by_key.get(rk, "")
        if not sid:
            continue
        student = {"student_id": sid, "row_key": rk,
                   "reference_no": r["reference_no"] or "", "enrollment_no": r["enrollment_no"] or "",
                   "session": r["session"] or "", "remark": r["remark"] or ""}
        try:
            mvs_sync.push_student(student, r["current_status"] or "", conn)
            c.execute("""INSERT INTO transfer_log
                (row_key, reference_no, enrollment_no, student_name, mobile, session,
                 old_status, new_status, transferred_at, mode)
                VALUES (?,?,?,?,?,?,?,?,?,'manual')""",
                (rk, r["reference_no"] or "", r["enrollment_no"] or "", r["student_name"] or "",
                 r["mobile"] or "", r["session"] or "", r["current_status"] or "",
                 r["current_status"] or "", now_s))
            pushed += 1
        except Exception as e:
            logger.warning(f"transfer-sync push failed {rk}: {e}")
    conn.commit(); conn.close()
    return {"message": f"Synced {pushed} matched student(s) to MVS Portal.", "count": pushed}

@app.get("/api/portal-stale")
async def portal_stale(user=Depends(verify_token)):
    """Preview OLD / removed MVS Portal students: students we still track as source=mvs_portal
    whose record is NO LONGER present on the live portal (e.g. an old batch you removed).
    Returns the list only — nothing is deleted here. Guarded so a failed/empty fetch never
    flags everyone as stale."""
    import mvs_sync
    if not mvs_sync.enabled():
        raise HTTPException(status_code=400, detail="MVS Portal bridge is not enabled (set MVS_MODE).")
    try:
        portal = mvs_sync.fetch_students_for_tracker(include_done=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach MVS Portal: {e}")
    live_keys = {p["row_key"] for p in portal if p.get("row_key")}
    # Safety: if the portal returned nothing, refuse to flag anything (avoid mass-delete).
    if not live_keys:
        return {"ok": False, "fetched": 0, "stale": [],
                "message": "Portal returned 0 students — not flagging anything (try again later)."}
    conn = get_db()
    rows = conn.execute(
        "SELECT row_key, student_name, reference_no, enrollment_no, session, current_status, "
        "mobile, last_checked FROM student_status "
        "WHERE COALESCE(deleted,0)=0 AND COALESCE(source,'mvs_tracker')='mvs_portal' "
        "ORDER BY student_name").fetchall()
    conn.close()
    stale = []
    for r in rows:
        if r["row_key"] not in live_keys:
            stale.append({"row_key": r["row_key"], "student_name": r["student_name"] or "—",
                          "reference_no": r["reference_no"] or r["enrollment_no"] or "—",
                          "session": r["session"] or "—", "status": r["current_status"] or "—",
                          "mobile": r["mobile"] or "—", "last_checked": r["last_checked"] or "—"})
    return {"ok": True, "fetched": len(live_keys), "stale": stale, "count": len(stale)}

@app.post("/api/portal-to-tracker")
async def portal_to_tracker(body: dict, user=Depends(verify_token)):
    """Move selected students from MVS Portal -> MVS Tracker (keeps the record, just changes
    its data source). Used to preserve old portal records instead of deleting them: they stay
    tracked as Tracker data and are no longer tied to the live portal."""
    keys = [k for k in (body.get("row_keys", []) or []) if k][:1000]
    if not keys:
        raise HTTPException(status_code=400, detail="no students selected")
    conn = get_db()
    ph = ",".join("?" * len(keys))
    cur = conn.execute(
        f"UPDATE student_status SET source='mvs_tracker', cross_dup=0 "
        f"WHERE row_key IN ({ph}) AND COALESCE(deleted,0)=0", keys)
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": True, "moved": n}

@app.post("/api/run-now")
async def run_now(background_tasks: BackgroundTasks, user=Depends(verify_token)):
    background_tasks.add_task(run_status_check, "all")
    return {"message": "Run triggered for ALL students (Tracker + Portal)!"}

@app.post("/api/run-now-tracker")
async def run_now_tracker(background_tasks: BackgroundTasks, group: str = "all", user=Depends(verify_token)):
    """Manual run: MVS Tracker data, optionally limited to ONE session group so a
    counsellor can run just On Demand / Stream 2 / Public instead of every tracker
    student (saves CapSolver credits)."""
    g = group if group in ("ondemand", "stream2", "public") else "all"
    background_tasks.add_task(run_status_check, g, "mvs_tracker")
    label = {"ondemand": "On Demand", "stream2": "Stream 2", "public": "Public", "all": "all"}[g]
    return {"message": f"Run triggered for MVS Tracker — {label} students!"}

@app.post("/api/run-required")
async def run_now_required(background_tasks: BackgroundTasks, user=Depends(verify_token)):
    """Manual run: ONLY the students whose status is 'Document Required'. Lets a
    counsellor re-check just those after resolving their documents — so a fixed
    student moves out of the Required list without re-running everyone."""
    conn = get_db()
    ND = "COALESCE(deleted,0)=0 AND COALESCE(login_failed,0)=0 AND COALESCE(check_failed,0)=0"
    rows = conn.execute(
        f"SELECT row_key FROM student_status WHERE {ND} AND current_status='Document Required'"
    ).fetchall()
    conn.close()
    keys = [r["row_key"] for r in rows if r["row_key"]]
    if not keys:
        return {"message": "No Document Required students to run.", "count": 0}
    background_tasks.add_task(run_status_check, "all", None, "required", keys)
    return {"message": f"Re-checking {len(keys)} Document Required student(s)!", "count": len(keys)}

@app.post("/api/run-now-portal")
async def run_now_portal(background_tasks: BackgroundTasks, group: str = "all", user=Depends(verify_token)):
    """Manual run: MVS Portal data, optionally limited to ONE session group so a
    counsellor can run just On Demand / Stream 2 / Public instead of every verification
    student (saves CapSolver credits)."""
    g = group if group in ("ondemand", "stream2", "public") else "all"
    background_tasks.add_task(run_status_check, g, "mvs_portal")
    label = {"ondemand": "On Demand", "stream2": "Stream 2", "public": "Public", "all": "all"}[g]
    return {"message": f"Run triggered for MVS Portal — {label} students!"}

@app.post("/api/run-now-portal-new")
async def run_now_portal_new(background_tasks: BackgroundTasks, user=Depends(verify_token)):
    """Manual run: MVS Portal — NEW data only. Checks just the students that have arrived
    on the portal since last time (not already in the tracker), skipping all already-active
    students to save CapSolver credits."""
    background_tasks.add_task(run_status_check, "all", "mvs_portal", "new")
    return {"message": "Run triggered for MVS Portal — NEW data only!"}

@app.post("/api/run-now-upload")
async def run_now_upload(background_tasks: BackgroundTasks, user=Depends(verify_token)):
    """Run ONLY the students in the just-uploaded Excel sheet (MVS Tracker), not the
    whole database or MVS Portal. Used by the 'Run Check Now' button after upload."""
    background_tasks.add_task(run_status_check, "all", None, "upload")
    return {"message": "Checking only the uploaded students!"}

@app.post("/api/run-now-failed")
async def run_now_failed(background_tasks: BackgroundTasks, user=Depends(verify_token)):
    """Re-run EVERY 'Failed to Run' student with auto-fix: the status read auto-retries
    (transient Fetch Errors heal themselves) and a confirmed student's DOB is auto-flipped
    (date<->month) if that swap makes the NIOS login work. One-click backlog cleanup —
    the same fixes also run automatically on every normal run from now on."""
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM student_status WHERE COALESCE(deleted,0)=0 "
                     "AND (COALESCE(login_failed,0)=1 OR COALESCE(check_failed,0)=1)").fetchone()[0]
    conn.close()
    if n == 0:
        return {"message": "No failed students to re-check.", "count": 0}
    background_tasks.add_task(run_status_check, "all", None, "failed")
    return {"message": f"Re-checking {n} failed student(s) with auto-fix…", "count": n}

@app.post("/api/run-selected")
async def run_selected(body: dict, background_tasks: BackgroundTasks, user=Depends(verify_token)):
    """Run ONLY hand-picked students (1..20) — saves CapSolver credits when a counsellor
    needs just one or a few statuses right now instead of running everyone."""
    keys = body.get("row_keys") or []
    if not isinstance(keys, list):
        raise HTTPException(status_code=400, detail="row_keys must be a list")
    # de-dup + clean
    keys = [str(k) for k in keys if str(k).strip()]
    keys = list(dict.fromkeys(keys))
    if len(keys) < 1:
        raise HTTPException(status_code=400, detail="Select at least 1 student")
    if len(keys) > 20:
        raise HTTPException(status_code=400, detail="You can select a maximum of 20 students at a time")
    # keep only real, non-deleted students
    conn = get_db()
    qmarks = ",".join("?" * len(keys))
    valid = [r["row_key"] for r in conn.execute(
        f"SELECT row_key FROM student_status WHERE COALESCE(deleted,0)=0 AND row_key IN ({qmarks})",
        keys).fetchall()]
    conn.close()
    if not valid:
        raise HTTPException(status_code=404, detail="No valid students found for the selection")
    background_tasks.add_task(run_status_check, "all", None, "selected", valid)
    return {"message": f"Checking {len(valid)} selected student(s)…", "count": len(valid)}

@app.post("/api/cancel-run")
async def cancel_run(run_id: int = Form(...), user=Depends(verify_token)):
    conn = get_db()
    row = conn.execute("SELECT status FROM run_logs WHERE id=?", (run_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Run not found")
    if row["status"] != "running":
        conn.close()
        return {"message": f"Run already {row['status']}", "status": row["status"]}
    conn.execute("UPDATE run_logs SET status='cancelled' WHERE id=?", (run_id,))
    conn.commit()
    conn.close()
    return {"message": "Run cancelled", "status": "cancelled"}

@app.get("/api/progress")
async def run_progress(user=Depends(verify_token)):
    """Live progress of the currently running check (for the progress bar)."""
    conn = get_db()
    row = conn.execute("SELECT id, group_type, run_at, progress_current, progress_total, "
                       "progress_changed, progress_same, progress_total_mvs, progress_done_mvs, "
                       "progress_total_trk, progress_done_trk "
                       "FROM run_logs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        return {"running": False}
    cur = row["progress_current"] or 0
    tot = row["progress_total"] or 0
    pct = int(cur * 100 / tot) if tot else 0
    tm = row["progress_total_mvs"] or 0
    dm = row["progress_done_mvs"] or 0
    tt = row["progress_total_trk"] or 0
    dt = row["progress_done_trk"] or 0
    return {"running": True, "id": row["id"], "group_type": row["group_type"],
            "run_at": row["run_at"], "current": cur, "total": tot, "percent": pct,
            "changed": row["progress_changed"] or 0, "same": row["progress_same"] or 0,
            "remaining": max(0, tot - cur),
            "mvs": {"done": dm, "total": tm, "percent": int(dm*100/tm) if tm else 0},
            "trk": {"done": dt, "total": tt, "percent": int(dt*100/tt) if tt else 0}}

GROUP_LABELS = {"ondemand": "On Demand", "stream2": "Stream 2",
                "public": "Public (April / October)", "portalnew": "MVS Portal — New data"}
_GROUP_JID = {"ondemand": "job_ondemand", "stream2": "job_stream2", "public": "job_public", "portalnew": "job_portalnew"}
_GROUP_DH = {"ondemand": 6, "stream2": 6, "public": 12, "portalnew": 3}

def _job_remaining_sec(jid):
    """Seconds until a scheduled job's next run (None if not scheduled)."""
    job = scheduler.get_job(jid)
    if not job or not job.next_run_time:
        return None
    nrt = job.next_run_time
    if nrt.tzinfo is None:
        delta = (nrt - datetime.now()).total_seconds()
    else:
        delta = (nrt - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(delta))

@app.get("/api/next-runs")
async def next_runs(user=Depends(verify_token)):
    """Seconds remaining until the next automatic run of each group, plus that group's
    own paused flag (each group can be paused independently)."""
    out = []
    for grp, jid, _dh in RUN_GROUPS:
        paused = get_setting(f"paused_{grp}", "") == "1"
        if paused:
            try:
                secs = int(get_setting(f"pause_remaining_{grp}_sec", "") or 0)
            except Exception:
                secs = None
        else:
            secs = _job_remaining_sec(jid)
        out.append({"group": grp, "label": GROUP_LABELS.get(grp, grp),
                    "seconds": secs, "paused": paused})
    # MVS Portal — New data timer
    pn_paused = get_setting("paused_portalnew", "") == "1"
    if pn_paused:
        try:
            pn_secs = int(get_setting("pause_remaining_portalnew_sec", "") or 0)
        except Exception:
            pn_secs = None
    else:
        pn_secs = _job_remaining_sec("job_portalnew")
    out.append({"group": "portalnew", "label": GROUP_LABELS["portalnew"],
                "seconds": pn_secs, "paused": pn_paused})
    return {"runs": out}

def _valid_group(g):
    if g not in ("ondemand", "stream2", "public", "portalnew"):
        raise HTTPException(status_code=400, detail="group must be 'ondemand', 'stream2', 'public' or 'portalnew'")
    return g

@app.get("/api/report-settings")
async def get_report_settings(user=Depends(verify_token)):
    return {
        "enabled": get_setting("report_enabled", "") == "1",
        "numbers": get_setting("report_numbers", ""),
        "campaign_set": bool(os.environ.get("AISENSY_CAMPAIGN_REPORT", "").strip()),
        "last_status": get_setting("report_last_status", ""),
    }

@app.post("/api/report-settings")
async def save_report_settings(body: dict, user=Depends(verify_token)):
    enabled = "1" if body.get("enabled") else ""
    raw = str(body.get("numbers", "") or "")
    # Keep only valid-ish numbers (digits, 10–13 long after cleaning), comma-joined.
    import re as _re
    cleaned = []
    for part in raw.replace("\n", ",").split(","):
        d = _re.sub(r"\D", "", part)
        if 10 <= len(d) <= 13:
            cleaned.append(d)
    if len(cleaned) > 10:
        cleaned = cleaned[:10]
    set_setting("report_enabled", enabled)
    set_setting("report_numbers", ",".join(cleaned))
    return {"enabled": enabled == "1", "numbers": ",".join(cleaned), "count": len(cleaned)}

@app.post("/api/report-test")
async def report_test(user=Depends(verify_token)):
    """Send a sample report right now to the configured numbers, to verify setup."""
    import whatsapp, links
    raw = (get_setting("report_numbers", "") or "").replace("\n", ",")
    nums = [n.strip() for n in raw.split(",") if n.strip()]
    if not nums:
        raise HTTPException(status_code=400, detail="Add at least one WhatsApp number first")
    today = datetime.now().strftime("%Y-%m-%d")
    when = datetime.now().strftime("%d %b, %I:%M %p")
    url = links.report_url(today)
    # live current counts for a realistic test
    conn = get_db(); ND = "COALESCE(deleted,0)=0"
    conf = conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND is_confirmed=1").fetchone()[0]
    req = conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND current_status='Document Required'").fetchone()[0]
    ver = conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND current_status='Verified'").fetchone()[0]
    dvp = conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND current_status='Documents Verification In Progress'").fetchone()[0]
    err = conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND current_status IN ('Unknown','Fetch Error')").fetchone()[0]
    conn.close()
    params = whatsapp.make_report_params(f"TEST report - {when}", conf, req, err, 0,
                                         conf + req + err + ver + dvp, url)
    sent, errs = whatsapp.send_report_to_all(nums, params)
    return {"sent": sent, "total": len(nums), "errors": errs}

@app.get("/api/recent-runs")
async def recent_runs(user=Depends(verify_token)):
    """Recent completed runs that have a saved report, for the manual 'send report' picker."""
    import json as _json
    conn = get_db()
    rows = conn.execute("SELECT id, group_type, run_at, report_label, report_json, total_checked "
                        "FROM run_logs WHERE report_json IS NOT NULL AND report_json != '' "
                        "ORDER BY id DESC LIMIT 15").fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            st = _json.loads(r["report_json"])
        except Exception:
            st = {}
        out.append({
            "id": r["id"],
            "label": r["report_label"] or (r["group_type"] or "Run"),
            "run_at": r["run_at"],
            "confirmed": st.get("confirmed", 0),
            "required": st.get("required", 0),
            "checked": st.get("checked", r["total_checked"] or 0),
        })
    return out

@app.post("/api/send-run-report")
async def send_run_report(body: dict, user=Depends(verify_token)):
    """Send a SPECIFIC past run's report (chosen from the picker) to all management numbers."""
    import whatsapp, links, json as _json
    run_id = body.get("run_id")
    raw = (get_setting("report_numbers", "") or "").replace("\n", ",")
    nums = [n.strip() for n in raw.split(",") if n.strip()]
    if not nums:
        raise HTTPException(status_code=400, detail="Add at least one management WhatsApp number in Settings first")
    if not os.environ.get("AISENSY_CAMPAIGN_REPORT", "").strip():
        raise HTTPException(status_code=400, detail="AISENSY_CAMPAIGN_REPORT env var is not set on Railway")
    conn = get_db()
    r = conn.execute("SELECT report_json, report_label FROM run_logs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    if not r or not r["report_json"]:
        raise HTTPException(status_code=404, detail="That run's report was not found")
    st = _json.loads(r["report_json"])
    label = r["report_label"] or "Run report"
    same = max(0, st.get("checked", 0) - st.get("changed", 0))
    url = links.report_url(datetime.now().strftime("%Y-%m-%d"))
    # Confirmed / Required / Error from LIVE totals (match the Excel Summary), like the
    # automatic report. Total-checked + unchanged stay as that run's own numbers.
    import job_runner as _jr
    live = _jr._live_report_counts()
    params = whatsapp.make_report_params(label, live["confirmed"], live["required"],
                                         live["error"], same, st.get("checked", 0), url)
    sent, errs = whatsapp.send_report_to_all(nums, params)
    stamp = datetime.now().strftime("%d %b %I:%M %p")
    set_setting("report_last_status",
                (f"Sent '{label}' to {sent}/{len(nums)} on {stamp}." if sent
                 else f"FAILED on {stamp}: " + ("; ".join(errs)[:200] if errs else "unknown error")))
    return {"sent": sent, "total": len(nums), "errors": errs}

@app.post("/api/send-latest-report")
async def send_latest_report(user=Depends(verify_token)):
    """Push the latest run report to ALL configured management numbers right now —
    a manual safety net for when the auto-report did not reach everyone."""
    import whatsapp, links
    raw = (get_setting("report_numbers", "") or "").replace("\n", ",")
    nums = [n.strip() for n in raw.split(",") if n.strip()]
    if not nums:
        raise HTTPException(status_code=400, detail="Add at least one management WhatsApp number in Settings first")
    if not os.environ.get("AISENSY_CAMPAIGN_REPORT", "").strip():
        raise HTTPException(status_code=400, detail="AISENSY_CAMPAIGN_REPORT env var is not set on Railway — add it, then redeploy")
    conn = get_db(); ND = "COALESCE(deleted,0)=0"
    last = conn.execute("SELECT group_type, run_at FROM run_logs "
                        "WHERE status LIKE 'completed%' OR status LIKE 'done%' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    conf = conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND is_confirmed=1").fetchone()[0]
    req = conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND current_status='Document Required'").fetchone()[0]
    ver = conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND current_status='Verified'").fetchone()[0]
    dvp = conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND current_status='Documents Verification In Progress'").fetchone()[0]
    err = conn.execute(f"SELECT COUNT(*) FROM student_status WHERE {ND} AND current_status IN ('Unknown','Fetch Error')").fetchone()[0]
    conn.close()
    today = datetime.now().strftime("%Y-%m-%d")
    when = datetime.now().strftime("%d %b, %I:%M %p")
    label = (last["group_type"].title() + " run" if last and last["group_type"] else "Latest run")
    url = links.report_url(today)
    params = whatsapp.make_report_params(f"{label} - {when}", conf, req, err, 0,
                                         conf + req + err + ver + dvp, url)
    sent, errs = whatsapp.send_report_to_all(nums, params)
    stamp = datetime.now().strftime("%d %b %I:%M %p")
    set_setting("report_last_status",
                (f"Sent to {sent}/{len(nums)} on {stamp} (manual)." if sent
                 else f"FAILED on {stamp}: " + ("; ".join(errs)[:200] if errs else "unknown error")))
    return {"sent": sent, "total": len(nums), "errors": errs}

@app.post("/api/intervals-pause")
async def intervals_pause(body: dict, user=Depends(verify_token)):
    """Freeze ONE group's auto-run timer (regular OR public): capture how long is left,
    then stop just that group's schedule. Resuming continues from this remaining time."""
    grp = _valid_group((body or {}).get("group", ""))
    jid = _GROUP_JID[grp]
    dh = _GROUP_DH[grp]
    rem = _job_remaining_sec(jid)
    if rem is None:
        rem = _interval_minutes(grp, dh) * 60
    set_setting(f"pause_remaining_{grp}_sec", rem)
    try:
        scheduler.remove_job(jid)
    except Exception:
        pass
    set_setting(f"paused_{grp}", "1")
    return {"paused": True, "group": grp,
            "message": f"{GROUP_LABELS.get(grp, grp)} paused — timer frozen."}

@app.post("/api/intervals-resume")
async def intervals_resume(body: dict, user=Depends(verify_token)):
    """Resume ONE group: re-schedule it to fire after its FROZEN remaining time, so the
    countdown picks up exactly where it was paused (not reset to a full interval)."""
    grp = _valid_group((body or {}).get("group", ""))
    jid = _GROUP_JID[grp]
    dh = _GROUP_DH[grp]
    mins = _interval_minutes(grp, dh)
    try:
        rem = int(get_setting(f"pause_remaining_{grp}_sec", "") or 0)
    except Exception:
        rem = 0
    if rem <= 0:
        rem = mins * 60
    if grp == "portalnew":
        job_fn = lambda: run_status_check("all", "mvs_portal", "new")
    else:
        job_fn = lambda g=grp: run_status_check(g, _group_source(g))
    scheduler.add_job(job_fn,
                      trigger=IntervalTrigger(minutes=mins),
                      id=jid, replace_existing=True,
                      next_run_time=datetime.now() + timedelta(seconds=rem))
    set_setting(f"paused_{grp}", "")
    set_setting(f"pause_remaining_{grp}_sec", "")
    return {"paused": False, "group": grp,
            "message": f"{GROUP_LABELS.get(grp, grp)} resumed — timer continues from where it was paused."}

@app.post("/api/upload-excel")
async def upload_excel(file: UploadFile = File(...), user=Depends(verify_token)):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only .xlsx files allowed")
    async with aiofiles.open(EXCEL_PATH, "wb") as f:
        content = await file.read()
        await f.write(content)

    # Parse the uploaded sheet to report counts + a preview. A student is a
    # "duplicate" if it already exists in the system (matched by reference OR
    # email OR name+mobile) OR appears more than once within this file. Only the
    # genuinely NEW students are counted as new.
    resp = {"message": f"Excel uploaded ({len(content)} bytes)", "filename": file.filename,
            "total": 0, "duplicates": 0, "unique": 0, "preview": []}
    try:
        from excel_handler import read_students_from_excel

        def _norm(v):
            return str(v or "").strip().lower()

        def _real_email(e):
            e = _norm(e)
            return ("@" in e and "." in e.split("@")[-1]
                    and e not in ("temp", "na", "none", "nil", "-", "null", "n/a"))

        def _valid_mobile(m):
            d = "".join(ch for ch in str(m or "") if ch.isdigit())
            return d if len(d) >= 10 else ""

        def _rowkey(ref, enroll, email):
            # Reference number is NIOS's TRUE unique id, so it decides identity FIRST.
            # (Siblings often share an email/phone but have different reference numbers —
            # they must NOT be merged.) Email is only a fallback when there is no reference.
            if ref:
                return "ref:" + ref.strip(), "Reference No"
            if _real_email(email):
                return "email:" + _norm(email), "Email"
            if enroll:
                return "enr:" + enroll.strip(), "Enrollment No"
            return "", ""

        # Existing students: keep enough to SHOW which student a new row matches, so the
        # counsellor can verify a real duplicate vs. a sibling sharing a phone/email.
        conn = get_db()
        existing_by_key, existing_by_ref = {}, {}
        existing_by_mob, existing_by_email = {}, {}
        for r in conn.execute("SELECT row_key, student_name, reference_no, enrollment_no, mobile, email "
                              "FROM student_status WHERE COALESCE(deleted,0)=0").fetchall():
            nm = r["student_name"] or ""
            rf = (r["reference_no"] or r["enrollment_no"] or "").strip()
            if r["row_key"]:
                existing_by_key[r["row_key"]] = (nm, rf)
            if r["reference_no"] and r["reference_no"].strip():
                existing_by_ref[r["reference_no"].strip()] = (nm, rf)
            mb = _valid_mobile(r["mobile"])
            if mb:
                existing_by_mob.setdefault(mb, (nm, rf))
            em = _norm(r["email"])
            if _real_email(em):
                existing_by_email.setdefault(em, (nm, rf))
        conn.close()

        students = read_students_from_excel(EXCEL_PATH)
        seen_keys, seen_ref, seen_mob, seen_email = {}, {}, {}, {}
        preview = []
        dups = 0
        sims = 0
        for s in students:
            ref = str(s.get("reference_no") or "").strip()
            enroll = str(s.get("enrollment_no") or "").strip()
            email = str(s.get("email") or "")
            em = _norm(email)
            mob = _valid_mobile(s.get("mobile"))
            nm = s.get("student_name", "")
            rk, basis = _rowkey(ref, enroll, email)

            # HARD duplicate = SAME reference (true NIOS id) or same unique key.
            # This is genuinely the same student — at import it is merged & re-checked.
            hard = False
            match = None      # (name, ref) of the student this row matches
            if ref and ref in existing_by_ref:
                hard, basis, match = True, "Reference No", existing_by_ref[ref]
            elif ref and ref in seen_ref:
                hard, basis, match = True, "Reference No", seen_ref[ref]
            elif rk and rk in existing_by_key:
                hard, match = True, existing_by_key[rk]
            elif rk and rk in seen_keys:
                hard, match = True, seen_keys[rk]

            # SOFT "similar" = a DIFFERENT student (different reference) who happens to
            # share a mobile OR an email. NOT merged — both run. Shown so the counsellor
            # can confirm (e.g. siblings) or remove a true duplicate afterwards.
            similar = ""
            if not hard:
                if mob and mob in existing_by_mob:
                    similar, match = "Mobile No", existing_by_mob[mob]
                elif mob and mob in seen_mob:
                    similar, match = "Mobile No", seen_mob[mob]
                elif em and _real_email(em) and em in existing_by_email:
                    similar, match = "Email", existing_by_email[em]
                elif em and _real_email(em) and em in seen_email:
                    similar, match = "Email", seen_email[em]

            # Record this row so later rows in the same sheet can match against it.
            if rk:
                seen_keys.setdefault(rk, (nm, ref or enroll))
            if ref:
                seen_ref.setdefault(ref, (nm, ref))
            if mob:
                seen_mob.setdefault(mob, (nm, ref or enroll))
            if em and _real_email(em):
                seen_email.setdefault(em, (nm, ref or enroll))

            if hard:
                dups += 1
            elif similar:
                sims += 1
            preview.append({
                "student_name": nm,
                "reference_no": s.get("reference_no", ""),
                "enrollment_no": s.get("enrollment_no", ""),
                "email": s.get("email", ""),
                "class_level": s.get("class_level", ""),
                "session": s.get("session", ""),
                "dob": s.get("dob", ""),
                "mobile": s.get("mobile", ""),
                "dup": hard,
                "dup_basis": basis if hard else "",
                "similar": similar,
                "match_name": (match[0] if match else ""),
                "match_ref": (match[1] if match else ""),
            })
        total = len(students)
        resp.update({"total": total, "duplicates": dups, "similar": sims,
                     "unique": total - dups - sims, "preview": preview[:2000]})
    except Exception as e:
        resp["parse_error"] = str(e)[:200]
    return resp

@app.get("/api/download-excel")
async def download_excel(user=Depends(verify_token)):
    if not os.path.exists(EXCEL_PATH):
        raise HTTPException(status_code=404, detail="Excel file not found")
    return FileResponse(EXCEL_PATH,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="nios_status_updated.xlsx")

@app.get("/api/sample-sheet")
async def sample_sheet(type: str = "regular", user=Depends(verify_token)):
    """Generate a ready-to-fill sample Excel so counsellors always know the format."""
    import io, openpyxl
    from openpyxl.utils import get_column_letter
    from fastapi import Response
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Students"
    headers = ["STUDENT NAME", "MOBILE NO", "ALTERNATE NUMBER (optional)", "CLASS", "REFERENCE NUMBER", "Enrol No", "Email",
               "Date of Birth", "ADMISSION SESSION", "ADMISSION STATUS", "REMARKS",
               "DOWNLOAD ID CARD", "DOWNLOAD APPLICATION FORM", "HALL TICKET"]
    ws.append(headers)
    if type == "syc":
        rows = [
            ["AYUSH KUMAR", "9876543210", "9988776655", "12TH", "", "220004253089", "", "19-06-2006", "SYC"],
            ["PETER RANA", "7428240153", "", "12TH", "", "50258253204", "peter@example.com", "17-05-2001", "SYC"],
        ]
    else:
        rows = [
            ["SABBA NOOR", "6205148930", "8123456789", "12TH", "D1026300062", "", "", "05-02-2010", "On Demand"],
            ["DEVRAJ JAT", "7737485139", "", "10TH", "B0926200020", "", "", "01-07-2004", "Stream 2"],
            ["SANA PARWEEN", "9523534252", "9012345678", "12TH", "A1026300040", "", "sana@example.com", "28-02-2009", "April"],
        ]
    for r in rows:
        ws.append(r)
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(h) + 2)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fn = "MVS_sample_syc.xlsx" if type == "syc" else "MVS_sample_regular.xlsx"
    return Response(content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'})

@app.get("/api/sample-alt-sheet")
async def sample_alt_sheet(user=Depends(verify_token)):
    """Two-column sample for the bulk alternate-number upload."""
    import io, openpyxl
    from fastapi import Response
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Alternate Numbers"
    headers = ["Reference No", "Alternate Number"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4F46E5")
    for r in [["D1026300062", "9988776655"], ["B0926200020", "9012345678"], ["A1026300040", "8123456789"]]:
        ws.append(r)
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 20
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return Response(content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="MVS_alternate_numbers_sample.xlsx"'})

def _send_alt_to_confirmed(row_keys):
    """Send the documents ONLY to the alternate number for already-confirmed students
    (their primary number already received them, so it is not messaged again)."""
    if get_setting("wa_enabled", "0") != "1":
        return
    import whatsapp
    conn = get_db(); c = conn.cursor()
    sent = 0
    for rk in row_keys:
        r = c.execute("SELECT * FROM student_status WHERE row_key=?", (rk,)).fetchone()
        if not r:
            continue
        alt = (r["alt_mobile"] if "alt_mobile" in r.keys() else "") or ""
        if not alt:
            continue
        try:
            ok, info = whatsapp.send_for_student({
                "row_key": rk, "student_name": r["student_name"] or "",
                "mobile": r["mobile"] or "", "alt_mobile": alt,
                "session": r["session"] or "", "reference_no": r["reference_no"] or "",
                "dob": r["dob"] or ""}, only_number=alt)
            if ok:
                sent += 1
                c.execute("UPDATE student_status SET whatsapp_info=? WHERE row_key=?",
                          (f"Sent to 2 numbers (own + alternate {alt})", rk))
                conn.commit()
            logger.info(f"Alt-send {rk} -> {alt}: {'ok' if ok else 'FAIL'} | {info}")
        except Exception as e:
            logger.warning(f"Alt-send error {rk}: {e}")
    conn.close()
    logger.info(f"Alternate-number send complete: {sent}/{len(row_keys)}")

@app.post("/api/upload-alt-numbers")
async def upload_alt_numbers(background_tasks: BackgroundTasks,
                             file: UploadFile = File(...), user=Depends(verify_token)):
    """Bulk add/update ALTERNATE WhatsApp numbers for students already in the system.
    Sheet needs a Reference No column and an Alternate Number column (header names are
    matched flexibly). Matches by reference (then enrollment), updates alt_mobile, and for
    students that are ALREADY confirmed, sends the documents to the new alternate number."""
    if not (file.filename or "").lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only .xlsx files allowed")
    import io, openpyxl
    content = await file.read()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read the file: {e}")
    ws = wb.active
    data = list(ws.iter_rows(values_only=True))
    if not data:
        return {"message": "The sheet is empty.", "updated": 0, "not_found": 0, "to_send": 0}
    header = [str(h or "").strip().lower() for h in data[0]]

    def _find(cands):
        for i, h in enumerate(header):
            if any(cc in h for cc in cands):
                return i
        return -1
    ref_i = _find(["reference", "ref no", "ref_no", "refno", "ref"])
    enr_i = _find(["enrollment", "enrol"])
    alt_i = _find(["alternate", "alt", "second", "whatsapp", "other"])
    if ref_i < 0 and enr_i < 0:
        return {"message": "No 'Reference No' (or Enrollment) column found in the sheet.",
                "updated": 0, "not_found": 0, "to_send": 0}
    if alt_i < 0:
        return {"message": "No 'Alternate Number' column found in the sheet.",
                "updated": 0, "not_found": 0, "to_send": 0}

    def _digits(v):
        return "".join(ch for ch in str(v or "") if ch.isdigit())

    conn = get_db(); c = conn.cursor()
    updated, not_found, send_keys = 0, 0, []
    for row in data[1:]:
        if not row:
            continue
        ref = str(row[ref_i] or "").strip() if 0 <= ref_i < len(row) else ""
        enr = str(row[enr_i] or "").strip() if 0 <= enr_i < len(row) else ""
        alt = _digits(row[alt_i]) if alt_i < len(row) else ""
        if not alt or len(alt) < 10:
            continue
        rec = None
        if ref:
            rec = c.execute("SELECT row_key, mobile, is_confirmed FROM student_status "
                            "WHERE reference_no=? AND COALESCE(deleted,0)=0 LIMIT 1", (ref,)).fetchone()
        if rec is None and enr:
            rec = c.execute("SELECT row_key, mobile, is_confirmed FROM student_status "
                            "WHERE enrollment_no=? AND COALESCE(deleted,0)=0 LIMIT 1", (enr,)).fetchone()
        if rec is None:
            not_found += 1
            continue
        if alt == _digits(rec["mobile"]):
            continue   # alternate same as primary — skip
        c.execute("UPDATE student_status SET alt_mobile=? WHERE row_key=?", (alt, rec["row_key"]))
        updated += 1
        if rec["is_confirmed"] == 1:
            send_keys.append(rec["row_key"])
    conn.commit(); conn.close()

    wa_on = get_setting("wa_enabled", "0") == "1"
    if send_keys and wa_on:
        background_tasks.add_task(_send_alt_to_confirmed, send_keys)
    msg = f"Updated the alternate number for {updated} student(s)."
    if not_found:
        msg += f" {not_found} reference(s) were not found in the system."
    if send_keys:
        msg += (f" Sending documents to the alternate number for {len(send_keys)} confirmed student(s) in the background…"
                if wa_on else f" {len(send_keys)} are confirmed — turn ON WhatsApp in Settings to send to the alternate number.")
    return {"message": msg, "updated": updated, "not_found": not_found,
            "to_send": (len(send_keys) if wa_on else 0)}

@app.get("/api/intervals")
async def get_intervals(user=Depends(verify_token)):
    return {"ondemand_min": _interval_minutes("ondemand", 6),
            "stream2_min": _interval_minutes("stream2", 6),
            "public_min": _interval_minutes("public", 12),
            "portalnew_min": _interval_minutes("portalnew", 3),
            "ondemand_src": get_setting("runsrc_ondemand", "both") or "both",
            "stream2_src": get_setting("runsrc_stream2", "both") or "both",
            "public_src": get_setting("runsrc_public", "both") or "both"}

@app.post("/api/intervals")
async def set_intervals(body: dict, user=Depends(verify_token)):
    od = int(body.get("ondemand_min", 360))
    st = int(body.get("stream2_min", 360))
    pub = int(body.get("public_min", 720))
    pn = int(body.get("portalnew_min", 180))
    MAX_MIN = 43200   # 30 days — lets counsellors run every few days, not just hours
    for v in (od, st, pub, pn):
        if not (15 <= v <= MAX_MIN):
            raise HTTPException(status_code=400, detail="Interval must be between 15 minutes and 30 days")
    set_setting("interval_ondemand_min", od)
    set_setting("interval_stream2_min", st)
    set_setting("interval_public_min", pub)
    set_setting("interval_portalnew_min", pn)
    set_setting("interval_regular_min", "")   # retire the old combined setting
    # Per-group DATA SOURCE for auto runs: both (default) | mvs_tracker | mvs_portal.
    def _vsrc(x):
        x = str(x or "both")
        return x if x in ("both", "mvs_tracker", "mvs_portal") else "both"
    set_setting("runsrc_ondemand", _vsrc(body.get("ondemand_src")))
    set_setting("runsrc_stream2", _vsrc(body.get("stream2_src")))
    set_setting("runsrc_public", _vsrc(body.get("public_src")))
    # Saving new intervals restarts the timers fresh, so clear any paused state.
    for grp in ("ondemand", "stream2", "public", "portalnew"):
        set_setting(f"paused_{grp}", "")
        set_setting(f"pause_remaining_{grp}_sec", "")
    reschedule_jobs()
    def _fmt(m):
        if m % 1440 == 0:
            d = m // 1440
            return f"{d} day" + ("s" if d != 1 else "")
        if m % 60 == 0:
            return f"{m//60}h"
        if m < 60:
            return f"{m}m"
        return f"{m//60}h {m%60}m"
    return {"message": f"On Demand: every {_fmt(od)}, Stream 2: every {_fmt(st)}, Public: every {_fmt(pub)}",
            "ondemand_min": od, "stream2_min": st, "public_min": pub}

@app.get("/api/source-counts")
async def source_counts(user=Depends(verify_token)):
    """How many active students are tagged MVS Portal vs MVS Tracker (for the move tool)."""
    conn = get_db()
    rows = conn.execute("SELECT COALESCE(NULLIF(source,''),'mvs_tracker') AS src, COUNT(*) AS n "
                        "FROM student_status WHERE COALESCE(deleted,0)=0 GROUP BY src").fetchall()
    conn.close()
    out = {"mvs_portal": 0, "mvs_tracker": 0}
    for r in rows:
        key = "mvs_portal" if r["src"] == "mvs_portal" else "mvs_tracker"
        out[key] += r["n"]
    return out

@app.post("/api/change-source-bulk")
async def change_source_bulk(body: dict, user=Depends(verify_token)):
    """Move EVERY active student from one data type to another (e.g. a sheet that was
    auto-detected as MVS Portal by mistake but should be MVS Tracker). No student is
    deleted — only the 'source' tag changes, so nothing is lost."""
    frm = body.get("from_source", "")
    to = body.get("to_source", "")
    if frm not in ("mvs_portal", "mvs_tracker") or to not in ("mvs_portal", "mvs_tracker") or frm == to:
        raise HTTPException(status_code=400, detail="pick two different data types")
    conn = get_db()
    cur = conn.execute("UPDATE student_status SET source=? "
                       "WHERE COALESCE(deleted,0)=0 AND COALESCE(NULLIF(source,''),'mvs_tracker')=?",
                       (to, frm))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": True, "moved": n, "from": frm, "to": to}

@app.post("/api/change-source-selected")
async def change_source_selected(body: dict, user=Depends(verify_token)):
    """Move ONLY the selected students to a data type (precise control from the list)."""
    keys = [k for k in (body.get("row_keys", []) or []) if k][:5000]
    to = body.get("to_source", "")
    if to not in ("mvs_portal", "mvs_tracker"):
        raise HTTPException(status_code=400, detail="invalid data type")
    if not keys:
        raise HTTPException(status_code=400, detail="no students selected")
    conn = get_db()
    ph = ",".join("?" * len(keys))
    cur = conn.execute(f"UPDATE student_status SET source=? WHERE row_key IN ({ph})", [to] + keys)
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": True, "moved": n, "to": to}

@app.get("/api/source-override")
async def get_source_override(user=Depends(verify_token)):
    return {"value": get_setting("source_override", "")}

@app.post("/api/source-override")
async def set_source_override(value: str = "", user=Depends(verify_token)):
    """Force the data type of the NEXT run's sheet. Empty = auto-detect."""
    if value not in ("", "mvs_portal", "mvs_tracker"):
        raise HTTPException(status_code=400, detail="invalid value")
    set_setting("source_override", value)
    return {"ok": True, "value": value}

@app.get("/api/debug-login")
async def debug_login_endpoint(ref: str, dob: str, action: str = "", user=Depends(verify_token)):
    """Phase 2 debug: login with a confirmed student & discover download links."""
    try:
        from nios_login import debug_login
        result = debug_login(ref, dob, action or None)
        return result
    except Exception as e:
        return {"error": str(e)}

def build_report_excel(path):
    """Build the run-report Excel: a Summary sheet plus Confirmed / Document Required /
    Error & Unknown student lists (current actionable state). Returns the counts."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    conn = get_db()
    ND = "COALESCE(deleted,0)=0"
    NF = "COALESCE(login_failed,0)=0 AND COALESCE(check_failed,0)=0"
    cols = ("student_name, reference_no, enrollment_no, dob, session, mobile, email, "
            "current_status, COALESCE(NULLIF(login_remark,''),NULLIF(remark,'')) as remark, last_checked")
    def rows(where):
        return conn.execute(f"SELECT {cols} FROM student_status WHERE {ND} AND {where} "
                            f"ORDER BY student_name").fetchall()
    confirmed = rows(f"is_confirmed=1 AND {NF}")
    # "Confirmed Today" = confirmations since the start of today — BUT if the latest run
    # CROSSED MIDNIGHT (started yesterday ~11 PM, finished after 12 AM), count from that
    # run's start instead, so the whole run's confirmations stay together and aren't split
    # by the date change. Same-day morning/evening runs are unaffected (cutoff = midnight).
    now = datetime.now()
    midnight = now.strftime("%Y-%m-%d") + " 00:00:00"
    cutoff = midnight
    try:
        lr = conn.execute("SELECT run_at FROM run_logs ORDER BY id DESC LIMIT 1").fetchone()
        if lr and lr["run_at"]:
            ra = str(lr["run_at"])
            recent = (now - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
            if ra < midnight and ra >= recent:
                cutoff = ra   # run began before midnight & is recent -> include from run start
    except Exception:
        pass
    confirmed_today = rows(f"is_confirmed=1 AND {NF} AND last_changed >= '{cutoff}'")
    required = rows(f"current_status='Document Required' AND {NF}")
    verified = rows(f"current_status='Verified' AND {NF}")
    docsprog = rows(f"current_status='Documents Verification In Progress' AND {NF}")
    error = rows("(current_status IN ('Unknown','Fetch Error') "
                 "OR COALESCE(check_failed,0)=1 OR COALESCE(login_failed,0)=1)")
    # Session-wise breakdown (same definitions as the dashboard) for the Summary sheet.
    sess_raw = conn.execute(
        f"SELECT session, COUNT(*) as cnt, "
        f"SUM(CASE WHEN is_confirmed=1 THEN 1 ELSE 0 END) as confirmed, "
        f"SUM(CASE WHEN COALESCE(is_confirmed,0)=0 AND current_status='Verified' THEN 1 ELSE 0 END) as verified, "
        f"SUM(CASE WHEN COALESCE(is_confirmed,0)=0 AND current_status='Documents Verification In Progress' THEN 1 ELSE 0 END) as docsv, "
        f"SUM(CASE WHEN current_status='Document Required' THEN 1 ELSE 0 END) as req, "
        f"SUM(CASE WHEN COALESCE(is_confirmed,0)=0 AND COALESCE(current_status,'')!='SYC' "
        f"         AND (session IS NULL OR session NOT LIKE '%syc%') THEN 1 ELSE 0 END) as active "
        f"FROM student_status WHERE {ND} AND {NF} AND session != '' GROUP BY session"
    ).fetchall()
    conn.close()

    sess_agg = {}
    for r in sess_raw:
        norm = normalize_session(r["session"])
        if not norm:
            continue
        d = sess_agg.setdefault(norm, {"total": 0, "confirmed": 0, "verified": 0,
                                       "docsv": 0, "active": 0, "req": 0})
        d["total"] += r["cnt"] or 0
        for k in ("confirmed", "verified", "docsv", "active", "req"):
            d[k] += r[k] or 0
    sess_list = sorted(sess_agg.items(), key=lambda kv: kv[1]["total"], reverse=True)

    wb = Workbook()
    hfill = PatternFill("solid", fgColor="4F46E5"); hfont = Font(bold=True, color="FFFFFF")
    ws = wb.active; ws.title = "Summary"
    ws.append(["NIOS Status Tracker — Run Report"]); ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Generated", datetime.now().strftime("%d %b %Y, %I:%M %p")])
    ws.append([])
    ws.append(["Category", "Count"])
    for c in ("A4", "B4"):
        ws[c].fill = hfill; ws[c].font = hfont
    ws.append(["Confirmed Today", len(confirmed_today)])
    ws.append(["Confirmed (Total)", len(confirmed)])
    ws.append(["Admission Verified", len(verified)])
    ws.append(["Documents Verification In Progress", len(docsprog)])
    ws.append(["Document Required", len(required)])
    ws.append(["Error / Unknown", len(error)])

    # Session-wise breakdown table
    ws.append([])
    ws.append(["Session-wise Breakdown"]); ws[f"A{ws.max_row}"].font = Font(bold=True, size=12)
    shdr = ["Session", "Total", "Confirmed", "Verified", "Docs Verification", "Active", "Required"]
    ws.append(shdr)
    hr = ws.max_row
    for ci in range(1, len(shdr) + 1):
        cell = ws.cell(row=hr, column=ci); cell.fill = hfill; cell.font = hfont
    for name, d in sess_list:
        ws.append([name, d["total"], d["confirmed"], d["verified"], d["docsv"], d["active"], d["req"]])
    for col, w in zip("ABCDEFG", [34, 9, 11, 10, 17, 9, 10]):
        ws.column_dimensions[col].width = w

    def sheet(title, data):
        s = wb.create_sheet(title)
        hdr = ["Name", "Reference No", "Enrollment No", "DOB", "Session", "Mobile", "Email",
               "Status", "Remark", "Last Checked"]
        s.append(hdr)
        for i in range(1, len(hdr) + 1):
            cell = s.cell(row=1, column=i); cell.fill = hfill; cell.font = hfont
        for r in data:
            s.append([r["student_name"], r["reference_no"], r["enrollment_no"], r["dob"], r["session"],
                      r["mobile"], r["email"], r["current_status"], r["remark"], r["last_checked"]])
        widths = [22, 15, 16, 13, 15, 13, 24, 20, 30, 18]
        for i, w in enumerate(widths, 1):
            s.column_dimensions[chr(64 + i)].width = w
        s.freeze_panes = "A2"
    sheet("Confirmed Today", confirmed_today)
    sheet("Confirmed (Total)", confirmed)
    sheet("Admission Verified", verified)
    sheet("Document Required", required)
    sheet("Error & Unknown", error)
    wb.save(path)
    return {"confirmed": len(confirmed), "confirmed_today": len(confirmed_today),
            "verified": len(verified), "docs_progress": len(docsprog),
            "required": len(required), "error": len(error)}

@app.get("/report-excel/{token}")
async def report_excel(token: str):
    """Serve the run-report Excel for a signed link (admins open it straight from
    WhatsApp — no login). The signature is the security; data reflects the latest state."""
    from links import verify_report_token
    from fastapi.responses import FileResponse
    day = verify_report_token(token)
    if not day:
        raise HTTPException(status_code=404, detail="Invalid or expired report link")
    import tempfile
    path = os.path.join(tempfile.gettempdir(), f"nios_report_{day}.xlsx")
    build_report_excel(path)
    return FileResponse(path, filename=f"NIOS_Report_{day}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

def whatsapp_webhook_token():
    """Unguessable, stable token for the AiSensy delivery webhook URL."""
    import hmac as _h, hashlib as _hh
    return _h.new(SECRET_KEY.encode(), b"aisensy-delivery-webhook", _hh.sha256).hexdigest()[:20]

@app.get("/api/webhook-url")
async def get_webhook_url(user=Depends(verify_token)):
    base = os.environ.get("PUBLIC_BASE_URL", "https://status.mvsfoundation.in").rstrip("/")
    return {"url": f"{base}/webhook/whatsapp/{whatsapp_webhook_token()}"}

@app.post("/webhook/whatsapp/{token}")
async def whatsapp_delivery_webhook(token: str, request):
    """Receives WhatsApp delivery status from AiSensy and records whether a confirmed
    student's documents were actually DELIVERED (not just accepted by the gateway).
    Parsed defensively so it works across AiSensy payload shapes. Matched by mobile."""
    from fastapi import Request as _Req  # noqa
    if token != whatsapp_webhook_token():
        raise HTTPException(status_code=404, detail="not found")
    try:
        raw = await request.body()
        text = raw.decode("utf-8", "ignore").lower()
    except Exception:
        text = ""
    if not text:
        return {"ok": True, "matched": 0}
    # Map the event to a delivery state (ignore plain "sent"/"accepted" — already known)
    if "undelivered" in text or "failed" in text:
        state = "failed"
    elif "read" in text or "delivered" in text:
        state = "delivered"
    else:
        return {"ok": True, "matched": 0}
    import re as _re
    nums = _re.findall(r"\d{10,15}", text)
    phone10 = nums[0][-10:] if nums else ""
    if not phone10:
        return {"ok": True, "matched": 0}
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    matched = 0
    for r in conn.execute("SELECT row_key, mobile FROM student_status WHERE whatsapp_sent=1").fetchall():
        m = "".join(ch for ch in (r["mobile"] or "") if ch.isdigit())
        if m and m[-10:] == phone10:
            conn.execute("UPDATE student_status SET whatsapp_delivery=?, whatsapp_delivery_at=? WHERE row_key=?",
                         (state, now, r["row_key"]))
            matched += 1
    conn.commit(); conn.close()
    return {"ok": True, "matched": matched, "state": state}

@app.get("/api/download-doc")
async def download_doc(ref: str, dob: str, kind: str, user=Depends(verify_token)):
    """Login as the student and return their document (PDF or print-ready HTML)."""
    from fastapi import Response
    from nios_login import fetch_document
    # Public-cycle students (April / October / 'apr-27') only have an ID Card. Block any
    # other document so a stale/wrong link can never open the wrong file — matches the
    # student-facing links. (Admins still see every document for On Demand / Stream 2.)
    if kind != "id_card" and ref:
        try:
            conn = get_db()
            srow = conn.execute("SELECT session FROM student_status WHERE COALESCE(deleted,0)=0 "
                                "AND reference_no=? LIMIT 1", (ref,)).fetchone()
            conn.close()
            sess = ((srow["session"] if srow else "") or "").lower()
            is_s2 = ("stream 2" in sess or "stream2" in sess or "stream-2" in sess)
            is_od = ("on demand" in sess or "ondemand" in sess or "on-demand" in sess or "odes" in sess)
            if srow is not None and not is_s2 and not is_od:   # public / unknown
                raise HTTPException(status_code=404,
                    detail="This document is not available for Public (April / October) admission. Only the ID Card can be opened for this student.")
        except HTTPException:
            raise
        except Exception:
            pass
    content, ctype, filename = fetch_document(ref, dob, kind)
    if content is None:
        # Login/bounce failure -> flag this student as Failed to Run so it surfaces in
        # the sidebar (and any pending WhatsApp is blocked) even if it was confirmed
        # earlier. Matched by reference + DOB.
        err = ctype or "NIOS login failed"
        low = err.lower()
        if ("login" in low or "rejected" in low or "dob" in low) and ref:
            try:
                conn = get_db()
                conn.execute(
                    "UPDATE student_status SET login_failed=1, login_remark=?, "
                    "whatsapp_info=?, whatsapp_sent=0 "
                    "WHERE COALESCE(deleted,0)=0 AND reference_no=? AND COALESCE(dob,'')=?",
                    (err[:240], ("Not sent — " + err)[:180], ref, dob))
                conn.commit(); conn.close()
            except Exception:
                pass
        raise HTTPException(status_code=404, detail=err)
    # PDF -> attachment download; HTML -> inline (open in tab to print)
    if "pdf" in ctype:
        disp = f'attachment; filename="{filename}"'
    else:
        disp = "inline"
    return Response(content=content, media_type=ctype, headers={"Content-Disposition": disp})

@app.get("/api/syc")
async def get_syc(page: int = 1, per_page: int = 20, search: str = "", user=Depends(verify_token)):
    """List SYC students (session contains SYC). No status check is done for these."""
    conn = get_db()
    where = ("WHERE COALESCE(deleted,0)=0 AND COALESCE(login_failed,0)=0 AND COALESCE(check_failed,0)=0 "
             "AND (session LIKE '%syc%' OR current_status='SYC')")
    params = []
    if search:
        where += " AND (student_name LIKE ? OR enrollment_no LIKE ? OR mobile LIKE ? OR reference_no LIKE ?)"
        like = f"%{search}%"
        params += [like, like, like, like]
    total = conn.execute(f"SELECT COUNT(*) c FROM student_status {where}", params).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM student_status {where} ORDER BY student_name LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page]).fetchall()
    conn.close()
    return {"students": [dict(r) for r in rows], "total": total, "page": page,
            "pages": max(1, (total + per_page - 1) // per_page)}

@app.get("/api/syc-doc")
async def syc_doc(row_key: str, kind: str = "hall_ticket", user=Depends(verify_token)):
    """Fetch a SYC student's document (enrollment-login aware) for the counsellor."""
    res, err = _fetch_doc_for(row_key, kind)
    if err:
        raise HTTPException(status_code=404, detail=err)
    return _serve_doc(*res)

@app.post("/api/syc-delete")
async def syc_delete(row_key: str, user=Depends(verify_token)):
    """Delete a SINGLE SYC student only. Guarded so it can never touch a
    non-SYC (On Demand / Stream 2 / Public) student."""
    conn = get_db()
    row = conn.execute("SELECT session, current_status FROM student_status WHERE row_key=?",
                       (row_key,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Student not found")
    is_syc = ("syc" in (row["session"] or "").lower()) or (row["current_status"] == "SYC")
    if not is_syc:
        conn.close()
        raise HTTPException(status_code=400, detail="Not a SYC student — refused")
    # Soft-delete (same as other students) so it can be restored from Settings → Trash.
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE student_status SET deleted=1, deleted_at=? WHERE row_key=?", (now_s, row_key))
    conn.commit()
    conn.close()
    return {"ok": True, "soft": True}

@app.post("/api/student-delete")
async def student_delete(row_key: str, user=Depends(verify_token)):
    """SOFT-delete a student: hide it from the portal but keep it in Trash so it can
    be restored from Settings if removed by mistake. Soft-deleted students are also
    skipped by status runs."""
    conn = get_db()
    row = conn.execute("SELECT row_key, student_name FROM student_status WHERE row_key=?",
                       (row_key,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Student not found")
    name = row["student_name"] or ""
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE student_status SET deleted=1, deleted_at=? WHERE row_key=?", (now_s, row_key))
    conn.commit()
    conn.close()
    return {"ok": True, "name": name, "soft": True}

@app.post("/api/students-delete-bulk")
async def students_delete_bulk(body: dict, user=Depends(verify_token)):
    """SOFT-delete MANY students at once (move to Trash). Restorable from Settings."""
    keys = [k for k in (body.get("row_keys", []) or []) if k][:1000]
    if not keys:
        raise HTTPException(status_code=400, detail="no students selected")
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    ph = ",".join("?" * len(keys))
    cur = conn.execute(f"UPDATE student_status SET deleted=1, deleted_at=? "
                       f"WHERE row_key IN ({ph}) AND COALESCE(deleted,0)=0", [now_s] + keys)
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": True, "deleted": n}

@app.get("/api/deleted-students")
async def deleted_students(user=Depends(verify_token)):
    """List soft-deleted students (the Trash) for the Settings restore panel."""
    conn = get_db()
    rows = conn.execute(
        "SELECT row_key, student_name, reference_no, enrollment_no, session, "
        "current_status, deleted_at, COALESCE(source,'mvs_tracker') AS source "
        "FROM student_status WHERE COALESCE(deleted,0)=1 ORDER BY deleted_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/student-restore")
async def student_restore(row_key: str, user=Depends(verify_token)):
    """Restore a soft-deleted student back to the portal."""
    conn = get_db()
    conn.execute("UPDATE student_status SET deleted=0, deleted_at=NULL WHERE row_key=?", (row_key,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/student-purge")
async def student_purge(row_key: str, user=Depends(verify_token)):
    """Permanently delete a student from Trash (cannot be undone)."""
    conn = get_db()
    conn.execute("DELETE FROM student_status WHERE row_key=?", (row_key,))
    conn.execute("DELETE FROM short_links WHERE row_key=?", (row_key,))
    try:
        conn.execute("DELETE FROM status_history WHERE row_key=?", (row_key,))
    except Exception:
        pass
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/students-restore-bulk")
async def students_restore_bulk(body: dict, user=Depends(verify_token)):
    """Restore MANY soft-deleted students from Trash at once."""
    keys = [k for k in (body.get("row_keys", []) or []) if k][:2000]
    if not keys:
        raise HTTPException(status_code=400, detail="no students selected")
    conn = get_db()
    ph = ",".join("?" * len(keys))
    cur = conn.execute(f"UPDATE student_status SET deleted=0, deleted_at=NULL WHERE row_key IN ({ph})", keys)
    conn.commit()
    n = cur.rowcount
    conn.close()
    return {"ok": True, "restored": n}

@app.post("/api/students-purge-bulk")
async def students_purge_bulk(body: dict, user=Depends(verify_token)):
    """Permanently delete MANY students from Trash at once (cannot be undone)."""
    keys = [k for k in (body.get("row_keys", []) or []) if k][:2000]
    if not keys:
        raise HTTPException(status_code=400, detail="no students selected")
    conn = get_db()
    ph = ",".join("?" * len(keys))
    cur = conn.execute(f"DELETE FROM student_status WHERE row_key IN ({ph})", keys)
    n = cur.rowcount
    try:
        conn.execute(f"DELETE FROM short_links WHERE row_key IN ({ph})", keys)
    except Exception:
        pass
    try:
        conn.execute(f"DELETE FROM status_history WHERE row_key IN ({ph})", keys)
    except Exception:
        pass
    conn.commit()
    conn.close()
    return {"ok": True, "purged": n}

@app.get("/api/student-get")
async def student_get(row_key: str, user=Depends(verify_token)):
    """Fetch ONE student's editable details (for the Edit modal)."""
    conn = get_db()
    r = conn.execute("SELECT row_key, student_name, mobile, alt_mobile, email, dob, reference_no, "
                     "enrollment_no, class_level, session, current_status, "
                     "COALESCE(login_failed,0) AS login_failed, "
                     "COALESCE(check_failed,0) AS check_failed, login_remark "
                     "FROM student_status WHERE row_key=?", (row_key,)).fetchone()
    conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="Student not found")
    return dict(r)

@app.post("/api/student-edit")
async def student_edit(body: dict, user=Depends(verify_token)):
    """Edit a student's uploaded details (DOB, Reference/Enrollment No, email, mobile,
    name, session) WITHOUT re-uploading the sheet. Resets the login-failed flag and the
    WhatsApp-sent flag so a re-run can verify the fix and send fresh."""
    row_key = (body.get("row_key") or "").strip()
    if not row_key:
        raise HTTPException(status_code=400, detail="row_key required")
    conn = get_db()
    row = conn.execute("SELECT row_key FROM student_status WHERE row_key=?", (row_key,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Student not found")
    allowed = ["student_name", "mobile", "alt_mobile", "email", "dob", "reference_no",
               "enrollment_no", "class_level", "session"]
    sets, params = [], []
    for f in allowed:
        if f in body:
            sets.append(f"{f}=?"); params.append((body.get(f) or "").strip())
    if not sets:
        conn.close()
        raise HTTPException(status_code=400, detail="No fields to update")
    # Editing the data invalidates any previous login result / sent flag.
    sets += ["login_failed=0", "login_remark=''", "check_failed=0", "whatsapp_sent=0"]
    params.append(row_key)
    conn.execute(f"UPDATE student_status SET {', '.join(sets)} WHERE row_key=?", params)
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/student-recheck")
async def student_recheck(row_key: str, background_tasks: BackgroundTasks, user=Depends(verify_token)):
    """Re-run ONE student (after an edit). Runs in the background; the UI polls the
    student row to see the new status / login result."""
    from job_runner import recheck_one
    conn = get_db()
    row = conn.execute("SELECT row_key FROM student_status WHERE row_key=?", (row_key,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Student not found")
    background_tasks.add_task(recheck_one, row_key)
    return {"ok": True, "message": "Re-checking this student…"}

@app.post("/api/run-log-delete")
async def run_log_delete(id: int, user=Depends(verify_token)):
    """Delete ONE run-log entry (e.g. a 'Nothing to check' or failed run). A run that is
    still 'running' cannot be deleted — cancel it first."""
    conn = get_db()
    row = conn.execute("SELECT status FROM run_logs WHERE id=?", (id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Run not found")
    if row["status"] == "running":
        conn.close()
        raise HTTPException(status_code=400, detail="Run is still running — cancel it first")
    conn.execute("DELETE FROM run_logs WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/run-logs-clear")
async def run_logs_clear(user=Depends(verify_token)):
    """Delete all run-log entries that are not currently running."""
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM run_logs WHERE status!='running'").fetchone()[0]
    conn.execute("DELETE FROM run_logs WHERE status!='running'")
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": n}

# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp (AiSensy) — settings, test, manual resend
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/wa-diagnose")
def wa_diagnose(user=Depends(verify_token)):
    """One-shot health check for the whole WhatsApp pipeline — never raises, returns plain JSON
    so the exact broken piece (module import / API key / campaign env / DB lock) is obvious."""
    import os
    d = {}
    try:
        import whatsapp
        d["whatsapp_module"] = "imported OK"
        try:
            d["is_configured"] = whatsapp.is_configured()
        except Exception as e:
            d["is_configured"] = f"ERROR: {e}"
        for g in ("ondemand", "stream2", "public", "syc"):
            try:
                d[f"campaign_{g}"] = whatsapp.campaign_for(g) or "(empty)"
            except Exception as e:
                d[f"campaign_{g}"] = f"ERROR: {e}"
    except Exception as e:
        d["whatsapp_module"] = f"IMPORT FAILED: {e}"
    d["env_AISENSY_API_KEY_set"] = bool(os.environ.get("AISENSY_API_KEY", "").strip())
    d["env_AISENSY_API_KEY_PUBLIC_set"] = bool(os.environ.get("AISENSY_API_KEY_PUBLIC", "").strip())
    d["env_CAMPAIGN_REQUIRED"] = os.environ.get("AISENSY_CAMPAIGN_REQUIRED", "").strip() or "(empty)"
    d["env_CAMPAIGN_REQUIRED_PUBLIC"] = os.environ.get("AISENSY_CAMPAIGN_REQUIRED_PUBLIC", "").strip() or "(empty)"
    try:
        d["wa_enabled_setting"] = get_setting("wa_enabled", "0")
        d["wa_required_enabled_setting"] = get_setting("wa_required_enabled", "0")
        d["db_read"] = "OK"
    except Exception as e:
        d["db_read"] = f"ERROR: {e}"
    try:
        conn = get_db()
        d["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
    except Exception as e:
        d["journal_mode"] = f"ERROR: {e}"
    try:
        conn = get_db()
        rows = conn.execute("SELECT student_name, COALESCE(whatsapp_sent,0) AS s, whatsapp_info "
                            "FROM student_status WHERE is_confirmed=1 AND COALESCE(whatsapp_info,'')!='' "
                            "ORDER BY whatsapp_sent_at DESC LIMIT 5").fetchall()
        conn.close()
        d["recent_send_results"] = [{"name": r["student_name"], "sent_flag": r["s"],
                                     "info": (r["whatsapp_info"] or "")[:140]} for r in rows]
    except Exception as e:
        d["recent_send_results"] = f"ERROR: {e}"
    return d


@app.get("/api/wa-settings")
def wa_settings_get(user=Depends(verify_token)):
    """Always returns 200 with valid JSON — campaigns come straight from env vars, so even if
    the DB is busy or the whatsapp module hiccups, the panel still loads and shows what's set."""
    import os
    def env(k):
        try:
            return os.environ.get(k, "").strip()
        except Exception:
            return ""
    out = {
        "enabled": False,
        "required_enabled": False,
        "configured": bool(env("AISENSY_API_KEY") or env("AISENSY_API_KEY_PUBLIC")),
        "campaigns": {"ondemand": "", "stream2": "", "public": "", "syc": ""},
        "campaigns_env": {
            "ondemand": bool(env("AISENSY_CAMPAIGN_ONDEMAND")),
            "stream2": bool(env("AISENSY_CAMPAIGN_STREAM2")),
            "public": bool(env("AISENSY_CAMPAIGN_PUBLIC")),
            "syc": bool(env("AISENSY_CAMPAIGN_SYC")),
        },
        "required_campaigns": {
            "main": env("AISENSY_CAMPAIGN_REQUIRED"),
            "public": env("AISENSY_CAMPAIGN_REQUIRED_PUBLIC"),
        },
    }
    try:
        out["enabled"] = get_setting("wa_enabled", "0") == "1"
        out["required_enabled"] = get_setting("wa_required_enabled", "0") == "1"
    except Exception:
        pass
    try:
        import whatsapp
        out["configured"] = whatsapp.is_configured()
        out["campaigns"] = {
            "ondemand": whatsapp.campaign_for("ondemand"),
            "stream2": whatsapp.campaign_for("stream2"),
            "public": whatsapp.campaign_for("public"),
            "syc": whatsapp.campaign_for("syc"),
        }
    except Exception:
        pass
    return out

@app.post("/api/wa-settings")
def wa_settings_set(body: dict, user=Depends(verify_token)):
    if "enabled" in body:
        set_setting("wa_enabled", "1" if body.get("enabled") else "0")
    if "required_enabled" in body:
        set_setting("wa_required_enabled", "1" if body.get("required_enabled") else "0")
    return {"message": "saved",
            "enabled": get_setting("wa_enabled", "0") == "1",
            "required_enabled": get_setting("wa_required_enabled", "0") == "1"}

@app.post("/api/wa-campaigns")
def wa_campaigns_set(body: dict, user=Depends(verify_token)):
    """Save the confirmed-send campaign names from the Settings page. Stored in the DB and used
    in preference to the Railway env vars, so campaigns can be fixed in-app. Empty value clears
    the override (falls back to the env var)."""
    for g in ("ondemand", "stream2", "public", "syc"):
        if g in body:
            set_setting("wa_campaign_" + g, str(body.get(g) or "").strip())
    import whatsapp
    return {"ok": True, "campaigns": {
        "ondemand": whatsapp.campaign_for("ondemand"),
        "stream2": whatsapp.campaign_for("stream2"),
        "public": whatsapp.campaign_for("public"),
        "syc": whatsapp.campaign_for("syc"),
    }}

@app.post("/api/wa-test")
def wa_test(body: dict, user=Depends(verify_token)):
    """Send a test message of a chosen template group to any number."""
    import whatsapp
    number = body.get("number", "")
    group = body.get("group", "ondemand")
    name = body.get("name", "") or "Test Student"
    ok, info = whatsapp.send_test(number, name, group)
    return {"ok": ok, "info": info}


def _humanize_remark(remark):
    """Turn NIOS's raw RC comment into a simple, warm document request — written the way a
    human counsellor would, so the student actually understands what to send. This is only a
    DRAFT: the counsellor reviews and edits it before anything is sent. Returns a Hinglish
    line for the message ({{2}} param). Empty string -> unrecognised, counsellor writes it."""
    r = (remark or "").lower()
    asks = []
    if "address" in r or "correspondence" in r or "rent agreement" in r:
        if "delhi" in r or "ncr" in r:
            asks.append("Apna Delhi/NCR ka address proof (Aadhaar card, bijli/paani/phone ka bill, "
                        "ration card, voter ID, passport ya rent agreement me se koi ek — jisme aapka "
                        "ya aapke parents ka naam aur poora address ho)")
        else:
            asks.append("Apna sahi address proof (Aadhaar card, bijli/paani/phone ka bill, ration card, "
                        "voter ID, passport ya rent agreement me se koi ek — jisme aapka ya aapke "
                        "parents ka naam aur poora address ho)")
    if "mark sheet" in r or "marksheet" in r or "mark-sheet" in r:
        asks.append("Apni Class 10th ki original marksheet")
    if ("matriculation" in r or "passing certificate" in r or "secondary examination" in r
            or "date of birth" in r or ("10th" in r and "certificate" in r)):
        asks.append("Apna Class 10th passing certificate (jisme aapki date of birth likhi ho)")
    if ("father" in r) and ("not match" in r or "correct" in r or "e-service" in r or "e service" in r):
        asks.append("Apne father ke naam ka sahi record — abhi jo naam hai wo academic record se match "
                    "nahi kar raha; aap apne dashboard ki E-Services se ise theek kar sakte hain")
    if ("mother" in r) and ("not match" in r or "correct" in r or "e-service" in r or "e service" in r):
        asks.append("Apne mother ke naam ka sahi record (dashboard ki E-Services se correction)")
    if "photo" in r or "photograph" in r:
        asks.append("Apni clear passport-size photo")
    if "signature" in r:
        asks.append("Apne signature ki clear photo")
    if not asks:
        return ""
    if len(asks) == 1:
        return asks[0]
    return "; ".join(asks)


# Fixed, approved wrapper around the editable {{2}} document line (kept in sync with the
# AiSensy template the user gets approved). Shown in the portal preview.
DOCREQ_PREFIX = "Namaste {name} \U0001F64F\n\nAapke NIOS admission ko poora karne ke liye humein aapse ye chahiye:\n\n"
DOCREQ_SUFFIX = ("\n\nJab aapko suvidha ho, kripya ye MVS Foundation ko bhej dijiye — koi jaldi ya "
                 "chinta ki baat nahi \U0001F60A Hum aapki poori madad ke liye yahin hain.\n\n"
                 "Kisi bhi sawaal ke liye is number par message kar dijiye.\n\nMVS Foundation Team")


def _docreq_rows(conn, keys=None, pending_only=False):
    q = ("SELECT row_key, student_name, mobile, alt_mobile, session, remark, required_msg, "
         "required_img, required_notified, required_notified_at FROM student_status "
         "WHERE COALESCE(deleted,0)=0 AND current_status='Document Required' "
         "AND COALESCE(is_confirmed,0)=0 AND COALESCE(login_failed,0)=0 AND COALESCE(check_failed,0)=0")
    params = []
    if pending_only:
        q += " AND COALESCE(required_notified,0)=0"
    if keys:
        q += " AND row_key IN (%s)" % ",".join("?" * len(keys))
        params = keys
    q += " ORDER BY COALESCE(required_notified,0) ASC, student_name"
    return conn.execute(q, params).fetchall()


@app.get("/api/doc-requests")
async def doc_requests(user=Depends(verify_token)):
    """List Document-Required students with the (editable) document-request message that
    will be sent. The counsellor reviews/edits these on the portal before sending."""
    conn = get_db()
    rows = _docreq_rows(conn)
    conn.close()
    out = []
    for r in rows:
        saved = (r["required_msg"] or "").strip()
        draft = saved or _humanize_remark(r["remark"] or "")
        out.append({
            "row_key": r["row_key"], "student_name": r["student_name"] or "\u2014",
            "mobile": r["mobile"] or "\u2014", "session": r["session"] or "\u2014",
            "remark": r["remark"] or "", "message": draft,
            "image": (r["required_img"] if ("required_img" in r.keys()) else "") or "",
            "edited": bool(saved), "auto_blank": (not saved and not draft),
            "sent": bool(r["required_notified"] == 1), "sent_at": r["required_notified_at"] or "",
        })
    return {"requests": out, "count": len(out),
            "prefix": DOCREQ_PREFIX, "suffix": DOCREQ_SUFFIX}


@app.post("/api/doc-request-save")
async def doc_request_save(body: dict, user=Depends(verify_token)):
    """Save the counsellor's edited document-request message for one student."""
    rk = body.get("row_key", "")
    msg = (body.get("message", "") or "").strip()
    if not rk:
        raise HTTPException(status_code=400, detail="row_key required")
    conn = get_db()
    conn.execute("UPDATE student_status SET required_msg=? WHERE row_key=?", (msg[:1000], rk))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/doc-request-send")
def doc_request_send(body: dict, request: Request, user=Depends(verify_token)):
    """Send the reviewed document-request WhatsApp message to selected (or all pending)
    Document-Required students. Routes per session (public -> public API; others -> main API).
    Uses ONE universal image-header template: sends the uploaded screenshot when present,
    otherwise the default MVS banner. Marks each as sent so it isn't messaged twice.
    NOTE: this is a *sync* endpoint on purpose — the AiSensy HTTP calls are blocking, so running
    them in FastAPI's threadpool keeps the event loop free (otherwise the whole app freezes and
    even the banner image can't be served back to AiSensy while a send is in progress)."""
    if get_setting("wa_required_enabled", "0") != "1":
        raise HTTPException(status_code=400, detail="Document-request reminders are turned OFF in Settings — turn them on first.")
    import whatsapp
    if not whatsapp.is_configured():
        raise HTTPException(status_code=400, detail="AISENSY_API_KEY is not set.")
    base = str(request.base_url).rstrip("/")
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://"):]
    default_img = f"{base}/media/docreq-default.png"
    send_all = bool(body.get("all"))
    keys = [k for k in (body.get("row_keys", []) or []) if k][:500]
    conn = get_db()
    rows = _docreq_rows(conn, keys=None if send_all else keys, pending_only=send_all)
    if not send_all and not keys:
        conn.close()
        raise HTTPException(status_code=400, detail="no students selected")
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sent, failed, results = 0, 0, []
    for r in rows:
        msg = (r["required_msg"] or "").strip() or _humanize_remark(r["remark"] or "")
        media = (r["required_img"] if ("required_img" in r.keys()) else "") or None
        ok, info = whatsapp.send_required_reminder({
            "row_key": r["row_key"], "student_name": r["student_name"] or "",
            "mobile": r["mobile"] or "", "session": r["session"] or "",
            "alt_mobile": (r["alt_mobile"] if ("alt_mobile" in r.keys()) else "") or "",
        }, msg, media_url=media, default_img=default_img)
        if ok:
            conn.execute("UPDATE student_status SET required_notified=1, required_notified_at=?, "
                         "required_msg=? WHERE row_key=?", (now_s, msg[:1000], r["row_key"]))
            sent += 1
        else:
            failed += 1
        results.append({"student_name": r["student_name"] or "\u2014", "ok": ok, "info": str(info)[:120]})
    conn.commit()
    conn.close()
    return {"ok": True, "sent": sent, "failed": failed, "results": results}


import os as _os_docreq
DOCREQ_MEDIA_DIR = "/tmp/docreq_media"

@app.get("/media/docreq/{fname}")
async def docreq_media(fname: str):
    """Serve an uploaded screenshot publicly (no auth) so the WhatsApp gateway can fetch it."""
    safe = _os_docreq.path.basename(fname)
    path = _os_docreq.path.join(DOCREQ_MEDIA_DIR, safe)
    if not _os_docreq.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
    mt = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
          "webp": "image/webp"}.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=mt, headers={"Cache-Control": "public, max-age=3600"})

def _make_default_banner(path):
    """A friendly MVS-branded banner used as the image header whenever no screenshot is
    attached — so the universal image-template always has an image to show."""
    from PIL import Image, ImageDraw, ImageFont
    W, H = 800, 418
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    c1, c2 = (79, 70, 229), (147, 51, 234)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(c1[0] + (c2[0]-c1[0])*t),
                                       int(c1[1] + (c2[1]-c1[1])*t),
                                       int(c1[2] + (c2[2]-c1[2])*t)))
    def font(sz, bold=True):
        for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
                  else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
        return ImageFont.load_default()
    def center(text, y, f, fill=(255, 255, 255)):
        bb = d.textbbox((0, 0), text, font=f)
        d.text(((W - (bb[2]-bb[0])) / 2, y), text, font=f, fill=fill)
    d.rounded_rectangle([W/2-46, 54, W/2+46, 146], radius=22, fill=(255, 255, 255))
    center("MVS", 78, font(46), fill=(79, 70, 229))
    center("MVS Foundation", 168, font(30))
    center("Admission Document Update", 230, font(40))
    center("Aapke admission ke liye ek zaroori update", 300, font(22, bold=False),
           fill=(233, 213, 255))
    img.save(path, "PNG")

@app.get("/media/docreq-default.png")
async def docreq_default_banner():
    """Default branded banner image (generated once, cached) for document requests with no
    screenshot attached. Public (no auth) so the WhatsApp gateway can fetch it."""
    path = _os_docreq.path.join(DOCREQ_MEDIA_DIR, "_default_banner.png")
    if not _os_docreq.path.isfile(path):
        _os_docreq.makedirs(DOCREQ_MEDIA_DIR, exist_ok=True)
        try:
            _make_default_banner(path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"banner generation failed: {e}")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

@app.post("/api/doc-request-image")
async def doc_request_image(request: Request, row_key: str = Form(...),
                            file: UploadFile = File(...), user=Depends(verify_token)):
    """Upload a demo screenshot for a Document-Required student. Stored on disk and served at a
    public URL, which is attached as the WhatsApp message's image header when you send."""
    import uuid as _uuid
    fn = file.filename or ""
    ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else "jpg"
    if ext not in ("jpg", "jpeg", "png", "webp"):
        raise HTTPException(status_code=400, detail="Only JPG / PNG / WEBP images are allowed.")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 5 MB).")
    _os_docreq.makedirs(DOCREQ_MEDIA_DIR, exist_ok=True)
    fname = f"{_uuid.uuid4().hex}.{ext}"
    with open(_os_docreq.path.join(DOCREQ_MEDIA_DIR, fname), "wb") as f:
        f.write(data)
    base = str(request.base_url).rstrip("/")
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://"):]
    url = f"{base}/media/docreq/{fname}"
    conn = get_db()
    conn.execute("UPDATE student_status SET required_img=? WHERE row_key=?", (url, row_key))
    conn.commit()
    conn.close()
    return {"ok": True, "url": url}

@app.post("/api/doc-request-image-remove")
async def doc_request_image_remove(body: dict, user=Depends(verify_token)):
    rk = body.get("row_key", "")
    if not rk:
        raise HTTPException(status_code=400, detail="row_key required")
    conn = get_db()
    conn.execute("UPDATE student_status SET required_img=NULL WHERE row_key=?", (rk,))
    conn.commit()
    conn.close()
    return {"ok": True}

def _wa_send_one(row_key, verify=False):
    """(Re)send the documents for ONE confirmed student. Opens its own DB connection so it
    is safe to run as a background task. Returns (ok, info, login_failed). Never raises.

    verify=False (default, FAST): the student is already CONFIRMED — their Reference/DOB were
    already proven valid when the status was confirmed, and the WhatsApp message only carries
    signed links (the document is fetched live when the student taps it). So we skip the slow
    NIOS re-login (CapSolver reCAPTCHA, ~15-40s each) and send straight away (~1-2s each).
    verify=True: re-verify the NIOS login first (slow) — used only if explicitly requested."""
    import whatsapp
    try:
        conn = get_db()
        row = conn.execute("SELECT row_key, student_name, mobile, alt_mobile, session, reference_no, dob, enrollment_no "
                           "FROM student_status WHERE row_key=?", (row_key,)).fetchone()
        if not row:
            conn.close()
            return False, "student not found", False
        if verify:
            from nios_login import verify_login
            ok_login, lmsg = verify_login(row["reference_no"], row["dob"], row["enrollment_no"] or "")
            if not ok_login:
                conn.execute("UPDATE student_status SET login_failed=1, login_remark=?, whatsapp_info=?, "
                             "whatsapp_sent=0, whatsapp_attempts=COALESCE(whatsapp_attempts,0)+1 WHERE row_key=?",
                             (lmsg[:240], ("Not sent — " + lmsg)[:180], row_key))
                conn.commit(); conn.close()
                return False, lmsg, True
            conn.execute("UPDATE student_status SET login_failed=0, login_remark='' WHERE row_key=?", (row_key,))
        ok, info = whatsapp.send_for_student(dict(row))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if ok:
            conn.execute("UPDATE student_status SET whatsapp_sent=1, whatsapp_info=?, "
                         "whatsapp_sent_at=?, whatsapp_delivery='' WHERE row_key=?",
                         (str(info)[:180], now, row_key))
        else:
            conn.execute("UPDATE student_status SET whatsapp_info=?, "
                         "whatsapp_attempts=COALESCE(whatsapp_attempts,0)+1 WHERE row_key=?",
                         (str(info)[:180], row_key))
        conn.commit(); conn.close()
        return ok, info, False
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return False, f"error: {e}", False

WA_PROGRESS = {"running": False, "total": 0, "done": 0, "sent": 0, "failed": 0,
               "started_at": "", "finished_at": "", "label": ""}

def auto_send_pending_whatsapp(batch=None, label="Auto sweep"):
    """Background sweep: for every CONFIRMED student whose documents have not been sent
    on WhatsApp yet, send them automatically — so the counsellor never has to click
    'Resend' for each one. Tracks live progress (WA_PROGRESS) so the UI can show a
    percentage bar. Gives up on a student after several failed attempts."""
    if get_setting("wa_enabled", "0") != "1":
        return {"sent": 0, "checked": 0}
    if WA_PROGRESS["running"]:
        return {"skipped": "already running"}
    conn = get_db()
    q = ("SELECT row_key FROM student_status WHERE is_confirmed=1 "
         "AND COALESCE(whatsapp_sent,0)=0 AND COALESCE(login_failed,0)=0 "
         "AND COALESCE(deleted,0)=0 AND COALESCE(whatsapp_attempts,0) < 8 "
         "AND mobile IS NOT NULL AND TRIM(mobile) != '' "
         "ORDER BY COALESCE(whatsapp_attempts,0) ASC, last_changed DESC")
    if batch:
        q += f" LIMIT {int(batch)}"
    rows = conn.execute(q).fetchall()
    conn.close()
    keys = [r["row_key"] for r in rows]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    WA_PROGRESS.update({"running": True, "total": len(keys), "done": 0, "sent": 0,
                        "failed": 0, "started_at": now, "finished_at": "", "label": label})
    sent = 0
    try:
        for rk in keys:
            ok, info, _lf = _wa_send_one(rk)
            WA_PROGRESS["done"] += 1
            if ok:
                WA_PROGRESS["sent"] += 1; sent += 1
            else:
                WA_PROGRESS["failed"] += 1
    finally:
        WA_PROGRESS["running"] = False
        WA_PROGRESS["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if keys:
        logger.info(f"WhatsApp {label}: sent {sent}/{len(keys)} pending confirmed student(s)")
    return {"sent": sent, "checked": len(keys)}

@app.post("/api/wa-resend")
async def wa_resend(body: dict, user=Depends(verify_token)):
    """Manual (re)send for ONE student. Runs the send in a worker thread (so the event loop
    never blocks) and returns the ACTUAL result, so the counsellor immediately sees whether
    it went through or exactly why it failed (e.g. 'no campaign set for public')."""
    row_key = body.get("row_key", "")
    if not row_key:
        raise HTTPException(status_code=400, detail="row_key required")
    from fastapi.concurrency import run_in_threadpool
    ok, info, lf = await run_in_threadpool(_wa_send_one, row_key)
    return {"ok": ok, "info": info, "login_failed": lf}

@app.post("/api/wa-resend-bulk")
async def wa_resend_bulk(body: dict, background_tasks: BackgroundTasks, user=Depends(verify_token)):
    """Resend WhatsApp to MANY selected students at once (all in the background)."""
    keys = body.get("row_keys", []) or []
    keys = [k for k in keys if k][:200]
    if not keys:
        raise HTTPException(status_code=400, detail="no students selected")
    def _run(klist):
        for rk in klist:
            _wa_send_one(rk)
    background_tasks.add_task(_run, keys)
    return {"ok": True, "queued": len(keys),
            "message": f"Resending to {len(keys)} student(s) in the background"}

@app.post("/api/wa-autosend-now")
async def wa_autosend_now(background_tasks: BackgroundTasks, user=Depends(verify_token)):
    """'Send all pending' — sends to ALL confirmed students that are still 'Not sent yet'
    (in the background, with live progress shown on the Confirmed page)."""
    if WA_PROGRESS["running"]:
        return {"ok": True, "already_running": True, "message": "A send is already running"}
    background_tasks.add_task(auto_send_pending_whatsapp, None, "Send all pending")
    return {"ok": True, "message": "Sending to all pending students…"}

@app.get("/api/wa-progress")
async def wa_progress(user=Depends(verify_token)):
    """Live progress of the WhatsApp 'send all pending' / auto-sweep run."""
    p = dict(WA_PROGRESS)
    p["remaining"] = max(0, p.get("total", 0) - p.get("done", 0))
    p["pct"] = int(round(100 * p.get("done", 0) / p["total"])) if p.get("total") else (100 if p.get("finished_at") else 0)
    return p

@app.get("/api/debug-doc")
async def debug_doc(ref: str, dob: str, kind: str = "app_form", user=Depends(verify_token)):
    """Inspect how a document page embeds & sizes its images (photo/signature/QR),
    so we can match NIOS exactly. Returns image pixel sizes, EXIF orientation & CSS."""
    try:
        from nios_login import inspect_doc_page
        return inspect_doc_page(ref, dob, kind)
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/debug-idcard")
async def debug_idcard(ref: str, dob: str, user=Depends(verify_token)):
    """Show the ID card's visible text + best-effort Regional Centre address
    (used to finalise the Stream 2 address parser)."""
    try:
        from nios_login import debug_idcard_text
        return debug_idcard_text(ref, dob)
    except Exception as e:
        return {"error": str(e)}

# ─────────────────────────────────────────────────────────────────────────────
# Danger zone — wipe all student data for a fresh upload (keeps settings)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/reset-data")
async def reset_data(body: dict, user=Depends(verify_token)):
    conn = get_db()
    conn.execute("DELETE FROM student_status")
    conn.execute("DELETE FROM status_history")
    conn.execute("DELETE FROM run_logs")
    try:
        conn.execute("DELETE FROM short_links")
    except Exception:
        pass
    conn.commit()
    conn.close()
    removed = False
    try:
        if os.path.exists(EXCEL_PATH):
            os.remove(EXCEL_PATH)
            removed = True
    except Exception:
        pass
    logger.info("All student data cleared via reset endpoint")
    return {"message": "All data has been cleared.", "excel_removed": removed}

# ─────────────────────────────────────────────────────────────────────────────
# Public document links (student opens WITHOUT portal login; token-signed)
# ─────────────────────────────────────────────────────────────────────────────
def _allowed_kinds(session):
    s = (session or "").lower()
    if "stream 2" in s or "stream2" in s or "stream-2" in s:
        return [("id_card", "ID Card"), ("app_form", "Application Form")]
    if "on demand" in s or "ondemand" in s or "on-demand" in s:
        return [("id_card", "ID Card"), ("app_form", "Application Form"), ("hall_ticket", "Hall Ticket")]
    if "april" in s or "october" in s or "public" in s:
        return [("id_card", "ID Card")]
    return [("id_card", "ID Card"), ("app_form", "Application Form"), ("hall_ticket", "Hall Ticket")]

DOC_PAGE_TPL = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Your NIOS Documents — MVS Foundation</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
body{background:linear-gradient(135deg,#4F46E5,#7C3AED);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:18px}
.card{background:#fff;border-radius:20px;max-width:440px;width:100%;padding:30px 26px;box-shadow:0 20px 60px rgba(0,0,0,.3)}
.logo{width:58px;height:58px;border-radius:14px;background:linear-gradient(135deg,#4F46E5,#7C3AED);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:20px;margin:0 auto 14px}
h1{font-size:19px;text-align:center;color:#0F172A}
.sub{text-align:center;color:#64748B;font-size:13px;margin:6px 0 22px}
.name{text-align:center;font-weight:700;color:#4F46E5;font-size:16px;margin-bottom:2px}
.btns{display:flex;flex-direction:column;gap:12px;margin-top:8px}
.docbtn{display:flex;align-items:center;gap:12px;padding:15px 18px;border:2px solid #E2E8F0;border-radius:14px;
  color:#0F172A;font-weight:600;font-size:15px;cursor:pointer;background:#F8FAFC;transition:.15s;text-decoration:none}
.docbtn:hover{border-color:#4F46E5;background:#EEF2FF}
.docbtn .ico{width:34px;height:34px;border-radius:9px;background:#4F46E5;color:#fff;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:17px}
.note{margin-top:18px;background:#FEF9C3;border:1px solid #FDE68A;border-radius:12px;padding:11px 14px;font-size:12.5px;color:#854D0E}
.foot{text-align:center;color:#94A3B8;font-size:11.5px;margin-top:18px}
.busy{opacity:.55;pointer-events:none}
</style></head><body>
<div class="card">
  <img src="/logo.png" alt="MVS Foundation" style="width:60px;height:60px;border-radius:50%;object-fit:cover;display:block;margin:0 auto 14px">
  <h1>Your NIOS Documents</h1>
  <div class="sub">Admission Confirmed</div>
  <div class="name">__NAME__</div>
  <div class="sub" style="margin-top:0">Ref: __REF__</div>
  <div class="btns">__BUTTONS__</div>
  <div class="note">&#128161; It may take a few seconds to open each document (securely fetched from NIOS). Once it opens, tap <b>"Save as PDF / Print"</b> at the top to save it.</div>
  <div class="foot">MVS Foundation &middot; NIOS Open Schooling</div>
</div>
<script>
function openDoc(el,url){ if(el.dataset.b==='1')return; el.dataset.b='1';
  var t=el.querySelector('.lbl'); var o=t.textContent; t.textContent='Opening...';
  el.classList.add('busy'); window.open(url,'_blank');
  setTimeout(function(){t.textContent=o;el.classList.remove('busy');el.dataset.b='';},16000); }
</script>
</body></html>"""

@app.get("/d/{token}", response_class=HTMLResponse)
async def public_doc_page(token: str):
    from links import verify_doc_token
    row_key = verify_doc_token(token)
    if not row_key:
        return HTMLResponse("<h3 style='font-family:sans-serif;text-align:center;margin-top:60px'>Invalid or broken link</h3>", status_code=404)
    conn = get_db()
    row = conn.execute("SELECT student_name, session, reference_no FROM student_status WHERE row_key=?",
                       (row_key,)).fetchone()
    conn.close()
    if not row:
        return HTMLResponse("<h3 style='font-family:sans-serif;text-align:center;margin-top:60px'>Student not found</h3>", status_code=404)
    btns = ""
    for kind, label in _allowed_kinds(row["session"]):
        url = f"/d/{token}/{kind}"
        btns += (f'<a class="docbtn" onclick="openDoc(this,\'{url}\')">'
                 f'<span class="ico">&#128196;</span><span class="lbl">{label}</span></a>')
    html = (DOC_PAGE_TPL
            .replace("__NAME__", (row["student_name"] or "Student"))
            .replace("__REF__", (row["reference_no"] or "—"))
            .replace("__BUTTONS__", btns))
    return HTMLResponse(html)

@app.get("/d/{token}/{kind}")
async def public_doc_file(token: str, kind: str):
    from fastapi import Response
    from links import verify_doc_token
    from nios_login import fetch_document
    row_key = verify_doc_token(token)
    if not row_key:
        raise HTTPException(status_code=404, detail="invalid link")
    conn = get_db()
    row = conn.execute("SELECT reference_no, dob, session FROM student_status WHERE row_key=?",
                       (row_key,)).fetchone()
    conn.close()
    if not row or not row["reference_no"]:
        raise HTTPException(status_code=404, detail="student not found")
    import whatsapp
    if whatsapp.group_of(row["session"]) == "public" and kind != "id_card":
        raise HTTPException(status_code=404, detail="This document is not available for Public (April / October) admission. Only the ID Card can be opened.")
    content, ctype, filename = fetch_document(row["reference_no"], row["dob"], kind)
    if content is None:
        raise HTTPException(status_code=404, detail=ctype)
    disp = f'attachment; filename="{filename}"' if "pdf" in ctype else "inline"
    return Response(content=content, media_type=ctype, headers={"Content-Disposition": disp})

DOC_LABELS = {"id_card": "ID Card", "app_form": "Application / Registration Form",
              "hall_ticket": "Hall Ticket"}

LOADING_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Preparing your document — MVS Foundation</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
body{background:linear-gradient(135deg,#4F46E5,#7C3AED);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.box{background:#fff;border-radius:20px;max-width:430px;width:100%;padding:40px 30px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.3)}
.spinner{width:52px;height:52px;border:5px solid #EEF2FF;border-top-color:#4F46E5;border-radius:50%;margin:0 auto 22px;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
h1{font-size:19px;color:#0F172A;margin-bottom:8px}
.doc{color:#4F46E5;font-weight:700}
p{color:#64748B;font-size:14px;line-height:1.6;margin-top:6px}
.warn{margin-top:22px;background:#FEF3C7;border:1px solid #FDE68A;border-radius:12px;padding:12px 14px;color:#92400E;font-size:13px;font-weight:600}
.err{display:none;margin-top:18px;color:#DC2626;font-size:14px}
.retry{display:none;margin-top:14px;padding:11px 26px;border:none;border-radius:10px;background:#4F46E5;color:#fff;font-weight:600;font-size:15px;cursor:pointer}
</style></head><body>
<div class="box">
  <img src="/logo.png" alt="MVS Foundation" style="width:60px;height:60px;border-radius:50%;object-fit:cover;display:block;margin:0 auto 14px">
  <div class="spinner" id="sp"></div>
  <h1>Preparing your <span class="doc">__LABEL__</span></h1>
  <p>Please wait a few seconds while we securely fetch your document from NIOS.</p>
  <div class="warn">&#9203; Please do not refresh this page or press the back button.</div>
  <div class="err" id="err">Sorry, the document could not be loaded. Please try again.</div>
  <button class="retry" id="retry" onclick="loadDoc()">Try Again</button>
</div>
<script>
function loadDoc(){
  document.getElementById('err').style.display='none';
  document.getElementById('retry').style.display='none';
  document.getElementById('sp').style.display='block';
  fetch('__PREP__').then(function(r){ return r.json(); })
  .then(function(d){
    if(d && d.ok){ window.location.replace('__VIEW__'); }
    else { throw new Error((d && d.error) || 'failed'); }
  }).catch(function(e){
    document.getElementById('sp').style.display='none';
    document.getElementById('err').style.display='block';
    document.getElementById('retry').style.display='inline-block';
  });
}
loadDoc();
</script></body></html>"""

# Short-lived in-memory cache so the document is fetched from NIOS once (during the
# loader's "prepare" step) and then served as a REAL page navigation at "view".
# Real navigation (not document.write) is what makes the browser's Save-as-PDF /
# Print work reliably on every device, including phones.
import time as _time
_DOC_CACHE = {}
_DOC_TTL = 600  # 10 minutes

def _cache_put(key, tup):
    _DOC_CACHE[key] = (_time.time(),) + tuple(tup)
    if len(_DOC_CACHE) > 150:
        now = _time.time()
        for k in [k for k, v in list(_DOC_CACHE.items()) if now - v[0] > _DOC_TTL]:
            _DOC_CACHE.pop(k, None)

def _cache_get(key):
    v = _DOC_CACHE.get(key)
    if not v:
        return None
    if _time.time() - v[0] > _DOC_TTL:
        _DOC_CACHE.pop(key, None)
        return None
    return v[1], v[2], v[3]

def _fetch_doc_for(row_key, kind):
    """Return ((content, ctype, filename), None) or (None, error_message).
    Never raises — a NIOS/network hiccup becomes a clean error so the student
    sees a 'Try Again' instead of a server crash."""
    try:
        from nios_login import fetch_document
        conn = get_db()
        row = conn.execute("SELECT reference_no, enrollment_no, dob, session FROM student_status WHERE row_key=?",
                           (row_key,)).fetchone()
        conn.close()
        ref = (row["reference_no"] if row else "") or ""
        enroll = (row["enrollment_no"] if row else "") or ""
        if not row or (not ref and not enroll):
            return None, "student not found"
        # Public-cycle students (April / October) only have an ID Card. If an old/wrong
        # link for another document is opened, show a clear message instead of serving the
        # wrong document — this neutralises any links that were sent before the grouping fix.
        import whatsapp
        if whatsapp.group_of(row["session"]) == "public" and kind != "id_card":
            return None, ("This document is not available for Public (April / October) admission. "
                          "Only the ID Card can be opened for your admission type.")
        content, ctype, filename = fetch_document(ref, row["dob"], kind, enrollment_no=enroll)
        if content is None:
            return None, (ctype or "could not load document")
        return (content, ctype, filename), None
    except Exception as e:
        logger.warning(f"doc fetch failed for {kind}: {e}")
        return None, "could not load document"

def _serve_doc(content, ctype, filename):
    from fastapi import Response
    # inline -> the browser renders it as a normal page so Print / Save-as-PDF works
    return Response(content=content, media_type=ctype,
                    headers={"Content-Disposition": "inline"})

def _loader_html(kind, prep_url, view_url):
    label = DOC_LABELS.get(kind, "Document")
    return (LOADING_PAGE.replace("__LABEL__", label)
            .replace("__PREP__", prep_url).replace("__VIEW__", view_url))

def _invalid_link_html():
    return HTMLResponse("<h3 style='font-family:sans-serif;text-align:center;margin-top:60px'>"
                        "This link is invalid or has expired.</h3>", status_code=404)

# ── Long signed links: /doc/{token} ──
@app.get("/doc/{token}", response_class=HTMLResponse)
async def public_single_doc(token: str):
    from links import verify_doc_link
    row_key, kind = verify_doc_link(token)
    if not row_key:
        return _invalid_link_html()
    return HTMLResponse(_loader_html(kind, f"/doc/{token}/prepare", f"/doc/{token}/view"))

@app.get("/doc/{token}/prepare")
async def public_doc_prepare(token: str):
    from links import verify_doc_link
    row_key, kind = verify_doc_link(token)
    if not row_key:
        return {"ok": False, "error": "invalid link"}
    res, err = _fetch_doc_for(row_key, kind)
    if err:
        return {"ok": False, "error": err}
    _cache_put("doc:" + token, res)
    return {"ok": True}

@app.get("/doc/{token}/view")
async def public_doc_view(token: str):
    cached = _cache_get("doc:" + token)
    if not cached:
        from links import verify_doc_link
        row_key, kind = verify_doc_link(token)
        if not row_key:
            raise HTTPException(status_code=404, detail="invalid link")
        res, err = _fetch_doc_for(row_key, kind)
        if err:
            raise HTTPException(status_code=404, detail=err)
        cached = res
    return _serve_doc(*cached)

# ── Short links: /s/{code} ──
@app.get("/s/{code}", response_class=HTMLResponse)
async def short_doc(code: str):
    from shortlinks import resolve_short
    row_key, kind = resolve_short(code)
    if not row_key:
        return _invalid_link_html()
    return HTMLResponse(_loader_html(kind, f"/s/{code}/prepare", f"/s/{code}/view"))

@app.get("/s/{code}/prepare")
async def short_doc_prepare(code: str):
    from shortlinks import resolve_short
    row_key, kind = resolve_short(code)
    if not row_key:
        return {"ok": False, "error": "invalid link"}
    res, err = _fetch_doc_for(row_key, kind)
    if err:
        return {"ok": False, "error": err}
    _cache_put("s:" + code, res)
    return {"ok": True}

@app.get("/s/{code}/view")
async def short_doc_view(code: str):
    cached = _cache_get("s:" + code)
    if not cached:
        from shortlinks import resolve_short
        row_key, kind = resolve_short(code)
        if not row_key:
            raise HTTPException(status_code=404, detail="invalid link")
        res, err = _fetch_doc_for(row_key, kind)
        if err:
            raise HTTPException(status_code=404, detail=err)
        cached = res
    return _serve_doc(*cached)

@app.get("/logo.png")
async def logo_png():
    """MVS Foundation logo (embedded, served for portal + student pages)."""
    import base64
    from fastapi import Response
    try:
        from assets import LOGO_B64
        return Response(content=base64.b64decode(LOGO_B64), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        raise HTTPException(status_code=404, detail="logo not found")
