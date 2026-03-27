/**
 * Browser interaction recorder for playbook generation.
 *
 * Injected via context.add_init_script(). Captures user interactions (clicks,
 * typing, navigation, selections, scrolling) and sends structured events to
 * Python via window.__cuaRecordEvent() (exposed by context.expose_function).
 *
 * Each interacted element gets multiple selector candidates for fallback chains.
 */
(function () {
  "use strict";

  // Guard against double-injection (SPA soft navigations)
  if (window.__cuaRecorderActive) return;
  window.__cuaRecorderActive = true;

  let __seq = 0;
  let __lastUrl = location.href;

  // =========================================================================
  // Selector Generation
  // =========================================================================

  function isStableId(id) {
    if (!id) return false;
    if (/^[0-9a-f]{8,}$/i.test(id)) return false; // hex hash
    if (/[0-9a-f]{8}-[0-9a-f]{4}/i.test(id)) return false; // UUID fragment
    if (/^(ember|react|vue|ng-|_|rc-|radix-|headlessui-)\d/i.test(id))
      return false; // framework-generated
    if (/^\d+$/.test(id)) return false; // pure numeric
    if (id.length > 80) return false; // suspiciously long
    return true;
  }

  function isUnique(selector) {
    try {
      return document.querySelectorAll(selector).length === 1;
    } catch {
      return false;
    }
  }

  function getAccessibleName(el) {
    const label = el.getAttribute("aria-label");
    if (label) return label.trim();

    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const labelEl = document.getElementById(labelledBy);
      if (labelEl) return labelEl.textContent.trim();
    }

    // For buttons/links, use inner text
    const tag = el.tagName.toLowerCase();
    if (tag === "button" || tag === "a") {
      const text = el.textContent.trim();
      if (text.length > 0 && text.length <= 40) return text;
    }

    return "";
  }

  function getImplicitRole(el) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const roleMap = {
      button: "button",
      a: "link",
      input: type === "checkbox" ? "checkbox" : type === "radio" ? "radio" : type === "submit" ? "button" : null,
      select: "combobox",
      textarea: "textbox",
      nav: "navigation",
      main: "main",
      header: "banner",
      footer: "contentinfo",
      table: "table",
      tr: "row",
      th: "columnheader",
      td: "cell",
    };
    return el.getAttribute("role") || roleMap[tag] || null;
  }

  function generateSelectors(el) {
    const candidates = [];

    // 1. Stable #id
    const id = el.id;
    if (isStableId(id)) {
      const sel = "#" + CSS.escape(id);
      if (isUnique(sel)) candidates.push(sel);
    }

    // 2. role= + accessible name (Playwright syntax)
    const role = getImplicitRole(el);
    const name = getAccessibleName(el);
    if (role && name) {
      candidates.push("role=" + role + "[name='" + name.replace(/'/g, "\\'") + "']");
    } else if (role && !name) {
      // Role without name — less specific but still useful as fallback
      const roleOnly = "role=" + role;
      // Don't add bare roles since they're rarely unique
    }

    // 3. text= for short unique text (buttons, links)
    const tag = el.tagName.toLowerCase();
    if (tag === "button" || tag === "a" || el.getAttribute("role") === "button") {
      const text = el.textContent.trim();
      if (text.length > 0 && text.length <= 40) {
        candidates.push("text=" + text);
      }
    }

    // 4. Attribute-based CSS
    const attrSelectors = [];
    const dataTestId = el.getAttribute("data-testid") || el.getAttribute("data-test-id");
    if (dataTestId) {
      attrSelectors.push("[data-testid='" + dataTestId.replace(/'/g, "\\'") + "']");
    }

    const elName = el.getAttribute("name");
    if (elName) {
      attrSelectors.push(tag + "[name='" + elName.replace(/'/g, "\\'") + "']");
    }

    const placeholder = el.getAttribute("placeholder");
    if (placeholder) {
      attrSelectors.push(tag + "[placeholder='" + placeholder.replace(/'/g, "\\'") + "']");
    }

    const type = el.getAttribute("type");
    if (type && tag === "input") {
      const typesel = "input[type='" + type + "']";
      if (isUnique(typesel)) attrSelectors.push(typesel);
    }

    for (const sel of attrSelectors) {
      if (isUnique(sel)) candidates.push(sel);
    }

    // 5. Structural CSS (climb up 2-3 levels)
    if (candidates.length < 2) {
      const structural = buildStructuralSelector(el);
      if (structural && isUnique(structural)) {
        candidates.push(structural);
      }
    }

    // Deduplicate
    const seen = new Set();
    const unique = [];
    for (const c of candidates) {
      if (!seen.has(c)) {
        seen.add(c);
        unique.push(c);
      }
    }

    return {
      primary: unique[0] || tag,
      fallbacks: unique.slice(1),
      description: describeElement(el),
    };
  }

  function buildStructuralSelector(el) {
    const parts = [];
    let current = el;
    let depth = 0;

    while (current && current !== document.body && depth < 3) {
      const tag = current.tagName.toLowerCase();
      let part = tag;

      // Add distinguishing class if available
      const classes = Array.from(current.classList).filter(
        (c) => !/^(active|selected|hover|focus|open|show|hide|visible|hidden)$/i.test(c) && c.length < 30
      );
      if (classes.length > 0) {
        part += "." + classes[0];
      } else if (current.parentElement) {
        // nth-of-type for disambiguation (matches tag-filtered siblings)
        const siblings = Array.from(current.parentElement.children).filter(
          (s) => s.tagName === current.tagName
        );
        if (siblings.length > 1) {
          const index = siblings.indexOf(current) + 1;
          part += ":nth-of-type(" + index + ")";
        }
      }

      parts.unshift(part);
      current = current.parentElement;
      depth++;
    }

    return parts.join(" > ");
  }

  function describeElement(el) {
    const tag = el.tagName.toLowerCase();
    const role = getImplicitRole(el);
    const text = (el.textContent || "").trim().slice(0, 40);
    const ariaLabel = el.getAttribute("aria-label") || "";
    const placeholder = el.getAttribute("placeholder") || "";

    let desc = role || tag;
    if (ariaLabel) desc += " '" + ariaLabel + "'";
    else if (text) desc += " '" + text + "'";
    else if (placeholder) desc += " '" + placeholder + "'";

    return desc;
  }

  // =========================================================================
  // Event dispatch
  // =========================================================================

  function recordEvent(action, el, params) {
    const payload = {
      seq: ++__seq,
      timestamp: Date.now(),
      action: action,
      selector: el ? generateSelectors(el) : null,
      params: params || {},
      url: location.href,
      pageTitle: document.title,
      elementTag: el ? el.tagName.toLowerCase() : "",
      elementText: el ? (el.textContent || "").trim().slice(0, 80) : "",
    };

    if (typeof window.__cuaRecordEvent === "function") {
      try {
        window.__cuaRecordEvent(JSON.stringify(payload));
      } catch (e) {
        // Silently ignore — function may not be ready yet
      }
    }
  }

  // =========================================================================
  // Text input accumulator (debounced)
  // =========================================================================

  const _inputState = {
    element: null,
    selectors: null,
    startValue: "",
    debounceTimer: null,
  };

  function flushTextInput() {
    if (!_inputState.element) return;

    const el = _inputState.element;
    const currentValue = el.value || el.textContent || "";

    if (currentValue !== _inputState.startValue) {
      recordEvent("key_press", el, { text: currentValue });
    }

    clearTimeout(_inputState.debounceTimer);
    _inputState.element = null;
    _inputState.selectors = null;
    _inputState.startValue = "";
    _inputState.debounceTimer = null;
  }

  // =========================================================================
  // Event listeners (all capture phase)
  // =========================================================================

  // --- Click ---
  document.addEventListener(
    "click",
    function (e) {
      const target = e.composedPath ? e.composedPath()[0] : e.target;
      if (!target || !target.tagName) return;

      const tag = target.tagName.toLowerCase();
      // Skip clicks on text inputs (handled by input accumulator)
      if (tag === "input" && !["submit", "button", "checkbox", "radio"].includes(target.type)) return;
      if (tag === "textarea") return;

      recordEvent("click", target, {});
    },
    true
  );

  // --- Focus in (start text accumulation) ---
  document.addEventListener(
    "focusin",
    function (e) {
      const el = e.target;
      if (!el || !el.tagName) return;

      const tag = el.tagName.toLowerCase();
      const isTextInput =
        (tag === "input" && !["submit", "button", "checkbox", "radio", "file", "password"].includes(el.type)) ||
        tag === "textarea" ||
        el.contentEditable === "true";

      if (!isTextInput) return;

      // Flush any prior input
      if (_inputState.element && _inputState.element !== el) {
        flushTextInput();
      }

      _inputState.element = el;
      _inputState.startValue = el.value || el.textContent || "";
    },
    true
  );

  // --- Input (debounced text capture) ---
  document.addEventListener(
    "input",
    function (e) {
      const el = e.target;
      if (_inputState.element !== el) return;

      clearTimeout(_inputState.debounceTimer);
      _inputState.debounceTimer = setTimeout(flushTextInput, 500);
    },
    true
  );

  // --- Focus out (flush text) ---
  document.addEventListener(
    "focusout",
    function (e) {
      if (_inputState.element && _inputState.element === e.target) {
        flushTextInput();
      }
    },
    true
  );

  // --- Keydown (Enter/Tab/Escape + done hotkey) ---
  document.addEventListener(
    "keydown",
    function (e) {
      // Done hotkey: Ctrl+Shift+S
      if (e.ctrlKey && e.shiftKey && e.key === "S") {
        e.preventDefault();
        if (typeof window.__cuaRecordEvent === "function") {
          window.__cuaRecordEvent(JSON.stringify({ action: "__done" }));
        }
        return;
      }

      // Enter/Tab/Escape on text inputs — flush then record the key
      if (["Enter", "Tab", "Escape"].includes(e.key)) {
        if (_inputState.element) {
          flushTextInput();
        }
        recordEvent("key_press", e.target, { key: e.key });
      }
    },
    true
  );

  // --- Select change ---
  document.addEventListener(
    "change",
    function (e) {
      const el = e.target;
      if (!el || el.tagName.toLowerCase() !== "select") return;

      const option = el.options[el.selectedIndex];
      recordEvent("select", el, {
        value: el.value,
        optionText: option ? option.textContent.trim() : "",
      });
    },
    true
  );

  // --- Scroll (debounced) ---
  let _scrollTimer = null;
  let _scrollStartY = window.scrollY;
  let _scrollStartX = window.scrollX;

  window.addEventListener(
    "scroll",
    function () {
      clearTimeout(_scrollTimer);
      _scrollTimer = setTimeout(function () {
        const dy = window.scrollY - _scrollStartY;
        const dx = window.scrollX - _scrollStartX;

        if (Math.abs(dy) > 100 || Math.abs(dx) > 100) {
          const direction = Math.abs(dy) >= Math.abs(dx) ? (dy > 0 ? "down" : "up") : (dx > 0 ? "right" : "left");
          const amount = Math.abs(dy) >= Math.abs(dx) ? Math.abs(dy) : Math.abs(dx);

          recordEvent("scroll", null, { direction: direction, amount: amount });
        }

        _scrollStartY = window.scrollY;
        _scrollStartX = window.scrollX;
      }, 300);
    },
    { capture: true, passive: true }
  );

  // =========================================================================
  // SPA navigation detection
  // =========================================================================

  function checkNavigation() {
    const currentUrl = location.href;
    if (currentUrl !== __lastUrl) {
      recordEvent("goto", null, { url: currentUrl });
      __lastUrl = currentUrl;
    }
  }

  // Override pushState / replaceState
  const origPushState = history.pushState;
  history.pushState = function () {
    origPushState.apply(this, arguments);
    checkNavigation();
  };

  const origReplaceState = history.replaceState;
  history.replaceState = function () {
    origReplaceState.apply(this, arguments);
    checkNavigation();
  };

  window.addEventListener("popstate", checkNavigation);
  window.addEventListener("hashchange", checkNavigation);
})();
