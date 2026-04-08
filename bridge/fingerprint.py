"""Realistic browser fingerprint generation and injection.

Uses browserforge (same data source as Camoufox) to generate consistent
fingerprints from real browser telemetry, then injects them via CDP and
init scripts so every property anti-bot systems probe is coherent.

The key insight from Camoufox: individual JS patches (like our stealth_evasions.js)
can be detected because they create *inconsistencies* — e.g. a Linux user-agent
with a macOS WebGL renderer. A holistic fingerprint where all values come from the
same real browser session is much harder to detect.

References:
    - https://camoufox.com/fingerprint/
    - https://github.com/nicegui-main/browserforge
    - https://github.com/nicegui-main/camoufox
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from browserforge.fingerprints import FingerprintGenerator, Screen

logger = logging.getLogger(__name__)

_generator = FingerprintGenerator()

# Cached fingerprints live next to storage state on the Modal volume.
_FINGERPRINT_DIR = Path("/recordings/.fingerprints")


def generate_fingerprint(
    width: int = 1280,
    height: int = 1080,
    start_url: str | None = None,
) -> dict:
    """Generate a realistic browser fingerprint for Chrome on Linux.

    When a start_url is provided and the recordings volume is mounted,
    the fingerprint is cached to disk keyed by domain. This ensures the
    same site always sees the same "device" across sandbox runs.

    Returns a dict with all properties needed to configure the browser
    context and inject JS overrides.
    """
    # Try to load a cached fingerprint for this domain
    cache_path = _cache_path_for_url(start_url)
    if cache_path and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            logger.info("Loaded cached fingerprint for %s", start_url)
            return cached
        except Exception:
            logger.debug("Failed to load cached fingerprint", exc_info=True)

    # Generate a fresh fingerprint
    result = _generate_fresh(width, height)

    # Cache it for future runs
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(result))
            logger.info("Cached fingerprint to %s", cache_path)
        except Exception:
            logger.debug("Failed to cache fingerprint", exc_info=True)

    return result


def _cache_path_for_url(url: str | None) -> Path | None:
    """Return a fingerprint cache path keyed by domain, or None."""
    if not url:
        return None
    if not _FINGERPRINT_DIR.parent.exists():
        return None
    from urllib.parse import urlparse

    domain = urlparse(url).hostname or "default"
    domain_hash = hashlib.sha256(domain.encode()).hexdigest()[:12]
    return _FINGERPRINT_DIR / f"{domain}_{domain_hash}.json"


def _generate_fresh(width: int, height: int) -> dict:
    """Generate a new random fingerprint, rejecting unrealistic values.

    Validates:
    - No SwiftShader (headless giveaway)
    - No aarch64 + Intel/NVIDIA combos (impossible hardware)
    - Hardware concurrency clamped to 2-16 (realistic consumer range)
    - Device memory clamped to 2-16 GB
    """
    for _attempt in range(20):
        fp = _generator.generate(
            browser="chrome",
            os="linux",
            screen=Screen(
                min_width=width,
                max_width=max(width, 1920),
                min_height=height,
                max_height=max(height, 1080),
            ),
        )
        renderer = (fp.videoCard.renderer if fp.videoCard else None) or ""
        ua = fp.navigator.userAgent or ""

        # Reject SwiftShader (headless signal)
        if "swiftshader" in renderer.lower():
            continue

        # Reject aarch64 + Intel/NVIDIA (impossible combo)
        if "aarch64" in ua and (
            "intel" in renderer.lower() or "nvidia" in renderer.lower()
        ):
            continue

        break
    else:
        logger.warning("Could not generate valid fingerprint after 20 attempts")

    vc_vendor = (fp.videoCard.vendor if fp.videoCard else None) or "Google Inc."
    vc_renderer = (
        fp.videoCard.renderer if fp.videoCard else None
    ) or "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)"

    # Force x86_64 in UA — Modal sandboxes run on x86_64
    user_agent = fp.navigator.userAgent
    if "aarch64" in user_agent:
        user_agent = user_agent.replace("aarch64", "x86_64")
    app_version = fp.navigator.appVersion or ""
    if "aarch64" in app_version:
        app_version = app_version.replace("aarch64", "x86_64")

    platform = fp.navigator.platform or "Linux x86_64"
    if "aarch64" in platform:
        platform = platform.replace("aarch64", "x86_64")

    # Clamp hardware values to realistic consumer ranges
    hw_concurrency = min(max(fp.navigator.hardwareConcurrency or 4, 2), 16)
    device_memory = min(max(fp.navigator.deviceMemory or 8, 2), 16)

    result = {
        # Navigator
        "userAgent": user_agent,
        "platform": platform,
        "language": fp.navigator.language,
        "languages": fp.navigator.languages,
        "hardwareConcurrency": hw_concurrency,
        "deviceMemory": device_memory,
        "maxTouchPoints": fp.navigator.maxTouchPoints,
        "vendor": fp.navigator.vendor,
        "appVersion": app_version,
        "userAgentData": fp.navigator.userAgentData,
        "extraProperties": fp.navigator.extraProperties,
        # Screen
        "screen": {
            "width": fp.screen.width,
            "height": fp.screen.height,
            "availWidth": fp.screen.availWidth,
            "availHeight": fp.screen.availHeight,
            "colorDepth": fp.screen.colorDepth,
            "pixelDepth": fp.screen.pixelDepth,
            "devicePixelRatio": fp.screen.devicePixelRatio,
            "outerWidth": fp.screen.outerWidth,
            "outerHeight": fp.screen.outerHeight,
        },
        # WebGL
        "webgl": {
            "vendor": vc_vendor,
            "renderer": vc_renderer,
        },
        # Media
        "audioCodecs": fp.audioCodecs,
        "videoCodecs": fp.videoCodecs,
        "plugins": fp.pluginsData,
    }

    logger.info(
        "Generated fingerprint: UA=%s, Screen=%dx%d, WebGL=%s/%s, HW=%d, Mem=%d",
        user_agent[:70],
        fp.screen.width,
        fp.screen.height,
        vc_vendor[:20],
        vc_renderer[:40],
        hw_concurrency,
        device_memory,
    )
    return result


def build_fingerprint_js(fp: dict) -> str:
    """Build a JS init script that injects the fingerprint into the page.

    This replaces the static patches in stealth_evasions.js with values
    from a real browser session, making all properties consistent.
    """
    nav = fp
    screen = fp["screen"]
    webgl = fp["webgl"]
    plugins_data = fp.get("plugins", {})
    extra = fp.get("extraProperties", {})

    # Build plugins JS array from fingerprint data
    plugins_js = json.dumps(plugins_data.get("plugins", []))
    mime_types_js = json.dumps(plugins_data.get("mimeTypes", []))

    return f"""(() => {{
  'use strict';

  // === Navigator overrides (from real browser fingerprint) ===
  const navProps = {{
    hardwareConcurrency: {nav["hardwareConcurrency"]},
    deviceMemory: {nav["deviceMemory"]},
    maxTouchPoints: {nav["maxTouchPoints"]},
    platform: {json.dumps(nav["platform"])},
    vendor: {json.dumps(nav["vendor"] or "Google Inc.")},
    appVersion: {json.dumps(nav["appVersion"])},
    language: {json.dumps(nav["language"])},
    languages: {json.dumps(nav["languages"])},
    pdfViewerEnabled: {json.dumps(extra.get("pdfViewerEnabled", True))},
  }};

  for (const [prop, value] of Object.entries(navProps)) {{
    try {{
      Object.defineProperty(Navigator.prototype, prop, {{
        get: () => value,
        configurable: true,
      }});
    }} catch {{}}
  }}

  // navigator.webdriver = false
  try {{
    Object.defineProperty(Navigator.prototype, 'webdriver', {{
      get: () => false,
      configurable: true,
    }});
  }} catch {{}}

  // === Screen overrides ===
  const screenProps = {{
    width: {screen["width"]},
    height: {screen["height"]},
    availWidth: {screen["availWidth"]},
    availHeight: {screen["availHeight"]},
    colorDepth: {screen["colorDepth"]},
    pixelDepth: {screen["pixelDepth"]},
  }};

  for (const [prop, value] of Object.entries(screenProps)) {{
    try {{
      Object.defineProperty(Screen.prototype, prop, {{
        get: () => value,
        configurable: true,
      }});
    }} catch {{}}
  }}

  try {{
    Object.defineProperty(window, 'devicePixelRatio', {{
      get: () => {screen["devicePixelRatio"]},
      configurable: true,
    }});
    Object.defineProperty(window, 'outerWidth', {{
      get: () => {screen["outerWidth"]},
      configurable: true,
    }});
    Object.defineProperty(window, 'outerHeight', {{
      get: () => {screen["outerHeight"]},
      configurable: true,
    }});
  }} catch {{}}

  // === WebGL overrides (critical — SwiftShader is a dead giveaway) ===
  const webglVendor = {json.dumps(webgl["vendor"])};
  const webglRenderer = {json.dumps(webgl["renderer"])};

  for (const Proto of [WebGLRenderingContext.prototype, WebGL2RenderingContext.prototype]) {{
    try {{
      const origGetParam = Proto.getParameter;
      Proto.getParameter = function(param) {{
        if (param === 37445) return webglVendor;   // UNMASKED_VENDOR_WEBGL
        if (param === 37446) return webglRenderer;  // UNMASKED_RENDERER_WEBGL
        return origGetParam.call(this, param);
      }};
    }} catch {{}}
  }}

  // === chrome.runtime / chrome.app / chrome.csi / chrome.loadTimes ===
  try {{
    if (!window.chrome) window.chrome = {{}};
    if (!window.chrome.runtime) {{
      window.chrome.runtime = {{
        connect: function() {{}},
        sendMessage: function() {{}},
        id: undefined,
        onMessage: {{ addListener: function() {{}}, removeListener: function() {{}}, hasListener: function() {{ return false; }} }},
        onConnect: {{ addListener: function() {{}}, removeListener: function() {{}}, hasListener: function() {{ return false; }} }},
      }};
    }}
    if (!window.chrome.app) {{
      window.chrome.app = {{
        isInstalled: false,
        InstallState: {{ DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }},
        RunningState: {{ CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }},
        getDetails: function() {{}},
        getIsInstalled: function() {{ return false; }},
      }};
    }}
    if (!window.chrome.csi) {{
      window.chrome.csi = function() {{
        return {{ startE: Date.now(), onloadT: Date.now(), pageT: Math.random() * 1000 + 500, tpiT: 0 }};
      }};
    }}
    if (!window.chrome.loadTimes) {{
      window.chrome.loadTimes = function() {{
        return {{
          commitLoadTime: Date.now() / 1000,
          connectionInfo: 'h2',
          finishDocumentLoadTime: Date.now() / 1000 + Math.random(),
          finishLoadTime: Date.now() / 1000 + Math.random() * 2,
          firstPaintTime: Date.now() / 1000 + Math.random(),
          navigationType: 'Other',
          npnNegotiatedProtocol: 'h2',
          requestTime: Date.now() / 1000 - Math.random() * 5,
          startLoadTime: Date.now() / 1000 - Math.random() * 5,
          wasAlternateProtocolAvailable: false,
          wasFetchedViaSpdy: true,
          wasNpnNegotiated: true,
        }};
      }};
    }}
  }} catch {{}}

  // === Plugins (from fingerprint data) ===
  try {{
    const pluginsData = {plugins_js};
    const mimeTypesData = {mime_types_js};

    if (pluginsData.length > 0) {{
      const pluginArray = pluginsData.map(p => ({{
        name: p.name, description: p.description || '',
        filename: p.filename || '', length: (p.mimeTypes || []).length,
      }}));
      pluginArray.refresh = function() {{}};
      Object.defineProperty(navigator, 'plugins', {{
        get: () => pluginArray,
        configurable: true,
      }});
    }}
  }} catch {{}}

  // === Permissions API fix ===
  try {{
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = function(params) {{
      if (params.name === 'notifications') {{
        return Promise.resolve({{ state: Notification.permission }});
      }}
      return origQuery(params);
    }};
  }} catch {{}}

  // === Remove cdc_ properties (ChromeDriver artifacts) ===
  try {{
    const cdcProps = Object.getOwnPropertyNames(window).filter(
      n => /^cdc_|^[a-z]{{3}}_[a-z]{{22}}_/.test(n)
    );
    for (const prop of cdcProps) {{
      try {{ delete window[prop]; }} catch {{}}
    }}
  }} catch {{}}

  // === Protect overrides from toString detection ===
  try {{
    const nativeToString = Function.prototype.toString;
    Function.prototype.toString = function() {{
      if (this === navigator.permissions.query) return 'function query() {{ [native code] }}';
      if (this === WebGLRenderingContext.prototype.getParameter) return 'function getParameter() {{ [native code] }}';
      if (this === WebGL2RenderingContext.prototype.getParameter) return 'function getParameter() {{ [native code] }}';
      return nativeToString.call(this);
    }};
    Function.prototype.toString.toString = function() {{ return 'function toString() {{ [native code] }}'; }};
  }} catch {{}}
}})();"""
