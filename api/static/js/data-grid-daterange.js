/* Log Platform — the date filter's two fields and its on-demand calendar
 * (UI-20260831-01, reshaped by UI-20260831-02).
 *
 * WHAT THE USER SEES. One field per endpoint — `Od` and `Do` in range mode, a
 * single field in `przed`/`po` — written the way a date is written here:
 * `dd.mm.rrrr`, optionally `dd.mm.rrrr gg:mm`. Clicking or tabbing into a field
 * expands a calendar beneath the group; two clicks pick a range, and typing a
 * date marks it on the calendar. Nothing else is on screen.
 *
 * WHAT THE SERVER SEES IS UNCHANGED. The controls the server renders are still
 * the ones that submit: this module turns each into a hidden carrier holding the
 * exact wire value (`YYYY-MM-DDTHH:MM`, or `YYYY-MM-DD` on a date-only column)
 * under its original `name`, and puts a text field in front of it. No parameter
 * is added, renamed or reformatted, and every added control is `name`-less and
 * `type="button"`, so the request is byte-identical with or without this script.
 * With scripting off the native pickers are simply still there and still work.
 *
 * THREE RULES HOLD THE THING TOGETHER. Breaking any one of them destroyed user
 * input in an earlier round, so each is stated where it is enforced:
 *
 *   1. THE CARRIERS ARE THE ONLY STATE. The fields and the calendar are views
 *      over them, re-derived after every change. There is no second copy of the
 *      range's progress — a start with no end IS the half-open state.
 *   2. A PROGRAMMATIC WRITE IS NEVER READ BACK. A field commits only what the
 *      user actually typed, so the `change`/`blur` pair a browser fires on
 *      tab-out cannot round-trip a value the script itself had just rendered.
 *   3. ACTIVATION AND DISMISSAL ARE OURS. Enter and Space are handled here and
 *      `preventDefault`ed rather than left to a browser default action, and the
 *      calendar collapses only after a click has landed — never mid-gesture,
 *      which would move the target out from under the pointer.
 */
(function () {
  "use strict";

  var DAY_MS = 86400000;

  function all(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function strings(container) {
    try {
      return JSON.parse(container.getAttribute("data-db-strings") || "{}") || {};
    } catch (error) {
      return {};
    }
  }

  function format(template, values) {
    return String(template || "").replace(/\{(\w+)\}/g, function (whole, key) {
      return Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : whole;
    });
  }

  /* Focus that cannot dismiss the panel it is moving within.
   *
   * data-grid-filters.js owns the rule, because it owns the scroll-dismissal it
   * has to avoid; this module focuses calendar days and rejected fields inside
   * the very same panel. The fallback covers the module being loaded on its own:
   * `preventScroll` still prevents the dismissal, only the reveal is missing. */
  function focusVisibly(node) {
    if (!node || !node.focus) {
      return;
    }
    if (typeof window.dbGridFocusVisibly === "function") {
      window.dbGridFocusVisibly(node);
      return;
    }
    try {
      node.focus({ preventScroll: true });
    } catch (error) {
      node.focus();
    }
  }

  function setHidden(node, hidden) {
    if (hidden) { node.setAttribute("hidden", ""); } else { node.removeAttribute("hidden"); }
  }

  function contains(root, node) {
    var cursor = node;
    while (cursor) {
      if (cursor === root) { return true; }
      cursor = cursor.parentNode;
    }
    return false;
  }

  /* ------------------------------------------------------------- date model */

  function pad(number, width) {
    var text = String(number);
    while (text.length < width) { text = "0" + text; }
    return text;
  }

  /* A calendar day, held as UTC midnight so arithmetic never meets a DST edge.
     Nothing here is a timestamp: it is a date on a wall calendar. */
  function dayFromParts(year, month, day) {
    var stamp = Date.UTC(year, month - 1, day);
    var date = new Date(stamp);
    /* Rejects 2026-02-30 and friends, which Date would otherwise roll over. */
    if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) {
      return null;
    }
    return stamp;
  }

  function dayToISO(stamp) {
    var date = new Date(stamp);
    return date.getUTCFullYear() + "-" + pad(date.getUTCMonth() + 1, 2) + "-" + pad(date.getUTCDate(), 2);
  }

  function dayFromISO(text) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text || ""));
    if (!match) { return null; }
    return dayFromParts(Number(match[1]), Number(match[2]), Number(match[3]));
  }

  /* ACCEPTED WRITTEN FORMS. Deliberately a small, unambiguous set:
   *
   *     YYYY-MM-DD            2026-08-31
   *     YYYY.MM.DD  YYYY/MM/DD
   *     DD.MM.YYYY            31.08.2026     (the Polish written convention)
   *     DD-MM-YYYY  DD/MM/YYYY
   *
   * each optionally followed by a time, `HH:MM` or `HH:MM:SS`, separated by a
   * space or by `T`. Anything else is rejected rather than guessed at: a
   * two-digit leading field is read as a day, never as a year, so `03.04.2026`
   * has exactly one meaning. Ambiguous month/day orders (`03/04/26`) are not
   * accepted at all.
   */
  function parseTyped(text) {
    var raw = String(text === undefined || text === null ? "" : text).trim();
    if (!raw) { return { empty: true }; }
    /* The date part is digits and separators only, so an ISO `T` is read as the
       time separator it is instead of being swallowed by a greedy match. */
    var split = /^([\d./-]+)(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$/.exec(raw);
    if (!split) { return { error: true }; }
    var datePart = split[1];
    var hour = split[2] === undefined ? null : Number(split[2]);
    var minute = split[3] === undefined ? null : Number(split[3]);
    if (hour !== null && (hour > 23 || minute > 59)) { return { error: true }; }

    /* Bare digits, the shape the input mask produces and the shape a paste or a
       fast typist can leave behind if the mask never ran. `ddmmyyyy`, optionally
       followed by `HHMM`. Accepted here as well as in the mask so the two cannot
       disagree about what a run of digits means. */
    var bare = /^(\d{2})(\d{2})(\d{4})(?:(\d{2})(\d{2}))?$/.exec(datePart);
    if (bare && hour === null) {
      var bareStamp = dayFromParts(Number(bare[3]), Number(bare[2]), Number(bare[1]));
      if (bareStamp === null) { return { error: true }; }
      var bareHour = bare[4] === undefined ? null : Number(bare[4]);
      var bareMinute = bare[5] === undefined ? null : Number(bare[5]);
      if (bareHour !== null && (bareHour > 23 || bareMinute > 59)) { return { error: true }; }
      return { day: bareStamp, hour: bareHour, minute: bareMinute };
    }

    var fields = datePart.split(/[-./]/);
    if (fields.length !== 3 || !fields.every(function (part) { return /^\d+$/.test(part); })) {
      return { error: true };
    }
    var year;
    var month;
    var day;
    if (fields[0].length === 4) {
      year = Number(fields[0]); month = Number(fields[1]); day = Number(fields[2]);
    } else if (fields[2].length === 4) {
      day = Number(fields[0]); month = Number(fields[1]); year = Number(fields[2]);
    } else {
      return { error: true };
    }
    var stamp = dayFromParts(year, month, day);
    if (stamp === null) { return { error: true }; }
    return { day: stamp, hour: hour, minute: minute };
  }

  /* --------------------------------------------------------------- the mask */

  /* Separators insert themselves as the digits arrive, so `01082026` becomes
   * `01.08.2026` without the user reaching for the dot. The index is the digit
   * the separator precedes: `dd.mm.rrrr gg:mm`.
   *
   * LAZY, never trailing. A separator appears only once the digit after it
   * exists, so the field never shows `01.` waiting to be filled — and, more
   * importantly, a Backspace can never be undone by the mask re-adding the very
   * separator that was just deleted.
   */
  var MASK_SEPARATORS = { 2: ".", 4: ".", 8: " ", 10: ":" };

  function digitsOnly(text) {
    return String(text === undefined || text === null ? "" : text).replace(/\D/g, "");
  }

  function maskDigits(digits, withTime) {
    var limit = withTime ? 12 : 8;
    var capped = digits.slice(0, limit);
    var out = "";
    for (var index = 0; index < capped.length; index += 1) {
      if (MASK_SEPARATORS[index]) { out += MASK_SEPARATORS[index]; }
      out += capped.charAt(index);
    }
    return out;
  }

  function digitsBefore(text, caret) {
    return digitsOnly(String(text).slice(0, caret)).length;
  }

  function caretAfterDigits(masked, count) {
    if (count <= 0) { return 0; }
    var seen = 0;
    for (var index = 0; index < masked.length; index += 1) {
      if (/\d/.test(masked.charAt(index))) {
        seen += 1;
        if (seen === count) { return index + 1; }
      }
    }
    return masked.length;
  }

  /* ------------------------------------------------------ one control group */

  /* A group is one column's whole date filter: the operator select, the single
     control, the range pair, and the calendar they share. */
  function enhance(group) {
    if (group.getAttribute("data-db-date-ready") === "1") { return; }
    group.setAttribute("data-db-date-ready", "1");

    var S = strings(group);
    var isDateTime = group.getAttribute("data-db-date-type") === "datetime-local";
    var columnLabel = group.getAttribute("data-db-date-column") || "";
    var months = String(S.months || "").split(",");
    var weekdays = String(S.weekdays || "").split(",");

    var status = el("p", "db-date-status");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");

    /* ------------------------------------------------------- wire <-> view */

    /* The end of a range means the end OF that day, which is the convention the
       server-side date presets already encode. */
    function defaultTimeFor(role) {
      return role === "to" ? "23:59" : "00:00";
    }

    function toWire(stamp, role, time) {
      var iso = dayToISO(stamp);
      if (!isDateTime) { return iso; }
      return iso + "T" + (time || defaultTimeFor(role));
    }

    function wireDay(value) {
      return dayFromISO(String(value || "").slice(0, 10));
    }

    function wireTime(value) {
      var match = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/.exec(String(value || ""));
      return match ? match[1] : null;
    }

    /* `dd.mm.rrrr`, plus `gg:mm` on a column that carries a time. The time is
       always shown there rather than only when it is unusual: a filter boundary
       that silently means midnight, or one minute to midnight, is exactly the
       thing a reader needs to see. */
    function display(value) {
      var stamp = wireDay(value);
      if (stamp === null) { return ""; }
      var date = new Date(stamp);
      var text = pad(date.getUTCDate(), 2) + "." + pad(date.getUTCMonth() + 1, 2) + "." + date.getUTCFullYear();
      if (!isDateTime) { return text; }
      return text + " " + (wireTime(value) || "00:00");
    }

    /* ---------------------------------------------------------- the fields */

    var fields = {};

    function makeField(role, carrier) {
      /* The control the server rendered keeps its name and its value and stops
         being visible: it is now purely the wire carrier. Hiding it this way
         also takes it out of the tab order, so the group has exactly one stop
         per endpoint. */
      var wire = carrier.value;
      carrier.type = "hidden";
      carrier.value = wire;

      var text = el("input", "db-date-field");
      text.type = "text";
      /* Addressable by role, so the field and the carrier behind it can be
         matched up from the outside — by a test, or by anyone reading the DOM. */
      text.setAttribute("data-db-date-field", role);
      var id = (carrier.getAttribute("id") || ("db-date-" + role)) + "-text";
      text.id = id;
      text.setAttribute("autocomplete", "off");
      text.setAttribute("inputmode", "numeric");
      text.setAttribute("placeholder", S.formatHint || "");
      text.setAttribute("aria-expanded", "false");
      if (role === "single") {
        text.setAttribute("aria-label", format(S.singleLabel || "", { column: columnLabel }));
      }
      /* The visible `Od` / `Do` the server rendered stays the field's label. */
      all('label[for="' + (carrier.getAttribute("id") || "") + '"]', group).forEach(function (label) {
        label.setAttribute("for", id);
      });
      if (carrier.parentNode) {
        carrier.parentNode.insertBefore(text, carrier.nextSibling);
      }

      var userTyped = false;

      function show(value) {
        text.value = value;
        userTyped = false;
      }

      function reject() {
        text.classList.add("invalid");
        text.setAttribute("aria-invalid", "true");
        status.textContent = S.invalid || "";
        status.classList.add("invalid");
      }

      function accept() {
        text.classList.remove("invalid");
        text.removeAttribute("aria-invalid");
        if (!group.querySelector(".db-date-field.invalid")) {
          status.classList.remove("invalid");
          status.textContent = describe();
        }
      }

      function commit() {
        if (!userTyped) {
          return !text.classList.contains("invalid");
        }
        userTyped = false;
        var parsed = parseTyped(text.value);
        if (parsed.empty) {
          carrier.value = "";
          accept();
          render();
          return true;
        }
        if (parsed.error) {
          reject();
          return false;
        }
        var time = null;
        if (isDateTime && parsed.hour !== null) {
          time = pad(parsed.hour, 2) + ":" + pad(parsed.minute, 2);
        }
        carrier.value = toWire(parsed.day, role, time);
        accept();
        render();
        return true;
      }

      /* The mask only takes over input that IS the mask's own shape: bare digits,
         or exactly what the mask would have produced. Anything else — a `-`, a
         `/`, an ISO string pasted in whole — is left untouched for the commit
         parser, which still accepts every form -01 documented. That is also what
         lets a user type their own dots: `01.` is not something the lazy mask
         would emit, so it survives until the next digit makes it canonical. */
      function inMaskShape(raw) {
        /* Every non-digit must be exactly the separator the mask would place at
           that digit boundary. Testing instead that the value already EQUALS the
           mask output is too strict: half way through typing, the value is the
           canonical form plus one digit whose separator is not due yet, and the
           mask would refuse to touch it from the third digit onwards. */
        var seen = 0;
        for (var index = 0; index < raw.length; index += 1) {
          var ch = raw.charAt(index);
          if (ch >= "0" && ch <= "9") { seen += 1; continue; }
          if (MASK_SEPARATORS[seen] !== ch) { return false; }
        }
        return true;
      }

      function applyMask() {
        var raw = text.value;
        if (!inMaskShape(raw)) { return; }
        var caret = typeof text.selectionStart === "number" ? text.selectionStart : null;
        var before = caret === null ? null : digitsBefore(raw, caret);
        var digits = digitsOnly(raw);
        var out = maskDigits(digits, isDateTime);
        if (out === raw) { return; }
        /* A separator the user typed themselves, exactly where one is due, is
           left standing rather than swallowed and re-added on the next digit. */
        if (raw === out + MASK_SEPARATORS[digits.length]) { return; }
        text.value = out;
        if (before !== null && text.setSelectionRange) {
          var at = caretAfterDigits(out, before);
          try { text.setSelectionRange(at, at); } catch (error) { /* detached field */ }
        }
      }

      text.addEventListener("input", function () { userTyped = true; applyMask(); });

      /* Backspace and Delete work on DIGITS, stepping over the separators the
         mask inserted — one press removes one digit wherever the caret sits, so
         deleting never stalls on a separator the mask would immediately re-add.
         A selection, or a value in some other format, is left to the browser. */
      text.addEventListener("keydown", function (event) {
        if (event.key !== "Backspace" && event.key !== "Delete") { return; }
        if (!inMaskShape(text.value)) { return; }
        var start = text.selectionStart;
        var end = text.selectionEnd;
        if (typeof start !== "number" || start !== end) { return; }
        var digits = digitsOnly(text.value);
        var index = digitsBefore(text.value, start);
        if (event.key === "Backspace") {
          if (index === 0) { return; }
          digits = digits.slice(0, index - 1) + digits.slice(index);
          index -= 1;
        } else {
          if (index >= digits.length) { return; }
          digits = digits.slice(0, index) + digits.slice(index + 1);
        }
        event.preventDefault();
        userTyped = true;
        var out = maskDigits(digits, isDateTime);
        text.value = out;
        if (text.setSelectionRange) {
          var at = caretAfterDigits(out, index);
          try { text.setSelectionRange(at, at); } catch (error) { /* detached field */ }
        }
      });
      text.addEventListener("change", commit);
      text.addEventListener("blur", commit);
      function revealOnFocus() {
        /* The browser has already scrolled to reveal this field, and that is
           left standing: a reveal is not a gesture aimed at the grid, so it no
           longer dismisses, and undoing it would only put the field back out of
           sight. The panel scrollbar handles what the panel itself is hiding.
           Revealed WITHOUT re-focusing — this element already has focus, and
           focusing it again would re-enter this handler. */
        expand();
        if (typeof window.dbGridRevealWithinPanel === "function") {
          window.dbGridRevealWithinPanel(text);
        }
      }

      text.addEventListener("focus", revealOnFocus);
      text.addEventListener("click", revealOnFocus);
      text.addEventListener("keydown", function (event) {
        if (event.key !== "Enter") { return; }
        /* Enter is handled here and nowhere else: leaning on implicit form
           submission made the outcome depend on a default action. */
        event.preventDefault();
        if (!commit()) {
          focusVisibly(text);
          return;
        }
        applyOwningForm();
      });

      return {
        role: role,
        carrier: carrier,
        node: text,
        commit: commit,
        show: show,
        isInvalid: function () { return text.classList.contains("invalid"); },
      };
    }

    ["single", "from", "to"].forEach(function (role) {
      var carrier = group.querySelector('[data-db-date-input="' + role + '"]');
      if (carrier) { fields[role] = makeField(role, carrier); }
    });
    if (!fields.from && !fields.to && !fields.single) { return; }

    function owningForm() {
      var node = group;
      while (node && node.nodeType === 1) {
        if (node.tagName && String(node.tagName).toLowerCase() === "form") { return node; }
        node = node.parentNode;
      }
      return null;
    }

    /* Submit the way a click on Apply would, so the form's own validation and
       the shared single-submit guard both still run. `form.submit()` is
       deliberately not used: it bypasses the submit event and with it both. */
    function applyOwningForm() {
      var form = owningForm();
      if (!form) { return; }
      var button = form.querySelector('button[type="submit"]');
      if (form.requestSubmit) {
        form.requestSubmit(button || undefined);
      } else if (button && button.click) {
        button.click();
      }
    }

    /* ------------------------------------------------------- which mode ----*/

    /* The operator select decides which control is on screen; the calendar
       follows it rather than keeping a mode of its own. */
    function rangeMode() {
      var box = group.querySelector("[data-db-value-range]");
      return !!(box && !box.hasAttribute("hidden") && fields.from && fields.to);
    }

    function activeFields() {
      return rangeMode() ? [fields.from, fields.to] : (fields.single ? [fields.single] : []);
    }

    /* ---------------------------------------------------------- the calendar */

    var calendar = el("div", "db-date-calendar");
    calendar.setAttribute("role", "group");
    calendar.setAttribute("aria-label", S.calendarLabel || "");
    setHidden(calendar, true);

    var head = el("div", "db-date-cal-head");
    var prev = el("button", "db-date-nav", "‹");
    prev.type = "button";
    prev.setAttribute("aria-label", S.prevMonth || "");
    var title = el("span", "db-date-cal-title");
    var next = el("button", "db-date-nav", "›");
    next.type = "button";
    next.setAttribute("aria-label", S.nextMonth || "");
    head.appendChild(prev);
    head.appendChild(title);
    head.appendChild(next);
    calendar.appendChild(head);

    var weekRow = el("div", "db-date-weekdays");
    weekdays.forEach(function (name) {
      var cell = el("span", null, name);
      cell.setAttribute("aria-hidden", "true");
      weekRow.appendChild(cell);
    });
    calendar.appendChild(weekRow);

    var grid = el("div", "db-date-grid");
    calendar.appendChild(grid);

    var clear = el("button", "db-date-clear", S.clearRange || "");
    clear.type = "button";
    calendar.appendChild(clear);

    group.appendChild(calendar);
    group.appendChild(status);

    var viewYear;
    var viewMonth;
    var focusDay = null;
    var expanded = false;

    function expand() {
      if (expanded) { return; }
      expanded = true;
      setHidden(calendar, false);
      activeFields().forEach(function (field) {
        field.node.setAttribute("aria-expanded", "true");
      });
      render();
    }

    function collapse() {
      if (!expanded) { return false; }
      expanded = false;
      setHidden(calendar, true);
      ["single", "from", "to"].forEach(function (role) {
        if (fields[role]) { fields[role].node.setAttribute("aria-expanded", "false"); }
      });
      return true;
    }

    /* --------------------------------------------------------- range state */

    function endpoints() {
      if (rangeMode()) {
        return { start: wireDay(fields.from.carrier.value), end: wireDay(fields.to.carrier.value) };
      }
      return { start: fields.single ? wireDay(fields.single.carrier.value) : null, end: null };
    }

    function describe() {
      var range = endpoints();
      if (!rangeMode()) {
        return range.start === null ? (S.pickStart || "") : display(fields.single.carrier.value);
      }
      if (range.start !== null && range.end !== null) {
        return format(S.rangeSelected || "", {
          from: display(fields.from.carrier.value),
          to: display(fields.to.carrier.value),
        });
      }
      if (range.start !== null) { return S.pickEnd || ""; }
      return S.pickStart || "";
    }

    function setRange(startStamp, endStamp) {
      /* SWAP, not restart: two clicks always produce a usable range, and the
         stored value is always start <= end, so the request can never carry an
         inverted range the server would read as empty. */
      if (startStamp !== null && endStamp !== null && endStamp < startStamp) {
        var swap = startStamp;
        startStamp = endStamp;
        endStamp = swap;
      }
      fields.from.carrier.value = startStamp === null
        ? "" : toWire(startStamp, "from", wireTime(fields.from.carrier.value));
      fields.to.carrier.value = endStamp === null
        ? "" : toWire(endStamp, "to", wireTime(fields.to.carrier.value));
      render();
    }

    function pick(stamp) {
      if (!rangeMode()) {
        fields.single.carrier.value = toWire(stamp, "single", wireTime(fields.single.carrier.value));
        render();
        return;
      }
      var range = endpoints();
      /* No start yet, or an already-complete range: this click begins a new one.
         Otherwise it closes the open one — so a start the user TYPED can be
         completed with a click. */
      if (range.start === null || range.end !== null) {
        fields.from.carrier.value = toWire(stamp, "from", null);
        fields.to.carrier.value = "";
        render();
        return;
      }
      setRange(range.start, stamp);
    }

    /* Everything visible is redrawn from the carriers — never from a parallel
       copy, which is how a field and a calendar start disagreeing. */
    function render() {
      var range = endpoints();

      ["single", "from", "to"].forEach(function (role) {
        var field = fields[role];
        if (!field || field.isInvalid()) { return; }
        field.show(display(field.carrier.value));
      });

      var anchor = range.start !== null ? range.start : (range.end !== null ? range.end : Date.now());
      if (viewYear === undefined) {
        var view = new Date(anchor);
        viewYear = view.getUTCFullYear();
        viewMonth = view.getUTCMonth() + 1;
      }
      if (focusDay === null) { focusDay = range.start !== null ? range.start : dayFromISO(dayToISO(anchor)); }

      if (expanded) { renderGrid(range.start, range.end); }
      if (!group.querySelector(".db-date-field.invalid")) {
        status.classList.remove("invalid");
        status.textContent = describe();
      }
    }

    function renderGrid(start, end) {
      title.textContent = (months[viewMonth - 1] || viewMonth) + " " + viewYear;
      while (grid.firstChild) { grid.removeChild(grid.firstChild); }

      var first = dayFromParts(viewYear, viewMonth, 1);
      /* Monday-first, matching the weekday header. */
      var lead = (new Date(first).getUTCDay() + 6) % 7;
      var daysInMonth = new Date(Date.UTC(viewYear, viewMonth, 0)).getUTCDate();
      var index;
      for (index = 0; index < lead; index += 1) {
        grid.appendChild(el("span", "db-date-blank"));
      }
      var focusInMonth = false;
      for (index = 1; index <= daysInMonth; index += 1) {
        var stamp = dayFromParts(viewYear, viewMonth, index);
        var cell = el("button", "db-date-day", index);
        cell.type = "button";
        cell.setAttribute("data-db-day", dayToISO(stamp));
        var label = dayToISO(stamp);
        if (start !== null && stamp === start) {
          cell.classList.add("range-start");
          label += " — " + (S.rangeStart || "");
        }
        if (end !== null && stamp === end) {
          cell.classList.add("range-end");
          label += " — " + (S.rangeEnd || "");
        }
        if (start !== null && end !== null && stamp > start && stamp < end) {
          cell.classList.add("in-range");
          label += " — " + (S.inRange || "");
        }
        if ((start !== null && stamp === start) || (end !== null && stamp === end)) {
          cell.setAttribute("aria-pressed", "true");
        }
        cell.setAttribute("aria-label", label);
        /* Roving tabindex: one stop for the whole grid, arrows move within it. */
        var isFocus = focusDay !== null && stamp === focusDay;
        if (isFocus) { focusInMonth = true; }
        cell.tabIndex = isFocus ? 0 : -1;
        grid.appendChild(cell);
      }
      if (!focusInMonth) {
        var firstDay = grid.querySelector(".db-date-day");
        if (firstDay) { firstDay.tabIndex = 0; }
      }
    }

    function dayCellFrom(node) {
      var cell = node;
      while (cell && cell !== grid && !(cell.getAttribute && cell.getAttribute("data-db-day"))) {
        cell = cell.parentNode;
      }
      return cell && cell !== grid ? cell : null;
    }

    function refocusDay(stamp) {
      focusVisibly(grid.querySelector('[data-db-day="' + dayToISO(stamp) + '"]'));
    }

    function shiftMonth(delta, restoreFocus) {
      viewMonth += delta;
      while (viewMonth < 1) { viewMonth += 12; viewYear -= 1; }
      while (viewMonth > 12) { viewMonth -= 12; viewYear += 1; }
      var lastDay = new Date(Date.UTC(viewYear, viewMonth, 0)).getUTCDate();
      var wanted = focusDay === null ? 1 : Math.min(new Date(focusDay).getUTCDate(), lastDay);
      focusDay = dayFromParts(viewYear, viewMonth, wanted);
      render();
      if (restoreFocus) { refocusDay(focusDay); }
    }

    function moveFocus(deltaDays) {
      if (focusDay === null) { return; }
      focusDay += deltaDays * DAY_MS;
      var moved = new Date(focusDay);
      viewYear = moved.getUTCFullYear();
      viewMonth = moved.getUTCMonth() + 1;
      render();
      refocusDay(focusDay);
    }

    grid.addEventListener("click", function (event) {
      var cell = dayCellFrom(event.target);
      if (!cell) { return; }
      var stamp = dayFromISO(cell.getAttribute("data-db-day"));
      if (stamp === null) { return; }
      focusDay = stamp;
      pick(stamp);
    });

    grid.addEventListener("keydown", function (event) {
      /* ACTIVATION IS OURS, not the browser's. A focused button normally
         synthesises a click on Enter and Space; owning the keys and
         `preventDefault`ing removes the dependency on that default action and
         makes a double-advance impossible. */
      if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
        var target = dayCellFrom(event.target);
        if (!target) { return; }
        var chosen = dayFromISO(target.getAttribute("data-db-day"));
        if (chosen === null) { return; }
        event.preventDefault();
        focusDay = chosen;
        pick(chosen);
        refocusDay(chosen);
        return;
      }
      var step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
      if (Object.prototype.hasOwnProperty.call(step, event.key)) {
        event.preventDefault();
        moveFocus(step[event.key]);
        return;
      }
      if (event.key === "PageUp" || event.key === "PageDown") {
        event.preventDefault();
        shiftMonth(event.key === "PageUp" ? -1 : 1, true);
      }
    });

    prev.addEventListener("click", function () { shiftMonth(-1, false); });
    next.addEventListener("click", function () { shiftMonth(1, false); });
    clear.addEventListener("click", function () {
      ["single", "from", "to"].forEach(function (role) {
        if (fields[role]) { fields[role].carrier.value = ""; }
      });
      render();
    });

    /* Switching operator changes which control is on screen; the calendar must
       not stay open over a control that is no longer there. */
    var operator = group.querySelector("[data-db-op]");
    if (operator) {
      operator.addEventListener("change", function () { collapse(); render(); });
    }

    /* A rejected typed date must not be quietly dropped by an Apply. */
    var form = owningForm();
    if (form && form.addEventListener) {
      form.addEventListener("submit", function (event) {
        var bad = null;
        activeFields().forEach(function (field) {
          field.commit();
          if (!bad && field.isInvalid()) { bad = field; }
        });
        if (bad) {
          event.preventDefault();
          event.stopPropagation();
          focusVisibly(bad.node);
        }
      });
    }

    groups.push({
      group: group,
      collapse: collapse,
      isExpanded: function () { return expanded; },
    });

    render();
  }

  /* ------------------------------------------------- collapsing the calendar */

  /* Registered once for the page rather than per group, so a click closes every
     other group's calendar as well as deciding about its own. */
  var groups = [];

  function initDismissal() {
    /* AFTER the click has landed, never on pointerdown: collapsing removes a
       block of layout, and doing that mid-gesture moves the target out from
       under the pointer. */
    document.addEventListener("click", function (event) {
      groups.forEach(function (entry) {
        if (!contains(entry.group, event.target)) { entry.collapse(); }
      });
    });

    /* The keyboard path. A pointer-driven focusout usually reports no related
       target, so this only fires for a real Tab out of the group. */
    document.addEventListener("focusout", function (event) {
      var moved = event.relatedTarget;
      if (!moved) { return; }
      groups.forEach(function (entry) {
        if (!contains(entry.group, moved)) { entry.collapse(); }
      });
    });

    /* Escape collapses the calendar FIRST; a second Escape then reaches
       data-grid-filters.js and closes the column menu, which is the layered
       dismissal the rest of the grid already follows. Capture phase, because
       that module's handler is registered earlier and would otherwise win. */
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" && event.keyCode !== 27) { return; }
      var collapsedAny = false;
      groups.forEach(function (entry) {
        if (entry.isExpanded() && entry.collapse()) { collapsedAny = true; }
      });
      if (collapsedAny && event.preventDefault) { event.preventDefault(); }
    }, true);
  }

  function init() {
    initDismissal();
    all("[data-db-date-group]").forEach(enhance);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  /* Exposed for the deterministic harness only. */
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { parseTyped: parseTyped, dayFromISO: dayFromISO, dayToISO: dayToISO };
  }
})();
