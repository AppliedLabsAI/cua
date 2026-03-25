/**
 * Smart body text extraction for CUA agent.
 *
 * Scopes to main content area and strips noise (footer, nav, scripts, SVGs)
 * before returning text. Used when extract(body, text) is called.
 *
 * Optimizations:
 *   - textContent instead of innerText (no layout recalc on detached clone)
 *   - Whitespace normalization via regex
 *
 * Registered as window.__smartExtract via add_init_script.
 */
const __NOISE_SELECTOR =
  'footer, [role=contentinfo], nav, .footer, ' +
  'script, style, noscript, iframe, svg';

window.__smartExtract = () => {
  const main = document.querySelector(
    'main, [role=main], #content, #content-main, article, .content',
  );
  const el = main || document.body;
  const clone = el.cloneNode(true);
  for (const n of clone.querySelectorAll(__NOISE_SELECTOR)) n.remove();
  // textContent is much faster than innerText on detached nodes
  // (no layout recalculation needed)
  return (clone.textContent || '').replace(/\s+/g, ' ').trim();
};
