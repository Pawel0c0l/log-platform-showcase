/* Log Platform — Database Explorer grid selection and clipboard (stage S7).
 *
 * Excel-like rectangular cell selection over the rows the server already
 * rendered, plus `Ctrl`/`⌘`+`C` as spreadsheet-pasteable TSV.
 *
 *   * pointer  — press a data cell to anchor, drag to extend, `Shift`+click to
 *                extend from the existing anchor
 *   * keyboard — arrows move the active cell, `Shift`+arrows extend the range,
 *                `Home`/`End` reach the row edges (`INTERACTION_SPEC.md` §2.2)
 *   * copy     — one rectangle, tab-separated, CRLF between rows
 *
 * Deliberate boundaries, each of which is a product decision rather than a
 * simplification:
 *
 *   - **Page-local.** The rectangle can never leave the rendered page. `D-012`
 *     settles phase 1 as pagination without cross-page selection, so extension
 *     stops at the first and last rendered row and never requests a page.
 *   - **Ephemeral.** Selection is not in the URL, not in localStorage and not on
 *     the server. A reload, a filter, a sort or a page change starts with none —
 *     `INTERACTION_SPEC.md` §5 requires exactly that, and every one of those
 *     transitions is a real navigation here, so a new document is the mechanism.
 *   - **No data of its own.** The clipboard is built from the same
 *     `data-db-copy` value the S2 single-cell copy uses, read out of the cells
 *     on screen. This module issues no request, and it cannot disclose anything
 *     the page did not already show.
 *   - **No technical identity.** Only `td[data-db-column]` — the approved
 *     user-visible columns — participate. The row identity is not in the page at
 *     all, and the opaque S6 row reference is not cell data: the detail column
 *     carries no `data-db-column` and is therefore outside the selection
 *     universe entirely.
 *   - **Disabled at ≤768 px** (`RESPONSIVE_SPEC.md`), because a touch drag
 *     across cells fights the table's own scrolling. Below the cutoff the table
 *     behaves exactly as it did before this module existed, including ordinary
 *     browser text copy.
 */
(function () {
  "use strict";

  var sheet = null;
  var strings = {};
  var media = null;

  /* WHICH TABLE THIS INSTANCE DRIVES.
     The behaviour below is not Database-Explorer-specific -- a rectangle of
     cells, extended by pointer or keyboard, copied as TSV, is the same
     interaction wherever a server-rendered table appears. What IS specific is
     the vocabulary: which table element, which attribute marks a participating
     column, which attribute carries the clipboard value.

     Resolved once at init from the host element, defaulting to the Database
     Explorer's own names so that surface is untouched by this generalisation.
     A singleton is still correct: these tables live on different pages, and
     only one of them is ever in a given document. */
  var profile = {
    table: "table.db-table",
    columnAttr: "data-db-column",
    copyAttr: "data-db-copy"
  };

  function colSel(tag) {
    return tag + "[" + profile.columnAttr + "]";
  }

  /* The selection, held by *semantic* identity: a row by its position on the
     rendered page, a column by its name. Positional column indexes would be a
     bug waiting for the first S5 reorder — the same index means a different
     value once columns move. */
  var anchor = null; /* { row: <int>, column: <name> } */
  var active = null;
  var painted = [];
  var dragging = false;

  /* ------------------------------------------------------------ enablement */

  function enabled() {
    /* No matchMedia (an old browser, or a harness) is treated as desktop: the
       feature is additive, and refusing it everywhere would be the larger
       regression. The CSS cutoff is the visual half of the same rule. */
    return !media || !!media.matches;
  }

  /* ----------------------------------------------------------- grid model */

  function grid() {
    var table = document.querySelector(profile.table);
    if (!table) {
      return null;
    }
    var heads = table.querySelectorAll("thead " + colSel("th"));
    var columns = [];
    for (var i = 0; i < heads.length; i += 1) {
      columns.push(heads[i].getAttribute(profile.columnAttr));
    }
    var rows = table.querySelectorAll("tbody tr");
    if (!columns.length || !rows.length) {
      return null;
    }
    return { table: table, columns: columns, rows: rows };
  }

  function columnIndex(model, name) {
    for (var i = 0; i < model.columns.length; i += 1) {
      if (model.columns[i] === name) {
        return i;
      }
    }
    return -1;
  }

  /* Cells of one row, keyed by column name. Built per row per operation rather
     than cached across them: the header order is re-read every time, which is
     what keeps a live width change, a pin or a reorder from ever attaching a
     selection to a different value. */
  function rowCells(row) {
    var map = {};
    var cells = row.querySelectorAll(colSel("td"));
    for (var i = 0; i < cells.length; i += 1) {
      map[cells[i].getAttribute(profile.columnAttr)] = cells[i];
    }
    return map;
  }

  /* The rectangle in current display coordinates, or null when the selection no
     longer resolves — a selected column that has been hidden, for instance. A
     stale rectangle is cleared rather than approximated. */
  function rectangle(model) {
    if (!anchor || !active) {
      return null;
    }
    var a = columnIndex(model, anchor.column);
    var b = columnIndex(model, active.column);
    if (a === -1 || b === -1) {
      return null;
    }
    if (anchor.row >= model.rows.length || active.row >= model.rows.length) {
      return null;
    }
    return {
      top: Math.min(anchor.row, active.row),
      bottom: Math.max(anchor.row, active.row),
      left: Math.min(a, b),
      right: Math.max(a, b)
    };
  }

  /* --------------------------------------------------------------- painting */

  function unpaint() {
    for (var i = 0; i < painted.length; i += 1) {
      var cell = painted[i];
      cell.classList.remove("db-cell-in-range");
      cell.classList.remove("db-cell-active");
      cell.classList.remove("db-range-t");
      cell.classList.remove("db-range-b");
      cell.classList.remove("db-range-l");
      cell.classList.remove("db-range-r");
    }
    painted = [];
  }

  function paint(model, rect) {
    unpaint();
    if (!rect) {
      return;
    }
    for (var r = rect.top; r <= rect.bottom; r += 1) {
      var cells = rowCells(model.rows[r]);
      for (var c = rect.left; c <= rect.right; c += 1) {
        var cell = cells[model.columns[c]];
        if (!cell) {
          continue;
        }
        cell.classList.add("db-cell-in-range");
        /* Edge classes, not a border per cell: an internal border would read as
           a table rule that is not there, and the rectangle must look continuous
           across a pinned column boundary. */
        if (r === rect.top) { cell.classList.add("db-range-t"); }
        if (r === rect.bottom) { cell.classList.add("db-range-b"); }
        if (c === rect.left) { cell.classList.add("db-range-l"); }
        if (c === rect.right) { cell.classList.add("db-range-r"); }
        if (active && r === active.row && model.columns[c] === active.column) {
          cell.classList.add("db-cell-active");
        }
        painted.push(cell);
      }
    }
  }

  /* ------------------------------------------------------------ vocabulary */

  /* Polish plural categories: 1, 2–4, everything else — with the teens as the
     exception the naive rule gets wrong (12 behaves like 25, not like 2). The
     server holds the words; this holds only the rule. */
  function plural(count, forms) {
    if (!forms || forms.length < 3) {
      return "";
    }
    if (count === 1) {
      return forms[0];
    }
    var lastTwo = count % 100;
    if (lastTwo >= 12 && lastTwo <= 14) {
      return forms[2];
    }
    var last = count % 10;
    return last >= 2 && last <= 4 ? forms[1] : forms[2];
  }

  function quantity(count, key) {
    return String(count) + " " + plural(count, strings[key]);
  }

  function summaryText(rect) {
    var rows = rect.bottom - rect.top + 1;
    var columns = rect.right - rect.left + 1;
    return String(strings.summary || "")
      .replace("{rows}", quantity(rows, "row"))
      .replace("{columns}", quantity(columns, "column"))
      .replace("{cells}", quantity(rows * columns, "cell"));
  }

  /* ------------------------------------------------------------ announcing */

  function statusRegion() {
    return document.querySelector("[data-db-select-status]");
  }

  function announce(message) {
    var region = statusRegion();
    if (region) {
      region.textContent = message;
    }
  }

  /* The only thing this module publishes outward: which *rendered rows* the
     rectangle currently touches, by position on the page. Deliberately not the
     cells, not the values and above all not the row references — the export
     panel resolves positions to opaque S6 references itself, so the grid keeps
     knowing nothing about row identity or about export (`S8` consumes this;
     `S7` does not depend on anything consuming it). */
  function publishSelection(rect) {
    var host = sheet;
    if (!host || typeof window.CustomEvent !== "function") {
      return;
    }
    var rows = [];
    if (rect) {
      for (var r = rect.top; r <= rect.bottom; r += 1) {
        rows.push(r);
      }
    }
    try {
      host.dispatchEvent(new window.CustomEvent("db-selection-change", {
        detail: { rows: rows, cells: rect ? rows.length * (rect.right - rect.left + 1) : 0 }
      }));
    } catch (error) {
      /* A listener that throws must not take the grid down with it. */
    }
  }

  function syncFooter(rect) {
    var counter = document.querySelector("[data-db-select-count]");
    if (!counter) {
      return;
    }
    if (!rect) {
      counter.textContent = "";
      counter.setAttribute("hidden", "");
      return;
    }
    counter.textContent = summaryText(rect);
    counter.removeAttribute("hidden");
  }

  /* ------------------------------------------------------------------ sync */

  /* One place where the model, the paint, the footer and the live region are
     brought back into agreement. `speak` is false while a drag is still moving,
     so a pointer sweep does not narrate every cell it crosses. */
  function sync(speak) {
    var model = grid();
    if (!model) {
      clear(false);
      return null;
    }
    var rect = rectangle(model);
    if (!rect) {
      /* The selection stopped resolving — a column it covered is gone. Clearing
         is the deterministic answer; keeping a partial rectangle would copy a
         different set of values than the user selected. */
      anchor = null;
      active = null;
      unpaint();
      syncFooter(null);
      publishSelection(null);
      return null;
    }
    paint(model, rect);
    syncFooter(rect);
    publishSelection(rect);
    if (speak) {
      announce(summaryText(rect));
    }
    return { model: model, rect: rect };
  }

  function clear(speak) {
    anchor = null;
    active = null;
    unpaint();
    syncFooter(null);
    publishSelection(null);
    if (speak) {
      announce("");
    }
  }

  /* ------------------------------------------------------------- targeting */

  /* A control inside a cell keeps its own meaning. Starting a drag because the
     user reached for a resize handle, a column menu, a filter control or the
     row-detail trigger would make those controls unusable. */
  function isInteractive(target) {
    return !!(
      target &&
      target.closest &&
      target.closest(
        "a, button, input, select, textarea, summary, label, [contenteditable='true']," +
        " [data-db-col-menu], [data-db-resize], [data-db-columns], [data-db-row-open]," +
        " [data-db-distribution], [data-db-cols-list]"
      )
    );
  }

  function isEditable(target) {
    return !!(
      target &&
      target.closest &&
      target.closest("input, textarea, select, [contenteditable='true']")
    );
  }

  /* The selectable universe: a body cell that names an approved visible column.
     The detail column carries no `data-db-column`, so the opaque row reference
     is outside the selection geometry by construction rather than by a filter
     that could be forgotten. */
  function dataCell(target) {
    if (!target || !target.closest) {
      return null;
    }
    var cell = target.closest(colSel("td"));
    if (!cell) {
      return null;
    }
    return cell.closest("tbody") ? cell : null;
  }

  function positionOf(model, cell) {
    var row = cell.closest("tr");
    if (!row) {
      return null;
    }
    for (var i = 0; i < model.rows.length; i += 1) {
      if (model.rows[i] === row) {
        return { row: i, column: cell.getAttribute(profile.columnAttr) };
      }
    }
    return null;
  }

  /* ---------------------------------------------------------------- pointer */

  function onPointerDown(event) {
    if (!enabled() || (event.button !== undefined && event.button !== 0)) {
      return;
    }
    if (isInteractive(event.target)) {
      return;
    }
    var model = grid();
    if (!model) {
      return;
    }
    var cell = dataCell(event.target);
    if (!cell) {
      return;
    }
    var at = positionOf(model, cell);
    if (!at) {
      return;
    }
    /* A double click is the approved way to select a cell's text
       (`INTERACTION_SPEC.md` §8), so the native behaviour is left alone there.
       A single press suppresses it, because a text drag across cells is not the
       product's selection model and the two fighting looks broken. */
    if ((event.detail || 1) < 2 && event.preventDefault) {
      event.preventDefault();
      dropNativeSelection();
    }
    if (event.shiftKey && anchor) {
      active = at;
    } else {
      anchor = at;
      active = at;
    }
    dragging = true;
    sync(false);
    focusActive();
  }

  function onPointerOver(event) {
    if (!dragging || !enabled()) {
      return;
    }
    var model = grid();
    if (!model) {
      return;
    }
    var cell = dataCell(event.target);
    if (!cell) {
      return;
    }
    var at = positionOf(model, cell);
    if (!at) {
      return;
    }
    if (active && at.row === active.row && at.column === active.column) {
      return;
    }
    active = at;
    sync(false);
  }

  function endDrag() {
    if (!dragging) {
      return;
    }
    dragging = false;
    /* The committed range is announced once, on release, and focus lands on the
       active edge so the keyboard can carry on extending from there. */
    sync(true);
    focusActive();
  }

  function dropNativeSelection() {
    try {
      var selection = window.getSelection ? window.getSelection() : null;
      if (selection && selection.removeAllRanges) {
        selection.removeAllRanges();
      }
    } catch (error) {
      /* Nothing depends on this; it only stops a stray text highlight. */
    }
  }

  /* --------------------------------------------------------------- keyboard */

  function focusActive() {
    var model = grid();
    if (!model || !active) {
      return;
    }
    var cells = rowCells(model.rows[active.row]);
    var cell = cells[active.column];
    if (!cell) {
      return;
    }
    if (!cell.hasAttribute("tabindex")) {
      cell.setAttribute("tabindex", "-1");
    }
    if (cell.focus) {
      cell.focus();
    }
  }

  function clamp(value, low, high) {
    return value < low ? low : (value > high ? high : value);
  }

  /* Move the active edge. The anchor never moves, so travelling back toward it
     shrinks the rectangle exactly as a spreadsheet does. Bounds are the first
     and last *rendered* row and the first and last displayed column: no wrap,
     and above all no page change (`D-012` phase 1). */
  function step(model, rowDelta, columnDelta, toEdge) {
    var current = columnIndex(model, active.column);
    if (current === -1) {
      return false;
    }
    var row = clamp(active.row + rowDelta, 0, model.rows.length - 1);
    var column = current;
    if (toEdge) {
      column = columnDelta < 0 ? 0 : model.columns.length - 1;
    } else if (columnDelta) {
      column = clamp(current + columnDelta, 0, model.columns.length - 1);
    }
    if (row === active.row && column === current) {
      return false;
    }
    active = { row: row, column: model.columns[column] };
    return true;
  }

  var ARROWS = {
    ArrowUp: [-1, 0],
    ArrowDown: [1, 0],
    ArrowLeft: [0, -1],
    ArrowRight: [0, 1]
  };

  function onKeyDown(event) {
    if (!enabled() || isEditable(event.target)) {
      return;
    }
    if (event.key === "Escape") {
      onEscape(event);
      return;
    }
    if ((event.ctrlKey || event.metaKey) && String(event.key).toLowerCase() === "c") {
      onCopy(event);
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) {
      return;
    }
    var arrow = ARROWS[event.key];
    var edge = event.key === "Home" || event.key === "End";
    if (!arrow && !edge) {
      return;
    }
    var model = grid();
    if (!model) {
      return;
    }
    if (!active) {
      /* Entering the grid from the keyboard: only from inside the table, and
         only onto the first cell of the row focus is already on. Reaching in
         from anywhere on the page would steal arrow keys from the document. */
      var host = event.target && event.target.closest ? event.target.closest("tbody tr") : null;
      if (!host || !arrow) {
        return;
      }
      var at = null;
      for (var i = 0; i < model.rows.length; i += 1) {
        if (model.rows[i] === host) {
          at = { row: i, column: model.columns[0] };
          break;
        }
      }
      if (!at) {
        return;
      }
      anchor = at;
      active = at;
      event.preventDefault();
      sync(true);
      focusActive();
      return;
    }
    /* An open row drawer owns `↑`/`↓` for its own traversal (`DB-37`). Only the
       shifted form — which is unambiguously a range extension — is taken here
       while the drawer is on screen. */
    if (!event.shiftKey && document.querySelector("[data-db-row-detail]")) {
      return;
    }
    var moved = edge
      ? step(model, 0, event.key === "Home" ? -1 : 1, true)
      : step(model, arrow[0], arrow[1], false);
    if (!event.shiftKey) {
      /* An unshifted move collapses the range onto the new active cell. */
      anchor = active;
    }
    event.preventDefault();
    if (moved || !event.shiftKey) {
      sync(true);
      focusActive();
    }
  }

  function onEscape(event) {
    if (!anchor) {
      return;
    }
    /* Only the topmost transient surface is dismissed (`INTERACTION_SPEC.md`
       §3): the navigation drawer, a column menu, the column panel, the filter
       panel/drawer and the row drawer all sit above the grid, so each of them
       owns Escape before the selection does. The selection is the bottom of the
       stack and therefore also yields to any layer that already acted. */
    if (event.defaultPrevented) {
      return;
    }
    if (
      document.querySelector(
        "[data-nav-drawer]:not([hidden]), [data-db-col-menu][open]," +
          " [data-db-columns][open], [data-db-filters][open], [data-db-row-detail]"
      )
    ) {
      return;
    }
    /* The live region is emptied too: leaving the last summary standing would
       let a later, unrelated announcement read as a selection that is gone. */
    clear(true);
    if (event.preventDefault) {
      event.preventDefault();
    }
  }

  /* --------------------------------------------------------------- clipboard */

  /* One rectangle as spreadsheet-pasteable text: tabs between columns, CRLF
     between rows, and CSV-style quoting for any value that would otherwise
     break the geometry. A value containing a tab, a line break, a quote or
     meaningful edge whitespace is wrapped in `"` with internal quotes doubled —
     which is what Excel, LibreOffice and Sheets all read back as one cell. The
     value itself is never altered to make serialization easier. */
  function tsvField(value) {
    var text = String(value === null || value === undefined ? "" : value);
    if (/[\t\r\n"]/.test(text) || /^\s/.test(text) || /\s$/.test(text)) {
      return '"' + text.replace(/"/g, '""') + '"';
    }
    return text;
  }

  /* The canonical source value the S2 contract already publishes — never the
     rendered text, which is grouped, truncated, mid-elided, minute-precision or
     a badge. A cell with no `data-db-copy` is a NULL or an empty string, and
     both copy as empty by the approved contract. */
  function copyValue(cell) {
    /* The attribute may sit on the cell itself or on a descendant holder. The
       Database Explorer publishes it on an inner element (its cells wrap the
       value in badges and elision spans); a table whose cell IS the value can
       carry it directly rather than growing a span for the sake of this module. */
    if (cell.hasAttribute(profile.copyAttr)) {
      return cell.getAttribute(profile.copyAttr) || "";
    }
    var holder = cell.querySelector("[" + profile.copyAttr + "]");
    return holder ? (holder.getAttribute(profile.copyAttr) || "") : "";
  }

  function serialize(model, rect) {
    var lines = [];
    for (var r = rect.top; r <= rect.bottom; r += 1) {
      var cells = rowCells(model.rows[r]);
      var fields = [];
      for (var c = rect.left; c <= rect.right; c += 1) {
        var cell = cells[model.columns[c]];
        fields.push(tsvField(cell ? copyValue(cell) : ""));
      }
      lines.push(fields.join("\t"));
    }
    return lines.join("\r\n");
  }

  /* Same fallback the S2 single-cell copy uses, so a browser without the async
     clipboard keeps the feature instead of losing it. */
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

  function copied(rect) {
    announce(String(strings.copied || "").replace(
      "{cells}",
      quantity((rect.bottom - rect.top + 1) * (rect.right - rect.left + 1), "cell")
    ));
  }

  function copyFailed() {
    /* The selection survives a failed write: the user's next move is to try
       again, and destroying the rectangle would make that impossible. The
       message never contains any of the data that was being copied. */
    announce(String(strings.copyFailed || ""));
  }

  function onCopy(event) {
    var current = sync(false);
    if (!current) {
      /* No grid selection: this is an ordinary browser copy and must stay one. */
      return;
    }
    var text = serialize(current.model, current.rect);
    if (event.preventDefault) {
      event.preventDefault();
    }
    var rect = current.rect;
    var clipboard = window.navigator && window.navigator.clipboard;
    if (clipboard && clipboard.writeText) {
      try {
        var promise = clipboard.writeText(text);
        if (promise && promise.then) {
          promise.then(function () { copied(rect); }, function () {
            if (fallbackCopy(text)) { copied(rect); } else { copyFailed(); }
          });
          return;
        }
      } catch (error) {
        /* A throwing clipboard API must not break the grid; fall through. */
      }
    }
    if (fallbackCopy(text)) {
      copied(rect);
    } else {
      copyFailed();
    }
  }

  /* ------------------------------------------------------------------- init */

  function applyEnablement() {
    var on = enabled();
    sheet.setAttribute("data-db-selection", on ? "on" : "off");
    if (!on) {
      /* Crossing below the cutoff deactivates the feature and takes the footer
         state with it, so nothing stale is left claiming a selection that can no
         longer be extended or copied. */
      dragging = false;
      clear(false);
    }
  }

  function init() {
    sheet = document.querySelector("[data-db-sheet]");
    if (!sheet) {
      return;
    }
    /* Vocabulary overrides, before the first lookup that uses them. A page that
       sets none gets the Database Explorer's names, so that surface behaves
       exactly as it did before this module served more than one table. */
    profile.table = sheet.getAttribute("data-grid-table") || profile.table;
    profile.columnAttr =
      sheet.getAttribute("data-grid-column-attr") || profile.columnAttr;
    profile.copyAttr = sheet.getAttribute("data-grid-copy-attr") || profile.copyAttr;

    var table = document.querySelector(profile.table);
    if (!table) {
      return;
    }
    try {
      strings = JSON.parse(sheet.getAttribute("data-db-select-strings") || "{}");
    } catch (error) {
      strings = {};
    }
    var minWidth = parseInt(sheet.getAttribute("data-db-select-min-width") || "769", 10);
    if (!minWidth || minWidth < 1) {
      minWidth = 769;
    }
    if (window.matchMedia) {
      media = window.matchMedia("(min-width: " + minWidth + "px)");
      if (media.addEventListener) {
        media.addEventListener("change", applyEnablement);
      } else if (media.addListener) {
        media.addListener(applyEnablement);
      }
    }
    applyEnablement();

    table.addEventListener("pointerdown", onPointerDown);
    table.addEventListener("pointerover", onPointerOver);
    document.addEventListener("pointerup", endDrag);
    document.addEventListener("pointercancel", endDrag);
    document.addEventListener("keydown", onKeyDown);

    /* Back and Forward may swap the result identity underneath a selection —
       the density history entries `data-grid.js` pushes are in-page, and nothing
       guarantees the next one describes the same rows. Clearing is the honest
       answer; carrying the rectangle across would be a claim this module cannot
       verify, and the approved contract never asks for selection to survive. */
    window.addEventListener("popstate", function () { clear(false); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
