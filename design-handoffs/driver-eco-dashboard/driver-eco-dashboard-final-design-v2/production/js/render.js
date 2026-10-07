/* Driver Eco Dashboard — render.js
 * 1:1 port of the approved "Eco Dashboard Strona" design (3-slide page).
 * Pure: snapshot → HTML string (UMD, no DOM). app.js owns slide transitions
 * and the entrance animations (elements carry data-* animation hooks; the
 * rendered string always contains the FINAL values). */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(require("./format.js"));
  } else {
    root.EcoRender = factory(root.EcoFormat);
  }
})(typeof self !== "undefined" ? self : this, function (fmt) {
  "use strict";

  var NBSP = fmt.NBSP;

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function has(v) { return v !== null && v !== undefined; }

  var RATING_WORD = { safe: "Bezpieczny", acceptable: "Akceptowalny", dangerous: "Niebezpieczny" };
  var RATING_BADGE = { safe: "safe", acceptable: "", dangerous: "danger" };
  var GROUP_WORD = { safe: "bezpieczni", acceptable: "akceptowalni", dangerous: "niebezpieczni" };
  var GROUP_CLS = { safe: "g", acceptable: "y", dangerous: "r" };
  var GROUP_SQ = { safe: "#3E8E5A", acceptable: "#F2CB05", dangerous: "#C4463A" };
  var LOSS_RAMP = ["#E8721C", "#EC8438", "#F09754", "#F4AA71", "#F8BD8E"];
  var MONTHS = ["stycze\u0144", "luty", "marzec", "kwiecie\u0144", "maj", "czerwiec",
    "lipiec", "sierpie\u0144", "wrzesie\u0144", "pa\u017Adziernik", "listopad", "grudzie\u0144"];
  var ORDINAL = { 3: "trzeci", 4: "czwarty", 5: "pi\u0105ty", 6: "sz\u00F3sty", 7: "si\u00F3dmy",
    8: "\u00F3smy", 9: "dziewi\u0105ty", 10: "dziesi\u0105ty", 11: "jedenasty", 12: "dwunasty" };
  /* Approved column order for the daily table (data still keyed by snapshot). */
  var DAY_ORDER = ["overrev", "idle", "harsh_braking", "harsh_acceleration",
    "harsh_turning", "speeding_140_160", "speeding_160_170", "speeding_170_plus"];

  /* Column names for the daily table only. The snapshot's own short_label
   * stays the source of truth everywhere else; these are the longer names this
   * table asks for, keyed by category so a key we do not know still renders
   * the snapshot's wording rather than nothing. Used for the visible header
   * and the per-cell screen-reader prefix alike, so the two never diverge. */
  var DAY_COL_NAMES = {
    overrev: "Wysokie obroty",
    idle: "Nadmierny post\u00F3j",
    harsh_braking: "Gwa\u0142towne hamowania",
    harsh_acceleration: "Gwa\u0142towne przyspieszenia",
    harsh_turning: "Ostre skr\u0119ty",
    speeding_140_160: "Przekroczenia pr\u0119dko\u015Bci 140",
    speeding_160_170: "Przekroczenia pr\u0119dko\u015Bci 160",
    speeding_170_plus: "Przekroczenia pr\u0119dko\u015Bci 170"
  };
  function dayColName(c) { return DAY_COL_NAMES[c.key] || c.short_label; }

  function scoreColor(v, constants, faded) {
    var th = constants.rating_thresholds || {};
    if (isNum(th.safe) && v >= th.safe) return faded ? "#B8D4C2" : "#3E8E5A";
    if (isNum(th.acceptable) && v >= th.acceptable) return faded ? "#F5E48A" : "#F2CB05";
    return faded ? "#E4B0AA" : "#C4463A";
  }

  /* Count-up: the aria-hidden twin is what app.js animates; the sr-only twin
   * carries the exact final value from the first frame. */
  function cu(text, attrs) {
    return '<span data-cu' + (attrs ? " " + attrs : "") + '><span class="cu" aria-hidden="true">' +
      esc(text) + '</span><span class="sr-only">' + esc(text) + "</span></span>";
  }

  function coefText(c) { return isNum(c) ? fmt.int(c) + NBSP + "na 100 km" : ""; }

  function rangeShort(a, b) {
    var da = fmt.dateShort(a), db = fmt.dateShort(b);
    if (!da || !db) return da || db;
    var ma = da.slice(3), mb = db.slice(3);
    return ma === mb ? da.slice(0, 2) + "\u2013" + db : da + "\u2013" + db;
  }

  /* ------------------------------------------------------- access states */

  var ACCESS = {
    INVALID_LINK: { title: "Ten link nie dzia\u0142a",
      body: "Link jest nieprawid\u0142owy. Otw\u00F3rz najnowsz\u0105 wiadomo\u015B\u0107 e-mail z raportem i u\u017Cyj linku z niej.",
      icon: "M9 12a4 4 0 0 1 0-6l2-2a4 4 0 0 1 6 6l-1.5 1.5M15 12a4 4 0 0 1 0 6l-2 2a4 4 0 0 1-6-6L8.5 12.5M4 4l16 16" },
    LINK_EXPIRED: { title: "Ten link wygas\u0142",
      body: "Ka\u017Cdy raport ma sw\u00F3j w\u0142asny, \u015Bwie\u017Cy link. Otw\u00F3rz najnowsz\u0105 wiadomo\u015B\u0107 e-mail i wejd\u017A z niej ponownie.",
      icon: "M12 8v5l3 2M21 12a9 9 0 1 1-9-9 9 9 0 0 1 9 9z" },
    SNAPSHOT_UNAVAILABLE: { title: "Raport jest niedost\u0119pny",
      body: "Nie znale\u017Ali\u015Bmy raportu dla tego linku. Spr\u00F3buj ponownie z najnowszej wiadomo\u015Bci e-mail.",
      icon: "M7 3h7l5 5v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM14 3v5h5M9 14h6M9 17h4" },
    SERVICE_UNAVAILABLE: { title: "Serwis jest chwilowo niedost\u0119pny",
      body: "Nie uda\u0142o si\u0119 wczyta\u0107 raportu. Spr\u00F3buj ponownie za chwil\u0119.",
      icon: "M6 19a4 4 0 0 1 0-8 6 6 0 0 1 11.3-2A5 5 0 0 1 18 19H6zM12 11v4M12 18h.01", retry: true }
  };

  function renderAccessState(code) {
    var a = ACCESS[code] || ACCESS.SERVICE_UNAVAILABLE;
    return '<div class="ed-access" role="alert">' +
      '<div class="icon" aria-hidden="true"><svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="#6C7A66" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="' + a.icon + '"/></svg></div>' +
      "<h1>" + esc(a.title) + "</h1><p>" + esc(a.body) + "</p>" +
      (a.retry ? '<button type="button" class="ed-btn" data-action="retry">Spr\u00F3buj ponownie</button>' : "") +
      "</div>";
  }

  function renderSkeleton() {
    return '<div class="eco-skel" aria-busy="true">' +
      '<div class="skeleton-bar skeleton-head"></div>' +
      '<div class="skeleton-grid"><div class="skeleton-card skeleton-tall"></div>' +
      '<div class="skeleton-card"></div><div class="skeleton-card"></div></div>' +
      '<div class="skeleton-card skeleton-wide"></div>' +
      '<p class="sr-only">Trwa wczytywanie raportu.</p></div>';
  }

  /* ------------------------------------------------------------- headers */

  function header(main, title, identity, periodType, stepText) {
    var range = fmt.dateRange(identity.period_start_date, identity.period_end_date_display);
    var pill = "";
    if (main && range) {
      /* dates only — the period name used to be appended here */
      pill = '<span class="ed-pill">' + esc(range) + "</span>";
    }
    return '<div class="ed-head' + (main ? "" : " sub") + '">' +
      '<div class="ed-brand"><span class="ed-logo" role="img" aria-label="Telematics"></span>' +
      (main ? "<h1 class=\"ed-htitle\">" + esc(title) + "</h1>"
            : '<span class="ed-htitle">' + esc(title) + "</span>") +
      "</div>" +
      '<div class="ed-hmeta">' + pill + '<span class="ed-step">' + esc(stepText) + "</span></div></div>";
  }

  /* A3 — tab/tabpanel wiring. All three panels stay in the DOM (the approved
   * 800 ms transform transition moves them); the inactive ones are removed
   * from the accessibility tree and from the tab order. app.js keeps this in
   * sync on every slide change. */
  function panelAttrs(i, slide) {
    var sel = i === slide;
    return ' id="ed-panel-' + i + '" role="tabpanel" aria-labelledby="ed-tab-' + i +
      '" tabindex="' + (sel ? "0" : "-1") + '"' + (sel ? "" : ' aria-hidden="true" inert');
  }

  /* ------------------------------------------------------- slide 1 parts */

  function scoreCard(cur, constants) {
    var score = cur.eco_score_total, max = constants.score_max || 100;
    var col = isNum(score) ? scoreColor(score, constants, false) : "#E4EDE2";
    var deg = isNum(score) ? (Math.max(0, Math.min(1, score / max)) * 360).toFixed(1) : "0";
    var badge = "";
    if (cur.rating_type && RATING_WORD[cur.rating_type]) {
      var gap = "";
      var th = constants.rating_thresholds || {};
      if (cur.rating_type !== "safe" && isNum(th.safe) && isNum(score) && th.safe > score) {
        gap = " \u00B7 " + fmt.int(th.safe - score) + NBSP + "pkt do \u201EBezpieczny\u201D";
      }
      badge = '<span class="ed-badge ' + RATING_BADGE[cur.rating_type] + '">\u25CF ' +
        RATING_WORD[cur.rating_type] + esc(gap) + "</span>";
    }
    var delta = "";
    if (cur.comparison && isNum(cur.comparison.eco_score_delta)) {
      var d = cur.comparison.eco_score_delta;
      delta = d > 0 ? '<span class="ed-up">\u25B2 ' + esc(fmt.int(d)) + NBSP + "pkt wi\u0119cej ni\u017C w poprzednim okresie</span>"
        : d < 0 ? '<span class="ed-down">\u25BC ' + esc(fmt.int(Math.abs(d))) + NBSP + "pkt mniej ni\u017C w poprzednim okresie</span>"
        : '<span class="ed-chip-flat">= bez zmian wzgl\u0119dem poprzedniego okresu</span>';
    } else if (!cur.comparison) {
      delta = '<span class="ed-chip-nobasis">pierwszy zamkni\u0119ty okres \u2014 brak por\u00F3wnania</span>';
    }
    return '<section class="ed-card ed-score ed-hover"><h2 class="sr-only">Tw\u00F3j wynik</h2>' +
      '<span class="ed-kicker">Tw\u00F3j wynik w tym okresie</span>' +
      '<div class="ed-ring" data-ring data-val="' + (isNum(score) ? score : 0) + '" data-max="' + max +
      '" data-safe="' + esc(String((constants.rating_thresholds || {}).safe)) +
      '" data-acc="' + esc(String((constants.rating_thresholds || {}).acceptable)) +
      '" style="background:conic-gradient(' + col + " " + deg + 'deg,#E4EDE2 0)">' +
      '<div class="ed-ring-in"><span class="ed-score-num">' +
      (isNum(score) ? cu(fmt.int(score), 'data-cu-v="' + score + '"') : "\u2013") +
      '</span><span class="ed-score-sub">na ' + esc(fmt.int(max)) + NBSP + "pkt</span></div></div>" +
      badge + delta + "</section>";
  }

  /* A2 — period-over-period distance movement. Both facts are snapshot facts:
   * current `total_kilometers` and `comparison.previous_total_kilometers`.
   * With no comparable previous distance nothing is invented: no zero, no
   * neutral arrow — the existing "no basis" chip is used instead. */
  function distanceMove(cur) {
    var cmp = cur.comparison;
    var prev = cmp && isNum(cmp.previous_total_kilometers) ? cmp.previous_total_kilometers : null;
    if (prev === null || !isNum(cur.total_kilometers)) {
      return { chip: '<span class="ed-chip-nobasis">brak por\u00F3wnania dystansu</span>', prev: "" };
    }
    var now = Math.round(cur.total_kilometers), was = Math.round(prev);
    var chip = now > was
      ? '<span class="ed-chip-flat">\u25B2 ' + esc(fmt.unit(now - was, "km")) +
        ' wi\u0119cej<span class="sr-only"> ni\u017C w poprzednim okresie</span></span>'
      : now < was
      ? '<span class="ed-chip-flat">\u25BC ' + esc(fmt.unit(was - now, "km")) +
        ' mniej<span class="sr-only"> ni\u017C w poprzednim okresie</span></span>'
      : '<span class="ed-chip-flat">= tyle samo km, co poprzednio</span>';
    return { chip: chip, prev: "poprzednio " + fmt.unit(was, "km") };
  }

  function tiles(cur) {
    var out = "";
    if (cur.ranking_state === "RANKED" && isNum(cur.ranking_position) && isNum(cur.ranking_total_participants)) {
      /* Every condition renders a chip: holding a place is a fact worth
       * stating, and so is having nothing to compare against. */
      var chips;
      var rcmp = cur.comparison;
      var dp = rcmp && isNum(rcmp.ranking_position_delta_places)
        ? rcmp.ranking_position_delta_places : null;
      if (cur.ranking_transition === "NEWLY_RANKED") {
        chips = '<span class="ed-chip-flat">nowo\u015B\u0107 w rankingu</span>';
      } else if (cur.ranking_transition === "RANKED_TO_RANKED" && dp !== null) {
        chips = dp > 0
          ? '<span class="ed-chip-up">\u25B2 ' + esc(fmt.plural(dp, fmt.FORMS.places)) + " w g\u00F3r\u0119</span>"
          : dp < 0
          ? '<span class="ed-chip-flat">\u25BC ' + esc(fmt.plural(Math.abs(dp), fmt.FORMS.places)) + " w d\u00F3\u0142</span>"
          : '<span class="ed-chip-flat">= miejsce utrzymane</span>';
      } else {
        chips = '<span class="ed-chip-nobasis">brak por\u00F3wnania z poprzednim okresem</span>';
      }
      var top = Math.max(1, Math.round(cur.ranking_position / cur.ranking_total_participants * 100));
      out += '<section class="ed-tile ed-hover"><h2 class="sr-only">Miejsce w rankingu</h2>' +
        '<span class="ed-kicker">Miejsce w rankingu</span>' +
        '<span class="ed-big">' + cu(fmt.int(cur.ranking_position), 'data-cu-v="' + cur.ranking_position + '"') +
        " <small>z " + esc(fmt.int(cur.ranking_total_participants)) + "</small></span>" +
        '<span class="ed-cap">' + chips + (chips ? " " : "") + "Top " + esc(fmt.int(top)) + "% kierowc\u00F3w</span></section>";
    }
    var move = distanceMove(cur);
    out += '<section class="ed-tile ed-hover"><h2 class="sr-only">Dystans</h2>' +
      '<span class="ed-kicker">Dystans w tym okresie</span>' +
      '<span class="ed-big">' + cu(fmt.int(cur.total_kilometers), 'data-cu-v="' + cur.total_kilometers + '"') +
      " <small>km</small></span>" +
      '<span class="ed-cap">' + move.chip + " " +
      (move.prev ? esc(move.prev) + " \u00B7 " : "") +
      esc(fmt.plural(cur.trips_count, fmt.FORMS.trips)) + "</span></section>";
    out += distributionTile(cur);
    out += quickWinTile(cur);
    return out;
  }

  function distributionTile(cur) {
    if (cur.ranking_state !== "RANKED" || !cur.rating_group_distribution) return "";
    var dist = cur.rating_group_distribution, keys = ["safe", "acceptable", "dangerous"];
    var present = [], sum = 0;
    keys.forEach(function (k) { if (isNum(dist[k])) { present.push(k); sum += dist[k]; } });
    if (!present.length || sum <= 0) return "";
    var bar = present.map(function (k) {
      var w = (dist[k] / sum * 100).toFixed(2);
      return '<i class="' + GROUP_CLS[k] + '" data-aw="' + w + '" style="width:' + w + '%"></i>';
    }).join("");
    var legend = present.map(function (k) {
      var you = cur.rating_type === k ? ' \u00B7 <span class="ed-you">tu jeste\u015B Ty</span>' : "";
      return '<span><span class="sq" style="background:' + GROUP_SQ[k] + '" aria-hidden="true"></span>' +
        esc(GROUP_WORD[k]) + " \u00B7 " + esc(fmt.percent(dist[k], 2)) + you + "</span>";
    }).join("");
    return '<section class="ed-tile ed-hover"><h2 class="sr-only">Jak radz\u0105 sobie inni kierowcy</h2>' +
      '<span class="ed-kicker">Jak radz\u0105 sobie inni kierowcy</span>' +
      '<div class="ed-dist-bar" role="img" aria-label="Rozk\u0142ad grup w rankingu">' + bar + "</div>" +
      '<div class="ed-legend">' + legend + "</div></section>";
  }

  /* True when the period carries evidence of having been scored and every piece
   * of that evidence says zero. Counts alone are not enough to key on: a
   * snapshot can omit them and still be a perfect period, in which case the
   * coefficients and the points lost are what testify. Any single non-zero
   * fact disqualifies, so a scored period with violations can never match. */
  function noViolations(cur) {
    var cats = cur.categories || [];
    var evidence = false, clean = true;
    cats.forEach(function (c) {
      ["count", "coefficient_per_100km", "points_lost"].forEach(function (k) {
        if (!isNum(c[k])) return;
        evidence = true;
        if (c[k] !== 0) clean = false;
      });
    });
    return evidence && clean;
  }

  /* Nearest improvable area, used only when the host published no shortlist.
   * Ranked by how far the coefficient must fall to reach the next band, then
   * by the larger gain, then by snapshot order — a stable, total ordering. */
  function nearestImprovable(cur) {
    var best = null;
    (cur.categories || []).forEach(function (c) {
      if (!isNum(c.coefficient_per_100km) || !isNum(c.target_upper_bound) ||
          !isNum(c.target_points_gain)) return;
      var gap = c.coefficient_per_100km - c.target_upper_bound;
      if (gap <= 0) return;
      if (!best || gap < best.gap ||
          (gap === best.gap && c.target_points_gain > best.gain)) {
        best = { cat: c, gap: gap, gain: c.target_points_gain };
      }
    });
    return best;
  }

  function winTile(cap, gain) {
    return '<section class="ed-tile win ed-hover"><h2 class="sr-only">Szybka wygrana</h2>' +
      '<span class="ed-kicker">Szybka wygrana</span>' +
      '<span class="ed-big">+' + esc(fmt.int(gain)) + NBSP + "pkt</span>" +
      (cap ? '<span class="ed-cap">' + esc(cap) + "</span>" : "") + "</section>";
  }

  /* The tile always renders. Four conditions: the host's shortlist, a spotless
   * period, the nearest improvable area, or nothing left to improve. */
  function quickWinTile(cur) {
    /* A spotless period is congratulated first, before the shortlist is even
     * consulted: with no violations there is nothing to improve, so a stray
     * near_threshold entry must not turn the tile back into a suggestion. */
    if (noViolations(cur)) {
      return '<section class="ed-tile win ed-hover"><h2 class="sr-only">Gratulacje</h2>' +
        '<span class="ed-kicker">Gratulacje!</span>' +
        '<span class="ed-cap ed-cap-lead">W bie\u017C\u0105cym okresie nie masz \u017Cadnych wykrocze\u0144, ' +
        'Tw\u00F3j styl jazdy jest perfekcyjny!</span></section>';
    }
    var nt = cur.near_threshold && cur.near_threshold.length ? cur.near_threshold[0] : null;
    if (!nt || !isNum(nt.points_gain)) {
      /* No shortlist, but the period has violations: fall back to the nearest
       * improvable area. Every field used here is published by the host — the
       * category's own coefficient and its target band, gain and label. The
       * ordering is the only thing decided here, and `gap` reproduces the
       * host's own `coefficient_distance`. See the note in the handoff: the
       * clean fix is host-side, by always shortlisting at least one area. */
      var near = nearestImprovable(cur);
      if (near) {
        return winTile(near.cat.label + " \u2014 wystarczy " + fmt.int(near.gap) +
          NBSP + "mniej na 100" + NBSP + "km", near.gain);
      }
      /* Violations, but no area has a better band to aim for: every category is
       * already in its best band, so there is nothing to ask for. */
      return '<section class="ed-tile win ed-hover"><h2 class="sr-only">Szybka wygrana</h2>' +
        '<span class="ed-kicker">Szybka wygrana</span>' +
        '<span class="ed-cap ed-cap-lead">Ka\u017Cdy obszar jest ju\u017C w najlepszym przedziale \u2014 ' +
        'tak trzymaj!</span></section>';
    }
    var cat = null;
    for (var i = 0; i < cur.categories.length; i++) {
      if (cur.categories[i].key === nt.category_key) cat = cur.categories[i];
    }
    var cap = "";
    if (cat && isNum(nt.coefficient_distance)) {
      cap = esc(cat.label) + " \u2014 wystarczy " + esc(fmt.int(nt.coefficient_distance)) +
        NBSP + "mniej na 100" + NBSP + "km";
    } else if (cat && has(nt.target_band_label)) {
      cap = esc(cat.label) + " \u2014 zejd\u017A do przedzia\u0142u " + esc(String(nt.target_band_label));
    }
    return '<section class="ed-tile win ed-hover"><h2 class="sr-only">Szybka wygrana</h2>' +
      '<span class="ed-kicker">Szybka wygrana</span>' +
      '<span class="ed-big">+' + esc(fmt.int(nt.points_gain)) + NBSP + "pkt</span>" +
      (cap ? '<span class="ed-cap">' + cap + "</span>" : "") + "</section>";
  }

  function lossesCard(cur) {
    var lossy = [], full = [], total = 0;
    cur.categories.forEach(function (c) {
      if (isNum(c.points_lost) && c.points_lost < 0) { lossy.push(c); total += c.points_lost; }
      else if (isNum(c.points_lost) && c.points_lost === 0) full.push(c);
    });
    lossy.sort(function (a, b) { return a.points_lost - b.points_lost; });
    var maxAbs = lossy.length ? Math.abs(lossy[0].points_lost) : 0;
    var rows = lossy.map(function (c, i) {
      var w = maxAbs > 0 ? (Math.abs(c.points_lost) / maxAbs * 100).toFixed(1) : "0";
      var col = LOSS_RAMP[Math.min(i, LOSS_RAMP.length - 1)];
      return '<div class="ed-loss-row' + (c.deemphasize ? " ed-deemph" : "") + '">' +
        '<span class="ed-loss-name">' + esc(c.label) + "</span>" +
        '<div class="ed-track"><div class="ed-fill" data-aw="' + w + '" style="width:' + w + "%;background:" + col + '"></div></div>' +
        '<span class="ed-loss-pts">' + esc(fmt.pointsSigned(c.points_lost)) + "</span></div>";
    }).join("");
    var banner = "";
    if (full.length) {
      banner = '<div class="ed-fullpts"><span class="ok" aria-hidden="true">\u2713</span>' +
        "<span><strong>Pe\u0142ne punkty w " + esc(fmt.int(full.length)) + NBSP +
        (full.length === 1 ? "obszarze" : "obszarach") + ":</strong> " +
        full.map(function (c) { return esc(c.label); }).join(" \u00B7 ") +
        " \u2014 tak trzymaj!</span></div>";
    }
    if (!rows && !banner) return "";
    return '<section class="ed-card"><div class="ed-card-h"><h2 class="ed-h2">Gdzie uciekaj\u0105 punkty</h2>' +
      (total < 0 ? '<span class="ed-total-loss">' + esc(fmt.pointsSigned(total)) + "</span>" : "") +
      "</div>" + rows + banner + "</section>";
  }

  function trendCard(entry, constants, periodType) {
    var series = entry.series || [];
    if (!series.length) return "";
    var max = constants.score_max || 100;
    /* trailing strictly-rising run (presentational; see handoff §insights) */
    var run = 1;
    for (var i = series.length - 1; i > 0; i--) {
      if (series[i].eco_score_total > series[i - 1].eco_score_total) run++;
      else break;
    }
    var title = run >= 3 && ORDINAL[run]
      ? "Wynik ro\u015Bnie " + ORDINAL[run] + " raz z rz\u0119du"
      : "Wynik w kolejnych okresach";
    var bars = series.map(function (p) {
      var h = Math.max(2, Math.min(100, p.eco_score_total / max * 100)).toFixed(1);
      var col = scoreColor(p.eco_score_total, constants, !p.is_current);
      return '<div class="ed-tcol' + (p.is_current ? " cur" : "") + '">' +
        '<span class="ed-tval">' + esc(fmt.int(p.eco_score_total)) + "</span>" +
        '<div class="ed-tbox"><div class="ed-tbar" data-ah="' + h + '" style="height:' + h + "%;background:" + col + '"></div></div>' +
        '<span class="ed-tlab">' + esc(rangeShort(p.start_date, p.end_date_display)) + "</span></div>";
    }).join("");
    var ref = "";
    if (entry.series_reference && isNum(entry.series_reference.eco_score_total)) {
      ref = '<p class="ed-note" style="margin:8px 0 0">poprzedni miesi\u0105c: ' +
        esc(fmt.int(entry.series_reference.eco_score_total)) + NBSP + "pkt</p>";
    }
    return '<section class="ed-card"><div class="ed-card-h"><h2 class="ed-h2">' + esc(title) + "</h2></div>" +
      '<div class="ed-trend">' + bars + "</div>" + ref + "</section>";
  }

  function slide1(entry, doc, periodType, slide) {
    var cur = entry.current;
    return '<div class="ed-slide" data-slide="0" data-screen-label="Slajd 1 Tw\u00F3j wynik"' +
      panelAttrs(0, slide) + ">" +
      header(true, "Twoje Eco Driving", entry.period_identity, periodType, "1 / 3 \u00B7 Tw\u00F3j wynik") +
      '<div class="ed-wrap">' +
      '<div class="ed-hero">' + scoreCard(cur, doc.constants) +
      '<div class="ed-tiles">' + tiles(cur) + "</div></div>" +
      '<div class="ed-grid2">' + lossesCard(cur) + trendCard(entry, doc.constants, periodType) + "</div>" +
      '<div class="ed-endpill"><span>koniec sekcji \u00B7 przewi\u0144 dalej, aby zobaczy\u0107 wykroczenia \u25BC</span></div>' +
      "</div></div>";
  }

  /* ------------------------------------------------------- slide 2 parts */

  function cmpCard(cur) {
    var cmp = cur.comparison;
    if (!cmp) {
      return '<section class="ed-card ed-hover"><span class="ed-kicker">Teraz vs poprzedni raport</span>' +
        '<div class="ed-chgs" style="margin-top:10px"><span class="ed-chip-nobasis">brak podstawy do por\u00F3wnania \u2014 to pierwszy zamkni\u0119ty okres w tej serii</span></div></section>';
    }
    var range = fmt.dateRange(cmp.basis_start_date, cmp.basis_end_date_display);
    var prev = isNum(cmp.previous_eco_score_total)
      ? '<div class="ed-cmp-col"><span class="ed-cmp-prev">' +
        cu(fmt.int(cmp.previous_eco_score_total), 'data-cu-v="' + cmp.previous_eco_score_total + '" data-cu-seq="a"') +
        '</span><span class="ed-cmp-cap">' + esc(range) + "</span></div>" +
        '<span class="ed-cmp-arrow" aria-hidden="true">\u2192</span>' : "";
    var delta = "";
    if (isNum(cmp.eco_score_delta)) {
      var d = cmp.eco_score_delta;
      /* zero is not a rise: it takes the same no-change language slide 1 uses,
       * so the two slides never describe one fact two different ways */
      delta = d > 0 ? '<span class="ed-cmp-delta">\u25B2 +' + esc(fmt.int(d)) + "</span>"
        : d < 0 ? '<span class="ed-cmp-delta neg">\u25BC ' + esc(fmt.int(d)) + "</span>"
        : '<span class="ed-cmp-delta flat">= bez zmian</span>';
    }
    return '<section class="ed-card ed-hover"><span class="ed-kicker">Teraz vs poprzedni raport</span>' +
      '<div class="ed-cmp" style="margin-top:10px">' + prev +
      '<div class="ed-cmp-col"><span class="ed-cmp-now">' +
      (isNum(cur.eco_score_total) ? cu(fmt.int(cur.eco_score_total), 'data-cu-v="' + cur.eco_score_total + '" data-cu-seq="b"') : "\u2013") +
      '</span><span class="ed-cmp-cap">teraz</span></div>' + delta + "</div></section>";
  }

  function changesCard(cur) {
    var imp = [], wor = [], same = 0;
    cur.categories.forEach(function (c) {
      if (isNum(c.coefficient_per_100km) && isNum(c.previous_coefficient_per_100km)) {
        if (c.coefficient_per_100km < c.previous_coefficient_per_100km) imp.push(c);
        else if (c.coefficient_per_100km > c.previous_coefficient_per_100km) wor.push(c);
        else same++;
      }
    });
    if (!imp.length && !wor.length && !same) return "";
    var bySwing = function (a, b) {
      return Math.abs(b.coefficient_per_100km - b.previous_coefficient_per_100km) -
        Math.abs(a.coefficient_per_100km - a.previous_coefficient_per_100km);
    };
    imp.sort(bySwing); wor.sort(bySwing);
    var ex = function (c) {
      return esc(c.short_label) + ": " + esc(fmt.int(c.previous_coefficient_per_100km)) +
        " \u2192 " + esc(fmt.int(c.coefficient_per_100km)) + " na 100 km";
    };
    var chips = "";
    if (imp.length) chips += '<div class="ed-chg good ed-fade" data-fade="0.55"><span class="n">\u25BC ' + imp.length +
      '</span><span><strong>' + (imp.length === 1 ? "poprawa" : "poprawy") + "</strong><br>" + ex(imp[0]) + "</span></div>";
    if (wor.length) chips += '<div class="ed-chg bad ed-fade" data-fade="0.85"><span class="n">\u25B2 ' + wor.length +
      '</span><span><strong>' + (wor.length === 1 ? "pogorszenie" : "pogorszenia") + "</strong><br>" + ex(wor[0]) + "</span></div>";
    if (same) chips += '<div class="ed-chg flat ed-fade" data-fade="0.98"><span class="n">= ' + same +
      '</span><span><strong>bez zmian</strong><br>pozosta\u0142e obszary</span></div>';
    return '<section class="ed-card ed-hover"><span class="ed-kicker">Co si\u0119 zmieni\u0142o od poprzedniego raportu</span>' +
      '<div class="ed-chgs" style="margin-top:10px">' + chips + "</div></section>";
  }

  function prevBandIndex(c) {
    if (!isNum(c.previous_coefficient_per_100km)) return null;
    for (var i = 0; i < c.bands.length; i++) {
      var b = c.bands[i];
      if (b.upper_bound === null || c.previous_coefficient_per_100km <= b.upper_bound) return i;
    }
    return null;
  }

  function axisRow(c, i) {
    var n = c.bands.length;
    if (!n) return "";
    var idT = "ed-axtips-t-" + i, idB = "ed-axtips-b-" + i;
    var idN = "ed-axname-" + i, idR = "ed-axrate-" + i, idP = "ed-axpts-" + i;
    var segs = c.bands.map(function (b) {
      var cls = b.status === "green" ? "g" : b.status === "yellow" ? "y" : b.status === "red" ? "r" : "n";
      return '<div class="ed-seg ' + cls + '" style="width:' + (100 / n).toFixed(3) + '%"></div>';
    }).join("");
    var tipsTop = '<div class="ed-tips top" id="' + idT + '" data-reveal="soft" role="group" ' +
      'aria-label="Przedzia\u0142y wykrocze\u0144" aria-hidden="true">' +
      c.bands.map(function (b) { return "<span>" + esc(b.label) + "</span>"; }).join("") + "</div>";
    var tipsBot = '<div class="ed-tips bot" id="' + idB + '" data-reveal="soft" role="group" ' +
      'aria-label="Utrata punkt\u00F3w w przedzia\u0142ach" aria-hidden="true">' +
      c.bands.map(function (b) { return "<span>" + esc(fmt.pointsSigned(b.points_lost)) + "</span>"; }).join("") + "</div>";
    var mkNow = "";
    if (isNum(c.marker_band_index)) {
      var x = ((c.marker_band_index + 0.5) / n * 100).toFixed(2);
      mkNow = '<div class="ed-mk-now" data-ax="' + x + '" style="left:' + x + '%"></div>';
    }
    var pIdx = prevBandIndex(c), mkPrev = "";
    if (pIdx !== null) {
      var px = ((pIdx + 0.5) / n * 100).toFixed(2);
      mkPrev = '<div class="ed-mk-prev" data-ax="' + px + '" style="left:' + px + '%"></div>';
    }
    var chg = "";
    if (isNum(c.coefficient_per_100km) && isNum(c.previous_coefficient_per_100km)) {
      var now = c.coefficient_per_100km, was = c.previous_coefficient_per_100km;
      chg = now < was ? '<i class="imp">\u25BC by\u0142o ' + esc(fmt.int(was)) + "</i>"
        : now > was ? '<i class="wor">\u25B2 by\u0142o ' + esc(fmt.int(was)) + "</i>"
        : '<i class="same">= bez zmian</i>';
    }
    /* The chip is what aria-describedby points at, so the space between the
     * two elements is deliberate: it keeps the description one natural phrase
     * ("-15 pkt straconych") while the flex column still stacks them. */
    var pts = isNum(c.points_lost)
      ? '<span class="ed-ax-pts' + (c.points_lost === 0 ? " zero" : "") + '" id="' + idP + '">' +
        "<b>" + esc(fmt.pointsSigned(c.points_lost)) + "</b> <i>straconych</i></span>"
      : '<span id="' + idP + '"></span>';
    var sr = esc(c.label) + ": " +
      (isNum(c.count) ? fmt.plural(c.count, fmt.FORMS.events) + ", " : "") +
      (has(c.band_label) ? "przedzia\u0142 " + esc(String(c.band_label)) : "");
    var controls = idT + " " + idB;
    return '<div class="ed-axis-row' + (c.deemphasize ? " ed-deemph" : "") + '" data-action="axtap">' +
      '<button type="button" class="ed-axis-name" id="' + idN + '" data-action="axtap" ' +
      'aria-expanded="false" aria-controls="' + controls + '" ' +
      'aria-describedby="' + idR + " " + idP + '">' + esc(c.label) + "</button>" +
      '<div class="ed-axis">' +
      '<div class="ed-axis-segs" role="img" aria-label="' + sr + '">' + segs + "</div>" +
      tipsTop + mkPrev + mkNow + tipsBot + "</div>" +
      '<div class="ed-rate" id="' + idR + '"><b>' + esc(coefText(c.coefficient_per_100km)) + "</b> " + chg + "</div>" +
      pts + "</div>";
  }

  function axesCard(cur) {
    var rows = cur.categories.map(axisRow).join("");
    var full = cur.categories.filter(function (c) { return isNum(c.points_lost) && c.points_lost === 0; });
    var banner = full.length
      ? '<div class="ed-fullpts"><span class="ok" aria-hidden="true">\u2713</span><span><strong>Pe\u0142ne punkty w ' +
        esc(fmt.int(full.length)) + NBSP + (full.length === 1 ? "obszarze" : "obszarach") + ":</strong> " +
        full.map(function (c) { return esc(c.label); }).join(" \u00B7 ") + " \u2014 tak trzymaj!</span></div>"
      : "";
    return '<section class="ed-card"><div class="ed-card-h"><h2 class="ed-h2">Ka\u017Cde wykroczenie na osi</h2>' +
      '<div class="ed-ax-legend">' +
      '<span><span class="ed-dot-now" aria-hidden="true"></span> teraz</span>' +
      '<span><span class="ed-dot-prev" aria-hidden="true"></span> poprzedni raport</span>' +
      '<span class="ed-ax-hint"><span class="grad" aria-hidden="true"></span>najed\u017A lub dotknij o\u015B, aby zobaczy\u0107 szczeg\u00F3\u0142y segment\u00F3w</span>' +
      "</div></div>" + rows + banner + "</section>";
  }

  function slide2(entry, doc, periodType, slide) {
    var cur = entry.current;
    return '<div class="ed-slide" data-slide="1" data-screen-label="Slajd 2 Wykroczenia"' +
      panelAttrs(1, slide) + ">" +
      header(false, "Szczeg\u00F3\u0142y wykrocze\u0144", entry.period_identity, periodType, "2 / 3 \u00B7 Wykroczenia na osi") +
      '<div class="ed-wrap">' +
      '<div class="ed-hero s2">' + cmpCard(cur) + changesCard(cur) + "</div>" +
      axesCard(cur) +
      '<div class="ed-endpill"><span>koniec sekcji \u00B7 przewi\u0144 dalej, aby zobaczy\u0107 podsumowanie dzienne \u25BC</span></div>' +
      "</div></div>";
  }

  /* ------------------------------------------------------- slide 3 parts */

  function orderedDayCats(cats) {
    var byKey = {};
    cats.forEach(function (c) { byKey[c.key] = c; });
    return DAY_ORDER.map(function (k) { return byKey[k]; }).filter(Boolean);
  }

  function daysCard(entry, cur, periodType) {
    var days = cur.days || [];
    if (!days.length) return "";
    var cats = orderedDayCats(cur.categories);
    var head = '<div class="ed-tr ed-thead" aria-hidden="false"><span class="lft">Dzie\u0144</span><span>Dystans [km]</span>' +
      cats.map(function (c) { return "<span>" + esc(dayColName(c)) + "</span>"; }).join("") + "</div>";
    var maxKm = 0, maxEv = 0, maxKmDay = null, maxEvDay = null;
    days.forEach(function (d) {
      if (d.kilometers > maxKm) { maxKm = d.kilometers; maxKmDay = d; }
      var ev = 0;
      d.categories.forEach(function (dc) { if (isNum(dc.count)) ev += dc.count; });
      if (ev > maxEv) { maxEv = ev; maxEvDay = d; }
    });
    var byKeyIdx = {};
    cur.categories.forEach(function (c, i) { byKeyIdx[c.key] = i; });
    var rows = days.map(function (d, i) {
      var rest = d.kilometers === 0;
      var bg = rest ? "#F1F4EF" : (i % 2 ? "#F9FBF8" : "#FFFFFF");
      var cells = cats.map(function (c) {
        var dc = null;
        for (var j = 0; j < d.categories.length; j++) if (d.categories[j].key === c.key) dc = d.categories[j];
        if (!dc || rest || dc.status === "neutral") {
          return '<span class="z"><span aria-hidden="true">\u2014</span><span class="sr-only">' +
            esc(dayColName(c)) + ": nieoceniane</span></span>";
        }
        var zero = isNum(dc.count) && dc.count === 0;
        return '<span class="' + (zero ? "z" : "v") + '"><span aria-hidden="true">' +
          (isNum(dc.count) ? esc(fmt.int(dc.count)) : "\u2014") + "</span>" +
          '<span class="sr-only">' + esc(dayColName(c)) + ": " +
          (isNum(dc.count) ? esc(fmt.plural(dc.count, fmt.FORMS.events)) : "nieoceniane") + "</span></span>";
      }).join("");
      /* Plain rows: the per-day disclosure was removed by owner decision, so
       * there is no button, no caret and no detail block. The period's
       * coefficients still live on slide 2, at the axes. */
      return '<div class="ed-tr ed-day ed-casc" data-casc="' + i + '"' +
        ' style="background:' + bg + '">' +
        '<span class="d">' + esc(fmt.dateShort(d.date)) + " <small>\u00B7 " +
        esc(d.weekday_short) + "</small></span>" +
        (rest ? '<span class="z"><span aria-hidden="true">\u2014</span><span class="sr-only">dzie\u0144 bez jazdy</span></span>'
              : '<span class="km">' + esc(fmt.int(d.kilometers)) + "</span>") +
        cells + "</div>";
    }).join("");
    var catTotals = cats.map(function (c) {
      var hiVal = 0;
      cats.forEach(function (cc) { if (isNum(cc.count) && cc.count > hiVal) hiVal = cc.count; });
      var v = isNum(c.count) ? c.count : 0;
      var cls = v === 0 ? "zz" : (v === hiVal ? "hi" : "");
      return '<span class="' + cls + '">' + esc(fmt.int(v)) + "</span>";
    }).join("");
    var foot = '<div class="ed-tr ed-tfoot"><span class="lft">Razem</span>' +
      '<span class="sum-km">' + esc(fmt.int(cur.total_kilometers)) + "</span>" + catTotals + "</div>";
    var p = fmt.dateShort(cur.period_start_date || "") || "";
    var mIdx = null, year = "";
    var mm = String(cur.period_start_date || "").match(/^(\d{4})-(\d{2})/);
    if (mm) { year = mm[1]; mIdx = parseInt(mm[2], 10) - 1; }
    var title = "Dzie\u0144 po dniu" + (mIdx !== null && MONTHS[mIdx] ? " \u00B7 " + MONTHS[mIdx] + " " + year : "");
    var hls = "";
    if (maxKmDay) hls += '<div class="ed-hl km ed-fade" data-fade="0.9"><b>' + esc(fmt.unit(maxKm, "km")) +
      "</b><span><strong>najwi\u0119kszy dzienny dystans</strong> \u00B7 " + esc(fmt.dateShort(maxKmDay.date)) + "</span></div>";
    if (maxEvDay && maxEv > 0) hls += '<div class="ed-hl ev ed-fade" data-fade="0.97"><b>' +
      esc(fmt.plural(maxEv, ["wykroczenie", "wykroczenia", "wykrocze\u0144"])) +
      "</b><span><strong>najwi\u0119cej wykrocze\u0144</strong> \u00B7 " + esc(fmt.dateShort(maxEvDay.date)) + "</span></div>";
    return '<section class="ed-card"><div class="ed-card-h"><h2 class="ed-h2">' + esc(title) + "</h2>" +
      '<div class="ed-ax-legend"><span>liczby to sumy z danego dnia</span>' +
      '<span style="color:var(--ghost);font-weight:700">0 = brak zdarze\u0144</span>' +
      "<span>\u2014 = dzie\u0144 bez jazdy</span></div></div>" +
      /* A3 - the pan track is the only way to reach the columns beyond the
       * viewport at 320px, so it has to be reachable WITHOUT a pointer. Once
       * the per-day disclosure was removed it held no focusable descendant at
       * all, which left keyboard users unable to scroll it. Making the track
       * itself focusable (tabindex="0") is the standard remedy for a
       * scrollable region: the browser's own arrow-key scrolling then applies,
       * so no key handling is added here. It is named, because a bare tab stop
       * announcing nothing is its own defect, and role="group" is the same
       * vocabulary the axis tips already use. */
      '<div class="ed-scrollx" tabindex="0" role="group" ' +
      'aria-label="Tabela dzie\u0144 po dniu \u2014 przewi\u0144 w poziomie, ' +
      'aby zobaczy\u0107 wszystkie kolumny">' +
      '<div class="ed-table">' + head + rows + foot + "</div></div>" +
      (hls ? '<div class="ed-hls" style="margin-top:12px">' + hls + "</div>" : "") +
      '<p class="ed-note" style="margin:10px 0 0">Tabela pokazuje sumy zdarze\u0144 z danego dnia. Wska\u017Aniki ca\u0142ego okresu znajdziesz na poprzednim slajdzie, przy osiach wykrocze\u0144.</p>' +
      "</section>";
  }

  function slide3(entry, doc, periodType, slide) {
    var cur = entry.current;
    var range = fmt.dateRange(entry.period_identity.period_start_date, entry.period_identity.period_end_date_display);
    return '<div class="ed-slide" data-slide="2" data-screen-label="Slajd 3 Podsumowanie dzienne"' +
      panelAttrs(2, slide) + ">" +
      header(false, "Podsumowanie dzienne", entry.period_identity, periodType, "3 / 3 \u00B7 Dzie\u0144 po dniu") +
      '<div class="ed-wrap">' + daysCard(entry, cur, periodType) +
      '<div class="ed-endpill quiet"><span>koniec raportu \u00B7 wynik dotyczy okresu ' + esc(range) + "</span></div>" +
      "</div></div>";
  }

  /* ------------------------------------------------------------ dashboard */

  function renderDashboard(doc, route) {
    var period = route && doc.periods[route.period] ? route.period
      : (doc.periods.weekly ? "weekly" : "monthly");
    var entry = doc.periods[period];
    var slide = route && isNum(route.slide) ? Math.max(0, Math.min(2, route.slide)) : 0;

    if (entry.status !== "OK" || !entry.current) {
      var identity = entry.period_identity;
      var rng = fmt.dateRange(identity.period_start_date, identity.period_end_date_display);
      var isInsuf = entry.status === "INSUFFICIENT_DISTANCE";
      var title = isInsuf ? "Za ma\u0142o kilometr\u00F3w w tym okresie" : "Raport w przygotowaniu";
      var body = isInsuf
        ? "Raport powstaje, gdy w ca\u0142ym okresie " + (rng ? rng + " " : "") +
          "przejedziesz \u0142\u0105cznie co najmniej " + fmt.unit(doc.constants.min_qualifying_distance_km, "km") +
          ". Ten okres nie zawiera oceny."
        : "Raport za okres " + (rng ? rng + " " : "") + "nie jest jeszcze gotowy. Zajrzyj tu p\u00F3\u017Aniej.";
      return '<div class="ed-stage"><div class="ed-slide" data-slide="0">' +
        header(true, "Twoje Eco Driving", identity, period, "") +
        '<div class="ed-wrap"><section class="ed-card ed-state">' +
        '<div class="icon" aria-hidden="true"><svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#6C7A66" stroke-width="1.7" stroke-linecap="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/></svg></div>' +
        "<h2>" + esc(title) + "</h2><p>" + esc(body) + "</p></section></div></div></div>";
    }

    var labels = ["Tw\u00F3j wynik", "Wykroczenia na osi", "Podsumowanie dzienne"];
    var dots = '<div class="ed-dots" role="tablist" aria-orientation="vertical" ' +
      'aria-label="Sekcje raportu">' +
      [0, 1, 2].map(function (i) {
        var sel = i === slide;
        return '<button type="button" class="ed-dot" role="tab" id="ed-tab-' + i +
          '" data-action="dot" data-i="' + i + '" aria-selected="' + (sel ? "true" : "false") +
          '" aria-controls="ed-panel-' + i + '" tabindex="' + (sel ? "0" : "-1") +
          '"><span class="sr-only">' + labels[i] + "</span></button>";
      }).join("") + "</div>";

    return '<div class="ed-stage" id="ed-stage">' +
      '<div class="ed-mover" id="ed-mover" style="--seg:' + slide + '">' +
      slide1(entry, doc, period, slide) + slide2(entry, doc, period, slide) +
      slide3(entry, doc, period, slide) +
      "</div>" + dots + "</div>";
  }

  return {
    renderAccessState: renderAccessState,
    renderDashboard: renderDashboard,
    renderSkeleton: renderSkeleton,
    scoreColor: scoreColor
  };
});
