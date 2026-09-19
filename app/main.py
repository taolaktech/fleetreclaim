"""Toll bill -> Turo trip matcher."""

import csv
import io
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .matching import match
from .parsers import parse_tolls, parse_trips

app = FastAPI(title="Toll ↔ Turo matcher")
STATIC = Path(__file__).resolve().parent.parent / "static"


class MatchRequest(BaseModel):
    tolls: list[dict[str, Any]]
    trips: list[dict[str, Any]]
    buffer_hours: float = 2.0
    markup_pct: float = 0.0
    fee_per_toll: float = 0.0


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/api/parse")
async def parse(
    toll_files: list[UploadFile] = File(...),
    trip_file: UploadFile = File(...),
) -> dict[str, Any]:
    trips = parse_trips(trip_file.filename or "trips.csv", await trip_file.read())
    known_plates = {t.plate for t in trips if t.plate}

    tolls: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    for upload in toll_files:
        name = upload.filename or "bill"
        try:
            parsed, text = parse_tolls(name, await upload.read(), known_plates)
        except Exception as exc:  # surface parse failures instead of a 500 page
            raise HTTPException(status_code=400, detail=f"Could not read {name}: {exc}") from exc
        texts[name] = text
        for toll in parsed:
            row = toll.dict()
            row["row_id"] = len(tolls)
            tolls.append(row)

    return {
        "tolls": tolls,
        "trips": [t.dict() for t in trips],
        "raw_text": texts,
        "plates": sorted(known_plates),
    }


@app.post("/api/match")
def run_match(request: MatchRequest) -> dict[str, Any]:
    return match(
        request.tolls,
        request.trips,
        buffer_hours=request.buffer_hours,
        markup_pct=request.markup_pct,
        fee_per_toll=request.fee_per_toll,
    )


@app.post("/api/export")
def export(request: MatchRequest) -> StreamingResponse:
    result = match(
        request.tolls,
        request.trips,
        buffer_hours=request.buffer_hours,
        markup_pct=request.markup_pct,
        fee_per_toll=request.fee_per_toll,
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["status", "matched_on", "toll_date", "toll_time", "plate", "location", "toll_amount",
         "charge", "trip_id", "guest", "vehicle", "trip_start", "trip_end"]
    )
    for row in result["rows"]:
        trip = row.get("trip") or {}
        writer.writerow([
            row["status"], row.get("basis", ""), row["date"], row.get("time", ""), row.get("plate", ""),
            row.get("location", ""), row["amount"], row["charge"],
            trip.get("trip_id", ""), trip.get("guest", ""), trip.get("vehicle", ""),
            trip.get("start", ""), trip.get("end", ""),
        ])
    writer.writerow([])
    writer.writerow(["TRIP TOTALS"])
    writer.writerow(["trip_id", "guest", "vehicle", "trip_start", "trip_end", "tolls", "toll_total", "charge_total"])
    for trip in result["trips"]:
        writer.writerow([
            trip["trip_id"], trip["guest"], trip["vehicle"], trip["start"], trip["end"],
            trip["toll_count"], trip["toll_total"], trip["charge_total"],
        ])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=toll-charges.csv"},
    )
