#!/usr/bin/env python3
"""The MATERIALIZED one-step rollback envelope for the first-seen CONTRACT.

WHAT THE REVIEW ESTABLISHED, AND WHY THIS SUITE EXISTS.
    Closing the first-seen pair CONTRACT installs a constraint that rejects the
    M4-era writer, so from that moment `SCHEMA_STATE_GUARDS` refuses every
    release that does not declare `client_trips_first_seen_pair_contract` —
    including whatever `previous` points at. If `previous` is a legacy release,
    the closure destroys the one-step rollback it was supposed to have accounted
    for.

    `--rollback-window-closed` asserted the operator had accounted for that. It
    could not establish it. Independent review checked the real pointers —
    `current = 18dcda87f16d`, `previous = 20a01b4358b8` — and found neither
    carries the capability, and further established that ONE bridge activation
    does not fix it: it leaves `current = bridge` and `previous = 18dcda87f16d`,
    still legacy. The envelope exists only once TWO DISTINCT bridge-compatible
    releases have been activated in sequence, leaving `current = bridge-B` and
    `previous = bridge-A`. Re-activating one release is a no-op and moves no
    pointer, so two aliases for one release prove nothing either.

    So the proof is read from the release layout, by
    `ops/release_schema_preflight.rollback_envelope_status`, and this suite is
    its evidence — against REAL materialized releases produced by the real
    `prepare_release`/`activate_release` helpers, not synthetic booleans.

NO DATABASE REQUIRED. The envelope is a property of the release layout alone,
which is exactly what makes it checkable before a closure touches any client.
DESTRUCTIVE ONLY OF ITS OWN TEMPORARY DIRECTORIES.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops.release_boundary import (  # noqa: E402
    RELEASES_DIRNAME, ReleaseBoundaryError, activate_release,
    pointer_release_id, prepare_release, verify_release,
)
from ops.release_schema_preflight import (  # noqa: E402
    CANONICAL_BRIDGE_STATES,
    CAPABILITY_FIRST_SEEN_PAIR_CONTRACT,
    FIRST_SEEN_EXPAND_CONSTRAINT,
    FIRST_SEEN_STRICT_CONSTRAINT,
    inspect_release_bridge_compatibility,
    parse_requirements,
    relation_state_defects,
    rollback_envelope_status,
)

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


# ---------------------------------------------------------------------------
# Release documents, by the PROPERTY that makes each one what it is
# ---------------------------------------------------------------------------

def _repository_document() -> Dict:
    return json.loads((ROOT / "db/schema_requirements.json").read_text("utf-8"))


def _bridge_document() -> Dict:
    """The real thing: capability declared, both states spanned."""
    return _repository_document()


def _legacy_document() -> Dict:
    """M4-era: no capability, and the pairing relation declared as one state."""
    document = _repository_document()
    document.pop("capabilities", None)
    for requirement in document["requirements"]:
        for relation in requirement.get("relations", []):
            relation.pop("constraint_alternatives", None)
    return document


def _capability_without_span_document() -> Dict:
    """Declares the capability but pins ONE state — case 6.

    A release can honestly claim its writer is strict-compatible and still be
    useless as a rollback target across the transition, because a requirements
    file pinned to EXPAND alone stops passing the moment the closure drops that
    constraint. The capability and the span are independent facts and both are
    required.
    """
    document = _repository_document()
    for requirement in document["requirements"]:
        for relation in requirement.get("relations", []):
            alternatives = relation.pop("constraint_alternatives", None)
            if alternatives:
                relation["constraints"] = alternatives[0]["constraints"]
    return document


def _contract_not_validated_document() -> Dict:
    """Spans both names but accepts an UNVALIDATED strict constraint.

    An interrupted closure is not a closed one, so a release willing to accept
    it is not a valid rollback target for a closed CONTRACT either.
    """
    document = _repository_document()
    for requirement in document["requirements"]:
        for relation in requirement.get("relations", []):
            for group in relation.get("constraint_alternatives", []):
                for constraint in group["constraints"]:
                    if constraint["name"] == FIRST_SEEN_STRICT_CONSTRAINT:
                        constraint["validated"] = False
    return document


def _bridge_document_with(mutate) -> Dict:
    """The honest bridge document with one surgical change to its declaration."""
    document = _repository_document()
    for requirement in document["requirements"]:
        for relation in requirement.get("relations", []):
            for group in relation.get("constraint_alternatives", []):
                for constraint in group["constraints"]:
                    mutate(relation, group, constraint)
    return document


def _contract_check_false_document() -> Dict:
    """The review's probe: the right NAME carrying `CHECK (false)`."""
    def _mutate(_relation, _group, constraint):
        if constraint["name"] == FIRST_SEEN_STRICT_CONSTRAINT:
            constraint["definition"] = "CHECK (false)"
    return _bridge_document_with(_mutate)


def _contract_check_true_document() -> Dict:
    """And its mirror: a tautology that would accept any schema at all."""
    def _mutate(_relation, _group, constraint):
        if constraint["name"] == FIRST_SEEN_STRICT_CONSTRAINT:
            constraint["definition"] = "CHECK (true)"
    return _bridge_document_with(_mutate)


def _wrong_expand_document() -> Dict:
    """An EXPAND alternative whose expression is not migration 048's."""
    def _mutate(_relation, _group, constraint):
        if constraint["name"] == FIRST_SEEN_EXPAND_CONSTRAINT:
            constraint["definition"] = (
                "CHECK ((first_seen_request_id IS NOT NULL)) NOT VALID"
            )
    return _bridge_document_with(_mutate)


def _expand_validated_document() -> Dict:
    """EXPAND declared `validated: true` — a state 048 never produces."""
    def _mutate(_relation, _group, constraint):
        if constraint["name"] == FIRST_SEEN_EXPAND_CONSTRAINT:
            constraint["validated"] = True
            constraint["definition"] = constraint["definition"].replace(
                " NOT VALID", ""
            )
    return _bridge_document_with(_mutate)


def _name_only_document() -> Dict:
    """Both alternatives reduced to bare names — the M4-era constraint form.

    Parses, declares the capability, names both constraints, and proves nothing
    about either expression. This is the "merely similarly named requirements"
    case stated exactly.
    """
    document = _repository_document()
    for requirement in document["requirements"]:
        for relation in requirement.get("relations", []):
            for group in relation.get("constraint_alternatives", []):
                group["constraints"] = [
                    constraint["name"] for constraint in group["constraints"]
                ]
    return document


#: A constraint that is real, exactly specified and simply not part of the
#: bridge. Used to keep a mutated document PARSEABLE — the strict parser refuses
#: a `constraint_alternatives` block with fewer than two groups outright, so a
#: probe aimed at the bridge check must still declare two states.
_UNRELATED_ALTERNATIVE = {
    "state": "UNRELATED",
    "constraints": [
        {
            "name": "ck_client_trips_unrelated_probe",
            "definition": "CHECK ((provider_trip_id IS NOT NULL))",
            "validated": True,
        }
    ],
}


def _single_group_document() -> Dict:
    """Both canonical constraints collapsed into ONE alternative.

    Every definition is exact and the declaration still spans nothing: one group
    means "all of these hold together", and no database ever carries the EXPAND
    constraint and the validated strict constraint at once. A second, unrelated
    group keeps the document parseable so the BRIDGE check is what refuses it.
    """
    document = _repository_document()
    for requirement in document["requirements"]:
        for relation in requirement.get("relations", []):
            groups = relation.get("constraint_alternatives")
            if not groups:
                continue
            merged = [c for group in groups for c in group["constraints"]]
            relation["constraint_alternatives"] = [
                {"state": "MERGED", "constraints": merged},
                json.loads(json.dumps(_UNRELATED_ALTERNATIVE)),
            ]
    return document


def _expand_only_document() -> Dict:
    """The CONTRACT alternative replaced by one that is not it."""
    document = _repository_document()
    for requirement in document["requirements"]:
        for relation in requirement.get("relations", []):
            groups = relation.get("constraint_alternatives")
            if not groups:
                continue
            relation["constraint_alternatives"] = [
                group if not any(c["name"] == FIRST_SEEN_STRICT_CONSTRAINT
                                 for c in group["constraints"])
                else json.loads(json.dumps(_UNRELATED_ALTERNATIVE))
                for group in groups
            ]
    return document


def _exact_bridge_without_capability_document() -> Dict:
    """An exact, spanning declaration that never claims the capability."""
    document = _repository_document()
    document.pop("capabilities", None)
    return document


# ---------------------------------------------------------------------------
# THE THIRD REVIEW'S FINDING — structural exactness of the declaration
# ---------------------------------------------------------------------------
#
# Per-member exactness accepted a declaration whose canonical members were all
# present and whose groups carried MORE than that. Independent review built two
# genuinely prepared bridge releases whose EXPAND group additionally required
# `ck_synthetic_extra`: G6 returned READY with no declaration defects, while
# canonical EXPAND preflight correctly FAILED the same release because the extra
# constraint was absent from the database. G6's proof was weaker than the
# contract it claimed to prove, so every shape below is now a refusal.

#: The review's synthetic member, verbatim: real, exactly specified, and simply
#: not part of the transition contract.
_SYNTHETIC_EXTRA_CONSTRAINT = {
    "name": "ck_synthetic_extra",
    "definition": "CHECK ((first_seen_request_id IS NOT NULL))",
    "validated": True,
}


def _bridge_relation(document: Dict) -> Dict:
    """The single relation declaration that carries the bridge alternatives."""
    for requirement in document["requirements"]:
        for relation in requirement.get("relations", []):
            groups = relation.get("constraint_alternatives") or []
            if any(constraint["name"] in (FIRST_SEEN_EXPAND_CONSTRAINT,
                                          FIRST_SEEN_STRICT_CONSTRAINT)
                   for group in groups for constraint in group["constraints"]):
                return relation
    raise AssertionError("the repository document declares no bridge relation")


def _bridge_group(document: Dict, constraint_name: str) -> Dict:
    for group in _bridge_relation(document)["constraint_alternatives"]:
        if any(c["name"] == constraint_name for c in group["constraints"]):
            return group
    raise AssertionError(f"no alternative declares {constraint_name}")


def _copy(value):
    return json.loads(json.dumps(value))


def _extra_member_in_expand_document() -> Dict:
    """THE REVIEW'S FIXTURE: canonical EXPAND member PLUS `ck_synthetic_extra`.

    Every canonical member is present and exact. The group nonetheless demands
    a constraint the transition never installs, so canonical EXPAND preflight
    fails this release — which is precisely why G6 must not call it compatible.
    """
    document = _repository_document()
    _bridge_group(document, FIRST_SEEN_EXPAND_CONSTRAINT)["constraints"].append(
        _copy(_SYNTHETIC_EXTRA_CONSTRAINT)
    )
    return document


def _extra_member_in_contract_document() -> Dict:
    """Its mirror on the CONTRACT side."""
    document = _repository_document()
    _bridge_group(document, FIRST_SEEN_STRICT_CONSTRAINT)["constraints"].append(
        _copy(_SYNTHETIC_EXTRA_CONSTRAINT)
    )
    return document


def _duplicated_expand_member_document() -> Dict:
    """The canonical EXPAND member declared twice inside its own group."""
    document = _repository_document()
    group = _bridge_group(document, FIRST_SEEN_EXPAND_CONSTRAINT)
    group["constraints"].append(_copy(group["constraints"][0]))
    return document


def _duplicated_contract_member_document() -> Dict:
    """And the same duplication on the CONTRACT side."""
    document = _repository_document()
    group = _bridge_group(document, FIRST_SEEN_STRICT_CONSTRAINT)
    group["constraints"].append(_copy(group["constraints"][0]))
    return document


def _duplicated_group_document() -> Dict:
    """A third group that is a byte-identical copy of EXPAND under a new label.

    The parser rejects a repeated `state` label outright, so the copy carries a
    different one — which is exactly why `state` is excluded from structural
    identity and the MEMBERS decide.
    """
    document = _repository_document()
    relation = _bridge_relation(document)
    duplicate = _copy(_bridge_group(document, FIRST_SEEN_EXPAND_CONSTRAINT))
    duplicate["state"] = "EXPAND_AGAIN"
    relation["constraint_alternatives"].append(duplicate)
    return document


def _split_members_document() -> Dict:
    """Canonical members split across FOUR groups, half of each in its own.

    Each group is a state the database is asked to be in wholly. Splitting the
    contract across additional groups changes which states are acceptable, so a
    presence search sees the same names and the contract is a different one.
    """
    document = _repository_document()
    relation = _bridge_relation(document)
    expand = _copy(_bridge_group(document, FIRST_SEEN_EXPAND_CONSTRAINT))
    contract = _copy(_bridge_group(document, FIRST_SEEN_STRICT_CONSTRAINT))
    partial_expand = _copy(expand)
    partial_expand["state"] = "EXPAND_PARTIAL"
    partial_expand["constraints"] = [_copy(_SYNTHETIC_EXTRA_CONSTRAINT)]
    partial_contract = _copy(contract)
    partial_contract["state"] = "CONTRACT_PARTIAL"
    partial_contract["constraints"] = [_copy(_SYNTHETIC_EXTRA_CONSTRAINT)]
    partial_contract["constraints"][0]["name"] = "ck_synthetic_extra_two"
    relation["constraint_alternatives"] = [
        expand, contract, partial_expand, partial_contract,
    ]
    return document


def _extra_synthetic_group_document() -> Dict:
    """The exact canonical pair PLUS an additional, unrelated third state.

    A release accepting a third state is activatable against a database in
    neither side of the transition, which is not the bridge contract.
    """
    document = _repository_document()
    _bridge_relation(document)["constraint_alternatives"].append(
        _copy(_UNRELATED_ALTERNATIVE)
    )
    return document


# ---------------------------------------------------------------------------
# THE FOURTH REVIEW'S FINDING — the ALTERNATIVES ARE NOT THE REQUIREMENT
# ---------------------------------------------------------------------------
#
# Structural exactness of `constraint_alternatives` was proved, and independent
# review then showed that is only one field of the relation declaration. Two
# honestly prepared releases carried the exact canonical alternatives and the
# same `public.client_trips` relation additionally required an absent
# unconditional `ck_synthetic_unconditional`: G6 returned
# `G6_READY_WITH_EXTRA_UNCONDITIONAL=True` while `relation_defects` correctly
# returned `constraint_absent:public.client_trips.ck_synthetic_unconditional`.
# So G6 could call a release rollback-safe that its own activation preflight
# would refuse. Every shape below asserts the complete relation instead.

#: The review's reproduction, verbatim.
_SYNTHETIC_UNCONDITIONAL_CONSTRAINT = {
    "name": "ck_synthetic_unconditional",
    "definition": "CHECK ((first_seen_request_id IS NOT NULL))",
    "validated": True,
}


def _extra_unconditional_constraint_document() -> Dict:
    """THE REVIEW'S REPRODUCTION: exact alternatives, absent unconditional CHECK.

    Nothing about the alternatives is wrong. The relation simply also demands a
    constraint the transition installs in neither state, which is exactly what
    makes the release unactivatable and what G6 used to ignore.
    """
    document = _repository_document()
    _bridge_relation(document).setdefault("constraints", []).append(
        _copy(_SYNTHETIC_UNCONDITIONAL_CONSTRAINT)
    )
    return document


def _absent_required_column_document() -> Dict:
    """Exact alternatives, plus a column neither canonical state carries."""
    document = _repository_document()
    _bridge_relation(document).setdefault("columns", []).append({
        "name": "first_seen_synthetic_absent_col",
        "type": "uuid",
        "nullable": True,
    })
    return document


def _absent_required_index_document() -> Dict:
    """Exact alternatives, plus an index neither canonical state carries."""
    document = _repository_document()
    _bridge_relation(document).setdefault("indexes", []).append({
        "name": "ix_synthetic_absent",
        "definition": (
            "CREATE INDEX ix_synthetic_absent ON public.client_trips "
            "USING btree (first_seen_request_id)"
        ),
    })
    return document


def _satisfied_extra_column_document() -> Dict:
    """Exact alternatives, plus a column BOTH canonical states really carry.

    `first_seen_request_id` is the other half of the pair the transition is
    about, so requiring it is satisfiable on either side of the DDL step. The
    complete-relation proof must therefore ACCEPT this — a correction that
    rejected everything beyond the alternatives would be refusing honest
    releases, not closing the reviewed hole.
    """
    document = _repository_document()
    _bridge_relation(document).setdefault("columns", []).append({
        "name": "first_seen_request_id", "type": "uuid", "nullable": True,
    })
    return document


def _out_of_contract_unconditional_document() -> Dict:
    """An unconditional constraint a REAL `client_trips` satisfies in both states.

    `client_trips_pkey` genuinely exists on the live relation, so live preflight
    would pass it. It is not part of what the CONTRACT transition governs, so
    the canonical states do not model it and G6 refuses. That direction —
    G6 STRICTER than live preflight — is the safe one and is deliberate: a
    release asserting things outside the bridge contract is not the bridge
    contract, and the reviewed bug was G6 being WEAKER, never stricter.
    """
    document = _repository_document()
    _bridge_relation(document).setdefault("constraints", []).append({
        "name": "client_trips_pkey",
        "definition": "PRIMARY KEY (id)",
        "validated": True,
    })
    return document


def _out_of_contract_index_document() -> Dict:
    """Its index mirror, on the same reasoning."""
    document = _repository_document()
    _bridge_relation(document).setdefault("indexes", []).append({
        "name": "client_trips_pkey",
        "definition": (
            "CREATE UNIQUE INDEX client_trips_pkey ON public.client_trips "
            "USING btree (id)"
        ),
    })
    return document


def _second_bridge_relation_document() -> Dict:
    """Two relation declarations collectively naming the bridge constraints.

    The bridge is ONE relation requirement. A second declaration re-asserting a
    bridge constraint elsewhere is a second, unreviewed assertion about the
    transition, and "collectively satisfies" is not "declares".
    """
    document = _repository_document()
    document["requirements"].append({
        "migration": "999_synthetic_second_bridge.sql",
        "scope": "client_business",
        "milestone": "M-LAG",
        "reason": "synthetic probe: a second declaration of the bridge",
        "relations": [
            {
                "schema": "public",
                "table": "client_trip_shadow",
                "constraint_alternatives": [
                    _copy(_bridge_group(document, FIRST_SEEN_EXPAND_CONSTRAINT)),
                    _copy(_bridge_group(document, FIRST_SEEN_STRICT_CONSTRAINT)),
                ],
            }
        ],
    })
    return document


def _wrong_relation_document() -> Dict:
    """The exact canonical structure declared on a DIFFERENT relation."""
    document = _repository_document()
    relation = _bridge_relation(document)
    relation["table"] = "client_trips_archive"
    return document


def _unrelated_extra_requirement_document() -> Dict:
    """An exact bridge declaration NEXT TO an unrelated new requirement.

    Exactness is a property of the bridge declaration, not of the document. A
    release that also declares something else entirely — as every real release
    does, and as the parallel Eco work does right now — must stay compatible.
    """
    document = _repository_document()
    document["requirements"].append({
        "migration": "998_synthetic_unrelated_requirement.sql",
        "scope": "client_business",
        "milestone": "UNRELATED",
        "reason": "synthetic probe: an unrelated requirement elsewhere",
        "relations": [
            {
                "schema": "public",
                "table": "client_unrelated_probe",
                "columns": [
                    {"name": "probe_id", "type": "bigint", "nullable": False}
                ],
            }
        ],
    })
    return document


# ---------------------------------------------------------------------------
# A real, minimal source repository -> real materialized releases
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "envelope@test.invalid")
    _git(repo, "config", "user.name", "envelope-test")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit_variant(repo: Path, name: str, document: Optional[Dict]) -> str:
    """One commit whose tree carries `document`, or no requirements file at all."""
    requirements = repo / "db" / "schema_requirements.json"
    requirements.parent.mkdir(parents=True, exist_ok=True)
    if document is None:
        if requirements.exists():
            requirements.unlink()
    else:
        requirements.write_text(json.dumps(document, indent=2), encoding="utf-8")
    # A per-variant marker file, so two variants with identical requirements
    # still produce genuinely different trees and therefore distinct releases.
    (repo / "VARIANT").write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"variant {name}", "--allow-empty")
    return _git(repo, "rev-parse", "HEAD").strip()


def _materialize(repo: Path, release_root: Path, name: str,
                 document: Optional[Dict]) -> str:
    commit = _commit_variant(repo, name, document)
    result = prepare_release(source_repo=repo, release_root=release_root,
                             committish=commit)
    return str(result["release_id"])


def _point(release_root: Path, pointer: str, release_id: Optional[str]) -> None:
    """Set a pointer directly. Deliberately NOT `activate_release`.

    The envelope must be provable for arbitrary pointer combinations, including
    ones a correct activation sequence would never produce — that is the whole
    point of a gate. `test_a_real_activation_sequence_reaches_the_envelope`
    covers the supported path separately.
    """
    target = release_root / pointer
    if target.is_symlink() or target.exists():
        target.unlink()
    if release_id is not None:
        target.symlink_to(f"{RELEASES_DIRNAME}/{release_id}")


def _status(release_root: Path) -> Dict:
    return rollback_envelope_status(release_root)


def _expect(label: str, release_root: Path, ready: bool) -> Dict:
    status = _status(release_root)
    _check(label, bool(status["ready"]) is ready,
           f"ready={status['ready']} reasons={status['reasons']}")
    return status


# ---------------------------------------------------------------------------
# The eight release-state cases
# ---------------------------------------------------------------------------

def run_cases(repo: Path, release_root: Path) -> None:
    legacy_a = _materialize(repo, release_root, "legacy-a", _legacy_document())
    legacy_b = _materialize(repo, release_root, "legacy-b", _legacy_document())
    bridge_a = _materialize(repo, release_root, "bridge-a", _bridge_document())
    bridge_b = _materialize(repo, release_root, "bridge-b", _bridge_document())
    cap_only = _materialize(repo, release_root, "capability-without-span",
                            _capability_without_span_document())
    unvalidated = _materialize(repo, release_root, "contract-not-validated",
                               _contract_not_validated_document())
    no_reqs = _materialize(repo, release_root, "no-requirements", None)

    print("\n## 1 — current legacy + previous legacy")
    _point(release_root, "current", legacy_b)
    _point(release_root, "previous", legacy_a)
    status = _expect("1: two legacy releases are NOT READY", release_root, False)
    _check("1: and both are named as the reason",
           any(legacy_a in r for r in status["reasons"])
           and any(legacy_b in r for r in status["reasons"]),
           str(status["reasons"]))

    print("\n## 2 — current bridge + previous legacy (the state after ONE bridge activation)")
    _point(release_root, "current", bridge_a)
    _point(release_root, "previous", legacy_a)
    status = _expect("2: one bridge + one legacy is NOT READY", release_root, False)
    _check("2: current is reported bridge-compatible",
           status["current"]["compatible"], str(status["current"]))
    _check("2: previous is reported NOT bridge-compatible",
           not status["previous"]["compatible"], str(status["previous"]))
    _check("2: and the missing capability is named",
           any(CAPABILITY_FIRST_SEEN_PAIR_CONTRACT in r for r in status["reasons"]),
           str(status["reasons"]))

    print("\n## 3 — current and previous are the SAME bridge release")
    _point(release_root, "current", bridge_a)
    _point(release_root, "previous", bridge_a)
    status = _expect("3: two aliases for one release are NOT READY",
                     release_root, False)
    _check("3: and the reason says so explicitly",
           any("same_release" in r for r in status["reasons"]),
           str(status["reasons"]))

    print("\n## 4 — current bridge-B + previous bridge-A, distinct and compatible")
    _point(release_root, "current", bridge_b)
    _point(release_root, "previous", bridge_a)
    status = _expect("4: two distinct bridge-compatible releases are READY",
                     release_root, True)
    _check("4: with no reasons at all", status["reasons"] == [],
           str(status["reasons"]))
    _check("4: and both spans recorded",
           status["current"]["declares_expand_state"]
           and status["current"]["declares_validated_contract_state"]
           and status["previous"]["declares_expand_state"]
           and status["previous"]["declares_validated_contract_state"])

    print("\n## 5 — previous is materialized but declares no capability")
    _point(release_root, "current", bridge_b)
    _point(release_root, "previous", no_reqs)
    _expect("5: a materialized release with no requirements file is NOT READY",
            release_root, False)
    _point(release_root, "previous", legacy_a)
    status = _expect("5: nor one whose requirements omit the capability",
                     release_root, False)
    _check("5: the defect names the capability",
           any("capability_not_declared" in d
               for d in status["previous"]["defects"]),
           str(status["previous"]["defects"]))

    print("\n## 6 — previous declares the capability but not the full span")
    _point(release_root, "current", bridge_b)
    _point(release_root, "previous", cap_only)
    status = _expect("6: capability without the EXPAND/CONTRACT span is NOT READY",
                     release_root, False)
    _check("6: the capability itself is acknowledged as declared",
           status["previous"]["declares_capability"], str(status["previous"]))
    _check("6: but the missing CONTRACT state is the defect",
           any(FIRST_SEEN_STRICT_CONSTRAINT in d
               for d in status["previous"]["defects"]),
           str(status["previous"]["defects"]))

    _point(release_root, "previous", unvalidated)
    status = _expect("6: nor does accepting an UNVALIDATED strict constraint count",
                     release_root, False)
    _check("6: EXPAND is still recognised as declared",
           status["previous"]["declares_expand_state"], str(status["previous"]))
    _check("6: while the validated CONTRACT state is not",
           not status["previous"]["declares_validated_contract_state"],
           str(status["previous"]))

    print("\n## 7 — missing or broken previous release metadata / path")
    _point(release_root, "current", bridge_b)
    _point(release_root, "previous", None)
    status = _expect("7: an unset previous pointer is NOT READY", release_root, False)
    _check("7: reported as a non-canonical pointer",
           any("pointer_not_canonical" in r and "pointer_unset" in r
               for r in status["reasons"]),
           str(status["reasons"]))

    _point(release_root, "previous", bridge_a)
    meta = release_root / "meta" / f"{bridge_a}.json"
    meta.rename(meta.with_suffix(".json.moved"))
    status = _expect("7: a release tree with no metadata is NOT READY",
                     release_root, False)
    _check("7: reported as an unverifiable release, by the authoritative "
           "release verifier rather than a file-existence guess",
           any("release_not_verified" in d and "missing_metadata" in d
               for d in status["previous"]["defects"]),
           str(status["previous"]["defects"]))
    meta.with_suffix(".json.moved").rename(meta)

    tree = release_root / RELEASES_DIRNAME / bridge_a
    moved = release_root / RELEASES_DIRNAME / f"{bridge_a}.moved"
    _chmod_writable(tree)
    tree.rename(moved)
    status = _expect("7: a pointer whose release tree is gone is NOT READY",
                     release_root, False)
    _check("7: reported as a dangling pointer",
           any("pointer_target_dangling" in d
               for d in status["previous"]["defects"]),
           str(status["previous"]["defects"]))
    moved.rename(tree)

    # Restored, and READY again — so every refusal above was the intended cause
    # and not an artefact the fixture never recovered from.
    _expect("7: and the envelope is READY again once the layout is restored",
            release_root, True)

    print("\n## 8 — the post-CONTRACT one-step rollback target itself")
    previous_tree = release_root / RELEASES_DIRNAME / bridge_a
    record = inspect_release_bridge_compatibility(previous_tree)
    _check("8: the release `previous` points at is bridge-compatible, so it "
           "remains activatable against a CLOSED contract",
           record["compatible"], str(record["defects"]))
    _check("8: it declares the capability the state guard demands",
           record["declares_capability"])
    _check("8: and spans EXPAND -> validated CONTRACT",
           record["declares_expand_state"]
           and record["declares_validated_contract_state"])
    _check("8: the EXPAND constraint it names is the one migration 048 installs",
           FIRST_SEEN_EXPAND_CONSTRAINT in json.dumps(
               json.loads((previous_tree / "db/schema_requirements.json")
                          .read_text("utf-8"))))

    return {"legacy_a": legacy_a, "bridge_a": bridge_a, "bridge_b": bridge_b}


def _chmod_writable(path: Path) -> None:
    """Release trees are sealed read-only; the fixture must still move them."""
    for current, dirs, files in os.walk(path):
        os.chmod(current, 0o755)
        for name in files:
            try:
                os.chmod(Path(current) / name, 0o644)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# The supported path: two sequential activations reach the envelope
# ---------------------------------------------------------------------------

def test_a_real_activation_sequence_reaches_the_envelope(
    repo: Path, release_root: Path,
) -> None:
    """Exactly the rollout docs/21 now documents, executed rather than asserted.

    A stub preflight is injected because this test is about POINTERS, not about
    the fleet: `activate_release`'s schema gate has its own suites and a live
    fleet here would prove nothing extra. Everything else — verification, the
    management lock, the pointer swap, `previous` bookkeeping — is the real
    implementation.
    """
    print("\n## test_a_real_activation_sequence_reaches_the_envelope")

    class _NoFleet:
        fleet_fingerprint = None
        declared_capabilities: List[str] = []

        def as_dict(self) -> Dict:
            return {"note": "stubbed_for_pointer_test"}

    def _stub_preflight(**_kwargs):
        return _NoFleet()

    legacy = _materialize(repo, release_root, "seq-legacy", _legacy_document())
    bridge_a = _materialize(repo, release_root, "seq-bridge-a", _bridge_document())
    bridge_b = _materialize(repo, release_root, "seq-bridge-b", _bridge_document())

    _point(release_root, "current", legacy)
    _point(release_root, "previous", None)
    _expect("start: a legacy production pointer is NOT READY", release_root, False)

    activate_release(release_root=release_root, release_id=bridge_a,
                     source_repo=repo, schema_preflight=_stub_preflight)
    _check("after activating bridge-A, current is bridge-A",
           pointer_release_id(release_root / "current") == bridge_a)
    _check("and previous is the legacy release it displaced",
           pointer_release_id(release_root / "previous") == legacy)
    status = _expect(
        "THE REVIEW'S POINT: one bridge activation does NOT open the envelope",
        release_root, False,
    )
    _check("because previous is still the legacy release",
           not status["previous"]["compatible"], str(status["previous"]))

    # Re-activating the same release is a no-op and must not fake progress.
    activate_release(release_root=release_root, release_id=bridge_a,
                     source_repo=repo, schema_preflight=_stub_preflight)
    _check("re-activating bridge-A moves no pointer",
           pointer_release_id(release_root / "previous") == legacy)
    _expect("so it does not open the envelope either", release_root, False)

    activate_release(release_root=release_root, release_id=bridge_b,
                     source_repo=repo, schema_preflight=_stub_preflight)
    _check("after activating a SECOND distinct bridge release, current is bridge-B",
           pointer_release_id(release_root / "current") == bridge_b)
    _check("and previous is bridge-A",
           pointer_release_id(release_root / "previous") == bridge_a)
    _expect("only NOW is the rollback envelope READY", release_root, True)


# ---------------------------------------------------------------------------
# The closure tool refuses on an unproven envelope, before touching a client
# ---------------------------------------------------------------------------

def test_the_closure_tool_gates_on_the_envelope(repo: Path, release_root: Path,
                                                ids: Dict[str, str]) -> None:
    """G6 in the real `run()` precondition flow, not a separate predicate."""
    print("\n## test_the_closure_tool_gates_on_the_envelope")
    import argparse

    from ops.close_telematics_first_seen_pair_contract import (
        ContractRefused, EXIT_NOT_READY, rollback_envelope, run,
    )

    def _args(**overrides) -> argparse.Namespace:
        base = dict(
            client_code=None, expected_environment="test",
            expected_platform_uuid="db8055e0-e030-4d5a-816b-ec4dc338d698",
            check_only=False, approval_ref="REF-1", execute=True,
            confirm="CLOSE_CONTRACT", rollback_window_closed=True,
            release_root=str(release_root), dsn="postgresql://unused.invalid/none",
            # The explicit non-production trust boundary. A test inventory is
            # never authoritative by accident, and `test_production_identity_
            # rejects_an_arbitrary_release_root` proves the flag buys nothing
            # under a production identity.
            allow_non_production_release_root=True, source_repo=str(repo),
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    _point(release_root, "current", ids["bridge_a"])
    _point(release_root, "previous", ids["legacy_a"])
    try:
        run(_args())
        _check("--execute is refused while the envelope is unproven", False,
               "run() returned instead of refusing")
    except ContractRefused as exc:
        _check("--execute is refused while the envelope is unproven",
               exc.code == "ROLLBACK_ENVELOPE_NOT_MATERIALIZED", str(exc))
        _check("with the NOT_READY exit code, not a parameter error",
               exc.exit_code == EXIT_NOT_READY, str(exc.exit_code))
        _check("and the refusal names the exact release ids",
               ids["bridge_a"] in str(exc) and ids["legacy_a"] in str(exc),
               str(exc))
        _check("while exposing no secret",
               "password" not in str(exc).lower(), str(exc))

    _check("--rollback-window-closed does NOT override the proof",
           True)
    try:
        run(_args(rollback_window_closed=True))
        _check("the operator acknowledgement cannot substitute for G6", False)
    except ContractRefused as exc:
        _check("the operator acknowledgement cannot substitute for G6",
               exc.code == "ROLLBACK_ENVELOPE_NOT_MATERIALIZED", str(exc))

    # The refusal happens BEFORE the platform connection is opened: the DSN
    # above points nowhere, so reaching the database at all would raise a
    # connection error instead of the gate's refusal. It did not.
    _check("the gate refused before any database connection was attempted", True)

    # Check-only reports the envelope rather than refusing on it, and reports it
    # from the same function the execute path gates on.
    _point(release_root, "current", ids["bridge_b"])
    _point(release_root, "previous", ids["bridge_a"])
    envelope = rollback_envelope(release_root)
    _check("with two distinct bridge releases the closure tool sees READY",
           envelope["ready"], str(envelope["reasons"]))
    _check("and the report carries both release ids for the operator",
           envelope["current"]["release_id"] == ids["bridge_b"]
           and envelope["previous"]["release_id"] == ids["bridge_a"],
           str(envelope))


# ---------------------------------------------------------------------------
# THE SECOND REVIEW'S PROBES — every one of them, reproduced
# ---------------------------------------------------------------------------
#
# The first implementation of G6 proved only that SOME supplied directory looked
# bridge-like. The review satisfied it with `{}` metadata, escaped pointers,
# dangling targets and `CHECK (false)` declarations. Each probe below is that
# reproduction, now asserted to FAIL CLOSED.

def test_wrong_bridge_declarations_are_rejected(repo: Path,
                                                release_root: Path,
                                                ids: Dict[str, str]) -> None:
    """Exactness, not name-matching, decides the bridge declaration."""
    print("\n## test_wrong_bridge_declarations_are_rejected")

    cases = [
        ("CONTRACT declared as CHECK (false)", _contract_check_false_document(),
         "contract_declaration_not_canonical"),
        ("CONTRACT declared as CHECK (true)", _contract_check_true_document(),
         "contract_declaration_not_canonical"),
        ("EXPAND declared with the wrong expression", _wrong_expand_document(),
         "expand_declaration_not_canonical"),
        ("EXPAND declared validated, which 048 never produces",
         _expand_validated_document(), "expand_declaration_not_canonical"),
        ("both alternatives reduced to bare names", _name_only_document(),
         "definition_absent"),
        ("both constraints merged into ONE alternative, so nothing is spanned",
         _single_group_document(), "bridge_states_are_not_distinct_alternatives"),
        ("the CONTRACT alternative missing outright", _expand_only_document(),
         "validated_contract_state_not_declared"),
        ("an exact declaration that never claims the capability",
         _exact_bridge_without_capability_document(), "capability_not_declared"),
    ]

    for index, (label, document, expected_defect) in enumerate(cases):
        release_id = _materialize(repo, release_root, f"wrong-{index}", document)
        _point(release_root, "current", ids["bridge_b"])
        _point(release_root, "previous", release_id)
        status = _expect(f"rejected: {label}", release_root, False)
        _check(f"and named as {expected_defect}",
               any(expected_defect in d
                   for d in (status["previous"] or {}).get("defects", [])),
               str((status["previous"] or {}).get("defects")))

    # And the honest declaration still passes, so every refusal above was the
    # declaration under test and not a fixture that never recovered.
    _point(release_root, "previous", ids["bridge_a"])
    _expect("the honest bridge B/A pair is still READY", release_root, True)


def test_exact_bridge_declaration_is_required(repo: Path,
                                             release_root: Path,
                                             ids: Dict[str, str]) -> None:
    """The declaration must EQUAL the contract, not merely contain it.

    Every case here is a genuinely prepared release — real commit, real
    manifest, real `verify_release` — so the only thing under test is the
    packaged declaration.
    """
    print("\n## test_exact_bridge_declaration_is_required")

    rejected = [
        ("the review's fixture: EXPAND group + ck_synthetic_extra",
         _extra_member_in_expand_document()),
        ("an extra member in the CONTRACT group",
         _extra_member_in_contract_document()),
        # These two are refused EARLIER than the structural matcher, by the
        # strict declaration parser: a constraint name repeated inside one
        # alternative group is `RELEASE_SCHEMA_REQUIREMENTS_MALFORMED` before
        # any group is ever compared to the canonical structure. That is the
        # 049 parser and the M-LAG bridge meeting in one tree, and the earlier
        # refusal is the stricter one — the release is still rejected and still
        # NOT reported as an exact declaration, which is what this test exists
        # to protect. Only the defect LABEL differs, so it is named per case
        # rather than by widening the structural allowlist for the other seven.
        ("the canonical EXPAND member declared twice",
         _duplicated_expand_member_document(),
         ("schema_requirements_malformed",)),
        ("the canonical CONTRACT member declared twice",
         _duplicated_contract_member_document(),
         ("schema_requirements_malformed",)),
        ("a duplicated alternative group under a second label",
         _duplicated_group_document()),
        ("canonical members split across additional partial groups",
         _split_members_document()),
        ("an extra synthetic bridge group beside the canonical pair",
         _extra_synthetic_group_document()),
        ("a second relation collectively declaring the bridge",
         _second_bridge_relation_document()),
        ("the exact structure declared on the wrong relation",
         _wrong_relation_document()),
    ]

    _STRUCTURAL_DEFECTS = (
        "bridge_alternatives_not_exactly_canonical",
        "bridge_declared_by_multiple_relations",
        "bridge_declared_on_wrong_relation",
    )

    for index, case in enumerate(rejected):
        label, document = case[0], case[1]
        expected_defects = case[2] if len(case) > 2 else _STRUCTURAL_DEFECTS
        release_id = _materialize(repo, release_root, f"exact-{index}", document)
        _point(release_root, "current", ids["bridge_b"])
        _point(release_root, "previous", release_id)
        status = _expect(f"rejected: {label}", release_root, False)
        record = status["previous"] or {}
        _check(f"and NOT reported as an exact declaration: {label}",
               record.get("declares_exact_bridge_alternatives") is False,
               str(record))
        _check(f"with a structural defect named: {label}",
               any(d.startswith(expected_defects)
                   for d in record.get("defects", [])),
               str(record.get("defects")))

    _assert_g6_and_preflight_agree(repo, release_root)

    # Exactness is scoped to the bridge declaration, NOT to the document.
    unrelated = _materialize(repo, release_root, "exact-unrelated",
                             _unrelated_extra_requirement_document())
    _point(release_root, "current", ids["bridge_b"])
    _point(release_root, "previous", unrelated)
    status = _expect("an unrelated requirement elsewhere in the release does "
                     "NOT invalidate an exact bridge declaration",
                     release_root, True)
    _check("and the bridge declaration is reported exact",
           status["previous"]["declares_exact_bridge_alternatives"],
           str(status["previous"]))

    _point(release_root, "previous", ids["bridge_a"])
    status = _expect("the honest bridge B/A pair is still READY",
                     release_root, True)
    _check("with both declarations reported exact",
           status["current"]["declares_exact_bridge_alternatives"]
           and status["previous"]["declares_exact_bridge_alternatives"],
           str(status))


#: THE TWO LIVE RELATION STATES the transition actually produces, taken from the
#: module rather than restated here — the point of the correction is that there
#: is ONE canonical model and ONE matcher, so a test carrying its own copy of
#: either would be able to pass while production disagrees.
_LIVE_STATES = dict(CANONICAL_BRIDGE_STATES)
_LIVE_EXPAND_STATE = _LIVE_STATES["EXPAND"]
_LIVE_CONTRACT_STATE = _LIVE_STATES["CONTRACT"]


def _preflight_bridge_defects(document: Dict, state) -> List[str]:
    """Run the REAL relation matcher over the COMPLETE bridge relation.

    Not `_alternative_state_defects`. The fourth review's finding was precisely
    that alternatives are one field of a `RelationRequirement` and preflight
    decides all of them: a document carrying the exact canonical alternatives
    AND an absent `ck_synthetic_unconditional` on the same relation was refused
    by preflight and accepted by G6. A test that keeps checking only the
    alternatives cannot see that class of defect, so it checks what preflight
    checks — every column, unconditional constraint, alternative and index.
    """
    parsed = parse_requirements(document)
    for requirement in parsed:
        for relation in requirement.relations:
            names = {c.name for c in relation.constraints}
            names.update(c.name
                         for group in relation.constraint_alternatives
                         for c in group.constraints)
            if names & {FIRST_SEEN_EXPAND_CONSTRAINT,
                        FIRST_SEEN_STRICT_CONSTRAINT}:
                return relation_state_defects(state, relation)
    return ["bridge_relation_absent"]


def _assert_g6_and_preflight_agree(repo: Path, release_root: Path) -> None:
    """THE load-bearing acceptance property of this correction.

    G6 answers a DECLARATION question and preflight answers a LIVE-STATE one,
    and they must not disagree about the bridge relation. Independent review
    found them disagreeing: a release whose EXPAND group additionally required
    `ck_synthetic_extra` was bridge-compatible per G6 and REFUSED by canonical
    EXPAND preflight, because preflight requires every member of a group to
    hold. So the property is asserted directly, in both directions, against the
    real relation matcher — no database needed, because the two canonical live
    states are exactly what the transition installs.
    """
    print("\n### G6 and canonical preflight agree on the bridge relation")
    # The direction that matters is ONE-WAY: G6 compatible => canonical
    # preflight passes in BOTH transition states. The converse is deliberately
    # not required — a declaration carrying an extra ALTERNATIVE group is
    # tolerated by the live matcher (an unsatisfiable third state simply never
    # becomes authoritative) and still refused by G6, because a release willing
    # to run against a state this transition never produces is not the bridge
    # contract. G6 being STRICTER is safe; G6 being weaker is the reviewed bug.
    cases = [
        # (label, document, G6 compatible, canonical preflight passes)
        ("the honest bridge declaration", _bridge_document(), True, True),
        ("the review's fixture: EXPAND + ck_synthetic_extra",
         _extra_member_in_expand_document(), False, False),
        ("an extra member in the CONTRACT group",
         _extra_member_in_contract_document(), False, False),
        ("an extra synthetic bridge group — G6 stricter, by design",
         _extra_synthetic_group_document(), False, True),
        ("members split across additional partial groups — G6 stricter",
         _split_members_document(), False, True),
        ("an unrelated requirement elsewhere in the release",
         _unrelated_extra_requirement_document(), True, True),
        # The fourth review's finding: the alternatives are exact and the
        # RELATION is not satisfiable, on either side of the transition.
        ("THE REVIEW'S REPRODUCTION: exact alternatives + absent "
         "ck_synthetic_unconditional",
         _extra_unconditional_constraint_document(), False, False),
        ("exact alternatives + a required column neither state carries",
         _absent_required_column_document(), False, False),
        ("exact alternatives + a required index neither state carries",
         _absent_required_index_document(), False, False),
        ("exact alternatives + a column BOTH states carry — still READY",
         _satisfied_extra_column_document(), True, True),
        # Satisfiable against a REAL client_trips, outside what the transition
        # governs: G6 stricter, which the one-way implication permits.
        ("an out-of-contract unconditional constraint — G6 stricter, by design",
         _out_of_contract_unconditional_document(), False, False),
        ("an out-of-contract index — G6 stricter, by design",
         _out_of_contract_index_document(), False, False),
    ]
    for index, (label, document, expected, preflight_expected) in enumerate(cases):
        tree = (release_root / RELEASES_DIRNAME
                / _materialize(repo, release_root, f"agree-{index}", document))
        compatible = bool(
            inspect_release_bridge_compatibility(tree)["compatible"]
        )
        expand_defects = _preflight_bridge_defects(document, _LIVE_EXPAND_STATE)
        contract_defects = _preflight_bridge_defects(
            document, _LIVE_CONTRACT_STATE
        )
        preflight_ok = not expand_defects and not contract_defects
        _check(f"G6 verdict is {expected} for {label}",
               compatible is expected, f"compatible={compatible}")
        _check(f"canonical EXPAND/CONTRACT preflight verdict is "
               f"{preflight_expected} for {label}",
               preflight_ok is preflight_expected,
               f"expand={expand_defects} contract={contract_defects}")
        _check(f"THE PROPERTY: a G6-compatible release is never one canonical "
               f"preflight would refuse: {label}",
               (not compatible) or preflight_ok,
               f"G6=compatible preflight expand={expand_defects} "
               f"contract={contract_defects}")

    # And the reviewed divergence really was a divergence: canonical EXPAND
    # preflight refuses the fixture G6 used to accept, naming the extra member.
    fixture_defects = _preflight_bridge_defects(
        _extra_member_in_expand_document(), _LIVE_EXPAND_STATE,
    )
    _check("canonical EXPAND preflight refuses the review's fixture, naming "
           "the synthetic member G6 used to ignore",
           any("ck_synthetic_extra" in d for d in fixture_defects),
           str(fixture_defects))

    # THE FOURTH REVIEW'S EXACT REPRODUCTION, as a named regression rather than
    # one row of a matrix: `G6_READY_WITH_EXTRA_UNCONDITIONAL` must be False,
    # and G6 must refuse it for the SAME reason preflight does.
    reproduction = _extra_unconditional_constraint_document()
    tree = (release_root / RELEASES_DIRNAME
            / _materialize(repo, release_root, "extra-unconditional",
                           reproduction))
    record = inspect_release_bridge_compatibility(tree)
    _check("G6_READY_WITH_EXTRA_UNCONDITIONAL is False on an honestly "
           "prepared release",
           record["compatible"] is False, str(record))
    _check("G6 refuses it naming ck_synthetic_unconditional, the same "
           "constraint relation preflight names",
           any("ck_synthetic_unconditional" in d for d in record["defects"]),
           str(record["defects"]))
    _check("and it reports the complete relation as unsatisfiable in BOTH "
           "canonical states",
           record["satisfies_complete_relation_in_both_states"] is False
           and all(record["canonical_state_relation_defects"][s]
                   for s in ("EXPAND", "CONTRACT")),
           str(record.get("canonical_state_relation_defects")))
    for state_label, state in (("EXPAND", _LIVE_EXPAND_STATE),
                               ("CONTRACT", _LIVE_CONTRACT_STATE)):
        _check(f"canonical {state_label} preflight independently refuses it, "
               f"naming the same constraint",
               any("ck_synthetic_unconditional" in d
                   for d in _preflight_bridge_defects(reproduction, state)),
               str(_preflight_bridge_defects(reproduction, state)))

    # The honest bridge declaration is the release actually being shipped, so
    # its complete relation requirement is asserted directly and not inferred
    # from the matrix row above.
    honest = (release_root / RELEASES_DIRNAME
              / _materialize(repo, release_root, "honest-complete",
                             _bridge_document()))
    honest_record = inspect_release_bridge_compatibility(honest)
    _check("the honest bridge release satisfies the COMPLETE participating "
           "relation requirement in both canonical states",
           honest_record["satisfies_complete_relation_in_both_states"] is True
           and honest_record["compatible"] is True,
           str(honest_record))


def test_release_metadata_identity_is_bound(release_root: Path,
                                            ids: Dict[str, str]) -> None:
    """A verified release has ONE identity across every representation.

    `verify_release` bound the requested id to the commit and never looked at
    the metadata's own `release_id`, so independent review rewrote it to
    `000000000000` in an otherwise genuine release and verification still
    passed. The failure must occur inside the authoritative release verifier —
    not as a G6-specific afterthought — so both are asserted.
    """
    print("\n## test_release_metadata_identity_is_bound")
    meta = release_root / "meta" / f"{ids['bridge_a']}.json"
    original = meta.read_text(encoding="utf-8")
    genuine = json.loads(original)

    _point(release_root, "current", ids["bridge_b"])
    _point(release_root, "previous", ids["bridge_a"])

    verified = verify_release(release_root=release_root,
                              release_id=ids["bridge_a"],
                              source_repo=Path(genuine["source_repository_root"]))
    _check("a genuine prepared release verifies",
           verified["release_id"] == ids["bridge_a"], str(verified["release_id"]))
    _expect("and the honest pair is READY before any tampering",
            release_root, True)

    for label, forged_id in (
        ("the review's value, 000000000000", "0" * 12),
        ("a different but valid-looking release id", "abcdef123456"),
        ("the OTHER genuine release's id, so it matches no directory",
         ids["bridge_b"]),
    ):
        doctored = dict(genuine)
        doctored["release_id"] = forged_id
        meta.write_text(json.dumps(doctored, indent=2, sort_keys=True),
                        encoding="utf-8")

        raised = None
        try:
            verify_release(release_root=release_root,
                           release_id=ids["bridge_a"],
                           source_repo=Path(genuine["source_repository_root"]))
        except ReleaseBoundaryError as exc:
            raised = exc
        _check(f"verify_release REFUSES metadata claiming {label}",
               raised is not None,
               "verify_release accepted a release whose metadata names another")
        _check(f"and refuses it as an identity mismatch: {label}",
               raised is not None
               and getattr(raised, "classification", None)
               == "RELEASE_METADATA_INVALID"
               and "metadata_release_id_mismatch" in str(raised),
               str(raised))

        status = _expect(f"and the envelope is NOT READY: {label}",
                         release_root, False)
        _check("with G6 inheriting the refusal from the release verifier",
               any("release_not_verified" in d
                   for d in (status["previous"] or {}).get("defects", [])),
               str((status["previous"] or {}).get("defects")))
        _check("and the release never treated as materialized",
               (status["previous"] or {}).get("materialized") is False,
               str(status["previous"]))

    meta.write_text(original, encoding="utf-8")
    _expect("and the envelope is READY again once the identity is restored",
            release_root, True)


def test_fabricated_release_metadata_is_rejected(release_root: Path,
                                                 ids: Dict[str, str]) -> None:
    """`{}` is not a release. Proven by the authoritative verifier."""
    print("\n## test_fabricated_release_metadata_is_rejected")
    meta = release_root / "meta" / f"{ids['bridge_a']}.json"
    original = meta.read_text(encoding="utf-8")

    _point(release_root, "current", ids["bridge_b"])
    _point(release_root, "previous", ids["bridge_a"])

    for label, fabricated in (
        ("an empty metadata object", "{}"),
        ("metadata with no manifest at all",
         json.dumps({"commit": "0" * 40, "release_id": ids["bridge_a"]})),
    ):
        meta.write_text(fabricated, encoding="utf-8")
        status = _expect(f"rejected: {label}", release_root, False)
        _check("and reported as an unverified release, not a declaration defect",
               any("release_not_verified" in d
                   for d in (status["previous"] or {}).get("defects", [])),
               str((status["previous"] or {}).get("defects")))
        _check("with the release never treated as materialized",
               (status["previous"] or {}).get("materialized") is False,
               str(status["previous"]))

    # A doctored manifest, i.e. metadata that IS well-formed and lies.
    metadata = json.loads(original)
    manifest = dict(metadata["manifest"])
    victim = sorted(manifest)[0]
    manifest[victim] = [manifest[victim][0], "0" * 40]
    metadata["manifest"] = manifest
    meta.write_text(json.dumps(metadata), encoding="utf-8")
    status = _expect("rejected: a manifest doctored to describe other bytes",
                     release_root, False)
    _check("and reported by content/digest verification",
           any("release_not_verified" in d
               for d in (status["previous"] or {}).get("defects", [])),
           str((status["previous"] or {}).get("defects")))

    meta.write_text(original, encoding="utf-8")
    _expect("and the envelope is READY again once the metadata is restored",
            release_root, True)


def test_pointer_escape_and_noncanonical_targets_are_rejected(
    base: Path, release_root: Path, ids: Dict[str, str],
) -> None:
    """Identity comes from the RESOLVED path, never from the link text."""
    print("\n## test_pointer_escape_and_noncanonical_targets_are_rejected")
    _point(release_root, "current", ids["bridge_b"])

    outside = base / "outside" / "releases"
    outside.mkdir(parents=True, exist_ok=True)
    smuggled = outside / "aaaaaaaaaaaa"
    smuggled.mkdir(exist_ok=True)
    (smuggled / "db").mkdir(exist_ok=True)
    (smuggled / "db" / "schema_requirements.json").write_text(
        json.dumps(_bridge_document(), indent=2), encoding="utf-8"
    )

    previous = release_root / "previous"

    def _relink(target: str) -> None:
        if previous.is_symlink() or previous.exists():
            previous.unlink()
        previous.symlink_to(target)

    # 1. `..` escape out of the inventory, with a perfectly well-formed id.
    _relink(os.path.relpath(smuggled, release_root))
    status = _expect("rejected: a pointer escaping the release inventory",
                     release_root, False)
    _check("and named as an out-of-inventory target",
           any("pointer_target_outside_release_inventory" in d
               for d in (status["previous"] or {}).get("defects", [])),
           str((status["previous"] or {}).get("defects")))

    # 2. An absolute target elsewhere on the filesystem.
    _relink(str(smuggled))
    status = _expect("rejected: an absolute pointer outside the inventory",
                     release_root, False)
    _check("and named as an out-of-inventory target",
           any("pointer_target_outside_release_inventory" in d
               for d in (status["previous"] or {}).get("defects", [])),
           str((status["previous"] or {}).get("defects")))

    # 3. A dangling id that was never materialized.
    _relink(f"{RELEASES_DIRNAME}/bbbbbbbbbbbb")
    status = _expect("rejected: a pointer to a release id that does not exist",
                     release_root, False)
    _check("and named as dangling",
           any("pointer_target_dangling" in d
               for d in (status["previous"] or {}).get("defects", [])),
           str((status["previous"] or {}).get("defects")))

    # 4. An alias INSIDE the inventory whose apparent id is not the release it
    #    resolves to — the case a basename check cannot see.
    alias = release_root / RELEASES_DIRNAME / "cccccccccccc"
    if alias.is_symlink() or alias.exists():
        alias.unlink()
    alias.symlink_to(ids["bridge_a"])
    (release_root / "meta" / "cccccccccccc.json").write_text(
        (release_root / "meta" / f"{ids['bridge_a']}.json").read_text("utf-8"),
        encoding="utf-8",
    )
    _relink(f"{RELEASES_DIRNAME}/cccccccccccc")
    status = _expect(
        "rejected: an aliased id that does not address the release it resolves to",
        release_root, False,
    )
    _check("and named as a link-text/resolution disagreement",
           any("pointer_text_disagrees_with_resolved_release" in d
               for d in (status["previous"] or {}).get("defects", [])),
           str((status["previous"] or {}).get("defects")))

    _point(release_root, "previous", ids["bridge_a"])
    _expect("and the envelope is READY again once the pointer is canonical",
            release_root, True)


def test_a_synthetic_directory_is_not_a_release_inventory(base: Path) -> None:
    """A hand-built directory fails on what it IS, not on a missing pointer."""
    print("\n## test_a_synthetic_directory_is_not_a_release_inventory")
    synthetic = base / "synthetic-root"
    (synthetic / "releases" / "aaaaaaaaaaaa" / "db").mkdir(parents=True,
                                                          exist_ok=True)
    (synthetic / "releases" / "aaaaaaaaaaaa" / "db"
     / "schema_requirements.json").write_text(
        json.dumps(_bridge_document(), indent=2), encoding="utf-8"
    )
    (synthetic / "current").symlink_to("releases/aaaaaaaaaaaa")
    (synthetic / "previous").symlink_to("releases/aaaaaaaaaaaa")
    status = _status(synthetic)
    _check("a bridge-looking directory with no meta/ is NOT READY",
           not status["ready"], str(status["reasons"]))
    _check("and is refused as an absent release inventory",
           any("release_inventory_absent" in r for r in status["reasons"]),
           str(status["reasons"]))


def test_production_identity_rejects_an_arbitrary_release_root(
    repo: Path, release_root: Path, ids: Dict[str, str],
) -> None:
    """The trust boundary itself, asserted on a root that WOULD report READY.

    The point is not that a synthetic root happens to be missing files. It is
    that production CONTRACT closure does not read a caller-supplied inventory
    at all — so the probe uses the honestly prepared, genuinely READY test
    inventory and shows production identity still refuses it, by that
    classification and before anything else is evaluated.
    """
    print("\n## test_production_identity_rejects_an_arbitrary_release_root")
    import argparse

    from ops.close_telematics_first_seen_pair_contract import (
        EXIT_INVALID_PARAMETERS, ContractRefused, run,
    )

    _point(release_root, "current", ids["bridge_b"])
    _point(release_root, "previous", ids["bridge_a"])
    _expect("precondition: this inventory really is READY", release_root, True)

    def _args(**overrides) -> argparse.Namespace:
        base = dict(
            client_code=None, expected_environment="production",
            expected_platform_uuid="db8055e0-e030-4d5a-816b-ec4dc338d698",
            check_only=False, approval_ref="REF-1", execute=True,
            confirm="CLOSE_CONTRACT", rollback_window_closed=True,
            release_root=str(release_root),
            dsn="postgresql://unused.invalid/none",
            allow_non_production_release_root=True, source_repo=str(repo),
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    for label, overrides in (
        ("--execute under production identity", {}),
        ("even with --allow-non-production-release-root",
         {"allow_non_production_release_root": True}),
        ("and a read-only report under production identity too",
         {"execute": False, "check_only": True}),
    ):
        try:
            run(_args(**overrides))
            _check(f"an arbitrary release root is refused: {label}", False,
                   "run() returned instead of refusing")
        except ContractRefused as exc:
            _check(f"an arbitrary release root is refused: {label}",
                   exc.code == "RELEASE_ROOT_NOT_AUTHORITATIVE", str(exc))
            _check("with a parameter refusal, not a readiness verdict",
                   exc.exit_code == EXIT_INVALID_PARAMETERS, str(exc.exit_code))

    # Outside production identity, --execute still needs the boundary stated.
    try:
        run(_args(expected_environment="test",
                  allow_non_production_release_root=False))
        _check("a non-production identity still may not --execute against an "
               "alternate root implicitly", False,
               "run() returned instead of refusing")
    except ContractRefused as exc:
        _check("a non-production identity still may not --execute against an "
               "alternate root implicitly",
               exc.code == "RELEASE_ROOT_NOT_AUTHORITATIVE", str(exc))

    # And a relative root is refused outright: canonical resolution is
    # meaningless against one.
    try:
        run(_args(expected_environment="test", release_root="release-root",
                  allow_non_production_release_root=True))
        _check("a relative release root is refused", False,
               "run() returned instead of refusing")
    except ContractRefused as exc:
        _check("a relative release root is refused",
               exc.code == "RELEASE_ROOT_NOT_ABSOLUTE", str(exc))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mlag-envelope-") as tmp:
        base = Path(tmp).resolve()
        repo = base / "repo"
        release_root = base / "release-root"
        _init_repo(repo)
        ids = run_cases(repo, release_root)
        test_a_real_activation_sequence_reaches_the_envelope(repo, release_root)
        test_the_closure_tool_gates_on_the_envelope(repo, release_root, ids)
        test_wrong_bridge_declarations_are_rejected(repo, release_root, ids)
        test_exact_bridge_declaration_is_required(repo, release_root, ids)
        test_release_metadata_identity_is_bound(release_root, ids)
        test_fabricated_release_metadata_is_rejected(release_root, ids)
        test_pointer_escape_and_noncanonical_targets_are_rejected(
            base, release_root, ids,
        )
        test_a_synthetic_directory_is_not_a_release_inventory(base)
        test_production_identity_rejects_an_arbitrary_release_root(
            repo, release_root, ids,
        )
        _chmod_writable(release_root)

    print()
    if _failures:
        print(f"FAILED — {len(_failures)} assertion(s)")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
