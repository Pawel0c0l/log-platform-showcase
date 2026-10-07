/* THE hard-retention ceiling, as the Worker sees it.
 *
 * WHY A SECOND DECLARATION OF THE SAME NUMBER EXISTS AT ALL. The authoritative
 * registry is `ops/retention_registry.py`, and a Worker cannot import Python.
 * The value is therefore restated here exactly once, and pinned to the
 * registry by `ops/tests_manual/test_retention_registry.py`, which parses this
 * file and fails if the two disagree. That is the whole mechanism: one
 * authority, one mirror, one test that makes drift impossible to ship. No route
 * in this Worker may compute a retention horizon from anything but this file.
 *
 * WHAT THIS IS NOT. It is not a capability lifetime. `capability_ttl.js` owns
 * those (weekly 10 days, monthly 60) and they are much shorter; a grant stops
 * authorising at its own TTL regardless of anything here. This file governs
 * how long the platform may keep the RECORD after it has stopped working — the
 * expired-grant tombstone, the publication ledger row, and the historical
 * snapshot object in R2.
 *
 * CALENDAR MONTHS, NOT DAYS. `hardRetentionCutoffSeconds` subtracts 13 calendar
 * months from an instant, clamping to the end of the target month, which is the
 * same rule `subtract_calendar_months` implements in Python — 31 March minus 13
 * is 28 February, not "395 days ago". A day approximation would be wrong by up
 * to two days depending on where the leap year falls, and the difference is the
 * difference between deleting a record a day early and keeping it a day late.
 */

/* Mirrors `ops.retention_registry.HARD_RETENTION_MONTHS`. Pinned by test. */
export const HARD_RETENTION_MONTHS = 13;

/* Mirrors `ops.retention_registry.GLOBAL_POLICY_ID`, so a maintenance log line
 * and a host-side retention log line name the same policy. */
export const HARD_RETENTION_POLICY_ID = "platform.global_hard_retention";

const DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

function isLeapYear(year) {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

function daysInMonth(year, monthIndex) {
  if (monthIndex === 1 && isLeapYear(year)) return 29;
  return DAYS_IN_MONTH[monthIndex];
}

/**
 * `nowSeconds` minus `months` calendar months, as whole UTC seconds.
 *
 * Deliberately built from the UTC calendar fields rather than from
 * `Date.setUTCMonth`, whose overflow behaviour is the opposite of what a
 * retention cutoff needs: setting month to February on the 31st rolls FORWARD
 * into March, which would move the cutoff later and retain data past the
 * ceiling. Clamping to the last day of the target month is the only direction
 * that can never over-retain.
 */
export function subtractCalendarMonths(nowSeconds, months) {
  if (!Number.isInteger(nowSeconds)) throw new TypeError("nowSeconds must be an integer");
  if (!Number.isInteger(months) || months < 0) throw new TypeError("months must be a non-negative integer");
  const moment = new Date(nowSeconds * 1000);
  const total = moment.getUTCFullYear() * 12 + moment.getUTCMonth() - months;
  const year = Math.floor(total / 12);
  const monthIndex = total - year * 12;
  const day = Math.min(moment.getUTCDate(), daysInMonth(year, monthIndex));
  const shifted = Date.UTC(
    year, monthIndex, day,
    moment.getUTCHours(), moment.getUTCMinutes(), moment.getUTCSeconds(),
  );
  return Math.floor(shifted / 1000);
}

/** The DEADLINE horizon: `now` minus 13 calendar months, exactly. */
export function hardRetentionCutoffSeconds(nowSeconds) {
  return subtractCalendarMonths(nowSeconds, HARD_RETENTION_MONTHS);
}

/* THE ENFORCEMENT LEAD, and why a deadline alone is not enough.
 *
 * Maintenance is periodic: the platform hard-retention sweep calls this Worker
 * once a week. A sweep that deleted exactly "older than 13 months" would leave
 * a grant, a ledger row and a snapshot alive for up to another week past their
 * deadline — thirteen months plus the next maintenance run, which is not
 * thirteen months. So the cutoff is moved EARLIER by one guaranteed maintenance
 * interval: everything whose deadline falls before the next guaranteed sweep
 * goes on THIS one. Deleting a few days early is permitted by the policy;
 * deleting late is not.
 *
 * Mirrors `MAINTENANCE_CYCLES["eco-dashboard-maintenance"].guaranteed_interval`
 * in `ops/retention_registry.py`, and is pinned to it by
 * `ops/tests_manual/test_driver_eco_dashboard_hard_retention.py`. D1 and R2 are
 * in NO repository-controlled backup set, so there is no backup shadow to add
 * here — the platform Postgres and MinIO stores that are carry a larger lead,
 * and the registry composes each one from the audited topology.
 */
export const HARD_RETENTION_ENFORCEMENT_LEAD_SECONDS = 7 * 24 * 60 * 60;

/**
 * THE cutoff a sweep must use. Anything strictly older is eligible.
 *
 * Deliberately the only cutoff any route may act on. `hardRetentionCutoffSeconds`
 * is exported for reporting the bare deadline next to it, never for deleting.
 */
export function enforcementCutoffSeconds(nowSeconds) {
  if (!Number.isInteger(nowSeconds)) throw new TypeError("nowSeconds must be an integer");
  return subtractCalendarMonths(
    nowSeconds + HARD_RETENTION_ENFORCEMENT_LEAD_SECONDS, HARD_RETENTION_MONTHS);
}
