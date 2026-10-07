/* Log Platform — Database Explorer data sheet behaviour.
 *
 * Three concerns, all progressive enhancements over markup that already works:
 *
 *   1. density  — browser-local preference, applied without a reload
 *   2. fade     — the right-edge affordance while the table overflows
 *   3. copy     — click a cell to copy its underlying value
 *
 * Column menus and the filter panel live in data-grid-filters.js.
 *
 * Without JavaScript every one of these degrades to something usable: density
 * is a link carrying ?density=, the fade is simply absent, and cells stay
 * selectable text.
 */
(function () {
  "use strict";

  var DENSITY_KEY = "logplatform.database.density";
  var DENSITIES = ["compact", "comfortable"];

  /* ---------------------------------------------------------------- density */

  function readStoredDensity() {
    try {
      var value = window.localStorage.getItem(DENSITY_KEY);
      return DENSITIES.indexOf(value) === -1 ? null : value;
    } catch (err) {
      /* Private mode or a blocked store: fall back to what the server sent. */
      return null;
    }
  }

  function writeStoredDensity(density) {
    try {
      window.localStorage.setItem(DENSITY_KEY, density);
    } catch (err) {
      /* The choice still applies to this document. */
    }
  }

  function applyDensity(sheet, density) {
    sheet.setAttribute("data-density", density);
    var options = document.querySelectorAll("[data-density-option]");
    for (var i = 0; i < options.length; i += 1) {
      var option = options[i];
      var pressed = option.getAttribute("data-density-option") === density;
      option.setAttribute("aria-pressed", pressed ? "true" : "false");
    }
  }

  /* Read an explicit density out of a URL's query string, if it carries one.
     Hand-parsed rather than via URLSearchParams so the behaviour matches the
     rest of this file's browser floor. */
  function densityFromSearch(search) {
    var query = String(search || "");
    if (query.charAt(0) === "?") {
      query = query.slice(1);
    }
    var parts = query.split("&");
    for (var i = 0; i < parts.length; i += 1) {
      var pair = parts[i].split("=");
      if (decodeURIComponent(pair[0] || "") === "density") {
        var value = decodeURIComponent((pair[1] || "").replace(/\+/g, " "));
        return DENSITIES.indexOf(value) === -1 ? null : value;
      }
    }
    return null;
  }

  function historyAvailable() {
    return !!(window.history && typeof window.history.pushState === "function");
  }

  /* The density the page should show for a given URL, using the same precedence
     the server applies: an explicit ?density= wins, then the stored preference,
     then the approved default. Keeping one function for this is what stops the
     URL and the DOM drifting apart. */
  function resolveForUrl(search, fallback) {
    return densityFromSearch(search) || readStoredDensity() || fallback;
  }

  function initDensity(sheet) {
    /* Precedence, and it must stay in this order:
         1. an explicit ?density= in the URL   — a shared or bookmarked link
                                                 shows what its author saw
         2. the browser-local preference       — this user's standing choice
         3. the server-rendered default        — the approved default, Zwarta
       The server marks case 1 with data-density-locked, because only it knows
       whether the value came from the query string or from its own default. */
    /* The server states its own default. Reading the rendered density instead
       would be wrong whenever the URL pinned one, because then the rendered
       value IS the URL's, not the default. */
    var approvedDefault = sheet.getAttribute("data-density-default") || DENSITIES[0];
    var locked = sheet.hasAttribute("data-density-locked");
    if (!locked) {
      var stored = readStoredDensity();
      if (stored) {
        applyDensity(sheet, stored);
      }
    }

    document.addEventListener("click", function (event) {
      var target = event.target;
      while (target && target !== document) {
        if (target.hasAttribute && target.hasAttribute("data-density-option")) {
          var density = target.getAttribute("data-density-option");
          if (DENSITIES.indexOf(density) === -1) {
            return;
          }
          /* Prevent the href fallback from navigating: changing density must
             not requery, so filters, sort, page, columns and scroll position
             all survive untouched. */
          event.preventDefault();
          applyDensity(sheet, density);
          writeStoredDensity(density);

          /* Push the new density into the URL. Without this the address bar
             would keep asserting the previous density while the table showed
             the new one, and since the URL legitimately wins on load, a reload
             would silently undo the change. The href is the server-built link
             for this exact view, so every other parameter and the route come
             along unchanged. pushState (not replaceState) is what gives Back
             and Forward something to move between. */
          if (historyAvailable()) {
            var href = target.getAttribute("href");
            if (href) {
              try {
                window.history.pushState({ density: density }, "", href);
              } catch (err) {
                /* A blocked history call must not cost the user the change
                   they just made; the DOM and the stored preference stand. */
              }
            }
          }

          /* The row height changed, so the overflow state may have too. */
          syncAllFades();
          return;
        }
        target = target.parentNode;
      }
    });

    /* Back and Forward move between density URLs this script pushed. Nothing
       is refetched — the rows on screen already belong to this view — so the
       handler only re-derives what the history URL asks for. */
    window.addEventListener("popstate", function () {
      var location = window.location || {};
      applyDensity(sheet, resolveForUrl(location.search, approvedDefault));
      syncAllFades();
    });
  }

  /* ------------------------------------------------------------------ fade */

  function syncFade(scroller) {
    var viewport = scroller.parentNode;
    if (!viewport || !viewport.hasAttribute) {
      return;
    }
    var remaining = scroller.scrollWidth - scroller.clientWidth - scroller.scrollLeft;
    /* 1px of slack absorbs sub-pixel widths, which would otherwise leave the
       fade showing on a table that is already fully scrolled. */
    viewport.setAttribute("data-overflowing", remaining > 1 ? "true" : "false");
  }

  function syncAllFades() {
    var scrollers = document.querySelectorAll("[data-db-scroll]");
    for (var i = 0; i < scrollers.length; i += 1) {
      syncFade(scrollers[i]);
    }
  }

  function initFades() {
    var scrollers = document.querySelectorAll("[data-db-scroll]");
    for (var i = 0; i < scrollers.length; i += 1) {
      (function (scroller) {
        syncFade(scroller);
        scroller.addEventListener("scroll", function () { syncFade(scroller); }, { passive: true });
      })(scrollers[i]);
    }
    window.addEventListener("resize", syncAllFades);
  }

  /* ------------------------------------------------------------------ copy */

  function fallbackCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
    } catch (err) {
      /* Nothing further to try; the value stays selectable in the cell. */
    } finally {
      area.parentNode.removeChild(area);
    }
  }

  function initCopy() {
    document.addEventListener("click", function (event) {
      var cell = event.target && event.target.closest
        ? event.target.closest("[data-db-copy]")
        : null;
      if (!cell) {
        return;
      }
      /* The copied value is the source value, not the formatted one: a user
         pasting a timestamp or a truncated identifier must get what the
         database holds, not what the cell had room to show. */
      var value = cell.getAttribute("data-db-copy") || "";
      if (!value) {
        return;
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(value).catch(function () { fallbackCopy(value); });
      } else {
        fallbackCopy(value);
      }
      cell.classList.add("db-copied");
      window.setTimeout(function () { cell.classList.remove("db-copied"); }, 700);
    });
  }

  /* ------------------------------------------------------------------ init */

  function init() {
    var sheet = document.querySelector("[data-db-sheet]");
    if (sheet) {
      initDensity(sheet);
    }
    initFades();
    initCopy();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
