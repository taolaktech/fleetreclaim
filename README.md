# Tolls/tickets ↔ Turo matcher

Upload a tolls/tickets bill (PDF or photo) plus a Turo trip export (CSV/XLSX) and get,
per trip, how much to charge the guest.

## How it works

1. **Read the bill** — text PDFs are parsed with `pdfplumber`; scanned PDFs and photos go
   through Tesseract OCR. OCR word boxes are regrouped into visual rows so a line item's
   date, plate, and amount stay on one line even in multi-column statements.
2. **Read the trips** — column names are auto-detected (trip start/end, license plate,
   guest, vehicle, reservation id) from CSV or Excel. When there is no plate column, the
   plate is read out of the vehicle label, e.g. `Taofeek's Nissan (TX #VCG6008)`.
3. **Match** — a toll belongs to a trip when the plates agree and the toll timestamp falls
   inside the trip window (± a configurable buffer, default 2h). If nothing matches on
   time, the toll falls back to calendar-date overlap with the trip. Tolls that fit several
   trips equally well are charged in full to the best-ranked one and flagged `ambiguous`;
   tolls outside every trip's dates, or whose plate contradicts every candidate trip's
   plate, stay `unmatched` and are charged to nobody. Statement lines that are not tolls
   (tag-store rebills, card auto-charges, support-services system fees) are dropped while parsing.
4. **Charge** — optional markup % and per-item admin fee are applied; anything Turo
   already collected on the trip (its "Tolls & tickets" column) is subtracted, so only the
   shortfall is owed. Totals roll up per trip and export to CSV.

Everything parsed is editable in the browser, so OCR mistakes (plate `0` vs `O`, a wrong
amount) can be fixed and matching re-runs instantly.

## Run

```bash
pip install -r requirements.txt          # plus: apt install tesseract-ocr poppler-utils
python -m uvicorn app.main:app --port 8080
# open http://localhost:8080
```

## Sample data / test

```bash
python samples/make_samples.py   # writes a fake toll bill (PDF+PNG) and Turo CSV
python samples/smoke_test.py     # end-to-end check against a running server
```
