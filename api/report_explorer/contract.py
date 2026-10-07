"""The declared file contract of a report definition, and its enforcement.

`docs/40` §1.1 makes the file contract a RESPONSIBILITY of the definition —
*"the declared members a successful generation produces: format, semantic role,
which one is the main file"* — and §7 makes each of those facts required on the
member. Migration `068` stored the declaration as bounded JSON and nothing read
it, so a definition could declare `PDF` + `XLSX` and a publication could succeed
with a single CSV. The declaration was documentation, not a contract.

This module makes it one. It is deliberately FINITE: a fixed set of keys, a
fixed vocabulary and a cardinality rule. It is not a workflow engine, it
introduces no persistence of its own, and it reads exactly the column `068`
already declares.

THE DECLARATION.

    file_contract = (
      {"role": "main_document", "format": "PDF",  "main": True},
      {"role": "detailed_data", "format": "XLSX", "required": False},
      {"role": "raw_data",      "format": "CSV",  "required": False, "max": 3},
    )

    role      required   one of the three approved semantic roles
    format    required   one of the approved file formats
    main      optional   exactly one entry declares it; default False
    required  optional   default True — a successful publication must carry it
    max       optional   default 1 — how many members of this (role, format)

An EMPTY declaration asserts nothing and constrains nothing: a definition that
has not declared its outputs is not thereby claiming they are arbitrary, it is
claiming nothing, and inventing a rule for it would be this module deciding
product behaviour it was not given.
"""
from __future__ import annotations

from .errors import ReportFileContractError
from .models import ROLE_DETAILED, ROLE_MAIN, ROLE_RAW

VALID_ROLES = (ROLE_MAIN, ROLE_DETAILED, ROLE_RAW)
VALID_FORMATS = ("PDF", "XLSX", "CSV", "JSON", "TXT", "ZIP")

_ENTRY_KEYS = frozenset({"role", "format", "main", "required", "max"})
MAX_CONTRACT_ENTRIES = 12


def _fail(message: str) -> None:
    raise ReportFileContractError(message)


def normalize_contract(declaration) -> tuple[dict, ...]:
    """Validate a declaration and return it in canonical form.

    Raises :class:`ReportFileContractError` rather than returning a partially
    understood contract: a contract that is enforced only where it happened to
    parse is worse than none, because it reads as enforcement.
    """
    if declaration is None:
        return ()
    if isinstance(declaration, (str, bytes)) or not isinstance(declaration, (list, tuple)):
        _fail("a file contract is a list of declared files")
    entries = list(declaration)
    if len(entries) > MAX_CONTRACT_ENTRIES:
        _fail(f"a file contract declares at most {MAX_CONTRACT_ENTRIES} files")

    normalized: list[dict] = []
    seen: set[tuple[str, str]] = set()
    mains = 0
    for raw in entries:
        if not isinstance(raw, dict):
            _fail("every declared file is an object")
        unknown = sorted(set(raw) - _ENTRY_KEYS)
        if unknown:
            _fail(f"unknown file contract key(s): {', '.join(unknown)}")
        role = str(raw.get("role") or "")
        fmt = str(raw.get("format") or "")
        if role not in VALID_ROLES:
            _fail(f"unknown declared file role {role!r}")
        if fmt not in VALID_FORMATS:
            _fail(f"unknown declared file format {fmt!r}")
        is_main = bool(raw.get("main", False))
        required = bool(raw.get("required", True))
        cardinality = raw.get("max", 1)
        if isinstance(cardinality, bool) or not isinstance(cardinality, int) or cardinality < 1:
            _fail("a declared file's `max` is a positive integer")
        if (role, fmt) in seen:
            # Two entries for one `(role, format)` would make "which entry does
            # this member satisfy" ambiguous, and the cardinalities would have
            # to be summed by a rule nobody declared.
            _fail(f"the file contract declares {role}/{fmt} twice")
        seen.add((role, fmt))
        if is_main:
            mains += 1
            if not required:
                _fail("the declared main file cannot be optional")
            if cardinality != 1:
                _fail("the declared main file is a single file")
        normalized.append(
            {"role": role, "format": fmt, "main": is_main,
             "required": required, "max": cardinality}
        )

    if normalized and mains != 1:
        # `RP-13`/`I-6` require the main file to be a declared, explicit fact at
        # both levels: the definition says which output it is, the member says
        # which file it was.
        _fail(f"a file contract declares exactly one main file, got {mains}")
    return tuple(normalized)


def contract_from_stored(value) -> tuple[dict, ...]:
    """The contract as persisted, re-validated before it is enforced.

    A stored declaration is re-read through the same validator that accepted it,
    so a row written before this module existed — or edited around the
    boundary — cannot be silently treated as an empty (and therefore vacuous)
    contract.
    """
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except ValueError:
            _fail("the stored file contract is not readable JSON")
    return normalize_contract(value)


def assert_publication_satisfies(contract: tuple[dict, ...], members) -> None:
    """Prove the successful file set is the set the definition declared.

    `members` are the already-individually-validated `PublishedFile` records.
    This is the last gate before the member set is written, so a publication
    either satisfies its own type's contract or it is not a successful
    publication at all.
    """
    if not contract:
        return

    counted: dict[tuple[str, str], int] = {}
    for member in members:
        key = (str(member.semantic_role), str(member.file_format))
        counted[key] = counted.get(key, 0) + 1

    declared = {(e["role"], e["format"]): e for e in contract}

    undeclared = sorted(k for k in counted if k not in declared)
    if undeclared:
        shown = ", ".join(f"{role}/{fmt}" for role, fmt in undeclared)
        raise ReportFileContractError(
            f"this report type does not declare {shown}; a publication cannot "
            f"carry a file its definition never declared"
        )

    for key, entry in declared.items():
        count = counted.get(key, 0)
        label = f"{key[0]}/{key[1]}"
        if entry["required"] and count == 0:
            raise ReportFileContractError(
                f"this report type declares {label} as required and the publication omits it"
            )
        if count > entry["max"]:
            raise ReportFileContractError(
                f"this report type declares at most {entry['max']} {label} file(s), got {count}"
            )

    main_entry = next((e for e in contract if e["main"]), None)
    if main_entry is not None:
        main_members = [m for m in members if m.is_main_file]
        # Exactly-one is already proven by the member validator and by `I-6`;
        # what the contract adds is WHICH declared output the main file is.
        actual = main_members[0] if main_members else None
        if actual is None or (
            str(actual.semantic_role) != main_entry["role"]
            or str(actual.file_format) != main_entry["format"]
        ):
            raise ReportFileContractError(
                f"this report type declares its main file as "
                f"{main_entry['role']}/{main_entry['format']}"
            )


__all__ = [
    "MAX_CONTRACT_ENTRIES",
    "VALID_FORMATS",
    "VALID_ROLES",
    "assert_publication_satisfies",
    "contract_from_stored",
    "normalize_contract",
]
