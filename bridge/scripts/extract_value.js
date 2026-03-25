/**
 * Extract form field value from a DOM element.
 *
 * Handles select, textarea, input, and generic elements.
 * Returns the value as a string, or '[not found]' if selector doesn't match.
 *
 * Registered as window.__extractValue via add_init_script.
 */
window.__extractValue = sel => {
  const el = document.querySelector(sel);
  if (!el) return '[not found]';
  const tag = el.tagName.toLowerCase();
  if (tag === 'select') {
    const opt = el.options?.[el.selectedIndex];
    return opt ? opt.text.trim() : '';
  }
  if (tag === 'textarea' || tag === 'input') return el.value;
  return el.innerText || el.textContent || '';
};
