/**
 * DOM snapshot for CUA agent.
 *
 * Captures a compact representation of the page:
 *   Phase 1: Labeled form field values
 *   Phase 2: Data tables (headers + first 3 rows)
 *   Phase 3: Content summary (headings from main area)
 *   Phase 4: Interactive elements (main content first, nav backfill)
 *
 * Optimizations:
 *   - checkVisibility() instead of getComputedStyle (avoids style recalc)
 *   - Pre-built nav Set instead of closest() per element (O(1) lookup)
 *   - renderElement hoisted to module scope (avoids per-call allocation)
 *
 * Registered as window.__domSnapshot via add_init_script.
 */

// --- Module-scope helper (allocated once, not per call) ---
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

// --- Nav selector constant ---
const __NAV_SELECTOR = 'nav, #nav-sidebar, .sidebar, [role=navigation]';
const __INTERACTIVE_SELECTOR =
  'a, button, input, select, textarea, ' +
  '[role=button], [role=link], [onclick], [tabindex]';

window.__domSnapshot = (rootSelector, maxChars) => {
  const MAX = maxChars || 3500;
  const parts = [];
  let len = 0;
  let root = document.body;
  if (rootSelector) {
    const s = document.querySelector(rootSelector);
    if (s) root = s;
  }

  // Phase 1: Labeled form field values
  const labels = root.querySelectorAll('label');
  if (labels.length > 0) {
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

  // Phase 2: Tables — headers + first 3 data rows
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
    const maxRows = Math.min(rows.length, 3);
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
    if (rows.length > 3) {
      const more = `(${rows.length - 3} more rows)\n`;
      parts.push(more);
      len += more.length;
    }
  }

  // Phase 3: Content summary — headings from main area
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
