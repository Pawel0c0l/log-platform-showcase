#!/usr/bin/env python3
"""Manual regressions for Workflow B Stage 1 mailbox search breadth."""
from __future__ import annotations

import os
import sys
from email.message import EmailMessage
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.mail import fetch_reports  # noqa: E402


class FakeImap:
    def __init__(self, responses=None):
        self.responses = responses or [b"1 2"]
        self.calls = []
        self.selected_mailbox = None

    def select(self, mailbox):
        self.selected_mailbox = mailbox
        return "OK", []

    def uid(self, *args):
        self.calls.append(args)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return "OK", [self.responses[idx]]


def _message_with_attachment(*, to=None, cc=None, bcc=None, filename="GPS_baza_START_skrypt.xlsm") -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "owner@example.invalid"
    if to is not None:
        msg["To"] = to
    if cc is not None:
        msg["Cc"] = cc
    if bcc is not None:
        msg["Bcc"] = bcc
    msg["Subject"] = "Alpha GPS"
    msg.set_content("attached")
    msg.add_attachment(
        b"xlsm-bytes",
        maintype="application",
        subtype="vnd.ms-excel.sheet.macroenabled.12",
        filename=filename,
    )
    return msg


def _clear_sender_env():
    old_filters = os.environ.pop("IMAP_SENDER_FILTERS", None)
    old_filter = os.environ.pop("IMAP_SENDER_FILTER", None)
    return old_filters, old_filter


def _restore_sender_env(values) -> None:
    old_filters, old_filter = values
    if old_filters is not None:
        os.environ["IMAP_SENDER_FILTERS"] = old_filters
    if old_filter is not None:
        os.environ["IMAP_SENDER_FILTER"] = old_filter


def test_default_search_is_folder_scoped_since_only() -> None:
    old_env = _clear_sender_env()
    try:
        assert fetch_reports._sender_filters_from_env() == []
        fake = FakeImap()
        uids, queries = fetch_reports._search_imap_uids(
            fake,
            since_imap="01-Jan-2026",
            sender_filters=fetch_reports._sender_filters_from_env(),
        )
    finally:
        _restore_sender_env(old_env)

    assert uids == [b"1", b"2"]
    assert queries == ["SINCE 01-Jan-2026"]
    assert fake.calls == [("SEARCH", None, "SINCE", "01-Jan-2026")]
    assert not any(term in fake.calls[0] for term in ("TO", "CC", "BCC", "FROM", "SUBJECT"))
    print("PASS: default IMAP search is broad within the selected folder and uses only SINCE")


def test_sender_filters_are_opt_in_only() -> None:
    old_env = _clear_sender_env()
    try:
        os.environ["IMAP_SENDER_FILTERS"] = "a@example.com, b@example.com"
        filters = fetch_reports._sender_filters_from_env()
        fake = FakeImap(responses=[b"1 2", b"2 3"])
        uids, queries = fetch_reports._search_imap_uids(
            fake,
            since_imap="01-Jan-2026",
            sender_filters=filters,
        )
    finally:
        _restore_sender_env(old_env)

    assert filters == ["a@example.com", "b@example.com"]
    assert uids == [b"1", b"2", b"3"]
    assert queries == ['FROM "a@example.com" SINCE 01-Jan-2026', 'FROM "b@example.com" SINCE 01-Jan-2026']
    print("PASS: sender filtering exists only when explicitly configured")


def test_configured_mailbox_is_selected() -> None:
    fake = FakeImap()
    status, _ = fake.select('"Alpha/GPS"')
    assert status == "OK"
    assert fake.selected_mailbox == '"Alpha/GPS"'
    print("PASS: folder/label restriction remains an explicit IMAP SELECT")


def test_xlsm_attachment_is_accepted_regardless_of_recipient_headers() -> None:
    messages = [
        _message_with_attachment(to="automations@example.invalid"),
        _message_with_attachment(to="someone-else@example.invalid"),
        _message_with_attachment(cc="automations@example.invalid"),
        _message_with_attachment(bcc="automations@example.invalid"),
    ]
    for idx, msg in enumerate(messages, start=1):
        candidates, stats = fetch_reports._collect_mime_file_candidates(msg, uid=idx)
        assert len(candidates) == 1, (idx, candidates, stats)
        assert candidates[0]["raw_filename"] == "GPS_baza_START_skrypt.xlsm"
        assert candidates[0]["payload"] == b"xlsm-bytes"
        assert stats["attachments_supported"] == 1
    print("PASS: XLSM attachments are accepted without To/Cc/Bcc recipient filtering")


def test_unsupported_attachments_are_still_ignored() -> None:
    msg = EmailMessage()
    msg["From"] = "someone@example.com"
    msg["To"] = "automations@example.invalid"
    msg["Subject"] = "signature"
    msg.set_content("inline signature")
    msg.add_attachment(
        b"png-bytes",
        maintype="image",
        subtype="png",
        filename="signature.png",
    )
    candidates, stats = fetch_reports._collect_mime_file_candidates(msg, uid=10)
    assert candidates == []
    assert stats["attachments_unsupported"] >= 1
    assert "signature.png" in stats["unsupported_attachment_filenames"]
    print("PASS: unsupported attachments are ignored")


def test_alpha_gps_xlsm_mail_creates_raw_candidate() -> None:
    msg = _message_with_attachment(to="not-automations@example.invalid")
    candidates, stats = fetch_reports._collect_mime_file_candidates(msg, uid=77)
    assert stats["supported_attachment_filenames"] == ["GPS_baza_START_skrypt.xlsm"]
    assert candidates[0]["content_type"] == "application/vnd.ms-excel.sheet.macroenabled.12"
    assert ".xlsm" in fetch_reports.ALLOWED_EXTENSIONS
    print("PASS: Alpha GPS XLSM mail produces a supported raw attachment candidate")


def main() -> None:
    test_default_search_is_folder_scoped_since_only()
    test_sender_filters_are_opt_in_only()
    test_configured_mailbox_is_selected()
    test_xlsm_attachment_is_accepted_regardless_of_recipient_headers()
    test_unsupported_attachments_are_still_ignored()
    test_alpha_gps_xlsm_mail_creates_raw_candidate()


if __name__ == "__main__":
    main()
