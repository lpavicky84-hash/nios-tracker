MVS FOUNDATION — NIOS ADMISSIONS TRACKER
========================================
Live: status.mvsfoundation.in  |  GitHub repo: lpavicky84-hash/nios-tracker
Host: Railway (auto-redeploys on GitHub push)
Stack: FastAPI + SQLite. Single large main.py holds the whole dashboard (PORTAL_HTML inline JS).

FILES IN THIS ZIP (all the code — deploy by pasting into GitHub):
  main.py         - all API endpoints + the full dashboard UI (HTML/JS)
  job_runner.py   - the status-check run engine (scrape -> update -> WhatsApp -> push)
  scraper.py      - public NIOS admission-status scraper (status + TOC from one page)
  nios_login.py   - NIOS login (REFERENCE-ONLY now), doc fetching, TOC parsing
  mvs_sync.py     - talks to the MVS Portal (login.mvsfoundation.in) trackerList + push
  database.py     - SQLite schema + migrations (auto-adds columns on boot)
  whatsapp.py     - AiSensy campaigns + per-session/TOC document rules
  excel_handler.py, assets.py, links.py, shortlinks.py - helpers
  Procfile, requirements.txt - Railway config

NOT INCLUDED (created automatically at runtime, don't upload):
  nios_tracker.db (the database), __pycache__, doccache/ (cached documents)

ENV VARS (set in Railway, not in code):
  CAPTCHA_API_KEY (CapSolver), CAPSOLVER_PROXY (India residential),
  AISENSY_API_KEY, MVS_API_URL / MVS_TRACKER_KEY (Portal), SECRET_KEY, DATA_DIR

ADMIN LOGIN (test): username 'admin'  password 'MVS2025'

KEY BEHAVIOURS (as of this build, Jul 2026):
  * TOC read comes FREE with each status fetch (same NIOS page = 1 captcha).
    Mismatch with Portal tocStatus is auto-corrected + pushed. NIOS is source of truth.
  * Confirm-time final TOC check + resend-time TOC check -> WhatsApp always uses correct campaign.
  * NIOS login now REFERENCE-ONLY (NIOS removed enrollment from the login page).
    Status-check page still supports reference OR enrollment.
  * Runs QUEUE (auto) — a running check is never auto-cancelled; manual run while busy is refused.
  * Portal origin split (real_enrolment / bulk_imported) drives the Enrol vs MVS Portal cards.
  * "Not fully saved" filter = students missing >=1 document (matches the header count).
  * Confirmed students are NEVER downgraded by a lower NIOS status once docs are sent.
  * TWO-WAY DETAIL SYNC (Jul 2026): any tracker-side edit (DOB, mobile, reference,
    enrollment, name, email, class, session) is pushed to the Portal on Save; the result
    is stored in edit_sync and shown in the Edit modal. TOC pushes now BLANK the Portal's
    TOC-subject field on NO (else the Portal auto-flips back to yes) and WRITE the
    subjects on YES. Bulk backfill: dashboard -> Tracker vs Portal panel ->
    "Sync details -> Portal" (endpoints /api/portal-sync-details + -status).

DEPLOY: paste each changed file into GitHub (web UI) -> Railway redeploys.
  main.py is huge (~580KB) — after pasting, confirm the file size/updated-time on GitHub.
  Hard-refresh the browser (Ctrl+Shift+R) after deploy to clear cached JS.
