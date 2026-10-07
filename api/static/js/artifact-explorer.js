/* Artifact Explorer progressive enhancement.
 *
 * Extracted verbatim from the stylesheet-sized inline <script> the module used
 * to emit on every page, so it now travels through the shared page-scoped asset
 * architecture (cached, versioned, loaded only by the Artifacts module).
 * Behaviour is unchanged: multi-select combo filters over the existing GET
 * filter form, and copy-to-clipboard technical cells.
 *
 * Every control it enhances already works without it: the combo inputs are
 * plain text inputs the server parses, and a copy cell is a normal table cell.
 */
(function () {
  function splitValues(text) {
    return String(text || "").split(",").map(function (part) {
      return part.trim();
    }).filter(Boolean);
  }
  function syncCombo(combo) {
    var input = combo.querySelector(".combo-input");
    var checks = Array.prototype.slice.call(combo.querySelectorAll("input[type=checkbox]"));
    var selected = splitValues(input.value);
    checks.forEach(function (check) {
      check.checked = selected.indexOf(check.value) !== -1;
    });
  }
  function updateInput(combo) {
    var input = combo.querySelector(".combo-input");
    var checks = Array.prototype.slice.call(combo.querySelectorAll("input[type=checkbox]"));
    input.value = checks.filter(function (check) {
      return check.checked;
    }).map(function (check) {
      return check.value;
    }).join(", ");
  }
  function filterOptions(combo) {
    var input = combo.querySelector(".combo-input");
    var query = String(input.value || "").toLowerCase();
    var lastPart = query.split(",").pop().trim();
    var visible = 0;
    Array.prototype.slice.call(combo.querySelectorAll(".combo-option")).forEach(function (option) {
      var value = String(option.getAttribute("data-value") || "").toLowerCase();
      var show = !lastPart || value.indexOf(lastPart) !== -1;
      option.style.display = show ? "" : "none";
      if (show) visible += 1;
    });
    var empty = combo.querySelector(".combo-empty");
    if (empty) empty.style.display = visible ? "none" : "";
  }
  document.querySelectorAll("[data-combo]").forEach(function (combo) {
    var input = combo.querySelector(".combo-input");
    var menu = combo.querySelector(".combo-menu");
    if (!input || !menu) return;
    input.addEventListener("focus", function () {
      combo.classList.add("open");
      filterOptions(combo);
    });
    input.addEventListener("click", function () {
      combo.classList.add("open");
      filterOptions(combo);
    });
    input.addEventListener("input", function () {
      syncCombo(combo);
      combo.classList.add("open");
      filterOptions(combo);
    });
    combo.querySelectorAll("input[type=checkbox]").forEach(function (check) {
      check.addEventListener("change", function () {
        updateInput(combo);
        filterOptions(combo);
        input.focus();
      });
    });
  });
  document.addEventListener("click", function (event) {
    document.querySelectorAll("[data-combo]").forEach(function (combo) {
      if (!combo.contains(event.target)) combo.classList.remove("open");
    });
  });
  document.querySelectorAll("form[data-artifact-filters]").forEach(function (form) {
    form.addEventListener("submit", function () {
      form.querySelectorAll("input[data-combo-hidden]").forEach(function (node) {
        node.remove();
      });
      form.querySelectorAll("[data-combo]").forEach(function (combo) {
        var input = combo.querySelector(".combo-input");
        if (!input) return;
        var checks = Array.prototype.slice.call(combo.querySelectorAll("input[type=checkbox]"));
        var selectedValues = checks.filter(function (check) {
          return check.checked;
        }).map(function (check) {
          return check.value;
        });
        if (selectedValues.length) {
          selectedValues.forEach(function (value) {
            var hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.name = input.name;
            hidden.value = value;
            hidden.setAttribute("data-combo-hidden", "1");
            form.appendChild(hidden);
          });
        } else if (String(input.value || "").trim()) {
          var hidden = document.createElement("input");
          hidden.type = "hidden";
          hidden.name = input.name + "_search";
          hidden.value = String(input.value || "").trim();
          hidden.setAttribute("data-combo-hidden", "1");
          form.appendChild(hidden);
        }
        input.disabled = true;
      });
    });
  });
  var copyStatusTimer = null;
  function setCopyStatus(message, isError) {
    document.querySelectorAll("[data-copy-status]").forEach(function (node) {
      node.textContent = message || "";
      node.classList.toggle("copy-error", Boolean(isError));
    });
    if (copyStatusTimer) window.clearTimeout(copyStatusTimer);
    if (message) {
      copyStatusTimer = window.setTimeout(function () {
        setCopyStatus("", false);
      }, 1600);
    }
  }
  function fallbackCopyText(text) {
    return new Promise(function (resolve, reject) {
      var textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      document.body.appendChild(textarea);
      textarea.select();
      try {
        if (document.execCommand("copy")) resolve();
        else reject(new Error("copy command failed"));
      } catch (err) {
        reject(err);
      } finally {
        textarea.remove();
      }
    });
  }
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return fallbackCopyText(text);
  }
  document.addEventListener("click", function (event) {
    var target = event.target && event.target.closest ? event.target.closest("[data-copy-value]") : null;
    if (!target || target.closest(".actions")) return;
    var value = target.getAttribute("data-copy-value") || "";
    if (!value) return;
    copyText(value).then(function () {
      target.classList.add("copied");
      setCopyStatus("Copied", false);
      window.setTimeout(function () { target.classList.remove("copied"); }, 900);
    }).catch(function () {
      setCopyStatus("Copy failed", true);
    });
  });
})();
