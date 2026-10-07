/* Log Platform — Database Explorer column-centric filtering (DB-005).
 *
 * Everything this file touches already works without it. The column menus and
 * the filter panel are <details> elements containing plain GET forms and links,
 * so with scripting off a user can still sort, filter, remove one filter and
 * clear them all. This module adds only what markup cannot express:
 *
 *   1. one menu open at a time, and dismissal that DISCARDS pending edits
 *      (`Esc`, outside click, table scroll) — INTERACTION_SPEC.md §3;
 *   2. focus into the menu on open and back to its header on close — §2.3;
 *   3. operator-aware value controls, so a `puste` filter shows no value field
 *      and `od–do` shows two;
 *   4. `Znajdź kolumnę` filtering of the panel's add-a-filter list;
 *   5. single-submit protection, so one Apply cannot be sent twice.
 *
 * It owns no authorization, no operator allowlist and no SQL semantics. The
 * server validates every parameter it produces, exactly as it validates a
 * hand-written URL.
 *
 * The filter panel deliberately does NOT discard on dismissal: its edits are
 * staged and reopening must show them again (RESPONSIVE_SPEC.md §, INT §3).
 *
 * UI-20260831-01 — staying open while the user refines:
 *
 *   6. a click INSIDE an open menu keeps it open. It used to close it: the
 *      outside-click handler passed the menu ELEMENT to `closeAllMenus`, which
 *      compares against the menu RECORD, so the "except" never matched and every
 *      menu was dismissed on every click, including the one being used;
 *   7. the panel dismisses on an outside click too, which it never did;
 *   8. an Apply, a sort or a preset navigates, and the surface the user was
 *      working in is reopened on the page that comes back. Nothing about the
 *      request changes — the memory is one sessionStorage entry, never a
 *      parameter, so saved views, chips and the no-script path are untouched.
 */
(function () {
  "use strict";

  /* ------------------------------------------------------------ utilities */

  function closest(node, predicate) {
    var current = node;
    while (current && current !== document) {
      if (current.nodeType === 1 && predicate(current)) {
        return current;
      }
      current = current.parentNode;
    }
    return null;
  }

  function hasAttr(el, name) {
    return !!(el && el.hasAttribute && el.hasAttribute(name));
  }

  function all(selector, root) {
    var found = (root || document).querySelectorAll(selector);
    return Array.prototype.slice.call(found);
  }

  /* -------------------------------------------------------- value controls */

  /* Which value control belongs to which operator. The server ignores the
     fields that do not belong to the submitted operator — they are named
     separately for exactly that reason — so this is presentation only. */
  function syncValueControls(container) {
    var select = container.querySelector("[data-db-op]");
    if (!select) {
      return;
    }
    var operator = select.value || "";
    var wantsRange = operator === "between" || operator === "range";
    var wantsMulti = operator === "in";
    var wantsNone = operator === "blank" || operator === "is_true" || operator === "is_false" || operator === "";
    var single = container.querySelector("[data-db-value-single]");
    var range = container.querySelector("[data-db-value-range]");
    var multi = container.querySelector("[data-db-value-multi]");
    var note = container.querySelector("[data-db-blank-note]");
    if (single) {
      setHidden(single, wantsRange || wantsMulti || wantsNone);
    }
    if (range) {
      setHidden(range, !wantsRange);
    }
    if (multi) {
      setHidden(multi, !wantsMulti);
    }
    if (note) {
      setHidden(note, operator !== "blank");
    }
  }

  function setHidden(element, hidden) {
    if (hidden) {
      element.setAttribute("hidden", "");
    } else {
      element.removeAttribute("hidden");
    }
  }

  function syncAllValueControls(root) {
    all("[data-db-filter-controls]", root).forEach(syncValueControls);
  }

  /* ---------------------------------------------------- focus without scroll */

  /* Moving focus must never dismiss the surface the focus serves.
   *
   * A bare `focus()` on an element that is out of view scrolls its nearest
   * scrollable ancestors to reveal it — and one of those is the grid, whose
   * scroll INTERACTION_SPEC §3 reads as "the user scrolled away" and dismisses
   * the open menu. Opening a column menu near the bottom of the viewport was
   * enough: the menu focused its first control, the browser scrolled to show it,
   * and the menu closed on the same gesture that opened it.
   *
   * `preventScroll` alone would fix the dismissal and leave the user typing into
   * something they cannot see. So the reveal is done here instead, against the
   * PANEL's own scrollbar — `.db-col-panel` is `max-height: 60vh; overflow-y:
   * auto` — which is not the grid and therefore dismisses nothing. The walk
   * stops dead at `[data-db-scroll]`: that container is never scrolled to reveal
   * anything. */
  /* `scrollHeight > clientHeight` alone is NOT "this element scrolls": it is
     true of any element whose content overflows, `overflow: visible` included.
     Walking up from a calendar day, the first such ancestor is `.db-date-calendar`
     — taller than its box and not scrollable — so the walk stopped there and
     wrote `scrollTop` to an element that ignores it. The panel was never
     reached, and the reveal silently did nothing.

     A box must therefore be DECLARED scrollable and ACTUALLY overflowing. */
  function isScrollableBox(element) {
    var overflow = "";
    try {
      if (window.getComputedStyle) {
        var style = window.getComputedStyle(element);
        overflow = (style && (style.overflowY || style.overflow)) || "";
      }
    } catch (error) {
      overflow = "";
    }
    if (overflow !== "auto" && overflow !== "scroll" && overflow !== "overlay") {
      return false;
    }
    return typeof element.scrollHeight === "number" &&
      typeof element.clientHeight === "number" &&
      element.scrollHeight > element.clientHeight;
  }

  function panelScroller(node) {
    var current = node && node.parentNode;
    while (current && current.nodeType === 1) {
      if (hasAttr(current, "data-db-scroll")) {
        return null;
      }
      if (isScrollableBox(current)) {
        return current;
      }
      current = current.parentNode;
    }
    return null;
  }

  /* Bring a node into view using the PANEL's scrollbar only. Separate from
     focusing, because a field the browser has already focused must be revealed
     without being focused a second time. */
  function revealWithinPanel(node) {
    var box = panelScroller(node);
    if (!box || !node || !node.getBoundingClientRect || !box.getBoundingClientRect) {
      return;
    }
    var target = node.getBoundingClientRect();
    var frame = box.getBoundingClientRect();
    var delta = 0;
    if (target.top < frame.top) {
      delta = target.top - frame.top;            /* negative: scroll up */
    } else if (target.bottom > frame.bottom) {
      delta = target.bottom - frame.bottom;      /* positive: scroll down */
    }
    if (!delta) {
      return;
    }
    /* Clamped to the scrollbox's real range. Unclamped, a request to scroll past
       the end is silently truncated by the engine and the element can end up
       pushed out of the opposite edge — the panel sat at its exact maximum with
       the field at -132, revealed off the TOP. */
    var limit = (typeof box.scrollHeight === "number" && typeof box.clientHeight === "number")
      ? Math.max(0, box.scrollHeight - box.clientHeight)
      : null;
    var wanted = box.scrollTop + delta;
    if (limit !== null) {
      wanted = Math.max(0, Math.min(limit, wanted));
    } else if (wanted < 0) {
      wanted = 0;
    }
    box.scrollTop = wanted;
  }

  /* Focus, and let the browser reveal what it focused.
   *
   * This used to pass `preventScroll` and reveal against the panel instead. That
   * stopped the dismissal but left the user focused on a control they could not
   * see: the panel is itself clipped by the viewport, so revealing WITHIN it
   * cannot bring anything into view that the viewport is cutting off. Now that a
   * reveal-scroll no longer dismisses — it is not a gesture aimed at the grid —
   * the browser's own reveal is simply allowed to do its job, and the panel
   * scrollbar only handles what the panel itself is hiding. */
  /* Focus without letting the browser scroll the grid to reveal.
   *
   * The reveal was allowed for one round, on the theory that it was the only
   * thing that could bring a below-fold field into view. It cannot: the panel is
   * anchored under a sticky header and does not move with the grid, so the
   * reveal scrolled the table roughly 150px per keystroke and revealed nothing.
   * With the panel made to fit, the panel's own scrollbar can reach all of its
   * content, and the grid never needs to move at all. */
  function focusVisibly(node) {
    if (!node || !node.focus) {
      return;
    }
    try {
      node.focus({ preventScroll: true });
    } catch (error) {
      node.focus();
    }
    revealWithinPanel(node);
  }

  window.dbGridRevealWithinPanel = revealWithinPanel;

  /* Shared with data-grid-daterange.js, which focuses calendar days and rejected
     fields inside the same panel and must not dismiss it either. */
  window.dbGridFocusVisibly = focusVisibly;

  /* ------------------------------------------- what the gesture was aimed at */

  /* §3 dismisses on a table scroll. But not every scroll of the table is the
     user scrolling the table:
     
       - focusing an element below the fold makes the browser scroll to reveal
         it, and the position changes SYNCHRONOUSLY inside `focus()` while the
         event arrives a tick later;
       - once the caret is below the fold the browser reveals it again on EVERY
         `input` — one scroll per keystroke, not a one-time event;
       - expanding the calendar shifts the layout and the browser compensates.
     
     None of those is a gesture at the grid, and no one-shot can cover them
     because they recur. What separates them is not timing and not where focus
     sits — the open handler puts focus in the panel immediately, so
     focus-within would exempt everything — but WHAT THE GESTURE WAS AIMED AT.
     
       dismiss  <=>  the last user gesture was directed at the grid
     
     A wheel over the table, a drag of its scrollbar, arrows with a cell focused:
     aimed at the grid, and they dismiss exactly as §3 requires. Typing into a
     panel field, walking the calendar, and every reveal the browser performs on
     their behalf: aimed inside the panel, and they never do. */
  var lastGestureAtGrid = true;

  function aimedAtGrid(target) {
    var inGrid = closest(target, function (el) { return hasAttr(el, "data-db-scroll"); });
    if (!inGrid) {
      return false;
    }
    /* The menus and the filter panel live INSIDE the scrolling container, so
       "inside the grid" is not enough to mean "aimed at the grid". */
    var inMenu = closest(target, function (el) { return hasAttr(el, "data-db-col-menu"); });
    var inPanel = closest(target, function (el) { return hasAttr(el, "data-db-filters"); });
    return !inMenu && !inPanel;
  }

  function initGestureTracking() {
    ["wheel", "pointerdown", "touchstart", "keydown"].forEach(function (type) {
      document.addEventListener(type, function (event) {
        lastGestureAtGrid = aimedAtGrid(event.target);
      }, true);
    });
  }

  /* ------------------------------------------------------ click provenance */

  /* THE ONE PLACE that decides which transient surface a click came from.
   *
   * Walking `parentNode` from `event.target` is not enough on its own. A control
   * that re-renders itself inside its OWN click handler — the range calendar
   * rebuilds its grid on every day click — has already replaced the clicked node
   * by the time this document-level handler runs. The walk from a detached node
   * reaches nothing, the click is misread as "outside", and the surface the user
   * is working in is dismissed underneath them.
   *
   * The pointerdown that began the same gesture happened while the node was
   * still attached, so that is what we fall back to. This lives in the shared
   * classifier rather than in the calendar because ANY future control that
   * rebuilds itself on click would otherwise reintroduce the same defect. */
  var lastPointerSurface = null;

  function surfaceOf(node) {
    return {
      menu: closest(node, function (el) { return hasAttr(el, "data-db-col-menu"); }),
      panel: closest(node, function (el) { return hasAttr(el, "data-db-filters"); }),
    };
  }

  function clickSurface(event) {
    var target = event ? event.target : null;
    /* `isConnected === false` is the detached case specifically. An engine that
       does not expose the property leaves it `undefined`, which keeps the
       original behaviour rather than silently trusting a stale pointerdown. */
    if (target && target.isConnected === false && lastPointerSurface) {
      return lastPointerSurface;
    }
    return surfaceOf(target);
  }

  function initPointerTracking() {
    /* `keydown` is in the list because a button activated with Enter or Space
       produces a click with no pointer event before it. Without it a keyboard
       user activating a re-rendering control would fall back to whatever the
       last pointer gesture happened to be — which is exactly the stale
       provenance this classifier exists to avoid. */
    ["pointerdown", "mousedown", "touchstart", "keydown"].forEach(function (type) {
      document.addEventListener(type, function (event) {
        lastPointerSurface = surfaceOf(event.target);
      }, true);
    });
  }

  /* --------------------------------------------------------- column menus */

  /* The server-rendered state of a menu's form, captured once. Dismissing a
     menu must leave the applied filter exactly as it was, so a pending edit is
     rolled back rather than merely hidden. */
  function snapshotForm(form) {
    var state = [];
    all("select, input, textarea", form).forEach(function (field) {
      state.push([field, field.type === "checkbox" || field.type === "radio" ? field.checked : field.value]);
    });
    return state;
  }

  function restoreForm(state) {
    state.forEach(function (pair) {
      var field = pair[0];
      if (field.type === "checkbox" || field.type === "radio") {
        field.checked = pair[1];
      } else {
        field.value = pair[1];
      }
    });
  }

  var menus = [];

  /* `menus` holds records, not elements. Every caller that starts from a DOM
     node has to come through here: passing the element straight to
     `closeAllMenus` silently matches nothing, which is what made a click inside
     an open menu dismiss it. */
  function menuRecordFor(element) {
    for (var index = 0; index < menus.length; index += 1) {
      if (menus[index].element === element) {
        return menus[index];
      }
    }
    return null;
  }

  function closeMenu(menu, options) {
    if (!menu.element.open) {
      return;
    }
    if (menu.snapshot) {
      restoreForm(menu.snapshot);
      syncAllValueControls(menu.element);
    }
    menu.element.open = false;
    if (options && options.returnFocus) {
      focusVisibly(menu.element.querySelector("summary"));
    }
  }

  function closeAllMenus(except, options) {
    menus.forEach(function (menu) {
      if (menu !== except) {
        closeMenu(menu, options);
      }
    });
  }

  function initMenus() {
    menus = all("[data-db-col-menu]").map(function (element) {
      var form = element.querySelector("[data-db-col-form]");
      return { element: element, form: form, snapshot: null };
    });

    menus.forEach(function (menu) {
      menu.element.addEventListener("toggle", function () {
        if (!menu.element.open) {
          return;
        }
        /* Re-snapshot on every open: the applied state is whatever the server
           last rendered plus anything a previous discard restored. */
        menu.snapshot = menu.form ? snapshotForm(menu.form) : null;
        closeAllMenus(menu);
        syncAllValueControls(menu.element);
        focusVisibly(menu.element.querySelector(".db-col-panel a, .db-col-panel select, .db-col-panel input, .db-col-panel textarea, .db-col-panel button"));
        placeMenu(menu.element);
      });
    });

    document.addEventListener("click", function (event) {
      var inside = clickSurface(event).menu;
      if (!inside) {
        /* The panel's add-a-filter link opens a column menu. Without this the
           very same click would close the menu it had just opened. */
        var adder = closest(event.target, function (el) {
          return hasAttr(el, "data-db-add-column");
        });
        if (adder && document.getElementById) {
          inside = document.getElementById("dbcol-" + adder.getAttribute("data-db-add-column"));
        }
      }
      closeAllMenus(menuRecordFor(inside));
    });

    /* Scrolling the table dismisses an open menu and discards its pending
       edits, per INTERACTION_SPEC.md §3. */
    all("[data-db-scroll]").forEach(function (scroller) {
      scroller.addEventListener("scroll", function () {
        /* One exception, and only one: the scroll restored immediately after an
           apply is the browser catching up, not the user scrolling away. It
           would otherwise close the very menu the restore had just reopened.
           The suppression is armed only by an actual restore and is released by
           the first real input, so ordinary scroll dismissal is untouched. */
        if (suppressScrollDismissal) {
          return;
        }
        /* Collateral of something the user is doing INSIDE the panel — a
           reveal, a caret scroll, a layout shift. Not a scroll of the table. */
        if (!lastGestureAtGrid) {
          return;
        }
        closeAllMenus(null);
      }, { passive: true });
    });
  }

  /* Breathing room between the panel's bottom edge and the viewport's. */
  var PANEL_VIEWPORT_MARGIN = 8;
  /* A panel shorter than this cannot show anything useful, but it is still
     better than one that overhangs: an overhanging band is unreachable by ANY
     scrollbar, whereas a short panel scrolls. So this is a floor on when to
     bother, not a floor on the height — the available space always wins. */
  var PANEL_MIN_USEFUL_HEIGHT = 48;

  /* A menu anchored to a right-hand column would otherwise open past the edge
     of the horizontal scroller. Flipping its alignment is the whole fix — for
     the HORIZONTAL case. */
  function placeMenu(element) {
    var panel = element.querySelector(".db-col-panel");
    var scroller = closest(element, function (el) { return hasAttr(el, "data-db-scroll"); });
    if (!panel || !scroller || !panel.getBoundingClientRect) {
      return;
    }
    element.classList.remove("db-col-menu-flip");
    var panelBox = panel.getBoundingClientRect();
    var scrollBox = scroller.getBoundingClientRect();
    if (panelBox.right > scrollBox.right) {
      element.classList.add("db-col-menu-flip");
    }
    fitPanelToViewport(panel);
  }

  /* Make the panel FIT.
   *
   * `max-height: 60vh` measures against the viewport, but the panel starts at
   * the bottom of a sticky header, and 60% of the viewport measured from part
   * way down it does not fit inside it. Measured at 340px: the panel began at
   * 231 and 60vh gave it 204, so its own box ended at 435 — 95px below the fold.
   *
   * That is not a scrolling bug and no scroll logic can fix it. The panel is
   * absolutely positioned under a STICKY header, so it does not move when the
   * grid scrolls: a grid reveal cannot bring panel content into view, and the
   * panel's own scrollbar can only move content within a box that is itself
   * partly off-screen. Content in that 95px band was unreachable by either.
   *
   * The available space is (viewport − panel top − margin), which no `vh` value
   * can express because none of them know the offset. So it is computed here,
   * at open and on resize.
   *
   * NOT the flip: that is `left: auto; right: 0`, a horizontal alignment swap.
   * The vertical equivalent would be opening the panel ABOVE its header, and the
   * header is sticky to the top of the scroller — there is no space up there to
   * open into. Flip is the wrong lever, not a mis-tuned one.
   *
   * The stylesheet still wins wherever it already fits: this only ever makes the
   * panel shorter, never taller, so nothing changes at heights that were fine. */
  function fitPanelToViewport(panel) {
    if (!panel || !panel.style || !panel.getBoundingClientRect) {
      return;
    }
    /* Back to the stylesheet's own value before measuring, so repeated opens do
       not ratchet the panel smaller. */
    panel.style.maxHeight = "";
    var viewport = window.innerHeight ||
      (document.documentElement && document.documentElement.clientHeight) || 0;
    if (!viewport) {
      return;
    }
    var top = panel.getBoundingClientRect().top;
    var available = viewport - top - PANEL_VIEWPORT_MARGIN;
    var allowed = cssMaxHeight(panel);
    if (allowed !== null && available >= allowed) {
      return;                       /* the stylesheet already fits: leave it */
    }
    if (available < PANEL_MIN_USEFUL_HEIGHT) {
      /* Nothing useful can be shown in the space below the header. Leaving the
         stylesheet's height is the lesser evil: at least the top of the panel is
         readable, and the caller has a viewport problem this cannot solve. */
      return;
    }
    panel.style.maxHeight = available + "px";
  }

  function cssMaxHeight(panel) {
    try {
      if (!window.getComputedStyle) {
        return null;
      }
      var value = parseFloat(window.getComputedStyle(panel).maxHeight);
      return isFinite(value) ? value : null;
    } catch (error) {
      return null;
    }
  }

  /* A viewport change moves the fold under an open panel. */
  function initPanelFit() {
    window.addEventListener("resize", function () {
      menus.forEach(function (menu) {
        if (menu.element.open) {
          placeMenu(menu.element);
        }
      });
    });
  }

  /* ---------------------------------------------------------- filter panel */

  function initPanel() {
    var panel = document.querySelector("[data-db-filters]");
    if (!panel) {
      return;
    }
    var trigger = panel.querySelector("summary");

    /* Collapse only. Staged edits stay staged: reopening must show them
       (INTERACTION_SPEC.md §3 — the panel holds staged work). */
    collapsePanel = function () {
      if (!panel.open) {
        return false;
      }
      panel.open = false;
      focusVisibly(trigger);
      return true;
    };

    panel.addEventListener("toggle", function () {
      if (!panel.open) {
        return;
      }
      syncAllValueControls(panel);
      var heading = panel.querySelector(".db-panel-heading");
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        focusVisibly(heading);
      }
    });

    /* `Znajdź kolumnę` narrows the add-a-filter list in place. */
    var search = panel.querySelector("[data-db-column-search]");
    if (search) {
      search.addEventListener("input", function () {
        var needle = String(search.value || "").toLowerCase();
        all("[data-db-add-column]", panel).forEach(function (item) {
          var text = String(item.textContent || "").toLowerCase();
          setHidden(item, needle !== "" && text.indexOf(needle) === -1);
        });
      });
    }

    /* A click anywhere outside collapses the panel, the same deliberate
       dismissal `Esc` performs. Staged edits are kept, not discarded — this
       only hides the surface. A click INSIDE must never collapse it: that is
       the whole point of the panel staying open across several refinements. */
    document.addEventListener("click", function (event) {
      if (!panel.open) {
        return;
      }
      var surface = clickSurface(event);
      if (surface.panel) {
        return;
      }
      /* A column menu opened from the panel floats outside it in the DOM. The
         panel must not collapse underneath the menu it just opened. */
      if (surface.menu) {
        return;
      }
      panel.open = false;
    });

    /* Opening a column menu from the panel: the link is a fragment to that
       menu, which browsers expand on their own. Focus is moved here so the
       keyboard path matches the pointer one. */
    all("[data-db-add-column]", panel).forEach(function (link) {
      link.addEventListener("click", function () {
        var name = link.getAttribute("data-db-add-column");
        var target = document.getElementById("dbcol-" + name);
        if (target) {
          target.open = true;
        }
      });
    });
  }

  /* ------------------------------------------------------ layer dismissal */

  /* One handler, because `Esc` closes the TOPMOST transient layer only. Two
     independent handlers would both fire on the same keypress and collapse the
     panel underneath the menu the user was actually dismissing. */
  var collapsePanel = function () { return false; };

  function initEscape() {
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" && event.keyCode !== 27) {
        return;
      }
      /* A layer above this module already consumed the key — the shell's
         navigation drawer is the one that can. Only one layer closes per
         press. */
      if (event.defaultPrevented) {
        return;
      }
      var open = menus.filter(function (menu) { return menu.element.open; });
      if (open.length) {
        event.preventDefault();
        open.forEach(function (menu) { closeMenu(menu, { returnFocus: true }); });
        return;
      }
      /* The column panel sits above the filter panel, and owns `Esc` first. */
      if (document.querySelector("[data-db-columns][open]")) {
        return;
      }
      if (collapsePanel()) {
        event.preventDefault();
      }
    });
  }

  /* --------------------------------------------- surface memory across nav */

  /* Applying a filter, sorting or picking a preset is a real GET navigation,
     and the server always renders `<details>` collapsed. Remembering which
     surface was in use and reopening it on the page that comes back is what
     makes "the panel stays open while the table updates behind it" true without
     changing the apply model.
     *
     * It is deliberately sessionStorage and not a query parameter: the filter
     * URL is a contract (chips, saved views, the server-side filter builder),
     * and a presentation detail must not appear in it. */
  var OPEN_KEY = "db-open-surface";

  function sessionStore() {
    try {
      return window.sessionStorage || null;
    } catch (error) {
      /* Private modes throw on access rather than returning null. */
      return null;
    }
  }

  function surfacePath() {
    try {
      return (window.location && window.location.pathname) || "";
    } catch (error) {
      return "";
    }
  }

  function rememberSurface(token) {
    var store = sessionStore();
    if (!store || !token) {
      return;
    }
    try {
      store.setItem(OPEN_KEY, surfacePath() + "\n" + token);
      /* The grid's scroll offsets ride along under the key
         data-grid-row-detail.js already restores on every load, so applying a
         filter lands on the same rows the user was looking at. Reusing that
         mechanism rather than adding a second one keeps one writer and one
         reader per navigation. */
      var box = document.querySelector("[data-db-scroll]");
      if (box && typeof box.scrollLeft === "number") {
        store.setItem("db-row-scroll", JSON.stringify({ left: box.scrollLeft, top: box.scrollTop }));
      }
    } catch (error) {
      /* Storage full or blocked: the enhancement is simply absent. */
    }
  }

  function takeRememberedSurface() {
    var store = sessionStore();
    if (!store) {
      return null;
    }
    var raw = null;
    try {
      raw = store.getItem(OPEN_KEY);
      store.removeItem(OPEN_KEY);
    } catch (error) {
      return null;
    }
    if (!raw) {
      return null;
    }
    var split = raw.indexOf("\n");
    /* Path-scoped, so leaving the dataset and coming back does not reopen a
       menu the user has long since finished with. */
    if (split === -1 || raw.slice(0, split) !== surfacePath()) {
      return null;
    }
    return raw.slice(split + 1);
  }

  function surfaceTokenFor(node) {
    var menu = closest(node, function (el) { return hasAttr(el, "data-db-col-menu"); });
    if (menu) {
      return "menu:" + (menu.getAttribute("data-db-column") || "");
    }
    var panel = closest(node, function (el) { return hasAttr(el, "data-db-filters"); });
    return panel ? "panel" : null;
  }

  function initSurfaceMemory() {
    document.addEventListener("click", function (event) {
      var link = closest(event.target, function (el) { return hasAttr(el, "href"); });
      if (!link) {
        return;
      }
      /* `#dbcol-…` opens a menu in place and navigates nowhere; remembering it
         would reopen a surface on some unrelated later load. */
      var href = String(link.getAttribute("href") || "");
      if (!href || href.charAt(0) === "#") {
        return;
      }
      rememberSurface(surfaceTokenFor(link));
    });

    document.addEventListener("submit", function (event) {
      rememberSurface(surfaceTokenFor(event.target));
    });
  }

  var suppressScrollDismissal = false;

  function releaseScrollSuppression() {
    suppressScrollDismissal = false;
  }

  function armScrollSuppression() {
    suppressScrollDismissal = true;
    ["pointerdown", "wheel", "touchstart", "keydown"].forEach(function (type) {
      document.addEventListener(type, releaseScrollSuppression);
    });
  }

  function restoreSurface() {
    var token = takeRememberedSurface();
    if (!token) {
      return;
    }
    if (token === "panel") {
      var panel = document.querySelector("[data-db-filters]");
      if (panel) {
        panel.open = true;
        armScrollSuppression();
      }
      return;
    }
    if (token.indexOf("menu:") !== 0) {
      return;
    }
    var column = token.slice(5);
    for (var index = 0; index < menus.length; index += 1) {
      if (menus[index].element.getAttribute("data-db-column") === column) {
        /* Assigning `open` fires `toggle`, which snapshots the freshly rendered
           state and moves focus in — exactly as a manual reopen would. */
        menus[index].element.open = true;
        armScrollSuppression();
        return;
      }
    }
  }

  /* ------------------------------------------------- duplicate submissions */

  /* An Apply navigates. Without this a second click before the response lands
     sends a second, identical request; the control keeps its exact width so the
     layout does not move. */
  var guarded = [];

  function releaseGuards() {
    guarded.forEach(function (button) {
      button.disabled = false;
      button.removeAttribute("aria-disabled");
      button.style.minWidth = "";
    });
    guarded = [];
  }

  function initSubmitGuard() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form || !form.querySelector) {
        return;
      }
      var button = form.querySelector('button[type="submit"]');
      if (!button || button.disabled) {
        return;
      }
      var box = button.getBoundingClientRect ? button.getBoundingClientRect() : null;
      if (box && box.width) {
        button.style.minWidth = Math.round(box.width) + "px";
      }
      button.setAttribute("aria-disabled", "true");
      guarded.push(button);
      window.setTimeout(function () { button.disabled = true; }, 0);
    });

    /* A guarded button is disabled for the lifetime of a navigation that is
       about to replace the page. The back-forward cache breaks that assumption:
       it restores this exact DOM, disabled button and all, so `Back` after
       applying a filter would land on a page whose Apply no longer works.
       Releasing on restore keeps browser Back usable (INTERACTION_SPEC.md §7)
       without weakening the guard during the submission itself. */
    window.addEventListener("pageshow", function (event) {
      if (event && event.persisted) {
        releaseGuards();
      }
    });
  }

  /* ------------------------------------------------------------------ init */

  function init() {
    document.addEventListener("change", function (event) {
      var container = closest(event.target, function (el) {
        return hasAttr(el, "data-db-filter-controls");
      });
      if (container) {
        syncValueControls(container);
      }
    });
    /* Before every click handler: the classifier they share needs the
       pointerdown that precedes the first click. */
    initPointerTracking();
    initGestureTracking();
    initMenus();
    initPanelFit();
    initPanel();
    initEscape();
    initSubmitGuard();
    initSurfaceMemory();
    syncAllValueControls(document);
    /* Last: the menus and the panel must both be wired before one is reopened. */
    restoreSurface();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
