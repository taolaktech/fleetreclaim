"""Extraction of toll line items and trips from user-supplied files."""

import io
import re
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import pdfplumber
import pytesseract
from PIL import Image

MONEY = re.compile(r"\$?\s?(\d{1,3}(?:,\d{3})*\.\d{2})")
# Marketplace vehicle labels embed the plate, e.g. "Taofeek's Nissan (TX #VCG6008)".
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

# Account activity that is not a toll: tag purchases, card auto-replenishments, fees.
EXCLUDED_LINE = re.compile(
    r"REBILL\s+TAG\s+STORE|AUTO\s?CHARGE|AUTO\s?REPLENISH|SUPPORT\s+SERVICES\W+\s*SYSTEM",
    re.IGNORECASE,
)

# Words that look like plates but are column headers / agency names.
PLATE_STOPWORDS = {
    "TOTAL", "AMOUNT", "INVOICE", "ACCOUNT", "BALANCE", "STATEMENT", "LICENSE",
    "PLATE", "TOLLS", "DUE", "PAGE", "NUMBER", "VEHICLE", "CLASS", "AXLES",
}


PDF_RENDER_DPI = 150


@dataclass
class PageLine:
    """One visual line of a bill plus where it sits on the rendered page."""

    text: str
    page: int
    box: tuple[int, int, int, int] | None   # left, top, right, bottom in page-image pixels


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
    page: int = -1     # page of the rendered bill this line came from, -1 if unknown
    box: list[int] | None = None

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
    already_charged: float = 0.0   # the marketplace's own "Tolls & tickets" charge

    def dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- text


def _ocr_line_boxes(image: Image.Image, page: int) -> list[PageLine]:
    """OCR one page image into visual lines with their pixel bounding boxes."""
    if image.mode != "RGB":
        image = image.convert("RGB")
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    words = [
        {
            "text": data["text"][i].strip(),
            "left": data["left"][i],
            "top": data["top"][i],
            "right": data["left"][i] + data["width"][i],
            "bottom": data["top"][i] + data["height"][i],
            "mid": data["top"][i] + data["height"][i] / 2,
            "h": data["height"][i],
        }
        for i in range(len(data["text"]))
        if data["text"][i].strip()
    ]
    return _group_words(words, page)


def _group_words(words: list[dict[str, Any]], page: int) -> list[PageLine]:
    """Group word boxes into lines by vertical centre."""
    if not words:
        return []
    heights = sorted(word["h"] for word in words)
    tolerance = max(6.0, heights[len(heights) // 2] * 0.6)
    rows: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda w: w["mid"]):
        if rows and abs(word["mid"] - rows[-1][0]["mid"]) <= tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])
    lines: list[PageLine] = []
    for row in rows:
        ordered = sorted(row, key=lambda w: w["left"])
        lines.append(
            PageLine(
                text=" ".join(w["text"] for w in ordered),
                page=page,
                box=(
                    int(min(w["left"] for w in row)),
                    int(min(w["top"] for w in row)),
                    int(max(w["right"] for w in row)),
                    int(max(w["bottom"] for w in row)),
                ),
            )
        )
    return lines


def _render_pdf_pages(data: bytes) -> list[Image.Image]:
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "in.pdf"
        pdf_path.write_bytes(data)
        subprocess.run(
            ["pdftoppm", "-r", str(PDF_RENDER_DPI), "-png", str(pdf_path), str(Path(tmp) / "page")],
            check=True,
            capture_output=True,
        )
        return [Image.open(p).copy() for p in sorted(Path(tmp).glob("page*.png"))]


def _pdf_text_lines(data: bytes) -> tuple[list[PageLine], list[list[list[str]]]]:
    """Lines from a PDF's text layer, with boxes scaled to the rendered page image."""
    scale = PDF_RENDER_DPI / 72
    lines: list[PageLine] = []
    tables: list[list[list[str]]] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for index, page in enumerate(pdf.pages):
            words = [
                {
                    "text": word["text"],
                    "left": word["x0"] * scale,
                    "top": word["top"] * scale,
                    "right": word["x1"] * scale,
                    "bottom": word["bottom"] * scale,
                    "mid": (word["top"] + word["bottom"]) / 2 * scale,
                    "h": (word["bottom"] - word["top"]) * scale,
                }
                for word in page.extract_words()
            ]
            lines.extend(_group_words(words, index))
            for table in page.extract_tables():
                tables.append([[(cell or "").strip() for cell in row] for row in table])
    return lines, tables


def _prepare_image(image: Image.Image) -> Image.Image:
    """Upscale small scans so OCR (and the crops taken from them) stay legible."""
    if image.mode != "RGB":
        image = image.convert("RGB")
    if max(image.size) < 1600:
        scale = 1600 / max(image.size)
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    return image


@dataclass
class Document:
    """A toll bill reduced to lines, the page images those lines sit on, and raw text."""

    lines: list[PageLine]
    pages: list[Image.Image]
    text: str
    tables: list[list[list[str]]]


def extract_document(filename: str, data: bytes) -> Document:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        lines, tables = _pdf_text_lines(data)
        pages = [_prepare_image(page) for page in _render_pdf_pages(data)]
        if len("".join(line.text for line in lines).strip()) < 40:   # scanned bill
            lines = [line for i, page in enumerate(pages) for line in _ocr_line_boxes(page, i)]
        return Document(lines, pages, "\n".join(line.text for line in lines), tables)
    if suffix in {".txt", ".csv"}:
        text = data.decode("utf-8", errors="replace")
        return Document([PageLine(line, -1, None) for line in text.splitlines()], [], text, [])
    page_image = _prepare_image(Image.open(io.BytesIO(data)))
    lines = _ocr_line_boxes(page_image, 0)
    return Document(lines, [page_image], "\n".join(line.text for line in lines), [])


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


def is_excluded(line: str, extra_phrases: Sequence[str] = ()) -> bool:
    """True for statement lines that are account activity rather than tolls."""
    if EXCLUDED_LINE.search(line):
        return True
    squashed = re.sub(r"\s+", " ", line).upper()
    return any(
        re.sub(r"\s+", " ", phrase).strip().upper() in squashed
        for phrase in extra_phrases
        if phrase.strip()
    )


def _line_to_toll(
    page_line: PageLine,
    row_id: int,
    source: str,
    known_plates: set[str],
    extra_exclusions: Sequence[str] = (),
) -> Toll | None:
    line = page_line.text
    amounts = MONEY.findall(line)
    date = _parse_date(line)
    if not amounts or not date or is_excluded(line, extra_exclusions):
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
        page=page_line.page,
        box=list(page_line.box) if page_line.box else None,
    )


def _with_wrapped_rows(lines: list[PageLine]) -> list[PageLine]:
    """Grow each dated line's box over the continuation lines that wrap under it.

    Bills wrap long descriptions onto following lines; without this a crop of the
    row cuts that text in half.
    """
    grown: list[PageLine] = []
    for index, line in enumerate(lines):
        if line.box is None or not _parse_date(line.text):
            grown.append(line)
            continue
        bottom = line.box[3]
        for follower in lines[index + 1:]:
            if (
                follower.box is None
                or follower.page != line.page
                or _parse_date(follower.text)
                or follower.box[1] - bottom > (line.box[3] - line.box[1])
            ):
                break
            bottom = follower.box[3]
        grown.append(
            PageLine(line.text, line.page, (line.box[0], line.box[1], line.box[2], bottom))
        )
    return grown


def parse_tolls(
    filename: str,
    data: bytes,
    known_plates: set[str],
    extra_exclusions: Sequence[str] = (),
) -> tuple[list[Toll], Document]:
    document = extract_document(filename, data)
    lines = _with_wrapped_rows([line for line in document.lines if line.text.strip()])
    for table in document.tables:
        for row in table:
            joined = " ".join(cell for cell in row if cell)
            if joined.strip():
                lines.append(PageLine(joined, -1, None))

    tolls: list[Toll] = []
    seen: set[tuple[str, str, float, str]] = set()
    for line in lines:
        toll = _line_to_toll(line, len(tolls), filename, known_plates, extra_exclusions)
        if toll is None:
            continue
        key = (toll.date, toll.time, toll.amount, toll.plate)
        if key in seen:
            continue
        seen.add(key)
        toll.row_id = len(tolls)
        tolls.append(toll)
    return tolls, document


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


def _to_amount(value: Any) -> float:
    """Read a money cell such as "$12.50", "(3.00)" or 12.5 as a positive float."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return abs(round(float(value), 2))
    text = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return abs(round(float(text), 2))
    except ValueError:
        return 0.0


def _clean_plate(value: str) -> str:
    plate = value.strip().upper().replace(" ", "").replace("-", "")
    return "" if plate in {"NAN", "NONE"} else plate


def _plate_from_vehicle(vehicle: str) -> str:
    """Pull the plate out of a vehicle label such as "Nissan (TX #VCG6008)"."""
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
    charged_col = _pick(columns, ("toll", "ticket"), ("toll",))

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
                already_charged=_to_amount(row.get(charged_col)) if charged_col else 0.0,
            )
        )
    return trips
