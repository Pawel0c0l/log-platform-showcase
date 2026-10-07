#!/usr/bin/env python3
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.mail.fetch_reports import _persist_raw_file_candidate, _report_key_from_filename


def _test_dedupe_duplicate_content_without_failed() -> None:
    # Case: first payload creates canonical row, second payload for same key/fingerprint
    # resolves to DUPLICATE_CONTENT with duplicate_of_id (no fallback FAILED path).
    with patch("jobs.mail.fetch_reports._insert_raw_file_new", side_effect=["raw-1", None]), patch(
        "jobs.mail.fetch_reports._select_raw_file_by_sha", side_effect=[None]
    ), patch(
        "jobs.mail.fetch_reports._select_canonical_by_key", return_value=("raw-1", "sha-aaa", "NORMALIZED")
    ), patch(
        "jobs.mail.fetch_reports._insert_raw_file_duplicate", return_value="raw-2"
    ):
        first = _persist_raw_file_candidate(
            cur=None,
            candidate=None,
            imap_message_id="msg-1",
            account="acc",
            sha256="sha-aaa",
            raw_filename="D104.1.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=123,
            raw_path="/tmp/raw-1",
            report_key="104.1",
            content_fingerprint="fp-1",
            dedup_basis="tables_70_90",
        )
        second = _persist_raw_file_candidate(
            cur=None,
            candidate=None,
            imap_message_id="msg-2",
            account="acc",
            sha256="sha-bbb",
            raw_filename="D104.1-copy.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=124,
            raw_path="/tmp/raw-2",
            report_key="104.1",
            content_fingerprint="fp-1",
            dedup_basis="tables_70_90",
        )

    assert first["action"] == "NEW", first
    assert second["action"] == "DUPLICATE_CONTENT", second
    assert second["canonical_id"] == "raw-1", second
    assert second["raw_file_id"] == "raw-2", second


def _test_report_key_parsing() -> None:
    assert _report_key_from_filename("D104.1 Raport floty.xlsx") == "d104.1 raport floty"
    assert _report_key_from_filename("Powiadomienie D104.7 (limit e-mail).csv") == "powiadomienie d104.7 (limit e-mail)"
    assert _report_key_from_filename("Raport D105.2 za okres.xls") == "raport d105.2 za okres"
    assert _report_key_from_filename("report_112_11111111-1111-1111-1111-111111111111 - 15_lut_2026.xlsx") == "report_112"


def main() -> None:
    _test_dedupe_duplicate_content_without_failed()
    print("PASS: dedupe duplicate_of_id without FAILED fallback")
    _test_report_key_parsing()
    print("PASS: report_key parsing")


if __name__ == "__main__":
    main()
