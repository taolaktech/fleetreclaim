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
        page.fill("#excludeList", "")  # a previous run may have saved exclusions
        page.set_input_files("#tollFiles", str(HERE / "toll_bill.pdf"))
        page.set_input_files("#tripFile", str(HERE / "turo_trips.csv"))
        page.click("#parseBtn")
        page.wait_for_selector("#chargeTable tr:nth-child(2)", timeout=120_000)

        rows = lambda: page.locator("#detailTable tr").count() - 1
        all_rows = rows()
        print("rows unfiltered:", all_rows, "| charge total:", page.locator("#cards .card").nth(5).inner_text().replace("\n", " "))

        # R-1001 has $10 already billed by Turo, so only the shortfall is charged.
        r1001 = page.locator("#chargeTable tr", has_text="R-1001").first
        cells = r1001.locator("td").all_inner_texts()
        print("R-1001 row:", cells)
        assert cells[6] == "$16.35" and cells[7] == "$10.00" and cells[8] == "$6.35", cells

        # R-1005 matched no toll but Turo collected $4 on it: still listed, $0 owed.
        paid = page.locator("#chargeTable tr", has_text="R-1005").first.locator("td").all_inner_texts()
        print("R-1005 row:", paid)
        assert paid[5] == "0" and paid[7] == "$4.00" and paid[8] == "$0.00", paid

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

        page.click("#clearFilters")
        page.select_option("#dStatus", "unmatched")
        page.wait_for_timeout(300)
        statuses = page.locator("#detailTable tr td:nth-child(1)").all_inner_texts()
        print("detail status filter:", statuses, "|", page.locator("#detailNote").inner_text())
        assert statuses and set(statuses) == {"unmatched"}, statuses
        note = page.locator("#detailNote").inner_text()
        assert "$3.25" in note, note  # the one unmatched sample toll
        page.select_option("#dStatus", "")
        page.select_option("#dPlate", "KJL4821")
        page.wait_for_timeout(300)
        plates = page.locator("#detailTable tr td:nth-child(5)").all_inner_texts()
        print("detail plate filter:", plates)
        assert plates and set(plates) == {"KJL4821"}, plates
        trip_rows = page.locator("#chargeTable tr").count() - 1
        assert trip_rows == 5, trip_rows  # detail filters must not touch the charge table

        page.click("#clearFilters")
        count_link = page.locator("#chargeTable [data-trip]").first
        expected = int(count_link.inner_text())
        count_link.click()
        page.wait_for_selector("#tripModal:not(.hidden)", timeout=5_000)
        modal_rows = page.locator("#modalTable tr").count() - 1
        print("modal:", page.locator("#modalHead").inner_text().replace("\n", " | "), "| rows:", modal_rows)
        assert modal_rows == expected, (modal_rows, expected)
        with page.expect_download() as trip_dl:
            page.click("#modalCsv")
        trip_csv = HERE / "trip_check.csv"
        trip_dl.value.save_as(str(trip_csv))
        print("per-trip csv:", trip_dl.value.suggested_filename, len(trip_csv.read_text().splitlines()), "lines")
        page.click("#modalPng")
        page.wait_for_selector("#pngBody img", timeout=15_000)
        print("bill preview from trip modal:", page.locator("#pngHead").inner_text().replace("\n", " | "))
        page.keyboard.press("Escape")  # closes the preview, leaving the trip modal open
        page.wait_for_selector("#pngModal.hidden", state="attached", timeout=5_000)
        assert page.locator("#tripModal:not(.hidden)").count() == 1
        page.keyboard.press("Escape")
        page.wait_for_selector("#tripModal.hidden", state="attached", timeout=5_000)

        page.click("#chargeTable [data-png] >> nth=0")
        page.wait_for_selector("#pngBody img", timeout=15_000)
        print("guest row preview:", page.locator("#pngHead").inner_text().replace("\n", " | "))
        assert page.locator("#tripModal.hidden").count() == 1  # guest link skips the trip modal
        with page.expect_download() as png_dl:
            page.click("#pngDownload")
        trip_png = HERE / "trip_check.png"
        png_dl.value.save_as(str(trip_png))
        print("preview download:", png_dl.value.suggested_filename, trip_png.stat().st_size, "bytes")
        assert trip_png.stat().st_size > 1_000
        page.keyboard.press("Escape")
        page.wait_for_selector("#pngModal.hidden", state="attached", timeout=5_000)

        with page.expect_download() as download:
            page.click("#exportBtn")
        out = HERE / "export_check.csv"
        download.value.save_as(str(out))
        print("csv lines:", len(out.read_text().splitlines()))

        page.screenshot(path=str(HERE / "ui_filters.png"), full_page=True)

        # user-defined exclusion phrase drops the matching bill lines at parse time
        page.fill("#excludeList", "Golden Gate Bridge")
        page.click("#parseBtn")
        page.wait_for_function(
            "() => document.getElementById('parseStatus').textContent.includes('bill line items')",
            timeout=120_000,
        )
        excluded_rows = page.locator("#detailTable tr").count() - 1
        print("after custom exclusion:", page.locator("#parseStatus").inner_text())
        assert excluded_rows == all_rows - 1, (excluded_rows, all_rows)
        assert "Golden Gate" not in page.locator("#detailTable").inner_text()

        page.close()
    print("UI check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
