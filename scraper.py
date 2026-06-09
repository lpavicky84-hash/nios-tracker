import time
import logging
import os
import subprocess
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

logger = logging.getLogger(__name__)

NIOS_URL = "https://sdmis.nios.ac.in/registration/check-admission-status"

STATUS_COLORS = {
    "pending":                {"hex": "FFF9C4", "label": "Pending"},
    "documents verification": {"hex": "FFE0B2", "label": "Documents Verification In Progress"},
    "verified":               {"hex": "C8E6C9", "label": "Verified"},
    "approved":               {"hex": "B2DFDB", "label": "Approved"},
    "admission confirmed":    {"hex": "69F0AE", "label": "Admission Confirmed"},
    "admitted":               {"hex": "BBDEFB", "label": "Admitted"},
    "rejected":               {"hex": "FFCDD2", "label": "Rejected"},
    "error":                  {"hex": "E0E0E0", "label": "Fetch Error"},
    "not found":              {"hex": "F8BBD0", "label": "Not Found"},
    "unknown":                {"hex": "F5F5F5", "label": "Unknown"},
}

def get_status_color(status_text: str) -> dict:
    if not status_text:
        return STATUS_COLORS["unknown"]
    s = status_text.lower()
    for key, val in STATUS_COLORS.items():
        if key in s:
            return val
    return STATUS_COLORS["unknown"]

def find_chrome_binary():
    """Find Chrome/Chromium binary on Railway (Nix) or local."""
    candidates = [
        "/run/current-system/sw/bin/chromium",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/nix/var/nix/profiles/default/bin/chromium",
    ]
    # Also try which
    for name in ["chromium", "chromium-browser", "google-chrome"]:
        try:
            result = subprocess.run(["which", name], capture_output=True, text=True)
            if result.returncode == 0:
                path = result.stdout.strip()
                if path:
                    logger.info(f"Found Chrome via which: {path}")
                    return path
        except Exception:
            pass

    for path in candidates:
        if os.path.exists(path):
            logger.info(f"Found Chrome at: {path}")
            return path

    # Try nix-specific path
    try:
        result = subprocess.run(
            ["find", "/nix", "-name", "chromium", "-type", "f"],
            capture_output=True, text=True, timeout=10
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        if lines:
            logger.info(f"Found Chrome via find: {lines[0]}")
            return lines[0]
    except Exception:
        pass

    logger.warning("Chrome binary not found!")
    return None

def find_chromedriver():
    """Find chromedriver binary."""
    candidates = [
        "/run/current-system/sw/bin/chromedriver",
        "/usr/bin/chromedriver",
        "/nix/var/nix/profiles/default/bin/chromedriver",
    ]
    for name in ["chromedriver"]:
        try:
            result = subprocess.run(["which", name], capture_output=True, text=True)
            if result.returncode == 0:
                path = result.stdout.strip()
                if path:
                    logger.info(f"Found chromedriver via which: {path}")
                    return path
        except Exception:
            pass

    for path in candidates:
        if os.path.exists(path):
            logger.info(f"Found chromedriver at: {path}")
            return path

    try:
        result = subprocess.run(
            ["find", "/nix", "-name", "chromedriver", "-type", "f"],
            capture_output=True, text=True, timeout=10
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        if lines:
            logger.info(f"Found chromedriver via find: {lines[0]}")
            return lines[0]
    except Exception:
        pass

    return None

def create_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    chrome_bin = find_chrome_binary()
    if chrome_bin:
        options.binary_location = chrome_bin

    chromedriver_path = find_chromedriver()
    if chromedriver_path:
        service = Service(chromedriver_path)
    else:
        # Last resort: try default
        service = Service()

    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver

def fetch_status_for_reference(driver, reference_no: str) -> dict:
    result = {
        "reference_no": reference_no,
        "status": "error",
        "raw_text": "",
        "success": False,
    }
    try:
        driver.get(NIOS_URL)
        wait = WebDriverWait(driver, 20)

        # Wait for Reference No input
        ref_input = wait.until(EC.presence_of_element_located((
            By.XPATH,
            "//input[@placeholder='Reference No' or contains(@id,'reference') "
            "or contains(@name,'reference') or contains(@placeholder,'Reference')]"
        )))
        ref_input.clear()
        ref_input.send_keys(str(reference_no).strip())
        time.sleep(0.5)

        # Click Submit
        submit_btn = driver.find_element(
            By.XPATH,
            "//button[@type='submit' or contains(text(),'Submit')] | //input[@type='submit']"
        )
        submit_btn.click()
        time.sleep(3)

        # Grab result text
        try:
            result_area = wait.until(EC.presence_of_element_located((
                By.XPATH,
                "//*[contains(@class,'status') or contains(@class,'result') "
                "or contains(@class,'admission') or contains(@id,'result') "
                "or contains(@id,'status') or contains(@class,'alert')]"
            )))
            raw_text = result_area.text.strip()
        except Exception:
            raw_text = driver.find_element(By.TAG_NAME, "body").text

        if not raw_text or len(raw_text) < 5:
            result["status"] = "Not Found"
            result["raw_text"] = "No result returned"
        else:
            result["raw_text"] = raw_text[:500]
            color_info = get_status_color(raw_text)
            result["status"] = color_info["label"]
            result["success"] = True

    except Exception as e:
        logger.error(f"Error fetching {reference_no}: {e}")
        result["raw_text"] = str(e)[:300]

    return result

def scrape_all_students(reference_numbers: list) -> list:
    logger.info(f"Starting scrape for {len(reference_numbers)} students...")
    results = []
    driver = None
    try:
        driver = create_driver()
        for i, ref_no in enumerate(reference_numbers):
            logger.info(f"[{i+1}/{len(reference_numbers)}] Checking: {ref_no}")
            res = fetch_status_for_reference(driver, ref_no)
            results.append(res)
            time.sleep(2)
    except Exception as e:
        logger.error(f"Driver-level error: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    logger.info(f"Scrape complete. {len(results)} results.")
    return results
