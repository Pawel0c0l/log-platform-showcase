#!/usr/bin/env python3
"""Dependency-free browser harness for the Driver Eco Dashboard frontend.

WHY THIS EXISTS. The frontend's public contract is two functions —
`window.EcoApp.boot({source})` and `window.EcoRender.renderAccessState(code)` —
and everything below them (DOM, CSS, charts, copy, section order) belongs to the
design. A test that greps the current markup therefore tests the wrong thing: it
would refuse a correct replacement implementation. This module drives the real
frontend in a real browser through those two functions and lets the tests assert
*observable behaviour and data semantics* instead.

WHAT IT IS NOT. It is not a framework, not a page-object layer and not a
screenshot differ. It is the smallest W3C WebDriver client that can open a page,
resize it, run a script and read the result, plus a static server that publishes
the production asset directory unchanged.

NO NEW PRODUCTION FILES. The harness page and its mount script are served from
memory under `/__contract/`; nothing is written into
`assets/driver_eco_dashboard/`. The page loads exactly the production
presentation entrypoints, in the production order, and substitutes only the
repo-owned `js/boot.js` — because `boot.js` performs the capability/session
exchange, which has no place in a fixture test. Its single call into the design
layer is reproduced verbatim by `/__contract/mount.js`.

REQUIREMENTS. `geckodriver` and `firefox` on PATH. Both are present on the
platform host. If they are not, `BrowserUnavailable` is raised and the caller
decides — the suites in this repository treat that as a failure, not a silent
skip, because the browser layer is where the design contract is actually proved.
"""

from __future__ import annotations

import functools
import http.server
import re
import json
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "assets" / "driver_eco_dashboard"

#: The paths the delivery Worker allowlists. The design owns the *content* of
#: the presentation files; the *names* are a production constraint.
PRODUCTION_ASSETS: tuple[str, ...] = (
    "index.html",
    "css/dashboard.css",
    "js/format.js",
    "js/render.js",
    "js/snapshot-source.js",
    "js/app.js",
    "js/boot.js",
    "js/capability-bootstrap.js",
)

#: Loaded by the harness page, in production order. `boot.js` is replaced by the
#: mount script; `capability-bootstrap.js` is a no-op without a `#k=` fragment
#: and is deliberately still loaded, so the page composition stays honest.
PRESENTATION_SCRIPTS: tuple[str, ...] = (
    "js/format.js",
    "js/render.js",
    "js/snapshot-source.js",
    "js/app.js",
)


#: The ONE component allowed to pan horizontally inside its own bounded box:
#: the daily table, which carries a column per event category and cannot be
#: narrowed to a phone without becoming unreadable. Document-level horizontal
#: overflow stays forbidden, and no other element may scroll sideways.
APPROVED_PAN_CLASS = "ed-scrollx"


class BrowserUnavailable(RuntimeError):
    """geckodriver/firefox could not be started."""


# --------------------------------------------------------------- the page ---

#: First thing in the document: capture anything the page reports as broken.
#: Nothing else in the harness can observe a console error after the fact.
_ERROR_SHIM = """
window.__contractErrors = [];
(function () {
  var record = function (kind, message) {
    window.__contractErrors.push({ kind: kind, message: String(message) });
  };
  var original = console.error;
  console.error = function () {
    record("console.error", Array.prototype.map.call(arguments, String).join(" "));
    return original.apply(console, arguments);
  };
  window.addEventListener("error", function (event) {
    if (event.target && event.target !== window && event.target.src) {
      record("resource", event.target.src);
      return;
    }
    record("uncaught", event.message);
  }, true);
  window.addEventListener("unhandledrejection", function (event) {
    record("unhandledrejection", event.reason);
  });
})();
"""

#: The ONLY call the harness makes into the design layer. This is the frozen
#: boundary, reproduced from the repo-owned js/boot.js.
_MOUNT_SCRIPT = """
(function () {
  "use strict";
  var params = new URLSearchParams(location.search);
  var source = null;
  if (params.get("doc")) {
    source = { kind: "static", load: function () {
      return fetch(params.get("doc"), { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (doc) { return window.EcoSnapshotSource.createStaticSource(doc).load(); });
    } };
  } else if (params.get("fixture")) {
    source = window.EcoSnapshotSource.createFixtureSource({
      basePath: "/fixtures/", name: params.get("fixture")
    });
  }
  window.__contractApi = {
    hasEcoApp: !!(window.EcoApp && typeof window.EcoApp.boot === "function"),
    hasEcoRender: !!(window.EcoRender && typeof window.EcoRender.renderAccessState === "function"),
    hasSnapshotSource: !!window.EcoSnapshotSource
  };
  if (!source) { window.__contractMounted = "no-source"; return; }
  try {
    window.EcoApp.boot({ source: source });
    window.__contractMounted = "ok";
  } catch (error) {
    window.__contractMounted = "threw";
    window.__contractErrors.push({ kind: "boot", message: String(error) });
  }
})();
"""


def _frame_page() -> str:
    """The page under test: the production presentation entrypoints, in order."""
    scripts = "\n".join(f'<script src="/{path}"></script>' for path in PRESENTATION_SCRIPTS)
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Driver Eco Dashboard — contract harness</title>
<script src="/__contract/shim.js"></script>
<script src="/js/capability-bootstrap.js"></script>
<link rel="stylesheet" href="/css/dashboard.css">
</head>
<body>
<div id="eco-root" data-snapshot-url="/api/snapshot"></div>
{scripts}
<script src="/__contract/mount.js"></script>
</body>
</html>
"""


#: WHY AN IFRAME. Firefox refuses to make its window narrower than 500 px, and
#: subtracts the scrollbar on top of that, so `setWindowRect` cannot produce a
#: 320 px or 390 px layout viewport — a sweep built on it would silently test
#: ~488 px five times and report mobile coverage it never had. An iframe with an
#: exact CSS width IS the layout viewport for everything inside it: media
#: queries, `clientWidth`, `scrollWidth` and element geometry all resolve
#: against it, so the widths below are the widths under test.
_SHELL_PAGE = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<title>viewport shell</title>
<style>
  html, body { margin: 0; padding: 0; background: #fff; }
  iframe { display: block; border: 0; }
</style>
</head>
<body>
<script src="/__contract/shell.js"></script>
</body>
</html>
"""

_SHELL_SCRIPT = """
(function () {
  var params = new URLSearchParams(location.search);
  var width = parseInt(params.get("w") || "1280", 10);
  var height = parseInt(params.get("h") || "900", 10);
  var inner = new URLSearchParams();
  ["fixture", "doc"].forEach(function (key) {
    if (params.get(key)) inner.set(key, params.get(key));
  });
  /* The dashboard's own route is a fragment, as production requires; it is
   * carried here as a query parameter so that two different routes are two
   * different shell URLs and the frame really reloads. */
  var route = params.get("route") ? "#" + params.get("route") : "";
  var frame = document.createElement("iframe");
  frame.id = "viewport";
  frame.style.width = width + "px";
  frame.style.height = height + "px";

  frame.addEventListener("load", function () { window.__viewportReady = true; });

  frame.src = "/__contract/frame.html?" + inner.toString() + route;
  document.body.appendChild(frame);
})();
"""


# ------------------------------------------------------------- the server ---


class _Handler(http.server.SimpleHTTPRequestHandler):
    virtual: Mapping[str, bytes] = {}

    def log_message(self, *args: Any) -> None:  # noqa: D102 - silence the server
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]
        payload = self.virtual.get(path)
        if payload is None:
            super().do_GET()
            return
        if path.endswith(".js"):
            content_type = "text/javascript; charset=utf-8"
        elif path.endswith(".json"):
            content_type = "application/json; charset=utf-8"
        else:
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


class ContractServer:
    """Serves `assets/driver_eco_dashboard/` plus in-memory `/__contract/` files.

    `documents` publishes synthetic snapshots at `/__contract/doc/<name>.json`
    without writing anything into the repository, which is what lets the tests
    cover states the checked-in fixture set deliberately does not contain.
    """

    def __init__(self, documents: Mapping[str, Any] | None = None) -> None:
        virtual: dict[str, bytes] = {
            "/__contract/harness.html": _SHELL_PAGE.encode("utf-8"),
            "/__contract/shell.js": _SHELL_SCRIPT.encode("utf-8"),
            "/__contract/frame.html": _frame_page().encode("utf-8"),
            "/__contract/shim.js": _ERROR_SHIM.encode("utf-8"),
            "/__contract/mount.js": _MOUNT_SCRIPT.encode("utf-8"),
        }
        for name, document in (documents or {}).items():
            body = json.dumps(document, ensure_ascii=False).encode("utf-8")
            virtual[f"/__contract/doc/{name}.json"] = body
        handler = functools.partial(
            type("_BoundHandler", (_Handler,), {"virtual": virtual}),
            directory=str(APP_ROOT),
        )
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler, bind_and_activate=False)
        self._server.allow_reuse_address = True
        self._server.daemon_threads = True
        self._server.server_bind()
        self._server.server_activate()
        self.port = self._server.server_address[1]
        self._serial = 0
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "ContractServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def harness_url(self, *, fixture: str | None = None, document: str | None = None,
                    route: str = "", width: int = 1280, height: int = 900) -> str:
        """A URL that always produces a fresh mount.

        `route` is the dashboard's own `#weekly/summary` fragment. It travels in
        the query string because a fragment-only difference would be a
        same-document navigation: the shell script would not re-run, the frame
        would not reload, and the previous route would be measured instead. The
        serial does the same job for two mounts of the same state.
        """
        self._serial += 1
        query = [f"w={width}", f"h={height}", f"n={self._serial}"]
        if fixture:
            query.append(f"fixture={fixture}")
        elif document:
            query.append(f"doc=/__contract/doc/{document}.json")
        if route:
            query.append(f"route={route.lstrip('#')}")
        return f"{self.origin}/__contract/harness.html?{'&'.join(query)}"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ------------------------------------------------------------ the browser ---


class Browser:
    """The smallest W3C WebDriver client that can prove a frontend contract."""

    def __init__(self, *, width: int = 1280, height: int = 900,
                 reduced_motion: bool = False) -> None:
        if shutil.which("geckodriver") is None or shutil.which("firefox") is None:
            raise BrowserUnavailable(
                "geckodriver and firefox are required for the frontend contract suite"
            )
        self._port = _free_port()
        self._process = subprocess.Popen(
            ["geckodriver", "--port", str(self._port), "--host", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._session: str | None = None
        try:
            self._await_driver()
            payload = self._request("POST", "/session", {
                "capabilities": {"alwaysMatch": {
                    "browserName": "firefox",
                    "acceptInsecureCerts": False,
                    "pageLoadStrategy": "normal",
                    "moz:firefoxOptions": {
                        "args": ["-headless"],
                        #: `ui.prefersReducedMotion: 1` is how Firefox is told
                        #: the OS asked for reduced motion; the page then sees
                        #: `prefers-reduced-motion: reduce` for real, which is
                        #: the only honest way to test that contract.
                        "prefs": ({"ui.prefersReducedMotion": 1}
                                  if reduced_motion else {}),
                    },
                }}
            })
            self._session = payload["value"]["sessionId"]
            self.resize(width, height)
        except Exception as error:  # noqa: BLE001 - always surface as unavailable
            self.quit()
            if isinstance(error, BrowserUnavailable):
                raise
            raise BrowserUnavailable(f"could not start firefox: {error}") from error

    # -- plumbing ---------------------------------------------------------

    def _await_driver(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._process.poll() is not None:
                raise BrowserUnavailable("geckodriver exited immediately")
            try:
                self._request("GET", "/status")
                return
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.1)
        raise BrowserUnavailable("geckodriver did not become ready")

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self._port}{path}",
            data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"webdriver {method} {path} failed: {detail}") from error

    # -- surface ----------------------------------------------------------

    def resize(self, width: int, height: int) -> None:
        self._request("POST", f"/session/{self._session}/window/rect",
                      {"width": width, "height": height, "x": 0, "y": 0})

    def open(self, url: str) -> None:
        self._request("POST", f"/session/{self._session}/url", {"url": url})

    def script(self, source: str, *args: Any) -> Any:
        return self._request("POST", f"/session/{self._session}/execute/sync",
                             {"script": source, "args": list(args)})["value"]

    def quit(self) -> None:
        try:
            if self._session:
                self._request("DELETE", f"/session/{self._session}")
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass
        finally:
            self._session = None
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def __enter__(self) -> "Browser":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.quit()

    # -- contract helpers -------------------------------------------------

    #: Every helper runs against the framed document, which is the layout
    #: viewport under test. `doc`/`win` are bound for the caller.
    _IN_FRAME = (
        "var frame = document.getElementById('viewport');"
        "var win = frame ? frame.contentWindow : window;"
        "var doc = win.document;"
        "var root = doc.getElementById('eco-root');"
    )

    def in_frame(self, body: str, *args: Any) -> Any:
        """Run a script inside the framed page. `win`, `doc` and `root` are bound."""
        return self.script(self._IN_FRAME + body, *args)

    def ready(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.in_frame(
                "return [win.__contractMounted || null, doc.readyState, !!root,"
                " !!window.__viewportReady];"
            )
            if state and state[0] and state[1] == "complete" and state[2] and state[3]:
                return
            time.sleep(0.1)
        raise AssertionError("the dashboard page never finished loading")

    def settle(self, timeout: float = 20.0, quiet_samples: int = 3,
               interval: float = 0.15) -> float:
        """Wait until the dashboard root stops changing.

        Design-agnostic: it watches the rendered markup rather than any element.
        The returned settle time is itself a contract — motion must finish, and
        nothing may animate for ever.
        """
        deadline = time.time() + timeout
        started = time.time()
        previous, stable = None, 0
        while time.time() < deadline:
            current = self.in_frame(
                "return root ? root.innerHTML.length + '|' + root.textContent.length : '';"
            )
            if current and current == previous:
                stable += 1
                if stable >= quiet_samples:
                    return time.time() - started
            else:
                stable = 0
            previous = current
            time.sleep(interval)
        raise AssertionError("the dashboard never stopped changing")

    def fit_viewport(self, target: int, attempts: int = 4) -> int:
        """Give the frame back what its scrollbar took.

        A rendered dashboard is taller than the frame, so a classic scrollbar
        appears and eats into the LAYOUT viewport: a 320px frame lays out at
        308px. Without this the sweep would quietly test a width nobody asked
        for. The scrollbar only exists once the content is there, so the
        correction happens after the first paint, not on frame load.
        """
        for _ in range(attempts):
            achieved = self.script(
                """
                var frame = document.getElementById('viewport');
                var inside = frame.contentDocument;
                var deficit = arguments[0] - inside.documentElement.clientWidth;
                if (deficit !== 0) {
                  frame.style.width = (parseInt(frame.style.width, 10) + deficit) + 'px';
                }
                return inside.documentElement.clientWidth;
                """,
                target,
            )
            if achieved == target:
                return achieved
        return self.viewport()

    def mount(self, url: str, *, timeout: float = 20.0, viewport: int | None = None) -> float:
        self.open(url)
        self.ready(timeout=timeout)
        elapsed = self.settle(timeout=timeout)
        if viewport is not None:
            self.fit_viewport(viewport)
            elapsed += self.settle(timeout=timeout)
        return elapsed

    def errors(self) -> list[dict[str, str]]:
        return self.in_frame("return win.__contractErrors || [];")

    def api(self) -> dict[str, bool]:
        return self.in_frame("return win.__contractApi || {};")

    def hash(self) -> str:
        return self.in_frame("return win.location.hash;")

    def set_hash(self, value: str) -> None:
        self.in_frame("win.location.hash = arguments[0];", value)

    def text(self) -> str:
        """Everything a driver can read, including the accessible names."""
        return self.in_frame(
            """
            if (!root) return '';
            var parts = [root.innerText || root.textContent || ''];
            var nodes = root.querySelectorAll('[aria-label],[title],[alt],[aria-valuetext]');
            for (var i = 0; i < nodes.length; i++) {
              var node = nodes[i];
              parts.push(node.getAttribute('aria-label') || '');
              parts.push(node.getAttribute('title') || '');
              parts.push(node.getAttribute('alt') || '');
              parts.push(node.getAttribute('aria-valuetext') || '');
            }
            return parts.join(' \\n ');
            """
        )

    def markup(self) -> str:
        return self.in_frame("return root ? root.innerHTML : '';")

    def viewport(self) -> int:
        return self.in_frame("return doc.documentElement.clientWidth;")

    def overflow(self) -> dict[str, Any]:
        """Horizontal geometry, with the document and the components separated.

        The product rule is not "nothing may scroll sideways" — it is that the
        DOCUMENT may not. A dedicated analytical surface is allowed to pan
        inside its own bounded box (see `APPROVED_PAN_CLASS`), which is how a
        wide daily table stays readable at 320 px without shrinking its columns
        to nothing. So this reports three different things:

          * `scrollWidth` / `bodyScrollWidth` vs `clientWidth` — the document
            rule, which admits no exception;
          * `widestRight` — how far the widest element reaches, IGNORING the
            content of a pan track, because that content is clipped by the
            track and never reaches the viewport edge;
          * `panners` — every element that scrolls horizontally, with the class
            check, its own bounding box and whether its content can still be
            reached from the keyboard. An unapproved entry here is a failure.
        """
        return self.in_frame(
            """
            var TRACK = arguments[0];
            var page = doc.documentElement;
            var widest = 0, offender = '';
            var panners = [];
            var nodes = doc.querySelectorAll('#eco-root *');
            for (var i = 0; i < nodes.length; i++) {
              var node = nodes[i];
              var box = node.getBoundingClientRect();
              if (box.width === 0 && box.height === 0) continue;

              /* A pan surface is an element the user can actually scroll
               * sideways: `overflow-x: auto|scroll` AND content wider than the
               * box. Clipping (`overflow: hidden`, the ellipsis truncation on
               * table cells, the 1px `.sr-only` box) is not panning — nothing
               * moves and nothing is reachable by scrolling. */
              var overflowX = win.getComputedStyle(node).overflowX || '';
              if ((overflowX === 'auto' || overflowX === 'scroll') &&
                  node.scrollWidth > node.clientWidth + 1) {
                var focusable = node.querySelectorAll(
                  'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])');
                panners.push({
                  selector: node.tagName + '.' + (node.className || ''),
                  approved: node.classList.contains(TRACK),
                  left: Math.floor(box.left),
                  right: Math.ceil(box.right),
                  scrollWidth: node.scrollWidth,
                  clientWidth: node.clientWidth,
                  overflowX: overflowX,
                  focusables: focusable.length,
                  tabbable: node.tabIndex >= 0
                });
              }

              /* content inside a pan track is clipped by the track; the track
               * itself is measured, which is the box that must stay inside. */
              var track = node.closest ? node.closest('.' + TRACK) : null;
              if (track && track !== node) continue;

              if (box.right > widest) {
                widest = box.right;
                offender = node.tagName + '.' + (node.className || '');
              }
            }
            return {
              scrollWidth: page.scrollWidth,
              clientWidth: page.clientWidth,
              bodyScrollWidth: doc.body ? doc.body.scrollWidth : 0,
              widestRight: Math.ceil(widest),
              offender: offender,
              panners: panners
            };
            """,
            APPROVED_PAN_CLASS,
        )

    def resources(self) -> list[str]:
        return self.in_frame(
            "return (win.performance.getEntriesByType('resource') || [])"
            ".map(function (entry) { return entry.name; });"
        )


# ------------------------------------------------------------- utilities ---


def normalise(text: str) -> str:
    """Fold the typography the pl-PL formatter emits.

    The no-break spaces are *inside* numbers (`1 100`), so they collapse; an
    ordinary space still separates two different values, which is what keeps
    `contains_number` from matching a digit run that spans two of them.
    """
    for separator in ("\u00a0", "\u202f", "\u2009"):
        text = text.replace(separator, "")
    for minus in ("\u2212", "\u2013", "\u2014"):
        text = text.replace(minus, "-")
    return text


def spellings(value: int | float) -> tuple[str, ...]:
    """Every plausible rendering of one number: formatting stays design-owned."""
    if isinstance(value, int) or float(value).is_integer():
        whole = str(int(value))
        return (whole, f"{int(value):,}".replace(",", " "), f"{int(value):,}".replace(",", "."))
    return (
        f"{value:.2f}".replace(".", ","), f"{value:.2f}",
        f"{value:.1f}".replace(".", ","), f"{value:.1f}",
    )


def contains_number(haystack: str, value: int | float) -> bool:
    """Is this exact value rendered anywhere, in any plausible spelling?

    Digit boundaries are enforced on both sides, so `137` does not match inside
    `1370` and two adjacent values cannot be read as one.
    """
    text = normalise(haystack)
    for candidate in spellings(value):
        pattern = r"(?<![\d])" + re.escape(normalise(candidate)) + r"(?![\d])"
        if re.search(pattern, text):
            return True
    return False


def readable(markup: str) -> str:
    """Markup minus its inline style attributes.

    Layout maths legitimately reaches the DOM as `style="width:63%"` (the CSP
    allows style attributes for exactly that). Those numbers are geometry, not
    business values, so a leak check must not read them as rendered content.
    """
    return re.sub(r'\sstyle\s*=\s*"[^"]*"', " ", markup)


def load_fixture(name: str) -> dict:
    path = APP_ROOT / "fixtures" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_names() -> Sequence[str]:
    return sorted(path.stem for path in (APP_ROOT / "fixtures").glob("*.json"))
