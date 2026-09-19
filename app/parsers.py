"""Extraction of toll line items and Turo trips from user-supplied files."""

import io
import re
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pdfplumber
import pytesseract
from PIL import Image

MONEY = re.compile(r"\$?\s?(\d{1,3}(?:,\d{3})*\.\d{2})")
# Turo vehicle labels embed the plate, e.g. "Taofeek's Nissan (TX #VCG6008)".
EMBEDDED_PLATE = re.compile(r"#\s*([A-Z0-9][A-Z0-9 -]{2,10})", re.IGNORECASE)
PLATE = re.compile(r"\b[A-Z0-9][A-Z0-9-]{4,8}\b")
DATE_PATTERNS = [
    (re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b"), ["%m/%d/%Y", "%m/%d/%y"]),
    (re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"), ["%Y-%m-%d"]),
    (
        re.compile(r"\b([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})\b"),
        ["%b %d %Y", "%B %d %Y", "%b. %d %Y"],
    ),
]
TIME = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\s*([AaPp]\.?[Mm]\.?)?\b")

# Words that look like plates but are column headers / agency names.
PLATE_STOPWORDS = {
    "TOTAL", "AMOUNT", "INVOICE", "ACCOUNT", "BALANCE", "STATEMENT", "LICENSE",
    "PLATE", "TOLLS", "DUE", "PAGE", "NUMBER", "VEHICLE", "CLASS", "AXLES",
}


@dataclass
class Toll:
    row_id: int
    date: str          # ISO yyyy-mm-dd
    time: str          # HH:MM (24h) or ""
    plate: str
    amount: float
    location: str
    source: str
    raw: str

    def dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Trip:
    row_id: int
    trip_id: str
    guest: str
    vehicle: str
    plate: str
    start: str         # ISO yyyy-mm-ddTHH:MM or yyyy-mm-dd
    end: str
    raw: dict[str, Any]

    def dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- text


def _ocr_lines(image: Image.Image) -> str:
    """OCR an image and rebuild visual rows from word boxes.

    Tesseract's block ordering reads tabular bills column by column, which
    separates a toll's date from its plate and amount. Grouping words by their
    vertical centre restores one line per toll.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    if max(image.size) < 1600:
        scale = 1600 / max(image.size)
        image = image.resize((int(image.width * scale), int(image.height * scale)))

    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    words = [
        {
            "text": data["text"][i].strip(),
            "x": data["left"][i],
            "y": data["top"][i] + data["height"][i] / 2,
            "h": data["height"][i],
        }
        for i in range(len(data["text"]))
        if data["text"][i].strip() and int(data["conf"][i]) >= 0
    ]
    if not words:
        return ""

    heights = sorted(word["h"] for word in words)
    tolerance = max(6.0, heights[len(heights) // 2] * 0.6)

    rows: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda w: w["y"]):
        if rows and abs(word["y"] - rows[-1][0]["y"]) <= tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])
    return "\n".join(
        " ".join(w["text"] for w in sorted(row, key=lambda w: w["x"])) for row in rows
    )


def _ocr_image(data: bytes) -> str:
    return _ocr_lines(Image.open(io.BytesIO(data)))


def _ocr_pdf(data: bytes) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "in.pdf"
        pdf_path.write_bytes(data)
        subprocess.run(
            ["pdftoppm", "-r", "200", "-png", str(pdf_path), str(Path(tmp) / "page")],
            check=True,
            capture_output=True,
        )
        pages = sorted(Path(tmp).glob("page*.png"))
        return "\n".join(_ocr_lines(Image.open(p)) for p in pages)


def extract_text(filename: str, data: bytes) -> tuple[str, list[list[list[str]]]]:
    """Return (text, tables) for a toll document."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        text_parts: list[str] = []
        tables: list[list[list[str]]] = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
                for table in page.extract_tables():
                    tables.append([[(cell or "").strip() for cell in row] for row in table])
        text = "\n".join(text_parts)
        if len(text.strip()) < 40:
            text = _ocr_pdf(data)
        return text, tables
    if suffix in {".txt", ".csv"}:
        return data.decode("utf-8", errors="replace"), []
    return _ocr_image(data), []


# --------------------------------------------------------------------------- tolls


def _parse_date(text: str) -> str:
    for pattern, formats in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1).replace(",", "")
        for fmt in formats:
            try:
                parsed = datetime.strptime(raw, fmt)
            except ValueError:
                continue
            if parsed.year < 100:
                parsed = parsed.replace(year=parsed.year + 2000)
            return parsed.strftime("%Y-%m-%d")
    return ""


def _parse_time(text: str) -> str:
    match = TIME.search(text)
    if not match:
        return ""
    hour_minute = match.group(1)
    meridiem = (match.group(2) or "").replace(".", "").upper()
    try:
        parts = [int(p) for p in hour_minute.split(":")]
    except ValueError:
        return ""
    hour, minute = parts[0], parts[1]
    if meridiem == "PM" and hour != 12:
        hour += 12
    if meridiem == "AM" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def _parse_plate(text: str, known_plates: set[str]) -> str:
    upper = text.upper()
    for plate in known_plates:
        if plate and plate in upper.replace(" ", "").replace("-", ""):
            return plate
    for candidate in PLATE.findall(upper):
        cleaned = candidate.replace("-", "")
        if cleaned in PLATE_STOPWORDS or cleaned.isdigit() or cleaned.isalpha():
            continue
        if _parse_date(candidate) or ":" in candidate:
            continue
        return cleaned
    return ""


def _line_to_toll(line: str, row_id: int, source: str, known_plates: set[str]) -> Toll | None:
    amounts = MONEY.findall(line)
    date = _parse_date(line)
    if not amounts or not date:
        return None
    amount = float(amounts[-1].replace(",", ""))
    if amount <= 0 or amount > 200:
        return None
    location = MONEY.sub("", line)
    for pattern, _ in DATE_PATTERNS:
        location = pattern.sub("", location)
    location = TIME.sub("", location).strip(" -|\t")
    plate = _parse_plate(line, known_plates)
    if plate:
        location = re.sub(re.escape(plate), "", location, flags=re.IGNORECASE)
    return Toll(
        row_id=row_id,
        date=date,
        time=_parse_time(line),
        plate=plate,
        amount=amount,
        location=re.sub(r"\s{2,}", " ", location).strip(" -|\t")[:80],
        source=source,
        raw=line.strip(),
    )


def parse_tolls(filename: str, data: bytes, known_plates: set[str]) -> tuple[list[Toll], str]:
    text, tables = extract_text(filename, data)
    lines: list[str] = []
    for table in tables:
        for row in table:
            joined = " ".join(cell for cell in row if cell)
            if joined.strip():
                lines.append(joined)
    lines.extend(line for line in text.splitlines() if line.strip())

    tolls: list[Toll] = []
    seen: set[tuple[str, str, float, str]] = set()
    for line in lines:
        toll = _line_to_toll(line, len(tolls), filename, known_plates)
        if toll is None:
            continue
        key = (toll.date, toll.time, toll.amount, toll.plate)
        if key in seen:
            continue
        seen.add(key)
        toll.row_id = len(tolls)
        tolls.append(toll)
    return tolls, text


# --------------------------------------------------------------------------- trips


def _read_table(filename: str, data: bytes) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        return pd.read_excel(io.BytesIO(data))
    for sep in [None, ",", ";", "\t"]:
        try:
            frame = pd.read_csv(io.BytesIO(data), sep=sep, engine="python")
        except Exception:
            continue
        if frame.shape[1] > 1:
            return frame
    return pd.read_csv(io.BytesIO(data))


def _pick(columns: list[str], *keyword_groups: tuple[str, ...]) -> str | None:
    lowered = {column: column.lower() for column in columns}
    for keywords in keyword_groups:
        for column, low in lowered.items():
            if all(keyword in low for keyword in keywords):
                return column
    return None


def _to_iso(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return ""
    if parsed.hour or parsed.minute:
        return parsed.strftime("%Y-%m-%dT%H:%M")
    return parsed.strftime("%Y-%m-%dT00:00")


def _clean_plate(value: str) -> str:
    plate = value.strip().upper().replace(" ", "").replace("-", "")
    return "" if plate in {"NAN", "NONE"} else plate


def _plate_from_vehicle(vehicle: str) -> str:
    """Pull the plate out of a Turo vehicle label such as "Nissan (TX #VCG6008)"."""
    match = EMBEDDED_PLATE.search(vehicle)
    if match:
        return _clean_plate(match.group(1).split(")")[0])
    inside = re.findall(r"\(([^)]*)\)", vehicle)
    for group in reversed(inside):
        candidate = _clean_plate(group.split()[-1]) if group.split() else ""
        if len(candidate) >= 5 and any(char.isdigit() for char in candidate):
            return candidate
    return ""


def parse_trips(filename: str, data: bytes) -> list[Trip]:
    frame = _read_table(filename, data)
    frame.columns = [str(column).strip() for column in frame.columns]
    columns = list(frame.columns)

    start_col = _pick(columns, ("trip", "start"), ("start",), ("pick", "up"), ("from",))
    end_col = _pick(columns, ("trip", "end"), ("end",), ("drop", "off"), ("return",), ("to",))
    plate_col = _pick(columns, ("plate",), ("license",), ("registration",))
    vehicle_col = _pick(columns, ("vehicle",), ("car",), ("model",), ("listing",))
    guest_col = _pick(columns, ("guest",), ("renter",), ("customer",), ("driver",))
    id_col = _pick(columns, ("reservation",), ("trip", "id"), ("confirmation",))

    trips: list[Trip] = []
    for index, row in frame.iterrows():
        start = _to_iso(row.get(start_col)) if start_col else ""
        end = _to_iso(row.get(end_col)) if end_col else ""
        if not start and not end:
            continue
        vehicle = str(row.get(vehicle_col) or "").strip()
        plate = _clean_plate(str(row.get(plate_col) or "")) if plate_col else ""
        if not plate:
            plate = _plate_from_vehicle(vehicle)
        trips.append(
            Trip(
                row_id=len(trips),
                trip_id=str(row.get(id_col) or f"row-{index + 2}").strip(),
                guest=str(row.get(guest_col) or "").strip(),
                vehicle=vehicle,
                plate=plate,
                start=start,
                end=end or start,
                raw={k: ("" if pd.isna(v) else str(v)) for k, v in row.items()},
            )
        )
    return trips
