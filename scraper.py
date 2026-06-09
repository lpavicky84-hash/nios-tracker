import time
import logging
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

logger = logging.getLogger(__name__)

NIOS_URL = "https://sdmis.nios.ac.in/registration/check-admission-status"

# Status colour mapping
STATUS_COLORS = {
    "pending":                     {"hex": "FFF9C4", "label": "Pending"},
    "documents verification":      {"hex": "FFE0B2", "label": "Documents Verification In Progress"},
    "verified":                     {"hex": "C8E6C9", "label": "Verified"},
    "approved":                     {"hex": "B2DFDB", "label": "Approved"},
    "admitted":                     {"hex": "BBDEFB", "label": "Admitted"},
    "rejected":                     {"hex": "FFCDD2", "label": "Rejected"},
    "error":                        {"hex": "E0E0E0", "label": "Fetch Error"},
    "not found":                    {"hex": "F8BBD0", "label": "Not Found"},
    "unknown":                      {"hex": "F5F5F5", "label": "Unknown"},
}

def get_status_color(status_text: str) -> dict:
    """Match status text to a colour config."""
    if not status_text:
        return STATUS_COLORS["unknown"]
    s = status_text.lower()
    for key, val in STATUS_COLORS.items():
        if key in s:
            return val
    return STATUS_COLORS["unknown"]

def create_driver():
    """Create a headless Chrome driver suitable for Railway."""
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
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    # On Railway, chromedriver is typically in PATH after buildpack install
    try:
        service = Service("/usr/bin/chromedriver")
        driver = webdriver.Chrome(service=service, options=options)
    except Exception:
        # Fallback: use webdriver-manager (local dev)
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver

def fetch_status_for_reference(driver, reference_no: str) -> dict:
    """
    Open NIOS page, enter reference number, submit, read result.
    Returns dict: { reference_no, status, raw_text, success }
    """
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
        ref_input = wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//input[@placeholder='Reference No' or contains(@id,'reference') or contains(@name,'reference')]")
            )
        )
        ref_input.clear()
        ref_input.send_keys(str(reference_no).strip())
        time.sleep(0.5)

        # Click Submit
        submit_btn = driver.find_element(
            By.XPATH,
            "//button[@type='submit' or contains(text(),'Submit')] | //input[@type='submit']"
        )
        submit_btn.click()

        # Wait for result container (adjust selector if NIOS changes layout)
        time.sleep(3)

        # Try to grab any result/status element
        try:
            result_area = wait.until(
                EC.presence_of_element_located(
                    (By.XPATH,
                     "//*[contains(@class,'status') or contains(@class,'result') "
                     "or contains(@class,'admission') or contains(@id,'result') "
                     "or contains(@id,'status')]")
                )
            )
            raw_text = result_area.text.strip()
        except Exception:
            # Fallback: grab whole body text
            raw_text = driver.find_element(By.TAG_NAME, "body").text

        if not raw_text or len(raw_text) < 5:
            result["status"] = "not found"
            result["raw_text"] = "No result text returned"
        else:
            result["raw_text"] = raw_text[:500]
            # Determine clean status label
            color_info = get_status_color(raw_text)
            result["status"] = color_info["label"]
            result["success"] = True

    except Exception as e:
        logger.error(f"Error fetching {reference_no}: {e}")
        result["raw_text"] = str(e)[:300]

    return result

def scrape_all_students(reference_numbers: list) -> list:
    """
    Scrape NIOS status for a list of reference numbers.
    Returns list of result dicts.
    """
    logger.info(f"Starting scrape for {len(reference_numbers)} students...")
    results = []
    driver = None
    try:
        driver = create_driver()
        for i, ref_no in enumerate(reference_numbers):
            logger.info(f"[{i+1}/{len(reference_numbers)}] Checking: {ref_no}")
            res = fetch_status_for_reference(driver, ref_no)
            results.append(res)
            # Polite delay to avoid rate limiting
            time.sleep(2)
    except Exception as e:
        logger.error(f"Driver-level error: {e}")
    finally:
        if driver:
            driver.quit()

    logger.info(f"Scrape complete. {len(results)} results fetched.")
    return results
