# 🏫 NIOS Status Tracker — MVS Foundation

Automatic NIOS admission status checker for 100+ students.
Runs every 6 hours, updates Excel, tracks changes, monitoring portal included.

---

## 📁 Files

```
nios-tracker/
├── main.py           # FastAPI app + scheduler + all API routes
├── scraper.py        # Selenium NIOS website scraper
├── excel_handler.py  # Excel read/write with colour coding
├── job_runner.py     # Core job logic (scrape + compare + update)
├── database.py       # SQLite DB setup
├── portal.html       # Monitoring portal UI
├── requirements.txt  # Python dependencies
├── nixpacks.toml     # Railway: install Chrome + ChromeDriver
├── Procfile          # Railway: start command
└── .env.example      # Environment variables template
```

---

## 🚀 Deploy to Railway

### Step 1: Push to GitHub
```bash
git add .
git commit -m "NIOS Tracker initial deploy"
git push
```

### Step 2: Create Railway Service
1. Go to railway.app → New Project → Deploy from GitHub
2. Select your repo

### Step 3: Set Environment Variables in Railway
Go to your service → Variables tab → Add:

| Variable      | Value                          |
|--------------|--------------------------------|
| PORTAL_USER  | admin                          |
| PORTAL_PASS  | YourPassword123                |
| SECRET_KEY   | any-random-long-string         |
| EXCEL_PATH   | students.xlsx                  |

### Step 4: Deploy
Railway will auto-detect nixpacks.toml and install Chrome + ChromeDriver.

---

## 📊 Excel Format Required

Your Excel file MUST have a column named **"Reference No"** (or similar).

Optional columns auto-detected:
- Name / Student Name
- Class / Class Level

The system will **add** these columns automatically:
- `NIOS Status` — colour coded by status
- `Last Checked` — when last checked
- `Last Changed` — when status last changed

### Colour coding:
| Status                                  | Colour       |
|----------------------------------------|--------------|
| Pending                                 | 🟡 Yellow    |
| Documents Verification In Progress      | 🟠 Orange    |
| Verified                                | 🟢 Green     |
| Approved                                | 🩵 Teal      |
| Admitted                                | 🔵 Blue      |
| Rejected                                | 🔴 Red       |
| Fetch Error                             | ⚪ Grey      |

---

## 🖥️ Portal Usage

1. Open your Railway URL in browser
2. Login with PORTAL_USER / PORTAL_PASS
3. Upload your Excel file (Upload Excel tab)
4. Click **"Run Now"** to test immediately
5. System will auto-run every 6 hours

### Portal Features:
- 📊 Dashboard — stats, distribution chart, recent runs
- 👥 Students — searchable table with all statuses + colours
- 📋 Change History — every status change logged
- 🔄 Run Logs — every run with checked/changed/failed counts
- 📤 Upload Excel — replace student file
- ⚙️ Settings — change run interval (1–24 hours)

---

## ⚠️ Important Notes

1. **NIOS website changes**: If NIOS changes their website layout,
   update the CSS selectors in `scraper.py` → `fetch_status_for_reference()`

2. **Rate limiting**: System waits 2 seconds between each student check
   to avoid getting blocked. 100 students ≈ 5-6 minutes per run.

3. **Excel persistence on Railway**: Railway's filesystem resets on redeploy.
   Always re-upload Excel after redeploy, or use a persistent volume.

4. **Persistent Volume** (recommended): In Railway, add a volume mounted
   at `/data` and set `EXCEL_PATH=/data/students.xlsx`

---

## 🔧 Local Testing

```bash
pip install -r requirements.txt
python database.py   # Initialize DB
uvicorn main:app --reload --port 8000
# Open http://localhost:8000
```
