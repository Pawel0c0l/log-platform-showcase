/* Log Platform — Database Explorer value distributions (DB-005 §4).
 *
 * Supplemental discovery UI over the S3 filter menus. It is deliberately not
 * required for filtering: with scripting off the menus still carry their
 * operator select, their value fields and their Apply, and this file simply
 * never runs. Nothing here is a security or correctness boundary — the endpoint
 * re-authorizes every request and resolves the column's family itself.
 *
 * What it does:
 *
 *   1. fetches a column's distribution the first time its menu opens, never on
 *      page load and never for a menu the user has not opened;
 *   2. renders the distinct-value picker (text) or the 14-bucket histogram
 *      (numeric), plus the identity's non-null count;
 *   3. stages selections into the existing S3 form fields — the column menu's
 *      own `Zastosuj` still applies them (`D-005`);
 *   4. moves the histogram threshold marker as the user types, with no request;
 *   5. drops stale responses so a slow column cannot overwrite a newer one.
 */
(function () {
  "use strict";

  var NBSP = " ";

  /* Request identity per container. A response is applied only if its token is
     still the one the container is waiting for, which is what stops a slow
     column from painting over a menu the user has since moved on from. */
  var tokens = new WeakMap ? new WeakMap() : null;
  var counter = 0;

  function text(node, value) {
    node.textContent = value;
  }

  function el(tag, className, content) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (content !== undefined && content !== null) {
      node.textContent = String(content);
    }
    return node;
  }

  /* Every user-facing string is translated on the server and handed down as a
     JSON data attribute on the container. That keeps the translation catalogue
     the single source of the vocabulary, keeps Polish out of this file, and
     needs no inline <script> — the row sheet ships none, and must not start. */
  var stringCache = new WeakMap ? new WeakMap() : null;

  function strings(container) {
    if (stringCache && stringCache.has(container)) {
      return stringCache.get(container);
    }
    var parsed = {};
    try {
      parsed = JSON.parse(container.getAttribute("data-db-strings") || "{}");
    } catch (err) {
      parsed = {};
    }
    if (stringCache) {
      stringCache.set(container, parsed);
    }
    return parsed;
  }

  function format(raw, params) {
    if (!raw) {
      return "";
    }
    if (!params) {
      return raw;
    }
    return String(raw).replace(/\{(\w+)\}/g, function (match, name) {
      return Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match;
    });
  }

  function groupDigits(value) {
    var digits = String(value);
    var sign = "";
    if (digits.charAt(0) === "-") {
      sign = "-";
      digits = digits.slice(1);
    }
    var out = "";
    while (digits.length > 3) {
      out = NBSP + digits.slice(-3) + out;
      digits = digits.slice(0, -3);
    }
    return sign + digits + out;
  }

  /* ------------------------------------------------------------- fetching */

  function containersFor(menu) {
    var found = menu.querySelectorAll("[data-db-distribution]");
    return Array.prototype.slice.call(found);
  }

  function load(container) {
    /* Loaded, or already in flight. Without the second check, open → close →
       reopen while the first request is still running fires a second aggregate
       for the same scope. The flag is cleared on failure, so reopening after an
       error still retries. */
    if (container.getAttribute("data-db-loaded") === "true") {
      return;
    }
    if (container.getAttribute("data-db-loading") === "true") {
      return;
    }
    var url = container.getAttribute("data-db-distribution-url");
    if (!url || !window.fetch) {
      return;
    }
    container.setAttribute("data-db-loading", "true");
    var body = container.querySelector("[data-db-distribution-body]");
    counter += 1;
    var token = counter;
    if (tokens) {
      tokens.set(container, token);
    }
    var S = strings(container);
    if (body) {
      body.setAttribute("aria-busy", "true");
      body.innerHTML = "";
      body.appendChild(el("p", "db-distribution-note", S.loading));
    }
    window.fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }).then(function (response) {
      if (!response.ok) {
        throw new Error("distribution unavailable");
      }
      return response.json();
    }).then(function (payload) {
      if (tokens && tokens.get(container) !== token) {
        /* A newer request owns this container now. */
        return;
      }
      container.removeAttribute("data-db-loading");
      container.setAttribute("data-db-loaded", "true");
      render(container, payload);
    }).catch(function () {
      if (tokens && tokens.get(container) !== token) {
        /* A newer request owns the container and its flag; leave it in flight. */
        return;
      }
      container.removeAttribute("data-db-loading");
      /* Not marked loaded: reopening the menu retries. Manual filtering in this
         same menu is untouched either way. */
      if (body) {
        body.setAttribute("aria-busy", "false");
        body.innerHTML = "";
        body.appendChild(el("p", "db-distribution-error", S.error));
      }
    });
  }

  /* ------------------------------------------------------------ rendering */

  function render(container, payload) {
    var S = strings(container);
    var menu = container.closest ? container.closest("[data-db-col-menu]") : null;
    if (menu && payload && typeof payload.non_null_count === "number") {
      var slot = menu.querySelector("[data-db-nonnull]");
      if (slot) {
        text(slot, format(S.nonNull, { count: groupDigits(payload.non_null_count) }));
      }
    }
    var body = container.querySelector("[data-db-distribution-body]");
    if (!body) {
      return;
    }
    body.setAttribute("aria-busy", "false");
    body.innerHTML = "";
    if (payload.mode === "categorical") {
      renderCategorical(S, body, payload, menu);
    } else if (payload.mode === "numeric") {
      renderNumeric(S, container, body, payload, menu);
    }
    /* The counts exclude this column's own filter, so the scope is stated in
       words. Without it the numbers read as if they described the visible rows
       and would silently disagree with the toolbar counter. */
    body.appendChild(el("p", "db-distribution-scope", S.scope));
  }

  function renderCategorical(S, body, payload, menu) {
    var values = payload.values || [];
    if (!values.length) {
      body.appendChild(el("p", "db-distribution-note", S.empty));
      return;
    }
    body.appendChild(el("p", "db-distribution-meta",
      format(S.distinct, { count: groupDigits(payload.distinct_count) })));
    if (payload.truncated) {
      /* The list is the top n of a larger domain, and stays labelled as such —
         local search below narrows this fetched set, not the database. */
      body.appendChild(el("p", "db-distribution-note",
        format(S.truncated, { shown: groupDigits(values.length), total: groupDigits(payload.distinct_count) })));
    }

    var max = 0;
    values.forEach(function (item) { max = Math.max(max, item.count); });

    /* Search over the fetched rows only. It is a presentation filter: it issues
       no request, changes no applied filter, and hiding a selected row does not
       deselect it. */
    var search = document.createElement("input");
    search.type = "search";
    search.className = "db-distribution-search";
    search.setAttribute("placeholder", S.search || "");
    search.setAttribute("aria-label", S.search || "");
    body.appendChild(search);
    body.appendChild(el("p", "db-distribution-note", S.searchHint));

    var list = el("ul", "db-distribution-values");
    var rows = values.map(function (item) {
      var row = valueRow(S, item, max, menu);
      list.appendChild(row);
      return { row: row, needle: item.value === null ? "" : String(item.value).toLowerCase() };
    });
    body.appendChild(list);

    var noMatches = el("p", "db-distribution-note", S.noMatches);
    noMatches.setAttribute("hidden", "");
    body.appendChild(noMatches);

    search.addEventListener("input", function () {
      var needle = String(search.value || "").toLowerCase();
      var shown = 0;
      rows.forEach(function (entry) {
        var visible = needle === "" || entry.needle.indexOf(needle) !== -1;
        if (visible) {
          entry.row.removeAttribute("hidden");
          shown += 1;
        } else {
          entry.row.setAttribute("hidden", "");
        }
      });
      if (shown === 0) {
        noMatches.removeAttribute("hidden");
      } else {
        noMatches.setAttribute("hidden", "");
      }
    });

    /* Visible rows start reflecting the existing staged selection. */
    syncCheckboxes(menu);

    /* Values already selected but absent from this fetched set are counted, so
       the user can see that the staged filter is wider than the visible list. */
    var offList = selection(menu).filter(function (value) {
      return !values.some(function (item) { return item.value === value; });
    });
    if (offList.length) {
      body.appendChild(el("p", "db-distribution-meta",
        format(S.offList, { count: groupDigits(offList.length) })));
    }
  }

  /* NULL and the empty string are shown with their own markers and their own
     counts, because S2 made that distinction a hard requirement and a value
     picker that merged them would undo it. They are not `in (…)` candidates —
     `in` compares text and cannot express NULL — so they offer the approved
     `puste` operator instead, which covers exactly those two. A whitespace-only
     value is ordinary text and stays selectable. */
  function valueRow(S, item, max, menu) {
    var row = el("li", "db-distribution-value");
    var isNull = item.value === null;
    var isEmpty = item.value === "";
    var share = max > 0 ? Math.round((item.count / max) * 100) : 0;

    var label;
    if (isNull) {
      label = S.nullMarker;
    } else if (isEmpty) {
      label = S.blankMarker;
    } else {
      label = item.value;
    }

    var control;
    if (isNull || isEmpty) {
      control = el("button", "db-distribution-blank");
      control.type = "button";
      control.setAttribute("aria-label", format(S.selectBlank, { count: groupDigits(item.count) }));
      control.addEventListener("click", function () { stageBlank(menu); });
    } else {
      control = el("label", "db-distribution-pick");
      var box = document.createElement("input");
      box.type = "checkbox";
      box.value = item.value;
      box.setAttribute("data-db-value-pick", "");
      box.setAttribute("aria-label", format(S.selectValue, { value: item.value, count: groupDigits(item.count) }));
      box.addEventListener("change", function () { toggleValue(S, menu, box); });
      control.appendChild(box);
    }

    var name = el("span", "db-distribution-label" + (isNull || isEmpty ? " db-distribution-marker" : ""), label);
    var bar = el("span", "db-distribution-bar");
    var fill = el("span", "db-distribution-fill");
    fill.style.width = share + "%";
    bar.appendChild(fill);
    var count = el("span", "db-distribution-count", groupDigits(item.count));

    control.appendChild(name);
    row.appendChild(control);
    row.appendChild(bar);
    row.appendChild(count);
    return row;
  }

  /* ------------------------------------------------------- selection model */

  /* The staged `in (…)` selection for a menu, as an ordered list of exact
     values. It is seeded once from the server's validated record, which is the
     only representation that survives whitespace-only text, leading or trailing
     spaces and embedded newlines. Rebuilding it from the visible checkboxes —
     as this file first did — silently dropped every selected value that was not
     in the fetched top set. */
  var selections = new WeakMap ? new WeakMap() : null;

  function multiContainer(menu) {
    return menu ? menu.querySelector("[data-db-value-multi]") : null;
  }

  function selection(menu) {
    if (!menu) {
      return [];
    }
    if (selections && selections.has(menu)) {
      return selections.get(menu);
    }
    var current = [];
    var container = multiContainer(menu);
    if (container) {
      try {
        var seeded = JSON.parse(container.getAttribute("data-db-selected") || "[]");
        if (Object.prototype.toString.call(seeded) === "[object Array]") {
          current = seeded.map(function (value) { return String(value); });
        }
      } catch (err) {
        current = [];
      }
    }
    if (selections) {
      selections.set(menu, current);
    }
    return current;
  }

  function setSelection(menu, values) {
    if (selections) {
      selections.set(menu, values);
    }
    writeSelection(menu, values);
  }

  /* Staging only. The menu's own `Zastosuj` is still what applies it (`D-005`),
     and it writes through the canonical S3 parameter contract — one hidden
     `filter_exact__` input per value — rather than inventing a
     distribution-only filter path. The textarea is cleared because the two
     forms would otherwise disagree, and the exact form is the one that can
     carry every value the picker can display. */
  function writeSelection(menu, values) {
    var container = multiContainer(menu);
    var host = menu ? menu.querySelector("[data-db-exact-values]") : null;
    var operator = menu ? menu.querySelector("[data-db-op]") : null;
    if (!container || !host || !operator) {
      return;
    }
    host.innerHTML = "";
    values.forEach(function (value) {
      var field = document.createElement("input");
      field.type = "hidden";
      field.name = host.getAttribute("data-db-exact-name") || "";
      field.value = value;
      host.appendChild(field);
    });
    var textarea = container.querySelector("textarea");
    if (textarea) {
      textarea.value = "";
    }
    container.setAttribute("data-db-selected", JSON.stringify(values));
    if (values.length) {
      operator.value = "in";
    }
    fire(operator, "change");
  }

  function syncCheckboxes(menu) {
    if (!menu) {
      return;
    }
    var current = selection(menu);
    var boxes = menu.querySelectorAll("[data-db-value-pick]");
    Array.prototype.slice.call(boxes).forEach(function (box) {
      box.checked = current.indexOf(box.value) !== -1;
    });
  }

  /* One visible checkbox changed. Everything else — including every selected
     value the fetched list does not contain — is carried through untouched. */
  function toggleValue(S, menu, box) {
    var current = selection(menu).slice();
    var index = current.indexOf(box.value);
    if (box.checked) {
      if (index === -1) {
        var limit = parseInt(S.maxValues, 10);
        if (!isNaN(limit) && current.length >= limit) {
          /* Refuse rather than truncate: the previous valid selection stands and
             the cap is stated, matching the server's own refusal. */
          box.checked = false;
          announce(menu, format(S.limitReached, { limit: String(limit) }));
          return;
        }
        current.push(box.value);
      }
    } else if (index !== -1) {
      current.splice(index, 1);
    }
    setSelection(menu, current);
  }

  function announce(menu, message) {
    var body = menu ? menu.querySelector("[data-db-distribution-body]") : null;
    if (!body) {
      return;
    }
    var existing = body.querySelector(".db-distribution-limit");
    if (existing) {
      existing.textContent = message;
      return;
    }
    body.appendChild(el("p", "db-distribution-limit", message));
  }

  function stageBlank(menu) {
    if (!menu) {
      return;
    }
    var operator = menu.querySelector("[data-db-op]");
    if (!operator) {
      return;
    }
    operator.value = "blank";
    fire(operator, "change");
  }

  function fire(node, type) {
    var event;
    try {
      event = new Event(type, { bubbles: true });
    } catch (err) {
      event = document.createEvent("Event");
      event.initEvent(type, true, false);
    }
    node.dispatchEvent(event);
  }

  /* ------------------------------------------------------------ histogram */

  function renderNumeric(S, container, body, payload, menu) {
    if (payload.domain === "empty") {
      body.appendChild(el("p", "db-distribution-note", S.empty));
      return;
    }
    if (payload.domain === "all_null") {
      body.appendChild(el("p", "db-distribution-note", S.allNull));
      return;
    }
    if (payload.domain === "single_value") {
      body.appendChild(el("p", "db-distribution-note", format(S.singleValue, { value: payload.min })));
      return;
    }

    var buckets = payload.buckets || [];
    var max = 0;
    buckets.forEach(function (bucket) { max = Math.max(max, bucket.count); });

    var chart = el("div", "db-histogram");
    chart.setAttribute("role", "img");
    chart.setAttribute("aria-label", format(S.range, { min: payload.min, max: payload.max }));
    buckets.forEach(function (bucket) {
      var bar = el("span", "db-histogram-bar");
      bar.setAttribute("data-db-bucket", String(bucket.index));
      bar.setAttribute("data-db-bucket-min", bucket.min);
      bar.setAttribute("data-db-bucket-max", bucket.max);
      bar.style.height = (max > 0 ? Math.max(2, Math.round((bucket.count / max) * 100)) : 2) + "%";
      chart.appendChild(bar);
    });
    body.appendChild(chart);

    var axis = el("p", "db-histogram-axis", format(S.range, { min: payload.min, max: payload.max }));
    body.appendChild(axis);

    /* The bars carry no text, so the same numbers are given to assistive
       technology as an ordinary list rather than as a picture only. */
    var table = el("ul", "lp-visually-hidden");
    buckets.forEach(function (bucket) {
      table.appendChild(el("li", null,
        format(S.bucket, { min: bucket.min, max: bucket.max, count: groupDigits(bucket.count) })));
    });
    body.appendChild(table);

    bindMarker(container, menu, payload);
  }

  /* The marker is presentation state: it follows what is typed in the menu and
     never issues a query. Seeing where a threshold lands BEFORE applying is the
     stated point of the histogram (TGS §3.3). */
  function bindMarker(container, menu, payload) {
    if (!menu) {
      return;
    }
    var chart = container.querySelector(".db-histogram");
    if (!chart) {
      return;
    }
    var inputs = Array.prototype.slice.call(
      menu.querySelectorAll('[data-db-value-single] input, [data-db-value-range] input')
    );
    if (!inputs.length) {
      return;
    }

    function update() {
      var operator = menu.querySelector("[data-db-op]");
      var mode = operator ? operator.value : "";
      var bars = Array.prototype.slice.call(chart.querySelectorAll(".db-histogram-bar"));
      bars.forEach(function (bar) { bar.classList.remove("db-histogram-marked"); });
      if (mode === "blank" || mode === "in") {
        return;
      }
      var values = inputs
        .filter(function (input) { return input.offsetParent !== null || input.value !== ""; })
        .map(function (input) { return parseFloat(input.value); })
        .filter(function (value) { return !isNaN(value); });
      if (!values.length) {
        return;
      }
      var low = Math.min.apply(null, values);
      var high = Math.max.apply(null, values);
      bars.forEach(function (bar) {
        var barMin = parseFloat(bar.getAttribute("data-db-bucket-min"));
        var barMax = parseFloat(bar.getAttribute("data-db-bucket-max"));
        if (barMax >= low && barMin <= high) {
          bar.classList.add("db-histogram-marked");
        }
      });
    }

    inputs.forEach(function (input) {
      input.addEventListener("input", update);
    });
    var operator = menu.querySelector("[data-db-op]");
    if (operator) {
      operator.addEventListener("change", update);
    }
    update();
  }

  /* ----------------------------------------------------------------- init */

  function init() {
    var menus = document.querySelectorAll("[data-db-col-menu]");
    for (var i = 0; i < menus.length; i += 1) {
      (function (menu) {
        menu.addEventListener("toggle", function () {
          if (!menu.open) {
            return;
          }
          containersFor(menu).forEach(load);
        });
      })(menus[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
