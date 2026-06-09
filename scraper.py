import time
import logging
import asyncio
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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

def get_status_label(text: str) -> str:
    if not text:
        return "Unknown"
    t = text.lower()
    for key, val in STATUS_COLORS.items():
        if key in t:
            return val["label"]
    return "Unknown"

def fetch_status_for_reference(page, reference_no: str) -> dict:
    result = {
        "reference_no": reference_no,
        "status": "Fetch Error",
        "raw_text": "",
        "success": False,
    }
    try:
        page.goto(NIOS_URL, wait_until="networkidle", timeout=30000)

        # Fill Reference No input
        ref_input = page.locator(
            "input[placeholder*='Reference'], input[id*='reference'], input[name*='reference']"
        ).first
        ref_input.wait_for(timeout=15000)
        ref_input.fill(str(reference_no).strip())
        time.sleep(0.5)

        # Click Submit
        page.locator("button[type='submit'], input[type='submit']").first.click()

        # Wait for result
        time.sleep(3)

        # Try to get result text
        try:
            result_el = page.locator(
                ".status, .result, .admission, [id*='result'], [id*='status'], .alert, .card-body"
            ).first
            raw_text = result_el.inner_text(timeout=5000).strip()
        except Exception:
            raw_text = page.locator("body").inner_text(timeout=5000)

        if raw_text and len(raw_text) > 5:
            result["raw_text"] = raw_text[:500]
            result["status"] = get_status_label(raw_text)
            result["success"] = True
            logger.info(f"  {reference_no} → {result['status']}")
        else:
            result["status"] = "Not Found"
            result["raw_text"] = "No result text found"

    except Exception as e:
        logger.error(f"Error fetching {reference_no}: {e}")
        result["raw_text"] = str(e)[:300]

    return result

def scrape_all_students(reference_numbers: list) -> list:
    logger.info(f"Starting scrape for {len(reference_numbers)} students...")
    results = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ]
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()

            for i, ref_no in enumerate(reference_numbers):
                logger.info(f"[{i+1}/{len(reference_numbers)}] Checking: {ref_no}")
                res = fetch_status_for_reference(page, ref_no)
                results.append(res)
                time.sleep(2)  # polite delay

            browser.close()

    except Exception as e:
        logger.error(f"Playwright error: {e}")
        # Return error results for any remaining
        checked = {r["reference_no"] for r in results}
        for ref in reference_numbers:
            if ref not in checked:
                results.append({
                    "reference_no": ref,
                    "status": "Fetch Error",
                    "raw_text": str(e)[:200],
                    "success": False,
                })

    logger.info(f"Scrape complete. {len(results)} results.")
    return results
