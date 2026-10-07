/* Log Platform — shared shell behaviour.
 *
 * Only the collapsed navigation drawer (<=1279 px, RESPONSIVE_SPEC "Shell").
 * The drawer is a trapped layer: focus is contained while open and returned to
 * the menu button on close (ACCESSIBILITY_SPEC §5).
 *
 * Everything else in the shell is a real link or a real button and needs no
 * scripting.
 */
(function () {
  "use strict";

  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function init() {
    var toggle = document.querySelector("[data-nav-toggle]");
    var drawer = document.querySelector("[data-nav-drawer]");
    var scrim = document.querySelector("[data-nav-scrim]");
    if (!toggle || !drawer) {
      return;
    }

    var lastFocused = null;

    function isOpen() {
      return !drawer.hasAttribute("hidden");
    }

    function open() {
      lastFocused = document.activeElement;
      drawer.removeAttribute("hidden");
      if (scrim) {
        scrim.removeAttribute("hidden");
      }
      toggle.setAttribute("aria-expanded", "true");
      var first = drawer.querySelector(FOCUSABLE);
      if (first) {
        first.focus();
      }
    }

    function close() {
      if (!isOpen()) {
        return;
      }
      drawer.setAttribute("hidden", "");
      if (scrim) {
        scrim.setAttribute("hidden", "");
      }
      toggle.setAttribute("aria-expanded", "false");
      if (lastFocused && typeof lastFocused.focus === "function") {
        lastFocused.focus();
      }
    }

    toggle.addEventListener("click", function () {
      if (isOpen()) {
        close();
      } else {
        open();
      }
    });

    /* A drawer left open when the user followed one of its links is restored by
       the back-forward cache exactly as it was: visible, over a scrim, with a
       stale `aria-expanded="true"`. Back must not land on a stuck overlay, so a
       restore resets the layer. Focus is only moved when it would otherwise be
       left inside the drawer that just disappeared. */
    window.addEventListener("pageshow", function (event) {
      if (!event || !event.persisted || !isOpen()) {
        return;
      }
      var inside = drawer.contains && drawer.contains(document.activeElement);
      drawer.setAttribute("hidden", "");
      if (scrim) {
        scrim.setAttribute("hidden", "");
      }
      toggle.setAttribute("aria-expanded", "false");
      lastFocused = null;
      if (inside && toggle.focus) {
        toggle.focus();
      }
    });

    if (scrim) {
      scrim.addEventListener("click", close);
    }

    var closeButton = drawer.querySelector("[data-nav-drawer-close]");
    if (closeButton) {
      closeButton.addEventListener("click", close);
    }

    document.addEventListener("keydown", function (event) {
      if (!isOpen()) {
        return;
      }
      if (event.key === "Escape") {
        /* `Esc` closes the TOPMOST transient layer only. The nav drawer is the
           highest layer in the shell, so it closes and marks the event handled;
           every module-level layer below it yields on `defaultPrevented` rather
           than collapsing under the same keypress. */
        close();
        if (event.preventDefault) {
          event.preventDefault();
        }
        return;
      }
      if (event.key !== "Tab") {
        return;
      }
      var items = drawer.querySelectorAll(FOCUSABLE);
      if (!items.length) {
        return;
      }
      var first = items[0];
      var last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
