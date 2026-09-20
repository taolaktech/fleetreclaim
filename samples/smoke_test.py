"""End-to-end smoke test against a running server."""

import json
import sys
import urllib.request
from pathlib import Path

BASE = "http://localhost:8080"
HERE = Path(__file__).resolve().parent


def post_multipart(url: str, files: list[tuple[str, Path, str]]) -> dict:
    boundary = "----tollmatcher"
    body = b""
    for field, path, ctype in files:
        body += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
            f"filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n"
        ).encode()
        body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    return json.load(urllib.request.urlopen(request, timeout=300))


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    return json.load(urllib.request.urlopen(request, timeout=120))


def main() -> int:
    parsed = post_multipart(
        f"{BASE}/api/parse",
        [
            ("toll_files", HERE / "toll_bill.pdf", "application/pdf"),
            ("trip_file", HERE / "turo_trips.csv", "text/csv"),
        ],
    )
    result = post_json(
        f"{BASE}/api/match",
        {"tolls": parsed["tolls"], "trips": parsed["trips"], "buffer_hours": 2, "markup_pct": 0, "fee_per_toll": 0},
    )
    for trip in result["trips"]:
        print(trip["trip_id"], trip["guest"], trip["plate"], trip["toll_count"], trip["charge_total"])
    for row in result["rows"]:
        if row["status"] != "matched":
            print("REVIEW", row["status"], row["date"], row["plate"], row["amount"])
    print(json.dumps(result["summary"], indent=2))

    # R-1001 carries $10 in Turo's "Tolls & tickets", so only $6.35 of its $16.35 is owed.
    expected = {"R-1001": 6.35, "R-1002": 15.75, "R-1003": 4.50, "R-1004": 13.00}
    actual = {t["trip_id"]: t["charge_total"] for t in result["trips"]}
    assert actual == expected, f"expected {expected}, got {actual}"
    assert result["summary"]["unmatched"] == 1, result["summary"]
    assert result["summary"]["ambiguous"] == 1, result["summary"]
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
