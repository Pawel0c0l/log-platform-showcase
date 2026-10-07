#!/usr/bin/env python3
import csv
import io
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.mail.fetch_reports import (
    _collect_link_file_candidates,
    _extract_urls_from_email,
    _xlsx_to_canonical_csv,
)


def _test_link_collection_with_allowlist_and_mocked_http() -> None:
    msg = EmailMessage()
    msg["Subject"] = "Powiadomienie raportu (limit e-mail)"
    msg.set_content(
        "Pobierz raport: https://fleetmail.telematics-provider.example/download/report.csv\n"
        "Nie powinno przejsc: https://evil.example.org/payload.csv\n"
    )

    urls = _extract_urls_from_email(msg)
    assert len(urls) == 2, f"Expected 2 URLs, got: {urls}"

    candidates, stats, events = _collect_link_file_candidates(
        msg,
        subject=msg["Subject"],
        uid=123,
        allowset={"fleetmail.telematics-provider.example"},
    )

    assert stats["link_urls_found"] == 2, stats
    assert stats["link_urls_allowed"] == 1, stats
    assert len(candidates) == 1, candidates
    assert candidates[0]["source"] == "link", candidates[0]
    assert candidates[0]["raw_filename"].endswith(".csv"), candidates[0]
    assert candidates[0]["payload"] is None, candidates[0]
    assert any(event["message"] == "Report link blocked by allowlist" for event in events), events


def _test_xlsx_multisheet_to_single_csv() -> None:
    openpyxl = __import__("openpyxl")
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "FleetA"
    ws1.append(["vin", "distance_km"])
    ws1.append(["AAA111", 120])

    ws2 = wb.create_sheet("FleetB")
    ws2.append(["vin", "distance_km"])
    ws2.append(["BBB222", 240])

    buffer = io.BytesIO()
    wb.save(buffer)
    wb.close()
    raw_xlsx = buffer.getvalue()

    with tempfile.TemporaryDirectory(prefix="xlsx-multisheet-") as tmp:
        out_path = Path(tmp) / "canonical.csv"
        _xlsx_to_canonical_csv(raw_xlsx, out_path)

        with out_path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f, delimiter=";"))

    assert rows, "Expected non-empty CSV output"
    flattened = "\n".join(";".join(row) for row in rows)
    assert "__sheet_name" not in flattened, flattened
    assert "vin;distance_km" in flattened, flattened
    assert "AAA111;120" in flattened, flattened
    assert "BBB222;240" in flattened, flattened
    assert rows == [
        ["vin", "distance_km"],
        ["AAA111", "120"],
        [],
        ["vin", "distance_km"],
        ["BBB222", "240"],
    ], rows


def main() -> None:
    _test_link_collection_with_allowlist_and_mocked_http()
    print("PASS: link collection and allowlist")
    _test_xlsx_multisheet_to_single_csv()
    print("PASS: xlsx multisheet canonical csv")


if __name__ == "__main__":
    main()
