/* Log Platform — Database Explorer column management (approved stage S5).
 *
 * Everything here is an enhancement over markup that already works. The server
 * resolves visibility, order, width and pin state from the URL and renders the
 * result — the <colgroup>, the header, every body cell and the DB-008 panel —
 * so a page opened cold from an S5 link is already correct with no script at
 * all, and the panel's checkboxes, its ▲/▼ links, the column menu's pin/hide
 * links and its explicit width field keep working with scripting unavailable.
 *
 * What this module adds:
 *
 *   1. pointer resize with a live guide, and double-click autofit
 *   2. keyboard resize on the focused handle
 *   3. drag reorder in the DB-008 panel, staged until Zastosuj
 *   4. in-place application of order/width/pin changes — no requery, because
 *      the rows on screen already belong to the view
 *   5. URL and history synchronisation, and Back/Forward restoration
 *
 * Authorization lives entirely on the server. Nothing here can widen the
 * approved-column set: the catalogue this module resolves against is the set
 * the server already permitted and rendered.
 */
(function () {
  "use strict";

  var sheet = null;
  var strings = {};
  var CATALOG = [];
  var MIN_WIDTH = 64;
  var MAX_WIDTH = 480;
  var MAX_PINS = 4;
  var PIN_FRACTION = 0.4;
  var PIN_MAX_WIDTH = 576;
  var CHAR_PX = 7;
  var PAD_PX = 52;
  var initialCols = "";

  /* -------------------------------------------------------------- helpers */

  function tokens(value) {
    var out = [];
    var parts = String(value == null ? "" : value).split(",");
    for (var i = 0; i < parts.length; i += 1) {
      var token = parts[i].replace(/^\s+|\s+$/g, "");
      if (token) {
        out.push(token);
      }
    }
    return out;
  }

  /* Hand-parsed rather than via URLSearchParams, matching the browser floor the
     rest of the grid scripts target. Blank values are KEPT: a present-but-empty
     `colpin` is the explicit "nothing pinned" state and must not collapse into
     "nothing said". */
  function parseSearch(search) {
    var params = {};
    var query = String(search || "");
    if (query.charAt(0) === "?") {
      query = query.slice(1);
    }
    if (!query) {
      return params;
    }
    var parts = query.split("&");
    for (var i = 0; i < parts.length; i += 1) {
      if (!parts[i]) {
        continue;
      }
      var pair = parts[i].split("=");
      var key = decodeURIComponent((pair[0] || "").replace(/\+/g, " "));
      var value = decodeURIComponent((pair.slice(1).join("=") || "").replace(/\+/g, " "));
      if (!key) {
        continue;
      }
      (params[key] || (params[key] = [])).push(value);
    }
    return params;
  }

  function encodeSearch(params) {
    var parts = [];
    for (var key in params) {
      if (!Object.prototype.hasOwnProperty.call(params, key)) {
        continue;
      }
      var values = params[key] || [];
      for (var i = 0; i < values.length; i += 1) {
        parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(values[i]));
      }
    }
    return parts.join("&");
  }

  function clampWidth(raw) {
    var text = String(raw == null ? "" : raw).replace(/^\s+|\s+$/g, "");
    if (!text || text.length > 9 || !/^\d+$/.test(text)) {
      return null;
    }
    var value = parseInt(text, 10);
    if (!value) {
      return null;
    }
    return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, value));
  }

  function indexOf(list, value) {
    for (var i = 0; i < list.length; i += 1) {
      if (list[i] === value) {
        return i;
      }
    }
    return -1;
  }

  /* The documented fill rule, identical to the server's: named approved columns
     first in the order given, then every catalogue column the order did not
     name, in catalogue order. */
  function fillOrder(requested, catalog) {
    var ordered = [];
    for (var i = 0; i < requested.length; i += 1) {
      if (indexOf(catalog, requested[i]) !== -1 && indexOf(ordered, requested[i]) === -1) {
        ordered.push(requested[i]);
      }
    }
    for (var j = 0; j < catalog.length; j += 1) {
      if (indexOf(ordered, catalog[j]) === -1) {
        ordered.push(catalog[j]);
      }
    }
    return ordered;
  }

  function sameList(a, b) {
    if (a.length !== b.length) {
      return false;
    }
    for (var i = 0; i < a.length; i += 1) {
      if (a[i] !== b[i]) {
        return false;
      }
    }
    return true;
  }

  /* The shortest prefix the fill rule expands back to the full order, so moving
     one column costs one token in the URL rather than the whole permutation. */
  function compactOrder(resolved, catalog) {
    for (var length = 0; length <= resolved.length; length += 1) {
      if (sameList(fillOrder(resolved.slice(0, length), catalog), resolved)) {
        return resolved.slice(0, length);
      }
    }
    return resolved.slice(0);
  }

  /* ------------------------------------------------------------- DOM reads */

  function colElements() {
    return sheet.querySelectorAll("col[data-db-column]");
  }

  function defaultWidths() {
    var widths = {};
    var cols = colElements();
    for (var i = 0; i < cols.length; i += 1) {
      var name = cols[i].getAttribute("data-db-column");
      widths[name] = parseInt(cols[i].getAttribute("data-db-default-width"), 10) || MIN_WIDTH;
    }
    return widths;
  }

  function renderedWidth(name) {
    var cols = colElements();
    for (var i = 0; i < cols.length; i += 1) {
      if (cols[i].getAttribute("data-db-column") === name) {
        return parseInt(String(cols[i].style.width || "").replace("px", ""), 10) || MIN_WIDTH;
      }
    }
    return MIN_WIDTH;
  }

  /* ---------------------------------------------------------- the resolver */

  /* One resolver, used by every commit and by Back/Forward, so the URL and the
     rendered layout can never describe different arrangements. */
  function resolveLayout(params) {
    var defaults = defaultWidths();
    var requested = [];
    var raw = params.colorder || [];
    for (var i = 0; i < raw.length; i += 1) {
      requested = requested.concat(tokens(raw[i]));
    }
    var order = fillOrder(requested, CATALOG);

    var widths = {};
    var rawWidths = params.colw || [];
    for (var w = 0; w < rawWidths.length; w += 1) {
      var pairs = tokens(rawWidths[w]);
      for (var q = 0; q < pairs.length; q += 1) {
        var split = pairs[q].indexOf(":");
        if (split === -1) {
          continue;
        }
        var name = pairs[q].slice(0, split);
        if (indexOf(CATALOG, name) === -1 || Object.prototype.hasOwnProperty.call(widths, name)) {
          continue;
        }
        var width = clampWidth(pairs[q].slice(split + 1));
        if (width !== null) {
          widths[name] = width;
        }
      }
    }

    var requestedPins;
    var pinExplicit = Object.prototype.hasOwnProperty.call(params, "colpin");
    if (pinExplicit) {
      requestedPins = [];
      for (var p = 0; p < params.colpin.length; p += 1) {
        requestedPins = requestedPins.concat(tokens(params.colpin[p]));
      }
    } else {
      requestedPins = order.slice(0, 1);
    }

    var pins = [];
    var pinnedWidth = 0;
    for (var k = 0; k < requestedPins.length; k += 1) {
      var pin = requestedPins[k];
      if (indexOf(order, pin) === -1 || indexOf(pins, pin) !== -1) {
        continue;
      }
      if (pins.length >= MAX_PINS) {
        break;
      }
      var pinWidth = widths[pin] || defaults[pin] || MIN_WIDTH;
      if (pins.length && pinnedWidth + pinWidth > PIN_MAX_WIDTH) {
        break;
      }
      pins.push(pin);
      pinnedWidth += pinWidth;
    }

    var display = pins.slice(0);
    for (var d = 0; d < order.length; d += 1) {
      if (indexOf(pins, order[d]) === -1) {
        display.push(order[d]);
      }
    }
    var effective = {};
    for (var e = 0; e < display.length; e += 1) {
      effective[display[e]] = widths[display[e]] || defaults[display[e]] || MIN_WIDTH;
    }
    return {
      order: order,
      display: display,
      widths: widths,
      effective: effective,
      pins: pins,
      pinExplicit: pinExplicit
    };
  }

  /* --------------------------------------------------------------- applying */

  function reorderChildren(container, order) {
    if (!container) {
      return;
    }
    var known = {};
    var trailing = [];
    var children = [];
    for (var i = 0; i < container.childNodes.length; i += 1) {
      var child = container.childNodes[i];
      if (child.nodeType !== 1) {
        continue;
      }
      children.push(child);
      var name = child.getAttribute ? child.getAttribute("data-db-column") : null;
      if (name) {
        known[name] = child;
      } else {
        trailing.push(child);
      }
    }
    for (var j = 0; j < order.length; j += 1) {
      if (known[order[j]]) {
        container.appendChild(known[order[j]]);
      }
    }
    /* The row-detail link column is not part of layout state and always stays
       at the end. */
    for (var t = 0; t < trailing.length; t += 1) {
      container.appendChild(trailing[t]);
    }
  }

  function applyLayout(layout) {
    var table = sheet.querySelector(".db-table");
    if (!table) {
      return;
    }
    reorderChildren(table.querySelector("colgroup"), layout.display);
    var headRow = table.querySelector("thead tr");
    reorderChildren(headRow, layout.display);
    var bodyRows = table.querySelectorAll("tbody tr");
    for (var r = 0; r < bodyRows.length; r += 1) {
      reorderChildren(bodyRows[r], layout.display);
    }

    var total = 0;
    var cols = colElements();
    for (var c = 0; c < cols.length; c += 1) {
      var name = cols[c].getAttribute("data-db-column");
      var width = layout.effective[name];
      if (width) {
        cols[c].style.width = width + "px";
        total += width;
      }
    }
    var detail = table.querySelector("col.db-detail-col");
    if (detail) {
      total += parseInt(String(detail.style.width || "").replace("px", ""), 10) || 0;
    }
    table.style.width = total + "px";

    /* Sticky offsets accumulate in pin order, so several pinned columns sit
       side by side instead of on top of each other. They are recomputed from
       column identity every time, which is what stops a reorder from leaving a
       pin on whatever now occupies an old position. */
    var offsets = {};
    var running = 0;
    for (var p = 0; p < layout.pins.length; p += 1) {
      offsets[layout.pins[p]] = running;
      running += layout.effective[layout.pins[p]] || 0;
    }
    var last = layout.pins.length ? layout.pins[layout.pins.length - 1] : null;
    var cells = sheet.querySelectorAll("[data-db-column]");
    for (var i = 0; i < cells.length; i += 1) {
      var cell = cells[i];
      var tag = String(cell._tag || cell.tagName || "").toLowerCase();
      if (tag !== "th" && tag !== "td") {
        continue;
      }
      var cellName = cell.getAttribute("data-db-column");
      if (Object.prototype.hasOwnProperty.call(offsets, cellName)) {
        cell.classList.add("db-sticky-col");
        cell.style.left = offsets[cellName] + "px";
        if (cellName === last) {
          cell.classList.add("db-pin-edge");
        } else {
          cell.classList.remove("db-pin-edge");
        }
      } else {
        cell.classList.remove("db-sticky-col");
        cell.classList.remove("db-pin-edge");
        cell.style.left = "";
      }
    }
  }

  /* ------------------------------------------------------------- committing */

  function currentParams() {
    return parseSearch((window.location || {}).search || "");
  }

  function writeLayoutParams(params, layout) {
    var compact = compactOrder(layout.display, CATALOG);
    if (compact.length) {
      params.colorder = [compact.join(",")];
    } else {
      delete params.colorder;
    }

    var defaults = defaultWidths();
    var names = [];
    for (var name in layout.widths) {
      if (Object.prototype.hasOwnProperty.call(layout.widths, name)
          && indexOf(layout.display, name) !== -1
          && layout.widths[name] !== defaults[name]) {
        names.push(name);
      }
    }
    names.sort();
    if (names.length) {
      var encoded = [];
      for (var i = 0; i < names.length; i += 1) {
        encoded.push(names[i] + ":" + layout.widths[names[i]]);
      }
      params.colw = [encoded.join(",")];
    } else {
      delete params.colw;
    }

    /* Mirrors the server: the transitional default pin is derivable, so it is
       not serialized, and an explicit "nothing pinned" survives as a
       present-but-empty parameter. */
    var defaultPin = layout.display.length ? [layout.display[0]] : [];
    if (!layout.pinExplicit && sameList(layout.pins, defaultPin)) {
      delete params.colpin;
    } else if (layout.pins.length) {
      params.colpin = [layout.pins.join(",")];
    } else if (layout.pinExplicit) {
      params.colpin = [""];
    } else {
      delete params.colpin;
    }
    return params;
  }

  function historyAvailable() {
    return !!(window.history && typeof window.history.pushState === "function");
  }

  /* One committed layout state becomes one history entry. Intermediate drag or
     key-repeat movement is applied to the DOM but never pushed, so a resize
     leaves one entry rather than one per pixel. */
  function commit(layout, replace) {
    applyLayout(layout);
    if (!historyAvailable()) {
      return;
    }
    var params = writeLayoutParams(currentParams(), layout);
    var query = encodeSearch(params);
    var location = window.location || {};
    var url = String(location.pathname || "") + (query ? "?" + query : "");
    try {
      if (replace) {
        window.history.replaceState({ layout: 1 }, "", url);
      } else {
        window.history.pushState({ layout: 1 }, "", url);
      }
    } catch (err) {
      /* A blocked history call must not cost the user the change they just
         made; the rendered layout stands. */
    }
  }

  function layoutFromDom() {
    var params = currentParams();
    return resolveLayout(params);
  }

  /* ------------------------------------------------------------------ pins */

  function pinnedBudget() {
    var scroller = sheet.querySelector("[data-db-scroll]");
    var viewport = scroller ? (scroller.clientWidth || 0) : 0;
    /* Grid spec §1.3: the pinned region must not exceed 40 % of the table
       viewport. The server enforces a bounded absolute fallback because it does
       not know the viewport; here the real proportional rule applies. */
    if (!viewport) {
      return PIN_MAX_WIDTH;
    }
    return Math.min(PIN_MAX_WIDTH, Math.floor(viewport * PIN_FRACTION));
  }

  function refuse(anchor, message) {
    var note = document.createElement("p");
    note.className = "db-col-note db-col-refusal";
    note.setAttribute("role", "alert");
    note.appendChild(document.createTextNode(message));
    var host = anchor && anchor.parentNode ? anchor.parentNode : null;
    if (host) {
      host.appendChild(note);
      window.setTimeout(function () {
        if (note.parentNode) {
          note.parentNode.removeChild(note);
        }
      }, 6000);
    }
  }

  function handlePin(link) {
    var name = link.getAttribute("data-db-column");
    var wanted = link.getAttribute("data-db-pin") === "on";
    var layout = layoutFromDom();
    var pins = [];
    for (var i = 0; i < layout.pins.length; i += 1) {
      if (layout.pins[i] !== name) {
        pins.push(layout.pins[i]);
      }
    }
    if (wanted) {
      if (pins.length >= MAX_PINS) {
        refuse(link, strings.pin_refused || "");
        return;
      }
      var budget = pinnedBudget();
      var used = 0;
      for (var p = 0; p < pins.length; p += 1) {
        used += layout.effective[pins[p]] || 0;
      }
      if (pins.length && used + (layout.effective[name] || 0) > budget) {
        refuse(link, strings.pin_refused || "");
        return;
      }
      pins.push(name);
    }
    var params = currentParams();
    params.colpin = [pins.join(",")];
    commit(resolveLayout(params), false);
  }

  /* --------------------------------------------------------------- autofit */

  /* The same deterministic estimate the server applies, so the no-script link
     and this in-place path produce the same pixel value. It measures what is
     RENDERED on the current page — never a fresh scan of the dataset — so
     autofit issues no request at all. */
  function autofitWidth(name) {
    var longest = 0;
    var header = sheet.querySelector('th[data-db-column="' + name + '"]');
    if (header) {
      var labelNode = header.querySelector(".db-col-label");
      var labelText = String((labelNode || header).textContent || "").replace(/^\s+|\s+$/g, "");
      longest = labelText.length;
    }
    var cells = sheet.querySelectorAll('td[data-db-column="' + name + '"]');
    for (var i = 0; i < cells.length; i += 1) {
      var text = String(cells[i].textContent || "").replace(/^\s+|\s+$/g, "");
      if (text.length > longest) {
        longest = text.length;
      }
    }
    return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, PAD_PX + longest * CHAR_PX));
  }

  function setWidth(name, width, replace) {
    var params = currentParams();
    var layout = resolveLayout(params);
    layout.widths[name] = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, width));
    layout.effective[name] = layout.widths[name];
    commit(layout, !!replace);
  }

  /* ---------------------------------------------------------------- resize */

  var drag = null;
  var keyTimer = null;

  function guideElement() {
    var guide = document.createElement("div");
    guide.className = "db-resize-guide";
    document.body.appendChild(guide);
    return guide;
  }

  function initResize() {
    document.addEventListener("mousedown", function (event) {
      var handle = event.target && event.target.closest
        ? event.target.closest("[data-db-resize]")
        : null;
      if (!handle) {
        return;
      }
      event.preventDefault();
      drag = {
        name: handle.getAttribute("data-db-column"),
        startX: event.clientX,
        startWidth: renderedWidth(handle.getAttribute("data-db-column")),
        moved: false,
        guide: guideElement()
      };
      drag.guide.style.left = event.clientX + "px";
    });

    document.addEventListener("mousemove", function (event) {
      if (!drag) {
        return;
      }
      var next = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, drag.startWidth + (event.clientX - drag.startX)));
      if (next !== drag.startWidth) {
        drag.moved = true;
      }
      drag.width = next;
      if (drag.guide) {
        drag.guide.style.left = (drag.startX + (next - drag.startWidth)) + "px";
      }
      /* Live width, no history: the address bar is synchronised on release. */
      var cols = colElements();
      for (var i = 0; i < cols.length; i += 1) {
        if (cols[i].getAttribute("data-db-column") === drag.name) {
          cols[i].style.width = next + "px";
        }
      }
    });

    document.addEventListener("mouseup", function () {
      if (!drag) {
        return;
      }
      var finished = drag;
      drag = null;
      if (finished.guide && finished.guide.parentNode) {
        finished.guide.parentNode.removeChild(finished.guide);
      }
      if (finished.moved) {
        setWidth(finished.name, finished.width, false);
      }
      window.__dbResizeCommitted = !!finished.moved;
    });

    /* Click and double-click on the edge both autofit. The element is a link to
       the server-computed autofit URL, so this only replaces a round trip with
       the same result applied in place. */
    document.addEventListener("click", function (event) {
      var target = event.target && event.target.closest
        ? event.target.closest("[data-db-resize], [data-db-autofit-action]")
        : null;
      if (!target) {
        return;
      }
      event.preventDefault();
      var name = target.getAttribute("data-db-column");
      setWidth(name, autofitWidth(name), false);
    });

    /* The approved accessible equivalent of pointer resize is the column menu's
       explicit width field; this is an additional convenience on the focused
       handle. A burst of key repeats collapses into one history entry. */
    document.addEventListener("keydown", function (event) {
      var handle = event.target && event.target.closest
        ? event.target.closest("[data-db-resize]")
        : null;
      if (!handle) {
        return;
      }
      var step = 0;
      if (event.key === "ArrowRight") {
        step = 16;
      } else if (event.key === "ArrowLeft") {
        step = -16;
      } else {
        return;
      }
      event.preventDefault();
      var name = handle.getAttribute("data-db-column");
      var next = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, renderedWidth(name) + step));
      var cols = colElements();
      for (var i = 0; i < cols.length; i += 1) {
        if (cols[i].getAttribute("data-db-column") === name) {
          cols[i].style.width = next + "px";
        }
      }
      if (keyTimer) {
        window.clearTimeout(keyTimer);
      }
      keyTimer = window.setTimeout(function () {
        keyTimer = null;
        setWidth(name, next, false);
      }, 400);
    });
  }

  /* ----------------------------------------------------------- DB-008 panel */

  function panelRows(list) {
    var rows = [];
    for (var i = 0; i < list.childNodes.length; i += 1) {
      var node = list.childNodes[i];
      if (node.nodeType === 1 && node.getAttribute && node.getAttribute("data-db-column")) {
        rows.push(node);
      }
    }
    return rows;
  }

  function panelOrder(list) {
    var rows = panelRows(list);
    var order = [];
    for (var i = 0; i < rows.length; i += 1) {
      if (rows[i].getAttribute("data-db-state") === "visible") {
        order.push(rows[i].getAttribute("data-db-column"));
      }
    }
    return order;
  }

  function syncOrderField(form, list) {
    var field = form.querySelector("[data-db-order-field]");
    if (field) {
      var order = panelOrder(list);
      field.setAttribute("value", order.join(","));
      field.value = order.join(",");
    }
  }

  function checkedValues(form, name) {
    var inputs = form.querySelectorAll('input[name="' + name + '"]');
    var values = [];
    for (var i = 0; i < inputs.length; i += 1) {
      if (inputs[i].getAttribute("type") === "checkbox" && inputs[i].checked) {
        values.push(inputs[i].getAttribute("value"));
      }
    }
    return values;
  }

  function movePanelRow(list, form, name, direction) {
    var rows = panelRows(list);
    var visible = [];
    for (var i = 0; i < rows.length; i += 1) {
      if (rows[i].getAttribute("data-db-state") === "visible") {
        visible.push(rows[i]);
      }
    }
    var index = -1;
    for (var v = 0; v < visible.length; v += 1) {
      if (visible[v].getAttribute("data-db-column") === name) {
        index = v;
      }
    }
    var target = index + direction;
    if (index === -1 || target < 0 || target >= visible.length) {
      return null;
    }
    var node = visible[index];
    if (direction < 0) {
      list.insertBefore(node, visible[target]);
    } else if (visible[target].nextSibling) {
      list.insertBefore(node, visible[target].nextSibling);
    } else {
      list.appendChild(node);
    }
    syncOrderField(form, list);
    announce(name, target + 1, visible.length);
    return target;
  }

  function announce(name, position, total) {
    var region = sheet.querySelector("[data-db-cols-status]");
    if (!region) {
      return;
    }
    var template = strings.moved || "";
    region.textContent = template
      .replace("{column}", name)
      .replace("{position}", String(position))
      .replace("{total}", String(total));
  }

  function initPanel() {
    var panel = sheet.querySelector("[data-db-columns]");
    if (!panel) {
      return;
    }
    var form = panel.querySelector("[data-db-columns-form]");
    var list = panel.querySelector("[data-db-cols-list]");
    if (!form || !list) {
      return;
    }

    /* The search field and the tab filters do nothing without script, so the
       server ships them hidden and this is what makes them exist. */
    var search = panel.querySelector("[data-db-cols-search]");
    if (search) {
      search.removeAttribute("hidden");
    }
    var tabs = panel.querySelector("[data-db-cols-tabs]");
    if (tabs) {
      tabs.removeAttribute("hidden");
    }
    var handles = panel.querySelectorAll("[data-db-handle]");
    for (var h = 0; h < handles.length; h += 1) {
      handles[h].removeAttribute("hidden");
    }

    panel.addEventListener("click", function (event) {
      var target = event.target && event.target.closest ? event.target.closest("[data-db-move]") : null;
      if (target) {
        event.preventDefault();
        movePanelRow(
          list, form,
          target.getAttribute("data-db-column"),
          target.getAttribute("data-db-move") === "up" ? -1 : 1
        );
        return;
      }
      var tab = event.target && event.target.closest ? event.target.closest("[data-db-cols-tab]") : null;
      if (tab) {
        applyPanelFilter(panel, list, tab.getAttribute("data-db-cols-tab"), null);
      }
    });

    panel.addEventListener("keydown", function (event) {
      var handle = event.target && event.target.closest
        ? event.target.closest("[data-db-handle]")
        : null;
      if (!handle) {
        return;
      }
      var direction = 0;
      if (event.key === "ArrowUp") {
        direction = -1;
      } else if (event.key === "ArrowDown") {
        direction = 1;
      } else {
        return;
      }
      event.preventDefault();
      var row = handle.parentNode;
      var name = row.getAttribute("data-db-column");
      movePanelRow(list, form, name, direction);
      /* Focus follows the moved column, so a keyboard reorder can continue
         without hunting for the handle again. */
      handle.focus();
    });

    panel.addEventListener("input", function (event) {
      if (!event.target || !event.target.hasAttribute || !event.target.hasAttribute("data-db-cols-query")) {
        return;
      }
      applyPanelFilter(panel, list, null, String(event.target.value || "").toLowerCase());
    });

    /* Drag reorder. Staged like every other panel edit: nothing reaches the
       grid until Zastosuj (INT §4). */
    list.addEventListener("dragstart", function (event) {
      var row = event.target && event.target.closest ? event.target.closest("[data-db-col-row]") : null;
      if (!row) {
        return;
      }
      row.setAttribute("data-db-dragging", "true");
      if (event.dataTransfer && event.dataTransfer.setData) {
        event.dataTransfer.setData("text/plain", row.getAttribute("data-db-column"));
      }
    });

    list.addEventListener("dragover", function (event) {
      var row = event.target && event.target.closest ? event.target.closest("[data-db-col-row]") : null;
      if (!row) {
        return;
      }
      if (event.preventDefault) {
        event.preventDefault();
      }
      row.setAttribute("data-db-drop", "before");
    });

    list.addEventListener("drop", function (event) {
      if (event.preventDefault) {
        event.preventDefault();
      }
      var target = event.target && event.target.closest ? event.target.closest("[data-db-col-row]") : null;
      var source = list.querySelector('[data-db-dragging="true"]');
      if (!target || !source || target === source) {
        return;
      }
      list.insertBefore(source, target);
      source.removeAttribute("data-db-dragging");
      target.removeAttribute("data-db-drop");
      syncOrderField(form, list);
    });

    list.addEventListener("dragend", function () {
      var rows = panelRows(list);
      for (var i = 0; i < rows.length; i += 1) {
        rows[i].removeAttribute("data-db-dragging");
        rows[i].removeAttribute("data-db-drop");
      }
    });

    /* Dismissal discards (INT §3): the panel holds staged work, so closing it
       without Zastosuj must restore the order it was opened with rather than
       leave a half-made arrangement behind. */
    var opened = panelOrder(list).join(",");
    var trigger = panel.querySelector("summary");

    function restoreStagedOrder() {
      var wanted = tokens(opened);
      var rows = {};
      panelRows(list).forEach(function (row) { rows[row.getAttribute("data-db-column")] = row; });
      for (var i = 0; i < wanted.length; i += 1) {
        if (rows[wanted[i]]) {
          list.appendChild(rows[wanted[i]]);
        }
      }
      syncOrderField(form, list);
    }

    function dismiss() {
      if (!panel.hasAttribute("open")) {
        return false;
      }
      panel.removeAttribute("open");
      restoreStagedOrder();
      if (trigger && trigger.focus) {
        trigger.focus();
      }
      return true;
    }

    panel.addEventListener("toggle", function () {
      if (panel.hasAttribute("open")) {
        opened = panelOrder(list).join(",");
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" && event.keyCode !== 27) {
        return;
      }
      /* `Esc` closes the TOPMOST transient layer only, so a column menu open
         over the sheet is dismissed by its own handler first, and a shell layer
         above this one marks the event handled before it reaches here. */
      if (event.defaultPrevented) {
        return;
      }
      if (document.querySelector("[data-db-col-menu][open]")) {
        return;
      }
      if (dismiss() && event.preventDefault) {
        event.preventDefault();
      }
    });

    document.addEventListener("click", function (event) {
      if (!panel.hasAttribute("open")) {
        return;
      }
      var inside = event.target && event.target.closest
        ? event.target.closest("[data-db-columns]")
        : null;
      if (!inside) {
        dismiss();
      }
    });

    /* Visibility must round-trip through the server, because revealing a column
       changes which columns the row SELECT asks for. Order and pin state must
       NOT: the rows on screen already belong to the view, so they apply in
       place and only the URL moves. */
    form.addEventListener("submit", function (event) {
      var wanted = checkedValues(form, "cols");
      if (!wanted.length) {
        /* DB-28: at least one column stays visible, refused inline rather than
           silently restoring the whole approved set. */
        if (event.preventDefault) {
          event.preventDefault();
        }
        showPanelWarning(panel, strings.min_one || "");
        return;
      }
      var current = layoutFromDom().display.slice(0).sort();
      var next = wanted.slice(0).sort();
      if (!sameList(current, next)) {
        return;
      }
      if (event.preventDefault) {
        event.preventDefault();
      }
      var params = currentParams();
      params.colorder = [panelOrder(list).join(",")];
      params.colpin = [checkedValues(form, "colpin").join(",")];
      commit(resolveLayout(params), false);
      panel.removeAttribute("open");
    });
  }

  function showPanelWarning(panel, message) {
    var existing = panel.querySelector(".db-cols-warning");
    if (existing) {
      existing.textContent = message;
      return;
    }
    var note = document.createElement("p");
    note.className = "db-cols-warning";
    note.setAttribute("role", "alert");
    note.appendChild(document.createTextNode(message));
    var list = panel.querySelector("[data-db-cols-list]");
    if (list && list.parentNode) {
      list.parentNode.appendChild(note);
    }
  }

  var panelTab = "all";
  var panelQuery = "";

  function applyPanelFilter(panel, list, tab, query) {
    if (tab !== null && tab !== undefined) {
      panelTab = tab;
      var buttons = panel.querySelectorAll("[data-db-cols-tab]");
      for (var b = 0; b < buttons.length; b += 1) {
        buttons[b].setAttribute(
          "aria-pressed",
          buttons[b].getAttribute("data-db-cols-tab") === panelTab ? "true" : "false"
        );
      }
    }
    if (query !== null && query !== undefined) {
      panelQuery = query;
    }
    var rows = panelRows(list);
    for (var i = 0; i < rows.length; i += 1) {
      var state = rows[i].getAttribute("data-db-state");
      var label = rows[i].getAttribute("data-db-label") || "";
      var matches = (panelTab === "all" || panelTab === state)
        && (!panelQuery || label.indexOf(panelQuery) !== -1);
      if (matches) {
        rows[i].removeAttribute("hidden");
      } else {
        rows[i].setAttribute("hidden", "");
      }
    }
  }

  /* ------------------------------------------------------------ back/forward */

  function colsSignature(params) {
    var values = (params.cols || []).slice(0).sort();
    return values.join("|");
  }

  function initHistory() {
    window.addEventListener("popstate", function () {
      var params = currentParams();
      if (colsSignature(params) !== initialCols) {
        /* Visibility changed, so the rendered rows no longer carry the right
           columns. That is the one layout transition that legitimately needs
           the server, and the popped URL is already current. */
        if (window.location && typeof window.location.reload === "function") {
          window.location.reload();
        }
        return;
      }
      applyLayout(resolveLayout(params));
    });
  }

  /* -------------------------------------------------------------------- init */

  function init() {
    sheet = document.querySelector("[data-db-sheet]");
    if (!sheet || !sheet.querySelector(".db-table")) {
      return;
    }
    try {
      strings = JSON.parse(sheet.getAttribute("data-db-strings") || "{}");
    } catch (err) {
      strings = {};
    }
    CATALOG = tokens(sheet.getAttribute("data-col-catalog") || "");
    MIN_WIDTH = parseInt(sheet.getAttribute("data-col-min-width"), 10) || MIN_WIDTH;
    MAX_WIDTH = parseInt(sheet.getAttribute("data-col-max-width"), 10) || MAX_WIDTH;
    MAX_PINS = parseInt(sheet.getAttribute("data-col-max-pins"), 10) || MAX_PINS;
    PIN_FRACTION = parseFloat(sheet.getAttribute("data-col-pin-fraction")) || PIN_FRACTION;
    PIN_MAX_WIDTH = parseInt(sheet.getAttribute("data-col-pin-max-width"), 10) || PIN_MAX_WIDTH;
    CHAR_PX = parseInt(sheet.getAttribute("data-col-autofit-char"), 10) || CHAR_PX;
    PAD_PX = parseInt(sheet.getAttribute("data-col-autofit-padding"), 10) || PAD_PX;
    initialCols = colsSignature(currentParams());

    document.addEventListener("click", function (event) {
      var pin = event.target && event.target.closest ? event.target.closest("[data-db-pin]") : null;
      if (!pin) {
        return;
      }
      event.preventDefault();
      handlePin(pin);
    });

    initResize();
    initPanel();
    initHistory();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
