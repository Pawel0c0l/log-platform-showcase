from __future__ import annotations

import io
import json
import stat
import tempfile
from pathlib import Path

from jobs.reports.stage2.link_reconciliation import (
    LinkClassification,
    PlanEntry,
    ReconciliationResult,
    artifact_compatible,
    canonical_payload,
    payload_digest,
    validate_object,
    write_plan,
)


RAW = {"raw_file_id": "bd7662a5-eeb4-4614-8720-d477abfcb227", "status": "NORMALIZED", "stage2_status": "OK", "stage2_report_type": "report_207", "client_code": "C001", "source_identity": "a" * 64}
ART = {"artifact_id": "b454f82c-5857-4bab-8342-b7258e5cf7de", "raw_file_id": RAW["raw_file_id"], "kind": "REPORT", "content_type": "text/csv", "size_bytes": 3, "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "storage_backend": "S3", "storage_key": "secret-key", "workflow_name": "workflow_b", "stage_name": "stage_2_clean", "artifact_role": "cleaned", "report_type": "report_207", "client_code": "C001", "layout_version": 1, "metadata_json": {}, "idempotency_scope": None, "idempotency_key": None}


class Body(io.BytesIO):
    pass


class S3:
    def __init__(self, *, data=b"abc", size=3, metadata=None, missing=False):
        self.data, self.size, self.metadata, self.missing = data, size, metadata or {}, missing
    def head_object(self, **_kwargs):
        if self.missing:
            raise RuntimeError("missing")
        return {"ContentLength": self.size, "Metadata": self.metadata}
    def get_object(self, **_kwargs):
        return {"Body": Body(self.data)}


def test_compatibility() -> None:
    assert artifact_compatible(RAW, ART)
    keyed = dict(ART, idempotency_scope="workflow_b.stage2.cleaned.v1", idempotency_key="key")
    assert artifact_compatible(RAW, keyed)
    assert artifact_compatible(dict(RAW, stage2_report_type="Report Type"), dict(ART, report_type="report_type"))
    for field, value in (("workflow_name", "wrong"), ("stage_name", "wrong"), ("artifact_role", "raw"), ("report_type", "other")):
        assert not artifact_compatible(RAW, dict(ART, **{field: value}))
    assert not artifact_compatible(RAW, dict(ART, metadata_json={"source_identity": "wrong"}))


def test_object_validation() -> None:
    assert validate_object(S3(), "bucket", ART) is None
    assert validate_object(S3(missing=True), "bucket", ART) == LinkClassification.BLOCKED_OBJECT_MISSING
    assert validate_object(S3(size=4), "bucket", ART) == LinkClassification.BLOCKED_OBJECT_SIZE_MISMATCH
    assert validate_object(S3(data=b"xyz"), "bucket", ART) == LinkClassification.BLOCKED_OBJECT_CHECKSUM_MISMATCH
    assert validate_object(S3(metadata={"sha256": ART["sha256"]}), "bucket", ART) is None


def test_plan_is_deterministic_and_protected() -> None:
    entries = [PlanEntry(RAW["raw_file_id"], ART["artifact_id"], "OK", True, LinkClassification.SAFE_TO_LINK, ART["sha256"], 3, "0123456789abcdef", True)]
    result = ReconciliationResult(True, entries, 1, 1, 1, 1)
    payload = canonical_payload(result, environment="local_dev", database_name="logdb", platform_identity_id="f6222a11-06ee-4e4f-8b25-302a9d963cfa", repository_commit="abc")
    assert payload_digest(payload) == payload_digest(json.loads(json.dumps(payload)))
    with tempfile.TemporaryDirectory() as directory:
        one, two = Path(directory) / "one.json", Path(directory) / "two.json"
        d1 = write_plan(one, payload, created_at="one")
        d2 = write_plan(two, payload, created_at="two")
        assert d1 == d2
        assert stat.S_IMODE(one.stat().st_mode) == 0o600
        text = one.read_text()
        for prohibited in ("filename", "storage_key", "customer", "subject", "email"):
            assert prohibited not in text.lower()


def test_blocked_plan_is_not_executable() -> None:
    result = ReconciliationResult(True, [PlanEntry(RAW["raw_file_id"], None, "OK", True, LinkClassification.BLOCKED_NO_ARTIFACT)])
    assert not result.execution_permitted
    assert result.blocked_count == 1


def main() -> None:
    test_compatibility()
    test_object_validation()
    test_plan_is_deterministic_and_protected()
    test_blocked_plan_is_not_executable()
    print("OK - Stage 2 cleaned-link reconciliation regressions passed")


if __name__ == "__main__":
    main()
