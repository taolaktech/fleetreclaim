"""Unit checks for the matching passes (no server needed)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.matching import match  # noqa: E402
from app.parsers import PageLine, _line_to_toll  # noqa: E402

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

    # Trip plate unknown: the date alone carries the match, flagged for review.
    plateless = {**TRIP, "plate": ""}
    result = run([toll(plate="ZZZ9999", time="23:40")], trips=(plateless,))
    assert result["rows"][0]["status"] == "ambiguous", result["rows"][0]
    assert result["rows"][0]["charge"] == 2.5

    # Outside every trip date: nobody is charged.
    result = run([toll(date="2026-07-04", time="12:00")])
    assert result["rows"][0]["status"] == "unmatched", result["rows"][0]
    assert result["rows"][0]["charge"] == 0.0

    # Two overlapping trips, unreadable plate -> ambiguous but still charged.
    other = {**TRIP, "trip_id": "X-2", "plate": "ABC1234", "guest": "Ada"}
    result = run([toll(plate="", time="12:00")], trips=(TRIP, other))
    assert result["rows"][0]["status"] == "ambiguous", result["rows"][0]
    assert result["rows"][0]["charge"] == 2.5

    # Account-activity lines are dropped before matching ever sees them.
    as_line = lambda text: PageLine(text, 0, (0, 0, 10, 10))
    assert _line_to_toll(as_line("07/31/2026 10:29 CDT REBILL TAG STORE AutoCharge: MASTERCARD $80.00"), 0, "f", set()) is None
    assert _line_to_toll(as_line("07/30/2026 11:32 TJM5546 290-GILESMLWB $1.53"), 0, "f", set()) is not None

    # Markup and fee apply to every charged toll.
    result = match([toll(time="12:00")], [TRIP], markup_pct=10, fee_per_toll=1)
    assert result["rows"][0]["charge"] == 3.75, result["rows"][0]

    print("all matching checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
