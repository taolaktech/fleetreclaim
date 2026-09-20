"""Match toll line items to Turo trips by plate and timestamp."""

from datetime import datetime, timedelta
from typing import Any


def _dt(date: str, time: str) -> datetime | None:
    if not date:
        return None
    try:
        if time:
            return datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        return datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return None


def _trip_window(trip: dict[str, Any], buffer_hours: float) -> tuple[datetime, datetime] | None:
    try:
        start = datetime.fromisoformat(trip["start"])
        end = datetime.fromisoformat(trip["end"] or trip["start"])
    except (ValueError, KeyError, TypeError):
        return None
    if end < start:
        start, end = end, start
    delta = timedelta(hours=buffer_hours)
    return start - delta, end + delta


def _norm_plate(plate: str) -> str:
    return (plate or "").upper().replace(" ", "").replace("-", "")


# Characters OCR routinely swaps on plates.
CONFUSABLES = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1",
                             "Z": "2", "S": "5", "B": "8", "G": "6", "T": "7"})


def _fuzzy_plate(plate: str) -> str:
    return _norm_plate(plate).translate(CONFUSABLES)


def _plates_agree(toll_plate: str, trip_plate: str, mode: str) -> bool:
    """Modes: strict (equal), fuzzy (equal after OCR folding), no_conflict (either side unknown)."""
    if not toll_plate or not trip_plate:
        return True
    if toll_plate == trip_plate:
        return True
    return mode in {"fuzzy", "no_conflict"} and _fuzzy_plate(toll_plate) == _fuzzy_plate(trip_plate)


def _candidates(
    toll: dict[str, Any],
    trips: list[dict[str, Any]],
    stamp: datetime | None,
    plate: str,
    buffer_hours: float,
    date_only: bool = False,
    plate_mode: str = "strict",
) -> list[tuple[int, dict[str, Any]]]:
    if stamp is None:
        return []
    found: list[tuple[int, dict[str, Any]]] = []
    for trip in trips:
        window = _trip_window(trip, 0.0 if date_only else buffer_hours)
        if window is None:
            continue
        trip_plate = _norm_plate(trip.get("plate", ""))
        if not _plates_agree(plate, trip_plate, plate_mode):
            continue
        start, end = window
        if date_only or not toll.get("time"):
            # Inclusive of both the pickup day and the return day.
            if not (start.date() <= stamp.date() <= end.date()):
                continue
        elif not (start <= stamp <= end):
            continue
        # Rank: plate-confirmed and time-confirmed beat date-only guesses.
        score = 0
        if plate and trip_plate and plate == trip_plate:
            score += 2
        if toll.get("time") and not date_only:
            score += 1
        found.append((score, trip))
    return found


def match(
    tolls: list[dict[str, Any]],
    trips: list[dict[str, Any]],
    buffer_hours: float = 2.0,
    markup_pct: float = 0.0,
    fee_per_toll: float = 0.0,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    for toll in tolls:
        if toll.get("ignored"):
            continue
        amount = float(toll.get("amount") or 0)
        stamp = _dt(toll.get("date", ""), toll.get("time", ""))
        plate = _norm_plate(toll.get("plate", ""))

        # Progressively looser passes: exact plate + time window, then calendar-date
        # overlap, then OCR-tolerant plate comparison, then date alone.
        candidates: list[tuple[int, dict[str, Any]]] = []
        basis = "none"
        for date_only, plate_mode, label in (
            (False, "strict", "plate + time"),
            (True, "strict", "plate + trip dates"),
            (True, "fuzzy", "similar plate + trip dates"),
            (True, "no_conflict", "trip dates only"),
        ):
            candidates = _candidates(
                toll, trips, stamp, plate, buffer_hours,
                date_only=date_only, plate_mode=plate_mode,
            )
            if candidates:
                basis = label
                break

        charge = round(amount * (1 + markup_pct / 100) + fee_per_toll, 2)
        if not candidates:
            results.append({**toll, "status": "unmatched", "trip": None, "charge": 0.0})
            continue

        best_score = max(score for score, _ in candidates)
        best = [trip for score, trip in candidates if score == best_score]
        # Without agreeing plates on both sides the trip is a guess worth eyeballing.
        best_plate = _norm_plate(best[0].get("plate", ""))
        unverified_plate = not (plate and best_plate and _plates_agree(plate, best_plate, "fuzzy"))
        results.append(
            {
                **toll,
                "basis": basis,
                "status": "matched" if len(best) == 1 and not unverified_plate else "ambiguous",
                "trip": best[0],
                "alternatives": [t["trip_id"] for t in best[1:]],
                "charge": charge,
            }
        )

    by_trip: dict[str, dict[str, Any]] = {}
    for row in results:
        if row["status"] == "unmatched":
            continue
        trip = row["trip"]
        bucket = by_trip.setdefault(
            trip["trip_id"],
            {
                "trip_id": trip["trip_id"],
                "guest": trip["guest"],
                "vehicle": trip["vehicle"],
                "plate": trip["plate"],
                "start": trip["start"],
                "end": trip["end"],
                "toll_count": 0,
                "toll_total": 0.0,
                "charge_total": 0.0,
                "already_charged": round(float(trip.get("already_charged") or 0), 2),
                "tolls": [],
            },
        )
        bucket["toll_count"] += 1
        bucket["toll_total"] = round(bucket["toll_total"] + float(row["amount"]), 2)
        bucket["charge_total"] = round(bucket["charge_total"] + float(row["charge"]), 2)
        bucket["tolls"].append(
            {
                "date": row["date"],
                "time": row.get("time", ""),
                "plate": row.get("plate", ""),
                "location": row.get("location", ""),
                "amount": row["amount"],
                "charge": row["charge"],
            }
        )

    # Trips Turo already collected on stay listed even with no toll of their own,
    # so the amounts Turo charged can still be checked.
    for trip in trips:
        already = round(float(trip.get("already_charged") or 0), 2)
        if already <= 0 or trip["trip_id"] in by_trip:
            continue
        by_trip[trip["trip_id"]] = {
            "trip_id": trip["trip_id"],
            "guest": trip["guest"],
            "vehicle": trip["vehicle"],
            "plate": trip["plate"],
            "start": trip["start"],
            "end": trip["end"],
            "toll_count": 0,
            "toll_total": 0.0,
            "charge_total": 0.0,
            "already_charged": already,
            "tolls": [],
        }

    # Turo may already have billed the guest for tolls; only the shortfall is owed.
    for bucket in by_trip.values():
        bucket["gross_charge"] = bucket["charge_total"]
        bucket["charge_total"] = round(
            max(0.0, bucket["gross_charge"] - bucket["already_charged"]), 2
        )

    unmatched = [r for r in results if r["status"] == "unmatched"]
    ambiguous = [r for r in results if r["status"] == "ambiguous"]
    return {
        "rows": results,
        "trips": sorted(by_trip.values(), key=lambda t: t["start"]),
        "summary": {
            "toll_count": len(results),
            "matched": len(results) - len(unmatched) - len(ambiguous),
            "unmatched": len(unmatched),
            "ambiguous": len(ambiguous),
            "toll_total": round(sum(float(r["amount"]) for r in results), 2),
            "charge_total": round(sum(t["charge_total"] for t in by_trip.values()), 2),
            "unmatched_total": round(sum(float(r["amount"]) for r in unmatched), 2),
        },
    }
