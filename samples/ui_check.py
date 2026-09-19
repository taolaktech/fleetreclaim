"""Drive the page in a real browser to check parsing, filtering, and export."""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:29229")
        page = browser.contexts[0].new_page()
        page.goto("http://localhost:8080", wait_until="load")
        page.set_input_files("#tollFiles", str(HERE / "toll_bill.pdf"))
        page.set_input_files("#tripFile", str(HERE / "turo_trips.csv"))
        page.click("#parseBtn")
        page.wait_for_selector("#chargeTable tr:nth-child(2)", timeout=120_000)

        rows = lambda: page.locator("#detailTable tr").count() - 1
        all_rows = rows()
        print("rows unfiltered:", all_rows, "| charge total:", page.locator("#cards .card").nth(5).inner_text().replace("\n", " "))

        page.select_option("#fPlate", "KJL4821")
        page.wait_for_timeout(300)
        print("plate filter rows:", rows(), "|", page.locator("#filterNote").inner_text())
        assert rows() < all_rows and rows() > 0

        page.select_option("#fPlate", "")
        page.fill("#fFrom", "2026-03-08")
        page.fill("#fTo", "2026-03-15")
        page.wait_for_timeout(300)
        dates = page.locator("#detailTable tr td:nth-child(3)").all_inner_texts()
        print("date filter rows:", rows(), sorted(set(dates)))
        assert all("2026-03-08" <= d <= "2026-03-15" for d in dates), dates

        page.click("#clearFilters")
        page.fill("#fTrip", "R-1004")
        page.wait_for_timeout(300)
        trip_cells = page.locator("#chargeTable tr td:nth-child(1)").all_inner_texts()
        print("trip filter:", trip_cells)
        assert trip_cells == ["R-1004"], trip_cells

        page.click("#clearFilters")
        vehicles = page.locator("#fVehicle option").all_inner_texts()
        print("vehicle options:", vehicles)
        rav4 = next(v for v in vehicles if "RAV4" in v)
        page.select_option("#fVehicle", rav4)
        page.wait_for_timeout(300)
        shown = page.locator("#chargeTable tr td:nth-child(3)").all_inner_texts()
        print("vehicle filter rows:", rows(), shown)
        assert rows() > 0 and set(shown) == {rav4}, shown

        with page.expect_download() as download:
            page.click("#exportBtn")
        out = HERE / "export_check.csv"
        download.value.save_as(str(out))
        print("csv lines:", len(out.read_text().splitlines()))

        page.screenshot(path=str(HERE / "ui_filters.png"), full_page=True)
        page.close()
    print("UI check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
