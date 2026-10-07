/* Log Platform — theme engine (SHL-003, D-011).
 *
 * Three modes: AUTO (follow `prefers-color-scheme`), explicit light, explicit
 * dark. AUTO is represented by the ABSENCE of `data-theme` on <html>, so the
 * `@media (prefers-color-scheme: dark)` block in tokens.css keeps applying and
 * an OS change re-themes the page live, with no reload.
 *
 * AUTHORITY (approved stage S13). For an authenticated account on a deployment
 * that has the S13 schema, the DURABLE preference is the account row on the
 * server, and the server has already applied it to <html> before this script
 * runs. The document says so with `data-theme-scope="server"` plus
 * `data-theme-mode`. In that case this script:
 *
 *   * takes the server value as the current mode — it never reads the local
 *     mirror to decide, so signing into a second account in the same browser
 *     cannot inherit the first account's theme;
 *   * OVERWRITES the local mirror with the server value, so a stale mirror
 *     written by a previous account can never resurface later;
 *   * writes a change back to the server in place, so switching theme does not
 *     reload the view, lose filters or reset scroll (`SH-9`).
 *
 * With `data-theme-scope="unavailable"` the account IS authoritative but its
 * preference could not be read — a database fault, a permission denial, a
 * partially migrated schema. The server has already rendered the neutral
 * default, and this script then neither READS nor WRITES the local mirror. The
 * mirror is per-browser and carries no account identity, so consulting it here
 * would let account B silently adopt whatever account A last chose on this
 * machine and present it as B's durable preference. A choice made in this state
 * applies to the open document only and is stated as not stored.
 *
 * Without either marker — the sign-in screen, or a database that confirmed it
 * does not have migration 066 — the pre-S13 browser-local behaviour is used
 * unchanged. The mirror is therefore a per-browser cache, never a cross-account
 * authority, and it never holds an account identity.
 *
 * A write that fails is STATED, and the surface goes back to the last preference
 * the SERVER is known to hold. Leaving the failed choice on screen would present
 * a theme as durably stored that the account row does not have, and — once an
 * earlier choice had already been persisted — would leave the visible state, the
 * browser mirror and the account row describing three different things.
 *
 * WRITE ORDERING. Preference writes are SERIALIZED and carry only the latest
 * intent: at most one request is in flight, and while it is, a further choice
 * replaces the queued one instead of starting a second request. Two overlapping
 * writes could otherwise be applied by the server in the order they ARRIVED
 * rather than the order they were made, leaving the account row on an earlier
 * choice than the one the user is looking at.
 *
 * LAST CONFIRMED DURABLE MODE. A superseded response must not overwrite a newer
 * optimistic choice, but it still carries knowledge: the server told us what it
 * durably stored. That value is remembered — it starts as the mode the server
 * rendered this page with — and it is what the surface reconciles to when the
 * LATEST intent turns out to fail. So once the queue settles: if the latest
 * intent succeeded, the account row, the mirror and the pressed control are all
 * that choice; if it failed, they are all the last choice the server confirmed,
 * and the status line says so. The server stays the authority in both cases —
 * the mirror is never durable account state and never decides anything.
 *
 * Loaded synchronously from <head> so the stored preference is applied before
 * first paint.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "logplatform.theme";
  var MODES = ["auto", "light", "dark"];
  var root = document.documentElement;

  function readStored() {
    try {
      var value = window.localStorage.getItem(STORAGE_KEY);
      return MODES.indexOf(value) === -1 ? "auto" : value;
    } catch (err) {
      /* Private mode or a blocked store: fall back to AUTO for this document. */
      return "auto";
    }
  }

  function writeStored(mode) {
    try {
      if (mode === "auto") {
        window.localStorage.removeItem(STORAGE_KEY);
      } else {
        window.localStorage.setItem(STORAGE_KEY, mode);
      }
    } catch (err) {
      /* Preference stays in effect for this document only. */
    }
  }

  function applyMode(mode) {
    if (mode === "light" || mode === "dark") {
      root.setAttribute("data-theme", mode);
    } else {
      root.removeAttribute("data-theme");
    }
  }

  var themeScope = root.getAttribute("data-theme-scope");
  var serverScope = themeScope === "server";
  var unavailableScope = themeScope === "unavailable";
  var current;

  if (serverScope) {
    var serverMode = root.getAttribute("data-theme-mode");
    current = MODES.indexOf(serverMode) === -1 ? "auto" : serverMode;
    applyMode(current);
    /* Reconciliation, not a read: the account row wins, and the mirror is
       rewritten to match it so no legacy or foreign value survives. */
    writeStored(current);
  } else if (unavailableScope) {
    /* The account is authoritative and could not be read. Render exactly what
       the server rendered, and do NOT consult the mirror: it belongs to the
       browser, not to this account. */
    var renderedMode = root.getAttribute("data-theme-mode");
    current = MODES.indexOf(renderedMode) === -1 ? "auto" : renderedMode;
    applyMode(current);
  } else {
    current = readStored();
    applyMode(current);
  }

  function syncControls(scope) {
    var options = (scope || document).querySelectorAll("[data-theme-option]");
    for (var i = 0; i < options.length; i += 1) {
      var option = options[i];
      option.setAttribute(
        "aria-pressed",
        option.getAttribute("data-theme-option") === current ? "true" : "false"
      );
    }
  }

  function setStatus(message) {
    var nodes = document.querySelectorAll("[data-theme-status]");
    for (var i = 0; i < nodes.length; i += 1) {
      nodes[i].textContent = message || "";
    }
  }

  /* One in-flight write at a time, and only the newest intent is queued. */
  var writeInFlight = false;
  var queuedMode = null;
  /* The last mode the SERVER is known to hold. Under `server` scope the page was
     rendered from the account row, so that is a confirmed durable fact before any
     write is attempted; every accepted write then advances it to the mode the
     SERVER reports having stored, not to the mode we happened to send. */
  var confirmedMode = serverScope ? current : null;

  function sendPreference(form, mode) {
    var body = new URLSearchParams();
    body.set("theme", mode);
    var next = form.querySelector('input[name="next"]');
    if (next) {
      body.set("next", next.value || "");
    }
    return window
      .fetch(form.getAttribute("action"), {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          Accept: "application/json"
        },
        body: body.toString()
      })
      .then(function (response) {
        return response.json().catch(function () {
          return { stored: response.ok };
        });
      })
      .then(function (payload) {
        var stored = !!(payload && payload.stored);
        /* The server answers with the mode it actually stored. It is preferred
           over the mode this client sent, because what the account row holds is
           the server's fact to state, not the caller's to assume. */
        var storedMode = payload && payload.theme;
        return {
          stored: stored,
          mode: stored && MODES.indexOf(storedMode) !== -1 ? storedMode : mode
        };
      })
      .catch(function () {
        return { stored: false, mode: mode };
      });
  }

  /* Put the visible state and the browser mirror back onto the last preference
     the server confirmed. Called when the LATEST intent failed: the optimistic
     choice was never persisted, so it must not keep standing in for one. */
  function reconcileToConfirmed() {
    if (confirmedMode === null || confirmedMode === current) {
      return;
    }
    current = confirmedMode;
    applyMode(current);
    syncControls();
    writeStored(current);
  }

  function pumpPreferenceWrites() {
    if (writeInFlight || queuedMode === null) {
      return;
    }
    var form = document.querySelector("[data-theme-form]");
    if (!form || !window.fetch) {
      /* There is no way to reach the account row from here, so the choice was
         never persisted and must not be left looking as though it was. */
      queuedMode = null;
      setStatus(themeFailedMessage());
      reconcileToConfirmed();
      return;
    }
    var mode = queuedMode;
    queuedMode = null;
    writeInFlight = true;
    sendPreference(form, mode).then(function (result) {
      writeInFlight = false;
      if (result.stored) {
        /* Recorded even when this write has been superseded: the account row
           really does hold this value now, and if the newer intent goes on to
           fail, this is the truth the surface has to fall back to. */
        confirmedMode = result.mode;
      }
      if (queuedMode !== null) {
        /* This write has been superseded. Its outcome describes a choice the
           user has already moved on from, so it must not touch the visible
           state, the mirror or the status: the queued write decides. */
        pumpPreferenceWrites();
        return;
      }
      if (result.stored) {
        setStatus("");
        current = confirmedMode;
        applyMode(current);
        syncControls();
        writeStored(current);
      } else {
        /* The latest intent did not reach the account row. State it, and put
           the surface back on the last preference the server confirmed so the
           visible state, the mirror and the account row cannot disagree. */
        setStatus(form.getAttribute("data-theme-failed-message") || "");
        reconcileToConfirmed();
      }
    });
  }

  function persistToServer(mode) {
    queuedMode = mode;
    pumpPreferenceWrites();
  }

  function setMode(mode) {
    if (MODES.indexOf(mode) === -1) {
      return;
    }
    current = mode;
    applyMode(current);
    syncControls();
    if (serverScope) {
      /* The mirror is only updated once the server confirms, so it can never
         claim a durability the account row does not have. */
      persistToServer(current);
    } else if (unavailableScope) {
      /* Applies to this document only. The mirror is not touched, because a
         value written while the account's own preference is unreadable would be
         indistinguishable from another account's later. */
      setStatus(themeFailedMessage());
    } else {
      writeStored(current);
    }
  }

  function themeFailedMessage() {
    /* The durable control carries the message on its form; the `unavailable`
       control is not a form and carries it on the switcher group itself. */
    var node =
      document.querySelector("[data-theme-form]") ||
      document.querySelector("[data-theme-switcher]");
    return (node && node.getAttribute("data-theme-failed-message")) || "";
  }

  function activate() {
    var groups = document.querySelectorAll("[data-theme-switcher]");
    for (var i = 0; i < groups.length; i += 1) {
      /* The browser-local control is inert without scripting, so it is served
         hidden and revealed only once this handler is attached. The
         server-backed form is a real form and is never hidden. */
      groups[i].removeAttribute("hidden");
    }
    syncControls();
    document.addEventListener("click", function (event) {
      var target = event.target;
      while (target && target !== document) {
        if (target.hasAttribute && target.hasAttribute("data-theme-option")) {
          event.preventDefault();
          setMode(target.getAttribute("data-theme-option"));
          return;
        }
        target = target.parentNode;
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", activate);
  } else {
    activate();
  }

  window.LogPlatformTheme = { get: function () { return current; }, set: setMode };
})();
