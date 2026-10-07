/* Report Explorer progressive enhancement (S15).
 *
 * Every control this file touches already works without it:
 *   - the toolbar selects live inside a plain GET form with a submit button;
 *   - the rail is a list of ordinary links;
 *   - the preview is an <object> the server already pointed at the right bytes;
 *   - the detail link already carries the whole library state in its query.
 *
 * What it adds is the scroll half of `SH-13` (the query string carries filters,
 * page and page size on its own; only the pixel offset needs the browser), the
 * `Pełny ekran` control, PDF page navigation over the embedded viewer, and
 * auto-submit on the filter selects.
 */
(function () {
  "use strict";

  var LIBRARY_PATH = "/user/reports";

  function each(nodes, fn) {
    Array.prototype.slice.call(nodes || []).forEach(fn);
  }

  /* ---- SH-13: restore, then keep, the list scroll offset ---- */

  function restoreScroll() {
    if (window.location.pathname !== LIBRARY_PATH) return;
    var params = new URLSearchParams(window.location.search);
    var offset = parseInt(params.get("scroll") || "0", 10);
    if (offset > 0) {
      window.scrollTo(0, offset);
    }
  }

  function stampScrollOnDetailLinks() {
    // The href already carries the filters, the page and the page size. Only the
    // scroll offset is a browser fact, so it is stamped at click time rather
    // than rendered into every link and immediately going stale.
    document.addEventListener(
      "click",
      function (event) {
        var link = event.target && event.target.closest ? event.target.closest("a") : null;
        if (!link || !link.href) return;
        if (link.pathname.indexOf("/user/reports/instances/") !== 0) return;
        var offset = Math.round(window.scrollY || window.pageYOffset || 0);
        if (!offset) return;
        try {
          var url = new URL(link.href, window.location.origin);
          url.searchParams.set("scroll", String(offset));
          link.href = url.pathname + url.search;
        } catch (error) {
          /* A URL the browser cannot parse is left exactly as rendered. */
        }
      },
      true
    );
  }

  /* ---- toolbar: submit on change, still submittable without JS ---- */

  function autoSubmit() {
    each(document.querySelectorAll("[data-rep-autosubmit]"), function (control) {
      control.addEventListener("change", function () {
        var form = control.form;
        if (!form) return;
        // A filter change always returns to the first page: keeping page 7 while
        // narrowing the list would render an empty page that is not the
        // approved empty state.
        var page = form.querySelector("input[name=page]");
        if (page) page.value = "";
        form.submit();
      });
    });
  }

  /* ---- rail: filter the type list in place ---- */

  function railFilter() {
    var input = document.querySelector("[data-rep-rail-filter]");
    if (!input) return;
    var items = document.querySelectorAll(".rep-rail-item");
    input.addEventListener("input", function () {
      var needle = String(input.value || "").trim().toLowerCase();
      each(items, function (item) {
        var name = item.querySelector(".rep-rail-name");
        var text = name ? String(name.textContent || "").toLowerCase() : "";
        item.hidden = needle !== "" && text.indexOf(needle) === -1;
      });
    });
  }

  /* ---- preview: full screen and page navigation ---- */

  function fullscreen() {
    var button = document.querySelector("[data-rep-fullscreen]");
    var frame = document.querySelector("[data-rep-preview-frame]");
    if (!button || !frame) return;
    if (!frame.requestFullscreen) {
      // No API, no promise: the control is removed rather than left as a dead
      // button, because the approved states never render an inert control.
      button.remove();
      return;
    }
    button.addEventListener("click", function () {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else {
        frame.requestFullscreen();
      }
    });
  }

  function pageNav() {
    var nav = document.querySelector("[data-rep-page-nav]");
    var object = document.querySelector("[data-rep-preview-src]");
    if (!nav || !object) return;
    var total = parseInt(nav.getAttribute("data-rep-pages") || "0", 10);
    if (!(total > 1)) return;
    var indicator = nav.querySelector("[data-rep-page-indicator]");
    var base = object.getAttribute("data-rep-preview-src");
    var current = 1;

    function go(next) {
      current = Math.min(total, Math.max(1, next));
      // The embedded viewer honours the `#page=` fragment; reassigning `data`
      // is what makes it re-read it.
      object.setAttribute("data", base + "#page=" + current);
      if (indicator) indicator.textContent = current + " / " + total;
    }

    var prev = nav.querySelector("[data-rep-page-prev]");
    var next = nav.querySelector("[data-rep-page-next]");
    if (prev) prev.addEventListener("click", function () { go(current - 1); });
    if (next) next.addEventListener("click", function () { go(current + 1); });
  }

  function boot() {
    restoreScroll();
    stampScrollOnDetailLinks();
    autoSubmit();
    railFilter();
    fullscreen();
    pageNav();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
