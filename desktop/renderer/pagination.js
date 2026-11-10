/*
 * Deterministic reader pagination shared by the Electron renderer and the
 * small Node smoke tests.  The reader never relies on a scroll position: a
 * page is a bounded group of sanitized HTML blocks with a stable text range.
 */
(function attachPagination(root) {
  const BREAK_CHARS = /[。！？；：,.!?;:\s]/;

  function cleanText(value) {
    return String(value || '').replace(/\s+/g, ' ').trim();
  }

  function splitText(value, maxChars) {
    const text = cleanText(value);
    const limit = Math.max(4, Number(maxChars) || 480);
    if (!text) return [];
    const parts = [];
    let cursor = 0;
    while (cursor < text.length) {
      const end = Math.min(text.length, cursor + limit);
      if (end >= text.length) {
        parts.push(text.slice(cursor));
        break;
      }
      let cut = end;
      for (let index = end; index > cursor + Math.floor(limit * 0.55); index -= 1) {
        if (BREAK_CHARS.test(text[index - 1])) {
          cut = index;
          break;
        }
      }
      if (cut <= cursor) cut = end;
      parts.push(text.slice(cursor, cut).trim());
      cursor = cut;
    }
    return parts.filter(Boolean);
  }

  function splitLines(value, maxChars) {
    return splitText(value, Math.max(4, Number(maxChars) || 28));
  }

  function estimateBudget(container, compact = false) {
    const height = Number(container?.clientHeight) || (compact ? 150 : 410);
    const width = Number(container?.clientWidth) || 344;
    const lineHeight = compact ? 24 : 28;
    const charsPerLine = Math.max(12, Math.floor(width / (compact ? 15 : 17)));
    const lines = Math.max(4, Math.floor((height - 12) / lineHeight));
    return Math.max(96, Math.floor(lines * charsPerLine * 0.72));
  }

  function copyBlockWithText(node, text) {
    const tag = node.nodeType === 1 && node.tagName ? node.tagName.toLowerCase() : 'p';
    const element = node.ownerDocument.createElement(tag === 'br' ? 'p' : tag);
    if (node.nodeType === 1) {
      if (node.className) element.className = node.className;
      if (node.getAttribute && node.getAttribute('style')) element.setAttribute('style', node.getAttribute('style'));
    }
    element.textContent = text;
    return element;
  }

  function unitsFromFragment(fragment, unitBudget) {
    const units = [];
    Array.from(fragment.childNodes).forEach((node) => {
      if (node.nodeType === 3) {
        splitText(node.textContent, unitBudget).forEach((part) => units.push({ node: copyBlockWithText(node, part), text: part }));
        return;
      }
      if (node.nodeType !== 1) return;
      const text = cleanText(node.textContent);
      if (!text && node.tagName !== 'IMG') return;
      if (text.length > unitBudget) {
        splitText(text, unitBudget).forEach((part) => units.push({ node: copyBlockWithText(node, part), text: part }));
      } else {
        units.push({ node: node.cloneNode(true), text });
      }
    });
    return units;
  }

  function serializeNodes(nodes, ownerDocument) {
    const holder = ownerDocument.createElement('div');
    nodes.forEach((node) => holder.appendChild(node.cloneNode(true)));
    return holder.innerHTML;
  }

  function paginateFragment(fragment, options = {}) {
    const container = options.container;
    const compact = Boolean(options.compact);
    const maxChars = Math.max(96, Number(options.maxChars) || estimateBudget(container, compact));
    const units = unitsFromFragment(fragment, Math.max(48, Math.floor(maxChars * 0.84)));
    const pages = [];
    let current = [];
    let currentChars = 0;
    let startOffset = 0;

    const flush = () => {
      if (!current.length) return;
      const text = cleanText(current.map((item) => item.text).join(' '));
      pages.push({
        html: serializeNodes(current.map((item) => item.node), fragment.ownerDocument),
        text,
        startOffset,
        endOffset: startOffset + text.length,
      });
      startOffset += text.length;
      current = [];
      currentChars = 0;
    };

    units.forEach((unit) => {
      const length = Math.max(1, unit.text.length || (unit.node.tagName === 'IMG' ? 1 : 0));
      if (current.length && currentChars + length > maxChars) flush();
      current.push(unit);
      currentChars += length;
    });
    flush();
    if (!pages.length) pages.push({ html: '', text: '', startOffset: 0, endOffset: 0 });
    return pages.map((page, index) => ({ ...page, index, total: pages.length }));
  }

  const api = { cleanText, splitText, splitLines, estimateBudget, paginateFragment };
  root.WeFishPagination = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
