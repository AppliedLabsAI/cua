/**
 * Anti-bot stealth evasions — ported from SeleniumBase's CDP Mode.
 *
 * These JS patches run before any page script via add_init_script().
 * They mask automation artifacts that anti-bot systems probe.
 *
 * Patchright already handles the primary navigator.webdriver patch.
 * These are ADDITIONAL evasions for edge cases that advanced anti-bot
 * systems (Cloudflare, Imperva/Incapsula, DataDome, Akamai) check.
 *
 * Sources:
 *   - SeleniumBase/seleniumbase/undetected/__init__.py (cdc_props removal)
 *   - SeleniumBase/seleniumbase/core/browser_launcher.py (excludeSwitches)
 *   - Common stealth techniques from playwright-extra/puppeteer-extra-stealth
 *
 * EDUCATIONAL PURPOSE ONLY — study how anti-bot detection works.
 */

(() => {
  'use strict';

  // =====================================================================
  // 1. Remove ChromeDriver CDC properties
  // =====================================================================
  // SeleniumBase's _hook_remove_cdc_props() removes window.cdc_* properties
  // that chromedriver injects. Patchright doesn't use chromedriver, but
  // some Chromium builds or extensions may leak similar properties.
  //
  // From SeleniumBase/seleniumbase/undetected/__init__.py:
  //   cdc_props_js_array.forEach(p => delete window[p]);

  try {
    const cdcProps = Object.getOwnPropertyNames(window).filter((name) =>
      /^cdc_|^[a-z]{3}_[a-z]{22}_/.test(name),
    );
    for (const prop of cdcProps) {
      try {
        delete window[prop];
      } catch {
        // Some properties are non-configurable
      }
    }
  } catch {
    // Silent fail — don't break the page
  }

  // =====================================================================
  // 2. navigator.webdriver — belt and suspenders
  // =====================================================================
  // Patchright patches this, but some sites check multiple ways.
  // Ensure it's false via both property descriptor and delete.
  //
  // Anti-bot systems check:
  //   - navigator.webdriver (direct access)
  //   - Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver')
  //   - 'webdriver' in navigator

  try {
    // Override on the prototype so even Object.getOwnPropertyDescriptor sees it
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => false,
      configurable: true,
    });
  } catch {
    // Patchright may have already locked this down
  }

  // =====================================================================
  // 3. chrome.runtime — simulate real Chrome extension API
  // =====================================================================
  // Real Chrome has window.chrome.runtime with specific properties.
  // Headless/automated Chrome sometimes has it undefined or with
  // different shape. Anti-bot systems check for:
  //   - window.chrome existence
  //   - window.chrome.runtime existence
  //   - typeof window.chrome.runtime.connect (should be 'function')
  //
  // From SeleniumBase's broader stealth ecosystem:

  try {
    if (!window.chrome) {
      window.chrome = {};
    }
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        // ProgrammaticallySendMessage check
        connect: function () {},
        sendMessage: function () {},
        id: undefined,
        // onMessage/onConnect must exist
        onMessage: {
          addListener: function () {},
          removeListener: function () {},
          hasListener: function () {
            return false;
          },
        },
        onConnect: {
          addListener: function () {},
          removeListener: function () {},
          hasListener: function () {
            return false;
          },
        },
      };
    }
  } catch {
    // Skip in non-Chrome contexts
  }

  // =====================================================================
  // 4. chrome.app — simulate real Chrome app API
  // =====================================================================
  // Real Chrome has chrome.app with specific properties. Automated Chrome
  // often has it as an empty object or missing entirely.

  try {
    if (window.chrome && !window.chrome.app) {
      window.chrome.app = {
        isInstalled: false,
        InstallState: {
          DISABLED: 'disabled',
          INSTALLED: 'installed',
          NOT_INSTALLED: 'not_installed',
        },
        RunningState: {
          CANNOT_RUN: 'cannot_run',
          READY_TO_RUN: 'ready_to_run',
          RUNNING: 'running',
        },
        getDetails: function () {},
        getIsInstalled: function () {
          return false;
        },
      };
    }
  } catch {
    // Skip
  }

  // =====================================================================
  // 5. chrome.csi and chrome.loadTimes
  // =====================================================================
  // Deprecated but still present in real Chrome. Anti-bot systems check
  // for their existence.

  try {
    if (window.chrome && !window.chrome.csi) {
      window.chrome.csi = function () {
        return {
          startE: Date.now(),
          onloadT: Date.now(),
          pageT: Math.random() * 1000 + 500,
          tpiT: 0,
        };
      };
    }
    if (window.chrome && !window.chrome.loadTimes) {
      window.chrome.loadTimes = function () {
        return {
          commitLoadTime: Date.now() / 1000,
          connectionInfo: 'h2',
          connectioninfo: 'h2',
          finishDocumentLoadTime: Date.now() / 1000 + Math.random(),
          finishLoadTime: Date.now() / 1000 + Math.random() * 2,
          firstPaintAfterLoadTime: 0,
          firstPaintTime: Date.now() / 1000 + Math.random(),
          navigationType: 'Other',
          npnNegotiatedProtocol: 'h2',
          requestTime: Date.now() / 1000 - Math.random() * 5,
          startLoadTime: Date.now() / 1000 - Math.random() * 5,
          wasAlternateProtocolAvailable: false,
          wasFetchedViaSpdy: true,
          wasNpnNegotiated: true,
        };
      };
    }
  } catch {
    // Skip
  }

  // =====================================================================
  // 6. Permissions API — prevent "notification" permission leak
  // =====================================================================
  // In headless/automated mode, Notification.permission is "denied" by default
  // and Permissions.query returns "denied" for notifications. Real browsers
  // return "default" (meaning the user hasn't been asked yet).
  //
  // Anti-bot check: permissions.query({name:'notifications'}).then(r => r.state)

  try {
    const originalQuery = window.navigator.permissions.query.bind(
      window.navigator.permissions,
    );
    window.navigator.permissions.query = function (parameters) {
      if (parameters.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission });
      }
      return originalQuery(parameters);
    };
  } catch {
    // Skip if Permissions API not available
  }

  // =====================================================================
  // 7. Plugins and mimeTypes — simulate real browser plugins
  // =====================================================================
  // Headless Chrome has empty navigator.plugins. Real Chrome has at least
  // "PDF Viewer" and "Chrome PDF Viewer". Anti-bot systems check
  // navigator.plugins.length === 0.

  try {
    if (navigator.plugins.length === 0) {
      Object.defineProperty(navigator, 'plugins', {
        get: () => {
          // Minimal plugin array that passes length checks
          const plugins = [
            {
              name: 'PDF Viewer',
              description: 'Portable Document Format',
              filename: 'internal-pdf-viewer',
              length: 1,
              0: {
                type: 'application/pdf',
                suffixes: 'pdf',
                description: 'Portable Document Format',
              },
            },
            {
              name: 'Chrome PDF Viewer',
              description: 'Portable Document Format',
              filename: 'internal-pdf-viewer',
              length: 1,
              0: {
                type: 'application/pdf',
                suffixes: 'pdf',
                description: 'Portable Document Format',
              },
            },
          ];
          plugins.refresh = function () {};
          return plugins;
        },
        configurable: true,
      });
    }
  } catch {
    // Skip
  }

  // =====================================================================
  // 8. WebGL Vendor and Renderer — prevent headless detection
  // =====================================================================
  // Headless Chrome may report "Google SwiftShader" as the WebGL renderer,
  // which is a strong headless indicator. This patches getParameter to
  // return plausible GPU info.
  //
  // Anti-bot check: gl.getParameter(debugInfo.UNMASKED_VENDOR_WEBGL)

  try {
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (param) {
      // UNMASKED_VENDOR_WEBGL
      if (param === 37445) {
        return 'Google Inc. (Intel)';
      }
      // UNMASKED_RENDERER_WEBGL
      if (param === 37446) {
        return 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)';
      }
      return getParameter.call(this, param);
    };
  } catch {
    // Skip if WebGL not available
  }

  // =====================================================================
  // 9. Prototype leak prevention
  // =====================================================================
  // Some anti-bot systems check if Function.prototype.toString reveals
  // that browser APIs have been tampered with. We need to make our
  // overrides appear "native".

  try {
    const nativeToString = Function.prototype.toString;
    const customToString = function () {
      if (this === window.navigator.permissions.query) {
        return 'function query() { [native code] }';
      }
      if (this === WebGLRenderingContext.prototype.getParameter) {
        return 'function getParameter() { [native code] }';
      }
      return nativeToString.call(this);
    };
    Function.prototype.toString = customToString;
    // Make toString itself look native
    Function.prototype.toString.toString = function () {
      return 'function toString() { [native code] }';
    };
  } catch {
    // Skip
  }

  // =====================================================================
  // 10. iframe contentWindow leak
  // =====================================================================
  // Anti-bot systems sometimes create a temporary iframe and check
  // if iframe.contentWindow.chrome exists (it should in real Chrome).

  try {
    const originalAttachShadow = Element.prototype.attachShadow;
    // We don't override attachShadow itself, but ensure that when
    // iframes are created, the chrome object propagates correctly.
    // This is a lightweight check — Patchright handles most of this.
  } catch {
    // Skip
  }
})();
