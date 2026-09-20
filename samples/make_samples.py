"""Generate sample toll bill (PDF + PNG) and Turo trip export for testing."""

import csv
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent

ROWS = [
    ("03/02/2026", "08:14 AM", "KJL4821", "SR-91 Express Lanes EB", "7.25"),
    ("03/02/2026", "06:41 PM", "KJL4821", "SR-91 Express Lanes WB", "9.10"),
    ("03/05/2026", "11:02 AM", "8XYZ123", "Golden Gate Bridge SB", "9.75"),
    ("03/09/2026", "02:35 PM", "KJL4821", "I-110 ExpressLanes", "4.50"),
    ("03/05/2026", "11:30 AM", "", "Richmond Bridge EB", "6.00"),
    ("03/13/2026", "05:10 AM", "8XYZ123", "I-880 Express", "5.00"),
    ("03/14/2026", "07:58 AM", "8XYZ123", "Bay Bridge WB", "8.00"),
    ("03/21/2026", "09:20 AM", "PLT9900", "Orlando SR-417", "3.25"),
]

# Turo exports carry the plate inside the vehicle label rather than its own column.
TRIPS = [
    ["Reservation ID", "Guest", "Vehicle", "Trip start", "Trip end"],
    ["R-1001", "Dana Reyes", "Taofeek's Tesla (TX #KJL4821)", "2026-03-01 15:00", "2026-03-03 11:00"],
    ["R-1002", "Miguel Ortiz", "Taofeek's RAV4 (CA #8XYZ123)", "2026-03-04 09:00", "2026-03-06 18:00"],
    ["R-1003", "Priya Shah", "Taofeek's Tesla (TX #KJL4821)", "2026-03-08 12:00", "2026-03-10 10:00"],
    ["R-1004", "Chris Okafor", "Taofeek's RAV4 (CA #8XYZ123)", "2026-03-13 16:00", "2026-03-15 12:00"],
    ["R-1005", "Sam Lee", "Taofeek's Kia (GA #PLT9900)", "2026-03-05 08:00", "2026-03-07 08:00"],
]


def write_trips() -> None:
    with (HERE / "turo_trips.csv").open("w", newline="") as handle:
        csv.writer(handle).writerows(TRIPS)
    pd.DataFrame(TRIPS[1:], columns=TRIPS[0]).to_excel(HERE / "turo_trips.xlsx", index=False)


def render_bill() -> Image.Image:
    image = Image.new("RGB", (1400, 1000), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=22)
    bold = ImageFont.load_default(size=28)
    draw.text((60, 40), "METRO TOLL AUTHORITY - STATEMENT", font=bold, fill="black")
    draw.text((60, 90), "Invoice 55-2026-3311    Account 90881    Due 04/15/2026", font=font, fill="black")
    headers = ["DATE", "TIME", "LICENSE PLATE", "TOLL LOCATION", "AMOUNT"]
    xs = [60, 260, 440, 680, 1180]
    for x, header in zip(xs, headers):
        draw.text((x, 170), header, font=font, fill="black")
    draw.line((60, 200, 1340, 200), fill="black", width=2)
    y = 225
    for row in ROWS:
        for x, cell in zip(xs, row):
            draw.text((x, y), ("$" + cell) if cell == row[-1] else cell, font=font, fill="black")
        y += 45
    total = sum(float(r[-1]) for r in ROWS)
    draw.text((900, y + 30), f"TOTAL DUE  ${total:.2f}", font=bold, fill="black")
    return image


def main() -> None:
    write_trips()
    bill = render_bill()
    bill.save(HERE / "toll_bill.png")
    bill.save(HERE / "toll_bill.pdf", "PDF", resolution=150)
    print("wrote", [p.name for p in sorted(HERE.iterdir())])


if __name__ == "__main__":
    main()
