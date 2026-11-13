const $ = (id) => document.getElementById(id);

const state = {
  baseUrl: '',
  visible: true,
  currentBook: null,
  chapters: [],
  chapterIndex: -1,
  pages: [],
  pageIndex: 0,
  pageCache: new Map(),
  chapterHtmlCache: new Map(),
  preloadedPages: [],
  loadGeneration: 0,
  elapsed: 0,
  reportedElapsed: 0,
  reading: false,
  timer: null,
  pollTimer: null,
  qrUid: '',
  entitlement: {},
  membership: { status: 'unknown', source: 'web-reader-bootstrap', hasSnapshot: false },
  chapterOffset: 0,
  chapterSummary: '',
  bookProgress: 0,
  chapterTextLength: 0,
  lines: [],
  lineIndex: 0,
  lineAuto: true,
  lineTimer: null,
  lineIntervalMs: 1000,
  trayWidthMode: 'dynamic',
  trayWidthChars: 28,
  shelfBooks: [],
  recentBooks: [],
  shelfQuery: '',
  shelfLoaded: false,
  shelfRequest: null,
  syncStatus: 'idle',
  remoteProgress: null,
  pendingReports: [],
  reportInFlight: false,
  positionSyncTimer: null,
  positionSyncQueued: false,
  showLineProgress: true,
  showPercent: true,
  flushStarted: false,
  restoreTimer: null,
};

function formatDuration(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(value / 60);
  const hours = Math.floor(minutes / 60);
  const mm = String(minutes % 60).padStart(2, '0');
  const ss = String(value % 60).padStart(2, '0');
  return hours ? `${String(hours).padStart(2, '0')}:${mm}:${ss}` : `${mm}:${ss}`;
}

function formatReadingTime(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  return hours ? `${hours} 小时 ${minutes} 分钟` : `${minutes} 分钟`;
}

function setStatus(message, kind = '') {
  $('status-text').textContent = message;
  $('status-dot').className = `status-dot ${kind}`.trim();
}

function toast(message, error = false) {
  const node = $('toast');
  node.textContent = message;
  node.style.borderColor = error ? 'rgba(242, 124, 92, .5)' : '';
  node.style.background = error ? '#351d1a' : '';
  node.style.color = error ? '#f7ad96' : '';
  node.classList.add('visible');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove('visible'), 3400);
}

async function waitForBridge() {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const url = await window.wefish.bridgeUrl();
    if (url) return url;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error('本地阅读服务没有启动');
}

async function api(route, options = {}) {
  if (!state.baseUrl) throw new Error('本地阅读服务尚未就绪');
  const { timeoutMs = 15000, ...requestOptions } = options;
  const controller = timeoutMs > 0 && typeof AbortController !== 'undefined' && !requestOptions.signal
    ? new AbortController()
    : null;
  const timeout = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const response = await fetch(`${state.baseUrl}${route}`, {
      ...requestOptions,
      ...(controller ? { signal: controller.signal } : {}),
      headers: { 'Content-Type': 'application/json', ...(requestOptions.headers || {}) },
    });
    const contentType = response.headers.get('content-type') || '';
    const payload = contentType.includes('text/html') ? await response.text() : await response.json();
    if (!response.ok) {
      const message = typeof payload === 'string'
        ? payload
        : (payload.message || payload.error || payload.code || `请求失败（${response.status}）`);
      throw new Error(message || `请求失败（${response.status}）`);
    }
    return payload;
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

function getApi(route) {
  return api(route, { method: 'GET' });
}

function jsonBody(value) {
  return { method: 'POST', body: JSON.stringify(value || {}) };
}

function normalizeBook(book) {
  const value = book && typeof book === 'object' ? book : {};
  return {
    ...value,
    bookId: String(value.bookId || value.book_id || ''),
    title: String(value.title || value.bookName || value.name || value.bookId || '未命名书籍'),
    author: String(value.author || value.authorName || ''),
    progress: Number(value.progress || 0),
    readUpdateTime: Number(value.readUpdateTime || value.read_update_time || value.updateTime || 0),
  };
}

function shelfSearchValue(value) {
  return String(value || '').trim().toLocaleLowerCase();
}

function formatRecentTime(value) {
  const numeric = Number(value || 0);
  const timestamp = numeric > 0 && numeric < 100000000000 ? numeric * 1000 : numeric;
  if (!timestamp) return '刚刚打开';
  const date = new Date(timestamp);
  const now = new Date();
  const sameDay = date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth()
    && date.getDate() === now.getDate();
  if (sameDay) return `今天 ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
  return date.toLocaleDateString([], { month: 'numeric', day: 'numeric' });
}

function bookMatchesQuery(book, query) {
  if (!query) return true;
  return [book.title, book.author, book.bookId].some((value) => shelfSearchValue(value).includes(query));
}

function createShelfItem(rawBook, recent = false) {
  const book = normalizeBook(rawBook);
  const button = document.createElement('button');
  button.className = 'shelf-item';
  button.innerHTML = '<span><strong></strong><small></small></span><span>→</span>';
  button.querySelector('strong').textContent = book.title;
  const meta = [book.author];
  if (recent) meta.push(`最近 ${formatRecentTime(book.lastReadAt || book.readUpdateTime)}`);
  if (Number.isFinite(Number(book.progress))) meta.push(`${Math.round(book.progress)}%`);
  button.querySelector('small').textContent = meta.filter(Boolean).join(' · ');
  button.addEventListener('click', () => loadBook(book));
  return button;
}

function persistRecentBooks() {
  try { localStorage.setItem('wefish.recentBooks', JSON.stringify(state.recentBooks.slice(0, 12))); } catch (_) { /* private mode */ }
}

function rememberBook(rawBook) {
  const book = normalizeBook(rawBook);
  if (!book.bookId) return;
  const existing = state.recentBooks.find((item) => item.bookId === book.bookId) || {};
  state.recentBooks = [
    { ...existing, ...book, lastReadAt: Date.now() },
    ...state.recentBooks.filter((item) => item.bookId !== book.bookId),
  ].slice(0, 12);
  persistRecentBooks();
  renderShelfList();
}

function loadRecentBooks() {
  try {
    const stored = JSON.parse(localStorage.getItem('wefish.recentBooks') || '[]');
    state.recentBooks = Array.isArray(stored)
      ? stored.map(normalizeBook).filter((book) => book.bookId).slice(0, 12)
      : [];
  } catch (_) {
    state.recentBooks = [];
  }
}

function mergeRecentBookDetails(books) {
  const details = new Map(books.map((book) => [book.bookId, book]));
  const merged = state.recentBooks.map((book) => ({ ...book, ...(details.get(book.bookId) || {}) }));
  const known = new Set(merged.map((book) => book.bookId));
  books
    .filter((book) => book.bookId && book.readUpdateTime)
    .forEach((book) => {
      if (known.has(book.bookId)) return;
      merged.push({ ...book, lastReadAt: book.readUpdateTime });
    });
  state.recentBooks = merged
    .sort((left, right) => Number(right.lastReadAt || right.readUpdateTime || 0) - Number(left.lastReadAt || left.readUpdateTime || 0))
    .slice(0, 12);
  persistRecentBooks();
}

function renderShelfList() {
  const list = $('shelf-list');
  const query = shelfSearchValue(state.shelfQuery);
  const books = state.shelfBooks.filter((book) => bookMatchesQuery(book, query));
  const recent = state.recentBooks.filter((book) => bookMatchesQuery(book, query));
  $('shelf-count').textContent = query ? `${books.length} / ${state.shelfBooks.length} 本` : `${state.shelfBooks.length} 本`;
  const recentSection = $('recent-section');
  const recentList = $('recent-list');
  recentList.replaceChildren();
  recent.forEach((book) => recentList.appendChild(createShelfItem(book, true)));
  recentSection.hidden = recent.length === 0;
  list.replaceChildren();
  if (!state.shelfBooks.length) {
    const empty = document.createElement('div');
    empty.className = 'list-empty';
    empty.textContent = '还没有拿到书架。可以先打开阅读链接，或确认 Skill 已启用。';
    list.appendChild(empty);
    return;
  }
  if (!books.length) {
    const empty = document.createElement('div');
    empty.className = 'list-empty';
    empty.textContent = `没有找到包含“${state.shelfQuery.trim()}”的书。`;
    list.appendChild(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  books.forEach((book) => fragment.appendChild(createShelfItem(book)));
  list.appendChild(fragment);
}

function renderShelf(books) {
  state.shelfBooks = books.map(normalizeBook);
  mergeRecentBookDetails(state.shelfBooks);
  state.shelfLoaded = true;
  renderShelfList();
}

async function openShelf() {
  $('shelf-drawer').setAttribute('aria-hidden', 'false');
  if (!state.shelfLoaded) await loadShelf();
}

function closeShelf() {
  $('shelf-drawer').setAttribute('aria-hidden', 'true');
}

async function loadShelf({ force = false } = {}) {
  if (state.shelfRequest && !force) return state.shelfRequest;
  if (state.shelfLoaded && !force) {
    renderShelfList();
    return state.shelfBooks;
  }
  setStatus('同步书架中…');
  state.shelfRequest = (async () => {
    try {
      const data = await api('/api/shelf', { ...jsonBody(), timeoutMs: 12000 });
      const books = Array.isArray(data.books) ? data.books : [];
      renderShelf(books);
      setStatus(`${books.length} 本书已就绪`, 'active');
      toast(books.length ? `书架已同步，共 ${books.length} 本` : '书架为空');
      return state.shelfBooks;
    } catch (error) {
      state.shelfLoaded = false;
      state.shelfBooks = [];
      renderShelfList();
      setStatus('书架需要授权', 'warn');
      toast(error.message, true);
      return [];
    } finally {
      state.shelfRequest = null;
    }
  })();
  return state.shelfRequest;
}

function safeReaderHtml(source) {
  const documentFragment = new DOMParser().parseFromString(String(source || ''), 'text/html');
  documentFragment.querySelectorAll('script, iframe, object, embed, form, input, button').forEach((node) => node.remove());
  documentFragment.querySelectorAll('*').forEach((node) => {
    [...node.attributes].forEach((attribute) => {
      const name = attribute.name.toLowerCase();
      const value = attribute.value.trim();
      if (name.startsWith('on') || (name === 'href' && /^javascript:/i.test(value))) node.removeAttribute(attribute.name);
      if ((name === 'src' || name === 'href') && !/^(https?:|data:|#|\/)/i.test(value)) node.removeAttribute(attribute.name);
    });
  });
  return documentFragment.body;
}

function updateReaderHeader(chapter) {
  const book = state.currentBook || {};
  $('book-kicker').textContent = book.isMp ? '公众号文章' : `阅读 · ${state.chapterIndex + 1}/${Math.max(state.chapters.length, 1)}`;
  $('book-title').textContent = chapter?.title || book.title || '未命名书籍';
  const syncText = state.syncStatus === 'pending' ? '待同步' : (state.syncStatus === 'synced' ? '已同步' : '本地阅读');
  $('book-meta').textContent = [book.title, book.author, `${Math.round(state.bookProgress || book.progress || 0)}%`, syncText].filter(Boolean).join(' · ');
  const progress = state.bookProgress || (state.chapters.length > 1 ? (state.chapterIndex / (state.chapters.length - 1)) * 100 : Number(book.progress || 0));
  $('progress-bar').style.width = `${Math.max(0, Math.min(100, progress))}%`;
  $('page-label').textContent = state.pages.length ? `${state.pageIndex + 1} / ${state.pages.length}` : '0 / 0';
  $('position-slider').value = String(Math.round(Math.max(0, Math.min(100, progress))));
}

function readerPosition() {
  const page = state.pages[state.pageIndex] || { text: '', startOffset: 0, endOffset: 0 };
  const lines = state.lines.length ? state.lines : [page.text || ''];
  const lineFraction = lines.length > 1 ? state.lineIndex / Math.max(1, lines.length - 1) : 0;
  const offset = Math.max(0, Math.min(state.chapterTextLength || page.endOffset || 0,
    Math.floor(page.startOffset + (page.text || '').length * lineFraction)));
  const summary = String(lines[Math.min(state.lineIndex, lines.length - 1)] || page.text || '').slice(0, 20);
  const chapterCount = Math.max(state.chapters.length, 1);
  const chapterFraction = state.chapterTextLength > 0 ? offset / state.chapterTextLength : 0;
  const progress = ((state.chapterIndex + chapterFraction) / chapterCount) * 100;
  return { offset, summary, progress: Math.max(0, Math.min(100, progress)) };
}

function updateReaderPosition() {
  if (state.chapterIndex < 0 || !state.currentBook) return;
  const position = readerPosition();
  state.chapterOffset = position.offset;
  state.chapterSummary = position.summary;
  state.bookProgress = position.progress;
  $('progress-bar').style.width = `${position.progress}%`;
  $('position-slider').value = String(Math.round(position.progress));
  $('page-label').textContent = `${state.pageIndex + 1} / ${state.pages.length}`;
  updateReaderHeader(state.chapters[state.chapterIndex]);
  updateProgressDisplay(position.progress);
}

function lineStartOffset() {
  const page = state.pages[state.pageIndex] || { startOffset: 0, text: '' };
  const pageText = currentPageText();
  const line = String(state.lines[state.lineIndex] || pageText || '');
  let cursor = 0;
  for (let index = 0; index < state.lineIndex; index += 1) {
    const previous = String(state.lines[index] || '');
    const found = pageText.indexOf(previous, cursor);
    cursor = found >= 0 ? found + previous.length : cursor + previous.length;
  }
  const found = pageText.indexOf(line, cursor);
  return Math.max(0, Number(page.startOffset || 0) + (found >= 0 ? found : cursor));
}

function currentParagraphInfo() {
  const page = state.pages[state.pageIndex] || { startOffset: 0, text: '' };
  const pageText = currentPageText();
  const root = $('reader-body').querySelector('.reader-page');
  const blocks = root
    ? [...root.querySelectorAll('h1,h2,h3,h4,p,blockquote,li')]
      .map((node) => String(node.textContent || '').replace(/\s+/g, ' ').trim())
      .filter(Boolean)
    : [];
  const lineRelative = Math.max(0, lineStartOffset() - Number(page.startOffset || 0));
  let cursor = 0;
  for (const text of blocks) {
    const found = pageText.indexOf(text, cursor);
    const start = found >= 0 ? found : cursor;
    const end = start + text.length;
    if (lineRelative >= start && lineRelative <= end) {
      return {
        text,
        range: `${Number(page.startOffset || 0) + start}-${Number(page.startOffset || 0) + end}`,
      };
    }
    cursor = Math.max(cursor, end);
  }
  return { text: pageText, range: `${Number(page.startOffset || 0)}-${Number(page.startOffset || 0) + pageText.length}` };
}

function currentAnnotationContext(kind = 'line') {
  const line = String(state.lines[state.lineIndex] || currentPageText() || '').trim();
  const lineStart = lineStartOffset();
  const lineRange = `${lineStart}-${lineStart + line.length}`;
  const paragraph = currentParagraphInfo();
  return {
    text: kind === 'paragraph' ? paragraph.text : line,
    line,
    range: kind === 'paragraph' ? paragraph.range : lineRange,
    paragraph: paragraph.text,
    paragraphRange: paragraph.range,
  };
}

function chapterLineProgress() {
  const width = trayLineWidth();
  let before = 0;
  let total = 0;
  state.pages.forEach((page, index) => {
    const count = Math.max(1, WeFishPagination.splitLines(String(page.text || ''), width).length);
    total += count;
    if (index < state.pageIndex) before += count;
  });
  const currentPageTotal = Math.max(1, state.lines.length);
  const current = Math.min(total || currentPageTotal, before + Math.min(currentPageTotal, Math.max(1, state.lineIndex + 1)));
  return { current: Math.max(1, current), total: Math.max(1, total || currentPageTotal) };
}

function updateProgressDisplay(progress = state.bookProgress) {
  const lineCount = $('progress-line-count');
  const percent = $('progress-percent');
  const ring = $('progress-ring-value');
  const lineProgress = chapterLineProgress();
  if (lineCount) {
    lineCount.textContent = `${lineProgress.current} / ${lineProgress.total} 行`;
    lineCount.hidden = !state.showLineProgress;
  }
  if (percent) {
    percent.textContent = `${Math.round(Number(progress) || 0)}%`;
    percent.hidden = !state.showPercent;
  }
  if (ring) {
    const circumference = 2 * Math.PI * 22;
    const ratio = Math.max(0, Math.min(100, Number(progress) || 0)) / 100;
    ring.style.strokeDasharray = String(circumference);
    ring.style.strokeDashoffset = String(circumference * (1 - ratio));
    ring.setAttribute('aria-valuenow', String(Math.round(ratio * 100)));
    ring.closest('.progress-ring-wrap')?.toggleAttribute('hidden', !state.showPercent);
  }
  const lineToggle = $('show-line-progress');
  const percentToggle = $('show-percent-progress');
  if (lineToggle) lineToggle.checked = state.showLineProgress;
  if (percentToggle) percentToggle.checked = state.showPercent;
}

function setProgressDisplay(kind, enabled, notify = false) {
  if (kind === 'lines') state.showLineProgress = Boolean(enabled);
  if (kind === 'percent') state.showPercent = Boolean(enabled);
  try {
    localStorage.setItem(`wefish.progress.${kind}`, String(Boolean(enabled)));
  } catch (_) { /* private mode */ }
  updateProgressDisplay(state.bookProgress);
  if (state.pages.length) setTrayLine(state.lines[state.lineIndex] || currentPageText());
  if (notify) toast(`${kind === 'lines' ? '行数' : '百分比'}${enabled ? '已显示' : '已隐藏'}`);
}

function setTrayLine(text) {
  if (window.wefish && typeof window.wefish.setLine === 'function') {
    const rawText = String(text || '阅读器').replace(/\s+/g, ' ').trim() || '阅读器';
    const annotation = currentAnnotationContext();
    const position = readerPosition();
    const lineProgress = chapterLineProgress();
    window.wefish.setLine({
      text: formatTrayLine(rawText),
      rawText,
      paragraph: annotation.paragraph,
      widthMode: state.trayWidthMode,
      widthChars: state.trayWidthChars,
      bookId: state.currentBook?.bookId || '',
      bookTitle: state.currentBook?.title || '',
      chapterUid: state.chapters[state.chapterIndex]?.chapterUid || '',
      chapterName: state.chapters[state.chapterIndex]?.title || '',
      range: annotation.range,
      paragraphRange: annotation.paragraphRange,
      lineProgress: `${lineProgress.current} / ${lineProgress.total} 行`,
      percent: position.progress,
      showLineProgress: state.showLineProgress,
      showPercent: state.showPercent,
    });
  }
}

function currentPageText() {
  return String(state.pages[state.pageIndex]?.text || '').trim();
}

function displayWidth(value) {
  return Array.from(String(value || '')).length;
}

function sliceDisplayText(value, width) {
  let result = '';
  let used = 0;
  for (const character of Array.from(String(value || ''))) {
    if (used + 1 > width) break;
    result += character;
    used += 1;
  }
  return result;
}

function trayLineWidth() {
  return state.trayWidthMode === 'dynamic'
    ? (window.innerHeight < 400 ? 18 : 28)
    : state.trayWidthChars;
}

function formatTrayLine(text) {
  const value = String(text || '阅读器').replace(/\s+/g, ' ').trim() || '阅读器';
  if (state.trayWidthMode === 'dynamic') return value;
  const width = Math.max(12, state.trayWidthChars);
  const clipped = displayWidth(value) > width
    ? `${sliceDisplayText(value, Math.max(1, width - 1))}…`
    : value;
  return clipped + '\u00a0'.repeat(Math.max(0, width - displayWidth(clipped)));
}

function updateTrayWidthControls() {
  const mode = $('tray-width-mode');
  const range = $('tray-width-chars');
  const number = $('tray-width-number');
  const label = $('tray-width-value-label');
  const hint = $('tray-width-hint');
  if (!mode || !range || !number || !label || !hint) return;
  mode.value = state.trayWidthMode;
  range.value = String(state.trayWidthChars);
  number.value = String(state.trayWidthChars);
  const isDynamic = state.trayWidthMode === 'dynamic';
  range.disabled = isDynamic;
  number.disabled = isDynamic;
  label.textContent = state.trayWidthMode === 'fill' ? '填充上限' : '固定长度';
  hint.textContent = isDynamic
    ? '文字多长，顶部栏就占多长。'
    : state.trayWidthMode === 'fill'
      ? '会尽量占满菜单栏空余位置；数值是防止挤压系统图标的最大宽度。'
      : '短句补齐空白，长句按这个长度分页显示。';
}

function setTrayWidth(mode, chars, notify = false) {
  const nextMode = ['dynamic', 'fixed', 'fill'].includes(mode) ? mode : 'dynamic';
  const requested = Number(chars);
  const fallback = nextMode === 'fill' ? Math.max(64, state.trayWidthChars) : state.trayWidthChars;
  const nextChars = Math.max(12, Math.min(120, Math.round(Number.isFinite(requested) && requested > 0 ? requested : fallback)));
  state.trayWidthMode = nextMode;
  state.trayWidthChars = nextChars;
  state.lineIndex = 0;
  updateTrayWidthControls();
  try {
    localStorage.setItem('wefish.trayWidthMode', state.trayWidthMode);
    localStorage.setItem('wefish.trayWidthChars', String(state.trayWidthChars));
  } catch (_) { /* private mode */ }
  if (state.pages.length) updateLineDisplay();
  if (notify) {
    const message = nextMode === 'dynamic'
      ? '顶部栏宽度：跟随文本'
      : nextMode === 'fill'
        ? `顶部栏宽度：尽量占满（上限 ${nextChars} 字）`
        : `顶部栏宽度：固定 ${nextChars} 字`;
    toast(message);
  }
}

function updateLineDisplay() {
  const pageText = currentPageText();
  state.lines = WeFishPagination.splitLines(pageText, trayLineWidth());
  state.lineIndex = Math.max(0, Math.min(state.lineIndex, Math.max(0, state.lines.length - 1)));
  const line = state.lines[state.lineIndex] || pageText || '阅读器';
  setTrayLine(line);
  const button = $('line-toggle');
  button.classList.toggle('active', state.lineAuto);
  button.textContent = state.lineAuto ? '行▶' : '行Ⅱ';
  updateReaderPosition();
  schedulePositionSync();
}

function resetLineTimer() {
  clearInterval(state.lineTimer);
  state.lineTimer = null;
  if (!state.lineAuto) return;
  state.lineTimer = setInterval(() => {
    if (state.reading) nextLine(1, true);
  }, state.lineIntervalMs);
}

function setLineInterval(seconds, notify = false) {
  const value = Math.max(0.5, Math.min(10, Number(seconds) || 1));
  state.lineIntervalMs = value * 1000;
  const input = $('line-interval');
  const helpInput = $('help-line-interval');
  if (input) input.value = String(value);
  if (helpInput) helpInput.value = String(value);
  try { localStorage.setItem('wefish.lineInterval', String(value)); } catch (_) { /* private mode */ }
  resetLineTimer();
  if (notify) toast(`逐行速度：每 ${value} 秒换行`);
}

function setLineAuto(enabled) {
  state.lineAuto = Boolean(enabled);
  resetLineTimer();
  updateLineDisplay();
}

function nextLine(delta = 1, fromTimer = false) {
  if (!state.pages.length) return;
  const next = state.lineIndex + delta;
  if (next >= 0 && next < state.lines.length) {
    state.lineIndex = next;
    updateLineDisplay();
    return;
  }
  if (delta > 0 && state.pageIndex < state.pages.length - 1) {
    showPage(state.pageIndex + 1);
  } else if (delta < 0 && state.pageIndex > 0) {
    showPage(state.pageIndex - 1, { lineIndex: Number.MAX_SAFE_INTEGER });
  } else if (!fromTimer && delta > 0) {
    goNextChapter();
  }
}

function showPage(index, options = {}) {
  if (!state.pages.length) return;
  state.pageIndex = Math.max(0, Math.min(Number(index) || 0, state.pages.length - 1));
  const page = state.pages[state.pageIndex];
  const body = $('reader-body');
  const wrapper = document.createElement('div');
  wrapper.className = 'reader-page';
  wrapper.innerHTML = page.html;
  body.replaceChildren(wrapper);
  state.lineIndex = options.lineIndex === Number.MAX_SAFE_INTEGER ? Number.MAX_SAFE_INTEGER : Math.max(0, Number(options.lineIndex) || 0);
  state.pageCache.set(state.pageIndex, page);
  updateLineDisplay();
  preloadAroundPage();
}

function preloadAroundPage() {
  state.preloadedPages = [];
  for (let index = state.pageIndex + 1; index <= Math.min(state.pages.length - 1, state.pageIndex + 2); index += 1) {
    state.pageCache.set(index, state.pages[index]);
    state.preloadedPages.push(index);
  }
  const nextChapter = state.chapterIndex + 1;
  if (nextChapter < state.chapters.length) fetchChapterHtml(nextChapter).catch(() => {});
}

async function fetchChapterHtml(index) {
  if (!state.currentBook || index < 0 || index >= state.chapters.length) return '';
  if (state.chapterHtmlCache.has(index)) return state.chapterHtmlCache.get(index);
  const chapter = state.chapters[index];
  const html = await api('/api/reader/chapter', jsonBody({
    bookId: state.currentBook.bookId,
    chapterUid: chapter.chapterUid,
  }));
  state.chapterHtmlCache.set(index, String(html || ''));
  return String(html || '');
}

async function loadChapter(index, resume = null) {
  if (!state.currentBook || !state.chapters.length) return;
  await waitForReportIdle(900);
  await reportReading(true);
  const nextIndex = Math.max(0, Math.min(index, state.chapters.length - 1));
  const chapter = state.chapters[nextIndex];
  const generation = ++state.loadGeneration;
  state.chapterIndex = nextIndex;
  if ($('chapter-jump')) $('chapter-jump').value = String(nextIndex);
  state.pages = [];
  state.pageIndex = 0;
  state.pageCache.clear();
  state.chapterOffset = 0;
  state.chapterSummary = '';
  state.bookProgress = 0;
  updateReaderHeader(chapter);
  setStatus('加载正文中…');
  try {
    const html = await fetchChapterHtml(nextIndex);
    if (generation !== state.loadGeneration) return;
    const body = safeReaderHtml(html);
    state.pages = WeFishPagination.paginateFragment(body, {
      container: $('reader-body'),
      compact: window.innerHeight < 400,
    });
    state.chapterTextLength = Math.max(1, state.pages[state.pages.length - 1]?.endOffset || 1);
    const resumeOffset = Math.max(0, Number(resume?.chapterOffset || 0));
    state.pageIndex = state.pages.findIndex((page, index) => (
      resumeOffset >= page.startOffset
      && (resumeOffset < page.endOffset || index === state.pages.length - 1)
    ));
    if (state.pageIndex < 0) state.pageIndex = 0;
    state.bookProgress = Number(resume?.progress || state.currentBook.progress || 0);
    state.syncStatus = resume ? 'synced' : 'idle';
    let resumeLineIndex = 0;
    if (resume && state.pages[state.pageIndex]?.text) {
      const page = state.pages[state.pageIndex];
      const lineWidth = trayLineWidth();
      const lines = WeFishPagination.splitLines(page.text, lineWidth);
      const fraction = Math.max(0, Math.min(1,
        (resumeOffset - page.startOffset) / Math.max(1, page.text.length),
      ));
      resumeLineIndex = Math.floor(fraction * Math.max(0, lines.length - 1));
      if (resumeOffset > page.startOffset && lines.length > 1) resumeLineIndex = Math.max(1, resumeLineIndex);
    }
    showPage(state.pageIndex, { lineIndex: resumeLineIndex });
    updateReaderPosition();
    state.reading = true;
    if (window.location.hash !== '#self-test') {
      rememberBook({ ...state.currentBook, progress: state.bookProgress, chapterUid: state.chapters[state.chapterIndex]?.chapterUid });
    }
    setStatus('正在阅读', 'active');
    resetLineTimer();
  } catch (error) {
    state.reading = false;
    setStatus('正文暂时不可用', 'warn');
    $('reader-body').innerHTML = '<div class="empty-state"><div class="empty-glyph">!</div><h2>这章还没打开</h2><p></p></div>';
    $('reader-body p').textContent = error.message;
    toast(error.message, true);
  }
}

async function loadBook(rawBook) {
  const book = normalizeBook(rawBook);
  if (!book.bookId) return toast('这本书缺少 bookId', true);
  closeShelf();
  if (window.location.hash !== '#self-test') rememberBook(book);
  state.currentBook = book;
  state.chapters = [];
  state.chapterIndex = -1;
  state.pages = [];
  state.pageIndex = 0;
  state.pageCache.clear();
  state.chapterHtmlCache.clear();
  state.entitlement = {};
  state.membership = { status: 'unknown', source: 'web-reader-bootstrap', hasSnapshot: false };
  state.chapterOffset = 0;
  state.chapterSummary = '';
  state.bookProgress = 0;
  state.remoteProgress = null;
  state.syncStatus = 'idle';
  setStatus('打开书籍…');
  try {
    const data = await api('/api/reader/catalog', jsonBody({ bookId: book.bookId }));
    state.entitlement = data.entitlement || {};
    state.chapters = (data.chapters || []).filter((chapter) => Number(chapter.wordCount || 1) > 0 && chapter.title !== '封面');
    if (!state.chapters.length) throw new Error('没有找到可读章节');
    renderChapterJump();
    if (!book.isMp) {
      try {
        const rights = await api('/api/reader/entitlement', jsonBody({
          bookId: book.bookId,
          chapterUid: state.chapters[0].chapterUid,
        }));
        state.membership = rights.membership || state.membership;
      } catch (_) {
        // Older Web Reader bootstrap responses may omit the optional
        // membership snapshot; readable content remains usable.
      }
    }
    let remote = null;
    if (!book.isMp) {
      try {
        remote = await getApi(`/api/reader/progress?bookId=${encodeURIComponent(book.bookId)}`);
        state.remoteProgress = remote;
      } catch (_) {
        // A cookie-only session can read while the optional Gateway progress
        // endpoint is unavailable. Keep the shelf position and show local
        // status instead of turning a partial authorization into a login error.
      }
    }
    const linkResume = !book.isMp && book.readerUrl && (book.chapterUid || Number(book.chapterOffset || 0) > 0)
      ? book
      : null;
    const resume = remote || linkResume;
    const currentUid = String(resume?.chapterUid || book.chapterUid || '');
    const currentIndex = state.chapters.findIndex((chapter) => String(chapter.chapterUid) === currentUid);
    await loadChapter(currentIndex >= 0 ? currentIndex : 0, resume);
  } catch (error) {
    state.reading = false;
    setStatus('正文需要 Web Reader Cookie', 'warn');
    toast(`${error.message}；请扫码后重试`, true);
    showQR();
  }
}

function renderChapterJump() {
  const select = $('chapter-jump');
  select.replaceChildren();
  const first = document.createElement('option');
  first.value = '';
  first.textContent = '章节';
  select.appendChild(first);
  state.chapters.forEach((chapter, index) => {
    const option = document.createElement('option');
    option.value = String(index);
    option.textContent = `${index + 1}. ${chapter.title || `第${index + 1}章`}`;
    select.appendChild(option);
  });
  select.value = state.chapterIndex >= 0 ? String(state.chapterIndex) : '';
}

function insightCard(label, value, detail = '') {
  const card = document.createElement('div');
  card.className = 'insight-card';
  const title = document.createElement('span');
  title.className = 'insight-label';
  title.textContent = label;
  const main = document.createElement('strong');
  main.textContent = value;
  const note = document.createElement('small');
  note.textContent = detail;
  card.append(title, main, note);
  return card;
}

function statsReadingSeconds(stats) {
  const value = stats || {};
  return Number(value.totalReadTime ?? value.readingTime ?? value.totalReadingTime ?? value.wrReadTime ?? 0) || 0;
}

function statsReadingDays(stats) {
  const value = stats || {};
  return Number(value.readDays ?? value.readingDays ?? value.days ?? 0) || 0;
}

function membershipLabel(snapshot = state.membership) {
  const status = snapshot?.status;
  if (status === 'active') return '会员有效';
  if (status === 'inactive') return '会员已失效';
  if (status === 'not-applicable') return '公众号不适用';
  return '状态未返回';
}

function membershipDetail(snapshot = state.membership) {
  const remaining = Number(snapshot?.remainingSeconds);
  if (Number.isFinite(remaining) && remaining > 0) {
    return `已从 Web Reader 同步 · 约 ${Math.ceil(remaining / 86400)} 天`;
  }
  if (snapshot?.hasSnapshot) return '已从 Web Reader 同步';
  return '以账号实际可读权益为准';
}

function insightSection(title, rows, emptyText) {
  const section = document.createElement('section');
  section.className = 'insight-section';
  const heading = document.createElement('h3');
  heading.textContent = title;
  section.appendChild(heading);
  if (!rows.length) {
    const empty = document.createElement('p');
    empty.className = 'insight-empty';
    empty.textContent = emptyText;
    section.appendChild(empty);
    return section;
  }
  const list = document.createElement('div');
  list.className = 'insight-list';
  rows.slice(0, 8).forEach((row) => {
    const item = document.createElement('div');
    item.className = 'insight-row';
    const content = document.createElement('p');
    content.textContent = String(row.markText || row.content || row.abstract || row.range || '');
    item.appendChild(content);
    const chapter = row.chapterName || row.chapterUid;
    if (chapter !== undefined && chapter !== null && String(chapter)) {
      const meta = document.createElement('small');
      meta.textContent = `章节 ${chapter}`;
      item.appendChild(meta);
    }
    list.appendChild(item);
  });
  section.appendChild(list);
  return section;
}

async function showInsights() {
  openModal('<p class="insights-loading">正在读取阅读数据…</p>', '阅读数据');
  const statsResult = await Promise.allSettled([
    getApi('/api/stats?mode=weekly'),
    getApi('/api/stats?mode=monthly'),
    state.currentBook && !state.currentBook.isMp && state.chapterIndex >= 0
      ? api('/api/reader/annotations', jsonBody({
        bookId: state.currentBook.bookId,
        chapterUid: state.chapters[state.chapterIndex].chapterUid,
      }))
      : Promise.resolve(null),
  ]);
  const weeklyPayload = statsResult[0].status === 'fulfilled' ? statsResult[0].value : null;
  const monthlyPayload = statsResult[1].status === 'fulfilled' ? statsResult[1].value : null;
  const statsPayload = monthlyPayload || weeklyPayload;
  const annotations = statsResult[2].status === 'fulfilled' ? statsResult[2].value : null;
  const content = $('modal-content');
  content.replaceChildren();

  if (!statsPayload && !annotations) {
    const error = document.createElement('p');
    error.className = 'insight-empty';
    error.textContent = statsResult[1].reason?.message || statsResult[0].reason?.message || '阅读数据暂时不可用';
    content.appendChild(error);
  } else {
    const stats = statsPayload?.stats || {};
    const notes = statsPayload?.notes || {};
    const grid = document.createElement('div');
    grid.className = 'insight-grid';
    const weekly = weeklyPayload?.stats || {};
    const monthly = monthlyPayload?.stats || {};
    grid.append(
      insightCard('本周阅读时长', formatReadingTime(statsReadingSeconds(weekly)), '微信读书云端统计'),
      insightCard('本月阅读时长', formatReadingTime(statsReadingSeconds(monthly)), '微信读书云端统计'),
      insightCard('本月阅读天数', `${statsReadingDays(monthly)} 天`, '云端活跃天数'),
      insightCard('我的笔记', String(notes.totalNoteCount ?? notes.noteCount ?? 0), `${Number(notes.totalBookCount || 0)} 本书`),
      insightCard('会员权益', membershipLabel(), membershipDetail()),
    );
    content.appendChild(grid);

    if (state.currentBook) {
      const bookSection = document.createElement('section');
      bookSection.className = 'insight-section';
      const heading = document.createElement('h3');
      heading.textContent = state.currentBook.title;
      bookSection.appendChild(heading);
      const detail = document.createElement('p');
      detail.className = 'insight-empty';
      const entitlement = state.entitlement || {};
      const entitlementText = Object.prototype.hasOwnProperty.call(entitlement, 'has_paid_content')
        ? (entitlement.has_paid_content ? `目录含 ${entitlement.paid_chapters || 0} 个受限章节` : '当前目录未标记付费章节')
        : '公众号文章或尚未加载目录';
      detail.textContent = `当前进度 ${Math.round(Number(state.bookProgress || state.currentBook.progress || 0))}% · ${membershipLabel()} · ${entitlementText}`;
      bookSection.appendChild(detail);
      content.appendChild(bookSection);
    }

    if (annotations) {
      content.appendChild(insightSection('本章摘录', annotations.underlines || [], '当前章节还没有摘录。'));
      content.appendChild(insightSection('书友想法', annotations.reviews || [], '当前章节还没有书友想法。'));
      content.appendChild(insightSection('我的想法', annotations.my_reviews || [], '当前章节还没有我的想法。'));
    } else {
      content.appendChild(insightSection('本章摘录与想法', [], '打开普通书籍的一章后，这里会显示摘录、书友想法和我的想法。'));
    }
  }
  const actions = document.createElement('div');
  actions.className = 'modal-actions';
  const shelf = document.createElement('button');
  shelf.className = 'button ghost';
  shelf.textContent = '打开书架';
  shelf.addEventListener('click', async () => {
    closeModal();
    await openShelf();
  });
  actions.appendChild(shelf);
  const refresh = document.createElement('button');
  refresh.className = 'button primary';
  refresh.textContent = '刷新数据';
  refresh.addEventListener('click', showInsights);
  actions.appendChild(refresh);
  content.appendChild(actions);
}

async function highlightCurrent(kind = 'line') {
  if (!state.currentBook || state.currentBook.isMp || state.chapterIndex < 0) {
    toast('公众号文章暂不支持云端划线', true);
    return;
  }
  const annotation = currentAnnotationContext(kind === 'paragraph' ? 'paragraph' : 'line');
  if (!annotation.text) {
    toast('当前没有可划线的正文', true);
    return;
  }
  try {
    await api('/api/reader/highlight', jsonBody({
      bookId: state.currentBook.bookId,
      chapterUid: state.chapters[state.chapterIndex].chapterUid,
      chapterName: state.chapters[state.chapterIndex].title || '',
      range: annotation.range,
      markText: annotation.text,
      contextAbstract: annotation.paragraph,
      colorStyle: 0,
      style: 1,
    }));
    toast(`已云端划线${kind === 'paragraph' ? '当前段' : '当前行'}`);
  } catch (error) {
    toast(`划线未发送：${error.message}`, true);
  }
}

async function submitThought(content) {
  const text = String(content || '').trim();
  if (!text) return;
  if (!state.currentBook || state.currentBook.isMp || state.chapterIndex < 0) {
    toast('公众号文章暂不支持云端写想法', true);
    return;
  }
  const annotation = currentAnnotationContext('line');
  try {
    await api('/api/reader/thought', jsonBody({
      bookId: state.currentBook.bookId,
      chapterUid: state.chapters[state.chapterIndex].chapterUid,
      range: annotation.range,
      abstract: annotation.line,
      content: text,
      type: 1,
    }));
    toast('想法已同步到微信读书');
  } catch (error) {
    toast(`想法未发送：${error.message}`, true);
  }
}

function goPrevious() {
  if (state.pageIndex > 0) {
    showPage(state.pageIndex - 1, { lineIndex: Number.MAX_SAFE_INTEGER });
  } else {
    goPreviousChapter();
  }
}

function goNext() {
  if (state.pageIndex < state.pages.length - 1) {
    showPage(state.pageIndex + 1);
  } else {
    goNextChapter();
  }
}

function goPreviousChapter() {
  if (state.chapterIndex > 0) loadChapter(state.chapterIndex - 1);
}

function goNextChapter() {
  if (state.chapterIndex >= 0 && state.chapterIndex < state.chapters.length - 1) loadChapter(state.chapterIndex + 1);
}

async function jumpToProgress(value) {
  if (!state.chapters.length) return;
  await waitForReportIdle(900);
  await reportReading(true);
  const fraction = Math.max(0, Math.min(1, Number(value || 0) / 100));
  const target = Math.min(state.chapters.length - 1, Math.floor(fraction * state.chapters.length));
  await loadChapter(target);
  const local = fraction * state.chapters.length - target;
  showPage(Math.floor(Math.max(0, Math.min(0.999, local)) * state.pages.length));
  // A position jump is meaningful even when no additional reading seconds
  // have accumulated; force the new chapter/page offset to the cloud.
  await reportReading(true);
}

function loadPendingReports() {
  try {
    const value = JSON.parse(localStorage.getItem('wefish.pendingReports') || '[]');
    state.pendingReports = Array.isArray(value) ? value.slice(-12) : [];
  } catch (_) {
    state.pendingReports = [];
  }
}

function savePendingReports() {
  try { localStorage.setItem('wefish.pendingReports', JSON.stringify(state.pendingReports.slice(-12))); } catch (_) { /* private mode */ }
}

async function flushPendingReports(timeoutMs = 0) {
  if (state.reportInFlight || !state.pendingReports.length) return;
  state.reportInFlight = true;
  try {
    while (state.pendingReports.length) {
      const options = jsonBody(state.pendingReports[0]);
      if (timeoutMs > 0) options.timeoutMs = timeoutMs;
      await api('/api/reader/report', options);
      state.pendingReports.shift();
      savePendingReports();
    }
    state.syncStatus = 'synced';
  } catch (_) {
    state.syncStatus = 'pending';
  } finally {
    state.reportInFlight = false;
  }
}

function waitForReportIdle(timeoutMs = 900) {
  if (!state.reportInFlight) return Promise.resolve();
  return new Promise((resolve) => {
    const started = Date.now();
    const check = () => {
      if (!state.reportInFlight || Date.now() - started >= timeoutMs) {
        resolve();
        return;
      }
      setTimeout(check, 25);
    };
    check();
  });
}

function schedulePositionSync() {
  clearTimeout(state.positionSyncTimer);
  state.positionSyncTimer = null;
  if (!state.currentBook || !state.reading || state.currentBook.isMp || state.chapterIndex < 0) return;
  state.positionSyncTimer = setTimeout(() => {
    state.positionSyncTimer = null;
    if (state.reportInFlight) {
      state.positionSyncQueued = true;
      return;
    }
    reportReading(true);
  }, 450);
}

async function reportReading(force = false, deadlineMs = 0) {
  if (!state.currentBook || !state.reading || state.chapterIndex < 0 || state.currentBook.isMp) return;
  if (state.reportInFlight) {
    state.positionSyncQueued = true;
    return;
  }
  if (state.pendingReports.length) {
    const timeoutMs = deadlineMs ? Math.max(0, deadlineMs - Date.now()) : 0;
    await flushPendingReports(timeoutMs);
  }
  const delta = Math.floor(state.elapsed - state.reportedElapsed);
  if (!force && delta < 30) return;
  if (delta <= 0 && !force) return;
  const chapter = state.chapters[state.chapterIndex];
  const position = readerPosition();
  state.chapterOffset = position.offset;
  state.chapterSummary = position.summary;
  state.bookProgress = position.progress;
  const payload = {
      bookId: state.currentBook.bookId,
      chapterUid: chapter.chapterUid,
      chapterIdx: chapter.chapterIdx || state.chapterIndex + 1,
      chapterOffset: position.offset,
      progress: position.progress || state.currentBook.progress || 0,
      summary: position.summary,
      elapsed: delta,
    };
  state.reportInFlight = true;
  try {
    const options = jsonBody(payload);
    const timeoutMs = deadlineMs ? Math.max(0, deadlineMs - Date.now()) : 0;
    if (timeoutMs > 0) options.timeoutMs = timeoutMs;
    await api('/api/reader/report', options);
    state.reportedElapsed = state.elapsed;
    state.syncStatus = 'synced';
    updateReaderHeader(chapter);
  } catch (error) {
    state.pendingReports.push(payload);
    savePendingReports();
    state.reportedElapsed = state.elapsed;
    state.syncStatus = 'pending';
    updateReaderHeader(chapter);
    if (force) toast(`阅读时长暂未上报：${error.message}`, true);
  } finally {
    state.reportInFlight = false;
    if (state.positionSyncQueued) {
      state.positionSyncQueued = false;
      schedulePositionSync();
    }
  }
}

function tick() {
  // The menu-bar line is still an active reading surface while the capsule
  // is hidden, so its elapsed time must not depend on BrowserWindow visibility.
  if (state.reading && state.currentBook) {
    state.elapsed += 1;
    $('duration').textContent = formatDuration(state.elapsed);
    reportReading(false);
  } else if (state.pendingReports.length) {
    flushPendingReports();
  }
}

function openModal(content, title = '扫码进入') {
  $('qr-modal').classList.remove('hidden');
  $('help-modal').classList.add('hidden');
  $('modal-title').textContent = title;
  $('modal-content').replaceChildren();
  $('modal-content').insertAdjacentHTML('beforeend', content);
  $('modal-backdrop').classList.remove('hidden');
}

function showHelp() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  $('help-line-interval').value = String(state.lineIntervalMs / 1000);
  updateTrayWidthControls();
  updateProgressDisplay(state.bookProgress);
  $('qr-modal').classList.add('hidden');
  $('help-modal').classList.remove('hidden');
  $('modal-backdrop').classList.remove('hidden');
}

function closeModal() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  $('modal-backdrop').classList.add('hidden');
  $('qr-modal').classList.remove('hidden');
  $('help-modal').classList.add('hidden');
}

function showManualAuth() {
  openModal(`
    <p class="modal-note">正文阅读需要 Web Reader Cookie；书架同步还需要可选的 Skill API Key。两者可以分别填写，凭据会保存在系统钥匙串中。</p>
    <input class="modal-input" id="api-key-input" autocomplete="off" placeholder="Skill API Key（可选）" />
    <input class="modal-input" id="cookie-input" autocomplete="off" placeholder="Web Reader Cookie，例如 wr_vid=…; wr_skey=…" />
    <div class="modal-actions"><button class="button ghost" id="manual-cancel">取消</button><button class="button primary" id="manual-save">保存并验证</button></div>
  `, '手动授权');
  $('manual-cancel').addEventListener('click', closeModal);
  $('manual-save').addEventListener('click', async () => {
    const apiKey = $('api-key-input').value.trim();
    const cookie = $('cookie-input').value.trim();
    try {
      await api('/api/auth/manual', jsonBody({ apiKey, cookie }));
      closeModal();
      toast('授权已保存');
      await loadState({ openShelfOnSuccess: true });
    } catch (error) {
      toast(error.message, true);
    }
  });
}

function showReaderLink() {
  openModal(`
    <p class="modal-note">粘贴微信读书网页版的阅读地址。这个入口只依赖 Web Reader Cookie，不要求先拿到 Skill API Key。</p>
    <input class="modal-input" id="reader-link-input" autocomplete="off" placeholder="https://weread.qq.com/web/reader/…" />
    <div class="modal-actions"><button class="button ghost" id="link-cancel">取消</button><button class="button primary" id="link-open">打开正文</button></div>
  `, '打开阅读链接');
  $('link-cancel').addEventListener('click', closeModal);
  $('link-open').addEventListener('click', async () => {
    try {
      const book = await api('/api/reader/import', jsonBody({ url: $('reader-link-input').value.trim() }));
      closeModal();
      await loadBook(book);
    } catch (error) {
      toast(error.message, true);
    }
  });
  $('reader-link-input').focus();
}

async function showQR() {
  clearInterval(state.pollTimer);
  openModal(`
    <img class="qr-image" id="qr-image" alt="微信读书登录二维码" />
    <p class="modal-status" id="qr-status">正在生成二维码…</p>
    <input class="modal-input" id="otp-input" inputmode="numeric" maxlength="8" placeholder="若手机显示验证码，在这里输入（可选）" />
    <div class="modal-actions"><button class="button ghost" id="qr-retry">刷新二维码</button><button class="button primary" id="qr-manual">手动授权</button></div>
    <p class="modal-note">请用微信读书 App 扫描。扫码成功后会先保存 Web Reader Cookie；Skill/API Key 失败不会影响正文阅读。</p>
  `, '微信扫码登录');
  $('qr-retry').addEventListener('click', showQR);
  $('qr-manual').addEventListener('click', showManualAuth);
  try {
    const started = await api('/api/qr/begin', jsonBody());
    state.qrUid = started.uid;
    $('qr-image').src = started.qrDataUrl;
    $('qr-status').textContent = '等待手机确认…';
    state.pollTimer = setInterval(pollQR, 2000);
    await pollQR();
  } catch (error) {
    $('qr-status').textContent = error.message;
    $('qr-status').style.color = 'var(--coral)';
  }
}

async function pollQR() {
  if (!state.qrUid || $('modal-backdrop').classList.contains('hidden')) return;
  try {
    const response = await api('/api/qr/poll', jsonBody({ uid: state.qrUid, otp: $('otp-input')?.value.trim() || '' }));
    const login = response.login || {};
    if (login.succeed) {
      clearInterval(state.pollTimer);
      const needsSkill = Boolean(response.account?.needs_skill);
      closeModal();
      await loadState({ openShelfOnSuccess: true });
      toast(needsSkill ? '扫码成功；正文可读，书架同步请启用 Skill' : '扫码成功，已登录');
      return;
    }
    if (login.transport_pending || login.poll_auth_pending) {
      $('qr-status').textContent = '等待手机确认…';
    } else if (login.need_otp || login.code === 'NEED_OTP') {
      $('qr-status').textContent = '请输入手机上显示的验证码';
    } else if (login.code === 'OTP_NOT_MATCH') {
      $('qr-status').textContent = '验证码不匹配，请重新输入';
    } else if (login.expired || login.code === 'EXPIRED') {
      $('qr-status').textContent = '二维码已过期，请刷新';
      clearInterval(state.pollTimer);
    } else if (login.message || login.errmsg) {
      $('qr-status').textContent = String(login.message || login.errmsg);
    }
  } catch (error) {
    $('qr-status').textContent = error.message;
  }
}

async function loadState({ openShelfOnSuccess = false } = {}) {
  clearTimeout(state.restoreTimer);
  state.restoreTimer = null;
  try {
    const value = await api('/api/state');
    if (value.credentialsLoading) {
      setStatus('正在恢复登录…');
      clearTimeout(state.restoreTimer);
      state.restoreTimer = setTimeout(loadState, 700);
      return;
    }
    if (value.credentialsError) {
      setStatus('登录恢复超时，请点击同步书架重试', 'warn');
      return;
    }
    if (value.hasWebCookie) {
      setStatus(value.accountName ? `${value.accountName} · Web Reader 已连接` : 'Web Reader 已连接', 'active');
    } else if (value.hasGatewayKey) {
      setStatus('Skill 已连接；正文还需要 Cookie', 'warn');
    } else {
      setStatus('准备就绪');
    }
    const signedIn = Boolean(value.hasWebCookie || value.hasGatewayKey);
    const qrButton = $('qr-button');
    if (qrButton) qrButton.classList.toggle('hidden', signedIn);
    if (openShelfOnSuccess && signedIn) await openShelf();
  } catch (error) {
    setStatus('连接本地服务失败', 'warn');
    toast(error.message, true);
  }
}

function bindEvents() {
  $('open-shelf').addEventListener('click', openShelf);
  $('open-insights').addEventListener('click', showInsights);
  $('open-help').addEventListener('click', showHelp);
  $('close-shelf').addEventListener('click', closeShelf);
  $('hide-panel').addEventListener('click', () => window.wefish.hide());
  $('sync-button').addEventListener('click', () => loadShelf({ force: true }));
  $('shelf-search').addEventListener('input', (event) => {
    state.shelfQuery = event.target.value;
    renderShelfList();
  });
  $('insights-button').addEventListener('click', showInsights);
  $('manual-button').addEventListener('click', showManualAuth);
  $('qr-button').addEventListener('click', showQR);
  $('shelf-button').addEventListener('click', openShelf);
  $('link-button').addEventListener('click', showReaderLink);
  $('help-button').addEventListener('click', showHelp);
  $('close-modal').addEventListener('click', closeModal);
  $('close-help').addEventListener('click', closeModal);
  $('previous-button').addEventListener('click', goPrevious);
  $('next-button').addEventListener('click', goNext);
  $('chapter-jump').addEventListener('change', (event) => {
    if (event.target.value !== '') loadChapter(Number(event.target.value));
  });
  $('position-slider').addEventListener('change', (event) => jumpToProgress(event.target.value));
  $('line-toggle').addEventListener('click', () => setLineAuto(!state.lineAuto));
  $('line-interval').addEventListener('change', (event) => setLineInterval(event.target.value, true));
  $('help-line-interval').addEventListener('change', (event) => setLineInterval(event.target.value, true));
  $('show-line-progress').addEventListener('change', (event) => setProgressDisplay('lines', event.target.checked, true));
  $('show-percent-progress').addEventListener('change', (event) => setProgressDisplay('percent', event.target.checked, true));
  $('tray-width-mode').addEventListener('change', (event) => setTrayWidth(event.target.value, $('tray-width-number').value, true));
  $('tray-width-chars').addEventListener('input', (event) => setTrayWidth(state.trayWidthMode, event.target.value));
  $('tray-width-number').addEventListener('change', (event) => setTrayWidth(state.trayWidthMode, event.target.value, true));
  $('toggle-reading').addEventListener('click', () => {
    state.reading = !state.reading;
    setLineAuto(state.reading);
    $('toggle-reading em').textContent = state.reading ? '阅读中' : '已暂停';
    setStatus(state.reading ? '正在阅读' : '阅读已暂停', state.reading ? 'active' : '');
  });
  window.addEventListener('keydown', (event) => {
    if (!(event.metaKey || event.ctrlKey) || !event.altKey) return;
    const key = event.key;
    if (key === 'ArrowLeft') { event.preventDefault(); goPrevious(); }
    if (key === 'ArrowRight') { event.preventDefault(); goNext(); }
    if (key === 'ArrowUp') { event.preventDefault(); nextLine(-1); }
    if (key === 'ArrowDown') { event.preventDefault(); nextLine(1); }
    if (key === ' ') { event.preventDefault(); setLineAuto(!state.lineAuto); }
  });
  $('modal-backdrop').addEventListener('click', (event) => {
    if (event.target === $('modal-backdrop')) closeModal();
  });
  window.wefish.onCommand((command) => {
    const action = typeof command === 'string' ? command : command?.type;
    if (action === 'sync') loadShelf();
    if (action === 'page-previous') goPrevious();
    if (action === 'page-next') goNext();
    if (action === 'chapter-previous') goPreviousChapter();
    if (action === 'chapter-next') goNextChapter();
    if (action === 'line-previous') nextLine(-1);
    if (action === 'line-next') nextLine(1);
    if (action === 'line-toggle') setLineAuto(!state.lineAuto);
    if (action === 'help') showHelp();
    if (action === 'highlight-line') highlightCurrent('line');
    if (action === 'highlight-paragraph') highlightCurrent('paragraph');
    if (action === 'thought-submit') submitThought(command.text);
    if (action === 'progress-lines:toggle') setProgressDisplay('lines', !state.showLineProgress, true);
    if (action === 'progress-percent:toggle') setProgressDisplay('percent', !state.showPercent, true);
    if (typeof action !== 'string') return;
    if (action.startsWith('line-interval:')) setLineInterval(action.split(':', 2)[1], true);
    if (action.startsWith('tray-width:')) {
      const [, mode, chars] = action.split(':');
      if (mode === 'settings') {
        showHelp();
        requestAnimationFrame(() => $('tray-width-mode')?.focus());
      } else {
        setTrayWidth(mode, chars, true);
      }
    }
    if (command === 'qr') showQR();
  });
  window.wefish.onVisibility((visible) => {
    state.visible = visible;
    if (!visible) reportReading(true);
  });
  window.wefish.onFlushReading(() => {
    if (state.flushStarted) return;
    state.flushStarted = true;
    (async () => {
      const deadlineMs = Date.now() + 1700;
      await waitForReportIdle(Math.min(900, Math.max(0, deadlineMs - Date.now())));
      await reportReading(true, deadlineMs);
      await flushPendingReports(Math.max(0, deadlineMs - Date.now()));
    })().finally(() => window.wefish.readingFlushed());
  });
  window.wefish.onBridgeError((message) => {
    setStatus('阅读服务启动失败', 'warn');
    toast(message, true);
  });
}

async function boot() {
  bindEvents();
  loadPendingReports();
  loadRecentBooks();
  renderShelfList();
  try {
    const stored = Number(localStorage.getItem('wefish.lineInterval') || 1);
    if (stored >= 0.5 && stored <= 10) {
      setLineInterval(stored);
    }
  } catch (_) { /* private mode */ }
  try {
    const mode = localStorage.getItem('wefish.trayWidthMode') || 'dynamic';
    const chars = Number(localStorage.getItem('wefish.trayWidthChars') || 28);
    setTrayWidth(mode, chars);
  } catch (_) { /* private mode */ }
  try {
    const storedLines = localStorage.getItem('wefish.progress.lines');
    const storedPercent = localStorage.getItem('wefish.progress.percent');
    if (storedLines !== null) state.showLineProgress = storedLines !== 'false';
    if (storedPercent !== null) state.showPercent = storedPercent !== 'false';
  } catch (_) { /* private mode */ }
  updateProgressDisplay(0);
  state.timer = setInterval(tick, 1000);
  $('duration').textContent = formatDuration(state.elapsed);
  try {
    state.baseUrl = await waitForBridge();
    // Keep startup responsive: shelf sync is user initiated, so a slow network
    // or a locked credential store cannot hold the first screen behind it.
    await loadState({ openShelfOnSuccess: false });
  } catch (error) {
    setStatus('本地服务未启动', 'warn');
    toast(error.message, true);
  }
}

if (window.location.hash === '#self-test') {
  window.__wefishTest = {
    async openMock() {
      await loadBook({ bookId: 'wf-001', title: '山谷来信', author: '合成作者', progress: 12 });
      return {
        pageCount: state.pages.length,
        pageIndex: state.pageIndex,
        lineIndex: state.lineIndex,
        resumeOffset: state.chapterOffset,
        preloadedPages: state.preloadedPages.slice(),
        overflow: getComputedStyle($('reader-body')).overflow,
        bodyHeight: $('reader-body').clientHeight,
        bodyWidth: $('reader-body').clientWidth,
        textLength: state.chapterTextLength,
        chapterCount: state.chapters.length,
        reading: state.reading,
        status: $('status-text').textContent,
        line: state.lines[state.lineIndex] || '',
        lineProgress: $('progress-line-count')?.textContent || '',
        membership: state.membership,
      };
    },
    nextPage() {
      goNext();
      return { pageIndex: state.pageIndex, pageCount: state.pages.length };
    },
    nextLine() {
      nextLine(1);
      return { lineIndex: state.lineIndex, line: state.lines[state.lineIndex] || '' };
    },
    async jumpToProgress(value) {
      await jumpToProgress(value);
      return { chapterIndex: state.chapterIndex, pageIndex: state.pageIndex, pageCount: state.pages.length };
    },
    async openInsights() {
      await showInsights();
      return $('modal-content').textContent || '';
    },
    async checkTrayWidths() {
      const original = { mode: state.trayWidthMode, chars: state.trayWidthChars };
      const snapshots = [];
      for (const [mode, chars] of [['dynamic', 28], ['fixed', 24], ['fill', 64]]) {
        setTrayWidth(mode, chars);
        snapshots.push({
          mode: state.trayWidthMode,
          chars: state.trayWidthChars,
          storedMode: localStorage.getItem('wefish.trayWidthMode'),
          storedChars: localStorage.getItem('wefish.trayWidthChars'),
        });
      }
      setTrayWidth(original.mode, original.chars);
      return snapshots;
    },
    checkShelfSearch() {
      const original = {
        books: state.shelfBooks,
        recent: state.recentBooks,
        query: state.shelfQuery,
        loaded: state.shelfLoaded,
        input: $('shelf-search').value,
      };
      state.shelfBooks = [
        normalizeBook({ bookId: 'wf-001', title: '山谷来信', author: '合成作者' }),
        normalizeBook({ bookId: 'wf-002', title: '海边札记', author: '另一位作者' }),
      ];
      state.recentBooks = [normalizeBook({ bookId: 'wf-001', title: '山谷来信', author: '合成作者', lastReadAt: Date.now() })];
      state.shelfLoaded = true;
      state.shelfQuery = '山谷';
      $('shelf-search').value = state.shelfQuery;
      renderShelfList();
      const matched = $('shelf-list').querySelectorAll('.shelf-item').length;
      const recentCount = $('recent-list').querySelectorAll('.shelf-item').length;
      state.shelfBooks = original.books;
      state.recentBooks = original.recent;
      state.shelfQuery = original.query;
      state.shelfLoaded = original.loaded;
      $('shelf-search').value = original.input;
      renderShelfList();
      return { matched, recentCount, total: 2 };
    },
  };
}

boot();
