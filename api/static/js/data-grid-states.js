/* Log Platform — Database Explorer system states (DB-59, DB-011; stage S9).
 *
 * The application is server-rendered. A filter Apply, a search submit, a sort
 * link, a page change, a page-size change or a column reload is an ordinary
 * navigation, and the gap between the click and the new document is the only
 * moment there is anything to show. So this module does exactly one thing: it
 * marks the sheet busy for the lifetime of a navigation the browser has already
 * accepted, and it renders the approved skeleton in place of the rows.
 *
 * Deliberately NOT here:
 *
 *   - no data fetching. Nothing is requested, parsed or rendered from a
 *     response; the browser still performs the navigation it always did.
 *   - no persistence. The pending flag lives in the DOM of a document that is
 *     about to be replaced — never in the URL, never in localStorage.
 *   - no control disabling. The duplicate-submit guard in data-grid-filters.js
 *     owns that, and it already releases on `pageshow`. Editable controls keep
 *     working here.
 *
 * Without JavaScript nothing changes: every trigger is a real form or a real
 * link and the server navigation is unaffected.
 *
 * The skeleton is built from the rendered table's own geometry — one block per
 * <col> at that column's width, as many rows as the page currently shows — so
 * the table area keeps its height and the page does not jump. Cell values are
 * never copied into it: a skeleton carries no data.
 */
(function () {
  "use strict";

  var PENDING_ATTR = "data-db-pending";
  var MAX_SKELETON_ROWS = 60;

  function closest(node, test) {
    var current = node;
    while (current && current !== document) {
      if (test(current)) {
        return current;
      }
      current = current.parentNode;
    }
    return null;
  }

  function sheet() {
    return document.querySelector("[data-db-sheet]");
  }

  /* ------------------------------------------------------------- skeleton */

  function buildSkeleton(target) {
    var table = target.querySelector(".db-table");
    if (!table) {
      return null;
    }
    var cols = table.querySelectorAll("colgroup col");
    var bodyRows = table.querySelectorAll("tbody tr");
    var rendered = bodyRows.length;
    /* The current page size, so an empty or short page still reserves the space
       the next one will occupy rather than collapsing to nothing. */
    var pageSize = parseInt(target.getAttribute("data-db-page-size") || "0", 10);
    var count = rendered > 0 ? rendered : pageSize;
    if (!count || count < 1) {
      count = 1;
    }
    if (count > MAX_SKELETON_ROWS) {
      count = MAX_SKELETON_ROWS;
    }

    var wrap = document.createElement("div");
    wrap.className = "db-skeleton";
    wrap.setAttribute("aria-hidden", "true");
    for (var r = 0; r < count; r += 1) {
      var row = document.createElement("div");
      row.className = "db-skeleton-row";
      for (var c = 0; c < cols.length; c += 1) {
        var cell = document.createElement("span");
        cell.className = "db-skeleton-cell";
        var width = cols[c].style ? cols[c].style.width : "";
        if (width) {
          cell.style.width = width;
        }
        row.appendChild(cell);
      }
      wrap.appendChild(row);
    }
    return wrap;
  }

  function enter(target) {
    if (!target || target.hasAttribute(PENDING_ATTR)) {
      return;
    }
    target.setAttribute(PENDING_ATTR, "");
    var viewport = target.querySelector(".db-table-viewport");
    if (viewport) {
      viewport.setAttribute("aria-busy", "true");
      var skeleton = buildSkeleton(target);
      if (skeleton) {
        viewport.appendChild(skeleton);
      }
    }
    /* `…` in place of every resolved count, per the approved loading state. */
    var counters = target.querySelectorAll(".db-counter");
    for (var i = 0; i < counters.length; i += 1) {
      counters[i].setAttribute("data-db-pending-counter", "");
    }
  }

  function leave(target) {
    if (!target || !target.hasAttribute(PENDING_ATTR)) {
      return;
    }
    target.removeAttribute(PENDING_ATTR);
    var viewport = target.querySelector(".db-table-viewport");
    if (viewport) {
      viewport.removeAttribute("aria-busy");
      var skeleton = viewport.querySelector(".db-skeleton");
      if (skeleton && skeleton.parentNode) {
        skeleton.parentNode.removeChild(skeleton);
      }
    }
    var counters = target.querySelectorAll("[data-db-pending-counter]");
    for (var i = 0; i < counters.length; i += 1) {
      counters[i].removeAttribute("data-db-pending-counter");
    }
  }

  /* ------------------------------------------------------------- triggers */

  function isNavigatingLink(link) {
    if (!link || !link.getAttribute) {
      return false;
    }
    var href = link.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#") {
      return false;
    }
    if (link.getAttribute("target") || link.hasAttribute("download")) {
      return false;
    }
    /* A density switch is intercepted in place and never requeries, so it must
       not raise a skeleton. Anything another module handles has already called
       preventDefault by the time this listener runs. */
    if (link.hasAttribute("data-density-option")) {
      return false;
    }
    return true;
  }

  function plainClick(event) {
    if (event.defaultPrevented) {
      return false;
    }
    if (event.button && event.button !== 0) {
      return false;
    }
    return !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey);
  }

  function init() {
    var target = sheet();
    if (!target) {
      return;
    }

    /* A fresh document is never pending. This also normalizes a document the
       browser restored with the attribute already set. */
    leave(target);

    document.addEventListener("submit", function (event) {
      if (event.defaultPrevented) {
        return;
      }
      var form = event.target;
      if (!form || !closest(form, function (el) { return el.hasAttribute && el.hasAttribute("data-db-sheet"); })) {
        return;
      }
      enter(target);
    });

    document.addEventListener("click", function (event) {
      if (!plainClick(event)) {
        return;
      }
      var link = closest(event.target, function (el) {
        return el.tagName && el.tagName.toLowerCase() === "a";
      });
      if (!link || !isNavigatingLink(link)) {
        return;
      }
      if (!closest(link, function (el) { return el.hasAttribute && el.hasAttribute("data-db-sheet"); })) {
        return;
      }
      enter(target);
    });

    /* Back/Forward restores this exact DOM. Without this the browser would show
       a skeleton over rows that are already there and never resolve, because no
       navigation is in flight any more. Normalizing on restore is what keeps
       history usable (INTERACTION_SPEC.md §7) — and it is why the flag lives
       nowhere but in the DOM. */
    window.addEventListener("pageshow", function () {
      leave(sheet());
    });
    window.addEventListener("pagehide", function () {
      leave(sheet());
    });
  }

  /* --------------------------------------------------- DB-011 reference copy */

  /* `Skopiuj referencję`. The value is an attribute the server wrote, so the
     button copies exactly the reference the state displays and reads nothing
     out of the page. A clipboard failure leaves the reference readable and
     selectable on screen, which is why there is no fallback prompt. */
  function initReferenceCopy() {
    document.addEventListener("click", function (event) {
      var button = closest(event.target, function (el) {
        return el.hasAttribute && el.hasAttribute("data-db-error-copy");
      });
      if (!button) {
        return;
      }
      event.preventDefault();
      var reference = button.getAttribute("data-db-error-copy") || "";
      if (!reference) {
        return;
      }
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(reference);
        }
      } catch (err) {
        /* Nothing to recover: the reference is already on screen. */
      }
    });
  }

  function boot() {
    init();
    initReferenceCopy();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
