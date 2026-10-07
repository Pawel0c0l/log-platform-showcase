/* Log Platform — Database Explorer export panel and background states (S8).
 *
 * The panel is a server-rendered POST form and stays one. This module adds only
 * what a form cannot express by itself:
 *
 *   1. the `Zaznaczone wiersze` scope, whose row set exists only in the browser
 *      because an S7 rectangle is client-side by construction;
 *   2. the live count and path notice as the user switches scope;
 *   3. the submit label, which changes with the resolved path;
 *   4. `Kopiuj ref` on the background-export list.
 *
 * What it deliberately does NOT do:
 *
 *   - **It grants nothing.** Every value it writes into the form — scope,
 *     column scope, format, row references — is re-validated server-side. The
 *     panel names a scope; it never supplies a row set, a column list or a count
 *     the server is bound by. `can_export_rows` is not a JavaScript boundary:
 *     without the grant the server renders no panel at all and refuses a forged
 *     POST independently.
 *   - **It never touches row identity.** The only row token it handles is the
 *     opaque S6 reference the server already rendered on each `<tr>`. It cannot
 *     decode it, and a raw `record_id` is not present in the page at all.
 *   - **It persists nothing.** No storage, no URL parameter. A selection that
 *     the grid clears clears here too, which is what stops a stale reference
 *     from ever reaching the server.
 */
(function () {
  "use strict";

  var strings = {};
  var panel = null;
  var form = null;
  var selectedReferences = [];

  /* ------------------------------------------------------------- utilities */

  function parseStrings(node) {
    try {
      return JSON.parse(node.getAttribute("data-db-export-strings") || "{}");
    } catch (error) {
      return {};
    }
  }

  /* Polish digit grouping with a non-breaking space, matching the server. */
  function groupDigits(value) {
    var text = String(value);
    var out = "";
    for (var i = 0; i < text.length; i += 1) {
      if (i > 0 && (text.length - i) % 3 === 0) {
        out += " ";
      }
      out += text.charAt(i);
    }
    return out;
  }

  function scopeInput(scope) {
    return form ? form.querySelector('input[name="row_scope"][value="' + scope + '"]') : null;
  }

  function currentScope() {
    if (!form) {
      return "view";
    }
    var inputs = form.querySelectorAll('input[name="row_scope"]');
    for (var i = 0; i < inputs.length; i += 1) {
      if (inputs[i].checked) {
        return inputs[i].getAttribute("value") || "view";
      }
    }
    return "view";
  }

  function currentFormat() {
    if (!form) {
      return "xlsx";
    }
    var inputs = form.querySelectorAll("[data-db-export-format]");
    for (var i = 0; i < inputs.length; i += 1) {
      if (inputs[i].checked) {
        return inputs[i].getAttribute("value") || "xlsx";
      }
    }
    return "xlsx";
  }

  function countFor(scope) {
    if (scope === "selection") {
      return selectedReferences.length;
    }
    var node = form ? form.querySelector('[data-db-export-count="' + scope + '"]') : null;
    if (!node) {
      /* The dataset total can legitimately be unknown; an unknown count must
         stay unknown rather than borrow another scope's number. */
      return null;
    }
    var digits = String(node.textContent || "").replace(/[^0-9]/g, "");
    return digits ? parseInt(digits, 10) : null;
  }

  /* The same rule the server applies, stated here only so the notice can update
     without a round trip. The submit handler re-derives it authoritatively. */
  function pathFor(total) {
    if (total === null || total === undefined) {
      return "unknown";
    }
    if (total > (strings.ceiling || 0)) {
      return "refused";
    }
    if (total > (strings.directCap || 0)) {
      return "background";
    }
    return "direct";
  }

  /* ------------------------------------------------------------- rendering */

  function syncPath() {
    if (!form) {
      return;
    }
    var scope = currentScope();
    var total = countFor(scope);
    var path = pathFor(total);
    var notice = form.querySelector("[data-db-export-path]");
    if (notice) {
      var text = strings[path] || strings.unknown || "";
      if (scope === "selection" && !selectedReferences.length) {
        text = strings.selectionEmpty || text;
        path = "unknown";
      }
      notice.textContent = text;
      notice.className = "db-export-path is-" + path;
    }
    var submit = form.querySelector("[data-db-export-submit]");
    if (submit) {
      if (path === "background" || path === "refused") {
        submit.textContent = strings.submitBackground || "";
      } else {
        submit.textContent = String(strings.submitDirect || "").replace(
          "{format}",
          currentFormat().toUpperCase()
        );
      }
      /* An impossible scope is refused by making the form unsubmittable, not by
         letting the user commit and meet a server error. */
      var blocked = path === "refused" || (scope === "selection" && !selectedReferences.length);
      if (blocked) {
        submit.setAttribute("disabled", "");
        submit.setAttribute("aria-disabled", "true");
      } else {
        submit.removeAttribute("disabled");
        submit.removeAttribute("aria-disabled");
      }
    }
  }

  function syncSelectionOption() {
    var option = form ? form.querySelector("[data-db-export-selection]") : null;
    if (!option) {
      return;
    }
    var label = option.parentNode;
    var count = label ? label.querySelector("[data-db-export-count]") : null;
    if (count) {
      count.textContent = groupDigits(selectedReferences.length);
    }
    if (selectedReferences.length) {
      option.removeAttribute("disabled");
    } else {
      option.setAttribute("disabled", "");
      if (option.checked) {
        /* A selection that just cleared must not leave the panel pointing at a
           row set that no longer exists. */
        var fallback = scopeInput("view");
        if (fallback) {
          fallback.checked = true;
        }
      }
    }
  }

  /* The references travel as hidden inputs so the form still submits exactly
     what the panel shows, with no separate request path and no JSON payload. */
  function syncRowFields() {
    var host = form ? form.querySelector("[data-db-export-rows]") : null;
    if (!host) {
      return;
    }
    while (host.firstChild) {
      host.removeChild(host.firstChild);
    }
    if (currentScope() !== "selection") {
      return;
    }
    var field = strings.rowField || "row_ref";
    for (var i = 0; i < selectedReferences.length; i += 1) {
      var input = document.createElement("input");
      input.setAttribute("type", "hidden");
      input.setAttribute("name", field);
      input.setAttribute("value", selectedReferences[i]);
      host.appendChild(input);
    }
  }

  function syncAll() {
    syncSelectionOption();
    syncRowFields();
    syncPath();
  }

  /* ------------------------------------------------------------- selection */

  /* S7 reports which rendered rows its rectangle touches, by position. Mapping
     a position to its opaque reference happens here rather than in the
     selection module, so the grid keeps knowing nothing about row identity or
     about export. */
  function referencesForRows(rowIndexes) {
    var table = document.querySelector("table.db-table");
    if (!table) {
      return [];
    }
    var rows = table.querySelectorAll("tbody tr");
    var out = [];
    var seen = {};
    var limit = strings.maxSelected || rows.length;
    for (var i = 0; i < rowIndexes.length; i += 1) {
      var row = rows[rowIndexes[i]];
      if (!row) {
        continue;
      }
      var reference = row.getAttribute("data-db-row");
      /* A row with no reference has no configured identity and therefore no
         addressable existence; it is skipped rather than guessed at. */
      if (!reference || seen[reference]) {
        continue;
      }
      seen[reference] = true;
      out.push(reference);
      if (out.length >= limit) {
        break;
      }
    }
    return out;
  }

  function onSelectionChange(event) {
    var detail = (event && event.detail) || {};
    var rows = detail.rows || [];
    selectedReferences = referencesForRows(rows);
    syncAll();
  }

  /* ------------------------------------------------------------ copy a ref */

  function fallbackCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.appendChild(area);
    if (area.select) {
      area.select();
    }
    var ok = false;
    try {
      ok = document.execCommand ? !!document.execCommand("copy") : false;
    } catch (error) {
      ok = false;
    }
    if (area.parentNode) {
      area.parentNode.removeChild(area);
    }
    return ok;
  }

  function announce(message) {
    var region = document.querySelector("[data-db-export-status]");
    if (region) {
      region.textContent = message;
    }
  }

  function copyReference(reference) {
    var clipboard = window.navigator && window.navigator.clipboard;
    function ok() { announce(strings.copied || ""); }
    function fail() { announce(strings.copyFailed || ""); }
    if (clipboard && clipboard.writeText) {
      try {
        var promise = clipboard.writeText(reference);
        if (promise && promise.then) {
          promise.then(ok, function () {
            if (fallbackCopy(reference)) { ok(); } else { fail(); }
          });
          return;
        }
      } catch (error) {
        /* Fall through to the same fallback a rejection would use. */
      }
    }
    if (fallbackCopy(reference)) { ok(); } else { fail(); }
  }

  function initCopyButtons() {
    document.addEventListener("click", function (event) {
      var button = event.target && event.target.closest
        ? event.target.closest("[data-db-export-copy]")
        : null;
      if (!button) {
        return;
      }
      event.preventDefault();
      /* The job reference and nothing else — never the failure text, and never
         a storage key, which the page does not hold in the first place. */
      copyReference(button.getAttribute("data-db-export-copy") || "");
    });
  }

  /* ------------------------------------------------------------------ init */

  function initPanel() {
    panel = document.querySelector("[data-db-export-panel]");
    if (!panel) {
      return;
    }
    strings = parseStrings(panel);
    form = panel.querySelector("[data-db-export-form]");
    if (!form) {
      return;
    }
    form.addEventListener("change", syncAll);
    var sheet = document.querySelector("[data-db-sheet]");
    if (sheet) {
      sheet.addEventListener("db-selection-change", onSelectionChange);
    }
    syncAll();
  }

  function init() {
    initPanel();
    var list = document.querySelector("[data-db-export-list]");
    if (list && !strings.copied) {
      strings = parseStrings(list);
    }
    initCopyButtons();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
