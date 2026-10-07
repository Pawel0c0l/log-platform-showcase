"""Failures the Report Explorer domain raises, all safe to surface as states.

None of these ever carries a DSN, a storage key, a SQL fragment or a driver
message: `REP-004` requires the user-facing states to explain themselves without
leaking infrastructure, so the exception vocabulary is the same vocabulary the
page renders.
"""
from __future__ import annotations


class ReportExplorerError(Exception):
    """Base class. Never rendered directly."""


class ReportSchemaUnavailableError(ReportExplorerError):
    """The generated-report relations are absent from this database.

    `S15` declares migration 068 in `db/schema_requirements.json`, so a release
    cannot activate over a database without it. This exception exists for the
    development and disaster cases that bypass that gate: the module then states
    plainly that the library cannot be read, rather than rendering an empty
    library that would be indistinguishable from "this client has no reports".
    """


class ReportInstanceNotFoundError(ReportExplorerError):
    """No instance with this reference is visible to this account.

    Deliberately one error for "does not exist", "belongs to another client" and
    "malformed reference": distinguishing them would let an unauthorized caller
    probe for existence (`REP-004` — do not leak existence across an
    authorization boundary).
    """


class ReportFileNotFoundError(ReportExplorerError):
    """No member with this reference belongs to that instance."""


class ReportFilePreviewUnsupportedError(ReportExplorerError):
    """The member exists and is downloadable, but is not embeddable.

    `RP-14` makes previewability a FILE property. This is the state of a member
    the platform will not serve inline — a tabular export, or anything whose
    bytes are not the one content type `api/artifacts/preview.py` embeds — and
    it is distinct from "the file is gone", because the download still works.
    """


class ReportFileUnavailableError(ReportExplorerError):
    """The member exists but its bytes do not.

    Expired or cleaned-up files are a *state* of a report that still exists, not
    a missing report.
    """


class ReportAccessDeniedError(ReportExplorerError):
    """The account is authenticated but has no report access to this client."""

    def __init__(self, message: str = "", *, has_database_access: bool = False,
                 client_code: str = "", client_display_name: str = "") -> None:
        super().__init__(message or "report access denied")
        # `RP-19`: an account with dataset access but no report access must be
        # told the two are separate grants. That is a different state from
        # having no relationship with the client at all, so the distinction is
        # carried here rather than re-derived by the page.
        self.has_database_access = bool(has_database_access)
        self.client_code = str(client_code or "")
        self.client_display_name = str(client_display_name or "")


class ReportClientNotFoundError(ReportExplorerError):
    """The client does not exist, is inactive, or this account cannot see it."""


class ReportPublicationError(ReportExplorerError):
    """A generated report could not be published as described."""


class EmptyPublicationError(ReportPublicationError):
    """`I-7`. A successful publication must publish at least one file.

    "Succeeded with zero files" would render as `Pliki wygasły`, which would be
    untrue about a report that never produced anything.
    """


class StaleAttemptError(ReportPublicationError):
    """The attempt that tried to publish no longer holds the instance.

    A crashed or superseded generation run must not be able to publish over the
    result of a newer one (`docs/40` §3.6).
    """


class AttemptAlreadyCompletedError(ReportPublicationError):
    """This claim already reached a terminal outcome, and a different one.

    A claim authorizes exactly one completion. Replaying the SAME completion is
    idempotent and returns normally; asking a claim that already succeeded to
    fail — the reviewed "late `fail()` turns a published report into a failed
    one" — is not a replay, it is a contradiction, and it is refused without
    touching the instance.
    """

    def __init__(self, message: str = "", *, outcome: str = "") -> None:
        super().__init__(message or "this generation attempt has already completed")
        self.outcome = str(outcome or "")


class ReportFileContractError(ReportPublicationError):
    """The declared file contract and the actual file set disagree.

    `docs/40` §1.1 makes the file contract a responsibility of the DEFINITION, so
    a publication that contradicts it is not a successful generation of that
    report type, whatever its files happen to be.
    """


class ReportProvenanceError(ReportPublicationError):
    """`RP-18` provenance does not describe this report.

    A stored snapshot that names another client, another dataset, a column the
    catalogue does not expose, or a range other than the instance's own
    reporting period would be a historical record that is simply false.
    """


class ReportDefinitionNotFoundError(ReportPublicationError):
    """No report definition carries this type key."""
