/* Log Platform — Database Explorer responsive and accessibility completion
 * (approved stage S10: `RSP-001`–`RSP-003`, `RS-1`–`RS-11`, `AC-1`–`AC-14`).
 *
 * The responsive layout itself is CSS. This module owns only the three things a
 * stylesheet cannot express:
 *
 *   1. `RSP-003` — the below-768 px advisory, its `Otwórz mimo to` escape hatch
 *      and the session-scoped memory of that decision;
 *   2. `RSP-002` — the filter drawer's modal semantics: scrim dismissal that
 *      KEEPS staged edits, focus containment while it is open, and the
 *      `role="group"` → `role="dialog"` swap that is only true in drawer mode;
 *   3. the row-detail panel's overlay semantics at the same band, where the
 *      approved contract makes it a trapped layer rather than a docked one.
 *
 * What it deliberately does NOT do:
 *
 *   - it never moves a control between DOM positions. The filter panel is
 *     relocated by CSS, so there is one panel, one form and one staged filter
 *     state at every width. Crossing a breakpoint cannot apply, discard or
 *     duplicate a staged edit, and no filter parameter is ever carried by two
 *     successful controls at once;
 *   - it holds no authorization, reads no dataset value, and issues no request.
 *     The `RSP-003` acknowledgement is a presentation preference and is not a
 *     security state;
 *   - it does not listen to `resize`. Band changes arrive through `matchMedia`,
 *     so nothing runs while a window is being dragged;
 *   - it does not sniff the browser or the device. Width is the only input.
 *
 * Escape is not handled here. The filter drawer's `Esc` is the filter panel's
 * own collapse (which keeps staged edits) and the row panel's `Esc` is its own
 * close; both already return focus to the control that opened them.
 */
(function () {
  "use strict";

  /* The approved bands. `bp/below-min` is <768 px, so 767 px is the last width
     that shows the advisory and 768 px is the first that does not. `RSP-002`
     covers `bp/tablet-portrait` and below, i.e. everything up to 1023 px. */
  var NARROW_QUERY = "(max-width: 767px)";
  var DRAWER_QUERY = "(max-width: 1023px)";

  /* One namespaced key holding one non-sensitive acknowledgement. No dataset,
     no client, no business value, and nothing a security decision reads. */
  var ACK_KEY = "logplatform.db.narrow-ack";

  var FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]),' +
    ' textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

  var narrowMedia = null;
  var drawerMedia = null;
  /* Fallback for a storage that is unavailable or throws: the acknowledgement
     then lives for this document only, and the next load shows the advisory
     again. Failing towards showing it is the safe direction. */
  var acceptedInMemory = false;

  /* ------------------------------------------------------------- utilities */

  function matches(query) {
    if (!window.matchMedia) {
      /* No matchMedia (a very old engine, or a test harness): treat the
         viewport as wide. The advisory and the drawer are both additive, and
         applying them everywhere would be the larger regression. */
      return false;
    }
    return window.matchMedia(query).matches;
  }

  function focusables(root) {
    if (!root || !root.querySelectorAll) {
      return [];
    }
    var found = root.querySelectorAll(FOCUSABLE);
    var out = [];
    for (var i = 0; i < found.length; i += 1) {
      var node = found[i];
      /* A control the layout has removed must not be reachable by Tab. */
      if (node.hasAttribute && node.hasAttribute("hidden")) {
        continue;
      }
      if (node.offsetParent === null && node.getClientRects && !node.getClientRects().length) {
        continue;
      }
      out.push(node);
    }
    return out;
  }

  function focusFirst(root, fallback) {
    var items = focusables(root);
    if (items.length && items[0].focus) {
      items[0].focus();
      return true;
    }
    if (fallback && fallback.focus) {
      if (!fallback.hasAttribute("tabindex")) {
        fallback.setAttribute("tabindex", "-1");
      }
      fallback.focus();
      return true;
    }
    return false;
  }

  /* ------------------------------------------------- RSP-003 session memory */

  function readAccepted() {
    if (acceptedInMemory) {
      return true;
    }
    try {
      return window.sessionStorage.getItem(ACK_KEY) === "1";
    } catch (error) {
      /* Private mode, a blocked store or a throwing quota: the advisory simply
         remains eligible. Nothing here is allowed to break the page. */
      return false;
    }
  }

  function writeAccepted() {
    acceptedInMemory = true;
    try {
      window.sessionStorage.setItem(ACK_KEY, "1");
    } catch (error) {
      /* Kept in memory for this document only. A new session — and a reload
         with an unavailable store — shows the advisory again, which is the
         approved failure direction. */
    }
  }

  /* ------------------------------------------------------- RSP-003 advisory */

  function sheet() {
    return document.querySelector("[data-db-sheet]");
  }

  function secondary() {
    return document.querySelector(".db-secondary");
  }

  function advisory() {
    return document.querySelector("[data-db-narrow-advisory]");
  }

  function setHidden(node, hidden) {
    if (!node) {
      return;
    }
    if (hidden) {
      node.setAttribute("hidden", "");
    } else {
      node.removeAttribute("hidden");
    }
  }

  /* The advisory and the working sheet are mutually exclusive, and the one that
     is not on screen carries `hidden` — so it leaves the tab order and the
     accessibility tree together. Neither is ever merely visually suppressed. */
  function applyNarrow(options) {
    var panel = advisory();
    if (!panel) {
      return;
    }
    var showAdvisory = matches(NARROW_QUERY) && !readAccepted();
    var wasHidden = panel.hasAttribute("hidden");
    setHidden(panel, !showAdvisory);
    setHidden(sheet(), showAdvisory);
    setHidden(secondary(), showAdvisory);
    if (showAdvisory && wasHidden && options && options.moveFocus) {
      var heading = panel.querySelector("#db-narrow-title");
      if (heading && heading.focus) {
        heading.focus();
      }
    }
  }

  function initNarrow() {
    var panel = advisory();
    if (!panel) {
      return;
    }
    var accept = panel.querySelector("[data-db-narrow-accept]");
    if (accept) {
      accept.addEventListener("click", function () {
        writeAccepted();
        applyNarrow({ moveFocus: false });
        /* The escape hatch must land the user in the working layout, not in a
           screen they can no longer see. Focus follows to the sheet. */
        var target = sheet();
        if (target) {
          focusFirst(target, target);
        }
      });
    }

    if (window.matchMedia) {
      narrowMedia = window.matchMedia(NARROW_QUERY);
      var onChange = function () { applyNarrow({ moveFocus: true }); };
      if (narrowMedia.addEventListener) {
        narrowMedia.addEventListener("change", onChange);
      } else if (narrowMedia.addListener) {
        narrowMedia.addListener(onChange);
      }
    }
    applyNarrow({ moveFocus: true });
  }

  /* ----------------------------------------------------- RSP-002 filter drawer */

  function filterDetails() {
    return document.querySelector("[data-db-filters]");
  }

  function filterPanel() {
    return document.querySelector("[data-db-filter-panel]");
  }

  function drawerMode() {
    return matches(DRAWER_QUERY);
  }

  /* `role="dialog"` is true only while the panel actually is an overlay. At the
     docked widths it is part of the page and stays a plain labelled group;
     claiming a dialog there would be a lie a screen reader acts on. */
  function syncFilterRole() {
    var panel = filterPanel();
    if (!panel) {
      return;
    }
    if (drawerMode()) {
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
    } else {
      panel.setAttribute("role", "group");
      panel.removeAttribute("aria-modal");
    }
  }

  function filterDrawerOpen() {
    var details = filterDetails();
    return !!(details && details.open && drawerMode());
  }

  /* Dismissal keeps staged edits — the drawer holds staged work, and only the
     column menu discards (`INTERACTION_SPEC.md` §3). Closing the <details> hides
     the same form it was already holding, so every staged value, every
     `filter_exact__` selection and every picker choice is still there when it
     reopens. */
  function closeFilterDrawer() {
    var details = filterDetails();
    if (!details || !details.open) {
      return false;
    }
    details.open = false;
    var trigger = details.querySelector("summary");
    if (trigger && trigger.focus) {
      trigger.focus();
    }
    return true;
  }

  function initFilterDrawer() {
    var details = filterDetails();
    if (!details) {
      return;
    }

    var scrim = details.querySelector("[data-db-filter-scrim]");
    if (scrim) {
      scrim.addEventListener("click", function () { closeFilterDrawer(); });
    }
    var close = details.querySelector("[data-db-filter-close]");
    if (close) {
      close.addEventListener("click", function () { closeFilterDrawer(); });
    }

    details.addEventListener("toggle", function () {
      if (!details.open || !drawerMode()) {
        return;
      }
      /* Focus enters the drawer at its heading, which is also its accessible
         name. The panel's own `toggle` handler in data-grid-filters.js already
         places focus on that heading; this only makes sure the drawer form is
         reachable when the heading is not focusable for some reason. */
      var panel = filterPanel();
      var heading = panel && panel.querySelector(".db-panel-heading");
      if (heading && heading.focus) {
        heading.setAttribute("tabindex", "-1");
        heading.focus();
      } else {
        focusFirst(panel, panel);
      }
    });

    if (window.matchMedia) {
      drawerMedia = window.matchMedia(DRAWER_QUERY);
      var onChange = function () { syncFilterRole(); syncRowDetail(); };
      if (drawerMedia.addEventListener) {
        drawerMedia.addEventListener("change", onChange);
      } else if (drawerMedia.addListener) {
        drawerMedia.addListener(onChange);
      }
    }
    syncFilterRole();
  }

  /* ------------------------------------------------- row detail overlay (S6) */

  function rowPanel() {
    return document.querySelector("[data-db-row-detail]");
  }

  /* Docked, the row panel is part of the page and is not trapped. As a
     full-width overlay it is a layer over the table, and the approved contract
     makes it a trapped dialog named by its visible heading. The identity model,
     the close/traverse links and the history behaviour are untouched: this only
     states what the panel currently is. */
  function syncRowDetail() {
    var panel = rowPanel();
    if (!panel) {
      return;
    }
    var heading = panel.querySelector("[data-db-row-heading]");
    if (drawerMode()) {
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      if (heading) {
        if (!heading.id) {
          heading.id = "db-row-detail-title";
        }
        panel.setAttribute("aria-labelledby", heading.id);
      }
    } else {
      panel.removeAttribute("role");
      panel.removeAttribute("aria-modal");
      panel.removeAttribute("aria-labelledby");
    }
  }

  /* ---------------------------------------------------- focus containment */

  /* One handler for every trapped layer this module owns, because two would
     fight over the same Tab. The nav drawer is a shell layer with its own trap
     and sits above both of these, so it is yielded to rather than contested. */
  function trappedSurface() {
    if (document.querySelector("[data-nav-drawer]:not([hidden])")) {
      return null;
    }
    if (filterDrawerOpen()) {
      return filterPanel();
    }
    if (drawerMode()) {
      return rowPanel();
    }
    return null;
  }

  function initTrap() {
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Tab" && event.keyCode !== 9) {
        return;
      }
      var surface = trappedSurface();
      if (!surface) {
        return;
      }
      var items = focusables(surface);
      if (!items.length) {
        return;
      }
      var first = items[0];
      var last = items[items.length - 1];
      var current = document.activeElement;
      if (!surface.contains || !surface.contains(current)) {
        event.preventDefault();
        first.focus();
        return;
      }
      if (event.shiftKey && current === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && current === last) {
        event.preventDefault();
        first.focus();
      }
    });
  }

  /* ------------------------------------------------------------------ init */

  function init() {
    if (!document.querySelector("[data-db-sheet]")) {
      return;
    }
    initNarrow();
    initFilterDrawer();
    syncRowDetail();
    initTrap();

    /* Back/Forward may restore this exact DOM. Re-deriving the band state on
       restore is what keeps a restored page from showing a stale advisory, a
       stuck overlay or a sheet that is still hidden. */
    window.addEventListener("pageshow", function (event) {
      if (event && event.persisted) {
        applyNarrow({ moveFocus: false });
        syncFilterRole();
        syncRowDetail();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
