/* Log Platform — Database Explorer row detail (approved stage S6).
 *
 * The server already renders everything this module touches. A row's `Szczegóły`
 * link, the panel's `×`, its `↑`/`↓` traversal and its field-scope toggle are all
 * ordinary links carrying `?row=<opaque reference>`, so the whole feature works
 * with scripting unavailable: the drawer is server-rendered from the reference,
 * and a copied URL reopens it cold.
 *
 * What this module adds:
 *
 *   1. opening a row by clicking or pressing Enter anywhere on it
 *   2. Esc to close, with focus returned to the row that opened the panel
 *   3. table scroll preservation across an open/close/traverse
 *   4. `↑`/`↓` traversal while focus stays in the panel
 *
 * Two things it deliberately does NOT do:
 *
 *   - it never decodes anything. The row reference is opaque AES-GCM ciphertext;
 *     the browser only ever copies it from one place to another. The raw
 *     technical identifier is not present in the page at all.
 *   - it owns no authorization. Every navigation goes back to the server, which
 *     re-runs the ordinary user/client/dataset checks before resolving the
 *     reference. Possession of a reference is not permission to see the row.
 *
 * The panel is docked and NOT modal (`COMPONENT_CATALOG.md`: the design has no
 * modal dialog; `ACCESSIBILITY_SPEC.md`: the docked row panel is not trapped),
 * so this module must not install a focus trap.
 */
(function () {
  "use strict";

  var sheet = null;
  /* The row that opened the panel, so focus can return to it (`DB-38`, `AC-4`). */
  var lastTrigger = null;

  function scroller() {
    return document.querySelector("[data-db-scroll]");
  }

  /* ------------------------------------------------------------ navigation */

  /* Scroll position must survive an open/close/traverse (`DB-36`). The sheet
   * re-renders on the server, so the offsets are carried across the navigation
   * in sessionStorage and reapplied once — not encoded into the URL, which the
   * approved contract does not ask for and which would make links unstable. */
  function rememberScroll() {
    var box = scroller();
    if (!box) {
      return;
    }
    try {
      window.sessionStorage.setItem(
        "db-row-scroll",
        JSON.stringify({ left: box.scrollLeft, top: box.scrollTop })
      );
    } catch (error) {
      /* Private mode or a full quota: scroll restoration is a nicety. */
    }
  }

  function restoreScroll() {
    var box = scroller();
    if (!box) {
      return;
    }
    var raw = null;
    try {
      raw = window.sessionStorage.getItem("db-row-scroll");
      window.sessionStorage.removeItem("db-row-scroll");
    } catch (error) {
      return;
    }
    if (!raw) {
      return;
    }
    try {
      var saved = JSON.parse(raw);
      if (saved && typeof saved.left === "number") {
        box.scrollLeft = saved.left;
        box.scrollTop = saved.top;
      }
    } catch (error) {
      /* Malformed value: ignore it rather than throwing during load. */
    }
  }

  function go(href) {
    if (!href) {
      return;
    }
    rememberScroll();
    /* A real navigation, so the server re-authorizes and re-renders. Using
     * assign() rather than pushState keeps one history entry per opened row and
     * guarantees the URL and the rendered drawer can never disagree — there is
     * no client-side state that could drift from the address. */
    window.location.assign(href);
  }

  /* ---------------------------------------------------------------- opening */

  function openFromRow(row) {
    if (!row) {
      return;
    }
    var link = row.querySelector("[data-db-row-open]");
    if (!link) {
      return;
    }
    lastTrigger = row;
    try {
      window.sessionStorage.setItem("db-row-trigger", row.getAttribute("data-db-row") || "");
    } catch (error) {
      /* Focus return degrades to the panel heading. */
    }
    go(link.getAttribute("href"));
  }

  function isInteractive(target) {
    /* A click on a header menu, a resize handle, a filter control or a link of
     * its own must keep its own meaning rather than opening the row. */
    return !!(
      target &&
      target.closest &&
      target.closest(
        "a, button, input, select, textarea, summary, label, [data-db-col-menu], [data-db-resize], [data-db-columns]"
      )
    );
  }

  /* When S7 grid selection is active, a press on a *data* cell anchors a range
   * rather than opening the row: `INTERACTION_SPEC.md` §5 keeps cell selection
   * and row selection independent, and a cell range must not open `DB-006`. The
   * row stays openable by its own `Szczegóły` control — which carries no
   * `data-db-column` and is therefore not a selectable cell — and by `Enter`.
   *
   * The selection module publishes its own enablement, so below the approved
   * 768 px cutoff (where S7 is disabled) this is false and the whole row keeps
   * opening on click exactly as before. */
  function selectionOwnsCell(target) {
    var grid = document.querySelector("[data-db-sheet]");
    if (!grid || grid.getAttribute("data-db-selection") !== "on") {
      return false;
    }
    return !!(target && target.closest && target.closest("td[data-db-column]"));
  }

  /* ---------------------------------------------------------------- closing */

  function closePanel() {
    var panel = document.querySelector("[data-db-row-detail]");
    if (!panel) {
      return false;
    }
    var close = panel.querySelector("[data-db-row-close]");
    if (!close) {
      return false;
    }
    go(close.getAttribute("href"));
    return true;
  }

  /* -------------------------------------------------------------- traversal */

  function traverse(direction) {
    var panel = document.querySelector("[data-db-row-detail]");
    if (!panel) {
      return false;
    }
    var control = panel.querySelector('a[data-db-row-traverse="' + direction + '"]');
    if (!control) {
      /* Either edge of the page: the control renders disabled, not missing an
       * action, so there is simply nowhere to go. */
      return false;
    }
    go(control.getAttribute("href"));
    return true;
  }

  /* ------------------------------------------------------------ focus rules */

  function placeFocus() {
    var panel = document.querySelector("[data-db-row-detail]");
    if (!panel) {
      /* The panel just closed. Return focus to the row that opened it, which is
       * mandatory rather than optional (`INTERACTION_SPEC.md` §2.3). */
      var wanted = null;
      try {
        wanted = window.sessionStorage.getItem("db-row-trigger");
        window.sessionStorage.removeItem("db-row-trigger");
      } catch (error) {
        return;
      }
      if (!wanted) {
        return;
      }
      var row = document.querySelector('[data-db-row="' + window.CSS.escape(wanted) + '"]');
      if (row) {
        row.focus();
      }
      return;
    }
    /* Opening a panel moves focus to its heading, and traversal keeps focus in
     * the panel — both are the approved behaviour. */
    var heading = panel.querySelector("[data-db-row-heading]");
    if (heading) {
      heading.focus();
    }
  }

  /* ------------------------------------------------------------------- init */

  function init() {
    sheet = document.querySelector("[data-db-sheet], .db-grid");
    var table = document.querySelector(".db-table");
    if (!table) {
      return;
    }

    /* Rows are focusable so Enter can reach them without a mouse. */
    var rows = document.querySelectorAll("tbody tr[data-db-row]");
    Array.prototype.forEach.call(rows, function (row) {
      if (!row.hasAttribute("tabindex")) {
        row.setAttribute("tabindex", "0");
      }
    });

    table.addEventListener("click", function (event) {
      if (isInteractive(event.target) || selectionOwnsCell(event.target)) {
        return;
      }
      var row = event.target && event.target.closest ? event.target.closest("tr[data-db-row]") : null;
      if (!row) {
        return;
      }
      event.preventDefault();
      openFromRow(row);
    });

    table.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" || isInteractive(event.target)) {
        return;
      }
      var row = event.target && event.target.closest ? event.target.closest("tr[data-db-row]") : null;
      if (!row) {
        return;
      }
      event.preventDefault();
      openFromRow(row);
    });

    document.addEventListener("keydown", function (event) {
      if (!document.querySelector("[data-db-row-detail]")) {
        return;
      }
      if (event.key === "Escape") {
        /* Only when no nearer transient layer is open — the navigation drawer,
         * a column menu, the column panel and the filter panel/drawer all sit
         * above the row panel, and `INTERACTION_SPEC.md` §3 dismisses the
         * topmost layer only. `defaultPrevented` covers a layer that already
         * acted on this same keypress; the selectors cover the rest. */
        if (event.defaultPrevented) {
          return;
        }
        if (
          document.querySelector(
            "[data-nav-drawer]:not([hidden]), [data-db-col-menu][open]," +
              " [data-db-columns][open], [data-db-filters][open]"
          )
        ) {
          return;
        }
        if (closePanel()) {
          event.preventDefault();
        }
        return;
      }
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        /* A shifted arrow is a range extension, not a row traversal; S7 owns it
         * even while the drawer is open. */
        if (event.shiftKey) {
          return;
        }
        var target = event.target;
        /* Never steal an arrow key from a field the user is typing in. */
        if (
          target &&
          target.closest &&
          target.closest("input, select, textarea, [contenteditable='true']")
        ) {
          return;
        }
        if (traverse(event.key === "ArrowUp" ? "previous" : "next")) {
          event.preventDefault();
        }
      }
    });

    /* Panel links are ordinary navigations; routing them through go() only adds
     * scroll preservation. */
    document.addEventListener("click", function (event) {
      var control =
        event.target && event.target.closest
          ? event.target.closest("[data-db-row-close], a[data-db-row-traverse], [data-db-row-fields]")
          : null;
      if (!control) {
        return;
      }
      event.preventDefault();
      go(control.getAttribute("href"));
    });

    restoreScroll();
    placeFocus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
