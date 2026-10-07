/* Driver Eco Dashboard — app.js
 * 1:1 port of the approved slide behaviour:
 *  - free scroll inside a slide STOPS at its boundary: the gesture that
 *    carried the reader there never crosses, however long its inertia tail
 *    runs. The next separate gesture in the same direction, begun while
 *    already at the boundary, crosses on its first notch — a gesture ends on a
 *    pause, a reversal, or a burst of energy against its own decay;
 *  - entrance animations (count-ups, ring, bars, cascades) run once per slide
 *    per page load; the DOM always contains the final values (render.js), the
 *    animation only replays them;
 *  - dot navigation, hash route #<weekly|monthly>/<1|2|3> via replaceState. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory(null, null);
  } else {
    root.EcoApp = factory(root.EcoRender, root.EcoFormat);
  }
})(typeof self !== "undefined" ? self : this, function (EcoRender, EcoFormat) {
  "use strict";

  var TRANS_MS = 800, ANIM_MS = 1600;

  /* Boundary gating: crossing takes a SECOND, separate gesture. A wheel
   * gesture may cross only if it was already at the boundary when it began, so
   * the scroll that consumed the slide stops there and its inertia tail banks
   * nothing. The next gesture then crosses on its first notch — the cost is one
   * natural pause, not a stop-and-reflick.
   *
   * GESTURE_GAP_MS is the segmentation threshold. Touchpad inertia arrives
   * dense (tens of ms) and a continuous mouse roll rarely exceeds ~150 ms
   * between notches, so both read as one gesture; a deliberate pause reads as
   * a new one. LOCK_MS stays shorter than TRANS_MS — see goTo for the
   * entrance-animation timer that shortfall makes necessary. */
  var LOCK_MS = 500;         /* wheel/touch lock after a transition starts */
  var GESTURE_GAP_MS = 250;  /* silence that ends a wheel gesture */
  var SWIPE_PX = 45;         /* touch swipe distance, measured from touchstart */
  /* Trend watch. A touchpad tail emits decaying events for seconds, far longer
   * than any idle gap, so silence alone cannot segment it. A magnitude that
   * jumps clear of the recent window counts as a fresh push of energy — but
   * only once the gesture is demonstrably past its peak, which is what keeps a
   * single accelerating swipe, and a constant-rate train, classified as one. */
  var BURST_WIN = 4;         /* events compared against */
  var BURST_FACTOR = 1.8;    /* how far above the window a burst must reach */
  var BURST_FLOOR = 18;      /* absolute floor, so tail jitter cannot burst */
  var BURST_DECAY = 0.6;     /* window must have fallen this far below the peak */

  var state = {
    doc: null, period: null, slide: 0,
    played: {},          /* slide index -> true (once per load) */
    lock: false, lastWheel: 0, gestureDir: 0, gestureCanCross: false, animTimer: null,
    wheelWin: [], gPeak: 0,
    touchY: 0, touchAtTop: false, touchAtBot: false, touchDone: false,
    rafs: []
  };

  function reducedMotion() {
    return typeof matchMedia === "function" &&
      matchMedia("(prefers-reduced-motion: reduce)").matches;
  }
  function rootEl() { return document.getElementById("eco-root"); }
  function slides() { return document.querySelectorAll(".ed-slide"); }
  function curSlideEl() { return slides()[state.slide] || null; }

  /* ------------------------------------------------------------- routing */

  function parseHash(hash) {
    var m = /^#(weekly|monthly)\/([123])$/.exec(hash || "");
    if (!m) return null;
    return { period: m[1], slide: parseInt(m[2], 10) - 1 };
  }
  function buildHash(route) { return "#" + route.period + "/" + (route.slide + 1); }
  function writeHash() {
    try { history.replaceState(null, "", buildHash({ period: state.period, slide: state.slide })); }
    catch (e) { /* sandboxed preview may refuse */ }
  }

  /* ---------------------------------------------------------- animations */

  function easeOut(p) { return 1 - Math.pow(1 - p, 3); }

  function startAnim(i) {
    if (state.played[i]) return;
    state.played[i] = true;
    var slideEl = slides()[i];
    if (!slideEl) return;
    if (reducedMotion()) return; /* final values already rendered */
    /* failsafe: visibility must never depend on rAF ticking (background tabs,
     * throttled rAF) — after the animation window everything becomes visible */
    setTimeout(function () { finishAllVisible(slideEl); }, ANIM_MS + 600);

    var cus = [].map.call(slideEl.querySelectorAll("[data-cu]"), function (w) {
      return { el: w.querySelector(".cu"), v: parseFloat(w.getAttribute("data-cu-v")),
        seq: w.getAttribute("data-cu-seq"), fin: null };
    }).filter(function (c) { return c.el && isFinite(c.v); });
    cus.forEach(function (c) { c.fin = c.el.textContent; });

    var aws = [].map.call(slideEl.querySelectorAll("[data-aw]"), function (el) {
      return { el: el, v: parseFloat(el.getAttribute("data-aw")) };
    });
    var ahs = [].map.call(slideEl.querySelectorAll("[data-ah]"), function (el) {
      return { el: el, v: parseFloat(el.getAttribute("data-ah")) };
    });
    var axs = [].map.call(slideEl.querySelectorAll("[data-ax]"), function (el) {
      return { el: el, v: parseFloat(el.getAttribute("data-ax")) };
    });
    var rings = [].map.call(slideEl.querySelectorAll("[data-ring]"), function (el) {
      return { el: el, v: parseFloat(el.getAttribute("data-val")) || 0,
        max: parseFloat(el.getAttribute("data-max")) || 100,
        safe: parseFloat(el.getAttribute("data-safe")),
        acc: parseFloat(el.getAttribute("data-acc")) };
    });
    var fades = slideEl.querySelectorAll(".ed-fade[data-fade]");
    var cascs = slideEl.querySelectorAll(".ed-casc[data-casc]");

    function ringColor(v, r) {
      if (isFinite(r.safe) && v >= r.safe) return "#3E8E5A";
      if (isFinite(r.acc) && v >= r.acc) return "#F2CB05";
      return "#C4463A";
    }

    var t0 = null;
    function frame(now) {
      if (t0 === null) t0 = now;
      var p = Math.min(1, (now - t0) / ANIM_MS);
      var e = easeOut(p);
      var ea = Math.min(1, e / 0.45), eb = Math.max(0, (e - 0.5) / 0.5);
      cus.forEach(function (c) {
        var k = c.seq === "a" ? ea : c.seq === "b" ? eb : e;
        c.el.textContent = p >= 1 ? c.fin : EcoFormat.int(Math.round(c.v * k));
      });
      aws.forEach(function (a) { a.el.style.width = (a.v * e).toFixed(2) + "%"; });
      ahs.forEach(function (a) { a.el.style.height = (a.v * e).toFixed(2) + "%"; });
      axs.forEach(function (a) { a.el.style.left = (a.v * e).toFixed(2) + "%"; });
      rings.forEach(function (r) {
        var shown = Math.round(r.v * e);
        var deg = (r.max > 0 ? Math.max(0, Math.min(1, r.v / r.max)) : 0) * 360 * e;
        r.el.style.background = "conic-gradient(" + ringColor(shown, r) + " " +
          deg.toFixed(1) + "deg,#E4EDE2 0)";
      });
      [].forEach.call(fades, function (el) {
        if (e > parseFloat(el.getAttribute("data-fade"))) el.classList.add("in");
      });
      [].forEach.call(cascs, function (el) {
        var idx = parseInt(el.getAttribute("data-casc"), 10) || 0;
        if (p > 0.06 + idx * 0.042) el.classList.add("in");
      });
      if (p < 1) state.rafs.push(requestAnimationFrame(frame));
    }
    state.rafs.push(requestAnimationFrame(frame));
  }

  function finishAllVisible(slideEl) {
    /* make a never-animated slide fully visible (fades/cascades) */
    if (!slideEl) return;
    [].forEach.call(slideEl.querySelectorAll(".ed-fade,.ed-casc"), function (el) {
      el.classList.add("in");
    });
  }

  /* -------------------------------------------------------------- slides */

  function setInert(el, on) {
    if ("inert" in el) { el.inert = on; return; }
    if (on) el.setAttribute("inert", ""); else el.removeAttribute("inert");
  }

  function focusPanel(el) {
    if (!el || !el.focus) return;
    try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); }
  }

  /* Keeps the tab/tabpanel contract true after every slide change and every
   * re-render: roving tabindex on the tabs, aria-selected on exactly one tab,
   * and the two inactive panels out of the a11y tree and the tab order. */
  function applySlide() {
    var mover = document.getElementById("ed-mover");
    if (mover) mover.style.setProperty("--seg", String(state.slide));
    [].forEach.call(document.querySelectorAll(".ed-dot"), function (d) {
      var sel = d.getAttribute("data-i") === String(state.slide);
      d.setAttribute("aria-selected", sel ? "true" : "false");
      d.setAttribute("tabindex", sel ? "0" : "-1");
    });
    var panels = document.querySelectorAll('.ed-slide[role="tabpanel"]');
    [].forEach.call(panels, function (p, i) {
      if (i === state.slide) {
        p.removeAttribute("aria-hidden");
        setInert(p, false);
        p.setAttribute("tabindex", "0");
        return;
      }
      /* never leave focus inside a panel we are about to hide from AT */
      if (document.activeElement && p.contains(document.activeElement)) {
        focusPanel(panels[state.slide]);
      }
      p.setAttribute("aria-hidden", "true");
      setInert(p, true);
      p.setAttribute("tabindex", "-1");
    });
  }

  /* `force` is set by explicit activation (dot click, tab keyboard): a person
   * asking for a slide is never swallowed by the wheel-inertia lock. */
  function goTo(i, force) {
    if (!state.doc || i < 0 || i > 2 || i === state.slide) return;
    if (state.lock && !force) return;
    if (!slides().length) return;
    state.lock = true;
    state.slide = i;
    /* the arriving slide must be asked for separately, by its own gesture.
     * gestureDir and the trend window deliberately survive: they describe the
     * physical gesture, which does not end because a transition began. */
    state.gestureCanCross = false;
    applySlide();
    writeHash();
    setTimeout(function () { state.lock = false; }, LOCK_MS);
    /* LOCK_MS < TRANS_MS, so the previous goTo's entrance timer can still be
     * pending when this one lands. Drop it, and re-check on fire, so an
     * entrance never starts on a slide the reader has already left. An
     * animation already running is left to finish: cancelling mid-flight would
     * freeze counters and bars at partial values, and it is off-screen anyway. */
    if (state.animTimer) clearTimeout(state.animTimer);
    state.animTimer = setTimeout(function () {
      state.animTimer = null;
      if (state.slide === i) startAnim(i);
    }, Math.min(TRANS_MS, 500));
  }

  /* ------------------------------------------------------------ rendering */

  function render() {
    var el = rootEl();
    if (!el || !state.doc) return;
    state.rafs.forEach(cancelAnimationFrame);
    state.rafs = [];
    el.innerHTML = EcoRender.renderDashboard(state.doc, { period: state.period, slide: state.slide });
    applySlide();
    /* already-played slides must show final fades/cascades after re-render */
    var all = slides();
    for (var i = 0; i < all.length; i++) {
      if (state.played[i] || reducedMotion()) { state.played[i] = true; finishAllVisible(all[i]); }
    }
    startAnim(state.slide);
  }

  /* --------------------------------------------------- delegated listeners */

  /* One disclosure implementation for both expandable analytical controls:
   * the axis segment reveal (slide 2) and the daily coefficient detail
   * (slide 3). The trigger carries aria-expanded/aria-controls; the revealed
   * nodes declare how they hide — "soft" (kept in layout, the approved opacity
   * reveal) or "hard" (the `hidden` attribute). */
  function disclosureHost(el) {
    return (el.closest && el.closest(".ed-axis-row")) || el;
  }

  function setDisclosure(el, next) {
    var host = disclosureHost(el);
    var trigger = host.querySelector("[aria-expanded]");
    if (!trigger) return;
    trigger.setAttribute("aria-expanded", next ? "true" : "false");
    if (next) host.classList.add("show"); else host.classList.remove("show");
    var ids = (trigger.getAttribute("aria-controls") || "").split(/\s+/);
    ids.forEach(function (id) {
      var target = id ? document.getElementById(id) : null;
      if (!target) return;
      if (target.getAttribute("data-reveal") === "hard") target.hidden = !next;
      else target.setAttribute("aria-hidden", next ? "false" : "true");
    });
  }

  function toggleDisclosure(el) {
    var host = disclosureHost(el);
    var trigger = host.querySelector("[aria-expanded]");
    if (!trigger) return;
    setDisclosure(el, trigger.getAttribute("aria-expanded") !== "true");
  }

  function onClick(ev) {
    var t = ev.target && ev.target.closest ? ev.target.closest("[data-action]") : null;
    if (!t) return;
    var action = t.getAttribute("data-action");
    if (action === "retry") { location.reload(); return; }
    if (action === "dot") { goTo(parseInt(t.getAttribute("data-i"), 10), true); return; }
    if (action === "axtap") { toggleDisclosure(t); }
  }

  function tabEls() {
    return [].slice.call(document.querySelectorAll('.ed-dot[role="tab"]'));
  }

  /* Tablist keyboard contract: Left/Right (and Up/Down — the dots are a
   * vertical tablist) move and activate, Home/End jump to the ends,
   * Enter/Space activate the focused tab. */
  function onKeyDown(ev) {
    if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
    var t = ev.target && ev.target.closest ? ev.target.closest("[data-action]") : null;
    if (!t) return;
    var action = t.getAttribute("data-action");
    var key = ev.key;
    var space = key === " " || key === "Spacebar";
    if (action === "dot") {
      var all = tabEls();
      var i = all.indexOf(t);
      if (i < 0) return;
      var next = null;
      if (key === "ArrowRight" || key === "ArrowDown") next = (i + 1) % all.length;
      else if (key === "ArrowLeft" || key === "ArrowUp") next = (i - 1 + all.length) % all.length;
      else if (key === "Home") next = 0;
      else if (key === "End") next = all.length - 1;
      else if (key === "Enter" || space) { ev.preventDefault(); goTo(i, true); return; }
      if (next === null) return;
      ev.preventDefault();
      goTo(next, true);
      var target = tabEls()[next];
      if (target) target.focus();
      return;
    }
    /* the axis reveal is driven from a real <button>, which already handles
     * Enter/Space; the row itself is only a pointer convenience. */
    if (action === "axtap" && t.tagName !== "BUTTON" && (key === "Enter" || space)) {
      ev.preventDefault();
      toggleDisclosure(t);
    }
  }

  function onWheel(ev) {
    if (!state.doc) return;
    var now = performance.now();
    var idle = now - state.lastWheel > GESTURE_GAP_MS;
    /* The clock and the trend window keep running through the lock. Returning
     * early without updating them would make the first event after the lock
     * look fresh, and one long train would then walk several slides. */
    state.lastWheel = now;
    if (Math.abs(ev.deltaY) < 8) return;
    var dir = ev.deltaY > 0 ? 1 : -1;
    var mag = Math.abs(ev.deltaY);

    var win = state.wheelWin, base = 0, i;
    for (i = 0; i < win.length; i++) if (win[i] > base) base = win[i];
    var burst = win.length > 0 && base < state.gPeak * BURST_DECAY &&
      mag >= BURST_FLOOR && mag > base * BURST_FACTOR;
    win.push(mag);
    if (win.length > BURST_WIN) win.shift();

    /* Three independent ways to begin a gesture: a pause, a burst of energy
     * against a decaying tail, or a reversal. */
    var newGesture = idle || burst || dir !== state.gestureDir;
    if (newGesture) { state.gestureDir = dir; state.gPeak = mag; }
    else if (mag > state.gPeak) { state.gPeak = mag; }

    if (state.lock) {
      /* a gesture still running across the transition — including the one that
       * caused it — is spent, and must not cross again when the lock lifts */
      state.gestureCanCross = false;
      return;
    }
    var el = curSlideEl();
    if (!el) return;
    var atEdge = dir > 0
      ? el.scrollHeight - el.scrollTop - el.clientHeight < 2
      : el.scrollTop < 2;
    /* A gesture earns the right to cross exactly once, when it starts: only a
     * gesture that began at the boundary may cross. */
    if (newGesture) state.gestureCanCross = atEdge;
    if (!atEdge || !state.gestureCanCross) return;
    state.gestureCanCross = false;
    goTo(state.slide + dir);
  }

  function onTouchStart(ev) {
    var el = curSlideEl();
    if (!el || !ev.touches || !ev.touches.length) return;
    state.touchY = ev.touches[0].clientY;
    state.touchDone = false;
    state.touchAtBot = el.scrollHeight - el.scrollTop - el.clientHeight < 2;
    state.touchAtTop = el.scrollTop < 2;
  }

  function onTouchMove(ev) {
    if (!state.doc || state.lock || state.touchDone || !ev.touches || !ev.touches.length) return;
    var dy = state.touchY - ev.touches[0].clientY;
    if (dy > SWIPE_PX && state.touchAtBot) { state.touchDone = true; goTo(state.slide + 1); }
    else if (dy < -SWIPE_PX && state.touchAtTop) { state.touchDone = true; goTo(state.slide - 1); }
  }

  function onHashChange() {
    if (!state.doc) return;
    var r = parseHash(location.hash);
    if (!r || !state.doc.periods[r.period]) { writeHash(); return; }
    if (r.period !== state.period) {
      state.period = r.period;
      state.slide = r.slide;
      state.played = {};
      render();
      writeHash();
      return;
    }
    if (r.slide !== state.slide) goTo(r.slide);
  }

  if (typeof document !== "undefined") {
    document.addEventListener("click", onClick);
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("wheel", onWheel, { passive: true });
    document.addEventListener("touchstart", onTouchStart, { passive: true });
    document.addEventListener("touchmove", onTouchMove, { passive: true });
  }

  /* ------------------------------------------------------------------ boot */

  function boot(options) {
    var el = rootEl();
    if (!el || !options || !options.source) return;
    el.innerHTML = EcoRender.renderSkeleton();
    options.source.load().then(function (result) {
      if (!result || !result.ok) {
        el.innerHTML = EcoRender.renderAccessState(result && result.code ? result.code : "SERVICE_UNAVAILABLE");
        return;
      }
      state.doc = result.document;
      state.played = {};
      /* the approved design has no period switcher: weekly wins when both exist */
      var r = parseHash(location.hash);
      if (r && state.doc.periods[r.period]) {
        state.period = r.period; state.slide = r.slide;
      } else {
        state.period = state.doc.periods.weekly ? "weekly" : "monthly";
        state.slide = 0;
      }
      writeHash();
      window.addEventListener("hashchange", onHashChange);
      render();
    }, function () {
      el.innerHTML = EcoRender.renderAccessState("SERVICE_UNAVAILABLE");
    });
  }

  return { boot: boot, parseHash: parseHash, buildHash: buildHash };
});
