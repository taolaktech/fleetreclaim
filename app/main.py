"""Toll bill -> Turo trip matcher."""

import csv
import io
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel

from .matching import match
from .parsers import parse_tolls, parse_trips

app = FastAPI(title="Toll ↔ Turo matcher")
STATIC = Path(__file__).resolve().parent.parent / "static"

# Rendered bill pages from recent uploads, so a trip's rows can be cropped out of
# the original document. Keyed by parse id -> filename -> page images.
PAGES: OrderedDict[str, dict[str, list[Image.Image]]] = OrderedDict()
PAGE_CACHE_SIZE = 5
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


class CropItem(BaseModel):
    source: str
    page: int
    box: list[int]


class CropRequest(BaseModel):
    parse_id: str
    title: str = ""
    subtitle: str = ""
    items: list[CropItem]


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
    exclude: str = Form(""),
) -> dict[str, Any]:
    trips = parse_trips(trip_file.filename or "trips.csv", await trip_file.read())
    known_plates = {t.plate for t in trips if t.plate}
    exclusions = [phrase.strip() for phrase in exclude.splitlines() if phrase.strip()]

    tolls: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    parse_id = uuid.uuid4().hex
    pages: dict[str, list[Image.Image]] = {}
    for upload in toll_files:
        name = upload.filename or "bill"
        try:
            parsed, document = parse_tolls(name, await upload.read(), known_plates, exclusions)
        except Exception as exc:  # surface parse failures instead of a 500 page
            raise HTTPException(status_code=400, detail=f"Could not read {name}: {exc}") from exc
        texts[name] = document.text
        pages[name] = document.pages
        for toll in parsed:
            row = toll.dict()
            row["row_id"] = len(tolls)
            row["parse_id"] = parse_id
            tolls.append(row)

    PAGES[parse_id] = pages
    while len(PAGES) > PAGE_CACHE_SIZE:
        PAGES.popitem(last=False)

    return {
        "parse_id": parse_id,
        "tolls": tolls,
        "trips": [t.dict() for t in trips],
        "raw_text": texts,
        "plates": sorted(known_plates),
        "exclusions": exclusions,
    }


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


@app.post("/api/crop")
def crop(request: CropRequest) -> Response:
    """Stack this trip's rows from the original bill into one screenshot-ready PNG."""
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
                (
                    0,
                    max(0, top - pad),
                    page_image.width,
                    min(page_image.height, bottom + pad),
                )
            )
        )
    if not crops:
        raise HTTPException(status_code=404, detail="No bill rows available for this trip.")

    gap, margin = 10, 16
    head = 52 + (30 if request.subtitle else 0) if request.title else 0
    width = max(crop_image.width for crop_image in crops) + margin * 2
    height = (
        head
        + sum(crop_image.height for crop_image in crops)
        + gap * (len(crops) - 1)
        + margin * 2
    )

    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    if request.title:
        draw.text((margin, margin), request.title, fill="black", font=_font(30))
        if request.subtitle:
            draw.text((margin, margin + 38), request.subtitle, fill="#444444", font=_font(22))

    y = head + margin
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
