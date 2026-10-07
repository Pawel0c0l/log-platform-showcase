/* DOM stub that executes the real api/static/js/data-grid-daterange.js.
 *
 * UI-20260831-01 asks for typed dates and a two-click range calendar. Both are
 * behaviour, not markup, so they are exercised by running the shipped file
 * against a stub DOM rather than by asserting on HTML strings.
 *
 * Invoked by ops/tests_manual/test_portal_database_date_range_filter.py:
 *
 *     node ops/tests_manual/data_grid_daterange_harness.js <scenario>
 *
 * Prints one JSON object describing the observed end state. A form submit is
 * recorded rather than performed, so a rejected typed date can be shown never
 * to reach a request.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const SCRIPT = path.join(__dirname, "..", "..", "api", "static", "js", "data-grid-daterange.js");

/* ------------------------------------------------------------------ DOM ---- */

function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    nodeType: 1,
    childNodes: [],
    parentNode: null,
    style: {},
    value: "",
    type: "",
    id: "",
    tabIndex: 0,
    _attrs: {},
    _classes: [],
    _listeners: {},
    _text: "",
    hasAttribute(name) { return name in this._attrs; },
    getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    removeAttribute(name) { delete this._attrs[name]; },
    addEventListener(type, fn) { (this._listeners[type] || (this._listeners[type] = [])).push(fn); },
    appendChild(child) { child.parentNode = this; this.childNodes.push(child); return child; },
    insertBefore(child, reference) {
      child.parentNode = this;
      const at = reference ? this.childNodes.indexOf(reference) : -1;
      if (at === -1) { this.childNodes.push(child); } else { this.childNodes.splice(at, 0, child); }
      return child;
    },
    get nextSibling() {
      if (!this.parentNode) { return null; }
      const at = this.parentNode.childNodes.indexOf(this);
      return at === -1 ? null : (this.parentNode.childNodes[at + 1] || null);
    },
    removeChild(child) {
      this.childNodes = this.childNodes.filter((c) => c !== child);
      child.parentNode = null;
      return child;
    },
    focus(options) {
      global.__focused = this;
      global.__focusOptions = options || null;
      global.__focusLog = (global.__focusLog || []).concat([{
        day: this.getAttribute ? this.getAttribute("data-db-day") : null,
        preventScroll: !!(options && options.preventScroll),
      }]);
    },
    /* A real text caret, so masking and its restoration are observable. */
    selectionStart: 0,
    selectionEnd: 0,
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
    get firstChild() { return this.childNodes[0] || null; },
    get textContent() {
      return this._text + this.childNodes.map((c) => c.textContent).join("");
    },
    set textContent(value) { this.childNodes = []; this._text = String(value); },
  };
  Object.defineProperty(node, "className", {
    get() { return node._classes.join(" "); },
    set(value) { node._classes = String(value).split(/\s+/).filter(Boolean); },
  });
  node.classList = {
    add: (name) => { if (node._classes.indexOf(name) === -1) node._classes.push(name); },
    remove: (name) => { node._classes = node._classes.filter((c) => c !== name); },
    contains: (name) => node._classes.indexOf(name) !== -1,
  };
  return node;
}

function walk(root, visit) {
  visit(root);
  root.childNodes.forEach((child) => walk(child, visit));
}

/* Only the selector shapes the script actually uses. Anything else throws, so
   the harness cannot pass by accidentally matching nothing. */
function matches(node, selector) {
  const sel = selector.trim();
  if (sel.charAt(0) === "[") {
    const inner = sel.slice(1, -1);
    const eq = inner.indexOf("=");
    if (eq === -1) { return node.hasAttribute(inner); }
    const name = inner.slice(0, eq);
    const value = inner.slice(eq + 1).replace(/^["']|["']$/g, "");
    return node.getAttribute(name) === value;
  }
  if (sel.charAt(0) === ".") {
    return sel.slice(1).split(".").every((cls) => node.classList.contains(cls));
  }
  /* `tag[attr="value"]` — the shape the script uses to find a form's Apply. */
  const tagAttr = /^([a-z]+)\[([^=\]]+)(?:=["']?([^"'\]]*)["']?)?\]$/i.exec(sel);
  if (tagAttr) {
    if (node.tagName !== tagAttr[1].toUpperCase()) { return false; }
    if (tagAttr[3] === undefined) { return node.hasAttribute(tagAttr[2]); }
    if (tagAttr[2] === "type") { return node.type === tagAttr[3]; }
    return node.getAttribute(tagAttr[2]) === tagAttr[3];
  }
  throw new Error("unsupported selector: " + selector);
}

function queryAll(root, selector) {
  const out = [];
  walk(root, (node) => {
    if (node !== root && matches(node, selector)) { out.push(node); }
  });
  return out;
}

function attachQuery(root) {
  walk(root, (node) => {
    node.querySelector = (sel) => queryAll(node, sel)[0] || null;
    node.querySelectorAll = (sel) => queryAll(node, sel);
  });
}

/* --------------------------------------------------------------- fixture --- */

function buildControl(type, fromValue, toValue, options) {
  const settings = options || {};
  const operatorValue = settings.operator || "range";
  const singleValue = settings.single || "";

  /* The group is the whole date filter for one column: operator select, the
     single control, the range pair and the calendar they share. This is the
     markup `_portal_database_filter_fieldset` emits for a date family, and it
     is identical in the column menu (`m-`) and the filter drawer (`p-`). */
  const group = makeNode("div");
  group.classList.add("db-filter-controls");
  group.setAttribute("data-db-filter-controls", "");
  group.setAttribute("data-db-family", "date");
  group.setAttribute("data-db-date-group", "");
  group.setAttribute("data-db-date-type", type);
  group.setAttribute("data-db-date-column", "Utworzono");
  group.setAttribute("data-db-strings", JSON.stringify({
    formatHint: type === "datetime-local" ? "dd.mm.rrrr gg:mm" : "dd.mm.rrrr",
    singleLabel: "Data dla {column}",
    openCalendar: "Pokaż kalendarz",
    closeCalendar: "Ukryj kalendarz",
    invalid: "Nieprawidłowa data — nie zastosowano.",
    calendarLabel: "Kalendarz zakresu dat",
    prevMonth: "Poprzedni miesiąc",
    nextMonth: "Następny miesiąc",
    pickStart: "Wybierz datę początkową.",
    pickEnd: "Wybierz datę końcową.",
    rangeSelected: "Zakres: {from} — {to}",
    inRange: "w zakresie",
    rangeStart: "początek zakresu",
    rangeEnd: "koniec zakresu",
    clearRange: "Wyczyść zakres",
    months: "styczeń,luty,marzec,kwiecień,maj,czerwiec,lipiec,sierpień,wrzesień,październik,listopad,grudzień",
    weekdays: "pon,wt,śr,czw,pt,sob,niedz",
  }));

  const operator = makeNode("select");
  operator.setAttribute("data-db-op", "");
  operator.setAttribute("name", "dateop__created_at");
  operator.value = operatorValue;
  group.appendChild(operator);

  /* Single control — shown for `przed` / `po`. */
  const singleBox = makeNode("div");
  singleBox.classList.add("db-filter-value");
  singleBox.setAttribute("data-db-value-single", "");
  if (operatorValue === "range") { singleBox.setAttribute("hidden", ""); }
  const singleInput = makeNode("input");
  singleInput.setAttribute("data-db-date-input", "single");
  singleInput.setAttribute("name", "date__created_at");
  singleInput.type = type;
  singleInput.id = "m-filter__created_at";
  singleInput.value = singleValue;
  const singleLabel = makeNode("label");
  singleLabel.setAttribute("for", singleInput.id);
  singleBox.appendChild(singleLabel);
  singleBox.appendChild(singleInput);
  group.appendChild(singleBox);

  /* Range pair — shown for `między`. */
  const rangeBox = makeNode("div");
  rangeBox.classList.add("db-filter-value");
  rangeBox.classList.add("db-filter-range");
  rangeBox.setAttribute("data-db-value-range", "");
  rangeBox.setAttribute("data-db-column", "created_at");
  if (operatorValue !== "range") { rangeBox.setAttribute("hidden", ""); }

  const fromInput = makeNode("input");
  fromInput.setAttribute("data-db-date-input", "from");
  fromInput.setAttribute("name", "date_from__created_at");
  fromInput.type = type;
  fromInput.id = "m-filter_to__created_at-from";
  fromInput.value = fromValue || "";
  const fromLabel = makeNode("label");
  fromLabel.setAttribute("for", fromInput.id);
  fromLabel._attrs["data-text"] = "Od";

  const toInput = makeNode("input");
  toInput.setAttribute("data-db-date-input", "to");
  toInput.setAttribute("name", "date_to__created_at");
  toInput.type = type;
  toInput.id = "m-filter_to__created_at-to";
  toInput.value = toValue || "";
  const toLabel = makeNode("label");
  toLabel.setAttribute("for", toInput.id);
  toLabel._attrs["data-text"] = "Do";

  rangeBox.appendChild(fromLabel);
  rangeBox.appendChild(fromInput);
  rangeBox.appendChild(toLabel);
  rangeBox.appendChild(toInput);
  group.appendChild(rangeBox);

  const form = makeNode("form");
  form.setAttribute("data-db-col-form", "");
  form.appendChild(group);
  const apply = makeNode("button");
  apply.type = "submit";
  form.appendChild(apply);

  const root = makeNode("body");
  root.appendChild(form);
  attachQuery(root);

  global.__focused = null;
  const submissions = [];
  const listeners = {};

  global.document = {
    nodeType: 9,
    readyState: "complete",
    addEventListener: (type2, fn) => { (listeners[type2] || (listeners[type2] = [])).push(fn); },
    createElement: (tag) => {
      const node = makeNode(tag);
      node.querySelector = (sel) => queryAll(node, sel)[0] || null;
      node.querySelectorAll = (sel) => queryAll(node, sel);
      return node;
    },
    querySelector: (sel) => queryAll(root, sel)[0] || null,
    querySelectorAll: (sel) => queryAll(root, sel),
  };
  global.window = { addEventListener() {} };

  function fire(node, type2, event) {
    attachQuery(root);
    (node._listeners[type2] || []).forEach((fn) => fn(Object.assign({ target: node,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {} }, event || {})));
  }

  /* Document-level events, for the dismissal contract. */
  function fireDocument(type2, event) {
    attachQuery(root);
    /* The caller's object is passed through UNMERGED when it already carries a
       `preventDefault`, so `defaultPrevented` is observable on the very object
       the caller holds. Copying it into a fresh object rebinds `this` and the
       flag lands somewhere the test can never see. */
    const payload = event && event.preventDefault ? event : Object.assign({
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {} }, event || {});
    (listeners[type2] || []).forEach((fn) => fn(payload));
  }

  function submit() {
    attachQuery(root);
    const event = { target: form, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
    (form._listeners.submit || []).forEach((fn) => fn(event));
    if (!event.defaultPrevented) {
      submissions.push({
        single: singleInput.value, from: fromInput.value, to: toInput.value });
    }
    return event.defaultPrevented;
  }

  form.requestSubmit = function () { submit(); };

  function pressKey(node, key) {
    attachQuery(root);
    ["keydown", "keypress", "keyup"].forEach((type2) => {
      const target = node;
      let host = node;
      while (host) {
        (host._listeners[type2] || []).forEach((fn) => fn({
          key: key,
          target: target,
          defaultPrevented: false,
          preventDefault() { this.defaultPrevented = true; },
          stopPropagation() {},
        }));
        host = host.parentNode;
      }
    });
  }

  return { root, form, group, container: group, operator,
           singleInput, fromInput, toInput, singleBox, rangeBox,
           fire, fireDocument, submit, pressKey, submissions };
}

function load() {
  eval(fs.readFileSync(SCRIPT, "utf8"));
}

/* The calendar is collapsed until a field is focused, so every scenario that
   touches a day opens it the way a user does. */
function openCalendar(fixture) {
  attachQuery(fixture.root);
  const field = fixture.container.querySelectorAll(".db-date-field")
    .filter((f) => !f.parentNode.hasAttribute("hidden"))[0];
  if (!field) { throw new Error("no visible date field to focus"); }
  fixture.fire(field, "focus");
  return field;
}

/* Address a field by the endpoint it carries, never by DOM order: the single
   control precedes the range pair, so index lookups pick the wrong one. */
function field(fixture, role) {
  attachQuery(fixture.root);
  const found = fixture.container.querySelector('[data-db-date-field="' + role + '"]');
  if (!found) { throw new Error("no field for role " + role); }
  return found;
}

function calendarVisible(fixture) {
  attachQuery(fixture.root);
  const calendar = fixture.container.querySelector(".db-date-calendar");
  return !!calendar && !calendar.hasAttribute("hidden");
}

function dayCell(fixture, iso) {
  attachQuery(fixture.root);
  return fixture.container.querySelector('[data-db-day="' + iso + '"]');
}

function clickDay(fixture, iso) {
  if (!calendarVisible(fixture)) { openCalendar(fixture); }
  const cell = dayCell(fixture, iso);
  if (!cell) { throw new Error("no day cell for " + iso); }
  const grid = fixture.container.querySelector(".db-date-grid");
  attachQuery(fixture.root);
  (grid._listeners.click || []).forEach((fn) => fn({ target: cell }));
}

function rangeShape(fixture) {
  attachQuery(fixture.root);
  const days = fixture.container.querySelectorAll(".db-date-day");
  return {
    from: fixture.fromInput.value,
    to: fixture.toInput.value,
    start: days.filter((d) => d.classList.contains("range-start")).map((d) => d.getAttribute("data-db-day")),
    end: days.filter((d) => d.classList.contains("range-end")).map((d) => d.getAttribute("data-db-day")),
    inRange: days.filter((d) => d.classList.contains("in-range")).map((d) => d.getAttribute("data-db-day")),
  };
}

/* What a real browser produces when someone types into a text field: `input`
   on every keystroke, then `change` when the value is committed on blur/Enter.
   Assigning `.value` and firing only `change` is NOT typing — the script
   deliberately ignores it, because a programmatic write must never be read back
   as user input. Dispatching the unrealistic order is what let the live
   typed-field defect through the previous harness. */
function typeInto(fixture, node, value) {
  /* You cannot type into a field without focusing it first, and focus is what
     expands the calendar — so the real order includes it. */
  fixture.fire(node, "focus");
  node.value = value;
  fixture.fire(node, "input");
  fixture.fire(node, "change");
}

/* One keystroke at a time, each inserting at the caret and firing `input` —
   which is what a mask actually has to survive. `typeInto` sets a whole value at
   once and is still right for paste; this is right for typing. */
function typeKeystrokes(fixture, node, text) {
  /* Focus FIRST — focusing expands the calendar, which re-renders the field from
     the canonical value, so clearing before that would be undone. This is the
     select-all-and-type-over gesture. */
  fixture.fire(node, "focus");
  node.value = "";
  node.selectionStart = node.selectionEnd = 0;
  String(text).split("").forEach((ch) => {
    const at = node.selectionStart;
    node.value = node.value.slice(0, at) + ch + node.value.slice(node.selectionEnd);
    node.selectionStart = node.selectionEnd = at + 1;
    fixture.fire(node, "input");
  });
}

/* Backspace/Delete go through keydown, because the module handles them there. */
function pressEditKey(fixture, node, key) {
  const event = {
    key: key, target: node, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() {},
  };
  (node._listeners.keydown || []).forEach((fn) => fn(event));
  if (!event.defaultPrevented) {
    /* The browser's own deletion, for the cases the module leaves alone. */
    const at = node.selectionStart;
    if (key === "Backspace" && at > 0) {
      node.value = node.value.slice(0, at - 1) + node.value.slice(at);
      node.selectionStart = node.selectionEnd = at - 1;
    }
    fixture.fire(node, "input");
  }
  return event.defaultPrevented;
}

function typedValues(fixture) {
  return [field(fixture, "from").value, field(fixture, "to").value];
}

/* -------------------------------------------------------------- scenarios -- */

/* The calendar anchors its view month on whatever the inputs hold, falling back
   to "today" when they hold nothing — which would make every scenario that
   clicks an August day depend on the date the suite is run. Seeding a COMPLETE
   August range pins the view AND puts the control in the state a user opening an
   already-filtered column is in, so the next click restarts the range. */
const ANCHOR_FROM = "2026-08-01";
const ANCHOR_TO = "2026-08-02";
/* The same anchor in the wire shape a timestamp column uses. */
const ANCHOR_FROM_TS = "2026-08-01T00:00";
const ANCHOR_TO_TS = "2026-08-02T23:59";

const scenario = process.argv[2];
const out = {};

if (scenario === "typed-formats") {
  const fixture = buildControl("date", "", "");
  load();
  const fromText = field(fixture, "from");
  out.accepted = {};
  out.rejected = {};
  [
    "2026-08-31", "2026.08.31", "2026/08/31",
    "31.08.2026", "31-08-2026", "31/08/2026",
    "2026-08-31 14:30", "2026-08-31T14:30", "  2026-08-31  ",
  ].forEach(function (form) {
    typeInto(fixture, fromText, form);
    out.accepted[form] = { value: fixture.fromInput.value, invalid: fromText.classList.contains("invalid") };
  });
  [
    "2026-02-30", "2026-13-01", "31.02.2026", "not a date",
    "2026-08", "03/04/26", "2026-08-31 25:00", "2026-08-31 12:74",
  ].forEach(function (form) {
    fixture.fromInput.value = "2026-01-01";
    typeInto(fixture, fromText, form);
    out.rejected[form] = { value: fixture.fromInput.value, invalid: fromText.classList.contains("invalid") };
  });
} else if (scenario === "calendar-range") {
  const fixture = buildControl("date", ANCHOR_FROM, ANCHOR_TO);
  load();
  clickDay(fixture, "2026-08-05");
  out.afterFirst = rangeShape(fixture);
  clickDay(fixture, "2026-08-09");
  out.afterSecond = rangeShape(fixture);
  out.typed = typedValues(fixture);
} else if (scenario === "calendar-swap") {
  const fixture = buildControl("date", ANCHOR_FROM, ANCHOR_TO);
  load();
  clickDay(fixture, "2026-08-20");
  clickDay(fixture, "2026-08-14");
  out.afterSwap = rangeShape(fixture);
} else if (scenario === "calendar-restart") {
  const fixture = buildControl("date", ANCHOR_FROM, ANCHOR_TO);
  load();
  clickDay(fixture, "2026-08-05");
  clickDay(fixture, "2026-08-09");
  out.complete = rangeShape(fixture);
  clickDay(fixture, "2026-08-21");
  out.afterThird = rangeShape(fixture);
} else if (scenario === "typed-sync") {
  const fixture = buildControl("date", ANCHOR_FROM, ANCHOR_TO);
  load();
  typeInto(fixture, field(fixture, "from"), "2026-08-04");
  typeInto(fixture, field(fixture, "to"), "2026-08-08");
  out.afterTyping = rangeShape(fixture);
  /* And the other direction: a calendar click rewrites the text fields. */
  clickDay(fixture, "2026-08-12");
  clickDay(fixture, "2026-08-15");
  out.afterClicking = { shape: rangeShape(fixture), typed: typedValues(fixture) };
} else if (scenario === "typed-invalid-blocks-apply") {
  const fixture = buildControl("date", "2026-08-01", "2026-08-31");
  load();
  typeInto(fixture, field(fixture, "from"), "32.13.2026");
  out.afterInvalid = {
    nativeUnchanged: fixture.fromInput.value,
    invalidMarked: field(fixture, "from").classList.contains("invalid"),
    ariaInvalid: field(fixture, "from").getAttribute("aria-invalid"),
    statusText: fixture.container.querySelector(".db-date-status").textContent,
  };
  out.blocked = fixture.submit();
  out.submissions = fixture.submissions.length;
  out.focusedInvalid = global.__focused === field(fixture, "from");
  /* Correcting it lets the apply through, unchanged in shape. */
  typeInto(fixture, field(fixture, "from"), "2026-08-02");
  out.afterFix = { blocked: fixture.submit(), submissions: fixture.submissions };
} else if (scenario === "timestamp-wire-format") {
  const fixture = buildControl("datetime-local", ANCHOR_FROM, ANCHOR_TO);
  load();
  clickDay(fixture, "2026-08-05");
  clickDay(fixture, "2026-08-09");
  out.range = { from: fixture.fromInput.value, to: fixture.toInput.value };
  typeInto(fixture, field(fixture, "from"), "2026-08-06 07:15");
  out.typedTime = fixture.fromInput.value;
} else if (scenario === "no-extra-fields") {
  const fixture = buildControl("date", "2026-08-01", "2026-08-31");
  load();
  openCalendar(fixture);          // the day buttons only exist once expanded
  attachQuery(fixture.root);
  const named = [];
  walk(fixture.root, (node) => {
    if (node.nodeType === 1 && node.hasAttribute("name")) { named.push(node.getAttribute("name")); }
  });
  out.namedFields = named;
  const buttons = fixture.container.querySelectorAll(".db-date-day")
    .concat(fixture.container.querySelectorAll(".db-date-nav"))
    .concat(fixture.container.querySelectorAll(".db-date-clear"));
  out.allButtonsAreTypeButton = buttons.every((b) => b.type === "button");
  out.buttonCount = buttons.length;
} else if (scenario === "keyboard-roving") {
  const fixture = buildControl("date", "2026-08-10", "");
  load();
  openCalendar(fixture);
  attachQuery(fixture.root);
  const grid = fixture.container.querySelector(".db-date-grid");
  function tabbable() {
    attachQuery(fixture.root);
    return fixture.container.querySelectorAll(".db-date-day")
      .filter((d) => d.tabIndex === 0).map((d) => d.getAttribute("data-db-day"));
  }
  out.initialTabStops = tabbable();
  (grid._listeners.keydown || []).forEach((fn) => fn({
    key: "ArrowRight", preventDefault() {}, target: grid,
  }));
  out.afterArrowRight = tabbable();
  (grid._listeners.keydown || []).forEach((fn) => fn({
    key: "ArrowDown", preventDefault() {}, target: fixture.container.querySelector(".db-date-grid"),
  }));
  out.afterArrowDown = tabbable();
} else if (scenario === "typed-blur-real-order") {
  /* B6 — the REAL browser order for tabbing out of a text field after typing:
     `input`, then `change`, then `blur`. The live defect appeared only after a
     calendar click had left the range half-open, so that is reproduced too. */
  const fixture = buildControl("datetime-local", ANCHOR_FROM, ANCHOR_TO);
  load();
  clickDay(fixture, "2026-08-20");           // half-open range, as the user had
  const fromText = field(fixture, "from");
  out.beforeTyping = { from: fixture.fromInput.value, typedFrom: fromText.value };
  fromText.value = "01.08.2026";             // a form on the accepted list
  fixture.fire(fromText, "input");
  fixture.fire(fromText, "change");
  fixture.fire(fromText, "blur");
  out.afterBlur = {
    nativeFrom: fixture.fromInput.value,
    typedFrom: fromText.value,
    invalid: fromText.classList.contains("invalid"),
  };
} else if (scenario === "typed-enter-real-order") {
  /* B3 — Enter inside a typed field must commit before anything applies, and a
     following blur must not undo it. */
  const fixture = buildControl("datetime-local", ANCHOR_FROM, ANCHOR_TO);
  load();
  clickDay(fixture, "2026-08-20");
  const toText = field(fixture, "to");
  toText.value = "15.09.2026";
  fixture.fire(toText, "input");
  fixture.fire(toText, "keydown", { key: "Enter" });
  out.afterEnter = { nativeTo: fixture.toInput.value, typedTo: toText.value };
  fixture.fire(toText, "blur");
  out.afterBlur = { nativeTo: fixture.toInput.value, typedTo: toText.value };
} else if (scenario === "typed-and-clicks-interleaved") {
  /* B5 — whatever the native inputs hold after any interleaving is exactly what
     an apply serialises. */
  const fixture = buildControl("datetime-local", ANCHOR_FROM, ANCHOR_TO);
  load();
  clickDay(fixture, "2026-08-10");
  typeInto(fixture, field(fixture, "to"), "12.08.2026");
  fixture.fire(field(fixture, "to"), "blur");
  out.afterTypedEnd = { from: fixture.fromInput.value, to: fixture.toInput.value };
  clickDay(fixture, "2026-08-25");           // third click restarts
  typeInto(fixture, field(fixture, "to"), "28.08.2026");
  fixture.fire(field(fixture, "to"), "blur");
  out.shown = { from: fixture.fromInput.value, to: fixture.toInput.value };
  fixture.submit();
  out.submitted = fixture.submissions[fixture.submissions.length - 1] || null;
} else if (scenario === "enter-in-typed-field-applies") {
  /* CC1/CC6 — a real Enter press must parse the text AND issue the apply. The
     previous version leaned on implicit form submission, so nothing here fired
     at all. */
  const fixture = buildControl("datetime-local", ANCHOR_FROM, ANCHOR_TO);
  load();
  typeInto(fixture, field(fixture, "to"), "27.08.2026");
  fixture.pressKey(field(fixture, "to"), "Enter");
  out.afterEnter = {
    nativeTo: fixture.toInput.value,
    typedTo: field(fixture, "to").value,
    invalid: field(fixture, "to").classList.contains("invalid"),
    submissions: fixture.submissions,
  };
} else if (scenario === "enter-in-typed-field-invalid-blocks") {
  /* CC2 — invalid text under a real Enter behaves exactly like the blur path. */
  const fixture = buildControl("datetime-local", ANCHOR_FROM, ANCHOR_TO);
  load();
  typeInto(fixture, field(fixture, "from"), "32.13.2026");
  fixture.pressKey(field(fixture, "from"), "Enter");
  out.afterInvalidEnter = {
    nativeFrom: fixture.fromInput.value,
    typedFrom: field(fixture, "from").value,
    invalid: field(fixture, "from").classList.contains("invalid"),
    ariaInvalid: field(fixture, "from").getAttribute("aria-invalid"),
    status: fixture.container.querySelector(".db-date-status").textContent,
    submissions: fixture.submissions.length,
  };
  /* CC3 — correcting it and pressing Enter clears the flag and applies. */
  typeInto(fixture, field(fixture, "from"), "28.08.2026");
  fixture.pressKey(field(fixture, "from"), "Enter");
  out.afterCorrection = {
    nativeFrom: fixture.fromInput.value,
    invalid: field(fixture, "from").classList.contains("invalid"),
    submissions: fixture.submissions,
  };
} else if (scenario === "calendar-day-keyboard-activation") {
  /* CC4/CC6 — Enter and Space on a focused day advance the range exactly as a
     pointer click does. No synthesised click is emulated, because the live page
     showed the browser does not deliver one here. */
  const fixture = buildControl("date", ANCHOR_FROM, ANCHOR_TO);
  load();
  openCalendar(fixture);
  const first = dayCell(fixture, "2026-08-09");
  fixture.pressKey(first, "Enter");
  out.afterEnterOnDay = rangeShape(fixture);
  const second = dayCell(fixture, "2026-08-13");
  fixture.pressKey(second, " ");
  out.afterSpaceOnDay = rangeShape(fixture);
  /* A third activation restarts, same as the pointer path. */
  const third = dayCell(fixture, "2026-08-20");
  fixture.pressKey(third, "Enter");
  out.afterThirdActivation = rangeShape(fixture);
  out.submissions = fixture.submissions.length;
} else if (scenario === "two-fields-and-collapsed-calendar") {
  /* A1/R1/R4 — range mode shows exactly two visible fields and no calendar
     until one of them is focused. The controls the server rendered are still
     present, carrying the wire values, but hidden. */
  const fixture = buildControl("datetime-local", ANCHOR_FROM_TS, ANCHOR_TO_TS);
  load();
  attachQuery(fixture.root);
  out.initial = {
    visibleFields: fixture.container.querySelectorAll(".db-date-field")
      .filter((f) => !f.parentNode.hasAttribute("hidden"))
      .map((f) => f.getAttribute("data-db-date-field")),
    carrierTypes: ["single", "from", "to"].map((role) =>
      fixture.container.querySelector('[data-db-date-input="' + role + '"]').type),
    calendarVisible: calendarVisible(fixture),
    ariaExpanded: field(fixture, "from").getAttribute("aria-expanded"),
    shown: [field(fixture, "from").value, field(fixture, "to").value],
    wire: { from: fixture.fromInput.value, to: fixture.toInput.value },
  };
  fixture.fire(field(fixture, "from"), "focus");
  out.afterFocus = {
    calendarVisible: calendarVisible(fixture),
    ariaExpanded: field(fixture, "from").getAttribute("aria-expanded"),
    dayButtons: fixture.container.querySelectorAll(".db-date-day").length,
  };
} else if (scenario === "calendar-collapse-contract") {
  /* R4/R7 — a click outside collapses the calendar; Escape collapses it first
     and leaves the menu to data-grid-filters.js on the SECOND press. */
  const fixture = buildControl("datetime-local", ANCHOR_FROM_TS, ANCHOR_TO_TS);
  load();
  openCalendar(fixture);
  out.open = calendarVisible(fixture);

  const escape = { key: "Escape", defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
  fixture.fireDocument("keydown", escape);
  out.afterFirstEscape = { calendarVisible: calendarVisible(fixture),
                           consumed: escape.defaultPrevented };

  /* Second press: nothing left to collapse, so the key is NOT consumed and the
     column menu's own handler gets it. */
  const escape2 = { key: "Escape", defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
  fixture.fireDocument("keydown", escape2);
  out.afterSecondEscape = { calendarVisible: calendarVisible(fixture),
                            consumed: escape2.defaultPrevented };

  /* And the pointer path: reopen, then click away. */
  openCalendar(fixture);
  out.reopened = calendarVisible(fixture);
  fixture.fireDocument("click", { target: fixture.root });
  out.afterOutsideClick = calendarVisible(fixture);
} else if (scenario === "single-mode") {
  /* R2/A5 — `przed`/`po` shows ONE field with the same interaction, and writes
     the single wire parameter. */
  const fixture = buildControl("datetime-local", "", "", { operator: "older", single: "2026-08-02T00:00" });
  load();
  attachQuery(fixture.root);
  out.visibleFields = fixture.container.querySelectorAll(".db-date-field")
    .filter((f) => !f.parentNode.hasAttribute("hidden"))
    .map((f) => f.getAttribute("data-db-date-field"));
  out.shown = field(fixture, "single").value;
  out.calendarVisible = calendarVisible(fixture);

  fixture.fire(field(fixture, "single"), "focus");
  out.afterFocus = { calendarVisible: calendarVisible(fixture) };
  clickDay(fixture, "2026-08-19");
  out.afterDayClick = {
    single: fixture.singleInput.value,
    from: fixture.fromInput.value,
    to: fixture.toInput.value,
    shown: field(fixture, "single").value,
  };
  typeInto(fixture, field(fixture, "single"), "07.09.2026 08:45");
  fixture.fire(field(fixture, "single"), "blur");
  out.afterTyping = { single: fixture.singleInput.value, shown: field(fixture, "single").value };
  fixture.submit();
  out.submitted = fixture.submissions[fixture.submissions.length - 1] || null;
} else if (scenario === "mask-typing") {
  /* A1/A2/R1/R2 — bare digits grow their own separators, keystroke by keystroke. */
  const fixture = buildControl("datetime-local", ANCHOR_FROM_TS, ANCHOR_TO_TS);
  load();
  const from = field(fixture, "from");
  fixture.fire(from, "focus");
  from.value = "";
  from.selectionStart = from.selectionEnd = 0;
  out.progressive = [];
  ["0", "1", "0", "8", "2", "0", "2", "6"].forEach((ch) => {
    const at = from.selectionStart;
    from.value = from.value.slice(0, at) + ch + from.value.slice(at);
    from.selectionStart = from.selectionEnd = at + 1;
    fixture.fire(from, "input");
    out.progressive.push(from.value);
  });
  fixture.fire(from, "blur");
  out.afterDateOnly = { shown: from.value, wire: fixture.fromInput.value };

  const to = field(fixture, "to");
  typeKeystrokes(fixture, to, "120820261430");
  out.beforeCommit = to.value;
  fixture.fire(to, "blur");
  out.afterDateTime = { shown: to.value, wire: fixture.toInput.value };
} else if (scenario === "mask-manual-separators") {
  /* A3/R3 — a user typing their own dots must not fight the mask. */
  const fixture = buildControl("datetime-local", ANCHOR_FROM_TS, ANCHOR_TO_TS);
  load();
  const from = field(fixture, "from");
  typeKeystrokes(fixture, from, "01.08.2026");
  out.whileTyping = from.value;
  fixture.fire(from, "blur");
  out.committed = { shown: from.value, wire: fixture.fromInput.value };

  /* And a format the mask must keep its hands off entirely. */
  const to = field(fixture, "to");
  typeInto(fixture, to, "2026-08-31");        // pasted/blur-committed, not keystroked
  out.isoUntouched = to.value;
  fixture.fire(to, "blur");
  out.isoCommitted = fixture.toInput.value;
} else if (scenario === "mask-backspace") {
  /* A4/R4 — one press removes one DIGIT, stepping over separators. */
  const fixture = buildControl("datetime-local", ANCHOR_FROM_TS, ANCHOR_TO_TS);
  load();
  const from = field(fixture, "from");
  typeKeystrokes(fixture, from, "01082026");
  out.start = from.value;
  out.steps = [];
  for (let i = 0; i < 5; i += 1) {
    pressEditKey(fixture, from, "Backspace");
    out.steps.push({ value: from.value, caret: from.selectionStart });
  }
  /* Mid-value edit: caret after the day, delete one digit forward. */
  typeKeystrokes(fixture, from, "01082026");
  from.selectionStart = from.selectionEnd = 2;     // "01|.08.2026"
  pressEditKey(fixture, from, "Delete");
  out.afterMidDelete = { value: from.value, caret: from.selectionStart };
} else if (scenario === "mask-paste-and-invalid") {
  /* A5/A6/R5/R6 — a pasted run of digits formats and commits; an impossible one
     is flagged rather than mangled. */
  const fixture = buildControl("datetime-local", ANCHOR_FROM_TS, ANCHOR_TO_TS);
  load();
  const from = field(fixture, "from");
  typeInto(fixture, from, "01082026");            // one shot, as a paste arrives
  out.pasted = { shown: from.value, wire: fixture.fromInput.value };

  const to = field(fixture, "to");
  typeInto(fixture, to, "120820261430");
  out.pastedWithTime = { shown: to.value, wire: fixture.toInput.value };

  typeInto(fixture, from, "32132026");
  out.invalid = {
    shown: from.value,
    wireUnchanged: fixture.fromInput.value,
    invalid: from.classList.contains("invalid"),
  };
} else if (scenario === "mask-parses-bare-digits") {
  /* R6 — defence in depth: the COMMIT parser accepts the bare form on its own,
     so a value that reached the field without the mask ever running still means
     what the user meant. Exercised through the module's exported parser rather
     than through the mask, which is the point of the requirement. */
  const fixture = buildControl("date", ANCHOR_FROM, ANCHOR_TO);
  load();
  const parse = module.exports.parseTyped;
  out.parsed = {};
  ["01082026", "31122026", "010820261430"].forEach((raw) => {
    const result = parse(raw);
    out.parsed[raw] = result.error ? "ERROR"
      : module.exports.dayToISO(result.day) + (result.hour === null ? ""
        : " " + String(result.hour).padStart(2, "0") + ":" + String(result.minute).padStart(2, "0"));
  });
  ["32132026", "00002026", "01132026"].forEach((raw) => {
    out.parsed[raw] = parse(raw).error ? "ERROR" : "ACCEPTED";
  });
  void fixture;
} else if (scenario === "calendar-focus-is-guarded") {
  /* G1 for the three daterange sites. The calendar is the exposed one: every
     arrow key and every activation moves focus to another day inside a panel
     that is `max-height: 60vh; overflow-y: auto`, so an unguarded focus would
     have the browser scroll the grid to reveal it — and the grid's scroll is
     what dismisses the menu. */
  const fixture = buildControl("date", ANCHOR_FROM, ANCHOR_TO);
  load();
  openCalendar(fixture);
  global.__focusLog = [];

  /* The shared guard is used when data-grid-filters.js has published it. */
  let delegated = 0;
  global.window.dbGridFocusVisibly = function (node) { delegated += 1; node.focus({ preventScroll: true }); };

  const grid = fixture.container.querySelector(".db-date-grid");
  ["ArrowRight", "ArrowDown"].forEach((key) => {
    (grid._listeners.keydown || []).forEach((fn) => fn({
      key: key, target: grid, preventDefault() {}, stopPropagation() {},
    }));
  });
  /* Activate a day BEFORE paging: PageDown moves the view to the next month, and
     a day cell from the old month no longer exists to be activated. */
  const day = dayCell(fixture, "2026-08-09");
  if (!day) { throw new Error("day cell missing before activation"); }
  fixture.pressKey(day, "Enter");
  (grid._listeners.keydown || []).forEach((fn) => fn({
    key: "PageDown", target: grid, preventDefault() {}, stopPropagation() {},
  }));

  out.delegatedToSharedGuard = delegated;
  out.focusCalls = global.__focusLog.length;
  out.everyFocusPreventedScroll = global.__focusLog.every((entry) => entry.preventScroll);

  /* And with the shared guard absent, the module still prevents the scroll on
     its own — only the reveal is missing. */
  global.window.dbGridFocusVisibly = undefined;
  global.__focusLog = [];
  (grid._listeners.keydown || []).forEach((fn) => fn({
    key: "ArrowRight", target: grid, preventDefault() {}, stopPropagation() {},
  }));
  out.standalone = {
    focusCalls: global.__focusLog.length,
    everyFocusPreventedScroll: global.__focusLog.every((entry) => entry.preventScroll),
  };
} else if (scenario === "invalid-field-focus-is-guarded") {
  /* Sites 426 and 778: focus returned to a field the commit rejected. */
  const fixture = buildControl("datetime-local", ANCHOR_FROM_TS, ANCHOR_TO_TS);
  load();
  const from = field(fixture, "from");
  typeInto(fixture, from, "51.08.2026");
  global.__focusOptions = undefined;
  fixture.pressKey(from, "Enter");                 // Enter path (426)
  out.afterEnter = {
    invalid: from.classList.contains("invalid"),
    focused: global.__focused === from,
    preventScroll: !!(global.__focusOptions && global.__focusOptions.preventScroll),
  };
  global.__focusOptions = undefined;
  fixture.submit();                                 // Apply path (778)
  out.afterSubmit = {
    submissions: fixture.submissions.length,
    focused: global.__focused === from,
    preventScroll: !!(global.__focusOptions && global.__focusOptions.preventScroll),
  };
} else {
  throw new Error("unknown scenario: " + scenario);
}

process.stdout.write(JSON.stringify(out));
