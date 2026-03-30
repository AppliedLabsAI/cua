/**
 * Unified page context for CUA agent — DOM snapshot + full page map.
 *
 * Two entry points sharing common helpers:
 *   window.__domSnapshot(rootSelector, maxChars, filterConfig)
 *     — Compact, viewport-aware snapshot for inline action responses.
 *     — Returns {dom, title, url}
 *
 *   window.__pageMap(maxChars, filterConfig)
 *     — Full page action map of ALL elements regardless of visibility.
 *     — Returns {map, title, url}
 *
 * Cognitive Blinders support:
 *   Optional filterConfig parameter controls which elements pass through.
 *   When absent, all elements are shown (backward compatible).
 */

// =========================================================================
// Shared helpers (used by both snapshot and map)
// =========================================================================

const __NAV_SELECTOR = 'nav, #nav-sidebar, .sidebar, [role=navigation]';
const __INTERACTIVE_SELECTOR =
  'a, button, input, select, textarea, ' +
  '[role=button], [role=link], [onclick], [tabindex]';

/**
 * Cognitive Blinders: element visibility filter.
 * Returns true if the element should be shown.
 */
function __shouldShow(el, filterConfig) {
  if (!filterConfig) return true; // no filter = show all (backward compat)

  // Check exclude selectors — always hide matching elements
  const excludeSels = filterConfig.excludeSelectors || [];
  for (let i = 0; i < excludeSels.length; i++) {
    try {
      if (el.matches(excludeSels[i]) || el.closest(excludeSels[i])) return false;
    } catch { /* invalid selector — skip */ }
  }

  // Check include selectors — always show matching elements
  const includeSels = filterConfig.includeSelectors || [];
  for (let i = 0; i < includeSels.length; i++) {
    try {
      if (el.matches(includeSels[i]) || el.closest(includeSels[i])) return true;
    } catch { /* invalid selector — skip */ }
  }

  const tag = el.tagName.toLowerCase();
  const text = (el.innerText || '').toLowerCase().trim();

  // Forms filter
  if (filterConfig.showForms === false) {
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return false;
    if (tag === 'button' && el.type === 'submit') return false;
  }

  // Action buttons filter — check dangerous text patterns
  if (filterConfig.showActionButtons === false) {
    const patterns = filterConfig.excludeTextPatterns || [];
    for (let i = 0; i < patterns.length; i++) {
      if (text.includes(patterns[i])) return false;
    }
  }

  // Account controls filter
  if (filterConfig.showAccountControls === false) {
    if (/\b(sign.?out|log.?out|my.?account|settings|profile|account)\b/i.test(text)) {
      return false;
    }
  }

  // Nav links filter
  if (filterConfig.showNavLinks === false) {
    if (tag === 'a' && el.closest('nav, [role=navigation], .sidebar, #nav-sidebar')) {
      return false;
    }
  }

  return true; // default: show
}

/**
 * Compact element renderer for DOM snapshot mode.
 */
function __renderElement(el) {
  const tag = el.tagName.toLowerCase();
  let line = `<${tag}`;
  const elId = el.getAttribute('id');
  const name = el.getAttribute('name');
  const type = el.getAttribute('type');
  const role = el.getAttribute('role');
  const aria = el.getAttribute('aria-label');
  const href = el.getAttribute('href');
  if (elId) line += ` id="${elId}"`;
  if (name) line += ` name="${name}"`;
  if (type) line += ` type="${type}"`;
  if (role) line += ` role="${role}"`;
  if (aria) line += ` aria-label="${aria.slice(0, 40)}"`;
  if (href) {
    try {
      const u = new URL(href, location.origin);
      const p =
        u.origin === location.origin ? u.pathname + (u.search || '') : href;
      line += ` href="${p.slice(0, 60)}"`;
    } catch {
      line += ` href="${href.slice(0, 60)}"`;
    }
  }
  if (tag === 'select' && el.selectedIndex >= 0) {
    const opt = el.options[el.selectedIndex];
    if (opt) line += ` selected="${opt.text.trim().slice(0, 30)}"`;
  }
  if ((tag === 'input' || tag === 'textarea') && el.value) {
    line += ` value="${el.value.slice(0, 40)}"`;
  }
  const text = (el.innerText || '').replace(/\n/g, ' ').trim();
  if (text && text.length <= 50) line += `>${text}</${tag}>`;
  else line += '>';
  return `${line}\n`;
}

// =========================================================================
// DOM Snapshot — compact, viewport-aware (for goto/click responses)
// =========================================================================

window.__domSnapshot = (rootSelector, maxChars, filterConfig) => {
  const MAX = maxChars || 3500;
  const parts = [];
  let len = 0;
  let root = document.body;
  if (rootSelector) {
    const s = document.querySelector(rootSelector);
    if (s) root = s;
  }

  // Phase 1: Labeled form field values (skip if forms hidden by blinders)
  const showForms = !filterConfig || filterConfig.showForms !== false;
  const labels = root.querySelectorAll('label');
  if (showForms && labels.length > 0) {
    let fieldCount = 0;
    for (const label of labels) {
      if (len >= MAX) break;
      const labelText = (label.innerText || '').replace(/[:\n]/g, '').trim();
      if (!labelText) continue;
      let input = null;
      const forId = label.getAttribute('for');
      if (forId) {
        input = root.querySelector(`#${CSS.escape(forId)}`);
      }
      if (!input) {
        const parent = label.closest(
          '.form-row, .form-group, .field, fieldset, .form-control, div',
        );
        if (parent)
          input = parent.querySelector('input, select, textarea, a[href]');
      }
      if (!input) continue;
      let val = '';
      const tag = input.tagName.toLowerCase();
      if (tag === 'select') {
        const opt = input.options?.[input.selectedIndex];
        val = opt ? opt.text.trim() : '';
      } else if (tag === 'textarea' || tag === 'input') {
        val = input.value || '';
      } else if (tag === 'a') {
        val = `${(input.innerText || '').trim()} [link: ${(input.getAttribute('href') || '').slice(0, 60)}]`;
      } else {
        val = (input.innerText || '').trim();
      }
      if (!val) continue;
      const line = `${labelText}: ${val.slice(0, 80)}\n`;
      parts.push(line);
      len += line.length;
      fieldCount++;
    }
    if (fieldCount > 0) {
      parts.splice(parts.length - fieldCount, 0, '--- Fields ---\n');
      len += 15;
    }
  }

  // Phase 2: Tables — headers + first 5 data rows
  const tables = root.querySelectorAll('table');
  for (const table of tables) {
    if (len >= MAX) break;
    if (table.closest(__NAV_SELECTOR)) continue;
    const bodyRows = table.querySelectorAll('tbody tr');
    if (bodyRows.length <= 2 && !table.getAttribute('id')) continue;
    const id = table.getAttribute('id');
    const thead = table.querySelector('thead');
    if (!thead) continue;
    const hCells = thead.querySelectorAll('th');
    if (hCells.length === 0) continue;
    const tLabel = id ? `table#${id}` : 'table';
    let header = `--- ${tLabel} ---\n`;
    const cols = [];
    for (const th of hCells) {
      let t = (th.innerText || '').replace(/\n/g, ' ').trim().slice(0, 20);
      if (!t) continue;
      const sortLink = th.querySelector('a[href]');
      if (sortLink) {
        const href = sortLink.getAttribute('href');
        if (href) t += `(${href.slice(0, 40)})`;
      }
      if (th.classList.contains('sorted')) {
        const dir = th.classList.contains('descending') ? '↓' : '↑';
        t += dir;
      }
      cols.push(t);
    }
    header += `| ${cols.join(' | ')} |\n`;
    parts.push(header);
    len += header.length;
    const rows = table.querySelectorAll('tbody tr');
    const maxRows = Math.min(rows.length, 5);
    for (let i = 0; i < maxRows && len < MAX; i++) {
      const cells = rows[i].querySelectorAll('td, th');
      const vals = [];
      for (const cell of cells) {
        const a = cell.querySelector('a');
        let v = a ? (a.innerText || '').trim() : (cell.innerText || '').trim();
        v = v.replace(/\n/g, ' ').slice(0, 25);
        vals.push(v);
      }
      const row = `| ${vals.join(' | ')} |\n`;
      parts.push(row);
      len += row.length;
    }
    if (rows.length > 5) {
      const more = `(${rows.length - 5} more rows)\n`;
      parts.push(more);
      len += more.length;
    }
  }

  // Phase 3: Content summary — headings + page info from main area
  const contentArea =
    root.querySelector(
      'main, [role=main], #content, #content-main, article, .content',
    ) || root;
  if (len < MAX) {
    const headings = contentArea.querySelectorAll('h1, h2, h3, h4');
    if (headings.length > 0) {
      parts.push('--- Content ---\n');
      len += 15;
      for (const h of headings) {
        if (len >= MAX) break;
        const t = (h.innerText || '').replace(/\n/g, ' ').trim().slice(0, 80);
        if (!t) continue;
        const line = `${h.tagName.toLowerCase()}: ${t}\n`;
        parts.push(line);
        len += line.length;
      }
    }
    // Capture page-level counts/summaries (e.g., "3 results", "Showing 1 to 10 of 50")
    const countEl = contentArea.querySelector('.paginator, .pagination, .results, .object-tools, p.paginator');
    if (countEl && len < MAX) {
      const countText = (countEl.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 100);
      if (countText) {
        const countLine = `page-info: ${countText}\n`;
        parts.push(countLine);
        len += countLine.length;
      }
    }
  }

  // Phase 4: Interactive elements — main content first, nav backfill
  if (len < MAX) {
    parts.push('--- Interactive ---\n');
    len += 19;
  }
  const MAX_ELEMENTS = 50;
  let elCount = 0;
  const allEls = root.querySelectorAll(__INTERACTIVE_SELECTOR);

  // Pre-build nav element Set (O(1) lookup instead of closest() per element)
  const navContainers = root.querySelectorAll(__NAV_SELECTOR);
  const navElSet = new Set();
  for (const nav of navContainers) {
    for (const el of nav.querySelectorAll(__INTERACTIVE_SELECTOR)) {
      navElSet.add(el);
    }
  }

  const navEls = [];
  const seenHrefs = new Set();

  // Pass 1: non-nav elements
  for (const el of allEls) {
    if (len >= MAX || elCount >= MAX_ELEMENTS) break;
    // checkVisibility() — faster than getComputedStyle, no style recalc
    if (typeof el.checkVisibility === 'function') {
      if (!el.checkVisibility()) continue;
    }
    if (navElSet.has(el)) {
      navEls.push(el);
      continue;
    }
    // Cognitive Blinders filter
    if (!__shouldShow(el, filterConfig)) continue;
    const elHref = el.getAttribute('href');
    if (el.tagName === 'A' && elHref) {
      if (seenHrefs.has(elHref)) continue;
      seenHrefs.add(elHref);
    }
    const line = __renderElement(el);
    parts.push(line);
    len += line.length;
    elCount++;
  }

  // Pass 2: backfill nav elements
  for (const el of navEls) {
    if (len >= MAX || elCount >= MAX_ELEMENTS) break;
    // Cognitive Blinders filter
    if (!__shouldShow(el, filterConfig)) continue;
    const elHref = el.getAttribute('href');
    if (el.tagName === 'A' && elHref) {
      if (seenHrefs.has(elHref)) continue;
      seenHrefs.add(elHref);
    }
    const line = __renderElement(el);
    parts.push(line);
    len += line.length;
    elCount++;
  }

  return JSON.stringify({
    dom: parts.join('').slice(0, MAX),
    title: document.title,
    url: location.href,
  });
};

// =========================================================================
// Page Map — full page, all elements regardless of visibility
// =========================================================================

window.__pageMap = (maxChars, filterConfig) => {
  const MAX = maxChars || 8000;
  const root = document.body;
  const parts = [];
  let len = 0;

  const _add = (s) => { parts.push(s); len += s.length; };
  const _fits = () => len < MAX;

  // --- Page context ---
  const url = location.href;
  const title = document.title || '';
  _add(`[${title}] ${url}\n`);

  // Headings — quick page structure overview
  const contentArea = root.querySelector(
    'main, [role=main], #content, #content-main, article, .content'
  ) || root;
  const headings = contentArea.querySelectorAll('h1, h2, h3');
  for (const h of headings) {
    if (!_fits()) break;
    const t = (h.innerText || '').replace(/\n/g, ' ').trim().slice(0, 100);
    if (t) _add(`${h.tagName.toLowerCase()}: ${t}\n`);
  }

  // Page-level counts (e.g., "3 results", "Showing 1 to 10 of 50")
  const countEls = root.querySelectorAll('.paginator, .pagination, .results, p.paginator, .object-tools');
  for (const el of countEls) {
    if (!_fits()) break;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120);
    if (t) _add(`page-info: ${t}\n`);
  }

  // --- Form fields with labels and values ---
  const labels = root.querySelectorAll('label');
  if (labels.length > 0) {
    let fieldLines = [];
    for (const label of labels) {
      const labelText = (label.innerText || '').replace(/[:\n]/g, '').trim();
      if (!labelText) continue;
      let input = null;
      const forId = label.getAttribute('for');
      if (forId) {
        try { input = root.querySelector(`#${CSS.escape(forId)}`); } catch {}
      }
      if (!input) {
        const parent = label.closest('.form-row, .form-group, .field, fieldset, div');
        if (parent) input = parent.querySelector('input, select, textarea, a[href]');
      }
      if (!input) continue;
      const tag = input.tagName.toLowerCase();
      let val = '', sel = '';
      if (tag === 'select') {
        const opt = input.options?.[input.selectedIndex];
        val = opt ? opt.text.trim() : '';
        sel = input.id ? `#${input.id}` : `select[name="${input.name}"]`;
      } else if (tag === 'textarea' || tag === 'input') {
        val = input.value || '';
        sel = input.id ? `#${input.id}` : `input[name="${input.name}"]`;
      } else if (tag === 'a') {
        val = `${(input.innerText || '').trim()} → ${(input.getAttribute('href') || '').slice(0, 60)}`;
        sel = '';
      }
      if (val || sel) {
        fieldLines.push(`  ${labelText}: ${val.slice(0, 150)}${sel ? ` [${sel}]` : ''}\n`);
      }
    }
    if (fieldLines.length > 0) {
      _add('--- Fields ---\n');
      for (const fl of fieldLines) {
        if (!_fits()) break;
        _add(fl);
      }
    }
  }

  // --- Tables — headers + rows (more generous than dom_snapshot) ---
  const tables = root.querySelectorAll('table');
  for (const table of tables) {
    if (!_fits()) break;
    if (table.closest(__NAV_SELECTOR)) continue;
    const thead = table.querySelector('thead');
    const bodyRows = table.querySelectorAll('tbody tr');
    if (!thead && bodyRows.length === 0) continue;

    const id = table.getAttribute('id');
    const tLabel = id ? `table#${id}` : 'table';
    _add(`--- ${tLabel} ---\n`);

    // Headers
    if (thead) {
      const hCells = thead.querySelectorAll('th');
      const cols = [];
      for (const th of hCells) {
        let t = (th.innerText || '').replace(/\n/g, ' ').trim().slice(0, 25);
        if (!t) continue;
        const sortLink = th.querySelector('a[href]');
        if (sortLink) {
          const href = sortLink.getAttribute('href');
          if (href) t += `(${href.slice(0, 40)})`;
        }
        if (th.classList.contains('sorted')) {
          t += th.classList.contains('descending') ? '↓' : '↑';
        }
        cols.push(t);
      }
      if (cols.length) _add(`| ${cols.join(' | ')} |\n`);
    }

    // Show up to 10 rows (much more than dom_snapshot's 5)
    const maxRows = Math.min(bodyRows.length, 10);
    for (let i = 0; i < maxRows && _fits(); i++) {
      const cells = bodyRows[i].querySelectorAll('td, th');
      const vals = [];
      for (const cell of cells) {
        const a = cell.querySelector('a[href]');
        let v;
        if (a) {
          const text = (a.innerText || '').trim().slice(0, 30);
          const href = a.getAttribute('href') || '';
          v = `${text} → ${href.slice(0, 50)}`;
        } else {
          v = (cell.innerText || '').replace(/\n/g, ' ').trim().slice(0, 35);
        }
        vals.push(v);
      }
      _add(`| ${vals.join(' | ')} |\n`);
    }
    if (bodyRows.length > maxRows) {
      _add(`(${bodyRows.length - maxRows} more rows)\n`);
    }
  }

  // --- ALL links on the page, grouped by region ---
  // This is the key difference from dom_snapshot: we skip visibility checks
  // and capture everything, so the LLM never needs to scroll.
  const navContainers = root.querySelectorAll(__NAV_SELECTOR);
  const navElSet = new Set();
  for (const nav of navContainers) {
    for (const el of nav.querySelectorAll('a, button')) navElSet.add(el);
  }

  // Helper to render a link/button compactly
  const _renderAction = (el) => {
    const tag = el.tagName.toLowerCase();
    const text = (el.innerText || '').replace(/\n/g, ' ').trim().slice(0, 50);
    if (!text) return null;

    if (tag === 'a') {
      const href = el.getAttribute('href') || '';
      if (!href || href === '#' || href.startsWith('javascript:')) return null;
      // Compact: "Link Text → /path"
      try {
        const u = new URL(href, location.origin);
        const p = u.origin === location.origin ? u.pathname + (u.search || '') : href;
        return `  [${text}](${p.slice(0, 70)})\n`;
      } catch {
        return `  [${text}](${href.slice(0, 70)})\n`;
      }
    }

    // Button
    const type = el.getAttribute('type') || '';
    const firstClass = (el.className || '').trim().split(/\s+/).find(Boolean);
    const sel = el.id ? `#${el.id}` : (firstClass ? `button.${firstClass}` : `text=${text}`);
    return `  <button${type ? ` type="${type}"` : ''}>${text}</button> [${sel}]\n`;
  };

  // Content actions (non-nav links and buttons)
  const contentActions = [];
  const seenHrefs = new Set();
  const allActionable = root.querySelectorAll('a[href], button, [role=button], input[type=submit]');

  for (const el of allActionable) {
    if (navElSet.has(el)) continue;
    // Cognitive Blinders filter
    if (!__shouldShow(el, filterConfig)) continue;

    const href = el.getAttribute('href');
    if (href && seenHrefs.has(href)) continue;
    if (href) seenHrefs.add(href);

    if (el.tagName === 'INPUT' && el.type === 'submit') {
      const val = el.value || 'Submit';
      contentActions.push(`  <input type="submit" value="${val.slice(0, 30)}"> [input[type="submit"]]\n`);
      continue;
    }

    const line = _renderAction(el);
    if (line) contentActions.push(line);
  }

  if (contentActions.length > 0 && _fits()) {
    _add('--- Actions ---\n');
    for (const line of contentActions) {
      if (!_fits()) break;
      _add(line);
    }
  }

  // Nav links (sidebar, navigation bars)
  const navActions = [];
  for (const el of navElSet) {
    const href = el.getAttribute('href');
    if (href && seenHrefs.has(href)) continue;
    if (href) seenHrefs.add(href);
    const line = _renderAction(el);
    if (line) navActions.push(line);
  }

  // Nav links — budget-aware: cap nav at 30% of total budget
  // so content-heavy pages don't get starved by massive sidebars
  if (navActions.length > 0 && _fits()) {
    const navBudget = Math.min(MAX * 0.3, MAX - len); // at most 30% or whatever remains
    const navStart = len;
    _add('--- Navigation ---\n');
    let navShown = 0;
    for (const line of navActions) {
      if (!_fits() || (len - navStart) > navBudget) break;
      _add(line);
      navShown++;
    }
    if (navShown < navActions.length) {
      _add(`(${navShown} of ${navActions.length} nav links shown)\n`);
    }
  }

  // --- Form inputs without labels (standalone inputs) ---
  const standaloneInputs = root.querySelectorAll('input:not([type=hidden]):not([type=submit]), select, textarea');
  const labeledIds = new Set();
  for (const label of labels) {
    const forId = label.getAttribute('for');
    if (forId) labeledIds.add(forId);
  }

  const unlabeledInputs = [];
  for (const input of standaloneInputs) {
    if (input.id && labeledIds.has(input.id)) continue;
    const tag = input.tagName.toLowerCase();
    const type = input.getAttribute('type') || '';
    const name = input.getAttribute('name') || '';
    const placeholder = input.getAttribute('placeholder') || '';
    const id = input.id;
    const sel = id ? `#${id}` : (name ? `${tag}[name="${name}"]` : null);
    if (!sel) continue;
    const hint = placeholder || name || type || tag;
    unlabeledInputs.push(`  ${hint}: [${sel}]\n`);
  }

  if (unlabeledInputs.length > 0 && _fits()) {
    _add('--- Inputs ---\n');
    for (const line of unlabeledInputs) {
      if (!_fits()) break;
      _add(line);
    }
  }

  return JSON.stringify({
    map: parts.join('').slice(0, MAX),
    title: title,
    url: url,
  });
};
