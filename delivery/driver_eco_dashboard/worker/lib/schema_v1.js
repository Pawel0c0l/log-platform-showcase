/* Strict allowlist validation for the browser-visible snapshot, schema v1.
 *
 * The delivery boundary must not forward an object just because it carries the
 * right `contract_id`. Anything the publisher stored that is not part of the v1
 * browser contract — an unknown field, a wrong type, an unexpected enum, a
 * private marker smuggled into an otherwise valid object — must fail closed.
 *
 * This is a STRUCTURAL and PRIVACY boundary only. It deliberately re-derives no
 * Eco business rule: it never scores, never bands a coefficient and never
 * checks that points add up. The numeric bounds are wide sanity windows, not
 * scoring assertions.
 *
 * `test_driver_eco_dashboard_delivery.py` asserts this schema against every
 * synthetic fixture in both directions — every field the Python builder emits
 * must be accepted, and every field this schema names must actually occur — so
 * the allowlist cannot drift away from the contract it mirrors.
 *
 * Validation REBUILDS the document from the allowlist. The bytes the Worker
 * returns are produced here, never forwarded from storage unchanged.
 */

export const REJECT = {
  TYPE: "TYPE",
  UNKNOWN_FIELD: "UNKNOWN_FIELD",
  MISSING_FIELD: "MISSING_FIELD",
  ENUM: "ENUM",
  RANGE: "RANGE",
  PATTERN: "PATTERN",
  LENGTH: "LENGTH",
  CARDINALITY: "CARDINALITY",
  CROSS_FIELD: "CROSS_FIELD",
};

class Reject extends Error {
  constructor(reason, path) {
    super(reason);
    this.reason = reason;
    this.path = path;
  }
}

const fail = (reason, path) => {
  throw new Reject(reason, path);
};

/* ----------------------------------------------------------- primitives -- */

const isPlainObject = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);

/* eslint-disable-next-line no-control-regex */
const CONTROL_CHARACTERS = /[\u0000-\u001F\u007F]/;

function bool() {
  return (value, path) => {
    if (typeof value !== "boolean") fail(REJECT.TYPE, path);
    return value;
  };
}

function integer(min, max) {
  return (value, path) => {
    if (typeof value !== "number" || !Number.isInteger(value)) fail(REJECT.TYPE, path);
    if (value < min || value > max) fail(REJECT.RANGE, path);
    return value;
  };
}

/* Percentages arrive as decimals; everything else in the contract is integral. */
function decimal(min, max) {
  return (value, path) => {
    if (typeof value !== "number" || !Number.isFinite(value)) fail(REJECT.TYPE, path);
    if (value < min || value > max) fail(REJECT.RANGE, path);
    return value;
  };
}

function text(maxLength, pattern) {
  return (value, path) => {
    if (typeof value !== "string") fail(REJECT.TYPE, path);
    if (value.length > maxLength) fail(REJECT.LENGTH, path);
    /* No control characters anywhere in a driver-facing string. */
    if (CONTROL_CHARACTERS.test(value)) fail(REJECT.PATTERN, path);
    if (pattern && !pattern.test(value)) fail(REJECT.PATTERN, path);
    return value;
  };
}

function enumOf(values) {
  const allowed = new Set(values);
  return (value, path) => {
    if (typeof value !== "string") fail(REJECT.TYPE, path);
    if (!allowed.has(value)) fail(REJECT.ENUM, path);
    return value;
  };
}

function nullable(schema) {
  return (value, path) => (value === null ? null : schema(value, path));
}

function array(item, maxLength) {
  return (value, path) => {
    if (!Array.isArray(value)) fail(REJECT.TYPE, path);
    if (value.length > maxLength) fail(REJECT.CARDINALITY, path);
    return value.map((entry, index) => item(entry, path + "[" + index + "]"));
  };
}

function exactArray(item, length) {
  return (value, path) => {
    if (!Array.isArray(value)) fail(REJECT.TYPE, path);
    if (value.length !== length) fail(REJECT.CARDINALITY, path);
    return value.map((entry, index) => item(entry, path + "[" + index + "]"));
  };
}

/**
 * `additionalProperties: false` at every boundary. Keys are rebuilt in schema
 * order, so the emitted document cannot carry anything the schema did not name.
 * `optional` keys may be absent; a key that is present is always validated.
 */
function object(fields, options) {
  const settings = options || {};
  const optional = new Set(settings.optional || []);
  return (value, path) => {
    if (!isPlainObject(value)) fail(REJECT.TYPE, path);
    for (const key of Object.keys(value)) {
      if (!Object.prototype.hasOwnProperty.call(fields, key)) {
        fail(REJECT.UNKNOWN_FIELD, path + "." + key);
      }
    }
    const out = {};
    for (const key of Object.keys(fields)) {
      const present = Object.prototype.hasOwnProperty.call(value, key);
      if (!present) {
        if (!optional.has(key)) fail(REJECT.MISSING_FIELD, path + "." + key);
        continue;
      }
      out[key] = fields[key](value[key], path + "." + key);
    }
    if (settings.check) settings.check(out, path);
    return out;
  };
}

/* ------------------------------------------------------- v1 vocabularies -- */

export const CATEGORY_KEYS = [
  "overrev", "harsh_braking", "harsh_acceleration", "harsh_turning",
  "idle", "speeding_140_160", "speeding_160_170", "speeding_170_plus",
];
export const STATUS_VALUES = ["green", "yellow", "red", "neutral"];
export const RATING_VALUES = ["safe", "acceptable", "dangerous"];
export const RANKING_STATES = [
  "RANKED", "NOT_RANKED_BY_CONFIGURATION", "NOT_ON_ROSTER", "LEFT_RANKING",
];
export const RANKING_TRANSITIONS = [
  "RANKED_TO_RANKED", "NEWLY_RANKED", "LEFT_RANKING",
  "NOT_RANKED_TO_NOT_RANKED", "NO_COMPARISON_BASIS",
];
export const ENTRY_STATUSES = ["OK", "INSUFFICIENT_DISTANCE", "REPORT_NOT_READY"];
export const COACHING_CODES = [
  "LARGEST_LOSS", "MOST_IMPROVED", "MOST_DETERIORATED", "BEST_OPPORTUNITY",
];
export const SELECTED_BY = ["points_lost", "coefficient_delta", "threshold_gain"];
export const COMPARISON_KINDS = ["PREVIOUS_CUMULATIVE_PERIOD", "PREVIOUS_CLOSED_MONTH"];
export const QUALIFICATION_STATUSES = ["QUALIFIED", "LOW_DISTANCE", "NO_DISTANCE"];
export const PERIOD_TYPES = ["weekly", "monthly"];
export const WEEKDAYS = ["Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd"];

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const PERIOD_LABEL = /^\d{4}-\d{2}(-W\d{1,2})?$/;

/* Presentation strings (Polish labels, band captions). Bounded, not enumerated:
 * enumerating them here would duplicate the business label set inside the
 * delivery layer, which is exactly what this boundary must not do. */
const LABEL = text(64);
const BAND_LABEL = text(24);

const SCORE = integer(-1000, 1000);
const POINTS = integer(-1000, 1000);
const COUNT = integer(0, 1000000);
const COEFFICIENT = integer(0, 1000000);
const KILOMETRES = integer(0, 10000000);
const INDEX = integer(0, 64);

/* ----------------------------------------------------------- structures -- */

const band = object({
  index: INDEX,
  label: BAND_LABEL,
  upper_bound: nullable(COEFFICIENT),
  points: POINTS,
  points_lost: POINTS,
  status: enumOf(STATUS_VALUES),
});

const category = object({
  key: enumOf(CATEGORY_KEYS),
  label: LABEL,
  short_label: LABEL,
  deemphasize: bool(),
  count: nullable(COUNT),
  coefficient_per_100km: nullable(COEFFICIENT),
  points: nullable(POINTS),
  points_max: POINTS,
  points_lost: nullable(POINTS),
  status: enumOf(STATUS_VALUES),
  band_label: nullable(BAND_LABEL),
  bands: array(band, 16),
  marker_band_index: nullable(INDEX),
  target_band_index: nullable(INDEX),
  target_band_label: nullable(BAND_LABEL),
  target_upper_bound: nullable(COEFFICIENT),
  target_points_gain: nullable(POINTS),
  previous_count: nullable(COUNT),
  previous_coefficient_per_100km: nullable(COEFFICIENT),
  previous_points_lost: nullable(POINTS),
});

const dayCategory = object({
  key: enumOf(CATEGORY_KEYS),
  count: nullable(COUNT),
  coefficient_per_100km: nullable(COEFFICIENT),
  band_label: nullable(BAND_LABEL),
  status: enumOf(STATUS_VALUES),
});

const day = object({
  date: text(10, DATE),
  weekday_short: enumOf(WEEKDAYS),
  kilometers: KILOMETRES,
  trips_count: COUNT,
  categories: array(dayCategory, 8),
});

const coachingValue = object(
  {
    points: POINTS,
    points_gain: POINTS,
    points_delta: nullable(POINTS),
    coefficient_delta: integer(-1000000, 1000000),
    coefficient_distance: COEFFICIENT,
  },
  {
    optional: ["points", "points_gain", "points_delta", "coefficient_delta", "coefficient_distance"],
    check: (out, path) => {
      if (Object.keys(out).length === 0) fail(REJECT.MISSING_FIELD, path);
    },
  }
);

const coachingInputs = object({
  coefficient: nullable(COEFFICIENT),
  previous_coefficient: nullable(COEFFICIENT),
  band_label: nullable(BAND_LABEL),
  points_max: POINTS,
  points_lost: nullable(POINTS),
  previous_points_lost: nullable(POINTS),
  kilometers: nullable(KILOMETRES),
  previous_kilometers: nullable(KILOMETRES),
  target_band_label: nullable(BAND_LABEL),
  target_upper_bound: nullable(COEFFICIENT),
  target_points_gain: nullable(POINTS),
});

const coachingInsight = object({
  code: enumOf(COACHING_CODES),
  category_key: enumOf(CATEGORY_KEYS),
  selected_by: enumOf(SELECTED_BY),
  value: coachingValue,
  inputs: coachingInputs,
});

const nearThreshold = object({
  category_key: enumOf(CATEGORY_KEYS),
  rank: integer(1, 2),
  coefficient_now: COEFFICIENT,
  target_upper_bound: COEFFICIENT,
  target_band_label: BAND_LABEL,
  points_gain: POINTS,
  coefficient_distance: COEFFICIENT,
});

const comparison = object({
  kind: enumOf(COMPARISON_KINDS),
  comparable: bool(),
  basis_period_label: text(16, PERIOD_LABEL),
  basis_start_date: text(10, DATE),
  basis_end_date_exclusive: text(10, DATE),
  basis_end_date_display: text(10, DATE),
  previous_eco_score_total: nullable(SCORE),
  previous_total_kilometers: nullable(KILOMETRES),
  previous_ranking_position: nullable(integer(1, 1000000)),
  eco_score_delta: nullable(SCORE),
  ranking_position_delta_places: nullable(integer(-1000000, 1000000)),
});

const periodIdentity = object({
  period_type: enumOf(PERIOD_TYPES),
  period_label: text(16, PERIOD_LABEL),
  period_start_date: text(10, DATE),
  period_end_date_exclusive: text(10, DATE),
  period_end_date_display: text(10, DATE),
  period_sequence_in_month: nullable(integer(1, 12)),
  closed_periods_in_month: nullable(integer(1, 12)),
  is_partial_period: nullable(bool()),
  snapshot_updated_at_utc: text(24, TIMESTAMP),
});

/* The population split of the ranked drivers, and it is SPARSE by
 * construction: the host derives it by grouping the ranked, qualified,
 * rated population, so a bucket nobody falls into produces no row and
 * therefore no key. Requiring all three was a structural assertion the host
 * never promised — it refused a valid snapshot for a client whose whole
 * ranked population happened to be `safe` and `acceptable`.
 *
 * Every bucket is therefore optional, and the object is still closed in every
 * direction that matters: the key set is exactly these three, an empty object
 * is not a distribution, an unknown bucket is an unknown field, and a value
 * outside 0..100 or of the wrong type still fails. */
const ratingDistribution = object(
  {
    safe: decimal(0, 100),
    acceptable: decimal(0, 100),
    dangerous: decimal(0, 100),
  },
  {
    optional: ["safe", "acceptable", "dangerous"],
    check: (out, path) => {
      if (Object.keys(out).length === 0) fail(REJECT.CARDINALITY, path);
    },
  }
);

const periodBlock = object(
  {
    period_type: enumOf(PERIOD_TYPES),
    period_label: text(16, PERIOD_LABEL),
    period_start_date: text(10, DATE),
    period_end_date_exclusive: text(10, DATE),
    period_end_date_display: text(10, DATE),
    period_sequence_in_month: nullable(integer(1, 12)),
    closed_periods_in_month: nullable(integer(1, 12)),
    is_partial_period: nullable(bool()),
    snapshot_updated_at_utc: text(24, TIMESTAMP),
    qualification_status: enumOf(QUALIFICATION_STATUSES),
    scoring_complete: bool(),
    ranking_state: enumOf(RANKING_STATES),
    ranking_transition: enumOf(RANKING_TRANSITIONS),
    eco_score_total: nullable(SCORE),
    rating_type: nullable(enumOf(RATING_VALUES)),
    ranking_position: nullable(integer(1, 1000000)),
    ranking_total_participants: nullable(integer(1, 1000000)),
    rating_group_share_percent: nullable(decimal(0, 100)),
    rating_group_distribution: nullable(ratingDistribution),
    total_kilometers: KILOMETRES,
    trips_count: COUNT,
    comparison: nullable(comparison),
    categories: exactArray(category, 8),
    near_threshold: array(nearThreshold, 2),
    coaching: array(coachingInsight, 4),
    days: array(day, 40),
  },
  {
    optional: ["ranking_position", "ranking_total_participants", "rating_group_share_percent"],
    /* Contract assertion A5, re-checked at the boundary: the ranking fields
     * exist if and only if the driver is actually ranked. This is the one
     * cross-field rule the delivery layer enforces, because it is a privacy
     * rule rather than a scoring rule. */
    check: (out, path) => {
      const ranked = out.ranking_state === "RANKED";
      const conditional = ["ranking_position", "ranking_total_participants", "rating_group_share_percent"];
      for (const field of conditional) {
        const present = Object.prototype.hasOwnProperty.call(out, field) && out[field] !== null;
        if (present !== ranked) fail(REJECT.CROSS_FIELD, path + "." + field);
      }
      if (!ranked && out.rating_group_distribution !== null) {
        fail(REJECT.CROSS_FIELD, path + ".rating_group_distribution");
      }
      const keys = out.categories.map((entry) => entry.key).join(",");
      if (keys !== CATEGORY_KEYS.join(",")) fail(REJECT.CROSS_FIELD, path + ".categories");
    },
  }
);

const previousBlock = object({
  period_label: text(16, PERIOD_LABEL),
  period_start_date: text(10, DATE),
  period_end_date_exclusive: text(10, DATE),
  period_end_date_display: text(10, DATE),
  eco_score_total: nullable(SCORE),
  total_kilometers: nullable(KILOMETRES),
  trips_count: COUNT,
});

const seriesPoint = object({
  period_label: text(16, PERIOD_LABEL),
  start_date: text(10, DATE),
  end_date_display: text(10, DATE),
  eco_score_total: SCORE,
  is_current: bool(),
});

const periodEntry = object(
  {
    status: enumOf(ENTRY_STATUSES),
    period_identity: periodIdentity,
    current: nullable(periodBlock),
    previous: nullable(previousBlock),
    series: array(seriesPoint, 12),
    series_reference: nullable(seriesPoint),
  },
  {
    /* Fail-closed rule from the snapshot contract (A10/A11): a non-OK entry
     * carries no Eco payload at all. Enforced here as well, so a mis-published
     * object cannot deliver one. */
    check: (out, path) => {
      if (out.status === "OK") {
        if (out.current === null) fail(REJECT.CROSS_FIELD, path + ".current");
        return;
      }
      if (out.current !== null) fail(REJECT.CROSS_FIELD, path + ".current");
      if (out.previous !== null) fail(REJECT.CROSS_FIELD, path + ".previous");
      if (out.series.length !== 0) fail(REJECT.CROSS_FIELD, path + ".series");
      if (out.series_reference !== null) fail(REJECT.CROSS_FIELD, path + ".series_reference");
    },
  }
);

const snapshotDocument = object(
  {
    schema_version: integer(1, 1),
    contract_id: enumOf(["driver_eco_dashboard_snapshot"]),
    generated_at_utc: text(24, TIMESTAMP),
    timezone: enumOf(["Europe/Warsaw"]),
    locale: enumOf(["pl-PL"]),
    constants: object({
      min_qualifying_distance_km: integer(0, 100000),
      rating_thresholds: object({ safe: SCORE, acceptable: SCORE }),
      score_max: SCORE,
    }),
    periods: object(
      {
        weekly: nullable(periodEntry),
        monthly: nullable(periodEntry),
      },
      {
        check: (out, path) => {
          if (out.weekly === null && out.monthly === null) fail(REJECT.CROSS_FIELD, path);
        },
      }
    ),
  },
  {
    check: (out, path) => {
      for (const type of PERIOD_TYPES) {
        const entry = out.periods[type];
        if (entry && entry.period_identity.period_type !== type) {
          fail(REJECT.CROSS_FIELD, path + ".periods." + type);
        }
      }
    },
  }
);

/**
 * Validate and REBUILD one snapshot document.
 * Returns `{ ok: true, document }` or `{ ok: false, reason, path }`.
 */
export function validateSnapshotDocument(candidate) {
  try {
    return { ok: true, document: snapshotDocument(candidate, "$") };
  } catch (error) {
    if (error instanceof Reject) return { ok: false, reason: error.reason, path: error.path };
    return { ok: false, reason: REJECT.TYPE, path: "$" };
  }
}
