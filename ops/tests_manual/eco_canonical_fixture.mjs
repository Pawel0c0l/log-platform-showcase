/* Canonical publication fixtures for the delivery harnesses.
 *
 * The browser fixtures under `assets/driver_eco_dashboard/fixtures/` are
 * pretty-printed and key-ordered for humans reading the frontend. That is
 * exactly what the publication route now refuses: the host is the canonical
 * serialisation authority and `/api/publish` only accepts bytes in its
 * canonical form.
 *
 * So a harness that publishes a fixture must canonicalise it first. This
 * helper does the two things `checkCanonicalJsonForm` actually inspects —
 * sorted object keys and no insignificant whitespace — and nothing else. It is
 * NOT a reimplementation of the host serialiser and must never be treated as
 * one: number formatting, string escaping and UTF-8 encoding stay wherever
 * `JSON.stringify` puts them.
 *
 * The genuine host↔worker byte contract is proven separately, in
 * `ops/tests_manual/test_driver_eco_dashboard_byte_integrity.py`, against
 * bytes produced by `jobs/ecodriving_dashboard/publication.py` itself. This
 * file exists only so the pre-existing scenarios keep exercising the paths
 * they were written for.
 */

import { readFile } from "node:fs/promises";

/** Deep rebuild with object keys in ascending order. Arrays keep their order. */
export function sortKeysDeep(value) {
  if (Array.isArray(value)) return value.map(sortKeysDeep);
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = sortKeysDeep(value[key]);
    return out;
  }
  return value;
}

/** Canonical-form text: sorted keys, compact separators. */
export function canonicalText(document) {
  return JSON.stringify(sortKeysDeep(document));
}

/** Load a browser fixture and return it in canonical publication form. */
export async function canonicalFixture(fixturePath) {
  return canonicalText(JSON.parse(await readFile(fixturePath, "utf8")));
}
