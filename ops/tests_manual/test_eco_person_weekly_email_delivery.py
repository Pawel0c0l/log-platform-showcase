#!/usr/bin/env python3
"""Manual regressions for BRAVO00016 weekly email delivery and Sent archiving.

Run:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" python3 ops/tests_manual/test_eco_person_weekly_email_delivery.py
"""
from __future__ import annotations

import os
import sys
from email.parser import BytesParser
from email import policy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving_person import email_delivery as delivery  # noqa: E402


class EnvPatch:
    def __init__(self, values: dict[str, str | None]):
        self.values = values
        self.old: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.old[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def __exit__(self, exc_type, exc, tb):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _crt_env(password: str | None = "smtp-secret") -> dict[str, str | None]:
    return {
        "BRAVO_ECO_WEEKLY_EMAIL_SMTP_HOST": "smtp.example.test",
        "BRAVO_ECO_WEEKLY_EMAIL_SMTP_PORT": "587",
        "BRAVO_ECO_WEEKLY_EMAIL_SMTP_USERNAME": "automations@example.invalid",
        "BRAVO_ECO_WEEKLY_EMAIL_SMTP_PASSWORD": password,
        "BRAVO_ECO_WEEKLY_EMAIL_SMTP_USE_TLS": "true",
        "BRAVO_ECO_WEEKLY_EMAIL_SMTP_USE_SSL": "false",
        "BRAVO_ECO_WEEKLY_EMAIL_FROM_EMAIL": "automations@example.invalid",
        "BRAVO_ECO_WEEKLY_EMAIL_FROM_NAME": "Ecodriving Telematics",
        "BRAVO_ECO_WEEKLY_EMAIL_REPLY_TO": "",
        "BRAVO_ECO_WEEKLY_EMAIL_TIMEOUT_SECONDS": "30",
        "BRAVO_ECO_WEEKLY_EMAIL_IMAP_HOST": "imap.example.test",
        "BRAVO_ECO_WEEKLY_EMAIL_IMAP_PORT": "993",
        "BRAVO_ECO_WEEKLY_EMAIL_IMAP_USERNAME": "automations@example.invalid",
        "BRAVO_ECO_WEEKLY_EMAIL_IMAP_PASSWORD": "imap-secret",
        "BRAVO_ECO_WEEKLY_EMAIL_IMAP_USE_SSL": "true",
        "BRAVO_ECO_WEEKLY_EMAIL_IMAP_TIMEOUT_SECONDS": "30",
    }


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.sendmail_calls = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def sendmail(self, envelope_sender, recipients, mime_bytes):
        self.sendmail_calls.append((envelope_sender, tuple(recipients), mime_bytes))
        return {}

    def quit(self):
        self.quit_called = True


class FailingSMTP(FakeSMTP):
    def sendmail(self, envelope_sender, recipients, mime_bytes):
        super().sendmail(envelope_sender, recipients, mime_bytes)
        raise RuntimeError("smtp down")


class FakeIMAP:
    instances = []
    existing_message_ids: set[str] = set()
    append_raises = False

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.login_args = None
        self.selected = None
        self.append_calls = []
        FakeIMAP.instances.append(self)

    def login(self, username, password):
        self.login_args = (username, password)
        return "OK", [b"logged in"]

    def list(self, reference, pattern):
        return "OK", [b"(\\HasNoChildren \\Sent) \".\" INBOX.Sent"]

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        message_id = str(criteria[-1])
        if message_id in FakeIMAP.existing_message_ids:
            return "OK", [b"1"]
        return "OK", [b""]

    def append(self, mailbox, flags, date_time, mime_bytes):
        self.append_calls.append((mailbox, flags, date_time, mime_bytes))
        if FakeIMAP.append_raises:
            raise RuntimeError("append down")
        msg = BytesParser(policy=policy.default).parsebytes(mime_bytes)
        FakeIMAP.existing_message_ids.add(str(msg["Message-ID"]))
        return "OK", [b"appended"]

    def logout(self):
        return "OK", [b"bye"]


def test_bravo00016_resolves_dedicated_env_and_identity() -> None:
    with EnvPatch(_crt_env()):
        settings = delivery.load_smtp_settings_from_env(dry_run=False, client_code="BRAVO00016")
        assert settings.env_prefix == "BRAVO_ECO_WEEKLY_EMAIL"
        assert settings.from_name == "Ecodriving Telematics"
        assert settings.from_email == "automations@example.invalid"
        assert settings.reply_to is None
        archive = delivery.load_sent_archive_settings_from_env(env_prefix=settings.env_prefix)
        assert archive.username == "automations@example.invalid"
    assert delivery.env_prefix_for_client("ALPHA00001") == "ECO_PERSON_EMAIL"
    print("PASS: BRAVO00016 resolves BRAVO_ECO_WEEKLY_EMAIL and ALPHA remains on its own prefix")


def test_missing_bravo_password_fails_with_manual_status() -> None:
    with EnvPatch(_crt_env(password="")):
        try:
            delivery.load_smtp_settings_from_env(dry_run=False, client_code="BRAVO00016")
        except RuntimeError as exc:
            assert str(exc) == "MANUAL_STEP_REQUIRED_MISSING_SMTP_SECRET"
        else:
            raise AssertionError("missing BRAVO SMTP password must fail before SMTP")
    print("PASS: missing BRAVO SMTP secret fails before connection")


def test_prepared_message_preserves_headers_and_envelope() -> None:
    with EnvPatch(_crt_env()):
        settings = delivery.load_smtp_settings_from_env(dry_run=False, client_code="BRAVO00016")
    prepared = delivery.prepare_email(
        settings=settings,
        recipient_email="owner@example.invalid",
        subject="Subject",
        html_body="<p>Hello</p>",
        text_body="Hello",
        message_id="<stable@example.test>",
    )
    assert prepared.message["From"] == "Ecodriving Telematics <automations@example.invalid>"
    assert prepared.message["To"] == "owner@example.invalid"
    assert "Reply-To" not in prepared.message
    assert prepared.envelope_sender == "automations@example.invalid"
    assert prepared.recipients == ("owner@example.invalid",)
    parsed = BytesParser(policy=policy.default).parsebytes(prepared.mime_bytes)
    assert parsed["Message-ID"] == "<stable@example.test>"
    assert parsed["Subject"] == "Subject"
    print("PASS: prepared MIME has exact From, omitted blank Reply-To, and exact envelope")


def test_smtp_success_triggers_sent_append_and_dedupe() -> None:
    FakeSMTP.instances = []
    FakeIMAP.instances = []
    FakeIMAP.existing_message_ids = set()
    FakeIMAP.append_raises = False
    with EnvPatch(_crt_env()):
        smtp = delivery.load_smtp_settings_from_env(dry_run=False, client_code="BRAVO00016")
        imap = delivery.load_sent_archive_settings_from_env(env_prefix=smtp.env_prefix)
    prepared = delivery.prepare_email(
        settings=smtp,
        recipient_email="driver@example.test",
        subject="Subject",
        html_body="<p>Hello</p>",
        text_body="Hello",
        message_id="<dedupe@example.test>",
    )
    delivery.send_prepared_email(settings=smtp, prepared=prepared, smtp_factory=FakeSMTP)
    result = delivery.archive_sent_message(
        settings=imap,
        message_id=prepared.message_id,
        mime_bytes=prepared.mime_bytes,
        imap_factory=FakeIMAP,
    )
    assert result.status == "appended"
    assert result.mailbox == "INBOX.Sent"
    assert FakeSMTP.instances[0].sendmail_calls[0][0] == "automations@example.invalid"
    assert len(FakeIMAP.instances[0].append_calls) == 1

    second = delivery.archive_sent_message(
        settings=imap,
        message_id=prepared.message_id,
        mime_bytes=prepared.mime_bytes,
        imap_factory=FakeIMAP,
    )
    assert second.status == "already_present"
    assert len(FakeIMAP.instances[-1].append_calls) == 0
    print("PASS: SMTP success appends to discovered Sent once and Message-ID dedupe prevents duplicates")


def test_smtp_failure_does_not_append_and_append_failure_does_not_resend() -> None:
    FakeSMTP.instances = []
    FakeIMAP.instances = []
    FakeIMAP.existing_message_ids = set()
    with EnvPatch(_crt_env()):
        smtp = delivery.load_smtp_settings_from_env(dry_run=False, client_code="BRAVO00016")
        imap = delivery.load_sent_archive_settings_from_env(env_prefix=smtp.env_prefix)
    prepared = delivery.prepare_email(
        settings=smtp,
        recipient_email="driver@example.test",
        subject="Subject",
        html_body="<p>Hello</p>",
        text_body="Hello",
    )
    try:
        delivery.send_prepared_email(settings=smtp, prepared=prepared, smtp_factory=FailingSMTP)
    except RuntimeError:
        pass
    else:
        raise AssertionError("SMTP failure should propagate")
    assert FakeIMAP.instances == []

    FakeSMTP.instances = []
    FakeIMAP.append_raises = True
    delivery.send_prepared_email(settings=smtp, prepared=prepared, smtp_factory=FakeSMTP)
    try:
        delivery.archive_sent_message(
            settings=imap,
            message_id=prepared.message_id,
            mime_bytes=prepared.mime_bytes,
            imap_factory=FakeIMAP,
        )
    except RuntimeError as exc:
        assert "append down" in str(exc)
    else:
        raise AssertionError("append failure should propagate for archive-only handling")
    assert len(FakeSMTP.instances[0].sendmail_calls) == 1
    print("PASS: SMTP failure does not append; append failure does not cause SMTP resend")


def main() -> None:
    test_bravo00016_resolves_dedicated_env_and_identity()
    test_missing_bravo_password_fails_with_manual_status()
    test_prepared_message_preserves_headers_and_envelope()
    test_smtp_success_triggers_sent_append_and_dedupe()
    test_smtp_failure_does_not_append_and_append_failure_does_not_resend()
    print("OK - BRAVO00016 weekly email delivery checks passed")


if __name__ == "__main__":
    main()
