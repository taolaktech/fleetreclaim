"""Unit checks for the matching passes (no server needed)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.matching import match  # noqa: E402
from app.parsers import PageLine, _line_to_toll, parse_trips  # noqa: E402

TRIP = {
    "trip_id": "58691937",
    "guest": "Sam Lee",
    "vehicle": "Taofeek's Nissan (TX #VCG6008)",
    "plate": "VCG6008",
    "start": "2026-06-25T14:00",
    "end": "2026-06-29T10:00",
}


def toll(**kwargs):
    base = {"date": "2026-06-29", "time": "", "plate": "VCG6008", "amount": 2.5, "location": ""}
    return {**base, **kwargs}


def run(tolls, trips=(TRIP,)):
    return match(list(tolls), list(trips))


def main() -> int:
    # Last day of the trip, after the return time: still the trip's toll.
    result = run([toll(time="23:40")])
    assert result["rows"][0]["status"] == "matched", result["rows"][0]
    assert result["rows"][0]["trip"]["trip_id"] == "58691937"
    assert result["rows"][0]["charge"] == 2.5

    # First day, before pickup time.
    result = run([toll(date="2026-06-25", time="02:00")])
    assert result["rows"][0]["status"] == "matched", result["rows"][0]

    # OCR swapped 0/O and 6/G on the plate.
    result = run([toll(plate="VC66OO8", time="12:00")])
    assert result["rows"][0]["status"] == "matched", result["rows"][0]
    assert result["rows"][0]["basis"] == "similar plate + trip dates"

    # A plate that contradicts the trip's plate is never charged to that trip.
    result = run([toll(plate="ZZZ9999", time="12:00")])
    assert result["rows"][0]["status"] == "unmatched", result["rows"][0]
    assert result["rows"][0]["charge"] == 0.0

    # Trip plate unknown: the date alone carries the match.
    plateless = {**TRIP, "plate": ""}
    result = run([toll(plate="ZZZ9999", time="23:40")], trips=(plateless,))
    assert result["rows"][0]["status"] == "matched", result["rows"][0]
    assert result["rows"][0]["charge"] == 2.5

    # Outside every trip date: nobody is charged.
    result = run([toll(date="2026-07-04", time="12:00")])
    assert result["rows"][0]["status"] == "unmatched", result["rows"][0]
    assert result["rows"][0]["charge"] == 0.0

    # Two overlapping trips, unreadable plate -> charged to the best-ranked one.
    other = {**TRIP, "trip_id": "X-2", "plate": "ABC1234", "guest": "Ada"}
    result = run([toll(plate="", time="12:00")], trips=(TRIP, other))
    assert result["rows"][0]["status"] == "matched", result["rows"][0]
    assert result["rows"][0]["charge"] == 2.5

    # Account-activity lines are dropped before matching ever sees them.
    as_line = lambda text: PageLine(text, 0, (0, 0, 10, 10))
    assert _line_to_toll(as_line("07/31/2026 10:29 CDT REBILL TAG STORE AutoCharge: MASTERCARD $80.00"), 0, "f", set()) is None
    assert _line_to_toll(as_line("07/29/2026 09:10 CDT SUPPORT SERVICES, SYSTEM FEE $1.00"), 0, "f", set()) is None
    assert _line_to_toll(as_line("07/30/2026 11:32 TJM5546 290-GILESMLWB $1.53"), 0, "f", set()) is not None
    # user-supplied phrases exclude extra lines, case- and spacing-insensitively
    line = as_line("07/30/2026 11:32 TJM5546 Golden  Gate Bridge SB $1.53")
    assert _line_to_toll(line, 0, "f", set(), ["golden gate bridge"]) is None
    assert _line_to_toll(line, 0, "f", set(), ["parking"]) is not None

    # The marketplace already billed part of the tolls: only the shortfall is owed, never negative.
    paid = {**TRIP, "already_charged": 1.0}
    result = run([toll(time="12:00"), toll(time="13:00")], trips=(paid,))
    trip_total = result["trips"][0]
    assert trip_total["toll_total"] == 5.0 and trip_total["charge_total"] == 4.0, trip_total
    assert result["summary"]["charge_total"] == 4.0
    result = run([toll(time="12:00")], trips=({**TRIP, "already_charged": 99.0},))
    assert result["trips"][0]["charge_total"] == 0.0

    # A canceled trip owns nothing: no match, and it never shows up as a trip to bill.
    canceled = {**TRIP, "canceled": True, "status": "Cancelled", "already_charged": 4.0}
    result = run([toll(time="12:00")], trips=(canceled,))
    assert result["rows"][0]["status"] == "unmatched", result["rows"][0]
    assert result["trips"] == [], result["trips"]
    assert result["summary"]["canceled_trips"] == 1

    # ... and the overlapping active trip takes the toll instead.
    active = {**TRIP, "trip_id": "X-3", "guest": "Ada", "status": "Completed"}
    result = run([toll(time="12:00")], trips=(canceled, active))
    assert result["rows"][0]["trip"]["trip_id"] == "X-3", result["rows"][0]
    assert [t["trip_id"] for t in result["trips"]] == ["X-3"], result["trips"]

    # Trip exports that carry a status column mark cancellations for the matcher.
    export = (
        "Reservation ID,Guest,Vehicle,Trip start,Trip end,Trip status\n"
        "R-1,Dana,Tesla (TX #KJL4821),2026-03-01 15:00,2026-03-03 11:00,Completed\n"
        "R-2,Miguel,RAV4 (CA #8XYZ123),2026-03-04 09:00,2026-03-06 18:00,Cancelled by guest\n"
        "R-3,Priya,Kia (GA #PLT9900),2026-03-08 12:00,2026-03-10 10:00,\n"
    )
    parsed = parse_trips("trips.csv", export.encode())
    assert [t.canceled for t in parsed] == [False, True, False], parsed
    assert parsed[1].status == "Cancelled by guest"

    # Markup and fee apply to every charged toll.
    result = match([toll(time="12:00")], [TRIP], markup_pct=10, fee_per_toll=1)
    assert result["rows"][0]["charge"] == 3.75, result["rows"][0]

    print("all matching checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
