/* Driver Eco Dashboard — formatting helpers (pl-PL).
 * Pure functions, no DOM. UMD so the Node test harness can require() it. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.EcoFormat = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var NBSP = "\u00A0";        // non-breaking space (before units and %)
  var NNBSP = "\u202F";       // narrow no-break space (thousands separator)
  var MINUS = "\u2212";       // typographic minus
  var EN_DASH = "\u2013";

  function isNum(v) { return typeof v === "number" && isFinite(v); }

  /* 3418 -> "3 418" (narrow no-break space). Negative -> typographic minus. */
  function int(v) {
    if (!isNum(v)) return "";
    var neg = v < 0;
    var s = String(Math.round(Math.abs(v)));
    var out = "";
    for (var i = 0; i < s.length; i++) {
      var fromEnd = s.length - i;
      out += s.charAt(i);
      if (fromEnd > 1 && (fromEnd - 1) % 3 === 0) out += NNBSP;
    }
    return (neg ? MINUS : "") + out;
  }

  /* 50.63 -> "50,63". Trims trailing zeros: 50.60 -> "50,6"; 50 -> "50". */
  function num(v, maxDecimals) {
    if (!isNum(v)) return "";
    var d = typeof maxDecimals === "number" ? maxDecimals : 2;
    var neg = v < 0;
    var s = Math.abs(v).toFixed(d);
    if (d > 0) s = s.replace(/0+$/, "").replace(/\.$/, "");
    var parts = s.split(".");
    var head = int(Number(parts[0]));
    return (neg ? MINUS : "") + head + (parts[1] ? "," + parts[1] : "");
  }

  /* 50.63 -> "50,63 %" (nbsp before %). */
  function percent(v, maxDecimals) {
    if (!isNum(v)) return "";
    return num(v, typeof maxDecimals === "number" ? maxDecimals : 1) + NBSP + "%";
  }

  /* Signed points: -6 -> "−6 pkt", 5 -> "+5 pkt", 0 -> "0 pkt". */
  function pointsSigned(v) {
    if (!isNum(v)) return "";
    if (v === 0) return "0" + NBSP + "pkt";
    return (v > 0 ? "+" : MINUS) + int(Math.abs(v)) + NBSP + "pkt";
  }

  function unit(v, unitStr) {
    if (!isNum(v)) return "";
    return int(v) + NBSP + unitStr;
  }

  /* Polish plural: plural(5, ["dzień","dni","dni"]) -> "dni" (form only). */
  function pluralForm(n, forms) {
    var a = Math.abs(Math.round(n));
    if (a === 1) return forms[0];
    var m10 = a % 10, m100 = a % 100;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return forms[1];
    return forms[2];
  }

  /* plural(5, forms) -> "5 dni" (nbsp between). */
  function plural(n, forms) {
    if (!isNum(n)) return "";
    return int(n) + NBSP + pluralForm(n, forms);
  }

  var FORMS = {
    days: ["dzie\u0144", "dni", "dni"],
    events: ["zdarzenie", "zdarzenia", "zdarze\u0144"],
    places: ["miejsce", "miejsca", "miejsc"],
    points: ["punkt", "punkty", "punkt\u00F3w"],
    trips: ["przejazd", "przejazdy", "przejazd\u00F3w"]
  };

  /* "2026-07-19" -> {d:"19", m:"07", y:"2026"} */
  function dateParts(iso) {
    if (typeof iso !== "string") return null;
    var m = iso.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!m) return null;
    return { y: m[1], m: m[2], d: m[3] };
  }

  /* "2026-07-19" -> "19.07" */
  function dateShort(iso) {
    var p = dateParts(iso);
    return p ? p.d + "." + p.m : "";
  }

  /* "2026-07-19" -> "19.07.2026" */
  function dateFull(iso) {
    var p = dateParts(iso);
    return p ? p.d + "." + p.m + "." + p.y : "";
  }

  /* ("2026-07-01","2026-07-19") -> "01.07 – 19.07.2026" */
  function dateRange(startIso, endIso) {
    var a = dateParts(startIso), b = dateParts(endIso);
    if (!a && !b) return "";
    if (!a) return dateFull(endIso);
    if (!b) return dateFull(startIso);
    var left = a.y === b.y ? a.d + "." + a.m : a.d + "." + a.m + "." + a.y;
    return left + " " + EN_DASH + " " + b.d + "." + b.m + "." + b.y;
  }

  return {
    NBSP: NBSP, NNBSP: NNBSP, MINUS: MINUS, EN_DASH: EN_DASH,
    FORMS: FORMS,
    int: int, num: num, percent: percent,
    pointsSigned: pointsSigned, unit: unit,
    plural: plural, pluralForm: pluralForm,
    dateShort: dateShort, dateFull: dateFull, dateRange: dateRange
  };
});
