/**
 * Lightweight readability-style content extraction for CUA agent.
 *
 * Scores candidate container elements by text density, paragraph quality,
 * and class/id signals to find the main article content. Returns clean
 * innerHTML (not textContent) so downstream markdown conversion preserves
 * headings, links, lists, tables, and code blocks.
 *
 * Inspired by Mozilla Readability / Jina Reader's approach:
 *   - Content density scoring (text length vs total element length)
 *   - Positive/negative class/id signals
 *   - 30% threshold: if best candidate has <30% of body text, use full body
 *
 * Registered as window.__readabilityExtract via add_init_script.
 */

const __RE_NOISE_SELECTOR =
  'script, style, noscript, iframe, svg, ' +
  'nav, footer, header, [role=navigation], [role=banner], [role=contentinfo], ' +
  '.footer, .nav, .sidebar, .ad, .ads, .advert, .advertisement, ' +
  '.social-share, .share-buttons, .cookie-banner, .cookie-consent, ' +
  '.popup, .modal, .overlay, .newsletter-signup, ' +
  '[aria-hidden=true], [hidden]';

const __RE_POSITIVE = /article|body|content|entry|main|page|post|story|text|blog|hentry/i;
const __RE_NEGATIVE = /sidebar|comment|footer|nav|menu|ad|promo|social|share|related|widget|banner|cookie|popup|modal|sponsor|masthead|breadcrumb/i;

const __RE_BLOCK_TAGS = new Set([
  'DIV', 'SECTION', 'ARTICLE', 'MAIN', 'ASIDE', 'BLOCKQUOTE',
  'FIGURE', 'DETAILS', 'TD', 'FORM',
]);

window.__readabilityExtract = () => {
  // Work on a clone to avoid mutating the live DOM
  const clone = document.body.cloneNode(true);

  // Strip noise elements
  for (const el of clone.querySelectorAll(__RE_NOISE_SELECTOR)) el.remove();

  // Collect full body text length for the 30% threshold
  const fullText = (clone.textContent || '').replace(/\s+/g, ' ').trim();
  const fullLen = fullText.length;
  if (fullLen < 50) {
    // Page has almost no text — return whatever is there
    return JSON.stringify({
      html: clone.innerHTML,
      title: document.title,
      url: location.href,
    });
  }

  // Score candidate containers
  const candidates = clone.querySelectorAll(
    'div, section, article, main, [role=main], td, blockquote, aside, figure',
  );

  let bestEl = null;
  let bestScore = -1;

  for (const el of candidates) {
    const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
    const textLen = text.length;

    // Skip tiny containers
    if (textLen < 80) continue;

    // Base score: text length (longer content = more likely to be main)
    let score = textLen;

    // Paragraph quality: count <p> tags and reward high paragraph density
    const paragraphs = el.querySelectorAll('p');
    const pCount = paragraphs.length;
    let pTextLen = 0;
    for (const p of paragraphs) {
      pTextLen += (p.textContent || '').trim().length;
    }
    // Reward paragraphs with meaningful content (avg >40 chars)
    if (pCount > 0 && pTextLen / pCount > 40) {
      score += pCount * 30;
    }
    score += pTextLen;

    // Text density: ratio of text to total innerHTML length
    const htmlLen = el.innerHTML.length;
    if (htmlLen > 0) {
      const density = textLen / htmlLen;
      // Reward moderate density (0.2-0.6 is typical for article content)
      if (density > 0.15) score *= (1 + density);
    }

    // Class/ID signals
    const classId = (el.className || '') + ' ' + (el.id || '');
    if (__RE_POSITIVE.test(classId)) score *= 1.5;
    if (__RE_NEGATIVE.test(classId)) score *= 0.3;

    // Boost semantic elements
    const tag = el.tagName;
    if (tag === 'ARTICLE') score *= 1.8;
    else if (tag === 'MAIN' || el.getAttribute('role') === 'main') score *= 1.6;

    // Penalize deeply nested elements (>6 levels deep from body)
    let depth = 0;
    let parent = el.parentElement;
    while (parent && parent !== clone && depth < 10) {
      depth++;
      parent = parent.parentElement;
    }
    if (depth > 6) score *= 0.7;

    // Penalize elements that are mostly links (nav-like)
    const links = el.querySelectorAll('a');
    let linkTextLen = 0;
    for (const a of links) {
      linkTextLen += (a.textContent || '').trim().length;
    }
    if (textLen > 0 && linkTextLen / textLen > 0.5) {
      score *= 0.4;
    }

    if (score > bestScore) {
      bestScore = score;
      bestEl = el;
    }
  }

  // Apply 30% threshold (Jina Reader technique):
  // If the best candidate has less than 30% of the body text,
  // the extraction is too aggressive — use the full cleaned body instead.
  let resultEl = clone;
  if (bestEl) {
    const bestText = (bestEl.textContent || '').replace(/\s+/g, ' ').trim();
    if (bestText.length >= fullLen * 0.3) {
      resultEl = bestEl;
    }
  }

  // Clean the result: remove empty elements and excessive whitespace
  for (const el of resultEl.querySelectorAll('*')) {
    // Remove elements that are purely structural with no content
    if (
      !el.querySelector('img, video, audio, canvas, table, pre, code, input, select, textarea') &&
      !(el.textContent || '').trim() &&
      __RE_BLOCK_TAGS.has(el.tagName)
    ) {
      el.remove();
    }
  }

  return JSON.stringify({
    html: resultEl.innerHTML,
    title: document.title,
    url: location.href,
  });
};
