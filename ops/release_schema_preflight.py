#!/usr/bin/env python3
"""Release-pinned schema prerequisite gate for release activation.

WHY THIS EXISTS.
    `ops/manage_release.py activate` verified release *bytes* and nothing else.
    A release whose code references a database object that does not exist yet
    would therefore activate cleanly and then fail at runtime, fleet-wide.

    M4 made that concrete and independent review classified it as blocking: the
    trip upsert names `client_trips.first_seen_request_id` unconditionally, so
    a single client business database missing migration 047 cannot ingest at
    all, and `workflow_a_control.provider_request_log` must exist before the
    dispatcher can finalize any coverage advance.

WHAT IT CHECKS, AND WHERE THE REQUIREMENTS COME FROM.
    From `db/schema_requirements.json` **inside the release being activated** —
    never the working copy. The release therefore declares its own
    prerequisites, and:

      * a release that predates a migration does not carry the requirement, so
        activating or rolling back to it is never incorrectly blocked;
      * a release that needs a migration cannot activate without it;
      * adding a future prerequisite is a data edit, not a code change.

    A release with no requirements file predates the mechanism entirely and
    passes its REQUIREMENT checks with that reason recorded, which is what keeps
    rollback usable.

WHY THAT DIRECTION IS NOT SUFFICIENT ON ITS OWN.
    A release-declared requirement can only express "I need this to exist". It
    cannot express "this database has narrowed, and code written before the
    narrowing is no longer safe against it" — because the release that must be
    refused is precisely the one that predates the narrowing and declares
    nothing. Closing the client first-seen pair CONTRACT is exactly that: the
    strict pairing CHECK rejects the M4-era writer's request-only inserts, so
    that release would activate cleanly and fail on its first production INSERT.

    `SCHEMA_STATE_GUARDS` inverts the direction for those states. The database
    is read for the narrowing state, and a release may only activate if it
    DECLARES the matching capability in the `capabilities` array of its own
    requirements file. Absence of the declaration is a refusal, so the guard is
    effective against every release built before the key existed. Guards are
    evaluated even when the release declares no requirement at all.

WHY ALTERNATIVE STATES EXIST, AND WHY THEY ARE EXCLUSIVE.
    A relation may declare `constraint_alternatives`: several exactly-specified
    acceptable states, of which EXACTLY ONE must hold. That is what lets one
    release be activatable on both sides of an expand-contract DDL step, and
    therefore what keeps rollback possible across it. It narrows nothing else —
    each alternative still pins constraint name, canonical definition and
    validation status.

    "Exactly one", not "at least one". An alternative is authoritative only when
    it holds in full AND no constraint belonging to a competing declared
    alternative is present in the catalog at all. Accepting the first matching
    state let a valid EXPAND constraint coexisting with a malformed strict
    CONTRACT constraint pass the gate — and that malformed CHECK rejects writes
    at runtime, so the gate had authorized an activation against a schema state
    no alternative describes. Exclusivity is scoped to the constraint names the
    alternatives themselves name; an unrelated constraint is never grounds for
    refusal.

WHY LEDGER *AND* PHYSICAL.
    A migration ledger records intent, and intent is exactly what a broken
    rollout leaves looking correct. Every requirement is checked twice — the
    filename in `public.schema_migrations`, and the actual relation, columns and
    constraints in `information_schema`/`pg_constraint`. Disagreement between
    the two is itself a refusal, because it means something is lying.

FAIL-CLOSED, WITHOUT EXCEPTIONS.
    * no filter parameter exists, so a client can never be excluded from the
      fleet check by an operator's convenience flag;
    * an empty affected fleet while enabled client accounts exist is a refusal,
      not a vacuous pass;
    * an unreachable client is a refusal, never a skip;
    * a ledger hit with a missing physical object is a refusal;
    * anything unexpected raises.

READ-ONLY.
    Every statement here is a SELECT. This module never writes to any database,
    never creates the ledger table it reads, and never moves a pointer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: This repository, used as the default source repository when a release
#: manifest is cross-checked against Git. An unreachable commit is reported
#: rather than fatal, so this degrades correctly when the module itself is
#: executing from inside a materialized release tree.
REPO_ROOT = Path(__file__).resolve().parents[1]

SCHEMA_REQUIREMENTS_RELPATH = "db/schema_requirements.json"
SCHEMA_REQUIREMENTS_VERSION = "log-platform-schema-requirements/1"

SCOPE_PLATFORM = "platform"
SCOPE_CLIENT_BUSINESS = "client_business"
KNOWN_SCOPES = frozenset({SCOPE_PLATFORM, SCOPE_CLIENT_BUSINESS})

#: Mode used to fence the authoritative fleet across pointer activation.
#: `SHARE` conflicts with `ROW EXCLUSIVE` — the mode every INSERT/UPDATE/DELETE
#: acquires — so it blocks writes from ANY session, including raw SQL that knows
#: nothing about this module. It does not conflict with `ACCESS SHARE`, so plain
#: reads are unaffected. This deliberately replaces an advisory lock, which only
#: constrained code that volunteered to take it.
FLEET_FENCE_LOCK_MODE = "SHARE"

#: Platform advisory-lock key serialising RELEASE ACTIVATION against CLIENT
#: SCHEMA-STATE TRANSITIONS. Fixed, arbitrary, and never reused elsewhere.
#:
#: WHY A SECOND PRIMITIVE IS NEEDED AT ALL.
#:     `FLEET_FENCE_LOCK_MODE` fences the authoritative fleet *table*, which is
#:     the right primitive for "membership must not change" and the wrong one
#:     for "no client may cross the EXPAND -> CONTRACT boundary right now". The
#:     narrowing is DDL in a client business database; it writes nothing in the
#:     platform database and therefore takes no lock the fleet fence conflicts
#:     with. Independent review reproduced the consequence directly: a CONTRACT
#:     closure committed inside the activation fence of a legacy release whose
#:     capability check had already passed, with the pointer swap still to come.
#:
#: WHY AN ADVISORY LOCK IS THE RIGHT ANSWER HERE, HAVING BEEN THE WRONG ONE THERE.
#:     An advisory lock constrains only code that volunteers to take it. For the
#:     fleet that was disqualifying, because ANY session can `UPDATE
#:     client_account` and none of them would volunteer. Here the constrained
#:     population is exactly two repository tools — `ops/manage_release.py
#:     activate` and `ops/close_telematics_first_seen_pair_contract.py --execute`
#:     — and no third party can perform this transition, because performing it
#:     *is* running the closure tool. The alternative, holding a lock in every
#:     client business database across the fleet for the duration of an
#:     activation, is distributed locking bought for no additional guarantee.
#:
#:     Both sides take this ONE key in the platform database, and both hold it
#:     across their whole critical interval:
#:
#:       activation  advisory lock -> fleet SHARE lock -> authoritative
#:                   state-guard re-read -> POINTER SWAP -> release
#:       closure     advisory lock -> client DDL swap/validate/ledger -> release
#:
#:     The acquisition order is identical on both sides, so the two cannot
#:     deadlock, and whichever wins the lock forces the other to observe its
#:     committed outcome rather than a stale one.
SCHEMA_TRANSITION_LOCK_KEY = 728503746327118002


class SchemaPreflightError(RuntimeError):
    """A refused activation prerequisite. Stable code, sanitized detail."""

    def __init__(self, code: str, detail: str, **context: Any) -> None:
        self.code = code
        self.detail = str(detail)[:500]
        self.context = {k: v for k, v in context.items() if v is not None}
        super().__init__(f"{code}: {self.detail}")


# ---------------------------------------------------------------------------
# Requirements, read from the release tree
# ---------------------------------------------------------------------------

def _normalize_definition(text: str) -> str:
    """Compare canonical SQL robustly, not by source formatting.

    `pg_get_constraintdef` and `pg_indexes.indexdef` are already canonicalized by
    PostgreSQL — it reprints from the parsed catalog entry, not from whatever the
    migration typed — so two databases carrying the same constraint always render
    it identically. The only difference a requirements file can plausibly carry is
    incidental whitespace from being wrapped in JSON, so that is all this
    normalizes. Nothing is lower-cased and no token is rewritten: a genuinely
    different expression must stay different.
    """
    return " ".join(str(text).split())


#: The two states a column-default requirement can ASSERT. The third state —
#: "this requirement does not check the default" — is the ABSENCE of a
#: `ColumnDefaultRequirement`, so all three are distinguishable and none of
#: them is spelled the same way as another.
DEFAULT_STATE_ABSENT = "absent"
DEFAULT_STATE_EXPRESSION = "expression"
KNOWN_DEFAULT_STATES = frozenset({DEFAULT_STATE_ABSENT, DEFAULT_STATE_EXPRESSION})


@dataclass(frozen=True)
class ColumnDefaultRequirement:
    """What a requirement asserts about ONE column's DEFAULT.

    THE DEFECT THIS TYPE ANSWERS. `default` used to be `Optional[str]`, and
    `None` had to mean two incompatible things at once: "this requirement does
    not check the default" and "this column must have no default". Only the
    first was implemented, so the second was unstateable — and independent
    review built exactly the database that gap accepts: migration 049 recorded,
    `provider_name` given `DEFAULT 'fake'`, physical preflight GREEN, and
    `DeliveryLedger.ensure_operation()` then refused by
    `chk_..._binding_coherent`, because its narrow INSERT names neither
    `provider_name` nor the five columns bound with it and depends on every one
    of them staying implicitly NULL.

    An absent default is therefore load-bearing schema, exactly like a present
    one, and it is now declared as such:

        `DEFAULT_STATE_ABSENT`      the column must physically carry no default;
        `DEFAULT_STATE_EXPRESSION`  it must carry exactly `expression`.

    `expression` is compared against `information_schema.columns.column_default`,
    which PostgreSQL reprints from the parsed catalog entry — so an equivalent
    but differently-typed source form still compares equal to itself, while a
    materially different expression stays different.
    """

    state: str
    expression: Optional[str] = None


@dataclass(frozen=True)
class ColumnRequirement:
    """One column the release needs, with the attributes that are load-bearing.

    `type` and `nullable` are the M4/M5 form. `default` was added for migration
    049, where it is not decorative in EITHER direction:
    `DeliveryLedger.ensure_operation()` INSERTs a narrow column list and relies
    on the table default for every other NOT NULL column — `delivery_id`, the
    counters, `operator_action_required`, the timestamps and `metadata_json` —
    and it relies just as hard on every column it does NOT name having no
    default at all. A database whose defaults were stripped, or given one the
    migration never created, accepts the DDL, records the migration and then
    refuses the only INSERT the publisher ever issues.

    `nullable` of `None` means "not asserted". `default` of `None` means the
    same and ONLY that: "must have no default" is `ColumnDefaultRequirement`
    with `DEFAULT_STATE_ABSENT`, which is a different value, never `None`.
    """

    name: str
    type: str
    nullable: Optional[bool] = None
    default: Optional["ColumnDefaultRequirement"] = None


@dataclass(frozen=True)
class ConstraintRequirement:
    """One constraint the release needs, optionally by DEFINITION.

    A name-only requirement (the M4 form) proves a constraint with that name
    exists. That was sufficient while the only failure mode being guarded was a
    missing migration. It is NOT sufficient against schema drift: a same-named
    constraint over the wrong columns, or a CHECK with the wrong expression,
    passes a name check and then breaks the release at runtime — which is the
    exact class of "the ledger looks correct" failure this gate exists to catch.

    Supplying `definition` compares against `pg_get_constraintdef`, whose output
    PostgreSQL canonicalizes, so the comparison is robust rather than a string
    match against the migration's source formatting. `validated` additionally
    rejects a constraint that exists but is NOT VALID.

    `validated` defaults to True but MUST be set False for a constraint a
    migration deliberately leaves NOT VALID. Note what NOT VALID does and does
    not mean: PostgreSQL still enforces it on every new INSERT and UPDATE, and
    only skips the one-off verification scan of pre-existing rows. So a NOT VALID
    constraint is not "not enforcing anything" — it is enforcing the rule going
    forward while declining to prove the past. That is exactly the state an
    expand-contract rollout wants during its expand window, because the
    verification scan would otherwise run under the ACCESS EXCLUSIVE lock the
    migration's own `ADD COLUMN` already holds
    (`db/client_business/048_client_trips_first_seen_response_received_at.sql`).
    """

    name: str
    definition: Optional[str] = None
    validated: bool = True


@dataclass(frozen=True)
class ConstraintAlternativeGroup:
    """One named schema STATE, satisfied only if all of its constraints hold.

    A relation may declare several of these, and the relation is compatible when
    **at least one** group holds exactly. That is the smallest representation
    that lets a single release span a migration boundary: during an
    expand-contract rollout the same code is correct against the EXPAND
    constraint and against the CONTRACT one, and it must be activatable — and
    rollback-able — on either side of the DDL step. Without it the contract
    closure would create a deadlock: every release declaring the EXPAND
    constraint stops being activatable the moment it is dropped, while a release
    declaring only the strict constraint cannot be activated before it exists.

    This is a compatibility declaration, not a relaxation. Every group states
    exact constraint names, exact canonical definitions and exact validation
    status, and a state that matches no group is a refusal — including a
    same-named constraint carrying a different definition. `state` is a label
    for diagnostics only; it is never matched against the database.
    """

    state: str
    constraints: Tuple["ConstraintRequirement", ...]


@dataclass(frozen=True)
class IndexRequirement:
    """One index the release needs, by canonical definition.

    Indexes are separate from constraints because the load-bearing M5 shapes —
    the base-role uniqueness and both recovery exclusivity keys — are PARTIAL
    unique indexes, which have no `pg_constraint` row at all. A requirement model
    that could only name constraints would silently omit them.
    """

    name: str
    definition: str


#: `pg_trigger.tgtype` bit meanings, from PostgreSQL's own `TRIGGER_TYPE_*`
#: macros. Decoded rather than compared as a number so a defect names the thing
#: that actually differs, and so the requirement file can state the trigger in
#: the words the migration used.
TRIGGER_TYPE_ROW = 1 << 0
TRIGGER_TYPE_BEFORE = 1 << 1
TRIGGER_TYPE_INSERT = 1 << 2
TRIGGER_TYPE_DELETE = 1 << 3
TRIGGER_TYPE_UPDATE = 1 << 4
TRIGGER_TYPE_TRUNCATE = 1 << 5
TRIGGER_TYPE_INSTEAD = 1 << 6

TRIGGER_EVENT_BITS: Tuple[Tuple[str, int], ...] = (
    ("INSERT", TRIGGER_TYPE_INSERT),
    ("UPDATE", TRIGGER_TYPE_UPDATE),
    ("DELETE", TRIGGER_TYPE_DELETE),
    ("TRUNCATE", TRIGGER_TYPE_TRUNCATE),
)

#: `pg_trigger.tgenabled` values that still fire in an ordinary session.
#: 'O' = origin (the default), 'A' = always. 'D' is disabled and 'R' fires only
#: under `session_replication_role = replica`, which the publisher never sets —
#: both would silently remove the guard while leaving its catalog row in place.
TRIGGER_ENABLED_STATES = frozenset({"O", "A"})


def _decode_trigger_type(tgtype: int) -> Tuple[str, Tuple[str, ...], str]:
    """`(timing, events, level)` from a `pg_trigger.tgtype` bitmask."""
    value = int(tgtype)
    if value & TRIGGER_TYPE_INSTEAD:
        timing = "INSTEAD OF"
    elif value & TRIGGER_TYPE_BEFORE:
        timing = "BEFORE"
    else:
        timing = "AFTER"
    events = tuple(name for name, bit in TRIGGER_EVENT_BITS if value & bit)
    level = "ROW" if value & TRIGGER_TYPE_ROW else "STATEMENT"
    return timing, events, level


@dataclass(frozen=True)
class TriggerRequirement:
    """One trigger the release needs, verified by BINDING and not by name.

    THE DEFECT THIS TYPE ANSWERS. Migration 049's guard trigger is the only
    place two invariants are enforced at all — the provider submission identity
    being immutable once bound, and the raw bearer the OLD row held not being
    copyable into a diagnostic column while the same statement clears it. A
    CHECK constraint structurally cannot see the OLD row, so nothing else in
    the schema covers them. A requirement that proved only "a trigger with this
    name exists" would be satisfied by a same-named trigger bound to a
    different function, fired AFTER instead of BEFORE, narrowed to INSERT, given
    a `WHEN (false)` condition, restricted to `UPDATE OF` some other column, or
    simply `ALTER TABLE ... DISABLE TRIGGER`-ed. Every one of those leaves the
    invariants unenforced.

    So the whole binding is asserted structurally, from `pg_trigger`: timing,
    event set, row/statement level, the schema-qualified function it executes,
    the WHEN condition and the UPDATE column list. Structural fields are used
    rather than `pg_get_triggerdef` because that function qualifies names
    against the session `search_path`, which would make the same trigger compare
    differently in two databases.

    `condition` and `update_columns` default to "none", which is the shape
    migration 049 creates. They are asserted rather than ignored: an undeclared
    narrowing is exactly how a trigger stops firing without disappearing.
    """

    name: str
    timing: str
    events: Tuple[str, ...]
    level: str
    function: str
    enabled: bool = True
    condition: Optional[str] = None
    update_columns: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FunctionRequirement:
    """One function the release needs, verified INCLUDING its implementation.

    A trigger binding is only worth what the function it names does. Replacing
    the guard body with `BEGIN RETURN NEW; END` keeps the name, the signature,
    the language, the trigger and every constraint intact and removes the whole
    enforcement — so a requirement that stopped at the signature would certify a
    schema that enforces nothing.

    `body_sha256` is SHA-256 over `pg_proc.prosrc` exactly as stored. That is
    the minimum stable way to answer "is this the implementation the migration
    installed": applied migrations are immutable in this repository and
    PostgreSQL stores the body verbatim, so every database that ran 049 carries
    identical bytes. It is deliberately not a normalized or partial comparison —
    a body that differs at all is a body this release did not verify, and the
    fidelity test regenerates the digest from the migration itself so an
    intentional edit fails loudly instead of drifting.

    `arguments` is `pg_get_function_identity_arguments`, so the requirement
    names one overload rather than a family.
    """

    schema: str
    name: str
    arguments: str = ""
    language: Optional[str] = None
    returns: Optional[str] = None
    body_sha256: Optional[str] = None

    @property
    def identity(self) -> str:
        return f"{self.schema}.{self.name}({self.arguments})"


@dataclass(frozen=True)
class RelationRequirement:
    schema: str
    table: str
    columns: Tuple[ColumnRequirement, ...]
    constraints: Tuple[ConstraintRequirement, ...] = ()
    indexes: Tuple[IndexRequirement, ...] = ()
    #: Triggers whose complete binding must hold. Empty for every requirement
    #: that predates migration 049, so nothing existing is narrowed or widened.
    triggers: Tuple[TriggerRequirement, ...] = ()
    #: Mutually exclusive acceptable schema STATES. Every entry in `constraints`
    #: must hold unconditionally; `constraint_alternatives` additionally
    #: requires that at least one group holds in full. The two are independent,
    #: so declaring alternatives never weakens an unconditional requirement.
    constraint_alternatives: Tuple[ConstraintAlternativeGroup, ...] = ()


@dataclass(frozen=True)
class SchemaRequirement:
    migration: str
    scope: str
    milestone: Optional[str]
    reason: Optional[str]
    relations: Tuple[RelationRequirement, ...]
    #: Schema-level objects that belong to no single relation. A trigger
    #: function is the concrete case: it is what a `TriggerRequirement` binds
    #: to, and it can be replaced without touching any relation at all.
    functions: Tuple[FunctionRequirement, ...] = ()


@dataclass(frozen=True)
class SchemaStateGuard:
    """A database state that NARROWS which releases may run against it.

    THE HOLE THIS CLOSES, PRECISELY.
        Every other check in this module is declared BY THE RELEASE and read
        from the release's own tree. That is correct for a prerequisite — a
        release that predates a migration must not be blocked by it — and it is
        exactly wrong for a narrowing. When the client first-seen CONTRACT
        closes, `ck_client_trips_first_seen_pairing` starts rejecting the
        M4-era writer's request-only inserts. The M4 release declares nothing
        about it, cannot declare anything about it, and would therefore activate
        cleanly and fail on its first production INSERT. Reviewing that as a
        release-tooling gap is what this type answers.

        So the direction is inverted: the DATABASE asserts the requirement and
        the RELEASE must answer it. A guard is detected by reading the client
        schema, and the release is refused unless it declares `capability`.
        Absence of the declaration is a refusal, which is what makes the guard
        effective against releases built before the capability key existed.

    DETECTION IS BY PRESENCE, NOT BY VALIDATION STATE.
        `ADD CONSTRAINT ... NOT VALID` already enforces the rule on every new
        INSERT and UPDATE, so the writer contract is closed the moment the
        catalog entry commits — before `VALIDATE` runs. Detecting only a
        validated constraint would leave the interrupted-closure window open to
        exactly the writer this guard exists to reject.

    A guard is DELIBERATELY narrow: one relation, one constraint name, one
    capability. It is not a general compatibility framework, and a new guard
    should be added only when a specific database state genuinely invalidates a
    specific class of previously-valid release.
    """

    capability: str
    scope: str
    schema: str
    table: str
    constraint: str
    reason: str


#: The capability a release must declare to run against a client business
#: database whose first-seen pair CONTRACT is closed.
CAPABILITY_FIRST_SEEN_PAIR_CONTRACT = "client_trips_first_seen_pair_contract"

#: Every state guard in the repository. One entry, on purpose.
SCHEMA_STATE_GUARDS: Tuple[SchemaStateGuard, ...] = (
    SchemaStateGuard(
        capability=CAPABILITY_FIRST_SEEN_PAIR_CONTRACT,
        scope=SCOPE_CLIENT_BUSINESS,
        schema="public",
        table="client_trips",
        constraint="ck_client_trips_first_seen_pairing",
        reason=(
            "the strict first-seen pairing CHECK rejects a request-only "
            "provenance INSERT, so only a release carrying the pair-atomic "
            "M-LAG writer may run against this database"
        ),
    ),
)


def detect_schema_state_guards(
    cur, *, scope: str, guards: Tuple[SchemaStateGuard, ...] = SCHEMA_STATE_GUARDS,
) -> List[SchemaStateGuard]:
    """Which guards the connected database currently asserts. Read-only."""
    detected: List[SchemaStateGuard] = []
    for guard in guards:
        if guard.scope != scope:
            continue
        cur.execute(
            "SELECT to_regclass(%s) AS relation",
            (f"{guard.schema}.{guard.table}",),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            continue
        cur.execute(
            """
            SELECT 1 FROM pg_constraint
             WHERE conname = %s AND conrelid = %s::regclass
             LIMIT 1
            """,
            (guard.constraint, f"{guard.schema}.{guard.table}"),
        )
        if cur.fetchone() is not None:
            detected.append(guard)
    return detected


# ---------------------------------------------------------------------------
# STRICT DECLARATION PARSING
#
# WHY THIS IS STRICT RATHER THAN TOLERANT.
#     This document is release-gate configuration: what it says is the only
#     thing standing between an activation and a schema that cannot run the
#     release. A tolerant parser turns a typo into a SILENTLY WEAKER GATE, and
#     independent review demonstrated all three shapes of that on this very
#     file — `"defualt"` removed a default assertion, `"enabeld"` removed a
#     trigger-enabled assertion, and `"nullable": "false"` was coerced by
#     Python truthiness into the boolean TRUE, i.e. into the OPPOSITE of what
#     the declaration said. None of the three produced any diagnostic.
#
#     So every declaration object is parsed against an explicit key set and
#     every value against its exact JSON type. An unknown key, a misspelled
#     key, a wrong primitive, an explicit `null`, an unsupported enum value or
#     a structurally incomplete object is `RELEASE_SCHEMA_REQUIREMENTS_MALFORMED`
#     — the gate refuses to run at all rather than run against a declaration it
#     did not fully understand. "The gate did not understand this" must never
#     read as "the release asserted nothing".
#
#     `null` is never accepted for any key. An optional property is expressed
#     by OMITTING it, which keeps exactly one representation per meaning and is
#     why `default` can now distinguish "not asserted" from "must be absent".
# ---------------------------------------------------------------------------

def _malformed(message: str) -> "SchemaPreflightError":
    return SchemaPreflightError("RELEASE_SCHEMA_REQUIREMENTS_MALFORMED", message)


def _where(migration: Optional[str]) -> str:
    return f" in {migration!r}" if migration else ""


def _require_mapping(value: object, *, what: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise _malformed(f"{what} is not an object")
    return value


def _require_object(
    value: object, *, what: str, migration: Optional[str] = None,
    required: Tuple[str, ...] = (), optional: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    """One declaration object, with its complete key set decided here.

    Unknown and misspelled keys are indistinguishable to any parser, so both
    are refused: a key this gate does not implement cannot have been enforced.
    """
    entry = _require_mapping(value, what=what)
    # `_comment` is reserved everywhere and asserts nothing. It is the only
    # non-semantic key: this document is read by humans deciding whether a
    # release may activate, and several declarations — the 048 EXPAND/CONTRACT
    # alternatives in particular — are unreadable without one. It is still
    # type-checked, so a value that is neither prose nor lines of prose is a
    # refusal rather than a place to hide structure the gate never reads.
    allowed = set(required) | set(optional) | {"_comment"}
    if "_comment" in entry:
        comment = entry["_comment"]
        if not (
            isinstance(comment, str)
            or (isinstance(comment, list)
                and all(isinstance(line, str) for line in comment))
        ):
            raise _malformed(
                f"{what}{_where(migration)} declares a '_comment' that is "
                "neither a string nor an array of strings"
            )
    unknown = sorted(k for k in entry if k not in allowed)
    if unknown:
        raise _malformed(
            f"{what}{_where(migration)} declares unknown key(s) {unknown}; "
            f"the accepted keys are {sorted(allowed)}. An unrecognised key is "
            "refused rather than ignored, because a misspelled key would "
            "otherwise silently remove the assertion it was meant to make"
        )
    missing = sorted(k for k in required if k not in entry)
    if missing:
        raise _malformed(
            f"{what}{_where(migration)} declares no {missing}"
        )
    return entry


def _json_string(
    entry: Dict[str, Any], key: str, *, what: str,
    migration: Optional[str] = None, allow_empty: bool = False,
) -> Optional[str]:
    """A JSON string, or `None` when the key is OMITTED. Never coerced."""
    if key not in entry:
        return None
    value = entry[key]
    if not isinstance(value, str):
        raise _malformed(
            f"{what}.{key}{_where(migration)} must be a JSON string, not "
            f"{type(value).__name__}"
        )
    if not allow_empty and not value.strip():
        raise _malformed(f"{what}.{key}{_where(migration)} is empty")
    return value


def _json_bool(
    entry: Dict[str, Any], key: str, *, what: str,
    migration: Optional[str] = None,
) -> Optional[bool]:
    """A JSON boolean, or `None` when the key is OMITTED.

    `isinstance(value, bool)` and nothing else. The string `"false"` and the
    integer `0` are refused rather than converted: `bool("false")` is `True`,
    which is how a declaration once asserted the opposite of what it read.
    """
    if key not in entry:
        return None
    value = entry[key]
    if not isinstance(value, bool):
        raise _malformed(
            f"{what}.{key}{_where(migration)} must be a JSON boolean "
            f"(true/false), not {type(value).__name__} {value!r}"
        )
    return value


def _json_list(
    entry: Dict[str, Any], key: str, *, what: str,
    migration: Optional[str] = None,
) -> List[Any]:
    """A JSON array, or `[]` when the key is OMITTED."""
    if key not in entry:
        return []
    value = entry[key]
    if not isinstance(value, list):
        raise _malformed(
            f"{what}.{key}{_where(migration)} must be a JSON array, not "
            f"{type(value).__name__}"
        )
    return value


def _string_array(
    entry: Dict[str, Any], key: str, *, what: str,
    migration: Optional[str] = None, allow_empty: bool = False,
) -> Tuple[str, ...]:
    values = _json_list(entry, key, what=what, migration=migration)
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise _malformed(
                f"{what}.{key}{_where(migration)} must be an array of "
                f"non-empty strings; found {item!r}"
            )
    cleaned = tuple(item.strip() for item in values)
    if not cleaned and not allow_empty:
        raise _malformed(
            f"{what}.{key}{_where(migration)} is empty; a requirement that "
            "asserts no member asserts nothing"
        )
    duplicates = sorted({v for v in cleaned if cleaned.count(v) > 1})
    if duplicates:
        raise _malformed(
            f"{what}.{key}{_where(migration)} repeats {duplicates}"
        )
    return cleaned


def _reject_duplicates(names: List[str], *, what: str,
                       migration: Optional[str] = None) -> None:
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise _malformed(
            f"{what}{_where(migration)} is declared more than once: "
            f"{duplicates}. Two declarations of one object are either "
            "redundant or contradictory, and the gate must not have to pick"
        )


def parse_capabilities(payload: object) -> frozenset:
    """Capabilities the release DECLARES it is compatible with.

    Distinct from a requirement in one decisive way: a requirement is checked
    only if the release asks for it, whereas a capability is DEMANDED BY THE
    DATABASE (see `SCHEMA_STATE_GUARDS`) and the release either declares it or
    is refused. Absence is therefore never a pass — which is exactly what makes
    it work against releases built before the key existed.
    """
    document = _require_mapping(payload, what="the requirements document")
    if "capabilities" not in document:
        return frozenset()
    return frozenset(
        _string_array(document, "capabilities",
                      what="the requirements document", allow_empty=True)
    )


def _parse_constraint(raw_constraint: object, *, migration: str) -> ConstraintRequirement:
    """One constraint entry, in either accepted form.

    A bare string stays valid: releases that predate the definition form must
    keep parsing unchanged.
    """
    if isinstance(raw_constraint, str):
        if not raw_constraint.strip():
            raise _malformed(
                f"a constraint in {migration!r} is an empty name"
            )
        return ConstraintRequirement(name=raw_constraint)
    entry = _require_object(
        raw_constraint, what="a constraint", migration=migration,
        required=("name",), optional=("definition", "validated"),
    )
    validated = _json_bool(entry, "validated", what="a constraint",
                           migration=migration)
    return ConstraintRequirement(
        name=str(_json_string(entry, "name", what="a constraint",
                              migration=migration)),
        definition=_json_string(entry, "definition", what="a constraint",
                                migration=migration),
        validated=(True if validated is None else validated),
    )


def _parse_trigger(raw_trigger: object, *, migration: str) -> TriggerRequirement:
    """One trigger entry. Strict: there is no name-only form, on purpose.

    A trigger's whole value is its binding, so a shorthand that omitted timing,
    events, level or the target function would reintroduce exactly the
    name-only weakness this type exists to close.
    """
    entry = _require_object(
        raw_trigger, what="a trigger", migration=migration,
        required=("name", "timing", "events", "level", "function"),
        optional=("enabled", "condition", "update_columns"),
    )
    timing = str(_json_string(entry, "timing", what="a trigger",
                              migration=migration)).strip().upper()
    if timing not in {"BEFORE", "AFTER", "INSTEAD OF"}:
        raise _malformed(
            f"trigger timing {timing!r} in {migration!r} is not BEFORE, AFTER "
            "or INSTEAD OF"
        )
    level = str(_json_string(entry, "level", what="a trigger",
                             migration=migration)).strip().upper()
    if level not in {"ROW", "STATEMENT"}:
        raise _malformed(
            f"trigger level {level!r} in {migration!r} is not ROW or STATEMENT"
        )
    events = tuple(
        e.upper() for e in _string_array(
            entry, "events", what="a trigger", migration=migration,
        )
    )
    known_events = {name for name, _bit in TRIGGER_EVENT_BITS}
    unknown = sorted(set(events) - known_events)
    if unknown:
        raise _malformed(
            f"trigger events {unknown} in {migration!r} are not "
            f"{sorted(known_events)}"
        )
    enabled = _json_bool(entry, "enabled", what="a trigger", migration=migration)
    return TriggerRequirement(
        name=str(_json_string(entry, "name", what="a trigger",
                              migration=migration)),
        timing=timing,
        events=tuple(sorted(set(events))),
        level=level,
        function=str(_json_string(entry, "function", what="a trigger",
                                  migration=migration)).strip(),
        enabled=(True if enabled is None else enabled),
        condition=_json_string(entry, "condition", what="a trigger",
                               migration=migration),
        update_columns=tuple(sorted(_string_array(
            entry, "update_columns", what="a trigger", migration=migration,
            allow_empty=True,
        ))),
    )


def _parse_function(raw_function: object, *, migration: str) -> FunctionRequirement:
    """One function entry. Requires enough to identify exactly one overload."""
    entry = _require_object(
        raw_function, what="a function", migration=migration,
        required=("schema", "name"),
        optional=("arguments", "language", "returns", "body_sha256"),
    )
    digest = _json_string(entry, "body_sha256", what="a function",
                          migration=migration)
    if digest is not None:
        digest = digest.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise _malformed(
                f"function body_sha256 in {migration!r} is not a SHA-256 hex "
                "digest"
            )
    arguments = _json_string(entry, "arguments", what="a function",
                             migration=migration, allow_empty=True)
    language = _json_string(entry, "language", what="a function",
                            migration=migration)
    returns = _json_string(entry, "returns", what="a function",
                           migration=migration)
    return FunctionRequirement(
        schema=str(_json_string(entry, "schema", what="a function",
                                migration=migration)).strip(),
        name=str(_json_string(entry, "name", what="a function",
                              migration=migration)).strip(),
        arguments=("" if arguments is None else arguments.strip()),
        language=(None if language is None else language.strip()),
        returns=(None if returns is None else returns.strip()),
        body_sha256=digest,
    )


def _parse_column_default(
    raw_column: Dict[str, Any], *, migration: str, column_name: str,
) -> Optional[ColumnDefaultRequirement]:
    """The THREE distinguishable default states, decided here.

    * key OMITTED          -> `None`, the requirement does not check the default;
    * `{"state": "absent"}` -> the column must physically have NO default;
    * `{"state": "expression", "expression": "..."}` or the equivalent string
      shorthand -> the column must carry exactly that catalog default.

    The shorthand is the M4-era form and the same string-or-object convention
    `constraints` already uses, so existing declarations keep parsing; it means
    the expression state and nothing else. What is NOT accepted is `null`,
    which is precisely the overload independent review found: a `default` of
    `None` could mean either "not checked" or "no default", so declaring the
    second was impossible and `provider_name DEFAULT 'fake'` passed the gate.
    """
    if "default" not in raw_column:
        return None
    raw = raw_column["default"]
    what = f"column {column_name!r} default"
    if isinstance(raw, str):
        if not raw.strip():
            raise _malformed(f"{what} in {migration!r} is an empty expression")
        return ColumnDefaultRequirement(
            state=DEFAULT_STATE_EXPRESSION, expression=raw,
        )
    entry = _require_object(
        raw, what=what, migration=migration,
        required=("state",), optional=("expression",),
    )
    state = str(_json_string(entry, "state", what=what, migration=migration))
    if state not in KNOWN_DEFAULT_STATES:
        raise _malformed(
            f"{what} in {migration!r} declares state {state!r}, which is not "
            f"one of {sorted(KNOWN_DEFAULT_STATES)}"
        )
    expression = _json_string(entry, "expression", what=what,
                              migration=migration)
    if state == DEFAULT_STATE_ABSENT:
        if expression is not None:
            raise _malformed(
                f"{what} in {migration!r} declares state 'absent' and also an "
                "expression; a column cannot both have no default and have "
                "that one"
            )
        return ColumnDefaultRequirement(state=DEFAULT_STATE_ABSENT)
    if expression is None:
        raise _malformed(
            f"{what} in {migration!r} declares state 'expression' but no "
            "'expression' to require"
        )
    return ColumnDefaultRequirement(
        state=DEFAULT_STATE_EXPRESSION, expression=expression,
    )


def _parse_column(raw_column: object, *, migration: str) -> ColumnRequirement:
    entry = _require_object(
        raw_column, what="a column", migration=migration,
        required=("name", "type"), optional=("nullable", "default"),
    )
    name = str(_json_string(entry, "name", what="a column", migration=migration))
    return ColumnRequirement(
        name=name,
        type=str(_json_string(entry, "type", what="a column",
                              migration=migration)),
        nullable=_json_bool(entry, "nullable", what="a column",
                            migration=migration),
        default=_parse_column_default(entry, migration=migration,
                                      column_name=name),
    )


def _parse_index(raw_index: object, *, migration: str) -> IndexRequirement:
    entry = _require_object(
        raw_index, what="an index", migration=migration,
        required=("name", "definition"),
    )
    return IndexRequirement(
        name=str(_json_string(entry, "name", what="an index",
                              migration=migration)),
        definition=str(_json_string(entry, "definition", what="an index",
                                    migration=migration)),
    )


def _parse_relation(raw_relation: object, *, migration: str) -> RelationRequirement:
    relation = _require_object(
        raw_relation, what="a relation", migration=migration,
        required=("schema", "table"),
        optional=("columns", "constraints", "indexes", "triggers",
                  "constraint_alternatives"),
    )
    # A relation may be required purely for its constraints or indexes —
    # `client_dataset_recovery_run` needs no new column from 062, only the
    # re-keyed exclusivity indexes and the coherence FK. What must never be
    # allowed is a relation that asserts nothing at all, which is checked once
    # every list below has been parsed.
    columns = [
        _parse_column(raw, migration=migration)
        for raw in _json_list(relation, "columns", what="a relation",
                              migration=migration)
    ]
    _reject_duplicates([c.name for c in columns], what="a column",
                       migration=migration)

    constraints = [
        _parse_constraint(raw, migration=migration)
        for raw in _json_list(relation, "constraints", what="a relation",
                              migration=migration)
    ]
    _reject_duplicates([c.name for c in constraints], what="a constraint",
                       migration=migration)

    # Alternative acceptable STATES for the same logical requirement. Parsed
    # strictly: a malformed or degenerate alternative block is a refusal, never
    # a silently-ignored key, because "the release said something the gate did
    # not understand" must not read as "the release asserted nothing".
    alternatives = []
    seen_states = set()
    for raw_group in _json_list(relation, "constraint_alternatives",
                                what="a relation", migration=migration):
        group = _require_object(
            raw_group, what="a constraint alternative", migration=migration,
            required=("state", "constraints"),
        )
        state = str(_json_string(group, "state", what="a constraint alternative",
                                 migration=migration)).strip()
        if state in seen_states:
            raise _malformed(
                f"constraint alternative state {state!r} is declared twice in "
                f"{migration!r}"
            )
        seen_states.add(state)
        raw_group_constraints = _json_list(
            group, "constraints", what="a constraint alternative",
            migration=migration,
        )
        if not raw_group_constraints:
            raise _malformed(
                f"constraint alternative {state!r} in {migration!r} asserts no "
                "constraint; an alternative that asserts nothing would make "
                "the whole requirement vacuous"
            )
        group_constraints = tuple(
            _parse_constraint(raw, migration=migration)
            for raw in raw_group_constraints
        )
        # Uniqueness INSIDE the group, by the same canonical identity every
        # other duplicate check in this parser uses: the constraint name. A
        # group is satisfied only when ALL of its constraints hold, so two
        # declarations of one name are either redundant or contradictory — and
        # a contradictory pair cannot both hold, which would make the group
        # unsatisfiable rather than strict. Independent review reproduced the
        # accepting case: a duplicated 048 EXPAND constraint parsed, and
        # physical matching reported no defect, so the duplication was
        # invisible in both directions.
        #
        # The scope is ONE GROUP, deliberately. Distinct groups naming the same
        # constraint is the normal shape of an expand-contract span — the same
        # CHECK may appear NOT VALID in EXPAND and validated in CONTRACT — and
        # rejecting that would break the compatibility declaration this key
        # exists for. Groups also do not nest: `constraints` inside a group is
        # parsed as constraint entries only, so this check sees every member of
        # the group and nothing belonging to another relation.
        _reject_duplicates(
            [c.name for c in group_constraints],
            what=f"a constraint in constraint alternative {state!r}",
            migration=migration,
        )
        alternatives.append(
            ConstraintAlternativeGroup(
                state=state,
                constraints=group_constraints,
            )
        )
    if len(alternatives) == 1:
        raise _malformed(
            f"relation in {migration!r} declares a single "
            "constraint_alternatives group; a one-of with one member is an "
            "unconditional requirement and must be declared in 'constraints' "
            "so it reads as one"
        )

    indexes = [
        _parse_index(raw, migration=migration)
        for raw in _json_list(relation, "indexes", what="a relation",
                              migration=migration)
    ]
    _reject_duplicates([i.name for i in indexes], what="an index",
                       migration=migration)

    triggers = [
        _parse_trigger(raw, migration=migration)
        for raw in _json_list(relation, "triggers", what="a relation",
                              migration=migration)
    ]
    _reject_duplicates([t.name for t in triggers], what="a trigger",
                       migration=migration)

    if (
        not columns and not constraints and not indexes
        and not alternatives and not triggers
    ):
        raise _malformed(
            f"relation in {migration!r} asserts nothing physical; a "
            "requirement must name at least one column, constraint, index or "
            "trigger"
        )
    return RelationRequirement(
        schema=str(_json_string(relation, "schema", what="a relation",
                                migration=migration)),
        table=str(_json_string(relation, "table", what="a relation",
                               migration=migration)),
        columns=tuple(columns),
        constraints=tuple(constraints),
        indexes=tuple(indexes),
        triggers=tuple(triggers),
        constraint_alternatives=tuple(alternatives),
    )


def parse_requirements(payload: object) -> List[SchemaRequirement]:
    """Strictly parse the requirements document. Never guesses, never defaults."""
    document = _require_object(
        payload, what="the requirements document",
        required=("version", "requirements"),
        optional=("capabilities",),
    )
    version = _json_string(document, "version",
                           what="the requirements document")
    if version != SCHEMA_REQUIREMENTS_VERSION:
        raise SchemaPreflightError(
            "RELEASE_SCHEMA_REQUIREMENTS_VERSION_UNSUPPORTED",
            f"requirements version {version!r} is not "
            f"{SCHEMA_REQUIREMENTS_VERSION!r}",
        )
    # Validated here as well as in `parse_capabilities`, so that "the document
    # parses" is one answer rather than two: a caller that reaches only this
    # function must not be the one that accepts a malformed capability list.
    _string_array(document, "capabilities", what="the requirements document",
                  allow_empty=True)
    raw_requirements = _json_list(document, "requirements",
                                  what="the requirements document")
    parsed: List[SchemaRequirement] = []
    for entry in raw_requirements:
        item = _require_object(
            entry, what="a requirement",
            required=("migration", "scope"),
            optional=("milestone", "reason", "relations", "functions"),
        )
        scope = str(_json_string(item, "scope", what="a requirement"))
        if scope not in KNOWN_SCOPES:
            raise _malformed(
                f"requirement scope {scope!r} is not one of {sorted(KNOWN_SCOPES)}"
            )
        migration = str(
            _json_string(item, "migration", what="a requirement")
        ).strip()
        functions = [
            _parse_function(raw, migration=migration)
            for raw in _json_list(item, "functions", what="a requirement",
                                  migration=migration)
        ]
        _reject_duplicates([f.identity for f in functions], what="a function",
                           migration=migration)

        raw_relations = _json_list(item, "relations", what="a requirement",
                                   migration=migration)
        if not raw_relations and not functions:
            raise _malformed(
                f"requirement {migration!r} declares no relations or "
                "functions; a requirement that asserts nothing physical is not "
                "a requirement"
            )
        relations = [
            _parse_relation(raw, migration=migration) for raw in raw_relations
        ]
        _reject_duplicates([f"{r.schema}.{r.table}" for r in relations],
                           what="a relation", migration=migration)
        milestone = _json_string(item, "milestone", what="a requirement",
                                 migration=migration)
        reason = _json_string(item, "reason", what="a requirement",
                              migration=migration)
        parsed.append(
            SchemaRequirement(
                migration=migration,
                scope=scope,
                milestone=milestone,
                reason=reason,
                relations=tuple(relations),
                functions=tuple(functions),
            )
        )
    _reject_duplicates([f"{r.scope}:{r.migration}" for r in parsed],
                       what="a requirement")
    return parsed


def load_release_requirements(
    release_tree: Path,
) -> Tuple[List[SchemaRequirement], frozenset, str]:
    """Read what the RELEASE declares. Returns `(requirements, capabilities, note)`.

    A release without the file predates this mechanism. Its REQUIREMENTS are
    empty and that is a pass with a recorded reason, not a failure: refusing
    would make every historical release un-rollback-able, which would be a worse
    safety outcome than the one this gate exists to prevent.

    Its CAPABILITIES are empty too, and that emphatically is NOT a pass — it is
    the whole point. A database state that a release must be built for (see
    `SCHEMA_STATE_GUARDS`) refuses any release that does not declare the
    matching capability, and a release predating the key cannot declare it.

    THE THREE OUTCOMES OF A FILE THAT IS PRESENT, kept distinct because an
    operator acts on the classification and not on the message:

      * the source could not be read at all (permission, I/O, a directory in
        the file's place) -> `RELEASE_SCHEMA_REQUIREMENTS_UNREADABLE`;
      * it was read and is not a valid UTF-8 JSON document ->
        `RELEASE_SCHEMA_REQUIREMENTS_MALFORMED`;
      * it is valid JSON declaring something this gate refuses ->
        `RELEASE_SCHEMA_REQUIREMENTS_MALFORMED`, from the parser.

    Grouping the second with the first — which is what `except (OSError,
    JSONDecodeError)` did — sent an operator to check filesystem permissions
    for a truncated file sitting there perfectly readable.
    """
    path = Path(release_tree) / SCHEMA_REQUIREMENTS_RELPATH
    if not path.exists():
        return [], frozenset(), "release_predates_schema_requirements"
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        # The SOURCE could not be obtained: absent-but-present path entry,
        # permission denial, a directory where a file must be, an I/O error.
        # Nothing was read, so nothing can be said about what the release
        # declares — which is a different fact from "the release declared
        # something this gate could not accept".
        raise SchemaPreflightError(
            "RELEASE_SCHEMA_REQUIREMENTS_UNREADABLE",
            f"{SCHEMA_REQUIREMENTS_RELPATH} could not be read "
            f"({type(exc).__name__})",
        ) from exc
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Read successfully as BYTES and not decodable as the UTF-8 the format
        # requires. Content, not filesystem: MALFORMED for the same reason.
        raise _malformed(
            f"{SCHEMA_REQUIREMENTS_RELPATH} is not valid UTF-8 ({exc.reason})"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        # The bytes ARRIVED and are not a JSON document. That is a defective
        # DECLARATION, exactly like a misspelled key or a wrong scalar type,
        # and it must carry the same classification as those: an operator
        # reading `UNREADABLE` looks at the filesystem and permissions, which
        # is the wrong place entirely when the file is sitting there, readable,
        # and truncated.
        raise _malformed(
            f"{SCHEMA_REQUIREMENTS_RELPATH} is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc
    return (
        parse_requirements(payload),
        parse_capabilities(payload),
        "release_declared_requirements",
    )


# ---------------------------------------------------------------------------
# Physical verification primitives (read-only)
# ---------------------------------------------------------------------------

def ledger_has_migration(cur, filename: str) -> bool:
    """Is `filename` recorded applied? A missing ledger table means 'no'.

    Deliberately does not create the table: this module is read-only, and an
    absent ledger is a legitimate answer — it means nothing has been applied.
    """
    cur.execute("SELECT to_regclass('public.schema_migrations') AS relation")
    row = cur.fetchone()
    if row is None or row[0] is None:
        return False
    cur.execute(
        "SELECT 1 FROM public.schema_migrations WHERE filename = %s LIMIT 1",
        (filename,),
    )
    return cur.fetchone() is not None


def _constraint_defects(
    present: Dict[str, Tuple[str, bool]],
    constraint: ConstraintRequirement,
    qualified: str,
) -> List[str]:
    """Compare one declared constraint against the catalog. Empty means it holds."""
    defects: List[str] = []
    observed = present.get(constraint.name)
    if observed is None:
        return [f"constraint_absent:{qualified}.{constraint.name}"]
    definition, validated = observed
    if constraint.definition is not None and (
        _normalize_definition(definition)
        != _normalize_definition(constraint.definition)
    ):
        # The name is right and the shape is wrong — precisely the drift a
        # name-only check waves through.
        defects.append(
            f"constraint_definition_mismatch:{qualified}."
            f"{constraint.name}:actual={_normalize_definition(definition)}"
        )
    if constraint.validated and not validated:
        defects.append(f"constraint_not_validated:{qualified}.{constraint.name}")
    return defects


def _trigger_defects(
    present: Dict[str, "ObservedTrigger"],
    trigger: TriggerRequirement,
    qualified: str,
) -> List[str]:
    """Compare one declared trigger against the catalog. Empty means it holds.

    Every field is compared, and every mismatch is reported rather than the
    first one: an operator reading a refusal needs to know whether the guard was
    rebound, retimed, narrowed or switched off.
    """
    defects: List[str] = []
    observed = present.get(trigger.name)
    if observed is None:
        return [f"trigger_absent:{qualified}.{trigger.name}"]
    if observed.timing != trigger.timing:
        defects.append(
            f"trigger_timing_mismatch:{qualified}.{trigger.name}"
            f":expected={trigger.timing}:actual={observed.timing}"
        )
    if tuple(sorted(observed.events)) != tuple(sorted(trigger.events)):
        defects.append(
            f"trigger_events_mismatch:{qualified}.{trigger.name}"
            f":expected={','.join(sorted(trigger.events))}"
            f":actual={','.join(sorted(observed.events))}"
        )
    if observed.level != trigger.level:
        defects.append(
            f"trigger_level_mismatch:{qualified}.{trigger.name}"
            f":expected={trigger.level}:actual={observed.level}"
        )
    if observed.function != trigger.function:
        # The rebind case: the name is right and the code it runs is not.
        defects.append(
            f"trigger_function_mismatch:{qualified}.{trigger.name}"
            f":expected={trigger.function}:actual={observed.function}"
        )
    if trigger.enabled and observed.enabled not in TRIGGER_ENABLED_STATES:
        defects.append(
            f"trigger_disabled:{qualified}.{trigger.name}"
            f":tgenabled={observed.enabled}"
        )
    declared_condition = (
        None if trigger.condition is None
        else _normalize_definition(trigger.condition)
    )
    observed_condition = (
        None if observed.condition is None
        else _normalize_definition(observed.condition)
    )
    if declared_condition != observed_condition:
        defects.append(
            f"trigger_condition_mismatch:{qualified}.{trigger.name}"
            f":expected={declared_condition or ''}"
            f":actual={observed_condition or ''}"
        )
    if tuple(sorted(observed.update_columns)) != tuple(
        sorted(trigger.update_columns)
    ):
        defects.append(
            f"trigger_update_columns_mismatch:{qualified}.{trigger.name}"
            f":expected={','.join(sorted(trigger.update_columns))}"
            f":actual={','.join(sorted(observed.update_columns))}"
        )
    return defects


def _alternative_state_defects(
    present: Dict[str, Tuple[str, bool]],
    alternatives: Tuple[ConstraintAlternativeGroup, ...],
    qualified: str,
) -> List[str]:
    """One-of, with EXCLUSIVITY. Empty means exactly one declared state holds.

    WHY "AT LEAST ONE MATCHES" WAS WRONG.
        The first implementation accepted the relation as soon as one group
        matched in full, and stopped looking. Independent review reproduced the
        consequence: a database carrying a perfect EXPAND constraint *and* a
        same-named-but-malformed `ck_client_trips_first_seen_pairing` passed the
        gate, because the EXPAND alternative was checked first and matched. That
        malformed strict CHECK is live DDL — it rejects writes at runtime — so
        the gate authorized an activation against a schema state no declared
        alternative describes.

    WHAT "THE STATE" MEANS NOW.
        The alternatives of one relation describe MUTUALLY EXCLUSIVE points in a
        transition, not a menu. A state is authoritative only when

          * every constraint it declares holds exactly (name, canonical
            definition, validation status), AND
          * no constraint belonging to any COMPETING declared alternative is
            present in the catalog at all — whatever shape it carries.

        and the relation is compatible only when EXACTLY ONE state is
        authoritative. Zero is the ordinary refusal; more than one would mean
        the declaration itself is not exclusive, and is refused rather than
        resolved by declaration order.

    WHAT IS DELIBERATELY NOT TOUCHED.
        Exclusivity is scoped to the constraint NAMES participating in this
        relation's declared alternatives. A constraint the requirements file
        never mentions is none of this function's business, so an unrelated
        CHECK, FK or PK never invalidates an otherwise valid state. Unconditional
        `constraints` are evaluated separately by the caller and are unaffected.
    """
    participating: set = set()
    for group in alternatives:
        for constraint in group.constraints:
            participating.add(constraint.name)

    authoritative: List[str] = []
    explanations: List[str] = []
    for group in alternatives:
        own = {constraint.name for constraint in group.constraints}
        group_defects: List[str] = []
        for constraint in group.constraints:
            group_defects.extend(
                _constraint_defects(present, constraint, qualified)
            )
        # The exclusivity half. A competing alternative's constraint being
        # present at all disqualifies this state, because the two states cannot
        # both describe the database and the catalog says one of them does not.
        for name in sorted(participating - own):
            if name in present:
                group_defects.append(
                    f"competing_state_constraint_present:{qualified}.{name}"
                )
        if not group_defects:
            authoritative.append(group.state)
            continue
        explanations.append(
            f"{group.state}=({','.join(sorted(group_defects))})"
        )

    if len(authoritative) == 1:
        return []
    if len(authoritative) > 1:
        # Not reachable with mutually exclusive declarations; refused rather
        # than silently resolved, because reaching it means the requirements
        # file declares two states that can hold at once and the gate can no
        # longer say which one it authorized.
        return [
            f"constraint_alternatives_ambiguous:{qualified}:"
            + ",".join(sorted(authoritative))
        ]
    return [
        f"constraint_alternatives_unsatisfied:{qualified}:"
        + ";".join(explanations)
    ]


@dataclass(frozen=True)
class ObservedColumn:
    """One column as `information_schema.columns` reports it.

    A named type rather than a tuple because the tuple grew a third member.
    Constructed positionally too, so every hand-built `RelationState` in the
    repository keeps reading the way it did.
    """

    type: str
    nullable: bool
    default: Optional[str] = None


@dataclass(frozen=True)
class ObservedTrigger:
    """One trigger as `pg_trigger` reports it, already decoded.

    `enabled` is the raw `tgenabled` character, kept raw so a defect can say
    which of 'D' and 'R' the catalog actually holds.
    """

    timing: str
    events: Tuple[str, ...]
    level: str
    function: str
    enabled: str
    condition: Optional[str] = None
    update_columns: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationState:
    """A CATALOG OBSERVATION of ONE relation, in the exact shape the matcher reads.

    WHY THIS TYPE EXISTS.
        `relation_defects` used to be one function that both READ the catalog
        and DECIDED whether a requirement holds. That is fine while the only
        caller has a live cursor, and it became the reviewed defect the moment a
        second caller did not: `inspect_release_bridge_compatibility` answers a
        DECLARATION question with no database in reach, so it grew its own
        partial re-implementation that compared `constraint_alternatives` and
        nothing else. Independent review then found the gap that opens — a
        packaged bridge relation additionally requiring an absent
        `ck_synthetic_unconditional` was G6-READY while the same release's
        relation preflight correctly refused it.

        Splitting the observation from the decision closes that by construction.
        There is now ONE definition of what a complete `RelationRequirement`
        means (`relation_state_defects`), used unchanged by live preflight and
        by the bridge gate, and the two cannot drift apart because there is
        nothing left to drift.

    The field shapes are the catalog's own, deliberately: `columns` maps to
    (`information_schema.columns.data_type`, is-nullable), `constraints` to
    (`pg_get_constraintdef`, `convalidated`) and `indexes` to `pg_indexes.
    indexdef`, so a state built by hand is the same kind of thing a state read
    from a cursor is.
    """

    present: bool = True
    columns: Dict[str, "ObservedColumn"] = field(default_factory=dict)
    constraints: Dict[str, Tuple[str, bool]] = field(default_factory=dict)
    indexes: Dict[str, str] = field(default_factory=dict)
    triggers: Dict[str, "ObservedTrigger"] = field(default_factory=dict)


def observe_relation_state(cur, relation: RelationRequirement) -> RelationState:
    """Read the live catalog for one relation. THE ONLY database half.

    Queries only what the requirement actually asserts, which is what the
    single-function version did and is worth keeping: a relation declaring no
    index requirement should not make the gate scan `pg_indexes`.

    Rows are read POSITIONALLY, which is what psycopg's default row factory and
    both connection factories in this module produce. A caller supplying a
    cursor with a mapping row factory must ask for `tuple_row` here.
    """
    qualified = f"{relation.schema}.{relation.table}"
    cur.execute("SELECT to_regclass(%s) AS relation", (qualified,))
    row = cur.fetchone()
    if row is None or row[0] is None:
        return RelationState(present=False)

    cur.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema = %s AND table_name = %s
        """,
        (relation.schema, relation.table),
    )
    columns = {
        str(r[0]): ObservedColumn(
            type=str(r[1]),
            nullable=str(r[2]).upper() == "YES",
            default=(None if r[3] is None else str(r[3])),
        )
        for r in cur.fetchall()
    }

    constraints: Dict[str, Tuple[str, bool]] = {}
    if relation.constraints or relation.constraint_alternatives:
        cur.execute(
            """
            SELECT conname, pg_get_constraintdef(oid), convalidated
              FROM pg_constraint
             WHERE conrelid = %s::regclass
            """,
            (qualified,),
        )
        constraints = {
            str(r[0]): (str(r[1]), bool(r[2])) for r in cur.fetchall()
        }

    indexes: Dict[str, str] = {}
    if relation.indexes:
        cur.execute(
            """
            SELECT indexname, indexdef FROM pg_indexes
             WHERE schemaname = %s AND tablename = %s
            """,
            (relation.schema, relation.table),
        )
        indexes = {str(r[0]): str(r[1]) for r in cur.fetchall()}

    triggers: Dict[str, ObservedTrigger] = {}
    if relation.triggers:
        # Structural, not `pg_get_triggerdef`: that function qualifies the
        # function name against the session `search_path`, so the same trigger
        # would render differently in two databases and a text comparison would
        # be a comparison of search paths. Internal triggers (the ones
        # PostgreSQL creates for foreign keys) are excluded — they are not
        # declarable and never what a requirement means.
        cur.execute(
            """
            SELECT t.tgname, t.tgtype, t.tgenabled,
                   fn.nspname || '.' || p.proname AS function_ident,
                   pg_get_expr(t.tgqual, t.tgrelid) AS when_clause,
                   COALESCE(
                     (SELECT array_agg(a.attname ORDER BY a.attnum)
                        FROM unnest(t.tgattr::int2[]) AS x(attnum)
                        JOIN pg_attribute a
                          ON a.attrelid = t.tgrelid AND a.attnum = x.attnum),
                     ARRAY[]::name[]
                   ) AS update_columns
              FROM pg_trigger t
              JOIN pg_proc p ON p.oid = t.tgfoid
              JOIN pg_namespace fn ON fn.oid = p.pronamespace
             WHERE t.tgrelid = %s::regclass AND NOT t.tgisinternal
            """,
            (qualified,),
        )
        for row in cur.fetchall():
            timing, events, level = _decode_trigger_type(row[1])
            triggers[str(row[0])] = ObservedTrigger(
                timing=timing,
                events=events,
                level=level,
                function=str(row[3]),
                enabled=str(row[2]),
                condition=(None if row[4] is None else str(row[4])),
                update_columns=tuple(sorted(str(c) for c in (row[5] or ()))),
            )

    return RelationState(
        present=True, columns=columns, constraints=constraints,
        indexes=indexes, triggers=triggers,
    )


def _column_default_defects(
    column: ColumnRequirement, found: "ObservedColumn", qualified: str,
) -> List[str]:
    """Decide ONE column's declared default state against the catalog.

    Absence is enforced as strictly as presence. A column the requirement says
    must have no default and that carries one is a defect even though nothing
    is missing from the schema — that is the whole point: `provider_name
    DEFAULT 'fake'` adds something, and what it breaks is the narrow lifecycle
    INSERT that depends on the column staying implicitly NULL.
    """
    requirement = column.default
    observed = found.default
    if requirement.state == DEFAULT_STATE_ABSENT:
        if observed is None:
            return []
        return [
            f"column_default_present:{qualified}.{column.name}"
            f":expected=no_default"
            f":actual={_normalize_definition(observed)}"
        ]
    expected = _normalize_definition(requirement.expression)
    if observed is None or _normalize_definition(observed) != expected:
        return [
            f"column_default_mismatch:{qualified}.{column.name}"
            f":expected={expected}"
            f":actual={'' if observed is None else _normalize_definition(observed)}"
        ]
    return []


def relation_state_defects(
    state: RelationState, relation: RelationRequirement,
) -> List[str]:
    """THE authoritative meaning of a complete `RelationRequirement`.

    Every preflight-relevant field of the requirement is decided here and
    nowhere else: relation existence, `columns`, unconditional `constraints`,
    `constraint_alternatives` and `indexes`. Empty list means the requirement
    holds against `state` in full.

    Any caller that needs to know whether a packaged relation requirement is
    satisfiable — against the live database or against a modeled state — must
    come through this function. A caller that re-derives part of the answer is
    the reviewed defect class, not an optimization.
    """
    defects: List[str] = []
    qualified = f"{relation.schema}.{relation.table}"
    if not state.present:
        return [f"relation_absent:{qualified}"]

    for column in relation.columns:
        found = state.columns.get(column.name)
        if found is None:
            defects.append(f"column_absent:{qualified}.{column.name}")
            continue
        if found.type != column.type:
            defects.append(
                f"column_type_mismatch:{qualified}.{column.name}"
                f":expected={column.type}:actual={found.type}"
            )
        if column.nullable is not None and found.nullable != column.nullable:
            defects.append(
                f"column_nullability_mismatch:{qualified}.{column.name}"
                f":expected_nullable={column.nullable}"
            )
        if column.default is not None:
            defects.extend(
                _column_default_defects(column, found, qualified)
            )

    for constraint in relation.constraints:
        defects.extend(
            _constraint_defects(state.constraints, constraint, qualified)
        )
    if relation.constraint_alternatives:
        defects.extend(
            _alternative_state_defects(
                state.constraints, relation.constraint_alternatives, qualified
            )
        )

    for index in relation.indexes:
        observed_def = state.indexes.get(index.name)
        if observed_def is None:
            defects.append(f"index_absent:{qualified}.{index.name}")
            continue
        if _normalize_definition(observed_def) != _normalize_definition(
            index.definition
        ):
            defects.append(
                f"index_definition_mismatch:{qualified}.{index.name}"
                f":actual={_normalize_definition(observed_def)}"
            )

    for trigger in relation.triggers:
        defects.extend(_trigger_defects(state.triggers, trigger, qualified))
    return defects


def relation_defects(cur, relation: RelationRequirement) -> List[str]:
    """Physical assertions for one relation. Empty list means it checks out.

    Observation plus decision, in that order. The decision half is shared with
    the bridge gate (`relation_state_defects`), which is what makes "G6 accepted
    it" and "preflight would accept it" the same sentence about this relation.
    """
    return relation_state_defects(observe_relation_state(cur, relation), relation)


@dataclass(frozen=True)
class FunctionState:
    """A catalog observation of ONE function, in the shape the matcher reads."""

    present: bool = True
    language: Optional[str] = None
    returns: Optional[str] = None
    body_sha256: Optional[str] = None


def observe_function_state(cur, function: FunctionRequirement) -> FunctionState:
    """Read the live catalog for one function identity. Read-only.

    The digest is computed by PostgreSQL over `prosrc` so the bytes never leave
    the server and no client-side encoding step can change the answer.
    """
    cur.execute(
        """
        SELECT l.lanname,
               pg_get_function_result(p.oid),
               encode(sha256(convert_to(p.prosrc, 'UTF8')), 'hex')
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
          JOIN pg_language l ON l.oid = p.prolang
         WHERE n.nspname = %s
           AND p.proname = %s
           AND pg_get_function_identity_arguments(p.oid) = %s
        """,
        (function.schema, function.name, function.arguments),
    )
    row = cur.fetchone()
    if row is None:
        return FunctionState(present=False)
    return FunctionState(
        present=True,
        language=str(row[0]),
        returns=str(row[1]),
        body_sha256=str(row[2]),
    )


def function_state_defects(
    state: FunctionState, function: FunctionRequirement,
) -> List[str]:
    """THE authoritative meaning of a `FunctionRequirement`."""
    if not state.present:
        return [f"function_absent:{function.identity}"]
    defects: List[str] = []
    if function.language is not None and state.language != function.language:
        defects.append(
            f"function_language_mismatch:{function.identity}"
            f":expected={function.language}:actual={state.language}"
        )
    if function.returns is not None and state.returns != function.returns:
        defects.append(
            f"function_result_mismatch:{function.identity}"
            f":expected={function.returns}:actual={state.returns}"
        )
    if function.body_sha256 is not None and (
        (state.body_sha256 or "").lower() != function.body_sha256
    ):
        # The no-op replacement case. Nothing structural changes when a body is
        # swapped, so nothing structural can detect it.
        defects.append(
            f"function_body_mismatch:{function.identity}"
            f":expected_sha256={function.body_sha256}"
            f":actual_sha256={state.body_sha256 or ''}"
        )
    return defects


def function_defects(cur, function: FunctionRequirement) -> List[str]:
    """Physical assertions for one function. Empty list means it checks out."""
    return function_state_defects(
        observe_function_state(cur, function), function
    )


def requirement_defects(cur, requirement: SchemaRequirement) -> List[str]:
    """Every physical assertion ONE requirement makes, in one place.

    Relations and schema-level functions are both parts of the same
    declaration, so activation must never evaluate one without the other. Both
    verification passes come through here for exactly that reason.
    """
    defects: List[str] = []
    for relation in requirement.relations:
        defects.extend(relation_defects(cur, relation))
    for function in requirement.functions:
        defects.extend(function_defects(cur, function))
    return defects


# ---------------------------------------------------------------------------
# Fleet enumeration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AffectedClient:
    client_id: str
    client_code: Optional[str]
    client_name: Optional[str]
    db_host: str
    db_port: int
    db_name: str

    def identity(self) -> str:
        return "|".join([
            self.client_id, self.client_code or "", self.db_host,
            str(self.db_port), self.db_name,
        ])


#: The one authoritative relation defining fleet membership and each client's
#: business-database coordinates. `enabled` lives here; so do `client_db_host`,
#: `client_db_port` and `client_db_name`. Nothing else resolves a Workflow A
#: client business database — `portal_clients.database_name` belongs to the
#: Portal Database Explorer and is never consulted by ingest. Because membership
#: is defined by a single table, the activation fence is a single table lock.
FLEET_RELATION = "workflow_a_control.client_account"


def enumerate_affected_clients(cur) -> Tuple[List[AffectedClient], int]:
    """**Every** authoritative enabled client account. Returns `(clients, count)`.

    WHAT CHANGED, AND WHY THE PREVIOUS RULE WAS WRONG.
        This used to additionally require the client to already own a
        `trips_sync` schedule. Independent review reproduced the consequence:
        four enabled accounts, three with schedules, the fourth's database
        absent — the gate examined three, found them healthy, and passed. The
        account-count guard only caught a *completely* empty result, never a
        partial one, so the omission was invisible.

        The requirement in `db/schema_requirements.json` was never "clients that
        can currently fire". It is that the enabled fleet's databases carry the
        schema the release needs. A client with no schedule today is one
        onboarding step away from having one, and its database is just as real.
        So the eligibility filter is gone: **enabled is the whole rule**.

    STRUCTURAL CONSEQUENCE. The returned set is now identical to the enabled
    account set by construction — one table, one predicate, one query. Partial
    enumeration is no longer something to detect; it is unrepresentable.
    `_verify_fleet` asserts the two counts agree regardless, so a future edit
    that reintroduces a filter fails loudly instead of silently narrowing the
    gate.

    There is no filter parameter, by design. A gate an operator can narrow is
    not a gate.
    """
    cur.execute(
        f"""
        SELECT ca.client_id::text AS client_id,
               ca.client_code,
               ca.client_name,
               ca.client_db_host,
               ca.client_db_port,
               ca.client_db_name
          FROM {FLEET_RELATION} ca
         WHERE ca.enabled = true
         ORDER BY ca.client_id
        """
    )
    affected = [
        AffectedClient(
            client_id=str(r[0]),
            client_code=None if r[1] is None else str(r[1]),
            client_name=None if r[2] is None else str(r[2]),
            db_host=str(r[3]),
            db_port=int(r[4]),
            db_name=str(r[5]),
        )
        for r in cur.fetchall()
    ]
    cur.execute(f"SELECT count(*) FROM {FLEET_RELATION} WHERE enabled = true")
    enabled_accounts = int(cur.fetchone()[0])
    return affected, enabled_accounts


def fleet_fingerprint(affected: List[AffectedClient], enabled_accounts: int) -> str:
    """Stable digest of the authoritative fleet this gate validated.

    It answers exactly one question: *is the fleet the expensive validation pass
    examined still the authoritative fleet?* `activation_fence` recomputes it
    under a `SHARE` lock on the fleet relation and refuses the activation if it
    has moved, which catches a mutation that committed **before** the fence was
    taken.

    It is not, by itself, the concurrency guarantee. A mutation attempted
    **during** the fence cannot commit at all until after the pointer swap,
    because the lock blocks it in PostgreSQL — for any session, including raw
    SQL. Fingerprint comparison covers the before-fence window; the table lock
    covers the during-fence window. Neither substitutes for the other.
    """
    payload = json.dumps(
        {
            "enabled_accounts": enabled_accounts,
            "affected": [client.identity() for client in affected],
        },
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Connection factories (overridable for tests; never used against production here)
# ---------------------------------------------------------------------------
#
# TWO KINDS OF CONNECTION, AND THEY ARE NOT INTERCHANGEABLE.
#
#   SCHEMA INSPECTION — `default_platform_conn`, `default_client_conn`.
#       Everything release-schema preflight does: read a ledger, read the
#       catalog, read a function body. It needs no write privilege of any kind,
#       so the SERVER is told to refuse writes on the session outright.
#
#   THE ACTIVATION FENCE — `default_fence_conn`.
#       A separate connection with its own contract: it opens a transaction,
#       takes `pg_advisory_xact_lock` and `LOCK TABLE ... IN SHARE MODE`, and
#       holds both across the caller's pointer swap. That contract is about
#       LOCKING, not about inspection, and it is deliberately NOT redefined
#       here. Independent review's requirement was that schema inspection be
#       read-only at the database, not that every release-management session
#       be reclassified — so the two factories stay distinct and the fence keeps
#       exactly the session semantics it was reviewed with.
#
# WHY THE ENFORCEMENT IS A LIBPQ STARTUP OPTION.
#     `options=-c default_transaction_read_only=on` is applied by the server
#     when the session is established, before the first statement of the first
#     transaction. So there is no window before it takes effect, `BEGIN` /
#     `COMMIT` / `ROLLBACK` do not reset it, and a mutating statement the gate
#     ASSEMBLED AT RUNTIME — an f-string, `"UP" + "DATE ..."`, an identifier
#     read out of a catalog — is refused by PostgreSQL with SQLSTATE 25006
#     exactly like a literal one. A keyword scan over the SQL a cursor sends is
#     kept as a test oracle in `test_release_schema_preflight_postgres.py`, and
#     it is only that: the boundary is the database.

#: libpq startup options that make PostgreSQL itself refuse every write on the
#: session. Passed to `connect()` rather than issued as a statement afterwards,
#: so it can never be preceded by the statement it exists to stop.
READ_ONLY_SESSION_OPTIONS = "-c default_transaction_read_only=on"


def _require_psycopg():
    try:
        import psycopg  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment defect
        raise SchemaPreflightError(
            "RELEASE_SCHEMA_PREFLIGHT_DEPENDENCY_MISSING",
            "psycopg is required to verify activation schema prerequisites",
        ) from exc
    return __import__("psycopg")


def _load_platform_credentials() -> None:
    """Read the host's `.env` for the connection settings, if they are not set.

    The preflight talks to the production databases, and its settings come from
    the environment. An operator running this CLI by hand has no reason to have
    exported them, so before this the DSN fell back to `password=` — an EMPTY
    password — the connection was refused, and the refusal surfaced as
    `RELEASE_SCHEMA_PLATFORM_UNREACHABLE`: "the platform database could not be
    reached". The database was fine. The credentials were never supplied.

    That error is read during a release, sometimes during an incident, and it
    pointed at the wrong system. So the file the release links to as `.env` — the
    same one production runs with — is read here instead of being demanded of
    the caller.

    `override=False`: anything already exported wins, so a deliberate override on
    the command line still does what it says.
    """
    if os.getenv("POSTGRES_PASSWORD") is not None:
        return
    #: The literal rather than `release_boundary.RUNTIME_LINK_ENV`: this module
    #: is imported BY that one, and reaching back would make the import circular.
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path, override=False)


def _platform_dsn() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )


def _client_dsn(client: AffectedClient) -> str:
    return (
        f"host={client.db_host} port={client.db_port} "
        f"dbname={client.db_name} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )


def _credentials_present() -> bool:
    """Whether a password was configured at all, from any source."""
    return bool(os.getenv("POSTGRES_PASSWORD"))


def default_platform_conn():
    """The PRODUCTION platform inspection session. Read-only at the server.

    This is the factory `manage_release.py` actually uses, which is the whole
    point: the previous evidence that the gate could not write was produced by
    an injected test factory that set the option, while this function — the one
    a real activation runs — did not, so nothing about production was proved.
    """
    _load_platform_credentials()
    psycopg = _require_psycopg()
    return psycopg.connect(_platform_dsn(), options=READ_ONLY_SESSION_OPTIONS)


def default_client_conn(client: AffectedClient):
    """The PRODUCTION client-business inspection session. Read-only at the server.

    Every client pass goes through here — the per-client requirement check in
    `_verify_fleet` and the state-guard read in `check_schema_state_guards`,
    including the one the activation fence performs — so a client database is
    never opened by this module on a session that could write to it.
    """
    _load_platform_credentials()
    psycopg = _require_psycopg()
    return psycopg.connect(_client_dsn(client), options=READ_ONLY_SESSION_OPTIONS)


def default_fence_conn():
    """The activation/transition LOCK session. Deliberately not the above.

    `activation_fence` and `schema_transition_lock` need lock and transaction
    semantics, not catalog inspection, and their contract was reviewed as it
    stands. Kept as its own factory so that making inspection read-only cannot
    silently redefine the fence, and so that a future change to either one has
    to be made to the connection it actually concerns.
    """
    psycopg = _require_psycopg()
    return psycopg.connect(_platform_dsn())


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@dataclass
class PreflightReport:
    release_id: str
    note: str
    requirements_checked: int = 0
    platform_checks: List[Dict[str, Any]] = field(default_factory=list)
    client_checks: List[Dict[str, Any]] = field(default_factory=list)
    affected_client_count: int = 0
    enabled_account_count: int = 0
    fleet_fingerprint: Optional[str] = None
    declared_capabilities: List[str] = field(default_factory=list)
    #: Guards the fleet actually asserts, per client. Recorded even when they
    #: are satisfied, so an activation record proves which narrowing states the
    #: release was checked against rather than only which ones refused it.
    schema_state_guards: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "release_id": self.release_id,
            "note": self.note,
            "requirements_checked": self.requirements_checked,
            "affected_client_count": self.affected_client_count,
            "enabled_account_count": self.enabled_account_count,
            "fleet_fingerprint": self.fleet_fingerprint,
            "declared_capabilities": self.declared_capabilities,
            "schema_state_guards": self.schema_state_guards,
            "platform_checks": self.platform_checks,
            "client_checks": self.client_checks,
        }


def verify_schema_prerequisites(
    *,
    release_tree: Path,
    release_id: str,
    platform_conn_factory=default_platform_conn,
    client_conn_factory=default_client_conn,
) -> PreflightReport:
    """Prove the target release's schema prerequisites hold. Raises or returns.

    Strictly read-only and side-effect free: it issues only `SELECT`s, takes no
    lock of any kind, and leaves no open transaction. This is the *expensive*
    pass — it opens a connection to every enabled client's business database —
    so fencing it would block ordinary control-plane writes for its whole
    duration to no purpose.

    Its result is therefore bound to a specific fleet by `fleet_fingerprint`,
    and `activation_fence` re-establishes that fleet under a `SHARE` table lock
    immediately before the pointer swap. Validation is expensive and unfenced;
    the fence is cheap and continuous.
    """
    requirements, capabilities, note = load_release_requirements(release_tree)
    report = PreflightReport(release_id=release_id, note=note)
    report.requirements_checked = len(requirements)
    report.declared_capabilities = sorted(capabilities)

    platform_requirements = [
        r for r in requirements if r.scope == SCOPE_PLATFORM
    ]
    client_requirements = [
        r for r in requirements if r.scope == SCOPE_CLIENT_BUSINESS
    ]

    try:
        conn = platform_conn_factory()
    except Exception as exc:
        # Two very different failures used to arrive here as one message. An
        # activation that says "the database could not be reached" when the
        # database is fine and no password was configured sends the operator to
        # inspect Postgres during a release. Name which one it is.
        if not _credentials_present():
            raise SchemaPreflightError(
                "RELEASE_SCHEMA_PLATFORM_CREDENTIALS_MISSING",
                "no database password is configured, so the activation "
                "prerequisites could not be verified: set POSTGRES_PASSWORD in "
                "the environment, or provide it in the repository's .env — this "
                "is NOT evidence that the database is unreachable",
            ) from exc
        raise SchemaPreflightError(
            "RELEASE_SCHEMA_PLATFORM_UNREACHABLE",
            "the platform database could not be reached to verify activation "
            "prerequisites at "
            f"{os.getenv('POSTGRES_HOST', '127.0.0.1')}:"
            f"{os.getenv('POSTGRES_PORT', '5432')} as "
            f"{os.getenv('POSTGRES_USER', 'loguser')} "
            f"({type(exc).__name__})",
        ) from exc

    try:
        with conn.cursor() as cur:
            # No lock here, deliberately. This pass is the expensive one — it
            # opens a connection to every client database — and holding a fence
            # across it would block ordinary control-plane writes for its whole
            # duration for no benefit. Its result is bound to a specific fleet
            # by `fleet_fingerprint`, and `activation_fence` re-establishes that
            # fleet under a real write lock before the pointer moves. Validation
            # is expensive and unfenced; the fence is cheap and continuous.
            _verify_platform(cur, platform_requirements, report)
            _verify_fleet(
                cur, client_requirements, report,
                capabilities=capabilities,
                client_conn_factory=client_conn_factory,
            )
        conn.rollback()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return report


def _verify_platform(
    cur, requirements: List[SchemaRequirement], report: PreflightReport,
) -> None:
    for requirement in requirements:
        recorded = ledger_has_migration(cur, requirement.migration)
        defects = requirement_defects(cur, requirement)
        report.platform_checks.append({
            "migration": requirement.migration,
            "ledger_recorded": recorded,
            "physical_defects": defects,
        })
        if not recorded:
            raise SchemaPreflightError(
                "RELEASE_SCHEMA_PLATFORM_MIGRATION_MISSING",
                f"platform migration {requirement.migration} is not recorded in "
                "public.schema_migrations; the release requires it",
                migration=requirement.migration, milestone=requirement.milestone,
            )
        if defects:
            raise SchemaPreflightError(
                "RELEASE_SCHEMA_PLATFORM_OBJECT_MISSING",
                f"platform migration {requirement.migration} is recorded applied "
                f"but the schema it must create is not present: "
                f"{'; '.join(sorted(defects))}",
                migration=requirement.migration,
            )


def _verify_fleet(
    cur,
    requirements: List[SchemaRequirement],
    report: PreflightReport,
    *,
    capabilities: frozenset,
    client_conn_factory,
) -> None:
    """Prove the fleet satisfies the release, AND that the release satisfies the fleet.

    Both directions run, and the second one runs unconditionally. A release that
    declares no client-business requirement used to skip the fleet entirely,
    which is precisely the release a `SchemaStateGuard` must reject: an M4-era
    tree declares nothing about the first-seen pair CONTRACT and would otherwise
    activate against a database that has already closed it.
    """
    affected, enabled_accounts = enumerate_affected_clients(cur)
    report.affected_client_count = len(affected)
    report.enabled_account_count = enabled_accounts
    report.fleet_fingerprint = fleet_fingerprint(affected, enabled_accounts)

    # The enumeration and the count come from one table and one predicate, so
    # they cannot legitimately disagree. Asserted anyway: this is exactly the
    # invariant a reintroduced eligibility filter would break, and independent
    # review already caught one such filter passing a partial fleet. A mismatch
    # means the gate is about to check fewer databases than the release
    # requires, which must refuse rather than pass.
    if len(affected) != enabled_accounts:
        raise SchemaPreflightError(
            "RELEASE_SCHEMA_FLEET_ENUMERATION_PARTIAL",
            f"enumerated {len(affected)} client(s) but {enabled_accounts} "
            "enabled account(s) exist; the prerequisite applies to every "
            "enabled client and a partially enumerated fleet must never pass",
            enumerated=len(affected), enabled_accounts=enabled_accounts,
        )

    if not affected:
        # A genuinely empty control plane. The client-business requirement is
        # vacuously satisfied — there is no client database to be unmigrated —
        # but it is recorded explicitly so the report can never be misread as
        # "the fleet was verified". With the corrected enumeration this is the
        # only way `affected` can be empty: it now means what it says. There is
        # likewise no database that could assert a state guard.
        report.note = "no_enabled_client_accounts"
        return

    # The DATABASE-asserted direction first, for the whole fleet. It is
    # independent of what the release declares, which is what lets it refuse a
    # release that declares nothing at all. `activation_fence` re-runs this
    # exact function under the transition lock immediately before the pointer
    # swap, so this pass is an early, cheap refusal and not the authority.
    report.schema_state_guards.extend(check_schema_state_guards(
        affected, capabilities=capabilities,
        client_conn_factory=client_conn_factory,
    ))

    if not requirements:
        # Nothing further to read per client. The guard pass above already ran
        # against every one of them, which is the direction that binds a release
        # declaring no requirement at all.
        return

    for client in affected:
        try:
            client_conn = client_conn_factory(client)
        except Exception as exc:
            raise SchemaPreflightError(
                "RELEASE_SCHEMA_CLIENT_UNREACHABLE",
                f"client {client.client_code or client.client_id} database "
                f"{client.db_name} could not be reached; every affected client "
                "must be provably migrated before activation",
                client_id=client.client_id, client_code=client.client_code,
            ) from exc
        try:
            with client_conn.cursor() as client_cur:
                for requirement in requirements:
                    recorded = ledger_has_migration(
                        client_cur, requirement.migration
                    )
                    defects = requirement_defects(client_cur, requirement)
                    report.client_checks.append({
                        "client_id": client.client_id,
                        "client_code": client.client_code,
                        "migration": requirement.migration,
                        "ledger_recorded": recorded,
                        "physical_defects": defects,
                    })
                    if not recorded:
                        raise SchemaPreflightError(
                            "RELEASE_SCHEMA_CLIENT_MIGRATION_MISSING",
                            f"client {client.client_code or client.client_id} "
                            f"has not recorded {requirement.migration}",
                            client_id=client.client_id,
                            client_code=client.client_code,
                            migration=requirement.migration,
                        )
                    if defects:
                        raise SchemaPreflightError(
                            "RELEASE_SCHEMA_CLIENT_OBJECT_MISSING",
                            f"client {client.client_code or client.client_id} "
                            f"records {requirement.migration} as applied but the "
                            f"schema is not present: {'; '.join(sorted(defects))}",
                            client_id=client.client_id,
                            client_code=client.client_code,
                            migration=requirement.migration,
                        )
            client_conn.rollback()
        finally:
            try:
                client_conn.close()
            except Exception:
                pass


def _set_lock_timeout(cur, lock_timeout: str) -> None:
    """`SET LOCAL lock_timeout`, validated. `SET LOCAL` takes no bind parameter."""
    if not re.fullmatch(r"[0-9]+(ms|s|min)?", str(lock_timeout)):
        raise SchemaPreflightError(
            "RELEASE_SCHEMA_FLEET_FENCE_UNAVAILABLE",
            f"invalid lock_timeout {lock_timeout!r}",
        )
    cur.execute(f"SET LOCAL lock_timeout = '{lock_timeout}'")


@contextmanager
def schema_transition_lock(
    *,
    platform_conn_factory=default_fence_conn,
    lock_timeout: str = "30s",
    key: int = SCHEMA_TRANSITION_LOCK_KEY,
):
    """Hold the activation/transition lock for a whole client-schema transition.

    For the CLOSURE side, which cannot use a transaction-scoped lock: the
    closure deliberately spans several transactions in several *client*
    databases (swap, validate, ledger), so the lock must outlive any one of
    them. A session-level `pg_advisory_lock` on a dedicated platform connection
    does exactly that, and is released by `pg_advisory_unlock` — and, belt and
    braces, by the connection closing in the `finally`.

    The activation side takes the same key transaction-scoped inside
    `activation_fence`, which is stricter (it cannot leak past the transaction)
    and is all that side needs.

    Bounded by `lock_timeout` so a closure blocked behind a live activation
    refuses loudly instead of hanging. It issues no statement that modifies
    anything in the platform database.
    """
    conn = platform_conn_factory()
    acquired = False
    try:
        try:
            with conn.cursor() as cur:
                _set_lock_timeout(cur, lock_timeout)
                cur.execute("SELECT pg_advisory_lock(%s)", (key,))
                cur.fetchone()
            acquired = True
        except SchemaPreflightError:
            raise
        except Exception as exc:
            raise SchemaPreflightError(
                "RELEASE_SCHEMA_TRANSITION_LOCK_UNAVAILABLE",
                "the release/schema-transition lock could not be acquired "
                f"({type(exc).__name__}); a release activation is most likely "
                "in progress, so refusing rather than transitioning a client "
                "schema underneath it",
            ) from exc
        yield key
    finally:
        if acquired:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
                    cur.fetchone()
            except Exception:
                pass
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def check_schema_state_guards(
    affected: List["AffectedClient"],
    *,
    capabilities: frozenset,
    client_conn_factory,
) -> List[Dict[str, Any]]:
    """Read every client's narrowing state and prove the release declares it.

    The DATABASE-asserted direction, factored out so the unfenced validation
    pass and the fenced pre-swap re-read cannot drift apart. Raises on the first
    asserted guard the release does not declare; returns the record of every
    guard it examined otherwise.
    """
    observed: List[Dict[str, Any]] = []
    for client in affected:
        try:
            conn = client_conn_factory(client)
        except Exception as exc:
            raise SchemaPreflightError(
                "RELEASE_SCHEMA_CLIENT_UNREACHABLE",
                f"client {client.client_code or client.client_id} database "
                f"{client.db_name} could not be reached; every affected client "
                "must be provably compatible before activation",
                client_id=client.client_id, client_code=client.client_code,
            ) from exc
        try:
            with conn.cursor() as cur:
                for guard in detect_schema_state_guards(
                    cur, scope=SCOPE_CLIENT_BUSINESS
                ):
                    satisfied = guard.capability in capabilities
                    observed.append({
                        "client_id": client.client_id,
                        "client_code": client.client_code,
                        "capability": guard.capability,
                        "constraint": f"{guard.schema}.{guard.table}"
                                      f".{guard.constraint}",
                        "declared_by_release": satisfied,
                    })
                    if not satisfied:
                        raise SchemaPreflightError(
                            "RELEASE_SCHEMA_STATE_CAPABILITY_MISSING",
                            f"client {client.client_code or client.client_id} "
                            f"has {guard.constraint} installed and the release "
                            f"does not declare capability "
                            f"{guard.capability!r}; {guard.reason}",
                            client_id=client.client_id,
                            client_code=client.client_code,
                            capability=guard.capability,
                        )
            conn.rollback()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return observed


@contextmanager
def activation_fence(
    *,
    expected_fingerprint: Optional[str],
    declared_capabilities: frozenset = frozenset(),
    platform_conn_factory=default_fence_conn,
    client_conn_factory=default_client_conn,
    lock_timeout: str = "30s",
):
    """Hold the authoritative fleet AND the schema-transition boundary still,
    continuously, across the pointer swap.

    THE FIRST RACE THIS CLOSES — FLEET MEMBERSHIP.
        The original implementation validated the fleet, released its lock,
        re-checked a fingerprint on a fresh connection, and *then* swapped the
        pointer. Independent review proved a mutation could commit between the
        re-check and the swap. It also relied on an advisory lock, which only
        constrains code that volunteers to take it — direct SQL simply ignored
        it. Both problems are the same problem: the protected interval ended
        before the thing it was protecting.

    THE SECOND RACE THIS CLOSES — CLIENT SCHEMA STATE.
        Fencing the fleet table is not enough, because the state that decides
        whether a release may activate is not in the platform database at all.
        `SCHEMA_STATE_GUARDS` is read from every CLIENT business database during
        validation, the connection closes, and the CONTRACT closure is client
        DDL that takes no platform lock whatsoever. Independent review
        reproduced the result: a CONTRACT closure committed *inside* the
        activation fence of a legacy release whose capability check had already
        passed, with the pointer swap still ahead of it. A later recheck would
        have refused, but it would have refused after the swap it was meant to
        protect.

    HOW BOTH ARE CLOSED. One transaction, opened here and held until the
    caller's body has finished — which is to say, until after the pointer has
    moved:

        BEGIN
        SELECT pg_advisory_xact_lock(SCHEMA_TRANSITION_LOCK_KEY)
        LOCK TABLE workflow_a_control.client_account IN SHARE MODE
        re-enumerate + fingerprint, compare to the validated fleet
        re-read every client's narrowing state, AUTHORITATIVELY
        <caller swaps the release pointer>
        ROLLBACK        -- read-only; releases both locks

    `SHARE` is the minimum table mode that does the job: it conflicts with
    `ROW EXCLUSIVE`, which every `INSERT`/`UPDATE`/`DELETE` must take, so **any**
    session blocks on a fleet write until this transaction ends — including raw
    SQL, which is the case an advisory lock could not cover. It does not
    conflict with `ACCESS SHARE`, so ordinary reads are unaffected.

    `SCHEMA_TRANSITION_LOCK_KEY` covers the other race, and an advisory lock is
    the correct primitive for it precisely where it was the wrong one for the
    fleet: the only way to move a client across the EXPAND/CONTRACT boundary is
    to run `ops/close_telematics_first_seen_pair_contract.py`, which takes the
    same key. The state-guard re-read below therefore happens while no closure
    can be in flight, and its answer stays true until this transaction ends —
    after the swap. If the closure won the lock first, it has already committed
    and this re-read observes the strict constraint and refuses here, before the
    pointer moves.

    WHY ONE TABLE IS ENOUGH FOR THE FLEET.
        `client_account` is the sole authoritative definition of both fleet
        membership (`enabled`) and each client's business-database coordinates.
        Fleet membership cannot change without writing that table, so fencing it
        fences the property.

    ON FAILURE. Any exception — including a failed pointer swap — propagates
    after the transaction is rolled back and the connection closed, so the locks
    are always released and the fence itself never leaves a write behind. It
    issues no statement that modifies anything.
    """
    if expected_fingerprint is None:
        # The caller could not establish an authoritative fleet at all — which,
        # since the guard pass became unconditional, only an injected preflight
        # can produce. Yield without fencing a property nobody asserted.
        yield None
        return

    conn = platform_conn_factory()
    try:
        try:
            with conn.cursor() as cur:
                # Bounded: an activation must fail loudly rather than hang
                # forever behind an unrelated long transaction or a running
                # closure.
                _set_lock_timeout(cur, lock_timeout)
                # Transition lock FIRST, then the table lock. The closure side
                # takes the same key first and takes no fleet lock at all, so
                # the acquisition order is consistent and the two cannot
                # deadlock. Transaction-scoped: it cannot outlive this block.
                cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (SCHEMA_TRANSITION_LOCK_KEY,),
                )
                cur.fetchone()
                cur.execute(f"LOCK TABLE {FLEET_RELATION} IN SHARE MODE")
                affected, enabled_accounts = enumerate_affected_clients(cur)
        except SchemaPreflightError:
            raise
        except Exception as exc:
            raise SchemaPreflightError(
                "RELEASE_SCHEMA_FLEET_FENCE_UNAVAILABLE",
                "the authoritative client fleet could not be locked for "
                f"activation ({type(exc).__name__}); refusing rather than "
                "activating against an unfenced fleet",
            ) from exc

        observed = fleet_fingerprint(affected, enabled_accounts)
        if observed != expected_fingerprint:
            # Something committed between validation and this fence. The
            # validated schema facts describe a fleet that is no longer the
            # authoritative one, so they prove nothing about the current one.
            raise SchemaPreflightError(
                "RELEASE_SCHEMA_FLEET_CHANGED_DURING_ACTIVATION",
                "the authoritative client fleet changed between prerequisite "
                "verification and pointer activation; re-run activation so the "
                "new fleet is verified",
                expected=expected_fingerprint, observed=observed,
            )

        # THE AUTHORITATIVE capability decision, taken here and nowhere else.
        # The unfenced validation pass made the same check cheaply and early;
        # this one is the one activation is entitled to rely on, because the
        # transition lock is held from before it until after the swap.
        check_schema_state_guards(
            affected,
            capabilities=frozenset(declared_capabilities or ()),
            client_conn_factory=client_conn_factory,
        )

        # Both locks are still held. The caller swaps the pointer inside this
        # `yield`, so neither a fleet write nor a client CONTRACT closure can
        # commit between the checks above and the release becoming current.
        yield observed
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The materialized rollback envelope
# ---------------------------------------------------------------------------
#
# WHY THIS IS NOT AN OPERATOR CHECKBOX.
#     Closing the first-seen pair CONTRACT is irreversible in the only sense
#     that matters operationally: from the moment the strict constraint commits,
#     every release lacking the pair-atomic writer is refused by
#     `SCHEMA_STATE_GUARDS`, including whatever `previous` currently points at.
#     A one-step rollback therefore stops working unless `previous` ALREADY
#     carries the bridge capability.
#
#     `--rollback-window-closed` asserted that the operator accepted that. It
#     could not, and did not, establish it. Independent review made the gap
#     concrete against real production pointers — `current = 18dcda87f16d`,
#     `previous = 20a01b4358b8`, neither carrying the capability — and showed
#     that a single bridge activation leaves `previous` legacy, so the envelope
#     is still not there. It exists only once TWO DISTINCT bridge-compatible
#     releases have been activated in sequence, leaving `current = bridge-B` and
#     `previous = bridge-A`.
#
#     So the proof is read from the release layout: both pointers, both trees,
#     both requirements files. An operator acknowledgement remains required, but
#     it is now an acknowledgement on top of the proof rather than instead of it.


# WHY A SUPPLIED DIRECTORY IS NOT EVIDENCE.
#     The second independent review satisfied the first implementation of this
#     gate with a synthetic directory: `{}` release metadata, pointers escaping
#     the inventory, dangling targets, and bridge declarations carrying
#     `CHECK (false)`. Every one of those was accepted, because the gate looked
#     for shapes — a pointer basename, a metadata file's existence, a constraint
#     NAME — instead of proving the two releases are the ones production would
#     actually run.
#
#     So the proof is now bound to the authoritative release machinery:
#     `ops.release_boundary.verify_release` recomputes each release from its own
#     bytes against its manifest and commit, the pointers are resolved
#     canonically inside `releases/`, and the bridge declaration is compared
#     against the exact canonical definitions below rather than by name.

#: The relation and the two constraint names the bridge declaration must span.
FIRST_SEEN_PAIR_SCHEMA = "public"
FIRST_SEEN_PAIR_TABLE = "client_trips"
FIRST_SEEN_EXPAND_CONSTRAINT = "ck_client_trips_first_seen_instant_needs_request"
FIRST_SEEN_STRICT_CONSTRAINT = "ck_client_trips_first_seen_pairing"

#: THE canonical constraint bodies, defined once for the whole repository.
#: `ops/close_telematics_first_seen_pair_contract.py` imports these rather than
#: restating them, so the bridge contract cannot drift into two definitions that
#: disagree about what the transition is.
#:
#: These are `pg_get_constraintdef` output with any trailing `NOT VALID` marker
#: removed — PostgreSQL reprints from the parsed catalog entry rather than from
#: the migration's source text, so an exact comparison is robust.
FIRST_SEEN_EXPAND_DEFINITION = (
    "CHECK (((first_seen_response_received_at_utc IS NULL) "
    "OR (first_seen_request_id IS NOT NULL)))"
)
FIRST_SEEN_STRICT_DEFINITION = (
    "CHECK (((first_seen_request_id IS NULL) "
    "= (first_seen_response_received_at_utc IS NULL)))"
)

#: What a bridge release's `db/schema_requirements.json` must DECLARE, exactly.
#: The declared form carries the `NOT VALID` marker for the EXPAND state because
#: that is what `pg_get_constraintdef` prints for it and therefore what
#: `_constraint_defects` compares against; the validation flags are the states
#: themselves, not decoration — an EXPAND alternative claiming `validated: true`
#: describes a database migration 048 never produces, and a CONTRACT alternative
#: claiming `validated: false` accepts an INTERRUPTED closure as a closed one.
FIRST_SEEN_EXPAND_DECLARED_DEFINITION = FIRST_SEEN_EXPAND_DEFINITION + " NOT VALID"
FIRST_SEEN_EXPAND_DECLARED_VALIDATED = False
FIRST_SEEN_STRICT_DECLARED_DEFINITION = FIRST_SEEN_STRICT_DEFINITION
FIRST_SEEN_STRICT_DECLARED_VALIDATED = True


#: THE canonical bridge declaration, normalized ONCE for the whole repository.
#:
#: WHY A NORMALIZED STRUCTURE RATHER THAN MORE SEARCHES.
#:     The previous implementation asked "are the canonical members present
#:     somewhere inside this relation's alternatives?". Independent review
#:     answered that with a declaration carrying the canonical EXPAND member
#:     PLUS a synthetic `ck_synthetic_extra` in the same group: G6 reported
#:     READY while the very same release failed canonical EXPAND preflight,
#:     because preflight requires every member of a group to hold and G6 had
#:     ignored the extra one. A presence search can never close that gap — the
#:     only proof that G6 and preflight are talking about the same contract is
#:     that the declared structure EQUALS the contract, member for member.
#:
#: So the expected declaration is built here from the same constants preflight
#: and `ops/close_telematics_first_seen_pair_contract.py` already use, normalized
#: with the same rules, and compared by equality. `state` labels are deliberately
#: excluded: they are diagnostic text the live-schema matcher never reads, so
#: making them part of identity would reject an honest release for a comment.
_ABSENT_DEFINITION = "\x00definition_absent"


def _normalized_bridge_member(
    constraint: "ConstraintRequirement",
) -> Tuple[str, str, bool]:
    """One constraint entry reduced to what the contract actually asserts."""
    return (
        constraint.name,
        _ABSENT_DEFINITION if constraint.definition is None
        else _normalize_definition(constraint.definition),
        bool(constraint.validated),
    )


def _normalized_bridge_group(
    group: "ConstraintAlternativeGroup",
) -> Tuple[Tuple[str, str, bool], ...]:
    """One alternative reduced to its member set, order-insensitively.

    Sorted, so declaration order is not identity; NOT de-duplicated, so a group
    naming the canonical member twice stays distinguishable from one naming it
    once. A duplicated member is a real structural divergence: it is a second
    assertion the live matcher would have to satisfy.
    """
    return tuple(sorted(
        (_normalized_bridge_member(c) for c in group.constraints), key=repr,
    ))


CANONICAL_BRIDGE_EXPAND_GROUP: Tuple[Tuple[str, str, bool], ...] = (
    (
        FIRST_SEEN_EXPAND_CONSTRAINT,
        _normalize_definition(FIRST_SEEN_EXPAND_DECLARED_DEFINITION),
        FIRST_SEEN_EXPAND_DECLARED_VALIDATED,
    ),
)
CANONICAL_BRIDGE_CONTRACT_GROUP: Tuple[Tuple[str, str, bool], ...] = (
    (
        FIRST_SEEN_STRICT_CONSTRAINT,
        _normalize_definition(FIRST_SEEN_STRICT_DECLARED_DEFINITION),
        FIRST_SEEN_STRICT_DECLARED_VALIDATED,
    ),
)

#: Exactly two alternative groups, each carrying exactly its canonical member.
#: Sorted by the same key the observed structure is sorted with, so equality is
#: a multiset comparison: a duplicated group, a third synthetic group, a split
#: of the members across groups and a merge of them into one all compare unequal.
CANONICAL_BRIDGE_ALTERNATIVES: Tuple[Tuple[Tuple[str, str, bool], ...], ...] = (
    tuple(sorted(
        (CANONICAL_BRIDGE_EXPAND_GROUP, CANONICAL_BRIDGE_CONTRACT_GROUP),
        key=repr,
    ))
)


#: THE TWO LIVE RELATION STATES the transition actually produces, modeled in
#: the exact catalog shape `relation_state_defects` consumes.
#:
#: WHY A MODELED STATE AND NOT A LIVE READ.
#:     G6 is asked before any DDL runs, about releases that are not this commit,
#:     and often with no client cursor in reach — that is what makes it usable
#:     as a pre-closure gate at all. So the states it proves against are the two
#:     the transition is DEFINED to produce, built from the same shared
#:     constants the declaration and `ops/close_telematics_first_seen_pair_contract
#:     .py` are built from.
#:
#: WHY A LOWER BOUND IS THE RIGHT MODEL, AND WHY THAT IS FAIL-CLOSED.
#:     These carry only what the transition GOVERNS: the two paired columns and
#:     the one constraint that names the state. A real `public.client_trips`
#:     carries far more — every other column, its primary key, its indexes — and
#:     enumerating them here would be a duplicate of the client schema that
#:     rots. It does not need enumerating, because `relation_state_defects` is
#:     MONOTONE in what the catalog contains: a requirement satisfied by this
#:     subset is satisfied by every richer relation that agrees on the bridge
#:     constraints, since extra columns, constraints and indexes can only
#:     satisfy more requirements, never fewer. The one non-monotone rule is
#:     alternative EXCLUSIVITY, and it is scoped to the participating constraint
#:     NAMES — which are exactly the two these states model.
#:
#:     The direction that leaves is the safe one. G6 proving zero defects here
#:     implies zero defects against the real relation; G6 may refuse a
#:     declaration the real relation would have satisfied (an index requirement,
#:     say, that the bridge contract does not govern), and refusing a release
#:     that asserts something outside the bridge contract is the intended answer,
#:     not a false negative to be tuned away.
#: Neither column carries a default in either transition state, and that is
#: stated rather than left unset: a release declaring one would be describing a
#: `client_trips` this transition never produces, and must fail the canonical
#: check rather than be waved through by an unmodelled field.
CANONICAL_BRIDGE_RELATION_COLUMNS: Dict[str, ObservedColumn] = {
    "first_seen_request_id": ObservedColumn("uuid", True, None),
    "first_seen_response_received_at_utc": ObservedColumn(
        "timestamp with time zone", True, None
    ),
}

CANONICAL_BRIDGE_STATE_EXPAND = RelationState(
    present=True,
    columns=dict(CANONICAL_BRIDGE_RELATION_COLUMNS),
    constraints={
        FIRST_SEEN_EXPAND_CONSTRAINT: (
            FIRST_SEEN_EXPAND_DECLARED_DEFINITION,
            FIRST_SEEN_EXPAND_DECLARED_VALIDATED,
        ),
    },
    indexes={},
)

CANONICAL_BRIDGE_STATE_CONTRACT = RelationState(
    present=True,
    columns=dict(CANONICAL_BRIDGE_RELATION_COLUMNS),
    constraints={
        FIRST_SEEN_STRICT_CONSTRAINT: (
            FIRST_SEEN_STRICT_DECLARED_DEFINITION,
            FIRST_SEEN_STRICT_DECLARED_VALIDATED,
        ),
    },
    indexes={},
)

#: Ordered so a refusal names which side of the transition it failed on.
CANONICAL_BRIDGE_STATES: Tuple[Tuple[str, RelationState], ...] = (
    ("EXPAND", CANONICAL_BRIDGE_STATE_EXPAND),
    ("CONTRACT", CANONICAL_BRIDGE_STATE_CONTRACT),
)


def _canonical_bridge_constraint_defect(
    constraint: "ConstraintRequirement",
    *,
    expected_definition: str,
    expected_validated: bool,
) -> Optional[str]:
    """None when this constraint entry IS the canonical declaration."""
    if constraint.definition is None:
        return "definition_absent"
    if (_normalize_definition(constraint.definition)
            != _normalize_definition(expected_definition)):
        return (
            "definition_not_canonical:"
            f"{_normalize_definition(constraint.definition)}"
        )
    if bool(constraint.validated) is not expected_validated:
        return f"validated_flag_not_canonical:{bool(constraint.validated)}"
    return None


def inspect_release_bridge_compatibility(
    release_tree: Path,
    *,
    capability: str = CAPABILITY_FIRST_SEEN_PAIR_CONTRACT,
) -> Dict[str, Any]:
    """Does this materialized release tree DECLARE the exact bridge contract?

    Read from the release's own bytes, never from the working copy — the whole
    point is to interrogate releases that are not this commit. Returns a record
    with `compatible` plus every defect found, so a refusal can say which of the
    two pointers is the problem and why.

    This answers a DECLARATION question, not a live-schema one. "Can `current`
    and `previous` both operate on either side of the upcoming transition?" is a
    property of what those releases packaged; whether the live database is ready
    to be transitioned at all is G1–G5 and the state machine, and the two must
    not be conflated.

    Compatible means all of:
      * the tree exists and carries `db/schema_requirements.json` (a release
        predating the mechanism cannot be bridge-compatible — it declares
        nothing, which is exactly the state the guard refuses);
      * that file parses under the strict parser used at activation;
      * it declares `capability`, which is what `SCHEMA_STATE_GUARDS` demands of
        any release running against a closed CONTRACT;
      * exactly ONE relation declaration in the whole document participates in
        the bridge, and it is `public.client_trips` in `client_business` scope —
        several declarations that collectively look like the contract are not
        the contract;
      * that declaration's alternatives EQUAL the canonical structure:
        `CANONICAL_BRIDGE_ALTERNATIVES`, i.e. exactly two groups carrying
        exactly the canonical EXPAND member (with its `NOT VALID` state) and
        exactly the canonical validated strict member, and nothing else;
      * neither bridge constraint is declared UNCONDITIONALLY, which would pin
        the release to one side of the transition however its alternatives read;
      * the COMPLETE packaged relation requirement — every field
        `relation_state_defects` decides, so columns, unconditional
        constraints, alternatives and indexes alike — holds against BOTH
        canonical transition states, evaluated by that one shared matcher.

    Exactness is the correction two reviews demanded, in two stages. The second
    review's point was that a declaration naming
    `ck_client_trips_first_seen_pairing` with `CHECK (false)`, `CHECK (true)` or
    any other expression is not the bridge contract but a same-named requirement
    that would pass activation against a database this transition never
    produces. The third review's point was that per-member exactness is still
    not structural exactness: a group carrying the canonical EXPAND member AND a
    synthetic extra constraint satisfied every per-member check while failing
    canonical EXPAND preflight, because preflight requires ALL members of a
    group to hold. So the decision is now equality against one normalized
    canonical structure, and the two can no longer disagree about this relation.

    The fourth review's point was that ALTERNATIVES ARE NOT THE REQUIREMENT. A
    relation declaration also carries columns, unconditional constraints and
    indexes, and every one of them can make normal preflight fail. Two honestly
    prepared releases carried the exact canonical alternatives and additionally
    required an absent `ck_synthetic_unconditional` on the same relation: G6
    said compatible, relation preflight said
    `constraint_absent:public.client_trips.ck_synthetic_unconditional`. So the
    alternatives check is now only half the proof — the other half evaluates the
    WHOLE packaged relation against both canonical states through
    `relation_state_defects`, the same function live preflight decides with.
    `compatible` consequently IMPLIES the release's own relation preflight
    passes in canonical EXPAND and in canonical validated CONTRACT.

    The scope of that proof is the PARTICIPATING relation and nothing else.
    Unrelated relations, migrations and objects elsewhere in the document are
    not part of bridge identity and are not inspected here; they remain the
    business of full release preflight, which continues to fail closed on them.
    """
    tree = Path(release_tree)
    record: Dict[str, Any] = {
        "release_tree": str(tree),
        "tree_present": tree.is_dir(),
        "requirements_present": False,
        "declares_capability": False,
        "declares_pair_relation": False,
        "declares_expand_state": False,
        "declares_validated_contract_state": False,
        "declares_exclusive_bridge_alternatives": False,
        "declares_exact_bridge_alternatives": False,
        "satisfies_complete_relation_in_both_states": False,
        "canonical_state_relation_defects": {},
        "bridge_declarations": [],
        "compatible": False,
        "defects": [],
    }
    defects: List[str] = record["defects"]

    if not record["tree_present"]:
        defects.append("release_tree_absent")
        return record

    requirements_path = tree / SCHEMA_REQUIREMENTS_RELPATH
    if not requirements_path.is_file():
        defects.append("schema_requirements_absent")
        return record
    record["requirements_present"] = True

    try:
        payload = json.loads(requirements_path.read_text(encoding="utf-8"))
        requirements = parse_requirements(payload)
        capabilities = parse_capabilities(payload)
    except SchemaPreflightError as exc:
        defects.append(f"schema_requirements_malformed:{exc.code}")
        return record
    except (OSError, json.JSONDecodeError) as exc:
        defects.append(f"schema_requirements_unreadable:{type(exc).__name__}")
        return record

    record["declared_capabilities"] = sorted(capabilities)
    record["declares_capability"] = capability in capabilities
    if not record["declares_capability"]:
        defects.append(f"capability_not_declared:{capability}")

    # Group identity is (migration filename, group index): two alternatives of
    # one relation are the same STATE only if they are literally the same group,
    # and the bridge requires the two states to be genuinely alternative — a
    # single group naming both constraints would demand they hold at once, which
    # no database can satisfy and which therefore spans nothing.
    expand_groups: List[str] = []
    contract_groups: List[str] = []

    # EVERY declaration anywhere in the document that PARTICIPATES in the
    # bridge, i.e. names either bridge constraint at all, in any scope and on
    # any relation. The bridge is ONE relation requirement, so a second
    # participant is itself a defect: "several declarations that collectively
    # look like the contract" is precisely the shape exactness must refuse, and
    # a participant on the wrong relation or in the wrong scope is a bridge
    # assertion the live matcher would evaluate somewhere the transition never
    # happens.
    participants: List[Tuple[str, str, RelationRequirement]] = []
    for requirement in requirements:
        for relation in requirement.relations:
            declared_names = {c.name for c in relation.constraints}
            declared_names.update(
                constraint.name
                for group in relation.constraint_alternatives
                for constraint in group.constraints
            )
            if declared_names & {
                FIRST_SEEN_EXPAND_CONSTRAINT, FIRST_SEEN_STRICT_CONSTRAINT
            }:
                participants.append(
                    (requirement.migration, requirement.scope, relation)
                )

    for requirement in requirements:
        if requirement.scope != SCOPE_CLIENT_BUSINESS:
            continue
        for relation in requirement.relations:
            if (relation.schema, relation.table) != (
                FIRST_SEEN_PAIR_SCHEMA, FIRST_SEEN_PAIR_TABLE
            ):
                continue
            record["declares_pair_relation"] = True
            for constraint in relation.constraints:
                if constraint.name in (
                    FIRST_SEEN_EXPAND_CONSTRAINT, FIRST_SEEN_STRICT_CONSTRAINT
                ):
                    # Unconditional means "must always hold", which is the one
                    # thing a release spanning the transition cannot say about
                    # either side of it.
                    defects.append(
                        "bridge_constraint_declared_unconditionally:"
                        f"{requirement.migration}:{constraint.name}"
                    )
            for index, group in enumerate(relation.constraint_alternatives):
                identity = f"{requirement.migration}#{index}"
                for constraint in group.constraints:
                    if constraint.name == FIRST_SEEN_EXPAND_CONSTRAINT:
                        defect = _canonical_bridge_constraint_defect(
                            constraint,
                            expected_definition=(
                                FIRST_SEEN_EXPAND_DECLARED_DEFINITION
                            ),
                            expected_validated=(
                                FIRST_SEEN_EXPAND_DECLARED_VALIDATED
                            ),
                        )
                        if defect is None:
                            expand_groups.append(identity)
                        else:
                            defects.append(
                                "expand_declaration_not_canonical:"
                                f"{identity}:{group.state}:{defect}"
                            )
                    elif constraint.name == FIRST_SEEN_STRICT_CONSTRAINT:
                        defect = _canonical_bridge_constraint_defect(
                            constraint,
                            expected_definition=(
                                FIRST_SEEN_STRICT_DECLARED_DEFINITION
                            ),
                            expected_validated=(
                                FIRST_SEEN_STRICT_DECLARED_VALIDATED
                            ),
                        )
                        if defect is None:
                            contract_groups.append(identity)
                        else:
                            defects.append(
                                "contract_declaration_not_canonical:"
                                f"{identity}:{group.state}:{defect}"
                            )

    if not record["declares_pair_relation"]:
        defects.append(
            "pair_relation_not_declared:"
            f"{FIRST_SEEN_PAIR_SCHEMA}.{FIRST_SEEN_PAIR_TABLE}"
        )

    record["declares_expand_state"] = bool(expand_groups)
    record["declares_validated_contract_state"] = bool(contract_groups)
    record["declares_exclusive_bridge_alternatives"] = bool(
        set(expand_groups) - set(contract_groups)
    ) and bool(set(contract_groups) - set(expand_groups))

    if not record["declares_expand_state"]:
        defects.append(
            f"expand_state_not_declared:{FIRST_SEEN_EXPAND_CONSTRAINT}"
        )
    if not record["declares_validated_contract_state"]:
        # A release declaring the strict constraint WITHOUT `validated` would
        # accept an interrupted closure as a closed one, so it is not a valid
        # rollback target for a closed CONTRACT either.
        defects.append(
            f"validated_contract_state_not_declared:{FIRST_SEEN_STRICT_CONSTRAINT}"
        )
    if (record["declares_expand_state"]
            and record["declares_validated_contract_state"]
            and not record["declares_exclusive_bridge_alternatives"]):
        defects.append(
            "bridge_states_are_not_distinct_alternatives:"
            f"{sorted(set(expand_groups) & set(contract_groups))}"
        )

    # THE AUTHORITATIVE DECLARATION PROOF.
    #     Everything above is per-member diagnosis: it says WHICH canonical
    #     member is missing or wrong, which is what a refusal has to be able to
    #     report. None of it establishes that the declaration is the contract
    #     and nothing else, and independent review made that gap concrete: a
    #     release declaring the canonical EXPAND member alongside a synthetic
    #     `ck_synthetic_extra` in the same group satisfied every check above
    #     while failing canonical EXPAND preflight, because preflight requires
    #     every member of a group to hold and the presence search had ignored
    #     the extra one.
    #
    #     So the decision is structural equality against the canonical
    #     declaration built once from the shared constants. Extra members,
    #     duplicated members, duplicated groups, split members, a third
    #     synthetic group, a merged group and a missing group are all simply
    #     "not equal", and G6 can no longer accept a bridge declaration that
    #     normal release preflight would reject.
    record["bridge_declarations"] = [
        f"{migration}:{scope}:{relation.schema}.{relation.table}"
        for migration, scope, relation in participants
    ]
    if not participants:
        defects.append("bridge_declaration_absent")
    elif len(participants) > 1:
        defects.append(
            "bridge_declared_by_multiple_relations:"
            f"{record['bridge_declarations']}"
        )
    else:
        migration, scope, relation = participants[0]
        if (scope, relation.schema, relation.table) != (
            SCOPE_CLIENT_BUSINESS, FIRST_SEEN_PAIR_SCHEMA, FIRST_SEEN_PAIR_TABLE
        ):
            defects.append(
                "bridge_declared_on_wrong_relation:"
                f"{migration}:{scope}:{relation.schema}.{relation.table}"
            )
        else:
            observed = tuple(sorted(
                (_normalized_bridge_group(group)
                 for group in relation.constraint_alternatives),
                key=repr,
            ))
            record["declares_exact_bridge_alternatives"] = (
                observed == CANONICAL_BRIDGE_ALTERNATIVES
            )
            if not record["declares_exact_bridge_alternatives"]:
                defects.append(
                    "bridge_alternatives_not_exactly_canonical:"
                    f"{migration}:observed={observed}:"
                    f"expected={CANONICAL_BRIDGE_ALTERNATIVES}"
                )

            # THE COMPLETE-RELATION PROOF.
            #     Everything above is about `constraint_alternatives`, and the
            #     fourth review showed that is not the whole requirement. Two
            #     honestly prepared releases carried the exact canonical
            #     alternatives AND additionally required an absent
            #     `ck_synthetic_unconditional` on the very same
            #     `public.client_trips` declaration. G6 returned
            #     compatible; the same release's relation preflight correctly
            #     returned
            #     `constraint_absent:public.client_trips.ck_synthetic_unconditional`.
            #     So G6 could call a release rollback-safe that its own
            #     activation would refuse — the exact implication this gate
            #     exists to establish, inverted.
            #
            #     The fix is not another field comparison. The COMPLETE
            #     packaged relation requirement — columns, unconditional
            #     constraints, alternatives and indexes, i.e. every field
            #     `relation_state_defects` decides — is evaluated against BOTH
            #     canonical transition states using that one authoritative
            #     matcher. `compatible` therefore IMPLIES the release's own
            #     relation preflight passes on either side of the DDL step,
            #     because it is literally the same function answering.
            state_defects: Dict[str, List[str]] = {}
            for state_label, state in CANONICAL_BRIDGE_STATES:
                found = relation_state_defects(state, relation)
                state_defects[state_label] = found
                if found:
                    defects.append(
                        f"bridge_relation_unsatisfiable_in_{state_label.lower()}"
                        f":{migration}:" + ",".join(found)
                    )
            record["canonical_state_relation_defects"] = state_defects
            record["satisfies_complete_relation_in_both_states"] = not any(
                state_defects.values()
            )

    record["compatible"] = not defects
    return record


# ---------------------------------------------------------------------------
# Canonical pointer resolution inside the authoritative inventory
# ---------------------------------------------------------------------------

def _resolve_canonical_pointer(
    root: Path, role: str, canonical_releases_dir: Path,
) -> Dict[str, Any]:
    """Resolve one pointer to a release id that is genuinely IN the inventory.

    Basename text is not identity. A `current` symlink reading
    `releases/../../elsewhere/aaaaaaaaaaaa` has a perfectly well-formed release
    id at the end of it and points outside the inventory entirely; a symlink to
    a release id that was removed resolves to nothing at all. Both were accepted
    by the first implementation, which read `Path(target).name`.

    So identity comes from the RESOLVED path: it must live directly in the
    canonical `releases/` directory, its name must be a release id, the link
    text must agree with what it resolved to, and the target must exist.
    """
    from ops.release_boundary import (  # noqa: E402
        _RELEASE_ID_RE, ReleaseBoundaryError, _pointer_target,
        pointer_release_id,
    )

    pointer = root / role
    entry: Dict[str, Any] = {
        "role": role,
        "pointer": str(pointer),
        "pointer_target": None,
        "resolved_path": None,
        "release_id": None,
        "canonical": False,
        "defects": [],
    }
    defects: List[str] = entry["defects"]

    try:
        target = _pointer_target(pointer)
    except ReleaseBoundaryError as exc:
        defects.append(f"pointer_invalid:{exc.classification}")
        return entry
    except OSError as exc:
        defects.append(f"pointer_unreadable:{type(exc).__name__}")
        return entry
    if target is None:
        defects.append("pointer_unset")
        return entry
    entry["pointer_target"] = target

    try:
        resolved = Path(os.path.realpath(pointer))
    except OSError as exc:  # pragma: no cover - realpath rarely raises
        defects.append(f"pointer_unresolvable:{type(exc).__name__}")
        return entry
    entry["resolved_path"] = str(resolved)

    if resolved.parent != canonical_releases_dir:
        # `..` escapes, absolute targets elsewhere on the filesystem, and
        # nested paths inside a release all land here.
        defects.append(
            "pointer_target_outside_release_inventory:"
            f"{resolved}!~{canonical_releases_dir}"
        )
        return entry
    if not _RELEASE_ID_RE.match(resolved.name):
        defects.append(f"pointer_target_is_not_a_release_id:{resolved.name}")
        return entry
    try:
        declared = pointer_release_id(pointer)
    except ReleaseBoundaryError as exc:
        defects.append(f"pointer_invalid:{exc.classification}")
        return entry
    if declared != resolved.name:
        # The link text says one release and the filesystem resolves to
        # another — an indirection no supported operation produces.
        defects.append(
            f"pointer_text_disagrees_with_resolved_release:{declared}"
            f"!={resolved.name}"
        )
        return entry
    if not resolved.is_dir():
        defects.append(f"pointer_target_dangling:{resolved.name}")
        return entry

    entry["release_id"] = resolved.name
    entry["canonical"] = True
    return entry


def rollback_envelope_status(
    release_root: Path,
    *,
    capability: str = CAPABILITY_FIRST_SEEN_PAIR_CONTRACT,
    source_repo: Optional[Path] = None,
) -> Dict[str, Any]:
    """Prove — or refuse — that a one-step rollback survives the CONTRACT closure.

    Ready requires every one of:
      1. the root carries a real release inventory (`releases/`, `meta/`);
      2. `current` and `previous` both resolve CANONICALLY to a directory that
         lives directly in that inventory — no `..` escape, no external target,
         no dangling id, and no link text disagreeing with what it resolved to;
      3. both are VERIFIED materialized releases: `verify_release` recomputes
         each one from its own bytes against its manifest, its commit and its
         source-tree digest, so fabricated metadata (`{}`), a doctored manifest
         or an edited file in the tree all fail here rather than downstream;
      4. they are DISTINCT release identities (re-activating one release is a
         no-op that leaves the pointers where they were, so two aliases for the
         same tree prove nothing);
      5. both declare `capability`;
      6. both declare the EXACT canonical EXPAND and validated CONTRACT states,
         as two distinct alternatives.

    `source_repo` is where the release commits are cross-checked when Git can
    still answer; a release must stay verifiable after its commit becomes
    unreachable, so an absent commit is reported, not fatal. Manifest and
    content verification are unconditional either way.

    READ-ONLY. `verify_release` recomputes and compares; it moves no pointer,
    writes no file and touches no database. Reports the exact release ids and
    per-release defects. Nothing secret is read: release ids, pointer paths,
    commits and requirement declarations only.
    """
    root = Path(release_root)
    status: Dict[str, Any] = {
        "release_root": str(root),
        "source_repo": None,
        "ready": False,
        "reasons": [],
        "current": None,
        "previous": None,
    }
    reasons: List[str] = status["reasons"]

    try:
        from ops.release_boundary import (  # noqa: E402
            META_DIRNAME, RELEASES_DIRNAME, ReleaseBoundaryError,
            verify_release,
        )
    except ImportError as exc:  # pragma: no cover - repository layout error
        reasons.append(f"release_boundary_unavailable:{type(exc).__name__}")
        return status

    repo = Path(source_repo) if source_repo is not None else REPO_ROOT
    status["source_repo"] = str(repo)

    releases_dir = root / RELEASES_DIRNAME
    meta_dir = root / META_DIRNAME
    if not releases_dir.is_dir() or not meta_dir.is_dir():
        # Not a release inventory at all. Said once and plainly, so a synthetic
        # directory fails on what it IS rather than on a missing pointer.
        reasons.append(
            "release_inventory_absent:"
            f"releases={releases_dir.is_dir()},meta={meta_dir.is_dir()}"
        )
        return status
    canonical_releases_dir = Path(os.path.realpath(releases_dir))

    for role in ("current", "previous"):
        entry = _resolve_canonical_pointer(root, role, canonical_releases_dir)
        status[role] = entry
        release_id = entry["release_id"]
        if release_id is None:
            entry["compatible"] = False
            reasons.append(
                f"{role}_pointer_not_canonical:" + ",".join(entry["defects"])
            )
            continue

        # THE MATERIALIZATION PROOF, from the authoritative primitive rather
        # than a G6-specific imitation of it.
        try:
            verified = verify_release(
                release_root=root, release_id=release_id, source_repo=repo,
            )
            entry["materialized"] = True
            entry["commit"] = verified["commit"]
            entry["source_tree_digest"] = verified["source_tree_digest"]
            entry["file_count"] = verified["file_count"]
            entry["verified_against_source_repository"] = verified[
                "verified_against_source_repository"
            ]
        except ReleaseBoundaryError as exc:
            entry["materialized"] = False
            entry["defects"].append(
                f"release_not_verified:{exc.classification}:"
                f"{exc.details.get('reason', '')}"
            )
            entry["compatible"] = False
            reasons.append(
                f"{role}_release_not_verified:{release_id}:{exc.classification}"
            )
            continue

        declaration = inspect_release_bridge_compatibility(
            releases_dir / release_id, capability=capability,
        )
        pointer_defects = list(entry["defects"])
        entry.update(declaration)
        entry["defects"] = pointer_defects + list(declaration["defects"])
        entry["compatible"] = bool(declaration["compatible"]) and not pointer_defects
        if not entry["compatible"]:
            reasons.append(
                f"{role}_release_not_bridge_compatible:{release_id}:"
                + ",".join(entry["defects"])
            )

    current_id = (status["current"] or {}).get("release_id")
    previous_id = (status["previous"] or {}).get("release_id")
    if current_id is not None and current_id == previous_id:
        reasons.append(
            f"current_and_previous_are_the_same_release:{current_id}"
        )

    status["ready"] = not reasons
    return status
