/* Driver Eco Dashboard — app.js
 * 1:1 port of the approved slide behaviour:
 *  - free scroll inside a slide; at its boundary the next FRESH wheel gesture
 *    (>180 ms since the previous wheel event — touchpad inertia arrives denser
 *    and never triggers) or a >70 px swipe starting at the boundary moves to
 *    the next/previous slide;
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

  var state = {
    doc: null, period: null, slide: 0,
    played: {},          /* slide index -> true (once per load) */
    lock: false, lastWheel: 0,
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

  function applySlide() {
    var mover = document.getElementById("ed-mover");
    if (mover) mover.style.setProperty("--seg", String(state.slide));
    [].forEach.call(document.querySelectorAll(".ed-dot"), function (d) {
      d.setAttribute("aria-current", d.getAttribute("data-i") === String(state.slide) ? "true" : "false");
    });
  }

  function goTo(i) {
    if (!state.doc || i < 0 || i > 2 || i === state.slide || state.lock) return;
    if (!slides().length) return;
    state.lock = true;
    state.slide = i;
    applySlide();
    writeHash();
    setTimeout(function () { state.lock = false; }, TRANS_MS + 300);
    setTimeout(function () { startAnim(i); }, Math.min(TRANS_MS, 500));
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

  function onClick(ev) {
    var t = ev.target && ev.target.closest ? ev.target.closest("[data-action]") : null;
    if (!t) return;
    var action = t.getAttribute("data-action");
    if (action === "retry") { location.reload(); return; }
    if (action === "dot") { goTo(parseInt(t.getAttribute("data-i"), 10)); return; }
    if (action === "axtap") { t.classList.toggle("show"); }
  }

  function onWheel(ev) {
    if (!state.doc || state.lock) return;
    var el = curSlideEl();
    if (!el) return;
    var now = performance.now();
    /* a "fresh" gesture: >180 ms since the last wheel event — inertia trains
     * arrive denser and therefore never trigger a transition on their own */
    var fresh = now - state.lastWheel > 180;
    state.lastWheel = now;
    if (Math.abs(ev.deltaY) < 8 || !fresh) return;
    if (ev.deltaY > 0 && el.scrollHeight - el.scrollTop - el.clientHeight < 2) goTo(state.slide + 1);
    else if (ev.deltaY < 0 && el.scrollTop < 2) goTo(state.slide - 1);
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
    if (dy > 70 && state.touchAtBot) { state.touchDone = true; goTo(state.slide + 1); }
    else if (dy < -70 && state.touchAtTop) { state.touchDone = true; goTo(state.slide - 1); }
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
