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

        candidates: list[tuple[int, dict[str, Any]]] = []
        for trip in trips:
            window = _trip_window(trip, buffer_hours)
            if window is None or stamp is None:
                continue
            trip_plate = _norm_plate(trip.get("plate", ""))
            if plate and trip_plate and plate != trip_plate:
                continue
            start, end = window
            if not (start <= stamp <= end):
                # Date-only tolls: accept if the calendar day overlaps the trip.
                if toll.get("time"):
                    continue
                if not (start.date() <= stamp.date() <= end.date()):
                    continue
            # Rank: plate-confirmed and time-confirmed beat date-only guesses.
            score = 0
            if plate and trip_plate and plate == trip_plate:
                score += 2
            if toll.get("time"):
                score += 1
            candidates.append((score, trip))

        charge = round(amount * (1 + markup_pct / 100) + fee_per_toll, 2)
        if not candidates:
            results.append({**toll, "status": "unmatched", "trip": None, "charge": 0.0})
            continue

        best_score = max(score for score, _ in candidates)
        best = [trip for score, trip in candidates if score == best_score]
        status = "matched" if len(best) == 1 else "ambiguous"
        results.append(
            {
                **toll,
                "status": status,
                "trip": best[0],
                "alternatives": [t["trip_id"] for t in best[1:]],
                "charge": charge if status == "matched" else 0.0,
            }
        )

    by_trip: dict[str, dict[str, Any]] = {}
    for row in results:
        if row["status"] != "matched":
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
            "charge_total": round(sum(float(r["charge"]) for r in results), 2),
            "unmatched_total": round(sum(float(r["amount"]) for r in unmatched), 2),
        },
    }
