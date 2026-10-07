/* THE capability lifetime policy. One table, one lookup, no defaults.
 *
 * A Driver Eco Dashboard capability is PERIOD-SCOPED and SNAPSHOT-PINNED: one
 * grant belongs to one reporting period of one driver and resolves forever to
 * that period's immutable snapshot object. Its lifetime is therefore a property
 * of the REPORTING PERIOD, not of the link, the driver or the moment of issue:
 *
 *   weekly   10 days
 *   monthly  60 days
 *
 * WHY THIS FILE EXISTS AT ALL. The previous contract was a single
 * `DEFAULT_CAPABILITY_TTL_SECONDS` of 45 days applied to every grant, and its
 * shape was the problem rather than its value: a default is something a caller
 * can omit, and every omission produced a lifetime nobody had chosen for that
 * period. There is no default here. `capabilityTtlSeconds` answers for the two
 * period types the product defines and THROWS for everything else — an unknown
 * period type is a protocol failure, not an invitation to guess weekly.
 *
 * WHERE THE PERIOD TYPE COMES FROM, AND WHY IT IS TRUSTWORTHY. The publisher
 * states it explicitly on the request (`X-Publication-Period`). It is never
 * parsed out of a URL, a subject reference, an object key or any presentation
 * text — none of which carry it, by design. The host derives it from
 * `eco_dashboard_delivery_operation.period_type`, which is part of the logical
 * delivery identity, is constrained to `weekly`/`monthly` by the table itself,
 * and is one of the fields hashed into the operation id
 * (`delivery_contract.derive_operation_id`). A different period type is
 * therefore a different operation id and a different publication — one
 * operation cannot be published as weekly and recovered as monthly without
 * ceasing to be the same operation.
 *
 * WHAT A LIFETIME IS NOT. It is not a retention policy for the snapshot. The R2
 * object outlives the grant and is not deleted when the bearer expires; that is
 * a separate data-retention concern, and the owner has since decided it: the
 * global hard-retention ceiling in `retention_policy.js` (13 calendar months,
 * mirroring `ops/retention_registry.py`) removes the object, the expired grant
 * row and its publication ledger entry. "Outlives the grant" therefore means by
 * months, not indefinitely. Nothing in THIS file changes as a result — a
 * capability lifetime is still 10 or 60 days and is still the only thing that
 * decides when a link stops working.
 *
 * ALREADY-ISSUED GRANTS ARE NOT REWRITTEN. This policy decides the lifetime of
 * a capability at the moment it is MINTED (publication) or REPLACED (recovery
 * rotation). Nothing here reads, shortens or extends the `expires_at` of a
 * grant that already exists, so a link issued under the previous 45-day rule
 * keeps the validity its recipient was given.
 */

const DAY_SECONDS = 60 * 60 * 24;

export const PERIOD_TYPE = Object.freeze({
  WEEKLY: "weekly",
  MONTHLY: "monthly",
});

/* THE authoritative mapping. Nothing else in the Worker states a capability
 * lifetime, and no route may compute one from anything but this table. */
export const CAPABILITY_TTL_SECONDS = Object.freeze({
  [PERIOD_TYPE.WEEKLY]: 10 * DAY_SECONDS,
  [PERIOD_TYPE.MONTHLY]: 60 * DAY_SECONDS,
});

export const UNKNOWN_PERIOD_TYPE = "UNKNOWN_PERIOD_TYPE";

/** Is this exactly one of the period types the policy defines? */
export function isSupportedPeriodType(value) {
  return typeof value === "string"
    && Object.prototype.hasOwnProperty.call(CAPABILITY_TTL_SECONDS, value)
    /* `hasOwnProperty` on a frozen literal is already safe, but the explicit
     * membership test keeps the answer independent of prototype surprises. */
    && (value === PERIOD_TYPE.WEEKLY || value === PERIOD_TYPE.MONTHLY);
}

/**
 * The capability lifetime, in seconds, for one reporting period type.
 *
 * THROWS `UNKNOWN_PERIOD_TYPE` for anything else — missing, empty, differently
 * cased, whitespace-padded, numeric, an object, or a period type this build
 * does not implement. Callers turn that into a 400 before any D1 or R2 state
 * exists. Failing is the whole contract: a capability with a silently chosen
 * lifetime is a capability whose expiry nobody can reason about.
 */
export function capabilityTtlSeconds(periodType) {
  if (!isSupportedPeriodType(periodType)) {
    const error = new Error(UNKNOWN_PERIOD_TYPE);
    error.name = UNKNOWN_PERIOD_TYPE;
    throw error;
  }
  return CAPABILITY_TTL_SECONDS[periodType];
}

export const TTL_SECONDS_REQUIRED = "TTL_SECONDS_REQUIRED";

/**
 * A capability lifetime the caller actually stated, for the primitives that
 * take one directly rather than deriving it from a period type.
 *
 * Refuses anything that is not a positive, finite, whole number of seconds —
 * including `0`, `null`, `undefined`, `NaN`, a numeric string and a fractional
 * value. `0` matters in particular: under the removed `params.ttl_seconds ||
 * DEFAULT` idiom it silently became the universal default, and here it is
 * refused before anything is written (the schema's `expires_at > issued_at`
 * check would reject it in any case).
 */
export function requireTtlSeconds(value) {
  if (typeof value !== "number" || !Number.isInteger(value) || value <= 0) {
    const error = new Error(TTL_SECONDS_REQUIRED);
    error.name = TTL_SECONDS_REQUIRED;
    throw error;
  }
  return value;
}
