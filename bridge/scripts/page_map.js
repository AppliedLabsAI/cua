/**
 * Full page action map — captures ALL actionable elements on the page,
 * regardless of viewport visibility. Organized for fast LLM decision-making.
 *
 * Unlike dom_snapshot (which only shows visible viewport elements),
 * this gives the LLM a complete picture of every link, button, form field,
 * and interactive element on the page so it can act in one shot.
 *
 * Output is structured by priority:
 *   1. Page context (title, URL, headings)
 *   2. Form fields with current values
 *   3. Data tables (full)
 *   4. ALL navigation links (grouped by region)
 *   5. ALL buttons and actions
 *
 * Registered as window.__pageMap via evaluate().
 */

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
  const navSel = 'nav, #nav-sidebar, .sidebar, [role=navigation]';
  for (const table of tables) {
    if (!_fits()) break;
    if (table.closest(navSel)) continue;
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

    // Show up to 10 rows (much more than dom_snapshot's 3)
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
  const navContainers = root.querySelectorAll(navSel);
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
    const sel = el.id ? `#${el.id}` : (el.className ? `button.${el.className.split(' ')[0]}` : `text=${text}`);
    return `  <button${type ? ` type="${type}"` : ''}>${text}</button> [${sel}]\n`;
  };

  // Content actions (non-nav links and buttons)
  const contentActions = [];
  const seenHrefs = new Set();
  const allActionable = root.querySelectorAll('a[href], button, [role=button], input[type=submit]');
  
  for (const el of allActionable) {
    if (navElSet.has(el)) continue;
    // Cognitive Blinders filter
    if (filterConfig && typeof filterConfig.__shouldShow === 'function' && !filterConfig.__shouldShow(el, filterConfig)) continue;
    
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
    for (const line of navActions) {
      if (!_fits() || (len - navStart) > navBudget) break;
      _add(line);
    }
    if (navActions.length > 0 && (len - navStart) > navBudget) {
      _add(`(${navActions.length} nav links total)\n`);
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
