"""Check the GA4 events the browser sends, and that none of them carry PII.

Run against a server started with GA_MEASUREMENT_ID set (see README).
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8090"

# Values from the sample data that must never reach Google Analytics.
FORBIDDEN = ["KJL4821", "R-1001", "Jordan", "trips.csv", "toll_bill.pdf", "dev@example.com", "dev-local"]

EXPECTED = {
    "processing_started",
    "processing_succeeded",
    "results_viewed",
    "document_upload_started",
    "document_upload_completed",
    "evidence_viewed",
    "evidence_downloaded",
    "unmatched_results_viewed",
    "results_exported",
    "subscription_management_viewed",
}


def events(page) -> list[tuple[str, dict]]:
    raw = page.evaluate("() => (window.dataLayer || []).map(a => Array.from(a))")
    return [(item[1], item[2] or {}) for item in raw if item and item[0] == "event"]


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:29229")
        page = browser.contexts[0].new_page()
        page.goto(BASE, wait_until="load")
        page.wait_for_function("() => !!window.dataLayer", timeout=15_000)

        page.fill("#excludeList", "")
        page.set_input_files("#tollFiles", str(HERE / "toll_bill.pdf"))
        page.set_input_files("#tripFile", str(HERE / "trips.csv"))
        page.click("#parseBtn")
        page.wait_for_selector("#chargeTable tbody tr", timeout=120_000)

        page.select_option("#dStatus", "unmatched")
        page.wait_for_timeout(200)
        page.select_option("#dStatus", "")

        page.click("#chargeTable [data-png] >> nth=0")
        page.wait_for_selector("#pngBody img", timeout=15_000)
        with page.expect_download():
            page.click("#pngDownload")
        page.keyboard.press("Escape")

        with page.expect_download():
            page.click("#exportBtn")
        page.click('.navitem[data-view="billing"]')
        page.wait_for_timeout(300)

        sent = events(page)
        names = {name for name, _ in sent}
        for name, params in sent:
            print(name, json.dumps(params, sort_keys=True))

        missing = EXPECTED - names
        assert not missing, f"missing events: {sorted(missing)}"

        blob = json.dumps(sent)
        leaked = [value for value in FORBIDDEN if value in blob]
        assert not leaked, f"PII in analytics payloads: {leaked}"

        succeeded = next(params for name, params in sent if name == "processing_succeeded")
        assert succeeded["matched_count"] > 0 and succeeded["processing_duration_ms"] > 0, succeeded

        page.close()
    print("analytics check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
