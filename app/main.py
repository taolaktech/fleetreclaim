"""Toll bill -> Turo trip matcher."""

import csv
import io
import mimetypes
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from .matching import match
from .parsers import parse_tolls, parse_trips

app = FastAPI(title="Toll ↔ Turo matcher")
STATIC = Path(__file__).resolve().parent.parent / "static"

# Bills exactly as uploaded, so a trip's evidence can be viewed in the browser.
# Keyed by parse id -> filename -> file bytes.
UPLOADS: OrderedDict[str, dict[str, bytes]] = OrderedDict()
# PDFs and scans rendered to page images, so every browser can display them.
PAGES: OrderedDict[str, dict[str, list[Image.Image]]] = OrderedDict()
UPLOAD_CACHE_SIZE = 5


class CropItem(BaseModel):
    source: str
    page: int
    box: list[int]


class CropRequest(BaseModel):
    parse_id: str
    items: list[CropItem] = Field(default_factory=list)


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
    parse_id = uuid.uuid4().hex
    uploads: dict[str, bytes] = {}
    pages: dict[str, list[Image.Image]] = {}
    for upload in toll_files:
        name = upload.filename or "bill"
        data = await upload.read()
        try:
            parsed, document = parse_tolls(name, data, known_plates)
        except Exception as exc:  # surface parse failures instead of a 500 page
            raise HTTPException(status_code=400, detail=f"Could not read {name}: {exc}") from exc
        texts[name] = document.text
        uploads[name] = data
        pages[name] = document.pages
        for toll in parsed:
            row = toll.dict()
            row["row_id"] = len(tolls)
            row["parse_id"] = parse_id
            tolls.append(row)

    UPLOADS[parse_id] = uploads
    PAGES[parse_id] = pages
    while len(UPLOADS) > UPLOAD_CACHE_SIZE:
        UPLOADS.popitem(last=False)
        PAGES.popitem(last=False)

    return {
        "parse_id": parse_id,
        "tolls": tolls,
        "trips": [t.dict() for t in trips],
        "raw_text": texts,
        "plates": sorted(known_plates),
        "page_counts": {name: len(images) for name, images in pages.items()},
    }


@app.get("/api/page")
def bill_page(parse_id: str, source: str, page: int) -> Response:
    """One page of an uploaded bill, rendered to PNG so any browser can show it."""
    images = (PAGES.get(parse_id) or {}).get(source) or []
    if not 0 <= page < len(images):
        raise HTTPException(status_code=404, detail="Re-run Parse files to view the bill.")
    buffer = io.BytesIO()
    images[page].save(buffer, format="PNG")
    return Response(content=buffer.getvalue(), media_type="image/png")


@app.get("/api/source")
def bill_file(parse_id: str, source: str) -> Response:
    """A bill exactly as uploaded, so the browser can display the original document."""
    data = (UPLOADS.get(parse_id) or {}).get(source)
    if data is None:
        raise HTTPException(status_code=404, detail="Re-run Parse files to view the bill.")
    media_type = mimetypes.guess_type(source)[0] or "application/octet-stream"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{Path(source).name}"'},
    )


@app.post("/api/crop")
def crop(request: CropRequest) -> Response:
    """Stack this trip's bill rows into one screenshot-ready PNG."""
    pages = PAGES.get(request.parse_id)
    if pages is None:
        raise HTTPException(status_code=404, detail="Re-run Parse files to rebuild bill images.")

    pad = 6
    crops: list[Image.Image] = []
    for item in request.items:
        page_images = pages.get(item.source) or []
        if not 0 <= item.page < len(page_images) or len(item.box) != 4:
            continue
        page_image = page_images[item.page]
        top, bottom = item.box[1], item.box[3]
        crops.append(
            page_image.crop(
                (0, max(0, top - pad), page_image.width, min(page_image.height, bottom + pad))
            )
        )
    if not crops:
        raise HTTPException(status_code=404, detail="No bill rows available for this trip.")

    gap, margin = 10, 16
    width = max(crop_image.width for crop_image in crops) + margin * 2
    height = sum(c.height for c in crops) + gap * (len(crops) - 1) + margin * 2

    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    y = margin
    for index, crop_image in enumerate(crops):
        sheet.paste(crop_image, (margin, y))
        y += crop_image.height
        if index < len(crops) - 1:
            draw.line([(margin, y + gap // 2), (width - margin, y + gap // 2)], fill="#dddddd")
            y += gap

    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG")
    return Response(content=buffer.getvalue(), media_type="image/png")


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
