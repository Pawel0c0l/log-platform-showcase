/* DOM stub that executes the real Database Explorer responsive/accessibility
 * modules (approved stage S10).
 *
 * The parts of `RSP-001`–`RSP-003` and `AC-1`–`AC-14` that live in JavaScript —
 * the below-768 advisory and its session-scoped acknowledgement, the `RSP-002`
 * filter drawer's modal semantics and staged-state preservation, the row-detail
 * overlay's dialog semantics, focus containment and the single-layer `Esc`
 * precedence across the shell and the grid — are exercised by running the
 * shipped files rather than by reading them.
 *
 * Loaded per scenario, in the same order the page loads them:
 *
 *     api/static/js/shell.js                (shell navigation drawer)
 *     api/static/js/data-grid-filters.js    (column menus + staged filter panel)
 *     api/static/js/data-grid-responsive.js (S10)
 *
 * Invoked by ops/tests_manual/test_portal_database_responsive_accessibility.py:
 *
 *     node ops/tests_manual/data_grid_responsive_harness.js <scenario>
 *
 * Prints one JSON object describing the observed end state. A form submit is
 * recorded rather than performed: crossing a breakpoint, dismissing the drawer
 * or accepting the advisory must never apply anything.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const JS_DIR = path.join(__dirname, "..", "..", "api", "static", "js");
const SHELL = path.join(JS_DIR, "shell.js");
const FILTERS = path.join(JS_DIR, "data-grid-filters.js");
const RESPONSIVE = path.join(JS_DIR, "data-grid-responsive.js");

/* ------------------------------------------------------------------ DOM ---- */

let nodeSeq = 0;

function makeNode(tag, attrs, children) {
  const node = {
    _tag: tag,
    _attrs: Object.assign({}, attrs || {}),
    _id: (nodeSeq += 1),
    nodeType: 1,
    childNodes: [],
    parentNode: null,
    style: {},
    classList: null,
    _classes: [],
    _listeners: {},
    disabled: false,
    hasAttribute(name) { return name in this._attrs; },
    getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    removeAttribute(name) { delete this._attrs[name]; },
    addEventListener(type, fn) { (this._listeners[type] || (this._listeners[type] = [])).push(fn); },
    getBoundingClientRect() { return { left: 0, right: 100, top: 0, bottom: 20, width: 80, height: 20 }; },
    getClientRects() { return [{ width: 80, height: 20 }]; },
    contains(other) {
      let cursor = other;
      while (cursor) {
        if (cursor === this) return true;
        cursor = cursor.parentNode;
      }
      return false;
    },
    focus() { global.__focused = this; },
    get id() { return this._attrs.id || ""; },
    set id(value) { this._attrs.id = String(value); },
    get textContent() {
      return (this._attrs["data-text"] || "") + this.childNodes.map((c) => c.textContent || "").join("");
    },
  };
  node.classList = {
    add: (name) => { if (node._classes.indexOf(name) === -1) node._classes.push(name); },
    remove: (name) => { node._classes = node._classes.filter((c) => c !== name); },
    contains: (name) => node._classes.indexOf(name) !== -1,
  };
  (node._attrs.class ? String(node._attrs.class).split(/\s+/) : []).forEach((name) => {
    if (name) node.classList.add(name);
  });
  (children || []).forEach((child) => {
    child.parentNode = node;
    node.childNodes.push(child);
  });
  return node;
}

function walk(root, visit) {
  visit(root);
  root.childNodes.forEach((child) => walk(child, visit));
}

/* A deliberately small selector engine covering exactly the shapes the shipped
   modules use. An unsupported selector throws rather than silently matching
   nothing, so the harness cannot pass by accident. */
function matchesSimple(node, selector) {
  let sel = String(selector).trim();
  if (!sel) {
    throw new Error("empty selector part");
  }
  /* :not([attr]) / :not([attr="value"]) suffix */
  let negation = null;
  const notAt = sel.indexOf(":not(");
  if (notAt !== -1) {
    const close = sel.lastIndexOf(")");
    negation = sel.slice(notAt + 5, close);
    sel = sel.slice(0, notAt) + sel.slice(close + 1);
  }
  if (negation !== null && !negation.startsWith("[")) {
    throw new Error("unsupported :not() selector: " + selector);
  }

  let ok = true;
  let rest = sel;

  /* leading tag */
  const tagMatch = /^[a-zA-Z][a-zA-Z0-9]*/.exec(rest);
  if (tagMatch) {
    ok = ok && node._tag === tagMatch[0];
    rest = rest.slice(tagMatch[0].length);
  }
  while (rest.length) {
    if (rest.charAt(0) === ".") {
      const next = rest.slice(1).search(/[.#[]/);
      const name = next === -1 ? rest.slice(1) : rest.slice(1, 1 + next);
      ok = ok && node.classList.contains(name);
      rest = next === -1 ? "" : rest.slice(1 + next);
    } else if (rest.charAt(0) === "#") {
      const next = rest.slice(1).search(/[.#[]/);
      const name = next === -1 ? rest.slice(1) : rest.slice(1, 1 + next);
      ok = ok && node.getAttribute("id") === name;
      rest = next === -1 ? "" : rest.slice(1 + next);
    } else if (rest.charAt(0) === "[") {
      const close = rest.indexOf("]");
      if (close === -1) throw new Error("unsupported selector: " + selector);
      ok = ok && attrMatches(node, rest.slice(1, close));
      rest = rest.slice(close + 1);
    } else {
      throw new Error("unsupported selector: " + selector);
    }
  }
  if (negation !== null) {
    ok = ok && !attrMatches(node, negation.slice(1, negation.lastIndexOf("]")));
  }
  return ok;
}

function attrMatches(node, inner) {
  const eq = inner.indexOf("=");
  if (eq === -1) {
    return node.hasAttribute(inner);
  }
  const name = inner.slice(0, eq);
  const value = inner.slice(eq + 1).replace(/^["']|["']$/g, "");
  return node.getAttribute(name) === value;
}

function queryAll(root, selector) {
  const parts = String(selector).split(",").map((s) => s.trim()).filter(Boolean);
  const out = [];
  walk(root, (node) => {
    if (node === root) return;
    for (let i = 0; i < parts.length; i += 1) {
      /* Descendant selectors used by the modules all end in a simple part, and
         every ancestor is inside the subtree being queried anyway. */
      const last = parts[i].split(/\s+/).pop();
      if (matchesSimple(node, last) && out.indexOf(node) === -1) {
        out.push(node);
        break;
      }
    }
  });
  return out;
}

function attachQuery(root) {
  walk(root, (node) => {
    node.querySelector = (sel) => queryAll(node, sel)[0] || null;
    node.querySelectorAll = (sel) => queryAll(node, sel);
    node.closest = (sel) => {
      let cursor = node;
      while (cursor && cursor.nodeType === 1) {
        const parts = String(sel).split(",").map((s) => s.trim()).filter(Boolean);
        for (let i = 0; i < parts.length; i += 1) {
          if (matchesSimple(cursor, parts[i])) return cursor;
        }
        cursor = cursor.parentNode;
      }
      return null;
    };
  });
}

/* --------------------------------------------------------------- storage --- */

function makeStorage(mode) {
  const data = {};
  return {
    _data: data,
    getItem(key) {
      if (mode === "throws") throw new Error("storage unavailable");
      return key in data ? data[key] : null;
    },
    setItem(key, value) {
      if (mode === "throws") throw new Error("storage unavailable");
      data[key] = String(value);
    },
    removeItem(key) {
      if (mode === "throws") throw new Error("storage unavailable");
      delete data[key];
    },
  };
}

/* -------------------------------------------------------------- fixture ---- */

function field(tag, name, value, attrs) {
  const node = makeNode(tag, Object.assign({ name: name }, attrs || {}));
  node.value = value;
  node.type = tag === "select" ? "select-one" : "text";
  return node;
}

/* The rendered sheet, in the shape api/main.py actually emits: an advisory
   shipped hidden, the sheet, the filter <details> with its scrim, drawer head
   and single staged form, and the demoted export section. */
function buildSheet(options) {
  const opts = options || {};

  const advisoryTitle = makeNode("h2", { id: "db-narrow-title", tabindex: "-1", class: "db-narrow-title" });
  const accept = makeNode("button", { type: "button", "data-db-narrow-accept": "", class: "db-narrow-action" });
  const reports = makeNode("a", { href: "/user/reports", class: "db-narrow-action primary" });
  const advisory = makeNode(
    "section",
    { "data-db-narrow-advisory": "", hidden: "", "aria-labelledby": "db-narrow-title", class: "db-narrow-advisory" },
    [advisoryTitle, reports, accept]
  );

  const opSelect = field("select", "op__driver_name", "contains", { "data-db-op": "" });
  const staged = field("input", "filter__driver_name", opts.stagedValue || "");
  const exact = field("input", "filter_exact__driver_name", opts.exactValue || "");
  const single = makeNode("div", { "data-db-value-single": "" }, [staged, exact]);
  const controls = makeNode("div", { "data-db-filter-controls": "", "data-db-family": "text" }, [opSelect, single]);
  const heading = makeNode("h3", { class: "db-panel-heading" });
  const apply = makeNode("button", { type: "submit", class: "portal-button db-panel-apply" });
  const clear = makeNode("a", { href: "/user/database/ds?reset=1", class: "portal-button secondary db-panel-clear" });
  const scrollRegion = makeNode("div", { class: "db-panel-scroll" }, [heading, controls]);
  const actions = makeNode("div", { class: "db-panel-actions" }, [apply, clear]);
  const panelForm = makeNode("form", { "data-db-panel-form": "", method: "get" }, [scrollRegion, actions]);
  const drawerClose = makeNode("button", { type: "button", "data-db-filter-close": "", class: "db-filter-drawer-close" });
  const drawerHead = makeNode("div", { class: "db-filter-drawer-head" }, [
    makeNode("span", { class: "db-filter-drawer-title" }),
    drawerClose,
  ]);
  const filterPanel = makeNode("div", { "data-db-filter-panel": "", role: "group", "aria-label": "Filtry", class: "db-filter-panel" }, [
    drawerHead,
    panelForm,
  ]);
  const scrim = makeNode("div", { "data-db-filter-scrim": "", class: "db-filter-scrim" });
  const filterSummary = makeNode("summary", { class: "db-tool-button db-filters-trigger" });
  const filters = makeNode("details", { "data-db-filters": "", id: "db-filters" }, [filterSummary, scrim, filterPanel]);
  filters.open = false;

  /* One column menu, so `Esc` precedence between the menu and the panel is a
     real collision rather than a description of one. */
  const menuOp = field("select", "op__trip_start", "eq", { "data-db-op": "" });
  const menuControls = makeNode("div", { "data-db-filter-controls": "", "data-db-family": "date" }, [menuOp]);
  const menuApply = makeNode("button", { type: "submit" });
  const menuForm = makeNode("form", { "data-db-col-form": "", method: "get" }, [menuControls, menuApply]);
  const menuPanel = makeNode("div", { class: "db-col-panel" }, [menuForm]);
  const menuSummary = makeNode("summary", {});
  const menu = makeNode("details", { "data-db-col-menu": "", id: "dbcol-trip_start", "data-db-column": "trip_start" }, [
    menuSummary,
    menuPanel,
  ]);
  menu.open = false;

  const colsSummary = makeNode("summary", { class: "db-columns-trigger" });
  const columnPanel = makeNode("details", { "data-db-columns": "", id: "db-columns" }, [colsSummary]);
  columnPanel.open = false;

  const th = makeNode("th", { scope: "col", "data-db-column": "trip_start", "aria-sort": "none" }, [menu]);
  const table = makeNode("table", { class: "db-table" }, [makeNode("thead", {}, [makeNode("tr", {}, [th])])]);
  const scroller = makeNode("div", { "data-db-scroll": "" }, [table]);
  const viewport = makeNode("div", { class: "db-table-viewport" }, [scroller]);
  const toolbar = makeNode("div", { class: "db-toolbar" }, [filters, columnPanel]);

  const children = [toolbar, viewport];
  let rowHeading = null;
  let rowPanel = null;
  if (opts.rowDetail) {
    rowHeading = makeNode("h3", { "data-db-row-heading": "", tabindex: "-1", class: "db-row-detail-title" });
    const rowClose = makeNode("a", { href: "/user/database/ds", "data-db-row-close": "", class: "db-row-close" });
    rowPanel = makeNode("aside", { "data-db-row-detail": "", class: "db-row-detail" }, [rowHeading, rowClose]);
    children.push(rowPanel);
  }
  const sheet = makeNode("div", { "data-db-sheet": "", class: "db-browser db-sheet" }, children);
  const secondary = makeNode("div", { class: "db-secondary" }, [makeNode("button", { type: "button" })]);

  /* The shell drawer lives outside the working area, exactly as the layout
     renders it. */
  const navToggle = makeNode("button", {
    type: "button",
    "data-nav-toggle": "",
    "aria-expanded": "false",
    "aria-controls": "lp-nav-drawer",
  });
  const navItem = makeNode("a", { href: "/user/reports", class: "lp-nav-drawer-item" });
  const navScrim = makeNode("div", { "data-nav-scrim": "", hidden: "" });
  const navDrawer = makeNode(
    "div",
    { "data-nav-drawer": "", id: "lp-nav-drawer", hidden: "", role: "dialog", "aria-modal": "true" },
    [navItem]
  );

  const root = makeNode("body", {}, [navToggle, advisory, sheet, secondary, navScrim, navDrawer]);
  attachQuery(root);

  return {
    root, advisory, advisoryTitle, accept, sheet, secondary,
    filters, filterSummary, filterPanel, scrim, drawerClose, panelForm, staged, exact,
    apply, clear, heading, menu, menuSummary, columnPanel, rowPanel, rowHeading,
    navToggle, navDrawer, navScrim,
  };
}

/* ------------------------------------------------------------- environment - */

function makeEnvironment(fixture, options) {
  const opts = options || {};
  const listeners = {};
  const submissions = [];
  const mediaState = { width: opts.width === undefined ? 1440 : opts.width };
  const mediaListeners = [];

  global.__focused = null;

  global.document = {
    nodeType: 9,
    readyState: "complete",
    body: fixture.root,
    addEventListener: (type, fn) => { (listeners[type] || (listeners[type] = [])).push(fn); },
    querySelector: (sel) => queryAll(fixture.root, sel)[0] || null,
    querySelectorAll: (sel) => queryAll(fixture.root, sel),
    getElementById: (id) => queryAll(fixture.root, "[id]").filter((n) => n.getAttribute("id") === id)[0] || null,
    get activeElement() { return global.__focused; },
  };

  function evaluate(query) {
    const max = /max-width:\s*(\d+)px/.exec(query);
    const min = /min-width:\s*(\d+)px/.exec(query);
    if (max) return mediaState.width <= parseInt(max[1], 10);
    if (min) return mediaState.width >= parseInt(min[1], 10);
    throw new Error("unsupported media query: " + query);
  }

  global.window = {
    addEventListener: (type, fn) => { (listeners[type] || (listeners[type] = [])).push(fn); },
    setTimeout: (fn) => { fn(); },
    sessionStorage: opts.storage || makeStorage("ok"),
    localStorage: makeStorage("ok"),
    matchMedia: opts.noMatchMedia
      ? undefined
      : function (query) {
          const entry = {
            media: query,
            get matches() { return evaluate(query); },
            addEventListener: (type, fn) => { if (type === "change") mediaListeners.push({ query, fn }); },
          };
          return entry;
        },
    location: { assign: (href) => { submissions.push("NAVIGATE:" + href); } },
    CSS: { escape: (value) => String(value) },
  };

  function fire(type, event) {
    (listeners[type] || []).forEach((fn) => fn(event));
  }

  function keyEvent(key, extra) {
    const event = Object.assign(
      {
        key: key,
        defaultPrevented: false,
        shiftKey: false,
        preventDefault() { this.defaultPrevented = true; },
      },
      extra || {}
    );
    return event;
  }

  function press(key, extra) {
    const event = keyEvent(key, extra);
    fire("keydown", event);
    return event;
  }

  function click(node) {
    (node._listeners.click || []).forEach((fn) => fn({ target: node, preventDefault() {} }));
  }

  function toggle(details, open) {
    details.open = open;
    if (open) {
      details.setAttribute("open", "");
    } else {
      details.removeAttribute("open");
    }
    (details._listeners.toggle || []).forEach((fn) => fn({}));
  }

  function resize(width) {
    mediaState.width = width;
    mediaListeners.forEach((entry) => entry.fn({ matches: evaluate(entry.query), media: entry.query }));
  }

  function restore() {
    fire("pageshow", { persisted: true });
  }

  return { fire, press, click, toggle, resize, restore, submissions, mediaState, listeners };
}

function loadModules(which) {
  (which || ["shell", "filters", "responsive"]).forEach((name) => {
    const file = name === "shell" ? SHELL : name === "filters" ? FILTERS : RESPONSIVE;
    /* eslint-disable no-eval */
    eval(fs.readFileSync(file, "utf8"));
  });
}

function focusedName(fixture) {
  const node = global.__focused;
  if (!node) return null;
  const named = {
    advisoryTitle: fixture.advisoryTitle,
    accept: fixture.accept,
    filterSummary: fixture.filterSummary,
    heading: fixture.heading,
    staged: fixture.staged,
    apply: fixture.apply,
    clear: fixture.clear,
    drawerClose: fixture.drawerClose,
    menuSummary: fixture.menuSummary,
    navToggle: fixture.navToggle,
    rowHeading: fixture.rowHeading,
  };
  const found = Object.keys(named).filter((key) => named[key] === node);
  return found.length ? found[0] : node._tag;
}

function snapshot(fixture, extra) {
  return Object.assign(
    {
      advisoryHidden: fixture.advisory.hasAttribute("hidden"),
      sheetHidden: fixture.sheet.hasAttribute("hidden"),
      secondaryHidden: fixture.secondary.hasAttribute("hidden"),
      filtersOpen: !!fixture.filters.open,
      menuOpen: !!fixture.menu.open,
      columnPanelOpen: !!fixture.columnPanel.open,
      stagedValue: fixture.staged.value,
      exactValue: fixture.exact.value,
      panelRole: fixture.filterPanel.getAttribute("role"),
      panelModal: fixture.filterPanel.getAttribute("aria-modal"),
      rowRole: fixture.rowPanel ? fixture.rowPanel.getAttribute("role") : null,
      rowModal: fixture.rowPanel ? fixture.rowPanel.getAttribute("aria-modal") : null,
      rowLabelledBy: fixture.rowPanel ? fixture.rowPanel.getAttribute("aria-labelledby") : null,
      navExpanded: fixture.navToggle.getAttribute("aria-expanded"),
      navDrawerHidden: fixture.navDrawer.hasAttribute("hidden"),
      focused: focusedName(fixture),
    },
    extra || {}
  );
}

const ACK_KEY = "logplatform.db.narrow-ack";

/* ------------------------------------------------------------- scenarios --- */

const SCENARIOS = {
  /* --- RSP-003 ---------------------------------------------------------- */

  "narrow-first-load": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 500 });
    loadModules();
    return snapshot(f, { ack: env.listeners && window.sessionStorage.getItem(ACK_KEY) });
  },

  "wide-load-shows-sheet": function () {
    const f = buildSheet({});
    makeEnvironment(f, { width: 1440 });
    loadModules();
    return snapshot(f, { ack: window.sessionStorage.getItem(ACK_KEY) });
  },

  "boundary-768-shows-sheet": function () {
    const f = buildSheet({});
    makeEnvironment(f, { width: 768 });
    loadModules();
    return snapshot(f, {});
  },

  "boundary-767-shows-advisory": function () {
    const f = buildSheet({});
    makeEnvironment(f, { width: 767 });
    loadModules();
    return snapshot(f, {});
  },

  "narrow-accept": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 500 });
    loadModules();
    env.click(f.accept);
    return snapshot(f, {
      ack: window.sessionStorage.getItem(ACK_KEY),
      submissions: env.submissions,
    });
  },

  /* Accepted, then widened, then narrowed again in the same session: the
     advisory must not reappear and must not flicker. */
  "narrow-accept-survives-band-changes": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 500 });
    loadModules();
    env.click(f.accept);
    env.resize(1440);
    const wide = f.advisory.hasAttribute("hidden");
    env.resize(500);
    return snapshot(f, { hiddenWhileWide: wide, ack: window.sessionStorage.getItem(ACK_KEY) });
  },

  /* Never accepted: widening resolves the advisory, narrowing brings it back. */
  "narrow-without-accept-follows-viewport": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 500 });
    loadModules();
    const first = f.advisory.hasAttribute("hidden");
    env.resize(1440);
    const wide = f.advisory.hasAttribute("hidden");
    env.resize(500);
    return snapshot(f, {
      hiddenOnLoad: first,
      hiddenWhileWide: wide,
      ack: window.sessionStorage.getItem(ACK_KEY),
    });
  },

  /* A storage that throws must not break the page, and the acknowledgement must
     not survive into a new document. */
  "narrow-storage-throws": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 500, storage: makeStorage("throws") });
    loadModules();
    const before = f.advisory.hasAttribute("hidden");
    env.click(f.accept);
    const after = f.advisory.hasAttribute("hidden");

    /* A new document with the same throwing storage: eligible again. */
    const f2 = buildSheet({});
    makeEnvironment(f2, { width: 500, storage: makeStorage("throws") });
    loadModules();
    return {
      hiddenOnLoad: before,
      hiddenAfterAccept: after,
      sheetHiddenAfterAccept: f.sheet.hasAttribute("hidden"),
      freshDocumentAdvisoryHidden: f2.advisory.hasAttribute("hidden"),
    };
  },

  "narrow-bfcache-restore": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 500 });
    loadModules();
    env.click(f.accept);
    env.restore();
    return snapshot(f, {});
  },

  /* No sheet on the page: the module is inert rather than throwing. */
  "no-sheet-is-inert": function () {
    const f = buildSheet({});
    f.sheet.removeAttribute("data-db-sheet");
    const env = makeEnvironment(f, { width: 500 });
    loadModules(["responsive"]);
    return { advisoryHidden: f.advisory.hasAttribute("hidden"), submissions: env.submissions };
  },

  /* --- RSP-002 filter drawer -------------------------------------------- */

  "drawer-role-follows-band": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    const drawer = {
      role: f.filterPanel.getAttribute("role"),
      modal: f.filterPanel.getAttribute("aria-modal"),
    };
    env.resize(1440);
    const docked = {
      role: f.filterPanel.getAttribute("role"),
      modal: f.filterPanel.getAttribute("aria-modal"),
    };
    env.resize(1023);
    const back = {
      role: f.filterPanel.getAttribute("role"),
      modal: f.filterPanel.getAttribute("aria-modal"),
    };
    return { drawer, docked, back };
  },

  "drawer-boundary-1024-is-docked": function () {
    const f = buildSheet({});
    makeEnvironment(f, { width: 1024 });
    loadModules();
    return snapshot(f, {});
  },

  /* Scrim dismissal KEEPS staged edits and returns focus to `Filtry`. */
  "drawer-scrim-keeps-staged-edits": function () {
    const f = buildSheet({ stagedValue: "Kowal", exactValue: "KRK-02" });
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    env.toggle(f.filters, true);
    f.staged.value = "Nowak";
    f.exact.value = "WAW-01";
    env.click(f.scrim);
    const closed = snapshot(f, { submissions: env.submissions });
    env.toggle(f.filters, true);
    return Object.assign(closed, {
      reopenedStaged: f.staged.value,
      reopenedExact: f.exact.value,
      reopenedOpen: !!f.filters.open,
    });
  },

  "drawer-close-button-keeps-staged-edits": function () {
    const f = buildSheet({ stagedValue: "Kowal" });
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    env.toggle(f.filters, true);
    f.staged.value = "Nowak";
    env.click(f.drawerClose);
    return snapshot(f, { submissions: env.submissions });
  },

  /* Escape on the drawer is the filter panel's own collapse: staged edits stay
     and focus returns to the trigger. */
  "drawer-escape-keeps-staged-edits": function () {
    const f = buildSheet({ stagedValue: "Kowal" });
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    env.toggle(f.filters, true);
    f.staged.value = "Nowak";
    const event = env.press("Escape");
    return snapshot(f, { prevented: event.defaultPrevented, submissions: env.submissions });
  },

  /* Crossing the breakpoint with staged edits open must not apply, reset or
     duplicate anything: it is the same DOM, relocated by CSS. */
  "drawer-breakpoint-crossing-preserves-staged": function () {
    const f = buildSheet({ stagedValue: "Kowal", exactValue: "KRK-02" });
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    env.toggle(f.filters, true);
    f.staged.value = "Nowak";
    f.exact.value = "WAW-01";
    env.resize(1440);
    const docked = snapshot(f, {});
    env.resize(760);
    return Object.assign(docked, {
      afterStaged: f.staged.value,
      afterExact: f.exact.value,
      afterOpen: !!f.filters.open,
      afterRole: f.filterPanel.getAttribute("role"),
      submissions: env.submissions,
      formCount: queryAll(f.root, "[data-db-panel-form]").length,
      stagedFieldCount: queryAll(f.root, '[name="filter__driver_name"]').length,
      exactFieldCount: queryAll(f.root, '[name="filter_exact__driver_name"]').length,
    });
  },

  /* --- focus containment ------------------------------------------------ */

  "drawer-traps-focus": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    env.toggle(f.filters, true);

    /* Tab from outside the drawer is pulled into it. */
    global.__focused = f.navToggle;
    const entering = env.press("Tab");
    const entered = focusedName(f);

    /* Tab on the last control wraps to the first. */
    global.__focused = f.clear;
    const wrapping = env.press("Tab");
    const wrapped = focusedName(f);

    /* Shift+Tab on the first control wraps to the last. */
    global.__focused = f.drawerClose;
    const backwards = env.press("Tab", { shiftKey: true });
    const backwardTarget = focusedName(f);

    return {
      entering: entering.defaultPrevented,
      entered,
      wrapping: wrapping.defaultPrevented,
      wrapped,
      backwards: backwards.defaultPrevented,
      backwardTarget,
    };
  },

  "docked-panel-does-not-trap-focus": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 1440 });
    loadModules();
    env.toggle(f.filters, true);
    global.__focused = f.clear;
    const event = env.press("Tab");
    return { prevented: event.defaultPrevented, focused: focusedName(f) };
  },

  "drawer-trap-yields-to-nav-drawer": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    env.toggle(f.filters, true);
    env.click(f.navToggle);
    global.__focused = f.clear;
    const event = env.press("Tab");
    return {
      navDrawerHidden: f.navDrawer.hasAttribute("hidden"),
      navExpanded: f.navToggle.getAttribute("aria-expanded"),
      prevented: event.defaultPrevented,
    };
  },

  /* --- row detail overlay ----------------------------------------------- */

  "row-detail-overlay-is-a-dialog": function () {
    const f = buildSheet({ rowDetail: true });
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    const overlay = snapshot(f, {});
    env.resize(1440);
    const docked = {
      rowRole: f.rowPanel.getAttribute("role"),
      rowModal: f.rowPanel.getAttribute("aria-modal"),
      rowLabelledBy: f.rowPanel.getAttribute("aria-labelledby"),
    };
    return { overlay, docked };
  },

  "row-detail-overlay-traps-focus": function () {
    const f = buildSheet({ rowDetail: true });
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    global.__focused = f.navToggle;
    const event = env.press("Tab");
    return { prevented: event.defaultPrevented, focused: focusedName(f) };
  },

  "row-detail-docked-does-not-trap": function () {
    const f = buildSheet({ rowDetail: true });
    const env = makeEnvironment(f, { width: 1440 });
    loadModules();
    global.__focused = f.navToggle;
    const event = env.press("Tab");
    return { prevented: event.defaultPrevented };
  },

  /* --- Escape precedence ------------------------------------------------ */

  /* The navigation drawer is the topmost layer: one press closes it and nothing
     underneath collapses with it. */
  "escape-nav-drawer-wins": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    env.toggle(f.filters, true);
    env.click(f.navToggle);
    const event = env.press("Escape");
    return snapshot(f, { prevented: event.defaultPrevented });
  },

  /* A column menu owns `Esc` before the filter panel does. */
  "escape-column-menu-before-filter-panel": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    env.toggle(f.filters, true);
    env.toggle(f.menu, true);
    const event = env.press("Escape");
    return snapshot(f, { prevented: event.defaultPrevented });
  },

  /* So does the column panel. */
  "escape-column-panel-before-filter-panel": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    env.toggle(f.filters, true);
    env.toggle(f.columnPanel, true);
    const event = env.press("Escape");
    return snapshot(f, { prevented: event.defaultPrevented });
  },

  /* With nothing above it open, the filter drawer is what closes. */
  "escape-closes-filter-drawer-only": function () {
    const f = buildSheet({ stagedValue: "Kowal" });
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    env.toggle(f.filters, true);
    const event = env.press("Escape");
    return snapshot(f, { prevented: event.defaultPrevented });
  },

  /* No product surface is open: the key is left alone for the browser. */
  /* Back onto a page whose nav drawer was left open must not restore a stuck
     overlay or a stale `aria-expanded`. */
  "nav-drawer-resets-on-bfcache-restore": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    env.click(f.navToggle);
    const opened = {
      hidden: f.navDrawer.hasAttribute("hidden"),
      expanded: f.navToggle.getAttribute("aria-expanded"),
    };
    global.__focused = f.navDrawer.querySelector("a[href]");
    env.restore();
    return {
      opened,
      hidden: f.navDrawer.hasAttribute("hidden"),
      scrimHidden: f.navScrim.hasAttribute("hidden"),
      expanded: f.navToggle.getAttribute("aria-expanded"),
      focused: focusedName(f),
    };
  },

  "escape-with-no-layer-is-not-consumed": function () {
    const f = buildSheet({});
    const env = makeEnvironment(f, { width: 900 });
    loadModules();
    const event = env.press("Escape");
    return { prevented: event.defaultPrevented };
  },
};

/* ------------------------------------------------------------------ main --- */

const name = process.argv[2];
if (!SCENARIOS[name]) {
  process.stderr.write("unknown scenario: " + name + "\n");
  process.exit(2);
}
process.stdout.write(JSON.stringify(SCENARIOS[name]()));
